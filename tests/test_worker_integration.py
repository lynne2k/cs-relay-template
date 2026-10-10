"""Real HTTP and video-file contract test against a local fake ComfyUI.

This verifies transport/resume/media plumbing, never model inference quality.
"""
import argparse
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import comfyui_worker as worker
from test_worker import png


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class WorkerIntegrationTests(unittest.TestCase):
    def test_submission_probe_resume_and_corrupt_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "input/cs_relay_jobs/demo/payload"
            package.mkdir(parents=True)
            for name in ("a", "b", "c"):
                (package / f"{name}.png").write_bytes(png())
            segments = [{"segment_id": f"{index:02d}-clip", "workflow": kind,
                         "first_frame": f"payload/{first}.png", "last_frame": f"payload/{last}.png",
                         "prompt": "Synthetic fixture", "seed": index, "width": 32, "height": 32,
                         "fps": 24, "length": 5} for index, (first, last, kind) in enumerate(
                             (("a", "b", "age_transition"), ("b", "c", "player_handoff")), 1)]
            request = {"schema_version": "cs-relay.job.v1", "job_id": "demo", "payload": {"segments": segments}}
            (package.parent / "request.json").write_text(json.dumps(request))
            submissions = []
            histories = {}

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def respond(self, value):
                    body = json.dumps(value).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    if self.path == "/system_stats":
                        self.respond({"system": {"host": "example-render-host"}})
                    elif self.path == "/object_info/MiniMaxH3ImageToVideo":
                        self.respond({"MiniMaxH3ImageToVideo": {"input": {"optional": {"first_frame": [], "last_frame": []}}}})
                    elif self.path.startswith("/object_info/"):
                        name = self.path.split("/")[-1]
                        field, keys = {"UNETLoader": ("unet_name", ["unet"]),
                            "LoraLoaderModelOnly": ("lora_name", ["lora"]),
                            "CLIPLoader": ("clip_name", ["clip"]),
                            "VAELoader": ("vae_name", ["video_vae", "audio_vae"])}[name]
                        self.respond({name: {"input": {"required": {field: [[worker.DEFAULT_MODELS[k] for k in keys]]}}}})
                    elif self.path.startswith("/history/"):
                        key = self.path.split("/")[-1]
                        self.respond({key: histories[key]})
                    else:
                        self.send_error(404)

                def do_POST(self):
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    submissions.append(payload)
                    graph = payload["prompt"]
                    relative = graph["17"]["inputs"]["filename_prefix"] + "_00001_.mp4"
                    destination = root / "output" / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=gray:s=32x32:r=24", "-frames:v", "5",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(destination)], check=True)
                    key = f"fixture-{len(submissions)}"
                    histories[key] = {"status": {"completed": True, "status_str": "success"},
                        "outputs": {"17": {"videos": [{"filename": destination.name,
                            "subfolder": str(Path(relative).parent), "type": "output"}]}}}
                    self.respond({"prompt_id": key})

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                args = argparse.Namespace(job_id="demo", comfyui_root=root, request=None,
                    output_root=None, url=f"http://127.0.0.1:{server.server_port}", dry_run=False,
                    runtime_validate=True, allow_unmapped_action_images=False, no_wait=False,
                    resume=False, timeout=10, poll_interval=0.01)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(worker.run_job(args), 0)
                    args.resume = True
                    self.assertEqual(worker.run_job(args), 0)
                self.assertEqual(len(submissions), 2)
                result_dir = root / "output/cs_relay_jobs/demo"
                result = json.loads((result_dir / "result.json").read_text())
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["segments"][0]["outputs"][0]["media"]["frames"], 5)
                self.assertNotIn("example-render-host", json.dumps(result))
                self.assertNotIn(str(root), json.dumps(result))
                self.assertEqual(submissions[0]["prompt"]["2"]["inputs"]["image"],
                                 submissions[1]["prompt"]["1"]["inputs"]["image"])
                (result_dir / result["segments"][0]["outputs"][0]["path"]).write_bytes(b"changed")
                with self.assertRaisesRegex(worker.JobError, "hash mismatch"):
                    worker.run_job(args)
                self.assertEqual(len(submissions), 2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
