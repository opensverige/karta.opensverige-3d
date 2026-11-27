"""The Google source: which sheets use it, the camera it hands to Cesium, and the Node call."""

from __future__ import annotations

import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rutor import commands
from rutor.errors import RutorError
from rutor.render import CELL_PX, METRES_PER_PIXEL, WINDOW_PX, render_cell
from rutor.renderer import google, sources
from tests.png import minimal_png
from tests.test_review_fixes import ServiceCase

try:
    import numpy
    from PIL import Image

    DRAWING = True
except ImportError:
    DRAWING = False

GOOGLE_SHEET = "stockholm-norr"
CELL = "10_12"
TOKEN = "ion-token-for-tests"


def _sheet(sheet_id: str = GOOGLE_SHEET) -> dict[str, object]:
    return {
        "id": sheet_id,
        "name": "Stockholm",
        "state": "open",
        "cols": 144,
        "rows": 70,
        "origin_lat": 59.335,
        "origin_lon": 18.055,
        "x_meters": -17408,
        "y_meters": 8704,
        "ground_height": 45,
        "heading": 0,
    }


def _installed(root: Path) -> Path:
    """A google-render folder that looks like npm ci has run in it."""
    (root / "node_modules" / "cesium" / "Build" / "Cesium").mkdir(parents=True)
    (root / "node_modules" / "cesium" / "Build" / "Cesium" / "Cesium.js").write_text("", encoding="utf-8")
    (root / "node_modules" / "puppeteer").mkdir(parents=True)
    (root / "render.mjs").write_text("", encoding="utf-8")
    return root


class _Node:
    """Stands in for node: answers --version and writes a PNG where render.mjs would."""

    def __init__(self, picture: bytes | None = None, code: int = 0, stderr: str = "", version: str = "v22.23.4") -> None:
        self.picture = picture
        self.code = code
        self.stderr = stderr
        self.version = version
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        self.calls.append((list(argv), dict(env)))
        if argv[1:] == ["--version"]:
            return 0, self.version + "\n", ""
        if self.code != 0:
            return self.code, "", self.stderr
        out = Path(argv[argv.index("--out") + 1])
        if self.picture is not None:
            out.write_bytes(self.picture)
        return 0, json.dumps({"cesium": "1.147.0", "tiles": 412, "gl": "test"}) + "\n", ""


def _picture(blank: bool = False) -> bytes:
    if blank:
        pixels = numpy.zeros((WINDOW_PX, WINDOW_PX, 4), dtype=numpy.uint8)
        pixels[..., 3] = 255
    else:
        ramp = numpy.linspace(0, 255, WINDOW_PX, dtype=numpy.uint8)
        pixels = numpy.zeros((WINDOW_PX, WINDOW_PX, 4), dtype=numpy.uint8)
        pixels[..., 0] = ramp[None, :]
        pixels[..., 1] = ramp[:, None]
        pixels[..., 3] = 255
    buffer = io.BytesIO()
    Image.fromarray(pixels, "RGBA").save(buffer, format="PNG")
    return buffer.getvalue()


class SheetKindTests(unittest.TestCase):
    def test_goteborg_is_open_data_and_stockholm_is_google(self) -> None:
        self.assertEqual(sources.sheet_kind("goteborg-norr"), "open")
        self.assertEqual(sources.sheet_kind("stockholm-norr"), "google")

    def test_an_unknown_sheet_is_refused(self) -> None:
        with self.assertRaises(RutorError) as caught:
            sources.sheet_kind("malmo-syd")
        self.assertEqual(caught.exception.code, 3)


# The pose the live map's own renderer gives these cells (grid-config.ts, stockholm-grid-north,
# buildShotCamera moved to the cell's window centre), earth-centred metres and unit vectors.
LIVE_DIRECTION = (-0.9211572317553505, -0.30028003318712004, -0.24759090462688976)
LIVE_UP = (-0.23539939060885046, -0.07673579969573188, 0.9688646675083359)
LIVE_RIGHT = (-0.3099298005771652, 0.9507594431370107, 0.0)
LIVE_POSITIONS = {
    (85, 24): (3099955.598567919, 1015239.582872198, 5465807.445436383),
    (143, 69): (3108677.2931785844, 1033699.6814787069, 5457498.371687988),
}


class GoogleCameraTests(unittest.TestCase):
    def test_the_pose_is_the_one_that_drew_the_live_map(self) -> None:
        for (col, row), position in LIVE_POSITIONS.items():
            with self.subTest(cell=f"{col}_{row}"):
                view = google.google_camera(_sheet(), col, row)
                for name, expected in (("direction", LIVE_DIRECTION), ("up", LIVE_UP), ("right", LIVE_RIGHT)):
                    for got, want in zip(view[name], expected):
                        self.assertAlmostEqual(got, want, places=12)
                for got, want in zip(view["position"], position):
                    self.assertAlmostEqual(got, want, delta=1e-6)

    def test_the_window_is_1536_px_at_a_quarter_metre(self) -> None:
        view = google.google_camera(_sheet(), 10, 12)
        self.assertEqual((view["width"], view["height"]), (WINDOW_PX, WINDOW_PX))
        half = WINDOW_PX / 2 * METRES_PER_PIXEL
        frustum = view["frustum"]
        self.assertEqual((frustum["left"], frustum["right"], frustum["bottom"], frustum["top"]), (-half, half, -half, half))
        self.assertLess(frustum["near"], google.RANGE_M)
        self.assertGreater(frustum["far"], google.RANGE_M)

    def test_neighbours_differ_by_exactly_one_cell_in_the_picture_plane(self) -> None:
        a = google.google_camera(_sheet(), 40, 30)
        b = google.google_camera(_sheet(), 41, 31)
        step = [q - p for p, q in zip(a["position"], b["position"])]
        across = sum(s * r for s, r in zip(step, a["right"]))
        up = sum(s * u for s, u in zip(step, a["up"]))
        self.assertAlmostEqual(across, CELL_PX * METRES_PER_PIXEL, places=6)
        self.assertAlmostEqual(up, -CELL_PX * METRES_PER_PIXEL, places=6)

    def test_the_axes_are_a_right_handed_unit_frame(self) -> None:
        view = google.google_camera(_sheet(), 0, 0)
        d, u, r = view["direction"], view["up"], view["right"]
        for vector in (d, u, r):
            self.assertAlmostEqual(math.hypot(*vector), 1.0, places=12)
        cross = (d[1] * u[2] - d[2] * u[1], d[2] * u[0] - d[0] * u[2], d[0] * u[1] - d[1] * u[0])
        for got, want in zip(cross, r):
            self.assertAlmostEqual(got, want, places=12)


class PinTests(unittest.TestCase):
    def test_the_python_side_names_the_cesium_version_npm_installs(self) -> None:
        package = Path(__file__).resolve().parents[1] / "google-render" / "package.json"
        pinned = json.loads(package.read_text(encoding="utf-8"))["dependencies"]
        self.assertEqual(pinned["cesium"], google.CESIUM_VERSION)
        for name, version in pinned.items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+$", f"{name} is not pinned to one version")


class GoogleRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.renderer = _installed(self.root / "google-render")
        self.out = self.root / "work" / f"{CELL}.src.png"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def render(self, node: _Node, env: dict[str, str] | None = None, which: str | None = "/usr/bin/node") -> dict[str, str]:
        return google.render_google(
            _sheet(),
            10,
            12,
            self.out,
            run=node,
            env={"CESIUM_ION_TOKEN": TOKEN, "PATH": "/usr/bin"} if env is None else env,
            which=lambda _name: which,
            render_dir=self.renderer,
        )

    def test_a_missing_token_says_where_to_get_one(self) -> None:
        with self.assertRaises(RutorError) as caught:
            self.render(_Node(), env={"PATH": "/usr/bin"})
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("CESIUM_ION_TOKEN", caught.exception.message)
        self.assertIn("ion.cesium.com", caught.exception.message)

    def test_a_missing_node_is_refused(self) -> None:
        with self.assertRaises(RutorError) as caught:
            self.render(_Node(), which=None)
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("Node 22", caught.exception.message)

    def test_an_old_node_is_refused(self) -> None:
        with self.assertRaises(RutorError) as caught:
            self.render(_Node(version="v18.20.7"))
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("Node 22", caught.exception.message)

    def test_a_renderer_without_npm_ci_is_refused(self) -> None:
        (self.renderer / "node_modules" / "puppeteer").rmdir()
        with self.assertRaises(RutorError) as caught:
            self.render(_Node())
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("npm ci", caught.exception.message)

    @unittest.skipUnless(DRAWING, "pillow and numpy are not installed")
    def test_the_token_goes_in_the_environment_never_on_the_command_line(self) -> None:
        node = _Node(_picture())
        self.render(node)
        argv, env = node.calls[-1]
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertEqual(env["CESIUM_ION_TOKEN"], TOKEN)
        camera_path = Path(argv[argv.index("--camera") + 1])
        self.assertNotIn(TOKEN, camera_path.read_text(encoding="utf-8"))

    @unittest.skipUnless(DRAWING, "pillow and numpy are not installed")
    def test_a_render_is_saved_as_a_1536_rgb_png(self) -> None:
        info = self.render(_Node(_picture()))
        with Image.open(self.out) as saved:
            self.assertEqual(saved.size, (WINDOW_PX, WINDOW_PX))
            self.assertEqual(saved.mode, "RGB")
        self.assertEqual(info["cesium"], "1.147.0")

    @unittest.skipUnless(DRAWING, "pillow and numpy are not installed")
    def test_a_blank_render_is_a_network_failure(self) -> None:
        with self.assertRaises(RutorError) as caught:
            self.render(_Node(_picture(blank=True)))
        self.assertEqual(caught.exception.code, 4)
        self.assertFalse(self.out.exists())

    def test_a_wrong_size_is_refused(self) -> None:
        with self.assertRaises(RutorError) as caught:
            self.render(_Node(minimal_png(1024, 1024)))
        self.assertEqual(caught.exception.code, 4)
        self.assertFalse(self.out.exists())

    def test_node_exit_codes_carry_over(self) -> None:
        for node_code, expected in ((4, 4), (3, 3), (1, 3)):
            with self.subTest(node_code=node_code):
                with self.assertRaises(RutorError) as caught:
                    self.render(_Node(code=node_code, stderr="starting\nerror: ion answered 401\n"))
                self.assertEqual(caught.exception.code, expected)
                self.assertIn("ion answered 401", caught.exception.message)


    def test_chrome_without_its_sandbox_is_named_not_left_as_a_link(self) -> None:
        # Puppeteer's launch error as render.mjs prints it, on Ubuntu 24.04 from a terminal. Its
        # last line is only a link, and that was all rutor said.
        stderr = (
            "gpu render failed (Failed to launch the browser process!), trying the software renderer\n"
            "error: Failed to launch the browser process:  Code: null\n\nstderr:\n"
            "[1:1:1127/183701.722417:FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:130] "
            "No usable sandbox! If you are running on Ubuntu 23.10+ or another Linux distro that has disabled "
            "unprivileged user namespaces with AppArmor, see https://chromium.googlesource.com/x\n"
            "Received signal 6\n\nTROUBLESHOOTING: https://pptr.dev/troubleshooting\n"
        )
        with self.assertRaises(RutorError) as caught:
            self.render(_Node(code=3, stderr=stderr))
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("No usable sandbox!", caught.exception.message)
        self.assertIn("AppArmor", caught.exception.message)
        self.assertIn("README.md", caught.exception.message)


class DispatchTests(unittest.TestCase):
    def test_a_google_sheet_renders_through_cesium(self) -> None:
        with patch.object(google, "render_google") as called, patch("rutor.renderer.render") as open_render:
            render_cell(_sheet(), 10, 12, Path("out.png"))
        called.assert_called_once()
        open_render.assert_not_called()

    def test_the_sheet_row_decides_not_the_id(self) -> None:
        with patch.object(google, "render_google") as called, patch("rutor.renderer.render") as open_render:
            render_cell({**_sheet("se-n5602-e1262"), "kind": "google"}, 10, 12, Path("out.png"))
        called.assert_called_once()
        open_render.assert_not_called()
        with patch.object(google, "render_google") as called, patch("rutor.renderer.render") as open_render:
            render_cell({**_sheet("any-town"), "kind": "open"}, 10, 12, Path("out.png"))
        called.assert_not_called()
        open_render.assert_called_once()

    def test_a_sheet_with_no_source_is_drawn_by_nobody(self) -> None:
        with patch.object(google, "render_google") as called, patch("rutor.renderer.render") as open_render:
            with self.assertRaises(RutorError) as caught:
                render_cell(_sheet("se-n5602-e1262"), 10, 12, Path("out.png"))
        self.assertEqual(caught.exception.code, 3)
        called.assert_not_called()
        open_render.assert_not_called()

    def test_an_open_sheet_never_calls_cesium(self) -> None:
        with patch.object(google, "render_google") as called, patch("rutor.renderer.render") as open_render:
            render_cell(_sheet("goteborg-norr"), 10, 12, Path("out.png"))
        called.assert_not_called()
        open_render.assert_called_once()


class GoogleSourceInfoTests(ServiceCase):
    def test_a_google_render_says_so_in_the_source_info_and_on_screen(self) -> None:
        self.svc.state["sheets"] = {GOOGLE_SHEET: _sheet()}

        def fake_render(sheet: dict[str, object], col: int, row: int, out: Path) -> None:
            del sheet, col, row
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(minimal_png(1536, 1536))

        with patch.object(commands, "render_cell", side_effect=fake_render):
            code, out, err = self.run_cmd(["render", GOOGLE_SHEET, CELL])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Google", out)
        self.assertIn("taken down", out)
        info_path = self.root / "work" / GOOGLE_SHEET / f"{CELL}.src.json"
        info = json.loads(info_path.read_text(encoding="utf-8"))
        self.assertEqual(info["provider"], "Google Photorealistic 3D Tiles via Cesium ion asset 2275207")
        self.assertTrue(info["version"].startswith("cesium 1.147.0; rendered "))


if __name__ == "__main__":
    unittest.main()


class RisingShareTests(unittest.TestCase):
    """Telling a place with 3D buildings from a photo laid on flat ground."""

    def test_flat_ground_has_nothing_rising(self) -> None:
        self.assertEqual(google.rising_share([12.0] * 576), 0.0)

    def test_a_gentle_slope_is_not_a_building(self) -> None:
        # Half a metre a step each way is 4 % of slope at 16 m between the points. A steeper
        # hill does count as rising, which errs towards drawing the cell.
        slope = [0.5 * (i + j) for j in range(24) for i in range(24)]
        self.assertEqual(google.rising_share(slope), 0.0)

    def test_roofs_beside_streets_rise(self) -> None:
        town = [20.0 if (i // 2 + j // 2) % 2 else 10.0 for j in range(24) for i in range(24)]
        share = google.rising_share(town)
        assert share is not None
        self.assertAlmostEqual(share, 0.5)
        self.assertGreater(share, google.FLAT_SHARE)

    def test_what_was_not_measured_is_not_guessed(self) -> None:
        for heights in (None, [], [1.0] * 575, [1.0] * 575 + [None], [1.0] * 575 + ["x"], [1.0] * 575 + [float("nan")]):
            with self.subTest(heights=str(heights)[:20]):
                self.assertIsNone(google.rising_share(heights))
