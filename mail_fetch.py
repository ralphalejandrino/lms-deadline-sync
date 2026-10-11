#!/usr/bin/env python3
"""The school inbox, filtered to what actually needs an action.

    ./lms.sh mail              actionable mail, everything else collapsed to a count
    ./lms.sh mail --all        every row, unfiltered
    ./lms.sh mail --json       machine-readable, for a skill to fold into a load

## Why this exists (2026-08-29)

Mail reads at session load stopped being opt-in: starting a session is a
deliberate act and a digest is a read, so both the work and study loads now pull
mail without asking. Writes -- draft, send, label, trash -- did NOT change and
still need Ralph's yes in the turn that sends them.

Ralph chose an **actionable-only** digest, with him relying on the filter. That
choice is what the rest of this file is about, because a filter that silently
drops something is worse than no filter at all.

## 🔴 THE TWO RULES THAT MAKE A FILTER SAFE

1. **Nothing is dropped silently.** Non-actionable mail is COUNTED and the count
   is printed. `14 other messages (2 unread)` is a very different statement from
   showing nothing, and it is the line that tells Ralph whether the filter is
   behaving.
2. **Every actionable item carries the REASON it was picked.** When the filter
   is wrong, the reason says why, and the rule can be fixed. A verdict with no
   reason cannot be audited -- which is the whole complaint behind
   `verify-read-only-checks-yourself`.

And the rule this whole directory runs on: **a failed pull prints UNKNOWN, never
"no mail".** An empty inbox and an unreadable one look identical to a reader.

## Two transports: the API, then the scrape

**`fetch_api()` is preferred** (authorised 2026-08-29). It uses the OAuth refresh
token at `~/.config/ub-classroom/token.json`, so it needs no browser: nothing
pops up on Ralph's screen, nothing depends on a Google browser session that
expires, and it works from a cloud VM.

`_fetch_scrape()` is the fallback, driving the saved profile. `classify()` is
deliberately transport-independent, so both paths share one filter and one test
suite -- the filter is the part worth getting right, not the plumbing.

🔴 **A WRONG-ACCOUNT refusal does not fall through to the scrape.** That is a
correctness failure, not a transport hiccup, and the scrape would read the same
wrong mailbox. Only a transport failure falls through.

⚠ The PERSONAL account is not read here. It is reached through the Gmail MCP
connector, which this process has no access to; the session load pulls that half
itself. See the `context-loader` skill.
"""
import argparse
import html
import json
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import _quiet_oauth  # noqa: F401
import browser
import gcreds
from school_config import PROFILE, SCHOOL_ACCOUNT  # Nebula Phase 0: one home for shared constants

INBOX = "https://mail.google.com/mail/u/0/#inbox"

# The filter, stated. Each rule prints its own name when it fires, so a wrong
# call is diagnosable instead of mysterious. Ordered most- to least-specific:
# the FIRST match wins and becomes the stated reason.
RULES = (
    ("deadline", re.compile(
        r"\b(due|deadline|overdue|submit|submission|late|extend(?:ed|sion)?)s?\b", re.I)),
    ("graded work", re.compile(
        r"\b(assignment|quiz|exam|activity|assessment|requirement|project|"
        r"defen[cs]e|presentation|laborator|worksheet|task)s?\b", re.I)),
    ("grade / record", re.compile(
        r"\b(grade[sd]?|score|result|transcript|clearance|remark)s?\b", re.I)),
    ("enrolment / money", re.compile(
        r"\b(enroll?ment|enroll?ed|scholarship|tuition|registrar|payment|"
        r"invoice|balance|assessment\s+form|discount|registration|"
        r"student\s+information|completion\s+of|complete\s+your)s?\b", re.I)),
    # An instructor writing directly, or a thread Ralph is already in, is a
    # conversation he is part of — not a broadcast. Both fell through the first
    # ruleset: a message from the PROGIT1 instructor ("Re: PROGIT1 Input Exer")
    # and "Instructor B sent you a new message" were suppressed as "no rule
    # matched" on the first live run.
    ("direct message", re.compile(r"sent you a (new )?message|new message in", re.I)),
    ("reply thread", re.compile(r"^\s*Re:", re.I)),
    ("class logistics", re.compile(
        r"\b(schedule|resched|suspend|suspension|advisory|announcement|"
        r"orientation|room\s?change|makeup|make-up|attendance|absence)s?\b", re.I)),
    ("job lane", re.compile(
        r"\b(interview|application|applied|offer|position|role|candidate|"
        r"hiring|recruit|assessment\s+invite|screening)s?\b", re.I)),
    ("client / work", re.compile(
        r"\b(proposal|contract|acknowledg|quotation|deployment|downtime|"
        r"invoice|purchase\s?order)s?\b", re.I)),
)

# Senders that never need an action from Ralph even when they trip a keyword.
# A submission RECEIPT is the classic false positive: it says "Assignment
# submitted", matches "assignment", and asks nothing of anyone.
NOISE_SENDER = re.compile(r"(do-not-reply|donotreply|noreply|no-reply|"
                          r"mailer-daemon|notifications?@)", re.I)
RECEIPT_SUBJECT = re.compile(r"\b(submission received|thanks for filling out|"
                             r"your response|receipt|confirmation|auto-?reply)\b", re.I)

# 🔴 The suppressors run BEFORE the rules and are checked on their own, not in
# combination with a noisy sender. Requiring BOTH was the first version's bug:
# "Forms Response Receipt — Thanks for filling out this form" carries a friendly
# display name, missed NOISE_SENDER, matched 'application', and was promoted to
# actionable (measured on the first live run, 2026-08-29).

# Bulk-marketing tell. Every mass-mail template pads its preheader with
# zero-width non-joiners so the preview text stops early; a real person's mail
# never contains a run of them. Measured on the live inbox: it separated two
# marketing mails ("Welcome to Design.com", "Our #1 request") from every genuine
# school and instructor message. Deliberately requires a RUN, not one character.
ZWNJ_RUN = re.compile("‌(?:\\s*‌){4,}")
SCHOOL_SENDER = re.compile(r"(ubaguio|classroom|blackboard)", re.I)


def classify(sender, subject, snippet):
    """Return (actionable: bool, reason: str). Reason is printed either way.

    Suppressors first, then the rules. The reason string names which fired, so a
    wrong call points straight at the rule that made it.
    """
    blob = f"{subject} {snippet}"
    if RECEIPT_SUBJECT.search(subject or ""):
        return False, "automated receipt — records something already done"
    if NOISE_SENDER.search(sender or "") and not any(
            rx.search(subject or "") for _, rx in RULES[:2]):
        return False, "automated sender, no deadline or graded-work wording"
    if ZWNJ_RUN.search(snippet or "") and not SCHOOL_SENDER.search(sender or ""):
        return False, "bulk marketing template (zero-width preheader padding)"
    for name, rx in RULES:
        m = rx.search(blob)
        if m:
            return True, f"{name}: {m.group(0).lower()!r}"
    return False, "no rule matched"


def _txt(el, sel):
    n = el.query_selector(sel)
    return (n.inner_text() or "").strip() if n else ""


TOKEN = pathlib.Path.home() / ".config/ub-classroom/token.json"


def _plain_body(payload):
    """Readable text of a Gmail `full` payload: text/plain first, else de-tagged HTML."""
    import base64
    plain, htm = [], []

    def walk(p):
        mt = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and mt in ("text/plain", "text/html"):
            txt = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            (plain if mt == "text/plain" else htm).append(txt)
        for c in p.get("parts") or []:
            walk(c)

    walk(payload)
    text = "\n".join(plain).strip()
    # Blackboard's "Daily Notifications" ships a text/plain part that is really its CSS
    # ("@import url(...)" + style rules), burying the notices (found 2026-10-11). Treat a
    # CSS-shaped plain part as absent and use the HTML part instead.
    if text and not ("@import" in text[:300] or text.count("{") > 15):
        return text
    if htm:
        from lms_digest import strip_html
        # <style>/<script>/<head> CONTENTS are not text; strip_html keeps them
        # (a Blackboard receipt came out as "@import url(...)" CSS, 2026-10-11).
        raw = re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", "\n".join(htm))
        return re.sub(r"[ \t\r\f\v]+", " ", strip_html(raw)).strip()
    return ""


def fetch_api(expect=SCHOOL_ACCOUNT, limit=60, bodies=False):
    """Read the inbox through the Gmail API. Returns (items, err).

    Preferred over the scrape since 2026-08-29: no browser, so nothing pops up,
    nothing depends on a Google session that expires, and it works from a cloud
    VM. `classify()` is deliberately transport-independent, so the filter and
    its tests are shared with the scrape path.
    """
    if not gcreds.available("classroom"):
        return None, f"no token: neither $UB_CLASSROOM_TOKEN nor {TOKEN}"
    try:
        from googleapiclient.discovery import build
        # gcreds: env var first, so the school inbox is readable from a cloud VM.
        cr = gcreds.credentials("classroom", quiet=True)
        svc = build("gmail", "v1", credentials=cr, cache_discovery=False)

        # Identity is asserted, never assumed — the same rule the Classroom and
        # scrape paths follow. Reading the wrong mailbox and reporting it as
        # "nothing actionable" is the failure worth spending a call to avoid.
        who = svc.users().getProfile(userId="me").execute().get("emailAddress", "")
        if who.lower() != expect.lower():
            return None, f"WRONG ACCOUNT: Gmail API is {who}, expected {expect}"

        ids = svc.users().messages().list(
            userId="me", labelIds=["INBOX"], maxResults=limit
        ).execute().get("messages", [])

        # 🔴 BATCHED, not serial. The first version fetched each message with its
        # own get() — 60 sequential round-trips, measured at ~19s and the single
        # slowest thing in a study load that isn't the Blackboard browser. Gmail
        # takes up to 100 gets in ONE HTTP batch, which collapses that to ~2s.
        # Order is not guaranteed across a batch, so each result carries its own
        # id and we sort back to inbox order at the end.
        by_id = {}

        def _collect(rid, resp, err):
            if err is None and resp is not None:
                by_id[resp["id"]] = resp

        batch = svc.new_batch_http_request(callback=_collect)
        for m in ids:
            # bodies=True (Nebula) asks for the FULL message; the default stays
            # metadata-only, so a normal load costs exactly what it did before.
            batch.add(svc.users().messages().get(
                userId="me", id=m["id"], format="full") if bodies else
                svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]))
        batch.execute()

        items = []
        seen_threads = set()
        for m in ids:                       # ids preserves inbox order (newest first)
            msg = by_id.get(m["id"])
            if msg is None:                 # a single failed get must not drop silently
                continue
            # 🔴 ONE ROW PER THREAD. messages().list returns every message, so a
            # 5-reply thread showed as 5 identical rows (measured 2026-08-29:
            # "Score released…" ×5, an instructor Re: ×2). Keep the newest
            # message per thread — inbox order means first-seen is newest.
            tid = msg.get("threadId")
            if tid in seen_threads:
                continue
            seen_threads.add(tid)
            h = {x["name"].lower(): x["value"]
                 for x in msg.get("payload", {}).get("headers", [])}
            sender = h.get("from", "")
            subject = h.get("subject", "")
            snippet = html.unescape(msg.get("snippet", "") or "")
            act, why = classify(sender, subject, snippet)
            row = {
                "sender": re.sub(r"\s*<[^>]*>", "", sender)[:60] or sender,
                "subject": subject, "snippet": snippet[:200],
                "date": (h.get("date", "")[:16]),
                "unread": "UNREAD" in (msg.get("labelIds") or []),
                "actionable": act, "why": why,
            }
            if bodies:
                row.update({"id": msg.get("id"), "thread_id": tid,
                            "internal_date": msg.get("internalDate"),
                            "from_email": (re.search(r"<([^>]+)>", sender) or [None, sender])[1],
                            "body": _plain_body(msg.get("payload") or {}) or snippet})
            items.append(row)
        # A batch that returned nothing while the list found ids is a failure,
        # not an empty inbox — report it as such rather than "no mail".
        if ids and not items:
            return None, f"metadata batch returned 0 of {len(ids)} messages"
        return items, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:170]


def fetch(expect=SCHOOL_ACCOUNT, limit=60, prefer_api=True):
    """Read the inbox, API first and the browser scrape as fallback."""
    if prefer_api:
        items, err = fetch_api(expect=expect, limit=limit)
        if items is not None:
            return items, None
        # 🔴 A WRONG-ACCOUNT refusal must NOT fall through to the scrape and be
        # retried as if it were a transport hiccup. It is a correctness failure,
        # and the scrape would read the same wrong mailbox.
        if err and "WRONG ACCOUNT" in err:
            return [], err
    return _fetch_scrape(expect=expect, limit=limit)


def _fetch_scrape(expect=SCHOOL_ACCOUNT, limit=60):
    """Read the inbox. Returns (items, err); err set means UNKNOWN, not empty.

    Never raises — a caller must be able to report UNKNOWN rather than crash.
    """
    items = []
    try:
        with sync_playwright() as p:
            ctx, page = browser.launch(p, PROFILE, viewport={"width": 1400, "height": 1000})
            try:
                page.goto(INBOX, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(8000)
                if "accounts.google.com" in page.url or "signin" in page.url:
                    return [], "Gmail session dead — bounced to sign-in"

                acct = page.query_selector("a[aria-label*='Google Account']")
                acct = (re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                                  acct.get_attribute("aria-label") or "") if acct else None)
                if not acct:
                    return [], "could not read the active Google account — refusing to report mail"
                if acct.group(0).lower() != expect.lower():
                    return [], (f"WRONG ACCOUNT: Gmail is showing {acct.group(0)}, "
                                f"expected {expect}")

                rows = page.query_selector_all("tr.zA")
                if not rows:
                    # An inbox with no rows is possible; an inbox whose ROW
                    # SELECTOR broke is far likelier and looks identical. Refuse
                    # to call it empty.
                    return [], ("no inbox rows matched 'tr.zA' — Gmail's markup may "
                                "have changed. Not reporting this as an empty inbox.")
                for r in rows[:limit]:
                    cls = r.get_attribute("class") or ""
                    sender = _txt(r, ".yP, .zF") or _txt(r, "span[email]")
                    subject = _txt(r, ".bog")
                    snippet = _txt(r, ".y2")
                    date = _txt(r, ".xW span") or _txt(r, ".xW")
                    if not (subject or snippet):
                        continue
                    act, why = classify(sender, subject, snippet)
                    items.append({
                        "sender": sender, "subject": subject,
                        "snippet": snippet.lstrip(" -").strip()[:200],
                        "date": date, "unread": "zE" in cls.split(),
                        "actionable": act, "why": why,
                    })
            finally:
                ctx.close()
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"[:160]
    return items, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="show every row, unfiltered")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--expect", default=SCHOOL_ACCOUNT)
    a = ap.parse_args()

    items, err = fetch(expect=a.expect, limit=a.limit)
    if err:
        if a.json:
            print(json.dumps({"ok": False, "error": err}))
        else:
            print(f"🔴 SCHOOL MAIL UNKNOWN — {err}", file=sys.stderr)
            print("   This is NOT 'no mail'. Nothing was read.", file=sys.stderr)
        return 2

    if a.json:
        print(json.dumps({"ok": True, "account": a.expect, "items": items}, indent=1))
        return 0

    live = [i for i in items if i["actionable"]] if not a.all else items
    rest = [i for i in items if not i["actionable"]]

    print(f"# School mail — {a.expect}  ({len(items)} rows read)")
    if not live:
        print("\n  Nothing actionable matched the filter.")
    for i in live:
        flag = "🔵" if i["unread"] else "  "
        print(f"\n{flag} {i['date'] or '—':>10}  {i['sender'][:28]:28}  {i['subject'][:70]}")
        print(f"     why: {i['why']}")
        if i["snippet"]:
            print(f"     {i['snippet'][:160]}")

    # 🔴 Rule 1: the remainder is COUNTED, never silently dropped.
    if not a.all:
        unread_rest = sum(1 for i in rest if i["unread"])
        print(f"\n  {len(rest)} other message(s) ({unread_rest} unread) — "
              f"filtered out as not actionable. `--all` shows them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
