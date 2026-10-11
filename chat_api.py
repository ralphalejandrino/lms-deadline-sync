#!/usr/bin/env python3
"""Google Chat through the OFFICIAL API — no browser, no hardcoded space list.

    ./lms.sh chat-api            every space, messages since the last run
    ./lms.sh chat-api --all      ignore the watermark
    ./lms.sh chat-api --json

## Why this replaces the scrape (2026-08-29)

`chat_fetch.py` drives a real browser and carries a **hand-maintained list of six
spaces** with a comment insisting the names "are not guessable" and that
`LITERA1`, `MATHMW1`, `NSTPRO1` and `PATHFT1` have no space at all.

The API says there are **nine**, and two of the extras matter:

  · `IAA1_LITERA1_2627A`  — LITERA1 DOES have a space. The scan was reporting
                            its chat cell as `n/a` ("no space exists"), so every
                            announcement posted there was invisible, and the
                            board looked complete while it was not.
  · `BSCS 2627A`          — program-wide announcements for the whole course.
                            Belongs to no single subject, so nothing that keys
                            off a course code would ever have found it.

It also hands back the space ID for `INTRCS1-IAA1`, which the scrape had as
`None` and reached by clicking the sidebar — the flaky path that intermittently
returned nothing.

**The lesson, and the reason this file exists at all:** a hardcoded inventory
cannot report what it does not contain. Enumerating from the source can.

## What it does NOT do

It reads. There is no send, reply, or reaction here, and the token carries only
`chat.spaces.readonly` and `chat.messages.readonly`.

⚠ Needs `~/.config/ub-classroom/token.json` (see `classroom_auth.py --serve`).
Without it, callers fall back to `chat_fetch.py`'s browser scrape.
"""
import argparse
import json
import pathlib
import re
import sys
sys.path.insert(0, __import__('os').path.dirname(__file__))
import _quiet_oauth  # noqa: F401
import gcreds
from school_config import CODES, course_code  # Nebula Phase 0: one home for shared constants

TOKEN = pathlib.Path.home() / ".config/ub-classroom/token.json"
CACHE = pathlib.Path.home() / ".cache/ub-lms"
SEEN = CACHE / "chat_api_seen.json"



def code_of(display):
    """Course code for a space name, or None for a program-wide space.

    Matches the bare stem too, because `ART APP-IAA1` collapses to `ARTAPPIAA1`
    and never contains the literal `ARTAPP1`.
    """
    return course_code(display, stem=True)


def _creds():
    # Same token as Classroom and school mail; gcreds reads $UB_CLASSROOM_TOKEN
    # first so this works on a cloud VM too (2026-09-14).
    return gcreds.credentials("classroom", quiet=True)


def available():
    return gcreds.available("classroom")


def fetch(mark_seen=True, per_space=25, records=None):
    """Read every space. Returns (results, failed, dead), matching chat_fetch.fetch().

    results maps space displayName -> {"text", "new", "code", "space_id"}.
    Never raises: a caller must be able to record UNKNOWN rather than crash.
    """
    if not available():
        return {}, [], (f"no token: neither $UB_CLASSROOM_TOKEN nor {TOKEN} — "
                        "run ./lms.sh classroom-auth --serve")
    try:
        from googleapiclient.discovery import build
        svc = build("chat", "v1", credentials=_creds(), cache_discovery=False)
        spaces = svc.spaces().list(pageSize=100).execute().get("spaces", [])
    except Exception as e:
        return {}, [], f"{type(e).__name__}: {e}"[:170]

    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    results, failed = {}, []
    for sp in spaces:
        # Direct messages are not course channels; skip rather than report them
        # as unread coursework. Nebula (records != None) still reads them, into
        # `records` ONLY -- they never touch results, coverage or the watermark.
        if sp.get("spaceType") == "DIRECT_MESSAGE":
            if records is not None:
                _dm_records(svc, sp, per_space, records)
            continue
        name = sp.get("displayName") or sp.get("name")
        try:
            msgs = svc.spaces().messages().list(
                parent=sp["name"], pageSize=per_space,
                orderBy="createTime desc").execute().get("messages", [])
        except Exception:
            failed.append(name)
            continue
        if records is not None:
            for m in msgs:
                if (m.get("text") or "").strip():
                    records.append(_record(m, "chat_space", name, code_of(name), sp))
        lines = []
        for m in reversed(msgs):
            txt = (m.get("text") or "").strip()
            if not txt:
                continue
            lines.append(f"[{(m.get('createTime') or '')[:16]}] {txt}")
        text = "\n".join(lines)
        last = seen.get(name)
        new = None
        if text:
            if not last:
                new = text
            else:
                # Everything after the last line we had recorded.
                idx = text.find(last[-300:]) if len(last) > 300 else -1
                new = text[idx + 300:] if idx >= 0 else (None if text == last else text)
        results[name] = {"text": text, "new": new or None,
                         "code": code_of(name), "space_id": sp["name"]}

    if mark_seen and results:
        seen.update({k: v["text"] for k, v in results.items()})
        CACHE.mkdir(parents=True, exist_ok=True)
        SEEN.write_text(json.dumps(seen))
    return results, failed, None


# ---- Nebula Phase 0: full message records ---------------------------------
INSTRUCTOR_DOMAIN = "@e.ubaguio.edu"      # students are @s.ubaguio.edu


def _record(m, source, space_name, code, sp, instructor_thread=None):
    snd = m.get("sender") or {}
    row = {"source": source, "id": m.get("name"), "course": code,
           "title": space_name, "author": snd.get("displayName") or "",
           "author_email": snd.get("email") or "", "when": m.get("createTime"),
           "url": sp.get("spaceUri") or "", "body": (m.get("text") or "").strip()}
    if instructor_thread is not None:
        row["instructor_thread"] = instructor_thread
    return row


def _dm_records(svc, sp, per_space, records):
    """A DM's messages, flagged `instructor_thread` when any sender is staff.

    Nebula's privacy rule keys on that flag: an instructor DM may go to the
    judge, a classmate DM never does. A read failure is RECORDED, not dropped.
    """
    try:
        msgs = svc.spaces().messages().list(
            parent=sp["name"], pageSize=per_space,
            orderBy="createTime desc").execute().get("messages", [])
    except Exception as e:
        records.append({"source": "chat_dm", "error": f"{type(e).__name__}"[:80],
                        "id": sp.get("name")})
        return
    staff = any(((m.get("sender") or {}).get("email") or "").lower()
                .endswith(INSTRUCTOR_DOMAIN) for m in msgs)
    for m in msgs:
        if (m.get("text") or "").strip():
            records.append(_record(m, "chat_dm", "Direct message", None, sp,
                                   instructor_thread=staff))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="ignore the watermark")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    results, failed, dead = fetch(mark_seen=not a.all)
    if dead:
        print(f"🔴 CHAT UNKNOWN — {dead}", file=sys.stderr)
        print("   This is NOT 'no news'. Nothing was read.", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"results": results, "failed": failed}, indent=1))
        return 0

    print(f"# Google Chat (API) — {len(results)} space(s)")
    for name, r in sorted(results.items()):
        body = r["text"] if a.all else r["new"]
        print(f"\n===== {name}  [{r['code'] or 'program-wide'}]")
        print((body or "(nothing new)")[-3000:])
    if failed:
        print(f"\n🔴 NOT READ: {', '.join(failed)} — UNKNOWN, not 'no news'.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
