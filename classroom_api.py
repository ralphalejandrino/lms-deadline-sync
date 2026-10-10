#!/usr/bin/env python3
"""Read Google Classroom through the OFFICIAL API, no browser, no scraping.

This is the laptop-independent replacement for the Classroom half of the
browser scrape. It needs only a refresh token, so it runs identically on REWSL
and on a cloud VM, and it does not put Ralph's Google session at risk.

Credential resolution, in order:
  1. $UB_CLASSROOM_TOKEN   -- the JSON blob, for cloud environment variables
  2. ~/.config/ub-classroom/token.json  -- the local file written by classroom_auth.py

## What it covers, and what it does NOT

Covers the three Classroom-primary subjects -- MATHMW1, NSTPRO1, INTRCS1 --
including the ones whose Blackboard shells are Closed, which is exactly where
this week's deadlines live.

🔴 It does NOT cover Blackboard, and therefore says nothing about PROGIT1,
SOCORN2 or CMPTRB1. Those three send no email either. If this script is the
only thing that ran, they are UNKNOWN, not clear. Print that, always -- a
silent empty result is indistinguishable from a healthy one, which is how the
NSTPRO1 activity was missed on 2026-08-13.
"""
import re
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _quiet_oauth  # noqa: F401 — silence the scope-mismatch log line
import gcreds

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

TOKEN = Path.home() / ".config/ub-classroom/token.json"
MNL = timezone(timedelta(hours=8))
# Subjects this API can never see. Naming them is part of the output contract.
BLIND = ["PROGIT1", "SOCORN2", "CMPTRB1"]


def creds():
    # The env-first rule this function pioneered now lives in gcreds, shared by
    # chat_api, mail_fetch and cal_apply too (2026-09-14).
    try:
        return gcreds.credentials("classroom")
    except gcreds.NoCredential as e:
        sys.exit(f"🔴 No Classroom credential.\n   {e}")


def due_dt(w):
    """Classroom stores dueDate/dueTime in UTC. Convert -- a 09:00Z deadline is
    17:00 Manila, and that exact conversion is why the first deadline of the
    term was missed."""
    d, t = w.get("dueDate"), w.get("dueTime") or {}
    if not d:
        return None
    return datetime(
        d["year"], d["month"], d["day"],
        t.get("hours", 23), t.get("minutes", 59), tzinfo=timezone.utc
    ).astimezone(MNL)


CODES = ("PROGIT1", "HUMCOM1", "INTRCS1", "MATHMW1", "LITERA1",
         "ARTAPP1", "NSTPRO1", "PATHFT1", "CMPTRB1", "SOCORN2")


def code_of(name):
    up = re.sub(r"[^A-Z0-9]", "", (name or "").upper())
    for c in CODES:
        if c in up or c.rstrip("1") in up:
            return c
    return None


# 🔴 COURSES RALPH HAS ABANDONED IN CLASSROOM (declared by him, 2026-08-29).
#
# He marked their work done and HID both classes; the subjects moved to
# Blackboard, and only NSTPRO1 still runs on Classroom. He plans to ask that
# instructor to move too, "so it's all in one place".
#
# This has to be DECLARED because the API cannot tell:
#   · hiding a class is a per-user UI preference Classroom does not expose —
#     all three courses still report courseState=ACTIVE (checked 2026-08-29);
#   · `courses.students.get` is 403 for a student, so there is no enrolment
#     flag to read either.
# Left unfiltered, their stale coursework reads as live: this scan reported
# `MATH IN MY FIELD` and `Natures Pattern Hunt` as NOT SUBMITTED and overdue,
# and both were dead items in an abandoned course.
#
# ⚠ This is an IGNORE list, never an allow list. A NEW Classroom course must
# still come through and be seen — the lesson from chat_fetch's hardcoded space
# list, which silently hid LITERA1 for weeks. Skips are reported, not silent.
# code -> the date Ralph declared it abandoned-in-Classroom. Not a bare set: the
# date lets the skip SELF-CORRECT. A hardcoded ignore list rots the moment
# enrolment shifts — the same class of bug that hid LITERA1's chat space for
# weeks. So when a skipped course shows activity NEWER than its stale_since, the
# scan shouts "confirm?" instead of silently swallowing it (2026-08-29).
# MATHMW1 UN-STALED 2026-09-15 (Ralph): Instructor E moved her First Grading exam to 10:00 AM in a
# Classroom-only post at 5:04 AM on exam day. The self-correct warning fired, and the post was
# found by mail, not by this scan — so her Classroom stream is live again and must be read.
STALE_COURSES = {"INTRCS1": "2026-08-29"}


def _has_activity_since(svc, cid, since_date):
    """Cheap check: any coursework/announcement updated after since_date (YYYY-MM-DD)?

    One call each, only ever run for the 1-2 stale courses. Returns the newest
    offending title, or None. Never raises — a failed check must not break the
    scan, so it degrades to None (no false 'confirm?')."""
    newest = None
    try:
        for kind, key, lister in (
            ("coursework", "courseWork",
             lambda: svc.courses().courseWork().list(
                 courseId=cid, pageSize=10, orderBy="updateTime desc").execute()),
            ("announcement", "announcements",
             lambda: svc.courses().announcements().list(
                 courseId=cid, pageSize=10, orderBy="updateTime desc").execute()),
        ):
            for x in (lister().get(key) or []):
                when = (x.get("updateTime") or "")[:10]
                if when > since_date:
                    raw = x.get("title") or x.get("text") or kind
                    label = " ".join(raw.split())[:50]   # collapse newlines/runs
                    newest = f"{label} ({when})"
                    break
            if newest:
                break
    except Exception:
        return None
    return newest


def available():
    return gcreds.available("classroom")


def fetch():
    """Structured Classroom read for `scan.py`. Returns (data, err).

    Library twin of main(): same API calls, no printing, and it NEVER calls
    sys.exit -- a caller must be able to record UNKNOWN instead of dying.

    ⚠ Submission state is read per item from studentSubmissions. It is the only
    thing that says "handed in"; a passed due date says nothing, and inferring
    from it is how a submitted item gets re-flagged (or worse, an unsubmitted
    one looks done).
    """
    if not available():
        return None, (f"no token: neither $UB_CLASSROOM_TOKEN nor {TOKEN} — "
                      "run ./lms.sh classroom-auth --serve")
    try:
        cr = gcreds.credentials("classroom", quiet=True)
        svc = build("classroom", "v1", credentials=cr, cache_discovery=False)
        courses = svc.courses().list(courseStates=["ACTIVE"], pageSize=50)\
                     .execute().get("courses", [])
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:180]

    out = {"courses": [], "items": [], "announcements": [],
           "skipped": [], "stale_activity": []}
    for c in courses:
        cid, name = c["id"], c.get("name", "?")
        code = code_of(name)
        if code in STALE_COURSES:
            out["skipped"].append(code)
            # SELF-CORRECT: a stale course with new activity is no longer stale.
            hit = _has_activity_since(svc, cid, STALE_COURSES[code])
            if hit:
                out["stale_activity"].append({"course": code, "newest": hit})
            continue
        out["courses"].append({"id": cid, "name": name, "code": code})
        try:
            anns = svc.courses().announcements().list(
                courseId=cid, pageSize=10,
                orderBy="updateTime desc").execute().get("announcements", [])
        except Exception:
            anns = []
        for a in anns:
            out["announcements"].append({
                "course": code, "when": a.get("updateTime"),
                "text": " ".join((a.get("text") or "").split()),
                "materials": [t.get("title") for m in (a.get("materials") or [])
                              for t in [next(iter(m.values()))]
                              if isinstance(t, dict) and t.get("title")],
            })
        try:
            works = svc.courses().courseWork().list(
                courseId=cid, pageSize=30,
                orderBy="updateTime desc").execute().get("courseWork", [])
        except Exception:
            works = []
        for w in works:
            dt = due_dt(w)
            state = "?"
            try:
                subs = svc.courses().courseWork().studentSubmissions().list(
                    courseId=cid, courseWorkId=w["id"], userId="me")\
                    .execute().get("studentSubmissions", [])
                if subs:
                    state = subs[0].get("state", "?")
            except Exception:
                pass
            out["items"].append({
                "course": code, "title": w.get("title", "?"),
                "due": dt.isoformat() if dt else None,
                "state": state,
                "submitted": state in ("TURNED_IN", "RETURNED"),
                "link": w.get("alternateLink"),
            })
    return out, None


def main():
    svc = build("classroom", "v1", credentials=creds(), cache_discovery=False)
    now = datetime.now(MNL)
    try:
        courses = svc.courses().list(courseStates=["ACTIVE"], pageSize=50)\
                     .execute().get("courses", [])
    except HttpError as e:
        sys.exit(f"🔴 Classroom API refused the request: {e}\n"
                 "   If this says PERMISSION_DENIED, UB's Workspace may block third-party apps.")

    print(f"# Classroom (official API) — {now:%a %d %b %Y %I:%M %p} Manila")
    print(f"# {len(courses)} active course(s)\n")

    outstanding = []
    for c in courses:
        cid, name = c["id"], c.get("name", "?")
        print(f"── {name}")

        try:
            anns = svc.courses().announcements().list(
                courseId=cid, pageSize=10, orderBy="updateTime desc").execute().get("announcements", [])
        except HttpError:
            anns = []
        for a in anns[:5]:
            when = a.get("updateTime", "")[:10]
            body = " ".join((a.get("text") or "").split())
            print(f"   📣 {when}  {body[:400]}")
            for m in a.get("materials", []) or []:
                t = next(iter(m.values()))
                title = t.get("title") if isinstance(t, dict) else None
                if title:
                    print(f"      ⚠ ATTACHMENT — open it yourself: {title}")

        try:
            works = svc.courses().courseWork().list(
                courseId=cid, pageSize=30, orderBy="updateTime desc").execute().get("courseWork", [])
        except HttpError:
            works = []
        for w in works:
            dt = due_dt(w)
            # Submission state is per-item and is the only thing that says
            # "already handed in" -- never infer it from the due date.
            state = "?"
            try:
                subs = svc.courses().courseWork().studentSubmissions().list(
                    courseId=cid, courseWorkId=w["id"], userId="me").execute().get("studentSubmissions", [])
                if subs:
                    state = subs[0].get("state", "?")
            except HttpError:
                pass
            when = f"{dt:%a %d %b %I:%M %p}" if dt else "no due date"
            flag = ""
            if state in ("NEW", "CREATED", "RECLAIMED_BY_STUDENT"):
                flag = "  🔴 NOT SUBMITTED"
                outstanding.append((name, w.get("title"), dt, when))
            elif state in ("TURNED_IN", "RETURNED"):
                flag = "  ✅ " + state
            print(f"   📝 {w.get('title')}  — due {when}{flag}")
        print()

    print("=" * 60)
    if outstanding:
        print("🔴 OUTSTANDING:")
        for nm, title, dt, when in sorted(outstanding, key=lambda x: (x[2] is None, x[2] or now)):
            print(f"   • [{nm}] {title} — {when}")
    else:
        print("✅ Nothing outstanding in Google Classroom.")

    # The output contract: never let this read as full coverage.
    print(f"\n⚠ THIS IS CLASSROOM ONLY. Blackboard was NOT checked, so "
          f"{', '.join(BLIND)} are UNKNOWN —\n"
          "  not clear. Those three send no email either, so nothing else covers them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
