"""Upload checks the PNG against the manifest before it talks to the service."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rutor.__main__ import main
from rutor import config
from rutor.config import write_token
from rutor.login import TERMS_VERSION, keep_terms
from tests.fake_service import FakeService
from tests.png import minimal_png
from tests.test_check import AVAILABLE as DRAWING


def _manifest(width: int, height: int, check: str | None = "ok", sha: str = "abc") -> dict[str, object]:
    manifest: dict[str, object] = {
        "pipeline": "2026.11-a",
        "seed": None,
        "sha256": sha,
        "width": width,
        "height": height,
        "source": {"provider": "unknown", "version": "unknown"},
        "gpu": "unknown",
        "renderedAt": "2026-11-17T12:00:00Z",
    }
    if check is not None:
        manifest["check"] = check
    return manifest


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = FakeService()
        self._saved = {name: os.environ.get(name) for name in ("PIXELBYGGET_HOME", "PIXELBYGGET_URL")}
        os.environ["PIXELBYGGET_HOME"] = self.tmp.name
        os.environ["PIXELBYGGET_URL"] = self.svc.start()
        write_token("test-token")
        # A builder who said yes to today's terms, as a claim and an upload need.
        keep_terms()
        self._root_patch = patch("rutor.config.repo_root", return_value=self.root)
        self._root_patch.start()

    def tearDown(self) -> None:
        self._root_patch.stop()
        self.svc.stop()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def run_cmd(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def write_pair(self, png_side: int, manifest_side: int, check: str | None = "ok") -> None:
        work = self.root / "work" / "goteborg-norr"
        work.mkdir(parents=True)
        data = minimal_png(png_side, png_side)
        (work / "64_59.png").write_bytes(data)
        (work / "64_59.manifest.json").write_text(
            json.dumps(_manifest(manifest_side, manifest_side, check, hashlib.sha256(data).hexdigest())) + "\n",
            encoding="utf-8",
        )

    def test_upload_refuses_a_cell_that_has_not_passed_check(self) -> None:
        for check in (None, "failed"):
            with self.subTest(check=check):
                self.svc.requests.clear()
                work = self.root / "work" / "goteborg-norr"
                if work.exists():
                    for item in work.iterdir():
                        item.unlink()
                    work.rmdir()
                self.write_pair(1536, 1536, check)
                code, _out, err = self.run_cmd(["upload", "goteborg-norr", "64_59"])
                self.assertEqual(code, 5)
                self.assertIn("has not passed rutor check", err)
                self.assertEqual([item for item in self.svc.requests if item.get("method") == "PUT"], [])

    @unittest.skipUnless(DRAWING, "pillow and numpy are not installed")
    def test_check_command_marks_the_manifest(self) -> None:
        from PIL import Image

        from tests.test_check import _redrawn, _scene

        work = self.root / "work" / "goteborg-norr"
        work.mkdir(parents=True)
        source = _scene()
        source.save(work / "64_59.src.png")
        _redrawn(source).save(work / "64_59.png")
        (work / "64_59.manifest.json").write_text(json.dumps(_manifest(1536, 1536, None)) + "\n", encoding="utf-8")
        code, out, err = self.run_cmd(["check", "goteborg-norr", "64_59"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("check: ok", out)
        manifest = json.loads((work / "64_59.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["check"], "ok")
        self.assertEqual(manifest["check_problems"], [])

        Image.open(work / "64_59.src.png").save(work / "64_59.png")
        code, out, _err = self.run_cmd(["check", "goteborg-norr", "64_59"])
        self.assertEqual(code, 5)
        self.assertIn("check: not redrawn", out)
        manifest = json.loads((work / "64_59.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["check"], "failed")
        self.assertTrue(manifest["check_problems"])

    def test_upload_sends_manifest_header_and_prints_state(self) -> None:
        self.write_pair(1536, 1536)
        code, out, err = self.run_cmd(["upload", "goteborg-norr", "64_59"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("uploaded: goteborg-norr 64_59 state vantar\n", out)
        self.assertIn("url: https://karta.opensverige.se/pixelbygget#goteborg-norr/64_59\n", out)
        puts = [item for item in self.svc.requests if item.get("method") == "PUT"]
        self.assertEqual(len(puts), 1)
        sent = puts[0]
        self.assertEqual(sent["path"], "/v1/upload/goteborg-norr/64_59")
        self.assertEqual(sent["body"], (self.root / "work" / "goteborg-norr" / "64_59.png").read_bytes())
        headers = sent["headers"]
        self.assertIsInstance(headers, dict)
        found = ""
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == "x-manifest" and isinstance(value, str):
                found = value
        decoded = json.loads(base64.b64decode(found))
        self.assertEqual(decoded["width"], 1536)
        self.assertEqual(decoded["pipeline"], "2026.11-a")

    def test_an_upload_says_which_terms_its_person_accepted(self) -> None:
        self.write_pair(1536, 1536)
        code, _out, err = self.run_cmd(["upload", "goteborg-norr", "64_59"])
        self.assertEqual((code, err), (0, ""))
        puts = [item for item in self.svc.requests if item.get("method") == "PUT"]
        headers = {str(key).lower(): value for key, value in dict(puts[0]["headers"]).items()}
        self.assertEqual(headers.get("x-terms-version"), TERMS_VERSION)

    def test_nothing_is_uploaded_without_a_yes_to_todays_terms(self) -> None:
        self.write_pair(1536, 1536)
        config.write_terms("2026-11-23", "2026-11-23T10:00:00Z")
        code, _out, err = self.run_cmd(["upload", "goteborg-norr", "64_59"])
        self.assertEqual(code, 2)
        self.assertEqual(
            err,
            f"error: refused: terms_needed: say yes to the terms of {TERMS_VERSION} first: run rutor terms. Any lock you hold is kept.\n",
        )
        self.assertEqual([item for item in self.svc.requests if item.get("method") == "PUT"], [])

    def test_manifest_mismatch_fails_locally(self) -> None:
        self.write_pair(1024, 1536)
        code, _out, err = self.run_cmd(["upload", "goteborg-norr", "64_59"])
        self.assertEqual(code, 1)
        self.assertIn("64_59.png", err)
        self.assertEqual(self.svc.requests, [])

    def test_upload_refused_passes_code(self) -> None:
        self.write_pair(1536, 1536)
        self.svc.fail_next_with(
            422,
            json.dumps({"error": "wrong_pipeline", "message": "pipeline differs"}).encode("utf-8"),
            "application/json",
        )
        code, _out, err = self.run_cmd(["upload", "goteborg-norr", "64_59"])
        self.assertEqual(code, 2)
        self.assertIn("refused: wrong_pipeline:", err)
