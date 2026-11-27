"""The Google source: Photorealistic 3D Tiles through Cesium ion, drawn by headless Chrome.

The contributor's own Cesium ion token is read from CESIUM_ION_TOKEN and handed to node in its
environment, never on a command line or in a file. Google's terms bind that token. The render
stays on the contributor's machine like any source render.

The camera is the one that drew the live map: one orthographic camera per sheet, looking along
the sheet's heading and 45 degrees down, fixed in the earth-centred frame at the sheet's origin.
Every cell moves it sideways in its own picture plane, so neighbours line up to the pixel and a
cell lands on the map pixels it replaces. A point at east e, north n and height h in the plane
that touches the ellipsoid at the origin lands at picture X = e cos a - n sin a,
Y = (e sin a + n cos a + h) / sqrt 2, and map pixel (X - x_meters, y_meters - Y) / 0.25. No
ground height enters: Google's tiles carry their own heights.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

from rutor import __version__, config
from rutor.errors import RutorError, environment, network
from rutor.frame import ecef as _ecef
from rutor.frame import mix as _mix
from rutor.frame import sheet_basis
from rutor.render import CELL_PX, METRES_PER_PIXEL, WINDOW_PX, _number

ION_ASSET = 2275207
# Must match cli/google-render/package.json; a test holds them together.
CESIUM_VERSION = "1.147.0"
TOKEN_ENV = "CESIUM_ION_TOKEN"
NODE_MAJOR = 22
# Distance and depth the live map's renders used (near 700 m before the target, far 350 m behind).
RANGE_M = 4000.0
NEAR_M = RANGE_M - 700.0
FAR_M = RANGE_M + 350.0
# The live renders' light: east-north-up at the sheet origin, intensity 2.2.
LIGHT_ENU = (-0.45, -0.55, -0.7)
LIGHT_INTENSITY = 2.2
# A render with less spread than this is the black background: no tiles arrived.
MIN_SPREAD = 4.0

# Telling a place with 3D buildings from a photo on flat ground. render.mjs reads the height of
# the picture's surface on a grid of points, 16 m apart on the screen. A point "rises" when it
# lies more than RISE_M above the lowest point within RISE_REACH steps of it: a roof or a crown
# beside a street does, a gentle slope does not. Measured 2026-11-26 on nine cells: where Google has
# buildings and trees as shapes, 38 to 80 % of the points rise; open water, fields and a town
# Google has only a photo of, 0 to 3 %.
HEIGHT_GRID = 24
RISE_M = 3.0
RISE_REACH = 2
FLAT_SHARE = 0.10
NODE_TIMEOUT = 900
# Where Cesium ion says whether a token may read the asset. The render asks the same address.
ION_ENDPOINT = f"https://api.cesium.com/v1/assets/{ION_ASSET}/endpoint"
ION_TIMEOUT = 30
ION_ANSWER_BYTES = 4096
# Cesium's own word for a refusal, such as INVALID_TOKEN. Anything else from outside is dropped.
_ION_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")

Runner = Callable[[list[str], dict[str, str]], tuple[int, str, str]]
Which = Callable[[str], str | None]
Asker = Callable[[str], tuple[int, str]]


def default_render_dir() -> Path:
    return config.repo_root() / "cli" / "google-render"


def google_camera(sheet: dict[str, object], col: int, row: int) -> dict[str, object]:
    """What render.mjs needs to point Cesium at one cell: the live map's camera, moved to the cell."""
    basis = sheet_basis(sheet)
    origin = _ecef(_number(sheet, "origin_lat"), _number(sheet, "origin_lon"), 0.0)
    # The window centre in picture metres: the middle of the cell, as camera_for counts it.
    x = _number(sheet, "x_meters") + (col * CELL_PX + CELL_PX // 2) * METRES_PER_PIXEL
    y = _number(sheet, "y_meters") - (row * CELL_PX + CELL_PX // 2) * METRES_PER_PIXEL
    centre = _mix((origin, 1.0), (basis["right"], x), (basis["forward"], y * math.sqrt(2.0)))
    position = _mix((centre, 1.0), (basis["direction"], -RANGE_M))
    norm = math.sqrt(sum(c * c for c in LIGHT_ENU))
    light = _mix(
        (basis["east"], LIGHT_ENU[0] / norm), (basis["north"], LIGHT_ENU[1] / norm), (basis["up"], LIGHT_ENU[2] / norm)
    )
    half = WINDOW_PX / 2 * METRES_PER_PIXEL
    return {
        "position": list(position),
        "direction": list(basis["direction"]),
        "up": list(basis["camera_up"]),
        "right": list(basis["right"]),
        "frustum": {
            "left": -half,
            "right": half,
            "bottom": -half,
            "top": half,
            "near": NEAR_M,
            "far": FAR_M,
        },
        "light": {"direction": list(light), "intensity": LIGHT_INTENSITY},
        "width": WINDOW_PX,
        "height": WINDOW_PX,
        "ion_asset": ION_ASSET,
    }


# What Chrome says when it may not build its sandbox. On Ubuntu 23.10 and later a program needs an
# AppArmor profile to make user namespaces, and the Chrome that npm ci fetches has none.
NO_SANDBOX = "No usable sandbox!"
SANDBOX_HELP = (
    "Chrome could not build its sandbox (No usable sandbox!). On Ubuntu 23.10 or newer the Chrome that "
    "npm ci fetched needs an AppArmor profile first: see Source render in README.md. A person with sudo "
    "installs it once"
)


def _run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    try:
        # Node writes UTF-8 whatever the console's code page is. Read with that code page, a
        # letter it lacks in a path broke the reading and hid the render's own error.
        done = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=NODE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise network(f"the Google render took longer than {NODE_TIMEOUT} s") from exc
    except OSError as exc:
        raise environment(f"could not start node: {exc}") from exc
    return done.returncode, done.stdout, done.stderr


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def _node(which: Which, run: Runner, env: dict[str, str]) -> str:
    node = which("node")
    if node is None:
        raise environment(f"the Google source needs Node {NODE_MAJOR} or newer on PATH")
    code, out, _err = run([node, "--version"], env)
    version = out.strip().lstrip("v")
    try:
        major = int(version.split(".")[0])
    except ValueError:
        major = 0
    if code != 0 or major < NODE_MAJOR:
        raise environment(f"the Google source needs Node {NODE_MAJOR} or newer, found {out.strip() or 'nothing'}")
    return node


def _require_installed(root: Path) -> None:
    needed = (
        root / "render.mjs",
        root / "node_modules" / "cesium" / "Build" / "Cesium" / "Cesium.js",
        root / "node_modules" / "puppeteer",
    )
    if not all(path.exists() for path in needed):
        raise environment(f"the Google renderer is not installed: run npm ci in {root}")


def token_missing(env: Mapping[str, str] | None = None) -> str | None:
    environ = os.environ if env is None else env
    if environ.get(TOKEN_ENV, "").strip():
        return None
    return (
        f"{TOKEN_ENV} is not set. The person at this machine makes a free account at "
        f"https://ion.cesium.com, creates a token with the default scopes and sets {TOKEN_ENV} themselves, "
        "where this program can read it. An agent does not make the account and does not handle the token"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the token to wherever it points, so none is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, msg, headers, newurl
        raise network(f"Cesium ion answered with a redirect ({code}); the token was not sent on")


def _ask_ion(token: str, url: str = ION_ENDPOINT) -> tuple[int, str]:
    """Ask Cesium ion whether the token may read the asset. Returns the status and Cesium's code.

    The token goes in a header, never in the address, where a proxy or a log would keep it.
    """
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": f"rutor/{__version__}",
        },
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=ION_TIMEOUT) as response:
            return int(response.status), ""
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(ION_ANSWER_BYTES)
        except OSError:
            raw = b""
        finally:
            exc.close()
        try:
            answer = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            answer = None
        code = answer.get("code") if isinstance(answer, dict) else None
        return int(exc.code), code if isinstance(code, str) and _ION_CODE.fullmatch(code) else ""
    except urllib.error.URLError as exc:
        raise network(f"could not reach Cesium ion to check the token ({exc.reason})") from exc
    except OSError as exc:
        raise network(f"could not reach Cesium ion to check the token ({exc.__class__.__name__})") from exc


def token_refused(env: Mapping[str, str] | None = None, ask: Asker | None = None) -> str | None:
    """What Cesium ion says is wrong with the token, or None when it may read the asset.

    Asked before a cell is locked: a token that is only set, and wrong, fails at the render with
    the cell already held for 48 hours. Raises a network error when Cesium ion cannot be asked,
    since that says nothing about the token. The token itself is never part of a message.
    """
    environ = os.environ if env is None else env
    token = environ.get(TOKEN_ENV, "").strip()
    if not token:
        return token_missing(environ)
    again = f"The person at this machine sets {TOKEN_ENV} again, with the whole token, once"
    if any(ch.isspace() or not ch.isprintable() for ch in token):
        return f"the token in {TOKEN_ENV} has a space or a line break inside it. {again}"
    status, code = (ask or _ask_ion)(token)
    said = f"{status} {code}".strip()
    if status == 200:
        return None
    if status == 401:
        dots = token.count(".")
        shape = "" if dots == 2 else f" It has {dots} dots where a token has two, so it was pasted twice or cut short."
        return f"Cesium ion refused the token in {TOKEN_ENV} ({said}).{shape} {again}"
    if status in (403, 404):
        return (
            f"Cesium ion did not let this token read asset {ION_ASSET} ({said}). The person at this machine "
            "checks at https://ion.cesium.com that the token has the scope assets:read and that "
            "Google Photorealistic 3D Tiles is among the account's assets"
        )
    raise network(f"Cesium ion answered {said} when asked about the token; nothing was locked, try again later")


def token_unusable(env: Mapping[str, str] | None = None, ask: Asker | None = None) -> str | None:
    """Why the token cannot be used right now, as one line for preflight, or None."""
    try:
        return token_refused(env, ask)
    except RutorError as exc:
        return exc.message


def _tool_environ(env: Mapping[str, str] | None) -> dict[str, str]:
    """The environment Node is looked for and started in.

    One that is handed in is used as it is. Otherwise it is this process's own, with the
    app's own Node in front when this is an app install.
    """
    if env is not None:
        return dict(env)
    return {**os.environ, **config.tool_env()}


def _which_in(environ: Mapping[str, str]) -> Which:
    """Look a program up on the path of the environment it will run in, not on this process's."""
    return lambda name: shutil.which(name, path=environ.get("PATH"))


def node_missing(which: Which | None = None, run: Runner | None = None, env: Mapping[str, str] | None = None) -> str | None:
    try:
        environ = _tool_environ(env)
        _node(which or _which_in(environ), run or _run, environ)
    except RutorError as exc:
        return exc.message
    return None


def renderer_missing(root: Path | None = None) -> str | None:
    try:
        _require_installed(root or default_render_dir())
    except RutorError as exc:
        return exc.message
    return None


def not_ready(
    env: Mapping[str, str] | None = None,
    which: Which | None = None,
    run: Runner | None = None,
    render_dir: Path | None = None,
    ask: Asker | None = None,
) -> str | None:
    """The first thing this machine lacks for a Google sheet, or None. Asked before a cell is locked.

    What the machine itself lacks comes first and needs no network. Cesium ion is asked about the
    token last, and a network error is raised when it cannot be asked.
    """
    return token_missing(env) or node_missing(which, run, env) or renderer_missing(render_dir) or token_refused(env, ask)


def rising_share(heights: object) -> float | None:
    """The share of the measured points that rise above the ground beside them. None: not measured."""
    count = HEIGHT_GRID * HEIGHT_GRID
    if not isinstance(heights, list) or len(heights) != count:
        return None
    values: list[float] = []
    for height in heights:
        if isinstance(height, bool) or not isinstance(height, (int, float)) or not math.isfinite(height):
            return None
        values.append(float(height))
    rising = 0
    for j in range(HEIGHT_GRID):
        for i in range(HEIGHT_GRID):
            lowest = min(
                values[y * HEIGHT_GRID + x]
                for y in range(max(0, j - RISE_REACH), min(HEIGHT_GRID, j + RISE_REACH + 1))
                for x in range(max(0, i - RISE_REACH), min(HEIGHT_GRID, i + RISE_REACH + 1))
            )
            if values[j * HEIGHT_GRID + i] - lowest > RISE_M:
                rising += 1
    return rising / count


def _save_rgb(raw: bytes, out: Path) -> None:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise environment("render needs pillow and numpy (pip install pillow numpy)") from exc
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            image = opened.convert("RGB")
    except OSError as exc:
        raise network("the Google render is not an image") from exc
    if image.size != (WINDOW_PX, WINDOW_PX):
        raise network(f"the Google render is {image.size[0]}x{image.size[1]}, expected {WINDOW_PX}x{WINDOW_PX}")
    if float(np.asarray(image, dtype=np.float32).std()) < MIN_SPREAD:
        raise network("the Google render is blank: no tiles arrived. Check the token and try again later")
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)


def render_google(
    sheet: dict[str, object],
    col: int,
    row: int,
    out: Path,
    run: Runner | None = None,
    env: Mapping[str, str] | None = None,
    which: Which | None = None,
    render_dir: Path | None = None,
) -> dict[str, str]:
    """Write the 1536 px source render for one cell from Google's tiles. Returns what node reported."""
    run = run or _run
    environ = _tool_environ(env)
    which = which or _which_in(environ)
    root = render_dir or default_render_dir()
    no_token = token_missing(environ)
    if no_token is not None:
        raise environment(no_token)
    node = _node(which, run, environ)
    _require_installed(root)
    view = google_camera(sheet, col, row)
    out.parent.mkdir(parents=True, exist_ok=True)
    camera_path = out.with_name(out.stem + ".camera.json")
    raw_path = out.with_name(out.stem + ".google.png")
    camera_path.write_text(json.dumps(view, indent=2) + "\n", encoding="utf-8")
    try:
        code, stdout, stderr = run(
            [node, str(root / "render.mjs"), "--camera", str(camera_path), "--out", str(raw_path)], environ
        )
        if code != 0:
            if NO_SANDBOX in stderr:
                # Puppeteer's error has many lines, and its last is only a link: say what it means.
                raise environment(f"the Google render failed: {SANDBOX_HELP}")
            message = f"the Google render failed: {_last_line(stderr)}"
            raise network(message) if code == 4 else environment(message)
        try:
            raw = raw_path.read_bytes()
        except OSError as exc:
            raise network("the Google render wrote no picture") from exc
        _save_rgb(raw, out)
    finally:
        raw_path.unlink(missing_ok=True)
    try:
        reported = json.loads(_last_line(stdout))
    except json.JSONDecodeError:
        reported = {}
    if not isinstance(reported, dict):
        reported = {}
    said = {str(key): str(value) for key, value in reported.items() if key != "heights"}
    share = rising_share(reported.get("heights"))
    if share is not None:
        said["rising"] = f"{share:.3f}"
    return said
