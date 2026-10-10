#!/usr/bin/env python3
"""Execute a portable ``cs-relay.job.v1`` package on a user-managed ComfyUI.

The Mac owns browser research, HLTV/photo provenance, subtitles, and final
editing.  This adapter owns only generation work: validate the package,
map its segments to the existing MiniMax H3 FL2VA API graph, submit them in
order, and persist reports and hashed outputs.  It never fetches source data
from the internet and only reads assets below ``input/``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Set only by configure_runtime(). The repository is independent of the server.
ROOT: Path
INPUT_ROOT: Path
OUTPUT_ROOT: Path
JOB_INPUT_ROOT: Path
RESULTS_SUBDIR = Path("cs_relay_jobs")
SCHEMA_VERSION = "cs-relay.job.v1"
RESULT_SCHEMA_VERSION = "cs-relay.result.v1"
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SEGMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DEFAULT_MODELS = {
    "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "lora": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
    "clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "sampler": "res_multistep",
    "scheduler": "simple",
    "steps": 4,
    "denoise": 1.0,
}

# Keep generation prompts generic.  Player names, years, and teams belong in
# the provenance manifest and endpoint images; they must never be sent to H3.
WORKFLOWS = {
    "age": "age_transition",
    "age_transition": "age_transition",
    "03": "age_transition",
    "handoff": "player_handoff",
    "player_handoff": "player_handoff",
    "04": "player_handoff",
}


class JobError(Exception):
    def __init__(self, message: str, code: str = "job_error") -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def configure_runtime(root: Path, jobs_subdir: str = "cs_relay_jobs", results_subdir: str = "cs_relay_jobs") -> None:
    global ROOT, INPUT_ROOT, OUTPUT_ROOT, JOB_INPUT_ROOT, RESULTS_SUBDIR
    for value in (jobs_subdir, results_subdir):
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path == Path("."):
            raise JobError("job/result subdirectories must be nonempty relative paths", "invalid_subdirectory")
    ROOT = root.expanduser().resolve()
    INPUT_ROOT, OUTPUT_ROOT = ROOT / "input", ROOT / "output"
    JOB_INPUT_ROOT = INPUT_ROOT / jobs_subdir
    RESULTS_SUBDIR = Path(results_subdir)
    if not INPUT_ROOT.is_dir():
        raise JobError("--comfyui-root must contain an input directory", "invalid_root")


def checked_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise JobError("ComfyUI URL must be HTTP(S), without credentials, query or fragment", "invalid_url")
    return value.rstrip("/")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_value(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def valid_id(value: str, pattern: re.Pattern[str], field: str) -> str:
    if not pattern.fullmatch(value):
        raise JobError(f"invalid {field}: {value!r}", f"invalid_{field}")
    return value


def input_asset(job_dir: Path, raw: Any, field: str) -> tuple[str, Path]:
    if not isinstance(raw, str) or not raw.strip():
        raise JobError(f"{field} must be a non-empty path", "missing_asset")
    path = Path(raw.strip())
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "payload":
        raise JobError(f"{field} must be a relative path under payload/", "invalid_asset")
    candidates = [job_dir / path]
    root = INPUT_ROOT.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if inside(resolved, root) and inside(resolved, job_dir / "payload") and resolved.is_file():
            return str(resolved.relative_to(root)), resolved
    raise JobError(f"{field} is not a file below input/: {raw}", "missing_asset")


def check_endpoint(path: Path, width: int, height: int) -> str:
    # Packages use canonical PNGs so first/last frame nodes see identical geometry.
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise JobError("packaged endpoints must be canonical PNG images", "invalid_endpoint")
    if struct.unpack(">II", header[16:24]) != (width, height):
        raise JobError("endpoint dimensions must exactly match generation dimensions", "endpoint_size_mismatch")
    return sha256_file(path)


def frame_length(duration: float, fps: float) -> int:
    requested = max(5, round(duration * fps))
    # H3 uses 17*k+5 frames.  Round upward to avoid making a requested clip shorter.
    return 17 * max(0, math.ceil((requested - 5) / 17)) + 5


def node(class_type: str, **inputs: Any) -> dict[str, Any]:
    return {"class_type": class_type, "inputs": inputs}


def model_config(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise JobError("segment.model must be an object", "invalid_model")
    result = copy.deepcopy(DEFAULT_MODELS)
    aliases = {"diffusion": "unet", "diffusion_model": "unet"}
    for key, value in raw.items():
        key = aliases.get(key, key)
        if key in result:
            result[key] = value
    for key in ("unet", "lora", "clip", "video_vae", "audio_vae", "sampler", "scheduler"):
        if not isinstance(result[key], str) or not result[key]:
            raise JobError(f"model.{key} must be a non-empty string", "invalid_model")
    if not isinstance(result["steps"], int) or result["steps"] <= 0:
        raise JobError("model.steps must be a positive integer", "invalid_model")
    if not isinstance(result["denoise"], (int, float)) or result["denoise"] <= 0:
        raise JobError("model.denoise must be positive", "invalid_model")
    return result


def normalize_request(raw_request: dict[str, Any], job_id: str, job_dir: Path) -> dict[str, Any]:
    if raw_request.get("schema_version") != SCHEMA_VERSION:
        raise JobError(f"schema_version must be {SCHEMA_VERSION!r}", "unsupported_schema")
    if raw_request.get("job_id") not in (None, job_id):
        raise JobError("request job_id does not match its directory", "job_id_mismatch")
    payload = raw_request.get("payload")
    segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list) or not segments:
        raise JobError("payload.segments must be a non-empty array", "invalid_segments")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(segments, 1):
        if not isinstance(raw, dict):
            raise JobError(f"segment {index} must be an object", "invalid_segment")
        segment_id = str(raw.get("segment_id") or raw.get("id") or f"segment-{index:02d}")
        valid_id(segment_id, SEGMENT_ID_RE, "segment_id")
        if segment_id in seen:
            raise JobError(f"duplicate segment_id: {segment_id}", "duplicate_segment")
        seen.add(segment_id)

        workflow_key = str(raw.get("workflow") or raw.get("type") or "").strip().lower()
        if workflow_key not in WORKFLOWS:
            raise JobError(f"segment {segment_id}: unsupported workflow {workflow_key!r}", "unsupported_workflow")
        workflow = WORKFLOWS[workflow_key]
        first, first_file = input_asset(job_dir, raw.get("first_frame") or raw.get("first"), "first_frame")
        last, last_file = input_asset(job_dir, raw.get("last_frame") or raw.get("last"), "last_frame")

        action_images = []
        raw_actions = raw.get("action_images") or []
        if not isinstance(raw_actions, list):
            raise JobError(f"segment {segment_id}: action_images must be an array", "invalid_action_images")
        for action in raw_actions:
            if isinstance(action, str):
                action = {"path": action}
            if not isinstance(action, dict) or not isinstance(action.get("path"), str):
                raise JobError(f"segment {segment_id}: action image needs path", "invalid_action_images")
            action_path, _ = input_asset(job_dir, action["path"], "action_images.path")
            action_images.append({"path": action_path, "role": str(action.get("role") or "reference")})

        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise JobError(f"segment {segment_id}: packaged prompt text is required", "missing_prompt")
        seed = raw.get("seed")
        if not isinstance(seed, int) or seed < 0:
            raise JobError(f"segment {segment_id}: seed must be non-negative integer", "invalid_seed")

        size = raw.get("size") if isinstance(raw.get("size"), dict) else {}
        width, height = raw.get("width", size.get("width", 544)), raw.get("height", size.get("height", 736))
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0 or width % 32 or height % 32:
            raise JobError(f"segment {segment_id}: width/height must be positive multiples of 32", "invalid_size")
        fps = raw.get("fps", 24.0)
        if not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
            raise JobError(f"segment {segment_id}: fps must be positive", "invalid_fps")
        requested_duration = raw.get("duration_seconds", raw.get("duration"))
        if requested_duration is not None and (not isinstance(requested_duration, (int, float)) or not math.isfinite(requested_duration) or requested_duration <= 0):
            raise JobError(f"segment {segment_id}: duration_seconds must be positive", "invalid_duration")
        length = raw.get("length")
        if length is None:
            length = frame_length(float(requested_duration or 4.0), float(fps))
        if not isinstance(length, int) or length < 5 or (length - 5) % 17:
            raise JobError(f"segment {segment_id}: length must equal 17*k+5", "invalid_length")

        normalized.append({
            "segment_id": segment_id,
            "workflow": workflow,
            "workflow_version": "minimax-h3-fl2va.v1",
            "first_frame": first,
            "last_frame": last,
            "first_frame_sha256": check_endpoint(first_file, width, height),
            "last_frame_sha256": check_endpoint(last_file, width, height),
            "action_images": action_images,
            "prompt": prompt,
            "seed": seed,
            "model": model_config(raw.get("model")),
            "width": width,
            "height": height,
            "fps": float(fps),
            "length": length,
            "duration_seconds_requested": requested_duration,
            "duration_seconds_generated": length / float(fps),
        })
    return {"schema_version": SCHEMA_VERSION, "job_id": job_id, "payload": {"segments": normalized}}


def api_payload(job_id: str, segment: dict[str, Any]) -> dict[str, Any]:
    model = segment["model"]
    graph = {
        "1": node("LoadImage", image=segment["first_frame"]),
        "2": node("LoadImage", image=segment["last_frame"]),
        "3": node("UNETLoader", unet_name=model["unet"], weight_dtype="default"),
        "4": node("LoraLoaderModelOnly", model=["3", 0], lora_name=model["lora"], strength_model=1.0),
        "5": node("CLIPLoader", clip_name=model["clip"], type="minimax", device="default"),
        "6": node("VAELoader", vae_name=model["video_vae"]),
        "7": node("VAELoader", vae_name=model["audio_vae"]),
        "8": node("MiniMaxH3ImageToVideo", clip=["5", 0], vae=["6", 0], prompt=segment["prompt"], width=segment["width"], height=segment["height"], length=segment["length"], first_frame=["1", 0], last_frame=["2", 0]),
        "9": node("RandomNoise", noise_seed=segment["seed"]),
        "10": node("KSamplerSelect", sampler_name=model["sampler"]),
        "11": node("BasicScheduler", model=["4", 0], scheduler=model["scheduler"], steps=model["steps"], denoise=model["denoise"]),
        "12": node("BasicGuider", model=["4", 0], conditioning=["8", 0]),
        "13": node("SamplerCustomAdvanced", noise=["9", 0], guider=["12", 0], sampler=["10", 0], sigmas=["11", 0], latent_image=["8", 1]),
        "14": node("VAEDecode", samples=["13", 0], vae=["6", 0]),
        "15": node("VAEDecodeAudio", samples=["13", 0], vae=["7", 0]),
        "16": node("CreateVideo", images=["14", 0], audio=["15", 0], fps=segment["fps"], bit_depth=8, color_space="sRGB"),
        "17": node("SaveVideo", video=["16", 0], filename_prefix=f"{RESULTS_SUBDIR.as_posix()}/{job_id}/segments/{segment['segment_id']}", format="mp4", codec="h264"),
    }
    return {"prompt": graph, "client_id": f"cs-relay:{job_id}"}


def http_json(url: str, method: str = "GET", body: Any = None, timeout: float = 30) -> Any:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise JobError(f"HTTP {error.code} from {url}: {detail[:1000]}", "remote_http_error") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise JobError(f"cannot reach ComfyUI API {url}: {error}", "remote_unreachable") from error


def preflight(base_url: str, segments: list[dict[str, Any]], check_models: bool) -> dict[str, Any]:
    http_json(base_url.rstrip("/") + "/system_stats", timeout=20)
    info = http_json(base_url.rstrip("/") + "/object_info/MiniMaxH3ImageToVideo", timeout=20)
    data = info.get("MiniMaxH3ImageToVideo", {}) if isinstance(info, dict) else {}
    inputs = data.get("input", {})
    names = set(inputs.get("optional", {})) | set(inputs.get("required", {}))
    if not {"first_frame", "last_frame"} <= names:
        raise JobError("MiniMaxH3ImageToVideo lacks first_frame/last_frame", "missing_keyframe_inputs")
    # Do not persist hardware, host or user information from system_stats.
    result: dict[str, Any] = {"reachable": True, "node": "MiniMaxH3ImageToVideo", "node_schema_sha256": sha256_value(data)}
    if check_models:
        checks = {
            ("UNETLoader", "unet_name"): {s["model"]["unet"] for s in segments},
            ("LoraLoaderModelOnly", "lora_name"): {s["model"]["lora"] for s in segments},
            ("CLIPLoader", "clip_name"): {s["model"]["clip"] for s in segments},
            ("VAELoader", "vae_name"): {s["model"][k] for s in segments for k in ("video_vae", "audio_vae")},
        }
        result["model_checks"] = {}
        for (node_name, field), required in checks.items():
            node_info = http_json(base_url.rstrip("/") + "/object_info/" + node_name, timeout=20)
            choices = node_info.get(node_name, {}).get("input", {}).get("required", {}).get(field, [[]])[0]
            missing = sorted(set(required) - set(choices))
            result["model_checks"][f"{node_name}.{field}"] = {"required": sorted(required), "missing": missing}
            if missing:
                raise JobError(f"runtime cannot see model(s) for {node_name}.{field}: {missing}", "missing_model")
    return result


def submit(base_url: str, payload: dict[str, Any]) -> str:
    result = http_json(base_url.rstrip("/") + "/prompt", "POST", payload, 60)
    if isinstance(result, dict) and result.get("node_errors"):
        raise JobError("ComfyUI rejected workflow: " + json.dumps(result["node_errors"], ensure_ascii=False), "remote_node_error")
    if not isinstance(result, dict) or not result.get("prompt_id"):
        raise JobError(f"ComfyUI returned no prompt_id: {result!r}", "remote_submit_error")
    return str(result["prompt_id"])


def history(base_url: str, prompt_id: str) -> dict[str, Any] | None:
    result = http_json(base_url.rstrip("/") + "/history/" + prompt_id, timeout=30)
    entry = result.get(prompt_id) if isinstance(result, dict) else None
    return entry if isinstance(entry, dict) else None


def video_files(entry: dict[str, Any], output_root: Path) -> list[Path]:
    files: list[Path] = []
    for output in (entry.get("outputs") or {}).values():
        if not isinstance(output, dict):
            continue
        for key in ("videos", "gifs", "images"):
            for item in output.get(key, []) or []:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                item_type = item.get("type", "output")
                if item_type not in {"output", "temp"}:
                    continue
                root = output_root if item_type == "output" else ROOT / "temp"
                candidate = (root / str(item.get("subfolder") or "") / str(item["filename"])).resolve()
                if inside(candidate, root) and candidate.is_file() and candidate.suffix.lower() == ".mp4":
                    files.append(candidate)
    return files


def wait_for_video(base_url: str, prompt_id: str, output_root: Path, timeout: float, interval: float) -> tuple[dict[str, Any], list[Path]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = history(base_url, prompt_id)
        if entry is not None:
            status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
            if status.get("status_str") in {"error", "failed"}:
                raise JobError(f"ComfyUI execution failed: {json.dumps(status, ensure_ascii=False)}", "remote_execution_error")
            files = video_files(entry, output_root)
            if files:
                return entry, files
        time.sleep(interval)
    raise JobError(f"timed out waiting for prompt {prompt_id}", "remote_timeout")


def report_hashes(reports_dir: Path) -> dict[str, str]:
    return {str(path.relative_to(reports_dir.parent)): sha256_file(path) for path in sorted(reports_dir.rglob("*")) if path.is_file()}


def write_result(path: Path, result: dict[str, Any], reports_dir: Path) -> None:
    result["report_sha256"] = report_hashes(reports_dir)
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_json(temporary, result)
    temporary.replace(path)


def base_result(job_id: str, request_path: Path) -> dict[str, Any]:
    request_label = str(request_path.relative_to(INPUT_ROOT))
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "job_id": job_id,
        "request": request_label,
        "started_at": utc_now(),
        "status": "validating",
        "adapter": "comfyui-h3",
        "owned_by_remote": ["workflow_mapping", "comfyui_submission", "video_segment_output"],
        "owned_by_client": ["browser_research", "photo_provenance", "subtitles", "final_editing"],
        "segments": [],
        "errors": [],
    }


def upsert_segment(result: dict[str, Any], item: dict[str, Any]) -> None:
    result["segments"] = [old for old in result.get("segments", []) if old.get("segment_id") != item.get("segment_id")]
    result["segments"].append(item)


def run_job(args: argparse.Namespace) -> int:
    configure_runtime(args.comfyui_root, getattr(args, "jobs_subdir", "cs_relay_jobs"), getattr(args, "results_subdir", "cs_relay_jobs"))
    args.url = checked_url(args.url)
    if args.timeout <= 0 or args.poll_interval <= 0:
        raise JobError("timeout and poll interval must be positive", "invalid_timeout")
    job_id = valid_id(args.job_id, JOB_ID_RE, "job_id")
    request_path = (args.request or (JOB_INPUT_ROOT / job_id / "request.json")).resolve()
    if not request_path.is_file():
        raise JobError(f"request.json not found: {request_path}", "missing_request")
    if not inside(request_path, JOB_INPUT_ROOT / job_id):
        raise JobError("request must be under input/cs_relay_jobs/JOB/", "invalid_request_path")

    output_root = (args.output_root or OUTPUT_ROOT).resolve()
    output_dir = output_root / RESULTS_SUBDIR / job_id
    segments_dir, reports_dir = output_dir / "segments", output_dir / "reports"
    if not inside(output_dir, output_root):
        raise JobError("result directory cannot escape the configured output root", "invalid_output_path")
    output_dir.mkdir(parents=True, exist_ok=True)
    segments_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "result.json"
    current_request_hash = sha256_file(request_path)
    prior_result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
    if prior_result and prior_result.get("request_sha256") != current_request_hash:
        raise JobError("request changed; use a new job ID rather than reusing prior outputs", "changed_request")
    if result_path.exists() and not args.resume:
        existing = prior_result
        if existing.get("status") == "completed":
            for item in existing.get("segments", []):
                verify_completed(item, output_dir)
            # Continue normalization below to verify the input images too.
            args.resume = True
        else:
            raise JobError(f"result already exists with status {existing.get('status')!r}; use --resume", "existing_result")

    result = json.loads(result_path.read_text(encoding="utf-8")) if args.resume and result_path.exists() else base_result(job_id, request_path)
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        normalized = normalize_request(request, job_id, request_path.parent)
    except (OSError, json.JSONDecodeError, JobError) as error:
        if args.dry_run and prior_result:
            raise JobError(str(error), "invalid_preview_request") from error
        code = error.code if isinstance(error, JobError) else "invalid_request"
        result["status"] = "blocked"
        result["finished_at"] = utc_now()
        result["errors"] = [{"code": code, "message": str(error)}]
        write_result(result_path, result, reports_dir)
        raise JobError(str(error), code)

    normalized_hash = sha256_value(normalized)
    if prior_result and prior_result.get("normalized_request_sha256") not in (None, normalized_hash):
        raise JobError("input images or normalized settings changed; use a new job ID", "changed_inputs")
    if args.dry_run and prior_result and any(
        item.get("prompt_id") or item.get("status") in {"submitting", "submission_unknown", "completed"}
        for item in prior_result.get("segments", [])
    ):
        if args.runtime_validate:
            preflight(args.url, normalized["payload"]["segments"], True)
        print(json.dumps({"job_id": job_id, "status": "dry-run", "existing_execution_preserved": True}))
        return 0
    result["request_sha256"] = current_request_hash
    result["normalized_request_sha256"] = normalized_hash
    result["errors"] = []
    write_json(reports_dir / "job.json", {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "request": result["request"],
        "request_sha256": result["request_sha256"],
        "normalized_request_sha256": result["normalized_request_sha256"],
        "segment_ids": [s["segment_id"] for s in normalized["payload"]["segments"]],
        "remote_scope": result["owned_by_remote"],
        "client_scope": result["owned_by_client"],
        "created_at": utc_now(),
    })

    try:
        if not args.dry_run or args.runtime_validate:
            result["preflight"] = preflight(args.url, normalized["payload"]["segments"], args.runtime_validate)
    except JobError as error:
        result["status"] = "blocked"
        result["finished_at"] = utc_now()
        result["errors"] = [{"code": error.code, "message": str(error)}]
        write_result(result_path, result, reports_dir)
        raise

    result["status"] = "dry-run" if args.dry_run else "running"
    existing = {item.get("segment_id"): item for item in result.get("segments", [])}
    write_result(result_path, result, reports_dir)

    for segment in normalized["payload"]["segments"]:
        segment_id = segment["segment_id"]
        prior = existing.get(segment_id)
        if prior and prior.get("status") == "completed":
            verify_completed(prior, output_dir)
            continue
        if prior and prior.get("status") == "submitted" and not args.resume:
            raise JobError(f"segment {segment_id} was already submitted; use --resume", "existing_submission")
        if prior and prior.get("status") in {"submitting", "submission_unknown"} and not prior.get("prompt_id"):
            raise JobError("submission receipt is unknown; reconcile the ComfyUI queue/history before retrying", "submission_unknown")

        payload = api_payload(job_id, segment)
        api_path = reports_dir / f"{segment_id}.api.json"
        report_path = reports_dir / f"{segment_id}.json"
        write_json(api_path, payload)
        item: dict[str, Any] = {
            "segment_id": segment_id,
            "workflow": segment["workflow"],
            "workflow_version": segment["workflow_version"],
            "api_workflow": str(api_path.relative_to(output_dir)),
            "first_frame": segment["first_frame"],
            "last_frame": segment["last_frame"],
            "first_frame_sha256": segment["first_frame_sha256"],
            "last_frame_sha256": segment["last_frame_sha256"],
            "action_images": segment["action_images"],
            "action_images_mapped": False,
            "action_images_note": "Current MiniMaxH3ImageToVideo only accepts first_frame and last_frame; action_images are provenance-only.",
            "prompt": segment["prompt"],
            "seed": segment["seed"],
            "model": segment["model"],
            "width": segment["width"],
            "height": segment["height"],
            "fps": segment["fps"],
            "length": segment["length"],
            "duration_seconds_requested": segment["duration_seconds_requested"],
            "duration_seconds_generated": segment["duration_seconds_generated"],
            "created_at": utc_now(),
        }
        if prior and prior.get("prompt_id"):
            item["prompt_id"] = prior["prompt_id"]
        write_json(report_path, item)

        if segment["action_images"] and not args.allow_unmapped_action_images:
            item["status"] = "blocked"
            item["error"] = {"code": "unsupported_action_images", "message": "Action images cannot be connected to the current FL2VA graph; use first/last frames or explicitly allow provenance-only images."}
            write_json(report_path, item)
            upsert_segment(result, item)
            result["status"] = "blocked"
            result["errors"].append(item["error"])
            result["finished_at"] = utc_now()
            write_result(result_path, result, reports_dir)
            return 2

        item["status"] = "prepared" if args.dry_run else ("submitted" if item.get("prompt_id") else "pending")
        upsert_segment(result, item)
        write_result(result_path, result, reports_dir)
        if args.dry_run:
            continue

        try:
            prompt_id = prior.get("prompt_id") if prior else None
            if not prompt_id:
                item["status"] = "submitting"
                upsert_segment(result, item)
                write_result(result_path, result, reports_dir)
                prompt_id = submit(args.url, payload)
            item["prompt_id"] = prompt_id
            item["status"] = "submitted"
            upsert_segment(result, item)
            result["status"] = "submitted"
            write_result(result_path, result, reports_dir)
            if args.no_wait:
                continue

            execution, source_files = wait_for_video(args.url, prompt_id, output_root, args.timeout, args.poll_interval)
            outputs = []
            for index, source in enumerate(source_files, 1):
                target = segments_dir / (f"{segment_id}.mp4" if index == 1 else f"{segment_id}-{index}.mp4")
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                outputs.append({
                    "path": str(target.relative_to(output_dir)),
                    "bytes": target.stat().st_size,
                    "sha256": sha256_file(target),
                    "media": probe_video(target, segment),
                })
            item["status"] = "completed"
            item["outputs"] = outputs
            item["history_status"] = execution.get("status")
            upsert_segment(result, item)
            result["status"] = "running"
            write_result(result_path, result, reports_dir)
        except JobError as error:
            item["status"] = "error" if item.get("prompt_id") else "submission_unknown"
            item["error"] = {"code": error.code, "message": str(error)}
            upsert_segment(result, item)
            result["status"] = "partial" if any(x.get("status") == "completed" for x in result["segments"]) else "error"
            result["errors"].append(item["error"])
            result["finished_at"] = utc_now()
            write_result(result_path, result, reports_dir)
            return 2

    result["status"] = "dry-run" if args.dry_run else ("submitted" if args.no_wait else "completed")
    result["finished_at"] = utc_now()
    write_result(result_path, result, reports_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def verify_completed(item: dict[str, Any], output_dir: Path) -> None:
    outputs = item.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise JobError("completed segment has no output records", "missing_output")
    for output in outputs:
        relative = Path(output.get("path", ""))
        path = output_dir / relative
        if (relative.is_absolute() or ".." in relative.parts or not inside(path, output_dir)
                or not path.is_file() or sha256_file(path) != output.get("sha256")):
            raise JobError("completed output missing or hash mismatch; use a new retry job", "invalid_output")


def probe_video(path: Path, segment: dict[str, Any]) -> dict[str, Any]:
    completed = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=width,height,r_frame_rate,nb_read_frames",
        "-of", "json", str(path),
    ], capture_output=True, text=True)
    if completed.returncode:
        raise JobError("ffprobe could not read generated video", "invalid_video")
    streams = json.loads(completed.stdout).get("streams", [])
    if not streams:
        raise JobError("generated file has no video stream", "invalid_video")
    media = streams[0]
    from fractions import Fraction
    try:
        fps = float(Fraction(media["r_frame_rate"]))
        frames = int(media["nb_read_frames"])
    except (KeyError, ValueError, ZeroDivisionError) as error:
        raise JobError("video has no usable frame count or FPS", "invalid_video") from error
    if (media.get("width") != segment["width"] or media.get("height") != segment["height"]
            or frames != segment["length"] or not math.isclose(fps, segment["fps"], abs_tol=0.001)):
        raise JobError("generated video does not match requested dimensions, frame count or FPS", "video_contract_mismatch")
    return {"width": media["width"], "height": media["height"], "frames": frames, "fps": fps}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", help="directory name under input/cs_relay_jobs/")
    parser.add_argument("--comfyui-root", type=Path, required=True, help="your ComfyUI installation, containing input/ and output/")
    parser.add_argument("--jobs-subdir", default="cs_relay_jobs", help="job packages relative to ComfyUI input/")
    parser.add_argument("--results-subdir", default="cs_relay_jobs", help="generated jobs relative to ComfyUI output/")
    parser.add_argument("--request", type=Path, help="override request.json path for a fixture")
    parser.add_argument("--output-root", type=Path, help="override output root for tests")
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--runtime-validate", action="store_true")
    parser.add_argument("--allow-unmapped-action-images", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--poll-interval", type=float, default=10.0)
    args = parser.parse_args()
    try:
        return run_job(args)
    except (JobError, json.JSONDecodeError, OSError) as error:
        print(f"comfyui_worker: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
