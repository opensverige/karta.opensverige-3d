"""GitHub device flow. The token is written by config.write_token."""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone

from rutor import __version__
from rutor.api import get
from rutor.config import github_client_id, read_terms, write_terms, write_token
from rutor.errors import RutorError, bad_input, environment, network, refused

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEVICE_URL = "https://github.com/login/device"

# The `Version` line of TERMS.md. The service holds the same date in service/src/terms.js and
# refuses a claim or an upload whose rutor says an older one; a test holds the three together.
# A change to the ten lines needs a new date, and then everyone is asked again.
TERMS_VERSION = "2026-11-25"

# The same ten lines as TERMS.md at the repository root; a test holds the two together.
TERMS = "\n".join(
    [
        '1. Building a cell is voluntary and unpaid. Nobody owes you anything for it, and you can stop at any time.',
        '2. You run it on your own machine, at your own cost and risk. The software comes as is, without warranty, under the MIT license.',
        '3. Only the redrawn PNG and its manifest leave your machine. The source render stays with you.',
        '4. You let the project show your cell on karta.opensverige.se, change it, blend it with its neighbours and remove it. As far as the rights are yours to give, you give them under the MIT license.',
        '5. A person reviews your cells before they go on the map. Only a builder the maintainer has marked trusted goes straight through. Any cell can be rejected, or taken down later.',
        '6. Your GitHub login is shown publicly on your cells, with the time each cell last changed. Open an issue to have it removed.',
        '7. The login asks GitHub for no permissions (an empty scope). The service uses your token only to ask GitHub who you are, and does not store it.',
        "8. The redraw model is under the Qwen Research License: non-commercial research and evaluation only. You download and run it under that license yourself. The LoRA is Apache 2.0, and its model card says it was trained on renders of Google's 3D tiles.",
        "9. A sheet with open data as its source is drawn from a public orthophoto and terrain model and from OpenStreetMap (© OpenStreetMap contributors, ODbL). Today that is goteborg-norr alone: Göteborgs Stad's orthophoto and terrain model (CC0).",
        "10. Every other sheet, stockholm-norr among them, is drawn from Google's 3D tiles through your own Cesium ion account, whose terms bind you, not this project. Google's Map Tiles policy does not allow deriving new content from its tiles: read it and decide for yourself. Google can close your access and ask for those cells to be taken down.",
    ]
)

Http = Callable[[str, dict[str, str]], dict[str, object]]


def github_http(url: str, form: dict[str, str]) -> dict[str, object]:
    payload = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": f"rutor/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise network(f"could not reach GitHub ({exc.reason})") from exc
    # A read timeout or a broken connection is not a URLError.
    except (OSError, http.client.HTTPException) as exc:
        raise network(f"could not reach GitHub ({exc.__class__.__name__})") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise network("GitHub answered, not JSON") from exc
    if not isinstance(parsed, dict):
        raise network("GitHub answered, not JSON")
    body: dict[str, object] = {}
    for key, value in parsed.items():
        if isinstance(key, str):
            body[key] = value
    return body


# How many questions in a row may go unanswered while a person approves their code.
NETWORK_MISSES = 5


def start_device_flow(client_id: str, http: Http | None = None) -> dict[str, object]:
    call = http or github_http
    return call(DEVICE_CODE_URL, {"client_id": client_id, "scope": ""})


def poll_for_token(
    client_id: str,
    device_code: str,
    interval: int,
    sleep: Callable[[float], None],
    http: Http | None = None,
) -> str:
    call = http or github_http
    current = interval
    misses = 0
    while True:
        try:
            body = call(
                ACCESS_TOKEN_URL,
                {
                    "client_id": client_id,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        except RutorError as exc:
            # The person may be typing the code at GitHub right now. One question that got
            # no answer must not throw their code away: it is asked again a few times.
            misses += 1
            if exc.code != 4 or misses > NETWORK_MISSES:
                raise
            sleep(float(current))
            continue
        misses = 0
        token = body.get("access_token")
        if isinstance(token, str) and token:
            return token
        error = body.get("error")
        if error == "slow_down":
            current += 5
            sleep(float(current))
            continue
        if error == "authorization_pending":
            sleep(float(current))
            continue
        if error == "expired_token":
            raise environment("The device code expired. Run rutor login again.")
        if error == "access_denied":
            raise refused("refused: access_denied: the GitHub login was denied")
        raise environment(f"GitHub login failed ({error}). Run rutor login again.")


def terms_current() -> bool:
    """Whether this person has said yes, on this machine, to today's terms or newer ones."""
    accepted = read_terms()
    return accepted is not None and accepted >= TERMS_VERSION


def ask_terms(accept: bool, flag: str) -> None:
    """Show the ten lines and take the yes, which is kept as the version and the moment.

    Always shown, so an agent that passes `flag` has still put them in front of its human.
    """
    print(TERMS, flush=True)
    if not accept:
        if not sys.stdin.isatty():
            raise bad_input(f"Pass {flag} to accept the terms.")
        answer = input("accept? (yes/no): ")
        if answer.strip().lower() not in {"y", "yes", "ja"}:
            raise bad_input("The terms were not accepted.")
    keep_terms()


def keep_terms() -> None:
    """Keep the yes to today's terms on this machine, with the moment it was given."""
    write_terms(TERMS_VERSION, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def cmd_terms(args: argparse.Namespace) -> int:
    # A new yes without a new login: a login spends one of the ten GitHub keeps per person and
    # app, and the eleventh ends an older one, perhaps one that works on another machine.
    ask_terms(args.accept, "--accept")
    print(f"terms: accepted {TERMS_VERSION}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    client_id = github_client_id()
    if not client_id.strip():
        raise environment("PIXELBYGGET_GITHUB_CLIENT_ID is set but empty. Unset it or give a client id.")
    ask_terms(args.accept_terms, "--accept-terms")
    started = start_device_flow(client_id)
    user_code = started.get("user_code")
    device_code = started.get("device_code")
    if not isinstance(user_code, str) or not isinstance(device_code, str):
        raise network("GitHub did not return a device code")
    print(f"code: {user_code}")
    print(f"url: {DEVICE_URL}")
    # The wait below can last minutes. Whoever reads this through a pipe must have the code first.
    print("waiting: a person opens the url and types the code. This command ends when they have, or when the code expires.", flush=True)
    interval_raw = started.get("interval", 5)
    interval = interval_raw if isinstance(interval_raw, int) else 5
    token = poll_for_token(client_id, device_code, interval, time.sleep)
    write_token(token)
    _status, me = get("/v1/me", token=token)
    login = me.get("login") if isinstance(me, dict) else None
    print(f"logged in: {login if isinstance(login, str) else ''}")
    return 0


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("login", help="Log in with GitHub and save the token")
    parser.add_argument("--accept-terms", action="store_true", help="accept the contribution terms")
    parser.set_defaults(func=cmd_login)

    terms = sub.add_parser(
        "terms",
        help="read the terms and say yes to them",
        description="Print the ten lines of TERMS.md and keep your yes. A claim and an upload need a yes to today's terms; this gives it without a new login.",
    )
    terms.add_argument("--accept", action="store_true", help="accept the terms without the question: only after your human said yes")
    terms.set_defaults(func=cmd_terms)
