import argparse
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import comfyui_worker as worker


def png(width=32, height=32):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    pixels = b"".join(b"\0" + b"\x80\x90\xa0" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = self.root / "input/cs_relay_jobs/demo"
        (self.job / "payload").mkdir(parents=True)
        for name in ("first.png", "last.png"):
            (self.job / "payload" / name).write_bytes(png())
        worker.configure_runtime(self.root)
        self.request = {
            "schema_version": "cs-relay.job.v1", "job_id": "demo",
            "payload": {"segments": [{
                "segment_id": "01-age", "workflow": "age_transition", "seed": 10,
                "prompt": "Synthetic test prompt", "first_frame": "payload/first.png",
                "last_frame": "payload/last.png", "width": 32, "height": 32,
                "fps": 24, "duration_seconds": 4,
            }]},
        }
        (self.job / "request.json").write_text(json.dumps(self.request))

    def test_native_length_and_portable_graph(self):
        normalized = worker.normalize_request(self.request, "demo", self.job)
        segment = normalized["payload"]["segments"][0]
        self.assertEqual(segment["length"], 107)
        self.assertEqual(segment["first_frame"], "cs_relay_jobs/demo/payload/first.png")
        graph = worker.api_payload("demo", segment)["prompt"]
        self.assertEqual(graph["8"]["inputs"]["last_frame"], ["2", 0])
        self.assertNotIn(str(self.root), json.dumps(graph))

    def test_reject_outside_payload_and_symlink(self):
        for name in ("../secret.png", "/secret.png", "input/other/file.png"):
            with self.assertRaises(worker.JobError):
                worker.input_asset(self.job, name, "first")
        outside = self.root / "outside.png"
        outside.write_bytes(png())
        (self.job / "payload/escape.png").symlink_to(outside)
        with self.assertRaises(worker.JobError):
            worker.input_asset(self.job, "payload/escape.png", "first")

    def test_endpoint_size_must_match(self):
        bad = copy.deepcopy(self.request)
        bad["payload"]["segments"][0]["width"] = 64
        with self.assertRaisesRegex(worker.JobError, "dimensions"):
            worker.normalize_request(bad, "demo", self.job)

    def test_no_urls_with_credentials(self):
        with self.assertRaises(worker.JobError):
            worker.checked_url("https://" + "user" + ":" + "example" + "@example.invalid")
        with self.assertRaises(worker.JobError):
            worker.checked_url("https://example.invalid/?key=example")
        self.assertEqual(worker.checked_url("http://127.0.0.1:8188/"), "http://127.0.0.1:8188")

    def test_offline_dry_run_and_changed_input_resume(self):
        args = argparse.Namespace(job_id="demo", comfyui_root=self.root, request=None,
            output_root=None, url="http://127.0.0.1:8188", dry_run=True,
            runtime_validate=False, allow_unmapped_action_images=False, no_wait=False,
            resume=False, timeout=10, poll_interval=1)
        with patch.object(worker, "http_json", side_effect=AssertionError("must be offline")):
            self.assertEqual(worker.run_job(args), 0)
            result = json.loads((self.root / "output/cs_relay_jobs/demo/result.json").read_text())
            self.assertEqual(result["status"], "dry-run")
            self.assertNotIn(str(self.root), json.dumps(result))
            args.resume = True
            (self.job / "payload/first.png").write_bytes(png() + b"changed")
            with self.assertRaisesRegex(worker.JobError, "changed"):
                worker.run_job(args)

    def test_resume_requires_completed_hash(self):
        output = self.root / "output"
        output.mkdir()
        video = output / "test.mp4"
        video.write_bytes(b"fixture")
        item = {"outputs": [{"path": "test.mp4", "sha256": hashlib.sha256(b"fixture").hexdigest()}]}
        worker.verify_completed(item, output)
        video.write_bytes(b"corrupt")
        with self.assertRaises(worker.JobError):
            worker.verify_completed(item, output)

    def test_dry_run_preserves_submitted_receipt(self):
        args = argparse.Namespace(job_id="demo", comfyui_root=self.root, request=None,
            output_root=None, url="http://127.0.0.1:8188", dry_run=False,
            runtime_validate=False, allow_unmapped_action_images=False, no_wait=True,
            resume=False, timeout=10, poll_interval=1)
        result_path = self.root / "output/cs_relay_jobs/demo/result.json"
        with patch.object(worker, "preflight", return_value={}), patch.object(worker, "submit", return_value="fixture-receipt") as submit:
            worker.run_job(args)
            original = result_path.read_bytes()
            args.resume, args.dry_run = True, True
            worker.run_job(args)
            self.assertEqual(original, result_path.read_bytes())
            args.dry_run = False
            worker.run_job(args)
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(json.loads(result_path.read_text())["segments"][0]["prompt_id"], "fixture-receipt")

    def test_unknown_submission_does_not_automatically_resubmit(self):
        args = argparse.Namespace(job_id="demo", comfyui_root=self.root, request=None,
            output_root=None, url="http://127.0.0.1:8188", dry_run=False,
            runtime_validate=False, allow_unmapped_action_images=False, no_wait=True,
            resume=False, timeout=10, poll_interval=1)
        with patch.object(worker, "preflight", return_value={}), patch.object(worker, "submit", side_effect=worker.JobError("lost receipt")) as submit:
            self.assertEqual(worker.run_job(args), 2)
            args.resume = True
            with self.assertRaisesRegex(worker.JobError, "receipt is unknown"):
                worker.run_job(args)
            self.assertEqual(submit.call_count, 1)

    def test_output_history_cannot_escape_root(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"fixture")
        entry = {"outputs": {"17": {"videos": [{"filename": "../outside.mp4", "type": "output"}]}}}
        self.assertEqual(worker.video_files(entry, self.root / "output"), [])


if __name__ == "__main__":
    unittest.main()
