"""Claim one district, or release a claim that has no finished PNG.

The claim file is the lock. Push it before you redraw. If the push is rejected,
someone else took that district: pick another one.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from repo import branch_name, claim_text, git_out, has_origin, load_districts, repo_root, require_identity, run_git


def _print_list(root: Path) -> int:
    districts = load_districts(root)
    for slug in sorted(districts):
        claimed = (root / "claims" / f"{slug}.md").exists()
        done = (root / "results" / f"{slug}.png").exists()
        if claimed or done:
            continue
        row = districts[slug]
        print(f"{slug}\t{row['name']}\t{row['code']}")
    return 0


def _sync(root: Path) -> None:
    if not has_origin(root):
        raise SystemExit("This clone has no origin remote. Use the repository you were invited to.")
    branch = branch_name(root)
    fetch = run_git(root, ["fetch", "origin"], check=False)
    if fetch.returncode != 0:
        raise SystemExit("Could not fetch origin. Do not claim a district until git fetch works.")
    pull = run_git(root, ["pull", "--ff-only", "origin", branch], check=False)
    if pull.returncode != 0:
        detail = (pull.stderr or pull.stdout or "").strip()
        raise SystemExit(f"Could not fast-forward {branch}. Resolve that before claiming.\n{detail}")


def _clean_worktree(root: Path) -> None:
    dirty = git_out(root, ["status", "--porcelain"]).strip()
    if dirty:
        raise SystemExit(
            "The worktree is not clean. Commit or remove local changes before claiming.\n"
            "Do not commit environment files, keys, or model weights."
        )


def _rollback_commit(root: Path, path: Path) -> None:
    run_git(root, ["reset", "--soft", "HEAD~1"], check=False)
    run_git(root, ["reset", "-q"], check=False)
    if path.exists():
        path.unlink()


def claim_district(root: Path, slug: str, push: bool = True) -> int:
    districts = load_districts(root)
    if slug not in districts:
        raise SystemExit(f"Unknown district {slug!r}. Run: python scripts/claim_district.py --list")
    if push:
        _sync(root)
        _clean_worktree(root)
    else:
        dirty = git_out(root, ["status", "--porcelain"]).strip()
        if dirty:
            raise SystemExit("The worktree is not clean.")
    claim = root / "claims" / f"{slug}.md"
    result = root / "results" / f"{slug}.png"
    if result.exists():
        raise SystemExit(f"{slug} already has results/{slug}.png. Pick another district.")
    if claim.exists():
        raise SystemExit(f"{slug} is already claimed. Pick another district.")
    row = districts[slug]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text(claim_text(slug, row, now), encoding="utf-8", newline="\n")
    require_identity(root)
    run_git(root, ["add", "--", f"claims/{slug}.md"])
    staged = [line for line in git_out(root, ["diff", "--cached", "--name-only"]).splitlines() if line]
    if staged != [f"claims/{slug}.md"]:
        run_git(root, ["reset", "-q"], check=False)
        if claim.exists():
            claim.unlink()
        raise SystemExit("Refusing to commit because more than the claim file would be included.")
    commit = run_git(root, ["commit", "-m", f"Claim {slug}"], check=False)
    if commit.returncode != 0:
        run_git(root, ["reset", "-q"], check=False)
        if claim.exists():
            claim.unlink()
        detail = (commit.stderr or commit.stdout or "").strip()
        raise SystemExit(f"Could not commit the claim.\n{detail}")
    if not push:
        print(f"Claimed {slug} locally. Push claims/{slug}.md before you redraw.")
        return 0
    branch = branch_name(root)
    pushed = run_git(root, ["push", "origin", f"HEAD:{branch}"], check=False)
    if pushed.returncode != 0:
        _rollback_commit(root, claim)
        run_git(root, ["pull", "--ff-only", "origin", branch], check=False)
        if claim.exists():
            raise SystemExit(
                f"Push was rejected and {slug} is already claimed on origin. "
                "Pick another district from --list. Do not redraw this one."
            )
        raise SystemExit(
            f"Push did not land, so the local claim for {slug} was rolled back. "
            "Fetch, run --list, and claim a district that is still free."
        )
    print(f"Claimed {slug}. Next: put your source render at inputs/{slug}.png and run the redraw.")
    print(f"Do not commit inputs/{slug}.png.")
    return 0


def release_claim(root: Path, slug: str, push: bool = True) -> int:
    districts = load_districts(root)
    if slug not in districts:
        raise SystemExit(f"Unknown district {slug!r}.")
    if push:
        _sync(root)
    claim = root / "claims" / f"{slug}.md"
    result = root / "results" / f"{slug}.png"
    if result.exists():
        raise SystemExit(f"{slug} already has results/{slug}.png. Leave the claim in place.")
    if not claim.exists():
        print(f"{slug} is already free.")
        return 0
    claim.unlink()
    require_identity(root)
    run_git(root, ["add", "-u", "--", f"claims/{slug}.md"])
    staged = [line for line in git_out(root, ["diff", "--cached", "--name-only"]).splitlines() if line]
    if staged != [f"claims/{slug}.md"]:
        run_git(root, ["reset", "-q"], check=False)
        raise SystemExit("Refusing to release because more than the claim file would be committed.")
    commit = run_git(root, ["commit", "-m", f"Release {slug}"], check=False)
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout or "").strip()
        raise SystemExit(f"Could not commit the release.\n{detail}")
    if not push:
        print(f"Released {slug} locally.")
        return 0
    branch = branch_name(root)
    pushed = run_git(root, ["push", "origin", f"HEAD:{branch}"], check=False)
    if pushed.returncode != 0:
        raise SystemExit(f"The release commit for {slug} is local only. Push it, or the district stays locked for others.")
    print(f"Released {slug}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claim one free district tile.")
    parser.add_argument("--district", help="District slug, for example stockholm")
    parser.add_argument("--list", action="store_true", help="Print unclaimed districts")
    parser.add_argument("--release", action="store_true", help="Give an unfinished claim back")
    parser.add_argument("--no-push", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = repo_root()
    if args.list:
        return _print_list(root)
    if not args.district:
        raise SystemExit("Pass --district <slug>, or --list to see free districts.")
    if args.release:
        return release_claim(root, args.district, push=not args.no_push)
    return claim_district(root, args.district, push=not args.no_push)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())
