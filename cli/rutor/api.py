"""HTTP client for the rut-tjänst. Stdlib only."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from rutor import __version__
from rutor.config import read_terms, service_url
from rutor.errors import network, refused

TIMEOUT_SECONDS = 30
MESSAGE_CHARS = 200


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the login to wherever it points, so none is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del req, fp, msg, headers, newurl
        raise network(f"the service answered with a redirect ({code}); nothing was sent on")


def _open(request: urllib.request.Request, timeout: float):  # type: ignore[no-untyped-def]
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def one_line(text: str, limit: int = MESSAGE_CHARS) -> str:
    """Text from outside as one short printable line: it is shown to people and to their agents."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _exchange(
    method: str,
    path: str,
    *,
    token: str | None,
    body: bytes | None,
    json_body: object | None,
    headers: dict[str, str] | None,
    limit: int | None = None,
    timeout: float | None = None,
) -> tuple[int, bytes]:
    hdrs = {
        "User-Agent": f"rutor/{__version__}",
        "Accept": "application/json",
    }
    data = body
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
        # The version of the terms this person said yes to. The service refuses a claim or an
        # upload without today's, and keeps it with them as the receipt.
        terms = read_terms()
        if terms:
            hdrs["X-Terms-Version"] = terms
    if headers:
        hdrs.update(headers)
    # HTTPError is a URLError. It has to be caught first so a 409 stays a refusal
    # and only a dead connection becomes a network error.
    request = urllib.request.Request(service_url() + path, data=data, headers=hdrs, method=method)
    try:
        with _open(request, TIMEOUT_SECONDS if timeout is None else timeout) as response:
            if limit is None:
                return int(response.status), response.read()
            data = response.read(limit + 1)
            if len(data) > limit:
                raise network("the service sent more than expected; nothing was kept")
            return int(response.status), data
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
            return int(exc.code), payload
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise network(f"could not reach the service ({exc.reason})") from exc
    # A read timeout is a TimeoutError, not a URLError, so it needs its own
    # clause; a dropped connection and a broken HTTP stream are the same story.
    # So is a TLS fault or any other socket error: all of them are OSError.
    except (OSError, http.client.HTTPException) as exc:
        raise network(f"could not reach the service ({exc.__class__.__name__}: {exc})") from exc


def _parse(status: int, raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise network(f"service answered {status}, not JSON") from exc


def _refuse(status: int, parsed: object) -> None:
    code = f"http_{status}"
    message = ""
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, str):
            code = one_line(error, 40)
        found = parsed.get("message")
        if isinstance(found, str):
            message = one_line(found)
    # "Try again later" is not a no. The service is busy, down, or asked too often, and an
    # agent that is told "refused" stops where it should wait.
    # A service the maintainer has closed says so in words of its own, and is a no.
    if (status >= 500 and code != "closed") or (status == 429 and code == "rate_limited"):
        raise network(f"the service cannot answer right now ({code}); try again in a minute")
    raise refused(f"refused: {code}: {message}")


def request(
    method: str,
    path: str,
    *,
    token: str | None,
    body: bytes | None = None,
    json_body: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[int, object]:
    status, raw = _exchange(method, path, token=token, body=body, json_body=json_body, headers=headers, timeout=timeout)
    parsed = _parse(status, raw)
    if status >= 400:
        _refuse(status, parsed)
    return status, parsed


def get(path: str, token: str | None = None, timeout: float | None = None) -> tuple[int, object]:
    return request("GET", path, token=token, timeout=timeout)


def post(path: str, json_body: object, token: str | None) -> tuple[int, object]:
    return request("POST", path, token=token, json_body=json_body)


def put_bytes(path: str, data: bytes, headers: dict[str, str], token: str | None) -> tuple[int, object]:
    return request("PUT", path, token=token, body=data, headers=headers)


def download(path: str, token: str | None, limit: int | None = None) -> bytes:
    """The bytes at a path of the service. With `limit`, a longer answer is an error and is not kept."""
    status, raw = _exchange(method="GET", path=path, token=token, body=None, json_body=None, headers={"Accept": "*/*"}, limit=limit)
    if status >= 400:
        _refuse(status, _parse(status, raw))
    return raw


def fetch_public(url: str, headers: dict[str, str], limit: int) -> tuple[int, bytes]:
    """GET an address that is not the service. The login is never sent, and nothing over `limit` is read."""
    hdrs = {"User-Agent": f"rutor/{__version__}", **headers}
    request = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with _open(request, TIMEOUT_SECONDS) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise network("the answer was larger than expected; nothing was kept")
            return int(response.status), data
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), b""
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise network(f"could not reach {urllib.parse.urlsplit(url).hostname} ({exc.reason})") from exc
    # OSError takes in a timeout, a dropped connection and a TLS fault in the middle of a read.
    except (OSError, http.client.HTTPException) as exc:
        raise network(f"could not reach {urllib.parse.urlsplit(url).hostname} ({exc.__class__.__name__}: {exc})") from exc
