#!/usr/bin/env python3
"""Blackboard WITHOUT a browser — the REST API's own OAuth2 client credentials.

    ./lms.sh bb-token --status     do we have an app registration, and does it work?
    ./lms.sh bb-token --init KEY SECRET
    ./lms.sh bb-token --ask        print the exact request to send UB's Blackboard admin

## Why this exists (2026-08-29)

Every other school surface came off the browser today — Gmail, Chat, Classroom
and Drive all run on OAuth refresh tokens. Blackboard was the last one that still
needed a live browser session, because its session comes from Google SAML and
dies daily. Ralph asked whether Blackboard even has an API.

**It does, and the toolchain has been using it all along** — every gradebook
column and announcement this directory reads comes from
`/learn/api/public/v1/...`. What was missing was a way to AUTHENTICATE to it
without a browser. Probed live on 2026-08-29:

    GET  /learn/api/public/v1/system/version  -> 200  Learn 4000.21.0 (SaaS)
    GET  /learn/api/public/v1/utilities/echo  -> 401  "not authenticated"
    POST /learn/api/public/v1/oauth2/token    -> 401  {"error":"invalid_client"}

That last line is the important one: the token endpoint is **live and working**.
It refuses us only because our application is not registered on UB's instance.

## 🔴 RALPH DOES NOT WANT UB INVOLVED (2026-08-29)

He would rather not ask the University for anything, and this file's step 2 is
the one thing here that needs them. **That is fine — nothing depends on it.**
Blackboard already works today through the SAML BROWSER session (see
`bb_session.py`), which authenticates as Ralph, through his own login, reading
his own courses, with no admin and no registration. That is the supported
workaround, and it is what runs.

This token path stays here **dormant**: `available()` is False with no
credential, so `bb_session` skips it instantly and falls straight to the browser
sources. It costs nothing sitting disabled. If he ever decides the daily-login
friction is worth one email, `--ask` and `--init` are ready — but the default
posture is: do not send it, keep the browser path.

The daily SAML expiry — the only real friction the token would have removed — is
handled without anyone else by `./lms.sh warm` + `relogin()`, which refresh the
session unattended as long as the Google session is alive.

## 🔴 THE ONE THING THAT CANNOT BE BUILT (only relevant if he opts in)

Two steps stand between here and a working token, and only the first is ours:

  1. Register an application at developer.anthology.com  -> Application ID,
     key and secret. Free, self-service, Ralph can do it.
  2. UB's Blackboard administrator adds that Application ID under
     **Admin -> REST API Integrations**, bound to Ralph's user.

Step 2 is a permission on UB's server. No code reaches around it, and no amount
of retrying changes `invalid_client` into a token. `--ask` prints the exact
request to send them, because the ask is small and specific and that is what
gets it approved: one Application ID, read-only, one user.

Everything else is ready. The moment step 2 lands, `--init` the key and secret
and `bb_session` picks the token source up automatically, ahead of both browser
sources — no window, no SAML, no daily expiry.

## Where the credential goes

`~/.config/ub-school/bb_app.json`, mode 600, OUTSIDE the vault, which
auto-commits and pushes to GitHub every turn. `./lms.sh test-secrets` proves it
stays out.
"""
import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOST = "https://blackboard.ubaguio.edu"
BASE = f"{HOST}/learn/api/public/v1"
APP = Path.home() / ".config/ub-school/bb_app.json"
CACHE = Path.home() / ".cache/ub-lms/bb_token.json"


def available():
    return APP.exists()


def _post_token(key, secret, timeout=25):
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    req = urllib.request.Request(
        f"{BASE}/oauth2/token", data=body, method="POST",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:300]
        # invalid_client is the SPECIFIC signal that step 2 has not been done.
        # Say so rather than reporting a generic auth failure, because the fix
        # is a request to a human, not anything in this file.
        if "invalid_client" in raw:
            return None, ("invalid_client — the application is NOT registered on "
                          "UB's Blackboard instance. Their admin must add it under "
                          "Admin -> REST API Integrations. Run --ask for the wording.")
        return None, f"HTTP {e.code}: {raw}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"[:160]


def token():
    """A live bearer token, or (None, why). Cached until shortly before expiry."""
    if not APP.exists():
        return None, f"no application credential at {APP} — run --ask, then --init"
    try:
        c = json.loads(APP.read_text())
    except Exception as e:
        return None, f"could not read {APP}: {e}"

    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text())
            # 60s of slack: a token that expires mid-scan is worse than one
            # fetched a minute early.
            if cached.get("expires_at", 0) > time.time() + 60:
                return cached["access_token"], None
        except Exception:
            pass

    data, err = _post_token(c["app_key"], c["app_secret"])
    if err:
        return None, err
    tok = data.get("access_token")
    if not tok:
        return None, f"token endpoint returned no access_token: {str(data)[:160]}"
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({
        "access_token": tok,
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
    }))
    CACHE.chmod(0o600)
    return tok, None


class TokenSession:
    """Same contract as bb_session.Session, over HTTP instead of a browser page.

    Callers must not be able to tell which transport they got — that is what
    lets the token source slot in ahead of the browser ones with no change to
    scan/sweep/digest.
    """

    def __init__(self, tok, me):
        self._tok = tok
        self.source = "token"
        self.me = me
        self.uid = me["id"]

    def get(self, url):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._tok}"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return {"_status": e.code}
        except Exception as e:
            return {"_status": -1, "_error": f"{type(e).__name__}: {e}"[:200]}


def session():
    """(TokenSession|None, detail). Gated on a real /users/me 200, like every
    other source in this directory — a token that parses is not a token that
    works."""
    tok, err = token()
    if err:
        return None, err
    probe = TokenSession(tok, {"id": "?"}).get(f"{BASE}/users/me")
    if probe.get("_status"):
        return None, f"/users/me -> {probe['_status']} with a valid token"
    if not probe.get("id"):
        return None, "/users/me -> 200 but carried no user id"
    return TokenSession(tok, probe), "OK"


ASK = """\
To: UB Blackboard / LMS administrator (via the IT Help Desk)
Subject: Request for a read-only REST API integration on Blackboard Learn

Good day,

I am <YOUR NAME> (Student No. <STUDENT NO>), <PROGRAM>,
Section <SECTION>. I maintain a small personal tool that reads my OWN Blackboard
deadlines so that I do not miss submissions, and I would like to ask whether it
may be registered against the University's Blackboard Learn instance.

What I am requesting:

  · Registration of one developer application under
    Administrator Panel -> REST API Integrations
  · Application ID: <PASTE THE APPLICATION ID FROM developer.anthology.com>
  · Bound to my own user account only (<STUDENT NO>)
  · Read-only use: I query only my own courses, gradebook columns and
    announcements -- the same information I already see when I log in.

Why it helps: without this, the tool has to sign in through a browser session
that expires daily. A registered integration is the supported, auditable way to
do the same reading, and it can be revoked by the University at any time.

I am happy to provide the Application ID, to limit it further, or to discuss any
security requirements you may have.

Thank you very much for considering this request.

Respectfully,
<YOUR NAME>
Student No. <STUDENT NO> | <PROGRAM>, <SECTION>
<STUDENT EMAIL>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--ask", action="store_true",
                    help="print the request to send UB's Blackboard admin")
    ap.add_argument("--init", nargs=2, metavar=("APP_KEY", "APP_SECRET"))
    a = ap.parse_args()

    if a.ask:
        print(ASK)
        print("Step 1 (yours, now):    https://developer.anthology.com — register a")
        print("                        Developer application, note the Application ID,")
        print("                        key and secret.")
        print("Step 2 (UB's admin):    the message above.")
        print("Step 3 (when approved): ./lms.sh bb-token --init <KEY> <SECRET>")
        return 0

    if a.init:
        APP.parent.mkdir(parents=True, exist_ok=True)
        APP.parent.chmod(0o700)
        APP.write_text(json.dumps(
            {"app_key": a.init[0], "app_secret": a.init[1]}, indent=1) + "\n")
        APP.chmod(0o600)
        print(f"✅ wrote {APP} (mode 600, outside the vault)")
        print("   Verify: ./lms.sh bb-token --status")
        return 0

    # --status (default)
    print(f"# Blackboard REST — {HOST}")
    try:
        with urllib.request.urlopen(f"{BASE}/system/version", timeout=20) as r:
            v = json.loads(r.read().decode()).get("learn", {})
        print(f"  API reachable   : ✅ Learn {v.get('major')}.{v.get('minor')}.{v.get('patch')}")
    except Exception as e:
        print(f"  API reachable   : 🔴 {type(e).__name__}")
    if not APP.exists():
        print(f"  app credential  : ABSENT ({APP})")
        print("  → Not an error yet. Run `./lms.sh bb-token --ask` for the two steps.")
        return 1
    sess, detail = session()
    if sess:
        nm = (sess.me.get("name") or {})
        print(f"  token session   : ✅ {nm.get('given','')} {nm.get('family','')}".rstrip())
        print("  → Blackboard no longer needs a browser.")
        return 0
    print(f"  token session   : 🔴 {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
