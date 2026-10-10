#!/usr/bin/env python3
"""Mac-side controller for the CS relay local/remote workflow.

The Mac owns browser evidence, player metadata, captions, and final assembly.
An explicitly configured remote host owns ComfyUI generation. This module uses the
Python standard library so it can run on a clean macOS installation.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "cs-relay.job.v1"
DEFAULT_VIDEO = {"width": 544, "height": 736, "fps": 24}
ASSET_POLICY_VERSION = "hltv-ranking-snapshot-portrait.v3"
USER_ASSET_POLICY_VERSION = "user-provided-past-present.v1"
DEFAULT_CONFIG = {
    "remote_host": None,
    "remote_project_root": None,
    "remote_jobs_root": "input/cs_relay_jobs",
    "remote_results_root": "output/cs_relay_jobs",
    "remote_comfyui_url": "http://127.0.0.1:8188",
    "default_video": DEFAULT_VIDEO,
}
PORTRAIT_ACQUISITION_METHOD = "hltv_ranking_snapshot_team_card.v1"
PORTRAIT_OVERRIDE_METHOD = "reviewed_reference_override.v1"
ALLOWED_PORTRAIT_ACQUISITION_METHODS = {
    PORTRAIT_ACQUISITION_METHOD,
    PORTRAIT_OVERRIDE_METHOD,
}
ALLOWED_PORTRAIT_KINDS = {
    "hltv_ranking_snapshot_playerbodyshot",
    "hltv_profile_playerbodyshot",
    "official_media_day_portrait",
    "user_provided_portrait",
}
ALLOWED_PORTRAIT_PROVIDERS = {"hltv", "official_media_day", "user"}
TEAMLESS_CURRENT_TEAMS = {"none", "retired"}
HLTV_PROFILE_RE = re.compile(
    r"^https://www\.hltv\.org/player/\d+(?:/[^?#]+)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)
HLTV_RANKING_SNAPSHOT_RE = re.compile(
    r"^https://www\.hltv\.org/ranking/teams/"
    r"(?P<year>\d{4})/(?P<month>[a-z]+)/(?P<day>\d{1,2})/?(?:[?#].*)?$",
    re.IGNORECASE,
)
HLTV_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ),
        start=1,
    )
}
FORBIDDEN_GENERATION_SOURCE_TOKENS = {
    "top20",
    "top-20",
    "ranking-card",
    "evidence-only",
    "event-photo",
    "event-evidence",
    "gallerypicture",
    "infographic",
    "news-photo",
}


def _ranking_snapshot_parts(url: object) -> tuple[int, int, int] | None:
    match = HLTV_RANKING_SNAPSHOT_RE.fullmatch(str(url or "").strip())
    if not match:
        return None
    year = int(match.group("year"))
    month = HLTV_MONTHS.get(match.group("month").casefold())
    day = int(match.group("day"))
    if month is None:
        return None
    try:
        dt.date(year, month, day)
    except ValueError:
        return None
    return year, month, day


def _portrait_source_violation(path: object, source_url: object) -> str | None:
    """Return a forbidden direct-image source token.

    The proof page is checked separately and must be a dated HLTV ranking
    snapshot. The image itself must be the playerbodyshot referenced by that
    snapshot's five-player team card.
    """
    return _contains_forbidden_source_token(path, source_url)
STATUSES = {
    "draft",
    "local_collected",
    "package_ready",
    "remote_submitted",
    "remote_generated",
    "local_assembled",
    "validated",
    "blocked",
}


class RelayError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def slug(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    if not result or result in {".", ".."}:
        raise RelayError(f"无法把 {value!r} 转成安全的 slug")
    return result.lower()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RelayError(f"文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise RelayError(f"JSON 无法解析：{path}: {exc}") from exc


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel_path(job_dir: Path, value: str | Path) -> Path:
    """Resolve a manifest path and reject absolute/path-traversal references."""

    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise RelayError(f"manifest 路径必须是任务目录内的相对路径：{value}")
    resolved = (job_dir / raw).resolve()
    if not resolved.is_relative_to(job_dir.resolve()):
        raise RelayError(f"manifest 路径通过符号链接逃出任务目录：{value}")
    return resolved


def load_manifest(job_dir: Path) -> dict[str, Any]:
    manifest = load_json(job_dir / "job.json")
    if not isinstance(manifest, dict):
        raise RelayError("job.json 必须是 JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RelayError(
            f"不支持的 schema_version：{manifest.get('schema_version')!r}"
        )
    if manifest.get("job_id") != job_dir.name:
        raise RelayError(
            f"job_id={manifest.get('job_id')!r} 与目录名 {job_dir.name!r} 不一致"
        )
    return manifest


def save_manifest(job_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = now_iso()
    save_json(job_dir / "job.json", manifest)


def default_manifest(job_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "status": "draft",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "remote": {
            "adapter": "comfyui-h3",
            "comfyui_url": "http://127.0.0.1:8188",
        },
        "players": [],
        "segments": [],
        "asset_policy": {
            "version": ASSET_POLICY_VERSION,
            "strict": True,
            "portrait_acquisition_method": PORTRAIT_ACQUISITION_METHOD,
            "reviewed_override_method": PORTRAIT_OVERRIDE_METHOD,
            "required_age_periods": ["current", "peak"],
            "allowed_portrait_kinds": sorted(ALLOWED_PORTRAIT_KINDS),
            "allowed_portrait_providers": sorted(ALLOWED_PORTRAIT_PROVIDERS),
            "require_exact_team_year": True,
            "require_portrait_evidence": True,
            "require_registered_endpoint": True,
        },
        "caption_policy": {
            "language": "zh-CN",
            "format": "srt",
            "burn_in": True,
        },
        "artifacts": {},
    }


def cmd_init(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    job_id = slug(args.job_id or job_dir.name)
    if job_id != job_dir.name:
        raise RelayError("任务目录名必须与安全的 job_id 相同")
    config = load_remote_config(job_dir, getattr(args, "config", None))
    if job_dir.exists() and any(job_dir.iterdir()) and not args.force:
        raise RelayError(f"任务目录非空，拒绝覆盖：{job_dir}（如确实需要请加 --force）")
    job_dir.mkdir(parents=True, exist_ok=True)
    for directory in (
        "assets/players",
        "evidence",
        "captions",
        "remote/payload",
        "remote/results",
        "renders",
        "reports",
    ):
        (job_dir / directory).mkdir(parents=True, exist_ok=True)
    manifest = default_manifest(job_id)
    manifest["video"] = dict(config["default_video"])
    if getattr(args, "asset_policy", "hltv") == "user-provided":
        manifest["asset_policy"] = {"version": USER_ASSET_POLICY_VERSION, "strict": True}
        manifest["caption_policy"].update({"format": "Name | Present/Past", "burn_in": False})
    save_manifest(job_dir, manifest)
    save_json(job_dir / "captions/events.json", [])
    save_json(
        job_dir / "remote/config.json",
        config,
    )
    (job_dir / "README.md").write_text(
        "# CS relay job\n\n"
        "本地负责证据、素材、字幕和剪辑；可配置的远端负责 ComfyUI 生成。\n",
        encoding="utf-8",
    )
    print(job_dir)
    return 0


def _find_player(manifest: dict[str, Any], player_slug: str) -> dict[str, Any] | None:
    return next(
        (p for p in manifest.get("players", []) if p.get("slug") == player_slug), None
    )


def _find_state(
    manifest: dict[str, Any], player_slug: str, state_key: str
) -> dict[str, Any] | None:
    player = _find_player(manifest, player_slug)
    if player is None:
        return None
    return next(
        (state for state in player.get("states", []) if state.get("key") == state_key),
        None,
    )


def _normalized_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _contains_forbidden_source_token(*values: Any) -> str | None:
    combined = " ".join(str(value or "").casefold() for value in values)
    return next(
        (token for token in sorted(FORBIDDEN_GENERATION_SOURCE_TOKENS) if token in combined),
        None,
    )


def _copy_evidence(job_dir: Path, source: Path, label: str, url: str | None = None) -> dict[str, Any]:
    """Copy browser-exported evidence into the job; never retain an external path."""

    if not source.is_file():
        raise RelayError(f"找不到证据文件：{source}")
    safe_label = slug(label)
    suffix = source.suffix.lower() or ".bin"
    destination = job_dir / "evidence" / f"{safe_label}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    record = {
        "path": str(destination.relative_to(job_dir)),
        "size": destination.stat().st_size,
        "sha256": sha256(destination),
    }
    if url:
        record["source_url"] = url
    return record


def cmd_register_player(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    player_slug = slug(args.player)
    state_key = slug(args.state)
    source_photo = Path(args.photo).expanduser().resolve()
    if not source_photo.is_file():
        raise RelayError(f"找不到照片：{source_photo}")
    if int(args.photo_year) != int(args.year):
        raise RelayError(
            f"定妆照年份 {args.photo_year} 与状态年份 {args.year} 不一致"
        )
    teamless_identity_only = bool(args.teamless_current_identity_only)
    if teamless_identity_only:
        if args.period != "current":
            raise RelayError("无队伍身份参考只允许用于 current 状态")
        if _normalized_label(args.team) not in TEAMLESS_CURRENT_TEAMS:
            raise RelayError("无队伍身份参考的状态队伍必须是 None 或 Retired")
        if args.acquisition_method != PORTRAIT_OVERRIDE_METHOD:
            raise RelayError("无队伍身份参考必须走 reviewed reference override")
        if not args.visual_identity_verified:
            raise RelayError("无队伍身份参考必须显式确认当前身份和来源队服")
    elif _normalized_label(args.photo_team) != _normalized_label(args.team):
        raise RelayError(
            f"定妆照队伍 {args.photo_team!r} 与状态队伍 {args.team!r} 不一致"
        )
    if not HLTV_PROFILE_RE.fullmatch(str(args.profile_url).strip()):
        raise RelayError("--profile-url 必须是 HLTV /player/<id>/... 页面")
    if args.review_status != "approved":
        raise RelayError("生成用定妆照必须经过明确审核并标记为 approved")
    if not teamless_identity_only and not args.visual_team_verified:
        raise RelayError("必须显式确认已目检照片中的队服与登记队伍/年份一致")
    if not args.profile_evidence:
        raise RelayError("必须保存用于核验年份和战队的选手 Profile 证据")

    acquisition_method = args.acquisition_method
    snapshot_parts: tuple[int, int, int] | None = None
    if acquisition_method == PORTRAIT_ACQUISITION_METHOD:
        if args.portrait_kind != "hltv_ranking_snapshot_playerbodyshot":
            raise RelayError("标准 Ranking 流程的 portrait kind 必须是 playerbodyshot")
        if args.portrait_provider != "hltv":
            raise RelayError("标准 Ranking 流程的 portrait provider 必须是 hltv")
        if not all(
            (
                args.snapshot_url,
                args.snapshot_evidence,
                args.snapshot_team,
                args.snapshot_player,
                args.snapshot_player_count is not None,
            )
        ):
            raise RelayError("标准 Ranking 流程必须完整登记快照 URL、证据、队伍、选手和五人数量")
        snapshot_parts = _ranking_snapshot_parts(args.snapshot_url)
        if snapshot_parts is None:
            raise RelayError(
                "定妆照证明页必须是带日期的 HLTV Ranking 快照："
                "https://www.hltv.org/ranking/teams/YYYY/month/day；"
                "禁止用 Team/Profile/Top 20/新闻页代替"
            )
        snapshot_year, _snapshot_month, _snapshot_day = snapshot_parts
        if snapshot_year != int(args.year):
            raise RelayError(
                f"Ranking 快照年份 {snapshot_year} 与状态年份 {args.year} 不一致"
            )
        if _normalized_label(args.snapshot_team) != _normalized_label(args.team):
            raise RelayError(
                f"Ranking 卡片队伍 {args.snapshot_team!r} 与状态队伍 {args.team!r} 不一致"
            )
        expected_player_names = {args.player, args.display_name or args.player}
        if _normalized_label(args.snapshot_player) not in {
            _normalized_label(value) for value in expected_player_names
        }:
            raise RelayError(
                f"Ranking 卡片选手 {args.snapshot_player!r} 与登记选手 {args.player!r} 不一致"
            )
        if int(args.snapshot_player_count) != 5:
            raise RelayError("历史 Ranking 战队卡片必须能核验为五位选手一排")
        forbidden = _portrait_source_violation(source_photo.name, args.photo_source_url)
        if forbidden:
            raise RelayError(
                f"生成用定妆照命中禁用来源标记 {forbidden!r}；"
                "只允许 Ranking 战队卡片引用的 playerbodyshot"
            )
        if "/playerbodyshot/" not in args.photo_source_url:
            raise RelayError("Ranking 卡片的定妆照原图 URL 必须包含 /playerbodyshot/")
    elif acquisition_method == PORTRAIT_OVERRIDE_METHOD:
        valid_override_pairs = {
            ("official_media_day_portrait", "official_media_day"),
            ("user_provided_portrait", "user"),
        }
        if teamless_identity_only:
            valid_override_pairs.add(("hltv_profile_playerbodyshot", "hltv"))
        if (args.portrait_kind, args.portrait_provider) not in valid_override_pairs:
            raise RelayError("替代参考只允许 official media-day 或 user-provided 的配对")
        if not str(args.override_reason or "").strip():
            raise RelayError("替代参考必须记录 --override-reason")
        snapshot_values = (
            args.snapshot_url,
            args.snapshot_evidence,
            args.snapshot_team,
            args.snapshot_player,
            args.snapshot_player_count,
        )
        if any(value is not None and value != [] for value in snapshot_values):
            if not all(
                (
                    args.snapshot_url,
                    args.snapshot_evidence,
                    args.snapshot_team,
                    args.snapshot_player,
                    args.snapshot_player_count is not None,
                )
            ):
                raise RelayError("替代参考如果登记 Ranking 上下文，必须一次填全")
            snapshot_parts = _ranking_snapshot_parts(args.snapshot_url)
            if snapshot_parts is None:
                raise RelayError("替代参考的 Ranking 上下文也必须是带日期的快照")
            if snapshot_parts[0] != int(args.year):
                raise RelayError("Ranking 上下文年份与状态年份不一致")
            if _normalized_label(args.snapshot_team) != _normalized_label(args.team):
                raise RelayError("Ranking 上下文队伍与状态队伍不一致")
            if int(args.snapshot_player_count) != 5:
                raise RelayError("Ranking 上下文必须为五人战队卡片")
    else:
        raise RelayError(f"不支持的定妆照取得方式：{acquisition_method}")

    if args.period == "peak":
        if args.hltv_rank is None or not (1 <= int(args.hltv_rank) <= 20):
            raise RelayError("peak 状态必须记录 1-20 的 HLTV 年度排名")
        if not args.ranking_url or not args.ranking_evidence:
            raise RelayError("peak 状态必须把排名 URL 和排名证据与定妆照证据分开登记")
    destination = (
        job_dir
        / "assets/players"
        / player_slug
        / state_key
        / "source"
        / source_photo.name
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source_photo != destination:
        shutil.copy2(source_photo, destination)
    snapshot_evidence: list[dict[str, Any]] = []
    for index, raw in enumerate(args.snapshot_evidence or [], start=1):
        path = Path(raw).expanduser().resolve()
        snapshot_evidence.append(
            _copy_evidence(
                job_dir,
                path,
                f"{player_slug}-{state_key}-ranking-snapshot-{index}",
                args.snapshot_url,
            )
        )
    portrait_evidence = list(snapshot_evidence)
    if acquisition_method == PORTRAIT_OVERRIDE_METHOD:
        source_record = _copy_evidence(
            job_dir,
            source_photo,
            f"{player_slug}-{state_key}-portrait-source",
            args.photo_source_url,
        )
        source_record["role"] = "reviewed_generation_reference"
        portrait_evidence.insert(0, source_record)
    profile_evidence: list[dict[str, Any]] = []
    for index, raw in enumerate(args.profile_evidence or [], start=1):
        path = Path(raw).expanduser().resolve()
        record = _copy_evidence(
            job_dir,
            path,
            f"{player_slug}-{state_key}-profile-{index}",
            args.profile_url,
        )
        record["role"] = "year_team_selection_evidence_only"
        profile_evidence.append(record)
    ranking_evidence: list[dict[str, Any]] = []
    for index, raw in enumerate(args.ranking_evidence or [], start=1):
        path = Path(raw).expanduser().resolve()
        record = _copy_evidence(
            job_dir,
            path,
            f"{player_slug}-{state_key}-ranking-{index}",
            args.ranking_url,
        )
        record["role"] = "ranking_evidence_only"
        ranking_evidence.append(record)
    player = _find_player(manifest, player_slug)
    if player is None:
        player = {"slug": player_slug, "display_name": args.display_name or args.player, "states": []}
        manifest.setdefault("players", []).append(player)
    states = player.setdefault("states", [])
    state = {
        "key": state_key,
        "period": args.period,
        "year": int(args.year),
        "team": args.team,
        "portrait": {
            "path": str(destination.relative_to(job_dir)),
            "sha256": sha256(destination),
            "kind": args.portrait_kind,
            "provider": args.portrait_provider,
            "role": (
                "generation_identity_only"
                if teamless_identity_only
                else "generation_identity"
            ),
            "year": int(args.photo_year),
            "team": args.photo_team,
            "source_team": args.photo_team,
            "teamless_current_identity_only": teamless_identity_only,
            "source_url": args.photo_source_url,
            "source_page_url": (
                args.snapshot_url
                if acquisition_method == PORTRAIT_ACQUISITION_METHOD
                else (args.photo_source_page_url or args.photo_source_url)
            ),
            "review_status": args.review_status,
            "review_note": args.review_note,
            "evidence": portrait_evidence,
        },
        "portrait_acquisition": {
            "method": acquisition_method,
            "profile_url": args.profile_url,
            "profile_evidence": profile_evidence,
        },
        "ranking_evidence": ranking_evidence,
    }
    if teamless_identity_only:
        state["portrait"]["visual_identity_review"] = {
            "status": "approved",
            "note": args.review_note,
        }
        state["portrait_acquisition"]["teamless_current_identity_only"] = True
    else:
        state["portrait"]["visual_team_review"] = {
            "status": "approved",
            "note": args.review_note,
        }
    if snapshot_parts is not None:
        snapshot_year, snapshot_month, snapshot_day = snapshot_parts
        state["portrait_acquisition"].update({
            "snapshot_url": args.snapshot_url,
            "snapshot_date": f"{snapshot_year:04d}-{snapshot_month:02d}-{snapshot_day:02d}",
            "snapshot_team": args.snapshot_team,
            "snapshot_player": args.snapshot_player,
            "snapshot_player_count": int(args.snapshot_player_count),
            "snapshot_evidence": snapshot_evidence,
        })
    if acquisition_method == PORTRAIT_OVERRIDE_METHOD:
        state["portrait_acquisition"]["override_reason"] = args.override_reason
    if args.period == "peak":
        state["peak_selection"] = {
            "provider": "HLTV",
            "year": int(args.year),
            "rank": int(args.hltv_rank),
            "source_url": args.ranking_url,
        }
    states[:] = [s for s in states if s.get("key") != state_key]
    states.append(state)
    if manifest.get("status") == "draft":
        manifest["status"] = "local_collected"
    save_manifest(job_dir, manifest)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def cmd_register_source(args: argparse.Namespace) -> int:
    """Register an explicitly reviewed user source without inventing team/year data."""
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    if manifest.get("asset_policy", {}).get("version") != USER_ASSET_POLICY_VERSION:
        raise RelayError("register-source 需要 init --asset-policy user-provided")
    if args.review_status != "approved" or not args.review_note.strip():
        raise RelayError("用户原图必须明确审核为 approved 并填写 review-note")
    source = Path(args.photo).expanduser().resolve()
    if not source.is_file():
        raise RelayError(f"找不到来源图片：{source}")
    player_slug, state_key = slug(args.player), slug(args.state)
    destination = job_dir / "assets/players" / player_slug / state_key / "source" / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != destination:
        shutil.copy2(source, destination)
    player = _find_player(manifest, player_slug)
    if player is None:
        player = {"slug": player_slug, "name": args.name or args.player, "states": []}
        manifest.setdefault("players", []).append(player)
    state = {
        "key": state_key,
        "period": args.period,
        "portrait": {
            "path": str(destination.relative_to(job_dir)),
            "sha256": sha256(destination),
            "provider": "user",
            "review_status": args.review_status,
            "review_note": args.review_note,
        },
    }
    # Replacing a source deliberately invalidates its old generated endpoint.
    player["states"] = [s for s in player.get("states", []) if s.get("key") != state_key] + [state]
    if manifest.get("status") == "draft":
        manifest["status"] = "local_collected"
    save_manifest(job_dir, manifest)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def cmd_register_endpoint(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    player_slug = slug(args.player)
    state_key = slug(args.state)
    state = _find_state(manifest, player_slug, state_key)
    if state is None:
        raise RelayError(f"尚未登记状态：{player_slug}/{state_key}")
    portrait = state.get("portrait")
    if not isinstance(portrait, dict) or not portrait.get("sha256"):
        raise RelayError(f"状态 {player_slug}/{state_key} 缺少合格定妆照")
    if args.review_status != "approved":
        raise RelayError("生成端点必须经过明确审核并标记为 approved")
    layout = None
    if manifest.get("asset_policy", {}).get("version") == USER_ASSET_POLICY_VERSION:
        if not args.layout_reference or not (args.layout_note or "").strip():
            raise RelayError("用户端点必须提供 --layout-reference 和 --layout-note，确认正脸、头部比例和布局一致")
        reference = rel_path(job_dir, args.layout_reference)
        if not reference.is_file():
            raise RelayError(f"布局参考图不存在：{args.layout_reference}")
        layout = {
            "reference": {"path": args.layout_reference, "sha256": sha256(reference)},
            "reviewed": True,
            "note": args.layout_note,
        }
    identity_only = portrait.get("teamless_current_identity_only") is True
    if identity_only and not args.neutralized_teamless_wardrobe:
        raise RelayError("无队伍身份参考生成的端点必须确认已中和旧队服与品牌")
    if args.neutralized_teamless_wardrobe and not identity_only:
        raise RelayError("--neutralized-teamless-wardrobe 只允许用于无队伍当前状态")
    source = Path(args.endpoint).expanduser().resolve()
    if not source.is_file():
        raise RelayError(f"找不到生成端点：{source}")
    destination = (
        job_dir
        / "assets/players"
        / player_slug
        / state_key
        / "endpoint"
        / source.name
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != destination:
        shutil.copy2(source, destination)
    state["endpoint"] = {
        "path": str(destination.relative_to(job_dir)),
        "sha256": sha256(destination),
        "derived_from_portrait_sha256": portrait["sha256"],
        "review_status": args.review_status,
        "review_note": args.review_note,
    }
    if identity_only:
        state["endpoint"]["teamless_wardrobe"] = "neutral_no_branding"
    if layout is not None:
        state["endpoint"]["layout"] = layout
    save_manifest(job_dir, manifest)
    print(json.dumps(state["endpoint"], ensure_ascii=False, indent=2))
    return 0


def _record_file_errors(
    job_dir: Path, record: dict[str, Any], label: str
) -> list[str]:
    errors: list[str] = []
    relative = record.get("path")
    expected_hash = record.get("sha256")
    if not isinstance(relative, str) or not relative:
        return [f"{label} 缺少 path"]
    try:
        path = rel_path(job_dir, relative)
    except RelayError as exc:
        return [f"{label}: {exc}"]
    if not path.is_file():
        errors.append(f"{label} 文件不存在：{relative}")
    elif not isinstance(expected_hash, str) or sha256(path) != expected_hash:
        errors.append(f"{label} SHA-256 与登记值不一致：{relative}")
    return errors


def _portrait_errors(
    job_dir: Path,
    player: dict[str, Any],
    state: dict[str, Any],
) -> list[str]:
    player_slug = str(player.get("slug") or "unknown-player")
    state_key = str(state.get("key") or "unknown-state")
    label = f"{player_slug}/{state_key}"
    errors: list[str] = []
    portrait = state.get("portrait")
    if not isinstance(portrait, dict):
        return [f"{label} 缺少独立的生成用定妆照记录 portrait"]
    errors.extend(_record_file_errors(job_dir, portrait, f"{label} portrait"))
    identity_only = portrait.get("teamless_current_identity_only") is True
    valid_identity_only_state = (
        state.get("period") == "current"
        and _normalized_label(state.get("team")) in TEAMLESS_CURRENT_TEAMS
    )
    expected_role = "generation_identity_only" if identity_only else "generation_identity"
    if portrait.get("role") != expected_role:
        errors.append(f"{label} portrait.role 必须是 {expected_role}")
    if identity_only and not valid_identity_only_state:
        errors.append(f"{label} 无队伍身份参考只允许用于 current 的 None/Retired 状态")
    if identity_only and not str(portrait.get("source_team") or "").strip():
        errors.append(f"{label} 无队伍身份参考缺少真实来源队服记录 source_team")
    if portrait.get("kind") not in ALLOWED_PORTRAIT_KINDS:
        errors.append(f"{label} portrait.kind 不是允许的官方定妆照类型")
    if portrait.get("provider") not in ALLOWED_PORTRAIT_PROVIDERS:
        errors.append(f"{label} portrait.provider 不是允许的官方来源")
    if portrait.get("review_status") != "approved":
        errors.append(f"{label} portrait 尚未审核为 approved")
    if not str(portrait.get("review_note") or "").strip():
        errors.append(f"{label} portrait 缺少审核说明")
    visual_review_key = "visual_identity_review" if identity_only else "visual_team_review"
    visual_review = portrait.get(visual_review_key)
    if not isinstance(visual_review, dict) or visual_review.get("status") != "approved":
        errors.append(f"{label} 缺少{visual_review_key}目检通过记录")
    elif not str(visual_review.get("note") or "").strip():
        errors.append(f"{label} 队服/年份目检记录缺少说明")
    if int(portrait.get("year") or -1) != int(state.get("year") or -2):
        errors.append(f"{label} 定妆照年份与状态年份不一致")
    if not identity_only and _normalized_label(portrait.get("team")) != _normalized_label(state.get("team")):
        errors.append(f"{label} 定妆照队伍与状态队伍不一致")
    evidence = portrait.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append(f"{label} 定妆照缺少独立页面/截图证据")
    else:
        for index, record in enumerate(evidence):
            if not isinstance(record, dict):
                errors.append(f"{label} portrait.evidence[{index}] 格式无效")
            else:
                errors.extend(
                    _record_file_errors(
                        job_dir, record, f"{label} portrait.evidence[{index}]"
                    )
                )
    acquisition = state.get("portrait_acquisition")
    if not isinstance(acquisition, dict):
        errors.append(f"{label} 缺少 portrait_acquisition 取证链")
    else:
        method = acquisition.get("method")
        if method not in ALLOWED_PORTRAIT_ACQUISITION_METHODS:
            errors.append(f"{label} portrait_acquisition.method 不在允许范围")
        if not HLTV_PROFILE_RE.fullmatch(str(acquisition.get("profile_url") or "")):
            errors.append(f"{label} Profile 证据 URL 不是 HLTV 选手页")
        for field, evidence_label in (("profile_evidence", "Profile 证据"),):
            records = acquisition.get(field)
            if not isinstance(records, list) or not records:
                errors.append(f"{label} 缺少{evidence_label}")
                continue
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    errors.append(f"{label} {field}[{index}] 格式无效")
                    continue
                errors.extend(
                    _record_file_errors(
                        job_dir, record, f"{label} {field}[{index}]"
                    )
                )
        if method == PORTRAIT_ACQUISITION_METHOD:
            if portrait.get("kind") != "hltv_ranking_snapshot_playerbodyshot":
                errors.append(f"{label} 标准 Ranking 流程必须使用 playerbodyshot")
            if portrait.get("provider") != "hltv":
                errors.append(f"{label} 标准 Ranking 流程的 provider 必须是 hltv")
            forbidden = _portrait_source_violation(
                portrait.get("path"), portrait.get("source_url")
            )
            if forbidden:
                errors.append(f"{label} 定妆照命中禁用来源标记：{forbidden}")
            if "/playerbodyshot/" not in str(portrait.get("source_url") or ""):
                errors.append(f"{label} Ranking 卡片定妆照 URL 不是 /playerbodyshot/ 资源")
            acquisition_snapshot = acquisition.get("snapshot_url")
            acquisition_parts = _ranking_snapshot_parts(acquisition_snapshot)
            if acquisition_parts is None:
                errors.append(f"{label} 取证页不是带日期的 HLTV Ranking 快照")
            elif acquisition_parts[0] != int(state.get("year") or -1):
                errors.append(f"{label} 取证 Ranking 快照年份与状态年份不一致")
            if acquisition_snapshot != portrait.get("source_page_url"):
                errors.append(f"{label} portrait 来源页与取证 Ranking 快照不一致")
            if _normalized_label(acquisition.get("snapshot_team")) != _normalized_label(state.get("team")):
                errors.append(f"{label} Ranking 卡片队伍与状态队伍不一致")
            display_name = player.get("display_name") or player_slug
            player_names = {_normalized_label(display_name), _normalized_label(player_slug)}
            if _normalized_label(acquisition.get("snapshot_player")) not in player_names:
                errors.append(f"{label} Ranking 卡片选手与登记选手不一致")
            if acquisition.get("snapshot_player_count") != 5:
                errors.append(f"{label} Ranking 战队卡片不是五位选手一排")
            records = acquisition.get("snapshot_evidence")
            if not isinstance(records, list) or not records:
                errors.append(f"{label} 缺少 Ranking 快照证据")
            else:
                for index, record in enumerate(records):
                    if not isinstance(record, dict):
                        errors.append(f"{label} snapshot_evidence[{index}] 格式无效")
                    else:
                        errors.extend(_record_file_errors(job_dir, record, f"{label} snapshot_evidence[{index}]"))
        elif method == PORTRAIT_OVERRIDE_METHOD:
            valid_override_pairs = {
                ("official_media_day_portrait", "official_media_day"),
                ("user_provided_portrait", "user"),
            }
            if identity_only:
                valid_override_pairs.add(("hltv_profile_playerbodyshot", "hltv"))
            if (portrait.get("kind"), portrait.get("provider")) not in valid_override_pairs:
                errors.append(f"{label} 替代参考的 kind/provider 配对无效")
            if not str(acquisition.get("override_reason") or "").strip():
                errors.append(f"{label} 替代参考缺少 override_reason")
            if identity_only and acquisition.get("teamless_current_identity_only") is not True:
                errors.append(f"{label} 取证链缺少无队伍身份参考标记")
            if "snapshot_url" in acquisition:
                acquisition_parts = _ranking_snapshot_parts(acquisition.get("snapshot_url"))
                if acquisition_parts is None:
                    errors.append(f"{label} 替代参考中的 Ranking 上下文不是有效快照")
                elif acquisition_parts[0] != int(state.get("year") or -1):
                    errors.append(f"{label} 替代参考的 Ranking 上下文年份不一致")
                if acquisition.get("snapshot_player_count") != 5:
                    errors.append(f"{label} 替代参考的 Ranking 上下文不是五人卡片")
                if _normalized_label(acquisition.get("snapshot_team")) != _normalized_label(state.get("team")):
                    errors.append(f"{label} 替代参考的 Ranking 上下文队伍不一致")
                display_name = player.get("display_name") or player_slug
                player_names = {_normalized_label(display_name), _normalized_label(player_slug)}
                if _normalized_label(acquisition.get("snapshot_player")) not in player_names:
                    errors.append(f"{label} 替代参考的 Ranking 上下文选手不一致")
                records = acquisition.get("snapshot_evidence")
                if not isinstance(records, list) or not records:
                    errors.append(f"{label} 替代参考缺少 Ranking 上下文证据")
                else:
                    for index, record in enumerate(records):
                        if not isinstance(record, dict):
                            errors.append(f"{label} override snapshot_evidence[{index}] 格式无效")
                        else:
                            errors.extend(_record_file_errors(job_dir, record, f"{label} override snapshot_evidence[{index}]"))
    if state.get("period") == "peak":
        selection = state.get("peak_selection")
        if not isinstance(selection, dict):
            errors.append(f"{label} peak 状态缺少 HLTV 年度排名选择记录")
        else:
            rank = selection.get("rank")
            if not isinstance(rank, int) or not (1 <= rank <= 20):
                errors.append(f"{label} HLTV 年度排名必须是 1-20")
            if int(selection.get("year") or -1) != int(state.get("year") or -2):
                errors.append(f"{label} 排名年份与状态年份不一致")
            if not str(selection.get("source_url") or "").strip():
                errors.append(f"{label} 缺少排名来源 URL")
        ranking_evidence = state.get("ranking_evidence")
        if not isinstance(ranking_evidence, list) or not ranking_evidence:
            errors.append(f"{label} peak 状态缺少与定妆照分离的排名证据")
        else:
            for index, record in enumerate(ranking_evidence):
                if not isinstance(record, dict):
                    errors.append(f"{label} ranking_evidence[{index}] 格式无效")
                    continue
                if record.get("role") != "ranking_evidence_only":
                    errors.append(
                        f"{label} ranking_evidence[{index}] 只能标记为 ranking_evidence_only"
                    )
                errors.extend(
                    _record_file_errors(
                        job_dir, record, f"{label} ranking_evidence[{index}]"
                    )
                )
    return errors


def _endpoint_errors(
    job_dir: Path,
    player: dict[str, Any],
    state: dict[str, Any],
) -> list[str]:
    label = f"{player.get('slug')}/{state.get('key')}"
    endpoint = state.get("endpoint")
    portrait = state.get("portrait")
    if not isinstance(endpoint, dict):
        return [f"{label} 尚未登记由该定妆照生成的 endpoint"]
    errors = _record_file_errors(job_dir, endpoint, f"{label} endpoint")
    if endpoint.get("review_status") != "approved":
        errors.append(f"{label} endpoint 尚未审核为 approved")
    if not str(endpoint.get("review_note") or "").strip():
        errors.append(f"{label} endpoint 缺少审核说明")
    portrait_hash = portrait.get("sha256") if isinstance(portrait, dict) else None
    if endpoint.get("derived_from_portrait_sha256") != portrait_hash:
        errors.append(f"{label} endpoint 没有绑定当前定妆照的 SHA-256")
    if isinstance(portrait, dict) and portrait.get("teamless_current_identity_only") is True:
        if endpoint.get("teamless_wardrobe") != "neutral_no_branding":
            errors.append(f"{label} endpoint 未记录无队伍状态的中性无品牌服装")
    return errors


def strict_asset_errors(
    job_dir: Path, manifest: dict[str, Any]
) -> list[str]:
    policy = manifest.get("asset_policy")
    if not isinstance(policy, dict) or policy.get("strict") is not True:
        return ["必须显式启用 asset_policy.strict=true；请使用 hltv 或 user-provided 策略"]
    user_provided = policy.get("version") == USER_ASSET_POLICY_VERSION
    errors: list[str] = []
    if policy.get("version") not in {ASSET_POLICY_VERSION, USER_ASSET_POLICY_VERSION}:
        errors.append(
            f"asset_policy.version 必须是 {ASSET_POLICY_VERSION}"
        )
    if not user_provided and policy.get("portrait_acquisition_method") != PORTRAIT_ACQUISITION_METHOD:
        errors.append(
            "asset_policy.portrait_acquisition_method 必须固定为 "
            f"{PORTRAIT_ACQUISITION_METHOD}"
        )
    if not user_provided and policy.get("reviewed_override_method") != PORTRAIT_OVERRIDE_METHOD:
        errors.append(
            "asset_policy.reviewed_override_method 必须固定为 "
            f"{PORTRAIT_OVERRIDE_METHOD}"
        )
    endpoint_index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for player in manifest.get("players", []):
        if not isinstance(player, dict):
            errors.append("players 中存在非 object 记录")
            continue
        for state in player.get("states", []):
            if not isinstance(state, dict):
                errors.append(f"{player.get('slug')} 的 states 中存在非 object 记录")
                continue
            if user_provided:
                errors.extend(_user_source_errors(job_dir, player, state))
            else:
                errors.extend(_portrait_errors(job_dir, player, state))
            endpoint = state.get("endpoint")
            if isinstance(endpoint, dict) and isinstance(endpoint.get("path"), str):
                endpoint_path = endpoint["path"]
                if endpoint_path in endpoint_index:
                    errors.append(f"endpoint 被多个状态重复登记：{endpoint_path}")
                endpoint_index[endpoint_path] = (player, state)

    for index, segment in enumerate(manifest.get("segments", [])):
        first_path = segment.get("first_frame")
        last_path = segment.get("last_frame")
        first_entry = endpoint_index.get(first_path) if isinstance(first_path, str) else None
        last_entry = endpoint_index.get(last_path) if isinstance(last_path, str) else None
        if first_entry is None:
            errors.append(
                f"segments[{index}].first_frame 不是已登记 endpoint：{first_path}"
            )
        if last_entry is None:
            errors.append(
                f"segments[{index}].last_frame 不是已登记 endpoint：{last_path}"
            )
        if first_entry is None or last_entry is None:
            continue
        first_player, first_state = first_entry
        last_player, last_state = last_entry
        errors.extend(_endpoint_errors(job_dir, first_player, first_state))
        errors.extend(_endpoint_errors(job_dir, last_player, last_state))
        if user_provided:
            errors.extend(_layout_errors(job_dir, first_state))
            errors.extend(_layout_errors(job_dir, last_state))
        kind = segment.get("kind") or segment.get("workflow")
        present_period = "present" if user_provided else "current"
        past_periods = {"past"} if user_provided else {"peak", "past"}
        if kind in {"age", "age_transition", "03"}:
            if first_player.get("slug") != last_player.get("slug"):
                errors.append(f"segments[{index}] age_transition 首尾不是同一选手")
            if first_state.get("period") != present_period or last_state.get("period") not in past_periods:
                errors.append(
                    f"segments[{index}] age_transition 必须使用 {present_period} → {'/'.join(sorted(past_periods))}"
                )
            first_portrait = first_state.get("portrait", {})
            last_portrait = last_state.get("portrait", {})
            if first_portrait.get("sha256") == last_portrait.get("sha256"):
                errors.append(f"segments[{index}] current 与 peak 不能复用同一张定妆照")
            if user_provided:
                first_layout = first_state.get("endpoint", {}).get("layout", {}).get("reference", {})
                last_layout = last_state.get("endpoint", {}).get("layout", {}).get("reference", {})
                if first_layout.get("sha256") != last_layout.get("sha256"):
                    errors.append(f"segments[{index}] Present/Past 必须使用同一张布局参考图")
        elif kind in {"handoff", "player_handoff", "04"}:
            if first_player.get("slug") == last_player.get("slug"):
                errors.append(f"segments[{index}] player_handoff 必须连接两位不同选手")
            if first_state.get("period") not in past_periods or last_state.get("period") != present_period:
                errors.append(
                    f"segments[{index}] player_handoff 必须使用上一位 peak/past → 下一位 current"
                )
    return list(dict.fromkeys(errors))


def _user_source_errors(job_dir: Path, player: dict[str, Any], state: dict[str, Any]) -> list[str]:
    label = f"{player.get('slug')}/{state.get('key')}"
    portrait = state.get("portrait")
    if not isinstance(portrait, dict):
        return [f"{label} 缺少用户原图 portrait"]
    errors = _record_file_errors(job_dir, portrait, f"{label} portrait")
    if state.get("period") not in {"present", "past"}:
        errors.append(f"{label} period 必须是 present 或 past")
    if portrait.get("provider") != "user":
        errors.append(f"{label} portrait.provider 必须是 user")
    if portrait.get("review_status") != "approved" or not str(portrait.get("review_note") or "").strip():
        errors.append(f"{label} 原图缺少 approved 审核及说明")
    return errors


def _layout_errors(job_dir: Path, state: dict[str, Any]) -> list[str]:
    endpoint = state.get("endpoint", {})
    layout = endpoint.get("layout") if isinstance(endpoint, dict) else None
    if not isinstance(layout, dict) or layout.get("reviewed") is not True or not str(layout.get("note") or "").strip():
        return [f"{state.get('key')} endpoint 缺少已审核的布局记录 layout"]
    reference = layout.get("reference")
    if not isinstance(reference, dict):
        return [f"{state.get('key')} layout 缺少 reference 文件记录"]
    return _record_file_errors(job_dir, reference, f"{state.get('key')} layout reference")


def cmd_record_evidence(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    source = Path(args.source).expanduser().resolve()
    record = _copy_evidence(job_dir, source, args.label, args.source_url)
    record["kind"] = args.kind
    manifest.setdefault("evidence", []).append(record)
    if manifest.get("status") == "draft":
        manifest["status"] = "local_collected"
    save_manifest(job_dir, manifest)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _check_optional_asset(job_dir: Path, value: str | None) -> None:
    if value:
        path = rel_path(job_dir, value)
        if not path.is_file():
            raise RelayError(f"segment 引用的素材不存在：{value}")


def cmd_add_segment(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    segment_id = slug(args.id)
    for value in (args.first_frame, args.last_frame, args.prompt_file, args.video_path):
        _check_optional_asset(job_dir, value)
    for value in args.action_image or []:
        _check_optional_asset(job_dir, value)
    segment: dict[str, Any] = {
        "id": segment_id,
        "kind": args.kind,
        "workflow": args.kind,
        "first_frame": args.first_frame,
        "last_frame": args.last_frame,
        "action_images": args.action_image or [],
        "prompt": args.prompt,
        "prompt_file": args.prompt_file,
        "video_path": args.video_path,
        "seed": args.seed,
        "length": args.length,
        "duration_s": args.duration,
        "fps": args.fps,
        "generation_width": args.width,
        "generation_height": args.height,
        "generation_fps": args.generation_fps,
        "caption": args.caption,
    }
    segments = manifest.setdefault("segments", [])
    segments[:] = [s for s in segments if s.get("id") != segment_id]
    segments.append(segment)
    save_manifest(job_dir, manifest)
    print(json.dumps(segment, ensure_ascii=False, indent=2))
    return 0


def _validate_event(event: dict[str, Any], index: int) -> list[str]:
    errors: list[str] = []
    for key in ("start", "end", "text"):
        if key not in event:
            errors.append(f"captions/events.json[{index}] 缺少 {key}")
    if "start" in event and "end" in event:
        try:
            if float(event["end"]) <= float(event["start"]):
                errors.append(f"captions/events.json[{index}] end 必须大于 start")
        except (TypeError, ValueError):
            errors.append(f"captions/events.json[{index}] start/end 必须是秒数")
    return errors


def load_events(job_dir: Path) -> list[dict[str, Any]]:
    events = load_json(job_dir / "captions/events.json")
    if not isinstance(events, list):
        raise RelayError("captions/events.json 必须是数组")
    errors: list[str] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append(f"captions/events.json[{index}] 必须是 object")
        else:
            errors.extend(_validate_event(event, index))
    if errors:
        raise RelayError("\n".join(errors))
    return events


def srt_timestamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def merge_contiguous_events(
    events: list[dict[str, Any]], *, tolerance_s: float = 0.001
) -> list[dict[str, Any]]:
    """Merge adjacent caption events with identical display text."""

    merged: list[dict[str, Any]] = []
    for event in events:
        current = dict(event)
        if (
            merged
            and str(merged[-1]["text"]) == str(current["text"])
            and abs(float(merged[-1]["end"]) - float(current["start"])) <= tolerance_s
        ):
            merged[-1]["end"] = current["end"]
        else:
            merged.append(current)
    return merged


def cmd_make_srt(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    events = merge_contiguous_events(load_events(job_dir))
    output = job_dir / (args.output or "captions/final.srt")
    blocks = []
    for index, event in enumerate(events, start=1):
        blocks.append(
            f"{index}\n{srt_timestamp(float(event['start']))} --> "
            f"{srt_timestamp(float(event['end']))}\n{event['text']}\n"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(blocks), encoding="utf-8")
    manifest.setdefault("artifacts", {})["captions"] = str(output.relative_to(job_dir))
    save_manifest(job_dir, manifest)
    print(output)
    return 0


def _iter_manifest_assets(manifest: dict[str, Any]) -> Iterable[str]:
    keys = ("first_frame", "last_frame", "action_image")
    for segment in manifest.get("segments", []):
        for key in keys:
            value = segment.get(key)
            if isinstance(value, str) and value:
                yield value
        for value in segment.get("action_images", []) or []:
            if isinstance(value, str):
                yield value


def _read_prompt(job_dir: Path, segment: dict[str, Any]) -> str:
    prompt = segment.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    prompt_file = segment.get("prompt_file")
    if isinstance(prompt_file, str) and prompt_file:
        path = rel_path(job_dir, prompt_file)
        if not path.is_file():
            raise RelayError(f"prompt_file 不存在：{prompt_file}")
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    raise RelayError(f"segment {segment.get('id')} 缺少 prompt 或 prompt_file")


def _copy_payload_asset(job_dir: Path, payload_dir: Path, relative: str) -> dict[str, Any]:
    source = rel_path(job_dir, relative)
    if not source.is_file():
        raise RelayError(f"manifest 引用的素材不存在：{relative}")
    destination = payload_dir / Path(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {
        "path": relative,
        "size": destination.stat().st_size,
        "sha256": sha256(destination),
    }


def _prepare_payload_endpoint(
    job_dir: Path,
    payload_dir: Path,
    relative: str,
    width: int,
    height: int,
    cache: dict[tuple[str, int, int], tuple[str, dict[str, Any]]],
) -> tuple[str, dict[str, Any]]:
    """Create one canonical H3 endpoint shared by both sides of a relay seam.

    MiniMaxH3ImageToVideo currently stretches ``first_frame`` but center-crops
    ``last_frame``.  Pre-normalizing the reusable master to the exact generation
    canvas makes both node branches no-ops geometrically, so the same endpoint
    cannot become thinner when it changes from a tail frame to a first frame.
    """

    key = (relative, width, height)
    if key in cache:
        return cache[key]
    source = rel_path(job_dir, relative)
    if not source.is_file():
        raise RelayError(f"manifest 引用的端点不存在：{relative}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RelayError("打包 H3 端点需要本机 ffmpeg，但当前 PATH 中未找到")
    prepared_relative = (
        Path("prepared")
        / f"{width}x{height}-center"
        / Path(relative).with_suffix(".png")
    )
    destination = payload_dir / prepared_relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:(in_w-out_w)/2:(in_h-out_h)/2"
    )
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            filter_graph,
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            str(destination),
        ],
        text=True,
        capture_output=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or "unknown ffmpeg error"
        raise RelayError(f"端点标准化失败：{relative}: {detail}")
    request_path = f"payload/{prepared_relative.as_posix()}"
    metadata = {
        "path": prepared_relative.as_posix(),
        "source_path": relative,
        "size": destination.stat().st_size,
        "sha256": sha256(destination),
        "width": width,
        "height": height,
        "transform": "aspect-preserving center crop",
    }
    cache[key] = (request_path, metadata)
    return cache[key]


def h3_frame_length(length: Any, duration: Any, fps: Any) -> int:
    """Explicit length is authoritative; duration is only a requested-length hint."""
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        raise RelayError("H3 fps 必须是有限正数")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
        raise RelayError("H3 duration_seconds 必须是有限正数")
    if length is None:
        requested = max(5, round((duration if duration is not None else 4.0) * fps))
        length = 17 * max(0, math.ceil((requested - 5) / 17)) + 5
    if isinstance(length, bool) or not isinstance(length, int) or length < 5 or (length - 5) % 17:
        raise RelayError("H3 length 必须是合法帧数 17*k+5（k 为非负整数）")
    return length


def cmd_package(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    if not manifest.get("segments"):
        raise RelayError("至少需要一个 segment 才能打包远端任务")
    asset_errors = strict_asset_errors(job_dir, manifest)
    if asset_errors:
        raise RelayError(
            "资产门禁未通过，未生成或覆盖远端 payload：\n- "
            + "\n- ".join(asset_errors)
        )
    lengths = []
    timeline = manifest.get("timeline_contract", {})
    for segment in manifest["segments"]:
        fps = segment.get("generation_fps", segment.get("fps", 24))
        length = h3_frame_length(segment.get("length"), segment.get("duration_s", segment.get("duration_seconds")), fps)
        if timeline and (timeline.get("segment_frames") != length or timeline.get("fps") != fps):
            raise RelayError("segment 帧数/fps 与 timeline_contract 不一致；请重新 build_relay_chain 同步字幕")
        lengths.append(length)
    payload_dir = job_dir / "remote/payload"
    if payload_dir.exists() and args.clean:
        for child in payload_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    payload_dir.mkdir(parents=True, exist_ok=True)
    assets = []
    seen: set[str] = set()
    for relative in _iter_manifest_assets(manifest):
        if relative in seen:
            continue
        seen.add(relative)
        assets.append(_copy_payload_asset(job_dir, payload_dir, relative))
    payload_segments: list[dict[str, Any]] = []
    prepared_endpoint_cache: dict[
        tuple[str, int, int], tuple[str, dict[str, Any]]
    ] = {}
    prepared_endpoints: list[dict[str, Any]] = []
    for segment, length in zip(manifest.get("segments", []), lengths):
        segment_id = segment.get("id") or segment.get("segment_id")
        if not isinstance(segment_id, str) or not segment_id:
            raise RelayError("每个 segment 必须有 id")
        first = segment.get("first_frame")
        last = segment.get("last_frame")
        if not isinstance(first, str) or not isinstance(last, str):
            raise RelayError(f"segment {segment_id} 缺少 first_frame/last_frame")
        for value in (first, last):
            if value not in seen:
                seen.add(value)
                assets.append(_copy_payload_asset(job_dir, payload_dir, value))
        actions: list[dict[str, str]] = []
        for action in segment.get("action_images", []) or []:
            raw_path = action.get("path") if isinstance(action, dict) else action
            if not isinstance(raw_path, str):
                raise RelayError(f"segment {segment_id} 的 action_images 路径无效")
            if raw_path not in seen:
                seen.add(raw_path)
                assets.append(_copy_payload_asset(job_dir, payload_dir, raw_path))
            actions.append({"path": f"payload/{raw_path}", "role": str(action.get("role") if isinstance(action, dict) else "reference")})
        size = segment.get("generation_size") if isinstance(segment.get("generation_size"), dict) else {}
        width = segment.get("generation_width", size.get("width", 544))
        height = segment.get("generation_height", size.get("height", 736))
        fps = segment.get("generation_fps", segment.get("fps", 24))
        width = int(width)
        height = int(height)
        first_payload, first_prepared = _prepare_payload_endpoint(
            job_dir,
            payload_dir,
            first,
            width,
            height,
            prepared_endpoint_cache,
        )
        last_payload, last_prepared = _prepare_payload_endpoint(
            job_dir,
            payload_dir,
            last,
            width,
            height,
            prepared_endpoint_cache,
        )
        for prepared in (first_prepared, last_prepared):
            if prepared not in prepared_endpoints:
                prepared_endpoints.append(prepared)
        seed = segment.get("seed")
        if not isinstance(seed, int) or seed < 0:
            raise RelayError(f"segment {segment_id} 必须提供非负整数 seed")
        workflow = segment.get("workflow") or segment.get("kind")
        if workflow not in {"age", "age_transition", "03", "handoff", "player_handoff", "04"}:
            raise RelayError(f"segment {segment_id} 的 workflow 不受远端适配器支持：{workflow!r}")
        payload_segments.append({
            "segment_id": segment_id,
            "workflow": workflow,
            "first_frame": first_payload,
            "last_frame": last_payload,
            "action_images": actions,
            "prompt": _read_prompt(job_dir, segment),
            "seed": seed,
            "length": length,
            "width": width,
            "height": height,
            "fps": float(fps),
            "duration_seconds": segment.get("duration_s", segment.get("duration_seconds")),
            "model": segment.get("model", {}),
        })
    request = {
        "schema_version": SCHEMA_VERSION,
        "job_id": manifest["job_id"],
        "created_at": now_iso(),
        "adapter": manifest.get("remote", {}).get("adapter", "comfyui-h3"),
        "payload": {"segments": payload_segments},
        "local_context": {
            "prepared_endpoints": prepared_endpoints,
            "endpoint_preprocess": {
                "policy": "aspect-preserving center crop",
                "reason": "MiniMax H3 stretches first_frame but center-crops last_frame; exact-size canonical inputs keep relay seams geometrically identical.",
            },
        },
        "input_contract": "The local controller retains provenance; the remote generates only the requested segments.",
    }
    save_json(job_dir / "remote/request.json", request)
    manifest.setdefault("artifacts", {})["remote_request"] = "remote/request.json"
    manifest["status"] = "package_ready"
    save_manifest(job_dir, manifest)
    print(job_dir / "remote/request.json")
    return 0


def load_remote_config(job_dir: Path, config_path: str | Path | None = None) -> dict[str, Any]:
    """Load one explicit config, otherwise the job-local config, with no host defaults."""
    path = Path(config_path).expanduser().resolve() if config_path else job_dir / "remote/config.json"
    config = dict(DEFAULT_CONFIG)
    config["default_video"] = dict(DEFAULT_VIDEO)
    if config_path or path.is_file():
        supplied = load_json(path)
        if not isinstance(supplied, dict):
            raise RelayError("配置必须是 JSON object")
        unknown = set(supplied) - set(DEFAULT_CONFIG)
        if unknown:
            raise RelayError(f"不支持的配置字段：{', '.join(sorted(unknown))}")
        config.update(supplied)
    video = config.get("default_video")
    if not isinstance(video, dict) or any(
        not isinstance(video.get(key), (int, float)) or isinstance(video.get(key), bool) or video[key] <= 0
        for key in ("width", "height", "fps")
    ):
        raise RelayError("default_video 需要正数 width/height/fps")
    return config


def remote_target(args: argparse.Namespace, job_dir: Path, manifest: dict[str, Any], *, results: bool = False) -> tuple[str, str]:
    config = load_remote_config(job_dir, getattr(args, "config", None))
    host = args.host or config.get("remote_host")
    root = args.remote_root or config.get("remote_project_root")
    if not host or not root:
        raise RelayError("尚未配置远端：通过 init --config、sync --config 或 --host/--remote-root 设置 remote_host 和 remote_project_root")
    if not isinstance(host, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", host) is None:
        raise RelayError("remote_host 必须是 SSH 配置别名，不得包含 user@、端口或 shell 字符")
    if not isinstance(root, str) or not root.startswith("/") or re.fullmatch(r"/[A-Za-z0-9_./-]+", root) is None or ".." in Path(root).parts:
        raise RelayError("remote_project_root 必须是无空格、无 shell 字符、无 .. 的远端绝对路径")
    key = "remote_results_root" if results else "remote_jobs_root"
    relative = config.get(key)
    if not isinstance(relative, str) or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", relative) is None or ".." in Path(relative).parts:
        raise RelayError(f"{key} 必须是安全的相对路径")
    job_id = manifest["job_id"]
    if not isinstance(job_id, str) or slug(job_id) != job_id or job_id in {".", ".."}:
        raise RelayError("job_id 不是安全的任务标识")
    return host, f"{root.rstrip('/')}/{relative.strip('/')}/{job_id}"


def stage_remote_inputs(job_dir: Path, stage: Path, manifest: dict[str, Any]) -> None:
    """Upload only the request and the exact input files referenced by that request."""
    request_path = job_dir / "remote/request.json"
    request = load_json(request_path)
    if not isinstance(request, dict) or request.get("job_id") != manifest["job_id"]:
        raise RelayError("remote/request.json 不属于本任务；请重新 package")
    segments = request.get("payload", {}).get("segments")
    if not isinstance(segments, list) or not segments:
        raise RelayError("远端 request 必须包含 segments")
    paths = set()
    for segment in segments:
        for value in [segment.get("first_frame"), segment.get("last_frame")]:
            paths.add(value)
        for action in segment.get("action_images", []):
            paths.add(action.get("path") if isinstance(action, dict) else action)
    for relative in paths:
        if not isinstance(relative, str) or not relative.startswith("payload/"):
            raise RelayError(f"远端输入必须位于 payload/：{relative!r}")
        source = rel_path(rel_path(job_dir, "remote"), relative)
        if not source.is_file():
            raise RelayError(f"远端输入不存在：{relative}")
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copy2(request_path, stage / "request.json")


def run_command(command: list[str], *, dry_run: bool = False) -> None:
    print("$ " + " ".join(command))
    if dry_run:
        return
    completed = subprocess.run(command, text=True)
    if completed.returncode:
        raise RelayError(f"命令失败（{completed.returncode}）：{' '.join(command)}")


def cmd_sync_up(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    host, remote_root = remote_target(args, job_dir, manifest)
    destination = f"{host}:{remote_root}/"
    # macOS 自带的旧版 rsync 不支持 GNU 风格的 --info=progress2；
    # --partial 在新旧 rsync 上都可用，也保留了中断后续传的基本能力。
    with tempfile.TemporaryDirectory(prefix="cs-relay-upload-") as temporary:
        stage = Path(temporary)
        stage_remote_inputs(job_dir, stage, manifest)
        run_command(["ssh", host, "mkdir", "-p", remote_root], dry_run=args.dry_run)
        run_command(["rsync", "-a", "--partial", str(stage) + "/", destination], dry_run=args.dry_run)
    if not args.dry_run:
        manifest["status"] = "remote_submitted"
        save_manifest(job_dir, manifest)
    return 0


def bind_remote_result(job_dir: Path, manifest: dict[str, Any], result_data: dict[str, Any]) -> tuple[bool, list[str]]:
    """Bind actual downloaded segment files and verify optional remote hashes."""

    job_dir = job_dir.resolve()
    result_segments = result_data.get("segments", [])
    by_id: dict[str, dict[str, Any]] = {}
    for item in result_segments:
        if not isinstance(item, dict):
            continue
        # 远端 cs-relay.result.v1 使用 segment_id；兼容早期本地 fixture 的 id。
        item_id = item.get("segment_id") or item.get("id")
        if item_id:
            by_id[str(item_id)] = item
    missing: list[str] = []
    for segment in manifest.get("segments", []):
        item = by_id.get(segment.get("id"))
        if not item:
            missing.append(f"{segment.get('id')}: result 中没有该 segment")
            continue
        remote_path = item.get("video_path") or item.get("path") or item.get("output")
        if not remote_path and isinstance(item.get("outputs"), list) and item["outputs"]:
            first_output = item["outputs"][0]
            if isinstance(first_output, dict):
                remote_path = first_output.get("path")
                expected_hash = first_output.get("sha256")
            else:
                expected_hash = None
        else:
            expected_hash = item.get("sha256")
        if (
            not isinstance(remote_path, str)
            or Path(remote_path).is_absolute()
            or ".." in Path(remote_path).parts
        ):
            missing.append(f"{segment.get('id')}: 非法 result 路径")
            continue
        try:
            local_path = rel_path(rel_path(job_dir, "remote/results"), remote_path)
        except RelayError:
            missing.append(f"{segment.get('id')}: result 路径逃出结果目录")
            continue
        if not local_path.is_file():
            missing.append(f"{segment.get('id')}: {remote_path}")
            continue
        if expected_hash and expected_hash != sha256(local_path):
            missing.append(f"{segment.get('id')}: SHA-256 不匹配")
            continue
        segment["video_path"] = str(local_path.relative_to(job_dir))
        segment["remote_result"] = {k: v for k, v in item.items() if k not in {"video_path", "path", "output"}}
    status = str(result_data.get("status", "")).lower()
    success = not missing and status in {"success", "completed", "complete", "ok"}
    return success, missing


def cmd_import_result(args: argparse.Namespace) -> int:
    """Validate a portable worker result completely before importing any files."""
    job_dir = Path(args.job_dir).expanduser().resolve()
    source_dir = Path(args.result_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    result_path = rel_path(source_dir, "result.json")
    result = load_json(result_path)
    if not isinstance(result, dict) or result.get("schema_version") != "cs-relay.result.v1":
        raise RelayError("导入结果必须采用 cs-relay.result.v1")
    if result.get("job_id") != manifest["job_id"]:
        raise RelayError("result.job_id 与当前任务不一致")
    if result.get("status") not in {"completed", "complete", "success", "ok"}:
        raise RelayError("只可导入完整成功的生成结果")
    request = job_dir / "remote/request.json"
    if request.is_file() and result.get("request_sha256") != sha256(request):
        raise RelayError("result.request_sha256 不匹配当前 request.json")
    entries = result.get("segments")
    if not isinstance(entries, list) or not entries or any(not isinstance(entry, dict) for entry in entries):
        raise RelayError("result.segments 必须是非空 object 列表")
    expected_ids = {segment.get("id") for segment in manifest.get("segments", [])}
    result_ids = [entry.get("segment_id") or entry.get("id") for entry in entries]
    if any(not isinstance(value, str) for value in result_ids) or len(set(result_ids)) != len(result_ids) or set(result_ids) != expected_ids:
        raise RelayError("result 必须恰好包含任务的全部 segment，且无重复 ID")
    files = {"result.json": (result_path, sha256(result_path))}
    for entry in entries:
        outputs = entry.get("outputs")
        if outputs is None:
            outputs = [{"path": entry.get("video_path") or entry.get("path") or entry.get("output"), "sha256": entry.get("sha256")}]
        if not isinstance(outputs, list) or not outputs:
            raise RelayError("每个结果 segment 必须有输出文件")
        for output in outputs:
            if not isinstance(output, dict):
                raise RelayError("结果输出必须包含 path 和 sha256")
            relative, expected_hash = output.get("path"), output.get("sha256")
            if not isinstance(relative, str) or not relative or relative == "result.json":
                raise RelayError("结果输出路径无效")
            source = rel_path(source_dir, relative)
            if not source.is_file() or not isinstance(expected_hash, str) or sha256(source) != expected_hash:
                raise RelayError(f"结果文件缺失或 SHA-256 不匹配：{relative}")
            if relative in files and files[relative][1] != expected_hash:
                raise RelayError(f"同一结果路径包含冲突内容：{relative}")
            files[relative] = (source, expected_hash)
    destination_dir = rel_path(job_dir, "remote/results")
    for relative, (_, expected_hash) in files.items():
        target = rel_path(destination_dir, relative)
        if target.exists() and (not target.is_file() or sha256(target) != expected_hash):
            raise RelayError(f"已有不同结果，拒绝覆盖：remote/results/{relative}；请使用新任务目录")
    # Binding also validates the primary output contract before the first write.
    staged_manifest = copy.deepcopy(manifest)
    with tempfile.TemporaryDirectory(prefix="cs-relay-result-") as temporary:
        stage = Path(temporary).resolve()
        for relative, (source, expected_hash) in files.items():
            target = stage / "remote/results" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256(target) != expected_hash:
                raise RelayError(f"导入期间来源文件发生变化：{relative}")
        success, missing = bind_remote_result(stage, staged_manifest, result)
        if not success:
            raise RelayError("结果绑定失败：" + "; ".join(missing))
        for relative in files:
            target = rel_path(destination_dir, relative)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stage / "remote/results" / relative, target)
    staged_manifest["status"] = "remote_generated"
    staged_manifest.pop("visual_review", None)
    staged_manifest.setdefault("artifacts", {})["remote_result"] = "remote/results/result.json"
    save_manifest(job_dir, staged_manifest)
    print(job_dir / "remote/results/result.json")
    return 0


def cmd_sync_down(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    host, remote_root = remote_target(args, job_dir, manifest, results=True)
    source = f"{host}:{remote_root}/"
    with tempfile.TemporaryDirectory(prefix="cs-relay-download-") as temporary:
        run_command(["rsync", "-a", "--partial", source, str(Path(temporary)) + "/"], dry_run=args.dry_run)
        if not args.dry_run:
            return cmd_import_result(argparse.Namespace(job_dir=str(job_dir), result_dir=temporary))
    return 0


def ffprobe_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def ffmpeg_has_filter(name: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return any(re.search(rf"\s{name}\s", line) for line in result.stdout.splitlines())


def cmd_assemble(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    if not manifest.get("segments"):
        raise RelayError("没有可剪辑的 segments")
    video = dict(DEFAULT_VIDEO)
    video.update(manifest.get("video", {}))
    output_dir = job_dir / "renders"
    normalized_dir = output_dir / ".normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)
    normalized: list[Path] = []
    for index, segment in enumerate(manifest["segments"], start=1):
        source_value = segment.get("video_path")
        if not source_value:
            raise RelayError(f"segment {segment.get('id', index)} 缺少 video_path")
        source = rel_path(job_dir, source_value)
        if not source.is_file():
            raise RelayError(f"segment 视频不存在：{source_value}")
        target = normalized_dir / f"{index:02d}_{slug(str(segment.get('id', index)))}.mp4"
        filter_graph = (
            f"scale={int(video['width'])}:{int(video['height'])}:force_original_aspect_ratio=decrease,"
            f"pad={int(video['width'])}:{int(video['height'])}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={float(video['fps'])},format=yuv420p"
        )
        run_command([
            "ffmpeg", "-y", "-i", str(source), "-vf", filter_graph,
            "-an", "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            str(target),
        ])
        normalized.append(target)
    concat_list = output_dir / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{path.as_posix().replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n" for path in normalized),
        encoding="utf-8",
    )
    master = output_dir / "master_no_subtitles.mp4"
    run_command(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(master)])
    captions = manifest.get("artifacts", {}).get("captions") or "captions/final.srt"
    captions_path = rel_path(job_dir, captions)
    final = output_dir / "master_burned.mp4"
    events = load_events(job_dir)
    caption_burn_status = "none"
    if captions_path.is_file() and events and ffmpeg_has_filter("subtitles"):
        escaped = str(captions_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        run_command([
            "ffmpeg", "-y", "-i", str(master), "-vf", f"subtitles='{escaped}'",
            "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf), "-c:a", "copy", str(final),
        ])
        caption_burn_status = "burned"
    elif captions_path.is_file() and events:
        caption_burn_status = "srt_only"
        print("WARNING: 当前 FFmpeg 没有 subtitles/libass filter；保留 SRT，不生成伪装成已烧录的文件。", file=sys.stderr)
    else:
        caption_burn_status = "none"
    timeline = {
        "schema_version": "cs-relay.timeline.v1",
        "master_no_subtitles": str(master.relative_to(job_dir)),
        "captions": str(captions_path.relative_to(job_dir)) if captions_path.is_file() else None,
        "caption_burn_status": caption_burn_status,
        "segments": [
            {
                "id": segment.get("id"),
                "video_path": segment.get("video_path"),
                "duration_s": segment.get("duration_s"),
                "caption": segment.get("caption"),
            }
            for segment in manifest.get("segments", [])
        ],
    }
    timeline_path = output_dir / "timeline.json"
    save_json(timeline_path, timeline)
    manifest.setdefault("artifacts", {})["master_no_subtitles"] = str(master.relative_to(job_dir))
    manifest["artifacts"]["timeline"] = str(timeline_path.relative_to(job_dir))
    manifest["artifacts"]["caption_burn_status"] = caption_burn_status
    if caption_burn_status == "burned":
        manifest["artifacts"]["master_burned"] = str(final.relative_to(job_dir))
    manifest["status"] = "local_assembled"
    save_manifest(job_dir, manifest)
    print(json.dumps({
        "master": str(master),
        "burned": str(final) if caption_burn_status == "burned" else None,
        "caption_burn_status": caption_burn_status,
        "timeline": str(timeline_path),
        "duration_s": ffprobe_duration(final if caption_burn_status == "burned" else master),
    }, ensure_ascii=False, indent=2))
    return 0


def visual_review_errors(job_dir: Path, manifest: dict[str, Any]) -> list[str]:
    review = manifest.get("visual_review")
    if not isinstance(review, dict) or review.get("reviewed") is not True or review.get("status") != "approved":
        return ["最终验收需要 visual_review.reviewed=true 且 status=approved；技术检查不代表画面通过"]
    errors = []
    for key in ("reviewer", "note"):
        if not isinstance(review.get(key), str) or not review[key].strip():
            errors.append(f"visual_review 缺少 {key}")
    artifact = review.get("artifact")
    if not isinstance(artifact, dict):
        errors.append("visual_review 缺少经审核成片 artifact 的 path/sha256")
    else:
        errors.extend(_record_file_errors(job_dir, artifact, "visual_review artifact"))
        artifacts = manifest.get("artifacts", {})
        final_key = "master_burned" if manifest.get("caption_policy", {}).get("burn_in") else "master_no_subtitles"
        expected = artifacts.get(final_key)
        if not expected or artifact.get("path") != expected:
            errors.append(f"visual_review.artifact 必须绑定 artifacts.{final_key}")
    return errors


def cmd_validate(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    manifest = load_manifest(job_dir)
    errors: list[str] = []
    warnings: list[str] = []
    if manifest.get("status") not in STATUSES:
        errors.append(f"非法 status：{manifest.get('status')}")
    elif manifest.get("status") == "blocked":
        errors.append("任务当前为 blocked，不能进行最终验收")
    if not manifest.get("players"):
        warnings.append("尚未登记 player/state")
    if not manifest.get("segments"):
        warnings.append("尚未登记 segment")
    errors.extend(strict_asset_errors(job_dir, manifest))
    for index, segment in enumerate(manifest.get("segments", [])):
        if not segment.get("id"):
            errors.append(f"segments[{index}] 缺少 id")
        for key in ("first_frame", "last_frame"):
            value = segment.get(key)
            if value:
                try:
                    if not rel_path(job_dir, value).is_file():
                        errors.append(f"segments[{index}].{key} 不存在：{value}")
                except RelayError as exc:
                    errors.append(str(exc))
    try:
        load_events(job_dir)
    except RelayError as exc:
        errors.append(str(exc))
    burn_status = manifest.get("artifacts", {}).get("caption_burn_status")
    if manifest.get("caption_policy", {}).get("burn_in") and burn_status == "srt_only":
        warnings.append("当前只有 SRT；尚未完成视频内嵌字幕")
    technical_ok = not errors
    review_errors = visual_review_errors(job_dir, manifest)
    if args.mark:
        if manifest.get("status") not in {"local_assembled", "validated"}:
            errors.append("只有已完成本地剪辑的 local_assembled 任务才能最终标记 validated")
        if not manifest.get("segments"):
            errors.append("最终验收至少需要一个 segment")
        if manifest.get("caption_policy", {}).get("burn_in") and burn_status != "burned":
            errors.append("字幕策略要求烧录，但尚未完成")
        errors.extend(review_errors)
    result = {
        "ok": not errors,
        "technical_ok": technical_ok,
        "visual_approved": not review_errors,
        "visual_review_errors": review_errors,
        "errors": errors,
        "warnings": warnings,
        "status": manifest.get("status"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors:
        return 2
    if args.mark:
        manifest["status"] = "validated"
        save_manifest(job_dir, manifest)
    return 0


def cmd_hash(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir).expanduser().resolve()
    load_manifest(job_dir)
    output = job_dir / "reports/checksums.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    excluded = {"reports/checksums.jsonl"}
    rows = []
    for path in sorted(job_dir.rglob("*")):
        if not path.is_file() or ".normalized" in path.parts:
            continue
        relative = str(path.relative_to(job_dir))
        if relative in excluded or ".git" in path.parts:
            continue
        rows.append({"path": relative, "size": path.stat().st_size, "sha256": sha256(path)})
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CS relay Mac-side pipeline controller")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="创建任务目录和固定目录结构")
    p.add_argument("job_dir")
    p.add_argument("--job-id")
    p.add_argument("--config", help="复制显式 JSON 配置到任务；未配置仍可完成本地流程")
    p.add_argument("--asset-policy", choices=("hltv", "user-provided"), default="hltv")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("register-source", help="登记用户提供的 Present/Past 来源图片")
    p.add_argument("job_dir")
    p.add_argument("--player", required=True)
    p.add_argument("--name", help="显示名；默认使用 player")
    p.add_argument("--state", required=True)
    p.add_argument("--period", required=True, choices=("present", "past"))
    p.add_argument("--photo", required=True)
    p.add_argument("--review-status", required=True, choices=("approved", "pending", "rejected"))
    p.add_argument("--review-note", required=True)
    p.set_defaults(func=cmd_register_source)

    p = sub.add_parser(
        "register-player",
        help="登记选手状态；生成用定妆照与排名证据必须分离",
    )
    p.add_argument("job_dir")
    p.add_argument("--player", required=True)
    p.add_argument("--display-name")
    p.add_argument("--state", required=True, help="状态唯一键，例如 current-2026")
    p.add_argument("--period", required=True, choices=("current", "peak", "past"))
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--team", required=True)
    p.add_argument(
        "--profile-url",
        required=True,
        help="只用于核验巅峰年份和当年战队的 HLTV 选手 Profile URL",
    )
    p.add_argument(
        "--profile-evidence",
        action="append",
        required=True,
        help="Profile 页面 HTML 或截图；仅作年份/战队选择证据",
    )
    p.add_argument(
        "--snapshot-url",
        help="照片所在的带日期 HLTV Ranking URL；禁止使用 Team 页面",
    )
    p.add_argument(
        "--snapshot-evidence",
        action="append",
        help="Ranking 页战队卡片与五人横排的 HTML 或截图",
    )
    p.add_argument("--snapshot-team")
    p.add_argument("--snapshot-player")
    p.add_argument("--snapshot-player-count", type=int)
    p.add_argument(
        "--acquisition-method",
        choices=tuple(sorted(ALLOWED_PORTRAIT_ACQUISITION_METHODS)),
        default=PORTRAIT_ACQUISITION_METHOD,
    )
    p.add_argument(
        "--portrait-kind",
        choices=tuple(sorted(ALLOWED_PORTRAIT_KINDS)),
        default="hltv_ranking_snapshot_playerbodyshot",
    )
    p.add_argument(
        "--portrait-provider",
        choices=tuple(sorted(ALLOWED_PORTRAIT_PROVIDERS)),
        default="hltv",
    )
    p.add_argument("--override-reason")
    p.add_argument("--photo", required=True)
    p.add_argument(
        "--photo-source-url",
        required=True,
        help="Ranking 战队卡片实际引用的 /playerbodyshot/ 原图 URL",
    )
    p.add_argument(
        "--photo-source-page-url",
        help="替代参考所在的页面 URL；未提供时回退到原图 URL",
    )
    p.add_argument("--photo-year", type=int, required=True)
    p.add_argument("--photo-team", required=True)
    p.add_argument(
        "--review-status", required=True, choices=("approved", "pending", "rejected")
    )
    p.add_argument("--review-note", required=True)
    p.add_argument(
        "--visual-team-verified",
        action="store_true",
        help="明确确认已目检队服、年份和队伍归属",
    )
    p.add_argument(
        "--teamless-current-identity-only",
        action="store_true",
        help="当前为 None/Retired 时，旧队服资料照仅作身份参考",
    )
    p.add_argument(
        "--visual-identity-verified",
        action="store_true",
        help="明确确认无队伍状态资料照的当前身份与真实来源队服",
    )
    p.add_argument("--hltv-rank", type=int)
    p.add_argument("--ranking-url")
    p.add_argument(
        "--ranking-evidence",
        action="append",
        help="只证明巅峰选择的排名证据，不参与生图",
    )
    p.set_defaults(func=cmd_register_player)

    p = sub.add_parser(
        "register-endpoint",
        help="登记由已审核定妆照生成并复核过的可复用端点",
    )
    p.add_argument("job_dir")
    p.add_argument("--player", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--endpoint", required=True)
    p.add_argument(
        "--review-status", required=True, choices=("approved", "pending", "rejected")
    )
    p.add_argument("--review-note", required=True)
    p.add_argument("--layout-reference", help="user-provided 必需：任务内相对路径，共用的布局参考图")
    p.add_argument("--layout-note", help="user-provided 必需：正脸、人物占比和布局审核说明")
    p.add_argument(
        "--neutralized-teamless-wardrobe",
        action="store_true",
        help="确认端点已去除身份参考照中的旧队服和品牌",
    )
    p.set_defaults(func=cmd_register_endpoint)

    p = sub.add_parser("record-evidence", help="把浏览器导出的 HTML/截图复制进 evidence/")
    p.add_argument("job_dir")
    p.add_argument("--source", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--kind", default="html", choices=("html", "screenshot", "photo", "other"))
    p.add_argument("--source-url")
    p.set_defaults(func=cmd_record_evidence)

    p = sub.add_parser("add-segment", help="登记一个远端生成 segment")
    p.add_argument("job_dir")
    p.add_argument("--id", required=True)
    p.add_argument("--kind", required=True, choices=("age_transition", "player_handoff", "other"))
    p.add_argument("--first-frame", required=True)
    p.add_argument("--last-frame", required=True)
    p.add_argument("--action-image", action="append")
    p.add_argument("--prompt")
    p.add_argument("--prompt-file")
    p.add_argument("--video-path")
    p.add_argument("--seed", type=int)
    p.add_argument("--length", type=int, help="显式 H3 原生帧数，优先于 --duration，必须为 17*k+5")
    p.add_argument("--duration", type=float)
    p.add_argument("--fps", type=float, default=24)
    p.add_argument("--width", type=int, default=544)
    p.add_argument("--height", type=int, default=736)
    p.add_argument("--generation-fps", type=float, default=24)
    p.add_argument("--caption")
    p.set_defaults(func=cmd_add_segment)

    p = sub.add_parser("make-srt", help="从 captions/events.json 生成 SRT")
    p.add_argument("job_dir")
    p.add_argument("--output")
    p.set_defaults(func=cmd_make_srt)

    p = sub.add_parser("package", help="生成只包含远端所需素材的 request 包")
    p.add_argument("job_dir")
    p.add_argument("--clean", action="store_true", help="清理旧 payload 后再复制")
    p.set_defaults(func=cmd_package)

    for name, func, help_text in (("sync-up", cmd_sync_up, "上传 request/payload 到显式配置的远端"), ("sync-down", cmd_sync_down, "下载远端结果")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("job_dir")
        p.add_argument("--host")
        p.add_argument("--remote-root")
        p.add_argument("--config", help="本次同步使用的显式 JSON 配置")
        p.add_argument("--dry-run", action="store_true", help="只打印 rsync 命令")
        p.set_defaults(func=func)

    p = sub.add_parser("assemble", help="在 Mac 上规范化、拼接并烧录字幕")
    p.add_argument("job_dir")
    p.add_argument("--preset", default="medium")
    p.add_argument("--crf", type=int, default=18)
    p.set_defaults(func=cmd_assemble)

    p = sub.add_parser("import-result", help="验证并导入本地或其他后端生成的 result.json 与视频")
    p.add_argument("job_dir")
    p.add_argument("result_dir", help="包含 result.json 的结果目录")
    p.set_defaults(func=cmd_import_result)

    p = sub.add_parser("validate", help="检查 manifest、素材引用和字幕")
    p.add_argument("job_dir")
    p.add_argument("--mark", action="store_true", help="通过后把 local_assembled 标成 validated")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("hash", help="生成全任务 SHA-256 清单")
    p.add_argument("job_dir")
    p.set_defaults(func=cmd_hash)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RelayError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
