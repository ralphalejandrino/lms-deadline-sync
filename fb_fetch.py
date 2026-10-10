#!/usr/bin/env python3
"""Pull posts from the UB + SIT Student Body Facebook pages -- WITHOUT a login.

Why this exists
---------------
Two of the channels that actually carry university-wide news are Facebook pages,
not the LMS. The UB advisory that moved week 1 online was posted there, and
`HUMCOM1`'s Blackboard announcement literally linked to a facebook.com/share/p/
URL. An LMS-only digest cannot see any of it.

How it reads a page while logged out
------------------------------------
Facebook shows a logged-out visitor ONE truncated post, then throws a login
modal over the rest. Three moves get past that, in this order -- all of them
are required:

1. **Remove the modal** (`[role=dialog]`) and any large `position: fixed`
   overlay, then restore `body { overflow: auto }`. The modal blocks scrolling,
   not just reading.
2. **Scroll.** More posts lazy-load; nothing beyond the first is in the initial
   HTML.
3. **Click every "See more".** This is the step that matters. Post bodies are
   truncated in the DOM, and only the FIRST post's full text appears in the
   inline JSON payload -- so scraping `"message":{"text":...}` out of the HTML
   silently returns one post and looks like it worked. Clicking expands the
   real text client-side, with no login prompt.

Two approaches that DO NOT work, so nobody re-tries them:
  - `page.request.get(<permalink>)` -> **HTTP 400**. Playwright's request context
    lacks the browser headers Facebook demands.
  - `page.goto(<permalink>)` per post -> **ERR_ABORTED** on roughly half of them,
    because Facebook client-side redirects logged-out permalink visits.

Identity / "new since last run"
-------------------------------
A post is identified by a **hash of its text**, stored in `fb_seen.json`.

🔴 **Do NOT key on the permalink.** That was the first design and it is broken:
Facebook's `pfbid…` tokens are **re-encoded per session**, so the identical post
came back under a different URL three hours later and every post looked new on
every run. Relative times ("16h", "1d") are equally useless as identity -- they
drift by definition. The text is the only stable thing a logged-out visitor can
see, so that is what gets hashed. Cost of this choice: an *edited* post
re-reports once. That is the right way to be wrong.

Runs INVISIBLY via browser.py (`--headless=new`). Pass --show to watch it.
"""
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from browser import launch

PROFILE = Path.home() / ".local/share/ub-fb-profile"
CACHE = Path.home() / ".cache/ub-lms"
OUT = CACHE / "fb_data.json"
SEEN = CACHE / "fb_seen.json"
MNL = timezone(timedelta(hours=8))

PAGES = {
    "SIT-SB": "https://www.facebook.com/ubsitsbo",
    "UB": "https://www.facebook.com/universityofbaguio",
    # Added 2026-09-10 (Ralph). Baguio City Public Information Office is the PRIMARY
    # source for class suspensions -- Memorandum Circulars land here first and UB
    # reshares them. On 09-10 the suspension reached us only as a UB repost, so a
    # direct pull is both earlier and independent of UB posting it at all.
    "PIO": "https://www.facebook.com/pio.baguio",
}

SCROLLS = 8

# Chrome/footer boilerplate that shows up inside a post container.
NOISE = re.compile(
    r"^(All reactions:|Like|Comment|Share|Send message|See less|See more|"
    r"College & university|Most relevant|View more comments|\d+[KM]?|·|\s*)$"
)


def clean(page):
    """Drop the login modal and any full-height fixed overlay, re-enable scroll."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    page.evaluate(
        """() => {
        document.querySelectorAll('[role=dialog]').forEach(e => e.remove());
        document.querySelectorAll('div').forEach(e => {
            const s = getComputedStyle(e);
            if (s.position === 'fixed' && e.offsetHeight > 300) e.remove();
        });
        document.body.style.overflow = 'auto';
    }"""
    )


def expand_all(page):
    """Click every 'See more'. Returns how many were clicked."""
    return page.evaluate(
        """() => {
        let n = 0;
        document.querySelectorAll('div[role=button], span[role=button], span').forEach(e => {
            if (e.innerText && e.innerText.trim() === 'See more') { e.click(); n++; }
        });
        return n;
    }"""
    )


def scrape_posts(page):
    """Return [{url, when, text}] by walking up from each permalink anchor."""
    return page.evaluate(
        """() => {
        const out = [];
        const seen = new Set();
        const anchors = Array.from(document.querySelectorAll('a[href]')).filter(a =>
            /\\/(posts|videos)\\/|story_fbid=|\\/permalink\\//.test(a.href));
        for (const a of anchors) {
            const url = a.href.split('?')[0];
            if (seen.has(url)) continue;
            // Walk up to the smallest ancestor that looks like a whole post.
            let node = a;
            for (let i = 0; i < 12 && node.parentElement; i++) {
                node = node.parentElement;
                const t = node.innerText || '';
                if (t.length > 120 && /All reactions:|Like\\n|Comment/.test(t)) break;
            }
            const text = (node.innerText || '').trim();
            if (text.length < 60) continue;
            seen.add(url);
            // The anchor's own text is usually the relative timestamp ("16h", "1d").
            const when = (a.innerText || '').trim();
            out.push({ url, when, text });
        }
        return out;
    }"""
    )


def tidy(text, when):
    """Strip page chrome and the trailing reactions block from a post body.

    Also NFKC-normalises the text: these pages write headlines in mathematical
    bold/italic glyphs (𝗚𝗘𝗧 𝗖𝗢𝗡𝗡𝗘𝗖𝗧𝗘𝗗), which normalise back to plain letters
    and become greppable.
    """
    text = unicodedata.normalize("NFKC", text)
    lines = [l.rstrip() for l in text.split("\n")]
    # Everything from "All reactions:" onward is chrome.
    for i, l in enumerate(lines):
        if l.strip().startswith("All reactions:"):
            lines = lines[:i]
            break
    keep = []
    for l in lines:
        # "See less" is appended to the final content line once expanded.
        l = re.sub(r"\s*See (less|more)\s*$", "", l)
        s = l.strip()
        if NOISE.match(s):
            continue
        if s == when or s in ("AI content", "Shared with Public"):
            continue
        # The page's own name appears as a header line and again as a footer
        # byline. Real body text that mentions UB runs far longer than this.
        if re.match(r"^University of Baguio( SIT Student Body)?$", s):
            continue
        keep.append(l)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()


def post_key(body):
    """Stable identity for a post: a hash of its normalised text.

    NOT the permalink -- Facebook re-encodes `pfbid` tokens per session, so the
    same post arrives under a different URL on the next run.
    """
    norm = re.sub(r"\s+", " ", body).strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def approx_date(when, now):
    """'16h' / '3d' -> an approximate absolute date. Display only."""
    m = re.match(r"^(\d+)\s*([hmd w])", (when or "").lower().replace("w", " w"))
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).strip()
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
             "d": timedelta(days=n), "w": timedelta(weeks=n)}.get(unit)
    return (now - delta) if delta else None


def _scrape_one(page, url, now, seen):
    """Load one page and return its posts. Raises on navigation failure; an empty
    list means nothing usable was rendered (the caller treats that as retryable)."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(5000)
    clean(page)
    for _ in range(SCROLLS):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1200)
    clean(page)
    expand_all(page)
    page.wait_for_timeout(2500)
    clean(page)
    posts = []
    for raw in scrape_posts(page):
        body = tidy(raw["text"], raw["when"])
        if len(body) < 40:
            continue
        d = approx_date(raw["when"], now)
        key = post_key(body)
        # The anchor text is normally a relative time, but on SHARED posts it is
        # the shared URL -- fall back to scanning the body.
        when = raw["when"]
        if not re.match(r"^\d+\s*[smhdw]", when.lower()):
            m = re.search(r"\b(\d+\s*[smhdw])\b", raw["text"][:400])
            when = m.group(1) if m else "?"
        posts.append({
            "key": key, "url": raw["url"], "when": when,
            "approx_date": d.strftime("%Y-%m-%d") if d else None,
            "text": body, "new": key not in seen,
        })
    return posts


def fetch(mark_seen=True):
    """Scrape all pages and return the data dict. Raises on browser failure.

    `mark_seen=False` leaves fb_seen.json alone, so a post keeps showing as new
    -- useful when re-running by hand after a partial read.
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    now = datetime.now(MNL)
    data = {"fetched": now.isoformat(), "pages": {}}

    with sync_playwright() as p:
        ctx, page = launch(p, PROFILE,
                           extra_args=["--disable-blink-features=AutomationControlled"])
        for label, url in PAGES.items():
            # 🔴 ONE RETRY per page (2026-08-29). The logged-out view is flaky by
            # nature: Facebook intermittently throws the login wall harder,
            # client-side-redirects the permalink, or renders zero posts before
            # the scroll catches up — all of which clear on a second attempt far
            # more often than not. A page that yields NOTHING is treated as a
            # soft failure worth retrying, not as "no news" (an empty logged-out
            # read is indistinguishable from a walled one — the rule this whole
            # directory runs on). A page that fails BOTH tries is recorded as
            # _error so scan_facebook can mark it UNKNOWN, never silently OK.
            posts, err = [], None
            for attempt in (1, 2):
                try:
                    posts = _scrape_one(page, url, now, seen)
                    if posts:
                        err = None
                        break
                    err = "zero posts (login wall or slow render)"
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"[:300]
                if attempt == 1:
                    page.wait_for_timeout(3000)
            if posts:
                print(f"{label}: {len(posts)} posts", file=sys.stderr)
                data["pages"][label] = posts
            else:
                print(f"{label}: 🔴 {err} (after retry)", file=sys.stderr)
                data["pages"][label] = [{"_error": err or "no posts"}]
        ctx.close()

    if mark_seen:
        for posts in data["pages"].values():
            for post in posts:
                if post.get("key"):
                    seen.setdefault(post["key"], now.isoformat())
        SEEN.write_text(json.dumps(seen, indent=2))
    OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


def main() -> int:
    data = fetch(mark_seen="--keep-new" not in sys.argv)
    for label, posts in data["pages"].items():
        print(f"\n## {label}")
        for post in posts:
            if post.get("_error"):
                print(f"   🔴 {post['_error']}")
                continue
            flag = "🆕 " if post["new"] else "   "
            print(f"{flag}[{post['when']}] {post['url']}")
            print("      " + post["text"].replace("\n", "\n      "))
    print(f"\nWROTE={OUT}", file=sys.stderr)
    # 🔴 Exit non-zero when NO page yielded a post (2026-09-14). On 2026-09-13 a
    # cloud VM's proxy blocked facebook.com: all three pages printed 🔴 and this
    # still returned 0, so a caller checking the exit code read total failure as
    # success. Every other arm of this toolchain exits non-zero on an unknown.
    answered = [l for l, posts in data["pages"].items()
                if any(not p.get("_error") for p in posts)]
    if not answered:
        print("🔴 Facebook: NO page answered — UNKNOWN, not 'no news'.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
