#!/usr/bin/env python3
"""Validate a whole relay plan before replacing segments and caption events."""

from __future__ import annotations

import argparse
import copy
import math
import sys
from pathlib import Path

import cs_relay


def endpoint(manifest: dict, player: str, state: str) -> str:
    record = cs_relay._find_state(manifest, cs_relay.slug(player), cs_relay.slug(state))
    if not record or not isinstance(record.get("endpoint"), dict):
        raise cs_relay.RelayError(f"缺少 endpoint：{player}/{state}")
    return record["endpoint"]["path"]


def node_endpoint(manifest: dict, node: dict, role: str) -> str:
    direct = node.get(f"{role}_path")
    if direct:
        if not isinstance(direct, str):
            raise cs_relay.RelayError(f"{node.get('player')}/{role}_path 必须是字符串")
        return direct
    state = node.get(role)
    if not isinstance(state, str):
        raise cs_relay.RelayError(f"{node.get('player')} 缺少 {role} 或 {role}_path")
    return endpoint(manifest, node["player"], state)


def prompt_from_plan(plan: dict, plan_base: Path, kind: str) -> str:
    key = f"{kind}_prompt_file"
    if not isinstance(plan.get(key), str) or not plan[key]:
        raise cs_relay.RelayError(f"chain plan 缺少 {key}；历史 report 不能充当模板")
    path = (plan_base / plan[key]).resolve()
    if not path.is_file():
        raise cs_relay.RelayError(f"prompt 模板不存在：{path}")
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise cs_relay.RelayError(f"prompt 模板为空：{path}")
    return prompt


def positive_number(plan: dict, key: str, *, integer: bool = False) -> int | float:
    value = plan.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 or (integer and not isinstance(value, int)):
        raise cs_relay.RelayError(f"chain plan {key} 必须是正{'整数' if integer else '数'}")
    return value


def build(job_dir: Path, plan_path: Path) -> None:
    plan = cs_relay.load_json(plan_path)
    if not isinstance(plan, dict):
        raise cs_relay.RelayError("chain plan 必须是 JSON object")
    manifest = copy.deepcopy(cs_relay.load_manifest(job_dir))
    nodes = plan.get("nodes")
    if not isinstance(nodes, list) or not nodes or any(not isinstance(n, dict) for n in nodes):
        raise cs_relay.RelayError("chain plan nodes 必须是非空 object 列表")
    terminal = plan.get("terminal_age_transition", False)
    terminal_enabled = terminal.get("enabled", False) if isinstance(terminal, dict) else terminal
    if not isinstance(terminal_enabled, bool):
        raise cs_relay.RelayError("terminal_age_transition 必须是布尔值或含布尔 enabled 的 object")
    if len(nodes) == 1 and not terminal_enabled:
        raise cs_relay.RelayError("单人计划需要 terminal_age_transition=true")
    fps = positive_number(plan, "fps")
    frames = positive_number(plan, "segment_frames", integer=True)
    duration = positive_number(plan, "duration_seconds_requested")
    width = positive_number(plan, "generation_width", integer=True)
    height = positive_number(plan, "generation_height", integer=True)
    cs_relay.h3_frame_length(frames, duration, fps)
    if width % 32 or height % 32:
        raise cs_relay.RelayError("H3 generation_width/generation_height 必须是 32 的正倍数")
    seed_base = plan.get("seed_base")
    segment_count = 2 * (len(nodes) - 1) + int(terminal_enabled)
    if isinstance(seed_base, bool) or not isinstance(seed_base, int) or seed_base < 0 or seed_base + segment_count >= 2**64:
        raise cs_relay.RelayError("chain plan seed_base 必须是非负整数且生成种子小于 2^64")
    age_prompt = prompt_from_plan(plan, plan_path.parent, "age")
    handoff_prompt = prompt_from_plan(plan, plan_path.parent, "handoff") if len(nodes) > 1 else ""
    caption_policy = plan.get("caption_policy", manifest.get("caption_policy", {}))
    if not isinstance(caption_policy, dict):
        raise cs_relay.RelayError("caption_policy 必须是 object")
    prepared_nodes = []
    for index, node in enumerate(nodes):
        player = node.get("player")
        if not isinstance(player, str) or cs_relay.slug(player) != player:
            raise cs_relay.RelayError(f"nodes[{index}].player 必须是安全 slug")
        roles = ["current", "past"] if index < len(nodes) - 1 or terminal_enabled else ["current"]
        prepared = dict(node)
        for role in roles:
            caption = node.get(f"{role}_caption")
            if not isinstance(caption, str) or not caption.strip():
                raise cs_relay.RelayError(f"{player} 缺少 {role}_caption")
            path = node_endpoint(manifest, node, role)
            if not cs_relay.rel_path(job_dir, path).is_file():
                raise cs_relay.RelayError(f"endpoint 文件不存在：{path}")
            prepared[f"{role}_path"] = path
        prepared_nodes.append(prepared)
    segments, events = [], []
    segment_seconds = frames / fps

    def append_segment(node: dict, next_node: dict | None = None) -> None:
        order = len(segments) + 1
        first_role, last_role = ("past", "current") if next_node else ("current", "past")
        target = next_node or node
        suffix = f"to-{target['player']}" if next_node else "age"
        caption_a, caption_b = node[f"{first_role}_caption"], target[f"{last_role}_caption"]
        kind = "player_handoff" if next_node else "age_transition"
        segments.append({
            "id": f"{order:02d}-{node['player']}-{suffix}", "kind": kind, "workflow": kind,
            "first_frame": node[f"{first_role}_path"], "last_frame": target[f"{last_role}_path"],
            "action_images": [], "prompt": handoff_prompt if next_node else age_prompt,
            "seed": seed_base + order, "length": frames, "duration_s": duration, "fps": fps,
            "generation_width": width, "generation_height": height, "generation_fps": fps,
            "caption": f"{caption_a} → {caption_b}",
        })
        start = (order - 1) * segment_seconds
        events.extend([
            {"start": start, "end": start + segment_seconds / 2, "text": caption_a},
            {"start": start + segment_seconds / 2, "end": start + segment_seconds, "text": caption_b},
        ])

    for index, node in enumerate(prepared_nodes[:-1]):
        append_segment(node)
        append_segment(node, prepared_nodes[index + 1])
    if terminal_enabled:
        append_segment(prepared_nodes[-1])
    manifest["segments"] = segments
    manifest["caption_policy"] = caption_policy
    errors = cs_relay.strict_asset_errors(job_dir, manifest)
    if errors:
        raise cs_relay.RelayError("计划资产校验失败；原任务未改变：\n- " + "\n- ".join(errors))
    manifest["timeline_contract"] = {
        "segment_frames": frames, "fps": fps, "segment_seconds": segment_seconds,
        "total_segments": len(segments), "total_frames": len(segments) * frames,
        "total_seconds": len(segments) * segment_seconds,
        "full_cycles": len(nodes) - 1, "half_cycles": int(terminal_enabled), "postprocess": "none",
    }
    # Plan changes invalidate review and previous execution state.
    manifest.pop("visual_review", None)
    manifest["artifacts"] = {}
    manifest["status"] = "local_collected"
    cs_relay.save_json(job_dir / "captions/events.json", events)
    cs_relay.save_manifest(job_dir, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_dir")
    parser.add_argument("plan")
    args = parser.parse_args(argv)
    try:
        build(Path(args.job_dir).resolve(), Path(args.plan).resolve())
        return 0
    except (cs_relay.RelayError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
