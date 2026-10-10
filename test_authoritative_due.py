#!/usr/bin/env python3
"""Offline regression test for authoritative_due() -- no browser, no network.

Guards the STALE DUE DATE bug. On 2026-08-19 the gradebook column-LIST endpoint
reported HUMCOM1 2.14 (40 pts, ONE attempt) as due 11:59 PM for at least 40
minutes after the real deadline had been moved to 6:00 PM, and hid 2.12's move
to 26 Aug entirely. Three sweeps repeated the wrong time. Run this after ANY
change to how a due date is resolved.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bb_sweep import authoritative_due

STALE = "2026-08-19T15:59:00.000Z"   # what the LIST said   (11:59 PM Manila)
TRUE  = "2026-08-19T10:00:00.000Z"   # what the ITEM said    ( 6:00 PM Manila)


def fake_get(payload):
    return lambda url: payload


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

    # 1. THE REAL BUG: list is stale, item has the truth. Item must win, loudly.
    due, warn = authoritative_due(
        fake_get({"grading": {"due": TRUE}}), "_9003_1", "_9004_1", STALE)
    check("stale list -> item's date wins", due == TRUE, f"got {due!r}")
    check("stale list -> mismatch is REPORTED", warn is not None and "DUE MISMATCH" in warn)
    check("warning names both times",
          warn is not None and "11:59 PM" in warn and "06:00 PM" in warn, f"got {warn!r}")

    # 2. Agreement must stay SILENT -- a warning on every row trains him to ignore it.
    due, warn = authoritative_due(
        fake_get({"grading": {"due": STALE}}), "_9003_1", "_9004_1", STALE)
    check("list agrees -> no warning", warn is None, f"got {warn!r}")
    check("list agrees -> date preserved", due == STALE)

    # 3. An item with no due date must not invent one, and must not warn.
    due, warn = authoritative_due(
        fake_get({"grading": {}}), "_9005_1", "_9006_1", None)
    check("due:null stays null", due is None, f"got {due!r}")
    check("due:null does not warn", warn is None)

    # 4. FAIL LOUD on an unreadable column. Falling back to the cached value is
    #    acceptable; doing it SILENTLY is not -- that is how a wrong date ships.
    due, warn = authoritative_due(
        fake_get({"_status": 403}), "_9003_1", "_9004_1", STALE)
    check("403 -> falls back to list value", due == STALE)
    check("403 -> says the value is cached/unverified",
          warn is not None and "LIST-CACHED" in warn, f"got {warn!r}")

    # 5. The list reporting NO due date while the item HAS one is the silent
    #    variant of this bug -- an item invisible to every 14-day window.
    due, warn = authoritative_due(
        fake_get({"grading": {"due": TRUE}}), "_9003_1", "_9004_1", None)
    check("list says null, item has a date -> date wins", due == TRUE)
    check("list says null, item has a date -> warns", warn is not None, f"got {warn!r}")

    # NEGATIVE CONTROL: prove these assertions can fail. If a knowingly wrong
    # expectation still 'passes', nothing above is actually being checked.
    due, _ = authoritative_due(
        fake_get({"grading": {"due": TRUE}}), "_9003_1", "_9004_1", STALE)
    if due == STALE:
        print("  FAIL  negative control: stale value survived the re-read")
        failed += 1
    else:
        print("  PASS  negative control: stale value never survives")

    print(f"\n{'🔴 ' + str(failed) + ' FAILED' if failed else '✅ ALL PASS'}"
          f"  ({ran} assertions + 1 control)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
