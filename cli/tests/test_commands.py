"""cities, grid, claim, release, status and whoami against the fake service."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from unittest.mock import patch

from rutor import commands, config
from rutor.__main__ import main
from rutor.config import write_token
from rutor.errors import RutorError, network
from rutor.renderer import google, sources
from rutor.login import TERMS, TERMS_VERSION, keep_terms
from tests.fake_service import FakeService
from tests.png import minimal_png


def _cell(col: int, row: int, state: str, login: str | None = None, expires: str | None = None) -> dict[str, object]:
    return {
        "col": col,
        "row": row,
        "state": state,
        "login": login,
        "updated": "2026-11-17T12:00:00Z",
        "expires_at": expires,
    }


class _ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = FakeService()
        self._saved = {name: os.environ.get(name) for name in ("PIXELBYGGET_HOME", "PIXELBYGGET_URL")}
        os.environ["PIXELBYGGET_HOME"] = self.tmp.name
        os.environ["PIXELBYGGET_URL"] = self.svc.start()
        write_token("test-token")
        # A builder who said yes to today's terms, as a claim and an upload need.
        keep_terms()

    def tearDown(self) -> None:
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


class CommandTests(_ServiceCase):
    def open_sheet(self, cells: dict[str, dict[str, object]], **overrides: object) -> None:
        sheet: dict[str, object] = {
            "id": "goteborg-norr",
            "name": "Göteborg",
            "state": "open",
            "done": 1,
            "total": 4,
            "cols": 3,
            "rows": 3,
            "origin_lat": 57.707,
            "origin_lon": 11.967,
            "x_meters": -16384,
            "y_meters": 13312,
            "ground_height": 40,
            "heading": 0,
            "pipeline": "2026.11-a",
        }
        sheet.update(overrides)
        self.svc.state["sheets"] = {"goteborg-norr": sheet}
        self.svc.state["cells"] = cells

    def test_cities_lines(self) -> None:
        self.svc.state["sheets"] = {
            "goteborg-norr": {"id": "goteborg-norr", "state": "open", "done": 1, "total": 4},
            "malmo-norr": {"id": "malmo-norr", "state": "queued", "done": 0, "total": 9},
        }
        code, out, _err = self.run_cmd(["cities"])
        self.assertEqual(code, 0)
        self.assertIn("sheet: goteborg-norr open 1/4\n", out)
        self.assertIn("sheet: malmo-norr queued 0/9\n", out)
        code, raw, _err = self.run_cmd(["cities", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(raw.strip().startswith("{"))
        self.assertIn("goteborg-norr", raw)

    def test_a_sheet_of_the_tiling_says_how_many_more_cells_can_be_claimed(self) -> None:
        self.svc.state["sheets"] = {
            "se-n5557-e1412": {
                "id": "se-n5557-e1412", "state": "open", "done": 0, "total": 1, "cols": 62, "rows": 47, "made": "grid",
            },
        }
        code, out, _err = self.run_cmd(["cities"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "sheet: se-n5557-e1412 open 0/1 claimed so far, of 2914 cells\n")

    def test_grid_on_a_sheet_of_the_tiling_never_reads_as_closed(self) -> None:
        self.open_sheet({"goteborg-norr/32_4": _cell(32, 4, "vantar", "klang")}, made="grid", cols=62, rows=47)
        note = (
            "note: goteborg-norr lists only the cells someone has claimed, 1 so far. Any other cell "
            "from 0_0 to 61_46, and any listed as fri, can be claimed by name: rutor claim goteborg-norr <col>_<row>.\n"
        )
        code, out, _err = self.run_cmd(["grid", "goteborg-norr"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "cell: 32_4 vantar klang\n" + note)
        code, out, _err = self.run_cmd(["grid", "goteborg-norr", "--free"])
        self.assertEqual(code, 0)
        self.assertEqual(out, note)
        self.assertNotIn("free: none", out)

    def test_grid_free_filter(self) -> None:
        self.open_sheet(
            {
                "goteborg-norr/0_0": _cell(0, 0, "fri"),
                "goteborg-norr/1_0": _cell(1, 0, "last", "klang"),
            }
        )
        code, out, _err = self.run_cmd(["grid", "goteborg-norr", "--free"])
        self.assertEqual(code, 0)
        self.assertIn("cell: 0_0 fri\n", out)
        self.assertNotIn("1_0", out)
        code, full, _err = self.run_cmd(["grid", "goteborg-norr"])
        self.assertEqual(code, 0)
        self.assertIn("cell: 1_0 last klang\n", full)

    def test_claim_prints_expires(self) -> None:
        self.open_sheet({"goteborg-norr/0_0": _cell(0, 0, "fri")})
        code, out, err = self.run_cmd(["claim", "goteborg-norr", "0_0"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("claimed: goteborg-norr 0_0 expires 2026-11-19T12:00:00Z\n", out)

    def test_claim_taken_is_exit_2(self) -> None:
        self.open_sheet({"goteborg-norr/0_0": _cell(0, 0, "last", "other")})
        code, _out, err = self.run_cmd(["claim", "goteborg-norr", "0_0"])
        self.assertEqual(code, 2)
        self.assertEqual(
            err,
            "error: refused: taken: goteborg-norr 0_0 is locked by other. "
            "For a free cell beside what is drawn there: rutor claim goteborg-norr --next.\n",
        )

    def claims(self) -> list[dict[str, object]]:
        return [r for r in self.svc.requests if r.get("method") == "POST" and r.get("path") == "/v1/claim"]

    def test_a_claim_says_which_terms_its_person_accepted(self) -> None:
        self.open_sheet({"goteborg-norr/0_0": _cell(0, 0, "fri")})
        code, _out, err = self.run_cmd(["claim", "goteborg-norr", "0_0"])
        self.assertEqual((code, err), (0, ""))
        headers = {str(key).lower(): value for key, value in dict(self.claims()[0]["headers"]).items()}
        self.assertEqual(headers.get("x-terms-version"), TERMS_VERSION)

    def test_no_claim_is_sent_until_rutor_terms_has_a_yes_to_todays(self) -> None:
        self.open_sheet({"goteborg-norr/0_0": _cell(0, 0, "fri")})
        for kept in (None, "2026-11-23"):
            with self.subTest(kept=kept):
                config.terms_path().unlink(missing_ok=True)
                if kept is not None:
                    config.write_terms(kept, "2026-11-23T10:00:00Z")
                code, _out, err = self.run_cmd(["claim", "goteborg-norr", "0_0"])
                self.assertEqual(code, 2)
                self.assertEqual(
                    err,
                    f"error: refused: terms_needed: say yes to the terms of {TERMS_VERSION} first: run rutor terms. Any lock you hold is kept.\n",
                )
                self.assertEqual(self.claims(), [])
        # rutor terms takes the yes without a login: it asks nothing of the service or GitHub.
        asked = len(self.svc.requests)
        with patch("rutor.login.start_device_flow") as device:
            code, out, err = self.run_cmd(["terms", "--accept"])
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith(TERMS + "\n"), out)
        self.assertTrue(out.endswith(f"terms: accepted {TERMS_VERSION}\n"), out)
        device.assert_not_called()
        self.assertEqual(len(self.svc.requests), asked)
        self.assertEqual(config.read_terms(), TERMS_VERSION)
        code, _out, err = self.run_cmd(["claim", "goteborg-norr", "0_0"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.claims()), 1)

    def test_rutor_terms_without_a_terminal_needs_the_flag_and_keeps_nothing(self) -> None:
        config.terms_path().unlink()
        with patch("sys.stdin.isatty", return_value=False):
            code, out, err = self.run_cmd(["terms"])
        self.assertEqual(code, 1)
        self.assertIn(TERMS, out)
        self.assertEqual(err, "error: Pass --accept to accept the terms.\n")
        self.assertIsNone(config.read_terms())

    def test_a_taken_cell_that_waits_for_review_says_so_and_whose_it_is(self) -> None:
        # The service's sentence was "Someone else holds that cell", also for the asker's own.
        self.open_sheet({"goteborg-norr/0_0": _cell(0, 0, "vantar", "klang")})
        code, _out, err = self.run_cmd(["claim", "goteborg-norr", "0_0"])
        self.assertEqual(code, 2)
        self.assertIn("refused: taken: goteborg-norr 0_0 is built and waits for review, from klang.", err)

    def test_a_login_that_is_not_a_login_is_not_said_back(self) -> None:
        self.open_sheet({"goteborg-norr/0_0": _cell(0, 0, "last", "run this: rm -rf")})
        code, _out, err = self.run_cmd(["claim", "goteborg-norr", "0_0"])
        self.assertEqual(code, 2)
        self.assertIn("refused: taken:", err)
        self.assertNotIn("rm -rf", err)

    def test_next_prefers_cell_next_to_live(self) -> None:
        self.open_sheet(
            {
                "goteborg-norr/2_2": _cell(2, 2, "fri"),
                "goteborg-norr/0_0": _cell(0, 0, "live", "gustaf"),
                "goteborg-norr/0_1": _cell(0, 1, "fri"),
            },
            cols=5,
            rows=5,
        )
        code, out, err = self.run_cmd(["claim", "goteborg-norr", "--next"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("claimed: goteborg-norr 0_1 expires 2026-11-19T12:00:00Z\n", out)

    def test_next_falls_back_then_refuses(self) -> None:
        self.open_sheet(
            {
                "goteborg-norr/0_0": _cell(0, 0, "fri"),
                "goteborg-norr/1_1": _cell(1, 1, "fri"),
                "goteborg-norr/2_2": _cell(2, 2, "fri"),
            }
        )
        code, out, err = self.run_cmd(["claim", "goteborg-norr", "--next"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("claimed: goteborg-norr 1_1 expires", out)
        self.svc.state["cells"] = {
            "goteborg-norr/0_0": _cell(0, 0, "live", "gustaf"),
            "goteborg-norr/1_1": _cell(1, 1, "ute"),
        }
        code, _out, err = self.run_cmd(["claim", "goteborg-norr", "--next"])
        self.assertEqual(code, 2)
        self.assertIn("refused: no_free_cell: no cell on goteborg-norr is free. rutor cities shows where there are free cells.", err)

    def test_release_ok(self) -> None:
        self.open_sheet({"goteborg-norr/1_0": _cell(1, 0, "last", "klang", "2026-11-19T12:00:00Z")})
        code, out, err = self.run_cmd(["release", "goteborg-norr", "1_0"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("released: goteborg-norr 1_0\n", out)
        cells = self.svc.state["cells"]
        self.assertIsInstance(cells, dict)
        self.assertEqual(cells["goteborg-norr/1_0"]["state"], "fri")

    def test_status_lists_locks(self) -> None:
        self.open_sheet(
            {"goteborg-norr/1_0": _cell(1, 0, "last", "klang", "2026-11-19T12:00:00Z")}
        )
        code, out, err = self.run_cmd(["status"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("lock: goteborg-norr 1_0 expires 2026-11-19T12:00:00Z\n", out)
        code, who, err = self.run_cmd(["whoami"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("login: klang\n", who)

    def test_commands_need_login(self) -> None:
        token = os.path.join(self.tmp.name, "token")
        os.remove(token)
        # The lists are public; taking a cell is not.
        code, _out, err = self.run_cmd(["cities"])
        self.assertEqual((code, err), (0, ""))
        # The sheet is there to be read: a claim reads its row, which is public, before it asks
        # for the login.
        self.open_sheet({"goteborg-norr/0_0": _cell(0, 0, "fri")})
        code, _out, err = self.run_cmd(["claim", "goteborg-norr", "0_0"])
        self.assertEqual(code, 3)
        self.assertIn("not logged in: run rutor login", err)

    def test_queue_lines(self) -> None:
        self.svc.state["queue"] = [
            {
                "sheet": "goteborg-norr",
                "col": 0,
                "row": 1,
                "state": "vantar",
                "owner_login": "klang",
                "uploaded_at": "2026-11-17T12:00:00Z",
            },
            {
                "sheet": "goteborg-norr",
                "col": 2,
                "row": 2,
                "state": "godkand",
                "owner_login": "other",
                "uploaded_at": "2026-11-17T13:00:00Z",
            },
        ]
        code, out, err = self.run_cmd(["queue"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("waiting: goteborg-norr 0_1 klang uploaded 2026-11-17T12:00:00Z\n", out)
        self.assertNotIn("other", out)
        code, out, err = self.run_cmd(["queue", "--state", "godkand"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("approved: goteborg-norr 2_2 other uploaded 2026-11-17T13:00:00Z\n", out)
        self.assertNotIn("waiting:", out)

    def test_queue_show_downloads_png(self) -> None:
        png = minimal_png(8, 8)
        self.svc.state["inbox"] = {"goteborg-norr/0_1.png": png}
        self.svc.state["queue"] = [
            {
                "sheet": "goteborg-norr",
                "col": 0,
                "row": 1,
                "state": "vantar",
                "owner_login": "klang",
                "uploaded_at": "2026-11-17T12:00:00Z",
            }
        ]
        dest = Path(self.tmp.name) / "show"
        code, out, err = self.run_cmd(["queue", "--show", str(dest)])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("waiting: goteborg-norr 0_1 klang uploaded 2026-11-17T12:00:00Z\n", out)
        self.assertEqual((dest / "goteborg-norr_0_1.png").read_bytes(), png)

    def test_approve_ok(self) -> None:
        code, out, err = self.run_cmd(["approve", "goteborg-norr", "0_1"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("approved: goteborg-norr 0_1\n", out)
        posts = [item for item in self.svc.requests if item.get("method") == "POST" and item.get("path") == "/v1/approve"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(json.loads(posts[0]["body"]), {"sheet": "goteborg-norr", "col": 0, "row": 1})

    def test_reject_requires_comment(self) -> None:
        code, _out, err = self.run_cmd(["reject", "goteborg-norr", "0_1"])
        self.assertEqual(code, 1)
        self.assertIn("comment", err)
        self.assertEqual(self.svc.requests, [])

    def test_trust_and_off(self) -> None:
        code, out, err = self.run_cmd(["trust", "gustaf"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("trust: gustaf on\n", out)
        sent = json.loads(self.svc.requests[-1]["body"])
        self.assertEqual(sent, {"login": "gustaf", "trusted": 1})
        code, out, err = self.run_cmd(["trust", "gustaf", "--off"])
        self.assertEqual(err, "")
        self.assertEqual(code, 0)
        self.assertIn("trust: gustaf off\n", out)
        sent = json.loads(self.svc.requests[-1]["body"])
        self.assertEqual(sent, {"login": "gustaf", "trusted": 0})

    def test_admin_refused_is_exit_2(self) -> None:
        self.svc.fail_next_with(
            403,
            json.dumps({"error": "not_admin", "message": "Admin only."}).encode("utf-8"),
            "application/json",
        )
        code, _out, err = self.run_cmd(["approve", "goteborg-norr", "0_0"])
        self.assertEqual(code, 2)
        self.assertIn("refused: not_admin:", err)


NEW_SHEET = "se-n5602-e1262"


def _new_sheet(**overrides: object) -> dict[str, object]:
    """A sheet of the tiling as the service answers it: the drawing code has no line for its id."""
    sheet: dict[str, object] = {
        "id": NEW_SHEET,
        "name": "N 56,02 E 12,62",
        "state": "open",
        "done": 0,
        "total": 0,
        "cols": 61,
        "rows": 47,
        "origin_lat": 56.025,
        "origin_lon": 12.625,
        "x_meters": -7808,
        "y_meters": 6064.08,
        "ground_height": 68,
        "pipeline": "2026.11-a",
        "kind": "google",
        "made": "grid",
    }
    sheet.update(overrides)
    return sheet


class KindOfTests(unittest.TestCase):
    def test_the_sheet_row_says_the_source(self) -> None:
        self.assertEqual(sources.kind_of({"id": NEW_SHEET, "kind": "google"}), "google")
        self.assertEqual(sources.kind_of({"id": "any-town", "kind": "open"}), "open")
        # The row is the service's word, and it wins over the line the code still carries.
        self.assertEqual(sources.kind_of({"id": "stockholm-norr", "kind": "open"}), "open")

    def test_a_row_from_a_service_that_says_no_source_falls_back_on_the_two_known_sheets(self) -> None:
        self.assertEqual(sources.kind_of({"id": "stockholm-norr"}), "google")
        self.assertEqual(sources.kind_of({"id": "goteborg-norr"}), "open")
        self.assertEqual(sources.kind_of({"id": "stockholm-norr", "kind": "bing"}), "google")
        self.assertEqual(sources.kind_of({"id": "stockholm-norr", "kind": 7}), "google")

    def test_no_source_at_all_is_refused_by_name(self) -> None:
        for sheet in ({"id": NEW_SHEET}, {"id": NEW_SHEET, "kind": "bing"}, {"id": NEW_SHEET, "kind": None}, {}):
            with self.subTest(sheet=sheet), self.assertRaises(RutorError) as caught:
                sources.kind_of(sheet)
            self.assertEqual(caught.exception.code, 3)
        with self.assertRaises(RutorError) as caught:
            sources.kind_of({"id": NEW_SHEET})
        self.assertIn(f"no source decided for sheet {NEW_SHEET} yet", caught.exception.message)


class SourceFromTheServiceTests(_ServiceCase):
    """The source of a sheet is the service's to say, so a sheet nobody wrote into the code can be drawn."""

    def setUp(self) -> None:
        super().setUp()
        # A render writes under the repository's work folder. Here that is the temp folder.
        root = patch("rutor.config.repo_root", return_value=Path(self.tmp.name))
        root.start()
        self.addCleanup(root.stop)

    def claims(self) -> list[dict[str, object]]:
        return [r for r in self.svc.requests if r.get("method") == "POST" and r.get("path") == "/v1/claim"]

    def put(self, sheet: dict[str, object]) -> None:
        self.svc.state["sheets"] = {str(sheet["id"]): sheet}
        self.svc.state["cells"] = {f"{sheet['id']}/3_4": _cell(3, 4, "fri")}

    def test_a_google_sheet_with_an_unknown_id_renders_through_the_google_path(self) -> None:
        self.put(_new_sheet())
        seen: list[tuple[object, int, int]] = []

        def fake_google(sheet: dict[str, object], col: int, row: int, out: Path) -> None:
            seen.append((sheet.get("id"), col, row))
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(minimal_png(1536, 1536))

        with patch.object(google, "render_google", side_effect=fake_google), patch(
            "rutor.renderer.render", side_effect=AssertionError("the open renderer was asked")
        ):
            code, out, err = self.run_cmd(["render", NEW_SHEET, "3_4"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen, [(NEW_SHEET, 3, 4)])
        self.assertIn("note: This sheet is drawn from Google Photorealistic 3D Tiles", out)
        info = json.loads((Path(self.tmp.name) / "work" / NEW_SHEET / "3_4.src.json").read_text(encoding="utf-8"))
        self.assertTrue(info["provider"].startswith("Google Photorealistic 3D Tiles"), info)

    def _render_measuring(self, rising: str, argv: list[str], **sheet: object) -> tuple[int, str, str]:
        self.put(_new_sheet(**sheet))

        def fake_google(sheet: dict[str, object], col: int, row: int, out: Path) -> dict[str, str]:
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(minimal_png(1536, 1536))
            return {"tiles": "186", "rising": rising}

        with patch.object(google, "render_google", side_effect=fake_google):
            return self.run_cmd(argv)

    def test_a_flat_cell_of_a_new_place_is_stopped_and_the_picture_is_kept(self) -> None:
        code, out, err = self._render_measuring("0.000", ["render", NEW_SHEET, "3_4"])
        self.assertEqual(code, 5)
        self.assertNotIn("rendered:", out)
        self.assertIn("error: flat: 0% of this cell rises above the ground.", err)
        self.assertIn(f"rutor release {NEW_SHEET} 3_4", err)
        self.assertIn("--flat-ok", err)
        self.assertTrue((Path(self.tmp.name) / "work" / NEW_SHEET / "3_4.src.png").exists())

    def test_a_person_who_has_looked_can_have_a_flat_cell_drawn(self) -> None:
        code, out, err = self._render_measuring("0.000", ["render", NEW_SHEET, "3_4", "--flat-ok"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"rendered: {NEW_SHEET} 3_4", out)

    def test_a_cell_with_buildings_that_stand_up_is_rendered(self) -> None:
        code, out, err = self._render_measuring("0.380", ["render", NEW_SHEET, "3_4"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"rendered: {NEW_SHEET} 3_4", out)

    def test_a_hand_made_sheet_is_not_asked_about_flat_cells(self) -> None:
        # Open water in Stockholm is flat and is meant to be drawn: its cells were picked by hand.
        code, out, err = self._render_measuring("0.000", ["render", NEW_SHEET, "3_4"], made="hand")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"rendered: {NEW_SHEET} 3_4", out)

    def test_a_sheet_with_no_source_is_not_claimed(self) -> None:
        self.put(_new_sheet(kind=None))
        code, out, err = self.run_cmd(["claim", NEW_SHEET, "3_4"])
        self.assertEqual((code, out), (3, ""))
        self.assertIn(f"no source decided for sheet {NEW_SHEET} yet", err)
        self.assertEqual(self.claims(), [])
        code, _out, err = self.run_cmd(["claim", NEW_SHEET, "--next"])
        self.assertEqual(code, 3)
        self.assertEqual(self.claims(), [])

    def test_a_sheet_with_no_source_is_not_rendered(self) -> None:
        self.put(_new_sheet(kind=None))
        with patch.object(commands, "render_cell", side_effect=AssertionError("a renderer was asked")):
            code, _out, err = self.run_cmd(["render", NEW_SHEET, "3_4"])
        self.assertEqual(code, 3)
        self.assertIn("no source decided", err)

    def test_stockholm_is_google_also_when_the_service_does_not_say(self) -> None:
        self.put(_new_sheet(id="stockholm-norr", kind=None))
        with patch.object(google, "not_ready", return_value="CESIUM_ION_TOKEN is not set."):
            code, _out, err = self.run_cmd(["claim", "stockholm-norr", "3_4"])
        self.assertEqual(code, 3)
        self.assertIn("this machine cannot render stockholm-norr yet: CESIUM_ION_TOKEN is not set", err)
        self.assertEqual(self.claims(), [])

    def test_a_google_sheet_with_an_unknown_id_is_not_claimed_on_a_machine_that_cannot_render_it(self) -> None:
        self.put(_new_sheet())
        with patch.object(google, "not_ready", return_value="the Google renderer is not installed."):
            code, _out, err = self.run_cmd(["claim", NEW_SHEET, "3_4"])
        self.assertEqual(code, 3)
        self.assertIn(f"this machine cannot render {NEW_SHEET} yet: the Google renderer is not installed", err)
        self.assertEqual(self.claims(), [])
        with patch.object(google, "not_ready", return_value=None):
            code, out, err = self.run_cmd(["claim", NEW_SHEET, "3_4"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"claimed: {NEW_SHEET} 3_4", out)
        self.assertEqual(len(self.claims()), 1)

    def test_a_claim_says_the_source_it_will_draw_from(self) -> None:
        # The service refuses a claim on a sheet of the tiling that names no source: that is
        # how it tells this rutor from one installed before, which could not draw the cell.
        self.put(_new_sheet())
        with patch.object(google, "not_ready", return_value=None):
            code, _out, err = self.run_cmd(["claim", NEW_SHEET, "3_4"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(bytes(self.claims()[-1]["body"])), {"sheet": NEW_SHEET, "col": 3, "row": 4, "kind": "google"})
        # A sheet whose row says no source gets the one the code knows for it.
        self.put(_new_sheet(id="goteborg-norr", kind=None, made="hand"))
        code, _out, err = self.run_cmd(["claim", "goteborg-norr", "3_4"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(bytes(self.claims()[-1]["body"]))["kind"], "open")

    def test_the_stand_in_refuses_a_claim_that_names_no_source_as_the_service_does(self) -> None:
        from rutor import api

        self.put(_new_sheet())
        with self.assertRaises(RutorError) as caught:
            api.post("/v1/claim", {"sheet": NEW_SHEET, "col": 3, "row": 4}, "test-token")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("refused: update_needed", caught.exception.message)
        cells = self.svc.state["cells"]
        assert isinstance(cells, dict)
        self.assertEqual(cells[f"{NEW_SHEET}/3_4"]["state"], "fri")

    def test_a_sheet_that_has_no_row_yet_is_not_rendered(self) -> None:
        # Before the first claim the service answers a sheet with a stand-in height. A picture
        # drawn from it would lie some hundred pixels off. The claim makes the row.
        self.put(_new_sheet(exists=False, ground_height=0, y_meters=6016))
        with patch.object(commands, "render_cell", side_effect=AssertionError("a renderer was asked")):
            code, out, err = self.run_cmd(["render", NEW_SHEET, "3_4"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("refused: not_claimed", err)
        self.assertIn(f"rutor claim {NEW_SHEET} 3_4", err)

    def test_an_open_sheet_asks_nothing_of_google(self) -> None:
        self.put(_new_sheet(id="any-town", kind="open"))
        with patch.object(google, "not_ready", side_effect=AssertionError("google was asked")):
            code, _out, err = self.run_cmd(["claim", "any-town", "3_4"])
        self.assertEqual((code, err), (0, ""))

    def test_preflight_asks_the_service_which_source_an_unknown_sheet_has(self) -> None:
        self.put(_new_sheet())
        machine_ok = (
            patch("rutor.commands._python_missing", return_value=None),
            patch("rutor.commands._disk_missing", return_value=None),
            patch.object(google, "node_missing", return_value=None),
            patch.object(google, "renderer_missing", return_value=None),
            patch.object(google, "token_unusable", return_value="CESIUM_ION_TOKEN is not set"),
        )
        for p in machine_ok:
            p.start()
            self.addCleanup(p.stop)
        code, out, err = self.run_cmd(["preflight", "--machine", "--sheet", NEW_SHEET])
        self.assertEqual((code, err), (3, ""))
        self.assertEqual(
            out.splitlines()[3:],
            ["check: node ok", "check: google renderer ok", "check: cesium token missing CESIUM_ION_TOKEN is not set"],
        )
        # A sheet nobody knows the source of is said, not passed over.
        code, out, _err = self.run_cmd(["preflight", "--machine", "--sheet", "no-such-sheet"])
        self.assertEqual(code, 3)
        self.assertIn("check: sheet missing ", out)


class ClaimByNameTests(_ServiceCase):
    """rutor claim --plats: a town or a landmark by name, looked up in OpenStreetMap."""

    def setUp(self) -> None:
        super().setUp()
        ready = patch.object(google, "not_ready", return_value=None)
        ready.start()
        self.addCleanup(ready.stop)
        row = _new_sheet()
        self.svc.state["sheets"] = {str(row["id"]): row}
        self.svc.state["cells"] = {}
        self.svc.state["place"] = {"sheet": {**row, "exists": False}, "col": 47, "row": 16, "state": "fri"}
        self.looked: list[str] = []

    def lookup(self, answer: object):  # type: ignore[no-untyped-def]
        def fake(name: str) -> object:
            self.looked.append(name)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return patch("rutor.commands._nominatim", side_effect=fake)

    def test_a_name_is_looked_up_and_claimed_as_the_cell_there(self) -> None:
        hit = [{"lat": "55.8392", "lon": "13.3034", "display_name": "Eslöv, Eslövs kommun, Skåne län, Sverige"}]
        with self.lookup(hit):
            code, out, err = self.run_cmd(["claim", "--plats", "Eslöv"])
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "found: Eslöv, Eslövs kommun, Skåne län, Sverige")
        self.assertEqual(lines[1], f"place: {NEW_SHEET} 47_16")
        self.assertTrue(lines[2].startswith(f"claimed: {NEW_SHEET} 47_16"))
        self.assertEqual(self.looked, ["Eslöv"])
        self.assertEqual([str(r.get("path")) for r in self.svc.requests if str(r.get("path")).startswith("/v1/place")], ["/v1/place?lat=55.8392&lon=13.3034"])

    def test_a_name_nobody_knows_takes_nothing(self) -> None:
        with self.lookup([]):
            code, out, err = self.run_cmd(["claim", "--plats", "Xyzzyby"])
        self.assertEqual(code, 1)
        self.assertIn("Xyzzyby", err)
        self.assertNotIn("claimed:", out)
        self.assertEqual([r for r in self.svc.requests if r.get("path") == "/v1/claim"], [])

    def test_no_answer_from_openstreetmap_is_the_network(self) -> None:
        with self.lookup(network("could not reach OpenStreetMap")):
            code, _out, _err = self.run_cmd(["claim", "--plats", "Eslöv"])
        self.assertEqual(code, 4)

    def test_a_name_and_a_point_or_a_cell_are_not_both_given(self) -> None:
        for argv in (["claim", "--plats", "Eslöv", "--at", "56.0,12.6"], ["claim", NEW_SHEET, "47_16", "--plats", "Eslöv"]):
            with self.subTest(argv=argv), self.lookup([]):
                code, _out, _err = self.run_cmd(argv)
                self.assertEqual(code, 1)
                self.assertEqual(self.looked, [])

    def test_the_lookup_asks_sweden_only_and_says_who_asks_without_a_person(self) -> None:
        seen: dict[str, object] = {}

        class Answer:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_a: object) -> None:
                return None

            def read(self, _n: int = -1) -> bytes:
                return b'[{"lat": "55.8392", "lon": "13.3034", "display_name": "Esl\\u00f6v"}]'

        def opener(request, timeout):  # type: ignore[no-untyped-def]
            seen["url"] = request.full_url
            seen["agent"] = request.get_header("User-agent")
            seen["timeout"] = timeout
            return Answer()

        with patch("rutor.commands._open_url", side_effect=opener):
            from rutor import commands

            hits = commands._nominatim("Stortorget Helsingborg")
        self.assertEqual(hits, [{"lat": "55.8392", "lon": "13.3034", "display_name": "Eslöv"}])
        url = str(seen["url"])
        self.assertTrue(url.startswith("https://nominatim.openstreetmap.org/search?"))
        self.assertIn("countrycodes=se", url)
        self.assertIn("limit=1", url)
        self.assertIn("q=Stortorget+Helsingborg", url)
        self.assertRegex(str(seen["agent"]), r"^rutor/\S+ \(\+https://github\.com/opensverige/karta\.opensverige-3d\)$")
        self.assertNotIn("@", str(seen["agent"]))


class ClaimAtTests(_ServiceCase):
    """rutor claim --at: a point on the earth, and the service says which sheet and cell it is."""

    def setUp(self) -> None:
        super().setUp()
        ready = patch.object(google, "not_ready", return_value=None)
        ready.start()
        self.addCleanup(ready.stop)

    def claims(self) -> list[dict[str, object]]:
        return [r for r in self.svc.requests if r.get("method") == "POST" and r.get("path") == "/v1/claim"]

    def asked(self) -> list[str]:
        return [str(r.get("path")) for r in self.svc.requests if str(r.get("path")).startswith("/v1/place")]

    def put_place(self, col: int = 47, row: int = 16, state: str = "fri", **sheet: object) -> None:
        row_of_sheet = _new_sheet(**sheet)
        self.svc.state["sheets"] = {str(row_of_sheet["id"]): row_of_sheet}
        self.svc.state["cells"] = {}
        self.svc.state["place"] = {"sheet": {**row_of_sheet, "exists": False}, "col": col, "row": row, "state": state}

    def test_a_point_is_claimed_as_the_cell_the_service_says_it_is(self) -> None:
        self.put_place()
        code, out, err = self.run_cmd(["claim", "--at", "56.0465,12.6945"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out.splitlines(),
            [
                f"place: {NEW_SHEET} 47_16",
                f"claimed: {NEW_SHEET} 47_16 expires 2026-11-19T12:00:00Z",
                f"url: https://karta.opensverige.se/pixelbygget#{NEW_SHEET}/47_16",
            ],
        )
        self.assertEqual(self.asked(), ["/v1/place?lat=56.0465&lon=12.6945"])
        # The claim says the source the cell will be drawn from: the service asks it on a new sheet.
        self.assertEqual(
            [json.loads(bytes(r["body"])) for r in self.claims()],
            [{"sheet": NEW_SHEET, "col": 47, "row": 16, "kind": "google"}],
        )

    def test_a_space_after_the_comma_and_a_southern_or_western_point_are_read(self) -> None:
        self.put_place()
        code, _out, err = self.run_cmd(["claim", "--at", "56.0465, 12.6945"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.asked(), ["/v1/place?lat=56.0465&lon=12.6945"])
        code, _out, _err = self.run_cmd(["claim", "--at=-33.9,-70.6"])
        self.assertEqual(self.asked()[-1], "/v1/place?lat=-33.9&lon=-70.6")

    def test_a_malformed_place_exits_1_and_asks_nobody(self) -> None:
        self.put_place()
        for text in ("56.0465", "56.0465;12.6945", "abc,def", "56.0465,12.6945,3", "1e2,12", ",", "", "91,12", "56,181", "56.,12", "0x38,12", "56.0465 12.6945"):
            with self.subTest(at=text):
                code, out, err = self.run_cmd(["claim", f"--at={text}"])
                self.assertEqual((code, out), (1, ""))
                self.assertIn("--at", err)
        self.assertEqual(self.svc.requests, [])

    def test_a_place_and_a_sheet_together_exit_1(self) -> None:
        self.put_place()
        for argv in (
            ["claim", NEW_SHEET, "--at", "56.0465,12.6945"],
            ["claim", NEW_SHEET, "47_16", "--at", "56.0465,12.6945"],
            ["claim", "--at", "56.0465,12.6945", "--next"],
            ["claim"],
            ["claim", NEW_SHEET],
        ):
            with self.subTest(argv=argv):
                code, out, _err = self.run_cmd(argv)
                self.assertEqual((code, out), (1, ""))
        self.assertEqual(self.svc.requests, [])

    def test_a_point_outside_sweden_is_refused_and_nothing_is_claimed(self) -> None:
        code, out, err = self.run_cmd(["claim", "--at", "59.9139,10.7522"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("refused: outside", err)
        self.assertEqual(self.claims(), [])

    def test_a_place_this_machine_cannot_render_is_not_claimed(self) -> None:
        self.put_place()
        with patch.object(google, "not_ready", return_value="CESIUM_ION_TOKEN is not set."):
            code, out, err = self.run_cmd(["claim", "--at", "56.0465,12.6945"])
        self.assertEqual(code, 3)
        # The place is said first, so the person knows which sheet the message is about.
        self.assertEqual(out, f"place: {NEW_SHEET} 47_16\n")
        self.assertIn(f"this machine cannot render {NEW_SHEET} yet: CESIUM_ION_TOKEN is not set", err)
        self.assertEqual(self.claims(), [])

    def test_a_place_that_is_taken_says_so_as_any_claim_does(self) -> None:
        self.put_place(state="last")
        cells = self.svc.state["cells"]
        assert isinstance(cells, dict)
        cells[f"{NEW_SHEET}/47_16"] = _cell(47, 16, "last", "ebba")
        code, out, err = self.run_cmd(["claim", "--at", "56.0465,12.6945"])
        self.assertEqual(code, 2)
        self.assertIn("refused: taken:", err)

    def test_an_answer_that_names_no_cell_is_not_claimed(self) -> None:
        for broken in (
            {"col": 47, "row": 16, "state": "fri"},
            {"sheet": _new_sheet(id="Not A Sheet"), "col": 47, "row": 16, "state": "fri"},
            {"sheet": _new_sheet(), "col": "47", "row": 16, "state": "fri"},
            {"sheet": _new_sheet(), "col": 47, "row": -1, "state": "fri"},
            {"sheet": _new_sheet(), "col": True, "row": 16, "state": "fri"},
        ):
            with self.subTest(broken=broken):
                self.svc.state["place"] = broken
                code, out, _err = self.run_cmd(["claim", "--at", "56.0465,12.6945"])
                self.assertEqual((code, out), (4, ""))
        self.assertEqual(self.claims(), [])

    def test_a_place_needs_a_login_to_be_claimed_but_not_to_be_found(self) -> None:
        self.put_place()
        os.remove(os.path.join(self.tmp.name, "token"))
        code, out, err = self.run_cmd(["claim", "--at", "56.0465,12.6945"])
        self.assertEqual(code, 3)
        self.assertEqual(out, f"place: {NEW_SHEET} 47_16\n")
        self.assertIn("not logged in: run rutor login", err)


class NextOnTheTilingTests(_ServiceCase):
    """--next on a sheet of the tiling: its free cells have no row, so there is no list to pick from."""

    SHEET: dict[str, object] = {"id": NEW_SHEET, "cols": 61, "rows": 47, "made": "grid"}

    def test_it_picks_a_free_neighbour_of_a_cell_that_is_drawn(self) -> None:
        cells: list[object] = [[30, 23, "live", "ebba", ""]]
        self.assertEqual(commands.pick_next(self.SHEET, cells), (30, 22))

    def test_a_neighbour_that_is_taken_is_passed_over(self) -> None:
        cells: list[object] = [[30, 23, "live", "ebba", ""], [30, 22, "last", "klang", ""], [29, 23, "vantar", "klang", ""]]
        self.assertEqual(commands.pick_next(self.SHEET, cells), (31, 23))

    def test_a_cell_that_was_given_back_is_free(self) -> None:
        cells: list[object] = [[30, 23, "live", "ebba", ""], [30, 22, "fri", None, ""]]
        self.assertEqual(commands.pick_next(self.SHEET, cells), (30, 22))

    def test_it_stays_inside_the_sheet(self) -> None:
        cells: list[object] = [[0, 0, "live", "ebba", ""]]
        self.assertEqual(commands.pick_next(self.SHEET, cells), (1, 1))
        cells = [[60, 46, "last", "ebba", ""]]
        self.assertEqual(commands.pick_next(self.SHEET, cells), (59, 45))

    def test_beside_a_cell_on_the_map_comes_before_beside_a_locked_one(self) -> None:
        cells: list[object] = [[30, 23, "last", "ebba", ""], [10, 10, "live", "ebba", ""]]
        col, row = commands.pick_next(self.SHEET, cells)
        self.assertLessEqual(max(abs(col - 10), abs(row - 10)), 1)

    def test_a_sheet_nobody_has_drawn_on_is_refused_with_what_to_do(self) -> None:
        with self.assertRaises(RutorError) as caught:
            commands.pick_next(self.SHEET, [])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("refused: no_free_cell", caught.exception.message)
        self.assertIn("name a cell, or use --at", caught.exception.message)

    def test_a_lone_cell_that_was_given_back_counts_as_drawn(self) -> None:
        self.assertEqual(commands.pick_next(self.SHEET, [[12, 40, "fri", None, ""]]), (13, 39))
        self.assertEqual(commands.pick_next(self.SHEET, [[30, 23, "fri", None, ""]]), (30, 23))

    def test_a_hand_made_sheet_picks_as_it_always_did(self) -> None:
        hand = {"id": "goteborg-norr", "cols": 5, "rows": 5}
        cells: list[object] = [[2, 2, "fri", None, ""], [0, 0, "live", "g", ""], [0, 1, "fri", None, ""]]
        self.assertEqual(commands.pick_next(hand, cells), (0, 1))
        with self.assertRaises(RutorError) as caught:
            commands.pick_next(hand, [[0, 0, "live", "g", ""]])
        self.assertNotIn("--at", caught.exception.message)

    def test_the_command_claims_the_neighbour(self) -> None:
        patcher = patch.object(google, "not_ready", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.svc.state["sheets"] = {NEW_SHEET: _new_sheet()}
        self.svc.state["cells"] = {f"{NEW_SHEET}/30_23": _cell(30, 23, "live", "ebba")}
        code, out, err = self.run_cmd(["claim", NEW_SHEET, "--next"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"claimed: {NEW_SHEET} 30_22 expires", out)
