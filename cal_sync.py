#!/usr/bin/env python3
"""Reconcile Google Calendar against AUTHORITATIVE Blackboard due dates.

WHY THIS EXISTS
---------------
Ralph runs his life off Google Calendar. Before this file, calendar events were
created once, by an LLM following prose, and never re-checked against Blackboard.
On 2026-08-19 that produced a 5-hour error on a 40-point ONE-ATTEMPT assessment
(HUMCOM1 2.14 was written as 11:59 PM; the real deadline was 6:00 PM) and missed
another item's move to 26 Aug entirely. Three sweeps repeated the wrong time,
because a sweep only *reports* -- nothing compared it to the calendar.

WHAT IT DOES
------------
Computes a diff between Blackboard truth and the calendar. This module never
writes: `cal_apply.py` (`./lms.sh apply`) executes the action list through its
own calendar-scoped token, and the MCP tools are the fallback. The DECISIONS are
deterministic and testable here; only the execution lives elsewhere.
(Corrected 2026-09-27 -- this used to say Python could not write the calendar,
which stopped being true when cal_apply got its token.)

IDENTITY -- the thing that actually broke
-----------------------------------------
Events are matched by a marker `bb:<courseId>:<columnId>` on its own line in the
event description, NOT by title. Instructors rename items in place (2026-08-19:
'...8/19/26' -> '...8/26/26'), and title-matching silently loses the link. A
column ID never changes.

An event with no marker is NEVER touched -- that protects everything Ralph made
by hand. Pre-existing deadline events are surfaced as ADOPT proposals instead.

SAFETY
------
- Never deletes. A vanished Blackboard item yields PROPOSE_DELETE, print only.
- Fails loud (exit 2) on a dead Blackboard session. An empty diff must never be
  producible by a failed pull -- that is indistinguishable from "all clean".
- Requires the calendar dump to be passed in. It will not assume an empty
  calendar, which would propose re-creating every event as a duplicate.

USAGE
-----
    ./lms.sh cal <events.json> [COURSE ...] [--json]

<events.json> is the raw result of the MCP `list_events` call (schema:
{"events": [...]}) -- the harness already saves that to a file when it is large.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bb_session import SessionDead, open_session
from bb_sweep import authoritative_due, BASE, MNL, FMT

MARKER = re.compile(r"^bb:(_\d+_\d+):(_\d+_\d+)\s*$", re.M)
# A person's "I did this, Blackboard cannot see it" mark. See human_done().
HUMAN_DONE = re.compile(r"^\s*human:\s*done\b", re.M | re.I)
PROFILE = Path.home() / ".local/share/ub-blackboard-profile"
STAMP = Path.home() / ".local/share/ub-lms-lastverified.json"

# Google Calendar colour ids. Ralph's scheme, 2026-08-27.
#
# 🔴 THE API ACCEPTS ONLY IDs 1-11. The Calendar web UI now shows a wider
# palette, but colorId "12" is rejected with "Request contains an invalid
# argument" (measured 2026-08-27). Anything outside 1-11 is not settable here.
# "Pistachio" is the UI's name for the light green the API calls Sage = 2.
#
#   8  graphite  class schedule -- deliberately dull, must not pull focus
#   4  flamingo  a live deadline still in the future
#   11 tomato    OVERDUE: past its stamp and still unstarted
#   2  pistachio done
COLOR_CLASS = "8"
COLOR_DUE = "4"
COLOR_OVERDUE = "11"
COLOR_DONE = "2"

# Back-compat alias. Callers that predate the overdue state used one "live"
# colour; that meaning is now split in two, and COLOR_DUE is the safe default.
COLOR_LIVE = COLOR_DUE

# 🔴 WORK TICKETS SHARE THIS CALENDAR AND ARE NOT OURS TO TOUCH.
# Ralph runs CFB / LOCUS / POS commitments on the same primary calendar, marked
# Grape (colorId 3) with a 💼 prefix (memory: work-calendar-colour). School
# tooling must never modify, retime, recolour or propose deleting one.
#
# Until 2026-08-23 they were safe only BY ACCIDENT: is_managed_title() happens
# not to list 💼, so adoption skipped them. That is not a guarantee — it is a
# coincidence one prefix change would remove. With opencode about to get write
# access to this calendar, the protection is now explicit and test-enforced.
# 🔴 WIDENED 2026-08-27. Work tickets are now TWO colours: lavender (1) while
# due, grape (3) once done. Matching only grape would have left every live work
# ticket protected by the 💼 prefix alone -- re-creating precisely the
# "safe by accident" state the note above says was deliberately fixed.
COLOR_WORK_DUE = "1"
COLOR_WORK_DONE = "3"
COLOR_WORK = COLOR_WORK_DONE          # back-compat alias
WORK_COLORS = {COLOR_WORK_DUE, COLOR_WORK_DONE}


def is_work_event(ev) -> bool:
    """True for Ralph's work tickets. They are invisible to this reconciler."""
    return ((ev.get("colorId") or "") in WORK_COLORS
            or (ev.get("summary") or "").startswith("💼"))


def marker_of(ev):
    m = MARKER.search(ev.get("description") or "")
    return (m.group(1), m.group(2)) if m else None


def human_done(ev) -> bool:
    """True when a PERSON marked this event done on evidence Blackboard lacks.

    🔴 THE DEFECT THIS CLOSES (2026-09-06, second sighting). `it["submitted"]`
    comes from Blackboard `attempts`, which cannot see a paper quiz, an office
    hand-in or a classroom sitting. So an event a person closed by hand was
    reverted `✅ DONE` -> `📌 OVERDUE`, sage -> tomato, on EVERY run:

        [UPDATE] '✅ DONE: PROGIT1 — FG Quiz 1 (Face to Face) (22 pts)'
              -> '📌 OVERDUE: PROGIT1 — FG Quiz 1 (Face to Face) (22 pts)'

    That quiz was sat in a room. The 2026-09-06 mitigation was a do-not-revert
    NOTE in the event description -- and the planner never read descriptions, so
    the very next run proposed the identical revert. A note is for the human
    reading the dry run; this function is the signal the CODE reads.

    Two accepted forms, either sufficient:

    1. `human: done` on its own line in the description -- explicit, and the
       form to use when the title must say something else.
    2. A `✅ DONE:` prefix on the summary. This tool NEVER writes DONE
       (`title_for` and `_reprefix` emit only SUBMITTED / OVERDUE / DUE), so a
       DONE prefix can only have been typed by a person. Same reasoning as the
       existing `deliberate` escape hatch, which reads a hand-written OVERDUE.

    🔑 DONE IS TERMINAL. A held event is never retimed, retitled or recoloured,
    not even once Blackboard catches up and agrees -- rewriting DONE to
    SUBMITTED churns a human's title to change nothing a reader cares about.
    The cost is real and accepted: a WRONG hand-mark is never auto-corrected.
    That is the same trade this file already takes everywhere else -- a wrong
    silent rewrite of a person's decision is worse than a stale one they can see.
    """
    if HUMAN_DONE.search(ev.get("description") or ""):
        return True
    return _prefix_of(ev.get("summary")) == "DONE"


def norm(s):
    """Loose token set for ADOPT matching only -- never for identity."""
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - {
        "due", "the", "and", "of", "a", "pts", "self", "check"}


def is_managed_title(t):
    return (t or "").startswith(("📌", "✅", "🔴"))


def load_events(path):
    raw = json.loads(Path(path).read_text())
    evs = raw.get("events", raw if isinstance(raw, list) else [])
    out = []
    for e in evs:
        start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
        end = (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date")
        out.append({"id": e.get("id"), "summary": e.get("summary") or "",
                    "description": e.get("description") or "", "start": start,
                    "end": end, "colorId": e.get("colorId")})
    return out


def witnessed(states, grade):
    """(submitted, states) from attempts, with the GRADE RECORD as second witness.

    🔴 THE DEFECT THIS CLOSES (2026-09-15). A face-to-face quiz the instructor
    marks by hand has NO attempts, so `bool(states)` read it as unstarted and the
    dry run proposed `CREATE 📌 OVERDUE: PROGIT1 — Quiz 2 (face to face)` and
    `Quiz 3` -- both `Graded 19` / `Graded 23` in the same night's study load.
    `study_load` and `bb_sweep` got this witness on 2026-09-14; the reconciler
    that WRITES the calendar did not, so the two disagreed about the same column.

    One rule, not a copy: the evidence test is `lms_digest.hand_graded` (Graded/
    Completed or a recorded score; NeedsGrading alone proves nothing). Attempts
    still decide first. Imported lazily -- lms_digest pulls in Playwright.
    """
    # 🔴 AN OPENED ATTEMPT IS NOT A SUBMITTED ONE (2026-09-29). This used to be
    # `if states: return True` -- so HUMCOM1 Week7-Quiz1, opened at 12:50 PM and
    # never sent (attempt `InProgress`, attemptDate null), was written to the
    # calendar as `✅ SUBMITTED` over a hand-set OVERDUE. `lms_digest` has had
    # the right rule since 2026-08-17; use it rather than a second copy.
    from lms_digest import SUBMITTED_STATES, hand_graded
    if any(s in SUBMITTED_STATES for s in (states or [])):
        return True, states
    g = hand_graded(grade)
    return (True, [g]) if g else (False, states)


def collect_blackboard(courses_filter=None):
    """Return (items, error, all_keys).

    `items` carries only columns that HAVE an authoritative due date -- that is
    what the reconciler schedules against. `all_keys` is every column Blackboard
    returned, due date or not.

    🔴 The distinction is load-bearing. On 2026-08-22 INTRCS1's 'Week 2
    Performance Task' (30 pts, UNSUBMITTED) had its due date cleared to null.
    It therefore vanished from `items`, its marked calendar event looked
    orphaned, and the reconciler proposed deleting a live 30-point deadline.
    "Due date removed" and "item deleted" are different facts, and only
    `all_keys` can tell them apart.
    """
    items, unreadable, all_keys = [], [], set()
    # Course ids whose read FAILED. Their columns are unknown, not absent —
    # without this a transient 401 on one course makes every event in it look
    # orphaned and proposes deleting the lot.
    blind = set()
    # Session source is bb_session.py: Ralph's real Comet browser over CDP, or
    # the saved scraper profile -- whichever actually authenticates. This used
    # to hardcode the profile, so a live Comet session could not rescue it.
    try:
        cm = open_session()
        sess = cm.__enter__()
    except SessionDead as e:
        return None, e.report(), (set(), set())
    try:
        get = sess.get
        uid = sess.uid

        memb = get(f"{BASE}/users/{uid}/courses?limit=100")
        for m in memb.get("results", []):
            cid = m.get("courseId")
            c = get(f"{BASE}/courses/{cid}")
            if c.get("_status"):
                unreadable.append(f"{cid} ({c['_status']})")
                blind.add(cid)
                continue
            code = (c.get("courseId") or "").split("_")[-1]
            if courses_filter and code.upper() not in courses_filter:
                # 🔴 FILTERED OUT IS BLIND, NOT EMPTY (2026-09-27). This used to be
                # a bare `continue`, so none of the course's columns reached
                # `all_keys` and every marked event in it looked orphaned:
                # `apply ARTAPP1 LITERA1` proposed ~45 deletions, including events
                # created minutes earlier. Unread is unknown, same as a failed read.
                blind.add(cid)
                continue
            gb = get(f"{BASE}/courses/{cid}/gradebook/columns?limit=200")
            if gb.get("_status"):
                unreadable.append(f"{code} columns ({gb['_status']})")
                blind.add(cid)
                continue
            for g in gb.get("results", []):
                colid = g.get("id")
                due, warn = authoritative_due(
                    get, cid, colid, (g.get("grading") or {}).get("due"))
                all_keys.add((cid, colid))
                due_unknown = not due
                if due_unknown:
                    # 🔴 USED TO BE `continue`. That one line kept every due:null
                    # column off the calendar permanently -- and instructors here
                    # genuinely ship graded work with no stamp (INTRCS1 Week 2
                    # Performance Task, 30 pts, `due:null` for a week). Place it
                    # on the next class meeting and LABEL it as not-a-deadline.
                    due = next_meeting(code)
                    if not due:
                        continue
                att = get(f"{BASE}/courses/{cid}/gradebook/columns/{colid}/attempts")
                states = ([a.get("status") for a in att.get("results", [])]
                          if not att.get("_status") else None)
                # Only an attempts list that READ AND CAME BACK EMPTY earns the
                # grade-record lookup. An unreadable one (None) stays unknown.
                grade = (get(f"{BASE}/courses/{cid}/gradebook/columns/{colid}/users/{uid}")
                         if states == [] else None)
                submitted, states = witnessed(states, grade)
                score = g.get("score")
                items.append({
                    "cid": cid, "colid": colid, "code": code,
                    "name": g.get("name") or "", "due": due,
                    "pts": score.get("possible") if isinstance(score, dict) else None,
                    "submitted": submitted, "states": states, "warn": warn,
                    "due_unknown": due_unknown,
                })
    finally:
        cm.__exit__(None, None, None)
    return items, ("; ".join(unreadable) if unreadable else None), (all_keys, blind)


# Class END times, from school/schedule.md. Manila weekdays, Mon=0.
# Used ONLY to place work that the instructor left with no due date -- the
# course's next meeting is the moment it could plausibly be collected, and a
# labelled guess beats silence.
SCHEDULE = {
    "PROGIT1": [(0, 9, 0), (2, 9, 0), (1, 17, 0), (3, 17, 0)],
    "HUMCOM1": [(0, 13, 0), (2, 13, 0), (2, 17, 0)],
    "INTRCS1": [(0, 12, 0), (2, 12, 0), (3, 11, 0)],
    "MATHMW1": [(1, 12, 30), (3, 12, 30)],
    "LITERA1": [(0, 10, 0), (2, 10, 0), (4, 10, 0)],
    "ARTAPP1": [(0, 18, 0), (2, 18, 0), (4, 18, 0)],
    "NSTPRO1": [(1, 18, 30), (3, 18, 30)],
    "PATHFT1": [(1, 10, 0)],
    "CMPTRB1": [(0, 17, 0)],
    "SOCORN2": [(5, 11, 0)],
}


def next_meeting(code, now=None):
    """ISO-UTC end time of this course's next class meeting, or None."""
    slots = SCHEDULE.get(code)
    if not slots:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(MNL)
    best = None
    for wd, hh, mm in slots:
        delta = (wd - now.weekday()) % 7
        cand = (now + timedelta(days=delta)).replace(
            hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=7)
        if best is None or cand < best:
            best = cand
    return best.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def is_overdue(it, now=None):
    """Unstarted work whose stamp has passed. Submitted work is never overdue."""
    if it["submitted"] or it.get("due_unknown"):
        return False
    due = _inst(it["due"])
    return bool(due and due < (now or datetime.now(timezone.utc)))


def title_for(it, now=None):
    if it["submitted"]:
        return f"✅ SUBMITTED: {it['code']} — {it['name']}"
    pts = f" ({it['pts']:g} pts)" if it["pts"] else ""
    # 📌 OVERDUE: is in the school-check §2a vocabulary. It keeps the 📌 so the
    # 6am iPhone Shortcut still surfaces it -- an overdue item is exactly the
    # thing that must not drop out of the morning notification.
    if it.get("due_unknown"):
        # NOT a deadline, and the title must not imply one. The block sits on the
        # course's next meeting because that is when it could plausibly be
        # collected -- INTRCS1's Week 2 Performance Task (30 pts) sat at
        # due:null for a week, invisible to the calendar by construction.
        return f"📌 NO DUE DATE: {it['code']} — {it['name']}{pts}"
    if is_overdue(it, now):
        return f"📌 OVERDUE: {it['code']} — {it['name']}{pts}"
    return f"📌 DUE: {it['code']} — {it['name']}{pts}"


def fmt(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MNL).strftime(FMT)


# Bars for adopting an event that is NOT on the item's due date. See _best_adopt.
ADOPT_OFFDAY_TOKENS = 3
ADOPT_OFFDAY_DAYS = 60


def _best_adopt(it, events, claimed):
    """Best unmarked candidate for this item, or None.

    Deliberately strict. A wrong adoption silently rewrites an unrelated event,
    which is worse than proposing a duplicate a human can see and reject.
    Requires: managed title, same course code, and >=2 shared title tokens.

    🔴 THE SAME-DAY RULE USED TO BE ABSOLUTE, AND THAT WAS THE SECOND DEFECT
    (fixed 2026-09-06). A CATCH-UP BLOCK IS DELIBERATELY ON ANOTHER DAY -- it is
    scheduled for when the work will actually be done, which is precisely NOT
    the deadline it already missed. So the one class of event most worth
    adopting could never match, `diff` saw no event, and proposed a duplicate
    CREATE on the dead stamp. Observed on `PROGIT1 Unit 3: Practice Activities
    (Continuation)`: due Wed 19 Aug, block placed Sun 06 Sep, duplicate proposed
    on every run. Ralph's workaround was to keep the marker OFF the event, which
    left it unreconciled forever -- a worse outcome than the duplicate.

    Off-day adoption therefore requires a HIGHER bar: the event must sit AFTER
    the due date (a catch-up always does), share >=3 title tokens, and fall
    within 60 days. Three is not arbitrary: sibling columns are the realistic
    mis-adoption risk, and they overlap on exactly two. 'Quiz 1-Lesson1.1' vs
    'Quiz 2-Lesson1.2' shares {quiz, lesson1} and stops here; the real match
    above shares five. Same-day keeps the old, looser bar -- the day itself is
    already a strong discriminator, and that path has run clean since 08-19.
    """
    due = _inst(it["due"])
    best, best_score = None, float("-inf")
    for e in events:
        if e["id"] in claimed or marker_of(e) or not is_managed_title(e["summary"]):
            continue
        # 🔴 A HELD EVENT IS NEVER AN ADOPTION TARGET (added 2026-09-10).
        # `human_done()` gated the WRITE path only, so a person's closed event
        # could still be seized by an unrelated column and have a marker
        # stapled to it -- after which every later run "legitimately" retimes
        # and recolours it. Observed the day this was added: `✅ DONE: PROGIT1
        # — Emailed Instructor A re FG Lab Quiz 1` was adopted by `_9000_1`, a
        # different quiz. Stripping the marker by hand did not help, because
        # the next run adopted it again.
        # 🔑 DONE IS TERMINAL means terminal for ADOPTION TOO, not only for
        # retiming. This is the cheap half of the fix; the root cause was the
        # lookback window (see cal_apply.LOOKBACK_DAYS).
        if human_done(e):
            continue
        if it["code"].lower() not in e["summary"].lower():
            continue
        overlap = len(norm(e["summary"]) & norm(it["name"]))
        if overlap < 2:
            continue
        est = _inst(e["start"] or "")
        if not (est and due):
            continue
        days = abs((est - due).total_seconds()) / 86400.0
        if est.astimezone(MNL).date() != due.astimezone(MNL).date():
            # 🔴 FORWARD ONLY. A catch-up block is always placed AFTER the
            # deadline it missed -- that is what makes it a catch-up. An event
            # sitting BEFORE the due date on a different day is the opposite
            # thing: a same-titled column from another week, which is a
            # different deadline, not this one. Recurring weekly self-checks
            # ('Lesson1.2-Self-Check', 'Lesson1.3-Self-Check') are the exact
            # shape at risk, and this is the test that separates them.
            if est < due or overlap < ADOPT_OFFDAY_TOKENS or days > ADOPT_OFFDAY_DAYS:
                continue
        # Rank by title agreement first, distance only as a tie-break. The old
        # formula subtracted HOURS from overlap*10, which went negative past a
        # day and a half -- harmless while every candidate was same-day, fatal
        # the moment one is not, because `best_score` started at 0 and every
        # off-day candidate scored below it.
        score = overlap * 100 - days
        if score > best_score:
            best, best_score = e, score
    return best


def window_guard(items, events):
    """Warnings for items whose marked event may lie OUTSIDE the loaded window.

    🔴 THE FAILURE THIS MAKES VISIBLE (2026-09-10). The planner cannot tell
    "this column has no event" from "this column's event was not loaded". Both
    look like an empty `by_marker` bucket, and the second one silently turns
    into a wrong ADOPT or a duplicate CREATE. The root cause was a 7-day
    lookback (see cal_apply.LOOKBACK_DAYS, now 150) -- but ANY finite window has
    an edge, so widening it removes today's trigger without removing the class.

    This is the tripwire. If an item's due date sits before the oldest event we
    were given, the window is too narrow FOR THAT ITEM and any adopt/create
    decision about it is untrustworthy. Say so, loudly, naming the item.

    🔑 It warns rather than blocks, on purpose. Blocking would strand a genuinely
    new item that merely happens to be old, and a silent tool that does nothing
    is the failure mode this whole file is built against. A named warning in the
    dry run is something a human can act on.
    """
    starts = [_inst(e["start"] or "") for e in events]
    starts = [d for d in starts if d]
    if not starts:
        return []
    oldest = min(starts)
    out = []
    for it in items:
        due = _inst(it["due"])
        if due and due < oldest:
            out.append(f"{it['code']} — {it['name']}: due {fmt(it['due'])}, "
                       f"but the oldest event loaded starts {oldest.astimezone(MNL):%a %d %b %I:%M %p}")
    return out


def diff(items, events, all_keys=None, blind=None):
    """Pure function -- the whole point. No I/O, fully testable offline."""
    actions = []
    # Drop work tickets before ANY matching happens. Filtering here rather than
    # at each decision means a new action type cannot forget the rule.
    #
    # Trade-off, deliberately chosen: if a work event somehow carried a `bb:`
    # marker its school item would look unmatched and get a duplicate CREATE.
    # A visible duplicate the human can delete beats silently retiming one of
    # his client commitments.
    events = [e for e in events if not is_work_event(e)]
    by_marker = {}
    for e in events:
        k = marker_of(e)
        if k:
            by_marker.setdefault(k, []).append(e)

    # Past deadlines are HISTORY. Re-timing them serves nothing, and a deliberate
    # catch-up block scheduled after a missed deadline is correct, not drifted.
    # Only reconcile what can still be acted on.
    now = datetime.now(timezone.utc)
    # Keys for EVERY item Blackboard returned, including the ones filtered out
    # below. Without this, filtering an item makes its marked event look like an
    # orphan and it gets proposed for deletion -- the filter would manufacture
    # exactly the destructive suggestion this tool exists to avoid.
    # EVERY column Blackboard returned, including due:null ones. Deriving this
    # from `items` alone was the 2026-08-22 bug: `items` is due-date-only, so a
    # cleared due date read as a deleted item. Callers that cannot supply the
    # full set fall back to the old, narrower behaviour.
    known = all_keys if all_keys is not None else {(it["cid"], it["colid"]) for it in items}
    blind = blind or set()

    # 🔴 THE 12-HOUR CLIFF IS GONE (2026-08-27). It used to read:
    #     items = [it for it in items if (due or now) > now - timedelta(hours=12)]
    # on the theory that "past deadlines are HISTORY". In practice that made the
    # two items most at risk INVISIBLE to the calendar: a 100-pt MATHMW1 column
    # and a 10-pt PROGIT1 column, both unstarted and both days past their stamp,
    # had to be added BY HAND on 2026-08-27. Overdue work is not history -- it is
    # the work most likely to be lost. Overdue items now stay in `items`, get
    # 📌 OVERDUE: + tomato, and (below) are never re-timed onto their dead stamp.
    #
    # Submitted past work is still dropped: an event for last week's finished
    # work is noise, and noise is what stops him reading the alerts.
    #
    # ⚠ A submitted due:null item is deliberately NOT dropped here, even though
    # its next_meeting() stand-in means this past-test never fires for it:
    # dropping it would also stop its 📌 event ever flipping to ✅. The damage
    # that stand-in used to do -- retiming finished work -- is closed at the
    # retime itself instead (see `submitted` in the end-drift test below).
    twins = satisfied_twins(items)
    items = [it for it in items
             if not (it["submitted"] and (_inst(it["due"]) or now) <= now - timedelta(hours=12))]

    seen = set()
    # An unmarked event may be adopted by AT MOST ONE item. Without this the
    # matcher happily assigned one PROGIT1 event to three different columns --
    # caught only by running it against the real calendar, never by unit tests.
    claimed = set()
    for it in items:
        key = (it["cid"], it["colid"])
        seen.add(key)
        overdue = is_overdue(it, now)
        want_title, want_start = title_for(it, now), it["due"]
        want_color = (COLOR_DONE if it["submitted"]
                      else COLOR_OVERDUE if overdue
                      else COLOR_DUE)
        matches = by_marker.get(key, [])

        # 🔴 TWIN COLUMNS (2026-09-27). INTRCS1 carries two identical 'Learning
        # and Discovery Report' columns -- same name, points and due -- one
        # Completed, one attempts=[]. The empty twin raised an OVERDUE CREATE on
        # every run for work Ralph had handed in. Flag it, never alarm it; and
        # touch nothing, not even an event it already has.
        if key in twins:
            actions.append({"action": "TWIN", "summary": f"{it['code']} — {it['name']}",
                            "why": f"identical to {twins[key]}, which is submitted — "
                                   "flagged, not alarmed"})
            continue

        if not matches:
            best = _best_adopt(it, events, claimed)
            if best:
                claimed.add(best["id"])
                actions.append({"action": "ADOPT", "eventId": best["id"],
                                "marker": f"bb:{it['cid']}:{it['colid']}",
                                "why": "existing event has no marker — add it, do not duplicate",
                                "item": want_title, "current": best["summary"]})
            elif it["submitted"]:
                # Contract rule, and plain sense: never create an event for work
                # that is already in. A calendar full of ✅ for last week's work
                # is noise, and noise is what makes him stop reading alerts.
                continue
            else:
                act = {"action": "CREATE", "summary": want_title,
                       "start": want_start, "startLocal": fmt(want_start),
                       "colorId": want_color,
                       "marker": f"bb:{it['cid']}:{it['colid']}",
                       "why": "due date with no calendar event"}
                # 🔴 AN OVERDUE BLOCK IS A CATCH-UP SLOT, NOT A TOMBSTONE
                # (2026-09-27). CREATE used to place it on the stamp it already
                # missed -- in the past, where neither the 6am Shortcut nor a
                # forward glance ever shows it. Three events landed on Thu 24 Sep
                # that way. Place it ONCE on the course's next meeting; the
                # no-retime-when-overdue rule below means it never chases again.
                if overdue:
                    slot = next_meeting(it["code"], now)
                    act["was_due"] = fmt(it["due"])
                    if slot:
                        act.update(start=slot, startLocal=fmt(slot),
                                   why="overdue — placed on the next class meeting as a catch-up slot")
                    else:
                        act["why"] = ("overdue — course has no timetable entry, "
                                      "left on its missed stamp")
                actions.append(act)
            continue

        ev = matches[0]
        for extra in matches[1:]:
            actions.append({"action": "PROPOSE_DELETE", "eventId": extra["id"],
                            "summary": extra["summary"],
                            "why": "duplicate marker — never deleted automatically"})
        changes = []
        # Ralph's events are WORK BLOCKS that lead up to a deadline: they start
        # earlier and END at the due time. Comparing start to due would drag every
        # block forward onto its own deadline and destroy the run-up. Compare END.
        ref = ev.get("end") or ev["start"]
        # Sub-minute tolerance. Blackboard stores 11:59:59.999 PM where a human
        # writes 11:59 PM; treating that as drift would flag a correct calendar
        # forever, and an alert that always fires is an alert he stops reading.
        # 🔴 NEVER RE-TIME AN EVENT ONTO A DEAD DEADLINE. Once an item is overdue
        # its event is a CATCH-UP SLOT, chosen by a human or scheduled forward --
        # dragging it back to the stamp it already missed buries it in the past
        # where nothing will ever show it again.
        #
        # This replaces the old `deliberate = "OVERDUE" in summary` string test,
        # which only worked while OVERDUE was hand-written. Now that the title is
        # machine-set, that test would match every overdue event and the two
        # meanings would be indistinguishable. Key off the ITEM's state instead.
        # Colour still syncs below -- only the retiming is suppressed.
        #
        # The ORIGINAL string escape is kept as well, and both are needed. The
        # item-state test covers machine-set overdue; the summary test covers a
        # human catch-up block written against an item that is NOT yet overdue
        # (a deliberate early slot). Dropping either one re-times a block a
        # person placed on purpose.
        deliberate = "OVERDUE" in ev["summary"].upper()
        # A human closed this on evidence the gradebook does not carry. Nothing
        # below may overrule that -- not the time, not the title, not the colour.
        # 🔑 UNCONDITIONAL, and asymmetric with `deliberate` on purpose.
        # A hand-written OVERDUE is a PESSIMISTIC mark, so Blackboard confirming
        # a submission rightly overrides it. A hand-written DONE is an OPTIMISTIC
        # one, and Blackboard agreeing only ever CONFIRMS it -- rewriting it to
        # ✅ SUBMITTED then churns a person's title to change nothing a reader
        # cares about. Both are ✅ and both are sage; the words differ, the
        # meaning does not.
        held = human_done(ev)
        has_state = _prefix_of(ev["summary"])
        # 🔴 cal_sync OWNS ONLY THREE STATES (2026-09-27). A marked event titled
        # 📌 CONFIRM / EVENT / BUY / WRITE / READ / CANCEL is a person's call:
        # `_prefix_of` protected its TITLE, but its colour was still synced --
        # the HUMCOM1 `📌 CONFIRM:` (flamingo by contract) was about to go
        # tomato -- and a marked `📌 BUY:` would have been re-prefixed to DUE.
        # Same treatment as DONE: compute what we'd do, report it, write nothing.
        foreign = has_state not in OWNED_STATES and not held
        # 🔴 FINISHED WORK IS NEVER RETIMED (2026-09-27). Its time is history.
        # The live case: due dates CLEARED on submitted columns got a
        # next_meeting() stand-in, and five ✅ events were about to move onto it.
        if ref and not overdue and not deliberate and not held \
                and not it["submitted"] and _drift(ref, want_start) > 120:
            changes.append(("end", fmt(want_start), ref))
        # Do NOT overwrite the title text. Hand-written titles ("🔴 DUE 6:00 PM …
        # 1 ATTEMPT ONLY") are better than the raw Blackboard column name, and
        # rewriting them was a regression the first live run caught. Only the
        # PREFIX is contractual, because the 6am iPhone Shortcut filters on 📌.
        # Three prefix states now, not two. A DUE -> OVERDUE transition needs the
        # same rewrite a DUE -> SUBMITTED one does; comparing only the ✅ would
        # leave an item reading "📌 DUE" days after its stamp died.
        want_state = ("SUBMITTED" if it["submitted"]
                      else "OVERDUE" if overdue else "DUE")
        # `has_state is None` -> not a managed title, leave it entirely alone.
        # `deliberate` -> a human wrote OVERDUE on an item that is not overdue
        # yet (an early catch-up slot). Demoting that back to DUE would erase a
        # judgement call they made on purpose. Submission still overrides it:
        # once the work is IN, ✅ is the truth regardless of who wrote what.
        if (has_state is not None and want_state != has_state
                and not (deliberate and not it["submitted"])):
            fixed = _reprefix(ev["summary"], it["submitted"], overdue) or want_title
            changes.append(("summary", fixed, ev["summary"]))
        if (ev.get("colorId") or "") != want_color:
            changes.append(("colorId", want_color, ev.get("colorId") or "NONE"))
        if foreign:
            if changes:
                actions.append({"action": "HELD", "eventId": ev["id"],
                                "summary": ev["summary"],
                                "detail": [f"would have set {c[0]}: {c[2]!r} -> {c[1]!r}"
                                           for c in changes],
                                "why": (f"prefix {has_state or _word_of(ev['summary'])} is set by a "
                                        "person — cal_sync owns only "
                                        + "/".join(sorted(OWNED_STATES)))})
            changes = []
        if held:
            # Report WHAT was suppressed, not merely THAT something was. A bare
            # "held" is indistinguishable from the reconciler not noticing the
            # item, and "it printed nothing" is how a real drift gets normalised.
            # Emitted only when there was something to suppress: a held event
            # that already agrees with Blackboard is not worth a line.
            if changes:
                actions.append({"action": "HELD", "eventId": ev["id"],
                                "summary": ev["summary"],
                                "detail": [f"would have set {c[0]}: {c[2]!r} -> {c[1]!r}"
                                           for c in changes],
                                "why": ("a human marked this done; Blackboard reads "
                                        + ("submitted" if it["submitted"]
                                           else "attempts=[]")
                                        + " — deferring to the person")})
            changes = []
        if changes:
            actions.append({"action": "UPDATE", "eventId": ev["id"],
                            "summary": next((c[1] for c in changes if c[0] == "summary"),
                                            ev["summary"]),
                            "end": want_start, "startLocal": fmt(want_start),
                            "colorId": want_color,
                            "changes": [c[0] for c in changes],
                            "detail": [f"{c[0]}: {c[2]!r} -> {c[1]!r}" for c in changes],
                            "why": "submitted" if it["submitted"] else "drifted from Blackboard"})

    for e in events:
        k = marker_of(e)
        if k and k not in seen and k not in known:
            # A course we could not READ tells us nothing about its columns.
            # Proposing a delete here would be an alert generated by our own
            # network error — and noise is what trains him to stop reading them.
            if k[0] in blind:
                continue
            actions.append({"action": "PROPOSE_DELETE", "eventId": e["id"],
                            "summary": e["summary"],
                            "why": "Blackboard item gone — CONFIRM before removing"})
    return actions


# The only title states this tool may write or recolour. Everything else in the
# calendar skill's vocabulary (CONFIRM, EVENT, BUY, WRITE, READ, CANCEL, DONE)
# is set by a person and is reported, never rewritten. See `foreign` in diff().
OWNED_STATES = {"DUE", "OVERDUE", "SUBMITTED"}


def satisfied_twins(items, key=None, ident=None, submitted=None):
    """{ident(unsubmitted): ident(submitted twin)} for exact-duplicate columns.

    🔴 THE CASE (2026-09-27): INTRCS1 `_9001_1` and `_9002_1`, both
    '2627A_Learning and Discovery Report - To be pass every before exam', 20 pts,
    due Tue 15 Sep 6:00 PM. One Completed, one attempts=[]. The empty one is not
    owed work; it is the instructor's duplicate.

    A due:null column's next_meeting() stand-in is NOT a due date and never
    pairs -- two unrelated undated columns would otherwise share one.

    STRICT ON PURPOSE: same course, exact name, same due AND same points. Any
    difference -- a weekly re-post with a new due, a sibling quiz -- is a real
    item and stays one. A twin is FLAGGED by callers, never silently dropped.

    Parameterised so study_load applies this same rule to its own item shape
    rather than keeping a copy (a second copy is how rules drift).
    """
    key = key or (lambda i: (i["cid"], (i["name"] or "").strip(), i["due"], i["pts"]))
    ident = ident or (lambda i: (i["cid"], i["colid"]))
    submitted = submitted or (lambda i: bool(i["submitted"]))
    done = {}
    for i in items:
        if submitted(i) and i.get("due") and not i.get("due_unknown"):
            done.setdefault(key(i), ident(i)[1])
    return {ident(i): done[key(i)] for i in items
            if not submitted(i) and i.get("due") and not i.get("due_unknown")
            and key(i) in done
            and done[key(i)] != ident(i)[1]}


CANON_PFX = re.compile(r"^(?:📌|✅|🔴)\s*(SUBMITTED|OVERDUE|DUE|DONE|READ|BUY|WRITE)\b",
                       re.I)


def _word_of(summary):
    """The first capitalised word of a title ('CONFIRM', 'EVENT'), for messages."""
    m = re.search(r"\b([A-Z]{3,})\b", summary or "")
    return m.group(1) if m else "(none)"


def _prefix_of(summary):
    """The STATE WORD of a managed title ('DUE'/'OVERDUE'/'SUBMITTED'), or None.

    Deliberately ignores which emoji was used. '🔴 DUE 6:00 PM:' and
    '📌 DUE:' are the same state, and rewriting one into the other would
    destroy a hand-written title to change nothing a reader cares about.
    None means the title is not managed at all -- leave it entirely alone.
    """
    m = CANON_PFX.match(summary or "")
    return m.group(1).upper() if m else None


def _reprefix(summary, done, overdue=False):
    """Swap only the status prefix, preserving the human-written title text.

    🔴 Splitting on the FIRST colon corrupts hand-written titles that contain a
    time. '🔴 DUE 6:00 PM: HUMCOM1 — 2.14 …' cut at the colon inside '6:00' and
    produced '✅ SUBMITTED: 00 PM: HUMCOM1 — 2.14 …'. The old test only checked
    the prefix and a tail substring, so it passed while mangling the body.

    Every managed title is 'PREFIX: CODE — name', so the real separator is the
    LAST colon before the em dash. Fall back to the first colon when there is
    no em dash to anchor on.
    """
    head = summary.split("—", 1)[0] if "—" in summary else summary
    cut = head.rfind(":")
    body = (summary[cut + 1:] if cut != -1 else summary).strip()
    if not body or not re.match(r"^(📌|✅|🔴)", summary):
        return None
    if done:
        return f"✅ SUBMITTED: {body}"
    return f"📌 OVERDUE: {body}" if overdue else f"📌 DUE: {body}"


def _drift(a, b):
    """Seconds between two timestamps; inf if either is unparseable."""
    x, y = _inst(a), _inst(b)
    return abs((x - y).total_seconds()) if (x and y) else float("inf")


def _inst(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    # An all-day event's start is a bare date ("2026-09-30"), which parses
    # NAIVE -- and subtracting it from an aware due date crashed every
    # `apply` on 2026-09-28. All-day events live in Manila time.
    return dt if dt.tzinfo else dt.replace(tzinfo=MNL)


def main() -> int:
    args = [a for a in sys.argv[1:]]
    as_json = "--json" in args
    args = [a for a in args if not a.startswith("-")]
    if not args:
        print(__doc__.split("USAGE")[-1].strip(), file=sys.stderr)
        print("\n🔴 refusing to run without a calendar dump — assuming an empty "
              "calendar would propose duplicating every event.", file=sys.stderr)
        return 2
    events = load_events(args[0])
    courses = {a.upper() for a in args[1:]} or None

    items, unreadable, (all_keys, blind) = collect_blackboard(courses)
    if items is None:
        print(f"\n🔴 {unreadable}\n   Run  ./lms.sh login  and sign in.\n"
              "   Do NOT treat this as 'calendar is correct'.", file=sys.stderr)
        return 2

    actions = diff(items, events, all_keys, blind)
    STAMP.write_text(json.dumps({"last_verified": datetime.now(timezone.utc).isoformat(),
                                 "by": "cal", "items": len(items), "actions": len(actions)}))

    if as_json:
        print(json.dumps(actions, indent=1))
        return 0

    print(f"# calendar reconcile — {datetime.now(MNL):{FMT}} (Manila)")
    print(f"  {len(items)} Blackboard items with due dates · {len(events)} calendar events read")
    for it in items:
        if it["warn"]:
            print(f"  🔴 [{it['code']}] {it['name']!r} {it['warn']}")
    if unreadable:
        print(f"  ⚠ NOT fully read: {unreadable} — treat those courses as UNKNOWN")
    for w in window_guard(items, events):
        print(f"  🔴 WINDOW TOO NARROW — {w}\n"
              f"     Its marked event may exist but not be loaded, so any ADOPT or "
              f"CREATE below for it is untrustworthy. Widen cal_apply.LOOKBACK_DAYS.")
    if not actions:
        print("\n✅ calendar matches Blackboard — nothing to change")
        return 0
    print(f"\n{len(actions)} action(s). This tool does NOT write; apply these via MCP:\n")
    for a in actions:
        print(f"  [{a['action']}] {a.get('summary') or a.get('item')}")
        if a.get("startLocal"):
            print(f"        when : {a['startLocal']}")
        for d in a.get("detail", []):
            print(f"        {d}")
        if a.get("eventId"):
            print(f"        event: {a['eventId']}")
        if a.get("marker"):
            print(f"        add  : {a['marker']}")
        print(f"        why  : {a['why']}")
    if any(a["action"] == "PROPOSE_DELETE" for a in actions):
        print("\n⚠ PROPOSE_DELETE is never executed automatically. Confirm with Ralph.")
    if any(a["action"] == "TWIN" for a in actions):
        print("⚠ TWIN writes nothing. The column is an exact duplicate of one already "
              "submitted —\n  it is not counted overdue and gets no event.")
    if any(a["action"] == "HELD" for a in actions):
        print("⚠ HELD writes nothing. A person marked that event done and this "
              "tool defers to them —\n  clear the ✅ DONE prefix or the "
              "'human: done' description line to hand it back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
