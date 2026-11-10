"""Shared paths and git helpers for the contributor door."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_districts(root: Path | None = None) -> dict[str, dict]:
    root = root or repo_root()
    data = json.loads((root / "districts.json").read_text(encoding="utf-8"))
    districts = {}
    for row in data["districts"]:
        slug = row["slug"]
        if not SLUG_RE.fullmatch(slug):
            raise ValueError(f"Unsafe district slug in districts.json: {slug}")
        districts[slug] = row
    return districts


def claim_text(slug: str, row: dict, claimed_at: str) -> str:
    """The exact claim file. The door check rejects a claim that differs from this."""
    return "\n".join(
        [
            "# Claim",
            "",
            f"district: {slug}",
            f"name: {row['name']}",
            f"code: {row['code']}",
            "status: claimed",
            f"claimed_at: {claimed_at}",
            "",
            f"One district, one agent. The only result file for this claim is results/{slug}.png.",
            "",
        ]
    )


def run_git(root: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=check,
        text=True,
        capture_output=True,
    )


def git_out(root: Path, args: list[str]) -> str:
    return run_git(root, args).stdout


def git_config(root: Path, key: str) -> str:
    result = run_git(root, ["config", "--get", key], check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def require_identity(root: Path) -> None:
    email = git_config(root, "user.email")
    name = git_config(root, "user.name")
    if not email or not name:
        raise SystemExit(
            "Set your own git identity in this clone before committing.\n"
            '  git config user.name "Your Name"\n'
            '  git config user.email "you@example.com"\n'
            "Use the email on your GitHub account. Do not commit tokens or environment files."
        )


def branch_name(root: Path) -> str:
    name = git_out(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    if not name or name == "HEAD":
        raise SystemExit("This clone is in detached HEAD. Check out the branch you were invited to push to.")
    return name


def has_origin(root: Path) -> bool:
    result = run_git(root, ["remote", "get-url", "origin"], check=False)
    return result.returncode == 0 and bool(result.stdout.strip())
