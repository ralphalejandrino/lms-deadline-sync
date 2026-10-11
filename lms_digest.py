#!/usr/bin/env python3
"""Study-session digest: what is new and what is due, across both LMSes.

Run at the start of a study session (MODE LOAD, study variant). Prints a compact
brief; writes nothing to the vault.

FAILS LOUD. If the saved session has expired it says so and exits 2 rather than
reporting "nothing due" -- a silent empty digest is indistinguishable from a
healthy one, and that is exactly how a deadline gets missed.

Runs INVISIBLY (see browser.py). Pass --show to watch it, --no-fb to skip the
Facebook pull.
  LD_LIBRARY_PATH=$HOME/pwlibs/_extracted/usr/lib/x86_64-linux-gnu \
    ~/CRM/.venv/bin/python lms_digest.py
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from browser import launch
# ONE session layer for every Blackboard tool -- see bb_session.py.
from bb_session import SessionDead, open_session
# Single source of truth for "when is this actually due" -- see bb_sweep.py.
from bb_sweep import authoritative_due
from school_config import BASE, MNL, PROFILE  # Nebula Phase 0: one home for shared constants

STATE = Path.home() / ".local/share/ub-lms-lastrun.json"
SOON_DAYS = 14


def mnl(iso, fmt="%a %d %b %I:%M %p"):
    if not iso:
        return "no due date"
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MNL).strftime(fmt)


def strip_html(h):
    """HTML -> readable text. NEVER truncate the result.

    On 2026-08-13 three separate things hid behind a cut-off string in one day:
    Instructor C's "there are NO ... penalties during Week 1", the PARQ due date (a
    1,500-char cap on 08-11), and Instructor D's "follow the activity instructions
    provided in the video lecture" -- which was a 20-point assignment nobody
    would otherwise have known existed. Truncation is where deadlines hide.
    """
    h = re.sub(r"<br\s*/?>", "\n", h or "")
    h = re.sub(r"</(p|div|li|tr|h\d)>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        h = h.replace(a, b)
    h = re.sub(r"[ \t]+", " ", h)
    return re.sub(r"\n{3,}", "\n\n", h).strip()


# Words that mean "this post creates work", even when no due date exists anywhere.
ACTION_WORDS = re.compile(
    r"\b(activit(y|ies)|assignment|submit|submission|pass|passing|deadline|due|"
    r"bring|wear|attendance|quiz|exam|reflection|paper|handwritten|hard copy|"
    r"react|confirm|instructions?|requirement)\b", re.I)


def flag(text):
    """Return the action words found, so a post that creates work is never quiet."""
    return sorted({w.group(0).lower() for w in ACTION_WORDS.finditer(text or "")})


SUBMITTED_STATES = ("NeedsGrading", "Completed", "Graded")


def classify_attempts(states):
    """Decide submitted/not from a gradebook column's ATTEMPT statuses.

    The gradebook COLUMN is not usable for this: it flips to NeedsGrading the
    moment an attempt exists, submitted or not. It printed a green tick for a
    30-point one-shot test that was still InProgress on 2026-08-17, and again
    for Unit 3: Practice Activities on 2026-08-18. Only the attempts say.

    Returns (submitted: bool, label: str).
    """
    states = [s for s in (states or []) if s]
    uniq = "/".join(dict.fromkeys(states))
    if not states:
        return False, "no attempt"
    if any(s in SUBMITTED_STATES for s in states):
        return True, uniq
    # Every attempt is InProgress/Draft: OPEN BUT NEVER SENT. This is the exact
    # state that scores zero while the column shows green. Fail LOUD.
    return False, f"🔴 {uniq} — OPEN, NOT SUBMITTED"


GRADED_STATES = ("Graded", "Completed")


def hand_graded(grade):
    """Label when the student's GRADE RECORD proves a column is done, else None.

    `grade` is `…/gradebook/columns/<col>/users/<uid>`. Added 2026-09-14: a
    face-to-face quiz the instructor marks by hand has NO attempts at all, so
    `classify_attempts` called four finished items "overdue" (FG Quiz 1 16/22,
    Quiz 2 19/21, Quiz 3 23/25, Unit 3 Continuation). Measured live: that record
    carries `status: "Graded", score: 16.0` for the quiz, and neither field for
    the genuinely outstanding `MATHMW1` Number Pattern.

    🔴 `NeedsGrading` alone proves NOTHING — it is the same flip that printed a
    false green tick on 2026-08-17. Only Graded/Completed, or a recorded score
    (a zero counts), is evidence.
    """
    if not isinstance(grade, dict) or grade.get("_status"):
        return None
    status, score = grade.get("status"), grade.get("score")
    has_score = isinstance(score, (int, float)) and not isinstance(score, bool)
    if not (status in GRADED_STATES or has_score):
        return None
    parts = [p for p in (status, f"{score:g}" if has_score else None) if p]
    return " ".join(parts) + " (marked by the instructor, no attempt)"


def classify_grade(states, grade):
    """classify_attempts, then the grade record as a second witness.

    Attempts still decide first. The grade record can only turn "not submitted"
    into "submitted" when it carries real evidence (see hand_graded)."""
    submitted, label = classify_attempts(states)
    if submitted:
        return True, label
    g = hand_graded(grade)
    return (True, g) if g else (False, label)


def main() -> int:
    now = datetime.now(timezone.utc)
    last = None
    if STATE.exists():
        try:
            last = datetime.fromisoformat(json.loads(STATE.read_text())["last_run"])
        except Exception:
            last = None

    print(f"# LMS digest — {now.astimezone(MNL):%a %d %b %Y %I:%M %p} (Manila)")
    print(f"  new since: {last.astimezone(MNL):%d %b %I:%M %p}" if last else "  first run — showing everything")

    overdue, soon, fresh = [], [], []

    # ---------------- Blackboard, via the SHARED session layer ----------------
    # 🔴 This file used to launch its own profile context and do its own
    # /users/me check, which made it the ONE Blackboard tool not on bb_session
    # (2026-08-23). It therefore never got cookie injection and could never be
    # rescued by Ralph's live Comet session, so `digest` could report SESSION
    # DEAD minutes after `sweep` ran clean. One session layer now, for everyone.
    unreadable = []
    try:
        cm = open_session()
        sess = cm.__enter__()
    except SessionDead as e:
        print(e.report())
        return 2
    try:
        get, uid = sess.get, sess.uid

        memb = get(f"{BASE}/users/{uid}/courses?limit=100")
        if memb.get("_status"):
            # The course LIST failing is total blindness, not a partial read.
            # Returning 0 courses here would print "nothing with a due date in
            # range" — a clean-looking digest built on nothing.
            print(f"\n🔴 COURSE LIST UNREADABLE (status {memb['_status']}) — "
                  "EVERY course is UNKNOWN, not 'nothing due'.")
            return 2

        for m in memb.get("results", []):
            cid = m.get("courseId")
            c = get(f"{BASE}/courses/{cid}")
            if c.get("_status"):
                # 🔴 This used to be a bare `continue`. A course that failed to
                # read simply vanished from the digest, and the digest then said
                # "nothing with a due date in range" — indistinguishable from a
                # healthy scan. Never silent again.
                unreadable.append(f"{cid} (course fetch {c['_status']})")
                continue
            code = (c.get("courseId") or cid).split("_")[-1]

            anns = get(f"{BASE}/courses/{cid}/announcements?limit=25")
            if anns.get("_status"):
                unreadable.append(f"{code} announcements ({anns['_status']})")
            else:
                for a in anns.get("results", []):
                    created = a.get("created")
                    if last and created:
                        if datetime.fromisoformat(created.replace("Z", "+00:00")) <= last:
                            continue
                    fresh.append((created, code, a.get("title"), strip_html(a.get("body"))))

            cols = get(f"{BASE}/courses/{cid}/gradebook/columns?limit=100")
            if cols.get("_status"):
                unreadable.append(f"{code} columns ({cols['_status']})")
                continue

            for col in cols.get("results", []):
                # The column LIST serves a cached grading.due. Re-read the column.
                due, due_warn = authoritative_due(
                    get, cid, col["id"], (col.get("grading") or {}).get("due"))
                if not due:
                    continue
                d = datetime.fromisoformat(due.replace("Z", "+00:00"))
                # 🔴 THE COLUMN LIES -- READ THE ATTEMPTS.
                # A gradebook column flips to NeedsGrading the moment an attempt
                # EXISTS, submitted or not. 2026-08-17: it printed ✅ for a 30-point
                # one-shot test whose attempt was still InProgress and unsent, ~3h
                # from the deadline. 2026-08-18: it did it again for `Unit 3:
                # Practice Activities`, 50 min before close. Only the attempt list
                # says whether the work actually went in.
                # ⚠ Query /attempts BARE. Adding ?userId=<uid> returns 403 -- that
                #   403 is what created the old "attempts is forbidden" belief.
                att = get(f"{BASE}/courses/{cid}/gradebook/columns/{col['id']}/attempts")
                if not att.get("_status"):
                    submitted, st = classify_attempts(
                        [a.get("status") for a in att.get("results", [])])
                else:
                    # Fallback to the column, with its known failure modes, and SAY
                    # that it is unverified rather than implying the attempt was read.
                    row = get(f"{BASE}/courses/{cid}/gradebook/columns/{col['id']}/users/{uid}")
                    if row.get("_status"):
                        row = {}
                    cst = row.get("status")
                    # `Graded` WITHOUT a score is NOT a submission. PATHFT1's PARQ
                    # reported status=Graded on the day it was posted, before any
                    # attempt could exist -- which put a ✅ next to untouched work.
                    # Fail toward warning: only trust `Graded` when a score backs it.
                    submitted = cst in ("NeedsGrading", "Completed") or row.get("score") is not None
                    st = f"{cst or 'no attempt'} ⚠ column only, attempts HTTP {att['_status']}"
                if due_warn:
                    st = f"{st}  🔴 {due_warn}" if st else f"🔴 {due_warn}"
                entry = (d, code, col.get("name"), submitted, st)
                if d < now and not submitted:
                    overdue.append(entry)
                elif now <= d <= now + timedelta(days=SOON_DAYS):
                    soon.append(entry)
    finally:
        cm.__exit__(None, None, None)

    print("\n## Blackboard")
    if overdue:
        print("🔴 OVERDUE, nothing recorded:")
        for d, code, nm, _, st in sorted(overdue, key=lambda r: r[0]):
            print(f"   [{code}] {nm} — was due {mnl(d.isoformat())}"
                  f"  [status: {st or 'no attempt'}]")
    if soon:
        print(f"⏳ due within {SOON_DAYS} days:")
        for d, code, nm, sub, st in sorted(soon, key=lambda r: r[0]):
            # An unsubmitted-but-open attempt must be SHOWN, not left blank --
            # a blank line reads as "nothing to do here".
            mark = (f"  ✅ {st or 'submitted'}" if sub
                    else (f"  {st}" if st and st != "no attempt" else ""))
            print(f"   [{code}] {nm} — {mnl(d.isoformat())}{mark}")
    if not overdue and not soon:
        print("   nothing with a due date in range")

    print("\n## New announcements")
    if fresh:
        # Print the FULL body, never a snippet. An announcement is the most
        # common place for work that has no gradebook column and therefore no
        # due date -- invisible to every other section of this digest.
        for created, code, title, body in sorted(fresh, reverse=True):
            hits = flag(title + " " + body)
            print(f"\n   {mnl(created, '%d %b %I:%M %p')}  [{code}] {title}"
                  + (f"\n   🔔 ACTION WORDS: {', '.join(hits)}" if hits else ""))
            for line in (body or "(empty body)").splitlines():
                print("      " + line)
    else:
        print("   none")


    # 🔴 A partial read is NOT a clean scan. Say which courses were not read, and
    # exit non-zero so no caller can mistake this for "nothing due".
    if unreadable:
        print("\n🔴 NOT FULLY READ — treat these as UNKNOWN, not as 'nothing due':")
        for u in unreadable:
            print(f"   • {u}")


    # ---------------- Google Classroom ----------------
    # 🔑 Deliberately NOT on the Blackboard session. Blackboard's SAML identity is
    # the SCHOOL account; the `cdp` source is Ralph's Comet, signed in as the
    # PERSONAL one. Following that session here would scrape the wrong account's
    # Classroom. This half always uses the saved scraper profile.
    with sync_playwright() as p:
        ctx, page = launch(p, PROFILE, viewport={"width": 1200, "height": 800})

        print("\n## Google Classroom — outstanding work")
        page.goto("https://classroom.google.com/u/0/a/not-turned-in/all",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(7000)
        if "accounts.google.com" in page.url:
            print("   🔴 Classroom session dead — re-run sso_login.py")
            ctx.close()
            return 2

        txt = re.sub(r"\n{2,}", "\n", page.inner_text("body"))
        m = re.search(r"(No due date.*?)(Help and Feedback|$)", txt, re.S)
        print("   " + (m.group(1).strip().replace("\n", "\n   ") if m else txt[:800]))

        # ---- Classroom STREAMS -------------------------------------------
        # The to-do list above only ever shows *graded assignments*. It cannot see
        # an announcement, and it cannot see work whose instructions live inside an
        # attachment. On 2026-08-13 NSTPRO1 posted a 20-point activity whose only
        # instructions were on the last slide of a 19-minute video: no assignment,
        # no due date, nothing in the to-do panel. This section exists because of it.
        print("\n## Google Classroom — recent stream posts (READ THESE)")
        page.goto("https://classroom.google.com/u/0/h", wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        courses = []
        for a in page.query_selector_all("a[href*='/c/']"):
            href = a.get_attribute("href") or ""
            mm = re.search(r"/c/([A-Za-z0-9_-]+)", href)
            if mm and mm.group(1) not in [c[0] for c in courses]:
                courses.append((mm.group(1), (a.inner_text() or "").split("\n")[0][:40]))
        if not courses:
            print("   🔴 could not enumerate Classroom courses — check manually")
        for cid_gc, cname in courses:
            page.goto(f"https://classroom.google.com/c/{cid_gc}", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            for _ in range(6):                      # expand any "See more"
                try:
                    page.get_by_role("button", name=re.compile(r"\bmore\b", re.I)
                                     ).first.click(timeout=2000)
                    page.wait_for_timeout(800)
                except Exception:
                    break
            body = re.sub(r"\n{3,}", "\n\n", page.inner_text("body"))
            body = re.split(r"Add comment", body)[0]
            body = re.sub(r"^.*?(Upcoming|Announce something)", "", body, flags=re.S)
            hits = flag(body)
            title = (page.title() or "").replace(" - Classroom", "").strip()
            print(f"\n   ── {title or cname or cid_gc}")
            if hits:
                print(f"   🔔 ACTION WORDS: {', '.join(hits)}")
            # NO per-line truncation, and no line cap on the newest posts. The whole
            # point of this section is that the missing clause is the assignment.
            skip = re.compile(r"^(Reply to this comment|Add class comment|Post$|more_vert|"
                              r"More options|edit$|View all|👍|❤️|\d+ class comments?)")
            for line in body.strip().splitlines()[:60]:
                s = line.strip()
                if s and not skip.match(s):
                    print("      " + s)
            # Attachments are opaque: a PDF or video can carry the entire assignment.
            att = re.findall(r"([^\n]{3,80}\.(?:pdf|docx?|pptx?|mp4|jpg|png))", body, re.I)
            if att:
                print("      ⚠ UNREAD MATERIAL — open these yourself, the scan cannot see inside:")
                for a_ in dict.fromkeys(att):
                    print(f"         • {a_.strip()}")
        ctx.close()

    # --- Facebook: UB + SIT Student Body -------------------------------
    # Separate browser profile (no login), so this runs after the LMS context
    # closes rather than sharing it.
    if "--no-fb" not in sys.argv:
        print("\n## Facebook — new posts")
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import fb_fetch

            fb = fb_fetch.fetch()
            any_new = False
            for label, posts in fb["pages"].items():
                errs = [p for p in posts if p.get("_error")]
                for e in errs:
                    # Fail loud: a page that errored is NOT a page with no news.
                    print(f"   🔴 [{label}] pull failed — {e['_error']}")
                for post in posts:
                    if post.get("_error") or not post["new"]:
                        continue
                    any_new = True
                    head = post["text"].split("\n")[0][:110]
                    print(f"   🆕 [{label}] {post['when']}  {head}")
                    print(f"      {post['url']}")
            if not any_new:
                print("   nothing new since the last run")
        except Exception as e:
            print(f"   🔴 FACEBOOK PULL FAILED — {type(e).__name__}: {e}"[:300])
            print("      Treat this as UNKNOWN, not as 'no news'.")

    STATE.write_text(json.dumps({"last_run": now.isoformat()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
