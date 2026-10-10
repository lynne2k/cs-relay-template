#!/usr/bin/env python3
"""Create deterministic visual-QA artifacts for generated CS relay segments."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


TILE_SIZE = (272, 368)
LABEL_HEIGHT = 44


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def frame_indexes(video: Path) -> dict[str, int]:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=nb_read_frames", "-of", "json", str(video),
    ], text=True, capture_output=True, check=True)
    count = int(json.loads(result.stdout)["streams"][0]["nb_read_frames"])
    if count < 1:
        raise RuntimeError("video has no decodable frames")
    return {"first": 0, "middle": (count - 1) // 2, "last": count - 1}


def extract_frame(video: Path, frame_index: int, output: Path) -> None:
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
        detail = completed.stderr.strip() or "frame was not written"
        raise RuntimeError(f"Failed to extract frame {frame_index} from {video}: {detail}")


def read_rgb(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)


def ssim(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} != {b.shape}")
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    mu_a = cv2.GaussianBlur(a, (11, 11), 1.5)
    mu_b = cv2.GaussianBlur(b, (11, 11), 1.5)
    mu_a2 = mu_a * mu_a
    mu_b2 = mu_b * mu_b
    mu_ab = mu_a * mu_b
    sigma_a2 = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu_a2
    sigma_b2 = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu_b2
    sigma_ab = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu_ab
    numerator = (2 * mu_ab + c1) * (2 * sigma_ab + c2)
    denominator = (mu_a2 + mu_b2 + c1) * (sigma_a2 + sigma_b2 + c2)
    return float(np.mean(numerator / np.maximum(denominator, 1e-12)))


def compare(a_path: Path, b_path: Path) -> dict:
    a = read_rgb(a_path)
    b = read_rgb(b_path)
    if a.shape != b.shape:
        b = np.asarray(
            Image.fromarray(b.astype(np.uint8)).resize((a.shape[1], a.shape[0]), Image.Resampling.LANCZOS),
            dtype=np.float64,
        )
    delta = a - b
    mse = float(np.mean(delta * delta))
    mae = float(np.mean(np.abs(delta)))
    psnr = math.inf if mse == 0 else float(20 * math.log10(255 / math.sqrt(mse)))
    return {
        "mae_0_255": round(mae, 6),
        "psnr_db": "inf" if math.isinf(psnr) else round(psnr, 6),
        "ssim": round(ssim(a, b), 8),
    }


def font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def tile(path: Path, label: str) -> Image.Image:
    image = Image.open(path).convert("RGB")
    image.thumbnail(TILE_SIZE, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (TILE_SIZE[0], TILE_SIZE[1] + LABEL_HEIGHT), "white")
    x = (TILE_SIZE[0] - image.width) // 2
    y = (TILE_SIZE[1] - image.height) // 2
    canvas.paste(image, (x, y))
    draw = ImageDraw.Draw(canvas)
    draw.text((8, TILE_SIZE[1] + 5), label, fill="black", font=font(15))
    return canvas


def sheet(groups: list[list[tuple[Path, str]]], groups_per_row: int, output: Path) -> None:
    group_width = len(groups[0]) * TILE_SIZE[0]
    row_height = TILE_SIZE[1] + LABEL_HEIGHT
    rows = math.ceil(len(groups) / groups_per_row)
    canvas = Image.new("RGB", (group_width * groups_per_row, row_height * rows), "#d8d8d8")
    for index, group in enumerate(groups):
        row, column = divmod(index, groups_per_row)
        for item_index, (path, label) in enumerate(group):
            canvas.paste(tile(path, label), (column * group_width + item_index * TILE_SIZE[0], row * row_height))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output, quality=95)


def local_payload_path(job_dir: Path, request_path: str) -> Path:
    prefix = "payload/"
    if not request_path.startswith(prefix):
        raise ValueError(f"unexpected request path: {request_path}")
    return job_dir / "remote" / "payload" / request_path[len(prefix):]


def run_manifest_variant(job_dir: Path) -> dict:
    """QA an integrated local variant whose videos are bound in job.json."""
    manifest = load_json(job_dir / "job.json")
    segments = manifest.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("job.json has no segments")

    qa_dir = job_dir / "qa"
    frame_dir = qa_dir / "generated-frames"
    segment_groups: list[list[tuple[Path, str]]] = []
    rows: list[dict] = []
    for segment in segments:
        segment_id = str(segment.get("id") or "")
        video_path = segment.get("video_path")
        first_path = segment.get("first_frame")
        last_path = segment.get("last_frame")
        if not all(isinstance(value, str) and value for value in (segment_id, video_path, first_path, last_path)):
            raise RuntimeError(f"incomplete manifest segment: {segment!r}")
        video = job_dir / video_path
        source_first = job_dir / first_path
        source_last = job_dir / last_path
        for required in (video, source_first, source_last):
            if not required.is_file():
                raise RuntimeError(f"missing QA input: {required}")
        extracted: dict[str, Path] = {}
        indexes = frame_indexes(video)
        for name, frame_index in indexes.items():
            path = frame_dir / f"{segment_id}-{name}.png"
            extract_frame(video, frame_index, path)
            extracted[name] = path
        rows.append({
            "segment_id": segment_id,
            "video": str(video.relative_to(job_dir)),
            "frame_indexes": indexes,
            "first_vs_source": compare(extracted["first"], source_first),
            "last_vs_source": compare(extracted["last"], source_last),
            "frames": {name: str(path.relative_to(job_dir)) for name, path in extracted.items()},
        })
        segment_groups.append([
            (extracted["first"], f"{segment_id} | first"),
            (extracted["middle"], f"{segment_id} | middle"),
            (extracted["last"], f"{segment_id} | last"),
        ])

    seam_groups: list[list[tuple[Path, str]]] = []
    seams: list[dict] = []
    for index in range(len(segments) - 1):
        left = segments[index]
        right = segments[index + 1]
        if left.get("last_frame") != right.get("first_frame"):
            raise RuntimeError(f"adjacent segments do not share one endpoint: {left.get('id')} -> {right.get('id')}")
        left_frame = job_dir / rows[index]["frames"]["last"]
        right_frame = job_dir / rows[index + 1]["frames"]["first"]
        source = job_dir / str(left["last_frame"])
        seam_id = f"{left['id']}__{right['id']}"
        seams.append({
            "seam_id": seam_id,
            "shared_source": str(source.relative_to(job_dir)),
            "left_last_vs_source": compare(left_frame, source),
            "right_first_vs_source": compare(right_frame, source),
            "left_last_vs_right_first": compare(left_frame, right_frame),
        })
        seam_groups.append([
            (left_frame, f"{left['id']} | last"),
            (source, "shared endpoint"),
            (right_frame, f"{right['id']} | first"),
        ])

    segments_sheet = qa_dir / "generated-segments-contact-sheet.png"
    seams_sheet = qa_dir / "all-seams-contact-sheet.png"
    sheet(segment_groups, groups_per_row=2, output=segments_sheet)
    if seam_groups:
        sheet(seam_groups, groups_per_row=2, output=seams_sheet)
    report = {
        "schema_version": "cs-relay.generated-visual-qa.v1",
        "job_id": manifest.get("job_id"),
        "mode": "integrated_manifest",
        "frame_sampling": "first, middle and last decoded frame of each segment",
        "segments": rows,
        "adjacent_seams": seams,
        "artifacts": {
            "segments_contact_sheet": str(segments_sheet.relative_to(job_dir)),
            "all_seams_contact_sheet": str(seams_sheet.relative_to(job_dir)) if seam_groups else None,
        },
    }
    write_path = qa_dir / "generated-visual-metrics.json"
    write_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def run(job_dir: Path) -> dict:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required")
    if not (job_dir / "remote" / "request.json").is_file():
        return run_manifest_variant(job_dir)
    request = load_json(job_dir / "remote" / "request.json")
    result = load_json(job_dir / "remote" / "results" / "result.json")
    requested = request["payload"]["segments"]
    completed = result["segments"]
    if len(requested) != len(completed):
        raise RuntimeError(f"request/result segment mismatch: {len(requested)} != {len(completed)}")

    qa_dir = job_dir / "qa"
    frame_dir = qa_dir / "generated-frames"
    segment_groups: list[list[tuple[Path, str]]] = []
    rows: list[dict] = []

    for req, res in zip(requested, completed, strict=True):
        segment_id = req["segment_id"]
        if res.get("segment_id") != segment_id or res.get("status") != "completed":
            raise RuntimeError(f"unexpected result for {segment_id}: {res.get('segment_id')} {res.get('status')}")
        video = job_dir / "remote" / "results" / res["outputs"][0]["path"]
        extracted: dict[str, Path] = {}
        indexes = frame_indexes(video)
        for name, frame_index in indexes.items():
            path = frame_dir / f"{segment_id}-{name}.png"
            extract_frame(video, frame_index, path)
            extracted[name] = path
        source_first = local_payload_path(job_dir, req["first_frame"])
        source_last = local_payload_path(job_dir, req["last_frame"])
        rows.append(
            {
                "segment_id": segment_id,
                "video": str(video.relative_to(job_dir)),
                "frame_indexes": indexes,
                "first_vs_source": compare(extracted["first"], source_first),
                "last_vs_source": compare(extracted["last"], source_last),
                "frames": {name: str(path.relative_to(job_dir)) for name, path in extracted.items()},
            }
        )
        segment_groups.append(
            [
                (extracted["first"], f"{segment_id} | first"),
                (extracted["middle"], f"{segment_id} | middle"),
                (extracted["last"], f"{segment_id} | last"),
            ]
        )

    seam_groups: list[list[tuple[Path, str]]] = []
    seams: list[dict] = []
    for index in range(len(requested) - 1):
        left_req = requested[index]
        right_req = requested[index + 1]
        if left_req["last_frame"] != right_req["first_frame"]:
            # A repair request may contain only non-adjacent segments.  Those
            # pairs have no seam to score; integrated variants are checked by
            # ``run_manifest_variant`` after replacements are selected.
            continue
        left_frame = job_dir / rows[index]["frames"]["last"]
        right_frame = job_dir / rows[index + 1]["frames"]["first"]
        source = local_payload_path(job_dir, left_req["last_frame"])
        seam_id = f"{left_req['segment_id']}__{right_req['segment_id']}"
        seams.append(
            {
                "seam_id": seam_id,
                "shared_source": str(source.relative_to(job_dir)),
                "left_last_vs_source": compare(left_frame, source),
                "right_first_vs_source": compare(right_frame, source),
                "left_last_vs_right_first": compare(left_frame, right_frame),
            }
        )
        short = left_req["segment_id"].split("-", 1)[-1].replace("-age", "")
        seam_groups.append(
            [
                (left_frame, f"{short} | previous last"),
                (source, f"{short} | shared source"),
                (right_frame, f"{short} | next first"),
            ]
        )

    segments_sheet = qa_dir / "generated-segments-contact-sheet.png"
    seams_sheet = qa_dir / "cycle-seams-contact-sheet.png"
    sheet(segment_groups, groups_per_row=2, output=segments_sheet)
    if seam_groups:
        sheet(seam_groups, groups_per_row=2, output=seams_sheet)
    report = {
        "schema_version": "cs-relay.generated-visual-qa.v1",
        "job_id": result.get("job_id"),
        "frame_sampling": "first, middle and last decoded frame of each segment",
        "segments": rows,
        "cycle_seams": seams,
        "artifacts": {
            "segments_contact_sheet": str(segments_sheet.relative_to(job_dir)),
            "cycle_seams_contact_sheet": (
                str(seams_sheet.relative_to(job_dir)) if seam_groups else None
            ),
        },
    }
    report_path = qa_dir / "generated-visual-metrics.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_dir")
    args = parser.parse_args()
    report = run(Path(args.job_dir).expanduser().resolve())
    print(json.dumps(report["artifacts"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
