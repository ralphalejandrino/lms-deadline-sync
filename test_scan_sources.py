#!/usr/bin/env python3
"""Offline tests for the Chat and Classroom sources -- no browser, no network.

## What these guard (added 2026-08-29)

Until today `scan.py` DECLARED `chat` and `classroom` as sources and read
neither. Both functions returned a note telling the reader to run a separate
command, and both coverage cells defaulted to `n/a` -- which `print_matrix` does
not count as a gap. The result was a scan that could print

    ✅ Every source reported for every course. A clean board here is real.

having never opened Google Chat, over PROGIT1 and ARTAPP1, the two subjects that
announce ONLY there.

So the assertion under test is not "does it read Chat" -- a live run shows that.
It is the harder one: **when a source cannot be read, does the cell actually go
UNKNOWN, and does the run therefore stop being reportable as clear?** A source
wired in without that property is worse than one left out, because it looks like
coverage.

Every test stubs the fetcher, so a dead session is simulated rather than waited
for. Run: ./lms.sh test, or python3 test_scan_sources.py
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import scan  # noqa: E402
from scan import NA, OK, SOURCES, UNKNOWN, print_matrix  # noqa: E402

failed = 0
ran = 0

COURSES = ("PROGIT1", "ARTAPP1", "HUMCOM1", "CMPTRB1", "INTRCS1",
           "SOCORN2", "LITERA1", "MATHMW1", "NSTPRO1", "PATHFT1")

SPACE_OF = {
    "PROGIT1 IAA1 2627A": "PROGIT1",
    "ART APP-IAA1":       "ARTAPP1",
    "HUMCOM1_IAA1-2627A": "HUMCOM1",
    "2627A IAA1_CMPTRB1": "CMPTRB1",
    "INTRCS1-IAA1":       "INTRCS1",
    "2627A IAA1_SOCORN2": "SOCORN2",
}


def check(name, ok, detail=""):
    global failed, ran
    ran += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        failed += 1
        if detail:
            print(f"        {detail}")


def fresh_cov():
    return {c: {s: NA for s in SOURCES} for c in COURSES}


def stub_chat(results=None, failed_spaces=(), dead=None, api=True):
    """Install fake Chat modules.

    api=True  -> chat_api is present and used (the preferred path)
    api=False -> chat_api reports no token, so scan_chat falls back to the
                 browser scrape. Both paths must satisfy the same guarantees,
                 which is the point of testing them with one helper.
    """
    a = types.ModuleType("chat_api")
    a.available = lambda: api
    # scan_chat derives failed spaces' course codes from the NAME via this, so a
    # stub without it silently falls through to the scrape path and the test
    # quietly measures the wrong branch.
    a.code_of = SPACE_OF.get
    a.fetch = lambda *ar, **k: (
        {n: dict(v, code=SPACE_OF.get(n)) for n, v in (results or {}).items()},
        list(failed_spaces), dead)
    sys.modules["chat_api"] = a

    m = types.ModuleType("chat_fetch")
    m.COURSE_OF = dict(SPACE_OF)
    m.SPACES = {k: None for k in SPACE_OF}
    m.fetch = lambda *ar, **k: (results or {}, list(failed_spaces), dead)
    sys.modules["chat_fetch"] = m
    return m


def stub_classroom_api(items=(), announcements=(), courses=("MATHMW1","NSTPRO1","INTRCS1"),
                       err=None, api=True):
    a = types.ModuleType("classroom_api")
    a.CODES = ("PROGIT1","HUMCOM1","INTRCS1","MATHMW1","LITERA1",
               "ARTAPP1","NSTPRO1","PATHFT1","CMPTRB1","SOCORN2")
    a.BLIND = ["PROGIT1", "SOCORN2", "CMPTRB1"]
    a.available = lambda: api
    a.fetch = lambda: (None, err) if err else ({
        "courses": [{"id": f"id{c}", "name": c, "code": c} for c in courses],
        "items": list(items), "announcements": list(announcements)}, None)
    sys.modules["classroom_api"] = a
    return a


def stub_classroom(courses=(), streams=None, err=None):
    # Default the API OFF so these cases exercise the scrape fallback; the API
    # path has its own block below.
    stub_classroom_api(api=False)
    m = types.ModuleType("classroom_scrape")
    m.CLASSROOM_COURSES = ("MATHMW1", "NSTPRO1", "INTRCS1")
    data = {
        "account": "student@s.ubaguio.edu",
        "courses": [{"id": f"id{c}", "title": c, "code": c} for c in courses],
        "todo": "",
        "streams": streams if streams is not None
        else {f"id{c}": "nothing of note" for c in courses},
    }
    m.fetch = lambda *a, **k: (data, err)
    sys.modules["classroom_scrape"] = m
    return m


def main():
    print("== Chat ==")

    # --- the headline regression: a dead session must not read as silence ---
    stub_chat(dead="not signed in (bounced to accounts.google.com)")
    cov = fresh_cov()
    cands, note = scan.scan_chat(cov, None)
    # EVERY course, not just the ones believed to have a space: the belief about
    # which courses have spaces came from a hardcoded list that turned out to be
    # wrong (it omitted LITERA1), and a dead source cannot confirm it either way.
    not_unknown = [c for c in COURSES if cov[c]["chat"] != UNKNOWN]
    check("dead session -> UNKNOWN for EVERY course, not just known spaces",
          not not_unknown, f"still not UNKNOWN: {not_unknown}")
    check("dead session -> the two chat-only subjects are UNKNOWN",
          cov["PROGIT1"]["chat"] == UNKNOWN and cov["ARTAPP1"]["chat"] == UNKNOWN)
    check("dead session -> no candidates invented", cands == [])
    check("dead session -> a note is emitted", bool(note) and "DEAD" in note)
    check("dead session -> print_matrix counts it as a gap (run is NOT clean)",
          print_matrix_bad(cov) >= 6)

    # --- one space unreadable: that course UNKNOWN, the rest still real ------
    stub_chat(results={n: {"text": "hi", "new": None}
                       for n in SPACE_OF if n != "ART APP-IAA1"},
              failed_spaces=["ART APP-IAA1"])
    cov = fresh_cov()
    scan.scan_chat(cov, None)
    check("one failed space -> only that course is UNKNOWN",
          cov["ARTAPP1"]["chat"] == UNKNOWN and cov["PROGIT1"]["chat"] == OK,
          f"ARTAPP1={cov['ARTAPP1']['chat']} PROGIT1={cov['PROGIT1']['chat']}")
    check("courses with NO space stay n/a, never OK and never UNKNOWN",
          all(cov[c]["chat"] == NA
              for c in ("LITERA1", "MATHMW1", "NSTPRO1", "PATHFT1")))

    # --- a healthy read must be able to reach OK (the positive control) ------
    stub_chat(results={n: {"text": "hi", "new": None} for n in SPACE_OF})
    cov = fresh_cov()
    _, note = scan.scan_chat(cov, None)
    check("all spaces read -> OK for all six, and no note",
          all(cov[c]["chat"] == OK for c in SPACE_OF.values()) and note is None)

    # --- new material carrying an action word becomes a candidate -----------
    stub_chat(results={"PROGIT1 IAA1 2627A":
                       {"text": "x", "new": "Unit 4 activity is due Monday"}})
    cov = fresh_cov()
    cands, _ = scan.scan_chat(cov, None)
    check("action word in NEW chat text -> candidate raised",
          len(cands) == 1 and cands[0]["course"] == "PROGIT1",
          f"got {cands}")

    stub_chat(results={"PROGIT1 IAA1 2627A": {"text": "x", "new": None}})
    cov = fresh_cov()
    cands, _ = scan.scan_chat(cov, None)
    check("NOTHING new -> no candidate (watermark is respected)", cands == [])

    # --- 2026-09-14: a delta is not a read — recent exam messages from the SPACE --
    from datetime import datetime, timedelta, timezone
    iso = lambda days: (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    moved = (f"[{iso(5)}] Examination Schedule - Same Lecture/Lab Room (Next Week)\n"
             f"LEC-Monday 11-12 noon / LAB-Thursday 9:30 - 11 am")
    stub_chat(results={"INTRCS1-IAA1": {"text": moved, "new": None}})   # already SEEN
    cov = fresh_cov()
    cands, _ = scan.scan_chat(cov, None)
    recent = [c for c in cands if c["kind"] == "chat-recent"]
    check("🔴 an already-seen exam message from 5 days ago STILL surfaces (the INTRCS1 miss)",
          len(recent) == 1 and recent[0]["course"] == "INTRCS1", f"got {cands}")
    check("…with its continuation line kept in the quote",
          bool(recent) and "LAB-Thursday" in recent[0]["quote"], f"got {recent}")
    check("…and marked as not new", bool(recent) and recent[0]["new"] is False)

    stub_chat(results={"INTRCS1-IAA1": {"text": f"[{iso(20)}] exam moved to Friday", "new": None}})
    cov = fresh_cov()
    cands, _ = scan.scan_chat(cov, None)
    check("older than 14 days -> not surfaced", cands == [], f"got {cands}")

    stub_chat(results={"INTRCS1-IAA1": {"text": f"[{iso(1)}] see you all tomorrow", "new": None}})
    cov = fresh_cov()
    cands, _ = scan.scan_chat(cov, None)
    check("recent but not exam-shaped -> not surfaced", cands == [], f"got {cands}")

    import tempfile
    real_inbox = scan.INBOX
    scan.INBOX = Path(tempfile.mkdtemp()) / "inbox.md"
    added = scan.append_candidates([{"course": "INTRCS1", "kind": "chat-recent", "title": "t",
                                     "when": None, "why": "w", "quote": "q"}])
    check("chat-recent is NEVER written to the inbox (it recurs by design)",
          added == 0 and not scan.INBOX.exists(), f"added={added}")
    scan.INBOX = real_inbox

    # --- the browser-scrape fallback must honour the SAME contract -----------
    stub_chat(dead="not signed in", api=False)
    cov = fresh_cov()
    cands, note = scan.scan_chat(cov, None)
    check("FALLBACK: dead scrape -> UNKNOWN for every course with a space",
          all(cov[c]["chat"] == UNKNOWN for c in SPACE_OF.values()))
    check("FALLBACK: says it used the scrape, so a thin space list is visible",
          "SCRAPE" in (note or "").upper(), f"note={note}")

    stub_chat(results={n: {"text": "hi", "new": None} for n in SPACE_OF}, api=False)
    cov = fresh_cov()
    scan.scan_chat(cov, None)
    check("FALLBACK: healthy scrape still reaches OK",
          all(cov[c]["chat"] == OK for c in SPACE_OF.values()))

    print("\n== Classroom ==")

    # --- unreadable Classroom must not be silence either --------------------
    stub_classroom(err="Classroom session dead — bounced to sign-in")
    cov = fresh_cov()
    cands, note = scan.scan_classroom(cov, None)
    check("fetch error -> UNKNOWN for all three Classroom subjects",
          all(cov[c]["classroom"] == UNKNOWN
              for c in ("MATHMW1", "NSTPRO1", "INTRCS1")))
    check("fetch error -> no candidates invented", cands == [])
    check("fetch error -> run is NOT clean", print_matrix_bad(cov) >= 3)

    # --- the wrong-account refusal reaches the matrix ------------------------
    stub_classroom(err="WRONG ACCOUNT: Classroom is showing someone@else.com")
    cov = fresh_cov()
    _, note = scan.scan_classroom(cov, None)
    check("wrong account -> UNKNOWN, and the note says so",
          cov["MATHMW1"]["classroom"] == UNKNOWN and "WRONG ACCOUNT" in (note or ""))

    # --- a course that should be there and is not ---------------------------
    stub_classroom(courses=("MATHMW1", "NSTPRO1"))
    cov = fresh_cov()
    _, note = scan.scan_classroom(cov, None)
    check("expected-but-absent course -> UNKNOWN, not omitted",
          cov["INTRCS1"]["classroom"] == UNKNOWN and "INTRCS1" in (note or ""),
          f"INTRCS1={cov['INTRCS1']['classroom']} note={note}")
    check("the courses that WERE read are OK",
          cov["MATHMW1"]["classroom"] == OK and cov["NSTPRO1"]["classroom"] == OK)

    # --- an unreadable stream downgrades that course ------------------------
    stub_classroom(courses=("MATHMW1", "NSTPRO1", "INTRCS1"),
                   streams={"idMATHMW1": None, "idNSTPRO1": "ok", "idINTRCS1": "ok"})
    cov = fresh_cov()
    scan.scan_classroom(cov, None)
    check("unreadable stream -> that course UNKNOWN even though the tile listed it",
          cov["MATHMW1"]["classroom"] == UNKNOWN
          and cov["NSTPRO1"]["classroom"] == OK)

    # --- positive control ---------------------------------------------------
    stub_classroom(courses=("MATHMW1", "NSTPRO1", "INTRCS1"))
    cov = fresh_cov()
    scan.scan_classroom(cov, None)
    check("all three read -> OK for all three (a clean cell is reachable)",
          all(cov[c]["classroom"] == OK
              for c in ("MATHMW1", "NSTPRO1", "INTRCS1")))
    check("Blackboard-only courses stay n/a for classroom",
          cov["PROGIT1"]["classroom"] == NA)

    print("\n== Classroom via the OFFICIAL API (preferred path) ==")

    stub_classroom_api(items=[
        {"course": "MATHMW1", "title": "MATH IN MY FIELD", "due": "2026-08-18T23:59:00+08:00",
         "state": "CREATED", "submitted": False, "link": "http://x"},
        {"course": "NSTPRO1", "title": "Quiz 1", "due": None,
         "state": "TURNED_IN", "submitted": True, "link": "http://y"},
    ])
    cov = fresh_cov()
    cands, note = scan.scan_classroom(cov, None)
    check("API path is used when a token exists (note says so)",
          "OFFICIAL API" in (note or ""), f"note={note}")
    check("API: the three Classroom subjects are OK",
          all(cov[c]["classroom"] == OK for c in ("MATHMW1","NSTPRO1","INTRCS1")))
    check("API: an UNSUBMITTED item becomes a candidate",
          any(c["title"] == "MATH IN MY FIELD" for c in cands), f"got {cands}")
    check("API: a TURNED_IN item does NOT",
          not any(c["title"] == "Quiz 1" for c in cands))
    check("API: submission state comes from Google, and is quoted in the reason",
          any("NOT SUBMITTED" in c["why"] for c in cands))
    check("API: Blackboard-only subjects stay n/a, and the note names them",
          cov["PROGIT1"]["classroom"] == NA and "PROGIT1" in (note or ""))

    # An API error must fall through to the scrape rather than silently pass.
    stub_classroom_api(err="PERMISSION_DENIED")
    stub_classroom(err="scrape also dead")
    stub_classroom_api(err="PERMISSION_DENIED")   # stub_classroom reset it
    cov = fresh_cov()
    cands, note = scan.scan_classroom(cov, None)
    check("API error -> falls back, and BOTH failures end as UNKNOWN",
          all(cov[c]["classroom"] == UNKNOWN
              for c in ("MATHMW1","NSTPRO1","INTRCS1")) and cands == [],
          f"note={note}")

    print(f"\n{ran - failed}/{ran} passed")
    return 1 if failed else 0


def print_matrix_bad(cov):
    """print_matrix's gap count, without its output cluttering the test log."""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return print_matrix(cov)


if __name__ == "__main__":
    sys.exit(main())
