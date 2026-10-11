#!/usr/bin/env python3
"""Pull courses, announcements and due dates out of UB Blackboard.

Uses Blackboard's public REST API with the saved session cookie -- structured
JSON rather than scraped HTML, so nothing depends on their markup.

Session source is decided by bb_session.py -- Ralph's real Comet browser over
CDP if it is signed in, otherwise the saved scraper profile. Either way the
session is proven with a real /users/me 200 before anything is pulled.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bb_session import SessionDead, open_session
from school_config import BASE  # Nebula Phase 0: one home for shared constants

OUT = Path.home() / ".cache/ub-lms/bb_data.json"


def main() -> int:
    prefer = next((a.split("=", 1)[1] for a in sys.argv[1:]
                   if a.startswith("--source=")), None)
    try:
        with open_session(prefer=prefer) as sess:
            return pull(sess)
    except SessionDead as e:
        # Keep the machine-readable marker lms_digest.py greps for, then explain.
        print("NOT_AUTH status=401")
        print(e.report())
        return 1


def pull(sess) -> int:
    data = {"courses": [], "announcements": {}, "calendar": [], "grades": {}}
    get = sess.get
    uid = sess.uid
    me = sess.me
    data["source"] = sess.source
    data["me"] = {"id": uid, "userName": me.get("userName"), "studentId": me.get("studentId")}

    # --- course memberships ---
    memb = get(f"{BASE}/users/{uid}/courses?limit=100")
    for m in memb.get("results", []):
        cid = m.get("courseId")
        c = get(f"{BASE}/courses/{cid}")
        if c.get("_status"):
            continue
        entry = {
            "courseId": cid,
            "name": c.get("name"),
            "courseCode": c.get("courseId"),
            "termId": c.get("termId"),
            "available": (c.get("availability") or {}).get("available"),
            "created": c.get("created"),
        }
        data["courses"].append(entry)

        ann = get(f"{BASE}/courses/{cid}/announcements?limit=25")
        data["announcements"][cid] = [
            {
                "title": a.get("title"),
                "created": a.get("created"),
                "body": (a.get("body") or "")[:8000],
            }
            for a in ann.get("results", [])
        ]

        gb = get(f"{BASE}/courses/{cid}/gradebook/columns?limit=100")
        cols = []
        for g in gb.get("results", []):
            cols.append({
                "name": g.get("name"),
                "due": (g.get("grading") or {}).get("due"),
                "points": g.get("score", {}).get("possible") if isinstance(g.get("score"), dict) else None,
            })
        data["grades"][cid] = cols

    # --- calendar: next 60 days ---
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    until = (now + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cal = get(f"{BASE}/calendars/items?since={since}&until={until}&limit=200")
    for it in cal.get("results", []):
        data["calendar"].append({
            "title": it.get("title"),
            "type": it.get("type"),
            "start": it.get("start"),
            "end": it.get("end"),
            "courseId": it.get("calendarId"),
        })

    OUT.write_text(json.dumps(data, indent=2))
    print(f"COURSES={len(data['courses'])}")
    for c in data["courses"]:
        n_ann = len(data["announcements"].get(c["courseId"], []))
        n_col = len(data["grades"].get(c["courseId"], []))
        print(f"  - {c['courseCode']} | {c['name']} | ann={n_ann} cols={n_col}")
    print(f"CALENDAR_ITEMS={len(data['calendar'])}")
    print(f"WROTE={OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
