#!/usr/bin/env python3
"""WRITE the calendar reconcile that cal_sync.py computes -- with no agent in the loop.

WHY THIS EXISTS
---------------
`cal_sync.py` already decides everything. It diffs authoritative Blackboard due
dates against the calendar and prints a mechanical action list. What it could not
do was *execute*: its docstring says "Python cannot write the calendar today",
and it was right -- the only OAuth token on this machine carried Classroom
read-only scopes. So the writes were delegated to an agent applying the list
through MCP.

That made the calendar -- the surface Ralph actually runs his life off --
depend on Claude having usage left. On 2026-08-22, four days before a weekly
reset and already at 75%, that became the thing worth fixing.

This file adds the missing half: its own OAuth token with a calendar-events
scope, and an executor for the action list.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
🔴 **It makes NO decisions.** Every rule -- titles, colours, marker identity,
what counts as drift, the 12-hour past-deadline cutoff -- lives in `cal_sync.py`
and is imported from it. Nothing here re-derives a convention. A second copy of
the calendar contract is exactly how `colorId` went missing from three events on
2026-08-19, and this file is a second executor, never a second rulebook.

🔴 **It never deletes.** `PROPOSE_DELETE` is printed and skipped, always, with no
flag to override. Same rule as `cal_sync.py`.

🔴 **It refuses to run on a dead Blackboard session** (exit 2). An empty action
list must never be producible by a failed pull -- that is indistinguishable from
"the calendar is correct", which is the failure this whole toolchain exists to
prevent.

WHAT IT CANNOT FIX -- state this, do not design around it
---------------------------------------------------------
The Blackboard SAML session lives ~5 hours and renewing it needs a human at a
Google form. **So this cannot be a scheduled poller.** It removes the *Claude*
dependency from the calendar write path, not the *Ralph* dependency. Run it when
a session is live; `./lms.sh check` says whether one is. Anything claiming to
poll Blackboard unattended is lying -- see `school/context/cron-school-mail-triage.md`.

USAGE
-----
    ./lms.sh cal-auth --url            one-time; open on the PHONE
    ./lms.sh cal-auth --code 4/0Ax...  one-time; exchanges and saves the token
    ./lms.sh cal-auth --status         is the token present and working?

    ./lms.sh apply                     DRY RUN -- fetch, diff, print. Writes nothing.
    ./lms.sh apply --apply             actually write CREATE / UPDATE / ADOPT
    ./lms.sh apply --apply MATHMW1     restrict to one or more course codes

Dry run is the default on purpose. A tool that writes to a live calendar when
invoked with no arguments is one typo away from a mess.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _quiet_oauth  # noqa: F401 — silence the scope-mismatch log line
import gcreds

CFG = Path.home() / ".config/ub-calendar"
CLIENT = CFG / "client_secret.json"
TOKEN = CFG / "token.json"

# 🔴 PKCE, and why this file exists on disk at all.
#
# google-auth-oauthlib generates a code_verifier when it builds the consent URL
# and sends only its hash to Google. The token exchange must present the SAME
# verifier. But this flow is deliberately split across two separate invocations
# (--url on RE, approve on the phone, --code back on RE), so `--code` builds a
# brand-new Flow with a brand-new verifier and Google rejects the exchange with
# "invalid_grant: Missing code verifier".
#
# Measured 2026-08-22 -- the first real consent failed exactly this way. So the
# verifier is persisted between the two steps and deleted once spent. Turning
# PKCE off instead would also work and is what most snippets do; keeping it is
# strictly better, and costs one short-lived 600 file.
VERIFIER = CFG / ".pkce_verifier"

# events, not `calendar` -- this may read and write events on a calendar Ralph
# already owns, and must not be able to create, share or delete a calendar.
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

# Same split-consent redirect as classroom_auth.py, for the same reason: the
# common case is Ralph on his phone with no way to click a window on RE. The
# phone cannot reach localhost, Google puts ?code= in the address bar anyway.
REDIRECT = "http://localhost:8765/"

CALENDAR_ID = "primary"

# How far back and ahead to read.
#
# 🔴 LOOKBACK WAS 7 DAYS AND THAT WAS A REAL DEFECT (fixed 2026-09-10).
# The old comment reasoned that cal_sync drops items more than 12h past, so a
# short lookback is harmless. THAT IS WRONG, and the reason is the marker, not
# the item: an OVERDUE item is NOT dropped -- it stays in `items` forever until
# it is submitted -- but the EVENT already carrying its `bb:` marker sits back
# on the original due date and ages out of the window. Once it does, `by_marker`
# has no entry, the item looks unmarked, and the planner proposes ADOPT on the
# nearest lookalike or CREATE of a duplicate.
#
# OBSERVED, 2026-09-10. `PROGIT1 FG Quiz 1 (Face to Face)` (_9000_1) is a paper
# quiz sat on Wed 03 Sep; its event runs 1:30-2:00 PM that day and holds the
# marker. At 11:23 AM on 10 Sep the window opened at 03 Sep 11:23 AM, the event
# was inside it, and the run correctly reported HELD. At 4:42 PM the SAME DAY
# the window opened at 03 Sep 4:42 PM -- two and a half hours past the event --
# so it vanished, the column looked unmarked, and the run adopted an unrelated
# event (`Emailed Instructor A re FG Lab Quiz 1`) that merely shared the tokens
# PROGIT1/FG/Quiz/1. Two events then claimed one column.
#
# 🔑 THE WINDOW MUST OUTLIVE THE OLDEST ITEM THE PLANNER STILL CARRIES, not the
# newest. An unsubmitted item from week one is still live in week twelve, so the
# lookback has to span the semester. 150 days covers a full term with room; the
# cost is reading a few hundred more events, which is one API page.
# `_window_guard()` in cal_sync.py fails loud if this is ever too small again.
LOOKBACK_DAYS, LOOKAHEAD_DAYS = 150, 45

# NOT a contract -- a choice, documented as one. cal_sync only knows the due
# time, and the contract says work blocks END at the deadline and start earlier
# (school-check SKILL.md, "Work-block events END at the deadline"). One hour is
# the smallest block that still reads as a block rather than a point.
BLOCK_MINUTES = 60

# school-check §2a: "Reminders: 2 days + 8 hours ahead for graded work."
REMINDERS = [{"method": "popup", "minutes": 2880},
             {"method": "popup", "minutes": 480}]


# --------------------------------------------------------------------- auth
def _flow():
    from google_auth_oauthlib.flow import Flow
    if not CLIENT.exists():
        sys.exit(
            f"🔴 No OAuth client at {CLIENT}\n"
            "   One-time Google Cloud Console setup is required and only Ralph can do it.\n"
            "   Walkthrough: school/context/calendar-api.md\n"
            "   Then load it with:\n"
            "     ./lms.sh cal-auth --init-client <CLIENT_ID> <CLIENT_SECRET>"
        )
    return Flow.from_client_secrets_file(str(CLIENT), scopes=SCOPES, redirect_uri=REDIRECT)


def credentials():
    """Live Credentials from the saved refresh token, or exit 2 with the fix."""
    # gcreds: $UB_CALENDAR_TOKEN first, then the file — so `apply` runs in a cloud
    # session too (2026-09-14). A broken or absent credential is still exit 2.
    try:
        return gcreds.credentials("calendar", default_scopes=SCOPES, quiet=True)
    except gcreds.NoCredential as e:
        print(f"🔴 {e}\n"
              "   Run  ./lms.sh cal-auth --url  and follow it on your phone.\n"
              "   Do NOT treat this as 'the calendar is correct'.", file=sys.stderr)
        raise SystemExit(2)


def service():
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=credentials(), cache_discovery=False)


def cmd_auth_url(_):
    f = _flow()
    url, _s = f.authorization_url(
        access_type="offline",   # no refresh token at all without this
        prompt="consent",        # force one even if already approved
        include_granted_scopes="true")
    CFG.mkdir(parents=True, exist_ok=True)
    VERIFIER.write_text(f.code_verifier or "")
    VERIFIER.chmod(0o600)
    print("\n1. Open this on your PHONE, signed in as you@gmail.com:")
    print("   (the PERSONAL account — the calendar lives there, not on the school one)\n")
    print(url)
    print("\n2. Approve. The browser will then FAIL to load a localhost page.")
    print("   That failure is expected and correct.")
    print("\n3. Copy the `code=` value out of the address bar and run:")
    print("   ./lms.sh cal-auth --code <THE_CODE>\n")


def cmd_auth_code(args):
    f = _flow()
    if VERIFIER.exists():
        f.code_verifier = VERIFIER.read_text().strip() or None
    else:
        print("⚠ No saved PKCE verifier — run ./lms.sh cal-auth --url first, and use\n"
              "  the code from THAT url. A code from an earlier url cannot be exchanged.",
              file=sys.stderr)
    f.fetch_token(code=args.code)
    c = f.credentials
    if not c.refresh_token:
        sys.exit("🔴 Google returned no refresh token. Re-run --url (it forces prompt=consent).")
    CFG.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps({
        "refresh_token": c.refresh_token, "client_id": c.client_id,
        "client_secret": c.client_secret, "token_uri": c.token_uri,
        "scopes": list(c.scopes or SCOPES)}, indent=2))
    TOKEN.chmod(0o600)
    VERIFIER.unlink(missing_ok=True)   # single-use; do not leave it lying around
    print(f"✅ Refresh token saved to {TOKEN} (mode 600, outside the vault).")
    print("   Verify it actually works:  ./lms.sh cal-auth --status")


def cmd_auth_init(args):
    cid, sec = args.init_client
    if not cid.endswith(".apps.googleusercontent.com"):
        sys.exit(f"🔴 That does not look like a client id: {cid[:40]}...\n"
                 "   It should end in .apps.googleusercontent.com")
    CFG.mkdir(parents=True, exist_ok=True)
    CLIENT.write_text(json.dumps({"installed": {
        "client_id": cid, "client_secret": sec,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT]}}, indent=2))
    CLIENT.chmod(0o600)
    print(f"✅ OAuth client saved to {CLIENT}. Next:  ./lms.sh cal-auth --url")


def cmd_auth_status(_):
    """Prove the token WORKS by making a real call, not by checking a file exists.

    The file test is the exact mistake lms.sh was built around (a browser that
    was downloaded but could not launch). A token on disk and a token Google
    still honours are different facts.
    """
    evs = fetch_events()
    print(f"✅ calendar token works — read {len(evs)} event(s) "
          f"from {CALENDAR_ID} over the next {LOOKAHEAD_DAYS} days.")


# -------------------------------------------------------------------- fetch
def fetch_events(svc=None):
    """Return events in cal_sync.load_events' shape, straight from the API."""
    svc = svc or service()
    now = datetime.now(timezone.utc)
    out, page = [], None
    while True:
        r = svc.events().list(
            calendarId=CALENDAR_ID,
            timeMin=(now - timedelta(days=LOOKBACK_DAYS)).isoformat(),
            timeMax=(now + timedelta(days=LOOKAHEAD_DAYS)).isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=2500,
            pageToken=page).execute()
        for e in r.get("items", []):
            s, en = e.get("start") or {}, e.get("end") or {}
            out.append({"id": e.get("id"), "summary": e.get("summary") or "",
                        "description": e.get("description") or "",
                        "start": s.get("dateTime") or s.get("date"),
                        "end": en.get("dateTime") or en.get("date"),
                        "colorId": e.get("colorId")})
        page = r.get("nextPageToken")
        if not page:
            break
    return out


# -------------------------------------------------------------------- apply
def _block(due_iso):
    """(start, end) for a new work block ENDING at the deadline."""
    end = datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
    return (end - timedelta(minutes=BLOCK_MINUTES)).isoformat(), end.isoformat()


def _with_marker(desc, marker):
    """Append the identity marker on its own line, idempotently."""
    lines = [l for l in (desc or "").splitlines() if l.strip() != marker]
    lines.append(marker)
    return "\n".join(lines).strip()


def apply_actions(actions, svc, write, log=print):
    """Execute the action list. Returns a {action: count} tally.

    `write=False` prints exactly what it would do and touches nothing -- the
    two paths share this function so the dry run cannot drift from the real one.
    """
    tally = {}
    ev = svc.events()
    for a in actions:
        kind = a["action"]
        tally[kind] = tally.get(kind, 0) + 1

        if kind == "HELD":
            # A no-op BY DESIGN, printed loudly. cal_sync deferred to a human's
            # ✅ DONE over Blackboard's attempts=[]; saying so is the point --
            # a silent deferral is indistinguishable from not noticing the item.
            log(f"  [HELD]   {a.get('summary')!r} — left exactly as it is.")
            log(f"           why  : {a.get('why')}")
            continue

        if kind == "TWIN":
            # Print-only, like HELD: an exact duplicate of a submitted column is
            # flagged so it stays visible, and nothing is created or recoloured.
            log(f"  [TWIN]   {a.get('summary')!r} — no event, not overdue.")
            log(f"           why  : {a.get('why')}")
            continue

        if kind == "PROPOSE_DELETE":
            # Print the REASON, not just the refusal. A human deciding whether to
            # delete needs to know whether the item vanished or merely duplicated.
            log(f"  [skip] PROPOSE_DELETE {a.get('summary')!r} — never automatic.")
            log(f"         why  : {a.get('why')}")
            log(f"         event: {a.get('eventId')}")
            continue

        if kind == "CREATE":
            start, end = _block(a["start"])
            # An overdue block sits on a catch-up slot, not on its deadline, so
            # the real stamp must travel in the description or it is lost.
            desc = (f"was due {a['was_due']} — placed on the next class meeting "
                    "as a catch-up slot" if a.get("was_due") else "")
            body = {"summary": a["summary"],
                    "description": _with_marker(desc, a["marker"]),
                    "start": {"dateTime": start}, "end": {"dateTime": end},
                    "colorId": a["colorId"],
                    "reminders": {"useDefault": False, "overrides": REMINDERS}}
            log(f"  [CREATE] {a['summary']}  @ {a['startLocal']}"
                + (f"  (was due {a['was_due']})" if a.get("was_due") else ""))
            if write:
                ev.insert(calendarId=CALENDAR_ID, body=body).execute()

        elif kind == "UPDATE":
            # PATCH, not update: a full update would drop every field cal_sync
            # does not know about -- the description with the prep notes, the
            # location, the reminders. Those are the parts Ralph wrote by hand.
            body = {"colorId": a["colorId"]}
            if "summary" in a.get("changes", []):
                body["summary"] = a["summary"]
            if "end" in a.get("changes", []):
                end = datetime.fromisoformat(a["end"].replace("Z", "+00:00"))
                body["end"] = {"dateTime": end.isoformat()}
                # 🔴 MOVE THE WHOLE BLOCK, DO NOT STRETCH IT. Patching `end`
                # alone left `start` where it was, so an event whose start was
                # days earlier became a multi-DAY band across the calendar --
                # measured 2026-08-27, when a HUMCOM1 self-check re-dated a week
                # forward turned into a 7-day event and had to be repaired by
                # hand. cal_sync only ever knows the deadline, and the contract
                # (school-check §2a) is that a work block ENDS at it, so the
                # start is derivable: exactly BLOCK_MINUTES earlier.
                body["start"] = {"dateTime":
                                 (end - timedelta(minutes=BLOCK_MINUTES)).isoformat()}
            log(f"  [UPDATE] {a['summary']}  ({', '.join(a.get('changes', []))})")
            for d in a.get("detail", []):
                log(f"           {d}")
            if write:
                ev.patch(calendarId=CALENDAR_ID, eventId=a["eventId"], body=body).execute()

        elif kind == "ADOPT":
            # Marker only. The title is Ralph's and stays his -- a hand-written
            # "1 ATTEMPT ONLY" beats the raw Blackboard column name every time.
            log(f"  [ADOPT]  {a.get('current')!r}  += {a['marker']}")
            if write:
                cur = ev.get(calendarId=CALENDAR_ID, eventId=a["eventId"]).execute()
                ev.patch(calendarId=CALENDAR_ID, eventId=a["eventId"],
                         body={"description": _with_marker(cur.get("description"),
                                                           a["marker"])}).execute()
        else:
            log(f"  [skip] unknown action {kind!r} — refusing to guess")
    return tally


# --------------------------------------------------------------------- main
def run(courses, write):
    import cal_sync
    svc = service()

    events = fetch_events(svc)
    items, unreadable, (all_keys, blind) = cal_sync.collect_blackboard(courses)
    if items is None:
        print(f"\n🔴 {unreadable}\n   Run  ./lms.sh login  and sign in.\n"
              "   Do NOT treat this as 'calendar is correct'.", file=sys.stderr)
        return 2

    actions = cal_sync.diff(items, events, all_keys, blind)
    cal_sync.STAMP.write_text(json.dumps({
        "last_verified": datetime.now(timezone.utc).isoformat(),
        "by": "apply" if write else "apply-dry",
        "items": len(items), "actions": len(actions)}))

    stamp = datetime.now(cal_sync.MNL).strftime(cal_sync.FMT)
    print(f"# calendar apply — {stamp} (Manila)   [{'WRITING' if write else 'DRY RUN'}]")
    print(f"  {len(items)} Blackboard items with due dates · {len(events)} calendar events read")
    for it in items:
        if it["warn"]:
            print(f"  🔴 [{it['code']}] {it['name']!r} {it['warn']}")
    if unreadable:
        print(f"  ⚠ NOT fully read: {unreadable} — treat those courses as UNKNOWN")

    if not actions:
        print("\n✅ calendar matches Blackboard — nothing to change")
        return 0

    print(f"\n{len(actions)} action(s):\n")
    tally = apply_actions(actions, svc, write)
    print()
    for k, n in sorted(tally.items()):
        print(f"  {k:<15} {n}")
    if not write:
        print("\n⚠ DRY RUN — nothing was written. Re-run with --apply to execute.")
    if tally.get("PROPOSE_DELETE"):
        print("\n⚠ PROPOSE_DELETE is never executed automatically. Confirm with Ralph.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="cal-apply", add_help=True)
    p.add_argument("--url", action="store_true", help="print the consent URL")
    p.add_argument("--code", help="exchange an authorisation code for a token")
    p.add_argument("--init-client", nargs=2, metavar=("CLIENT_ID", "CLIENT_SECRET"))
    p.add_argument("--status", action="store_true", help="prove the token still works")
    p.add_argument("--export", action="store_true",
                   help="print the token as ONE line, for UB_CALENDAR_TOKEN in a cloud environment")
    p.add_argument("--apply", action="store_true",
                   help="actually write (default is a dry run)")
    p.add_argument("courses", nargs="*", help="restrict to these course codes")
    a = p.parse_args()

    if a.export:
        # Mirrors `classroom_auth.py --export` (2026-09-14): the only way a cloud
        # session can run `apply` is this token supplied as an env var.
        try:
            print(gcreds.export("calendar"))
        except gcreds.NoCredential as e:
            sys.exit(f"🔴 {e}")
        print("\n☝ Paste that single line as UB_CALENDAR_TOKEN in the cloud environment "
              "settings. It can write calendar events: never commit it.", file=sys.stderr)
        return 0
    if a.url:
        return cmd_auth_url(a) or 0
    if a.code:
        return cmd_auth_code(a) or 0
    if a.init_client:
        return cmd_auth_init(a) or 0
    if a.status:
        return cmd_auth_status(a) or 0
    return run({c.upper() for c in a.courses} or None, a.apply)


if __name__ == "__main__":
    sys.exit(main())
