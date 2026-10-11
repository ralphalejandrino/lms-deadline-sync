#!/usr/bin/env python3
"""Tests for school_config — the one home for shared constants (Nebula Phase 0).

Run: ./test_school_config.py

What can go wrong when 21 modules stop defining their own constants:
  1. a module quietly keeps (or regains) a private copy that drifts;
  2. the shared `course_code()` answers differently from the per-module `code_of`
     it replaced — the stem rule is the trap: Chat's `ART APP-IAA1` only maps to
     ARTAPP1 when bare stems are accepted, and the Classroom SCRAPE never did.
So every module is checked for IDENTITY with the shared object, and every caller's
code_of is checked against a frozen copy of its pre-refactor body.
"""
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import school_config as sc

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '🔴'} {name}" + (f"  — {detail}" if detail and not cond else ""))


# ---- frozen pre-refactor bodies (2026-10-11), the reference behaviour --------
OLD_CODES = ("PROGIT1", "HUMCOM1", "INTRCS1", "MATHMW1", "LITERA1",
             "ARTAPP1", "NSTPRO1", "PATHFT1", "CMPTRB1", "SOCORN2")


def old_stem(text):                      # chat_api / classroom_api
    up = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    for c in OLD_CODES:
        if c in up or c.rstrip("1") in up:
            return c
    return None


def old_strict(text):                    # classroom_scrape
    up = (text or "").upper().replace(" ", "")
    for c in OLD_CODES:
        if c in up:
            return c
    return None


# Real names: every Chat space seen on 2026-10-11, Classroom title shapes from the
# module docstrings, plus edge cases.
CORPUS = ["2627A IAA1_SOCORN2", "BSCS 2627A", "PROGIT1 IAA1 2627A", "HUMCOM1_IAA1-2627A",
          "IAA1_LITERA1_2627A", "ART APP-IAA1", "2627A IAA1_CMPTRB1", "INTRCS1-IAA1",
          "Intramurals Artist", "2627_IAA1-INTRCS1", "1 - IAA1 - NSTPRO1", "MATHMW1 IAA1",
          "PATHFT1", "pathft1 lower", "", None, "NSTP RO1", "nothing here"]

print("# school_config — values unchanged")
check("MNL is UTC+8", sc.MNL.utcoffset(None) == timedelta(hours=8))
check("PROFILE path unchanged",
      sc.PROFILE == Path.home() / ".local/share/ub-blackboard-profile")
check("BASE unchanged", sc.BASE == "https://blackboard.ubaguio.edu/learn/api/public/v1")
check("SCHOOL_ACCOUNT unchanged", sc.SCHOOL_ACCOUNT == "student@s.ubaguio.edu")
check("CODES unchanged", sc.CODES == OLD_CODES)
check("SCHOOL is a path (LMS_STATE_DIR or two levels up)", hasattr(sc.SCHOOL, "name"))

print("\n# every module uses THE shared object, not a private copy")
USES = {
    "MNL": ["bb_sweep", "bb_relay", "due_check", "lms_digest", "classroom_api",
            "study_load", "sync_health", "warm_session", "fb_fetch", "scan"],
    "PROFILE": ["bb_check", "bb_cookies", "chat_fetch", "due_check", "classroom_scrape",
                "lms_digest", "sso_login", "bb_session", "cal_sync", "phone_login",
                "mail_fetch", "warm_session"],
    "BASE": ["bb_fetch", "bb_sweep", "due_check", "lms_digest", "bb_session"],
    "SCHOOL_ACCOUNT": ["classroom_scrape", "mail_fetch", "mail_send"],
    "CODES": ["chat_api", "classroom_scrape", "classroom_api", "recolor_classes"],
    "SCHOOL": ["study_load", "scan"],
}
for name, mods in USES.items():
    for m in mods:
        try:
            mod = __import__(m)
            check(f"{m}.{name} is school_config.{name}", getattr(mod, name) is getattr(sc, name))
        except Exception as e:                     # an import failure is a FAIL, not a skip
            check(f"{m} imports", False, f"{type(e).__name__}: {e}")

print("\n# code_of behaviour is byte-for-byte the pre-refactor behaviour")
import chat_api, classroom_api, classroom_scrape
for t in CORPUS:
    check(f"chat_api.code_of({t!r})", chat_api.code_of(t) == old_stem(t),
          f"{chat_api.code_of(t)} != {old_stem(t)}")
    check(f"classroom_api.code_of({t!r})", classroom_api.code_of(t) == old_stem(t))
    check(f"classroom_scrape.code_of({t!r})", classroom_scrape.code_of(t) == old_strict(t),
          f"{classroom_scrape.code_of(t)} != {old_strict(t)}")

print("\n# negative control: the stem rule is load-bearing and the two modes differ")
check("🔴 'ART APP-IAA1' → ARTAPP1 with stems", sc.course_code("ART APP-IAA1", stem=True) == "ARTAPP1")
check("🔴 'ART APP-IAA1' → None without stems", sc.course_code("ART APP-IAA1", stem=False) is None)
check("program-wide space maps to no course", sc.course_code("BSCS 2627A") is None)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
