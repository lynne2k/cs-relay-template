from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_release as guard


def entry(text: str, path: str = "docs/example.md", mode: str = "100644") -> guard.Entry:
    return guard.Entry(path, mode, text.encode())


def private_ip() -> str:
    return ".".join(map(str, (10, 11, 12, 13)))


def demo_attachment() -> str:
    identifier = "-".join(("12345678", "1234", "4234", "8234", "123456789abc"))
    return "https://github.com/user-attachments/assets/" + identifier


def demo_allowlist(text: str, path: str = "README.md") -> set[tuple[str, str, str]]:
    item = {
        "path": path,
        "category": "public-demo-attachment",
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "reason": "Explicitly approved public demonstration attachment.",
    }
    return guard.parse_allowlist(json.dumps({"version": 1, "entries": [item]}).encode())


class ReleaseContentTests(unittest.TestCase):
    def categories(self, text: str) -> set[str]:
        return {finding.category for finding in guard.scan([entry(text)])}

    def test_personal_paths(self):
        for path in (
            "/" + "/".join(("Users", "person", "project")),
            "/" + "/".join(("home", "person", "project")),
            "/" + "/".join(("mnt", "shared", "users", "person", "project")),
            "C:" + "\\".join(("", "Users", "person", "project")),
        ):
            with self.subTest(path=path):
                self.assertIn("local-identity-path", self.categories(path))

    def test_private_network_addresses_and_documentation_exceptions(self):
        self.assertIn("private-network-address", self.categories(private_ip()))
        self.assertIn("private-network-address", self.categories("[" + "fd00" + "::2]"))
        for ip in ("127.0.0.1", "192.0.2.40", "198.51.100.8", "203.0.113.22"):
            self.assertNotIn("private-network-address", self.categories(ip))
        self.assertNotIn("private-network-address", self.categories("http://[::1]:8188/"))

    def test_secrets_and_connection_identifiers(self):
        samples = {
            "private-key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "credential": "ghp_" + "q" * 30,
            "ssh-connection": "worker" + "@" + private_ip() + ":/project",
            "production-identifier": "-".join(("12345678", "1234", "4234", "8234", "123456789abc")),
        }
        for category, value in samples.items():
            with self.subTest(category=category):
                self.assertIn(category, self.categories(value))
        for name in ("api_key", "access_token", "password"):
            self.assertIn("credential", self.categories(json.dumps({name: "unsafe-sensitive-value"})))
        self.assertIn("credential", self.categories("SERVICE_API_KEY" + "=unsafe-sensitive-value"))
        host = "production-machine"
        self.assertIn("ssh-connection", self.categories(json.dumps({"remote_host": host})))
        self.assertIn("production-identifier", self.categories(json.dumps({"job_id": "private-run-" + "2026" + "0901"})))

    def test_placeholders_are_allowed(self):
        sample = json.dumps({"remote_host": "example-remote", "api_key": "${SERVICE_API_KEY}", "job_id": "demo-job"})
        self.assertEqual(set(), self.categories(sample))
        self.assertEqual(set(), self.categories("actions/checkout@v4"))
        self.assertEqual(set(), self.categories("actions/setup-python@v5"))
        self.assertEqual(set(), self.categories("git@example.com:/repository"))
        self.assertEqual(set(), self.categories("SERVICE_API_KEY" + "=${SERVICE_API_KEY}"))

    def test_dangerous_file_types_and_runtime_paths(self):
        for path, category in (
            ("jobs/my-job.json", "non-public-path"),
            ("config.json", "runtime-or-auth-file"),
            ("tests/weights.safetensors", "media-model-or-archive"),
            ("examples/video.mp4", "media-model-or-archive"),
            ("scripts/.ssh/config", "runtime-or-auth-file"),
        ):
            self.assertIn(category, {finding.category for finding in guard.scan([entry("", path)])})
        oversized = guard.Entry("docs/large.txt", "100644", b"a" * (guard.MAX_FILE_BYTES + 1))
        self.assertIn("oversize-file", {finding.category for finding in guard.scan([oversized])})

    def test_allowlist_is_exact_and_content_bound(self):
        text = private_ip()
        item = {"path": "docs/example.md", "category": "private-network-address", "sha256": hashlib.sha256(text.encode()).hexdigest(), "reason": "Synthetic address for a bounded test."}
        allowed = guard.parse_allowlist(json.dumps({"version": 1, "entries": [item]}).encode())
        self.assertEqual([], guard.scan([entry(text)], allowed))
        self.assertTrue(guard.scan([entry(text, "docs/different.md")], allowed))
        self.assertTrue(guard.scan([entry(text + " ")], allowed))
        for field, value in (("category", "credential"), ("path", "docs/*"), ("reason", "short")):
            invalid = dict(item, **{field: value})
            with self.assertRaises(ValueError):
                guard.parse_allowlist(json.dumps({"version": 1, "entries": [invalid]}).encode())

    def test_small_synthetic_png_needs_an_exact_allowlist(self):
        png = b"\x89PNG\r\n\x1a\n" + b"synthetic-test-data"
        image = guard.Entry("examples/synthetic/square.png", "100644", png)
        allowed = {(image.path, "synthetic-image", hashlib.sha256(png).hexdigest())}
        self.assertTrue(guard.scan([image]))
        self.assertEqual([], guard.scan([image], allowed))
        self.assertTrue(guard.scan([guard.Entry("examples/real.png", "100644", png)], allowed))
        big = guard.Entry(image.path, "100644", png + b"x" * guard.MAX_SYNTHETIC_PNG_BYTES)
        self.assertTrue(guard.scan([big], allowed))

    def test_public_demo_attachment_requires_exact_readme_and_line_hash(self):
        url = demo_attachment()
        for path in ("README.md", "README.zh-CN.md"):
            with self.subTest(path=path):
                self.assertIn("production-identifier", {f.category for f in guard.scan([entry(url, path)])})
                allowed = demo_allowlist(url, path)
                self.assertEqual([], guard.scan([entry(url, path)], allowed))
                self.assertEqual([], guard.scan([entry("# Demo\n\n" + url + "\n", path)], allowed))
                self.assertTrue(guard.scan([entry(url[:-1] + "d", path)], allowed))
                other = "README.zh-CN.md" if path == "README.md" else "README.md"
                self.assertTrue(guard.scan([entry(url, other)], allowed))

    def test_public_demo_attachment_cannot_expand_to_other_paths(self):
        url = demo_attachment()
        for path in ("docs/demo.md", "README.fr.md", "docs/README.md", "examples/video.mp4"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    demo_allowlist(url, path)
                # Even a direct scan caller cannot bypass the path boundary.
                allowed = {(path, "public-demo-attachment", hashlib.sha256(url.encode()).hexdigest())}
                self.assertTrue(guard.scan([entry(url, path)], allowed))

    def test_public_demo_attachment_rejects_unsafe_url_and_line_shapes(self):
        url = demo_attachment()
        candidates = (
            " " + url, url + " ", url + "?download=1", url + "#video",
            url + "/", url + ".mp4", "[Demo](" + url + ")", "<" + url + ">",
            "Demo: " + url, url + " " + url.rsplit("/", 1)[1],
            url.replace("https://", "http://"),
            url.replace("github.com/", "github.com.example.org/"),
            url.replace("github.com/", "github.com:443/"),
            url.replace("github.com/", "user:password@github.com/"),
            url.replace("/assets/", "/other/"),
            json.dumps({"project_id": url.rsplit("/", 1)[1]}),
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                allowed = demo_allowlist(candidate)
                self.assertIn("production-identifier", {f.category for f in guard.scan([entry(candidate, "README.md")], allowed)})

    def test_public_demo_attachment_does_not_hide_secrets_or_other_ids(self):
        url = demo_attachment()
        secret = "ghp_" + "q" * 30
        extra_id = url.rsplit("/", 1)[1]
        content = url + "\n" + secret + "\n" + extra_id
        categories = {f.category for f in guard.scan([entry(content, "README.md")], demo_allowlist(url))}
        self.assertEqual({"credential", "production-identifier"}, categories)
        same_line = url + " " + secret
        categories = {f.category for f in guard.scan([entry(same_line, "README.md")], demo_allowlist(same_line))}
        self.assertEqual({"credential", "production-identifier"}, categories)
        media = entry(url, "examples/demo.mp4")
        self.assertIn("media-model-or-archive", {f.category for f in guard.scan([media], demo_allowlist(url))})
        item = {
            "path": "README.md", "category": "production-identifier",
            "sha256": hashlib.sha256(extra_id.encode()).hexdigest(),
            "reason": "Arbitrary production identifiers must remain forbidden.",
        }
        with self.assertRaises(ValueError):
            guard.parse_allowlist(json.dumps({"version": 1, "entries": [item]}).encode())

    def test_symlink_must_resolve_to_an_included_in_tree_file(self):
        destination = entry("safe", "docs/destination.md")
        self.assertEqual([], guard.scan([destination, entry("destination.md", "docs/link.md", "120000")]))
        for target in ("../../outside", "/etc/passwd", "missing.md", "link.md"):
            findings = guard.scan([destination, entry(target, "docs/link.md", "120000")])
            self.assertIn("unsafe-symlink", {finding.category for finding in findings})
        chain = [entry("b.md", "docs/a.md", "120000"), entry("../../outside", "docs/b.md", "120000")]
        self.assertEqual(2, sum(f.category == "unsafe-symlink" for f in guard.scan(chain)))

    def test_scanner_and_test_source_do_not_self_match(self):
        for path in (Path(guard.__file__), Path(__file__)):
            source = guard.Entry("scripts/" + path.name, "100644", path.read_bytes())
            self.assertEqual([], guard.scan([source]))


class ReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def test_index_blob_wins_over_clean_worktree(self):
        self.git("init", "-q")
        path = self.root / "README.md"
        path.write_text(private_ip())
        self.git("add", "README.md")
        path.write_text("safe replacement in unstaged working tree")
        entries, source = guard.collect_entries(self.root)
        self.assertEqual("index", source)
        self.assertIn("private-network-address", {finding.category for finding in guard.scan(entries)})
        self.git("add", "README.md")
        path.write_text(private_ip())
        entries, _ = guard.collect_entries(self.root)
        self.assertEqual([], guard.scan(entries))

    def test_directory_fallback_excludes_runtime_data(self):
        (self.root / "README.md").write_text("safe")
        (self.root / "config.json").write_text(private_ip())
        (self.root / "jobs").mkdir()
        (self.root / "jobs" / "job.json").write_text(private_ip())
        (self.root / "scripts" / "__pycache__").mkdir(parents=True)
        (self.root / "scripts" / "__pycache__" / "cached.pyc").write_bytes(b"\xff")
        entries, source = guard.collect_entries(self.root)
        self.assertEqual("directory", source)
        self.assertEqual(["README.md"], [item.path for item in entries])

    def test_forced_index_requires_a_repository(self):
        with self.assertRaises(ValueError):
            guard.collect_entries(self.root, "index")

    def test_reports_only_location_and_category(self):
        secret = "ghp_" + "q" * 30
        (self.root / "README.md").write_text(secret)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = guard.main(["--root", str(self.root)])
        self.assertEqual(1, result)
        self.assertNotIn(secret, output.getvalue())
        self.assertEqual('"README.md":1:credential\n', output.getvalue())

    def test_unstaged_allowlist_cannot_hide_index_secrets(self):
        self.git("init", "-q")
        (self.root / "README.md").write_text(private_ip())
        self.git("add", "README.md")
        (self.root / "release-allowlist.json").write_text(json.dumps({"version": 1, "entries": []}))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, guard.main(["--root", str(self.root)]))


if __name__ == "__main__":
    unittest.main()
