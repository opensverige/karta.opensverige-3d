"""The app page: building a map cell from a page on this machine, without a terminal.

The checks below only read. They say whether this machine can draw, which tools
are missing, whether the Cesium token works, what the terms are, what the
electricity price advises and whether this copy of the pipeline is current.
The page owns the sentences. These functions return short words.

Below them, `Builder` holds the state of the person's eight steps and runs the
CLI as a child of this process: first the terms, then the GitHub login, then the
claim, drawing, check and upload of one cell.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import importlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import threading
import urllib.parse
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from rutor import __version__, api, app_install, app_jobs, commands, config, login, pipeline
from rutor.errors import RutorError, bad_input, environment, network
from rutor.localpage import LocalPage, Refusal, Reply, json_reply
from rutor.renderer import google
from rutor.renderer.sources import SHEET_KINDS, kind_of

if TYPE_CHECKING:
    # For the type of one argument only. See price() for why it is not loaded here.
    from rutor import se3_price


# ---- what the page knows before a job


def _platform() -> str:
    return sys.platform


def _posix_ram_bytes() -> int | None:
    sysconf = getattr(os, "sysconf", None)
    if not callable(sysconf):
        return None
    page = sysconf("SC_PAGE_SIZE")
    pages = sysconf("SC_PHYS_PAGES")
    if isinstance(page, int) and isinstance(pages, int) and page > 0 and pages > 0:
        return page * pages
    return None


def _windows_ram_bytes() -> int | None:
    """ullTotalPhys from GlobalMemoryStatusEx. Windows has no sysconf."""
    if sys.platform != "win32":
        return None
    import ctypes

    class _MemoryStatusEx(ctypes.Structure):
        _fields_ = (
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        )

    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(status)
    # A zero return means the call was refused, so there is no number.
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) == 0:
        return None
    total = int(status.ullTotalPhys)
    return total if total > 0 else None


def _ram_gb() -> int | None:
    """Installed memory in whole GiB, or None when this system will not say.

    There is no cutoff here. Whether the memory is enough is a later question.
    """
    for reader in (_posix_ram_bytes, _windows_ram_bytes):
        try:
            total = reader()
        except Exception:
            # A call that raises did not answer. The other way may still know.
            continue
        if isinstance(total, int) and total > 0:
            return round(total / 2**30)
    return None


# Measured on the test PC on 2026-11-24, on the first cell built from the page: the redraw
# child held 12.6 GiB of memory at its peak (and had asked the system for 22.8 GiB). Under
# that plus a margin for the system and the browser the page warns. It does not refuse:
# the only machine tried has 32 GB, so what 16 GB does is not known.
REDRAW_PEAK_GB = 13
MEMORY_MARGIN_GB = 4


def machine() -> dict[str, object]:
    needed = int(commands.DISK_MIN_CACHED_GB if commands._model_is_cached() else commands.DISK_MIN_GB)
    left = app_install.left_to_fetch_gb()
    if left is not None and left > 0:
        # An app install fetches everything itself, and knows how much is left. Asking for
        # the whole amount again would stop a fetch that is half done from going on.
        needed = max(int(commands.DISK_MIN_CACHED_GB), math.ceil(left + app_install.HEADROOM_GB))
    free = commands._free_gb()
    card = ""
    card_gb = 0
    driver = ""
    problem = ""
    # A Mac has no NVIDIA driver to ask, and the redraw cannot run here.
    if _platform() == "darwin":
        problem = "mac"
    else:
        found = commands.gpu_card()
        if found is None:
            problem = "no_card"
        else:
            vendor, name, mib = found
            # The name comes from the driver. It is shown, so it is one short line.
            card = api.one_line(name, 60)
            card_gb = round(mib / 1024)
            # The driver version is NVIDIA's. An AMD card is asked only on Linux, where no
            # driver number is held against it.
            driver = api.one_line(commands._nvidia_driver(), 20) if vendor == "nvidia" else ""
            if mib < commands.GPU_MIN_MIB:
                problem = "small_card"
            elif vendor == "amd" and not commands.kfd_usable():
                # The card is listed to everyone, but ROCm opens /dev/kfd, and this user may not.
                problem = "no_rocm"
            elif _platform() == "win32" and vendor == "nvidia" and commands.driver_too_old(driver):
                # Said here, before anything is fetched: PyTorch would fail on it much later.
                problem = "old_driver"
            elif free < needed:
                problem = "disk"
    ram = _ram_gb()
    # A warning is for a machine that may draw. One that gets a no has heard enough.
    little = problem == "" and ram is not None and ram < REDRAW_PEAK_GB + MEMORY_MARGIN_GB
    return {
        "ok": problem == "",
        "card": card,
        "card_gb": card_gb,
        "driver": driver,
        "free_gb": int(free),
        "needed_gb": needed,
        "ram_gb": ram,
        "problem": problem,
        "warning": "memory" if little else "",
    }


_MODEL_STACK = ("torch", "bitsandbytes")
# Run by a child of this process. It prints one line for each: "torch ok", "torch missing".
_MODEL_STACK_SCRIPT = (
    "from rutor import commands\n"
    "checks = (('torch', commands._torch_missing), ('bitsandbytes', commands._bitsandbytes_missing))\n"
    "for name, check in checks:\n"
    "    print(name, 'missing' if check() is not None else 'ok', flush=True)\n"
)
_MODEL_STACK_SECONDS = 120


def _model_stack_missing() -> list[str]:
    """Which of PyTorch and bitsandbytes this Python lacks, asked in a child that ends.

    Importing them here would leave PyTorch in the page's memory for as long as the page is
    open, beside the redraw that needs the memory, and a reinstalled PyTorch would not be
    seen until the page was started again. A child that cannot answer counts as missing.
    """
    try:
        done = subprocess.run(
            [sys.executable, "-c", _MODEL_STACK_SCRIPT],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_MODEL_STACK_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return list(_MODEL_STACK)
    present = {line.split()[0] for line in done.stdout.splitlines() if line.split()[1:] == ["ok"]}
    return [name for name in _MODEL_STACK if name not in present]


def tools(sheet_kind: str) -> dict[str, object]:
    missing: list[str] = _model_stack_missing()
    if commands._render_missing() is not None:
        missing.append("render")
    # An open-data sheet has no Node render, so those checks are not asked.
    if sheet_kind == "google":
        if google.node_missing() is not None:
            missing.append("node")
        if google.renderer_missing() is not None:
            missing.append("google_renderer")
    # The first drawing downloads the model, so a cold cache is not "missing".
    return {"ready": not missing, "model_cached": commands._model_is_cached(), "missing": missing}


def _page_token(candidate: str | None) -> str:
    """The token to judge. A paste, even a blank one, does not fall through."""
    if candidate is not None:
        return candidate.strip()
    # The same order as config.cesium_env: the token pasted into the page, else the variable.
    stored = config.read_cesium_token()
    if stored:
        return stored.strip()
    return os.environ.get(config.CESIUM_TOKEN_ENV, "").strip()


def cesium(candidate: str | None = None, ask: google.Asker | None = None) -> dict[str, object]:
    token = _page_token(candidate)
    if not token:
        return {"set": False, "ok": False, "problem": "missing"}
    # A space or a line break inside the token is a bad paste. Cesium is not asked.
    # So is anything that is not plain ASCII: a token has none, and a header cannot carry it.
    if not token.isascii() or any(character.isspace() or not character.isprintable() for character in token):
        return {"set": True, "ok": False, "problem": "broken"}
    try:
        # Cesium's own code is not one of the page's words. The token stays here.
        status = (ask or google._ask_ion)(token)[0]
    except RutorError:
        return {"set": True, "ok": False, "problem": "unreachable"}
    if status == 200:
        return {"set": True, "ok": True, "problem": ""}
    if status == 401:
        # Two dots is a token's shape. Any other count was pasted twice or cut.
        problem = "refused" if token.count(".") == 2 else "twice"
        return {"set": True, "ok": False, "problem": problem}
    if status in (403, 404):
        return {"set": True, "ok": False, "problem": "no_access"}
    return {"set": True, "ok": False, "problem": "unreachable"}


def terms() -> list[str]:
    return login.TERMS.split("\n")


def price(
    fetch: Callable[[date], list[se3_price.Slot] | None] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, str]:
    """What se3_price.main decides, up to evaluate. A price that cannot be read never stops the page."""
    try:
        # Loaded here and not with this module: the price is advice, and a machine where the
        # price check cannot even load must still be able to start every rutor command.
        prices = importlib.import_module("rutor.se3_price")
        now = clock() if clock is not None else datetime.now(prices.STOCKHOLM)
        now = now.astimezone(prices.STOCKHOLM)
        read = fetch if fetch is not None else prices.fetch_prices
        today = read(now.date())
        next_date = now.date() + timedelta(days=1)
        tomorrow = read(next_date)
        report = prices.evaluate(now, today, tomorrow, next_date)
        decision = report.get("decision")
        if decision == "RUN":
            return {"decision": "RUN", "after": ""}
        if decision == "WAIT":
            start = report.get("cheapest_hour_start")
            after = start if isinstance(start, str) else ""
            return {"decision": "WAIT", "after": after}
    except Exception:
        # The price is advice. A site that does not answer must not stop the page.
        return {"decision": "unknown", "after": ""}
    # A decision other than RUN or WAIT is not one the page can show.
    return {"decision": "unknown", "after": ""}


def version(sheet: dict[str, object]) -> dict[str, object]:
    raw = sheet.get("pipeline")
    # The service names the pipeline it wants. What it says is shown, so it is one short line.
    wanted = api.one_line(raw, 40) if isinstance(raw, str) else ""
    mine = pipeline.PIPELINE
    # Nothing said means this copy is current: the service did not ask for another.
    return {"mine": mine, "wanted": wanted, "current": wanted == "" or wanted == mine}


# ---- the app's own version: is a newer installer out?


# What GitHub answered about the releases, already parsed. It raises on any failure.
ReleaseFetch = Callable[[], object]

# The releases page of the app's own repo. Only a list is asked for: it is public, so no
# login token goes with it. Nothing is downloaded from here.
RELEASES_URL = "https://api.github.com/repos/opensverige/karta.opensverige-3d/releases?per_page=30"
RELEASES_SECONDS = 10
# An answer is a list of thirty releases, a few hundred bytes each. This is far more than
# that, and it keeps a page that is not a list of releases from filling the page's memory.
RELEASES_BYTES = 512_000
# The installer tags a release app-v0.1.0. Any other tag on the repo is not the app's.
_APP_TAG = re.compile(r"app-v(\d{1,4}(?:\.\d{1,4}){0,3})")
# One version as the service and the installer write it: 0.1.0, or 1.2.
_VERSION = re.compile(r"\d{1,4}(?:\.\d{1,4}){0,3}")
# A release page under the app's own repo. Nothing else is ever a link on the page.
_RELEASE_URL = re.compile(r"https://github\.com/opensverige/karta\.opensverige-3d/releases/[A-Za-z0-9._/-]{1,100}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would send the request wherever it points, so none is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, msg, headers, newurl
        raise network(f"GitHub answered with a redirect ({code}); nothing was read from it")


def _fetch_releases() -> object:
    """What GitHub says about the releases. Every failure is raised, for app_version to drop."""
    request = urllib.request.Request(
        RELEASES_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "rutor"},
    )
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=RELEASES_SECONDS) as response:
        raw = response.read(RELEASES_BYTES)
    return json.loads(raw.decode("utf-8"))


def _numbers(text: str) -> tuple[int, ...]:
    """A version as four numbers, so that 0.2 and 0.2.0 are the same version."""
    parts = [int(part) for part in text.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def _latest_app(releases: object) -> tuple[str, str]:
    """The highest app version GitHub named and that release's address, or ("", "").

    Everything here comes from outside, so only the tag shape is read and only the address
    shape of the release that won is passed on.
    """
    if not isinstance(releases, list):
        return "", ""
    highest: tuple[int, ...] = ()
    latest = ""
    url = ""
    for release in releases:
        if not isinstance(release, dict):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        matched = _APP_TAG.fullmatch(tag)
        if matched is None:
            continue
        numbers = _numbers(matched.group(1))
        # Compared as numbers, so 0.10.0 is newer than 0.9.0.
        if numbers <= highest:
            continue
        highest = numbers
        latest = matched.group(1)
        address = release.get("html_url")
        # A release with an address elsewhere still says a newer app is out. It is just no link.
        url = address if isinstance(address, str) and _is_release_page(address) else ""
    return latest, url


def _is_release_page(address: str) -> bool:
    """A page under the project's own releases, with no step that climbs out of them."""
    if _RELEASE_URL.fullmatch(address) is None:
        return False
    return ".." not in address and "//" not in address.removeprefix("https://")


def app_version(fetch: ReleaseFetch | None = None) -> dict[str, object]:
    """{"mine": str, "latest": str, "current": bool, "url": str} for the app itself.

    Only an app install asks GitHub. A clone was not downloaded from anywhere, so it has no
    newer installer to be told about. Every failure is the same answer: no news.
    """
    install = config.app_install()
    if install is None:
        return {"mine": __version__, "latest": "", "current": True, "url": ""}
    mine = install["version"]
    try:
        raw = (fetch if fetch is not None else _fetch_releases)()
    except Exception:
        # A page that cannot be reached, is slow or answers with another shape is no news.
        # Nothing of what it said is kept: this process holds a login.
        return {"mine": mine, "latest": "", "current": True, "url": ""}
    latest, url = _latest_app(raw)
    return {"mine": mine, "latest": latest, "current": _is_current(mine, latest), "url": url}


def _is_current(mine: str, latest: str) -> bool:
    """Whether the app here is at least as new as the one GitHub named."""
    if _VERSION.fullmatch(mine) is None or _VERSION.fullmatch(latest) is None:
        # A version of another shape cannot be compared, and an app that cannot be said to
        # be old is not called old.
        return True
    return _numbers(latest) <= _numbers(mine)


# ---- the eight steps


# What a child prints is data. These are the only shapes the page keeps. A code and a
# login name are printed again in the page and put in a URL, so anything else is dropped.
_CODE_SHAPE = re.compile(r"[A-Za-z0-9-]{1,20}")
_NAME_SHAPE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")

# A browser lets a hidden tab run its timers only about once a minute, so a page that has
# asked nothing for half a minute may be a tab nobody has touched. Two and a half minutes
# without a request is a page that has gone, not one that is asleep.
GONE_SECONDS = 150.0

# Enough lines to see how a drawing went, without a render's whole log in memory.
LINES_KEPT = 20

# The phases in which a job is out and the page must not start another.
_RUNNING = frozenset({"claim", "render", "redraw", "upload"})

# The token goes to the two commands that read it and to no other: claim asks the sheet's
# source before it locks a cell on a Google sheet, and render starts the Google renderer.
_TOKEN_COMMANDS = frozenset({"claim", "render"})

# What a command's exit means to the page. 2 is also how a refusal ends, which the claim
# itself says with its last error line.
_PROBLEM_BY_EXIT = {2: "refused", 3: "machine", 4: "network", 5: "check"}

# How a claim ends when it is certain that no lock was given: the service refused (2), or
# this machine was not ready and the service was never asked (3). Bad input (1) is certain
# too, but only when the command said so itself: a command that crashed also ends with 1.
_NO_LOCK_EXITS = frozenset({2, 3})

# Refusals of a claim that the page has a sentence of its own for, by the service's word.
_CLAIM_REFUSALS = (("taken", "taken"), ("too_many_locks", "locks"), ("too_many_waiting", "waiting"))

# What a release is told when there is no lock of ours to give back. The first means the
# service has the picture: the upload went in although its answer never came.
_UPLOADED_ALREADY = "refused: already_uploaded"
_NO_LOCK_OF_OURS = ("refused: not_yours", "refused: no_cell", _UPLOADED_ALREADY)

# Failures the page has a sentence of its own for, by a word in the command's last error
# line. A person who is told to try again tries again, so "try again" is said only when
# trying again can help.
_SAID_WHY = (
    ("refused: daily_limit", "daily"),
    ("refused: wrong_pipeline", "old_app"),
    ("quota is used up", "quota"),
    ("refused the token", "token"),
    ("did not let this token", "token"),
    ("flat: ", "flat"),
)

# How long a stop waits for an upload to end by itself before the upload is ended too.
UPLOAD_GRACE_SECONDS = 45.0

# A stored login the service could not be asked about is asked about again, this long after
# the ask that failed ended. The ask itself is short: the page waits for it.
LOGIN_RETRY_SECONDS = 30.0
LOGIN_ASK_SECONDS = 5.0


def my_locks() -> list[tuple[str, str]]:
    """The cells the logged-in person holds a lock on, as (sheet, cell). Empty when unknown."""
    token = config.read_token()
    if not token:
        return []
    try:
        _status, me = api.get("/v1/me", token=token)
    except RutorError:
        return []
    rows = me.get("locks") if isinstance(me, dict) else None
    found: list[tuple[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        sheet, col, lock_row = row.get("sheet"), row.get("col"), row.get("row")
        # What the service says becomes a command's arguments, so it is checked like input.
        if (
            isinstance(sheet, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,60}", sheet)
            and isinstance(col, int)
            and isinstance(lock_row, int)
            and not isinstance(col, bool)
            and not isinstance(lock_row, bool)
            and 0 <= col < 100_000
            and 0 <= lock_row < 100_000
        ):
            found.append((sheet, f"{col}_{lock_row}"))
    return found


def refuse_old_pipeline(sheet: dict[str, object]) -> None:
    """Refuse to draw for a sheet that wants another pipeline than this copy has.

    The service refuses such a cell at upload, which is after the whole drawing.
    """
    if version(sheet)["current"] is not True:
        raise Refusal(409, "this copy is too old for the sheet", "old_app")


class Builder:
    """The state of the page's steps, and the children it runs for the person.

    The GitHub login token is never held here. When the page asks the service who the
    person is, it reads the token from the file at that moment, hands it over and lets it
    go, so no answer, log line or exception from here can carry it.

    One lock guards all the state. Nothing that can take time is done while it is held: a
    child start, a network ask and a thread join all happen outside it.
    """

    def __init__(
        self,
        start: app_jobs.Start = app_jobs.start_rutor,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._start = start
        # The page's own clock: what build() counts as "seen", and when a page has gone.
        self._clock = clock
        self._lock = threading.Lock()
        self._login = ""
        self._code = ""
        self._url = ""
        self._waiting = False
        self._problem = ""
        self._looked = False
        # When the service was last asked about a stored login and did not answer.
        self._asked_at: float | None = None
        # Set once the page is closing. Nothing is started after that.
        self._closed = False
        # The login child, only while it is being read. A child that has ended is never
        # stopped: stopping signals its process group, and that number may be reused.
        self._run: app_jobs.Run | None = None
        self._thread: threading.Thread | None = None
        # The job: the cell the page chose, what its commands have said, and the child
        # that is being read right now. A finished child is dropped from `_job_run`.
        self._sheet = ""
        self._col = 0
        self._row = 0
        # The source of the chosen sheet, as the page was told it. None: nobody said, and the
        # two sheets the code knows are looked up when the build starts.
        self._kind: str | None = None
        self._phase = "idle"
        self._detail = ""
        self._job_problem = ""
        self._job_url = ""
        # The cell this page holds a lock on, as (sheet, cell), or None. It names the cell
        # because the one being built now may be another than the one still locked.
        self._held: tuple[str, str] | None = None
        self._stop_asked = False
        # A claim that ended without saying whether the service gave the lock.
        self._unsure = False
        self._releasing = False
        # A lock found from before, which stands in the way of a claim. It is given back
        # only when the person presses again for the cell that was refused.
        self._adopted = False
        # A stop was asked for while the upload was out, and waits for it to end.
        self._finishing = False
        # The release after a failed upload has been tried already, inside the job.
        self._asked_back = False
        self._lines: list[str] = []
        self._seen = clock()
        self._job_run: app_jobs.Run | None = None
        self._job_thread: threading.Thread | None = None

    def accept_terms(self) -> None:
        # Kept at once, as `rutor terms` keeps it: a person who is logged in already says yes
        # to new terms here, and no new login is made for it. The page has shown the lines.
        login.keep_terms()

    def terms_accepted(self) -> bool:
        # The yes kept on this machine, not the login: the terms can change after a login.
        return login.terms_current()

    def start_login(self) -> None:
        self._remember_stored_login()
        if not login.terms_current():
            raise Refusal(409, "accept the terms first", "terms")
        with self._lock:
            # A second press while the code is on screen must not start a second child.
            if self._waiting or self._closed:
                return
            self._code = ""
            self._url = ""
            self._problem = ""
            self._waiting = True
        try:
            run = self._start(["login", "--accept-terms"], {})
        except OSError:
            # The child could not start, which is a login that ended before it began.
            with self._lock:
                self._waiting = False
                self._problem = "expired"
            return
        # The login can last minutes, so the page reads it on its own thread and returns.
        thread = threading.Thread(target=self._read_login, args=(run,), daemon=True)
        with self._lock:
            closed = self._closed
            if not closed:
                self._run = run
            self._thread = thread
        if closed:
            # The page closed while the child was starting. It is not left behind.
            run.stop()
        thread.start()

    def _read_login(self, run: app_jobs.Run) -> None:
        name = ""
        exit_code = 1
        try:
            for line in run.lines():
                # The child prints the ten terms first, and other lines are not the page's.
                if line.kind == "code" and _CODE_SHAPE.fullmatch(line.text):
                    with self._lock:
                        self._code = line.text
                elif line.kind == "url" and line.text == login.DEVICE_URL:
                    # Only GitHub's own address is passed on, so no other page is offered.
                    with self._lock:
                        self._url = line.text
                elif line.kind == "logged in" and _NAME_SHAPE.fullmatch(line.text):
                    name = line.text
                    with self._lock:
                        self._login = name
            exit_code = run.wait()
        except Exception as exc:  # noqa: BLE001 - the last stop on this thread
            # The page must not wait for ever on a reader that broke. The kind of fault goes
            # to the terminal, never its message: this process holds the login.
            print(f"app: {type(exc).__name__} while reading the login", file=sys.stderr)
        with self._lock:
            self._run = None
            self._waiting = False
            self._code = ""
            self._url = ""
            if name:
                self._problem = ""
            else:
                # 4 is the CLI's network code, and 2 is a no: the person pressed cancel at
                # GitHub, or the service would not have them. Any other end is a code nobody
                # approved in time.
                self._problem = {4: "network", 2: "denied"}.get(exit_code, "expired")

    def login_state(self) -> dict[str, object]:
        self._remember_stored_login()
        with self._lock:
            return {
                "login": self._login,
                "code": self._code,
                "url": self._url,
                "waiting": self._waiting,
                "problem": self._problem,
            }

    def _remember_stored_login(self) -> None:
        """Ask the service who a stored token belongs to, until it has answered once."""
        with self._lock:
            if self._looked:
                return
            now = self._clock()
            if self._asked_at is not None and now - self._asked_at < LOGIN_RETRY_SECONDS:
                return
            self._asked_at = now
        token = config.read_token()
        if not token:
            with self._lock:
                self._looked = True
            return
        try:
            _status, me = api.get("/v1/me", token=token, timeout=LOGIN_ASK_SECONDS)
        except RutorError as exc:
            # A refusal is an answer: nobody is logged in. A service that could not be
            # reached is not, and a person who logged in yesterday is not asked to do it
            # again because the network was slow at start.
            with self._lock:
                if exc.code != 4:
                    self._looked = True
                # Counted from now, when the ask ended: a service that is slow to fail
                # would otherwise be asked again by the very next look.
                self._asked_at = self._clock()
            return
        with self._lock:
            self._looked = True
        name = me.get("login") if isinstance(me, dict) else None
        if isinstance(name, str) and _NAME_SHAPE.fullmatch(name):
            with self._lock:
                # A name a login child has just read is fresher than the stored token's.
                if not self._login:
                    self._login = name

    # ---- the job: one cell, claimed, drawn and uploaded

    def choose(self, sheet: str, col: int, row: int, kind: str | None = None) -> None:
        """Note the cell the person picked, and the source its sheet is drawn from.

        This takes no lock and starts nothing.
        """
        with self._lock:
            if self._busy():
                raise Refusal(409, "a cell is being built", "busy")
            if self._adopted and (sheet, col, row) != (self._sheet, self._col, self._row):
                # The lock from before was to be given back for the cell that was refused.
                # Another cell is another question, and the lock may be one the person is
                # drawing in a terminal.
                self._held = None
                self._adopted = False
            self._sheet = sheet
            self._col = col
            self._row = row
            self._kind = kind
            self._phase = "idle"
            self._detail = ""
            self._job_problem = ""
            self._job_url = ""

    def chosen(self) -> dict[str, object] | None:
        with self._lock:
            if not self._sheet:
                return None
            return {"sheet": self._sheet, "col": self._col, "row": self._row}

    def build(self) -> None:
        """Start the job for the chosen cell, on its own thread."""
        with self._lock:
            if self._closed:
                raise Refusal(409, "the page is closing", "busy")
            if self._busy():
                raise Refusal(409, "a cell is being built", "busy")
            sheet, col, row = self._sheet, self._col, self._row
            told = self._kind
            done = self._phase == "done"
        if not sheet or done:
            # A cell that has gone in is not built twice: the service would call it taken.
            raise Refusal(409, "choose a cell first", "no_cell")
        # The machine, the tools, the login and the token are asked here: they read the
        # disk and, for a stored login, the service. None of that happens under the lock.
        kind = told if told is not None else SHEET_KINDS.get(sheet, "")
        if not machine()["ok"]:
            raise Refusal(409, "this machine cannot draw a cell yet", "not_ready")
        if not tools(kind)["ready"]:
            raise Refusal(409, "a tool this build needs is missing", "not_ready")
        if not self.login_state()["login"]:
            raise Refusal(409, "log in before building", "not_ready")
        if kind == "google" and not cesium()["ok"]:
            raise Refusal(409, "the Cesium token does not work", "not_ready")
        with self._lock:
            # The page may have closed, or another tab pressed Rita, meanwhile.
            if self._closed:
                raise Refusal(409, "the page is closing", "busy")
            if self._busy() or (self._sheet, self._col, self._row) != (sheet, col, row):
                # Another tab chose another cell while this one was being checked.
                raise Refusal(409, "a cell is being built", "busy")
            # Starting counts as the page being here, and a new job starts a new report.
            self._seen = self._clock()
            self._stop_asked = False
            self._unsure = False
            self._phase = "claim"
            self._detail = ""
            self._job_problem = ""
            self._job_url = ""
            self._lines = []
            thread = threading.Thread(target=self._run_job, args=(sheet, col, row), daemon=True)
            self._job_thread = thread
        thread.start()

    def stop(self) -> None:
        """End the job and give its lock back. Returns once the lock is out."""
        with self._lock:
            running = self._phase in _RUNNING
            if running:
                self._stop_asked = True
                # An upload takes seconds, and ending it halfway leaves a cell that the
                # service may have and the page does not know about. It is given a while
                # to finish by itself.
                run = self._job_run
                # Only while the upload itself is out. The lock being asked back after an
                # upload that failed is the same phase, and is not an upload finishing.
                uploading = self._phase == "upload" and run is not None and not self._asked_back
                self._finishing = uploading
            else:
                uploading = False
                run = None
            thread = self._job_thread
        if uploading and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=UPLOAD_GRACE_SECONDS)
            with self._lock:
                self._finishing = False
                # Still the same child, still out: it is ended like any other.
                run = self._job_run if self._job_run is run else None
        if run is not None:
            # Only the child being read is stopped. A finished one is not touched.
            run.stop()
        if thread is not None and thread is not threading.current_thread():
            # So that when stop() returns, the release has been tried and the report is
            # final. The timeout keeps a child that will not die from hanging the page.
            thread.join(timeout=120)

    def seen(self) -> None:
        with self._lock:
            self._seen = self._clock()

    def away(self) -> bool:
        """Whether the page has asked for nothing for so long that it has gone."""
        with self._lock:
            return self._clock() - self._seen > GONE_SECONDS

    def quiet_for(self) -> float:
        """Seconds since the page last asked for its state."""
        with self._lock:
            return self._clock() - self._seen

    def busy(self) -> bool:
        """Whether a cell is being built, or its lock is still being given back."""
        with self._lock:
            return self._busy()

    def tick(self) -> None:
        """Stop a job whose page has not asked for anything for a long time."""
        with self._lock:
            gone = self._phase in _RUNNING and self._clock() - self._seen > GONE_SECONDS
        if gone:
            # A closed tab is not a stopped job: the lock is given back the same way.
            self.stop()

    def job_state(self) -> dict[str, object]:
        with self._lock:
            return {
                "phase": self._phase,
                "detail": self._detail,
                "problem": self._job_problem,
                "url": self._job_url,
                "sheet": self._sheet,
                "col": self._col,
                "row": self._row,
                "held": self._held is not None,
                # The cell a lock from before is on, for the sentence that names it.
                "old": f"{self._held[0]} {self._held[1]}" if self._adopted and self._held else "",
                "finishing": self._finishing,
            }

    def job_lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def _busy(self) -> bool:
        # A lock that is being given back is still out, so nothing new may start. Callers
        # hold the lock.
        return self._phase in _RUNNING or self._releasing

    def _run_job(self, sheet: str, col: int, row: int) -> None:
        cell = f"{col}_{row}"
        try:
            problem = self._run_commands(sheet, cell)
        except Exception as exc:  # noqa: BLE001 - the last stop on this thread
            # The page must not wait for ever on a job that broke. The kind of fault goes
            # to the terminal, never its message: a child's output may be in it.
            print(f"app: {type(exc).__name__} while building", file=sys.stderr)
            problem = "failed"
        with self._lock:
            if problem == "":
                # The service has the cell. A stop that came after that changes nothing.
                self._phase = "done"
                self._job_problem = ""
            elif self._stop_asked:
                self._phase = "stopped"
                self._job_problem = ""
                if self._adopted:
                    # A lock from before was found as the stop came, and the page will not
                    # say so. It is forgotten, so that no later press gives it back unasked.
                    self._held = None
                    self._adopted = False
            else:
                self._phase = "failed"
                self._job_problem = problem
            # No command runs now, so there is no progress line left to show.
            self._detail = ""
            self._finishing = False
            # An earlier cell's lock that was just asked back in vain is not asked a third time,
            # and neither is one that was asked back inside the job, after a failed upload.
            # One found from before is given back when the person presses again, not behind
            # their back: it may be a cell they are drawing in a terminal.
            kept = problem in ("lock_left", "old_lock") or self._asked_back
            self._asked_back = False
            held = None if kept else self._held
            unsure = self._unsure and held is None and not kept
            # A lock that is still out is given back before another job may start.
            self._releasing = held is not None or unsure
        if held is not None or unsure:
            try:
                # A lock that is known is asked back twice. One that only may exist is asked
                # back once.
                if held is not None:
                    answer = self._release(held[0], held[1], tries=2)
                else:
                    answer = self._release(sheet, cell, tries=1)
                    if answer == "":
                        # Nobody answered, so the lock may still be out. It is remembered,
                        # and asked back by its own name before the next cell is claimed.
                        with self._lock:
                            self._held = (sheet, cell)
            except Exception as exc:  # noqa: BLE001 - the page still has to learn it ended
                print(f"app: {type(exc).__name__} while releasing", file=sys.stderr)
            finally:
                with self._lock:
                    self._releasing = False

    def _run_commands(self, sheet: str, cell: str) -> str:
        """Run the four commands for one cell. "" when the upload went out, else the problem."""
        with self._lock:
            left = self._held
        if left is not None:
            # A lock from an earlier job that could not be given back then. A new builder may
            # hold one cell, so it is asked back before another is claimed, by its own name.
            answer = self._release(left[0], left[1], tries=2)
            with self._lock:
                self._adopted = False
                if self._held is not None:
                    return "lock_left"
            if answer == "uploaded" and left == (sheet, cell):
                # This very cell went in last time: its upload was stored, and the answer and
                # the release after it were both lost. It waits for review. It is not claimed
                # again, which the service would refuse as taken.
                return ""
        for command in ("claim", "render", "redraw", "upload"):
            with self._lock:
                if self._stop_asked:
                    # The loop ends here; the job thread reports it as stopped.
                    return "stopped"
                self._phase = command
                self._detail = ""
            env = config.cesium_env() if command in _TOKEN_COMMANDS else {}
            try:
                run = self._start([command, sheet, cell], env)
            except OSError:
                # A child that cannot start is a command that never ran.
                return "failed"
            last_error = ""
            taken_in = False
            with self._lock:
                self._job_run = run
                stop_now = self._stop_asked
            if stop_now:
                # The stop came while the child was starting and found nothing to stop.
                run.stop()
            try:
                for line in run.lines():
                    self._keep(line, detail=True)
                    if command == "claim" and line.kind == "claimed":
                        with self._lock:
                            self._held = (sheet, cell)
                    elif command == "upload" and line.kind == "uploaded":
                        # The service has the cell from here on, and the lock is spent.
                        taken_in = True
                        with self._lock:
                            self._held = None
                    elif command == "upload" and line.kind == "url":
                        # Only the map's own address is handed to the page.
                        if line.text.startswith(f"{config.MAP_URL}#"):
                            with self._lock:
                                self._job_url = line.text
                    if line.kind == "error":
                        # The CLI says why it refused in its last error line.
                        last_error = line.text
                exit_code = run.wait()
            except Exception:
                # A child whose output breaks is still up, and a render holds the card.
                run.stop()
                raise
            finally:
                with self._lock:
                    if self._job_run is run:
                        self._job_run = None
            if exit_code == 0 or taken_in:
                with self._lock:
                    if command == "claim":
                        self._held = (sheet, cell)
                    elif command == "upload":
                        # The cell waits for review on the map; no lock is out any more.
                        self._held = None
                continue
            if command == "claim":
                # Bad input is said in an error line. A 1 with no such line is a crash.
                certain = exit_code in _NO_LOCK_EXITS or (exit_code == 1 and last_error != "")
                with self._lock:
                    # Stopped, cut off by the network or broken: the service may have given
                    # the lock without the claim living to say so. A stopped child's exit
                    # says nothing: a signal gives -15, taskkill on Windows gives 1.
                    if self._stop_asked or not certain:
                        self._unsure = True
                if exit_code == 2:
                    for said, word in _CLAIM_REFUSALS:
                        if last_error.startswith(f"refused: {said}"):
                            if word == "locks" and self._adopt_lock():
                                return "old_lock"
                            return word
            problem = _PROBLEM_BY_EXIT.get(exit_code, "failed")
            for said, word in _SAID_WHY:
                if said in last_error:
                    problem = word
                    break
            if command == "upload":
                # An upload can be stored without its answer coming back. The lock is asked
                # back here, while the job is still the upload, and the service's answer
                # says which it was: the page is never told "failed" and then "done".
                with self._lock:
                    self._asked_back = True
                if self._release(sheet, cell, tries=2) == "uploaded":
                    return ""
            return problem
        return ""

    def _adopt_lock(self) -> bool:
        """Find the lock that stands in the way of a claim, so that it can be given back.

        A lock outlives the app that took it: the machine restarted, or the app was ended,
        in the middle of a cell. The page then holds a lock it does not know about, a new
        builder may hold one, and nothing on the page could release it for two days.
        """
        locks = my_locks()
        if not locks:
            return False
        with self._lock:
            if self._held is None:
                self._held = locks[0]
                self._adopted = True
        return True

    def _release(self, sheet: str, cell: str, tries: int) -> str:
        """Give the lock back, twice at most: the service ends an abandoned lock by itself.

        Returns "released", "uploaded" (the service has the cell's picture, so there is no
        lock), "none" (the service says the lock is not ours) or "" when nobody answered.
        """
        for _attempt in range(tries):
            try:
                run = self._start(["release", sheet, cell], {})
            except OSError:
                continue
            # A release child is never stopped: it ends at once, and its group number may
            # already belong to somebody else's program. It is read so nothing is left.
            last_error = ""
            for line in run.lines():
                self._keep(line, detail=False)
                if line.kind == "error":
                    last_error = line.text
            code = run.wait()
            # The service's own no is an answer too: asking again would get the same one,
            # and a lock that is not ours must not stop every cell after this one.
            gone = code == 2 and last_error.startswith(_NO_LOCK_OF_OURS)
            if code == 0 or gone:
                with self._lock:
                    if self._held == (sheet, cell):
                        self._held = None
                if code == 0:
                    return "released"
                return "uploaded" if last_error.startswith(_UPLOADED_ALREADY) else "none"
        return ""

    def _keep(self, line: app_jobs.Line, *, detail: bool) -> None:
        """Keep the line for the report, and a progress line for the page."""
        shown = f"{line.kind}: {line.text}" if line.kind else line.text
        with self._lock:
            self._lines.append(shown)
            if len(self._lines) > LINES_KEPT:
                del self._lines[: len(self._lines) - LINES_KEPT]
            if detail and not line.kind:
                self._detail = line.text

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            run = self._run
            thread = self._thread
        if run is not None:
            run.stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15)
        # A job that is drawing is stopped the way the page stops it, lock and all.
        self.stop()


# ---- the server


PAGE_DIR = Path(__file__).resolve().parent / "app_page"
# The page's own files, by the name the browser asks for. Nothing else is read from disk.
PAGE_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "app.css": ("app.css", "text/css; charset=utf-8"),
}
TICK_SECONDS = 5.0
# How long the shortcut's app stays up with no page, no job and no fetch.
IDLE_EXIT_SECONDS = 600.0
QUIT_PAUSE_SECONDS = 0.3

# How long an answer that cost something is allowed to stand.
_MACHINE_SECONDS = 30.0
_TOOLS_SECONDS = 30.0
# Asking starts a child that imports PyTorch, so a yes stands for a while.
_TOOLS_READY_SECONDS = 600.0
_CESIUM_OK_SECONDS = 600.0
_CESIUM_BAD_SECONDS = 30.0
_PRICE_SECONDS = 600.0
_SHEET_SECONDS = 60.0
_CESIUM_TOKEN_MAX = 2000

_CELL_STATES = frozenset({"fri", "last", "vantar", "godkand", "live", "ute"})

_BLANK_MACHINE: dict[str, object] = {
    "ok": False,
    "card": "",
    "card_gb": 0,
    "driver": "",
    "free_gb": 0,
    "needed_gb": 0,
    "ram_gb": None,
    "problem": "",
    "warning": "",
}
_BLANK_TOOLS: dict[str, object] = {"ready": False, "model_cached": False, "missing": []}
# Which phase of the fetch brings a tool. A tool that is still missing after a fetch has
# its phase done again: tools() is the judge of what is there, not the fetch.
_PHASE_OF_TOOL = {
    "torch": "stack",
    "bitsandbytes": "stack",
    "render": "stack",
    "node": "node",
    "google_renderer": "renderer",
}
_BLANK_PRICE: dict[str, object] = {"decision": "unknown", "after": ""}
_BLANK_CESIUM: dict[str, object] = {"set": False, "ok": False, "problem": ""}
# Until GitHub has answered there is no news, and that is also the answer when it cannot.
_BLANK_APP: dict[str, object] = {"mine": "", "latest": "", "current": True, "url": ""}
# A sheet that is not Google's needs no Cesium account. That is not a cached answer.
_NO_CESIUM: dict[str, object] = {"set": False, "ok": True, "problem": ""}


def _no_sheet(problem: str) -> dict[str, object]:
    return {"id": "", "name": "", "kind": "", "cols": 0, "rows": 0, "pipeline": "", "problem": problem}


def _short(value: object) -> str:
    return api.one_line(value, 80) if isinstance(value, str) else ""


def _size(value: object) -> int | None:
    """A sheet's width or height: a whole number from 1 to 10000, never a bool."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
        return None
    return value


def _source(sheet: dict[str, object]) -> str:
    """The source the service's row names for a sheet, or "" when none is decided."""
    try:
        return kind_of(sheet)
    except RutorError:
        return ""


def _sheet_record(raw: object) -> dict[str, object] | None:
    """One sheet as the page keeps it, or None when the row is not a sheet it can read."""
    if not isinstance(raw, dict):
        return None
    sheet_id = raw.get("id")
    if not isinstance(sheet_id, str) or config.SHEET_ID.fullmatch(sheet_id) is None:
        return None
    cols, rows = _size(raw.get("cols")), _size(raw.get("rows"))
    if cols is None or rows is None:
        return None
    return {
        "id": sheet_id,
        "name": _short(raw.get("name")),
        "kind": _source(raw),
        # A sheet of the tiling lists only the cells someone has taken. Any other cell of
        # it is free and has no row. On a hand-made sheet a cell with no row is not offered.
        "made": "grid" if raw.get("made") == "grid" else "hand",
        "cols": cols,
        "rows": rows,
        "pipeline": _short(raw.get("pipeline")),
        "problem": "",
    }


def _open_record(raw: object) -> dict[str, object] | None:
    """One sheet from the queue, or None when it is not an open sheet this page can draw."""
    if not isinstance(raw, dict) or raw.get("state") != "open":
        return None
    done, total = raw.get("done"), raw.get("total")
    if isinstance(done, bool) or isinstance(total, bool) or not isinstance(done, int) or not isinstance(total, int):
        return None
    if done >= total:
        return None
    return _sheet_record(raw)


def _any_free(cells: object) -> bool:
    if not isinstance(cells, list):
        return False
    for cell in cells:
        if isinstance(cell, list) and len(cell) >= 3 and cell[2] == "fri":
            return True
    return False


def _search_open_sheet() -> dict[str, object]:
    """The first open sheet that still has a free cell. Raises when the service cannot be asked."""
    token = config.read_token()
    _status, body = api.get("/v1/sheets", token=token)
    sheets = body.get("sheets") if isinstance(body, dict) else None
    if not isinstance(sheets, list):
        return _no_sheet("none_free")
    for raw in sheets:
        record = _open_record(raw)
        if record is None:
            continue
        _grid_status, grid = api.get(f"/v1/grid/{record['id']}", token=token)
        cells = grid.get("cells") if isinstance(grid, dict) else None
        if _any_free(cells):
            return record
    return _no_sheet("none_free")


def _kind_of(sheet: dict[str, object]) -> str:
    """Which renderer the checks should ask. No sheet yet means the one every free cell uses."""
    if not sheet.get("id"):
        return "google"
    kind = sheet.get("kind")
    return kind if isinstance(kind, str) else ""


class _Known:
    """Answers that cost something, kept until they are old or a job has the card.

    While a job is drawing, nothing here is worked out again: the page asks every second
    and the card is busy. The lock guards only the stored answers. The work happens
    outside it, because a check can take a while and must not stall the page.
    """

    def __init__(
        self,
        builder: Builder,
        clock: Callable[[], float] = time.monotonic,
        installing: Callable[[], bool] | None = None,
    ) -> None:
        self._builder = builder
        self._clock = clock
        # While the tools are fetched nothing is worked out either: asking about the tools
        # starts a child that imports PyTorch, and it would hold files pip is writing.
        self._installing = installing
        self._lock = threading.Lock()
        self._machine: tuple[dict[str, object], float] | None = None
        self._tools: tuple[str, dict[str, object], float] | None = None
        self._cesium: tuple[dict[str, object], float] | None = None
        self._price: tuple[dict[str, object], float] | None = None
        self._sheet: tuple[dict[str, object], float] | None = None
        # The sheet of a cell the person chose by its code. It is the page's sheet until they
        # ask for the next free cell: the search for an open sheet does not replace it.
        self._chosen: dict[str, object] | None = None

    def choose_sheet(self, record: dict[str, object] | None) -> None:
        """The person chose a cell on this sheet (None: back to the first open one)."""
        with self._lock:
            self._chosen = dict(record) if record is not None else None

    def forget_cesium(self) -> None:
        """The person pasted a token. The next state asks Cesium again."""
        with self._lock:
            self._cesium = None

    def forget_tools(self) -> None:
        """A fetch has ended. The next state asks which tools are there now."""
        with self._lock:
            self._tools = None

    def parts(self) -> dict[str, object]:
        sheet = self.sheet()
        kind = _kind_of(sheet)
        return {
            "machine": self.machine(),
            "tools": self.tools(kind),
            "cesium": self.cesium(kind),
            "price": self.price(),
            "sheet": sheet,
        }

    def machine(self) -> dict[str, object]:
        return self._kept("machine", _MACHINE_SECONDS, _BLANK_MACHINE, machine)

    def price(self) -> dict[str, object]:
        return self._kept("price", _PRICE_SECONDS, _BLANK_PRICE, price)

    def tools(self, kind: str) -> dict[str, object]:
        running = self._running()
        with self._lock:
            hit = self._tools
        limit = _TOOLS_READY_SECONDS if hit is not None and hit[1].get("ready") is True else _TOOLS_SECONDS
        if hit is not None and (running or (hit[0] == kind and self._clock() - hit[2] < limit)):
            return hit[1]
        if running:
            return dict(_BLANK_TOOLS)
        found = _dict(tools(kind), _BLANK_TOOLS)
        with self._lock:
            if not self._running():
                self._tools = (kind, found, self._clock())
        return found

    def cesium(self, kind: str) -> dict[str, object]:
        if kind != "google":
            return dict(_NO_CESIUM)
        running = self._running()
        with self._lock:
            hit = self._cesium
        limit = _CESIUM_OK_SECONDS if hit is not None and hit[0].get("ok") is True else _CESIUM_BAD_SECONDS
        if hit is not None and (running or self._clock() - hit[1] < limit):
            return hit[0]
        if running:
            return dict(_BLANK_CESIUM)
        found = _dict(cesium(), _BLANK_CESIUM)
        with self._lock:
            if not self._running():
                self._cesium = (found, self._clock())
        return found

    def sheet(self) -> dict[str, object]:
        """The open sheet. A service that cannot be asked becomes problem network."""
        try:
            return self._sheet_now(swallow=True)
        except RutorError:
            found = _no_sheet("network")
            self._remember_sheet(found)
            return found

    def sheet_for_choice(self) -> dict[str, object]:
        """The open sheet for a click. A dead service is said as the service said it."""
        return self._sheet_now(swallow=False)

    def _sheet_now(self, *, swallow: bool) -> dict[str, object]:
        running = self._running()
        with self._lock:
            hit = self._sheet
            chosen = self._chosen
        if chosen is not None:
            return chosen
        if hit is not None and (running or self._clock() - hit[1] < _SHEET_SECONDS):
            return hit[0]
        if running:
            return hit[0] if hit is not None else _no_sheet("")
        try:
            found = _search_open_sheet()
        except RutorError:
            if not swallow:
                raise
            found = _no_sheet("network")
        self._remember_sheet(found)
        return found

    def _remember_sheet(self, found: dict[str, object]) -> None:
        with self._lock:
            if self._running() and self._sheet is not None:
                return
            self._sheet = (found, self._clock())

    def _kept(
        self,
        slot: str,
        seconds: float,
        blank: dict[str, object],
        load: Callable[..., dict[str, object]],
    ) -> dict[str, object]:
        running = self._running()
        with self._lock:
            hit = self._machine if slot == "machine" else self._price
        if hit is not None and (running or self._clock() - hit[1] < seconds):
            return hit[0]
        if running:
            return dict(blank)
        found = _dict(load(), blank)
        with self._lock:
            if self._running():
                previous = self._machine if slot == "machine" else self._price
                if previous is not None:
                    return previous[0]
            stamped = (found, self._clock())
            if slot == "machine":
                self._machine = stamped
            else:
                self._price = stamped
        return found

    def _running(self) -> bool:
        if self._installing is not None and self._installing():
            return True
        phase = self._builder.job_state().get("phase")
        return isinstance(phase, str) and phase in _RUNNING


def _dict(found: object, blank: dict[str, object]) -> dict[str, object]:
    if not isinstance(found, dict):
        return dict(blank)
    return dict(found)


def _body(raw: bytes) -> dict[str, object]:
    """A JSON object. Anything else is a 400, including a body of nothing but brackets."""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise bad_input("the body is not JSON") from exc
    if not isinstance(parsed, dict):
        raise bad_input("the body is not a JSON object")
    return parsed


def _coord(value: object, size: object) -> int | None:
    """A column or row inside the sheet. A bool is not a coordinate."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if isinstance(size, bool) or not isinstance(size, int):
        return None
    if 0 <= value < size:
        return value
    return None


def _service_grid(sheet_id: str) -> list[object]:
    _status, body = api.get(f"/v1/grid/{sheet_id}", token=config.read_token())
    cells = body.get("cells") if isinstance(body, dict) else None
    if not isinstance(cells, list):
        raise network("service answer has no cells")
    return cells


def _public_cells(cols: int, rows: int, cells: object) -> list[list[object]]:
    """Cells the page may draw. The login and the time in each row are not its business."""
    if not isinstance(cells, list):
        return []
    kept: list[list[object]] = []
    for cell in cells:
        if not isinstance(cell, list) or len(cell) < 3:
            continue
        col, row, state = cell[0], cell[1], cell[2]
        if isinstance(col, bool) or isinstance(row, bool):
            continue
        if not isinstance(col, int) or not isinstance(row, int) or not isinstance(state, str):
            continue
        if state not in _CELL_STATES or not (0 <= col < cols and 0 <= row < rows):
            continue
        kept.append([col, row, state])
    return kept


def _is_free(cells: list[object], col: int, row: int, rowless_is_free: bool = False) -> bool:
    """True when the cell may be taken. A cell with no row is free only on a sheet of the tiling."""
    for cell in cells:
        if not isinstance(cell, list) or len(cell) < 3:
            continue
        found_col, found_row, state = cell[0], cell[1], cell[2]
        if isinstance(found_col, bool) or isinstance(found_row, bool):
            continue
        if found_col == col and found_row == row:
            return state == "fri"
    return rowless_is_free


# A cell's code as the map of cells gives it: the sheet's id, a space, and col_row.
_CODE_MAX = 100
_CODE = re.compile(rf"({config.SHEET_ID.pattern})\s+(\d{{1,4}})_(\d{{1,4}})")


def _sheet_id(sheet: dict[str, object]) -> str:
    found = sheet.get("id")
    return found if isinstance(found, str) else ""


def _replace_names(text: str, names: set[str]) -> str:
    """The person's name, wherever it stands as its own word, becomes user."""
    cleaned = [name for name in names if name]
    if not cleaned:
        return text
    cleaned.sort(key=len, reverse=True)
    pattern = re.compile(
        "|".join(rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])" for name in cleaned),
        re.IGNORECASE,
    )
    return pattern.sub("user", text)


def _secrets() -> list[str]:
    """Tokens long enough to be worth hiding. Short ones are not searched for."""
    found: list[str] = []
    for value in (config.read_token(), config.read_cesium_token(), os.environ.get(config.CESIUM_TOKEN_ENV)):
        if isinstance(value, str) and len(value) >= 8:
            found.append(value)
    found.sort(key=len, reverse=True)
    return found


def _scrub(text: str) -> str:
    """Take the home folder, the person's name and any stored token out of a report."""
    home = Path.home()
    text = text.replace(str(home), "~")
    names = {home.name}
    try:
        names.add(getpass.getuser())
    except (OSError, KeyError, ImportError):
        # Which of these a system without a user name raises depends on the Python.
        pass
    # The same folder written as an address: file:///C:/Users/Ebba%20Ek/...
    for spelled in {urllib.parse.quote(home.as_posix()), urllib.parse.quote(home.as_posix(), safe="/:"), urllib.parse.quote(str(home))}:
        if spelled and spelled != "/":
            text = text.replace(spelled, "~")
    for name in list(names):
        names.add(urllib.parse.quote(name))
    # Windows also writes a long or spaced folder name short: C:\Users\EBBAEK~1\...
    text = re.sub(r"(?i)(\busers[\\/])[^\\/\s\"']*~\d+", r"\1user", text)
    text = _replace_names(text, names)
    for secret in _secrets():
        text = text.replace(secret, "[removed]")
        if len(secret) >= 24:
            # A line is cut to a length before it is kept, so a token may stand there
            # without its end. Its beginning is enough to know it by.
            text = re.sub(re.escape(secret[:16]) + r"[A-Za-z0-9._~+/=-]*…?", "[removed]", text)
    return text


def _report(
    machine_info: dict[str, object],
    builder: Builder,
    install: dict[str, object] | None = None,
    install_lines: list[str] | None = None,
) -> str:
    card = machine_info.get("card")
    card_gb = machine_info.get("card_gb")
    if isinstance(card, str) and card and isinstance(card_gb, int) and not isinstance(card_gb, bool):
        card_line = f"card: {card} {card_gb} GB"
    else:
        card_line = "card: none"
    ram = machine_info.get("ram_gb")
    if isinstance(ram, int) and not isinstance(ram, bool):
        memory_line = f"memory: {ram} GB"
    else:
        memory_line = "memory: unknown"
    free = machine_info.get("free_gb")
    free_gb = free if isinstance(free, int) and not isinstance(free, bool) else 0
    chosen = builder.chosen()
    if chosen is None:
        cell_line = "cell: none"
    else:
        cell_line = f"cell: {chosen['sheet']} {chosen['col']}_{chosen['row']}"
    job = builder.job_state()
    phase = job.get("phase")
    phase_text = phase if isinstance(phase, str) else ""
    problem = job.get("problem")
    problem_text = problem if isinstance(problem, str) and problem else "none"
    held = "yes" if job.get("held") is True else "no"
    lines = [f"  {line}" for line in builder.job_lines()]
    driver = machine_info.get("driver")
    driver_line = f"driver: {driver}" if isinstance(driver, str) and driver else "driver: unknown"
    installed = config.app_install()
    # A clone has no installer version. Its code is named by the rutor line above.
    app_line = f"app: {api.one_line(installed['version'], 20)}" if installed is not None else "app: none"
    fetch = install or {}
    fetch_phase = fetch.get("phase")
    fetch_problem = fetch.get("problem")
    fetch_lines = [f"  {line}" for line in install_lines or []]
    # The host name is left out on purpose: a report is pasted into an issue.
    text = "\n".join(
        [
            "Tech Embassy Pixel report",
            f"rutor: {__version__}",
            app_line,
            f"pipeline: {pipeline.PIPELINE}",
            f"system: {platform.system()} {platform.release()} {platform.machine()}",
            f"python: {platform.python_version()}",
            card_line,
            driver_line,
            memory_line,
            f"disk: {free_gb} GB free",
            cell_line,
            f"phase: {phase_text}",
            f"problem: {problem_text}",
            f"held: {held}",
            "lines:",
            *lines,
            f"install: {fetch_phase if isinstance(fetch_phase, str) and fetch_phase else 'idle'}",
            f"install problem: {fetch_problem if isinstance(fetch_problem, str) and fetch_problem else 'none'}",
            "install lines:",
            *fetch_lines,
        ]
    )
    return _scrub(text)


class AppServer:
    """The build page behind the shell every page on this machine has: see `localpage`."""

    def __init__(
        self,
        builder: Builder | None = None,
        releases: ReleaseFetch | None = None,
        installer: app_install.Installer | None = None,
        idle_exit: float | None = None,
    ) -> None:
        self._builder = builder if builder is not None else Builder()
        # Seconds without a page, a job or a fetch after which the app ends by itself. None
        # for a start from a terminal, which Ctrl-C ends.
        self._idle_exit = idle_exit
        self._idle_seconds = 0.0
        self._ending = threading.Lock()
        self._ended = False
        self._installer = (
            installer
            if installer is not None
            else app_install.Installer(app_jobs.start_process, app_install.fetch_url, commands._free_gb)
        )
        # A cell and a fetch never run together. Whichever is started looks, under this
        # lock, that the other is not running.
        self._start_lock = threading.Lock()
        self._install_thread: threading.Thread | None = None
        self._known = _Known(self._builder, installing=self._installing)
        self._page = LocalPage("app", self._get, self._post)
        self.key = self._page.key
        self.port = self._page.port
        self.url = self._page.url
        self._timer_stop = threading.Event()
        self._timer: threading.Thread | None = None
        self._timer_lock = threading.Lock()
        # GitHub is asked once, on a thread of its own, so the page is up before it answers.
        self._releases = releases
        self._app: dict[str, object] = dict(_BLANK_APP)
        self._app_lock = threading.Lock()
        self._app_thread: threading.Thread | None = None

    def _ensure_version(self) -> None:
        """Start the one thread that asks GitHub. Repeated calls start nothing."""
        with self._app_lock:
            if self._app_thread is not None:
                return
            thread = threading.Thread(target=self._read_version, daemon=True)
            self._app_thread = thread
        thread.start()

    def _read_version(self) -> None:
        try:
            found = app_version(self._releases)
        except Exception as exc:  # noqa: BLE001 - the last stop on this thread
            # Whatever broke, the page has no news. The kind of fault goes to the terminal,
            # never its message: an answer from outside is data.
            print(f"app: {type(exc).__name__} while reading the app version", file=sys.stderr)
            found = dict(_BLANK_APP)
        with self._app_lock:
            self._app = found

    def _ensure_timer(self) -> None:
        with self._timer_lock:
            if self._timer is not None and self._timer.is_alive():
                return
            self._timer_stop.clear()
            self._timer = threading.Thread(target=self._tick_loop, daemon=True)
            self._timer.start()

    def _tick_loop(self) -> None:
        # A fault in one tick must not end the timer: the page is still open.
        while not self._timer_stop.wait(TICK_SECONDS):
            try:
                # Looked at before the tick, which may be what stops the job.
                working = self._installing() or self._builder.busy()
                quiet = self._builder.quiet_for()
                # Only a job is stopped for a page that has gone silent. A fetch holds no card
                # and no lock, and a tab that the browser put to sleep is not a closed one:
                # it goes on until it is done, stopped from the page, or the app is closed.
                self._builder.tick()
                if self._idle_exit is not None and self._idle(working, quiet):
                    self._end_soon()
            except Exception as exc:
                print(f"app: {type(exc).__name__} in tick", file=sys.stderr)

    def _idle(self, working: bool, quiet: float) -> bool:
        """Whether the app has been without a page and without work for long enough to end.

        The time is counted from the later of the page's last request and the last moment
        something ran, so the app never ends under a job, and a page that comes back after
        its job was stopped still finds the app.
        """
        if working:
            self._idle_seconds = quiet
            return False
        limit = self._idle_exit if self._idle_exit is not None else 0.0
        if quiet < self._idle_seconds:
            # The page asked again since the work ended.
            self._idle_seconds = 0.0
        return quiet - self._idle_seconds > limit

    def end_serving(self) -> None:
        """Make serve_forever return, from another thread."""
        self._page.end_serving()

    def _end_soon(self) -> None:
        """End the serving once, a moment from now, so that an answer on its way gets out."""
        with self._ending:
            if self._ended:
                return
            self._ended = True

        def end() -> None:
            time.sleep(QUIT_PAUSE_SECONDS)
            self._page.end_serving()

        threading.Thread(target=end, daemon=True).start()

    def start(self) -> None:
        self._ensure_timer()
        self._ensure_version()
        self._page.start()

    def serve_forever(self) -> None:
        self._ensure_timer()
        self._ensure_version()
        self._page.serve_forever()

    def stop(self) -> None:
        with self._timer_lock:
            self._timer_stop.set()
            timer = self._timer
            self._timer = None
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=130)
        # No new request gets in, and then anything the page started is stopped.
        self._page.stop()
        self._installer.stop()
        with self._start_lock:
            fetching = self._install_thread
        if fetching is not None and fetching is not threading.current_thread():
            fetching.join(timeout=30)
        self._builder.shutdown()

    def _get(self, rest: str) -> Reply | None:
        page = PAGE_FILES.get(rest)
        if page is not None:
            return Reply(200, page[1], (PAGE_DIR / page[0]).read_bytes())
        if rest == "api/state":
            return json_reply(200, self._state())
        if rest == "api/alive":
            # For a second start that wants to know whether this one is up. It costs
            # nothing and does not count as the page being there.
            return json_reply(200, {"app": "pixelbygget"})
        if rest == "api/grid":
            return json_reply(200, self._grid())
        if rest == "api/report":
            text = _report(self._known.machine(), self._builder, self._installer.state(), self._installer.lines())
            return Reply(200, "text/plain; charset=utf-8", text.encode("utf-8"))
        return None

    def _post(self, rest: str, raw: bytes) -> Reply | None:
        if rest == "api/terms":
            return json_reply(200, self._terms(raw))
        if rest == "api/login":
            _body(raw)
            self._builder.start_login()
            return json_reply(200, {"ok": True})
        if rest == "api/cesium":
            return json_reply(200, self._cesium_post(raw))
        if rest == "api/cell":
            return json_reply(200, self._cell(raw))
        if rest == "api/build":
            _body(raw)
            with self._start_lock:
                if self._installing():
                    raise Refusal(409, "the tools are being fetched", "busy")
                if app_install.is_app() and self._installer.state().get("whole") is not True:
                    # The tools may answer while the model is half fetched. The redraw
                    # would then fetch the rest in silence, under a locked cell.
                    raise Refusal(409, "the drawing tools are not all fetched", "not_ready")
                sheet = self._known.sheet()
                refuse_old_pipeline(sheet if isinstance(sheet, dict) else {})
                self._builder.build()
            return json_reply(200, {"ok": True})
        if rest == "api/install":
            _body(raw)
            self._install()
            return json_reply(200, {"ok": True})
        if rest == "api/quit":
            _body(raw)
            # Whatever runs is stopped first, lock and all. Then the answer, then the end.
            self._installer.stop()
            self._builder.stop()
            self._end_soon()
            return json_reply(200, {"ok": True})
        if rest == "api/install/stop":
            _body(raw)
            # The child is told to end, and the page reads how it went in the state.
            self._installer.stop()
            return json_reply(200, {"ok": True})
        if rest == "api/stop":
            _body(raw)
            self._builder.stop()
            return json_reply(200, {"ok": True})
        return None

    def _state(self) -> dict[str, object]:
        # Asked for, so the page is still there. A closed tab stops a job on its own.
        self._builder.seen()
        parts = self._known.parts()
        sheet = parts["sheet"]
        if not isinstance(sheet, dict):
            sheet = _no_sheet("network")
        with self._app_lock:
            app = dict(self._app)
        return {
            "machine": parts["machine"],
            "tools": parts["tools"],
            "terms": {"lines": terms(), "accepted": self._builder.terms_accepted()},
            "login": self._builder.login_state(),
            "cesium": parts["cesium"],
            "price": parts["price"],
            "sheet": sheet,
            "cell": self._builder.chosen(),
            "job": self._builder.job_state(),
            "install": {**self._installer.state(), "app": app_install.is_app()},
            "version": {**version(sheet), "app": app},
        }

    def _installing(self) -> bool:
        thread = self._install_thread
        return thread is not None and thread.is_alive()

    def _install(self) -> None:
        """Start fetching the tools, on a thread of its own."""
        if not app_install.is_app():
            # An install made by hand is the person's own. The page installs nothing into it.
            raise Refusal(409, "tools are fetched only into an app install", "not_an_app")
        with self._start_lock:
            if self._installing() or self._builder.busy():
                raise Refusal(409, "this machine is busy with a cell or a fetch", "busy")
            # Asked now and not from memory, like a build: nothing large is fetched to a
            # machine that cannot draw, or before the terms are accepted.
            if not machine()["ok"] or not self._builder.terms_accepted():
                raise Refusal(409, "the machine check and the terms come first", "not_ready")
            missing = tools(_kind_of(self._known.sheet())).get("missing")
            redo: list[str] = []
            for name in missing if isinstance(missing, list) else []:
                phase = _PHASE_OF_TOOL.get(name) if isinstance(name, str) else None
                if phase is not None and phase not in redo:
                    redo.append(phase)
            # Starting counts as the page being here.
            self._builder.seen()
            thread = threading.Thread(target=self._run_install, args=(tuple(redo),), daemon=True)
            self._install_thread = thread
            thread.start()

    def _run_install(self, redo: tuple[str, ...]) -> None:
        try:
            self._installer.run(redo=redo)
        except Refusal:
            # Nothing was started. The state says so.
            pass
        except Exception as exc:  # noqa: BLE001 - the last stop on this thread
            print(f"app: {type(exc).__name__} while fetching the tools", file=sys.stderr)
        finally:
            self._known.forget_tools()

    def _grid(self) -> dict[str, object]:
        sheet = self._known.sheet()
        sheet_id = _sheet_id(sheet)
        cols, rows = sheet.get("cols"), sheet.get("rows")
        if (
            not sheet_id
            or isinstance(cols, bool)
            or isinstance(rows, bool)
            or not isinstance(cols, int)
            or not isinstance(rows, int)
        ):
            return {"sheet": None, "cells": []}
        # The cells are asked again every time. Who holds one can change while the page is open.
        return {
            "sheet": {
                "id": sheet_id,
                "name": sheet.get("name") if isinstance(sheet.get("name"), str) else "",
                "made": "grid" if sheet.get("made") == "grid" else "hand",
                "cols": cols,
                "rows": rows,
                "pipeline": sheet.get("pipeline") if isinstance(sheet.get("pipeline"), str) else "",
            },
            "cells": _public_cells(cols, rows, _service_grid(sheet_id)),
        }

    def _terms(self, raw: bytes) -> dict[str, object]:
        if _body(raw).get("accept") is not True:
            raise bad_input("accept must be true")
        self._builder.accept_terms()
        return {"ok": True}

    def _cesium_post(self, raw: bytes) -> dict[str, object]:
        token = _body(raw).get("token")
        if not isinstance(token, str) or not 1 <= len(token) <= _CESIUM_TOKEN_MAX:
            raise bad_input("the Cesium token must be 1 to 2000 characters")
        answer = cesium(token)
        # A token Cesium refused is not stored. The old one, if any, stays.
        if answer.get("ok") is True:
            config.write_cesium_token(token)
        self._known.forget_cesium()
        problem = answer.get("problem")
        return {"ok": answer.get("ok") is True, "problem": problem if isinstance(problem, str) else ""}

    def _cell(self, raw: bytes) -> dict[str, object]:
        body = _body(raw)
        if "code" in body:
            return self._cell_coded(body.get("code"))
        if body.get("next") is True:
            return self._cell_next()
        return self._cell_named(body)

    def _cell_coded(self, code: object) -> dict[str, object]:
        """A cell by its code from the map: any sheet, also one this page was not looking at.

        The code is text a person pasted. It is taken apart by shape before anything is
        asked, it is never said back, and the sheet's size comes from the service.
        """
        bad = Refusal(400, "the code names no cell", "bad_code")
        if not isinstance(code, str) or len(code) > _CODE_MAX:
            raise bad
        found = _CODE.fullmatch(code.strip())
        if found is None:
            raise bad
        sheet_id, col, row = found.group(1), int(found.group(2)), int(found.group(3))
        try:
            _status, body = api.get(f"/v1/grid/{sheet_id}", token=config.read_token())
        except RutorError as exc:
            if exc.code == 2:
                # The service has no sheet by that name.
                raise bad from exc
            raise
        raw = body.get("sheet") if isinstance(body, dict) else None
        cells = body.get("cells") if isinstance(body, dict) else None
        record = _sheet_record(raw)
        if record is None or record["id"] != sheet_id or not isinstance(cells, list):
            raise bad
        if _coord(col, record["cols"]) is None or _coord(row, record["rows"]) is None:
            raise bad
        is_open = isinstance(raw, dict) and raw.get("state") == "open"
        if not is_open or not _is_free(cells, col, row, record["made"] == "grid"):
            raise Refusal(409, "that cell is not free", "taken")
        # Refused here while a cell is being built, before the page's sheet is changed.
        self._builder.choose(sheet_id, col, row, _kind_of(record))
        self._known.choose_sheet(record)
        return {"sheet": sheet_id, "col": col, "row": row}

    def _open(self) -> dict[str, object]:
        sheet = self._known.sheet_for_choice()
        if not _sheet_id(sheet):
            raise Refusal(409, "no cell is free", "none_free")
        return sheet

    def _cell_next(self) -> dict[str, object]:
        if self._builder.busy():
            raise Refusal(409, "a cell is being built", "busy")
        # "The next free cell" is asked of the first open sheet, not of one chosen by a code.
        self._known.choose_sheet(None)
        sheet = self._open()
        sheet_id = _sheet_id(sheet)
        cells = _service_grid(sheet_id)
        try:
            col, row = commands.pick_next(sheet, cells)
        except RutorError as exc:
            if exc.code != 2:
                raise
            # The last free cell went since the page last looked. The page has a word for that.
            raise Refusal(409, "no cell is free", "none_free") from exc
        self._builder.choose(sheet_id, col, row, _kind_of(sheet))
        return {"sheet": sheet_id, "col": col, "row": row}

    def _cell_named(self, body: dict[str, object]) -> dict[str, object]:
        sheet = self._open()
        sheet_id = _sheet_id(sheet)
        col = _coord(body.get("col"), sheet.get("cols"))
        row = _coord(body.get("row"), sheet.get("rows"))
        if body.get("sheet") != sheet_id or col is None or row is None:
            raise bad_input("the body names no cell")
        if not _is_free(_service_grid(sheet_id), col, row, sheet.get("made") == "grid"):
            raise Refusal(409, "that cell is not free", "taken")
        self._builder.choose(sheet_id, col, row, _kind_of(sheet))
        return {"sheet": sheet_id, "col": col, "row": row}


# ---- one app at a time, with or without a terminal


# The address of a page this program serves: this machine, a port and the page's long key.
_PAGE_URL = re.compile(r"http://127\.0\.0\.1:\d{1,5}/[A-Za-z0-9_-]{20,100}/")
ALIVE_SECONDS = 3.0
ALIVE_TRIES = 4
RACE_SECONDS = 5.0
# How long a running app gets to stop its job, give the lock back and end.
QUIT_SECONDS = 150.0
LOG_MAX_BYTES = 256_000


def running_path() -> Path:
    return config.home() / "app-running.json"


def log_path() -> Path:
    return config.home() / "app.log"


def _alive(pid: int) -> bool:
    """Whether a process with this number exists. It may be another program than the app."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill is no question on Windows: with any signal number it ends the process.
        import ctypes

        # A handle of this function's own, so that the types set here are not everybody's.
        kernel = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        # PROCESS_QUERY_LIMITED_INFORMATION. 259 is STILL_ACTIVE.
        handle = kernel.OpenProcess(0x1000, 0, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists and is somebody else's.
        return True
    return True


def _answers(url: str) -> str:
    """Whether the app page at this address says that it is one: "yes", "no" or "slow".

    "slow" is a port that took the connection and did not answer in time, which is an app
    that is busy, not one that is gone. No proxy sees the address.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(f"{url}api/alive", timeout=ALIVE_SECONDS) as response:
            found = json.loads(response.read(1000).decode("utf-8"))
    except TimeoutError:
        return "slow"
    except urllib.error.URLError as exc:
        return "slow" if isinstance(exc.reason, TimeoutError) else "no"
    except Exception:
        # Refused, or not JSON: there is no app page there.
        return "no"
    return "yes" if isinstance(found, dict) and found.get("app") == "pixelbygget" else "no"


def running_page() -> str | None:
    """The address of an app page that is already up on this machine, or None.

    The file is only a note. It counts when its process is alive and the page answers at
    its address. A note that is stale, or that is not such a note, is removed.
    """
    path = running_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        text = ""
    try:
        found = json.loads(text)
    except ValueError:
        found = None
    if isinstance(found, dict):
        pid, url = found.get("pid"), found.get("url")
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and isinstance(url, str)
            # Only an address of the page's own shape is ever asked: the file is data.
            and _PAGE_URL.fullmatch(url)
            and _alive(pid)
        ):
            for _attempt in range(ALIVE_TRIES):
                answer = _answers(url)
                if answer == "yes":
                    return url
                if answer == "no":
                    break
                time.sleep(0.5)
            else:
                # Alive, listening and slow every time: a machine that is busy drawing. It is
                # still the app, and a second one beside it would be worse than a slow page.
                return url
    with contextlib.suppress(OSError):
        path.unlink()
    return None


def _write_running(url: str) -> bool:
    """Note that this process serves the page. False when another start got there first."""
    path = running_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"pid": os.getpid(), "url": url}).encode("utf-8")
    for _attempt in range(2):
        try:
            # The address holds the page's key, so the note is the owner's alone.
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if running_page() is not None:
                return False
            # The note was stale and is gone now.
            continue
        try:
            os.write(descriptor, body)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
        return True
    return False


def _remove_running() -> None:
    """Take the note away, if it is this process's own."""
    path = running_path()
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(found, dict) and found.get("pid") == os.getpid():
        with contextlib.suppress(OSError):
            path.unlink()


class _Log:
    """The fault log of an app without a terminal: each line with its time, nothing else."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                flags |= os.O_TRUNC
        except OSError:
            pass
        self._file = os.fdopen(os.open(path, flags, 0o600), "a", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        self._lock = threading.Lock()
        self._rest = ""

    def write(self, text: str) -> int:
        with self._lock:
            *lines, self._rest = (self._rest + text).split("\n")
            for line in lines:
                if line.strip():
                    self._file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {api.one_line(line, 300)}\n")
            self._file.flush()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()


def _fault_line(kind: type[BaseException] | None, trace: object) -> str:
    """The kind of a fault and where, never its message: this process holds a login."""
    frames = traceback.extract_tb(trace) if trace is not None else []  # type: ignore[arg-type]
    where = f" at {Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else ""
    return f"app: {kind.__name__ if kind is not None else 'fault'}{where}"


@contextlib.contextmanager
def _quietly() -> Iterator[None]:
    """Print nothing. Fault lines go to the log, and a fault nobody caught goes there by its kind."""
    log = _Log(log_path())
    nowhere = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - closed below
    saved = (sys.stdout, sys.stderr, sys.excepthook, threading.excepthook)

    def on_thread(args: threading.ExceptHookArgs) -> None:
        log.write(_fault_line(args.exc_type, args.exc_traceback) + "\n")

    sys.stdout, sys.stderr = nowhere, log  # type: ignore[assignment]
    sys.excepthook = lambda kind, _value, trace: log.write(_fault_line(kind, trace) + "\n")
    threading.excepthook = on_thread
    try:
        yield
    except RutorError as exc:
        # The command's own sentence, which a terminal would have shown.
        log.write(f"error: {exc.message}\n")
        raise
    except Exception as exc:
        log.write(_fault_line(type(exc), exc.__traceback__) + "\n")
        raise
    finally:
        sys.stdout, sys.stderr, sys.excepthook, threading.excepthook = saved
        nowhere.close()
        log.close()


def _show(url: str, args: argparse.Namespace) -> None:
    print(f"url: {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)


def _serve(args: argparse.Namespace, quiet: bool) -> int:
    found = running_page()
    if found is not None:
        # The app is up already. Its page is shown, and no second one is started.
        _show(found, args)
        return 0
    try:
        server = AppServer(idle_exit=IDLE_EXIT_SECONDS if quiet else None)
    except OSError as exc:
        raise environment(f"could not open a port on this machine ({exc.strerror})") from exc
    if not _write_running(server.url):
        # Another start wrote its note between the look above and here. That one wins.
        server.stop()
        deadline = time.monotonic() + RACE_SECONDS
        while True:
            found = running_page()
            if found is not None:
                _show(found, args)
                return 0
            if time.monotonic() >= deadline:
                raise environment("another Tech Embassy Pixel was starting and did not come up; start it again")
            time.sleep(0.2)
    try:
        _show(server.url, args)
        print("Ctrl-C closes the page.", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        _remove_running()
    print("closed")
    return 0


def _quit_running() -> int:
    """End the app that is running, the way its own page ends it. For the installer.

    The installer replaces files the app holds open, so it asks first. Nothing running is
    no fault.
    """
    path = running_path()
    url = running_page()
    if url is None:
        return 0
    try:
        noted = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        noted = {}
    pid = noted.get("pid") if isinstance(noted, dict) else None
    request = urllib.request.Request(
        f"{url}api/quit", data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        # The answer comes when the job is stopped and its lock is given back.
        with opener.open(request, timeout=QUIT_SECONDS) as response:
            response.read(1000)
    except Exception as exc:
        raise environment("the running Tech Embassy Pixel did not answer; close it and try again") from exc
    deadline = time.monotonic() + QUIT_SECONDS
    while time.monotonic() < deadline:
        # Gone when the page is, and, for another process than this one, when it has ended:
        # only then are its files free.
        other = isinstance(pid, int) and not isinstance(pid, bool) and pid != os.getpid()
        if _answers(url) == "no" and not (other and _alive(pid)):
            return 0
        time.sleep(0.2)
    raise environment("the running Tech Embassy Pixel did not end; close it and try again")


def cmd_app(args: argparse.Namespace) -> int:
    quiet = bool(getattr(args, "quiet", False))
    work = _quit_running if getattr(args, "quit", False) else (lambda: _serve(args, quiet))
    if not quiet:
        return work()
    with _quietly():
        return work()


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    app_parser = sub.add_parser("app", aliases=["bygg"], help="open the page for building a cell")
    app_parser.add_argument("--no-open", action="store_true", help="print the address and do not open a browser")
    app_parser.add_argument(
        "--quiet",
        action="store_true",
        help="print nothing, write faults to app.log, and end after ten minutes without a page",
    )
    app_parser.add_argument("--quit", action="store_true", help="end the app that is running, if one is")
    app_parser.set_defaults(func=cmd_app)
