#!/usr/bin/env python3
"""Paint the recurring CLASS SCHEDULE graphite, so it stops competing with tickets.

Ralph's scheme (2026-08-27): the timetable is context, not a task. It should be
legible and dull. Deadlines are flamingo/tomato, done work is pistachio, work
tickets are lavender/grape -- and none of those read as urgent if the whole week
is already coloured.

The class series carry NO prefix (school-check §2a: "Classes carry no prefix and
no colour on purpose"), which is exactly what makes them identifiable here: an
event with a course code and no 📌/✅/🔴/💼 is a class.

    ./lms.sh recolor-classes            # dry run
    ./lms.sh recolor-classes --apply

Patches the RECURRING MASTER, not instances, so one call fixes the whole series
and every future occurrence.
"""
import sys

from cal_apply import CALENDAR_ID, service
from cal_sync import COLOR_CLASS, is_work_event
from school_config import CODES  # Nebula Phase 0: one home for shared constants

# Every course code that owns a recurring block. From school/schedule.md.

MANAGED = ("📌", "✅", "🔴", "💼")


def is_class_event(ev) -> bool:
    """A recurring timetable block, as opposed to a ticket about a course."""
    s = ev.get("summary") or ""
    if not s or s.startswith(MANAGED) or is_work_event(ev):
        return False
    if not ev.get("recurringEventId") and ev.get("status") != "confirmed":
        return False
    return any(s.startswith(c) for c in CODES)


def main():
    write = "--apply" in sys.argv
    svc = service()
    ev = svc.events()

    # One week is enough to see every series at least once; recurrence does the
    # rest. Widening this would just re-list the same masters.
    page, seen, out = None, {}, []
    while True:
        r = ev.list(calendarId=CALENDAR_ID, timeMin="2026-08-31T00:00:00+08:00",
                    timeMax="2026-09-06T23:59:59+08:00", singleEvents=True,
                    maxResults=250, pageToken=page).execute()
        for e in r.get("items", []):
            if not is_class_event(e):
                continue
            master = e.get("recurringEventId") or e["id"]
            if master in seen:
                continue
            seen[master] = e
            if (e.get("colorId") or "") != COLOR_CLASS:
                out.append((master, e.get("summary"), e.get("colorId") or "NONE"))
        page = r.get("nextPageToken")
        if not page:
            break

    print(f"# class recolour -> graphite ({COLOR_CLASS})"
          f"   [{'WRITING' if write else 'DRY RUN'}]")
    print(f"  {len(seen)} class series found, {len(out)} need recolouring\n")
    for master, summary, cur in out:
        print(f"  [{'SET' if write else 'would set'}] {summary}   {cur} -> {COLOR_CLASS}")
        if write:
            ev.patch(calendarId=CALENDAR_ID, eventId=master,
                     body={"colorId": COLOR_CLASS}).execute()
    if not out:
        print("  nothing to do — every class series is already graphite.")
    elif not write:
        print("\n⚠ DRY RUN — nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
