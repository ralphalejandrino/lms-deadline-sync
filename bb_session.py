#!/usr/bin/env python3
"""Where a Blackboard session comes from — and proof that it is actually alive.

## WHY THIS EXISTS (2026-08-22)

Every Blackboard script here used to hardcode ONE session source: the saved
Playwright profile at ~/.local/share/ub-blackboard-profile. When that profile's
SAML session expired, `digest`, `sweep` and `cal` all died with 401 and the only
recovery was `./lms.sh login` — a separate, headed, interactive login into a
profile Ralph never otherwise looks at. Two consecutive study loads (2026-08-21
12:45 AM and 2026-08-22 12:25 AM) returned SESSION DEAD for exactly this reason.

Meanwhile Ralph has a REAL browser (Comet) reachable over CDP, which he logs into
normally, and which the 2026-08-20 session proved can talk to the same Blackboard
REST API. There was no reason for the toolchain to be blind to it.

So: a session is now sourced from EITHER transport, whichever is actually
authenticated, and the scripts do not care which.

    cdp      -> Ralph's real Comet browser via the relay (browser-control skill)
    profile  -> the saved headless Playwright profile

## 🔴 THE RULE THIS FILE ENCODES

**A source is only "alive" if GET /users/me returned 200.** Not if the browser
launched. Not if the port answered. Not if cookies exist on the origin — the
2026-08-22 probe found **6 cookies on the Blackboard origin and a 401 anyway**,
so cookie presence is worthless as a gate. This is the same lesson as
`lms.sh`'s launch probe (a browser that downloads is not a browser that runs)
and the relay's JSON gate (an open port is not a working relay).

Every source is probed with a real API call and REJECTED on anything but 200.
`open_session()` raises `SessionDead` when no source authenticates. It never
returns a soft-green, because an empty sweep and a dead session are
indistinguishable to a reader — and that is precisely how a deadline gets missed.

## Transport note

All three in-page transports were measured against /users/me on 2026-08-22
(`fetch`, `XMLHttpRequest` with credentials, and Playwright's
`page.request.get`). They agree — all 200 together, all 401 together. The
2026-08-20 note that `fetch()` throws "Failed to fetch" on Ultra is about pages
running INSIDE the Ultra SPA, where a service worker intercepts. We navigate to
the plain origin and use `page.request`, which is an out-of-band HTTP client
seeded from the context cookie jar, so it sidesteps that entirely.

## ⚠ CDP hygiene — non-negotiable

Never call `browser.close()` on a CDP connection: it terminates Ralph's actual
browser. Close only pages this module opened. See the `browser-control` skill.
"""
import os
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bb_cookies
from browser import launch

PROFILE = Path.home() / ".local/share/ub-blackboard-profile"
BASE = "https://blackboard.ubaguio.edu/learn/api/public/v1"
ORIGIN = "https://blackboard.ubaguio.edu/ultra/course"
CDP_URL = os.environ.get("LMS_CDP_URL", "http://127.0.0.1:9222")

# Order matters: the real browser is tried FIRST. It is the one Ralph actually
# logs into, so it is the one most likely to be fresh, and using it means a
# single human login fixes every scan at once.
SOURCE_ORDER = ("token", "cdp", "profile")


class SessionDead(RuntimeError):
    """No source authenticated. Carries per-source detail for the error report."""

    def __init__(self, attempts):
        self.attempts = attempts          # list of (source, detail)
        super().__init__("no authenticated Blackboard source")

    def report(self) -> str:
        lines = ["\n🔴 SESSION DEAD — no Blackboard source is authenticated.",
                 "   Do NOT report 'nothing outstanding' from this run.", ""]
        for src, detail in self.attempts:
            lines.append(f"   {src:8s} : {detail}")
        lines += [
            "",
            "   FIX (either one works — they are independent):",
            "     1. Sign in to Blackboard in the Comet window that is already open,",
            "        then re-run. Nothing else is needed; this source is checked first.",
            "     2. ./lms.sh login     — headed login into the saved scraper profile.",
        ]
        return "\n".join(lines)


class Session:
    """An authenticated Blackboard API handle. Constructed only after a 200."""

    def __init__(self, page, source, me):
        self._page = page
        self.source = source
        self.me = me
        self.uid = me["id"]

    def get(self, url):
        """GET a Blackboard API url. Returns parsed JSON, or {'_status': N}.

        Shape is deliberately identical to the old per-script `get()` helpers so
        callers did not have to change how they check for failure.
        """
        try:
            r = self._page.request.get(url)
        except Exception as e:
            return {"_status": -1, "_error": f"{type(e).__name__}: {e}"[:200]}
        if r.status != 200:
            return {"_status": r.status}
        try:
            return r.json()
        except Exception:
            return {"_unparsed": r.text()[:300]}


def _cdp_reachable(timeout=4):
    """Gate on a JSON response naming the browser — an open port is not enough.

    🔴 Two-stage so the DOWN case is cheap (2026-08-29). The relay is usually not
    up during a scan (it does not survive a reboot), and CDP_URL points at a
    Windows-host IP that, when nothing is listening, does NOT refuse fast — it
    hits the full timeout. Measured: a flat 4.0s tax on EVERY scan, up to 8s with
    the relogin re-probe, for a source that is almost always absent.

    So first do a 0.4s raw TCP connect. No socket in 0.4s → the relay is down,
    return False immediately. Only if the port answers do we spend the real
    timeout confirming it is actually a browser (an open port is not enough — the
    original lesson this function encodes).
    """
    import socket
    host, _, port = CDP_URL.split("//", 1)[1].partition(":")
    try:
        socket.create_connection((host, int(port or 80)), timeout=0.4).close()
    except OSError:
        return False
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=timeout) as r:
            return b'"Browser"' in r.read(400)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _probe(page, source):
    """Return (Session|None, detail). The ONLY thing that promotes a source."""
    try:
        page.goto(ORIGIN, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
    except Exception as e:
        return None, f"could not load Blackboard ({type(e).__name__})"
    try:
        r = page.request.get(f"{BASE}/users/me")
    except Exception as e:
        return None, f"/users/me raised {type(e).__name__}"
    if r.status != 200:
        return None, f"/users/me -> {r.status} (not signed in)"
    try:
        me = r.json()
    except Exception:
        return None, "/users/me -> 200 but body was not JSON"
    if not me.get("id"):
        return None, "/users/me -> 200 but carried no user id"
    # 🔴 A LOGGED-OUT browser also gets a 200 here -- as the user "guest"
    # (found 2026-09-28: Ralph's Comet was signed out of Blackboard, the cdp
    # source was promoted as "guest", every course 403'd or came back empty,
    # and persist() overwrote the saved login cookies with the guest ones).
    if str(me.get("userName", "")).lower() == "guest":
        return None, "/users/me -> 200 but as 'guest' (not signed in)"
    return Session(page, source, me), "OK"


def _relogin(show=None, pw=None):
    """One silent SAML re-login attempt. Returns (ok, detail). Never raises."""
    try:
        import sso_login
        # Hand over OUR playwright instance — starting a second sync one inside
        # this one raises "Sync API inside the asyncio loop" (live test, 08-23).
        return sso_login.relogin(show=bool(show), pw=pw)
    except Exception as e:
        return False, f"could not run relogin ({type(e).__name__}: {e})"[:160]


@contextmanager
def open_session(prefer=None, show=None, quiet=False, allow_relogin=True):
    """Yield an authenticated Session, or raise SessionDead.

    prefer: 'cdp' | 'profile' | None. None tries SOURCE_ORDER.

    allow_relogin: when every source is dead, attempt ONE silent SAML re-login
        and probe again before giving up. Blackboard's cookies are session-only
        and expire in hours; Ralph's GOOGLE session in the profile is persistent,
        which is why `./lms.sh login` has recovered with no typing 7 times
        running. This turns that manual step into an automatic one.

        🔴 It cannot produce a false green. `_probe()` still gates every source
        on a real /users/me 200, so a failed recovery reports SESSION DEAD with
        the same message as before. Set False to avoid recursion (probe_all).
    """
    order = [prefer] if prefer else list(SOURCE_ORDER)
    attempts, pw, opened = [], None, []
    relogin_note = None

    # ---- token source: tried BEFORE playwright is even started -------------
    # It needs no browser at all, so when it works nothing is launched, nothing
    # appears on screen, and there is no session to expire. Handled here rather
    # than inside the loop precisely so the browser is never spun up on the
    # happy path. Blocked today on UB registering the app — see bb_oauth.py.
    if "token" in order:
        try:
            import bb_oauth
            if bb_oauth.available():
                sess, detail = bb_oauth.session()
                if sess:
                    if not quiet:
                        print(f"# session: token ({sess.me.get('userName') or sess.uid}"
                              f", no browser)", file=sys.stderr)
                    yield sess
                    return
                attempts.append(("token", detail))
            else:
                attempts.append(("token", "no application credential — "
                                          "./lms.sh bb-token --ask"))
        except Exception as e:
            attempts.append(("token", f"{type(e).__name__}: {e}"[:160]))
        order = [s for s in order if s != "token"]
        if not order:
            raise SessionDead(attempts)

    # ---- relay: a session RE verified, shipped encrypted through the vault ----
    # Only when $UB_RELAY_KEY is set — i.e. a cloud session. It writes the cookies
    # into the profile's state file, and the `profile` source below then injects
    # and PROBES them like any other saved session. It cannot fake a green: the
    # /users/me 200 gate is unchanged. See bb_relay.py (2026-09-14).
    relay_ok = False
    if "profile" in order:
        try:
            import bb_relay
            if bb_relay.env_key():
                relay_ok, relay_detail = bb_relay.import_state()
                if not quiet:
                    print(f"# relay: {relay_detail}", file=sys.stderr)
                if relay_ok:
                    PROFILE.mkdir(parents=True, exist_ok=True)
                else:
                    attempts.append(("relay", relay_detail))
        except Exception as e:
            attempts.append(("relay", f"{type(e).__name__}: {e}"[:160]))

    # ---- relay cookies over plain HTTP: no browser, tried before playwright --
    # 🆕 2026-09-14 (CLOUD). Scoped deliberately to `relay_ok` — i.e. to a cloud
    # session, the only place that imports a relay. RE keeps its existing order
    # (cdp first: Ralph's real Comet is the session a human login refreshes), so
    # this cannot change how a load behaves on RE.
    #
    # 🔴 Why it exists: in the cloud the browser is the broken part, not the
    # session. Chromium cannot complete a TLS handshake through the agent proxy,
    # so every browser source reports ERR_CONNECTION_RESET and the load called
    # that SESSION DEAD — while the same cookies answered /users/me with a 200
    # over urllib. See bb_cookies.CookieSession for the measurement.
    if relay_ok:
        try:
            sess, detail = bb_cookies.session()
            if sess:
                if not quiet:
                    print(f"# session: relay-cookies "
                          f"({sess.me.get('userName') or sess.uid}, no browser)",
                          file=sys.stderr)
                yield sess
                return
            attempts.append(("relay-cookies", detail))
        except Exception as e:
            attempts.append(("relay-cookies", f"{type(e).__name__}: {e}"[:160]))

    try:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()

        for attempt_round in (1, 2):
          for source in order:
            t0 = time.time()
            page = None
            try:
                if source == "cdp":
                    if not _cdp_reachable():
                        attempts.append((source, "relay/browser not reachable at "
                                                 f"{CDP_URL} — run browser-up.sh"))
                        continue
                    browser = pw.chromium.connect_over_cdp(CDP_URL)
                    if not browser.contexts:
                        attempts.append((source, "CDP connected but browser has no context"))
                        continue
                    page = browser.contexts[0].new_page()
                    # ⚠ page only. Closing the browser kills Ralph's Comet.
                    opened.append(("page", page))
                elif source == "profile":
                    if not PROFILE.exists():
                        attempts.append((source, f"profile absent at {PROFILE}"))
                        continue
                    ctx, page = launch(pw, PROFILE, show=show,
                                       viewport={"width": 1200, "height": 800})
                    opened.append(("ctx", ctx))
                    # Blackboard's cookies are session-only, so a fresh Chromium
                    # starts logged out no matter how recently Ralph logged in.
                    # Put the saved ones back BEFORE probing. This cannot fake a
                    # session: _probe still gates on a real /users/me 200.
                    detail = bb_cookies.inject(page.context)
                    if not quiet:
                        print(f"# cookies: {detail}", file=sys.stderr)
                else:
                    attempts.append((source, "unknown source name"))
                    continue

                page.set_default_timeout(45000)
                sess, detail = _probe(page, source)
                dt = time.time() - t0
                if sess and relay_ok and source == "profile":
                    # Same probe, same gate — but say where the session came
                    # from, so a brief can print the relay's age next to it.
                    sess.source = "relay"
                if sess:
                    # Save ONLY after a verified 200. Harvesting from either
                    # source into the same state file is what makes one human
                    # login in Comet revive the headless profile too.
                    saved = bb_cookies.persist(page.context, source)
                    if not quiet:
                        print(f"# session: {source} "
                              f"({sess.me.get('userName') or sess.uid}, {dt:.1f}s)",
                              file=sys.stderr)
                        print(f"# cookies: {saved}", file=sys.stderr)
                    yield sess
                    return
                attempts.append((source, detail))
            except Exception as e:
                attempts.append((source, f"{type(e).__name__}: {e}"[:160]))

          # ---- every source is dead. Try ONE silent recovery, then re-probe. ----
          if attempt_round == 1:
              if not allow_relogin:
                  break
              # 🔴 CLOSE ROUND ONE FIRST. The profile source still has a Chromium
              # holding the user-data-dir, and Chromium allows exactly one process
              # per profile: relogin's launch dies with "Failed to create a
              # ProcessSingleton for your profile directory". Found by the live
              # recovery test, 2026-08-23 — no unit test would have seen it.
              for kind, obj in opened:
                  try:
                      obj.close()
                  except Exception:
                      pass
              opened = []

              ok, relogin_note = _relogin(show=show, pw=pw)
              if not quiet:
                  print(f"# relogin: {relogin_note}", file=sys.stderr)
              if not ok:
                  break
              # The recovery wrote fresh cookies into the scraper profile, so
              # that is the only source worth re-probing. Drop round 1's
              # failures: they describe a session that no longer exists.
              attempts, order = [], ["profile"]

        if relogin_note:
            attempts.append(("relogin", relogin_note))
        raise SessionDead(attempts)
    finally:
        for kind, obj in opened:
            try:
                obj.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass


def probe_all(show=None):
    """Report every source's live status. Backs `./lms.sh sources`."""
    rows = []
    for source in SOURCE_ORDER:
        try:
            # allow_relogin=False: this function asks "what is live RIGHT NOW".
            # Recovering inside it would both change the answer and fire twice,
            # once per source.
            with open_session(prefer=source, show=show, quiet=True,
                              allow_relogin=False) as s:
                rows.append((source, "✅ LIVE",
                             f"{s.me.get('userName') or ''} "
                             f"({(s.me.get('contact') or {}).get('email','')})".strip()))
        except SessionDead as e:
            rows.append((source, "🔴 dead", e.attempts[0][1] if e.attempts else "?"))
    return rows


if __name__ == "__main__":
    print("# Blackboard session sources — a source is LIVE only if /users/me returned 200\n")
    print(f"  {bb_cookies.status()}\n")
    live = 0
    for source, status, detail in probe_all():
        print(f"  {source:8s} {status}  {detail}")
        live += status.startswith("✅")
    print()
    if live:
        print(f"{live} live source(s) — digest / sweep / cal will work.")
        sys.exit(0)
    print("🔴 NO live source. Every Blackboard course is UNKNOWN, not 'nothing due'.")
    print("   Sign in to Blackboard in the open Comet window, or run ./lms.sh login")
    sys.exit(2)
