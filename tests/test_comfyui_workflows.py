"""Offline regression checks for the public ComfyUI 0.4 canvas files.

Socket and widget order is independently asserted against the public v0.39.2
node schema. No ComfyUI server, browser, checkpoint, or GPU is needed.
"""

import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import export_comfyui_workflows as exporter


# Widget order follows official INPUT_TYPES/define_schema, not graph dict order.
WIDGETS = {
    "LoadImage": ["image"], "UNETLoader": ["unet_name", "weight_dtype"],
    "LoraLoaderModelOnly": ["lora_name", "strength_model"],
    "CLIPLoader": ["clip_name", "type", "device"], "VAELoader": ["vae_name"],
    "MiniMaxH3ImageToVideo": ["prompt", "width", "height", "length"],
    "RandomNoise": ["noise_seed"], "KSamplerSelect": ["sampler_name"],
    "BasicScheduler": ["scheduler", "steps", "denoise"], "BasicGuider": [],
    "SamplerCustomAdvanced": [], "VAEDecode": [], "VAEDecodeAudio": [],
    "CreateVideo": ["fps", "bit_depth", "color_space", "codec"],
    "SaveVideo": ["filename_prefix", "format", "format.codec", "format.codec.encoding"],
}
SOCKETS = {
    "LoadImage": [], "UNETLoader": [], "LoraLoaderModelOnly": ["model"],
    "CLIPLoader": [], "VAELoader": [], "RandomNoise": [], "KSamplerSelect": [],
    "MiniMaxH3ImageToVideo": ["clip", "vae", "first_frame", "last_frame"],
    "BasicScheduler": ["model"], "BasicGuider": ["model", "conditioning"],
    "SamplerCustomAdvanced": ["noise", "guider", "sampler", "sigmas", "latent_image"],
    "VAEDecode": ["samples", "vae"], "VAEDecodeAudio": ["samples", "vae"],
    "CreateVideo": ["images", "audio"], "SaveVideo": ["video"],
}


def read_canvas(preset):
    return json.loads((ROOT / "templates/comfyui" / f"{preset}.json").read_text())


def canvas_to_api(canvas):
    """Reconstruct backend inputs as the frontend does for this fixed graph."""
    links = {link[0]: link for link in canvas["links"]}
    graph = {}
    for node in canvas["nodes"]:
        kind = node["type"]
        values = node["widgets_values"]
        inputs = dict(zip(WIDGETS[kind], values))
        for socket in node["inputs"]:
            link = links[socket["link"]]
            inputs[socket["name"]] = [str(link[1]), link[2]]
        graph[str(node["id"])] = {"class_type": kind, "inputs": inputs}
    return graph


class ComfyUICanvasTests(unittest.TestCase):
    def test_checked_in_json_is_reproducible(self):
        for preset in exporter.PRESETS:
            with self.subTest(preset=preset):
                actual = (ROOT / "templates/comfyui" / f"{preset}.json").read_text()
                self.assertEqual(actual, exporter.serialized_workflow(preset))

    def test_exporter_check_is_offline_and_successful(self):
        result = subprocess.run([sys.executable, "scripts/export_comfyui_workflows.py", "--check"],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ui_graph_roundtrips_to_the_worker_exactly(self):
        for preset in exporter.PRESETS:
            with self.subTest(preset=preset):
                self.assertEqual(canvas_to_api(read_canvas(preset)), exporter.example_graph(preset))

    def test_zero_point_four_required_fields_and_public_metadata(self):
        for preset in exporter.PRESETS:
            canvas = read_canvas(preset)
            self.assertEqual(canvas["version"], 0.4)
            self.assertEqual(canvas["last_node_id"], 17)
            self.assertEqual(len(canvas["nodes"]), 17)
            self.assertEqual(canvas["extra"]["cs_relay"]["comfyui_version"], "0.39.2")
            self.assertNotIn("id", canvas)  # No generated workflow/session UUID.
            self.assertNotIn("revision", canvas)
            self.assertEqual(canvas["last_link_id"], len(canvas["links"]))
            for node in canvas["nodes"]:
                self.assertTrue({"id", "type", "pos", "size", "flags", "order", "mode", "properties"} <= node.keys())
                self.assertIsInstance(node["id"], int)
                self.assertEqual(node["mode"], 0)
                self.assertEqual(len(node["pos"]), 2)
                self.assertEqual(len(node["size"]), 2)
                self.assertTrue(all(isinstance(v, (int, float)) for v in node["pos"] + node["size"]))
                self.assertEqual(node["properties"]["Node name for S&R"], node["type"])
                self.assertEqual([i["name"] for i in node["inputs"]], SOCKETS[node["type"]])
            for group in canvas["groups"]:
                self.assertEqual(len(group["bounding"]), 4)
                self.assertIsInstance(group["title"], str)

    def test_link_ids_slots_types_and_backreferences(self):
        for preset in exporter.PRESETS:
            canvas = read_canvas(preset)
            nodes = {n["id"]: n for n in canvas["nodes"]}
            links = {link[0]: link for link in canvas["links"]}
            self.assertEqual(len(links), len(canvas["links"]))
            self.assertEqual(set(links), set(range(1, len(links) + 1)))
            for link in links.values():
                self.assertEqual(len(link), 6)
                link_id, source, source_slot, target, target_slot, dtype = link
                output = nodes[source]["outputs"][source_slot]
                socket = nodes[target]["inputs"][target_slot]
                self.assertEqual(output["type"], dtype)
                self.assertEqual(socket["type"], dtype)
                self.assertIn(link_id, output["links"])
                self.assertEqual(socket["link"], link_id)
                self.assertLess(nodes[source]["order"], nodes[target]["order"])
            for node in nodes.values():
                for slot, output in enumerate(node["outputs"]):
                    for link_id in output["links"] or []:
                        self.assertEqual(links[link_id][1:3], [node["id"], slot])

    def test_prompt_and_native_clip_preset(self):
        for preset in exporter.PRESETS:
            nodes = {n["id"]: n for n in read_canvas(preset)["nodes"]}
            prompt = (ROOT / "templates/prompts" / f"{preset}.txt").read_text().strip()
            self.assertEqual(nodes[8]["widgets_values"], [prompt, 544, 736, 107])
            self.assertEqual(nodes[16]["widgets_values"], [24, 8, "sRGB", "none"])
            self.assertEqual(nodes[9]["widgets_values"], [1, "fixed"])
            self.assertEqual(nodes[10]["widgets_values"], ["res_multistep"])
            self.assertEqual(nodes[11]["widgets_values"], ["simple", 4, 1.0])
            self.assertEqual(nodes[17]["widgets_values"][1:], ["mp4", "h264", "auto"])

    def test_same_past_endpoint_used_for_both_clips(self):
        age = read_canvas("age_transition_shirt_removal_v2")
        handoff = read_canvas("player_handoff_v2")
        age_nodes = {n["id"]: n for n in age["nodes"]}
        handoff_nodes = {n["id"]: n for n in handoff["nodes"]}
        self.assertEqual(age_nodes[2]["widgets_values"], ["subject_a_past.png", "image"])
        self.assertEqual(age_nodes[2]["widgets_values"], handoff_nodes[1]["widgets_values"])

    def test_optional_image_slots_and_both_decoder_branches(self):
        canvas = read_canvas("age_transition_shirt_removal_v2")
        nodes = {n["id"]: n for n in canvas["nodes"]}
        for index in (2, 3):
            self.assertEqual(nodes[8]["inputs"][index]["shape"], 7)
        self.assertEqual(nodes[16]["inputs"][1]["shape"], 7)
        api = canvas_to_api(canvas)
        self.assertEqual(api["14"]["inputs"]["samples"], ["13", 0])
        self.assertEqual(api["15"]["inputs"]["samples"], ["13", 0])
        self.assertEqual(api["14"]["inputs"]["vae"], ["6", 0])
        self.assertEqual(api["15"]["inputs"]["vae"], ["7", 0])
        self.assertEqual(api["16"]["inputs"]["audio"], ["15", 0])
        self.assertEqual(nodes[17]["outputs"], [{"name": "video", "type": "VIDEO", "links": None}])


if __name__ == "__main__":
    unittest.main()
