"""Where rutor keeps the token and the service url."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from pathlib import Path

from rutor.errors import bad_input, environment

DEFAULT_SERVICE_URL = "https://pixelbygget.gustaf-garnow.workers.dev"
# The pixel maps' tiles. Only the review page reads them, to show a cell among its neighbours.
DEFAULT_TILES_URL = "https://pixelkarta.gustaf-garnow.workers.dev/v1"
# The "Pixelbygget" OAuth App under the opensverige organisation. A client id is public; the
# device flow needs no secret.
GITHUB_CLIENT_ID = "Ov23liulid4VoLnmJCVH"


def home() -> Path:
    override = os.environ.get("PIXELBYGGET_HOME")
    if override:
        return Path(override)
    return Path.home() / ".config" / "pixelbygget"


def token_path() -> Path:
    return home() / "token"


def read_token() -> str | None:
    path = token_path()
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT honours umask, so a second chmod pins the mode at 0600.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)
    os.chmod(path, 0o600)


def write_token(token: str) -> None:
    _write_secret(token_path(), token)


# Which version of TERMS.md this person said yes to on this machine, and when. It sits beside
# the login: a login is made once, and the terms can change after it. Not a secret.
TERMS_VERSION_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")


def terms_path() -> Path:
    return home() / "terms.json"


def read_terms() -> str | None:
    """The terms version accepted here, or None when there is none or the file is not ours."""
    try:
        found = json.loads(terms_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = found.get("version") if isinstance(found, dict) else None
    return version if isinstance(version, str) and TERMS_VERSION_SHAPE.fullmatch(version) else None


def write_terms(version: str, accepted_at: str) -> None:
    if not TERMS_VERSION_SHAPE.fullmatch(version):
        raise bad_input(f"a terms version looks like 2026-11-25, not {version!r}")
    path = terms_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": version, "accepted_at": accepted_at}) + "\n", encoding="utf-8")


# The contributor's own Cesium ion token. The renderer reads it from this variable. A person
# who uses the app page pastes it there once instead, and it is kept beside the login.
CESIUM_TOKEN_ENV = "CESIUM_ION_TOKEN"


def cesium_token_path() -> Path:
    return home() / "cesium-token"


def read_cesium_token() -> str | None:
    path = cesium_token_path()
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def write_cesium_token(token: str) -> None:
    token = token.strip()
    if not token:
        raise bad_input("the Cesium token is empty; nothing was saved")
    _write_secret(cesium_token_path(), token)


def cesium_env() -> dict[str, str]:
    """What to add to a child's environment so that it can read the Cesium token.

    The token the person pasted into the app page, when there is one: it is what the page
    checked and showed as working, so it wins over a variable, and a variable that has gone
    stale can be put right from the page. With nothing pasted, nothing is added, and the
    child inherits the person's own variable if they set one. This process's environment is
    never changed, so a pasted token reaches only the commands that are handed it.
    """
    stored = read_cesium_token()
    return {CESIUM_TOKEN_ENV: stored} if stored else {}


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MAP_URL = "https://karta.opensverige.se/pixelbygget"
SHEET_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _own_or_this_machine(name: str, default: str, refusal: str) -> str:
    """The project's own address, or one on this machine when the variable `name` says so."""
    raw = os.environ.get(name, "").strip()
    if not raw or raw.rstrip("/") == default:
        return default
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname or ""
        port_ok = parts.port is None or 0 < parts.port < 65536
    except ValueError:
        host, parts, port_ok = "", None, False
    if (
        parts is not None
        and port_ok
        and parts.scheme in ("http", "https")
        and host in LOOPBACK_HOSTS
        and not parts.username
        and not parts.password
    ):
        return raw.rstrip("/")
    raise environment(refusal)


def service_url() -> str:
    """The service the token is sent to: the project's own, or one on this machine for testing.

    PIXELBYGGET_URL cannot point anywhere else. The token proves who you are to the service, so
    an instruction to "set PIXELBYGGET_URL to the new address" would hand your identity to
    whoever wrote it. A fork that runs its own service changes DEFAULT_SERVICE_URL in code.
    """
    return _own_or_this_machine(
        "PIXELBYGGET_URL",
        DEFAULT_SERVICE_URL,
        "PIXELBYGGET_URL may only point at this machine (localhost) for testing. "
        "Unset it to use the project's service. Your login is never sent to another address.",
    )


def tiles_url() -> str:
    """Where the review page reads today's map from: the project's tile service, or this machine.

    No login is sent there. The rule is the same as for the service all the same, so that no
    instruction can make the maintainer's machine fetch pictures from an address of its choosing.
    """
    return _own_or_this_machine(
        "PIXELBYGGET_TILES_URL",
        DEFAULT_TILES_URL,
        "PIXELBYGGET_TILES_URL may only point at this machine (localhost) for testing. "
        "Unset it to use the project's tile service.",
    )


def sheet_id(text: str) -> str:
    """A sheet id as the service names it. It becomes a folder name and part of a URL."""
    if not isinstance(text, str) or not SHEET_ID.fullmatch(text):
        raise bad_input("sheet must look like goteborg-norr: lower case letters, digits and hyphens")
    return text


def github_client_id() -> str:
    return os.environ.get("PIXELBYGGET_GITHUB_CLIENT_ID", GITHUB_CLIENT_ID)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def work_dir(sheet: str) -> Path:
    path = repo_root() / "work" / sheet_id(sheet)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- the app install: the folder the Windows installer makes

# The installer writes this file beside `cli`. A clone has none, and that is how the two
# are told apart: the page installs tools into an app install and into nothing else.
INSTALL_FILE = "install.json"
_INSTALL_KEYS = ("version", "lock_sha256")


def app_install() -> dict[str, str] | None:
    """What the installer wrote about itself, or None on an install made by hand."""
    try:
        # utf-8-sig: a Windows tool may have put a byte order mark first.
        found = json.loads((repo_root() / INSTALL_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict):
        return None
    values = {key: found.get(key) for key in _INSTALL_KEYS}
    if not all(isinstance(value, str) and value for value in values.values()):
        return None
    return {key: str(value) for key, value in values.items()}


def app_dir() -> Path | None:
    """The folder the installer made, or None on an install made by hand."""
    return repo_root() if app_install() is not None else None


def tool_env() -> dict[str, str]:
    """What to add to an environment so that the app's own tools are found first.

    The page fetches Node into the app's folder; nothing is put on the person's own PATH.
    A folder without node.exe is a download that did not finish, and counts as no Node.
    The renderer's Chrome is kept in the app's folder as well, where puppeteer both puts
    it and looks for it, so that removing the app removes all of it.
    On an install made by hand nothing is added: there the person's own Node is used.
    """
    folder = app_dir()
    if folder is None:
        return {}
    node = folder / "node"
    if not (node / "node.exe").is_file():
        return {}
    parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    # A child of the page asks again with the folder already in front.
    if not parts or parts[0] != str(node):
        parts.insert(0, str(node))
    return {"PATH": os.pathsep.join(parts), "PUPPETEER_CACHE_DIR": str(folder / "chrome")}
