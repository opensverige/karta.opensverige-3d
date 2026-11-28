"""One function per rutor command. None of them imports torch at import time."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from rutor import api, config, login, pipeline, redraw_core
from rutor.errors import RutorError, bad_input, check_failed, environment, network, refused, wait
from rutor.manifest import build_manifest, encode_manifest, png_size, sha256_file, write_manifest
from rutor.check import problems as check_problems
from rutor.render import render_cell
from rutor.renderer import google
from rutor.renderer.sources import SHEET_KINDS, kind_of, ortho_source, osm_date
from rutor.terrain import terrain_label


def require_token() -> str:
    token = config.read_token()
    if not token:
        raise environment("not logged in: run rutor login")
    return token


def require_terms() -> None:
    """A claim and an upload need a yes to today's terms. The service asks again, but a rutor
    that knows says so before it spends a request, in the same words."""
    if login.terms_current():
        return
    raise refused(
        f"refused: terms_needed: say yes to the terms of {login.TERMS_VERSION} first: run rutor terms. "
        "Any lock you hold is kept."
    )


def parse_cell(text: str) -> tuple[int, int]:
    parts = text.split("_")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise bad_input(f"cell must look like 64_59, not {text!r}")
    return int(parts[0]), int(parts[1])


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise network(f"service answer has no {label}")
    parsed: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            parsed[key] = item
    return parsed


def _rows(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise network(f"service answer has no {label}")
    return value


def cmd_cities(args: argparse.Namespace) -> int:
    # The lists are public: an agent can look before its human has logged in.
    _status, body = api.get("/v1/sheets", token=config.read_token())
    if args.json:
        print(json.dumps(body))
        return 0
    for sheet in _rows(_object(body, "object").get("sheets"), "sheets"):
        fields = _object(sheet, "sheet")
        line = f"sheet: {fields.get('id')} {fields.get('state')} {fields.get('done')}/{fields.get('total')}"
        cols, rows, total = fields.get("cols"), fields.get("rows"), fields.get("total")
        if fields.get("made") == "grid" and isinstance(cols, int) and isinstance(rows, int) and isinstance(total, int):
            # A sheet of the tiling counts only the cells someone has claimed, so 1/1 would
            # read as a finished sheet. A cell that was claimed and given back is counted too,
            # so the line does not say how many are left: any free cell can be named.
            line += f" claimed so far, of {cols * rows} cells"
        print(line)
    return 0


def _cell_line(cell: object, free_only: bool) -> str | None:
    if not isinstance(cell, list) or len(cell) < 3:
        raise network("service grid cell was not a list")
    col, row, state = cell[0], cell[1], cell[2]
    if free_only and state != "fri":
        return None
    line = f"cell: {col}_{row} {state}"
    login = cell[3] if len(cell) > 3 else None
    if isinstance(login, str) and login:
        line += f" {login}"
    return line


def cmd_grid(args: argparse.Namespace) -> int:
    _status, body = api.get(f"/v1/grid/{args.sheet}", token=config.read_token())
    if args.json:
        print(json.dumps(body))
        return 0
    payload = _object(body, "object")
    cells = _rows(payload.get("cells"), "cells")
    printed = 0
    for cell in cells:
        line = _cell_line(cell, args.free)
        if line is not None:
            print(line)
            printed += 1
    sheet = payload.get("sheet")
    cols = sheet.get("cols") if isinstance(sheet, dict) else None
    rows = sheet.get("rows") if isinstance(sheet, dict) else None
    if isinstance(sheet, dict) and sheet.get("made") == "grid" and isinstance(cols, int) and isinstance(rows, int):
        # A sheet of the tiling lists only the cells someone has claimed. An empty list, or
        # "free: none", would say the opposite of what is true: the rest can be claimed.
        print(
            f"note: {args.sheet} lists only the cells someone has claimed, {len(cells)} so far. "
            f"Any other cell from 0_0 to {cols - 1}_{rows - 1}, and any listed as fri, can be "
            f"claimed by name: rutor claim {args.sheet} <col>_<row>."
        )
        return 0
    if args.free and not printed:
        # An empty answer reads like a failure. Every cell here is on the map or not offered.
        print(f"free: none on {args.sheet}")
    return 0


def pick_next(sheet: dict[str, object], cells: list[object]) -> tuple[int, int]:
    cols = sheet.get("cols")
    rows = sheet.get("rows")
    if not isinstance(cols, int) or not isinstance(rows, int):
        raise network("sheet is missing cols or rows")
    centre_col = (cols - 1) / 2
    centre_row = (rows - 1) / 2
    parsed: list[tuple[int, int, str]] = []
    for cell in cells:
        if not isinstance(cell, list) or len(cell) < 3:
            continue
        col, row, state = cell[0], cell[1], cell[2]
        if isinstance(col, int) and isinstance(row, int) and isinstance(state, str):
            parsed.append((col, row, state))
    by_pos = {(col, row): state for col, row, state in parsed}
    free = [(col, row) for col, row, state in parsed if state == "fri"]
    if sheet.get("made") == "grid":
        # A sheet of the tiling lists only the cells someone has taken: every other cell is
        # free and has no row. "Next" is then a free cell beside one that has a row, so what
        # is drawn grows outwards from where someone started.
        around = {(col + d_col, row + d_row) for col, row in by_pos for d_col in (-1, 0, 1) for d_row in (-1, 0, 1)}
        free = sorted(
            (col, row)
            for col, row in around
            if 0 <= col < cols and 0 <= row < rows and by_pos.get((col, row), "fri") == "fri"
        )
        if not free:
            raise refused(
                f"refused: no_free_cell: nothing is drawn on {sheet.get('id')} yet, so there is no cell to build beside: name a cell, or use --at."
            )
    if not free:
        # Every cell here is on the map already, or nobody can build what is left yet.
        raise refused(f"refused: no_free_cell: no cell on {sheet.get('id')} is free. rutor cities shows where there are free cells.")

    def beside_live(col: int, row: int) -> bool:
        for d_col in (-1, 0, 1):
            for d_row in (-1, 0, 1):
                if d_col == 0 and d_row == 0:
                    continue
                if by_pos.get((col + d_col, row + d_row)) == "live":
                    return True
        return False

    def distance(col: int, row: int) -> float:
        return (col - centre_col) ** 2 + (row - centre_row) ** 2

    adjacent = [cell for cell in free if beside_live(cell[0], cell[1])]
    pool = adjacent or free
    pool.sort(key=lambda cell: (distance(cell[0], cell[1]), cell[1], cell[0]))
    return pool[0]


def _source_ready(sheet: dict[str, object], sheet_id: str) -> str:
    """The source the sheet is drawn from, once this machine is known to be able to render it.

    Without Node, the renderer and a token Cesium ion accepts, a Google render fails with the
    cell already taken from everyone else. A sheet with no source decided raises here too.
    """
    kind = kind_of(sheet)
    if kind == "google":
        problem = google.not_ready()
        if problem is not None:
            raise environment(f"this machine cannot render {sheet_id} yet: {problem}")
    return kind


def cell_url(sheet: str, col: int, row: int) -> str:
    """Where a person sees the cell on the map. The service's upload answer uses the same address."""
    return f"{config.MAP_URL}#{sheet}/{col}_{row}"


_DEGREES = r"-?\d{1,3}(?:\.\d{1,9})?"
_POINT = re.compile(rf"\s*({_DEGREES})\s*,\s*({_DEGREES})\s*")


def parse_point(text: str) -> tuple[str, str]:
    """Latitude and longitude out of `--at`, as the digits the person wrote."""
    found = _POINT.fullmatch(text)
    if found is None or abs(float(found.group(1))) > 90 or abs(float(found.group(2))) > 180:
        raise bad_input(f"--at must be latitude,longitude in decimal degrees, like 56.0465,12.6945, not {text!r}")
    return found.group(1), found.group(2)


# OpenStreetMap's own search. Its policy asks for at most one request a second and a name for
# the program that asks: rutor asks once per claim, and names itself, never the person.
NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_SECONDS = 15
NOMINATIM_BYTES = 200_000


def _open_url(request: urllib.request.Request, timeout: float):  # type: ignore[no-untyped-def]
    return urllib.request.urlopen(request, timeout=timeout)


def _nominatim(name: str) -> object:
    """What OpenStreetMap's search answers for a name in Sweden: a list of at most one place."""
    from rutor import __version__

    query = urllib.parse.urlencode({"q": name, "format": "jsonv2", "limit": 1, "countrycodes": "se"})
    request = urllib.request.Request(
        f"{NOMINATIM}?{query}",
        headers={"User-Agent": f"rutor/{__version__} (+https://github.com/opensverige/karta.opensverige-3d)", "Accept": "application/json"},
    )
    try:
        with _open_url(request, NOMINATIM_SECONDS) as answer:
            raw = answer.read(NOMINATIM_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise network(f"could not reach OpenStreetMap's search ({exc.__class__.__name__})") from exc
    if len(raw) > NOMINATIM_BYTES:
        raise network("OpenStreetMap's search sent more than a place")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise network("OpenStreetMap's search did not answer JSON") from exc


def find_place(name: str) -> tuple[str, str, str]:
    """Latitude, longitude and the full name of a town, street or landmark in Sweden."""
    cleaned = " ".join(name.split())
    if not 2 <= len(cleaned) <= 200:
        raise bad_input("--plats takes the name of a place, like Eslöv or \"Stortorget Helsingborg\"")
    hits = _nominatim(cleaned)
    hit = hits[0] if isinstance(hits, list) and hits and isinstance(hits[0], dict) else None
    if hit is None:
        raise bad_input(f"OpenStreetMap knows no place called {api.one_line(cleaned, 80)!r} in Sweden. Try a town or a street.")
    lat, lon = str(hit.get("lat", "")), str(hit.get("lon", ""))
    try:
        lat, lon = parse_point(f"{float(lat):.6f},{float(lon):.6f}")
    except (ValueError, RutorError) as exc:
        raise network("OpenStreetMap's search answered no point") from exc
    label = hit.get("display_name")
    return lat.rstrip("0").rstrip("."), lon.rstrip("0").rstrip("."), api.one_line(label if isinstance(label, str) else cleaned, 120)


def _place(lat: str, lon: str) -> tuple[dict[str, object], int, int]:
    """The sheet and cell the service says a point belongs to. Public: it needs no login."""
    _status, body = api.get(f"/v1/place?lat={lat}&lon={lon}", token=config.read_token())
    payload = _object(body, "object")
    sheet = _object(payload.get("sheet"), "sheet")
    sheet_id, col, row = sheet.get("id"), payload.get("col"), payload.get("row")
    if not isinstance(sheet_id, str) or config.SHEET_ID.fullmatch(sheet_id) is None:
        raise network("service answer names no sheet")
    for value in (col, row):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise network("service answer names no cell")
    assert isinstance(col, int) and isinstance(row, int)
    return sheet, col, row


def cmd_claim(args: argparse.Namespace) -> int:
    at = getattr(args, "at", None)
    name = getattr(args, "plats", None)
    if at is not None or name is not None:
        if args.sheet or args.cell or args.next or (at is not None and name is not None):
            raise bad_input("Pass one of: a place name with --plats, a point with --at, or a sheet and a cell.")
        if at is not None:
            lat, lon = parse_point(at)
    elif not args.sheet or bool(args.next) == bool(args.cell):
        raise bad_input("Pass a sheet and a cell like 64_59, or a sheet and --next, or a place with --plats or --at.")
    # A lock holds the cell for 48 hours, so a machine that cannot build one does not take one.
    # The machine is asked before the login: on one that cannot build, "log in" is no next step.
    problem = _gpu_missing()
    if problem is not None:
        raise environment(f"this machine cannot build a cell: {problem}")
    # The sheet's row says which source it is drawn from. The place and the grid are public, so
    # this needs no login, and a sheet with no source decided stops here with nothing claimed.
    if name is not None:
        lat, lon, found = find_place(name)
        print(f"found: {found}")
        at = f"{lat},{lon}"
    if at is not None:
        sheet, col, row = _place(lat, lon)
        sheet_id = str(sheet.get("id"))
        print(f"place: {sheet_id} {col}_{row}")
        kind = _source_ready(sheet, sheet_id)
        token = require_token()
        require_terms()
    else:
        sheet_id = args.sheet
        _status, body = api.get(f"/v1/grid/{sheet_id}", token=config.read_token())
        payload = _object(body, "object")
        sheet = _object(payload.get("sheet"), "sheet")
        kind = _source_ready(sheet, sheet_id)
        token = require_token()
        require_terms()
        if args.next:
            col, row = pick_next(sheet, _rows(payload.get("cells"), "cells"))
        else:
            col, row = parse_cell(str(args.cell))
    # The claim says the source this rutor will draw from. On a sheet of the tiling the service
    # refuses a claim that names none: that is how it stops a rutor from before, which would
    # lock the cell and then not know how to draw it.
    try:
        _status, claimed = api.post("/v1/claim", {"sheet": sheet_id, "col": col, "row": row, "kind": kind}, token)
    except RutorError as exc:
        if exc.code == 2 and exc.message.startswith("refused: taken"):
            raise refused(_taken_message(sheet_id, col, row, exc.message)) from exc
        raise
    expires = _object(claimed, "object").get("expires_at")
    print(f"claimed: {sheet_id} {col}_{row} expires {expires}")
    print(f"url: {cell_url(sheet_id, col, row)}")
    return 0


# What holds a cell that cannot be claimed, in words: the service's own word for the state.
_HELD = {
    "last": "is locked by",
    "vantar": "is built and waits for review, from",
    "godkand": "is approved and waits to go on the map, from",
    "live": "is on the map already, from",
}


def _taken_message(sheet_id: str, col: int, row: int, said: str) -> str:
    """Say what holds a taken cell, and the next thing to try.

    The service says only that someone holds it. Whose it is and in which state is on the
    public grid, and a person who asked for a place wants a cell beside it, not a dead end.
    If the grid cannot be read, what the service said stands.
    """
    try:
        _status, body = api.get(f"/v1/grid/{sheet_id}", token=config.read_token())
        cells = _rows(_object(body, "object").get("cells"), "cells")
    except RutorError:
        return said
    for cell in cells:
        if not isinstance(cell, list) or len(cell) < 3 or cell[0] != col or cell[1] != row:
            continue
        held = _HELD.get(str(cell[2]))
        login = cell[3] if len(cell) > 3 else None
        if held is None or not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
            break
        return (
            f"refused: taken: {sheet_id} {col}_{row} {held} {login}. "
            f"For a free cell beside what is drawn there: rutor claim {sheet_id} --next."
        )
    return said


def cmd_release(args: argparse.Namespace) -> int:
    token = require_token()
    col, row = parse_cell(args.cell)
    api.post("/v1/release", {"sheet": args.sheet, "col": col, "row": row}, token)
    print(f"released: {args.sheet} {col}_{row}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    del args
    token = require_token()
    _status, body = api.get("/v1/me", token=token)
    locks = _rows(_object(body, "object").get("locks"), "locks")
    if not locks:
        print("locks: 0")
        return 0
    for lock in locks:
        fields = _object(lock, "lock")
        print(f"lock: {fields.get('sheet')} {fields.get('col')}_{fields.get('row')} expires {fields.get('expires_at')}")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    del args
    token = require_token()
    _status, body = api.get("/v1/me", token=token)
    payload = _object(body, "object")
    print(f"login: {payload.get('login')}")
    trusted = payload.get("trusted")
    if isinstance(trusted, (bool, int)):
        print(f"trusted: {'yes' if trusted else 'no'}")
    approved = payload.get("approved_count")
    if isinstance(approved, int) and not isinstance(approved, bool):
        print(f"approved: {approved}")
    accepted = config.read_terms()
    if login.terms_current():
        print(f"terms: accepted {accepted}")
    elif accepted is None:
        print("terms: not accepted: run rutor terms before a claim or an upload")
    else:
        print(f"terms: accepted {accepted}, today's are {login.TERMS_VERSION}: run rutor terms before a claim or an upload")
    return 0


def _python_missing() -> str | None:
    if sys.version_info >= (3, 11):
        return None
    return f"{sys.version_info.major}.{sys.version_info.minor} is older than 3.11"


GPU_MIN_MIB = 11000
DISK_MIN_GB = 25.0
DISK_MIN_CACHED_GB = 2.0


def _nvidia_cards() -> list[tuple[str, int]]:
    """Name and memory in MiB of each NVIDIA card, asked of the driver. Needs no PyTorch."""
    tool = shutil.which("nvidia-smi")
    if tool is None:
        return []
    try:
        done = subprocess.run(
            [tool, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    cards: list[tuple[str, int]] = []
    for line in done.stdout.splitlines():
        name, _, memory = line.rpartition(",")
        try:
            cards.append((name.strip(), int(float(memory.strip()))))
        except ValueError:
            continue
    return cards


# The lowest NVIDIA driver for Windows that can run the CUDA 12 libraries inside the cu128
# wheels of PyTorch: "CUDA 12.x >= 528.33" in the table "CUDA Toolkit and Minimum Required
# Driver Version for CUDA Minor Version Compatibility" of NVIDIA's CUDA 12.8 release notes,
# https://docs.nvidia.com/cuda/archive/12.8.0/cuda-toolkit-release-notes/index.html
# (read 2026-11-24). The same notes name 570.65 as the driver CUDA 12.8 ships with; a
# driver between the two runs what was built for 12.x, so it is not refused. Linux drivers
# are numbered differently (525.60.13 there), and this number is not used for them.
DRIVER_MIN: tuple[int, int] = (528, 33)


def _nvidia_driver() -> str:
    """The driver version nvidia-smi reports, or "" when it does not answer."""
    tool = shutil.which("nvidia-smi")
    if tool is None:
        return ""
    try:
        done = subprocess.run(
            [tool, "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    # One line for each card, all with the same driver.
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    return lines[0] if lines else ""


def driver_too_old(version: str) -> bool:
    """Whether a Windows driver version is certainly below DRIVER_MIN. Unreadable is not old."""
    parts = version.strip().split(".")
    if len(parts) < 2 or not (parts[0].isascii() and parts[0].isdigit() and parts[1].isascii() and parts[1].isdigit()):
        return False
    return (int(parts[0]), int(parts[1])) < DRIVER_MIN


# The table the AMD kernel driver keeps for ROCm, the same one the ROCm runtime reads to find
# its GPUs. Reading it needs no PyTorch and no ROCm tools, so the machine check stays light.
KFD_NODES = Path("/sys/class/kfd/kfd/topology/nodes")
AMD_VENDOR_ID = 0x1002
# HSA_HEAPTYPE_FRAME_BUFFER_PUBLIC and _PRIVATE: the memory the GPU allocates from. On a
# discrete card that is its own memory. On an APU such as Strix Halo it is the share of system
# memory the driver gives the GPU (GTT), which is also what PyTorch sees as the card's memory.
_KFD_GPU_HEAPS = (1, 2)
# The device the ROCm runtime opens. The table above is readable by every user; this is not.
# A desktop login is usually given it by the seat, an SSH login only through the render group.
KFD_DEVICE = "/dev/kfd"


def kfd_usable() -> bool:
    """Whether this user may open the device ROCm needs."""
    return os.access(KFD_DEVICE, os.R_OK | os.W_OK)


def _kfd_properties(path: Path) -> dict[str, int]:
    """One KFD properties file, as name and number. Unreadable is empty."""
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return {}
    found: dict[str, int] = {}
    for line in text.splitlines():
        name, _, value = line.strip().partition(" ")
        if name and value.strip().isdigit():
            found[name] = int(value.strip())
    return found


def gfx_name(version: int) -> str:
    """The LLVM target of a KFD gfx_target_version: 110501 is gfx1151, 90402 is gfx942."""
    major, minor, stepping = version // 10000, (version // 100) % 100, version % 100
    return f"gfx{major}{minor:x}{stepping:x}"


def _amd_cards() -> list[tuple[str, int]]:
    """Name and memory in MiB of each AMD GPU the ROCm driver offers. Linux only; needs no PyTorch."""
    if not sys.platform.startswith("linux"):
        return []
    try:
        nodes = sorted(KFD_NODES.iterdir())
    except OSError:
        return []
    cards: list[tuple[str, int]] = []
    for node in nodes:
        properties = _kfd_properties(node / "properties")
        # A CPU is a node too, with no SIMDs.
        if properties.get("simd_count", 0) <= 0 or properties.get("vendor_id") != AMD_VENDOR_ID:
            continue
        memory = 0
        try:
            banks = sorted((node / "mem_banks").iterdir())
        except OSError:
            banks = []
        for bank in banks:
            bank_properties = _kfd_properties(bank / "properties")
            if bank_properties.get("heap_type") in _KFD_GPU_HEAPS:
                memory += bank_properties.get("size_in_bytes", 0)
        cards.append((f"AMD {gfx_name(properties.get('gfx_target_version', 0))}", memory // 2**20))
    return cards


def gpu_card() -> tuple[str, str, int] | None:
    """Vendor, name and memory in MiB of the card the redraw runs on, or None.

    An NVIDIA card first: that is the path every cell so far was drawn on. AMD is asked only on a
    machine without one, and then only on Linux, where PyTorch has ROCm builds.
    """
    for vendor, ask in (("nvidia", _nvidia_cards), ("amd", _amd_cards)):
        cards = ask()
        if cards:
            name, memory = max(cards, key=lambda card: card[1])
            return vendor, name, memory
    return None


def _gpu_missing() -> str | None:
    if sys.platform == "darwin":
        return "this is a Mac, and the redraw needs an NVIDIA card. Do not claim a cell here."
    card = gpu_card()
    if card is None:
        return (
            "no NVIDIA card found (nvidia-smi did not answer), and no AMD card the ROCm driver offers. "
            "Do not claim a cell here."
        )
    vendor, name, memory = card
    if memory < GPU_MIN_MIB:
        return f"{name} has {round(memory / 1024)} GB, and the redraw needs 12 GB. Do not claim a cell here."
    if vendor == "amd" and not kfd_usable():
        return (
            f"{name} is here, but this user cannot open {KFD_DEVICE}, so PyTorch would see no GPU. "
            "The account needs the render and video groups: sign out and in again after. Do not claim a cell here."
        )
    return None


def _hub_cache() -> Path:
    explicit = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit)
    base = os.environ.get("HF_HOME")
    return (Path(base) if base else Path.home() / ".cache" / "huggingface") / "hub"


def _model_is_cached() -> bool:
    folder = "models--" + pipeline.MODEL_ID.replace("/", "--")
    revision = pipeline.PINS.get("model_revision", "")
    return bool(revision) and (_hub_cache() / folder / "snapshots" / revision).is_dir()


def _free_gb() -> float:
    path = _hub_cache()
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free / 1e9


def _disk_missing() -> str | None:
    needed = DISK_MIN_CACHED_GB if _model_is_cached() else DISK_MIN_GB
    free = _free_gb()
    if free >= needed:
        return None
    return f"{free:.0f} GB free where the model is kept, and it needs {needed:.0f} GB"


def _torch_missing() -> str | None:
    try:
        import torch
    except ImportError:
        return "PyTorch is not installed"
    cuda = getattr(torch, "cuda", None)
    available = getattr(cuda, "is_available", None)
    if not callable(available) or not available():
        return _torch_build_wrong(torch) or "no CUDA device"
    return _no_kernels_for_card(torch)


def _no_kernels_for_card(torch: object) -> str | None:
    """A ROCm build of PyTorch that has no kernels for the AMD card it sees.

    It still answers yes to is_available, and the first sign is "invalid device function" in the
    middle of a redraw, after the lock and the downloads. The build's own list of targets says it
    before: nothing here is guessed. A CUDA build is not asked, and one that cannot say is let be.
    """
    if not getattr(getattr(torch, "version", None), "hip", None):
        return None
    cuda = getattr(torch, "cuda", None)
    try:
        # gcnArchName carries feature flags after a colon, like gfx90a:sramecc+:xnack-.
        arch = str(cuda.get_device_properties(0).gcnArchName).split(":")[0]
        # A build's list can carry feature flags too, like gfx90a:xnack-. The target is what counts.
        built = [str(name).split(":")[0] for name in cuda.get_arch_list()]
    except Exception:
        return None
    if not arch or not built or arch in built:
        return None
    return f"this ROCm build of PyTorch has no kernels for {arch}, the card here; it was built for {', '.join(built)}"


# The PyTorch index for an AMD card: the build the redraw was tried with on Strix Halo.
ROCM_TORCH_INDEX = "https://download.pytorch.org/whl/rocm7.2"


def _torch_build_wrong(torch: object) -> str | None:
    """A PyTorch built for the other vendor's cards sees no GPU. Say which build is needed."""
    card = gpu_card()
    if card is None:
        return None
    hip = getattr(getattr(torch, "version", None), "hip", None)
    if card[0] == "amd" and not hip:
        return f"PyTorch is not a ROCm build, and this card is AMD: install torch and torchvision from {ROCM_TORCH_INDEX}"
    if card[0] == "nvidia" and hip:
        return "PyTorch is a ROCm build, and this card is NVIDIA: install torch and torchvision from https://download.pytorch.org/whl/cu128"
    return None


def _bitsandbytes_missing() -> str | None:
    try:
        import bitsandbytes
    except ImportError:
        return "bitsandbytes is not installed"
    del bitsandbytes
    return None


def _render_missing() -> str | None:
    try:
        import numpy
        from PIL import Image
    except ImportError:
        return "pillow and numpy are not installed (pip install pillow numpy)"
    del numpy, Image
    return None


def _service_missing() -> str | None:
    try:
        _status, body = api.get("/v1/health")
    except RutorError as exc:
        return exc.message
    if isinstance(body, dict) and body.get("ok") is True:
        return None
    return "health did not answer ok"


def _login_missing() -> str | None:
    token = config.read_token()
    if not token:
        return "not logged in: run rutor login"
    try:
        _status, body = api.get("/v1/me", token=token)
    except RutorError as exc:
        return exc.message
    login = body.get("login") if isinstance(body, dict) else None
    if isinstance(login, str) and login:
        return None
    return "service did not return a login"


def _preflight_kind(sheet_id: str) -> tuple[str, str | None]:
    """The source of the sheet a preflight names, and what is wrong when it cannot be told.

    The two sheets this code knows are answered from its own table, so their preflight works
    with no service at all, as it did. Any other sheet is asked of the service: its grid is
    public and needs no login.
    """
    if not sheet_id:
        return "", None
    known = SHEET_KINDS.get(sheet_id)
    if known is not None:
        return known, None
    try:
        return kind_of(_sheet_body(config.read_token() or "", sheet_id)), None
    except RutorError as exc:
        return "", exc.message


def cmd_preflight(args: argparse.Namespace) -> int:
    # The machine first: python, card and disk need nothing installed and no login, so a person
    # learns whether this machine can build a cell before they accept terms or download anything.
    checks: list[tuple[str, Callable[[], str | None]]] = [
        ("python", _python_missing),
        ("gpu", _gpu_missing),
        ("disk", _disk_missing),
    ]
    # What the sheet's source needs, when a sheet is named. None of it needs a login.
    kind, unknown = _preflight_kind(getattr(args, "sheet", None) or "")
    if unknown is not None:
        checks.append(("sheet", lambda: unknown))
    if kind == "google":
        checks += [
            ("node", google.node_missing),
            ("google renderer", google.renderer_missing),
            ("cesium token", google.token_unusable),
        ]
    if not getattr(args, "machine", False):
        checks += [
            ("torch", _torch_missing),
            ("bitsandbytes", _bitsandbytes_missing),
            ("render", _render_missing),
            ("service", _service_missing),
            ("login", _login_missing),
        ]
    missing = False
    for name, check in checks:
        problem = check()
        if problem is None:
            print(f"check: {name} ok")
        else:
            print(f"check: {name} missing {problem}")
            missing = True
    return 3 if missing else 0


def _sheet_body(token: str, sheet_id: str) -> dict[str, object]:
    _status, body = api.get(f"/v1/grid/{sheet_id}", token=token)
    return _object(_object(body, "object").get("sheet"), "sheet")


def cmd_render(args: argparse.Namespace) -> int:
    token = require_token()
    col, row = parse_cell(args.cell)
    sheet = _sheet_body(token, args.sheet)
    if sheet.get("exists") is False:
        # Nobody has claimed on this sheet, so it has no row: the height and the picture's top
        # in this answer are stand-ins, and a cell drawn from them would lie off its place.
        raise refused(f"refused: not_claimed: nothing is claimed on {args.sheet} yet. Run rutor claim {args.sheet} {col}_{row} first.")
    out = config.work_dir(args.sheet) / f"{col}_{row}.src.png"
    kind = kind_of(sheet)
    if kind == "google":
        for line in GOOGLE_NOTICE:
            print(f"note: {line}")
    measured = render_cell(sheet, col, row, out)
    _write_source_info(args.sheet, kind, out)
    if kind == "google" and sheet.get("made") == "grid" and not args.flat_ok:
        _refuse_flat(args.sheet, f"{col}_{row}", measured, out)
    print(f"rendered: {args.sheet} {col}_{row} {out}")
    return 0


def _refuse_flat(sheet_id: str, cell: str, measured: object, out: Path) -> None:
    """Stop a cell of a new place where nothing stands up from the ground.

    Google has 3D buildings in some places only. Elsewhere its tiles are a photo laid on the
    ground, and the redraw of that is a smoothed photo, not a drawn town: the first cell an
    agent built on its own, in 2026-11, was one. Open water and fields measure the same, so
    this asks a person rather than deciding. A hand-made sheet is not asked: its cells were
    picked from a map that is drawn already.
    """
    try:
        share = float(measured.get("rising", "")) if isinstance(measured, dict) else None
    except ValueError:
        share = None
    if share is None or share >= google.FLAT_SHARE:
        return
    raise check_failed(
        f"flat: {share:.0%} of this cell rises above the ground. Either it is open water or fields, "
        "or Google has no 3D buildings here and the houses are a photo on flat ground, which does not "
        f"redraw into a drawn town. The picture is {out}. Give the cell back: rutor release {sheet_id} {cell}. "
        "Only if a person has looked at the picture and wants it drawn: run the render again with --flat-ok."
    )


GOOGLE_NOTICE = (
    "This sheet is drawn from Google Photorealistic 3D Tiles through your own Cesium ion token.",
    "Google's terms bind that token, not this project's MIT license.",
    "Google can ask for cells drawn from its tiles to be taken down.",
)


def _write_source_info(sheet_id: str, kind: str, png: Path) -> None:
    """Record what the source PNG was made from, next to the PNG itself."""
    if kind == "google":
        rendered = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        info = {
            "provider": f"Google Photorealistic 3D Tiles via Cesium ion asset {google.ION_ASSET}",
            "version": f"cesium {google.CESIUM_VERSION}; rendered {rendered}",
            "sha256": sha256_file(png),
        }
    else:
        source = ortho_source(sheet_id)
        info = {
            "provider": f"{urllib.parse.urlsplit(source.endpoint).netloc} {source.layer}",
            "version": f"osm {osm_date(sheet_id)}; ortho {source.layer}; terrain {terrain_label(sheet_id)}",
            "sha256": sha256_file(png),
        }
    png.with_suffix(".json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


def _se3_main() -> Callable[..., int]:
    try:
        from rutor import se3_price
    except ImportError:
        path = config.repo_root() / "scripts" / "se3_price.py"
        spec = importlib.util.spec_from_file_location("se3_price_script", path)
        if spec is None or spec.loader is None:
            raise environment("se3 price check is missing: scripts/se3_price.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        se3_price = loaded
    main = getattr(se3_price, "main", None)
    if not callable(main):
        raise environment("se3 price check has no main")
    return main


def _report_pins() -> None:
    unset = [name for name, pin in pipeline.PINS.items() if not pin]
    if unset:
        # Said so that a reviewer can see it, and worded so that an agent does not stop at it.
        print("pin: none set for " + ", ".join(unset) + ": nothing to compare, the redraw goes on")
    found: dict[str, str] = {}
    try:
        found["diffusers_version"] = importlib.metadata.version("diffusers")
    except importlib.metadata.PackageNotFoundError as exc:
        # An empty pin cannot mismatch, so it does not block either.
        if pipeline.PINS.get("diffusers_version"):
            raise environment("pin mismatch: diffusers_version, but diffusers is not installed") from exc
    mismatched = pipeline.check_pins(found)
    if mismatched:
        raise environment("pin mismatch: " + ", ".join(mismatched))


def _gpu_name() -> str:
    try:
        import torch
    except ImportError:
        return "unknown"
    cuda = getattr(torch, "cuda", None)
    available = getattr(cuda, "is_available", None)
    if not callable(available) or not available():
        return "unknown"
    device_name = getattr(cuda, "get_device_name", None)
    if not callable(device_name):
        return "unknown"
    return str(device_name(0))


def _source_info(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {"provider": "unknown", "version": "unknown"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise bad_input(f"{path.name} is not JSON") from exc
    payload = _object(raw, path.name)
    source: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            source[key] = value
    if "provider" not in source:
        source["provider"] = "unknown"
    if "version" not in source:
        source["version"] = "unknown"
    return source


def _open_image(path: Path) -> object:
    try:
        from PIL import Image
    except ImportError as exc:
        raise environment("Pillow is not installed. Follow the README install, then run rutor redraw again.") from exc
    return Image.open(path)


def _save_image(image: object, path: Path) -> None:
    save = getattr(image, "save", None)
    if not callable(save):
        raise environment("redraw did not return an image")
    save(path)


def _check_cell(directory: Path, col: int, row: int) -> list[str]:
    """Run the quality check on the source and the redrawn PNG in the work directory."""
    src = directory / f"{col}_{row}.src.png"
    png = directory / f"{col}_{row}.png"
    for path in (src, png):
        if not path.is_file():
            raise bad_input(f"missing {path.name}: run rutor render and rutor redraw first")
    return check_problems(_open_image(src), _open_image(png))


def _record_check(manifest: dict[str, object], found: list[str]) -> None:
    manifest["check"] = "ok" if not found else "failed"
    manifest["check_problems"] = list(found)
    for problem in found:
        print(f"check: {problem}")
    if not found:
        print("check: ok")


def _require_source_unchanged(
    directory: Path,
    col: int,
    row: int,
    manifest: dict[str, object],
    sheet_id: str,
) -> None:
    recorded = manifest.get("source_sha256")
    if not isinstance(recorded, str) or not recorded:
        # A manifest from 0.1.0 has no source sha to compare with.
        return
    src = directory / f"{col}_{row}.src.png"
    if not src.is_file() or sha256_file(src) != recorded:
        raise bad_input(
            f"{sheet_id} {col}_{row}: the source was rendered again after the redraw: "
            f"run rutor redraw {sheet_id} {col}_{row}"
        )


def cmd_check(args: argparse.Namespace) -> int:
    col, row = parse_cell(args.cell)
    directory = config.work_dir(args.sheet)
    manifest_path = directory / f"{col}_{row}.manifest.json"
    if not manifest_path.is_file():
        raise bad_input(f"missing {manifest_path.name}: run rutor redraw {args.sheet} {col}_{row} first")
    manifest = _read_manifest(manifest_path)
    _require_source_unchanged(directory, col, row, manifest, args.sheet)
    found = _check_cell(directory, col, row)
    _record_check(manifest, found)
    # The PNG is what the upload sends, so the manifest describes this file, not the old one.
    manifest["sha256"] = sha256_file(directory / f"{col}_{row}.png")
    write_manifest(manifest, manifest_path)
    if found:
        raise check_failed(f"{args.sheet} {col}_{row} failed the quality check; do not upload it")
    return 0


def _default_seed(sheet_id: str, col: int, row: int) -> int:
    digest = hashlib.sha256(f"{sheet_id}:{col}_{row}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def cmd_redraw(args: argparse.Namespace) -> int:
    col, row = parse_cell(args.cell)
    if args.wait_for_cheap:
        code = int(_se3_main()())
        if code == 2:
            raise wait("the SE3 price is high: keep your lock and run rutor redraw --wait-for-cheap again after the cheap hour")
        if code != 0:
            raise network(
                "the public SE3 price could not be read: keep your lock and try again later, "
                "or run rutor redraw without --wait-for-cheap"
            )
    message = redraw_core.preflight_message()
    if message:
        raise environment(message)
    _report_pins()
    token = require_token()
    directory = config.work_dir(args.sheet)
    src = directory / f"{col}_{row}.src.png"
    if not src.is_file():
        raise bad_input(f"missing {src.name}: run rutor render {args.sheet} {col}_{row} first")
    sheet = _sheet_body(token, args.sheet)
    name = sheet.get("name")
    if not isinstance(name, str) or not name:
        raise bad_input(f"sheet {args.sheet} has no name")
    seed = args.seed if isinstance(args.seed, int) else _default_seed(args.sheet, col, row)
    try:
        image = redraw_core.redraw_image(_open_image(src), pipeline.prompt_for(name), seed)
    except ValueError as exc:
        raise bad_input(str(exc)) from exc
    out = directory / f"{col}_{row}.png"
    _save_image(image, out)
    rendered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = build_manifest(
        pipeline=pipeline.PIPELINE,
        seed=seed,
        png=out,
        source=_source_info(directory / f"{col}_{row}.src.json"),
        gpu=_gpu_name(),
        rendered_at=rendered_at,
        source_sha256=sha256_file(src),
    )
    print(f"redrawn: {args.sheet} {col}_{row} {out}")
    found = _check_cell(directory, col, row)
    _record_check(manifest, found)
    write_manifest(manifest, directory / f"{col}_{row}.manifest.json")
    if found:
        raise check_failed(f"{args.sheet} {col}_{row} failed the quality check; the PNG is kept but must not be uploaded")
    return 0


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise bad_input(f"{path.name} is not JSON") from exc
    return _object(raw, path.name)


def cmd_upload(args: argparse.Namespace) -> int:
    token = require_token()
    require_terms()
    col, row = parse_cell(args.cell)
    directory = config.work_dir(args.sheet)
    png = directory / f"{col}_{row}.png"
    manifest_path = directory / f"{col}_{row}.manifest.json"
    if not png.is_file():
        raise bad_input(f"missing {png.name}: run rutor redraw {args.sheet} {col}_{row} first")
    if not manifest_path.is_file():
        raise bad_input(f"missing {manifest_path.name}: run rutor redraw {args.sheet} {col}_{row} first")
    width, height = png_size(png)
    manifest = _read_manifest(manifest_path)
    told_w = manifest.get("width")
    told_h = manifest.get("height")
    if told_w != width or told_h != height:
        raise bad_input(f"{png.name} is {width}x{height} but the manifest says {told_w}x{told_h}")
    recorded_png = manifest.get("sha256")
    if isinstance(recorded_png, str) and recorded_png and recorded_png != sha256_file(png):
        raise bad_input(
            f"{png.name}: the PNG changed after the manifest was written: "
            f"run rutor check {args.sheet} {col}_{row}"
        )
    _require_source_unchanged(directory, col, row, manifest, args.sheet)
    if manifest.get("check") != "ok":
        raise check_failed(f"{args.sheet} {col}_{row} has not passed rutor check: run rutor check {args.sheet} {col}_{row}")
    _status, body = api.put_bytes(
        f"/v1/upload/{args.sheet}/{col}_{row}",
        png.read_bytes(),
        {"Content-Type": "image/png", "X-Manifest": encode_manifest(manifest)},
        token,
    )
    fields = _object(body, "object")
    print(f"uploaded: {args.sheet} {col}_{row} state {fields.get('state')}")
    print(f"url: {fields.get('url')}")
    return 0


def _queue_cells(token: str, state: str) -> list[object]:
    _status, body = api.get(f"/v1/queue?state={state}", token=token)
    return _rows(_object(body, "object").get("cells"), "cells")


def _waiting_line(cell: dict[str, object]) -> str:
    label = "approved" if cell.get("state") == "godkand" else "waiting"
    return (
        f"{label}: {cell.get('sheet')} {cell.get('col')}_{cell.get('row')} "
        f"{cell.get('owner_login')} uploaded {cell.get('uploaded_at')}"
    )


def cmd_queue(args: argparse.Namespace) -> int:
    token = require_token()
    state = args.state if isinstance(args.state, str) else "vantar"
    if state not in ("vantar", "godkand"):
        raise bad_input("state must be vantar or godkand")
    cells = _queue_cells(token, state)
    parsed: list[dict[str, object]] = []
    for cell in cells:
        fields = _object(cell, "cell")
        parsed.append(fields)
        print(_waiting_line(fields))
    show = args.show
    if not isinstance(show, str) or not show:
        return 0
    dest = Path(show)
    dest.mkdir(parents=True, exist_ok=True)
    for fields in parsed:
        sheet = fields.get("sheet")
        col = fields.get("col")
        row = fields.get("row")
        if not isinstance(sheet, str) or isinstance(col, bool) or not isinstance(col, int):
            raise network("queue cell is missing sheet, col or row")
        if isinstance(row, bool) or not isinstance(row, int):
            raise network("queue cell is missing sheet, col or row")
        data = api.download(f"/v1/inkorg/{sheet}/{col}_{row}.png", token)
        (dest / f"{sheet}_{col}_{row}.png").write_bytes(data)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    token = require_token()
    col, row = parse_cell(args.cell)
    api.post("/v1/approve", {"sheet": args.sheet, "col": col, "row": row}, token)
    print(f"approved: {args.sheet} {col}_{row}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    comment = args.comment.strip() if isinstance(args.comment, str) else ""
    if not comment or len(comment) > 500:
        raise bad_input("Pass --comment with 1 to 500 characters.")
    token = require_token()
    col, row = parse_cell(args.cell)
    api.post(
        "/v1/reject",
        {"sheet": args.sheet, "col": col, "row": row, "comment": comment},
        token,
    )
    print(f"rejected: {args.sheet} {col}_{row}")
    return 0


def cmd_trust(args: argparse.Namespace) -> int:
    token = require_token()
    trusted = 0 if args.off else 1
    api.post("/v1/trust", {"login": args.login, "trusted": trusted}, token)
    print(f"trust: {args.login} {'off' if args.off else 'on'}")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cities = sub.add_parser("cities", help="list sheets")
    cities.add_argument("--json", action="store_true")
    cities.set_defaults(func=cmd_cities)

    grid = sub.add_parser("grid", help="list cells on a sheet")
    grid.add_argument("sheet")
    grid.add_argument("--free", action="store_true")
    grid.add_argument("--json", action="store_true")
    grid.set_defaults(func=cmd_grid)

    claim = sub.add_parser(
        "claim",
        help="lock one cell",
        description="Lock one free cell for 48 hours. Refuses on a machine that cannot build or render it, before anything is locked.",
    )
    claim.add_argument("sheet", nargs="?", help="the sheet, such as stockholm-norr")
    claim.add_argument("cell", nargs="?", help="the cell as col_row, such as 74_20")
    claim.add_argument("--next", action="store_true", help="take the free cell beside the map that is nearest the middle of the sheet")
    claim.add_argument(
        "--at",
        metavar="LAT,LON",
        help="take the cell at this place, such as 56.0465,12.6945: the service says which sheet and cell it is. Instead of a sheet and a cell",
    )
    claim.add_argument(
        "--plats",
        metavar="NAMN",
        help='take the cell at a town, street or landmark, such as Eslöv or "Stortorget Helsingborg": OpenStreetMap is asked where it is. Instead of a sheet and a cell',
    )
    claim.set_defaults(func=cmd_claim)

    release = sub.add_parser("release", help="give a lock back")
    release.add_argument("sheet")
    release.add_argument("cell")
    release.set_defaults(func=cmd_release)

    status = sub.add_parser("status", help="show your locks")
    status.set_defaults(func=cmd_status)

    whoami = sub.add_parser("whoami", help="show the logged-in GitHub login")
    whoami.set_defaults(func=cmd_whoami)

    preflight = sub.add_parser("preflight", help="check the machine before a redraw")
    preflight.add_argument("--machine", action="store_true", help="only python, the graphics card and the disk: needs no install and no login")
    preflight.add_argument("--sheet", help="also check what this sheet's source needs, such as Node and a Cesium ion token")
    preflight.set_defaults(func=cmd_preflight)

    render = sub.add_parser("render", help="render one cell to a source PNG")
    render.add_argument("sheet")
    render.add_argument("cell")
    render.add_argument(
        "--flat-ok",
        action="store_true",
        help="draw a cell of a new place although nothing in it rises above the ground",
    )
    render.set_defaults(func=cmd_render)

    redraw = sub.add_parser("redraw", help="redraw a source PNG into pixel art")
    redraw.add_argument("sheet")
    redraw.add_argument("cell")
    redraw.add_argument("--seed", type=int, default=None)
    redraw.add_argument("--wait-for-cheap", action="store_true")
    redraw.set_defaults(func=cmd_redraw)

    check = sub.add_parser("check", help="run the quality check on a redrawn cell")
    check.add_argument("sheet")
    check.add_argument("cell")
    check.set_defaults(func=cmd_check)

    upload = sub.add_parser("upload", help="upload the redrawn PNG")
    upload.add_argument("sheet")
    upload.add_argument("cell")
    upload.set_defaults(func=cmd_upload)

    queue = sub.add_parser("queue", help="list cells waiting for review")
    queue.add_argument("--state", choices=("vantar", "godkand"), default="vantar")
    queue.add_argument("--show", default=None, help="directory to download inbox PNGs into")
    queue.set_defaults(func=cmd_queue)

    approve = sub.add_parser("approve", help="approve one uploaded cell")
    approve.add_argument("sheet")
    approve.add_argument("cell")
    approve.set_defaults(func=cmd_approve)

    reject = sub.add_parser("reject", help="reject one cell and free it")
    reject.add_argument("sheet")
    reject.add_argument("cell")
    reject.add_argument("--comment", default="")
    reject.set_defaults(func=cmd_reject)

    trust = sub.add_parser("trust", help="mark a login trusted or not")
    trust.add_argument("login")
    trust.add_argument("--off", action="store_true")
    trust.set_defaults(func=cmd_trust)
