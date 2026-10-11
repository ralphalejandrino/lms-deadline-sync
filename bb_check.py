#!/usr/bin/env python3
"""Is the saved profile actually authenticated? Ask the servers, don't guess.

Blackboard: /learn/api/public/v1/users/me returns 200 + JSON when the session is
real, 401 otherwise. Unambiguous, unlike sniffing URLs or cookies.
Classroom: same Google SSO identity, so if the Blackboard SAML login worked the
Google session cookie is already in this profile -- check whether Classroom
renders courses or bounces to a sign-in page.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bb_session import probe_all
from browser import launch
from school_config import PROFILE  # Nebula Phase 0: one home for shared constants

SHOTS = Path.home() / ".cache/ub-lms"
BB_ME = "https://blackboard.ubaguio.edu/learn/api/public/v1/users/me"


def main() -> int:
    # Blackboard: report EVERY source, not just the scraper profile. A dead
    # profile with a live Comet session is a working setup, and the old
    # single-source check called that "not authenticated".
    print("# Blackboard sources — LIVE means /users/me actually returned 200")
    live = 0
    for source, status, detail in probe_all():
        print(f"  {source:8s} {status}  {detail}")
        live += status.startswith("✅")
    print(f"BB_LIVE_SOURCES={live}")
    if not live:
        print("BB_STATUS=401")
        print("🔴 No live Blackboard source — every course is UNKNOWN, not 'nothing due'.")
        print("   Sign in to Blackboard in the open Comet window, or run ./lms.sh login")

    # Classroom rides the same Google SSO identity, and only ever lived in the
    # scraper profile, so it is still checked there specifically.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = launch(p, PROFILE, viewport={"width": 1400, "height": 900})
        page.goto("https://classroom.google.com/", wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        print(f"GC_URL={page.url}")
        print(f"GC_TITLE={page.title()}")
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / "gc_state.png"), full_page=False)
        ctx.close()
    return 0 if live else 2


if __name__ == "__main__":
    sys.exit(main())
