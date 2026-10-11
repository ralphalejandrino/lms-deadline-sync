#!/usr/bin/env python3
"""Offline tests for the aggregate scan -- no browser, no network, no session.

The assertion this suite exists for is the one that protects a grade:

    a scan that could not read something must NEVER be reportable as "clear".

Everything else here guards the two ways that promise gets broken quietly -- a
coverage cell that says OK when nothing was read, and a timestamp comparison
that drops brand-new material because two ISO formats were compared as strings.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scan import (NA, OK, SOURCES, UNKNOWN, WORKLIKE, _looks_related,  # noqa: E402
                  newer, print_matrix)

failed = 0
ran = 0


def check(name, ok, detail=""):
    global failed, ran
    ran += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        failed += 1
        if detail:
            print(f"        {detail}")


def row(**kw):
    d = {s: OK for s in SOURCES}
    d.update(kw)
    return d


def main():
    print("scan — coverage, watermark, candidate detection\n")

    # 1. 🔴 THE ONE THAT MATTERS. An UNKNOWN cell must be counted, because main()
    #    turns a non-zero count into a non-zero EXIT CODE, and that exit code is
    #    the whole difference between "nothing due" and "I could not see".
    bad = print_matrix({"HUMCOM1": row(), "INTRCS1": row(gradebook=UNKNOWN)})
    check("an UNKNOWN cell is counted", bad == 1, f"got {bad}")
    bad = print_matrix({"A": row(gradebook=UNKNOWN, contents=UNKNOWN)})
    check("every UNKNOWN cell is counted, not just the first", bad == 2, f"got {bad}")

    # A clean board must report zero -- otherwise the alarm fires always, and an
    # alarm that always fires is one he stops reading.
    check("a fully-read scan counts zero",
          print_matrix({"A": row(), "B": row()}) == 0)

    # n/a is NOT unknown. A source that does not apply to a course (ARTAPP1 has
    # no LMS shell at all) must not raise the alarm forever.
    check("n/a is not UNKNOWN", print_matrix({"A": row(chat=NA, facebook=NA)}) == 0)

    # 2. 🔴 THE WATERMARK BUG THIS FUNCTION EXISTS TO PREVENT.
    #    Blackboard stamps UTC with Z; the ledger writes Manila with +08:00.
    #    Compared as STRINGS, '2026-08-27T02:55:00.000Z' < '2026-08-27T22:40+08:00'
    #    is True -- so a 10:55 AM announcement posted AFTER the last scan would be
    #    silently judged old and dropped. That is exactly the NSTPRO1 miss.
    ann_utc = "2026-08-27T02:55:00.000Z"          # 10:55 AM Manila
    before_mnl = "2026-08-27T09:00:00+08:00"      # 9:00 AM Manila -- earlier
    after_mnl = "2026-08-27T22:40:00+08:00"       # 10:40 PM Manila -- later
    check("UTC-Z vs Manila-offset: newer is recognised as NEW",
          newer(ann_utc, before_mnl) is True)
    check("UTC-Z vs Manila-offset: older is recognised as OLD",
          newer(ann_utc, after_mnl) is False)
    check("string comparison would have got this WRONG (proves the bug is real)",
          (ann_utc > before_mnl) is False)     # the naive test disagrees with truth
    check("no watermark -> everything is new", newer(ann_utc, None) is True)

    # Unparseable dates must fail OPEN. A candidate shown twice is noise; a
    # candidate never shown is a missed deadline.
    check("an unparseable created date is treated as NEW", newer("not-a-date", after_mnl))
    check("an unparseable watermark is treated as NEW", newer(ann_utc, "garbage"))

    # 3. Candidate detection. The NSTPRO1 shape: an assessment link with no
    #    gradebook column whose title shares nothing with any known column.
    check("an assessment link is worklike",
          any(w in "resource/x-bb-asmt-test-link" for w in WORKLIKE))
    check("a plain file is NOT worklike",
          not any(w in "resource/x-bb-file" for w in WORKLIKE))
    check("a folder is NOT worklike",
          not any(w in "resource/x-bb-folder" for w in WORKLIKE))

    check("two shared tokens count as related",
          _looks_related("Unit 3: Practice Activities", "Unit 3 Practice Activities (Cont)"))
    check("an unrelated title is not matched",
          not _looks_related("Typing Assignment", "Empathy Map"))

    # 3. 🔴 A 403 COURSE MUST NEVER VANISH (2026-10-11). scan_blackboard used to
    #    `continue` past it, so a course an instructor CLOSED left no row in `cov`
    #    and the load still said complete. Closed faculty-evaluation shells are
    #    the one expected 403 and must NOT raise the alarm forever.
    import scan as _scan
    from bb_sweep import classify_unreadable

    def _fake(memb, expand_ok=True):
        class S:
            uid = "_u"

            def get(self, url):
                if url.endswith("/courses?limit=100"):
                    return {"results": [{"courseId": m[0]} for m in memb]}
                if url.endswith("/courses?limit=100&expand=course"):
                    if not expand_ok:
                        return {"_status": 500}
                    return {"results": [{"courseId": m[0], "course": {
                        "courseId": m[1], "availability": {"available": m[2]}}}
                        for m in memb]}
                for cid, full, avail in memb:
                    if url.endswith(f"/courses/{cid}"):
                        return {"_status": 403} if avail == "No" else {"courseId": full}
                return {"results": []}
        return S()

    OPEN = ("_1_1", "1STSEM_IAA1_HUMCOM1", "Yes")
    EVAL = ("_2_1", "1STSEM_IAA1_ARTAPP1_EVAL", "No")
    REAL = ("_3_1", "1STSEM_IAA1_PROGIT1", "No")

    def _run(memb, **kw):
        cov = {}
        _i, _c, notes, _courses = _scan.scan_blackboard(_fake(memb, **kw), cov, None)
        return cov, notes

    cov, notes = _run([OPEN, EVAL])
    check("a closed _EVAL shell adds NO unknown cell",
          print_matrix(cov) == 0 and "EVAL" not in cov, str(cov))
    check("...but is still NAMED in the notes, not dropped silently",
          any("faculty-evaluation" in n and "ARTAPP1" in n for n in notes), str(notes))
    cov, notes = _run([OPEN, EVAL, REAL])
    check("🔴 a closed REAL course is UNKNOWN in the matrix",
          cov.get("PROGIT1", {}).get("gradebook") == UNKNOWN and print_matrix(cov) == 3,
          str(cov))
    check("...and named with why in the notes",
          any("PROGIT1" in n and "closed by the instructor" in n for n in notes), str(notes))
    cov, notes = _run([OPEN, EVAL], expand_ok=False)
    check("a failed identity lookup fails toward UNKNOWN (even for the eval shell)",
          cov.get("_2_1", {}).get("gradebook") == UNKNOWN, str(cov))
    check("classify_unreadable makes no call for an empty list",
          classify_unreadable(lambda u: 1 / 0, "_u", []) == ([], []))

    # NEGATIVE CONTROL for 3: the pre-fix behaviour (skip the 403) must be
    # caught by the assertion above, or the assertion proves nothing.
    _orig = _scan.classify_unreadable
    _scan.classify_unreadable = lambda *a: ([], [])          # = the old `continue`
    try:
        cov, _n = _run([OPEN, EVAL, REAL])
    finally:
        _scan.classify_unreadable = _orig
    if print_matrix(cov) == 0:
        print("  PASS  negative control: the old silent skip leaves a closed course invisible")
    else:
        print("  FAIL  negative control: the old silent skip was not reproduced")
        globals()["failed"] += 1

    # NEGATIVE CONTROL: prove these assertions can fail. If a knowingly-broken
    # matrix still reports zero, nothing above is being checked.
    if print_matrix({"A": row(gradebook=UNKNOWN)}) == 0:
        print("  FAIL  negative control: an UNKNOWN board reported as clean")
        globals()["failed"] += 1
    else:
        print("  PASS  negative control: an UNKNOWN board is never clean")

    print(f"\n{'🔴 ' + str(failed) + ' FAILED' if failed else '✅ ALL PASS'}"
          f"  ({ran} assertions + 1 control)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
