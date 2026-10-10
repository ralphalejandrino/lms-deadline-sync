#!/usr/bin/env python3
"""Make a Blackboard login outlive the browser process that made it.

## WHY THIS EXISTS (2026-08-22)

`bb_session.py` fixed *where* a session can come from. It did not fix the reason
sessions kept dying, which is this:

    Blackboard's auth cookies are SESSION-ONLY.

Measured on the saved profile's cookie store — `BbRouter`, `JSESSIONID` and
`samlCookie` all carry `is_persistent = 0`. Chromium keeps session cookies in
memory and **never restores them to a new process**. Every `lms.sh` run launches
a fresh Chromium, so the session was being thrown away client-side on every
single run. It was never "the login expired": nothing was ever kept.

The mechanism to fix it already existed and was never wired up. `sso_login.py`
writes `storage_state.json` — and Playwright's `storage_state()` DOES capture
session cookies, recording them as `expires: -1`. But `grep -rn storage_state`
across this directory returned exactly one hit: the write. Nothing read it back.
That file had been sitting on disk since 2026-08-19 holding a perfectly good
`samlCookie` that no scan ever looked at.

So this module does two things:

    inject(ctx)   before probing, put saved Blackboard cookies back, rewriting
                  the session-only ones to a real future expiry so Chromium
                  accepts them
    persist(ctx)  after a VERIFIED 200, save the Blackboard cookies back out

`persist` is called for **both** sources, which is the real prize: signing into
Blackboard once in Comet now also revives the headless scraper profile, because
the cookies are harvested from Comet and written to the shared state file.

## 🔴 WHY THIS CANNOT CREATE A FALSE GREEN

Injecting a cookie is not authentication. `bb_session._probe()` still gates every
source on a real `GET /users/me` returning 200 with a user id. A stale injected
cookie produces a 401 and the source is rejected exactly as before — the failure
path is untouched. This module can only ever turn a would-be 401 into a 200 that
the server itself granted. It can never turn a 401 into a reported success.

Corollary, and the reason every function here returns a string instead of
raising: **a failure to load or save state must never fail a scan.** Losing
persistence costs one interactive login. Crashing the sweep costs a deadline.
Every entry point is wrapped and degrades to "we just didn't persist this time".

## ⚠ The 7-day horizon is a CLIENT-side hint, not a claim about the server

We rewrite `expires: -1` to now + 7 days so Chromium will store and resend the
cookie. The server decides what is actually still valid, and it may well be
shorter. That is fine and by design: the probe finds out, and a dead session
still reports dead.
"""
import json
import os
import time
from pathlib import Path

PROFILE = Path.home() / ".local/share/ub-blackboard-profile"
STATE_FILE = PROFILE / "storage_state.json"
DOMAIN = "ubaguio.edu"
TTL_DAYS = 7

# The exact field set Playwright's add_cookies accepts. Anything else in a saved
# record (Chromium adds its own keys over time) is dropped rather than passed on.
FIELDS = ("name", "value", "domain", "path", "expires",
          "httpOnly", "secure", "sameSite")


def _is_bb(c) -> bool:
    return DOMAIN in (c.get("domain") or "")


def _trim(c) -> dict:
    return {k: c[k] for k in FIELDS if k in c}


def _read():
    """Return (state_dict, error_or_None). Never raises."""
    empty = {"cookies": [], "origins": []}
    try:
        d = json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return empty, "no saved state file"
    except Exception as e:
        return empty, f"state file unreadable ({type(e).__name__})"
    if not isinstance(d, dict):
        return empty, "state file is not a JSON object"
    if not isinstance(d.get("cookies"), list):
        return empty, "state file has no cookie list"
    d.setdefault("origins", [])
    return d, None


def inject(ctx) -> str:
    """Put saved, unexpired Blackboard cookies into a fresh context.

    Call BEFORE navigating. Returns a one-line detail string for the caller's
    log. Never raises — a bad state file just means nothing gets injected.
    """
    d, err = _read()
    if err:
        return f"no cookies injected — {err}"

    now = time.time()
    usable, stale = [], 0
    for c in d["cookies"]:
        if not _is_bb(c):
            continue
        exp = c.get("expires", -1)
        if exp in (None, -1) or exp <= now:
            stale += 1          # a -1 here means it was saved before this fix landed
            continue
        usable.append(_trim(c))

    if not usable:
        return (f"no cookies injected — {stale} saved Blackboard cookie(s) "
                "are expired or session-only")

    ok = 0
    for c in usable:
        try:
            ctx.add_cookies([c])   # one at a time: one malformed record must not lose the rest
            ok += 1
        except Exception:
            pass
    return f"injected {ok}/{len(usable)} saved Blackboard cookies"


def persist(ctx, source: str) -> str:
    """Harvest Blackboard cookies after a VERIFIED 200 and save them.

    Merges into the existing state file rather than replacing it — that file also
    carries the Google/Classroom cookies that `sso_login.py` captured, and those
    must survive. Only `ubaguio.edu` records are touched.
    """
    try:
        live = ctx.cookies()
    except Exception as e:
        return f"not persisted — could not read cookies ({type(e).__name__})"

    now = time.time()
    horizon = now + TTL_DAYS * 86400
    bb, revived = [], 0
    for c in live:
        if not _is_bb(c):
            continue
        c = _trim(dict(c))
        exp = c.get("expires", -1)
        if exp in (None, -1) or exp <= now:
            c["expires"] = horizon      # the session-only ones — this is the whole point
            revived += 1
        bb.append(c)

    if not bb:
        return "not persisted — no Blackboard cookies in this context"

    d, _ = _read()                       # a corrupt file is simply replaced, not merged
    d["cookies"] = [c for c in d.get("cookies", []) if not _is_bb(c)] + bb

    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, STATE_FILE)      # atomic: a crash mid-write cannot corrupt the state
    except Exception as e:
        return f"not persisted — write failed ({type(e).__name__})"

    return (f"persisted {len(bb)} Blackboard cookies from {source} "
            f"({revived} session-only → +{TTL_DAYS}d)")


def status() -> str:
    """Human-readable summary of what is currently saved. Backs `lms.sh sources`."""
    d, err = _read()
    if err:
        return f"saved session: none — {err}"
    now = time.time()
    bb = [c for c in d["cookies"] if _is_bb(c)]
    if not bb:
        return "saved session: none — no Blackboard cookies in the state file"
    live = [c for c in bb if (c.get("expires") or -1) > now]
    if not live:
        return f"saved session: {len(bb)} Blackboard cookies, ALL EXPIRED client-side"
    soonest = min(c["expires"] for c in live)
    hrs = (soonest - now) / 3600
    names = ", ".join(sorted({c["name"] for c in live}))
    return (f"saved session: {len(live)}/{len(bb)} cookies usable for {hrs:.0f}h "
            f"[{names}]")


if __name__ == "__main__":
    print(status())


# ------------------------------------------------- browserless HTTP transport
BB_API = "https://blackboard.ubaguio.edu/learn/api/public/v1"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class CookieSession:
    """Same contract as bb_session.Session, over HTTP instead of a browser page.

    🆕 2026-09-14 (CLOUD). THE SESSION WAS NEVER DEAD — THE BROWSER WAS.
    A cloud load reported `SESSION DEAD` while a plain `GET /users/me` with these
    very cookies returned **200** with Ralph's student id, on the same VM, in the
    same minute. A Claude Code cloud VM has no direct egress: everything goes
    through an agent proxy, and Chromium's TLS handshake through it is killed
    mid-flight (the proxy's own log: `1794 B sent, 39 B received, tunnel closed
    (1006)`), which reaches Playwright as a bare `net::ERR_CONNECTION_RESET`.
    `urllib` uses the same proxy and the same trusted CA without complaint.

    So reading Blackboard in the cloud needs no browser at all. This is exactly
    the move `bb_oauth.TokenSession` already makes — cookies instead of a bearer
    token — and it slots in ahead of the browser sources for the same reason:
    when it works, playwright is never started.

    🔴 It cannot fake a green. `session()` gates on a real /users/me 200 carrying
    a user id, like every other source in this directory. A dead cookie jar
    reports SESSION DEAD exactly as before.
    """

    def __init__(self, cookies, me):
        self._jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        self.source = "relay-cookies"
        self.me = me
        self.uid = me["id"]

    def get(self, url):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, headers={
            "Cookie": self._jar,
            "User-Agent": UA,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return {"_status": e.code}
        except Exception as e:
            return {"_status": -1, "_error": f"{type(e).__name__}: {e}"[:200]}


def session():
    """(CookieSession|None, detail) built from the saved/relayed cookies."""
    st, err = _read()
    if err:
        return None, err
    cookies = [c for c in st.get("cookies", []) if _is_bb(c)]
    if not cookies:
        return None, "no Blackboard cookies in the saved state"
    probe = CookieSession(cookies, {"id": "?"}).get(f"{BB_API}/users/me")
    if probe.get("_status"):
        return None, f"/users/me -> {probe['_status']} with the saved cookies"
    if not probe.get("id"):
        return None, "/users/me -> 200 but carried no user id"
    return CookieSession(cookies, probe), "OK"
