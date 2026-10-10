import argparse
import base64
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_relay_chain
import cs_relay


class PublicCoreTests(unittest.TestCase):
    PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.job = self.root / "test-job"
        self.assertEqual(cs_relay.main(["init", str(self.job), "--asset-policy", "user-provided"]), 0)
        (self.job / "layout.png").write_bytes(self.PNG)

    def register_pair(self, player="person-a"):
        for period in ("present", "past"):
            source = self.root / f"{player}-{period}.png"
            source.write_bytes(self.PNG + f"{player}:{period}".encode())
            self.assertEqual(cs_relay.main([
                "register-source", str(self.job), "--player", player, "--state", period,
                "--period", period, "--photo", str(source), "--review-status", "approved",
                "--review-note", "Synthetic source fixture, not a real person",
            ]), 0)
            self.assertEqual(cs_relay.main([
                "register-endpoint", str(self.job), "--player", player, "--state", period,
                "--endpoint", str(source), "--review-status", "approved",
                "--review-note", "Synthetic endpoint fixture",
                "--layout-reference", "layout.png", "--layout-note", "Same synthetic layout",
            ]), 0)

    def make_plan(self):
        for kind in ("age", "handoff"):
            (self.job / f"{kind}.txt").write_text(f"Synthetic {kind} template", encoding="utf-8")
        plan = {
            "seed_base": 100, "fps": 24, "segment_frames": 107,
            "duration_seconds_requested": 4, "generation_width": 32, "generation_height": 32,
            "age_prompt_file": "age.txt", "handoff_prompt_file": "handoff.txt",
            "terminal_age_transition": True,
            "nodes": [{"player": "person-a", "current": "present", "past": "past",
                       "current_caption": "Person A | Present", "past_caption": "Person A | Past"}],
        }
        path = self.job / "chain-plan.json"
        cs_relay.save_json(path, plan)
        return path, plan

    def test_local_init_works_without_a_server_and_sync_fails_clearly(self):
        config = cs_relay.load_remote_config(self.job)
        self.assertIsNone(config["remote_host"])
        self.assertIsNone(config["remote_project_root"])
        self.assertEqual(cs_relay.main(["validate", str(self.job)]), 0)
        with patch.object(cs_relay, "run_command") as command:
            self.assertEqual(cs_relay.main(["sync-up", str(self.job), "--dry-run"]), 2)
            command.assert_not_called()

    def test_explicit_config_and_cli_override(self):
        config = self.root / "config.json"
        cs_relay.save_json(config, {"remote_host": "your-ssh-alias", "remote_project_root": "/srv/ComfyUI"})
        target = self.root / "configured-job"
        self.assertEqual(cs_relay.main(["init", str(target), "--config", str(config)]), 0)
        args = argparse.Namespace(host="another-host", remote_root=None, config=None)
        self.assertEqual(cs_relay.remote_target(args, target, cs_relay.load_manifest(target)),
                         ("another-host", "/srv/ComfyUI/input/cs_relay_jobs/configured-job"))
        self.assertNotIn("host", cs_relay.load_manifest(target)["remote"])

    def test_remote_shell_metacharacters_and_traversal_are_rejected(self):
        manifest = cs_relay.load_manifest(self.job)
        for host, root in (("-oProxyCommand=x", "/srv/ComfyUI"), ("host;id", "/srv/ComfyUI"),
                           ("render-host", "/srv/$(id)"), ("render-host", "/srv/../etc")):
            with self.subTest(host=host, root=root), self.assertRaises(cs_relay.RelayError):
                cs_relay.remote_target(argparse.Namespace(host=host, remote_root=root, config=None), self.job, manifest)

    def test_symlink_escape_is_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (self.job / "escaped.txt").symlink_to(outside)
        with self.assertRaises(cs_relay.RelayError):
            cs_relay.rel_path(self.job, "escaped.txt")

    def test_strict_false_cannot_bypass_asset_validation(self):
        manifest = cs_relay.load_manifest(self.job)
        manifest["asset_policy"]["strict"] = False
        self.assertTrue(cs_relay.strict_asset_errors(self.job, manifest))

    def test_user_source_pair_builds_with_explicit_seed(self):
        self.register_pair()
        plan, _ = self.make_plan()
        self.assertEqual(build_relay_chain.main([str(self.job), str(plan)]), 0)
        manifest = cs_relay.load_manifest(self.job)
        self.assertEqual(manifest["segments"][0]["seed"], 101)
        self.assertEqual(manifest["timeline_contract"]["half_cycles"], 1)
        self.assertEqual(cs_relay.strict_asset_errors(self.job, manifest), [])

    def test_modified_source_or_layout_blocks_user_assets(self):
        self.register_pair()
        plan, _ = self.make_plan()
        self.assertEqual(build_relay_chain.main([str(self.job), str(plan)]), 0)
        manifest = cs_relay.load_manifest(self.job)
        (self.job / "layout.png").write_bytes(b"changed-layout")
        self.assertTrue(any("layout reference" in error for error in cs_relay.strict_asset_errors(self.job, manifest)))
        (self.job / "layout.png").write_bytes(self.PNG)
        source = manifest["players"][0]["states"][0]["portrait"]["path"]
        (self.job / source).write_bytes(b"changed-source")
        self.assertTrue(any("portrait" in error for error in cs_relay.strict_asset_errors(self.job, manifest)))

    def test_pair_requires_shared_layout_reference(self):
        self.register_pair()
        plan, _ = self.make_plan()
        self.assertEqual(build_relay_chain.main([str(self.job), str(plan)]), 0)
        manifest = cs_relay.load_manifest(self.job)
        other = self.job / "different-layout.png"
        other.write_bytes(self.PNG + b"other")
        manifest["players"][0]["states"][1]["endpoint"]["layout"]["reference"] = {
            "path": "different-layout.png", "sha256": cs_relay.sha256(other),
        }
        self.assertTrue(any("同一张布局参考图" in error for error in cs_relay.strict_asset_errors(self.job, manifest)))

    def test_invalid_late_node_does_not_destroy_existing_plan(self):
        self.register_pair()
        path, plan = self.make_plan()
        self.assertEqual(build_relay_chain.main([str(self.job), str(path)]), 0)
        before = (self.job / "job.json").read_bytes(), (self.job / "captions/events.json").read_bytes()
        plan["nodes"].append({"player": "missing-person", "current": "present", "past": "past",
                              "current_caption": "Missing | Present", "past_caption": "Missing | Past"})
        cs_relay.save_json(path, plan)
        self.assertEqual(build_relay_chain.main([str(self.job), str(path)]), 2)
        self.assertEqual(before, ((self.job / "job.json").read_bytes(), (self.job / "captions/events.json").read_bytes()))

    def test_missing_seed_or_prompt_template_rejected_without_changes(self):
        self.register_pair()
        path, plan = self.make_plan()
        before = (self.job / "job.json").read_bytes()
        for key in ("seed_base", "age_prompt_file"):
            invalid = copy.deepcopy(plan)
            invalid.pop(key)
            cs_relay.save_json(path, invalid)
            self.assertEqual(build_relay_chain.main([str(self.job), str(path)]), 2)
            self.assertEqual((self.job / "job.json").read_bytes(), before)

    def test_explicit_native_length_matches_packaged_video_and_caption_clock(self):
        self.register_pair()
        path, plan = self.make_plan()
        plan["segment_frames"] = 124
        cs_relay.save_json(path, plan)
        self.assertEqual(build_relay_chain.main([str(self.job), str(path)]), 0)
        self.assertEqual(cs_relay.main(["package", str(self.job)]), 0)
        request = cs_relay.load_json(self.job / "remote/request.json")
        segment = request["payload"]["segments"][0]
        events = cs_relay.load_events(self.job)
        self.assertEqual(segment["length"], 124)
        self.assertEqual(segment["duration_seconds"], 4)
        self.assertAlmostEqual(events[-1]["end"], segment["length"] / segment["fps"])

    def test_invalid_or_divergent_length_is_rejected_before_package_overwrite(self):
        self.register_pair()
        path, plan = self.make_plan()
        before = (self.job / "job.json").read_bytes()
        plan["segment_frames"] = 106
        cs_relay.save_json(path, plan)
        self.assertEqual(build_relay_chain.main([str(self.job), str(path)]), 2)
        self.assertEqual((self.job / "job.json").read_bytes(), before)
        plan["segment_frames"] = 107
        cs_relay.save_json(path, plan)
        self.assertEqual(build_relay_chain.main([str(self.job), str(path)]), 0)
        self.assertEqual(cs_relay.main(["package", str(self.job)]), 0)
        prior_request = (self.job / "remote/request.json").read_bytes()
        manifest = cs_relay.load_manifest(self.job)
        manifest["segments"][0]["length"] = 124
        cs_relay.save_manifest(self.job, manifest)
        self.assertEqual(cs_relay.main(["package", str(self.job)]), 2)
        self.assertEqual((self.job / "remote/request.json").read_bytes(), prior_request)

    def test_upload_stage_omits_config_results_and_unreferenced_payload(self):
        remote = self.job / "remote"
        (remote / "payload/input.png").write_bytes(self.PNG)
        (remote / "payload/unreferenced.txt").write_text("private", encoding="utf-8")
        (remote / "results/private.txt").write_text("private", encoding="utf-8")
        request = {"job_id": self.job.name, "payload": {"segments": [{"first_frame": "payload/input.png", "last_frame": "payload/input.png"}]}}
        cs_relay.save_json(remote / "request.json", request)
        stage = self.root / "stage"
        stage.mkdir()
        cs_relay.stage_remote_inputs(self.job, stage, cs_relay.load_manifest(self.job))
        self.assertEqual({str(p.relative_to(stage)) for p in stage.rglob("*") if p.is_file()},
                         {"request.json", "payload/input.png"})

    def test_visual_approval_is_bound_to_reviewed_render_bytes(self):
        self.register_pair()
        path, _ = self.make_plan()
        self.assertEqual(build_relay_chain.main([str(self.job), str(path)]), 0)
        manifest = cs_relay.load_manifest(self.job)
        render = self.job / "renders/review.mp4"
        render.write_bytes(b"synthetic-render-fixture")
        manifest["status"] = "local_assembled"
        manifest["artifacts"]["master_no_subtitles"] = "renders/review.mp4"
        cs_relay.save_manifest(self.job, manifest)
        self.assertEqual(cs_relay.main(["validate", str(self.job)]), 0)
        self.assertEqual(cs_relay.main(["validate", str(self.job), "--mark"]), 2)
        manifest["visual_review"] = {"reviewed": True, "status": "approved", "reviewer": "test fixture",
                                     "note": "Synthetic fixture approval only", "artifact": {
                                         "path": "renders/review.mp4", "sha256": cs_relay.sha256(render)}}
        cs_relay.save_manifest(self.job, manifest)
        self.assertEqual(cs_relay.main(["validate", str(self.job), "--mark"]), 0)
        self.assertEqual(cs_relay.load_manifest(self.job)["status"], "validated")
        render.write_bytes(b"new-unreviewed-render")
        self.assertEqual(cs_relay.main(["validate", str(self.job), "--mark"]), 2)

    def result_fixture(self):
        self.register_pair()
        path, _ = self.make_plan()
        self.assertEqual(build_relay_chain.main([str(self.job), str(path)]), 0)
        directory = self.root / "local-worker-output"
        (directory / "segments").mkdir(parents=True)
        video = directory / "segments/01-person-a-age.mp4"
        video.write_bytes(b"synthetic-worker-video")
        result = {"schema_version": "cs-relay.result.v1", "job_id": self.job.name, "status": "completed",
                  "segments": [{"segment_id": "01-person-a-age", "status": "completed", "outputs": [
                      {"path": "segments/01-person-a-age.mp4", "sha256": cs_relay.sha256(video)}]}]}
        cs_relay.save_json(directory / "result.json", result)
        return directory, video, result

    def test_import_result_without_ssh_and_idempotent_reimport(self):
        directory, _, _ = self.result_fixture()
        argv = ["import-result", str(self.job), str(directory)]
        self.assertEqual(cs_relay.main(argv), 0)
        self.assertEqual(cs_relay.main(argv), 0)
        manifest = cs_relay.load_manifest(self.job)
        self.assertEqual(manifest["status"], "remote_generated")
        self.assertEqual(manifest["segments"][0]["video_path"], "remote/results/segments/01-person-a-age.mp4")

    def test_import_bad_hash_or_job_id_does_not_change_job(self):
        directory, video, result = self.result_fixture()
        before = (self.job / "job.json").read_bytes()
        result["job_id"] = "another-job"
        cs_relay.save_json(directory / "result.json", result)
        self.assertEqual(cs_relay.main(["import-result", str(self.job), str(directory)]), 2)
        result["job_id"] = self.job.name
        cs_relay.save_json(directory / "result.json", result)
        video.write_bytes(b"changed-output")
        self.assertEqual(cs_relay.main(["import-result", str(self.job), str(directory)]), 2)
        self.assertEqual((self.job / "job.json").read_bytes(), before)
        self.assertEqual(list((self.job / "remote/results").rglob("*")), [])

    def test_import_conflicting_existing_result_is_not_overwritten(self):
        directory, _, _ = self.result_fixture()
        existing = self.job / "remote/results/segments/01-person-a-age.mp4"
        existing.parent.mkdir()
        existing.write_bytes(b"keep-existing-output")
        self.assertEqual(cs_relay.main(["import-result", str(self.job), str(directory)]), 2)
        self.assertEqual(existing.read_bytes(), b"keep-existing-output")
        self.assertFalse((self.job / "remote/results/result.json").exists())

    def test_sync_down_reuses_strict_import_without_overwriting_on_failure(self):
        directory, _, result = self.result_fixture()
        result["job_id"] = "another-job"
        cs_relay.save_json(directory / "result.json", result)
        existing = self.job / "remote/results/segments/01-person-a-age.mp4"
        existing.parent.mkdir()
        existing.write_bytes(b"keep-reviewed-local-output")
        before = (self.job / "job.json").read_bytes()

        def fake_download(command, *, dry_run=False):
            self.assertEqual(command[0], "rsync")
            self.assertNotEqual(Path(command[-1]).resolve(), (self.job / "remote/results").resolve())
            shutil.copytree(directory, Path(command[-1]), dirs_exist_ok=True)

        with patch.object(cs_relay, "run_command", side_effect=fake_download):
            rc = cs_relay.main(["sync-down", str(self.job), "--host", "your-ssh-alias", "--remote-root", "/srv/ComfyUI"])
        self.assertEqual(rc, 2)
        self.assertEqual(existing.read_bytes(), b"keep-reviewed-local-output")
        self.assertEqual((self.job / "job.json").read_bytes(), before)
        self.assertFalse((self.job / "remote/results/result.json").exists())

    def test_import_rejects_escaping_output_path(self):
        directory, video, result = self.result_fixture()
        result["segments"][0]["outputs"][0]["path"] = "../outside.mp4"
        (self.root / "outside.mp4").write_bytes(video.read_bytes())
        cs_relay.save_json(directory / "result.json", result)
        self.assertEqual(cs_relay.main(["import-result", str(self.job), str(directory)]), 2)


if __name__ == "__main__":
    unittest.main()
