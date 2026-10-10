#!/usr/bin/env python3
"""Two questions: has the overdue Blackboard item been submitted, and what does
Google Classroom say is outstanding?

Blackboard stores due dates in UTC; Ralph is UTC+8 (Asia/Manila), so every time
is converted before it is reported. An 09:00Z deadline is 17:00 the same day in
Baguio -- the sort of off-by-eight-hours that makes a missed deadline look fine.
"""
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import browser
from bb_sweep import authoritative_due

PROFILE = Path.home() / ".local/share/ub-blackboard-profile"
SHOTS = Path.home() / ".cache/ub-lms"
BASE = "https://blackboard.ubaguio.edu/learn/api/public/v1"
MNL = timezone(timedelta(hours=8))


def to_mnl(iso: str) -> str:
    if not iso:
        return "-"
    return (datetime.fromisoformat(iso.replace("Z", "+00:00"))
            .astimezone(MNL).strftime("%Y-%m-%d %H:%M %Z"))


def main() -> int:
    bb = json.load(open(SHOTS / "bb_data.json"))
    uid = bb["me"]["id"]
    now = datetime.now(timezone.utc)
    print(f"NOW_MANILA={now.astimezone(MNL):%Y-%m-%d %H:%M}")

    with sync_playwright() as p:
        # 🔴 THIS USED TO POP A REAL WINDOW ON RALPH'S SCREEN (fixed 2026-08-29).
        # It called launch_persistent_context directly with headless=False and
        # tried to hide the window off-screen at --window-position=-32000,-32000.
        # That hack is not reliable: WSLg's compositor can clamp a window back
        # into view, so a background scan interrupted whatever he was doing.
        # `browser.launch()` is the one place that knows the right incantation
        # (real Chromium + --headless=new, which is invisible AND keeps the
        # session that headless=True silently breaks). Never bypass it.
        ctx, page = browser.launch(p, PROFILE, viewport={"width": 1400, "height": 950})
        page.goto("https://blackboard.ubaguio.edu/ultra/course", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        print("\n=== BLACKBOARD: submission status ===")
        for c in bb["courses"]:
            cid = c["courseId"]
            code = c["courseCode"].split("_")[-1]
            cols = page.request.get(f"{BASE}/courses/{cid}/gradebook/columns?limit=100")
            if cols.status != 200:
                continue
            def _dget(u):
                r = page.request.get(u)
                return r.json() if r.status == 200 else {"_status": r.status}

            for col in cols.json().get("results", []):
                colid = col.get("id")
                # The column LIST serves a cached grading.due. Re-read the column.
                due, due_warn = authoritative_due(
                    _dget, cid, colid, (col.get("grading") or {}).get("due"))
                if due_warn:
                    print(f"   🔴 [{code}] {col.get('name')!r} {due_warn}")
                r = page.request.get(
                    f"{BASE}/courses/{cid}/gradebook/columns/{colid}/users/{uid}")
                st = "?"
                if r.status == 200:
                    j = r.json()
                    st = j.get("status") or "-"
                    disp = j.get("displayGrade") or {}
                    st = f"status={st} score={j.get('score')} grade={disp.get('text')}"
                elif r.status == 404:
                    st = "NO ATTEMPT RECORDED"
                else:
                    st = f"http {r.status}"
                overdue = ""
                if due:
                    d = datetime.fromisoformat(due.replace("Z", "+00:00"))
                    overdue = "  <<< PAST DUE" if d < now else "  (upcoming)"
                print(f"[{code}] {col.get('name')}\n    due={to_mnl(due)}{overdue}\n    {st}")

        # ---------------- Google Classroom ----------------
        print("\n=== GOOGLE CLASSROOM ===")
        page.goto("https://classroom.google.com/u/0/h", wait_until="domcontentloaded")
        page.wait_for_timeout(7000)
        print(f"GC_URL={page.url}")
        body = page.inner_text("body")
        print("--- classes/home text ---")
        print(re.sub(r"\n{2,}", "\n", body)[:2500])
        page.screenshot(path=str(SHOTS / "gc_classes.png"), full_page=True)

        page.goto("https://classroom.google.com/u/0/a/not-turned-in/all",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(7000)
        print("\n--- TO-DO / not turned in ---")
        print(re.sub(r"\n{2,}", "\n", page.inner_text("body"))[:2500])
        page.screenshot(path=str(SHOTS / "gc_todo.png"), full_page=True)

        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
