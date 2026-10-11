"""The ONE home for constants the LMS toolchain used to define module by module.

Before 2026-10-11 (Nebula Phase 0) `MNL` was defined in 10 modules, the Blackboard
profile path in 12, the course-code tuple in 4 and the school account in 3. Edit
them here; every module imports these names.

Deliberately NOT here: the per-module time formatters (`mnl()` in scan, bb_sweep,
lms_digest; `to_mnl` in due_check). They look alike but their empty-value and
format defaults differ, and callers depend on those differences.
"""
import os
import re
from datetime import timedelta, timezone
from pathlib import Path

# Manila time. Every due date and brief is read in it.
MNL = timezone(timedelta(hours=8))

# The persistent Chromium profile that holds the school Google + Blackboard session.
PROFILE = Path.home() / ".local/share/ub-blackboard-profile"

# Blackboard public REST root.
BB_HOST = "https://blackboard.ubaguio.edu"
BASE = f"{BB_HOST}/learn/api/public/v1"

# The school Google account (Classroom, Chat, school mail, sending).
SCHOOL_ACCOUNT = os.environ.get("LMS_SCHOOL_ACCOUNT", "student@s.ubaguio.edu")

# vault/school — this file lives at vault/school/assets/lms/.
SCHOOL = Path(os.environ.get("LMS_STATE_DIR", Path(__file__).resolve().parents[2]))

# Section IAA1's ten subjects, from school/schedule.md.
CODES = ("PROGIT1", "HUMCOM1", "INTRCS1", "MATHMW1", "LITERA1",
         "ARTAPP1", "NSTPRO1", "PATHFT1", "CMPTRB1", "SOCORN2")


def course_code(text, stem=True):
    """Course code named in a free-text title, or None.

    stem=True  — strip everything but A-Z0-9 and also accept the bare stem, because
                 a Chat space called `ART APP-IAA1` collapses to `ARTAPPIAA1` and never
                 contains `ARTAPP1` (chat_api, classroom_api).
    stem=False — only drop spaces and require the full code (classroom_scrape).
    """
    if stem:
        up = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
        for c in CODES:
            if c in up or c.rstrip("1") in up:
                return c
        return None
    up = (text or "").upper().replace(" ", "")
    for c in CODES:
        if c in up:
            return c
    return None
