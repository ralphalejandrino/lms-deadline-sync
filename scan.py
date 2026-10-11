#!/usr/bin/env python3
"""ONE scan over EVERY school channel, with a coverage matrix that cannot fake success.

    ./lms.sh scan              full scan, drains findings into school/inbox.md
    ./lms.sh scan --no-fb      skip the Facebook pull
    ./lms.sh scan --dry-run    report only; write neither inbox.md nor the ledger

🔴 WHY THIS EXISTS
`outstanding` answers "what unstarted work has a gradebook column?" — and for
three months that was quietly treated as "what do I owe?". It is not the same
question, and the gap has cost real points:

  · NSTPRO1 Quiz 1 (2026-08-27) — a Google Form linked in a Blackboard
    announcement, open only during the 5:00-6:30 PM class. No column. Invisible.
  · CMPTRB1 Weeks 1-2 Assessment (2026-08-24) — taken and submitted; the ONLY
    witness was an emailed receipt. No column, and absent from a content walk.
  · MATHMW1 NATURE PATTERN HUNT (2026-08-27) — 100 pts, in a course that had no
    Blackboard shell at all until the column appeared mid-day.

So this command reads every source, and reports per course what it could and
could NOT see. **It exits non-zero if any cell is UNKNOWN.** "Nothing due" is
only ever true when the matrix is clean — and now that is a machine's judgement,
not a tired human's at 11 PM.

WHAT IT ADDS OVER THE EXISTING COMMANDS
  - the content walk becomes a real source (bb_sweep.walk now returns data)
  - anything that looks like work but has NO column becomes a CANDIDATE, written
    to school/inbox.md for a human to triage rather than evaporating
  - school/state/ledger.json: every known item, so any surface can diff runs
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bb_session import BASE, SessionDead, open_session          # noqa: E402
from bb_sweep import authoritative_due, classify_unreadable, walk  # noqa: E402
from lms_digest import ACTION_WORDS, classify_attempts, strip_html  # noqa: E402
from school_config import BB_HOST, MNL, SCHOOL  # Nebula Phase 0: one home for shared constants

# ---- Nebula Phase 0: the full-body message record ---------------------------
# When EMIT is a list, every message a source READS is also recorded here with its
# WHOLE body -- not just the keyword hits, and not cut to 220 chars. That cut is
# why free text never reached the brief (09-27: five items missed on a
# `complete: true` load). EMIT is None by default, so scan/load behave exactly
# as before unless study_load is run with --emit-items.
EMIT = None
_FORM = re.compile(r"https?://\S*(forms\.gle|docs\.google\.com/forms)\S*")


def _keyword_hit(text):
    t = text or ""
    return bool(ACTION_WORDS.search(t) or EXAM_WORDS.search(t) or _FORM.search(t))


def _emit(**row):
    if EMIT is not None:
        row["keyword_hit"] = _keyword_hit(row.get("body"))
        EMIT.append(row)

INBOX = SCHOOL / "inbox.md"
LEDGER = SCHOOL / "state" / "ledger.json"

SOURCES = ("gradebook", "announcements", "contents", "classroom", "chat", "facebook")

# Handlers that can carry graded work. A content item with one of these and no
# gradebook column is the exact shape of the NSTPRO1 miss.
WORKLIKE = ("asmt-test-link", "assignment", "asmt-survey-link", "lesson")

OK, UNKNOWN, NA = "OK", "UNKNOWN", "n/a"

# Chat, read as a SPACE and not only as a delta (2026-09-14) — see scan_chat.
CHAT_RECENT_DAYS = 14
_CHAT_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\] ?(.*)$")
EXAM_WORDS = re.compile(
    r"\b(exam(ination)?s?|quiz(zes)?|periodical|resched\w*|re-sched\w*|moved|"
    r"postpone\w*|venue|cancel\w*|no class(es)?)\b", re.I)


def mnl(iso, empty="—"):
    if not iso:
        return empty
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MNL)\
            .strftime("%a %d %b %I:%M %p")
    except Exception:
        return empty


def _dt(iso):
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except Exception:
        return None


def newer(created, since):
    """True if `created` is after `since`. NEVER compare these as strings.

    🔴 Blackboard stamps UTC with a trailing Z ('2026-08-27T02:55:00.000Z');
    the ledger writes Manila with an offset ('2026-08-27T22:40:00+08:00').
    Lexicographic comparison of those two is meaningless — '0' < '2' would call
    a brand-new announcement old and drop it silently, which is the precise
    failure this whole command exists to prevent. Parse, then compare.

    Unknown/unparseable dates are treated as NEW. A candidate shown twice is
    noise; a candidate never shown is a missed deadline.
    """
    if not since:
        return True
    c, s = _dt(created), _dt(since)
    return True if (c is None or s is None) else c > s


def code_of(course):
    """IAA1 course codes are the last underscore segment of the courseId."""
    cid = course.get("courseId") or ""
    return cid.split("_")[-1] if "_" in cid else (course.get("name") or "?")


# --------------------------------------------------------------- blackboard
def scan_blackboard(sess, cov, since):
    """Gradebook + announcements + content walk, per course. Fills `cov`."""
    items, candidates, notes = [], [], []
    memb = sess.get(f"{BASE}/users/{sess.uid}/courses?limit=100")
    if memb.get("_status"):
        notes.append(f"course list unreadable (status {memb['_status']})")
        return items, candidates, notes, None

    courses, failed = [], []
    for m in memb.get("results", []):
        cid = m.get("courseId")
        c = sess.get(f"{BASE}/courses/{cid}")
        if c.get("_status"):
            # Was a bare `continue`: the course never reached `cov`, so a closed
            # REAL course would vanish while `complete` stayed true (2026-10-11).
            failed.append(cid)
            continue
        courses.append({"cid": cid, "code": code_of(c), "name": c.get("name") or ""})

    benign, unknown = classify_unreadable(sess.get, sess.uid, failed)
    if benign:
        notes.append(f"Blackboard: {len(benign)} faculty-evaluation shell(s) not open yet "
                     f"(403 is expected): {', '.join(sorted(b.split('_')[-2] for b in benign))}")
    for cid, full, why in unknown:
        key = code_of({"courseId": full}) if full else cid
        cov.setdefault(key, {s: NA for s in SOURCES})
        for s in ("gradebook", "announcements", "contents"):
            cov[key][s] = UNKNOWN
        notes.append(f"🔴 Blackboard course {full or cid} ({cid}) unreadable, {why}. "
                     f"UNKNOWN, not 'nothing due'")

    for c in courses:
        cid, code = c["cid"], c["code"]
        cov.setdefault(code, {s: NA for s in SOURCES})

        # --- gradebook (attempt level) ---------------------------------
        cols = sess.get(f"{BASE}/courses/{cid}/gradebook/columns?limit=200")
        if cols.get("_status"):
            cov[code]["gradebook"] = UNKNOWN
        else:
            cov[code]["gradebook"] = OK
            for g in cols.get("results", []):
                colid = g.get("id")
                due, _warn = authoritative_due(
                    sess.get, cid, colid, (g.get("grading") or {}).get("due"))
                att = sess.get(f"{BASE}/courses/{cid}/gradebook/columns/{colid}/attempts")
                states = ([a.get("status") for a in att.get("results", [])]
                          if not att.get("_status") else None)
                submitted, label = classify_attempts(states or [])
                if not submitted and states is not None:
                    # A hand-marked face-to-face quiz has no attempts but a Graded
                    # record with a score (2026-09-14: four false "overdue").
                    # One extra call, only for columns not already proven done.
                    from lms_digest import classify_grade
                    grade = sess.get(f"{BASE}/courses/{cid}/gradebook/columns/"
                                     f"{colid}/users/{sess.uid}")
                    submitted, label = classify_grade(states or [], grade)
                score = g.get("score")
                items.append({
                    "course": code, "cid": cid, "column": colid,
                    "title": g.get("name") or "", "due": due,
                    "pts": score.get("possible") if isinstance(score, dict) else None,
                    "attempts": states, "submitted": submitted, "state": label,
                    "source": "gradebook",
                })

        # --- announcements ---------------------------------------------
        anns = sess.get(f"{BASE}/courses/{cid}/announcements?limit=25")
        if anns.get("_status"):
            cov[code]["announcements"] = UNKNOWN
        else:
            cov[code]["announcements"] = OK
            for a in anns.get("results", []):
                created = a.get("created") or ""
                _emit(source="bb_announce", id=f"bb:{cid}:{a.get('id')}", course=code,
                      title=a.get("title") or "(untitled)", author="", author_email="",
                      when=created, url=f"{BB_HOST}/ultra/courses/{cid}/announcements",
                      body=strip_html(a.get("body") or ""))
                if not newer(created, since):
                    continue
                body = strip_html(a.get("body") or "")
                hit = ACTION_WORDS.search(body or "")
                link = re.search(r"https?://\S*(forms\.gle|docs\.google\.com/forms)\S*", body or "")
                if hit or link:
                    candidates.append({
                        "course": code, "kind": "announcement",
                        "title": a.get("title") or "(untitled)",
                        "when": created,
                        "why": ("contains a GOOGLE FORM link" if link
                                else f"action word: {hit.group(0)!r}"),
                        "quote": " ".join((body or "").split())[:220],
                    })

        # --- content walk ----------------------------------------------
        tree = walk(sess.get, cid, emit=False)
        if any(t.get("_status") for t in tree):
            cov[code]["contents"] = UNKNOWN
        else:
            cov[code]["contents"] = OK
            have = {i["column"] for i in items if i["course"] == code}
            for t in tree:
                if not any(w in (t.get("handler") or "") for w in WORKLIKE):
                    continue
                if not newer(t.get("created"), since):
                    continue
                # A worklike content item posted recently, with no column that
                # obviously belongs to it, is the shape that has bitten twice.
                if not have or not any(
                        _looks_related(t["title"], i["title"])
                        for i in items if i["course"] == code):
                    candidates.append({
                        "course": code, "kind": "content",
                        "title": t.get("title") or "(untitled)",
                        "when": t.get("created"),
                        "why": f"posted as {t.get('handler')} with NO matching gradebook column",
                        "quote": "",
                    })
    return items, candidates, notes, courses


def _looks_related(a, b):
    ta = set(re.findall(r"[a-z0-9]+", (a or "").lower()))
    tb = set(re.findall(r"[a-z0-9]+", (b or "").lower()))
    return len(ta & tb) >= 2


# ------------------------------------------------------------- other sources
def scan_facebook(cov):
    try:
        import fb_fetch
        data = fb_fetch.fetch(mark_seen=True)
    except Exception as e:
        for c in cov:
            cov[c]["facebook"] = UNKNOWN
        return [], f"Facebook pull FAILED ({type(e).__name__}) — UNKNOWN, not 'no news'"
    out, failed = [], []
    for page, posts in (data.get("pages") or {}).items():
        # A page that failed both tries carries a single _error entry. Surface it
        # as UNKNOWN for that page — never let one page's failure read as "all OK".
        if len(posts) == 1 and posts[0].get("_error"):
            failed.append(page)
            continue
        for p in posts:
            # 🔴 "text", not "body". fb_fetch stores post text under "text"; the
            # old "body" lookup was always empty, so FB posts NEVER became
            # candidates (latent bug found 2026-08-29). ACTION_WORDS now actually
            # sees the post.
            txt = p.get("text") or ""
            _emit(source="fb", id=f"fb:{page}:{p.get('key')}", course=None,
                  title=f"Facebook — {page}", author=page, author_email="",
                  when=p.get("approx_date") or p.get("when"), url=p.get("url") or "",
                  body=txt)
            if p.get("new") and ACTION_WORDS.search(txt):
                out.append({"course": "—", "kind": f"facebook/{page}",
                            "title": txt[:70], "when": p.get("when"),
                            "why": "new post with action words",
                            "quote": " ".join(txt.split())[:220]})
    note = (f"Facebook: {', '.join(failed)} UNKNOWN after retry — not 'no news'"
            if failed else None)
    return out, note


def scan_chat(cov, since):
    """Google Chat course spaces. PROGIT1 and ARTAPP1 announce ONLY here.

    Until 2026-08-29 this was not a scan at all: every `chat` cell was set to
    `n/a` and a note told the reader to run a separate command. `n/a` is not
    counted as a gap by print_matrix, so a scan that never opened Chat could
    still print "✅ Every source reported for every course" — over the two
    subjects with no other announcement channel.
    """
    # Prefer the API. It needs no browser, and — more importantly — it
    # ENUMERATES the spaces instead of trusting a hardcoded list. That list was
    # missing `IAA1_LITERA1_2627A` and the program-wide `BSCS 2627A`, so LITERA1
    # was being reported as "no space exists" while announcements sat in it.
    # See chat_api.py.
    note_src = None
    try:
        import chat_api
        if not chat_api.available():
            raise ImportError("no token")
        results, failed, dead = chat_api.fetch(records=EMIT)
        code_for = chat_api.code_of
    except Exception as e:
        note_src = (f"Chat via BROWSER SCRAPE ({type(e).__name__}) — its space list is "
                    f"hardcoded and was missing LITERA1; run ./lms.sh classroom-auth --serve")
        try:
            import chat_fetch
        except Exception as e2:
            for c in cov:
                cov[c]["chat"] = UNKNOWN
            return [], f"Chat unavailable ({type(e2).__name__}: {e2}) — UNKNOWN"
        results, failed, dead = chat_fetch.fetch()
        code_for = chat_fetch.COURSE_OF.get

    if dead:
        # 🔴 EVERY course goes UNKNOWN, including the ones believed to have no
        # space at all. A dead source cannot tell us which courses have spaces —
        # and the hardcoded belief about that was WRONG (it omitted LITERA1), so
        # leaving those cells at `n/a` would assert "no space exists" on exactly
        # the evidence that just failed to load.
        for c in cov:
            cov[c]["chat"] = UNKNOWN
        notes = [n for n in (note_src,) if n]
        notes.append(f"Chat source DEAD ({dead}) — ALL courses UNKNOWN, not 'no news'")
        return [], "; ".join(notes)

    # Failed spaces are absent from `results`, so their course code has to be
    # derived from the space NAME rather than looked up in what came back.
    read_codes = {code_for(n) for n in results}
    fail_codes = {code_for(n) for n in failed}
    code_of_space = {n: code_for(n) for n in list(results) + list(failed)}
    for code in cov:
        if code in fail_codes:
            cov[code]["chat"] = UNKNOWN
        elif code in read_codes:
            cov[code]["chat"] = OK
        # else: left as NA — those four courses have no space at all.

    cands = []
    for name, r in results.items():
        body = r.get("new")
        if not body:
            continue
        hit = ACTION_WORDS.search(body)
        link = re.search(r"https?://\S*(forms\.gle|docs\.google\.com/forms)\S*", body)
        if hit or link:
            cands.append({
                # A program-wide space (BSCS 2627A) belongs to no course code.
                # It still carries real announcements, so it is reported under
                # "—" rather than dropped for not fitting the per-course shape.
                "course": code_of_space.get(name) or "—",
                "kind": "chat", "title": f"new message in {name}", "when": None,
                "why": ("contains a GOOGLE FORM link" if link
                        else f"action word: {hit.group(0)!r}"),
                "quote": " ".join(body.split())[:220],
            })
    # 🆕 2026-09-14 — A DELTA IS NOT A READ. The block above only looks at text
    # newer than the watermark, so a message seen once is never looked at again.
    # That hid Instructor C's 09-09 7:12 PM "Examination Schedule" message (both INTRCS1
    # exams moved off the SIT table) from every load until 09-14 — five days, and
    # the lecture exam had already been sat. So ALSO read the whole space for the
    # last CHAT_RECENT_DAYS, keeping only exam-shaped messages. These re-surface on
    # every run by design; they are kind "chat-recent", which the inbox writer and
    # study_load's board_diff skip — they are a view of the space, not new work.
    cutoff = datetime.now(timezone.utc) - timedelta(days=CHAT_RECENT_DAYS)
    for name, r in results.items():
        msgs, cur = [], None
        for line in (r.get("text") or "").splitlines():
            m = _CHAT_LINE.match(line)
            if m:
                cur = [m.group(1), m.group(2)]
                msgs.append(cur)
            elif cur is not None:
                cur[1] += " " + line          # a message's continuation lines
        new_text = r.get("new") or ""
        for stamp, body in msgs:
            try:
                ts = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            hit = EXAM_WORDS.search(body)
            if ts < cutoff or not hit:
                continue
            flat = " ".join(body.split())
            cands.append({
                "course": code_of_space.get(name) or "—",
                "kind": "chat-recent", "when": ts.isoformat(),
                "title": f"{name}: {flat[:70]}",
                "new": flat[:60] in " ".join(new_text.split()),
                "why": f"exam-shaped word {hit.group(0)!r} within {CHAT_RECENT_DAYS} days",
                "quote": flat[:220],
            })

    notes = [n for n in (note_src,) if n]
    if failed:
        notes.append(f"Chat: {len(failed)} space(s) NOT read — {', '.join(failed)}")
    return cands, ("; ".join(notes) if notes else None)


def _classroom_from_api(cov, api, since):
    """Fold an official-API Classroom read into the coverage matrix.

    Richer than the scrape in the way that matters: it carries **per-item
    submission state**, so an unsubmitted item is a fact from Google rather than
    something inferred from a due date. Inferring it is how a handed-in item
    gets re-flagged, and how an unsubmitted one quietly looks done.
    """
    import classroom_api
    expected = set(classroom_api.CODES) & {c["code"] for c in api["courses"] if c["code"]}
    for code in expected:
        cov.setdefault(code, {s: NA for s in SOURCES})
        cov[code]["classroom"] = OK

    cands = []
    for it in api["items"]:
        if it["submitted"]:
            continue
        # Unsubmitted Classroom work IS a candidate: it has no Blackboard
        # gradebook column, so nothing else in this scan can see it.
        cands.append({
            "course": it["course"] or "—", "kind": "classroom/coursework",
            "title": it["title"], "when": it["due"],
            "why": (f"NOT SUBMITTED (state {it['state']}), due {mnl(it['due'])}"
                    if it["due"] else
                    f"NOT SUBMITTED (state {it['state']}), no due date set"),
            "quote": it.get("link") or "",
        })
    for a in api["announcements"]:
        _emit(source="classroom", id=f"gc:{a.get('id')}", course=a.get("course"),
              title=f"Classroom — {a.get('course') or '?'}", author="", author_email="",
              when=a.get("when"), url=a.get("link") or "",
              body=a.get("body") or a.get("text") or "")
        if not newer(a.get("when"), since):
            continue
        hit = ACTION_WORDS.search(a["text"] or "")
        link = re.search(r"https?://\S*(forms\.gle|docs\.google\.com/forms)\S*", a["text"] or "")
        if hit or link or a.get("materials"):
            cands.append({
                "course": a["course"] or "—", "kind": "classroom/announcement",
                "title": (a["text"] or "(no text)")[:70], "when": a.get("when"),
                "why": ("contains a GOOGLE FORM link" if link else
                        f"attachment to open: {a['materials'][0]}" if a.get("materials")
                        else f"action word: {hit.group(0)!r}"),
                "quote": (a["text"] or "")[:220],
            })
    notes = [f"Classroom via OFFICIAL API — {len(api['courses'])} course(s), "
             f"{len(api['items'])} coursework item(s)"]
    # Skips are REPORTED. A silently-ignored course is the same failure as a
    # silently-unread one; Ralph must be able to see that a subject stopped
    # being covered here and say so if that is wrong.
    if api.get("skipped"):
        notes.append(f"SKIPPED as abandoned-in-Classroom (Ralph, 2026-08-29): "
                     f"{', '.join(sorted(set(api['skipped'])))} — now Blackboard-only")
    # 🔴 The stale list SELF-CORRECTS: a skipped course with new activity is
    # flagged loud, not silently swallowed. This is the guard against the
    # hardcoded-list rot that hid LITERA1.
    for sa in api.get("stale_activity", []):
        notes.append(f"⚠ {sa['course']} is marked abandoned-in-Classroom but has NEW "
                     f"activity: {sa['newest']} — un-stale it if it moved back?")
    blind = [b for b in classroom_api.BLIND]
    notes.append(f"Classroom cannot see {', '.join(blind)} — Blackboard-only, "
                 f"their `classroom` cell is n/a by design, not by omission")
    return cands, "; ".join(notes)


def scan_classroom(cov, since, submitted_sink=None):
    """Classroom via the official API when authorised, else the profile scrape.

    Was a stub that returned a note and read nothing; see scan_chat's docstring
    for why a note plus an `n/a` cell is indistinguishable from success.

    submitted_sink: optional list; when given, already-submitted Classroom items
    are appended to it (the API path only — the scrape has no per-item state).
    `gather` uses it to build the artifact's "already done" counter-signal;
    `main` passes nothing and is unaffected.
    """
    import classroom_scrape

    # The official API is preferred: no browser, so nothing pops up and nothing
    # depends on a Google session that expires. Authorised 2026-08-29.
    note_api = None
    try:
        import classroom_api
        if classroom_api.available():
            api, api_err = classroom_api.fetch()
            if api_err:
                note_api = f"Classroom API failed ({api_err}); falling back to the scrape"
            else:
                if submitted_sink is not None:
                    for it in api.get("items", []):
                        if it.get("submitted"):
                            submitted_sink.append({"course": it["course"],
                                                   "title": it["title"],
                                                   "state": it["state"]})
                return _classroom_from_api(cov, api, since)
    except Exception as e:
        note_api = f"Classroom API unusable ({type(e).__name__}: {e}); using the scrape"

    data, err = classroom_scrape.fetch(streams=True)
    expected = set(classroom_scrape.CLASSROOM_COURSES)
    if err:
        for code in cov:
            if code in expected:
                cov[code]["classroom"] = UNKNOWN
        return [], f"Classroom UNKNOWN — {err}"

    seen = {c["code"] for c in data["courses"] if c["code"]}
    for code in expected | seen:
        cov.setdefault(code, {s: NA for s in SOURCES})
        cov[code]["classroom"] = OK if code in seen else UNKNOWN

    cands = []
    blob = data.get("todo") or ""
    for code in sorted(seen):
        stream = data["streams"].get(
            next((c["id"] for c in data["courses"] if c["code"] == code), None))
        if stream is None:
            cov[code]["classroom"] = UNKNOWN
            continue
        hit = ACTION_WORDS.search(stream)
        link = re.search(r"https?://\S*(forms\.gle|docs\.google\.com/forms)\S*", stream)
        if hit or link:
            cands.append({
                "course": code, "kind": "classroom/stream",
                "title": "recent Classroom stream post", "when": None,
                "why": ("contains a GOOGLE FORM link" if link
                        else f"action word: {hit.group(0)!r}"),
                "quote": " ".join(stream.split())[:220],
            })
    notes = [n for n in (note_api,) if n]
    missing = expected - seen
    if missing:
        notes.append(f"Classroom: EXPECTED but ABSENT — {', '.join(sorted(missing))}")
    if blob.strip():
        notes.append("Classroom to-do panel read; see ./lms.sh classroom-scrape "
                     "for the full text")
    return cands, ("; ".join(notes) if notes else None)


# --------------------------------------------------------------------- inbox
def append_candidates(cands, write=True):
    """Write CANDIDATEs into school/inbox.md, never duplicating one already there."""
    if not cands:
        return 0
    existing = INBOX.read_text() if INBOX.exists() else ""
    stamp = datetime.now(MNL).strftime("%Y-%m-%d %I:%M %p")
    lines, added = [], 0
    for c in cands:
        # chat-recent items re-surface EVERY run for 14 days on purpose (they are
        # the read-the-space view, 2026-09-14); the inbox is for new findings only.
        if c.get("kind") == "chat-recent":
            continue
        sig = f"{c['course']}·{c['title'][:60]}"
        if sig in existing:
            continue
        added += 1
        lines.append(
            f"- 🆕 **CANDIDATE — {c['course']} · {c['title']}**  "
            f"_({c['kind']}, {mnl(c.get('when'))})_\n"
            f"  - why: {c['why']}\n"
            + (f"  - quote: {c['quote']}\n" if c.get("quote") else "")
            + f"  - found by `./lms.sh scan` {stamp} · triage: real work? -> tasks.md\n")
    if not added or not write:
        return added
    block = (f"\n<!-- appended by ./lms.sh scan {stamp} -->\n" + "".join(lines))
    if "## Dump" in existing:
        head, _, tail = existing.partition("## Dump")
        existing = head + "## Dump" + tail.replace("\n---", block + "\n---", 1)
    else:
        existing += block
    INBOX.write_text(existing)
    return added


# ------------------------------------------------------------------- report
def print_matrix(cov):
    hdr = ["course"] + list(SOURCES)
    w = [max(9, max((len(c) for c in cov), default=6))] + [len(h) for h in SOURCES]
    print("  " + "  ".join(h.ljust(x) for h, x in zip(hdr, w)))
    bad = 0
    for code in sorted(cov):
        row = [code.ljust(w[0])]
        for i, s in enumerate(SOURCES):
            v = cov[code][s]
            if v == UNKNOWN:
                bad += 1
            row.append(v.ljust(w[i + 1]))
        print("  " + "  ".join(row))
    return bad


def gather(since, no_fb=False, on_status=None):
    """Read EVERY source once and return the structured result. Raises SessionDead.

    The single source of truth for the source-orchestration, shared by `main()`
    (the `scan` command) and `study_load.py` (the `load` command) so the two can
    never drift. Everything happens inside ONE `open_session()` — the browser is
    opened once — and every source fills the same `cov` matrix, so the coverage
    guarantee is identical no matter which command called it.

    on_status: optional callback(str) for progress lines; None stays silent
    (study_load wants a quiet gather; scan prints as it goes).
    """
    say = on_status or (lambda _m: None)
    cov, notes = {}, []

    # 🔴 ONLY Blackboard runs inside open_session(). It holds a live Playwright
    # instance, and `fb_fetch` starts its OWN `sync_playwright()` — nesting two
    # sync instances in one process raises a bare `Error` ("Playwright Sync API
    # inside the asyncio loop"). That is exactly what made Facebook report
    # "pull FAILED (Error)" inside `scan`/`load` while it worked standalone
    # (regression found 2026-08-29 by running the real sweep — the offline suite
    # could not see it). Chat and Classroom are API-based (no Playwright), so
    # they are safe anywhere; Facebook must run AFTER the session context closes.
    with open_session() as sess:
        say(f"blackboard: {sess.source} ({sess.me.get('userName')})")
        source = sess.source
        items, cands, bn, courses = scan_blackboard(sess, cov, since)
        notes += bn
    # ---- Blackboard's Playwright is now closed; the rest is safe ----

    # Already-submitted work, so a briefing can reconcile a mail/announcement
    # mention against "this is done" instead of guessing. Blackboard items carry
    # the flag directly; Classroom submitted items are collected via the sink
    # below (they are otherwise dropped, which is how a submitted quiz got
    # re-raised as an open question on 2026-08-29).
    submitted = [{"course": i["course"], "title": i["title"], "state": i["state"]}
                 for i in items if i["submitted"]]

    chat_c, chat_note = scan_chat(cov, since)          # API, no browser
    cands += chat_c
    if chat_note:
        notes.append(chat_note)

    if no_fb:
        for c in cov:
            cov[c]["facebook"] = NA
    else:
        fb_c, fb_note = scan_facebook(cov)             # own Playwright — safe here now
        cands += fb_c
        if fb_note:
            notes.append(fb_note)
            # A page failed both tries — a news channel we could not read is
            # UNKNOWN, not silently n/a. Fail loud (the directory's rule).
            for c in cov:
                cov[c]["facebook"] = UNKNOWN
        else:
            for c in cov:
                cov[c]["facebook"] = OK

    cl_c, cl_note = scan_classroom(cov, since, submitted_sink=submitted)  # API, no browser
    cands += cl_c
    if cl_note:
        notes.append(cl_note)

    return {"items": items, "candidates": cands, "coverage": cov,
            "notes": notes, "courses": courses, "source": source,
            "submitted": submitted}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fb", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", default=None,
                    help="ISO timestamp; only flag material newer than this")
    ap.add_argument("--all", action="store_true",
                    help="flag EVERYTHING, ignoring the last-scan watermark")
    a = ap.parse_args()

    # 🔴 INCREMENTAL BY DEFAULT. The first live run produced 17 candidates, most
    # of them August announcements containing the word "due" — a backlog dump.
    # A triage queue that reprints the same 17 rows every night is one he stops
    # reading, and this whole directory is built on the opposite principle. So
    # the watermark is the LAST SCAN, persisted in the ledger.
    #
    # It is deliberately NOT the digest's own last-run file: those are different
    # questions asked at different times, and sharing the watermark would let a
    # digest run hide material from the next scan.
    if not a.since and not a.all and LEDGER.exists():
        try:
            a.since = json.loads(LEDGER.read_text()).get("scanned")
        except Exception:
            a.since = None

    now = datetime.now(MNL)
    print(f"# SCAN — {now:%a %d %b %Y %I:%M %p} (Manila)")
    print(f"  new since: {mnl(a.since, 'the beginning of time (first run)')}")

    try:
        g = gather(a.since, no_fb=a.no_fb, on_status=lambda m: print(f"  {m}"))
    except SessionDead as e:
        print(f"\n🔴 SESSION DEAD — {e}")
        print("   Blackboard, announcements, contents and chat are ALL UNKNOWN.")
        print("   Do NOT report 'nothing due'. Recover with ./lms.sh login, or")
        print("   from a phone: python phone_login.py + tailscale serve.")
        return 2
    items, cands, cov, notes = (g["items"], g["candidates"],
                                g["coverage"], g["notes"])

    # ------------------------------------------------------------- output
    unstarted = [i for i in items if not i["submitted"]]
    overdue = [i for i in unstarted if i["due"] and
               datetime.fromisoformat(i["due"].replace("Z", "+00:00")) < datetime.now(timezone.utc)]
    print(f"\n  {len(items)} gradebook items · {len(unstarted)} unstarted · "
          f"{len(overdue)} OVERDUE · {len(cands)} candidate(s)")

    print("\n## COVERAGE")
    bad = print_matrix(cov)

    if cands:
        print(f"\n## CANDIDATES — look like work, have NO gradebook column ({len(cands)})")
        for c in cands:
            print(f"  · [{c['course']}] {c['title']}  ({c['kind']}, {mnl(c.get('when'))})")
            print(f"      {c['why']}")
        added = append_candidates(cands, write=not a.dry_run)
        print(f"\n  {'would append' if a.dry_run else 'appended'} {added} new -> {INBOX}")

    if notes:
        print("\n## NOTES")
        for n in notes:
            print(f"  ⚠ {n}")

    if not a.dry_run:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text(json.dumps(
            {"scanned": now.isoformat(), "items": items, "candidates": cands,
             "coverage": cov}, indent=1))
        print(f"\n  ledger -> {LEDGER}")

    if bad:
        print(f"\n🔴 {bad} UNKNOWN cell(s). This scan is INCOMPLETE — "
              f"'nothing due' is NOT a supported conclusion from this run.")
        return 1
    print("\n✅ Every source reported for every course. A clean board here is real.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
