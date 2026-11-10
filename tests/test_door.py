"""Door checks that do not download a model or call the price API."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_push  # noqa: E402
import claim_district  # noqa: E402
import publish_png  # noqa: E402
import redraw_tile  # noqa: E402
import se3_price  # noqa: E402
from repo import claim_text, load_districts  # noqa: E402

STOCKHOLM = ZoneInfo("Europe/Stockholm")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def slots_for(day, prices):
    start = datetime(day.year, day.month, day.day, tzinfo=STOCKHOLM)
    rows = []
    for index, price in enumerate(prices):
        slot_start = start + timedelta(minutes=15 * index)
        rows.append(se3_price.Slot(slot_start, slot_start + timedelta(minutes=15), float(price)))
    return rows


class GridTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(redraw_tile.WINDOW, 1024)
        self.assertEqual(redraw_tile.STEP, 512)
        self.assertEqual(redraw_tile.MODEL_ID, "addlabsviral/Qwen-Image-2.1-Turbo-4bit")
        self.assertEqual(redraw_tile.LORA_ID, "handful/mndf-pixel-qwen-image-2.1")

    def test_pad_and_views(self):
        self.assertEqual(redraw_tile.pad_size(500), 1024)
        self.assertEqual(redraw_tile.pad_size(1024), 1024)
        self.assertEqual(redraw_tile.pad_size(1025), 1536)
        self.assertEqual(redraw_tile.pad_size(2048), 2048)
        self.assertEqual(redraw_tile.get_views(1024, 1024), [(0, 0)])
        self.assertEqual(redraw_tile.get_views(1536, 1024), [(0, 0), (0, 512)])
        self.assertEqual(len(redraw_tile.get_views(2048, 2048)), 9)

    def test_windows_cover_the_tile_and_overlap(self):
        full_h, full_w = 1536, 2048
        scale = 16
        covered = [0] * ((full_h // scale) * (full_w // scale))
        for top, left in redraw_tile.get_views(full_w, full_h):
            for index in redraw_tile.window_token_indices(full_h, full_w, top, left, scale=scale):
                covered[index] += 1
        self.assertTrue(all(count >= 1 for count in covered))
        self.assertTrue(any(count > 1 for count in covered))

    def test_prompt_uses_the_lora_phrase(self):
        text = redraw_tile.prompt_for("Stockholm")
        self.assertIn("mndfstyle", text)
        self.assertIn("Stockholm", text)

    def test_districts_are_safe_slugs(self):
        districts = load_districts(ROOT)
        self.assertEqual(len(districts), 290)
        self.assertEqual(districts["stockholm"]["code"], "0180")
        self.assertEqual(districts["goteborg"]["name"], "Göteborg")


class PriceTests(unittest.TestCase):
    def test_public_url_has_no_key(self):
        url = se3_price.price_url(datetime(2026, 11, 10).date())
        self.assertEqual(url, "https://www.elprisetjustnu.se/api/v1/prices/2026/11-10_SE3.json")
        self.assertNotIn("token", url)
        self.assertNotIn("key", url)

    def test_high_price_waits_for_the_next_day(self):
        now = datetime(2026, 11, 9, 18, 30, tzinfo=STOCKHOLM)
        today = slots_for(now.date(), [4.0] * 96)
        tomorrow_prices = [1.2] * 12 + [0.4] * 4 + [2.0] * 80
        tomorrow = slots_for(now.date() + timedelta(days=1), tomorrow_prices)
        report = se3_price.evaluate(now, today, tomorrow, now.date() + timedelta(days=1))
        self.assertEqual(report["decision"], "WAIT")
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["horizon"], "next-day")
        self.assertIn("Do not claim", report["action"])
        self.assertIn("do not run the redraw", report["action"])
        self.assertTrue(report["cheapest_hour_start"].startswith("2026-11-10T03:00:00"))

    def test_runs_during_the_cheap_hour(self):
        day = datetime(2026, 11, 10, 3, 15, tzinfo=STOCKHOLM)
        prices = [1.2] * 12 + [0.4] * 4 + [2.0] * 80
        report = se3_price.evaluate(day, None, slots_for(day.date(), prices), day.date())
        self.assertEqual(report["decision"], "RUN")
        self.assertEqual(report["exit_code"], 0)

    def test_negative_price_floor(self):
        now = datetime(2026, 11, 10, 12, 0, tzinfo=STOCKHOLM)
        cheap = [-0.02] * 4 + [0.20] * 92
        report = se3_price.evaluate(now, None, slots_for(now.date(), cheap), now.date())
        self.assertEqual(report["decision"], "WAIT")
        near = datetime(2026, 11, 10, 0, 10, tzinfo=STOCKHOLM)
        near_report = se3_price.evaluate(near, None, slots_for(near.date(), cheap), near.date())
        self.assertEqual(near_report["decision"], "RUN")

    def test_unpublished_next_day_can_use_the_rest_of_today(self):
        now = datetime(2026, 11, 9, 4, 0, tzinfo=STOCKHOLM)
        today = slots_for(now.date(), [0.3] * 20 + [3.0] * 76)
        report = se3_price.evaluate(now, today, None, now.date() + timedelta(days=1))
        self.assertEqual(report["next_day_published"], "no")
        self.assertEqual(report["horizon"], "rest-of-today")
        self.assertEqual(report["decision"], "RUN")
        self.assertIn("not published", report["action"])

    def test_api_failure_does_not_say_run(self):
        def boom(_day):
            raise RuntimeError("offline")

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = se3_price.main(fetch=boom, clock=lambda: datetime(2026, 11, 9, 18, 0, tzinfo=STOCKHOLM))
        text = buffer.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("decision: WAIT", text)
        self.assertNotIn("decision: RUN", text)


class DoorPolicyTests(unittest.TestCase):
    def test_forbidden_names(self):
        self.assertIsNotNone(check_push.forbidden_reason(".env"))
        self.assertIsNotNone(check_push.forbidden_reason("nested/.env.local"))
        self.assertIsNotNone(check_push.forbidden_reason("id_rsa"))
        self.assertIsNotNone(check_push.forbidden_reason("server.pem"))
        self.assertIsNone(check_push.forbidden_reason("scripts/se3_price.py"))
        self.assertIsNone(check_push.forbidden_reason("results/stockholm.png"))

    def test_pull_request_allowlist(self):
        districts = set(load_districts(ROOT))
        self.assertTrue(check_push.pull_request_change_ok("A", "claims/stockholm.md", districts))
        self.assertTrue(check_push.pull_request_change_ok("A", "results/stockholm.png", districts))
        self.assertTrue(check_push.pull_request_change_ok("D", "claims/stockholm.md", districts))
        self.assertFalse(check_push.pull_request_change_ok("D", "results/stockholm.png", districts))
        self.assertFalse(check_push.pull_request_change_ok("A", "results/not-a-place.png", districts))
        self.assertFalse(check_push.pull_request_change_ok("M", "README.md", districts))
        self.assertFalse(check_push.pull_request_change_ok("A", ".env", districts))

    def test_this_repo_has_no_forbidden_files(self):
        self.assertEqual(check_push.scan_tree(ROOT), [])

    def test_contributor_events(self):
        self.assertTrue(check_push.contributor_event("pull_request_target", "guest", "Owner", "guest"))
        self.assertTrue(check_push.contributor_event("push", "guest", "Owner"))
        self.assertFalse(check_push.contributor_event("push", "owner", "Owner"))
        self.assertFalse(check_push.contributor_event("", "", ""))

    def test_only_the_owners_own_pull_request_skips_the_allowlist(self):
        # The owner opened it, and the owner triggered this run.
        self.assertFalse(check_push.contributor_event("pull_request_target", "owner", "Owner", "Owner"))
        # A contributor pushed to the owner's pull request.
        self.assertTrue(check_push.contributor_event("pull_request_target", "guest", "Owner", "Owner"))
        # The owner reopened, or pushed to, a contributor's pull request.
        self.assertTrue(check_push.contributor_event("pull_request_target", "Owner", "Owner", "guest"))
        # A missing name never counts as the owner.
        self.assertTrue(check_push.contributor_event("pull_request_target", "Owner", "Owner"))
        self.assertTrue(check_push.contributor_event("pull_request_target", "Owner", "", "Owner"))
        self.assertTrue(check_push.contributor_event("pull_request_target", "", "", ""))

    def test_claim_doc_shows_the_real_claim_file(self):
        row = load_districts(ROOT)["stockholm"]
        expected = claim_text("stockholm", row, "2026-11-09T17:35:00+00:00")
        doc = (ROOT / "CLAIM.md").read_text(encoding="utf-8")
        self.assertIn("```\n" + expected + "```", doc)


class GitDoorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="door-"))
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Door Test")
        self.git("config", "user.email", "door-test@example.com")
        self.git("config", "commit.gpgsign", "false")
        shutil.copy(ROOT / "districts.json", self.root / "districts.json")
        shutil.copy(ROOT / ".gitattributes", self.root / ".gitattributes")
        (self.root / "claims").mkdir()
        (self.root / "results").mkdir()
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        self.git("add", "districts.json", ".gitattributes", "README.md")
        self.git("commit", "-m", "base")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args):
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"git {args} failed\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def test_claim_is_exclusive_and_publish_commits_only_the_png(self):
        claim_district.claim_district(self.root, "stockholm", push=False)
        with self.assertRaises(SystemExit):
            claim_district.claim_district(self.root, "stockholm", push=False)
        (self.root / "README.md").write_text("base\nlocal note that must stay unstaged\n", encoding="utf-8")
        (self.root / ".env").write_text("not-a-real-secret\n", encoding="utf-8")
        self.git("add", ".env")
        dest = self.root / "results" / "stockholm.png"
        dest.write_bytes(PNG)
        publish_png.publish_png(self.root, "stockholm", push=False)
        names = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").split()
        self.assertEqual(names, ["results/stockholm.png"])
        tracked = self.git("ls-files")
        self.assertNotIn(".env", tracked.split())
        self.assertTrue((self.root / ".env").is_file())
        status = self.git("status", "--porcelain")
        self.assertIn("README.md", status)

    def test_release_frees_the_district(self):
        claim_district.claim_district(self.root, "uppsala", push=False)
        claim_district.release_claim(self.root, "uppsala", push=False)
        claim_district.claim_district(self.root, "uppsala", push=False)
        self.assertTrue((self.root / "claims" / "uppsala.md").is_file())

    def test_publish_rejects_a_non_png(self):
        claim_district.claim_district(self.root, "lund", push=False)
        (self.root / "results" / "lund.png").write_text("not a png", encoding="utf-8")
        with self.assertRaises(SystemExit):
            publish_png.publish_png(self.root, "lund", push=False)

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def door(self, base):
        return check_push.diff_problems(self.root, base, "HEAD", load_districts(self.root))

    def test_door_passes_a_scripted_claim_and_tile(self):
        base = self.head()
        claim_district.claim_district(self.root, "stockholm", push=False)
        (self.root / "results" / "stockholm.png").write_bytes(PNG)
        publish_png.publish_png(self.root, "stockholm", push=False)
        self.assertEqual(self.door(base), [])

    def test_door_passes_a_release(self):
        claim_district.claim_district(self.root, "uppsala", push=False)
        base = self.head()
        claim_district.release_claim(self.root, "uppsala", push=False)
        self.assertEqual(self.door(base), [])

    def test_door_rejects_a_tile_that_is_not_a_png(self):
        base = self.head()
        (self.root / "results" / "lund.png").write_text("<html>", encoding="utf-8")
        self.git("add", "results/lund.png")
        self.git("commit", "-m", "fake tile")
        problems = self.door(base)
        self.assertEqual(len(problems), 1)
        self.assertIn("is not a PNG", problems[0])

    def test_door_rejects_a_hand_edited_claim(self):
        claim_district.claim_district(self.root, "uppsala", push=False)
        base = self.head()
        claim = self.root / "claims" / "uppsala.md"
        with claim.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("machine: my-rig\n")
        self.git("add", "claims/uppsala.md")
        self.git("commit", "-m", "edit claim")
        problems = self.door(base)
        self.assertEqual(len(problems), 1)
        self.assertIn("Do not edit claims by hand", problems[0])

    def test_door_rejects_a_claim_for_another_district(self):
        base = self.head()
        row = load_districts(self.root)["lund"]
        claim = self.root / "claims" / "malmo.md"
        claim.write_text(claim_text("lund", row, "2026-11-09T17:35:00+00:00"), encoding="utf-8", newline="\n")
        self.git("add", "claims/malmo.md")
        self.git("commit", "-m", "wrong claim")
        self.assertEqual(len(self.door(base)), 1)

    def test_door_rejects_a_symlink_and_files_outside_the_door(self):
        base = self.head()
        # Build the symlink entry in the index, so the test also runs where the OS cannot create one.
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=self.root,
            input="../README.md",
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{blob},results/malmo.png")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "redraw_tile.py").write_text("print('changed')\n", encoding="utf-8")
        self.git("add", "scripts/redraw_tile.py")
        self.git("commit", "-m", "outside the door")
        problems = self.door(base)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("results/malmo.png must be a regular file" in problem for problem in problems))
        self.assertTrue(any("scripts/redraw_tile.py is not allowed" in problem for problem in problems))

    def test_owner_pull_request_is_still_scanned_for_secret_files(self):
        base = self.head()
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "redraw_tile.py").write_text("print('changed')\n", encoding="utf-8")
        self.git("add", "scripts/redraw_tile.py")
        self.git("commit", "-m", "owner change")
        self.assertEqual(check_push.secret_problems(self.root, base, "HEAD"), [])
        (self.root / ".env").write_text("TOKEN=1\n", encoding="utf-8")
        (self.root / "scripts" / "server.pem").write_text("key\n", encoding="utf-8")
        self.git("add", "-f", ".env", "scripts/server.pem")
        self.git("commit", "-m", "owner mistake")
        self.assertEqual(
            sorted(check_push.secret_problems(self.root, base, "HEAD")),
            [".env (environment file)", "scripts/server.pem (key file)"],
        )
        self.git("rm", "-q", ".env", "scripts/server.pem")
        self.git("commit", "-m", "owner cleanup")
        self.assertEqual(check_push.secret_problems(self.root, self.git("rev-parse", "HEAD~1").strip(), "HEAD"), [])


if __name__ == "__main__":
    unittest.main()
