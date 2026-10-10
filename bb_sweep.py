#!/usr/bin/env python3
"""Full ATTEMPT-LEVEL sweep of every gradebook column, with NO due-date filter.

WHY THIS EXISTS (2026-08-19)
lms_digest.py only prints items whose due date falls inside a 14-day window.
That is the right shape for a daily brief and the WRONG shape for the question
"is there anything else outstanding?", because several instructors file work
with no due date at all:

    INTRCS1  Attendance                    due:null
    INTRCS1  Week 1 Task Tracker           due:null   <- 100 points
    INTRCS1  Week 2 Activity               due:null   <- announced verbally in class

None of those can ever appear in the digest. On 2026-08-17 the Week 2 Activity
was due that same day and was invisible to every automated check; the only
reason it was caught is that Ralph had heard her say it out loud. So:

    🔴 "Nothing in the digest" is NOT "nothing outstanding."
       Run this before ever answering "you're clear."

Every column is read at the ATTEMPT level (…/gradebook/columns/<id>/attempts),
never at the column level -- the column flips to NeedsGrading the moment an
attempt EXISTS, submitted or not, and printed a green tick for a 30-point test
that was still InProgress on 2026-08-17. Same bug, same day, twice.

NOTE the endpoint quirk that caused that bug to survive a whole day: /attempts
403s only when you pass ?userId=<uid>. Query it BARE and it works fine.

Usage:
    ./lms.sh sweep                      every enrolled course
    ./lms.sh sweep INTRCS1 PROGIT1      just these
    ./lms.sh sweep --contents LITERA1   walk the content tree instead

--contents answers a different question: "did the instructor post something new
that has no gradebook column yet?" It prints availability and adaptive-release
windows, which is how you tell a genuinely-open late submission from a locked
one.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bb_session import SessionDead, open_session

# Written ONLY after a pull that actually authenticated. Silence is not proof of
# calm: the cron reads school mail and cannot see Blackboard at all, so "no
# notification" must never be mistaken for "nothing changed".
STAMP = Path.home() / ".local/share/ub-lms-lastverified.json"
BASE = "https://blackboard.ubaguio.edu/learn/api/public/v1"
MNL = timezone(timedelta(hours=8))

# am/pm everywhere. Ralph reads 24-hour time as a conversion tax on every glance.
FMT = "%a %d %b %I:%M %p"


def mnl(iso, empty="due:null"):
    if not iso:
        return empty
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MNL).strftime(FMT)


# 🔴 The gradebook COLUMN-LIST endpoint (/gradebook/columns?limit=N) serves a CACHED
# grading.due and will happily report a date the instructor already changed. On
# 2026-08-19 it reported HUMCOM1 2.14 (40 pts, ONE attempt) as due 11:59 PM for at
# least 40 minutes after the real deadline had been moved to 6:00 PM, and it hid
# 2.12's move to 26 Aug entirely. Three separate sweeps repeated the wrong time.
#
# The per-column endpoint (/gradebook/columns/{colId}) is authoritative. Always
# resolve a due date through here, and SAY SO OUT LOUD when the two disagree --
# the mismatch line is the canary for this whole class of bug.
def authoritative_due(get, cid, colid, list_due):
    """Return (due_iso, warning_or_None). Falls back to the list value on error."""
    one = get(f"{BASE}/courses/{cid}/gradebook/columns/{colid}")
    if one.get("_status"):
        # Fail toward the truth we can still see, but never silently.
        return list_due, f"⚠ could not re-read column (status {one['_status']}) — due date is LIST-CACHED, may be stale"
    real = (one.get("grading") or {}).get("due")
    if real != list_due:
        return real, f"⚠ DUE MISMATCH — list said {mnl(list_due)}, item says {mnl(real)} (item wins)"
    return real, None


def main() -> int:
    args = list(sys.argv[1:])
    contents_mode = "--contents" in args
    outstanding_only = "--outstanding" in args
    # --source=cdp|profile pins a transport; default tries both (see bb_session).
    prefer = next((a.split("=", 1)[1] for a in args if a.startswith("--source=")), None)
    want = [a.upper() for a in args if not a.startswith("-")] or None

    # The session comes from whichever transport is actually authenticated --
    # Ralph's real Comet browser over CDP, or the saved scraper profile.
    # FAIL LOUD when none is: an empty sweep and a dead session look identical,
    # and that is exactly how a deadline gets missed.
    try:
        with open_session(prefer=prefer) as sess:
            return sweep(sess, contents_mode, want, outstanding_only)
    except SessionDead as e:
        print(e.report())
        return 2


def sweep(sess, contents_mode, want, outstanding_only=False) -> int:
    """Full attempt-level sweep.

    outstanding_only: print ONLY the unstarted items, as a finished list.

    🔴 WHY THAT MODE EXISTS (2026-08-23). The fallback agent was asked to read the
    full sweep and report every ⬜ line. Across three runs of the same prompt it
    returned 4 items, then 5, then 6 — dropping a 30-point UNSUBMITTED task twice.
    A stochastic model asked to FILTER a long list will silently drop rows, and a
    dropped row here is a missed deadline. So the filtering moved into Python and
    the model is left with nothing to decide: it runs one command and repeats the
    output. Same principle as everything else in this directory — the tools
    decide, the model reports.
    """
    get = sess.get
    uid = sess.uid
    unstarted = []

    if outstanding_only:
        print(f"# OUTSTANDING — unstarted work, {datetime.now(MNL):%a %d %b %Y %I:%M %p} (Manila)")
        print(f"  source: {sess.source}")
    else:
        print(f"# Blackboard sweep — {datetime.now(MNL):%a %d %b %Y %I:%M %p} (Manila)")
        print(f"  source: {sess.source}  "
              f"({'real Comet browser over CDP' if sess.source == 'cdp' else 'saved scraper profile'})")
        print(f"  mode: {'CONTENT WALK' if contents_mode else 'ATTEMPT-LEVEL, no due-date filter'}")

    swept, skipped = [], []
    memb = get(f"{BASE}/users/{uid}/courses?limit=100")
    if memb.get("_status"):
        # Authenticated but the membership call failed -- say UNKNOWN, not "clear".
        print(f"\n🔴 course list unreadable (status {memb['_status']}) — "
              f"treat EVERY course as UNKNOWN, not 'nothing due'.")
        return 2
    for m in memb.get("results", []):
        cid = m.get("courseId")
        c = get(f"{BASE}/courses/{cid}")
        if c.get("_status"):
            skipped.append(f"{cid} (course fetch {c['_status']})")
            continue
        code = c.get("courseId") or ""
        name = c.get("name") or ""
        if want and not any(w in code.upper() or w in name.upper() for w in want):
            continue
        swept.append(code)
        if not outstanding_only:
            print(f"\n===== {code} | {name}  ({cid})")

        if contents_mode:
            walk(get, cid)
            continue

        gb = get(f"{BASE}/courses/{cid}/gradebook/columns?limit=200")
        if gb.get("_status"):
            # Loud in BOTH modes: an unreadable course is not an empty one.
            print(f"  🔴 [{code.split('_')[-1]}] columns unreadable "
                  f"(status {gb['_status']}) — treat as UNKNOWN, not 'nothing due'")
            skipped.append(f"{code} columns ({gb['_status']})")
            continue
        for g in gb.get("results", []):
            colid = g.get("id")
            att = get(f"{BASE}/courses/{cid}/gradebook/columns/{colid}/attempts")
            if att.get("_status"):
                states = f"🔴 ERR{att['_status']}"
            else:
                states = [a.get("status") for a in att.get("results", [])]
            score = g.get("score")
            pts = score.get("possible") if isinstance(score, dict) else None
            real_due, warn = authoritative_due(
                get, cid, colid, (g.get("grading") or {}).get("due"))
            due = mnl(real_due)
            # A hand-marked quiz has no attempts but a Graded record (2026-09-14).
            # Only consulted when there are no attempts; see lms_digest.hand_graded.
            graded = None
            if not states:
                from lms_digest import hand_graded
                graded = hand_graded(get(f"{BASE}/courses/{cid}/gradebook/columns/"
                                         f"{colid}/users/{sess.uid}"))
                if graded:
                    states = [graded]
            mark = "  " if states else "⬜"
            if not states:
                # Compute OVERDUE here, not in the reader's head. A model asked to
                # compare six dates against the clock got the count wrong on
                # 2026-08-23 (said 2,真 3) and under-flagged a 20-point item.
                # Anything derived is something that can be derived wrongly.
                overdue = False
                if real_due:
                    try:
                        overdue = datetime.fromisoformat(
                            real_due.replace("Z", "+00:00")) < datetime.now(timezone.utc)
                    except ValueError:
                        overdue = False
                # Carry the ISO date too: sorting on the FORMATTED string sorts
                # "Fri 21 Aug" before "Thu 20 Aug" alphabetically, which is not an
                # order at all once there are more than a handful of items.
                unstarted.append((code.split("_")[-1], g.get("name") or "",
                                  pts, due, warn, overdue, real_due or ""))
            if not outstanding_only:
                print(f"  {mark}[{colid}] {g.get('name')!r}  pts={pts}  {due}  attempts={states}")
                if warn:
                    print(f"       🔴 {warn}")

    import json as _json
    STAMP.write_text(_json.dumps({"last_verified": datetime.now(timezone.utc).isoformat(),
                                  "by": f"sweep:{sess.source}", "courses": swept}))
    if outstanding_only:
        # The finished answer. Nothing here is left for a reader to filter.
        if unstarted:
            n_over = sum(1 for u in unstarted if u[5])
            print(f"\n{len(unstarted)} OUTSTANDING ITEM(S) — no attempt exists for any of these."
                  f"  {n_over} OVERDUE.\n")
            # Overdue first, then by due date: the order a reader should act in.
            for code, nm, pts, due, warn, overdue, iso in sorted(
                    unstarted, key=lambda u: (not u[5], u[3] == "due:null", u[6])):
                pt = f"{pts:g} pts" if pts else "ungraded"
                print(f"  {'🔴 OVERDUE ' if overdue else '   '}[{code}] {nm}")
                when = ("NO DUE DATE SET — still owed" if due == "due:null" else f"due {due}")
                print(f"        {pt} · {when}")
                if warn:
                    print(f"        🔴 {warn}")
            if n_over:
                print("\n  ⚠ Whether late work is still accepted is NOT in this output — ask the "
                      "instructor.")
        else:
            print("\n✅ 0 outstanding items — every gradebook column has an attempt.")
        if skipped:
            print(f"\n🔴 NOT FULLY READ — these are UNKNOWN, not 'nothing due':")
            for sk in skipped:
                print(f"   • {sk}")
        print(f"\nCOURSES READ: {len(swept)} — {', '.join(c.split('_')[-1] for c in swept)}")
        print("⚠ Blackboard only. PROGIT1 and ARTAPP1 announce on Google Chat — run ./lms.sh chat.")
        return 0
    print(f"\nSWEPT: {', '.join(swept) if swept else 'NONE'}")
    if skipped:
        print(f"NOT SWEPT: {', '.join(skipped)}")
    print("⚠ Blackboard only. PROGIT1 and ARTAPP1 announce on Google Chat — run ./lms.sh chat.")
    return 0


def walk(get, cid, parent=None, depth=0, emit=True, out=None):
    """Walk the course content tree. Returns a flat list of every item found.

    🔴 THIS IS THE ONLY PROBE THAT SEES WORK WITH NO GRADEBOOK COLUMN, and until
    2026-08-27 it RETURNED NOTHING — it printed and was called by nothing but a
    manual `--contents` run, so its findings could not feed the sweep, the
    digest, the calendar or anything else. That is how a 20-pt NSTPRO1 Google
    Form (announced, never a column) and a CMPTRB1 assessment (submitted, never
    a column) stayed invisible to every automated check.

    `emit=False` collects silently, for callers that want the data rather than
    the printout. `out` accumulates across the recursion; callers pass nothing.

    The returned dicts carry `_status` on a failed read so a caller can tell an
    empty course from an unreadable one — silence must never look like "clean".
    """
    out = [] if out is None else out
    url = f"{BASE}/courses/{cid}/contents" + (f"/{parent}/children" if parent else "")
    d = get(url + "?limit=200")
    if d.get("_status"):
        out.append({"cid": cid, "_status": d["_status"], "parent": parent})
        return out
    for it in d.get("results", []):
        av = it.get("availability") or {}
        rel = av.get("adaptiveRelease") or {}
        out.append({
            "cid": cid, "id": it.get("id"), "title": it.get("title") or "",
            "handler": (it.get("contentHandler") or {}).get("id", ""),
            "created": it.get("created"), "available": av.get("available"),
            "window_start": rel.get("start"), "window_end": rel.get("end"),
            "depth": depth,
        })
        if emit:
            pad = "   " * depth
            print(f"{pad}- {it.get('title')!r} [{it.get('id')}] "
                  f"type={(it.get('contentHandler') or {}).get('id', '')} "
                  f"created={mnl(it.get('created'), '-')} avail={av.get('available')} "
                  f"window={mnl(rel.get('start'), '-')} → {mnl(rel.get('end'), '-')}")
        if it.get("hasChildren"):
            walk(get, cid, it.get("id"), depth + 1, emit=emit, out=out)
    return out


if __name__ == "__main__":
    sys.exit(main())
