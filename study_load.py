#!/usr/bin/env python3
"""ONE command for a whole study load — deadlines, mail, calendar, reconciled.

    ./lms.sh load            briefing-shaped text
    ./lms.sh load --json     one structured artifact, for Gigi to read

## Why this exists (2026-08-29)

A full `lets study` load took 13 minutes. The measured bottleneck was NOT I/O —
the APIs are sub-second and the Blackboard walk is ~30s. It was **Gigi's
orchestration**: she ran `scan`, then `mail`, then a calendar read, then
reconciled all three IN HER LLM across many Sonnet turns.

This command does all of that in ONE deterministic Python process — priming
once, opening the browser once — and emits a single artifact. It moves the
cross-source reconciliation out of a model and into code, which is:

  · faster   — one command, one read, far fewer subagent turns;
  · more reliable — Python does not drop rows (the `AGENTS.md §3` principle:
    "the tools decide, you report" — the same reason filtering/counting were
    moved out of the model after it dropped a 30-pt task three times);
  · lighter on context — the 71 KB calendar is filtered to a digest HERE, so
    the bulk never enters Gigi's window OR the main one. Gigi's whole value is
    keeping raw output out of Opus context; this preserves that.

Gigi's job shrinks to: run this once, spot-check 2-3 high-stakes claims against
disk, brief. She does NOT re-derive — that would rebuild the cost being removed.

## What it is NOT

It does not WRITE anything outward — no calendar writes (that stays the opt-in
`./lms.sh apply`), no mail, no board edits. It reads and reconciles; the human
decides. And it reuses `scan.gather()` verbatim, so the coverage matrix and its
UNKNOWN-means-incomplete guarantee are identical to `./lms.sh scan`.

## The completeness contract (fixed 2026-09-02)

**Exit 0 means EVERY source answered.** 2 = session dead, 1 = ran but INCOMPLETE,
0 = clean. `complete` in the artifact is the single field a caller checks; it is
false when either

  · `unknown_cells` — a cell of `scan.gather()`'s per-course coverage matrix is
    UNKNOWN (gradebook / announcements / contents / classroom / chat / facebook), or
  · `degraded`      — a source OUTSIDE that matrix failed: the calendar or school mail.

🔴 **The second half of that did not exist until 2026-09-02, and it cost four days.**
The gate read only `unknown_cells`, which is built from `cov` — and the calendar and
mail are fetched separately (see `_calendar_digest` / `mail_fetch.fetch_api`), so
their errors landed in `calendar_error`/`mail_error` and nothing looked at them.
The calendar OAuth token expired on ~08-29 and every load since returned
`calendar: null` **and exited 0**. A broken source that reports clean is the exact
failure this directory exists to prevent — same shape as the Chat/Classroom cells
that defaulted to `n/a` and slipped the gate.

➜ **If you add a source, add it to the matrix or to `degraded`. A source that can
fail without moving `complete` is not a source, it is a decoration.**
"""
import argparse
import json
import os
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Google intentionally grants a scope set that omits one implied scope, and
# oauthlib warns about it on every credential build. It is expected and handled
# (OAUTHLIB_RELAX_TOKEN_SCOPE); silence the cosmetic repeats so the briefing is
# clean. See classroom_auth.py for the full reasoning.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
warnings.filterwarnings("ignore", message=".*[Ss]cope.*")

sys.path.insert(0, str(Path(__file__).parent))
import scan                       # noqa: E402  — gather(), mnl(), NA/OK/UNKNOWN
from bb_session import SessionDead  # noqa: E402
from school_config import MNL, SCHOOL, SCHOOL_ACCOUNT  # Nebula Phase 0: one home for shared constants

TASKS = SCHOOL / "tasks.md"


def _iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _calendar_digest(now):
    """today / tomorrow / the 14-day dated set — filtered HERE so the raw 71 KB
    calendar never leaves this process. Returns ({}, err) on failure, never raises."""
    try:
        import cal_apply
        events = cal_apply.fetch_events()
    except Exception as e:
        return None, f"calendar UNKNOWN ({type(e).__name__}: {e})"[:140]
    today = now.date()
    tomorrow = today + timedelta(days=1)
    horizon = now + timedelta(days=14)
    d = {"today": [], "tomorrow": [], "upcoming": []}
    summaries = []
    for e in events:
        st = _iso(e.get("start"))
        if not st:
            continue
        st = st.astimezone(MNL)
        summaries.append(e.get("summary", ""))
        summary = e.get("summary", "")
        row = {"when": st.strftime("%a %d %b %I:%M %p"), "summary": summary}
        if st.date() == today:
            d["today"].append(row)          # diary: keep everything incl. classes
        elif st.date() == tomorrow:
            d["tomorrow"].append(row)
        elif today < st.date() <= horizon.date():
            # Beyond tomorrow, only DEADLINES matter — the recurring class
            # timetable is 50+ rows of noise. 📌 is the calendar skill's marker
            # for DUE / OVERDUE / EVENT / BUY; skip plain class blocks and ✅ done.
            if summary.startswith("📌"):
                d["upcoming"].append(row)
    d["_summaries"] = summaries          # internal, for reconciliation only
    # Columns a PERSON closed on evidence Blackboard cannot see (a paper quiz, an
    # office hand-in). cal_sync already defers to these; without the same set the
    # load kept listing them as overdue -- two tools, one column, two answers
    # (2026-09-27: PROGIT1 Tracing Activity, MATHMW1 Number Pattern). One rule,
    # imported, never copied.
    from cal_sync import human_done, marker_of
    d["_held"] = {marker_of(e) for e in events if marker_of(e) and human_done(e)}
    return d, None


def _board_text():
    """The board, plus the closed items `scripts/prune-state.py` moved out of it.

    🗂 2026-09-27 (CC-APP), vault reorg: closed `- [x]` items now leave tasks.md for
    `archive/tasks-history/YYYY-MM.md`, verbatim. An item that left the board because
    it was DONE must not come back as "on no board" — so the history counts as board
    for this match, exactly as it did while it still sat in tasks.md."""
    hist = TASKS.parent / "archive" / "tasks-history"
    parts = [TASKS] + (sorted(hist.glob("*.md")) if hist.is_dir() else [])
    return "\n".join(p.read_text() for p in parts if p.exists())


def _on_board(title, board_text):
    """Is this item already on the board? Loose containment on the distinctive
    words — a title need not match verbatim, but two shared 4+-char tokens do."""
    import re
    toks = [w for w in re.findall(r"[A-Za-z0-9]{4,}", (title or "").lower())]
    if not toks:
        return False
    low = board_text.lower()
    hits = sum(1 for t in set(toks) if t in low)
    return hits >= min(2, len(set(toks)))


def _on_calendar(title, summaries):
    return any(_on_board(title, s) for s in summaries)


def _stranded():
    """[(remote_branch, n)] for origin branches holding commits not in main.

    A module-level hook so tests can replace it and never shell out to git."""
    import sync_health
    return sync_health.stranded_branches()


def build(since=None, no_fb=False, persist=False):
    """Return (artifact_dict, session_dead_bool).

    persist=True writes the shared ledger (scanned/items/candidates/coverage) so
    the 'new since' watermark ADVANCES and the run is auditable after the fact —
    `load` used to skip this, which froze the watermark whenever Gigi ran `load`
    instead of `scan` (found 2026-08-29 when a study run left no disk trace).
    Tests call build() WITHOUT persist so they never clobber the real ledger."""
    now = datetime.now(MNL)
    try:
        g = scan.gather(since, no_fb=no_fb)
    except SessionDead as e:
        # 2026-09-15: a stale relay surfaced only as "SESSION DEAD", which reads like a
        # login problem. Lead with the real cause so a cloud brief cannot soften it.
        stale = next((d for _, d in getattr(e, "attempts", []) if "RELAY STALE" in str(d)), None)
        if stale:
            return {"ok": False, "when": now.isoformat(), "relay_stale": True,
                    "error": f"{stale} — full attempts: {e}"}, True
        return {"ok": False, "when": now.isoformat(),
                "error": f"SESSION DEAD — {e}. Blackboard/chat/classroom UNKNOWN, "
                         "NOT 'nothing due'. Recover: ./lms.sh login"}, True

    items, cands, cov = g["items"], g["candidates"], g["coverage"]
    if persist:
        try:
            scan.LEDGER.parent.mkdir(parents=True, exist_ok=True)
            scan.LEDGER.write_text(json.dumps(
                {"scanned": now.isoformat(), "items": items,
                 "candidates": cands, "coverage": cov}, indent=1))
        except Exception:
            pass                          # a ledger-write failure must not sink the load
    now_utc = datetime.now(timezone.utc)
    unsubmitted = [i for i in items if not i["submitted"]]

    # Calendar first: its human ✅ DONE marks decide what is NOT overdue.
    cal, cal_err = _calendar_digest(now)
    summaries = (cal or {}).pop("_summaries", []) if cal else []
    held_keys = (cal or {}).pop("_held", set()) if cal else set()

    # Same twin rule as cal_sync, applied to this item shape (title/column).
    from cal_sync import satisfied_twins
    gb = [i for i in items if i.get("source", "gradebook") == "gradebook"
          and i.get("cid") and i.get("column")]
    twins = satisfied_twins(
        gb, key=lambda i: (i["cid"], (i["title"] or "").strip(), i["due"], i.get("pts")),
        ident=lambda i: (i["cid"], i["column"]))

    # 🔴 VISIBLE, NOT DROPPED. A held or twin item leaves `overdue` but lands in
    # its own list with the reason, so the brief can still show it -- an item
    # that silently vanishes is indistinguishable from one never read.
    overdue, upcoming, held_by_you, twin_rows = [], [], [], []
    for i in unsubmitted:
        k = (i.get("cid"), i.get("column"))
        if k in held_keys:
            held_by_you.append({"course": i["course"], "title": i["title"],
                                "due": i["due"],
                                "why": "calendar event marked done by you; "
                                       "Blackboard shows no attempt"})
            continue
        if k in twins:
            twin_rows.append({"course": i["course"], "title": i["title"],
                              "due": i["due"],
                              "why": f"identical to {twins[k]}, which is submitted"})
            continue
        dt = _iso(i["due"])
        (overdue if (dt and dt < now_utc) else upcoming).append(i)

    mail_items, mail_err = None, None
    try:
        import mail_fetch
        # Only ASK for bodies when emitting: the default call is unchanged.
        mail_items, mail_err = (mail_fetch.fetch_api(bodies=True) if scan.EMIT is not None
                                else mail_fetch.fetch_api())
    except Exception as e:
        mail_err = f"{type(e).__name__}: {e}"[:140]
    if scan.EMIT is not None:
        if mail_err:
            # A failed pull is recorded as such, so Nebula shows UNKNOWN, not "no mail".
            scan.EMIT.append({"source": "school_mail", "error": mail_err})
        for m in mail_items or []:
            scan._emit(source="school_mail", id=f"gm:{m.get('id')}", course=None,
                       title=m.get("subject") or "(no subject)", author=m.get("sender") or "",
                       author_email=m.get("from_email") or "",
                       when=(datetime.fromtimestamp(int(m["internal_date"]) / 1000, timezone.utc)
                             .isoformat() if m.get("internal_date") else None),
                       url=f"https://mail.google.com/mail/u/?authuser={SCHOOL_ACCOUNT}"
                           f"#all/{m.get('thread_id')}",
                       body=m.get("body") or m.get("snippet") or "",
                       unread=m.get("unread"), rule_actionable=m.get("actionable"),
                       rule_why=m.get("why"))
    mail_actionable = [m for m in (mail_items or []) if m.get("actionable")]

    # ---- reconciliation, in Python: dated work on no board AND no calendar ----
    board_text = _board_text()
    board_diff = []
    for i in unsubmitted:
        if not i["due"]:
            continue
        if (i.get("cid"), i.get("column")) in held_keys or \
                (i.get("cid"), i.get("column")) in twins:
            continue          # already accounted for, under held_by_you / twins
        if not _on_board(i["title"], board_text) and not _on_calendar(i["title"], summaries):
            board_diff.append({"course": i["course"], "title": i["title"],
                               "due": i["due"], "why": "dated, on no board and no calendar"})
    for c in cands:
        if c.get("kind") == "chat-recent":
            continue          # reported under `chat_recent`, not as unboarded work
        if not _on_board(c["title"], board_text) and not _on_calendar(c["title"], summaries):
            board_diff.append({"course": c.get("course", "—"), "title": c["title"],
                               "due": c.get("when"), "why": c.get("why", "candidate")})

    bad = [f"{course}:{s}" for course in cov for s in cov[course]
           if cov[course][s] == scan.UNKNOWN]

    # Sources that live OUTSIDE the coverage matrix and so cannot appear in `bad`.
    # They must still be able to sink the load — see the completeness contract in
    # the module docstring. Missing this is what let a dead calendar exit 0 for
    # four days.
    degraded = []
    if cal_err:
        degraded.append(f"calendar: {cal_err}")
    if mail_err:
        degraded.append(f"school-mail: {mail_err}")
    # 🆕 2026-09-14: work stranded on a remote branch reaches neither the phone nor
    # RE. The 09-13 cloud session's 3 commits sat on a claude/* branch through a
    # whole RE load and were found by hand. A check that cannot run is not clean.
    try:
        for branch, n in _stranded():
            degraded.append(f"vault: {branch} holds {n} commit(s) not in main — merge it")
    except Exception as e:
        degraded.append(f"vault: stranded-branch check failed ({type(e).__name__})")

    return {
        "ok": True, "when": now.isoformat(), "source": g["source"],
        # The ONE field a caller checks. False = do not read this load as "nothing due".
        "complete": not (bad or degraded),
        "counts": {"items": len(items), "unsubmitted": len(unsubmitted),
                   "overdue": len(overdue), "held_by_you": len(held_by_you),
                   "twins": len(twin_rows), "candidates": len(cands),
                   "board_diff": len(board_diff), "unknown_cells": len(bad),
                   "degraded": len(degraded)},
        "degraded": degraded,
        "overdue": [{"course": i["course"], "title": i["title"], "due": i["due"],
                     "state": i["state"]} for i in overdue],
        "upcoming": [{"course": i["course"], "title": i["title"], "due": i["due"]}
                     for i in upcoming if i["due"]],
        "held_by_you": held_by_you,
        "twins": twin_rows,
        "board_diff": board_diff,
        # Counter-signal: already-done work, so a briefing reconciling a mail or
        # announcement mention ("New assignment: Quiz 1") can see it is submitted
        # instead of manufacturing a question about it (the 2026-08-29 NSTPRO1
        # false alarm). Deliberately compact — course/title/state only.
        "submitted": g.get("submitted", []),
        "mail_actionable": [{"from": m["sender"], "subject": m["subject"],
                             "why": m["why"], "unread": m["unread"]}
                            for m in mail_actionable],
        "mail_other_count": len(mail_items or []) - len(mail_actionable)
                            if mail_items is not None else None,
        "mail_error": mail_err,
        "calendar": cal, "calendar_error": cal_err,
        "coverage": cov, "notes": g["notes"], "unknown": bad,
        # The Chat SPACE, not just the delta (2026-09-14): every exam-shaped
        # message from the last 14 days, new or already seen. Brief these; a
        # moved exam lives here and in no gradebook column.
        "chat_recent": [{"course": c["course"], "when": c["when"], "quote": c["quote"],
                         "new": c.get("new", False), "why": c["why"]}
                        for c in cands if c.get("kind") == "chat-recent"],
    }, False


def exit_code(art, dead):
    """2 = session dead · 1 = ran but INCOMPLETE · 0 = every source answered.

    Extracted from main() on 2026-09-02 so it is directly testable. It used to be
    an inline expression reading only `unknown_cells`, which is precisely why no
    test could reach the bug: a dead calendar or mailbox exited 0.
    """
    if dead:
        return 2
    return 0 if art.get("complete") else 1


def _brief(a):
    """Terminal briefing — verdict, then the scannable bits. am/pm per house style."""
    if not a["ok"]:
        print(f"🔴 {a['error']}"); return
    c = a["counts"]
    print(f"# STUDY LOAD — {datetime.fromisoformat(a['when']):%a %d %b %Y %I:%M %p} "
          f"(Manila) · blackboard via {a['source']}")
    verdict = (f"{c['overdue']} overdue · {c['unsubmitted']} unsubmitted · "
               f"{c['board_diff']} not on any board/calendar · "
               f"{c['unknown_cells']} UNKNOWN cell(s) · "
               f"{c.get('degraded', 0)} source(s) DEGRADED")
    print(f"\nVERDICT: {verdict}")
    if c["unknown_cells"]:
        print(f"🔴 {c['unknown_cells']} source(s) UNKNOWN — this load is INCOMPLETE, "
              f"NOT 'nothing due': {', '.join(a['unknown'])}")
    # Printed HERE, next to the verdict, not only inside the DIARY/MAIL sections.
    # Both errors were already being printed further down all week and nobody
    # registered them — a failure buried under a heading reads as detail.
    if a.get("degraded"):
        print(f"🔴 {len(a['degraded'])} source(s) DEGRADED — this load is INCOMPLETE, "
              f"NOT 'nothing due':")
        for d in a["degraded"]:
            print(f"     · {d}")

    if a["overdue"]:
        print("\n## OVERDUE")
        for i in a["overdue"]:
            print(f"  🔴 [{i['course']}] {i['title']}  (due {scan.mnl(i['due'])}, {i['state']})")
    if a["board_diff"]:
        print("\n## DATED, ON NO BOARD OR CALENDAR (reconcile these)")
        for i in a["board_diff"]:
            print(f"  ⚠ [{i['course']}] {i['title']}  ({scan.mnl(i.get('due'))}) — {i['why']}")
    cal = a.get("calendar") or {}
    print("\n## DIARY")
    for lbl in ("today", "tomorrow"):
        rows = cal.get(lbl) or []
        print(f"  {lbl}: " + ("; ".join(r['summary'] for r in rows) if rows else "—"))
    if a.get("calendar_error"):
        print(f"  🔴 {a['calendar_error']}")
    print("\n## MAIL (actionable)")
    if a.get("mail_error"):
        print(f"  🔴 school mail UNKNOWN — {a['mail_error']}")
    for m in a["mail_actionable"]:
        flag = "🔵" if m["unread"] else "  "
        print(f"  {flag} {m['from'][:26]:26} {m['subject'][:56]}  ({m['why']})")
    if a.get("mail_other_count") is not None:
        print(f"  … {a['mail_other_count']} other message(s) filtered out")
    sub = a.get("submitted") or []
    if sub:
        # Printed so a reader can cross-check any mail/announcement mention
        # against "already done" before raising it as outstanding.
        print(f"\n## ALREADY SUBMITTED ({len(sub)}) — do NOT re-raise these")
        for s in sub:
            print(f"  ✅ [{s['course']}] {s['title'][:56]}  ({s['state']})")
    if a["notes"]:
        print("\n## NOTES")
        for n in a["notes"]:
            print(f"  ⚠ {n}")


def _write_emit(path, rows, art):
    """Atomic JSONL: a header line (run metadata) then one row per message."""
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    head = {"_run": {"when": art.get("when"), "complete": art.get("complete"),
                     "ok": art.get("ok", True), "rows": len(rows or [])}}
    with tmp.open("w") as f:
        f.write(json.dumps(head, ensure_ascii=False) + "\n")
        for r in rows or []:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-fb", action="store_true")
    ap.add_argument("--since", default=None)
    ap.add_argument("--all", action="store_true", help="ignore the last-scan watermark")
    ap.add_argument("--emit-items", metavar="PATH", default=None,
                    help="Nebula: also write every message read, with its FULL body, "
                         "as JSONL to PATH (atomic). Changes nothing else.")
    a = ap.parse_args()
    if a.emit_items:
        scan.EMIT = []

    since = a.since
    if not since and not a.all and scan.LEDGER.exists():
        try:
            since = json.loads(scan.LEDGER.read_text()).get("scanned")
        except Exception:
            since = None

    art, dead = build(since=since, no_fb=a.no_fb, persist=True)
    if a.emit_items:
        _write_emit(a.emit_items, scan.EMIT, art)
    if a.json:
        print(json.dumps(art, indent=1))
    else:
        _brief(art)
    # Exit non-zero when incomplete — same contract as scan, so a caller can
    # never mistake a broken load for a clean one.
    return exit_code(art, dead)


if __name__ == "__main__":
    sys.exit(main())
