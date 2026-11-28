"""The app page: building a cell from a page on this machine, without a terminal."""

from __future__ import annotations

import contextlib
import getpass
import http.client
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import zoneinfo
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import rutor
from rutor import app, app_jobs, commands, config, login, pipeline, se3_price
from rutor.__main__ import main
from rutor.errors import RutorError, network
from rutor.localpage import Refusal
from rutor.renderer import google
from tests.fake_service import FakeService

# Shaped like a Cesium ion token, which has two dots. It opens nothing.
CESIUM = "a.b.c"


class _OwnHome(unittest.TestCase):
    """A home folder of the test's own, and no Cesium token in the environment."""

    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ("PIXELBYGGET_HOME", "CESIUM_ION_TOKEN")}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PIXELBYGGET_HOME"] = self.tmp.name
        os.environ.pop("CESIUM_ION_TOKEN", None)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()


class CesiumTokenTests(_OwnHome):
    def test_a_written_token_is_read_back(self) -> None:
        self.assertIsNone(config.read_cesium_token())
        config.write_cesium_token(CESIUM)
        self.assertEqual(config.cesium_token_path(), Path(self.tmp.name) / "cesium-token")
        self.assertEqual(config.read_cesium_token(), CESIUM)
        # A pasted token often ends in a line break. It is not part of the token.
        config.write_cesium_token(f"  {CESIUM}\n")
        self.assertEqual(config.read_cesium_token(), CESIUM)
        self.assertEqual(config.cesium_token_path().read_bytes(), CESIUM.encode())

    @unittest.skipIf(os.name == "nt", "Windows does not report POSIX file modes")
    def test_the_token_file_is_the_owners_alone(self) -> None:
        config.write_cesium_token(CESIUM)
        self.assertEqual(stat.S_IMODE(config.cesium_token_path().stat().st_mode), 0o600)
        # A file that was there already, and readable by others, is closed too.
        os.chmod(config.cesium_token_path(), 0o644)
        config.write_cesium_token(CESIUM)
        self.assertEqual(stat.S_IMODE(config.cesium_token_path().stat().st_mode), 0o600)

    def test_the_token_pasted_into_the_page_is_the_one_handed_on(self) -> None:
        self.assertEqual(config.cesium_env(), {})
        # The person's own variable, and nothing pasted: the child inherits it, nothing is added.
        os.environ["CESIUM_ION_TOKEN"] = "x.y.z"
        self.assertEqual(config.cesium_env(), {})
        # A token pasted into the page is the page's answer to "which token", so it wins. A
        # variable that has gone stale could otherwise never be put right from the page.
        config.write_cesium_token(CESIUM)
        self.assertEqual(config.cesium_env(), {"CESIUM_ION_TOKEN": CESIUM})
        os.environ.pop("CESIUM_ION_TOKEN")
        self.assertEqual(config.cesium_env(), {"CESIUM_ION_TOKEN": CESIUM})

    def test_handing_the_token_on_does_not_set_it_here(self) -> None:
        config.write_cesium_token(CESIUM)
        config.cesium_env()
        self.assertNotIn("CESIUM_ION_TOKEN", os.environ)

    def test_an_empty_token_is_not_written(self) -> None:
        for empty in ("", "   ", "\n\t"):
            with self.assertRaises(RutorError) as caught:
                config.write_cesium_token(empty)
            self.assertEqual(caught.exception.code, 1)
        self.assertFalse(config.cesium_token_path().exists())
        # And one that is there stays as it was.
        config.write_cesium_token(CESIUM)
        with self.assertRaises(RutorError):
            config.write_cesium_token(" ")
        self.assertEqual(config.read_cesium_token(), CESIUM)

    def test_the_variable_is_the_one_the_renderer_reads(self) -> None:
        self.assertEqual(config.CESIUM_TOKEN_ENV, google.TOKEN_ENV)


class ChecksTests(_OwnHome):
    def _machine(
        self,
        cards: list[tuple[str, int]],
        *,
        free: float = 257.4,
        cached: bool = False,
        platform: str = "linux",
        driver: str = "616.56",
        amd: list[tuple[str, int]] | None = None,
        kfd: bool = True,
    ) -> dict[str, object]:
        with (
            patch("rutor.app._platform", return_value=platform),
            patch("rutor.commands._nvidia_cards", return_value=cards),
            patch("rutor.commands._amd_cards", return_value=amd or []),
            patch("rutor.commands.kfd_usable", return_value=kfd),
            patch("rutor.commands._nvidia_driver", return_value=driver),
            patch("rutor.commands._free_gb", return_value=free),
            patch("rutor.commands._model_is_cached", return_value=cached),
        ):
            return app.machine()

    def test_a_mac_is_a_no_before_anything_else(self) -> None:
        with (
            patch("rutor.app._platform", return_value="darwin"),
            patch("rutor.commands._nvidia_cards", side_effect=AssertionError("the driver was asked")),
            patch("rutor.commands._free_gb", return_value=257.4),
            patch("rutor.commands._model_is_cached", return_value=False),
        ):
            found = app.machine()
        self.assertFalse(found["ok"])
        self.assertEqual(found["problem"], "mac")
        self.assertEqual(found["card"], "")
        self.assertEqual(found["card_gb"], 0)
        self.assertEqual(found["free_gb"], 257)
        self.assertEqual(found["needed_gb"], 25)

    def test_no_card_is_a_no(self) -> None:
        found = self._machine([], platform="win32")
        self.assertFalse(found["ok"])
        self.assertEqual(found["problem"], "no_card")
        self.assertEqual(found["card"], "")
        self.assertEqual(found["card_gb"], 0)

    def test_a_six_gb_card_is_named_with_its_size(self) -> None:
        found = self._machine([("GeForce GTX 1060", 6144)])
        self.assertFalse(found["ok"])
        self.assertEqual(found["problem"], "small_card")
        self.assertEqual(found["card"], "GeForce GTX 1060")
        self.assertEqual(found["card_gb"], 6)

    def test_a_twelve_gb_card_with_room_is_a_yes(self) -> None:
        found = self._machine([("GeForce RTX 5070", 12227)], free=257.4, cached=False)
        self.assertTrue(found["ok"])
        self.assertEqual(found["problem"], "")
        self.assertEqual(found["card_gb"], 12)
        self.assertEqual(found["free_gb"], 257)
        self.assertEqual(found["needed_gb"], 25)

    def test_an_amd_card_on_linux_is_a_yes_with_no_driver_number(self) -> None:
        # nvidia-smi would answer 616.56 here. An AMD card is not judged by NVIDIA's number.
        found = self._machine([], amd=[("AMD gfx1151", 29437)], driver="616.56")
        self.assertTrue(found["ok"])
        self.assertEqual((found["problem"], found["card"], found["card_gb"], found["driver"]), ("", "AMD gfx1151", 29, ""))
        small = self._machine([], amd=[("AMD gfx1030", 8176)])
        self.assertEqual((small["ok"], small["problem"], small["card_gb"]), (False, "small_card", 8))
        shut = self._machine([], amd=[("AMD gfx1151", 29437)], kfd=False)
        self.assertEqual((shut["ok"], shut["problem"], shut["card"]), (False, "no_rocm", "AMD gfx1151"))
        # An NVIDIA card is not asked about /dev/kfd.
        nvidia = self._machine([("GeForce RTX 5070", 12227)], kfd=False)
        self.assertEqual((nvidia["ok"], nvidia["problem"]), (True, ""))

    def test_the_largest_card_is_the_one_that_counts(self) -> None:
        found = self._machine([("GeForce GT 710", 2048), ("GeForce RTX 5070", 12227)])
        self.assertTrue(found["ok"])
        self.assertEqual(found["card"], "GeForce RTX 5070")

    def test_too_little_disk_is_a_no(self) -> None:
        tight = self._machine([("GeForce RTX 5070", 12227)], free=9.0, cached=False)
        self.assertEqual(tight["problem"], "disk")
        self.assertEqual(tight["needed_gb"], 25)
        self.assertFalse(tight["ok"])
        cached = self._machine([("GeForce RTX 5070", 12227)], free=9.0, cached=True)
        self.assertTrue(cached["ok"])
        self.assertEqual(cached["problem"], "")
        self.assertEqual(cached["needed_gb"], 2)

    def test_a_card_name_from_the_driver_is_one_short_line(self) -> None:
        found = self._machine([("<b>x</b>\n" * 50, 12227)])
        card = found["card"]
        self.assertIsInstance(card, str)
        self.assertLessEqual(len(str(card)), 60)
        self.assertNotIn("\n", str(card))

    def test_memory_is_a_number_or_nothing(self) -> None:
        ram = app.machine()["ram_gb"]
        if ram is None:
            return
        self.assertIsInstance(ram, int)
        self.assertNotIsInstance(ram, bool)
        self.assertGreater(ram, 0)

    def test_little_memory_is_a_warning_not_a_no(self) -> None:
        # Measured on the first cell built from the page: the redraw held 12.6 GiB of the
        # machine's memory at its peak. Below that plus 4 GiB for everything else, the page
        # warns. It does not refuse: nobody has tried a 16 GB machine yet.
        self.assertEqual((app.REDRAW_PEAK_GB, app.MEMORY_MARGIN_GB), (13, 4))
        for ram, warning in ((8, "memory"), (16, "memory"), (17, ""), (32, ""), (None, "")):
            with self.subTest(ram=ram), patch("rutor.app._ram_gb", return_value=ram):
                found = self._machine([("GeForce RTX 5070", 12227)])
                self.assertEqual(found["warning"], warning)
                self.assertTrue(found["ok"])
                self.assertEqual(found["problem"], "")

    def test_a_machine_that_cannot_draw_gets_its_no_and_no_warning_about_memory(self) -> None:
        with patch("rutor.app._ram_gb", return_value=8):
            found = self._machine([("GeForce GTX 1060", 6144)])
        self.assertEqual((found["ok"], found["problem"], found["warning"]), (False, "small_card", ""))

    def test_a_driver_older_than_the_wheels_need_is_a_no(self) -> None:
        # NVIDIA's own number for Windows, from the CUDA 12.8 release notes.
        self.assertEqual(commands.DRIVER_MIN, (528, 33))
        major, minor = commands.DRIVER_MIN
        for old in (f"{major}.{minor - 1}", f"{major - 1}.99", "472.12"):
            with self.subTest(driver=old):
                found = self._machine([("GeForce RTX 3060", 12288)], platform="win32", driver=old)
                self.assertEqual((found["ok"], found["problem"], found["driver"]), (False, "old_driver", old))
                # The card itself is fine, and it is still named.
                self.assertEqual(found["card"], "GeForce RTX 3060")

    def test_the_driver_that_works_today_is_a_yes(self) -> None:
        for good in ("616.56", "528.33", "528.34", "1000.1"):
            with self.subTest(driver=good):
                found = self._machine([("GeForce RTX 5070", 12227)], platform="win32", driver=good)
                self.assertEqual((found["ok"], found["problem"], found["driver"]), (True, "", good))

    def test_a_driver_that_cannot_be_read_does_not_block(self) -> None:
        found = self._machine([("GeForce RTX 5070", 12227)], platform="win32", driver="")
        self.assertEqual((found["ok"], found["problem"], found["driver"]), (True, "", ""))
        # Neither does one that is not a number: a no is said only when it is certain.
        for odd in ("N/A", "[Not Supported]", "616", "x.y"):
            with self.subTest(driver=odd):
                found = self._machine([("GeForce RTX 5070", 12227)], platform="win32", driver=odd)
                self.assertTrue(found["ok"])

    def test_a_small_card_is_said_before_its_driver(self) -> None:
        found = self._machine([("GeForce GTX 1060", 6144)], platform="win32", driver="472.12")
        self.assertEqual(found["problem"], "small_card")

    def test_the_number_is_for_windows_and_judges_no_other_system(self) -> None:
        # Linux drivers have their own numbering and their own minimum. The app is not
        # installed there, so nothing is refused by a number that is not about it.
        found = self._machine([("GeForce RTX 3060", 12288)], platform="linux", driver="525.60.13")
        self.assertEqual((found["ok"], found["problem"], found["driver"]), (True, "", "525.60.13"))

    def test_a_driver_version_from_outside_is_one_short_line(self) -> None:
        found = self._machine([("GeForce RTX 5070", 12227)], platform="win32", driver="<b>9</b>\n" * 40)
        self.assertLessEqual(len(str(found["driver"])), 20)
        self.assertNotIn("\n", str(found["driver"]))

    def test_the_driver_is_asked_of_nvidia_smi(self) -> None:
        done = Mock(stdout="616.56\n616.56\n", returncode=0)
        with patch("shutil.which", return_value="/x/nvidia-smi"), patch("subprocess.run", return_value=done) as run:
            self.assertEqual(commands._nvidia_driver(), "616.56")
        self.assertEqual(run.call_args.args[0], ["/x/nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"])
        with patch("shutil.which", return_value=None):
            self.assertEqual(commands._nvidia_driver(), "")
        with patch("shutil.which", return_value="/x/nvidia-smi"), patch("subprocess.run", side_effect=OSError("gone")):
            self.assertEqual(commands._nvidia_driver(), "")

    def test_missing_tools_are_named(self) -> None:
        with (
            patch("rutor.app._model_stack_missing", return_value=["torch"]),
            patch("rutor.commands._render_missing", return_value=None),
            patch("rutor.commands._model_is_cached", return_value=False),
            patch("rutor.renderer.google.node_missing", return_value="no"),
            patch("rutor.renderer.google.renderer_missing", return_value=None),
        ):
            found = app.tools("google")
        self.assertEqual(found, {"ready": False, "model_cached": False, "missing": ["torch", "node"]})

    def test_an_open_data_sheet_needs_no_node(self) -> None:
        with (
            patch("rutor.renderer.google.node_missing", side_effect=AssertionError("node was asked")),
            patch("rutor.renderer.google.renderer_missing", side_effect=AssertionError("renderer was asked")),
            patch("rutor.app._model_stack_missing", return_value=[]),
            patch("rutor.commands._render_missing", return_value=None),
            patch("rutor.commands._model_is_cached", return_value=True),
        ):
            found = app.tools("open")
        self.assertTrue(found["ready"])
        self.assertEqual(found["missing"], [])
        self.assertTrue(found["model_cached"])

    def test_the_page_does_not_import_the_model_stack_itself(self) -> None:
        # PyTorch in the page's own process would sit in memory beside the redraw for as long
        # as the page is open. It is asked about in a child that ends.
        with (
            patch("rutor.commands._torch_missing", side_effect=AssertionError("torch was imported here")),
            patch("rutor.commands._bitsandbytes_missing", side_effect=AssertionError("bitsandbytes was imported here")),
            patch("rutor.app._model_stack_missing", return_value=["bitsandbytes"]),
            patch("rutor.commands._render_missing", return_value=None),
            patch("rutor.commands._model_is_cached", return_value=True),
        ):
            self.assertEqual(app.tools("open")["missing"], ["bitsandbytes"])

    def test_the_model_stack_is_asked_about_in_a_child(self) -> None:
        had = {name for name in ("torch", "bitsandbytes") if name in sys.modules}
        found = app._model_stack_missing()
        self.assertLessEqual(set(found), {"torch", "bitsandbytes"})
        self.assertEqual({name for name in ("torch", "bitsandbytes") if name in sys.modules}, had)

    def test_a_child_that_cannot_answer_counts_as_missing(self) -> None:
        with patch("rutor.app.subprocess.run", side_effect=OSError("no python")):
            self.assertEqual(app._model_stack_missing(), ["torch", "bitsandbytes"])
        with patch("rutor.app.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "Traceback\n", "")):
            self.assertEqual(app._model_stack_missing(), ["torch", "bitsandbytes"])
        said = "torch ok\nbitsandbytes missing\n<b>noise</b>\n"
        with patch("rutor.app.subprocess.run", return_value=subprocess.CompletedProcess([], 0, said, "")):
            self.assertEqual(app._model_stack_missing(), ["bitsandbytes"])

    def test_cesium_answers_are_words_the_page_knows(self) -> None:
        def at(status: int, code: str = "") -> Mock:
            return Mock(return_value=(status, code))

        self.assertEqual(app.cesium("a.b.c", ask=at(200)), {"set": True, "ok": True, "problem": ""})
        self.assertEqual(
            app.cesium("a.b.c", ask=at(401, "INVALID_TOKEN")),
            {"set": True, "ok": False, "problem": "refused"},
        )
        self.assertEqual(
            app.cesium("a.b.ca.b.c", ask=at(401, "INVALID_TOKEN")),
            {"set": True, "ok": False, "problem": "twice"},
        )
        self.assertEqual(app.cesium("a.b.c", ask=at(404)), {"set": True, "ok": False, "problem": "no_access"})
        self.assertEqual(app.cesium("a.b.c", ask=at(403)), {"set": True, "ok": False, "problem": "no_access"})
        self.assertEqual(app.cesium("a.b.c", ask=at(500)), {"set": True, "ok": False, "problem": "unreachable"})

        down = Mock(side_effect=network("x"))
        self.assertEqual(app.cesium("a.b.c", ask=down), {"set": True, "ok": False, "problem": "unreachable"})

        broken = Mock(return_value=(200, ""))
        self.assertEqual(app.cesium("a.b c", ask=broken), {"set": True, "ok": False, "problem": "broken"})
        broken.assert_not_called()

        absent = Mock(return_value=(200, ""))
        self.assertEqual(app.cesium(ask=absent), {"set": False, "ok": False, "problem": "missing"})
        absent.assert_not_called()

    def test_cesium_judges_the_stored_token_when_none_is_given(self) -> None:
        config.write_cesium_token("a.b.c")
        asked: list[str] = []

        def ask(token: str) -> tuple[int, str]:
            asked.append(token)
            return 200, ""

        found = app.cesium(ask=ask)
        self.assertEqual(asked, ["a.b.c"])
        self.assertEqual(found, {"set": True, "ok": True, "problem": ""})
        # The pasted token is the one judged, also beside a variable: it is the one handed on.
        os.environ["CESIUM_ION_TOKEN"] = "x.y.z"
        asked.clear()
        app.cesium(ask=ask)
        self.assertEqual(asked, ["a.b.c"])
        config.cesium_token_path().unlink()
        asked.clear()
        app.cesium(ask=ask)
        self.assertEqual(asked, ["x.y.z"])

    def test_a_token_with_a_letter_no_header_can_carry_is_broken(self) -> None:
        # A token is plain ASCII. A pasted ellipsis or a letter with an accent cannot be sent.
        quiet = Mock(side_effect=AssertionError("asked"))
        for pasted in ("a.b.c\u2026", "a.\u00e9.c"):
            self.assertEqual(app.cesium(pasted, ask=quiet), {"set": True, "ok": False, "problem": "broken"})
        quiet.assert_not_called()

    def test_the_token_is_in_no_answer(self) -> None:
        secret = "a.b.c-secret"

        def shows(candidate: str, ask: Mock, problem: str) -> None:
            result = app.cesium(candidate, ask=ask)
            self.assertEqual(result["problem"], problem)
            self.assertNotIn(secret, json.dumps(result))

        shows(secret, Mock(return_value=(200, "")), "")
        shows(secret, Mock(return_value=(401, "INVALID_TOKEN")), "refused")
        shows(f"{secret}.{secret}", Mock(return_value=(401, "INVALID_TOKEN")), "twice")
        shows(secret, Mock(return_value=(404, "")), "no_access")
        shows(secret, Mock(return_value=(403, "")), "no_access")
        shows(secret, Mock(return_value=(500, "")), "unreachable")
        shows(secret, Mock(side_effect=network(secret)), "unreachable")
        quiet = Mock(side_effect=AssertionError("asked"))
        shows(f"{secret} {secret}", quiet, "broken")
        quiet.assert_not_called()

    def test_the_terms_are_the_ten_lines(self) -> None:
        lines = app.terms()
        self.assertEqual(len(lines), 10)
        self.assertTrue(lines[0].startswith("1. Building a cell is voluntary"))
        self.assertTrue(lines[9].startswith("10. "))
        self.assertEqual("\n".join(lines), login.TERMS)

    def test_a_price_that_cannot_be_read_is_unknown(self) -> None:
        def fetch(day: date) -> list[se3_price.Slot] | None:
            del day
            raise OSError("down")

        self.assertEqual(app.price(fetch=fetch), {"decision": "unknown", "after": ""})

    def test_a_high_price_says_when_it_is_low(self) -> None:
        now = datetime(2026, 11, 21, 12, 0, tzinfo=se3_price.STOCKHOLM)
        tomorrow = now.date() + timedelta(days=1)

        def quarters(day: date, sek_at: Callable[[datetime], float]) -> list[se3_price.Slot]:
            start = datetime(day.year, day.month, day.day, tzinfo=se3_price.STOCKHOLM)
            slots: list[se3_price.Slot] = []
            for quarter in range(24 * 4):
                begin = start + timedelta(minutes=15 * quarter)
                slots.append(se3_price.Slot(begin, begin + timedelta(minutes=15), sek_at(begin)))
            return slots

        def fetch_high(day: date) -> list[se3_price.Slot]:
            def sek_at(begin: datetime) -> float:
                if day == tomorrow and begin.hour == 3:
                    return 0.02
                return 1.20

            return quarters(day, sek_at)

        waited = app.price(fetch=fetch_high, clock=lambda: now)
        cheap = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 3, tzinfo=se3_price.STOCKHOLM)
        self.assertEqual(waited, {"decision": "WAIT", "after": cheap.isoformat()})

        def fetch_flat(day: date) -> list[se3_price.Slot]:
            return quarters(day, lambda _begin: 0.40)

        self.assertEqual(app.price(fetch=fetch_flat, clock=lambda: now), {"decision": "RUN", "after": ""})

    def test_an_old_pipeline_is_not_current(self) -> None:
        old = app.version({"pipeline": "2026.12-a"})
        self.assertFalse(old["current"])
        self.assertEqual(old["wanted"], "2026.12-a")
        self.assertEqual(old["mine"], pipeline.PIPELINE)
        self.assertTrue(app.version({"pipeline": pipeline.PIPELINE})["current"])
        blank = app.version({})
        self.assertTrue(blank["current"])
        self.assertEqual(blank["wanted"], "")


# A child Python in which the time zone database is missing, as on a Windows without tzdata.
_NO_TZ_DATABASE = (
    "import zoneinfo\n"
    "def _missing(key):\n"
    "    raise zoneinfo.ZoneInfoNotFoundError('No time zone found with key ' + key)\n"
    "zoneinfo.ZoneInfo = _missing\n"
)


def _child(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False)


class WindowsWithoutTimeZonesTests(unittest.TestCase):
    """Windows has no time zone database of its own. Found on the first real machine."""

    def test_every_command_starts_without_the_time_zone_database(self) -> None:
        done = _child(_NO_TZ_DATABASE + "from rutor.__main__ import main\nraise SystemExit(main(['--version']))\n")
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertTrue(done.stdout.startswith("rutor "), done.stdout)

    def test_loading_the_page_does_not_load_the_price_check(self) -> None:
        # The price is advice. Nothing about it may decide whether rutor starts.
        done = _child("import sys\nimport rutor.app\nprint('rutor.se3_price' in sys.modules)\n")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "False"), done.stderr)

    def test_the_price_check_knows_swedish_time_by_its_rule(self) -> None:
        done = _child(
            _NO_TZ_DATABASE
            + "from datetime import datetime, timezone\n"
            + "from rutor import se3_price\n"
            + "for month in (1, 7):\n"
            + "    print(datetime(2026, month, 15, 12, tzinfo=timezone.utc).astimezone(se3_price.STOCKHOLM).isoformat())\n"
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split(), ["2026-01-15T13:00:00+01:00", "2026-07-15T14:00:00+02:00"])

    def test_the_rule_agrees_with_the_database_hour_by_hour(self) -> None:
        try:
            real = zoneinfo.ZoneInfo("Europe/Stockholm")
        except zoneinfo.ZoneInfoNotFoundError:
            self.skipTest("this machine has no time zone database to compare with")
        rule = se3_price.SwedishTime()
        instant = datetime(2025, 12, 31, 22, 30, tzinfo=timezone.utc)
        end = datetime(2028, 1, 2, tzinfo=timezone.utc)
        while instant < end:
            by_rule = instant.astimezone(rule)
            by_database = instant.astimezone(real)
            self.assertEqual(by_rule.isoformat(), by_database.isoformat())
            self.assertEqual(by_rule.tzname(), by_database.tzname())
            # And the other way: a clock time on the wall, read back as an instant.
            wall = by_database.replace(tzinfo=None)
            again = wall.replace(tzinfo=rule, fold=by_database.fold)
            self.assertEqual(again.utcoffset(), by_database.utcoffset(), wall)
            instant += timedelta(minutes=30)

    def test_a_price_check_that_cannot_load_is_unknown(self) -> None:
        with patch.dict(sys.modules, {"rutor.se3_price": None}):
            self.assertEqual(app.price(), {"decision": "unknown", "after": ""})


class FakeRun:
    """A child that says what the test scripted, and ends when the test lets it."""

    def __init__(self, lines: list[str], code: int = 0, hold: bool = False, again: bool = False) -> None:
        self._first = list(lines)
        self._later: list[str] = []
        self._code = code
        self._hold = hold
        self._again = again
        self._lock = threading.Lock()
        self._released = threading.Event()
        self._ended = threading.Event()
        self.stopped = False
        # What a stopped child exits with: -15 from a signal, 1 from taskkill on Windows.
        self.stopped_code = -15

    def lines(self) -> Iterator[app_jobs.Line]:
        for text in self._first:
            yield app_jobs.split(text)
        if self._hold:
            # The page has to show what it has before the child says the rest, so the
            # test holds the child here until it has looked.
            while not self._released.is_set() and not self.stopped:
                time.sleep(0.01)
        with self._lock:
            later = list(self._later)
            self._later.clear()
        for text in later:
            yield app_jobs.split(text)
        if self._again and not self.stopped:
            # One line is not enough to judge: the child waits again, so the test can look
            # at what it said before it ends and let the page move on.
            self._released.clear()
            while not self._released.is_set() and not self.stopped:
                time.sleep(0.01)
        self._ended.set()

    def say(self, *texts: str) -> None:
        """More lines, read after release(), as a child that prints its last word at the end."""
        with self._lock:
            self._later.extend(texts)

    def release(self) -> None:
        self._released.set()

    def wait(self) -> int:
        # A stop ends the child wherever it is, so wait() must not hang on a reader.
        self._ended.wait(timeout=30.0)
        return self.stopped_code if self.stopped else self._code

    def stop(self) -> None:
        self.stopped = True
        self._released.set()
        self._ended.set()


class FakeStart:
    """Stands in for app_jobs.start_rutor: records every start and plays a script per command."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.runs: list[FakeRun] = []
        self._scripts: dict[str, list[tuple[list[str], int, bool, bool]]] = {}
        # Called with the argv as a child starts, on the thread that starts it: a test uses
        # it to make something else happen at exactly that moment.
        self.on_start: Callable[[list[str]], None] | None = None

    def script(
        self, command: str, lines: list[str], code: int = 0, hold: bool = False, again: bool = False
    ) -> None:
        self._scripts.setdefault(command, []).append((list(lines), code, hold, again))

    def __call__(self, argv: list[str], extra_env: Mapping[str, str]) -> FakeRun:
        self.calls.append((list(argv), dict(extra_env)))
        queued = self._scripts.get(argv[0]) if argv else None
        if queued:
            lines, code, hold, again = queued.pop(0)
        else:
            # No script left means a child with nothing to say that ends at once.
            lines, code, hold, again = [], 0, False, False
        run = FakeRun(lines, code=code, hold=hold, again=again)
        self.runs.append(run)
        if self.on_start is not None:
            self.on_start(list(argv))
        return run


def wait_until(what: Callable[[], bool], seconds: float = 5.0) -> None:
    """Wait for the page's own thread to get there. Nothing in these tests sleeps a fixed time."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if what():
            return
        time.sleep(0.01)
    if not what():
        raise AssertionError("the page did not get there in time")


class LoginTests(_OwnHome):
    def _set_env(self, name: str, value: str) -> None:
        saved = os.environ.get(name)
        os.environ[name] = value

        def restore() -> None:
            if saved is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = saved

        self.addCleanup(restore)

    def _builder(self, start: FakeStart | None = None) -> app.Builder:
        builder = app.Builder(start if start is not None else FakeStart())
        self.addCleanup(builder.shutdown)
        return builder

    def _me_calls(self, service: FakeService) -> int:
        return len([request for request in service.requests if request["path"] == "/v1/me"])

    def test_no_login_starts_before_the_terms_are_accepted(self) -> None:
        fake = FakeStart()
        builder = self._builder(fake)
        with self.assertRaises(Refusal) as caught:
            builder.start_login()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "terms")
        self.assertEqual(fake.calls, [])
        self.assertFalse(builder.terms_accepted())

    def test_the_code_and_address_are_shown_while_it_waits(self) -> None:
        fake = FakeStart()
        fake.script(
            "login",
            [
                "1. Building a cell is voluntary and unpaid. Nobody owes you anything for it, and you can stop at any time.",
                "code: ABCD-1234",
                f"url: {login.DEVICE_URL}",
                "waiting: a person opens the url and types the code.",
            ],
            hold=True,
        )
        builder = self._builder(fake)
        builder.accept_terms()
        builder.start_login()
        wait_until(lambda: builder.login_state()["url"] != "")
        state = builder.login_state()
        self.assertTrue(state["waiting"])
        self.assertEqual(state["code"], "ABCD-1234")
        self.assertEqual(state["url"], login.DEVICE_URL)
        self.assertEqual(state["login"], "")
        self.assertEqual(fake.calls, [(["login", "--accept-terms"], {})])

    def test_a_finished_login_names_the_person(self) -> None:
        fake = FakeStart()
        fake.script(
            "login",
            ["code: ABCD-1234", f"url: {login.DEVICE_URL}"],
            hold=True,
        )
        builder = self._builder(fake)
        builder.accept_terms()
        builder.start_login()
        run = fake.runs[0]
        wait_until(lambda: builder.login_state()["url"] != "")
        run.say("logged in: klang")
        run.release()
        wait_until(lambda: not builder.login_state()["waiting"])
        self.assertEqual(
            builder.login_state(),
            {"login": "klang", "code": "", "url": "", "waiting": False, "problem": ""},
        )
        self.assertTrue(builder.terms_accepted())

    def test_a_code_nobody_approved_is_said_so(self) -> None:
        # 4 is the CLI's network code and 2 a no from GitHub or the service; anything else
        # that is not a login is an expired code.
        for code, problem in ((4, "network"), (3, "expired"), (2, "denied")):
            with self.subTest(code=code):
                fake = FakeStart()
                fake.script("login", ["code: ABCD-1234", f"url: {login.DEVICE_URL}"], code=code)
                builder = self._builder(fake)
                builder.accept_terms()
                builder.start_login()
                wait_until(lambda builder=builder: not builder.login_state()["waiting"])
                state = builder.login_state()
                self.assertEqual(state["problem"], problem)
                self.assertFalse(state["waiting"])
                self.assertEqual(state["code"], "")
                self.assertEqual(state["url"], "")
                self.assertEqual(state["login"], "")

    def test_a_second_press_starts_no_second_login(self) -> None:
        fake = FakeStart()
        fake.script("login", ["code: ABCD-1234"], hold=True)
        builder = self._builder(fake)
        builder.accept_terms()
        builder.start_login()
        builder.start_login()
        self.assertEqual(len(fake.calls), 1)

    def test_a_new_login_can_start_after_one_ended(self) -> None:
        fake = FakeStart()
        fake.script("login", [], code=3)
        builder = self._builder(fake)
        builder.accept_terms()
        builder.start_login()
        wait_until(lambda: not builder.login_state()["waiting"])
        self.assertEqual(builder.login_state()["problem"], "expired")
        fake.script("login", [], hold=True)
        builder.start_login()
        self.assertEqual(len(fake.calls), 2)
        state = builder.login_state()
        self.assertTrue(state["waiting"])
        self.assertEqual(state["problem"], "")
        self.assertEqual(state["code"], "")

    def test_the_address_is_githubs_own_or_nothing(self) -> None:
        for wrong in ("https://evil.test/x", f"{login.DEVICE_URL}/x"):
            with self.subTest(wrong=wrong):
                fake = FakeStart()
                # The code comes second, so a visible code means the address was judged.
                fake.script("login", [f"url: {wrong}", "code: ABCD-1234"], hold=True)
                builder = self._builder(fake)
                builder.accept_terms()
                builder.start_login()
                wait_until(lambda builder=builder: builder.login_state()["code"] != "")
                self.assertEqual(builder.login_state()["url"], "")

    def test_a_code_or_name_of_another_shape_is_not_shown(self) -> None:
        fake = FakeStart()
        fake.script("login", ["code: <b>x</b>", "logged in: <script>"])
        builder = self._builder(fake)
        builder.accept_terms()
        builder.start_login()
        wait_until(lambda: not builder.login_state()["waiting"])
        state = builder.login_state()
        self.assertEqual(state["code"], "")
        self.assertEqual(state["login"], "")
        self.assertEqual(state["problem"], "expired")

    def test_a_stored_login_is_named_at_start(self) -> None:
        service = FakeService()
        self.addCleanup(service.stop)
        self._set_env("PIXELBYGGET_URL", service.start())
        config.write_token("test-token")
        builder = self._builder()
        self.assertEqual(builder.login_state()["login"], "klang")
        self.assertEqual(self._me_calls(service), 1)
        # The stored login is asked about once. Every later look is the page's own state.
        builder.login_state()
        self.assertEqual(self._me_calls(service), 1)

    def test_a_stored_login_the_service_refuses_is_nobody(self) -> None:
        service = FakeService()
        self.addCleanup(service.stop)
        self._set_env("PIXELBYGGET_URL", service.start())
        config.write_token("test-token")
        service.fail_next_with(401, b'{"error":"bad_token","message":"no"}', "application/json")
        builder = self._builder()
        self.assertEqual(
            builder.login_state(),
            {"login": "", "code": "", "url": "", "waiting": False, "problem": ""},
        )
        self.assertFalse(builder.terms_accepted())

    def test_the_login_is_in_no_answer(self) -> None:
        service = FakeService()
        self.addCleanup(service.stop)
        self._set_env("PIXELBYGGET_URL", service.start())
        config.write_token("test-token")
        fake = FakeStart()
        fake.script("login", ["code: ABCD-1234", f"url: {login.DEVICE_URL}"], hold=True)
        builder = self._builder(fake)
        builder.login_state()
        builder.accept_terms()
        builder.start_login()
        run = fake.runs[0]
        wait_until(lambda: builder.login_state()["url"] != "")
        run.say("logged in: klang")
        run.release()
        wait_until(lambda: not builder.login_state()["waiting"])
        self.assertNotIn("test-token", json.dumps(builder.login_state()))
        self.assertNotIn("test-token", repr(vars(builder)))

    def test_shutdown_stops_a_waiting_login(self) -> None:
        fake = FakeStart()
        fake.script("login", ["code: ABCD-1234", f"url: {login.DEVICE_URL}"], hold=True)
        builder = self._builder(fake)
        builder.accept_terms()
        builder.start_login()
        run = fake.runs[0]
        wait_until(lambda: builder.login_state()["url"] != "")
        builder.shutdown()
        self.assertTrue(run.stopped)
        self.assertFalse(builder.login_state()["waiting"])

    def test_a_login_that_has_ended_is_never_stopped(self) -> None:
        # Stopping signals the child's whole process group, and the number of a group that
        # has ended may belong to somebody else's program by now.
        fake = FakeStart()
        fake.script("login", ["logged in: klang"])
        builder = self._builder(fake)
        builder.accept_terms()
        builder.start_login()
        wait_until(lambda: builder.login_state()["login"] == "klang" and not builder.login_state()["waiting"])
        builder.shutdown()
        self.assertFalse(fake.runs[0].stopped)

    def test_a_stored_login_is_asked_the_terms_until_a_yes_to_todays_is_kept(self) -> None:
        # The terms can change after a login. A login made under older ones, or before the yes
        # was kept at all, is shown the terms again, and the yes needs no new login.
        service = FakeService()
        self.addCleanup(service.stop)
        self._set_env("PIXELBYGGET_URL", service.start())
        config.write_token("test-token")
        fake = FakeStart()
        builder = self._builder(fake)
        # Asked before anything else: the page draws the terms step from this.
        self.assertFalse(builder.terms_accepted())
        config.write_terms("2026-11-23", "2026-11-23T10:00:00Z")
        self.assertFalse(builder.terms_accepted())
        builder.accept_terms()
        self.assertTrue(builder.terms_accepted())
        self.assertEqual(config.read_terms(), login.TERMS_VERSION)
        self.assertEqual(fake.calls, [])
        self.assertEqual(builder.login_state()["login"], "klang")

    def test_no_login_starts_once_the_page_is_closing(self) -> None:
        fake = FakeStart()
        builder = self._builder(fake)
        builder.accept_terms()
        builder.shutdown()
        builder.start_login()
        self.assertEqual(fake.calls, [])
        self.assertFalse(builder.login_state()["waiting"])

    def test_a_login_that_starts_while_the_page_closes_is_stopped(self) -> None:
        builder: app.Builder

        class ClosesMeanwhile(FakeStart):
            def __call__(self, argv: list[str], extra_env: Mapping[str, str]) -> FakeRun:
                run = super().__call__(argv, extra_env)
                # The page closes between the press and the child being up.
                builder.shutdown()
                return run

        fake = ClosesMeanwhile()
        fake.script("login", ["code: ABCD-1234"], hold=True)
        builder = self._builder(fake)
        builder.accept_terms()
        builder.start_login()
        self.assertTrue(fake.runs[0].stopped)
        wait_until(lambda: not builder.login_state()["waiting"])

    def test_a_fault_while_reading_the_login_does_not_leave_it_waiting(self) -> None:
        class Breaks(FakeRun):
            def lines(self) -> Iterator[app_jobs.Line]:
                yield app_jobs.split("code: ABCD-1234")
                raise RuntimeError("secret-1234")

        class StartsBroken(FakeStart):
            def __call__(self, argv: list[str], extra_env: Mapping[str, str]) -> FakeRun:
                self.calls.append((list(argv), dict(extra_env)))
                run = Breaks([])
                run.stop()
                self.runs.append(run)
                return run

        seen = io.StringIO()
        builder = self._builder(StartsBroken())
        builder.accept_terms()
        with contextlib.redirect_stderr(seen):
            builder.start_login()
            wait_until(lambda: not builder.login_state()["waiting"])
        state = builder.login_state()
        self.assertEqual((state["problem"], state["code"]), ("expired", ""))
        # One line for the terminal: the kind of fault, never its message.
        self.assertEqual(seen.getvalue(), "app: RuntimeError while reading the login\n")


class JobTests(_OwnHome):
    """The one job: a cell claimed, drawn, checked and uploaded, and how it ends."""

    SHEET = "stockholm-norr"
    CELL = "74_20"

    def _builder(self, *, log_in: bool = True) -> tuple[app.Builder, FakeStart]:
        """A builder whose machine, tools and token are ready, with a fake clock."""
        fake = FakeStart()
        for name, answer in (
            ("rutor.app.machine", {"ok": True, "problem": ""}),
            ("rutor.app.tools", {"ready": True, "missing": []}),
            ("rutor.app.cesium", {"set": True, "ok": True, "problem": ""}),
        ):
            patcher = patch(name, return_value=answer)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = 0.0
        builder = app.Builder(fake, clock=lambda: self.now)
        self.addCleanup(builder.shutdown)
        if log_in:
            fake.script("login", ["logged in: klang"])
            builder.accept_terms()
            builder.start_login()
            wait_until(lambda: builder.login_state()["login"] == "klang")
        return builder, fake

    def _script_ok(self, fake: FakeStart, upload_url: str | None = None) -> None:
        """The four commands saying what the CLI says when a cell goes well."""
        sheet, cell = self.SHEET, self.CELL
        address = upload_url or f"{config.MAP_URL}#{sheet}/{cell}"
        fake.script(
            "claim",
            [f"claimed: {sheet} {cell} expires 2026-11-25T12:00", f"url: {config.MAP_URL}#{sheet}/{cell}"],
        )
        fake.script("render", [f"rendered: work/{sheet}/{cell}.src.png"])
        fake.script("redraw", ["encode window 1/4", "check: ok", f"redrawn: work/{sheet}/{cell}.png"])
        fake.script("upload", [f"uploaded: {sheet} {cell} state vantar", f"url: {address}"])

    def _started(self, fake: FakeStart) -> list[str]:
        return [argv[0] for argv, _env in fake.calls]

    def _state(self, builder: app.Builder) -> dict[str, object]:
        return builder.job_state()

    def test_the_four_commands_run_in_order_for_the_chosen_cell(self) -> None:
        builder, fake = self._builder()
        self._script_ok(fake)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "done")
        self.assertEqual(
            [argv for argv, _env in fake.calls][1:],
            [
                ["claim", self.SHEET, self.CELL],
                ["render", self.SHEET, self.CELL],
                ["redraw", self.SHEET, self.CELL],
                ["upload", self.SHEET, self.CELL],
            ],
        )
        self.assertEqual(
            self._state(builder),
            {
                "phase": "done",
                "detail": "",
                "problem": "",
                "url": f"{config.MAP_URL}#{self.SHEET}/{self.CELL}",
                "sheet": self.SHEET,
                "col": 74,
                "row": 20,
                "held": False,
                "old": "",
                "finishing": False,
            },
        )

    def test_choosing_a_cell_takes_no_lock(self) -> None:
        builder, fake = self._builder()
        builder.choose(self.SHEET, 74, 20)
        self.assertEqual(self._started(fake), ["login"])
        self.assertEqual(
            self._state(builder),
            {
                "phase": "idle",
                "detail": "",
                "problem": "",
                "url": "",
                "sheet": self.SHEET,
                "col": 74,
                "row": 20,
                "held": False,
                "old": "",
                "finishing": False,
            },
        )
        self.assertEqual(builder.chosen(), {"sheet": self.SHEET, "col": 74, "row": 20})

    def test_a_cell_someone_else_took_stops_there(self) -> None:
        builder, fake = self._builder()
        fake.script(
            "claim",
            ["error: refused: taken: Someone else holds that cell. Pick another."],
            code=2,
        )
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "failed")
        builder.stop()
        state = self._state(builder)
        self.assertEqual((state["problem"], state["held"]), ("taken", False))
        # Nothing else was taken on the page's own: no render, and no lock to give back.
        self.assertEqual(self._started(fake), ["login", "claim"])
        self.assertEqual(builder.chosen(), {"sheet": self.SHEET, "col": 74, "row": 20})

    def test_another_refusal_is_not_called_taken(self) -> None:
        builder, fake = self._builder()
        fake.script(
            "claim",
            ["error: refused: sheet_closed: That sheet is not open for claims."],
            code=2,
        )
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "failed")
        builder.stop()
        self.assertEqual(self._state(builder)["problem"], "refused")

    def test_a_failed_render_gives_the_lock_back(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", ["note: the tile set did not answer"], code=4)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["held"] is False and self._state(builder)["phase"] == "failed")
        builder.stop()
        self.assertEqual(self._state(builder)["problem"], "network")
        self.assertEqual(fake.calls[-1][0], ["release", self.SHEET, self.CELL])
        self.assertNotIn("redraw", self._started(fake))

    def test_a_cell_that_fails_the_check_is_not_uploaded(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [f"rendered: work/{self.SHEET}/{self.CELL}.src.png"])
        fake.script("redraw", ["check: seam"], code=5)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["held"] is False and self._state(builder)["phase"] == "failed")
        builder.stop()
        self.assertEqual(self._state(builder)["problem"], "check")
        started = self._started(fake)
        self.assertNotIn("upload", started)
        self.assertEqual(started[-1], "release")
        self.assertIn("check: seam", builder.job_lines())

    def test_an_exit_nobody_planned_for_is_failed(self) -> None:
        for code, problem in ((1, "failed"), (3, "machine")):
            with self.subTest(code=code):
                builder, fake = self._builder()
                fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
                fake.script("render", [], code=code)
                builder.choose(self.SHEET, 74, 20)
                builder.build()
                wait_until(lambda builder=builder: self._state(builder)["phase"] == "failed")
                builder.stop()
                self.assertEqual(self._state(builder)["problem"], problem)

    def test_a_second_rita_is_refused_while_one_runs(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [], hold=True)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "render")
        with self.assertRaises(Refusal) as caught:
            builder.build()
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "busy"))
        self.assertEqual(self._started(fake).count("claim"), 1)

    def test_stop_ends_the_child_and_releases(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [f"rendered: work/{self.SHEET}/{self.CELL}.src.png"])
        fake.script("redraw", ["encode window 1/4"], hold=True)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "redraw")
        redraw = fake.runs[-1]
        builder.stop()
        self.assertTrue(redraw.stopped)
        self.assertEqual(fake.calls[-1][0], ["release", self.SHEET, self.CELL])
        state = self._state(builder)
        self.assertEqual((state["phase"], state["problem"], state["held"]), ("stopped", "", False))

    def test_an_uploaded_cell_is_not_released_by_stop(self) -> None:
        builder, fake = self._builder()
        self._script_ok(fake)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "done")
        builder.stop()
        self.assertNotIn("release", self._started(fake))
        self.assertFalse(any(run.stopped for run in fake.runs))

    def test_a_page_that_went_away_stops_the_job(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [], hold=True)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "render")
        self.now += app.GONE_SECONDS + 1
        builder.tick()
        self.assertEqual(self._state(builder)["phase"], "stopped")
        self.assertEqual(fake.calls[-1][0], ["release", self.SHEET, self.CELL])

    def test_a_page_that_is_still_there_keeps_the_job(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [], hold=True)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "render")
        # A visible tab asks on every tick.
        for _round in range(10):
            self.now += 60
            builder.seen()
            builder.tick()
        self.assertEqual(self._state(builder)["phase"], "render")
        # A hidden tab asks once a minute: its timers are behind, but it has not gone.
        for _round in range(10):
            self.now += 61
            builder.tick()
            builder.seen()
        self.assertEqual(self._state(builder)["phase"], "render")
        self.assertFalse(fake.runs[-1].stopped)

    def test_the_token_reaches_the_claim_and_the_render_and_no_other_command(self) -> None:
        # "a.b.c" is shaped like a Cesium ion token. It opens nothing.
        config.write_cesium_token(CESIUM)
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [f"rendered: work/{self.SHEET}/{self.CELL}.src.png"])
        fake.script("redraw", ["check: seam"], code=5)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["held"] is False and self._state(builder)["phase"] == "failed")
        builder.stop()
        self.assertEqual(fake.calls[-1][0], ["release", self.SHEET, self.CELL])

        # And a full job, so the upload is seen too.
        self._script_ok(fake)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "done")

        for argv, env in fake.calls:
            with self.subTest(command=argv[0]):
                if argv[0] in ("claim", "render"):
                    self.assertEqual(env, {config.CESIUM_TOKEN_ENV: CESIUM})
                else:
                    self.assertEqual(env, {})
        self.assertNotIn(config.CESIUM_TOKEN_ENV, os.environ)

    def test_progress_is_text_from_the_child_cut_to_one_line(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [f"rendered: work/{self.SHEET}/{self.CELL}.src.png"])
        fake.script("redraw", ["encode window 2/4"], code=5, hold=True, again=True)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["detail"] == "encode window 2/4")
        redraw = fake.runs[-1]
        redraw.say("<b>x</b>" * 1000)
        redraw.release()
        wait_until(lambda: self._state(builder)["detail"] != "encode window 2/4")
        detail = self._state(builder)["detail"]
        self.assertIsInstance(detail, str)
        self.assertTrue(detail)
        self.assertLessEqual(len(str(detail)), 200)
        self.assertNotIn("\n", str(detail))
        # Let the child end, so the job gives its lock back and its thread is done.
        redraw.release()
        wait_until(lambda: self._state(builder)["phase"] == "failed")

    def test_nothing_is_built_on_a_machine_that_is_not_ready(self) -> None:
        cases = (
            ("machine", {"ok": False, "problem": "mac"}),
            ("tools", {"ready": False, "missing": ["torch"]}),
            ("cesium", {"set": True, "ok": False, "problem": "refused"}),
        )
        for name, answer in cases:
            with self.subTest(not_ready=name):
                builder, fake = self._builder()
                builder.choose(self.SHEET, 74, 20)
                patcher = patch(f"rutor.app.{name}", return_value=answer)
                patcher.start()
                try:
                    with self.assertRaises(Refusal) as caught:
                        builder.build()
                finally:
                    patcher.stop()
                self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_ready"))
                self.assertEqual(self._started(fake), ["login"])
        with self.subTest(not_ready="login"):
            builder, fake = self._builder(log_in=False)
            builder.choose(self.SHEET, 74, 20)
            with self.assertRaises(Refusal) as caught:
                builder.build()
            self.assertEqual(caught.exception.code, "not_ready")
            self.assertEqual(fake.calls, [])
        # Open data has no Cesium token to ask about, so it must not be asked.
        with self.subTest(not_ready="open data"):
            builder, fake = self._builder()
            self._script_ok(fake)
            builder.choose("goteborg-norr", 12, 3)
            patcher = patch("rutor.app.cesium", side_effect=AssertionError("cesium was asked"))
            patcher.start()
            try:
                builder.build()
            finally:
                patcher.stop()
            wait_until(lambda: self._state(builder)["phase"] == "done")

    def test_the_source_the_page_names_decides_what_a_build_asks(self) -> None:
        # A sheet of the tiling has no line in the drawing code. The page hands its source over.
        builder, fake = self._builder()
        builder.choose("se-n5602-e1262", 3, 4, "google")
        with patch("rutor.app.tools", return_value={"ready": True, "missing": []}) as tools:
            with patch("rutor.app.cesium", return_value={"set": True, "ok": False, "problem": "refused"}):
                with self.assertRaises(Refusal) as caught:
                    builder.build()
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "not_ready"))
        tools.assert_called_once_with("google")
        self.assertEqual(self._started(fake), ["login"])
        # The same cell on a sheet said to be open data asks no Cesium.
        builder.choose("any-town", 3, 4, "open")
        self.SHEET, self.CELL = "any-town", "3_4"
        self._script_ok(fake)
        with patch("rutor.app.cesium", side_effect=AssertionError("cesium was asked")):
            builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "done")

    def test_rita_with_no_cell_is_refused(self) -> None:
        builder, fake = self._builder()
        with self.assertRaises(Refusal) as caught:
            builder.build()
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "no_cell"))
        self.assertEqual(self._started(fake), ["login"])

    def test_a_release_that_fails_is_tried_once_more_and_then_left(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [], code=4)
        fake.script("release", [], code=4)
        fake.script("release", [], code=4)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._started(fake).count("release") == 2)
        builder.stop()
        self.assertEqual(self._started(fake).count("release"), 2)
        state = self._state(builder)
        self.assertEqual((state["phase"], state["problem"], state["held"]), ("failed", "network", True))

        # A first release that fails and a second that works give the lock back.
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [], code=4)
        fake.script("release", [], code=4)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["held"] is False and self._state(builder)["phase"] == "failed")
        builder.stop()
        self.assertEqual(self._started(fake).count("release"), 2)

    def test_a_cell_cannot_be_chosen_while_one_is_built(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [], hold=True)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "render")
        with self.assertRaises(Refusal) as caught:
            builder.choose(self.SHEET, 75, 20)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "busy"))
        self.assertEqual((self._state(builder)["col"], self._state(builder)["row"]), (74, 20))

    def test_choosing_again_after_a_job_starts_afresh(self) -> None:
        builder, fake = self._builder()
        self._script_ok(fake)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "done")
        builder.choose(self.SHEET, 75, 21)
        self.assertEqual(
            self._state(builder),
            {
                "phase": "idle",
                "detail": "",
                "problem": "",
                "url": "",
                "sheet": self.SHEET,
                "col": 75,
                "row": 21,
                "held": False,
                "old": "",
                "finishing": False,
            },
        )

    def test_an_address_that_is_not_the_maps_own_is_dropped(self) -> None:
        for address in ("https://evil.test/x", f"{config.MAP_URL}.evil.test/#x"):
            with self.subTest(address=address):
                builder, fake = self._builder()
                self._script_ok(fake, upload_url=address)
                builder.choose(self.SHEET, 74, 20)
                builder.build()
                wait_until(lambda builder=builder: self._state(builder)["phase"] == "done")
                self.assertEqual(self._state(builder)["url"], "")

    def test_shutdown_stops_the_job_too(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])
        fake.script("render", [], hold=True)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "render")
        render = fake.runs[-1]
        builder.shutdown()
        self.assertTrue(render.stopped)
        self.assertEqual(fake.calls[-1][0], ["release", self.SHEET, self.CELL])
        self.assertEqual(self._state(builder)["phase"], "stopped")

    def _claimed(self, fake: FakeStart) -> None:
        fake.script("claim", [f"claimed: {self.SHEET} {self.CELL} expires 2026-11-25T12:00"])

    def test_a_stop_that_comes_as_a_child_starts_still_stops_it(self) -> None:
        # Between "start the render" and "the render is the child being read" there is a
        # moment when a stop finds no child. The render must not then run for fifteen minutes.
        builder, fake = self._builder()
        self._claimed(fake)
        fake.script("render", ["note: this takes a while"], hold=True)

        def as_the_render_starts(argv: list[str]) -> None:
            if argv[0] == "render":
                builder.stop()

        fake.on_start = as_the_render_starts
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "stopped" and self._state(builder)["held"] is False)
        render = fake.runs[self._started(fake).index("render")]
        self.assertTrue(render.stopped)
        self.assertEqual(fake.calls[-1][0], ["release", self.SHEET, self.CELL])
        self.assertNotIn("redraw", self._started(fake))

    def test_a_stop_after_the_cell_was_taken_in_does_not_call_it_stopped(self) -> None:
        builder, fake = self._builder()
        self._claimed(fake)
        fake.script("render", ["rendered: x"])
        fake.script("redraw", ["check: ok"])
        address = f"{config.MAP_URL}#{self.SHEET}/{self.CELL}"
        fake.script("upload", [f"uploaded: {self.SHEET} {self.CELL} state vantar", f"url: {address}"], hold=True)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["url"] == address)
        # An upload that does not end by itself is ended after a while, like any child.
        with patch("rutor.app.UPLOAD_GRACE_SECONDS", 0.05):
            builder.stop()
        state = self._state(builder)
        # The service has the cell. Saying "stopped, nothing was sent" would be untrue.
        self.assertEqual((state["phase"], state["held"], state["url"]), ("done", False, address))
        self.assertNotIn("release", self._started(fake))

    def test_a_cell_cannot_be_chosen_while_a_lock_is_being_given_back(self) -> None:
        builder, fake = self._builder()
        self._claimed(fake)
        fake.script("render", [], code=4)
        fake.script("release", [], hold=True)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._started(fake)[-1] == "release")
        with self.assertRaises(Refusal) as caught:
            builder.choose(self.SHEET, 1, 1)
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(builder.chosen(), {"sheet": self.SHEET, "col": 74, "row": 20})
        fake.runs[-1].release()
        wait_until(lambda: self._state(builder)["held"] is False)
        builder.stop()
        builder.choose(self.SHEET, 1, 1)
        self.assertEqual(builder.chosen(), {"sheet": self.SHEET, "col": 1, "row": 1})

    def test_the_cell_that_is_built_is_the_one_that_was_checked(self) -> None:
        builder, fake = self._builder()
        builder.choose(self.SHEET, 74, 20)

        def another_tab_chooses_meanwhile() -> dict[str, object]:
            builder.choose(self.SHEET, 1, 1)
            return {"ok": True, "problem": ""}

        with patch("rutor.app.machine", side_effect=another_tab_chooses_meanwhile):
            with self.assertRaises(Refusal) as caught:
                builder.build()
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(self._started(fake), ["login"])
        self.assertEqual(self._state(builder)["phase"], "idle")

    def test_a_claim_that_was_cut_off_is_released_in_case_it_went_through(self) -> None:
        # A claim that is stopped after the service said yes and before it printed so would
        # leave a lock nobody knows of for two days. One release is tried, whatever it says.
        builder, fake = self._builder()
        fake.script("claim", [], hold=True)
        fake.script("release", ["error: refused: not_yours: That cell is not yours."], code=2)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._started(fake)[-1] == "claim")
        builder.stop()
        state = self._state(builder)
        self.assertEqual((state["phase"], state["held"]), ("stopped", False))
        self.assertEqual(self._started(fake), ["login", "claim", "release"])

    def test_a_claim_the_network_cut_is_released_in_case_it_went_through(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", ["error: could not reach the service (timed out)"], code=4)
        fake.script("release", [f"released: {self.SHEET} {self.CELL}"])
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._started(fake)[-1] == "release")
        builder.stop()
        state = self._state(builder)
        self.assertEqual((state["phase"], state["problem"], state["held"]), ("failed", "network", False))
        self.assertEqual(self._started(fake).count("release"), 1)

    def test_a_claim_that_this_machine_refused_took_no_lock(self) -> None:
        builder, fake = self._builder()
        fake.script("claim", ["error: this machine cannot render stockholm-norr yet: CESIUM_ION_TOKEN is not set"], code=3)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "failed")
        builder.stop()
        self.assertEqual(self._state(builder)["problem"], "machine")
        self.assertEqual(self._started(fake), ["login", "claim"])

    def test_too_many_locks_and_too_many_waiting_have_words_of_their_own(self) -> None:
        # "Try again in a while" is no help to someone the service will refuse for two days.
        for said, word in (
            ("error: refused: too_many_locks: You already hold as many cells as you may.", "locks"),
            ("error: refused: too_many_waiting: Five of your cells are waiting for review.", "waiting"),
        ):
            with self.subTest(word=word):
                builder, fake = self._builder()
                fake.script("claim", [said], code=2)
                builder.choose(self.SHEET, 74, 20)
                builder.build()
                wait_until(lambda builder=builder: self._state(builder)["phase"] == "failed")
                builder.stop()
                self.assertEqual(self._state(builder)["problem"], word)
                self.assertNotIn("release", self._started(fake))

    def _left_holding(self) -> tuple[app.Builder, FakeStart]:
        """A job whose render failed and whose lock could not be given back."""
        builder, fake = self._builder()
        self._claimed(fake)
        fake.script("render", [], code=4)
        fake.script("release", ["error: could not reach the service"], code=4)
        fake.script("release", ["error: could not reach the service"], code=4)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._started(fake).count("release") == 2 and self._state(builder)["phase"] == "failed")
        builder.stop()
        self.assertTrue(self._state(builder)["held"])
        return builder, fake

    def _releases(self, fake: FakeStart) -> list[list[str]]:
        return [argv for argv, _env in fake.calls if argv[0] == "release"]

    def test_a_lock_that_could_not_be_given_back_is_asked_back_before_the_next_cell(self) -> None:
        builder, fake = self._left_holding()
        builder.choose(self.SHEET, 75, 20)
        fake.script("release", [f"released: {self.SHEET} 74_20"])
        fake.script("claim", [f"claimed: {self.SHEET} 75_20 expires 2026-11-25T12:00"])
        fake.script("render", ["rendered: x"])
        fake.script("redraw", ["check: ok"])
        fake.script("upload", [f"uploaded: {self.SHEET} 75_20 state vantar"])
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "done")
        builder.stop()
        # Every release names the cell that was locked, never the one chosen after it.
        self.assertEqual(self._releases(fake), [["release", self.SHEET, "74_20"]] * 3)
        self.assertEqual(fake.calls[-4][0], ["claim", self.SHEET, "75_20"])
        self.assertFalse(self._state(builder)["held"])

    def test_a_lock_that_is_still_out_stops_the_next_cell_before_it_is_claimed(self) -> None:
        builder, fake = self._left_holding()
        claims = self._started(fake).count("claim")
        builder.choose(self.SHEET, 75, 20)
        fake.script("release", ["error: could not reach the service"], code=4)
        fake.script("release", ["error: could not reach the service"], code=4)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "failed" and self._started(fake).count("release") == 4)
        builder.stop()
        state = self._state(builder)
        self.assertEqual((state["problem"], state["held"]), ("lock_left", True))
        self.assertEqual(self._started(fake).count("claim"), claims)
        self.assertEqual(self._releases(fake), [["release", self.SHEET, "74_20"]] * 4)

    def test_a_stopped_claim_is_released_whatever_its_exit_says(self) -> None:
        # taskkill on Windows ends a child with exit 1, which otherwise reads as "bad input".
        builder, fake = self._builder()
        fake.script("claim", [], hold=True)

        def as_windows_ends_it(argv: list[str]) -> None:
            if argv[0] == "claim":
                fake.runs[-1].stopped_code = 1

        fake.on_start = as_windows_ends_it
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._started(fake)[-1] == "claim")
        builder.stop()
        self.assertEqual(self._started(fake), ["login", "claim", "release"])
        self.assertEqual(self._state(builder)["phase"], "stopped")

    def test_rita_on_a_cell_that_is_already_in_is_refused(self) -> None:
        builder, fake = self._builder()
        self._script_ok(fake)
        builder.choose(self.SHEET, 74, 20)
        builder.build()
        wait_until(lambda: self._state(builder)["phase"] == "done")
        with self.assertRaises(Refusal) as caught:
            builder.build()
        self.assertEqual(caught.exception.code, "no_cell")
        self.assertEqual(self._started(fake).count("claim"), 1)
        self.assertEqual(self._state(builder)["phase"], "done")


# An address of a release page under the project's own name. It opens nothing.
RELEASE_URL = "https://github.com/opensverige/karta.opensverige-3d/releases/tag/app-v0.2.0"


class VersionTests(_OwnHome):
    """Whether a newer app is out: the one question the page asks GitHub, and only when installed."""

    def _installed(self, version: str | None) -> None:
        """What config.app_install answers: a version on an app install, None on a clone."""
        answer = None if version is None else {"version": version, "lock_sha256": "ab" * 32}
        patcher = patch("rutor.config.app_install", return_value=answer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_newer_release_is_named_with_its_address(self) -> None:
        self._installed("0.1.0")
        releases = [
            {"tag_name": "v9", "html_url": "https://evil.test/x"},
            {"tag_name": "app-v0.2.0", "html_url": RELEASE_URL},
        ]
        found = app.app_version(lambda: releases)
        self.assertEqual(found, {"mine": "0.1.0", "latest": "0.2.0", "current": False, "url": RELEASE_URL})

    def test_no_answer_from_github_is_not_an_old_app(self) -> None:
        self._installed("0.1.0")
        faults: list[BaseException] = [OSError("down"), ValueError("not json"), network("down")]
        for fault in faults:
            with self.subTest(fault=type(fault).__name__):

                def fetch(raising: BaseException = fault) -> object:
                    raise raising

                self.assertEqual(
                    app.app_version(fetch),
                    {"mine": "0.1.0", "latest": "", "current": True, "url": ""},
                )

    def test_an_address_elsewhere_is_not_passed_on(self) -> None:
        self._installed("0.1.0")
        releases = [{"tag_name": "app-v0.2.0", "html_url": "https://evil.test/x"}]
        found = app.app_version(lambda: releases)
        self.assertEqual(found, {"mine": "0.1.0", "latest": "0.2.0", "current": False, "url": ""})

    def test_ten_is_newer_than_nine(self) -> None:
        self._installed("0.9.0")
        newer = [{"tag_name": "app-v0.10.0", "html_url": ""}]
        self.assertFalse(app.app_version(lambda: newer)["current"])
        self._installed("0.10.0")
        older = [{"tag_name": "app-v0.9.0", "html_url": ""}]
        found = app.app_version(lambda: older)
        self.assertTrue(found["current"])
        self.assertEqual(found["latest"], "0.9.0")

    def test_an_address_that_climbs_out_of_the_releases_is_not_passed_on(self) -> None:
        self._installed("0.1.0")
        for address in (
            "https://github.com/opensverige/karta.opensverige-3d/releases/../../../someone/else",
            "https://github.com/opensverige/karta.opensverige-3d/releases/tag/..",
            "https://github.com/opensverige/karta.opensverige-3d/releases//evil.test",
        ):
            with self.subTest(address=address):
                found = app.app_version(lambda: [{"tag_name": "app-v0.2.0", "html_url": address}])
                self.assertEqual((found["current"], found["url"]), (False, ""))

    def test_a_version_written_shorter_is_the_same_version(self) -> None:
        # 0.2 and 0.2.0 are one version, whichever of the two a tag or an install wrote.
        for mine, tag in (("0.2.0", "app-v0.2"), ("0.2", "app-v0.2.0"), ("0.2.0", "app-v0.2.0.0")):
            with self.subTest(mine=mine, tag=tag):
                self._installed(mine)
                self.assertTrue(app.app_version(lambda: [{"tag_name": tag, "html_url": ""}])["current"])

    def test_an_answer_of_another_shape_is_no_news(self) -> None:
        self._installed("0.1.0")
        for answer in ({}, "x", [1, None, {"tag_name": 5}]):
            with self.subTest(answer=answer):
                self.assertEqual(
                    app.app_version(lambda: answer),
                    {"mine": "0.1.0", "latest": "", "current": True, "url": ""},
                )

    def test_a_clone_does_not_ask_github(self) -> None:
        self._installed(None)

        def fetch() -> object:
            raise AssertionError("GitHub was asked on a clone")

        self.assertEqual(
            app.app_version(fetch),
            {"mine": rutor.__version__, "latest": "", "current": True, "url": ""},
        )

    def test_the_same_version_is_current(self) -> None:
        self._installed("0.2.0")
        releases = [{"tag_name": "app-v0.2.0", "html_url": ""}]
        found = app.app_version(lambda: releases)
        self.assertEqual(found, {"mine": "0.2.0", "latest": "0.2.0", "current": True, "url": ""})


# What the page's checks answer in these tests. The real ones read the machine.
_MACHINE = {
    "ok": True,
    "card": "Test Card",
    "card_gb": 12,
    "free_gb": 100,
    "needed_gb": 2,
    "ram_gb": 16,
    "problem": "",
}
_TOOLS = {"ready": True, "model_cached": True, "missing": []}
_PRICE = {"decision": "RUN", "after": ""}

# One of each state the map uses, on a 3 by 3 sheet. (0, 1) is locked by someone else.
_SHEET_CELLS: list[tuple[int, int, str, str | None]] = [
    (0, 0, "live", "gustaf"),
    (1, 0, "fri", None),
    (2, 0, "fri", None),
    (0, 1, "last", "klang"),
    (1, 1, "vantar", "gustaf"),
    (2, 1, "godkand", "gustaf"),
    (0, 2, "ute", None),
    (1, 2, "live", "gustaf"),
    (2, 2, "fri", None),
]


class FakeInstaller:
    """Stands in for app_install.Installer: records what the page asked of it."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, ...]] = []
        self.stops = 0
        self.answer: dict[str, object] = {
            "phase": "idle", "done_gb": 0.0, "total_gb": 15.9, "problem": "", "whole": True,
        }  # fmt: skip
        self.said: list[str] = []
        # A run returns at once unless the test holds it here.
        self.go = threading.Event()
        self.go.set()
        self._running = threading.Event()

    def run(self, redo: tuple[str, ...] = ()) -> None:
        self.runs.append(tuple(redo))
        self._running.set()
        try:
            self.go.wait(timeout=30.0)
        finally:
            self._running.clear()

    def stop(self) -> None:
        self.stops += 1
        self.go.set()

    def running(self) -> bool:
        return self._running.is_set()

    def state(self) -> dict[str, object]:
        return dict(self.answer)

    def lines(self) -> list[str]:
        return list(self.said)


class AppServerTests(_OwnHome):
    """The page over real HTTP, against a service on this machine and a scripted child."""

    def setUp(self) -> None:
        super().setUp()
        self.svc = FakeService()
        self.addCleanup(self.svc.stop)
        saved_url = os.environ.get("PIXELBYGGET_URL")
        os.environ["PIXELBYGGET_URL"] = self.svc.start()

        def restore_url() -> None:
            if saved_url is None:
                os.environ.pop("PIXELBYGGET_URL", None)
            else:
                os.environ["PIXELBYGGET_URL"] = saved_url

        self.addCleanup(restore_url)
        config.write_token("test-token")
        # A builder who said yes to today's terms on this machine.
        login.keep_terms()
        config.write_cesium_token("a.b.c")
        self._put_sheet(list(_SHEET_CELLS))
        machine_patch = patch("rutor.app.machine", return_value=dict(_MACHINE))
        tools_patch = patch("rutor.app.tools", return_value=dict(_TOOLS))
        price_patch = patch("rutor.app.price", return_value=dict(_PRICE))
        ask_patch = patch("rutor.renderer.google._ask_ion", return_value=(200, ""))
        self.machine_mock = machine_patch.start()
        tools_patch.start()
        price_patch.start()
        self.ask = ask_patch.start()
        for patcher in (machine_patch, tools_patch, price_patch, ask_patch):
            self.addCleanup(patcher.stop)
        self.fake = FakeStart()
        self.builder = app.Builder(self.fake)
        self.installer = FakeInstaller()
        self.server = app.AppServer(self.builder, releases=list, installer=self.installer)
        self.addCleanup(self.server.stop)
        self.server.start()
        self.port = self.server.port
        self.base = f"/{self.server.key}"

    def _app_install(self) -> None:
        """From here on this is an app install: the folder the installer made."""
        for target, value in (
            ("rutor.config.app_dir", Path(self.tmp.name) / "Åsa Öberg" / "Pixelbygget"),
            ("rutor.config.app_install", {"version": "0.1.0", "lock_sha256": "ab" * 32}),
            ("rutor.app_install.is_app", True),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _install(self, expect: int = 200) -> dict[str, object]:
        status, _headers, body = self.call("POST", f"{self.base}/api/install", {})
        self.assertEqual(status, expect, body)
        parsed = json.loads(body)
        self.assertIsInstance(parsed, dict)
        return parsed

    def _hold_install(self) -> None:
        """An install that is under way until the test lets it go or stops it."""
        self.installer.go.clear()
        self.addCleanup(self.installer.go.set)
        self._install()
        wait_until(self.installer.running)

    def _put_sheet(self, cells: list[tuple[int, int, str, str | None]]) -> None:
        sheet: dict[str, object] = {
            "id": "stockholm-norr",
            "name": "Stockholm",
            "state": "open",
            "done": 1,
            "total": 9,
            "cols": 3,
            "rows": 3,
            "pipeline": pipeline.PIPELINE,
        }
        stored: dict[str, dict[str, object]] = {}
        grid: list[list[object]] = []
        for col, row, state, who in cells:
            stored[f"stockholm-norr/{col}_{row}"] = {
                "col": col,
                "row": row,
                "state": state,
                "login": who,
                "updated": "2026-11-17T12:00:00Z",
            }
            grid.append([col, row, state, who, "2026-11-17T12:00:00Z"])
        self.svc.state["sheets"] = {"stockholm-norr": sheet}
        self.svc.state["cells"] = stored
        self.grid_cells = grid
        self.pick_sheet = {"id": "stockholm-norr", "cols": 3, "rows": 3}

    def call(
        self,
        method: str,
        path: str,
        body: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            sent.setdefault("Content-Type", "application/json")
        try:
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def _state(self) -> dict[str, object]:
        status, _headers, body = self.call("GET", f"{self.base}/api/state")
        self.assertEqual(status, 200, body)
        parsed = json.loads(body)
        self.assertIsInstance(parsed, dict)
        return parsed

    def _assert_no_secret(self, headers: dict[str, str], body: bytes, *extra: bytes) -> None:
        for secret in (b"test-token", b"a.b.c", *extra):
            self.assertNotIn(secret, body)
            for value in headers.values():
                self.assertNotIn(secret.decode("ascii"), value)

    def test_the_page_and_its_files_are_served(self) -> None:
        for rest, (name, content_type) in app.PAGE_FILES.items():
            path = f"{self.base}/" if rest == "" else f"{self.base}/{rest}"
            status, headers, body = self.call("GET", path)
            self.assertEqual(status, 200, rest)
            self.assertEqual(headers.get("content-type"), content_type, rest)
            self.assertEqual(body, (app.PAGE_DIR / name).read_bytes(), rest)
        for rest in ("app.py", "../app.py"):
            status, _headers, _body = self.call("GET", f"{self.base}/{rest}")
            self.assertEqual(status, 404, rest)

    def _put_new_sheet(self, kind: object) -> None:
        self._put_sheet([(0, 0, "fri", None)])
        sheets = self.svc.state["sheets"]
        cells = self.svc.state["cells"]
        assert isinstance(sheets, dict) and isinstance(cells, dict)
        sheet = dict(sheets.pop("stockholm-norr"))
        sheet["id"] = "se-n5602-e1262"
        if kind is not None:
            sheet["kind"] = kind
        sheets["se-n5602-e1262"] = sheet
        cells["se-n5602-e1262/0_0"] = cells.pop("stockholm-norr/0_0")

    def test_the_sheet_says_its_own_source(self) -> None:
        # No line in the drawing code names this sheet: the service's row does.
        self._put_new_sheet("google")
        sheet = self._state()["sheet"]
        self.assertEqual((sheet["id"], sheet["kind"]), ("se-n5602-e1262", "google"))

    def test_a_sheet_with_no_source_has_none_on_the_page(self) -> None:
        for kind in (None, "bing", 7):
            with self.subTest(kind=kind):
                self._put_new_sheet(kind)
                self.assertEqual(app._open_record(dict(self.svc.state["sheets"]["se-n5602-e1262"]))["kind"], "")

    def test_a_build_on_a_new_google_sheet_asks_cesium(self) -> None:
        self._put_new_sheet("google")
        self.builder.accept_terms()
        self.fake.script("claim", [], hold=True)
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"next": True})
        self.assertEqual((status, json.loads(body)), (200, {"sheet": "se-n5602-e1262", "col": 0, "row": 0}))
        self.ask.reset_mock()
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual(status, 200, body)
        self.ask.assert_called()

    def test_the_state_has_every_part_the_page_draws(self) -> None:
        found = self._state()
        self.assertEqual(
            set(found),
            {"machine", "tools", "terms", "login", "cesium", "price", "sheet", "cell", "job", "install", "version"},
        )
        terms = found["terms"]
        self.assertIsInstance(terms, dict)
        self.assertEqual(len(terms["lines"]), 10)
        login_state = found["login"]
        self.assertIsInstance(login_state, dict)
        self.assertEqual(login_state["login"], "klang")
        sheet = found["sheet"]
        self.assertIsInstance(sheet, dict)
        self.assertEqual(sheet["id"], "stockholm-norr")
        self.assertEqual(sheet["kind"], "google")
        self.assertIsNone(found["cell"])
        job = found["job"]
        self.assertIsInstance(job, dict)
        self.assertEqual(job["phase"], "idle")

    def test_neither_secret_leaves_in_any_answer(self) -> None:
        self.fake.script("login", ["logged in: klang"])
        pages = (
            ("GET", f"{self.base}/", None),
            ("GET", f"{self.base}/app.js", None),
            ("GET", f"{self.base}/api/state", None),
            ("GET", f"{self.base}/api/grid", None),
            ("GET", f"{self.base}/api/report", None),
            ("POST", f"{self.base}/api/terms", {"accept": True}),
            ("POST", f"{self.base}/api/login", {}),
            ("POST", f"{self.base}/api/cesium", {"token": "x.y.z-new"}),
            ("POST", f"{self.base}/api/cell", {"next": True}),
            ("POST", f"{self.base}/api/build", {}),
            ("POST", f"{self.base}/api/stop", {}),
            ("POST", f"{self.base}/api/install/stop", {}),
        )
        for method, path, body in pages:
            status, headers, payload = self.call(method, path, body)
            self.assertEqual(status, 200, (path, payload))
            extra = (b"x.y.z-new",) if path.endswith("/api/cesium") else ()
            self._assert_no_secret(headers, payload, *extra)

    def test_a_cesium_token_is_kept_only_when_cesium_accepts_it(self) -> None:
        self.ask.return_value = (401, "INVALID_TOKEN")
        status, _headers, body = self.call("POST", f"{self.base}/api/cesium", {"token": "n.e.w"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {"ok": False, "problem": "refused"})
        self.assertEqual(config.read_cesium_token(), "a.b.c")
        self.ask.return_value = (200, "")
        status, _headers, body = self.call("POST", f"{self.base}/api/cesium", {"token": "n.e.w"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {"ok": True, "problem": ""})
        self.assertEqual(config.read_cesium_token(), "n.e.w")

    def test_next_picks_the_cell_the_command_would(self) -> None:
        expect = commands.pick_next(self.pick_sheet, self.grid_cells)
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"next": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {"sheet": "stockholm-norr", "col": expect[0], "row": expect[1]})
        cell = self._state()["cell"]
        self.assertEqual(cell, {"sheet": "stockholm-norr", "col": expect[0], "row": expect[1]})
        self.assertEqual(self.fake.calls, [])

    # ---- a cell by its code, pasted from the map

    NEW = "se-n5602-e1262"

    def _add_new_sheet(self, cells: tuple[tuple[int, int, str, str | None], ...] = (), **overrides: object) -> None:
        """A sheet of the tiling beside the open one: its free cells have no rows."""
        sheet: dict[str, object] = {
            "id": self.NEW,
            "name": "N 56,02 E 12,62",
            "state": "open",
            "done": 0,
            "total": len(cells),
            "cols": 61,
            "rows": 47,
            "pipeline": pipeline.PIPELINE,
            "kind": "google",
            "made": "grid",
        }
        sheet.update(overrides)
        sheets, stored = self.svc.state["sheets"], self.svc.state["cells"]
        assert isinstance(sheets, dict) and isinstance(stored, dict)
        sheets[self.NEW] = sheet
        for col, row, state, who in cells:
            stored[f"{self.NEW}/{col}_{row}"] = {"col": col, "row": row, "state": state, "login": who, "updated": "2026-11-17T12:00:00Z"}

    def _code(self, code: object) -> tuple[int, dict[str, object]]:
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"code": code})
        parsed = json.loads(body)
        self.assertIsInstance(parsed, dict)
        return status, parsed

    def test_a_good_code_chooses_that_cell_and_that_sheet(self) -> None:
        self._add_new_sheet()
        status, answer = self._code(f"{self.NEW} 31_22")
        self.assertEqual((status, answer), (200, {"sheet": self.NEW, "col": 31, "row": 22}))
        found = self._state()
        self.assertEqual(found["cell"], {"sheet": self.NEW, "col": 31, "row": 22})
        sheet = found["sheet"]
        self.assertEqual((sheet["id"], sheet["kind"], sheet["cols"], sheet["rows"], sheet["problem"]), (self.NEW, "google", 61, 47, ""))
        status, _headers, body = self.call("GET", f"{self.base}/api/grid")
        self.assertEqual(json.loads(body)["sheet"]["id"], self.NEW)
        # Nothing was claimed and no child was started: the cell is locked when Rita is pressed.
        self.assertEqual(self.fake.calls, [])
        self.assertEqual([r for r in self.svc.requests if r["method"] == "POST"], [])

    def test_the_chosen_sheet_stays_the_pages_sheet(self) -> None:
        # The page looks for an open sheet again every minute. A sheet the person chose is theirs.
        now = {"t": 0.0}
        self.server._known._clock = lambda: now["t"]
        self._add_new_sheet()
        self._code(f"{self.NEW} 31_22")
        now["t"] = 10_000.0
        self.assertEqual(self._state()["sheet"]["id"], self.NEW)

    def test_a_code_is_read_whatever_space_the_copy_left_around_it(self) -> None:
        self._add_new_sheet()
        for code in (f"  {self.NEW} 31_22\n", f"{self.NEW}\t31_22", f"{self.NEW}  31_22 "):
            with self.subTest(code=code):
                status, answer = self._code(code)
                self.assertEqual((status, answer.get("col"), answer.get("row")), (200, 31, 22))

    def test_a_code_for_a_hand_made_sheet_works_too(self) -> None:
        status, answer = self._code("stockholm-norr 1_0")
        self.assertEqual((status, answer), (200, {"sheet": "stockholm-norr", "col": 1, "row": 0}))
        status, answer = self._code("stockholm-norr 0_1")
        self.assertEqual((status, answer.get("code")), (409, "taken"))

    def test_a_code_for_a_cell_outside_the_sheet_is_a_bad_code(self) -> None:
        self._add_new_sheet()
        for code in (f"{self.NEW} 61_0", f"{self.NEW} 0_47", f"{self.NEW} 9999_9999", "stockholm-norr 3_0"):
            with self.subTest(code=code):
                status, answer = self._code(code)
                self.assertEqual((status, answer.get("code")), (400, "bad_code"))
        self.assertIsNone(self.builder.chosen())

    def test_a_code_of_another_shape_is_a_bad_code_and_the_service_is_not_asked(self) -> None:
        self._add_new_sheet()
        before = len(self.svc.requests)
        codes: tuple[object, ...] = (
            "SE-N5602-E1262 31_22", "../etc 1_1", "se n5602 1_1", self.NEW, f"{self.NEW} 31-22", f"{self.NEW} 31_22 extra",
            "31_22", "", "   ", f"{self.NEW} -1_2", f"{self.NEW} 1.0_2", f"{self.NEW}/31_22", f"{self.NEW} 31_22?x=1",
            f"{self.NEW} 00031_22", "x" * 101, 5602, None, True, ["se-n5602-e1262", 31, 22], {"sheet": self.NEW},
        )
        for code in codes:
            with self.subTest(code=code):
                status, answer = self._code(code)
                self.assertEqual((status, answer.get("code")), (400, "bad_code"))
        self.assertEqual(len(self.svc.requests), before)
        self.assertIsNone(self.builder.chosen())

    def test_a_code_with_markup_is_a_bad_code_and_is_not_said_back(self) -> None:
        self._add_new_sheet()
        for code in (f"<b>{self.NEW}</b> 31_22", '"><script>alert(1)</script>', f"{self.NEW} 31_22<img src=x>", f"javascript:{self.NEW} 31_22"):
            with self.subTest(code=code):
                status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"code": code})
                self.assertEqual((status, json.loads(body).get("code")), (400, "bad_code"))
                self.assertNotIn(b"<", body)
                self.assertNotIn(b"script", body)
        self.assertIsNone(self.builder.chosen())

    def test_a_code_for_a_sheet_the_service_does_not_have_is_a_bad_code(self) -> None:
        status, answer = self._code("se-n9999-e9999 1_1")
        self.assertEqual((status, answer.get("code")), (400, "bad_code"))
        self.assertIsNone(self.builder.chosen())

    def test_a_code_for_a_cell_someone_has_is_taken(self) -> None:
        self._add_new_sheet(((31, 22, "last", "ebba"), (30, 22, "fri", None)))
        status, answer = self._code(f"{self.NEW} 31_22")
        self.assertEqual((status, answer.get("code")), (409, "taken"))
        self.assertIsNone(self.builder.chosen())
        # One that was given back has a row and is free; one nobody took has no row and is free.
        self.assertEqual(self._code(f"{self.NEW} 30_22")[0], 200)
        self.assertEqual(self._code(f"{self.NEW} 12_40")[0], 200)

    def test_a_code_on_a_sheet_that_is_not_open_is_taken(self) -> None:
        self._add_new_sheet(state="done")
        status, answer = self._code(f"{self.NEW} 31_22")
        self.assertEqual((status, answer.get("code")), (409, "taken"))

    def test_a_code_on_a_hand_made_sheet_needs_the_cell_to_have_a_free_row(self) -> None:
        # On a hand-made sheet a cell with no row is not offered.
        self._put_sheet([(0, 0, "fri", None)])
        self.assertEqual(self._code("stockholm-norr 0_0")[0], 200)
        status, answer = self._code("stockholm-norr 2_2")
        self.assertEqual((status, answer.get("code")), (409, "taken"))

    def test_a_click_on_the_chosen_sheet_takes_a_cell_with_no_row(self) -> None:
        self._add_new_sheet(((31, 22, "live", "ebba"),))
        self._code(f"{self.NEW} 30_22")
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"sheet": self.NEW, "col": 32, "row": 22})
        self.assertEqual((status, json.loads(body)), (200, {"sheet": self.NEW, "col": 32, "row": 22}))
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"sheet": self.NEW, "col": 31, "row": 22})
        self.assertEqual((status, json.loads(body).get("code")), (409, "taken"))

    def test_the_grid_of_a_sheet_of_the_tiling_says_that_a_cell_with_no_row_is_free(self) -> None:
        self._add_new_sheet(((31, 22, "live", "ebba"),))
        self._code(f"{self.NEW} 30_22")
        status, _headers, body = self.call("GET", f"{self.base}/api/grid")
        grid = json.loads(body)
        self.assertEqual(grid["sheet"]["made"], "grid")
        self.assertEqual(grid["cells"], [[31, 22, "live"]])
        # A hand-made sheet does not say so.
        self.call("POST", f"{self.base}/api/cell", {"next": True})
        status, _headers, body = self.call("GET", f"{self.base}/api/grid")
        self.assertEqual(json.loads(body)["sheet"]["made"], "hand")

    def test_next_goes_back_to_the_open_sheet(self) -> None:
        self._add_new_sheet()
        self._code(f"{self.NEW} 31_22")
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"next": True})
        self.assertEqual((status, json.loads(body)["sheet"]), (200, "stockholm-norr"))
        self.assertEqual(self._state()["sheet"]["id"], "stockholm-norr")

    def test_a_build_after_a_code_is_judged_by_the_chosen_sheet(self) -> None:
        self.builder.accept_terms()
        self._add_new_sheet(pipeline="2099.01-z")
        self._code(f"{self.NEW} 31_22")
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual((status, json.loads(body).get("code")), (409, "old_app"))
        self._add_new_sheet()
        self._code(f"{self.NEW} 31_22")
        self.fake.script("claim", [], hold=True)
        self.ask.reset_mock()
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual(status, 200, body)
        self.ask.assert_called()
        wait_until(lambda: [argv for argv, _env in self.fake.calls if argv[0] == "claim"] == [["claim", self.NEW, "31_22"]])

    def test_a_code_is_not_taken_while_a_cell_is_being_built(self) -> None:
        self.builder.accept_terms()
        self._add_new_sheet()
        self.fake.script("claim", [], hold=True)
        self.call("POST", f"{self.base}/api/cell", {"next": True})
        self.call("POST", f"{self.base}/api/build", {})
        status, answer = self._code(f"{self.NEW} 31_22")
        self.assertEqual((status, answer.get("code")), (409, "busy"))
        self.assertEqual(self._state()["sheet"]["id"], "stockholm-norr")

    def test_a_cell_outside_the_sheet_is_refused(self) -> None:
        for body in (
            {"sheet": "stockholm-norr", "col": 9999, "row": 0},
            {"sheet": "goteborg-norr", "col": 0, "row": 0},
            {"sheet": "stockholm-norr", "col": True, "row": 0},
            [1, 2, 3],
        ):
            status, _headers, _payload = self.call("POST", f"{self.base}/api/cell", body)
            self.assertEqual(status, 400, body)
        self.assertIsNone(self.builder.chosen())

    def test_a_cell_that_is_not_free_is_refused(self) -> None:
        status, _headers, body = self.call(
            "POST", f"{self.base}/api/cell", {"sheet": "stockholm-norr", "col": 0, "row": 1}
        )
        self.assertEqual(status, 409, body)
        parsed = json.loads(body)
        self.assertEqual(parsed["code"], "taken")
        self.assertIsNone(self.builder.chosen())

    def test_what_the_service_says_arrives_as_one_plain_line(self) -> None:
        self._state()
        message = "<script>x</script>" * 100
        self.svc.fail_next_with(
            500,
            json.dumps({"error": "boom", "message": message}).encode("utf-8"),
            "application/json",
        )
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"next": True})
        parsed = json.loads(body)
        error = parsed["error"]
        self.assertIsInstance(error, str)
        self.assertLessEqual(len(error), 200)
        self.assertNotIn("\n", error)
        self.assertLess(len(error), len(message))
        self.assertIn("boom", error)

    def test_other_builders_logins_are_not_passed_on(self) -> None:
        status, _headers, body = self.call("GET", f"{self.base}/api/grid")
        self.assertEqual(status, 200, body)
        self.assertNotIn(b"klang", body)
        parsed = json.loads(body)
        cells = parsed["cells"]
        self.assertIsInstance(cells, list)
        self.assertGreater(len(cells), 0)
        for cell in cells:
            self.assertEqual(len(cell), 3)

    def test_a_sheet_with_nothing_free_says_so(self) -> None:
        stored = self.svc.state["cells"]
        self.assertIsInstance(stored, dict)
        for cell in stored.values():
            self.assertIsInstance(cell, dict)
            cell["state"] = "live"
        found = self._state()
        sheet = found["sheet"]
        self.assertIsInstance(sheet, dict)
        self.assertEqual(sheet["problem"], "none_free")
        self.assertEqual(sheet["id"], "")
        status, _headers, body = self.call("GET", f"{self.base}/api/grid")
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {"sheet": None, "cells": []})

    def test_a_service_that_cannot_be_reached_is_said_in_the_state(self) -> None:
        self.svc.stop()
        found = self._state()
        sheet = found["sheet"]
        self.assertIsInstance(sheet, dict)
        self.assertEqual(sheet["problem"], "network")

    def test_checks_are_not_worked_out_at_every_request(self) -> None:
        for _times in range(3):
            self._state()
        self.assertEqual(self.machine_mock.call_count, 1)

    def test_checks_stand_while_a_job_runs(self) -> None:
        now = {"t": 0.0}
        self.server._known._clock = lambda: now["t"]
        self.fake.script("claim", ["claimed: stockholm-norr 1_0 expires 2026-11-25T12:00:00Z"])
        self.fake.script("render", ["drawing"], hold=True)
        self._state()
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"next": True})
        self.assertEqual(status, 200, body)
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual(status, 200, body)
        # The detail is kept while the child is being read, so the stop has a child to stop.
        wait_until(lambda: self.builder.job_state()["detail"] == "drawing")
        calls = self.machine_mock.call_count
        now["t"] = 10_000.0
        self._state()
        self.assertEqual(self.machine_mock.call_count, calls)

    def test_leaving_stops_a_running_job(self) -> None:
        self.fake.script("claim", ["claimed: stockholm-norr 1_0 expires 2026-11-25T12:00:00Z"])
        self.fake.script("render", ["drawing"], hold=True)
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"next": True})
        self.assertEqual(status, 200, body)
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual(status, 200, body)
        wait_until(lambda: self.builder.job_state()["detail"] == "drawing")
        render = self.fake.runs[[argv[0] for argv, _env in self.fake.calls].index("render")]
        self.server.stop()
        self.assertTrue(render.stopped)
        self.assertIn("release", [argv[0] for argv, _env in self.fake.calls])

    def test_asking_for_the_state_tells_the_builder_the_page_is_there(self) -> None:
        seen = {"n": 0}
        real = self.builder.seen

        def count() -> None:
            seen["n"] += 1
            real()

        with patch.object(self.builder, "seen", count):
            self.call("GET", f"{self.base}/app.css")
            self.assertEqual(seen["n"], 0)
            self.call("GET", f"{self.base}/api/state")
            self.assertEqual(seen["n"], 1)

    def test_the_report_names_no_person_and_no_home_folder(self) -> None:
        home = str(Path.home())
        names = {Path.home().name}
        try:
            names.add(getpass.getuser())
        except OSError:
            pass
        self.fake.script("claim", [f"{home}/work/x.png", *sorted(names)], code=1)
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"next": True})
        self.assertEqual(status, 200, body)
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual(status, 200, body)
        wait_until(lambda: self.builder.job_state()["phase"] == "failed")
        status, headers, body = self.call("GET", f"{self.base}/api/report")
        self.assertEqual(status, 200, body)
        self.assertEqual(headers.get("content-type"), "text/plain; charset=utf-8")
        text = body.decode("utf-8")
        self.assertNotIn(home, text)
        self.assertIn("~", text)
        self.assertIn("~/work/x.png", text)
        for name in names:
            self.assertIsNone(
                re.search(rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])", text, re.IGNORECASE),
                name,
            )
        self.assertNotIn(b"test-token", body)
        self.assertNotIn(b"a.b.c", body)

    def test_nothing_is_fetched_before_the_machine_and_the_terms_say_yes(self) -> None:
        self._app_install()
        self.machine_mock.return_value = {**_MACHINE, "ok": False, "problem": "small_card"}
        self.assertEqual(self._install(expect=409).get("code"), "not_ready")
        self.machine_mock.return_value = dict(_MACHINE)
        with patch.object(self.builder, "terms_accepted", return_value=False):
            self.assertEqual(self._install(expect=409).get("code"), "not_ready")
        self.assertEqual(self.installer.runs, [])
        # With both, it starts.
        self._install()
        wait_until(lambda: self.installer.runs == [()])

    def test_an_install_and_a_cell_never_run_together(self) -> None:
        self._app_install()
        self.fake.script("claim", [], hold=True)
        self.call("POST", f"{self.base}/api/cell", {"next": True})
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual(status, 200, body)
        wait_until(lambda: self.builder.job_state()["phase"] == "claim")
        self.assertEqual(self._install(expect=409).get("code"), "busy")
        self.assertEqual(self.installer.runs, [])
        self.call("POST", f"{self.base}/api/stop", {})
        wait_until(lambda: self.builder.job_state()["phase"] == "stopped" and not self.builder.busy())
        # And the other way round: nothing is built, and no second fetch starts, while one runs.
        self._hold_install()
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual((status, json.loads(body).get("code")), (409, "busy"))
        self.assertEqual(self._install(expect=409).get("code"), "busy")
        self.assertEqual(len(self.installer.runs), 1)
        self.assertEqual(self.builder.job_state()["phase"], "stopped")

    def test_a_hand_made_install_gets_no_button(self) -> None:
        install = self._state()["install"]
        self.assertIsInstance(install, dict)
        self.assertIs(install["app"], False)
        self.assertEqual(self._install(expect=409).get("code"), "not_an_app")
        self.assertEqual(self.installer.runs, [])

    def test_the_state_carries_the_progress(self) -> None:
        self._app_install()
        self.installer.answer = {"phase": "model", "done_gb": 12.2, "total_gb": 20.0, "problem": "", "whole": False}
        self.assertEqual(
            self._state()["install"],
            {"phase": "model", "done_gb": 12.2, "total_gb": 20.0, "problem": "", "whole": False, "app": True},
        )

    def test_a_cell_is_not_built_on_a_fetch_that_is_not_whole(self) -> None:
        # PyTorch imports, so tools() says ready, but the model stopped half way. The redraw
        # would fetch the rest in silence under a locked cell.
        self._app_install()
        self.installer.answer = {"phase": "failed", "done_gb": 9.0, "total_gb": 15.9, "problem": "network", "whole": False}
        self.call("POST", f"{self.base}/api/cell", {"next": True})
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual((status, json.loads(body).get("code")), (409, "not_ready"))
        self.assertEqual(self.fake.calls, [])
        self.installer.answer["whole"] = True
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual(status, 200, body)

    def test_an_install_made_by_hand_is_built_on_whatever_the_fetch_says(self) -> None:
        self.installer.answer["whole"] = False
        self.call("POST", f"{self.base}/api/cell", {"next": True})
        status, _headers, body = self.call("POST", f"{self.base}/api/build", {})
        self.assertEqual(status, 200, body)

    def test_the_phase_of_a_tool_that_is_still_missing_is_done_again(self) -> None:
        self._app_install()
        missing = {"ready": False, "model_cached": False, "missing": ["torch", "bitsandbytes", "google_renderer", "odd"]}
        with patch("rutor.app.tools", return_value=missing):
            self._install()
            wait_until(lambda: bool(self.installer.runs))
        self.assertEqual(self.installer.runs, [("stack", "renderer")])

    def test_the_tools_are_not_asked_about_while_they_are_fetched(self) -> None:
        # Asking starts a child that imports PyTorch. Under pip that child would hold files
        # pip is writing. When the fetch has ended they are asked about at once.
        self._app_install()
        with patch("rutor.app.tools", return_value={"ready": False, "model_cached": False, "missing": ["torch"]}) as asked:
            self._state()
            self._hold_install()
            before = asked.call_count
            for _ in range(3):
                self.server._known._tools = None
                self.assertEqual(self._state()["tools"], {"ready": False, "model_cached": False, "missing": []})
            self.assertEqual(asked.call_count, before)
            asked.return_value = dict(_TOOLS)
            self.installer.go.set()
            wait_until(lambda: self._state()["tools"] == _TOOLS)

    def test_stopping_the_fetch_answers_at_once(self) -> None:
        self._app_install()
        self._hold_install()
        status, _headers, body = self.call("POST", f"{self.base}/api/install/stop", {})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(self.installer.stops, 1)
        # A stop with nothing running is not a fault.
        wait_until(lambda: not self.installer.running())
        status, _headers, _body = self.call("POST", f"{self.base}/api/install/stop", {})
        self.assertEqual(status, 200)

    def test_closing_the_page_stops_the_fetch(self) -> None:
        self._app_install()
        self._hold_install()
        self.server.stop()
        self.assertEqual(self.installer.stops, 1)
        self.assertFalse(self.installer.running())

    def test_a_page_that_stops_asking_does_not_end_the_fetch(self) -> None:
        # A browser puts a tab nobody looks at to sleep, and the page says that the machine
        # can be used meanwhile. A fetch holds no card and no lock, so silence does not stop it.
        now = [1000.0]
        installer = FakeInstaller()
        builder = app.Builder(FakeStart(), clock=lambda: now[0])
        with patch("rutor.app.TICK_SECONDS", 0.02):
            server = app.AppServer(builder, releases=list, installer=installer)
            self.addCleanup(server.stop)
            self.addCleanup(installer.go.set)
            server.start()
            self._app_install()
            installer.go.clear()
            connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
            connection.request("POST", f"/{server.key}/api/install", body=b"{}", headers={"Content-Type": "application/json"})
            self.assertEqual(connection.getresponse().status, 200)
            connection.close()
            wait_until(installer.running)
            # A hidden tab asks once a minute. That is not a page that has gone.
            now[0] += 100.0
            time.sleep(0.1)
            self.assertEqual(installer.stops, 0)
            now[0] += app.GONE_SECONDS + 3600.0
            time.sleep(0.15)
            self.assertEqual(installer.stops, 0)
            self.assertTrue(installer.running())

    def test_the_report_says_how_the_fetch_went(self) -> None:
        self._app_install()
        home = str(Path.home())
        self.installer.answer = {"phase": "failed", "done_gb": 3.1, "total_gb": 15.9, "problem": "network"}
        self.installer.said = ["Collecting torch", f"ERROR: could not write {home}/AppData/x"]
        self.machine_mock.return_value = {**_MACHINE, "driver": "616.56"}
        status, _headers, body = self.call("GET", f"{self.base}/api/report")
        self.assertEqual(status, 200, body)
        text = body.decode("utf-8")
        self.assertIn("driver: 616.56", text)
        self.assertIn("app: 0.1.0", text)
        self.assertIn("install: failed", text)
        self.assertIn("install problem: network", text)
        self.assertIn("  Collecting torch", text)
        self.assertIn("~/AppData/x", text)
        self.assertNotIn(home, text)

    def test_a_report_is_written_when_the_system_will_not_name_the_user(self) -> None:
        # getpass raises KeyError on some Pythons and OSError on others when no name is known.
        for fault in (KeyError("uid"), OSError("no name")):
            with patch("getpass.getuser", side_effect=fault):
                status, _headers, body = self.call("GET", f"{self.base}/api/report")
            self.assertEqual(status, 200, body)
            self.assertIn(b"Tech Embassy Pixel report", body)

    def test_next_on_a_sheet_that_just_filled_up_says_none_is_free(self) -> None:
        # The page knew of a free cell a moment ago. Someone took the last one since.
        self._state()
        stored = self.svc.state["cells"]
        assert isinstance(stored, dict)
        for cell in stored.values():
            cell["state"] = "live"
        status, _headers, body = self.call("POST", f"{self.base}/api/cell", {"next": True})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body).get("code"), "none_free")
        self.assertIsNone(self.builder.chosen())

    def test_the_state_carries_the_app_version(self) -> None:
        patcher = patch("rutor.config.app_install", return_value={"version": "0.1.0", "lock_sha256": "ab" * 32})
        patcher.start()
        self.addCleanup(patcher.stop)
        builder = app.Builder(FakeStart())
        self.addCleanup(builder.shutdown)
        server = app.AppServer(builder, releases=lambda: [{"tag_name": "app-v0.2.0", "html_url": RELEASE_URL}])
        self.addCleanup(server.stop)
        server.start()

        def app_answer() -> dict[str, object]:
            version = server._state()["version"]
            assert isinstance(version, dict)
            found = version["app"]
            assert isinstance(found, dict)
            return found

        # GitHub is asked on the server's own thread, so the page comes up before it answers.
        wait_until(lambda: app_answer().get("latest") == "0.2.0")
        self.assertEqual(
            app_answer(),
            {"mine": "0.1.0", "latest": "0.2.0", "current": False, "url": RELEASE_URL},
        )
        # The pipeline's own three keys are not touched by the app's answer.
        version = server._state()["version"]
        self.assertIsInstance(version, dict)
        self.assertEqual((version["mine"], version["current"]), (pipeline.PIPELINE, True))

    def test_a_page_that_stops_asking_ends_the_job_without_anyone_pressing(self) -> None:
        # The whole chain: nobody asks for the state, the server's own timer notices, the
        # child is stopped and the lock is given back.
        self.now = 0.0
        fake = FakeStart()
        builder = app.Builder(fake, clock=lambda: self.now)
        fake.script("claim", ["claimed: stockholm-norr 0_0 expires 2026-11-25T12:00"])
        fake.script("render", ["note: this takes a while"], hold=True)
        with patch("rutor.app.TICK_SECONDS", 0.02):
            server = app.AppServer(builder)
            self.addCleanup(server.stop)
            server.start()
            builder.choose("stockholm-norr", 0, 0)
            builder.build()
            wait_until(lambda: builder.job_state()["phase"] == "render")
            self.now += app.GONE_SECONDS + 1
            wait_until(lambda: builder.job_state()["phase"] == "stopped" and builder.job_state()["held"] is False)
        self.assertTrue(fake.runs[1].stopped)
        self.assertEqual(fake.calls[-1][0], ["release", "stockholm-norr", "0_0"])

    def test_a_body_that_is_not_what_the_route_takes_is_refused(self) -> None:
        status, _headers, _body = self.call("POST", f"{self.base}/api/terms", {"accept": "yes"})
        self.assertEqual(status, 400)
        status, _headers, _body = self.call("POST", f"{self.base}/api/cesium", {"token": ""})
        self.assertEqual(status, 400)
        status, _headers, _body = self.call("POST", f"{self.base}/api/cesium", {"token": 5})
        self.assertEqual(status, 400)
        status, _headers, _body = self.call("POST", f"{self.base}/api/cesium", b"[" * 3000)
        self.assertEqual(status, 400)

    def test_the_command_is_registered_with_its_swedish_name(self) -> None:
        for argv in (["app", "--help"], ["bygg", "--help"]):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("--no-open", stdout.getvalue())

    def test_the_page_files_ship_with_the_package(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        shipped = tomllib.loads(text)["tool"]["setuptools"]["package-data"]["rutor"]
        self.assertIn("app_page/*", shipped)


def _load_start_script() -> object:
    """installer/start.pyw as a module. It is a file the shortcut runs, not part of the package."""
    import importlib.machinery
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "installer" / "start.pyw"
    loader = importlib.machinery.SourceFileLoader("pixelbygget_start", str(path))
    spec = importlib.util.spec_from_loader("pixelbygget_start", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class WindowlessTests(_OwnHome):
    """The app as the shortcut starts it: no terminal, one instance, and a way to close it."""

    def setUp(self) -> None:
        super().setUp()
        for target, value in (
            ("rutor.app.machine", dict(_MACHINE)),
            ("rutor.app.tools", dict(_TOOLS)),
            ("rutor.app.price", dict(_PRICE)),
            ("rutor.app.cesium", {"set": True, "ok": True, "problem": ""}),
            ("rutor.app._search_open_sheet", app._no_sheet("none_free")),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = 1000.0
        self.fake = FakeStart()
        self.builder = app.Builder(self.fake, clock=lambda: self.now)
        self.builder.accept_terms()
        self.builder._login = "klang"
        self.builder._looked = True
        self.opened: list[str] = []
        browser = patch("webbrowser.open", side_effect=self.opened.append)
        browser.start()
        self.addCleanup(browser.stop)

    def _serving(self, **options: object) -> tuple[app.AppServer, threading.Thread]:
        """A server whose serve_forever runs on a thread, as cmd_app runs it on the main one."""
        server = app.AppServer(self.builder, releases=list, installer=FakeInstaller(), **options)  # type: ignore[arg-type]

        def serve() -> None:
            # As cmd_app does: when the serving ends, the server is closed and its port with it.
            # A port left listening still takes a connection on Linux, and then never answers.
            try:
                server.serve_forever()
            finally:
                server.stop()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def end() -> None:
            if thread.is_alive():
                server.end_serving()
                thread.join(timeout=10)
            server.stop()

        self.addCleanup(end)
        wait_until(lambda: self._get(server, "api/alive")[0] == 200)
        return server, thread

    @staticmethod
    def _get(server: app.AppServer, rest: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            connection.request("GET", f"/{server.key}/{rest}")
            response = connection.getresponse()
            return response.status, response.read()
        except OSError:
            return 0, b""
        finally:
            connection.close()

    @staticmethod
    def _post(server: app.AppServer, rest: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
        try:
            connection.request("POST", f"/{server.key}/{rest}", body=b"{}", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    @staticmethod
    def _args(*, quiet: bool = False, no_open: bool = False) -> object:
        import argparse

        return argparse.Namespace(quiet=quiet, no_open=no_open, quit=False)

    def test_the_installer_can_ask_a_running_app_to_end(self) -> None:
        import argparse

        self.fake.script("claim", ["claimed: stockholm-norr 0_0 expires 2026-11-25T12:00"])
        self.fake.script("render", ["note: this takes a while"], hold=True)
        server, thread = self._serving()
        app._write_running(server.url)
        with patch("rutor.app.SHEET_KINDS", {"stockholm-norr": "google"}):
            self.builder.choose("stockholm-norr", 0, 0)
            self.builder.build()
            wait_until(lambda: self.builder.job_state()["phase"] == "render")
            with patch("rutor.app.AppServer", side_effect=AssertionError("a server was made")):
                code = app.cmd_app(argparse.Namespace(quiet=True, no_open=True, quit=True))
        self.assertEqual(code, 0)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        # It was closed the way the page closes it: the job stopped, the lock given back.
        self.assertTrue(self.fake.runs[1].stopped)
        self.assertEqual(self.fake.calls[-1][0], ["release", "stockholm-norr", "0_0"])
        self.assertEqual(self.opened, [])

    def test_ending_an_app_that_is_not_running_is_no_fault(self) -> None:
        import argparse

        with patch("rutor.app.AppServer", side_effect=AssertionError("a server was made")):
            self.assertEqual(app.cmd_app(argparse.Namespace(quiet=True, no_open=True, quit=True)), 0)
        self.assertEqual(self.opened, [])

    def test_a_second_start_opens_the_first_ones_page(self) -> None:
        server, _thread = self._serving()
        self.assertTrue(app._write_running(server.url))
        self.assertEqual(app.running_page(), server.url)
        with patch("rutor.app.AppServer", side_effect=AssertionError("a second server was made")):
            self.assertEqual(app.cmd_app(self._args(quiet=True)), 0)
        self.assertEqual(self.opened, [server.url])
        # The first one's file is not the second one's to remove.
        self.assertTrue(app.running_path().is_file())

    def test_a_second_start_that_may_not_open_a_browser_prints_the_address(self) -> None:
        server, _thread = self._serving()
        app._write_running(server.url)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch("rutor.app.AppServer", side_effect=AssertionError("second")):
            self.assertEqual(app.cmd_app(self._args(no_open=True)), 0)
        self.assertEqual(self.opened, [])
        self.assertIn(f"url: {server.url}", out.getvalue())

    def test_a_file_left_by_a_dead_process_is_ignored(self) -> None:
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        path = app.running_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": gone.pid, "url": "http://127.0.0.1:9/" + "k" * 43 + "/"}), encoding="utf-8")
        self.assertIsNone(app.running_page())
        self.assertFalse(path.exists())
        # And a new server starts, writes its own file and takes it away again.
        seen: list[dict[str, object]] = []

        def serve(server: app.AppServer) -> None:
            seen.append(json.loads(path.read_text(encoding="utf-8")))

        with patch.object(app.AppServer, "serve_forever", serve):
            self.assertEqual(app.cmd_app(self._args(quiet=True)), 0)
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertEqual(self.opened, [seen[0]["url"]])
        self.assertFalse(path.exists())

    def test_a_live_process_that_is_not_the_app_is_not_the_app(self) -> None:
        # The pid was given to another program after the app died, and nothing answers.
        path = app.running_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:9/" + "k" * 43 + "/"}), encoding="utf-8")
        self.assertIsNone(app.running_page())
        self.assertFalse(path.exists())

    def test_a_first_start_that_is_slow_to_answer_is_still_the_app(self) -> None:
        # The machine is busy drawing and the person clicks the icon again. An app that is
        # alive and listening is not replaced because it took its time.
        path = app.running_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        url = "http://127.0.0.1:9/" + "k" * 43 + "/"
        path.write_text(json.dumps({"pid": os.getpid(), "url": url}), encoding="utf-8")
        with patch("rutor.app._answers", return_value="slow") as asked, patch("time.sleep"):
            self.assertEqual(app.running_page(), url)
        self.assertGreaterEqual(asked.call_count, 3)
        self.assertTrue(path.exists())

    def test_an_address_of_another_shape_is_never_asked(self) -> None:
        path = app.running_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in (
            json.dumps({"pid": os.getpid(), "url": "http://evil.test/" + "k" * 43 + "/"}),
            json.dumps({"pid": os.getpid(), "url": "https://127.0.0.1:9/" + "k" * 43 + "/"}),
            json.dumps({"pid": os.getpid(), "url": "http://127.0.0.1:9/short/"}),
            json.dumps({"pid": "x", "url": "http://127.0.0.1:9/" + "k" * 43 + "/"}),
            json.dumps([1, 2]),
            "not json",
        ):
            with self.subTest(content=content[:40]):
                path.write_text(content, encoding="utf-8")
                with patch("rutor.app._answers", side_effect=AssertionError("it was asked")):
                    self.assertIsNone(app.running_page())
                self.assertFalse(path.exists())

    def test_a_start_that_loses_the_race_opens_the_winners_page(self) -> None:
        server, _thread = self._serving()
        made: list[app.AppServer] = []
        real = app.AppServer

        def second(*args: object, **options: object) -> app.AppServer:
            # The first start writes its file just as the second has looked and found none.
            other = real(self.builder, releases=list, installer=FakeInstaller())  # type: ignore[arg-type]
            made.append(other)
            app._write_running(server.url)
            return other

        with patch("rutor.app.AppServer", side_effect=second):
            self.assertEqual(app.cmd_app(self._args(quiet=True)), 0)
        self.assertEqual(self.opened, [server.url])
        self.assertEqual(json.loads(app.running_path().read_text(encoding="utf-8"))["url"], server.url)
        # The loser's port is closed again.
        with self.assertRaises(OSError):
            http.client.HTTPConnection("127.0.0.1", made[0].port, timeout=2).request("GET", "/")

    def test_quit_stops_the_job_and_ends(self) -> None:
        self.fake.script("claim", ["claimed: stockholm-norr 0_0 expires 2026-11-25T12:00"])
        self.fake.script("render", ["note: this takes a while"], hold=True)
        server, thread = self._serving()
        with patch("rutor.app.SHEET_KINDS", {"stockholm-norr": "google"}):
            self.builder.choose("stockholm-norr", 0, 0)
            self.builder.build()
            wait_until(lambda: self.builder.job_state()["phase"] == "render")
            status, body = self._post(server, "api/quit")
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertTrue(self.fake.runs[1].stopped)
        self.assertEqual(self.fake.calls[-1][0], ["release", "stockholm-norr", "0_0"])
        self.assertEqual(self.builder.job_state()["held"], False)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

    def test_quit_stops_a_fetch(self) -> None:
        installer = FakeInstaller()
        installer.go.clear()
        self.addCleanup(installer.go.set)
        server = app.AppServer(self.builder, releases=list, installer=installer)  # type: ignore[arg-type]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.stop)
        wait_until(lambda: self._get(server, "api/alive")[0] == 200)
        with patch("rutor.app_install.is_app", return_value=True):
            self.assertEqual(self._post(server, "api/install")[0], 200)
            wait_until(installer.running)
            self.assertEqual(self._post(server, "api/quit")[0], 200)
        self.assertEqual(installer.stops, 1)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

    def test_a_page_nobody_looks_at_ends_by_itself(self) -> None:
        with patch("rutor.app.TICK_SECONDS", 0.02):
            _server, thread = self._serving(idle_exit=600.0)
            self.now += 599.0
            time.sleep(0.15)
            self.assertTrue(thread.is_alive())
            self.now += 2.0
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

    def test_a_page_somebody_looks_at_stays(self) -> None:
        with patch("rutor.app.TICK_SECONDS", 0.02):
            server, thread = self._serving(idle_exit=600.0)
            for _ in range(3):
                self.now += 400.0
                self.assertEqual(self._get(server, "api/state")[0], 200)
                time.sleep(0.1)
            self.assertTrue(thread.is_alive())

    def test_it_never_ends_under_a_running_job(self) -> None:
        self.fake.script("claim", ["claimed: stockholm-norr 0_0 expires 2026-11-25T12:00"])
        self.fake.script("render", ["note: this takes a while"], hold=True)
        with patch("rutor.app.TICK_SECONDS", 0.02), patch("rutor.app.SHEET_KINDS", {"stockholm-norr": "google"}):
            _server, thread = self._serving(idle_exit=600.0)
            self.builder.choose("stockholm-norr", 0, 0)
            self.builder.build()
            wait_until(lambda: self.builder.job_state()["phase"] == "render")
            self.now += 601.0
            # The page has been gone for more than 150 s, so the job is stopped and its lock
            # given back. The app is still up: the ten minutes count from when it went idle.
            wait_until(lambda: self.builder.job_state()["phase"] == "stopped" and not self.builder.busy())
            time.sleep(0.15)
            self.assertTrue(thread.is_alive())
            self.assertEqual(self.fake.calls[-1][0], ["release", "stockholm-norr", "0_0"])
            self.now += 601.0
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

    def test_a_terminal_start_never_ends_by_itself(self) -> None:
        with patch("rutor.app.TICK_SECONDS", 0.02):
            _server, thread = self._serving()
            self.now += 100000.0
            time.sleep(0.15)
            self.assertTrue(thread.is_alive())

    def test_being_alive_is_not_being_looked_at(self) -> None:
        server, _thread = self._serving()
        self.now += 50.0
        status, body = self._get(server, "api/alive")
        self.assertEqual((status, json.loads(body)), (200, {"app": "pixelbygget"}))
        self.assertEqual(self.builder.quiet_for(), 50.0)

    def test_the_running_file_names_no_secret(self) -> None:
        config.write_token("github-login-token")
        config.write_cesium_token("ces.ium.token")
        server, _thread = self._serving()
        self.assertTrue(app._write_running(server.url))
        text = app.running_path().read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), {"pid": os.getpid(), "url": server.url})
        self.assertNotIn("github-login-token", text)
        self.assertNotIn("ces.ium.token", text)
        self.assertEqual(app.running_path(), Path(self.tmp.name) / "app-running.json")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(app.running_path().stat().st_mode), 0o600)

    def test_quiet_prints_nothing_and_logs_the_kind_of_a_fault(self) -> None:
        config.write_token("github-login-token")

        def serve(server: app.AppServer) -> None:
            print("app: OSError in tick", file=sys.stderr)
            print("something for a terminal")
            raise ValueError("github-login-token leaked in a message")

        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            patch.object(app.AppServer, "serve_forever", serve),
            self.assertRaises(ValueError),
        ):
            app.cmd_app(self._args(quiet=True))
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        log = Path(self.tmp.name) / "app.log"
        text = log.read_text(encoding="utf-8")
        self.assertIn("app: OSError in tick", text)
        self.assertIn("app: ValueError at test_app.py", text)
        self.assertNotIn("github-login-token", text)
        self.assertNotIn("something for a terminal", text)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertFalse(app.running_path().exists())

    def test_a_fault_on_another_thread_is_logged_by_its_kind(self) -> None:
        def serve(server: app.AppServer) -> None:
            def broken() -> None:
                raise RuntimeError("a line from outside")

            thread = threading.Thread(target=broken)
            thread.start()
            thread.join()

        with patch.object(app.AppServer, "serve_forever", serve):
            self.assertEqual(app.cmd_app(self._args(quiet=True)), 0)
        text = (Path(self.tmp.name) / "app.log").read_text(encoding="utf-8")
        self.assertIn("app: RuntimeError at test_app.py", text)
        self.assertNotIn("a line from outside", text)

    def test_the_log_does_not_grow_for_ever(self) -> None:
        log = Path(self.tmp.name) / "app.log"
        log.write_text("x" * (app.LOG_MAX_BYTES + 1), encoding="utf-8")
        with patch.object(app.AppServer, "serve_forever", lambda server: None):
            app.cmd_app(self._args(quiet=True))
        self.assertLess(log.stat().st_size, 1000)

    def test_without_quiet_the_terminal_is_told(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(app.AppServer, "serve_forever", lambda server: None):
            self.assertEqual(app.cmd_app(self._args()), 0)
        self.assertIn("url: http://127.0.0.1:", out.getvalue())
        self.assertIn("closed", out.getvalue())
        self.assertFalse((Path(self.tmp.name) / "app.log").exists())
        self.assertEqual(len(self.opened), 1)

    def test_quiet_is_an_option_of_the_command(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["app", "--help"]), 0)
        self.assertIn("--quiet", stdout.getvalue())

    def test_the_shortcut_starts_the_apps_own_python_without_a_window(self) -> None:
        start = _load_start_script()
        folder = Path(self.tmp.name) / "Åsa Öberg" / "Pixelbygget"
        command = start.command(folder)  # type: ignore[attr-defined]
        self.assertEqual(command, [str(folder / "python" / "python.exe"), "-m", "rutor", "app", "--quiet"])
        environ = {"PATH": "/x", "PYTHONHOME": "/their/python", "PYTHONPATH": "/their/packages", "USERPROFILE": "p"}
        made = start.environment(folder, environ)  # type: ignore[attr-defined]
        # The person's own Python setup does not reach the app's Python.
        self.assertNotIn("PYTHONHOME", made)
        self.assertEqual(made["PYTHONPATH"], str(folder / "cli"))
        self.assertEqual(made["PYTHONNOUSERSITE"], "1")
        self.assertEqual((made["PATH"], made["USERPROFILE"]), ("/x", "p"))
        self.assertEqual(environ["PYTHONHOME"], "/their/python")

    def test_the_shortcut_starts_one_hidden_process_and_ends(self) -> None:
        start = _load_start_script()
        with patch("subprocess.Popen") as popen:
            popen.return_value.wait.side_effect = subprocess.TimeoutExpired("python", 4)
            self.assertEqual(start.main(), 0)  # type: ignore[attr-defined]
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:], ["-m", "rutor", "app", "--quiet"])
        options = popen.call_args.kwargs
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        # 0x08000000 is CREATE_NO_WINDOW: a console nobody sees, which every child inherits.
        self.assertEqual(start.NO_WINDOW, 0x08000000)  # type: ignore[attr-defined]

    def test_a_start_that_dies_at_once_is_said_in_a_box(self) -> None:
        # There is no terminal to read a fault in. A double-click that does nothing is the
        # worst answer.
        start = _load_start_script()
        with patch("subprocess.Popen") as popen, patch.object(start, "_say") as say:
            popen.return_value.wait.return_value = 3
            self.assertEqual(start.main(), 1)  # type: ignore[attr-defined]
        say.assert_called_once()
        # An app that is still up after a few seconds started, and so did one that found
        # another already running and ended with 0.
        for outcome in (subprocess.TimeoutExpired("python", 4), 0):
            with patch("subprocess.Popen") as popen, patch.object(start, "_say") as say:
                if isinstance(outcome, int):
                    popen.return_value.wait.return_value = outcome
                else:
                    popen.return_value.wait.side_effect = outcome
                self.assertEqual(start.main(), 0)  # type: ignore[attr-defined]
            say.assert_not_called()


class PageFilesTests(unittest.TestCase):
    """The page's three files, read as text: what they may not hold and what they must."""

    def read(self, name: str) -> str:
        return (app.PAGE_DIR / name).read_text(encoding="utf-8")

    def test_the_page_loads_nothing_from_outside(self) -> None:
        for name in ("index.html", "app.css"):
            text = self.read(name)
            self.assertNotIn("http://", text, name)
            self.assertNotIn("https://", text, name)
            self.assertNotIn("//fonts.", text, name)
            self.assertNotIn("@import", text, name)
            self.assertNotIn("url(", text, name)
        script = self.read("app.js")
        # The five places a person is sent to: GitHub's device page, Cesium ion, the release
        # page of a newer app, the map's own site and the map of cells. Links, not loads.
        allowed = {
            "https://github.com/login/device",
            "https://ion.cesium.com",
            "https://github.com/opensverige/karta.opensverige-3d/releases/",
            "https://techembassy.se",
            "https://karta.opensverige.se/pixelbygget",
        }
        found = set(re.findall(r"https?://[^\s\"'`)]*", script))
        self.assertEqual(found - allowed, set())
        self.assertEqual(found, allowed)
        for loader in ("import(", "importScripts", "new Worker", "XMLHttpRequest", "WebSocket", "EventSource"):
            self.assertNotIn(loader, script, loader)

    def test_text_is_never_set_as_markup(self) -> None:
        script = self.read("app.js")
        for markup in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "DOMParser", "eval(", "new Function"):
            self.assertNotIn(markup, script, markup)
        page = self.read("index.html")
        # The policy stops inline script and style, so the page must not need them.
        for inline in ("<style", "style=", "onclick", "onload", "javascript:"):
            self.assertNotIn(inline, page, inline)
        self.assertEqual(len(re.findall(r"<script\b", page)), 1)
        self.assertIn('<script src="app.js" defer></script>', page)
        self.assertIn('<link rel="stylesheet" href="app.css">', page)

    def test_every_step_of_the_sketch_is_there(self) -> None:
        page = self.read("index.html")
        for number in range(1, 9):
            self.assertEqual(page.count(f'id="steg-{number}"'), 1, number)
        self.assertNotIn('id="steg-9"', page)
        self.assertIn('lang="sv"', page)

    def test_no_emoji_in_the_page(self) -> None:
        for name in ("index.html", "app.js", "app.css"):
            for ch in self.read(name):
                code = ord(ch)
                emoji = 0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x26FF or code in (0xFE0F, 0x200D)
                self.assertFalse(emoji, f"{name}: U+{code:04X}")

    def test_every_problem_word_has_a_swedish_sentence(self) -> None:
        script = self.read("app.js")
        words = (
            "mac", "no_card", "small_card", "disk", "old_driver", "no_rocm",
            "missing", "refused", "twice", "no_access", "unreachable", "broken",
            "expired", "network",
            "taken", "machine", "check", "failed", "locks", "waiting", "lock_left",
            "daily", "old_app", "quota", "token", "flat", "old_lock", "denied",
            "memory",
            "busy", "no_cell", "not_ready", "terms", "none_free",
        )
        for word in words:
            self.assertRegex(script, rf"\b{word}: \"", word)
        self.assertIn("Något gick fel. Försök igen.", script)

    def test_the_page_says_what_a_reader_takes_it_to_say(self) -> None:
        page = self.read("index.html")
        script = self.read("app.js")
        # "Lediga rutor saknas på kartan" reads as "no cell is free", above a map of free ones.
        self.assertNotIn("Lediga rutor saknas", page)
        # The model is 12 GB. The page said 20.
        self.assertNotIn("20 GB", page)
        self.assertNotIn("20 GB", script)
        # The terms are in English, so what a person most needs to know is said in Swedish
        # too: unpaid and at their own risk, their name is shown, and the choice about Google.
        for said in ("Frivilligt och obetalt", "Ditt GitHub-namn visas öppet", "Googles regler tillåter inte det", "Den engelska texten är den som gäller"):
            self.assertIn(said, page)
        # The line about Google is about the source, not about Stockholm: a cell may lie anywhere.
        self.assertIn("Allt utom Göteborg ritas från Googles 3D-kartor, genom ditt eget Cesium-konto.", page)
        self.assertNotIn("Stockholm ritas från Googles", page)
        # A fetch whose bytes did not match can be tried again: the bad file is gone.
        self.assertNotIn('i.problem === "changed"', script)
        # A stop during the upload is said to wait for it, and a lock from before is named.
        self.assertIn("Skickar in klart först.", script)
        self.assertIn('"Låst sedan förut: " + j.old', script)
        self.assertIn('slut && !["daily", "old_lock"].includes(j.problem)', script)
        for said in ("bekostnad och risk", "för forskning och utvärdering, inte kommersiellt", "Google kan stänga din åtkomst"):
            self.assertIn(said, page)
        # No "try again" where it would get the same answer.
        self.assertIn('!["taken", "daily", "old_app", "flat"].includes(j.problem)', script)
        # A cell that waits is said to wait, and where it shows.
        self.assertIn("Tills dess syns den som Väntar.", page)
        # And is thanked: every cell is welcome, also one that is not perfect.
        self.assertIn("Tack. Varje ruta hjälper, och den behöver inte vara perfekt.", page)

    def test_step_six_takes_a_cell_by_its_code_from_the_map(self) -> None:
        page = self.read("index.html")
        script = self.read("app.js")
        # The field, with its label, and the two ways on: use the code, or go and fetch one.
        self.assertIn('<label for="kod">Klistra in rutans kod</label>', page)
        self.assertRegex(page, r'<input id="kod" type="text"[^>]*maxlength="100"')
        self.assertRegex(page, r'<button[^>]*id="kod-anvand"[^>]*>Använd den →</button>')
        self.assertRegex(page, r'<a[^>]*id="kod-karta"[^>]*>Välj på kartan →</a>')
        self.assertRegex(page, r'<a[^>]*id="kod-karta"[^>]*rel="noopener noreferrer"')
        # The address is set by the script, like every link the page has, and is the map of cells.
        self.assertIn('const KARTAN = "https://karta.opensverige.se/pixelbygget";', script)
        self.assertIn('el("kod-karta").setAttribute("href", KARTAN);', script)
        self.assertIn('"api/cell", { code: ', script)
        # The two things the app can answer a code with, each in a sentence.
        block = script[script.index("  svar: {") : script.index("}", script.index("  svar: {"))]
        self.assertIn('bad_code: "Koden ser inte rätt ut. Kopiera den från kartan igen."', block)
        self.assertRegex(block, r"\btaken: \"")
        # The field is there also when no sheet has a free cell: that is when it is needed most.
        step = page[page.index('id="steg-6"') : page.index('id="steg-7"')]
        self.assertLess(step.index('id="ruta-fel"'), step.index('id="kod"'))
        self.assertIn("visa(\"kod-del\", true);", script)
        # The Cesium step no longer says that the picture is of Stockholm.
        self.assertNotIn("Kartbilden över Stockholm", page)

    def test_a_step_the_page_moved_to_by_itself_gets_the_focus(self) -> None:
        script = self.read("app.js")
        self.assertIn("if (visat !== fokuserat)", script)
        # And the step line of the narrow layout is not hidden from a screen reader.
        self.assertNotIn('class="mobilsteg" aria-hidden', self.read("index.html"))

    def test_step_five_has_a_bar_and_a_button(self) -> None:
        page = self.read("index.html")
        for wanted in ('id="hamta"', 'id="hamta-matare"', 'id="hamta-avbryt"', 'role="progressbar"'):
            self.assertIn(wanted, page)
        self.assertIn(">Hämta →<", page)
        self.assertIn(">Avbryt<", page)

    def test_step_five_is_done_only_when_the_fetch_is_whole(self) -> None:
        script = self.read("app.js")
        self.assertIn("s.install.whole === true", script)
        self.assertNotIn("Hämtningen stannar om du stänger den", self.read("index.html"))

    def test_the_fetch_is_said_in_swedish(self) -> None:
        script = self.read("app.js")
        for sentence in (
            "Hämtar PyTorch",
            "Hämtar Node",
            "Hämtar kartverktyget",
            "Hämtar ritmodellen",
        ):
            self.assertIn(sentence, script)
        # Each word the install job can end with has a sentence of its own.
        block = script[script.index("  hamtning: {") : script.index("}", script.index("  hamtning: {"))]
        for word in ("disk", "network", "changed", "failed"):
            self.assertRegex(block, rf"\b{word}: \"", word)
        for word in ("not_an_app",):
            self.assertRegex(script, rf"\b{word}: \"", word)
        # The size is the manifest's, not a number written into the page.
        self.assertNotIn("25 GB", script)

    def test_the_page_has_a_way_to_close_the_app(self) -> None:
        page = self.read("index.html")
        self.assertEqual(page.count(">Stäng appen<"), 2)
        self.assertIn('id="stangd"', page)
        self.assertIn("Appen stängs. Du kan stänga fliken.", page)
        self.assertIn('"api/quit"', self.read("app.js"))

    def test_the_page_carries_its_name_and_what_it_is_for(self) -> None:
        # "Pixelbygget" is the project's own word. A person sees the name that points at the
        # site, one sentence about what the program does, and where the map is.
        page = self.read("index.html")
        self.assertIn("<title>Tech Embassy Pixel</title>", page)
        self.assertIn("<h1>Tech Embassy Pixel</h1>", page)
        self.assertIn("Din dator ritar en ruta av Sveriges pixelkarta.", page)
        # The address itself is set by the script, like every link the page has.
        self.assertIn('id="syfte-lank"', page)
        self.assertIn('const SAJTEN = "https://techembassy.se";', self.read("app.js"))
        for name in ("index.html", "app.js"):
            self.assertNotIn("Pixelbygget", self.read(name), name)

    def test_the_page_can_say_a_newer_app_is_out(self) -> None:
        page = self.read("index.html")
        self.assertIn('id="nyversion"', page)
        self.assertIn('id="nyversion-lank"', page)
        self.assertIn('rel="noopener noreferrer"', page)

    def test_a_sentence_in_the_page_is_at_most_twelve_words(self) -> None:
        page = re.sub(r"<[^>]+>", "\n", self.read("index.html"))
        script_strings = re.findall(r'"([^"\n]*[a-zåäö][^"\n]*[.?!])"', self.read("app.js"))
        for chunk in [*page.split("\n"), *script_strings]:
            for sentence in re.split(r"(?<=[.?!])\s+", chunk.strip()):
                words = [w for w in sentence.split() if re.search(r"[A-Za-zÅÄÖåäö0-9]", w)]
                self.assertLessEqual(len(words), 12, sentence)


if __name__ == "__main__":
    unittest.main()
