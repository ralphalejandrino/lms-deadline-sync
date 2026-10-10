#!/usr/bin/env python3
"""Offline tests for the actionable-mail filter -- no browser, no network.

Ralph chose an **actionable-only** digest and is relying on this filter. That
makes a wrong call expensive in one direction and merely noisy in the other:
a false NEGATIVE hides real work, a false POSITIVE just costs a line. So the
suite is deliberately lopsided -- most of it guards against dropping something.

Every case below is a REAL row from the live inbox on 2026-08-29, including the
two false positives and three false negatives the first version actually
shipped. They are here so they cannot come back.

Run: ./lms.sh test, or python3 test_mail_filter.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mail_fetch import classify  # noqa: E402

failed = 0
ran = 0

ZW = "‌"
PAD = (ZW + " ") * 8          # a bulk template's preheader padding


def check(name, ok, detail=""):
    global failed, ran
    ran += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        failed += 1
        if detail:
            print(f"        {detail}")


def want(actionable, sender, subject, snippet="", label=None):
    got, why = classify(sender, subject, snippet)
    check(label or f"{'ACT ' if actionable else 'SUP '} {subject[:52]}",
          got is actionable, f"got actionable={got} why={why!r}")


def main():
    print("== must be ACTIONABLE (a miss here hides real work) ==")
    want(True, "INSTRUCTOR C", "INTRCS1 Computer Laboratory - August 20",
         "Dear Students, Due to the...")
    want(True, "Instructor E", 'New assignment: "Natures Pattern Hunt"')
    want(True, "Instructor D (Classroom)", 'New assignment: "Quiz 1"',
         "please refer to the attached quiz. You are required to submit")
    want(True, "instructor b", "Score released: Quiz1-HUMCOM11_IAA1-Lec-Lesson1.2",
         "Your score has been released")
    want(True, "UB Student Announcements",
         "ADVISORY ON LENIENCY FOR LMS UNFAMILIARITY, LATE ENROLLMENT")
    want(True, "INSTRUCTOR F", "IAA1 - PATHFIT1_TUESDAY: August 25 - Venue",
         "New announcement from INSTRUCTOR F")

    print("\n-- the three the first ruleset MISSED (2026-08-29 live run) --")
    want(True, "Instructor A", "Re: PROGIT1 Input Exer",
         label="reply thread from the PROGIT1 instructor")
    want(True, "HUMAN COMPUTER INTERACTION",
         "Instructor B sent you a new message in HUMAN COMPUTER INTERACTION",
         label="instructor direct message")
    want(True, "UB Student Announcements",
         "COMPLETION OF STUDENT INFORMATION SHEET",
         label="'completion of' — a form he must fill in")

    print("\n== must be SUPPRESSED ==")
    print("-- the two false positives the first ruleset SHIPPED --")
    want(False, "Alec Lynch", "Hi. Welcome to Design.com",
         "Just a few steps to get a stunning logo for your business or project." + PAD,
         label="marketing mail that tripped 'project'")
    want(False, "Forms Response Receipts",
         "Thanks for filling out this form: Guidelines for Scholarships",
         "Here's what was received.",
         label="Forms receipt that tripped 'application'")

    print("-- ordinary noise --")
    want(False, "do-not-reply", "Submission received",
         "Assignment submitted Typing Assignment")
    want(False, "Guillermo Conde", "Our #1 request RALPH MIGUEL",
         "I have something to ask of you." + PAD)
    want(False, "Sololearn", "Is your name correct for your certificate, RALPH?",
         "Confirm now" + PAD)

    print("\n== the suppressors must not over-reach ==")
    # A school sender using a template must STILL be read. The marketing rule is
    # scoped by sender for exactly this reason -- Classroom and Blackboard both
    # send templated HTML, and suppressing them would be catastrophic.
    want(True, "Instructor D (Classroom)", 'New assignment: "Quiz 1"',
         "You are required to submit this" + PAD,
         label="Classroom template WITH padding is still read")
    want(True, "no-reply@blackboard.ubaguio.edu",
         "Assignment due tomorrow: Unit 3 Practice",
         "This is due on 30 August",
         label="automated sender WITH a deadline is still read")
    want(True, "notifications@classroom.google.com",
         "New assignment posted", "please submit by Friday",
         label="automated sender WITH graded-work wording is still read")

    print("\n== plurals ==")
    # `\b(scholarship)\b` does not match "Scholarships" -- the trailing \b fails
    # on the 's'. Found 2026-08-29 while mutation-testing this very suite: a
    # mutant that should have failed passed, because the row it used was being
    # missed for an unrelated reason. Every keyword rule now ends `s?\b`.
    want(True, "UB Student Announcements",
         "APPLICATION FOR SCHOLARSHIPS AND DISCOUNTS",
         label="plural 'Scholarships' still matches")
    want(True, "Registrar", "Submission of requirements",
         label="plural 'requirements' still matches")
    want(True, "instructor", "New assignments posted for this week",
         label="plural 'assignments' still matches")

    print("\n== a reason is always given, either way ==")
    ok = True
    for s, subj, sn in (("x", "New assignment: Quiz 1", ""),
                        ("do-not-reply", "Submission received", ""),
                        ("y", "totally unrelated", "")):
        _, why = classify(s, subj, sn)
        if not why or len(why) < 4:
            ok = False
    check("every verdict carries an auditable reason string", ok)

    print(f"\n{ran - failed}/{ran} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
