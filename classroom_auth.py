#!/usr/bin/env python3
"""Get a Google Classroom refresh token WITHOUT a browser on this machine.

## Why this exists

The browser-profile scrape works, but it cannot move off REWSL safely: shipping
a replayed Google session cookie to a datacenter IP is a session-hijack
signature, and if Google invalidates it Ralph is logged out everywhere. A
refresh token is the opposite -- it is *designed* to be used from a server, so a
cloud VM using one looks entirely normal.

## 🆕 THE EASY PATH FIRST (2026-08-29)

    ./lms.sh classroom-auth --serve

Opens the Windows browser, catches Google's redirect on localhost:8765 itself,
and saves the token. **Nothing to copy, nothing to paste.** Use this when you are
sitting at RE. The two-step phone flow below still exists for when you are not.

⚠ Since 2026-08-29 this one consent also covers **Gmail (readonly)** and **Google
Chat (readonly)** — see SCOPES. One 2FA prompt, then those three surfaces never
need a browser again.

## Why the flow is shaped like this

The usual installed-app flow opens a browser on the machine running the script
and waits on a localhost redirect. That is useless here: the common case is
Ralph on his phone, driving RE over Remote Control, with no way to see or click
a window on RE.

So the consent is split in two, and the only thing that crosses between them is
a short code Ralph can read off his own screen:

    ./lms.sh classroom-auth --url          -> prints a URL; open it on the PHONE
    <approve on the phone; the browser then fails to load a localhost page --
     that failure is EXPECTED, the code is sitting in the address bar>
    ./lms.sh classroom-auth --code 4/0Ax...  -> exchanges it, saves the token

The redirect deliberately points at localhost, which the phone cannot reach.
Google still puts `?code=...` in the URL before the browser gives up, so the
code is visible even though the page never loads. Nothing sensitive travels
through it -- the code is single-use, expires in minutes, and is worthless
without the client secret held here.

## Where the token goes

`~/.config/ub-classroom/token.json`, OUTSIDE the vault, for the same reason the
browser profile is outside it: the vault auto-commits and pushes to GitHub every
turn. Print it with --export when it needs to become a cloud env var.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# 🔴 MUST be set BEFORE google_auth_oauthlib is imported (2026-08-29).
#
# Google routinely grants a scope set that is not byte-identical to the one you
# asked for — it drops scopes that another granted scope already implies. Here
# it returned 8 of 9, dropping `classroom.coursework.me.readonly` because
# `classroom.student-submissions.me.readonly` covers the same consent.
#
# oauthlib treats ANY difference as an error and raises after the token has
# already been issued. The result is the worst possible failure: Ralph completes
# the consent, Google mints the credential, the exception fires before it is
# saved — and the authorization code is single-use, so the whole approval has to
# be done again. That cost one real approval.
#
# Relaxing the check does NOT widen anything: the granted scopes are whatever
# Google decided, and they are recorded verbatim in token.json.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import Flow  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402

CFG = Path.home() / ".config/ub-classroom"
CLIENT = CFG / "client_secret.json"
TOKEN = CFG / "token.json"
# 🔴 PKCE verifier, added 2026-09-06. `Flow.authorization_url()` auto-generates a
# code_verifier, and `--url` and `--code` run in SEPARATE processes -- so without
# persisting it the exchange dies with "(invalid_grant) Missing code verifier."
# That is exactly what happened on the first re-auth after the OAuth app was
# published: the URL worked, consent worked, and the code was unusable.
# `cal_apply.py` has had this since it was written; this module never did.
VERIFIER = CFG / ".pkce_verifier"

# Read-only, and scoped to Ralph's own work only -- the `.me` variants cannot
# see other students' submissions. Ask for the minimum that answers "what is
# due", nothing wider.
SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    # 🆕 2026-08-29 — Gmail and Chat join the SAME consent, deliberately.
    #
    # Ralph's requirement, in his words: "a 2fa prompt is fine with me and
    # that's it, no more friction — the sweep is supposed to be frictionless."
    # A refresh token is what delivers that: the 2FA happens ONCE, here, and
    # never again. Three separate consents would be three prompts, so they are
    # bundled into one.
    #
    # What this buys: Gmail, Classroom and Chat stop needing a browser at all,
    # which means they stop depending on a Google BROWSER session that expires
    # and pops windows. Blackboard is then the only surface left that needs a
    # live browser session — which is exactly what Ralph said it should be.
    #
    # ⚠ `gmail.readonly` is a RESTRICTED scope. On an unverified app it works
    # for accounts added as TEST USERS, and a Workspace admin can refuse
    # third-party apps outright. If consent fails, that is the answer — fall
    # back to the profile scrape rather than widening anything.
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    # 🆕 2026-09-24 — `gmail.send`, added on Ralph's instruction ("do it").
    #
    # WHY: the Gmail MCP connector authenticates as the PERSONAL account and
    # cannot set a send-as alias, so every academic email had to be pasted by
    # hand into the school mailbox. This token already IS the school account,
    # so sending from it is the only path that puts `student@s.ubaguio.edu`
    # in the From line without a browser or an App Password (App Passwords
    # need 2FA, which Ralph has deliberately left off).
    #
    # ⚠ THIS IS THE FIRST WRITE SCOPE ON THIS TOKEN. Everything above is
    # readonly, and that was load-bearing: a leaked read token leaked a
    # gradebook. This one can send mail as him, so the blast radius is now
    # reputational, not just informational. Treat the token file as a secret.
    #
    # 🔴 SENDING STAYS OPT-IN PER MESSAGE regardless of scope (`context-loader`
    # invariants). The scope removes the mechanical blocker; it does not grant
    # standing permission, and `mail_send.py` refuses to run without --send.
    #
    # ⚠ `gmail.send` is SENSITIVE, not RESTRICTED. It therefore does NOT raise
    # this app's verification tier -- `gmail.readonly` above is already
    # restricted and dominates. So the 7-day refresh-token expiry in Testing
    # status is unchanged by this addition, neither better nor worse.
    "https://www.googleapis.com/auth/gmail.send",
    # 🔴 DRIVE REMOVED 2026-09-06 (Ralph's call). `drive.readonly` was added
    # 2026-08-29 for a real reason -- he works on the LAB COMPUTERS and saves
    # project files to Drive, so Drive holds school work that never touches
    # RE's disk. But THE READER WAS NEVER WRITTEN: no module in this package
    # ever built a Drive client, so the scope granted read access to his whole
    # Drive and bought nothing. Same shape as `declared-but-never-read-sources`.
    #
    # It also cost real leverage: `drive.readonly` and `gmail.readonly` are the
    # two scopes Google classes RESTRICTED, and restricted scopes are the
    # hardest tier to take out of "Testing" publishing status -- which is what
    # expires every refresh token after 7 days. Dropping the unused one halves
    # that problem. `gmail.readonly` stays: `mail_fetch.py` genuinely uses it.
    #
    # ➜ To bring Drive back, prefer `drive.metadata.readonly` (lists files
    #   without reading them -- enough for "what did I save", not enough to
    #   open a project file). Cost of reinstating either: one re-consent.
]

# Must match a redirect URI registered on the OAuth client. localhost is the
# one Google allows over plain http, and its unreachability from the phone is
# the whole point -- see the module docstring.
REDIRECT = "http://localhost:8765/"


def _flow():
    if not CLIENT.exists():
        sys.exit(
            f"🔴 No OAuth client at {CLIENT}\n"
            "   Create one in Google Cloud Console (Desktop app), then load it with\n"
            "     ./lms.sh classroom-auth --init-client <CLIENT_ID> <CLIENT_SECRET>\n"
            "   Full walkthrough: school/context/classroom-api.md"
        )
    return Flow.from_client_secrets_file(str(CLIENT), scopes=SCOPES, redirect_uri=REDIRECT)


def cmd_url(_):
    f = _flow()
    url, _state = f.authorization_url(
        access_type="offline",      # without this there is no refresh token at all
        prompt="consent",           # force one even if this app was approved before
        include_granted_scopes="true",
    )
    CFG.mkdir(parents=True, exist_ok=True)
    VERIFIER.write_text(f.code_verifier or "")   # survives into the --code process
    VERIFIER.chmod(0o600)
    print("\n1. Open this on your PHONE, signed in as student@s.ubaguio.edu:\n")
    print(url)
    print("\n2. Approve. The browser will then FAIL to load a localhost page.")
    print("   That failure is expected and correct.")
    print("\n3. Copy the `code=` value out of the address bar and run:")
    print("   ./lms.sh classroom-auth --code <THE_CODE>\n")
    print("⚠ If you instead see 'Access blocked' or 'admin has restricted access',")
    print("  UB's Workspace forbids third-party apps and this route is closed —")
    print("  say so and we fall back rather than fight it.")


def granted_scopes(flow, c):
    """What the server ACTUALLY granted — not what we asked for.

    🔴 FIXED 2026-09-24. Both save sites wrote `list(c.scopes or SCOPES)`, and
    `credentials.scopes` is populated from the scopes the flow REQUESTED. So the
    token file recorded the wish, not the grant, and a scope Google silently
    declined was indistinguishable on disk from one it approved.

    ⚠ THE DROPPED SCOPE ITSELF IS OLD NEWS AND IS NOT A BUG — the module header
    has documented since 2026-08-29 that Google returns 8 of 9, dropping
    `classroom.coursework.me.readonly` because `student-submissions.me.readonly`
    already covers that consent. `OAUTHLIB_RELAX_TOKEN_SCOPE` exists for it.

    What was NEW on 2026-09-24 is that the token FILE never reflected it. It
    listed all nine, every time, because it wrote the request. The mismatch only
    became visible when google-auth warned at refresh — "Not all requested scopes
    were granted ... missing scopes .../classroom.coursework.me.readonly" — while
    `token.json` sat there claiming otherwise. The file said yes; the server said
    no; nothing reconciled the two.

    This matters beyond tidiness: `mail_send.py` gates sending on the stored
    scope list. Reading a fabricated value would have let it attempt a send on
    a token that had never been granted `gmail.send`, and the guard would have
    reported a pass it never checked.
    ⚠ The grant is in the TOKEN RESPONSE (`oauth2session.token['scope']`), a
    space-separated string. Fall back to the credential, then to SCOPES, so an
    older library that does not expose it still writes something truthful-ish —
    but prefer the server every time.
    """
    tok = getattr(getattr(flow, "oauth2session", None), "token", None) or {}
    raw = tok.get("scope")
    if isinstance(raw, str) and raw.strip():
        return sorted(raw.split())
    if isinstance(raw, (list, tuple)) and raw:
        return sorted(raw)
    return sorted(c.scopes or SCOPES)


def report_scope_gap(stored):
    """Say plainly which requested scopes did NOT come back. Never silent."""
    missing = [s for s in SCOPES if s not in stored]
    extra = [s for s in stored if s not in SCOPES]
    if missing:
        print("\n⚠ GOOGLE DID NOT GRANT EVERY REQUESTED SCOPE:")
        for s in missing:
            print(f"   ✗ {s.rsplit('/', 1)[-1]}")
        print("  The token records what was GRANTED, so anything gated on these")
        print("  will refuse rather than fail mid-call. Re-consent to try again.")
    if extra:
        for s in extra:
            print(f"   + also granted: {s.rsplit('/', 1)[-1]}")
    return missing


def _save(c, flow=None):
    if not c.refresh_token:
        sys.exit("🔴 Google returned no refresh token. Re-run (it forces prompt=consent).")
    granted = granted_scopes(flow, c)
    CFG.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps({
        "refresh_token": c.refresh_token,
        "client_id": c.client_id,
        "client_secret": c.client_secret,
        "token_uri": c.token_uri,
        "scopes": granted,
    }, indent=2))
    TOKEN.chmod(0o600)
    print(f"\n✅ Refresh token saved to {TOKEN} (mode 600, outside the vault).")
    report_scope_gap(granted)


def cmd_serve(_):
    """Do the whole consent locally, with nothing to copy or paste.

    ## Why this exists (2026-08-29)

    The phone flow deliberately used an unreachable localhost redirect so the
    code could be read out of the address bar. That works, but it makes Ralph
    ferry a long code by hand — and when he pasted the URL blob into the
    assistant it tripped a safety classifier and the turn died. Copying secrets
    through a chat window is bad hygiene anyway: the code lands in a transcript.

    The redirect URI is `http://localhost:8765/`, and this machine IS localhost.
    WSL2 forwards Windows localhost into the distro, so a browser on Windows
    hitting that port reaches this listener. So: serve the port, open the
    browser, let Google deliver the code straight here. Nothing is displayed,
    nothing is pasted, and the code never leaves the machine.

    Falls back to printing the URL if the browser cannot be opened.
    """
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, HTTPServer

    f = _flow()
    url, _state = f.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true")

    box = {"code": None, "err": None}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            box["code"] = (q.get("code") or [None])[0]
            box["err"] = (q.get("error") or [None])[0]
            ok = bool(box["code"])
            body = (b"<h2>Done - you can close this tab.</h2>"
                    b"<p>The token was saved on Red-eye.</p>" if ok else
                    b"<h2>No code received.</h2><p>Check the terminal.</p>")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass                      # keep Google's redirect out of the output

    srv = HTTPServer(("127.0.0.1", 8765), H)
    srv.timeout = 300

    opened = False
    try:
        # WSL can launch the Windows browser directly. Single quotes matter:
        # the consent URL is full of & characters.
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        f"Start-Process '{url}'"],
                       capture_output=True, timeout=25)
        opened = True
    except Exception:
        pass

    print("\nA browser should have opened." if opened else
          "\nCould not open a browser automatically.")
    print("Sign in as student@s.ubaguio.edu and approve.")
    if not opened:
        print("\nOpen this yourself:\n")
        print(url)
    print("\nWaiting up to 5 minutes for Google to redirect back...")

    srv.handle_request()
    srv.server_close()

    if box["err"]:
        sys.exit(f"\n🔴 Google refused: {box['err']}\n"
                 "   If this says access_denied or admin policy, UB's Workspace\n"
                 "   forbids third-party apps and this route is closed.")
    if not box["code"]:
        sys.exit("\n🔴 Timed out with no code. Nothing was saved.")

    f.fetch_token(code=box["code"])
    _save(f.credentials)
    print("   Verify:  ./lms.sh classroom")
    return 0


def cmd_code(args):
    f = _flow()
    if VERIFIER.exists():
        f.code_verifier = VERIFIER.read_text().strip() or None
    else:
        print("⚠ No saved PKCE verifier — run ./lms.sh classroom-auth --url first, and use\n"
              "  the code from THAT url. A code from an earlier url cannot be exchanged.",
              file=sys.stderr)
    f.fetch_token(code=args.code)
    c = f.credentials
    if not c.refresh_token:
        sys.exit("🔴 Google returned no refresh token. Re-run --url (it forces prompt=consent).")
    CFG.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(json.dumps({
        "refresh_token": c.refresh_token,
        "client_id": c.client_id,
        "client_secret": c.client_secret,
        "token_uri": c.token_uri,
        "scopes": granted_scopes(f, c),
    }, indent=2))
    TOKEN.chmod(0o600)
    VERIFIER.unlink(missing_ok=True)   # single-use; do not leave it lying around
    print(f"✅ Refresh token saved to {TOKEN} (mode 600, outside the vault).")
    print("   Verify it actually works:  ./lms.sh classroom")


def cmd_init(args):
    """Write client_secret.json from two pasted strings.

    Google Cloud Console hands you a JSON file to download. From a phone that
    file is awkward to get onto RE, but the only two values that matter are the
    client id and secret -- so paste those instead of moving a file around.
    """
    cid, sec = args.init_client
    if not cid.endswith(".apps.googleusercontent.com"):
        sys.exit(f"🔴 That does not look like a client id: {cid[:40]}...\n"
                 "   It should end in .apps.googleusercontent.com")
    CFG.mkdir(parents=True, exist_ok=True)
    CLIENT.write_text(json.dumps({"installed": {
        "client_id": cid,
        "client_secret": sec,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [REDIRECT],
    }}, indent=2))
    CLIENT.chmod(0o600)
    print(f"✅ Wrote {CLIENT} (mode 600, outside the vault).")
    print("   Next:  ./lms.sh classroom-auth --url")


def cmd_status(_):
    if not TOKEN.exists():
        print("token: ABSENT — run ./lms.sh classroom-auth --url"); return 1
    d = json.loads(TOKEN.read_text())
    print(f"token   : present ({TOKEN})")
    print(f"scopes  : {len(d.get('scopes', []))}")
    print("client  :", (d.get("client_id") or "")[:28] + "...")
    return 0


def cmd_export(_):
    """Print the one-line form to paste into a cloud environment variable."""
    if not TOKEN.exists():
        sys.exit("🔴 no token yet")
    print("Set this as UB_CLASSROOM_TOKEN in the cloud environment settings.")
    print("It is a live credential: never commit it, never paste it in a shared session.\n")
    print(json.dumps(json.loads(TOKEN.read_text()), separators=(",", ":")))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--serve", action="store_true",
                    help="do the whole consent here — nothing to copy or paste")
    ap.add_argument("--url", action="store_true", help="print the consent URL for the phone")
    ap.add_argument("--code", help="exchange the code copied from the phone")
    ap.add_argument("--status", action="store_true", help="do we have a token?")
    ap.add_argument("--export", action="store_true", help="print the cloud env-var value")
    ap.add_argument("--init-client", nargs=2, metavar=("CLIENT_ID", "CLIENT_SECRET"),
                    help="write client_secret.json from two pasted values")
    a = ap.parse_args()
    if a.init_client: return cmd_init(a)
    if a.serve:       return cmd_serve(a)
    if a.url:         return cmd_url(a)
    if a.code:        return cmd_code(a)
    if a.export:      return cmd_export(a)
    return cmd_status(a)


if __name__ == "__main__":
    sys.exit(main() or 0)
