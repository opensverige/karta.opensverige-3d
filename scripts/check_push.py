"""Reject secret files, and hold contributor changes to claim locks and finished PNGs.

The workflow runs this file from the base branch and reads a pull request only
as git data, so a pull request cannot change the check that judges it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Collection
from datetime import datetime
from pathlib import Path

from repo import claim_text, load_districts, repo_root

CLAIM_RE = re.compile(r"claims/([a-z0-9]+(?:-[a-z0-9]+)*)\.md")
RESULT_RE = re.compile(r"results/([a-z0-9]+(?:-[a-z0-9]+)*)\.png")
CLAIMED_AT_RE = re.compile(r"^claimed_at: (\S+)$", re.MULTILINE)
FORBIDDEN_NAMES = {"id_rsa", "id_ed25519", "id_dsa", "credentials.json"}
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
PULL_REQUEST_EVENTS = {"pull_request", "pull_request_target"}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Symlinks (120000) and submodules (160000) are not tiles or claims.
FILE_MODES = {"100644", "100755"}


def forbidden_reason(path: str) -> str | None:
    name = Path(path).name.lower()
    if name == ".env" or name.startswith(".env."):
        return "environment file"
    if name in FORBIDDEN_NAMES:
        return "credential file"
    if name.endswith(FORBIDDEN_SUFFIXES):
        return "key file"
    return None


def pull_request_change_ok(status: str, path: str, districts: Collection[str]) -> bool:
    if status.startswith("R"):
        return False
    claim = CLAIM_RE.fullmatch(path)
    result = RESULT_RE.fullmatch(path)
    if status == "D":
        return claim is not None and claim.group(1) in districts
    if status not in {"A", "M"}:
        return False
    if claim is not None:
        return claim.group(1) in districts
    if result is not None:
        return result.group(1) in districts
    return False


def _is_owner(login: str, owner: str) -> bool:
    return bool(login) and bool(owner) and login.lower() == owner.lower()


def contributor_event(event: str, actor: str, owner: str, pr_author: str = "") -> bool:
    """True when the change comes from a contributor and must stay inside the door.

    Contributors push to the shared branch, so a pull request is not the only way in.
    The owner's own pushes carry the docs and scripts and are not held to the allowlist.
    Neither is a pull request the owner opened, while the owner is also the one who
    triggered this run. Both names are needed: a contributor's push to the owner's pull
    request is judged, and so is a contributor's pull request the owner reopens.
    """
    if event in PULL_REQUEST_EVENTS:
        return not (_is_owner(pr_author, owner) and _is_owner(actor, owner))
    return event == "push" and bool(actor) and bool(owner) and not _is_owner(actor, owner)


def claim_problem(text: str, slug: str, row: dict) -> str | None:
    match = CLAIMED_AT_RE.search(text)
    if match is None:
        return "has no claimed_at line"
    stamp = match.group(1)
    try:
        datetime.fromisoformat(stamp)
    except ValueError:
        return "has a claimed_at that is not a timestamp"
    if text != claim_text(slug, row, stamp):
        return "is not the file scripts/claim_district.py writes. Do not edit claims by hand"
    return None


def blob_at(root: Path, rev: str, path: str) -> tuple[str, bytes] | None:
    """File mode and content of one path at a revision, read without checking it out."""
    listing = subprocess.run(
        ["git", "ls-tree", rev, "--", path],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    fields = listing.stdout.split()
    if listing.returncode != 0 or len(fields) < 3:
        return None
    mode, sha = fields[0], fields[2]
    content = subprocess.run(["git", "cat-file", "blob", sha], cwd=root, check=False, capture_output=True)
    if content.returncode != 0:
        return None
    return mode, content.stdout


def content_problem(root: Path, rev: str, path: str, districts: dict[str, dict]) -> str | None:
    found = blob_at(root, rev, path)
    if found is None:
        return "could not be read at the pushed revision"
    mode, data = found
    if mode not in FILE_MODES:
        return "must be a regular file"
    if RESULT_RE.fullmatch(path) is not None:
        if not data.startswith(PNG_MAGIC):
            return "is not a PNG"
        return None
    claim = CLAIM_RE.fullmatch(path)
    if claim is None:
        return "is not a claim or a tile"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "is not UTF-8 text"
    return claim_problem(text, claim.group(1), districts[claim.group(1)])


def scan_tree(root: Path) -> list[str]:
    bad = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", ".venv", "venv", "__pycache__"} for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        reason = forbidden_reason(rel)
        if reason:
            bad.append(f"{rel} ({reason})")
    return bad


def _zero(sha: str | None) -> bool:
    if not sha:
        return True
    return set(sha) <= {"0"}


def changed_paths(root: Path, base: str, head: str) -> list[tuple[str, str]] | None:
    """Status and path of every file the change touches, or None when git cannot compare."""
    result = subprocess.run(
        ["git", "diff", "--name-status", f"{base}...{head}"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    changes = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changes.append((parts[0], parts[-1]))
    return changes


def secret_problems(root: Path, base: str, head: str) -> list[str]:
    """Secret-like files a change adds. The owner's pull request is held to this part.

    The workflow checks out the base branch, so scan_tree never sees a pull request.
    """
    changes = changed_paths(root, base, head)
    if changes is None:
        return [f"Could not diff {base}...{head}"]
    problems = []
    for status, path in changes:
        reason = forbidden_reason(path)
        if reason and status != "D":
            problems.append(f"{path} ({reason})")
    return problems


def diff_problems(root: Path, base: str, head: str, districts: dict[str, dict]) -> list[str]:
    changes = changed_paths(root, base, head)
    if changes is None:
        return [f"Could not diff {base}...{head}"]
    problems = []
    for status, path in changes:
        reason = forbidden_reason(path)
        if reason:
            problems.append(f"{path} ({reason})")
            continue
        if not pull_request_change_ok(status, path, districts):
            problems.append(
                f"{path} is not allowed in a contributor change. "
                "Only claims/<district>.md and results/<district>.png may change."
            )
            continue
        if status != "D":
            problem = content_problem(root, head, path, districts)
            if problem:
                problems.append(f"{path} {problem}.")
    return problems


def main() -> int:
    root = repo_root()
    problems = scan_tree(root)
    event = os.environ.get("EVENT", "")
    actor = os.environ.get("ACTOR", "")
    owner = os.environ.get("OWNER", "")
    pr_author = os.environ.get("PR_AUTHOR", "")
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    pull_request = event in PULL_REQUEST_EVENTS
    contributor = contributor_event(event, actor, owner, pr_author)
    if contributor or pull_request:
        default_branch = os.environ.get("DEFAULT_BRANCH", "")
        # A first push to a new branch has no previous revision. Compare it with the default branch.
        if _zero(base) and event == "push" and default_branch:
            base = f"origin/{default_branch}"
        if _zero(base) or _zero(head):
            problems.append("This change has no base or head revision to compare.")
        elif contributor:
            problems.extend(diff_problems(root, base, head, load_districts(root)))
        else:
            problems.extend(secret_problems(root, base, head))
    what = "pull request" if pull_request else "push"
    if problems:
        print(f"This {what} does not pass the door check:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    if pull_request and not contributor:
        print("door check: ok (the owner's pull request, not held to the contributor allowlist)")
    else:
        print("door check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
