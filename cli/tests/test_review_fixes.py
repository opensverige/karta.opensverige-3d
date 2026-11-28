"""The review fixes: network errors, OSM caching, manifest ties, pins and seeds.

No test here needs a network or a GPU. HTTP is either injected or patched, and
torch and diffusers are faked the way test_placement.py does it.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.metadata
import inspect
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rutor import api, commands, config, pipeline, redraw_core, se3_price
from rutor.__main__ import main
from rutor.config import write_token
from rutor.errors import RutorError
from rutor.manifest import sha256_file
from rutor.render import camera_for
from rutor.renderer import sources
from rutor.login import TERMS_VERSION, keep_terms
from tests.fake_service import FakeService
from tests.png import minimal_png

SHEET = "goteborg-norr"
CELL = "64_59"
PINNED_DATE = "2026-11-15T00:00:00Z"


def _drawing_available() -> bool:
    try:
        import numpy
        from PIL import Image
    except ImportError:
        return False
    del numpy, Image
    return True


DRAWING = _drawing_available()


def _sheet() -> dict[str, object]:
    return {
        "id": SHEET,
        "name": "Göteborg",
        "state": "open",
        "cols": 128,
        "rows": 128,
        "origin_lat": 57.707,
        "origin_lon": 11.967,
        "x_meters": -16384,
        "y_meters": 13312,
        "ground_height": 40,
        "heading": 0,
        "pipeline": "2026.11-a",
    }


def _tile_png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (sources.ORTHO_TILE, sources.ORTHO_TILE), (40, 90, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


class _CacheFetch:
    """A fetch that answers the WMS with a tile and Overpass with an empty answer."""

    def __init__(self) -> None:
        self.wms = 0
        self.overpass = 0

    def __call__(self, url: str, data: bytes | None = None, timeout: float = 120.0) -> tuple[int, bytes]:
        del url, timeout
        if data is None:
            self.wms += 1
            return 200, _tile_png()
        self.overpass += 1
        return 200, b'{"elements":[]}'


class _FakeImage:
    """Enough of a PIL image for cmd_redraw: save writes a real PNG."""

    def save(self, path: object, *args: object, **kwargs: object) -> None:
        del args, kwargs
        Path(str(path)).write_bytes(minimal_png(1536, 1536))


class _FakePipe:
    """The parts of the Qwen pipeline that _load_pipe touches."""

    def __init__(self, lora_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, object, dict[str, object]]] = []
        self.vae = types.SimpleNamespace()
        self.lora_error = lora_error

    def load_lora_weights(self, name: str, **kwargs: object) -> None:
        self.calls.append(("lora", name, kwargs))
        if self.lora_error is not None:
            raise self.lora_error

    def to(self, device: str) -> None:
        self.calls.append(("to", device, {}))

    def set_progress_bar_config(self, **kwargs: object) -> None:
        self.calls.append(("progress", None, kwargs))


def _fake_model_modules(pipe: _FakePipe) -> object:
    torch_mod = types.ModuleType("torch")
    torch_mod.bfloat16 = "bfloat16"  # type: ignore[attr-defined]
    diffusers_mod = types.ModuleType("diffusers")

    class _Pipeline:
        @staticmethod
        def from_pretrained(model: str, **kwargs: object) -> _FakePipe:
            pipe.calls.append(("model", model, kwargs))
            return pipe

    diffusers_mod.QwenImage21Pipeline = _Pipeline  # type: ignore[attr-defined]
    return patch.dict(sys.modules, {"torch": torch_mod, "diffusers": diffusers_mod})


class ServiceCase(unittest.TestCase):
    """A temp work directory, a fake service and a token, as the other command tests."""

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

    def work(self) -> Path:
        return self.root / "work" / SHEET

    def manifest(self) -> dict[str, object]:
        return json.loads((self.work() / f"{CELL}.manifest.json").read_text(encoding="utf-8"))

    def redraw(self, extra: list[str] | None = None) -> tuple[int, str, str, dict[str, object]]:
        """Run cmd_redraw with the model and Pillow faked out."""
        self.svc.state["sheets"] = {SHEET: _sheet()}
        work = self.work()
        work.mkdir(parents=True, exist_ok=True)
        (work / f"{CELL}.src.png").write_bytes(minimal_png(1536, 1536))
        seen: dict[str, object] = {}

        def fake_redraw(image: object, prompt: str, seed: int | None) -> _FakeImage:
            del image, prompt
            seen["seed"] = seed
            return _FakeImage()

        with (
            patch.object(redraw_core, "preflight_message", return_value=None),
            patch.object(redraw_core, "redraw_image", side_effect=fake_redraw),
            patch.object(commands, "_open_image", lambda path: object()),
            patch.object(commands, "check_problems", return_value=[]),
        ):
            code, out, err = self.run_cmd(["redraw", SHEET, CELL] + list(extra or []))
        return code, out, err, seen


class NetworkErrorTests(ServiceCase):
    def test_api_read_timeout_is_exit_4(self) -> None:
        with patch("rutor.api._open", side_effect=TimeoutError("read timed out")):
            with self.assertRaises(RutorError) as caught:
                api.get("/v1/health")
        self.assertEqual(caught.exception.code, 4)
        self.assertTrue(caught.exception.message.startswith("could not reach the service ("))

    def test_api_broken_http_stream_is_exit_4(self) -> None:
        with patch("rutor.api._open", side_effect=http.client.BadStatusLine("boom")):
            with self.assertRaises(RutorError) as caught:
                api.get("/v1/health")
        self.assertEqual(caught.exception.code, 4)

    def test_http_request_reports_a_timeout_as_a_retryable_502(self) -> None:
        with patch("rutor.renderer.sources.urllib.request.urlopen", side_effect=TimeoutError("read timed out")):
            self.assertEqual(sources.http_request("https://overpass.test/api", b"data=q"), (502, b""))

    def test_overpass_connection_error_is_retried_then_exit_4(self) -> None:
        calls: list[int] = []

        def boom(*args: object, **kwargs: object) -> object:
            del args, kwargs
            calls.append(1)
            raise ConnectionError("reset by peer")

        with (
            patch("rutor.renderer.sources.urllib.request.urlopen", side_effect=boom),
            patch("rutor.renderer.sources.time.sleep"),
        ):
            with self.assertRaises(RutorError) as caught:
                sources.fetch_overpass("[out:json];out;")
        self.assertEqual(len(calls), sources.ATTEMPTS)
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("gave up after 5 tries", caught.exception.message)


class OverpassAnswerTests(ServiceCase):
    def test_an_answer_that_is_not_osm_json_is_retried(self) -> None:
        calls: list[int] = []

        def fetch(url: str, data: bytes | None = None, timeout: float = 120.0) -> tuple[int, bytes]:
            del url, data, timeout
            calls.append(1)
            return 200, b"<html>Overpass is busy</html>"

        with patch("rutor.renderer.sources.time.sleep"):
            with self.assertRaises(RutorError) as caught:
                sources.fetch_overpass("[out:json];out;", fetch=fetch)
        self.assertEqual(len(calls), sources.ATTEMPTS)
        self.assertEqual(caught.exception.code, 4)

    def test_an_overpass_remark_error_is_a_failed_fetch(self) -> None:
        remark = json.dumps({"remark": "runtime error: Query timed out in json", "elements": []}).encode("utf-8")
        calls = {"n": 0}

        def fetch(url: str, data: bytes | None = None, timeout: float = 120.0) -> tuple[int, bytes]:
            del url, data, timeout
            calls["n"] += 1
            if calls["n"] == 1:
                return 200, remark
            return 200, b'{"elements":[]}'

        with patch("rutor.renderer.sources.time.sleep"):
            payload = sources.fetch_overpass("[out:json];out;", fetch=fetch)
        self.assertEqual(payload, b'{"elements":[]}')
        self.assertEqual(calls["n"], 2)

    def test_an_answer_without_elements_is_not_a_success(self) -> None:
        def fetch(url: str, data: bytes | None = None, timeout: float = 120.0) -> tuple[int, bytes]:
            del url, data, timeout
            return 200, b'{"version": 0.6}'

        with patch("rutor.renderer.sources.time.sleep"):
            with self.assertRaises(RutorError) as caught:
                sources.fetch_overpass("[out:json];out;", fetch=fetch)
        self.assertEqual(caught.exception.code, 4)

    @unittest.skipUnless(DRAWING, "pillow and numpy are not installed")
    def test_a_broken_osm_cache_is_deleted_and_fetched_again(self) -> None:
        fetch = _CacheFetch()
        camera = camera_for(_sheet(), 64, 59)
        _orto, osm_path, _trees = sources.load_sources(SHEET, 64, 59, camera, fetch=fetch)
        before = fetch.overpass
        osm_path.write_text("[]", encoding="utf-8")
        again_orto, again_osm, _again_trees = sources.load_sources(SHEET, 64, 59, camera, fetch=fetch)
        self.assertEqual(fetch.overpass, before + 1)
        self.assertEqual(again_osm, osm_path)
        self.assertIsInstance(json.loads(again_osm.read_text(encoding="utf-8")), dict)
        self.assertTrue(again_orto.is_file())


class OsmDateTests(ServiceCase):
    def test_both_sheets_are_pinned(self) -> None:
        self.assertEqual(
            sources.OSM_DATES,
            {"goteborg-norr": PINNED_DATE, "stockholm-norr": PINNED_DATE},
        )

    def test_a_sheet_query_carries_the_date(self) -> None:
        buildings = sources.buildings_query(57.70, 11.96, 57.71, 11.97, date=PINNED_DATE)
        trees = sources.trees_query(57.70, 11.96, 57.71, 11.97, date=PINNED_DATE)
        header = f'[out:json][timeout:90][date:"{PINNED_DATE}"];\n'
        self.assertTrue(buildings.startswith(header))
        self.assertTrue(trees.startswith(header))
        nodes = sources.tree_nodes_query(57.70, 11.96, 57.71, 11.97, date=PINNED_DATE)
        self.assertIn(f'(if: timestamp() < "{PINNED_DATE}")', nodes)

    def test_a_query_without_a_date_keeps_the_plain_header(self) -> None:
        self.assertTrue(sources.buildings_query(57.70, 11.96, 57.71, 11.97).startswith("[out:json][timeout:90];\n"))

    @unittest.skipUnless(DRAWING, "pillow and numpy are not installed")
    def test_a_sheet_without_a_date_is_exit_3(self) -> None:
        def fetch(url: str, data: bytes | None = None, timeout: float = 120.0) -> tuple[int, bytes]:
            del url, data, timeout
            raise AssertionError("no fetch when the date is missing")

        with patch.dict(sources.OSM_DATES, {}, clear=True):
            with self.assertRaises(RutorError) as caught:
                sources.load_sources(SHEET, 64, 59, camera_for(_sheet(), 64, 59), fetch=fetch)
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(caught.exception.message, f"no OpenStreetMap date pinned for sheet {SHEET}")

    @unittest.skipUnless(DRAWING, "pillow and numpy are not installed")
    def test_the_cache_name_carries_the_date_and_a_new_date_refetches(self) -> None:
        fetch = _CacheFetch()
        camera = camera_for(_sheet(), 64, 59)
        _orto, first_osm, _trees = sources.load_sources(SHEET, 64, 59, camera, fetch=fetch)
        before = fetch.overpass
        self.assertIn("2026-11-15T00-00-00Z", first_osm.name)
        with patch.dict(sources.OSM_DATES, {SHEET: "2026-12-01T00:00:00Z"}):
            _orto, second_osm, _trees = sources.load_sources(SHEET, 64, 59, camera, fetch=fetch)
        # Buildings, tree areas and single trees.
        self.assertEqual(fetch.overpass, before + 3)
        self.assertIn("2026-12-01T00-00-00Z", second_osm.name)
        self.assertNotEqual(first_osm.name, second_osm.name)


class CacheKeyTests(ServiceCase):
    @unittest.skipUnless(DRAWING, "pillow and numpy are not installed")
    def test_the_per_cell_photo_is_named_after_its_box(self) -> None:
        fetch = _CacheFetch()
        camera = camera_for(_sheet(), 64, 59)
        orto_path, _osm, _trees = sources.load_sources(SHEET, 64, 59, camera, fetch=fetch)
        self.assertRegex(orto_path.name, r"^orto_[0-9a-f]{12}\.png$")
        moved = dict(camera)
        moved["west"] = camera["west"] + 0.001
        moved["east"] = camera["east"] + 0.001
        other_path, _osm, _trees = sources.load_sources(SHEET, 64, 59, moved, fetch=fetch)
        self.assertNotEqual(other_path.name, orto_path.name)
        self.assertTrue(other_path.is_file())

    @unittest.skipUnless(DRAWING, "pillow and numpy are not installed")
    def test_the_shared_tile_directory_is_named_after_the_layer(self) -> None:
        fetch = _CacheFetch()
        sources.load_sources(SHEET, 64, 59, camera_for(_sheet(), 64, 59), fetch=fetch)
        tile_root = self.root / "work" / SHEET / "sources" / "ortho"
        self.assertEqual([entry.name for entry in tile_root.iterdir()], ["orto_2025"])

    def test_a_layer_becomes_a_slug(self) -> None:
        self.assertEqual(sources._slug("Orto:2025/orto foto"), "orto_2025_orto_foto")


class SourceInfoTests(ServiceCase):
    def test_render_writes_the_source_info_next_to_the_png(self) -> None:
        self.svc.state["sheets"] = {SHEET: _sheet()}

        def fake_render(sheet: dict[str, object], col: int, row: int, out: Path) -> None:
            del sheet, col, row
            Path(out).write_bytes(minimal_png(1536, 1536))

        with patch.object(commands, "render_cell", side_effect=fake_render):
            code, out, err = self.run_cmd(["render", SHEET, CELL])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"rendered: {SHEET} {CELL}", out)
        info_path = self.work() / f"{CELL}.src.json"
        info = json.loads(info_path.read_text(encoding="utf-8"))
        self.assertEqual(info["provider"], "opengeodata.goteborg.se orto_2025")
        # The terrain model marks renders placed by the live camera on real ground (2026-11-23).
        self.assertEqual(info["version"], f"osm {PINNED_DATE}; ortho orto_2025; terrain hojdmodell_2022")
        png = self.work() / f"{CELL}.src.png"
        self.assertEqual(info["sha256"], hashlib.sha256(png.read_bytes()).hexdigest())
        for value in info.values():
            self.assertIsInstance(value, str)


class ManifestTieTests(ServiceCase):
    def test_redraw_records_the_source_sha256(self) -> None:
        code, _out, err, _seen = self.redraw()
        self.assertEqual((code, err), (0, ""))
        manifest = self.manifest()
        self.assertEqual(manifest["source_sha256"], sha256_file(self.work() / f"{CELL}.src.png"))
        self.assertEqual(manifest["sha256"], sha256_file(self.work() / f"{CELL}.png"))

    def test_check_refuses_when_the_source_was_rendered_again(self) -> None:
        self.redraw()
        (self.work() / f"{CELL}.src.png").write_bytes(minimal_png(1536, 1536) + b"\x00")
        with (
            patch.object(commands, "_open_image", lambda path: object()),
            patch.object(commands, "check_problems", return_value=[]),
        ):
            code, _out, err = self.run_cmd(["check", SHEET, CELL])
        self.assertEqual(code, 1)
        self.assertIn("the source was rendered again after the redraw: run rutor redraw", err)

    def test_check_writes_the_redrawn_sha256(self) -> None:
        self.redraw()
        png = self.work() / f"{CELL}.png"
        png.write_bytes(minimal_png(1536, 1536) + b"\x00")
        with (
            patch.object(commands, "_open_image", lambda path: object()),
            patch.object(commands, "check_problems", return_value=[]),
        ):
            code, _out, err = self.run_cmd(["check", SHEET, CELL])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.manifest()["sha256"], sha256_file(png))

    def test_upload_refuses_a_png_that_changed_after_the_manifest(self) -> None:
        self.redraw()
        manifest = self.manifest()
        manifest["sha256"] = "0" * 64
        (self.work() / f"{CELL}.manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        self.svc.requests.clear()
        code, _out, err = self.run_cmd(["upload", SHEET, CELL])
        self.assertEqual(code, 1)
        self.assertIn("the PNG changed after the manifest was written: run rutor check", err)
        self.assertEqual(self.svc.requests, [])

    def test_upload_refuses_a_source_that_changed_after_the_redraw(self) -> None:
        self.redraw()
        (self.work() / f"{CELL}.src.png").write_bytes(minimal_png(1536, 1536) + b"\x00")
        self.svc.requests.clear()
        code, _out, err = self.run_cmd(["upload", SHEET, CELL])
        self.assertEqual(code, 1)
        self.assertIn("the source was rendered again after the redraw: run rutor redraw", err)
        self.assertEqual(self.svc.requests, [])

    def test_upload_accepts_a_manifest_that_matches_its_files(self) -> None:
        self.redraw()
        self.svc.requests.clear()
        code, out, err = self.run_cmd(["upload", SHEET, CELL])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(f"uploaded: {SHEET} {CELL}", out)


def _slots(day: object, sek: float) -> list[dict[str, object]]:
    start = datetime(day.year, day.month, day.day, tzinfo=se3_price.STOCKHOLM)  # type: ignore[attr-defined]
    rows: list[dict[str, object]] = []
    for quarter in range(24 * 4):
        begin = start + timedelta(minutes=15 * quarter)
        rows.append(
            {
                "time_start": begin.isoformat(),
                "time_end": (begin + timedelta(minutes=15)).isoformat(),
                "SEK_per_kWh": sek,
            }
        )
    return rows


class Se3PriceTests(unittest.TestCase):
    NOW = datetime(2026, 11, 21, 12, 0, tzinfo=se3_price.STOCKHOLM)

    def run_main(self, fetch: object) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = se3_price.main(fetch=fetch, clock=lambda: self.NOW)
        return code, stdout.getvalue()

    def test_no_prices_waits_and_keeps_the_lock(self) -> None:
        code, out = self.run_main(lambda day: None)
        self.assertEqual(code, 2)
        self.assertIn("No SE3 prices are available", out)
        self.assertIn("Keep your lock and run rutor redraw --wait-for-cheap again after", out)
        self.assertNotIn("district", out)

    def test_a_high_price_names_the_time_and_keeps_the_lock(self) -> None:
        tomorrow = self.NOW.date() + timedelta(days=1)

        def fetch(day: object) -> list[object]:
            return se3_price.parse_slots(_slots(day, 0.10 if day == tomorrow else 1.00))

        code, out = self.run_main(fetch)
        self.assertEqual(code, 2)
        self.assertIn("Keep your lock and run rutor redraw --wait-for-cheap again after", out)
        self.assertIn("2026-11-22", out)
        self.assertNotIn("district", out)

    def test_a_cheap_price_says_run_the_redraw_now(self) -> None:
        def fetch(day: object) -> list[object]:
            return se3_price.parse_slots(_slots(day, 0.10))

        code, out = self.run_main(fetch)
        self.assertEqual(code, 0)
        self.assertIn("Run the redraw now.", out)
        self.assertNotIn("district", out)

    def test_a_price_that_cannot_be_read_is_exit_1(self) -> None:
        def fetch(day: object) -> list[dict[str, object]]:
            del day
            raise TimeoutError("read timed out")

        code, out = self.run_main(fetch)
        self.assertEqual(code, 1)
        self.assertIn("The public SE3 price could not be read.", out)
        self.assertIn("run rutor redraw without --wait-for-cheap", out)
        self.assertNotIn("district", out)

    def test_a_timeout_while_fetching_counts_as_could_not_read(self) -> None:
        with patch("rutor.se3_price.urllib.request.urlopen", side_effect=TimeoutError("read timed out")):
            with self.assertRaises(RuntimeError):
                se3_price.fetch_prices(self.NOW.date())


class WaitExitCodeTests(ServiceCase):
    def test_wait_for_cheap_maps_a_high_price_to_exit_6(self) -> None:
        with patch.object(commands, "_se3_main", return_value=lambda: 2):
            code, _out, err = self.run_cmd(["redraw", SHEET, CELL, "--wait-for-cheap"])
        self.assertEqual(code, 6)
        self.assertIn("SE3 price is high", err)

    def test_wait_for_cheap_maps_an_unreadable_price_to_exit_4(self) -> None:
        with patch.object(commands, "_se3_main", return_value=lambda: 1):
            code, _out, err = self.run_cmd(["redraw", SHEET, CELL, "--wait-for-cheap"])
        self.assertEqual(code, 4)
        self.assertIn("could not be read", err)


class RedrawCoreErrorTests(unittest.TestCase):
    def test_no_message_calls_a_cell_a_district(self) -> None:
        source = inspect.getsource(redraw_core).lower()
        self.assertNotIn("district", source)
        self.assertIn("list sheets", source)

    def test_preflight_points_at_sheets_and_cells(self) -> None:
        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"torch": fake_torch}), patch.object(sys, "platform", "darwin"):
            message = redraw_core.preflight_message()
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("list sheets", message)
        self.assertIn("claim a cell", message)

    def test_a_lora_that_will_not_load_is_exit_3_not_system_exit(self) -> None:
        pipe = _FakePipe(lora_error=RuntimeError("boom"))
        with _fake_model_modules(pipe):
            with self.assertRaises(RutorError) as caught:
                redraw_core._load_pipe()
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("Could not load LoRA", caught.exception.message)
        self.assertIn("Keep this cell", caught.exception.message)


class PinTests(unittest.TestCase):
    def test_the_weights_are_pinned_to_exact_commits(self) -> None:
        # A volunteer's machine loads these files. A moving branch would let whoever owns the
        # Hugging Face repository swap them, so both are named by commit.
        self.assertEqual(set(pipeline.PINS), {"model_revision", "lora_revision", "diffusers_version"})
        self.assertRegex(pipeline.PINS["model_revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(pipeline.PINS["lora_revision"], r"^[0-9a-f]{40}$")

    def test_the_loader_takes_safetensors_only(self) -> None:
        # A pickle file runs code when it is loaded; a safetensors file cannot.
        pipe = _FakePipe()
        with _fake_model_modules(pipe):
            redraw_core._load_pipe()
        model = [call for call in pipe.calls if call[0] == "model"][0]
        lora = [call for call in pipe.calls if call[0] == "lora"][0]
        self.assertIs(model[2]["use_safetensors"], True)
        self.assertIs(lora[2]["use_safetensors"], True)
        self.assertNotIn("trust_remote_code", model[2])
        self.assertEqual(model[2]["revision"], pipeline.PINS["model_revision"])
        self.assertEqual(lora[2]["revision"], pipeline.PINS["lora_revision"])

    def test_empty_pins_print_each_name_and_do_not_block(self) -> None:
        stdout = io.StringIO()
        with (
            patch.dict(pipeline.PINS, {"model_revision": "", "lora_revision": "", "diffusers_version": ""}, clear=True),
            redirect_stdout(stdout),
        ):
            commands._report_pins()
        self.assertIn(
            "pin: none set for model_revision, lora_revision, diffusers_version: nothing to compare, the redraw goes on",
            stdout.getvalue(),
        )

    def test_a_diffusers_pin_that_differs_is_exit_3(self) -> None:
        with (
            patch.dict(pipeline.PINS, {"diffusers_version": "1.0.0"}, clear=True),
            patch("importlib.metadata.version", return_value="2.0.0"),
        ):
            with self.assertRaises(RutorError) as caught:
                commands._report_pins()
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("pin mismatch: diffusers_version", caught.exception.message)

    def test_a_diffusers_pin_without_diffusers_is_exit_3(self) -> None:
        with (
            patch.dict(pipeline.PINS, {"diffusers_version": "1.0.0"}, clear=True),
            patch("importlib.metadata.version", side_effect=importlib.metadata.PackageNotFoundError("diffusers")),
        ):
            with self.assertRaises(RutorError) as caught:
                commands._report_pins()
        self.assertEqual(caught.exception.code, 3)

    def test_check_pins_compares_only_what_it_was_given(self) -> None:
        with patch.dict(pipeline.PINS, {"model_revision": "abc"}, clear=True):
            self.assertEqual(pipeline.check_pins({}), [])
            self.assertEqual(pipeline.check_pins({"model_revision": "abc"}), [])
            self.assertEqual(pipeline.check_pins({"model_revision": "def"}), ["model_revision"])

    def test_the_loader_passes_the_revisions(self) -> None:
        pipe = _FakePipe()
        with (
            _fake_model_modules(pipe),
            patch.dict(pipeline.PINS, {"model_revision": "model-sha", "lora_revision": "lora-sha"}, clear=True),
        ):
            redraw_core._load_pipe()
        model = [call for call in pipe.calls if call[0] == "model"][0]
        lora = [call for call in pipe.calls if call[0] == "lora"][0]
        self.assertEqual(model[2]["revision"], "model-sha")
        self.assertEqual(lora[2]["revision"], "lora-sha")

    def test_the_loader_passes_no_revision_when_the_pin_is_empty(self) -> None:
        pipe = _FakePipe()
        with (
            _fake_model_modules(pipe),
            patch.dict(pipeline.PINS, {"model_revision": "", "lora_revision": ""}, clear=True),
        ):
            redraw_core._load_pipe()
        model = [call for call in pipe.calls if call[0] == "model"][0]
        lora = [call for call in pipe.calls if call[0] == "lora"][0]
        self.assertNotIn("revision", model[2])
        self.assertNotIn("revision", lora[2])


class DefaultSeedTests(ServiceCase):
    def test_redraw_without_a_seed_uses_the_cell_seed(self) -> None:
        wanted = int.from_bytes(hashlib.sha256(f"{SHEET}:{CELL}".encode()).digest()[:4], "big")
        code, _out, err, seen = self.redraw()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen["seed"], wanted)
        self.assertEqual(self.manifest()["seed"], wanted)
        code, _out, err, seen = self.redraw()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen["seed"], wanted)
        self.assertEqual(self.manifest()["seed"], wanted)

    def test_an_explicit_seed_still_wins(self) -> None:
        code, _out, err, seen = self.redraw(["--seed", "7"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(seen["seed"], 7)
        self.assertEqual(self.manifest()["seed"], 7)


class WhoamiTests(ServiceCase):
    def test_a_trusted_login_shows_trust_and_the_approved_count(self) -> None:
        self.svc.state["user"] = {"login": "klang", "github_id": 4242, "trusted": 1, "approved_count": 3}
        code, out, err = self.run_cmd(["whoami"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"login: klang\ntrusted: yes\napproved: 3\nterms: accepted {TERMS_VERSION}\n")

    def test_an_untrusted_login_says_no(self) -> None:
        self.svc.state["user"] = {"login": "klang", "github_id": 4242, "trusted": 0, "approved_count": 0}
        code, out, err = self.run_cmd(["whoami"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"login: klang\ntrusted: no\napproved: 0\nterms: accepted {TERMS_VERSION}\n")

    def test_no_trust_lines_when_the_service_does_not_send_the_fields(self) -> None:
        self.svc.state["user"] = {"login": "klang"}
        code, out, err = self.run_cmd(["whoami"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"login: klang\nterms: accepted {TERMS_VERSION}\n")

    def test_whoami_says_when_the_terms_need_a_yes(self) -> None:
        self.svc.state["user"] = {"login": "klang"}
        config.terms_path().unlink()
        code, out, err = self.run_cmd(["whoami"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "login: klang\nterms: not accepted: run rutor terms before a claim or an upload\n")
        config.write_terms("2026-11-23", "2026-11-23T10:00:00Z")
        code, out, err = self.run_cmd(["whoami"])
        self.assertEqual(
            out,
            f"login: klang\nterms: accepted 2026-11-23, today's are {TERMS_VERSION}: run rutor terms before a claim or an upload\n",
        )


if __name__ == "__main__":
    unittest.main()
