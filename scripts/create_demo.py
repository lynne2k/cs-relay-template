#!/usr/bin/env python3
"""Build a local synthetic three-segment package; no people, network or GPU.

The geometric fixtures exercise plumbing, not portrait generation or quality.
Run from any working directory with an empty output job directory.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

import cs_relay


def synthetic_png(path: Path, color: tuple[int, int, int]) -> None:
    width, height = 160, 200
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            head = (x - 80) ** 2 + (y - 60) ** 2 < 28 ** 2
            torso = 42 <= x <= 118 and 92 <= y < 180
            pixels.extend((225, 200, 160) if head else color if torso else (128, 128, 128))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(pixels))) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def call(arguments: list[str]) -> None:
    with contextlib.redirect_stdout(io.StringIO()):
        code = cs_relay.main(arguments)
    if code:
        raise RuntimeError(f"demo command failed: {arguments[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    args = parser.parse_args()
    job = args.job_dir.expanduser().resolve()
    root = Path(__file__).resolve().parents[1]
    call(["init", str(job), "--asset-policy", "user-provided"])
    fixtures = job / "fixtures"
    layout = fixtures / "layout.png"
    synthetic_png(layout, (70, 80, 100))
    people = (("person-a", (40, 110, 180), (190, 90, 60)),
              ("person-b", (80, 140, 80), (140, 70, 150)))
    nodes = []
    for player, present, past in people:
        for state, color in (("present", present), ("past", past)):
            source = fixtures / f"{player}-{state}.png"
            synthetic_png(source, color)
            call(["register-source", str(job), "--player", player, "--state", state,
                  "--period", state, "--photo", str(source), "--review-status", "approved",
                  "--review-note", "Synthetic geometry fixture; not a portrait or a real-person review."])
            call(["register-endpoint", str(job), "--player", player, "--state", state,
                  "--endpoint", str(source), "--review-status", "approved",
                  "--review-note", "Deterministic synthetic endpoint, same fixed geometry.",
                  "--layout-reference", "fixtures/layout.png",
                  "--layout-note", "Synthetic fixture head and torso share exactly the same coordinates."])
        nodes.append({"player": player, "current": "present", "past": "past",
                      "current_caption": f"{player} | Present", "past_caption": f"{player} | Past"})
    prompts = job / "prompts"
    prompts.mkdir(exist_ok=True)
    for name in ("age_transition_shirt_removal_v2.txt", "player_handoff_v2.txt"):
        shutil.copy2(root / "templates/prompts" / name, prompts / name)
    plan = {"schema_version": "cs-relay.chain-plan.v1", "seed_base": 100,
            "segment_frames": 107, "fps": 24, "duration_seconds_requested": 4,
            "generation_width": 544, "generation_height": 736,
            "age_prompt_file": "prompts/age_transition_shirt_removal_v2.txt",
            "handoff_prompt_file": "prompts/player_handoff_v2.txt",
            "terminal_age_transition": True,
            "caption_policy": {"format": "Name | State", "burn_in": False,
                               "background": "none", "editor": "ChatCut"},
            "nodes": nodes}
    plan_path = job / "chain-plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(root / "scripts/build_relay_chain.py"),
                             str(job), str(plan_path)], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    call(["make-srt", str(job)])
    call(["package", str(job)])
    call(["validate", str(job)])
    manifest = cs_relay.load_manifest(job)
    if len(manifest["segments"]) != 3:
        raise RuntimeError("demo expected two-person chain plus terminal half-cycle")
    request = cs_relay.load_json(job / "remote/request.json")
    segments = request["payload"]["segments"]
    for left, right in zip(segments, segments[1:]):
        if left["last_frame"] != right["first_frame"]:
            raise RuntimeError("demo endpoints are not shared")
    print(json.dumps({"job_id": job.name, "segments": 3, "frames_planned": 321,
                      "status": "package_ready", "synthetic": True,
                      "network_requests": 0, "gpu_submissions": 0,
                      "visual_approved": False}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
