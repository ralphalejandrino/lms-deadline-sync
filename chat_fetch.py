#!/usr/bin/env python3
"""Read the school Google Chat spaces — PROGIT1 and ARTAPP1 announce ONLY here.

## Why this exists

For two weeks the vault recorded "Google Chat is invisible to CCLI" as a hard
limitation, and the two heaviest-signal subjects (`PROGIT1`, `ARTAPP1`) have no
other announcement channel. That claim was never tested. On 2026-08-23 it turned
out `chat.google.com` loads fine in the ordinary school scraper profile, and on
2026-08-24 this fetcher was written against it.

🔴 **Run it through `lms.sh`, never directly.** Chromium needs the unpacked
`LD_LIBRARY_PATH` that `lms.sh` exports; a direct `python chat_fetch.py` dies
with `libnspr4.so: cannot open shared object file`.

## What it does NOT do

It reads. It never sends, replies, or reacts — nothing here writes to a space.
"""
import argparse, json, pathlib, re, sys, datetime
from playwright.sync_api import sync_playwright
import browser
from school_config import PROFILE  # Nebula Phase 0: one home for shared constants

CACHE = pathlib.Path.home() / ".cache/ub-lms"
SEEN = CACHE / "chat_seen.json"

# The six course spaces that actually exist, with the room URL captured 2026-08-24.
#
# 🔴 THE NAMES ARE NOT GUESSABLE and there is no space for LITERA1, MATHMW1,
# NSTPRO1 or PATHFT1 — those four are Blackboard/Classroom only. An earlier
# version of this list invented "INTRCS1_IAA1" and "LITERA1 IAA1"; the first is
# spelled with a hyphen and the second does not exist. Enumerate, never assume.
#
# Navigating straight to the room URL is the reliable path. Clicking the rail
# works until it doesn't: the list virtualises, so a space far enough down is
# simply absent from the DOM, and ART APP-IAA1 failed that way on the first run
# while its neighbours succeeded. A silent click timeout on the ONE subject with
# no other announcement channel is exactly the failure this file exists to stop.
SPACES = {
    "PROGIT1 IAA1 2627A":  "https://chat.google.com/app/chat/AAQAy-GxOZA",
    "ART APP-IAA1":        "https://chat.google.com/app/chat/AAQAEHqcYDs",
    "HUMCOM1_IAA1-2627A":  "https://chat.google.com/app/chat/AAQAePI6bF8",
    "2627A IAA1_CMPTRB1":  "https://chat.google.com/app/chat/AAQA86Z-8aQ",
    "INTRCS1-IAA1":        None,   # room URL not yet captured — falls back to clicking
    "2627A IAA1_SOCORN2":  None,
}

def read_space(page, name, url=None):
    """Open one space and return its visible message text, or None if unreadable."""
    try:
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        else:
            page.goto("https://chat.google.com/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(8000)
            page.locator(f"span:has-text({name!r})").first.click(timeout=15000)
        page.wait_for_timeout(7000)
    except Exception as e:
        print(f"  🔴 {name}: could not open ({type(e).__name__})", file=sys.stderr)
        return None
    # evaluate(), not inner_text(): inner_text waits for actionability and hung
    # on Chat's overlays, while innerText just reads what is rendered.
    try:
        txt = page.evaluate("document.body.innerText")
    except Exception:
        return None
    return txt if txt and len(txt) > 120 else None


# Which subject each space belongs to. The four courses absent from this map
# (LITERA1, MATHMW1, NSTPRO1, PATHFT1) have NO space at all — see the note on
# SPACES above. For them "not read" is a true n/a, not a gap being hidden.
COURSE_OF = {
    "PROGIT1 IAA1 2627A": "PROGIT1",
    "ART APP-IAA1":       "ARTAPP1",
    "HUMCOM1_IAA1-2627A": "HUMCOM1",
    "2627A IAA1_CMPTRB1": "CMPTRB1",
    "INTRCS1-IAA1":       "INTRCS1",
    "2627A IAA1_SOCORN2": "SOCORN2",
}


def fetch(spaces=None, show=False, mark_seen=True):
    """Read the course spaces once and return the text. For `scan.py`.

    Returns (results, failed, dead) where results maps space name ->
    {"text": full text, "new": text unseen since the last run or None}.

    🔴 `dead` is NOT the same as `failed`. A dead session means every space is
    UNKNOWN; a failed space means that ONE space is UNKNOWN and the rest are
    real. Collapsing the two is how "no news" gets printed over a logged-out
    browser, which is the failure this whole directory exists to prevent.

    Never raises: a caller must be able to record UNKNOWN rather than crash.
    """
    wanted = spaces or list(SPACES)
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    results, failed = {}, []
    try:
        with sync_playwright() as pw:
            ctx, page = browser.launch(pw, PROFILE, show=show)
            try:
                page.goto("https://chat.google.com/", wait_until="domcontentloaded",
                          timeout=60000)
                page.wait_for_timeout(9000)
                if "accounts.google.com" in page.url or "signin" in page.url:
                    return {}, list(wanted), "not signed in (bounced to accounts.google.com)"
                for name in wanted:
                    txt = read_space(page, name, SPACES.get(name))
                    # ONE retry. Chat's space list virtualises and its overlays
                    # load late, so a space intermittently comes back empty --
                    # measured 2026-08-29, when PROGIT1 (the heaviest subject,
                    # and one of two that announce ONLY here) failed inside a
                    # scan and read perfectly 4 minutes later.
                    #
                    # 🔴 This must not soften the guard: a space that fails
                    # TWICE is still UNKNOWN. The retry removes noise, not the
                    # failure signal -- without it the real UNKNOWNs get
                    # ignored, which is how a guard dies.
                    if txt is None:
                        page.wait_for_timeout(3000)
                        txt = read_space(page, name, SPACES.get(name))
                        if txt is not None:
                            print(f"  ↻ {name}: recovered on retry", file=sys.stderr)
                    if txt is None:
                        failed.append(name)
                        continue
                    results[name] = {"text": txt, "new": _new_tail(txt, seen.get(name))}
            finally:
                ctx.close()
    except Exception as e:
        return {}, list(wanted), f"{type(e).__name__}: {e}"[:160]

    if mark_seen and results:
        seen.update({k: v["text"] for k, v in results.items()})
        CACHE.mkdir(parents=True, exist_ok=True)
        SEEN.write_text(json.dumps(seen))
    return results, failed, None


def _new_tail(body, old):
    """The part of `body` that was not present last run, or None if unchanged.

    Crude but honest, and deliberately fails towards showing MORE: when the
    overlap cannot be located the whole body is returned rather than nothing.
    """
    if not old:
        return body
    if len(old) > 400:
        idx = body.find(old[-400:])
        if idx >= 0:
            tail = body[idx + 400:]
            return tail or None
    return None if body == old else body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", action="append", help="only this space (repeatable)")
    ap.add_argument("--out", help="directory to write per-space dumps into")
    ap.add_argument("--show", action="store_true", help="visible window")
    ap.add_argument("--new", action="store_true", help="only text unseen since last run")
    args, _ = ap.parse_known_args()

    outdir = pathlib.Path(args.out) if args.out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now().strftime("%a %d %b %Y %I:%M %p")

    print(f"# Google Chat — {now} (Manila)")
    results, failed, dead = fetch(spaces=args.space, show=args.show)
    if dead:
        print(f"🔴 CHAT SESSION DEAD — {dead}. Run ./lms.sh login", file=sys.stderr)
        return 2

    for name, r in results.items():
        txt = r["text"]
        if outdir:
            safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
            (outdir / f"chat_{safe}.txt").write_text(txt, encoding="utf-8")
        print(f"\n===== {name}  ({len(txt)} chars)")
        body = (r["new"] or "(nothing new)") if args.new else txt
        print(body[-4000:])

    if failed:
        print(f"\n🔴 NOT READ: {', '.join(failed)} — UNKNOWN, not 'no news'.", file=sys.stderr)
    return 1 if failed and not results else 0


if __name__ == "__main__":
    sys.exit(main())
