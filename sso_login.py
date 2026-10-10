#!/usr/bin/env python3
"""One login, both systems.

Blackboard authenticates through Google SAML, so a single Google sign-in inside
THIS profile unlocks Classroom directly and lets Blackboard's SSO complete
without a second prompt.

Phase A: sign into Google (via Classroom).
Phase B: visit Blackboard, let SAML round-trip, confirm with the API.

Both checks ask a server, never a URL pattern:
  Classroom  -> host is classroom.google.com, not accounts.google.com
  Blackboard -> /learn/api/public/v1/users/me returns 200
"""
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
import bb_cookies

PROFILE = Path.home() / ".local/share/ub-blackboard-profile"
SHOTS = Path.home() / ".cache/ub-lms"
BB_ME = "https://blackboard.ubaguio.edu/learn/api/public/v1/users/me"
TIMEOUT_S = 900


def google_ok(page) -> bool:
    return urlparse(page.url).netloc.endswith("classroom.google.com")


def _mouse_click(page, el) -> bool:
    """Click via a real mouse event at the element's box.

    `ElementHandle.click()` reports success and does nothing when an overlay is
    in the way — measured on this very splash page (2026-08-23) and on Google's
    console the day before. A mouse event at the coordinates is what works.
    """
    try:
        bb = el.bounding_box()
        if not bb:
            return False
        page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
        return True
    except Exception:
        return False


def _saml_click(page) -> None:
    """Get from the UB splash page into the SAML handshake.

    🔴 TWO THINGS THE FIRST HEADLESS ATTEMPT GOT WRONG (both found live, 08-23):

    1. **The button is "Sign in with University Account"** — not "Sign in with
       Google" / "Sign In" / "Login", which is what the old label list looked for.
       There is no anchor to follow either: it is a <button> with no href, so
       there is no URL to navigate to directly.

    2. **A cookie-consent dialog sits on top of it** and silently swallows the
       click. It reappears whenever the consent cookie is missing — which is
       exactly the state a freshly-cleared profile is in. Dismiss it FIRST.
    """
    for b in page.query_selector_all("button"):
        try:
            if (b.inner_text() or "").strip() == "OK":
                _mouse_click(page, b)
                time.sleep(3)
                break
        except Exception:
            continue
    for b in page.query_selector_all("button, a"):
        try:
            t = (b.inner_text() or "").strip()
            if "University Account" in t or t in ("Sign in with Google", "Sign In", "Login"):
                _mouse_click(page, b)
                time.sleep(5)
                return
        except Exception:
            continue


# ---------------------------------------------------------------- auto path
# Blackboard's SAML session dies every few hours. Ralph's GOOGLE session in this
# profile does not -- Google's cookies are persistent where Blackboard's are
# session-only. That asymmetry is why `./lms.sh login` has restored the session
# with NO typing 7 times running (school/LOGS.md; also 6x, 4x, 2x earlier).
#
# So the interactive prompt below was, in the common case, asking Ralph to watch
# a thing happen by itself. `relogin()` is that same handshake with the human
# wait removed, run invisibly, for `bb_session` to call when every source is dead.
#
# 🔴 IT CANNOT MANUFACTURE A SESSION. It drives the same SAML round-trip a human
# would and then asks the SERVER. If Google itself needs a person, Phase A never
# reaches classroom.google.com, this returns False, and the caller reports
# SESSION DEAD exactly as before.
def relogin(timeout_s: int = 90, show: bool = False, pw=None):
    """Attempt a NON-interactive SAML re-login. Returns (ok, detail).

    pw: an ALREADY-STARTED sync_playwright instance. `bb_session` calls this from
        inside its own `sync_playwright().start()`, and nesting a second one
        raises "you are using Playwright Sync API inside the asyncio loop" —
        which is exactly how the first live recovery test failed (2026-08-23).
        Pass the live instance; only start our own when called standalone.

    Never raises: a failed recovery must degrade to the existing dead-session
    report, never replace it with a crash.
    """
    from playwright.sync_api import sync_playwright
    from browser import launch
    owned = None
    try:
        if pw is None:
            owned = sync_playwright().start()
            pw = owned
        if True:
            ctx, page = launch(pw, PROFILE, show=show,
                               viewport={"width": 1200, "height": 800})
            try:
                # Phase A -- Google. If the saved Google session is alive this
                # lands on Classroom with no interaction. If it bounces to
                # accounts.google.com and stays there, a human is genuinely needed.
                page.goto("https://classroom.google.com/", wait_until="domcontentloaded")
                deadline = time.time() + timeout_s
                while time.time() < deadline and not google_ok(page):
                    time.sleep(2)
                if not google_ok(page):
                    return False, ("google session needs a human "
                                   f"(still at {urlparse(page.url).netloc})")

                # Phase B -- Blackboard SAML.
                page.goto("https://blackboard.ubaguio.edu/", wait_until="domcontentloaded")
                time.sleep(5)
                _saml_click(page)

                bb_deadline = time.time() + timeout_s
                status = None
                while time.time() < bb_deadline:
                    status = page.request.get(BB_ME).status
                    if status == 200:
                        break
                    time.sleep(3)
                if status != 200:
                    return False, f"blackboard SAML did not complete (/users/me -> {status})"

                ctx.storage_state(path=str(PROFILE / "storage_state.json"))
                return True, "recovered: " + bb_cookies.persist(ctx, "relogin")
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        return False, f"relogin raised {type(e).__name__}: {e}"[:160]
    finally:
        if owned is not None:
            try:
                owned.stop()
            except Exception:
                pass


def main() -> int:
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            viewport={"width": 1400, "height": 900},
            # --disable-gpu: WSLg has no GBM ("falling back to sw"), and a GPU-composited
            # window rendered solid white then vanished (2026-10-05 7:50 AM).
            args=["--disable-blink-features=AutomationControlled", "--disable-gpu"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # ---------------- Phase A: Google ----------------
        page.goto("https://classroom.google.com/", wait_until="domcontentloaded")
        print(">>> SIGN IN AS student@s.ubaguio.edu IN THIS WINDOW <<<", flush=True)
        deadline = time.time() + TIMEOUT_S
        last = ""
        while time.time() < deadline:
            if page.url != last:
                print(f"  at: {page.url[:100]}", flush=True)
                last = page.url
            # Any tab counts: on 2026-10-05 the sign-in finished in a second tab
            # ("Home - Classroom") while this one sat on confirmidentifier forever.
            done = next((pg for pg in ctx.pages if google_ok(pg)), None)
            if done:
                page = done
                time.sleep(4)
                print("GOOGLE=ok", flush=True)
                break
            time.sleep(3)
        else:
            print("GOOGLE=timeout", flush=True)
            ctx.close()
            return 1

        page.screenshot(path=str(SHOTS / "gc_home.png"))
        print(f"GC_TITLE={page.title()}", flush=True)

        # ---------------- Phase B: Blackboard ----------------
        print("Now completing Blackboard SSO...", flush=True)
        page.goto("https://blackboard.ubaguio.edu/", wait_until="domcontentloaded")
        time.sleep(5)
        # Nudge the SAML handshake: dismiss the cookie dialog, then click
        # "Sign in with University Account". See _saml_click.
        _saml_click(page)
        print("  nudged the SAML handshake", flush=True)

        bb_deadline = time.time() + 300
        status = None
        while time.time() < bb_deadline:
            status = page.request.get(BB_ME).status
            if status == 200:
                break
            time.sleep(4)

        print(f"BB_STATUS={status}", flush=True)
        if status == 200:
            me = page.request.get(BB_ME).json()
            nm = me.get("name") or {}
            print(f"BB_NAME={nm.get('given','')} {nm.get('family','')}".strip(), flush=True)
            print("BB_IDS=" + json.dumps({k: me.get(k) for k in ("id", "userName", "studentId")}), flush=True)
        page.screenshot(path=str(SHOTS / "bb_home.png"))
        print(f"BB_URL={page.url}", flush=True)

        # storage_state() records session cookies as expires:-1, which Chromium
        # will never restore — so this write alone leaves the NEXT run logged out
        # and was exactly the bug (2026-08-22). Persist through bb_cookies so the
        # Blackboard cookies get a real expiry and actually come back.
        ctx.storage_state(path=str(PROFILE / "storage_state.json"))
        if status == 200:
            print("COOKIES=" + bb_cookies.persist(ctx, "sso_login"), flush=True)
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
