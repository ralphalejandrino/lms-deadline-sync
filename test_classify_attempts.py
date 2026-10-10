#!/usr/bin/env python3
"""Regression test for the false-✅ bug.

2026-08-17: the digest printed "✅ NeedsGrading" for a 30-point one-shot test
whose attempt was still InProgress and unsent, ~3h from the deadline.
2026-08-18: it did it again for `Unit 3: Practice Activities`, 50 min before
close. Root cause: it read the gradebook COLUMN, which flips to NeedsGrading as
soon as an attempt EXISTS -- submitted or not.

Run:  ./lms.sh test     (or: python test_classify_attempts.py)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lms_digest import classify_attempts

CASES = [
    # (attempt states,                    expect_submitted, must appear in label)
    ([],                                  False, "no attempt"),
    (["InProgress"],                      False, "NOT SUBMITTED"),   # THE BUG
    (["Draft"],                           False, "NOT SUBMITTED"),
    (["InProgress", "InProgress"],        False, "NOT SUBMITTED"),
    (["NeedsGrading"],                    True,  "NeedsGrading"),
    (["Completed"],                       True,  "Completed"),
    (["Graded"],                          True,  "Graded"),
    (["NeedsGrading", "NeedsGrading"],    True,  "NeedsGrading"),
    # a resubmission in progress on top of a real submission still counts as in
    (["NeedsGrading", "InProgress"],      True,  "NeedsGrading"),
    (None,                                False, "no attempt"),
    ([None],                              False, "no attempt"),
]

def main():
    failed = 0
    for states, want_sub, want_in in CASES:
        got_sub, label = classify_attempts(states)
        ok = (got_sub == want_sub) and (want_in in label)
        if not ok:
            failed += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {str(states):34} -> "
              f"submitted={got_sub!s:5} label={label!r}")
        if not ok:
            print(f"        expected submitted={want_sub} and {want_in!r} in label")

    # NEGATIVE CONTROL: the test must be capable of failing. If a deliberately
    # wrong expectation still 'passes', the assertions are not being checked.
    bogus_sub, _ = classify_attempts(["InProgress"])
    if bogus_sub is True:
        print("  FAIL  negative control: InProgress reported as submitted")
        failed += 1
    else:
        print("  PASS  negative control: InProgress is never 'submitted'")

    # --- the grade record as a second witness (2026-09-14) -------------------
    # Shapes measured live from …/columns/<col>/users/<uid>.
    from lms_digest import classify_grade
    GRADE_CASES = [
        # (attempts, grade record,                              submitted, in label)
        ([], {"status": "Graded", "score": 16.0},               True,  "Graded 16"),  # PROGIT1 FG Quiz 1
        ([], {"userId": "_1", "columnId": "_9007_1"},          False, "no attempt"), # MATHMW1 Number Pattern
        ([], {"status": "NeedsGrading"},                        False, "no attempt"), # the flip proves nothing
        ([], {"status": "Completed"},                           True,  "Completed"),
        ([], {"score": 0.0},                                    True,  "0"),          # a zero is a mark
        ([], {"_status": 403},                                  False, "no attempt"), # unreadable != done
        ([], None,                                              False, "no attempt"),
        ([], {"score": True},                                   False, "no attempt"), # bool is not a score
        (["InProgress"], {"status": "NeedsGrading"},            False, "NOT SUBMITTED"),
        (["NeedsGrading"], {},                                  True,  "NeedsGrading"),
    ]
    for states, grade, want_sub, want_in in GRADE_CASES:
        got_sub, label = classify_grade(states, grade)
        ok = (got_sub == want_sub) and (want_in in label)
        failed += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  grade {str(grade)[:40]:42} -> "
              f"submitted={got_sub!s:5} label={label!r}")

    print(f"\n{'🔴 ' + str(failed) + ' FAILED' if failed else '✅ ALL PASS'}"
          f"  ({len(CASES)} cases + 1 control)")
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
