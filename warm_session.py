#!/usr/bin/env python3
"""Keep the school sessions alive BEFORE they are needed, and prove it happened.

    ./lms.sh warm          touch Google, refresh Blackboard, append to the log
    ./lms.sh warm --status what the log says: last run, last success, streak

## Why this exists (2026-08-29)

Blackboard's SAML session dies roughly daily — a server-side lifetime no
client-side cookie trick reaches. Recovery was already automatic *when the
profile's Google session is alive*: `sso_login.relogin()` re-runs the SAML
handshake with no typing and has restored the session 7 times running. The
Google session is the load-bearing one, because it is the only one that
persists.

So the failure that actually costs Ralph an answer is narrow: **the Google
session going idle-stale, discovered at the moment he asks a question.** This
job moves that discovery earlier. Every few hours it touches Google as the
signed-in user (which resets the idle timer that invalidates a Workspace
session) and then re-establishes Blackboard, so the session is warm before a
scan ever asks for it.

Ralph chose this over storing a password: **the Blackboard local login form is
real but his account has no local password** — the one he changed is the Google /
university account credential, and 2FA (correctly) blocks automating that. So
there is no credential to store, and this needs none.

## 🔴 It reports evidence, not hope

The vault's Stop hook `|| exit 0`s its failures and taught a hard lesson: a
scheduled thing that fails silently is worse than one that never ran, because
its silence reads as success. So every run appends a timestamped line to
`~/.cache/ub-lms/warm.log`, `--status` reads that log back, and the process
exits non-zero when Blackboard could not be established — which makes
`systemctl --user status ub-warm` tell the truth on its own.

⚠ It CANNOT manufacture a session. If Google itself needs a human, `relogin()`
returns False, this exits 1, and the log says so. That is the correct outcome,
not a bug to paper over.
"""
import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

CACHE = Path.home() / ".cache/ub-lms"
LOG = CACHE / "warm.log"
MNL = timezone(timedelta(hours=8))
PROFILE = Path.home() / ".local/share/ub-blackboard-profile"


def log(line):
    CACHE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(MNL).strftime("%Y-%m-%d %I:%M %p")
    with LOG.open("a") as f:
        f.write(f"{stamp}  {line}\n")
    print(f"  {line}")


def touch_google():
    """Load a Google page as the signed-in user. Returns (ok, detail).

    This is the half that actually matters: it resets the inactivity timer that
    expires a Workspace session. Blackboard's own lifetime is server-side and
    cannot be extended — it is simply re-established below.
    """
    from playwright.sync_api import sync_playwright
    import browser
    try:
        with sync_playwright() as p:
            ctx, page = browser.launch(p, PROFILE, viewport={"width": 1100, "height": 800})
            try:
                page.goto("https://classroom.google.com/h",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                url = page.url
                if "accounts.google.com" in url or "signin" in url:
                    return False, "GOOGLE SESSION DEAD — a human must sign in"
                el = page.query_selector("a[aria-label*='Google Account']")
                who = ""
                if el:
                    import re
                    m = re.search(r"[\w.%+-]+@[\w.-]+\.\w{2,}",
                                  el.get_attribute("aria-label") or "")
                    who = m.group(0) if m else ""
                return True, f"google ok ({who or 'account not named'})"
            finally:
                ctx.close()
    except Exception as e:
        return False, f"google touch raised {type(e).__name__}: {e}"[:140]


def warm_blackboard():
    """Re-establish and persist the Blackboard session. Returns (ok, detail)."""
    from bb_session import SessionDead, open_session
    try:
        with open_session(quiet=True) as sess:
            return True, f"blackboard ok via {sess.source} ({sess.me.get('userName')})"
    except SessionDead as e:
        first = e.attempts[0][1] if e.attempts else "?"
        return False, f"BLACKBOARD DEAD — {first}"
    except Exception as e:
        return False, f"blackboard raised {type(e).__name__}: {e}"[:140]


def status():
    if not LOG.exists():
        print("🔴 never run — no log at", LOG)
        print("   A timer that has never fired is not a timer that is working.")
        return 1
    lines = [l.rstrip() for l in LOG.read_text().splitlines() if l.strip()]
    if not lines:
        print("🔴 log exists but is empty:", LOG)
        return 1
    oks = [l for l in lines if " ok " in l and "DEAD" not in l]
    print(f"# warm log — {LOG}  ({len(lines)} entries)")
    print(f"  last run     : {lines[-1]}")
    print(f"  last success : {oks[-1] if oks else '🔴 NONE — nothing has ever succeeded'}")
    for l in lines[-8:]:
        print(f"    {l}")
    # Staleness is the point: a timer that stopped firing looks identical to one
    # that never had anything to do.
    try:
        last = datetime.strptime(lines[-1][:19], "%Y-%m-%d %I:%M %p").replace(tzinfo=MNL)
        age = (datetime.now(MNL) - last).total_seconds() / 3600
        print(f"\n  last entry is {age:.1f}h old", end="")
        print("  🔴 STALE — the timer is not firing" if age > 9 else "  ✅")
        return 1 if age > 9 else 0
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        return status()

    t0 = time.time()
    g_ok, g_detail = touch_google()
    log(g_detail)
    b_ok, b_detail = warm_blackboard()
    log(f"{b_detail}  [{time.time() - t0:.0f}s]")
    if b_ok:
        # Hand the freshly verified session to cloud sessions (2026-09-14). Only
        # after a verified Blackboard 200, and a relay failure never fails the
        # warm run — see bb_relay.py.
        try:
            import bb_relay
            log(f"relay: {bb_relay.export()}")
        except Exception as e:
            log(f"relay: raised {type(e).__name__}: {e}"[:140])

    # 🔴 EITHER failure is a failure. The first version returned 0 whenever
    # Blackboard was reachable, and on its very first run that hid a dead GOOGLE
    # session behind a live Blackboard one (2026-08-29). That is backwards:
    # Google is the load-bearing session. When it dies, Gmail, Chat and
    # Classroom are ALL dead immediately, and Blackboard follows within about a
    # day with no unattended way back — `relogin()` needs the Google session to
    # already work. A green timer in that state is the exact false comfort this
    # job exists to remove.
    if not b_ok:
        print("\n🔴 Blackboard could not be established. The next scan will report "
              "UNKNOWN, which is correct — do not read it as 'nothing due'.",
              file=sys.stderr)
    if not g_ok:
        print("\n🔴 The GOOGLE session needs a human. Gmail, Google Chat and "
              "Classroom are ALL unreadable until it is fixed, and Blackboard "
              "loses its unattended recovery path.\n"
              "   At RE:        ./lms.sh login\n"
              "   From a phone: python phone_login.py + tailscale serve",
              file=sys.stderr)
    return 0 if (g_ok and b_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
