#!/usr/bin/env python3
"""Offline tests for the calendar EXECUTOR -- no browser, no network, no token.

cal_sync.py decides; cal_apply.py executes. This suite guards the execution
layer, and the assertion it exists for is the destructive one: a PROPOSE_DELETE
must never reach the API. The fake service below raises on `delete()`, so a
regression that starts deleting fails here rather than on Ralph's real calendar.

Also guards the two ways an executor quietly destroys data:
  - UPDATE must PATCH, never replace -- a full update drops the hand-written
    description (the prep notes: what to bring, how many attempts), which is the
    part of the event the LMS cannot regenerate.
  - ADOPT must touch the description ONLY -- the title is Ralph's.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cal_apply import _block, _with_marker, apply_actions, BLOCK_MINUTES, REMINDERS

MK = "bb:_9003_1:_9004_1"
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


class Calls:
    """Records API calls instead of making them. delete() is a tripwire."""

    def __init__(self, existing=None):
        self.log = []
        self.existing = existing or {}

    def events(self):
        return self

    def insert(self, **kw):
        self.log.append(("insert", kw))
        return self

    def patch(self, **kw):
        self.log.append(("patch", kw))
        return self

    def update(self, **kw):
        raise AssertionError("update() called — must PATCH, a full update drops "
                             "the hand-written description")

    def delete(self, **kw):
        raise AssertionError("delete() called — PROPOSE_DELETE must never execute")

    def get(self, **kw):
        self.log.append(("get", kw))
        self.existing_returned = self.existing
        return self

    def execute(self):
        return self.existing

    def kinds(self):
        return [k for k, _ in self.log]

    def bodies(self, kind):
        return [kw["body"] for k, kw in self.log if k == kind]


def create(due):
    return {"action": "CREATE", "summary": "📌 DUE: HUMCOM1 — 2.14 (40 pts)",
            "start": due, "startLocal": "Tue 6:00 pm", "colorId": "11",
            "marker": MK, "why": "due date with no calendar event"}


def update(**kw):
    a = {"action": "UPDATE", "eventId": "ev1", "summary": "📌 DUE: HUMCOM1 — 2.14",
         "end": "2026-08-26T10:00:00.000Z", "startLocal": "Wed 6:00 pm",
         "colorId": "11", "changes": ["end"], "detail": [], "why": "drifted"}
    a.update(kw)
    return a


def main():
    global failed
    due = "2026-08-26T10:00:00.000Z"
    print("cal_apply — execution layer\n")

    # 1. A new event is a work BLOCK that ENDS at the deadline (school-check
    #    §2a). Getting this backwards puts the block after the due time.
    s, e = _block(due)
    check("new block ENDS at the due time",
          datetime.fromisoformat(e) == datetime.fromisoformat(due.replace("Z", "+00:00")))
    check(f"new block starts {BLOCK_MINUTES} min earlier",
          (datetime.fromisoformat(e) - datetime.fromisoformat(s)).total_seconds() / 60
          == BLOCK_MINUTES)

    # 2. The marker is identity. It must survive on its own line, and adding it
    #    twice must not duplicate it -- ADOPT can legitimately run again.
    check("marker lands on its own line", _with_marker("prep: bring bond paper", MK)
          .splitlines()[-1] == MK)
    check("existing description is preserved",
          "bring bond paper" in _with_marker("prep: bring bond paper", MK))
    check("marker is idempotent",
          _with_marker(_with_marker("notes", MK), MK).count(MK) == 1,
          _with_marker(_with_marker("notes", MK), MK))

    # 3. 🔴 THE ONE THAT MATTERS. delete() raises; reaching it fails the suite.
    c = Calls()
    apply_actions([{"action": "PROPOSE_DELETE", "eventId": "ev9",
                    "summary": "✅ SUBMITTED: old", "why": "gone"}],
                  c, write=True, log=lambda *_: None)
    check("PROPOSE_DELETE issues NO api call at all", c.log == [], str(c.log))

    # 3b. 2026-09-27: TWIN is print-only, exactly like HELD.
    #     Two properties: no API call, and it is SAID. An unknown action kind
    #     already falls through silently, so "no call" alone proves nothing --
    #     the printed line is what the TWIN branch adds (a silent twin is the
    #     declared-but-never-read failure again).
    c, said = Calls(), []
    apply_actions([{"action": "TWIN", "summary": "INTRCS1 — L&D Report",
                    "why": "identical to _9001_1, which is submitted"}],
                  c, write=True, log=said.append)
    check("TWIN issues NO api call at all", c.log == [], str(c.log))
    check("TWIN is printed, with its reason",
          any("[TWIN]" in l for l in said) and any("_9001_1" in l for l in said), str(said))

    # 3c. 2026-09-27: an overdue CREATE sits on a catch-up slot, so the REAL
    #     stamp must travel in the description or it is lost for good.
    c = Calls()
    apply_actions([dict(create(due), was_due="Thu 24 Sep 05:00 PM")],
                  c, write=True, log=lambda *_: None)
    body = (c.bodies("insert") or [{}])[0]
    desc = body.get("description", "")
    check("catch-up CREATE keeps the missed stamp in its description",
          "was due Thu 24 Sep 05:00 PM" in desc, desc)
    check("...with the marker still on its own last line",
          desc.splitlines()[-1] == MK if desc else False, desc)
    c = Calls()
    apply_actions([create(due)], c, write=True, log=lambda *_: None)
    body = (c.bodies("insert") or [{}])[0]
    check("control: an on-time CREATE's description is the marker alone",
          body.get("description") == MK, repr(body.get("description")))

    # 4. Dry run must be inert. This is the negative control for every write
    #    assertion below -- if write=False still called the API, none of them
    #    would mean anything.
    c = Calls()
    apply_actions([create(due), update()], c, write=False, log=lambda *_: None)
    check("dry run writes nothing", c.log == [], str(c.log))

    # 5. CREATE carries the full contract: colour, marker, reminders.
    c = Calls()
    apply_actions([create(due)], c, write=True, log=lambda *_: None)
    body = c.bodies("insert")[0]
    check("CREATE inserts exactly once", c.kinds() == ["insert"], str(c.kinds()))
    check("CREATE sets colorId 11", body["colorId"] == "11")
    check("CREATE embeds the marker", MK in body["description"])
    check("CREATE sets the 2-day + 8-hour reminders",
          body["reminders"] == {"useDefault": False, "overrides": REMINDERS})
    check("CREATE does not use default reminders",
          body["reminders"]["useDefault"] is False)

    # 6. UPDATE patches ONLY what changed. A colour-only change must not rewrite
    #    the title -- hand-written titles beat raw Blackboard column names.
    c = Calls()
    apply_actions([update(changes=["colorId"], colorId="2")], c, write=True,
                  log=lambda *_: None)
    body = c.bodies("patch")[0]
    check("UPDATE uses patch", c.kinds() == ["patch"], str(c.kinds()))
    check("colour-only UPDATE does not touch the title", "summary" not in body, str(body))
    check("colour-only UPDATE does not touch the times", "end" not in body, str(body))
    check("colour-only UPDATE does set the colour", body["colorId"] == "2")

    # 7. A time change MOVES THE WHOLE BLOCK. Until 2026-08-27 this patched only
    #    `end`, so an event whose start was days earlier stretched into a
    #    multi-day band -- a HUMCOM1 self-check re-dated a week forward became a
    #    7-day event on the live calendar and had to be repaired by hand.
    c = Calls()
    apply_actions([update(changes=["end"])], c, write=True, log=lambda *_: None)
    body = c.bodies("patch")[0]
    check("time UPDATE moves the end", "end" in body, str(body))
    check("time UPDATE moves the start too", "start" in body, str(body))
    _s = datetime.fromisoformat(body["start"]["dateTime"])
    _e = datetime.fromisoformat(body["end"]["dateTime"])
    check("time UPDATE keeps the block one BLOCK_MINUTES long",
          (_e - _s) == timedelta(minutes=BLOCK_MINUTES), f"{_s} -> {_e}")

    # 8. ADOPT adds the marker to an event a human made, and changes nothing else.
    c = Calls(existing={"summary": "🔴 DUE 6:00 PM — 1 ATTEMPT ONLY",
                        "description": "wear the purple shirt"})
    apply_actions([{"action": "ADOPT", "eventId": "ev1", "marker": MK,
                    "current": "🔴 DUE 6:00 PM — 1 ATTEMPT ONLY",
                    "item": "📌 DUE: HUMCOM1 — 2.14", "why": "no marker"}],
                  c, write=True, log=lambda *_: None)
    body = c.bodies("patch")[0]
    check("ADOPT patches only the description", list(body) == ["description"], str(body))
    check("ADOPT preserves the human's notes", "purple shirt" in body["description"])
    check("ADOPT adds the marker", MK in body["description"])

    # 9. An action shape this file does not know must be skipped loudly, never
    #    guessed at. Guessing is how an executor invents a write nobody decided.
    c = Calls()
    apply_actions([{"action": "SOMETHING_NEW"}], c, write=True, log=lambda *_: None)
    check("unknown action makes no api call", c.log == [], str(c.log))

    # NEGATIVE CONTROL: prove the harness can actually observe a write. If the
    # fake service silently swallowed calls, every assertion above would pass
    # against a completely inert executor.
    c = Calls()
    apply_actions([create(due)], c, write=True, log=lambda *_: None)
    if not c.log:
        print("  FAIL  negative control: a real write was not observed")
        failed += 1
    else:
        print("  PASS  negative control: a real write IS observed")

    print(f"\n{'🔴 ' + str(failed) + ' FAILED' if failed else '✅ ALL PASS'}"
          f"  ({ran} assertions + 1 control)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
