#!/usr/bin/env python3
"""Build portable ComfyUI canvas files from the worker's API graph, offline.

The socket and widget order follows ComfyUI v0.39.2 (core nodes.py,
comfy_extras/nodes_minimax_h3.py, nodes_custom_sampler.py, nodes_audio.py,
and nodes_video.py). No model, private image, or server is contacted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch

import comfyui_worker as worker


ROOT = Path(__file__).resolve().parents[1]
TARGET_VERSION = "0.39.2"
TARGET_REVISION = "3c1b7a17fdf239d35ce98cc777756f572d089795"
PRESETS = {
    "age_transition_shirt_removal_v2": {
        "workflow": "age_transition", "segment_id": "01-age",
        "first_frame": "subject_a_present.png", "last_frame": "subject_a_past.png",
        "title": "Present to Past - overhead shirt removal",
    },
    "player_handoff_v2": {
        "workflow": "player_handoff", "segment_id": "02-handoff",
        "first_frame": "subject_a_past.png", "last_frame": "subject_b_present.png",
        "title": "Subject A to Subject B - camera handoff",
    },
}

# Socket order excludes widgets, exactly as in a non-converted canvas node.
# Outputs are (display name, backend type); widget keys follow schema order.
SPECS = {
    "LoadImage": ([], [("IMAGE", "IMAGE"), ("MASK", "MASK")], ["image"]),
    "UNETLoader": ([], [("MODEL", "MODEL")], ["unet_name", "weight_dtype"]),
    "LoraLoaderModelOnly": ([("model", "MODEL")], [("MODEL", "MODEL")], ["lora_name", "strength_model"]),
    "CLIPLoader": ([], [("CLIP", "CLIP")], ["clip_name", "type", "device"]),
    "VAELoader": ([], [("VAE", "VAE")], ["vae_name"]),
    "MiniMaxH3ImageToVideo": (
        [("clip", "CLIP"), ("vae", "VAE"), ("first_frame", "IMAGE"), ("last_frame", "IMAGE")],
        [("positive", "CONDITIONING"), ("LATENT", "LATENT")], ["prompt", "width", "height", "length"]),
    "RandomNoise": ([], [("NOISE", "NOISE")], ["noise_seed"]),
    "KSamplerSelect": ([], [("SAMPLER", "SAMPLER")], ["sampler_name"]),
    "BasicScheduler": ([("model", "MODEL")], [("SIGMAS", "SIGMAS")], ["scheduler", "steps", "denoise"]),
    "BasicGuider": ([("model", "MODEL"), ("conditioning", "CONDITIONING")], [("GUIDER", "GUIDER")], []),
    "SamplerCustomAdvanced": (
        [("noise", "NOISE"), ("guider", "GUIDER"), ("sampler", "SAMPLER"), ("sigmas", "SIGMAS"), ("latent_image", "LATENT")],
        [("output", "LATENT"), ("denoised_output", "LATENT")], []),
    "VAEDecode": ([("samples", "LATENT"), ("vae", "VAE")], [("IMAGE", "IMAGE")], []),
    "VAEDecodeAudio": ([("samples", "LATENT"), ("vae", "VAE")], [("AUDIO", "AUDIO")], []),
    "CreateVideo": ([("images", "IMAGE"), ("audio", "AUDIO")], [("VIDEO", "VIDEO")], ["fps", "bit_depth", "color_space", "codec"]),
    "SaveVideo": ([("video", "VIDEO")], [("video", "VIDEO")], ["filename_prefix"]),
}
LAYOUT = {
    1: ([40, 100], [330, 360]), 2: ([40, 530], [330, 360]),
    3: ([440, 100], [370, 100]), 4: ([440, 270], [370, 130]),
    5: ([440, 470], [370, 130]), 6: ([440, 670], [370, 90]),
    7: ([440, 830], [370, 90]), 8: ([880, 100], [490, 820]),
    9: ([1440, 100], [270, 110]), 10: ([1440, 280], [270, 80]),
    11: ([1440, 430], [270, 150]), 12: ([1440, 680], [270, 90]),
    13: ([1800, 370], [300, 200]),
    14: ([2190, 240], [240, 90]), 15: ([2190, 550], [240, 90]),
    16: ([2520, 370], [280, 190]), 17: ([2890, 220], [370, 440]),
}


def example_segment(preset: str) -> dict:
    details = PRESETS[preset]
    return {
        key: details[key] for key in ("workflow", "segment_id", "first_frame", "last_frame")
    } | {
        "prompt": (ROOT / "templates/prompts" / f"{preset}.txt").read_text(encoding="utf-8").strip(),
        "width": 544, "height": 736, "length": 107, "fps": 24,
        "seed": 1, "model": worker.model_config(None),
    }


def example_graph(preset: str) -> dict:
    # The public sample's output prefix must not depend on prior runtime setup.
    with patch.object(worker, "RESULTS_SUBDIR", Path("cs_relay_jobs")):
        return worker.api_payload("demo", example_segment(preset))["prompt"]


def widget_values(node: dict) -> list:
    kind, inputs = node["class_type"], node["inputs"]
    values = [inputs.get(key, "none") if key == "codec" and kind == "CreateVideo" else inputs[key]
              for key in SPECS[kind][2]]
    if kind == "LoadImage":
        values.append("image")  # ComfyUI's upload widget, not a backend input.
    elif kind == "RandomNoise":
        values.append("fixed")  # Preserve reproducible seed when pressing Queue.
    elif kind == "SaveVideo":
        # DynamicCombo children serialize immediately after their parent.
        # Dotted backend names become this positional widget sequence.
        selection = [inputs[key] for key in ("format", "format.codec", "format.codec.encoding")]
        if selection != ["mp4", "h264", "auto"]:
            raise ValueError("Public workflow expects MP4/H.264/auto; update the UI mapping deliberately")
        values.extend(selection)
    return values


def build_workflow(preset: str) -> dict:
    graph = example_graph(preset)
    nodes = {}
    for key, api_node in graph.items():
        node_id = int(key)
        kind = api_node["class_type"]
        ports, outputs, _ = SPECS[kind]
        position, size = LAYOUT[node_id]
        inputs = [{"name": name, "type": dtype, "link": None} for name, dtype in ports]
        for port in inputs:
            if (kind == "MiniMaxH3ImageToVideo" and port["name"] in {"first_frame", "last_frame"}) or (kind == "CreateVideo" and port["name"] == "audio"):
                port["shape"] = 7
        nodes[node_id] = {
            "id": node_id, "type": kind, "pos": position, "size": size,
            "flags": {}, "order": node_id - 1, "mode": 0,
            "inputs": inputs,
            "outputs": [{"name": name, "type": dtype, "links": None} for name, dtype in outputs],
            "properties": {"Node name for S&R": kind},
            "widgets_values": widget_values(api_node),
        }
    nodes[1]["title"] = "FIRST - " + PRESETS[preset]["first_frame"]
    nodes[2]["title"] = "LAST - " + PRESETS[preset]["last_frame"]
    nodes[8]["title"] = PRESETS[preset]["title"]
    nodes[6]["title"] = "Video VAE"
    nodes[7]["title"] = "Audio VAE"
    nodes[17]["title"] = "Save MP4 / H.264 (ComfyUI output folder)"
    links = []
    for target_id, canvas_node in nodes.items():
        for target_slot, port in enumerate(canvas_node["inputs"]):
            source_id, source_slot = graph[str(target_id)]["inputs"][port["name"]]
            output = nodes[int(source_id)]["outputs"][source_slot]
            if output["type"] != port["type"]:
                raise ValueError(f"Incompatible link at node {target_id}: {port['name']}")
            link_id = len(links) + 1
            links.append([link_id, int(source_id), source_slot, target_id, target_slot, port["type"]])
            port["link"] = link_id
            output["links"] = (output["links"] or []) + [link_id]
    groups = [
        {"title": "1. Upload matching 544 x 736 PNG endpoints", "bounding": [10, 20, 390, 950], "color": "#456579", "font_size": 22},
        {"title": "2. Select your local H3 model files", "bounding": [410, 20, 430, 950], "color": "#456579", "font_size": 22},
        {"title": "3. Prompt + 107 frames at 24 fps", "bounding": [850, 20, 550, 950], "color": "#5b6553", "font_size": 22},
        {"title": "4. Four-step sampling", "bounding": [1410, 20, 730, 950], "color": "#5b6553", "font_size": 22},
        {"title": "5. Decode and save video + audio", "bounding": [2160, 20, 1140, 950], "color": "#675477", "font_size": 22},
    ]
    return {
        "last_node_id": max(nodes), "last_link_id": len(links),
        "nodes": list(nodes.values()), "links": links, "groups": groups,
        "config": {}, "extra": {"ds": {"scale": 0.48, "offset": [40, 70]},
            "cs_relay": {"preset": preset, "comfyui_version": TARGET_VERSION,
                         "comfyui_revision": TARGET_REVISION,
                         "note": "Upload your own reviewed endpoints. Same prepared Past PNG must be shared across the two workflows. See docs/comfyui.md."}},
        "version": 0.4,
    }


def serialized_workflow(preset: str) -> str:
    return json.dumps(build_workflow(preset), ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if tracked canvas JSON has drifted")
    parser.add_argument("--stdout", choices=tuple(PRESETS), help="Print one canvas without writing files")
    args = parser.parse_args()
    if args.stdout:
        sys.stdout.write(serialized_workflow(args.stdout))
        return 0
    changed = []
    for preset in PRESETS:
        destination = ROOT / "templates/comfyui" / f"{preset}.json"
        rendered = serialized_workflow(preset)
        if args.check:
            if not destination.exists() or destination.read_text(encoding="utf-8") != rendered:
                changed.append(destination.relative_to(ROOT).as_posix())
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8")
    if changed:
        print("Outdated workflow(s): " + ", ".join(changed), file=sys.stderr)
        return 1
    if not args.check:
        print("Wrote two portable ComfyUI canvas workflows; no inference was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
