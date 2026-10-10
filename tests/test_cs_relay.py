import base64
import json
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cs_relay  # noqa: E402


class RelayPipelineTests(unittest.TestCase):
    PNG_1X1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def _register_state(
        self,
        job: Path,
        *,
        player: str,
        state: str,
        period: str,
        year: int,
        team: str,
    ) -> str:
        fixture_dir = job / "fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        portrait = fixture_dir / f"{player}-{state}-portrait.png"
        # PNG decoders tolerate trailing fixture bytes; the suffix makes the
        # current/peak identity-source hashes distinct for the strict gate.
        portrait.write_bytes(self.PNG_1X1 + f"{player}:{state}".encode())
        endpoint = fixture_dir / f"{player}-{state}-endpoint.png"
        endpoint.write_bytes(self.PNG_1X1)
        snapshot_evidence = fixture_dir / f"{player}-{state}-snapshot.html"
        snapshot_evidence.write_text(
            f"dated HLTV ranking card: {team}; five players including {player}; {year}",
            encoding="utf-8",
        )
        profile_evidence = fixture_dir / f"{player}-{state}-profile.html"
        profile_evidence.write_text(
            f"HLTV profile history for {player}, {team}, {year}",
            encoding="utf-8",
        )
        argv = [
            "register-player",
            str(job),
            "--player",
            player,
            "--state",
            state,
            "--period",
            period,
            "--year",
            str(year),
            "--team",
            team,
            "--profile-url",
            f"https://www.hltv.org/player/1/{player}",
            "--profile-evidence",
            str(profile_evidence),
            "--snapshot-url",
            f"https://www.hltv.org/ranking/teams/{year}/december/25",
            "--snapshot-evidence",
            str(snapshot_evidence),
            "--snapshot-team",
            team,
            "--snapshot-player",
            player,
            "--snapshot-player-count",
            "5",
            "--photo",
            str(portrait),
            "--photo-source-url",
            f"https://img-cdn.hltv.org/playerbodyshot/{player}-{state}.png",
            "--photo-year",
            str(year),
            "--photo-team",
            team,
            "--review-status",
            "approved",
            "--review-note",
            "fixture provenance reviewed",
            "--visual-team-verified",
        ]
        if period == "peak":
            ranking_evidence = fixture_dir / f"{player}-{state}-ranking.html"
            ranking_evidence.write_text(
                f"HLTV Top 20 rank 1 for {player} in {year}", encoding="utf-8"
            )
            argv.extend(
                [
                    "--hltv-rank",
                    "1",
                    "--ranking-url",
                    f"https://www.hltv.org/news/top-20-{player}-{year}",
                    "--ranking-evidence",
                    str(ranking_evidence),
                ]
            )
        self.assertEqual(cs_relay.main(argv), 0)
        self.assertEqual(
            cs_relay.main(
                [
                    "register-endpoint",
                    str(job),
                    "--player",
                    player,
                    "--state",
                    state,
                    "--endpoint",
                    str(endpoint),
                    "--review-status",
                    "approved",
                    "--review-note",
                    "fixture endpoint reviewed",
                ]
            ),
            0,
        )
        manifest = cs_relay.load_manifest(job)
        registered = cs_relay._find_state(manifest, cs_relay.slug(player), cs_relay.slug(state))
        return registered["endpoint"]["path"]

    def _registration_argv(
        self,
        job: Path,
        fixture_dir: Path,
        *,
        snapshot_url: str = "https://www.hltv.org/ranking/teams/2026/september/21",
        snapshot_player_count: int = 5,
        photo_source_url: str = "https://img-cdn.hltv.org/playerbodyshot/player-a.png",
    ) -> list[str]:
        photo = fixture_dir / "player-a.png"
        photo.write_bytes(self.PNG_1X1)
        evidence = fixture_dir / "evidence.html"
        evidence.write_text("Team A: player-a plus four teammates", encoding="utf-8")
        return [
            "register-player",
            str(job),
            "--player",
            "player-a",
            "--state",
            "current-2026",
            "--period",
            "current",
            "--year",
            "2026",
            "--team",
            "Team A",
            "--profile-url",
            "https://www.hltv.org/player/1/player-a",
            "--profile-evidence",
            str(evidence),
            "--snapshot-url",
            snapshot_url,
            "--snapshot-evidence",
            str(evidence),
            "--snapshot-team",
            "Team A",
            "--snapshot-player",
            "player-a",
            "--snapshot-player-count",
            str(snapshot_player_count),
            "--photo",
            str(photo),
            "--photo-source-url",
            photo_source_url,
            "--photo-year",
            "2026",
            "--photo-team",
            "Team A",
            "--review-status",
            "approved",
            "--review-note",
            "dated ranking snapshot reviewed",
            "--visual-team-verified",
        ]

    def _override_registration_argv(
        self,
        job: Path,
        fixture_dir: Path,
        *,
        override_reason: str | None = "historical Ranking portrait shows a wrong-era jersey",
    ) -> list[str]:
        photo = fixture_dir / "player-a-official.jpg"
        photo.write_bytes(b"official-media-day-fixture")
        evidence = fixture_dir / "profile.html"
        evidence.write_text("HLTV profile history for player-a", encoding="utf-8")
        argv = [
            "register-player", str(job),
            "--player", "player-a",
            "--state", "peak-2015",
            "--period", "peak",
            "--year", "2015",
            "--team", "Team A",
            "--profile-url", "https://www.hltv.org/player/1/player-a",
            "--profile-evidence", str(evidence),
            "--acquisition-method", cs_relay.PORTRAIT_OVERRIDE_METHOD,
            "--portrait-kind", "official_media_day_portrait",
            "--portrait-provider", "official_media_day",
            "--photo", str(photo),
            "--photo-source-url", "https://official.example/media-day/player-a.jpg",
            "--photo-year", "2015",
            "--photo-team", "Team A",
            "--review-status", "approved",
            "--review-note", "official media-day jersey and identity visually reviewed",
            "--visual-team-verified",
            "--hltv-rank", "1",
            "--ranking-url", "https://www.hltv.org/news/example",
            "--ranking-evidence", str(evidence),
        ]
        if override_reason is not None:
            argv.extend(["--override-reason", override_reason])
        return argv

    def _teamless_registration_argv(
        self,
        job: Path,
        fixture_dir: Path,
        *,
        period: str = "current",
        team: str = "Retired",
    ) -> tuple[list[str], Path]:
        photo = fixture_dir / "retired-current.jpg"
        photo.write_bytes(b"current-identity-in-last-active-shirt")
        evidence = fixture_dir / "retired-profile.html"
        evidence.write_text("HLTV profile says Retired; source shirt is Falcons", encoding="utf-8")
        endpoint = fixture_dir / "retired-endpoint.png"
        endpoint.write_bytes(self.PNG_1X1)
        argv = [
            "register-player", str(job),
            "--player", "retired-player",
            "--state", "current-2026-retired",
            "--period", period,
            "--year", "2026",
            "--team", team,
            "--profile-url", "https://www.hltv.org/player/1/retired-player",
            "--profile-evidence", str(evidence),
            "--acquisition-method", cs_relay.PORTRAIT_OVERRIDE_METHOD,
            "--portrait-kind", "official_media_day_portrait",
            "--portrait-provider", "official_media_day",
            "--override-reason", "current profile is teamless; last-active portrait is identity-only",
            "--photo", str(photo),
            "--photo-source-url", "https://official.example/current-profile.jpg",
            "--photo-year", "2026",
            "--photo-team", "Falcons",
            "--review-status", "approved",
            "--review-note", "current identity and visible Falcons source shirt reviewed",
            "--teamless-current-identity-only",
            "--visual-identity-verified",
        ]
        return argv, endpoint

    def test_init_and_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "demo-job"
            self.assertEqual(cs_relay.main(["init", str(job)]), 0)
            events = [
                {"start": 0, "end": 1.5, "text": "2025 · Falcons · Player A"},
                {"start": 1.5, "end": 3, "text": "2019 · Liquid · Player A"},
            ]
            (job / "captions/events.json").write_text(json.dumps(events), encoding="utf-8")
            self.assertEqual(cs_relay.main(["make-srt", str(job)]), 0)
            srt = (job / "captions/final.srt").read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:01,500", srt)
            self.assertIn("2025 · Falcons · Player A", srt)

    def test_srt_merges_contiguous_identical_captions(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "demo-job"
            self.assertEqual(cs_relay.main(["init", str(job)]), 0)
            events = [
                {"start": 0, "end": 1.5, "text": "Player A | Team | 2026"},
                {"start": 1.5, "end": 3, "text": "Player A | Team | 2026"},
                {"start": 3, "end": 4.5, "text": "Player A | Team | 2018"},
            ]
            (job / "captions/events.json").write_text(json.dumps(events), encoding="utf-8")
            self.assertEqual(cs_relay.main(["make-srt", str(job)]), 0)
            srt = (job / "captions/final.srt").read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:03,000", srt)
            self.assertEqual(srt.count("Player A | Team | 2026"), 1)

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "demo-job"
            cs_relay.main(["init", str(job)])
            manifest = cs_relay.load_manifest(job)
            manifest["segments"] = [{"id": "x", "first_frame": "../outside.png"}]
            cs_relay.save_manifest(job, manifest)
            self.assertEqual(cs_relay.main(["validate", str(job)]), 2)

    def test_remote_result_binds_downloaded_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "demo-job"
            cs_relay.main(["init", str(job)])
            segment_file = job / "remote/results/segments/age.mp4"
            segment_file.parent.mkdir(parents=True, exist_ok=True)
            segment_file.write_bytes(b"fixture-video")
            manifest = cs_relay.load_manifest(job)
            manifest["segments"] = [{"id": "age", "kind": "age_transition"}]
            result = {
                "status": "completed",
                "segments": [
                    {
                        "id": "age",
                        "video_path": "segments/age.mp4",
                        "sha256": cs_relay.sha256(segment_file),
                    }
                ],
            }
            ok, missing = cs_relay.bind_remote_result(job, manifest, result)
            self.assertTrue(ok)
            self.assertEqual(missing, [])
            self.assertEqual(manifest["segments"][0]["video_path"], "remote/results/segments/age.mp4")

    def test_remote_result_v1_segment_id_and_outputs_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "remote-v1-job"
            cs_relay.main(["init", str(job)])
            segment_file = job / "remote/results/segments/handoff.mp4"
            segment_file.parent.mkdir(parents=True, exist_ok=True)
            segment_file.write_bytes(b"remote-v1-video")
            manifest = cs_relay.load_manifest(job)
            manifest["segments"] = [{"id": "handoff", "kind": "player_handoff"}]
            result = {
                "status": "completed",
                "segments": [
                    {
                        "segment_id": "handoff",
                        "outputs": [
                            {
                                "path": "segments/handoff.mp4",
                                "sha256": cs_relay.sha256(segment_file),
                            }
                        ],
                    }
                ],
            }
            ok, missing = cs_relay.bind_remote_result(job, manifest, result)
            self.assertTrue(ok)
            self.assertEqual(missing, [])
            self.assertEqual(manifest["segments"][0]["video_path"], "remote/results/segments/handoff.mp4")

    def test_package_matches_remote_request_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "contract-job"
            cs_relay.main(["init", str(job)])
            first = self._register_state(
                job,
                player="player-a",
                state="peak-2018",
                period="peak",
                year=2018,
                team="Team A",
            )
            last = self._register_state(
                job,
                player="player-b",
                state="current-2026",
                period="current",
                year=2026,
                team="Team B",
            )
            (job / "prompt.txt").write_text("a restrained player handoff", encoding="utf-8")
            self.assertEqual(
                cs_relay.main(
                    [
                        "add-segment",
                        str(job),
                        "--id",
                        "handoff-01",
                        "--kind",
                        "player_handoff",
                        "--first-frame",
                        first,
                        "--last-frame",
                        last,
                        "--prompt-file",
                        "prompt.txt",
                        "--seed",
                        "42",
                        "--duration",
                        "4",
                        "--width",
                        "544",
                        "--height",
                        "736",
                    ]
                ),
                0,
            )
            self.assertEqual(cs_relay.main(["package", str(job)]), 0)
            request = json.loads((job / "remote/request.json").read_text(encoding="utf-8"))
            segment = request["payload"]["segments"][0]
            self.assertEqual(request["schema_version"], "cs-relay.job.v1")
            self.assertEqual(segment["segment_id"], "handoff-01")
            self.assertEqual(segment["workflow"], "player_handoff")
            self.assertEqual(
                segment["first_frame"],
                f"payload/prepared/544x736-center/{Path(first).with_suffix('.png')}",
            )
            self.assertEqual(
                segment["last_frame"],
                f"payload/prepared/544x736-center/{Path(last).with_suffix('.png')}",
            )
            self.assertEqual(segment["width"], 544)
            self.assertEqual(segment["height"], 736)
            self.assertEqual(segment["seed"], 42)
            prepared = request["local_context"]["prepared_endpoints"]
            self.assertEqual(len(prepared), 2)
            self.assertTrue(
                (job / "remote/payload/prepared/544x736-center" / Path(first).with_suffix(".png")).is_file()
            )
            self.assertEqual(
                request["local_context"]["endpoint_preprocess"]["policy"],
                "aspect-preserving center crop",
            )

    def test_shared_relay_endpoint_reuses_one_prepared_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "shared-endpoint-job"
            cs_relay.main(["init", str(job)])
            current = self._register_state(
                job,
                player="player-a",
                state="current-2026",
                period="current",
                year=2026,
                team="Team A Current",
            )
            past = self._register_state(
                job,
                player="player-a",
                state="peak-2018",
                period="peak",
                year=2018,
                team="Team A Peak",
            )
            next_current = self._register_state(
                job,
                player="player-b",
                state="current-2026",
                period="current",
                year=2026,
                team="Team B",
            )
            (job / "prompt.txt").write_text("relay", encoding="utf-8")
            common = [
                "--prompt-file",
                "prompt.txt",
                "--duration",
                "4",
                "--width",
                "544",
                "--height",
                "736",
            ]
            self.assertEqual(
                cs_relay.main(
                    [
                        "add-segment",
                        str(job),
                        "--id",
                        "age",
                        "--kind",
                        "age_transition",
                        "--first-frame",
                        current,
                        "--last-frame",
                        past,
                        "--seed",
                        "1",
                        *common,
                    ]
                ),
                0,
            )
            self.assertEqual(
                cs_relay.main(
                    [
                        "add-segment",
                        str(job),
                        "--id",
                        "handoff",
                        "--kind",
                        "player_handoff",
                        "--first-frame",
                        past,
                        "--last-frame",
                        next_current,
                        "--seed",
                        "2",
                        *common,
                    ]
                ),
                0,
            )
            self.assertEqual(cs_relay.main(["package", str(job)]), 0)
            request = json.loads(
                (job / "remote/request.json").read_text(encoding="utf-8")
            )
            segments = request["payload"]["segments"]
            self.assertEqual(segments[0]["last_frame"], segments[1]["first_frame"])
            self.assertEqual(
                segments[0]["last_frame"],
                f"payload/prepared/544x736-center/{Path(past).with_suffix('.png')}",
            )
            self.assertEqual(
                len(request["local_context"]["prepared_endpoints"]), 3
            )

    def test_generation_photo_rejects_top20_or_gallery_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "bad-photo-job"
            cs_relay.main(["init", str(job)])
            self.assertEqual(
                cs_relay.main(
                    self._registration_argv(
                        job,
                        Path(tmp),
                        photo_source_url="https://img-cdn.hltv.org/gallerypicture/example.jpg",
                    )
                ),
                2,
            )

    def test_team_page_is_rejected_as_historical_portrait_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "team-page-job"
            cs_relay.main(["init", str(job)])
            self.assertEqual(
                cs_relay.main(
                    self._registration_argv(
                        job,
                        Path(tmp),
                        snapshot_url="https://www.hltv.org/team/1/team-a",
                    )
                ),
                2,
            )

    def test_ranking_snapshot_year_must_match_state_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "wrong-year-job"
            cs_relay.main(["init", str(job)])
            self.assertEqual(
                cs_relay.main(
                    self._registration_argv(
                        job,
                        Path(tmp),
                        snapshot_url="https://www.hltv.org/ranking/teams/2025/december/29",
                    )
                ),
                2,
            )

    def test_ranking_snapshot_team_card_must_have_five_players(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "wrong-player-count-job"
            cs_relay.main(["init", str(job)])
            self.assertEqual(
                cs_relay.main(
                    self._registration_argv(
                        job,
                        Path(tmp),
                        snapshot_player_count=4,
                    )
                ),
                2,
            )

    def test_visual_team_review_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "visual-review-job"
            cs_relay.main(["init", str(job)])
            argv = self._registration_argv(job, Path(tmp))
            argv.remove("--visual-team-verified")
            self.assertEqual(cs_relay.main(argv), 2)

    def test_reviewed_official_portrait_override_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "override-job"
            cs_relay.main(["init", str(job)])
            self.assertEqual(
                cs_relay.main(self._override_registration_argv(job, Path(tmp))),
                0,
            )
            state = cs_relay._find_state(
                cs_relay.load_manifest(job), "player-a", "peak-2015"
            )
            self.assertEqual(
                state["portrait_acquisition"]["method"],
                cs_relay.PORTRAIT_OVERRIDE_METHOD,
            )

    def test_reviewed_override_requires_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "override-no-reason-job"
            cs_relay.main(["init", str(job)])
            self.assertEqual(
                cs_relay.main(
                    self._override_registration_argv(
                        job, Path(tmp), override_reason=None
                    )
                ),
                2,
            )

    def test_teamless_current_identity_reference_requires_neutral_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "teamless-current-job"
            cs_relay.main(["init", str(job)])
            argv, endpoint = self._teamless_registration_argv(job, Path(tmp))
            self.assertEqual(cs_relay.main(argv), 0)
            base = [
                "register-endpoint", str(job),
                "--player", "retired-player",
                "--state", "current-2026-retired",
                "--endpoint", str(endpoint),
                "--review-status", "approved",
                "--review-note", "neutral unbranded wardrobe reviewed",
            ]
            self.assertEqual(cs_relay.main(base), 2)
            self.assertEqual(
                cs_relay.main(base + ["--neutralized-teamless-wardrobe"]),
                0,
            )
            state = cs_relay._find_state(
                cs_relay.load_manifest(job), "retired-player", "current-2026-retired"
            )
            self.assertEqual(state["portrait"]["team"], "Falcons")
            self.assertEqual(state["team"], "Retired")
            self.assertEqual(state["endpoint"]["teamless_wardrobe"], "neutral_no_branding")

    def test_teamless_identity_reference_rejects_noncurrent_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "teamless-past-job"
            cs_relay.main(["init", str(job)])
            argv, _endpoint = self._teamless_registration_argv(
                job, Path(tmp), period="past"
            )
            self.assertEqual(cs_relay.main(argv), 2)

    def test_blocked_job_cannot_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp) / "blocked-job"
            cs_relay.main(["init", str(job)])
            manifest = cs_relay.load_manifest(job)
            manifest["status"] = "blocked"
            cs_relay.save_manifest(job, manifest)
            self.assertEqual(cs_relay.main(["validate", str(job)]), 2)


if __name__ == "__main__":
    unittest.main()
