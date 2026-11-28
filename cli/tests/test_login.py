"""GitHub device flow: polling, the client id, and the terms."""

from __future__ import annotations

import hashlib
import io
import os
import re
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from rutor.__main__ import main
from rutor.config import github_client_id
from rutor.errors import RutorError
from pathlib import Path

from rutor.login import TERMS, TERMS_VERSION, poll_for_token, start_device_flow

Http = Callable[[str, dict[str, str]], dict[str, object]]


class LoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old = os.environ.get("PIXELBYGGET_GITHUB_CLIENT_ID")
        os.environ.pop("PIXELBYGGET_GITHUB_CLIENT_ID", None)

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("PIXELBYGGET_GITHUB_CLIENT_ID", None)
        else:
            os.environ["PIXELBYGGET_GITHUB_CLIENT_ID"] = self._old

    def test_start_returns_codes(self) -> None:
        seen: dict[str, object] = {}

        def http(url: str, form: dict[str, str]) -> dict[str, object]:
            seen["url"] = url
            seen["form"] = dict(form)
            return {
                "device_code": "dev",
                "user_code": "ABCD-1234",
                "verification_uri": "https://github.com/login/device",
                "interval": 5,
                "expires_in": 900,
            }

        result = start_device_flow("client", http=http)
        self.assertEqual(result["device_code"], "dev")
        self.assertEqual(result["user_code"], "ABCD-1234")
        self.assertEqual(result["interval"], 5)
        self.assertIn("/login/device/code", str(seen["url"]))
        form = seen["form"]
        self.assertIsInstance(form, dict)
        self.assertEqual(form["client_id"], "client")
        self.assertEqual(form["scope"], "")

    def test_poll_returns_token_when_granted(self) -> None:
        def http(_url: str, _form: dict[str, str]) -> dict[str, object]:
            return {"access_token": "gho_token"}

        sleeps: list[float] = []
        token = poll_for_token("client", "dev", 5, sleeps.append, http=http)
        self.assertEqual(token, "gho_token")
        self.assertEqual(sleeps, [])

    def test_poll_slow_down_backs_off(self) -> None:
        answers: list[dict[str, object]] = [{"error": "slow_down"}, {"access_token": "gho_token"}]

        def http(_url: str, _form: dict[str, str]) -> dict[str, object]:
            return answers.pop(0)

        sleeps: list[float] = []
        interval = 5
        token = poll_for_token("client", "dev", interval, sleeps.append, http=http)
        self.assertEqual(token, "gho_token")
        self.assertEqual(sleeps, [float(interval + 5)])

    def test_poll_authorization_pending_keeps_waiting(self) -> None:
        answers: list[dict[str, object]] = [
            {"error": "authorization_pending"},
            {"error": "authorization_pending"},
            {"access_token": "gho_token"},
        ]

        def http(_url: str, _form: dict[str, str]) -> dict[str, object]:
            return answers.pop(0)

        sleeps: list[float] = []
        token = poll_for_token("client", "dev", 5, sleeps.append, http=http)
        self.assertEqual(token, "gho_token")
        self.assertEqual(sleeps, [5.0, 5.0])

    def test_expired_device_code_exit_3(self) -> None:
        def http(_url: str, _form: dict[str, str]) -> dict[str, object]:
            return {"error": "expired_token"}

        with self.assertRaises(RutorError) as caught:
            poll_for_token("client", "dev", 5, lambda _seconds: None, http=http)
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("login", caught.exception.message)

    def test_default_client_id_is_the_pixelbygget_app(self) -> None:
        self.assertEqual(github_client_id(), "Ov23liulid4VoLnmJCVH")

    def test_empty_client_id_refused(self) -> None:
        os.environ["PIXELBYGGET_GITHUB_CLIENT_ID"] = ""
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["login"])
        self.assertEqual(code, 3)
        self.assertIn("PIXELBYGGET_GITHUB_CLIENT_ID", stderr.getvalue())

    def test_the_terms_say_what_a_volunteer_must_know(self) -> None:
        # One text in two places: TERMS.md for people, rutor login for the terminal.
        written = (Path(__file__).resolve().parents[2] / "TERMS.md").read_text(encoding="utf-8")
        for line in TERMS.splitlines():
            self.assertIn(line.split(". ", 1)[1], written, line)
        lowered = TERMS.lower()
        for must in (
            "voluntary",
            "unpaid",
            "without warranty",
            "your own cost",
            "a person reviews",
            "shown publicly",
            "qwen research license",
            "openstreetmap",
            "google's map tiles policy",
            "empty scope",
        ):
            self.assertIn(must, lowered)
        # Short enough to read before saying yes.
        self.assertLessEqual(len(TERMS.splitlines()), 10)

    def test_the_terms_name_the_source_of_a_sheet_not_the_city(self) -> None:
        # A cell may lie in any town now. A line that names stockholm-norr alone would tell a
        # builder in Helsingborg nothing about Google's tiles, which their cell is drawn from.
        lines = TERMS.splitlines()
        self.assertTrue(lines[8].startswith("9. A sheet with open data as its source is drawn from"), lines[8])
        self.assertIn("goteborg-norr", lines[8])
        self.assertIn("(CC0)", lines[8])
        self.assertTrue(
            lines[9].startswith(
                "10. Every other sheet, stockholm-norr among them, is drawn from Google's 3D tiles through your own Cesium ion account, whose terms bind you, not this project."
            ),
            lines[9],
        )
        # What line 10 said about Google's policy stands, word for word.
        self.assertTrue(
            lines[9].endswith(
                "Google's Map Tiles policy does not allow deriving new content from its tiles: read it and decide for yourself. Google can close your access and ask for those cells to be taken down."
            ),
            lines[9],
        )
        written = (Path(__file__).resolve().parents[2] / "TERMS.md").read_text(encoding="utf-8")
        self.assertIn("Version 2026-11-25.", written)

    def test_one_terms_version_in_terms_md_the_cli_and_the_service(self) -> None:
        # The service refuses a claim whose rutor says an older version than its own, so the
        # three dates move together or a builder is refused for terms they have accepted.
        root = Path(__file__).resolve().parents[2]
        written = (root / "TERMS.md").read_text(encoding="utf-8")
        self.assertIn(f"Version {TERMS_VERSION}.", written)
        service = (root / "service" / "src" / "terms.js").read_text(encoding="utf-8")
        found = re.search(r"export const TERMS_VERSION = '([0-9-]+)';", service)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.group(1), TERMS_VERSION)

    def test_the_ten_lines_change_only_with_a_new_version(self) -> None:
        # Every builder said yes to one text under one date. A new text under the old date
        # would be one nobody is asked about: give it a new date, in all three places, and
        # add its digest here.
        pinned = {
            "2026-11-25": "8591b948a44becdc69f69a27645a8a75adb989b34c8688f14d7dcbf228465bca",
        }
        self.assertIn(TERMS_VERSION, pinned, "a new terms version: add its digest here")
        self.assertEqual(hashlib.sha256(TERMS.encode("utf-8")).hexdigest(), pinned[TERMS_VERSION])

    def test_login_requires_accept_terms_or_prints_terms(self) -> None:
        os.environ["PIXELBYGGET_GITHUB_CLIENT_ID"] = "Iv1.real"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["login"])
        self.assertEqual(code, 1)
        self.assertIn(TERMS, stdout.getvalue())
        self.assertIn("MIT", TERMS)
