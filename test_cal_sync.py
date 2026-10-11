#!/usr/bin/env python3
"""Offline tests for the calendar reconciler's decision layer -- no browser, no network.

diff() is deliberately pure so every decision that touches Ralph's calendar can be
proven here. Guards the 2026-08-19 failures: a retitled Blackboard item silently
losing its calendar link, and a drifted due date never being corrected.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cal_sync import (diff, COLOR_LIVE, COLOR_DONE, COLOR_WORK, COLOR_CLASS,
                      COLOR_DUE, COLOR_OVERDUE, COLOR_WORK_DUE, COLOR_WORK_DONE,
                      is_work_event, human_done, window_guard, witnessed,
                      next_meeting, satisfied_twins, fmt)

CID, COL = "_9003_1", "_9004_1"
MK = f"bb:{CID}:{COL}"

# 🔴 THESE MUST BE RELATIVE TO NOW, NOT HARDCODED (fixed 2026-08-22).
#
# They used to be the literal strings "2026-08-19T10:00:00.000Z" and
# "...T15:59:00.000Z" -- the real due dates of the day the suite was written.
# But diff() deliberately drops any item whose deadline is more than 12 hours
# past, because re-timing history serves nothing. So from 2026-08-20 onward
# EVERY fixture was filtered out before a single decision was made, diff()
# returned [] for everything, and 18 of 34 assertions failed -- including the
# negative control.
#
# The suite therefore passed on exactly one day and rotted silently on the next,
# which left the ONLY guard on the calendar reconciler red and unnoticed for
# three days. A test whose fixtures expire is worse than no test: it fails for a
# reason that has nothing to do with the code, so the red gets normalised.
#
# Anchor on TOMORROW in Manila so the fixtures are always inside the window, and
# keep both times on the SAME Manila day -- "adoption requires the same day"
# depends on that, and a naive "now + 1 day" would straddle midnight.
MNL = timezone(timedelta(hours=8))
_D = (datetime.now(MNL) + timedelta(days=1)).date()
DUE_6PM = f"{_D}T10:00:00.000Z"        # 6:00 PM Manila, tomorrow
DUE_1159 = f"{_D}T15:59:00.000Z"       # 11:59 PM Manila, same day


def item(**kw):
    d = {"cid": CID, "colid": COL, "code": "HUMCOM1", "name": "2.14 Version Control",
         "due": DUE_6PM, "pts": 40.0, "submitted": False, "states": [], "warn": None}
    d.update(kw)
    return d


def ev(**kw):
    d = {"id": "evt1", "summary": "📌 DUE: HUMCOM1 — 2.14 Version Control (40 pts)",
         "description": f"stuff\n{MK}\n", "start": DUE_6PM, "end": DUE_6PM,
         "colorId": COLOR_LIVE}
    d.update(kw)
    return d


def main() -> int:
    failed = 0

    ran = 0

    def check(name, cond, detail=""):
        nonlocal failed, ran
        ran += 1
        if not cond:
            failed += 1
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond and detail:
            print(f"        {detail}")

    def acts(items, events):
        return diff(items, events)

    # 1. In sync -> silence.
    check("matching item+event -> no actions", acts([item()], [ev()]) == [])

    # 2. THE BUG: calendar says 11:59 PM, Blackboard says 6:00 PM.
    a = acts([item()], [ev(start=DUE_1159, end=DUE_1159)])
    check("drifted time -> UPDATE", len(a) == 1 and a[0]["action"] == "UPDATE", str(a))
    check("UPDATE carries the true time", a and a[0]["end"] == DUE_6PM)
    check("UPDATE names the change", a and "end" in a[0]["changes"])

    # 2b. A WORK BLOCK that starts early and ENDS at the deadline is CORRECT and
    #     must be left alone. Comparing start-to-due would drag every block onto
    #     its own deadline and destroy the run-up time. (Live-run regression.)
    block = ev(start="2026-08-19T09:00:00.000Z", end=DUE_6PM)   # 5-6 PM Manila
    check("block ending at the deadline -> no action", acts([item()], [block]) == [],
          str(acts([item()], [block])))

    # 2c. A hand-written title must NOT be overwritten with the raw Blackboard
    #     name. Only the 📌/✅ prefix is contractual. (Live-run regression.)
    human = ev(summary="🔴 DUE 6:00 PM: HUMCOM1 — 2.14 Version Control · 1 ATTEMPT ONLY")
    check("hand-written title is preserved", acts([item()], [human]) == [],
          str(acts([item()], [human])))
    a = acts([item(submitted=True, states=["NeedsGrading"])], [human])
    check("submitted -> prefix swapped, title text KEPT",
          a and "1 ATTEMPT ONLY" in a[0]["summary"] and a[0]["summary"].startswith("✅"),
          str(a))

    # 2d. Sub-minute difference is NOT drift. Blackboard stores 11:59:59.999 PM
    #     where the calendar says 11:59 PM. (Live-run regression.)
    near = item(due="2026-08-19T09:59:59.999Z")   # 5:59:59.999 PM vs a 6:00 PM event
    _near = acts([near], [ev()])
    # Asserted as "no END change", not "no actions at all". This fixture's due
    # date is in the PAST, so since 2026-08-27 it is correctly reclassified as
    # OVERDUE and does pick up a prefix + colour change. The regression this
    # case exists to guard is the TIME one, and that must still be silent.
    check("sub-minute difference is not drift",
          all("end" not in a.get("changes", []) for a in _near), str(_near))

    # 2e. OVERDUE WORK MUST REACH THE CALENDAR. Until 2026-08-27 diff() dropped
    #     every item more than 12 h past its stamp ("past deadlines are HISTORY"),
    #     which made the two items MOST at risk invisible: a 100-pt MATHMW1 column
    #     and a 10-pt PROGIT1 column, both unstarted and days late, had to be put
    #     on the calendar by hand. Unstarted overdue work is not history.
    _past = f"{(datetime.now(MNL) - timedelta(days=3)).date()}T10:00:00.000Z"
    od = item(due=_past)
    a = acts([od], [])
    check("overdue + no event -> CREATE, not silence",
          len(a) == 1 and a[0]["action"] == "CREATE", str(a))
    check("overdue CREATE is titled OVERDUE",
          a and a[0]["summary"].startswith("📌 OVERDUE:"), str(a))
    check("overdue CREATE is tomato", a and a[0]["colorId"] == COLOR_OVERDUE, str(a))

    # ...but SUBMITTED past work stays dropped. An event for last week's finished
    # work is noise, and noise is what stops him reading the alerts at all.
    check("submitted + long past -> still no CREATE",
          acts([item(due=_past, submitted=True, states=["NeedsGrading"])], []) == [],
          str(acts([item(due=_past, submitted=True, states=["NeedsGrading"])], [])))

    # 2f. An overdue item is NEVER re-timed onto the deadline it already missed --
    #     its event is a catch-up slot. Colour and prefix still sync.
    _catchup = ev(start=DUE_6PM, end=DUE_6PM, summary="📌 OVERDUE: HUMCOM1 — 2.14 Version Control")
    a = acts([od], [_catchup])
    check("overdue event is not dragged back to a dead stamp",
          all("end" not in x.get("changes", []) for x in a), str(a))

    # 2g. The colour map itself (Ralph's scheme, 2026-08-27). Guards against a
    #     silent revert to the old two-colour world.
    check("future due -> flamingo",
          acts([item()], [ev(colorId="9")])[0]["colorId"] == COLOR_DUE)
    check("submitted -> pistachio",
          acts([item(submitted=True, states=["NeedsGrading"])], [ev(colorId="9")])[0]["colorId"]
          == COLOR_DONE)
    check("the four school colours are distinct",
          len({COLOR_CLASS, COLOR_DUE, COLOR_OVERDUE, COLOR_DONE}) == 4)
    check("school colours never collide with work colours",
          not ({COLOR_CLASS, COLOR_DUE, COLOR_OVERDUE, COLOR_DONE}
               & {COLOR_WORK_DUE, COLOR_WORK_DONE}))
    check("every colour is API-settable (1-11)",
          all(c.isdigit() and 1 <= int(c) <= 11
              for c in (COLOR_CLASS, COLOR_DUE, COLOR_OVERDUE, COLOR_DONE,
                        COLOR_WORK_DUE, COLOR_WORK_DONE)))

    # 3. THE OTHER BUG: instructor retitles the item. Marker must hold the link,
    #    so this is an UPDATE -- never a duplicate CREATE.
    a = acts([item(name="2.14 Version Control RENAMED")], [ev(end=DUE_1159)])
    check("retitled item -> UPDATE, not CREATE",
          len(a) == 1 and a[0]["action"] == "UPDATE", str(a))
    check("retitle never duplicates", not any(x["action"] == "CREATE" for x in a))

    # 4. Missing entirely -> CREATE, carrying the marker so it is linked next time.
    a = acts([item()], [])
    check("no event -> CREATE", len(a) == 1 and a[0]["action"] == "CREATE")
    check("CREATE carries the marker", a and a[0]["marker"] == MK)
    check("CREATE is red", a and a[0]["colorId"] == COLOR_LIVE)

    # 5. Submitted -> retitle green, per Ralph's decision (keep the record).
    a = acts([item(submitted=True, states=["NeedsGrading"])], [ev()])
    check("submitted -> UPDATE", len(a) == 1 and a[0]["action"] == "UPDATE")
    check("submitted -> ✅ SUBMITTED title", a and a[0]["summary"].startswith("✅ SUBMITTED:"))
    check("submitted -> green", a and a[0]["colorId"] == COLOR_DONE)
    check("submitted -> 📌 dropped (6am Shortcut filters on it)",
          a and "📌" not in a[0]["summary"])

    # 6. Hand-made events must be untouchable.
    hand = {"id": "mine", "summary": "Dentist", "description": "no marker",
            "start": DUE_6PM, "colorId": None}
    check("unmarked, unrelated event is ignored",
          all(x.get("eventId") != "mine" for x in acts([item()], [ev(), hand])))

    # 7. MIGRATION: today's real calendar has 📌 events with no marker. Those must
    #    be ADOPTed, never duplicated -- a duplicate alert trains him to ignore alerts.
    legacy = ev(id="legacy", description="created by hand, no marker")
    a = acts([item()], [legacy])
    check("legacy 📌 event -> ADOPT", len(a) == 1 and a[0]["action"] == "ADOPT", str(a))
    check("ADOPT targets the existing event", a and a[0]["eventId"] == "legacy")
    check("ADOPT never CREATEs a duplicate", not any(x["action"] == "CREATE" for x in a))

    # 8. Vanished item -> proposal only. NEVER an executed delete.
    a = acts([], [ev()])
    check("orphan -> PROPOSE_DELETE", len(a) == 1 and a[0]["action"] == "PROPOSE_DELETE")
    check("no action is ever a bare DELETE",
          not any(x["action"] == "DELETE" for x in a))

    # 9. Two events sharing one marker -> flag the duplicate, keep one.
    a = acts([item()], [ev(id="e1"), ev(id="e2")])
    check("duplicate markers -> PROPOSE_DELETE for the extra",
          any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))

    # 10. REGRESSION (found by the first live run, not by these tests):
    #     one unmarked event was proposed for adoption by THREE different items.
    #     Applying that would have corrupted the calendar.
    shared = ev(id="shared", description="no marker",
                summary="📌 DUE: PROGIT1 — Unit 3 Practice Activities")
    three = [item(colid="_1_1", code="PROGIT1", name="Unit 3: Learning Activities"),
             item(colid="_2_1", code="PROGIT1", name="Unit 3: Practice Activities"),
             item(colid="_3_1", code="PROGIT1", name="Unit 3: Practice Activities (Continuation)")]
    a = acts(three, [shared])
    adopts = [x for x in a if x["action"] == "ADOPT"]
    check("one event is adopted at most ONCE", len(adopts) <= 1,
          f"{len(adopts)} adoptions of the same event: {adopts}")
    check("losers CREATE instead of double-adopting",
          len([x for x in a if x["action"] == "CREATE"]) == 2, str(a))

    # 11. REGRESSION: proposed CREATE for work submitted last week. The contract
    #     has always said never create an event for already-submitted work.
    old_done = item(due="2026-08-11T09:00:00.000Z", submitted=True, states=["NeedsGrading"])
    check("submitted + no event -> NO action", acts([old_done], []) == [],
          str(acts([old_done], [])))
    check("unsubmitted + no event -> still CREATEs",
          [x["action"] for x in acts([item()], [])] == ["CREATE"])

    # 12. Adoption must not reach across days -- a same-titled event in another
    #     week is a different deadline, not this one.
    far = ev(id="far", description="no marker", start="2026-09-02T10:00:00.000Z")
    check("adoption requires the same day",
          not any(x["action"] == "ADOPT" for x in acts([item()], [far])), str(acts([item()], [far])))

    # 13. REGRESSION: filtering an item out of scope must NEVER make its event
    #     look orphaned. The history filter briefly proposed deleting three real
    #     submitted-work events. A filter that invents deletions is worse than
    #     no filter.
    old_item = item(due="2026-08-11T09:00:00.000Z", submitted=True, states=["NeedsGrading"])
    old_ev = ev(id="old", start="2026-08-11T09:00:00.000Z", end="2026-08-11T09:00:00.000Z",
                summary="✅ SUBMITTED: HUMCOM1 — 2.14 Version Control")
    a = acts([old_item], [old_ev])
    check("filtered-out item is never proposed for deletion",
          not any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))

    # 14. An event deliberately labelled OVERDUE is a catch-up block the human
    #     chose. Do not drag it back onto the missed deadline.
    catchup = ev(summary="📌 OVERDUE (late OK): PROGIT1 — Continuation",
                 start="2026-08-19T12:00:00.000Z", end="2026-08-19T13:00:00.000Z")
    check("explicit OVERDUE block is left alone",
          acts([item()], [catchup]) == [], str(acts([item()], [catchup])))

    # 15. 🔴 REGRESSION 2026-08-22 — a CLEARED DUE DATE IS NOT A DELETED ITEM.
    #     INTRCS1's 'Week 2 Performance Task' (30 pts, UNSUBMITTED) had its due
    #     date set to null. collect_blackboard only returns dated columns, so the
    #     item fell out of `items`, its marked event looked orphaned, and the
    #     reconciler proposed deleting a LIVE 30-point deadline. `all_keys`
    #     carries every column Blackboard returned, dated or not.
    marked = ev(description=MK)
    a = acts([], [marked])
    check("without all_keys, a vanished item still proposes deletion",
          any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))
    a = diff([], [marked], all_keys={(CID, COL)})
    check("due:null column is NEVER proposed for deletion",
          not any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))
    a = diff([], [marked], all_keys=set())
    check("a genuinely deleted column IS still proposed for deletion",
          any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))

    # 16. 🔴 A course we could not READ must not have its events proposed for
    #     deletion. Otherwise one transient 401 generates a delete proposal for
    #     every event in that course — an alert manufactured by our own error.
    marked = ev(description=MK)
    a = diff([], [marked], all_keys=set(), blind={CID})
    check("a course that failed to read proposes NO deletion",
          not any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))
    a = diff([], [marked], all_keys=set(), blind={"_999_9"})
    check("a DIFFERENT course being blind does not suppress a real deletion",
          any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))

    # 17. 🔴 WORK TICKETS ARE UNTOUCHABLE. They live on the same calendar (Grape,
    #     colorId 3, 💼 prefix) and carry Ralph's client commitments. opencode is
    #     getting write access to this calendar, so "safe by accident" is no
    #     longer good enough.
    check("a Grape event is recognised as work", is_work_event({"colorId": "3", "summary": "x"}))
    check("a 💼 title is recognised as work", is_work_event({"summary": "💼 WORK: CFB receipt"}))
    check("a school event is NOT work", not is_work_event(ev()))

    #     Even with our own marker forcibly attached, a work ticket yields nothing.
    hijacked = ev(summary="💼 WORK: CFB acknowledgment receipt", description=MK,
                  start=DUE_1159, end=DUE_1159, colorId=COLOR_WORK)
    a = acts([item()], [hijacked])
    check("a work ticket is never UPDATEd, even carrying our marker",
          not any(x["action"] == "UPDATE" for x in a), str(a))
    check("a work ticket is never PROPOSE_DELETEd",
          not any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))
    check("a work ticket is never ADOPTed",
          not any(x["action"] == "ADOPT" for x in a), str(a))
    check("...and the school item still gets its own event instead",
          any(x["action"] == "CREATE" for x in a), str(a))

    #     POSITIVE CONTROL: the same event WITHOUT the work marks is still handled,
    #     so the guard is not just suppressing everything.
    normal = ev(description=MK, start=DUE_1159, end=DUE_1159)
    check("positive control: a normal drifted school event still UPDATEs",
          any(x["action"] == "UPDATE" for x in acts([item()], [normal])),
          str(acts([item()], [normal])))

    # ------------------------------------------------------------------
    # 9. A HUMAN'S ✅ DONE OUTRANKS Blackboard's attempts=[].
    #
    #    The 2026-09-06 defect, sighted twice. `submitted` comes from
    #    `attempts`, which cannot see a paper quiz or a classroom sitting, so an
    #    event a person closed by hand was reverted DONE -> OVERDUE, sage ->
    #    tomato, on EVERY run. The first mitigation was a note in the event
    #    DESCRIPTION -- which the planner never read, so the next run proposed
    #    the identical revert. These assertions are the code-level guard.
    _past = f"{(datetime.now(MNL) - timedelta(days=3)).date()}T10:00:00.000Z"
    sat_in_class = ev(summary="✅ DONE: HUMCOM1 — 2.14 Version Control (40 pts)",
                      colorId=COLOR_DONE, start=_past, end=_past)
    a = acts([item(due=_past)], [sat_in_class])
    check("human ✅ DONE is never reverted to OVERDUE",
          not any(x["action"] == "UPDATE" for x in a), str(a))
    check("...and the deferral is REPORTED, not silent",
          any(x["action"] == "HELD" for x in a), str(a))
    check("...and HELD writes nothing (no colorId/summary to apply)",
          all("changes" not in x for x in a if x["action"] == "HELD"), str(a))

    #    The description form, for when the title must say something else.
    #    🔴 This fixture must DISAGREE with the reconciler, or it proves nothing.
    #    A '📌 DUE' + flamingo event on an overdue item is exactly what diff()
    #    wants to rewrite to OVERDUE + tomato; only the description holds it.
    noted = ev(summary="📌 DUE: HUMCOM1 — 2.14 Version Control (40 pts)",
               description=f"sat it in F209\nhuman: done\n{MK}\n",
               colorId=COLOR_DUE, start=_past, end=_past)
    _n = acts([item(due=_past)], [noted])
    check("'human: done' in the description also holds",
          not any(x["action"] == "UPDATE" for x in _n), str(_n))
    check("...and it too is reported as HELD",
          any(x["action"] == "HELD" for x in _n), str(_n))
    check("human_done() reads both forms",
          human_done(sat_in_class) and human_done(noted))

    #    NEGATIVE CONTROL for the hold: an ordinary overdue event with NO human
    #    mark must still be corrected. Without this, "never reverts" could be
    #    passing because the reconciler stopped doing anything at all.
    plain = ev(summary="📌 DUE: HUMCOM1 — 2.14 Version Control (40 pts)",
               colorId=COLOR_DUE, start=_past, end=_past)
    a = acts([item(due=_past)], [plain])
    check("negative control: an UNMARKED overdue event is still corrected",
          any(x["action"] == "UPDATE" for x in a), str(a))
    check("human_done() is False on an unmarked event", not human_done(plain))

    #    DONE IS TERMINAL: even once Blackboard agrees, the human title stands.
    #    🔴 The due date must be RECENT. diff() drops submitted work more than
    #    12 h past its stamp, so a long-past fixture here is filtered out before
    #    a single decision runs and the assertion passes vacuously.
    _recent = f"{datetime.now(MNL).date()}T10:00:00.000Z"
    still_done = ev(summary="✅ DONE: HUMCOM1 — 2.14 Version Control (40 pts)",
                    colorId=COLOR_DONE, start=_recent, end=_recent)
    a = acts([item(due=_recent, submitted=True, states=["NeedsGrading"])], [still_done])
    check("a held event is not churned DONE -> SUBMITTED once Blackboard agrees",
          not any(x["action"] == "UPDATE" for x in a), str(a))

    # 10. ADOPT A CATCH-UP BLOCK THAT SITS ON ANOTHER DAY.
    #
    #     The second 2026-09-06 defect. A catch-up block is deliberately NOT on
    #     the deadline it missed, so the absolute same-day rule made the events
    #     most worth adopting unmatchable -- and diff() proposed a duplicate
    #     CREATE on the dead stamp instead. Real case: PROGIT1 Unit 3 Practice
    #     Activities (Continuation), due 19 Aug, block placed 6 Sep.
    _long_past = f"{(datetime.now(MNL) - timedelta(days=18)).date()}T01:00:00.000Z"
    _block_day = f"{(datetime.now(MNL) + timedelta(days=1)).date()}T02:00:00.000Z"
    catchup = ev(id="evt-catchup", description="no marker here",
                 summary="📌 OVERDUE (late OK): HUMCOM1 — 2.14 Version Control (40 pts)",
                 start=_block_day, end=_block_day, colorId=COLOR_OVERDUE)
    a = acts([item(due=_long_past)], [catchup])
    check("an off-day catch-up block is ADOPTed, not duplicated",
          [x["action"] for x in a] == ["ADOPT"], str(a))
    check("...and ADOPT targets that very event",
          a and a[0]["eventId"] == "evt-catchup", str(a))
    check("...and no duplicate CREATE is proposed alongside it",
          not any(x["action"] == "CREATE" for x in a), str(a))

    #     NEGATIVE CONTROL: a SIBLING column must NOT be adopted across days.
    #     'Quiz 1-Lesson1.1' and 'Quiz 2-Lesson1.2' share exactly two tokens,
    #     which is why the off-day bar is three.
    sibling = ev(id="evt-sib", description="no marker",
                 summary="📌 DUE: HUMCOM1 — Quiz 2-Lesson1.2",
                 start=_block_day, end=_block_day)
    a = acts([item(due=_long_past, name="Quiz 1-Lesson1.1")], [sibling])
    check("negative control: a sibling column is NOT adopted across days",
          not any(x["action"] == "ADOPT" for x in a), str(a))
    check("...it gets its own CREATE instead", 
          any(x["action"] == "CREATE" for x in a), str(a))

    #     NEGATIVE CONTROL: FORWARD ONLY. An event BEFORE a not-yet-due
    #     deadline is another week's column, not this item's catch-up block.
    #     (This is the rule test 12 below has always asserted; the off-day
    #     relaxation had to be narrowed to keep it true.)
    _before = f"{(datetime.now(MNL) - timedelta(days=5)).date()}T02:00:00.000Z"
    earlier = ev(id="evt-early", description="no marker",
                 summary="📌 DUE: HUMCOM1 — 2.14 Version Control (40 pts)",
                 start=_before, end=_before)
    check("negative control: an event BEFORE the due date is not adopted",
          not any(x["action"] == "ADOPT" for x in acts([item()], [earlier])),
          str(acts([item()], [earlier])))

    #     NEGATIVE CONTROL: distance is bounded. A same-named event a year away
    #     is not this item's catch-up block.
    _far = f"{(datetime.now(MNL) + timedelta(days=200)).date()}T02:00:00.000Z"
    faraway = ev(id="evt-far", description="no marker",
                 summary="📌 OVERDUE (late OK): HUMCOM1 — 2.14 Version Control (40 pts)",
                 start=_far, end=_far)
    check("negative control: adoption is bounded to 60 days",
          not any(x["action"] == "ADOPT" for x in acts([item(due=_long_past)], [faraway])),
          str(acts([item(due=_long_past)], [faraway])))

    #     The same-day path must be UNCHANGED -- it keeps the looser 2-token bar.
    same_day_sib = ev(id="evt-sd", description="no marker",
                      summary="📌 DUE: HUMCOM1 — Quiz 2-Lesson1.2",
                      start=DUE_6PM, end=DUE_6PM)
    check("same-day adoption still uses the looser 2-token bar",
          any(x["action"] == "ADOPT"
              for x in acts([item(name="Quiz 1-Lesson1.2")], [same_day_sib])),
          str(acts([item(name="Quiz 1-Lesson1.2")], [same_day_sib])))

    # ---------------------------------------------------------------- 2026-09-10
    # THE MIS-ADOPTION DEFECT. A marked event aged out of the 7-day lookback,
    # its column looked unmarked, and an unrelated same-series event was
    # adopted. Three guards, each proven separately.

    # (a) A human-closed event is never an adoption target.
    held_lookalike = ev(id="held1", description="human: done\n(no marker)",
                        summary="✅ DONE: HUMCOM1 — Emailed Instructor B re 2.14 Version Control")
    a = acts([item(name="2.14 Version Control")], [held_lookalike])
    check("human-closed event is never ADOPTed",
          not any(x["action"] == "ADOPT" for x in a), str(a))

    # (b) Same, via the ✅ DONE: title prefix rather than the description line.
    held_by_title = ev(id="held2", description="(no marker at all)",
                       summary="✅ DONE: HUMCOM1 — 2.14 Version Control sat in class")
    a = acts([item(name="2.14 Version Control")], [held_by_title])
    check("✅ DONE: title alone also blocks ADOPT",
          not any(x["action"] == "ADOPT" for x in a), str(a))

    # (c) An UNHELD lookalike is still adoptable — proves (a) and (b) are the
    #     hold doing the work, not the fixture failing to match at all.
    open_lookalike = ev(id="open1", description="(no marker)",
                        summary="📌 DUE: HUMCOM1 — 2.14 Version Control")
    a = acts([item(name="2.14 Version Control")], [open_lookalike])
    check("an unheld lookalike IS still adoptable (control for a+b)",
          any(x["action"] == "ADOPT" for x in a), str(a))

    # (d) The window tripwire fires when an item predates every loaded event.
    old_item = item(due=(datetime.now(MNL) - timedelta(days=200)).isoformat())
    check("window_guard names an item older than the oldest loaded event",
          len(window_guard([old_item], [ev()])) == 1,
          str(window_guard([old_item], [ev()])))

    # (e) ...and stays silent when the window covers the item. Without this,
    #     a guard that warned unconditionally would pass (d) and be useless.
    check("window_guard is silent when the window covers the item",
          window_guard([item()], [ev()]) == [],
          str(window_guard([item()], [ev()])))

    # --- the grade record as second witness (2026-09-15) --------------------
    # Live shapes: PROGIT1 Quiz 2 (face to face) = attempts [] + Graded 19;
    # MATHMW1 Number Pattern = attempts [] + a record with no status or score.
    PAST = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    sub, st = witnessed([], {"status": "Graded", "score": 19.0})
    check("hand-graded quiz with no attempts counts as submitted",
          sub is True and st and "Graded 19" in st[0], f"{sub} {st}")
    quiz = item(name="Quiz 2 (face to face)", code="PROGIT1", due=PAST,
                submitted=sub, states=st)
    a = acts([quiz], [])
    check("hand-graded past quiz -> NO false OVERDUE create", a == [], str(a))

    sub, st = witnessed([], {"userId": "_1", "columnId": "_9007_1"})
    check("empty grade record does NOT count as submitted", sub is False, f"{sub} {st}")
    open_item = item(name="Number Pattern", code="MATHMW1", due=PAST,
                     submitted=sub, states=st)
    a = acts([open_item], [])
    check("genuinely unstarted past item STILL creates OVERDUE (control)",
          len(a) == 1 and a[0]["action"] == "CREATE"
          and a[0]["summary"].startswith("📌 OVERDUE"), str(a))

    check("NeedsGrading alone in the grade record proves nothing",
          witnessed([], {"status": "NeedsGrading"})[0] is False)
    check("unreadable attempts (None) are never upgraded",
          witnessed(None, None) == (False, None))
    check("real attempts still decide first",
          witnessed(["NeedsGrading"], None) == (True, ["NeedsGrading"]))
    # 2026-09-29: HUMCOM1 Week7-Quiz1 -- attempt opened, never sent.
    check("an InProgress-only attempt is NOT submitted",
          witnessed(["InProgress"], None)[0] is False)
    check("a Draft-only attempt is NOT submitted",
          witnessed(["Draft"], None)[0] is False)
    check("an unsent attempt beside a sent one still counts (last-attempt tests)",
          witnessed(["Completed", "InProgress"], None)[0] is True)

    # ------------------------------------------------------------------
    # 2026-09-27 — five defects found by a live dry run. Each block below goes
    # RED with its fix removed (negative controls run and recorded that day).
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    _wk_ago = f"{(datetime.now(MNL) - timedelta(days=14)).date()}T09:00:00.000Z"

    # D1. SUBMITTED WORK IS NEVER RETIMED. The live case: SOCORN2 due dates were
    #     CLEARED after submission; the next_meeting() stand-in (always future)
    #     slipped past the drop-past-submitted filter, and five ✅ events were
    #     about to move onto the next class meeting -- every week, forever.
    stand_in = next_meeting("SOCORN2")
    done_undated = item(code="SOCORN2", name="Career Pathing e-Certificate", pts=30.0,
                        due=stand_in, due_unknown=True, submitted=True,
                        states=["Completed"])
    old_ev = ev(summary="✅ SUBMITTED: SOCORN2 — Career Pathing e-Certificate (30 pts)",
                start=_wk_ago, end=_wk_ago, colorId=COLOR_DONE)
    a = acts([done_undated], [old_ev])
    check("D1 submitted + cleared due -> NOT retimed onto the stand-in",
          all("end" not in x.get("changes", []) for x in a), str(a))
    check("D1 ...and an event already correct stays silent", a == [], str(a))
    pending_ev = ev(summary="📌 OVERDUE: SOCORN2 — Thrivingforl;fe (30 pts)",
                    start=_wk_ago, end=_wk_ago, colorId=COLOR_OVERDUE)
    a = acts([dict(done_undated, name="Thrivingforl;fe")], [pending_ev])
    check("D1 submitted + cleared due -> prefix STILL flips to ✅ (no over-fix)",
          len(a) == 1 and a[0]["action"] == "UPDATE"
          and a[0]["summary"].startswith("✅ SUBMITTED")
          and "end" not in a[0]["changes"], str(a))
    moved = item(submitted=True, states=["NeedsGrading"],
                 due=f"{(datetime.now(MNL) + timedelta(days=5)).date()}T10:00:00.000Z")
    a = acts([moved], [ev(summary="✅ SUBMITTED: HUMCOM1 — 2.14 Version Control",
                          colorId=COLOR_DONE)])
    check("D1 submitted item whose REAL due moved forward -> not retimed",
          all("end" not in x.get("changes", []) for x in a), str(a))
    unsub = acts([item(due=moved["due"])], [ev()])
    check("D1 control: UNSUBMITTED drift is still corrected",
          len(unsub) == 1 and "end" in unsub[0]["changes"], str(unsub))

    # D2. A course left out by a filter is BLIND, not empty -> no deletions.
    other = ev(id="evt9", description=f"bb:_9008_1:_1_1\n")
    a = diff([item()], [ev(), other], {(CID, COL)}, {"_9008_1"})
    check("D2 filtered-out course -> NO PROPOSE_DELETE",
          not any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))
    a = diff([item()], [ev(), other], {(CID, COL)}, set())
    check("D2 control: a genuinely vanished column still proposes delete",
          any(x["action"] == "PROPOSE_DELETE" for x in a), str(a))

    # D2 at its source: collect_blackboard itself must mark a filtered-out
    # course blind. A fake session stands in for Blackboard (no network).
    import cal_sync as _cs

    class _Sess:
        uid = "_u"

        def get(self, url):
            if url.endswith("/courses?limit=100"):
                return {"results": [{"courseId": "_1_1"}, {"courseId": "_2_1"}]}
            if url.endswith("/courses/_1_1"):
                return {"courseId": "1STSEM_IAA1_HUMCOM1"}
            if url.endswith("/courses/_2_1"):
                return {"courseId": "1STSEM_IAA1_SOCORN2"}
            return {"results": []}

    class _CM:
        def __enter__(self): return _Sess()
        def __exit__(self, *a): return False

    _orig = _cs.open_session
    _cs.open_session = lambda: _CM()
    try:
        _items, _err, (_keys, _blind) = _cs.collect_blackboard({"HUMCOM1"})
    finally:
        _cs.open_session = _orig
    check("D2 collect_blackboard: a filtered-out course lands in blind",
          _blind == {"_2_1"}, str(_blind))

    # D2b. A 403 course is NAMED (2026-10-11): a closed faculty-evaluation shell
    # leaves the "NOT fully read" warning, a closed real course stays on it, and
    # BOTH stay blind so neither can propose deleting an event.
    class _Sess2:
        uid = "_u"
        M = [("_1_1", "1STSEM_IAA1_HUMCOM1", "Yes"),
             ("_2_1", "1STSEM_IAA1_ARTAPP1_EVAL", "No"),
             ("_3_1", "1STSEM_IAA1_PROGIT1", "No")]

        def get(self, url):
            if url.endswith("/courses?limit=100"):
                return {"results": [{"courseId": m[0]} for m in self.M]}
            if url.endswith("/courses?limit=100&expand=course"):
                return {"results": [{"courseId": m[0], "course": {
                    "courseId": m[1], "availability": {"available": m[2]}}} for m in self.M]}
            for cid, full, av in self.M:
                if url.endswith(f"/courses/{cid}"):
                    return {"_status": 403} if av == "No" else {"courseId": full}
            return {"results": []}

    class _CM2:
        def __enter__(self): return _Sess2()
        def __exit__(self, *a): return False

    _cs.open_session = lambda: _CM2()
    try:
        _items, _err, (_keys, _blind) = _cs.collect_blackboard()
    finally:
        _cs.open_session = _orig
    check("D2b closed _EVAL shell is NOT in 'NOT fully read'",
          _err is not None and "_EVAL" not in _err and "_2_1" not in _err, str(_err))
    check("D2b ...it is listed as a closed evaluation shell instead",
          _cs.CLOSED_EVAL == ["1STSEM_IAA1_ARTAPP1_EVAL"], str(_cs.CLOSED_EVAL))
    check("D2b closed REAL course is in 'NOT fully read', by name",
          "1STSEM_IAA1_PROGIT1" in _err and "closed by the instructor" in _err, str(_err))
    check("D2b both 403 courses stay blind (no deletes)",
          _blind == {"_2_1", "_3_1"}, str(_blind))

    # D3. OVERDUE CREATE -> the course's next class meeting, placed once.
    _past3 = f"{(datetime.now(MNL) - timedelta(days=3)).date()}T09:00:00.000Z"
    od = item(code="PROGIT1", name="M Lab Quiz 1 (If Else Complete and Trace)",
              due=_past3)
    a = acts([od], [])
    slot = next_meeting("PROGIT1")
    check("D3 overdue CREATE lands on the next class meeting",
          len(a) == 1 and a[0]["action"] == "CREATE" and a[0]["start"] == slot, str(a))
    check("D3 ...in the future, where the 6am Shortcut can see it",
          a and datetime.fromisoformat(a[0]["start"].replace("Z", "+00:00")) > now)
    check("D3 ...and keeps the real stamp as was_due",
          a and a[0].get("was_due") == fmt(_past3), str(a))
    a = acts([item(due=DUE_6PM)], [])
    check("D3 control: a FUTURE item is still created on its own due date",
          len(a) == 1 and a[0]["start"] == DUE_6PM and "was_due" not in a[0], str(a))
    placed = ev(summary="📌 OVERDUE: PROGIT1 — M Lab Quiz 1 (40 pts)",
                start=slot, end=slot, colorId=COLOR_OVERDUE)
    a = acts([od], [placed])
    check("D3 a placed catch-up slot is NEVER chased afterwards", a == [], str(a))
    check("D3 next_meeting: Sunday 1 PM -> PROGIT1 Mon 9:00 AM",
          next_meeting("PROGIT1", datetime(2026, 9, 27, 5, 0, tzinfo=timezone.utc))
          == "2026-09-28T01:00:00Z")
    check("D3 next_meeting: a slot already passed today rolls to its next day",
          next_meeting("CMPTRB1", datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))
          == "2026-10-05T09:00:00Z")
    check("D3 next_meeting: unknown course -> None", next_meeting("NOPE101") is None)

    # D4. cal_sync owns only DUE / OVERDUE / SUBMITTED.
    confirm = ev(summary="📌 CONFIRM: HUMCOM1 Lab — Movie webpage (25 pts): due this week?",
                 colorId=COLOR_DUE)
    a = acts([item(due=_past3)], [confirm])
    check("D4 📌 CONFIRM on an overdue item -> HELD, no UPDATE",
          [x["action"] for x in a] == ["HELD"], str(a))
    check("D4 ...and the HELD line says the prefix belongs to a person",
          bool(a) and "set by a person" in a[0].get("why", ""), str(a))
    buy = ev(summary="📌 BUY: HUMCOM1 — 2.14 Version Control", colorId=COLOR_DUE)
    a = acts([item(submitted=True, states=["Completed"])], [buy])
    check("D4 📌 BUY is never re-prefixed",
          not any(x["action"] == "UPDATE" for x in a), str(a))
    event = ev(summary="📌 EVENT: HUMCOM1 — 2.14 Version Control demo", colorId="7")
    a = acts([item()], [event])
    check("D4 📌 EVENT is never recoloured",
          not any(x["action"] == "UPDATE" for x in a), str(a))
    a = acts([item(submitted=True, states=["Completed"])], [ev()])
    check("D4 control: an owned 📌 DUE still flips to ✅ SUBMITTED",
          len(a) == 1 and a[0]["action"] == "UPDATE", str(a))

    # D5. Exact twin of a submitted column -> TWIN, never an OVERDUE alarm.
    ld = dict(code="INTRCS1", pts=20.0, due=_past3,
              name="2627A_Learning and Discovery Report - To be pass every before exam")
    twin_done = item(colid="_9001_1", submitted=True, states=["Completed"], **ld)
    twin_empty = item(colid="_9002_1", **ld)
    a = acts([twin_done, twin_empty], [])
    check("D5 empty twin of a submitted column -> TWIN, no CREATE",
          [x["action"] for x in a] == ["TWIN"] and "_9001_1" in a[0]["why"], str(a))
    a = acts([twin_done, item(colid="_9002_1", **dict(ld, due=DUE_6PM))], [])
    check("D5 control: same name, DIFFERENT due -> a real item, still created",
          [x["action"] for x in a] == ["CREATE"], str(a))
    a = acts([twin_done, item(colid="_9002_1", **dict(ld, pts=25.0))], [])
    check("D5 control: same name, different points -> still created",
          [x["action"] for x in a] == ["CREATE"], str(a))
    und = dict(due=stand_in, due_unknown=True, code="SOCORN2", name="Reflection")
    a = satisfied_twins([item(colid="_a", submitted=True, **und), item(colid="_b", **und)])
    check("D5 two UNDATED columns never pair on the stand-in", a == {}, str(a))
    t = satisfied_twins(
        [{"cid": "c", "column": "_1", "title": "X", "due": _past3, "pts": 5, "submitted": True},
         {"cid": "c", "column": "_2", "title": "X", "due": _past3, "pts": 5, "submitted": False}],
        key=lambda i: (i["cid"], i["title"].strip(), i["due"], i["pts"]),
        ident=lambda i: (i["cid"], i["column"]))
    check("D5 helper works on study_load's item shape", t == {("c", "_2"): "_1"}, str(t))

    # NEGATIVE CONTROL: prove the assertions can fail. If a knowingly-wrong
    # calendar still reports "in sync", nothing above is being checked.
    if acts([item()], [ev(start=DUE_1159, end=DUE_1159)]) == []:
        print("  FAIL  negative control: a wrong due date reported as in-sync")
        failed += 1
    else:
        print("  PASS  negative control: a wrong due date is never silent")

    # 🔴 COUNT, DO NOT HARDCODE. This line read "(47 assertions + 1 control)" as
    # a literal while 55 were actually running (found 2026-08-27 by adding eleven
    # and watching the number not move). A hardcoded total is worse than none: it
    # looks like a coverage check, so an assertion that stops running -- deleted,
    # or stranded after an early return -- reports the same reassuring number.
    print(f"\n{'🔴 ' + str(failed) + ' FAILED' if failed else '✅ ALL PASS'}"
          f"  ({ran} assertions + 1 control)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
