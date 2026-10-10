#!/usr/bin/env python3
"""Google Classroom out of the saved scraper profile — with the account PROVEN.

## Why this exists (2026-08-29)

`scan.py` declared `classroom` a source and then never read it: `scan_classroom()`
returned a note telling the reader to run a different command, and the coverage
cell defaulted to `n/a`, which `print_matrix` does not count as a gap. So a scan
could print "✅ Every source reported for every course" having never opened
Classroom. This module is the half that actually reads it.

The official API (`classroom_api.py`) is the better transport and needs no
browser, but it has never been authorised — `~/.config/ub-classroom/token.json`
does not exist. Until it does, the profile scrape is the only working path, so
`scan` uses the API when the token is there and this when it is not.

## 🔴 THE RULE THIS FILE ENCODES: prove the account, never assume the index

The vault recorded for a week that the scrapers were pointed at `u/0`, "the empty
personal account", and that `u/1` was the school one. Measured 2026-08-29: in
THIS profile every `u/N` redirects to `u/0` and returns the school account —
the `u/1` observation was true of Comet, a different browser with a different
account order, and was never true here.

Both the old note and a "fix" that hardcoded `u/1` would have been wrong, and
wrong in the direction that costs points: a Classroom pointed at an account with
no courses returns an empty to-do list, which reads exactly like being caught up.

So this module hardcodes no index at all. It lets Google resolve the default,
then reads the ACTIVE account off the account button and refuses to return data
unless it is Ralph's school address. The signal is the one Google renders for a
human — `a[aria-label*='Google Account']`, whose label carries the active
account's email and NOT the other entries in the switcher.
"""
import os
import argparse
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import browser

PROFILE = pathlib.Path.home() / ".local/share/ub-blackboard-profile"
SCHOOL_ACCOUNT = os.environ.get("LMS_SCHOOL_ACCOUNT", "student@s.ubaguio.edu")
HOME = "https://classroom.google.com/h"
TODO = "https://classroom.google.com/a/not-turned-in/all"

# Classroom carries these three; the rest of the grid is Blackboard-only. Naming
# them is part of the output contract -- a course absent from Classroom is a true
# n/a, and a course that SHOULD be here and is missing is a gap worth shouting
# about. Mirrors classroom_api.BLIND from the other direction.
CLASSROOM_COURSES = ("MATHMW1", "NSTPRO1", "INTRCS1")

# Course code out of a Classroom course title, e.g. "2627_IAA1-INTRCS1" or
# "1 - IAA1 - NSTPRO1". The codes are fixed and few, so match them rather than
# trying to parse a naming scheme the instructors do not share.
CODES = ("PROGIT1", "HUMCOM1", "INTRCS1", "MATHMW1", "LITERA1",
         "ARTAPP1", "NSTPRO1", "PATHFT1", "CMPTRB1", "SOCORN2")


def code_of(title):
    up = (title or "").upper().replace(" ", "")
    for c in CODES:
        if c in up:
            return c
    return None


def active_account(page):
    """The email Google says is ACTIVE, or None if the page will not say.

    Reads the account button's aria-label rather than scraping any email from
    the HTML: when two accounts are signed in, both appear in the markup and
    only this label distinguishes the one whose data is on screen.
    """
    el = page.query_selector("a[aria-label*='Google Account']")
    if not el:
        return None
    m = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                  el.get_attribute("aria-label") or "")
    return m.group(0) if m else None


def fetch(show=False, expect=SCHOOL_ACCOUNT, streams=True):
    """Read Classroom. Returns (data, err); err is a string when nothing is usable.

    Never raises — a caller must be able to record UNKNOWN instead of crashing.
    """
    data = {"account": None, "courses": [], "todo": "", "streams": {}}
    try:
        with sync_playwright() as p:
            ctx, page = browser.launch(p, PROFILE, show=show,
                                       viewport={"width": 1200, "height": 900})
            try:
                page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(7000)
                if "accounts.google.com" in page.url or "signin" in page.url:
                    return data, "Classroom session dead — bounced to sign-in"

                acct = active_account(page)
                data["account"] = acct
                if acct is None:
                    return data, ("could not read the active Google account — "
                                  "refusing to report Classroom as read")
                if acct.lower() != expect.lower():
                    return data, (f"WRONG ACCOUNT: Classroom is showing {acct}, "
                                  f"expected {expect}. Anything read here would "
                                  f"describe the wrong person's coursework.")

                for a in page.query_selector_all("a[href*='/c/']"):
                    m = re.search(r"/c/([A-Za-z0-9_-]+)", a.get_attribute("href") or "")
                    if not m:
                        continue
                    cid = m.group(1)
                    if any(c["id"] == cid for c in data["courses"]):
                        continue
                    # 🔴 NOT split("\n")[0]. The first line of a course tile is
                    # the AVATAR INITIAL -- "M", "1", "2" -- not the course
                    # name, and reading it that way made every code come back
                    # unidentified while the tile plainly said MATHMW1
                    # (measured 2026-08-29). Take the whole tile text and match
                    # a known code anywhere inside it.
                    full = " ".join((a.inner_text() or "").split())
                    data["courses"].append({"id": cid, "title": full[:80],
                                            "code": code_of(full)})

                page.goto(TODO, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(7000)
                txt = re.sub(r"\n{2,}", "\n", page.evaluate("document.body.innerText"))
                m = re.search(r"(No due date.*?)(Help and Feedback|$)", txt, re.S)
                data["todo"] = (m.group(1) if m else txt)[:4000]

                # The to-do panel only ever lists GRADED assignments. It cannot
                # see an announcement, nor work whose instructions live inside an
                # attachment -- which is exactly how the NSTPRO1 20-pointer was
                # missed on 2026-08-13. The streams are where that shows up.
                if streams:
                    for c in data["courses"]:
                        try:
                            page.goto(f"https://classroom.google.com/c/{c['id']}",
                                      wait_until="domcontentloaded", timeout=60000)
                            page.wait_for_timeout(5000)
                            for _ in range(6):          # expand every "See more"
                                try:
                                    page.get_by_role(
                                        "button", name=re.compile(r"\bmore\b", re.I)
                                    ).first.click(timeout=2000)
                                    page.wait_for_timeout(700)
                                except Exception:
                                    break
                            # 🔴 This text still contains Classroom's PAGE CHROME
                            # — the sidebar ("Home Calendar Gemini Enrolled
                            # To-do", the class list, "Archived classes"). On
                            # 2026-08-29 that produced three CANDIDATEs whose
                            # only "action word" was the word *due* inside
                            # "To-do" in the navigation, quoting the sidebar back
                            # as evidence. They had to be deleted from inbox.md.
                            #
                            # The official API (classroom_api.fetch) returns
                            # announcement TEXT with no chrome and is preferred
                            # for exactly this reason. This path is the fallback;
                            # treat its candidates with suspicion, and strip the
                            # nav before trusting a keyword hit.
                            body = page.evaluate("document.body.innerText")
                            body = re.split(r"Add class comment|Add comment", body)[0]
                            body = re.split(r"Archived classes|Stream\s+Classwork\s+People",
                                            body)[-1]
                            data["streams"][c["id"]] = re.sub(r"\n{3,}", "\n\n", body)
                        except Exception as e:
                            data["streams"][c["id"]] = None
                            print(f"  🔴 stream unreadable for {c['title']}: "
                                  f"{type(e).__name__}", file=sys.stderr)
            finally:
                ctx.close()
    except Exception as e:
        return data, f"{type(e).__name__}: {e}"[:160]
    return data, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--no-streams", action="store_true")
    ap.add_argument("--expect", default=SCHOOL_ACCOUNT,
                    help="account that MUST be active (identity assertion)")
    a = ap.parse_args()

    data, err = fetch(show=a.show, expect=a.expect, streams=not a.no_streams)
    if err:
        print(f"🔴 CLASSROOM UNKNOWN — {err}", file=sys.stderr)
        print("   This is NOT 'nothing due'. Nothing was read.", file=sys.stderr)
        return 2

    print(f"# Google Classroom — account {data['account']}")
    print(f"\n## Courses ({len(data['courses'])})")
    for c in data["courses"]:
        print(f"  · {c['code'] or '?':8s} {c['title']}")
    missing = [c for c in CLASSROOM_COURSES
               if c not in {x["code"] for x in data["courses"]}]
    if missing:
        print(f"\n  🔴 EXPECTED but ABSENT: {', '.join(missing)} — "
              f"treat these as UNKNOWN, not clear.")
    print("\n## To-do / not turned in")
    print("  " + data["todo"].replace("\n", "\n  "))
    # Only report streams when they were actually requested — an empty dict
    # under --no-streams is "not asked for", not "unreadable", and printing the
    # red marker for it invents a failure that did not happen.
    if not a.no_streams:
        for c in data["courses"]:
            s = data["streams"].get(c["id"])
            print(f"\n===== stream: {c['title']}")
            print("  🔴 unreadable" if s is None else s[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
