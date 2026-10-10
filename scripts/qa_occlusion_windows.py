#!/usr/bin/env python3
"""Build a dense contact sheet around the transition window of age clips.

This is a visual acceptance artifact, not an automatic pass/fail classifier.
It samples the interval in which an occluder should fully cover the frame so a
reviewer can verify that the old state remains visible before the seal and the
new state is already established when the seal opens.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from qa_generated_segments import frame_indexes


SAMPLE_FRACTIONS = (0.22, 0.30, 0.38, 0.46, 0.54, 0.62, 0.70, 0.78)
TILE_SIZE = (136, 184)
LABEL_HEIGHT = 28


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def extract(video: Path, frame_index: int, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"select=eq(n\\,{frame_index})",
            "-vsync",
            "0",
            "-frames:v",
            "1",
            str(output),
        ],
        text=True,
        capture_output=True,
    )
    if completed.returncode or not output.is_file():
        raise RuntimeError(completed.stderr.strip() or f"frame {frame_index} was not written")


def tile(path: Path, label: str) -> Image.Image:
    source = Image.open(path).convert("RGB")
    source.thumbnail(TILE_SIZE, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (TILE_SIZE[0], TILE_SIZE[1] + LABEL_HEIGHT), "white")
    canvas.paste(source, ((TILE_SIZE[0] - source.width) // 2, (TILE_SIZE[1] - source.height) // 2))
    ImageDraw.Draw(canvas).text((4, TILE_SIZE[1] + 4), label, fill="black", font=font(12))
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", type=Path)
    args = parser.parse_args()

    job_dir = args.job_dir.expanduser().resolve()
    request = load_json(job_dir / "remote" / "request.json")
    result = load_json(job_dir / "remote" / "results" / "result.json")
    completed = {item["segment_id"]: item for item in result.get("segments", [])}
    requested = [
        item for item in request["payload"]["segments"]
        if item.get("workflow") == "age_transition" and item.get("segment_id") in completed
    ]
    if not requested:
        raise RuntimeError("no completed age_transition segments found")

    qa_dir = job_dir / "qa" / "occlusion-window"
    rows: list[list[Image.Image]] = []
    for segment in requested:
        segment_id = segment["segment_id"]
        output = completed[segment_id]["outputs"][0]["path"]
        video = job_dir / "remote" / "results" / output
        row: list[Image.Image] = []
        last = frame_indexes(video)["last"]
        for frame_index in (round(last * fraction) for fraction in SAMPLE_FRACTIONS):
            frame = qa_dir / "frames" / f"{segment_id}-{frame_index:03d}.png"
            extract(video, frame_index, frame)
            row.append(tile(frame, f"{segment_id} f{frame_index}"))
        rows.append(row)

    canvas = Image.new(
        "RGB",
        (len(SAMPLE_FRACTIONS) * TILE_SIZE[0], len(rows) * (TILE_SIZE[1] + LABEL_HEIGHT)),
        "#b8b8b8",
    )
    for row_index, row in enumerate(rows):
        for column_index, item in enumerate(row):
            canvas.paste(item, (column_index * TILE_SIZE[0], row_index * (TILE_SIZE[1] + LABEL_HEIGHT)))
    output = qa_dir / "occlusion-window-contact-sheet.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, quality=95)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
