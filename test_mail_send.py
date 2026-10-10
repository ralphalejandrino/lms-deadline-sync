#!/usr/bin/env python3
"""Tests for mail_send — the ONLY write path in this package.

Run: ./test_mail_send.py   (or ./lms.sh test)

🔴 Everything else here reads. This module SENDS MAIL AS RALPH, from his
university address, and a send cannot be taken back. So the tests that matter
are the negative ones: the rails must hold when the inputs are wrong.

No test in this file touches the network or the real token. The Gmail service is
a stub, and the property under test is what `send()` does BEFORE and AFTER the
stubbed call — not that Google works.

The failure this guards against is concrete and has happened on the neighbouring
connector: `create_draft` SENT four job applications while reporting them as
drafts. A send path that looks successful while doing the wrong thing is the
whole risk here.
"""
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import mail_send

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '🔴'} {name}" + (f"  — {detail}" if detail and not cond else ""))


class _Creds:
    def __init__(self, scopes):
        self.scopes = scopes


SCHOOL = mail_send.SCHOOL_ACCOUNT
FULL = ["https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly"]


class _Svc:
    """Minimal Gmail stub. Records what it was asked to do."""

    def __init__(self, profile=SCHOOL, stored_from=None, no_id=False):
        self._profile = profile
        self._stored_from = stored_from or SCHOOL
        self._no_id = no_id
        self.sent_raw = None

    # -- users().getProfile().execute() / messages().send() / .get()
    def users(self):
        return self

    def getProfile(self, userId=None):
        return _Exec({"emailAddress": self._profile})

    def messages(self):
        return self

    def send(self, userId=None, body=None):
        self.sent_raw = (body or {}).get("raw")
        return _Exec({} if self._no_id else {"id": "MID1", "labelIds": ["SENT"]})

    def get(self, userId=None, id=None, format=None, metadataHeaders=None):
        return _Exec({
            "threadId": "T1", "labelIds": ["SENT"], "snippet": "s",
            "payload": {"headers": [
                {"name": "From", "value": self._stored_from},
                {"name": "To", "value": SCHOOL},
                {"name": "Subject", "value": "x"},
            ]},
        })


class _Exec:
    def __init__(self, v):
        self._v = v

    def execute(self):
        return self._v


def _patch(svc):
    """Swap the googleapiclient builder for the stub, for one call."""
    import types
    mod = types.ModuleType("googleapiclient.discovery")
    mod.build = lambda *a, **k: svc
    pkg = types.ModuleType("googleapiclient")
    pkg.discovery = mod
    sys.modules["googleapiclient"] = pkg
    sys.modules["googleapiclient.discovery"] = mod


def main():
    print("# mail_send — the message")
    msg = mail_send.build_message([SCHOOL], "Subj", "Body here.")
    check("From is the SCHOOL address, never the personal one",
          msg["From"] == SCHOOL, msg["From"])
    check("body survives intact", "Body here." in msg.get_content())
    check("default subject is Ralph's standing UB format",
          mail_send.DEFAULT_SUBJECT == "SECTION - Surname, Given Name M.")

    try:
        mail_send.build_message([SCHOOL], "S", "B", attachments=["/nope/missing.pdf"])
        check("🔴 a missing attachment is refused, not silently dropped", False)
    except mail_send.SendError:
        check("🔴 a missing attachment is refused, not silently dropped", True)

    print("# mail_send — the scope guard")
    import gcreds
    real_avail, real_creds = gcreds.available, gcreds.credentials
    try:
        gcreds.available = lambda k: True
        gcreds.credentials = lambda k, **kw: _Creds(
            ["https://www.googleapis.com/auth/gmail.readonly"])
        try:
            mail_send._credentials()
            check("🔴 a token without gmail.send is refused", False)
        except mail_send.SendError as e:
            check("🔴 a token without gmail.send is refused", True)
            check("…and the refusal names the re-consent fix",
                  "classroom-auth" in str(e))

        gcreds.credentials = lambda k, **kw: _Creds(FULL)
        try:
            mail_send._credentials()
            check("a token WITH gmail.send is accepted (positive control)", True)
        except mail_send.SendError as e:
            check("a token WITH gmail.send is accepted (positive control)", False, str(e))

        gcreds.available = lambda k: False
        try:
            mail_send._credentials()
            check("🔴 no token at all is refused", False)
        except mail_send.SendError:
            check("🔴 no token at all is refused", True)
    finally:
        gcreds.available, gcreds.credentials = real_avail, real_creds

    print("# mail_send — the account guard (rail 2)")
    svc = _Svc(profile="you@gmail.com")
    _patch(svc)
    try:
        mail_send.send(msg, _Creds(FULL))
        check("🔴 WRONG ACCOUNT is refused", False)
    except mail_send.SendError as e:
        check("🔴 WRONG ACCOUNT is refused", "WRONG ACCOUNT" in str(e))
    check("🔴 …and NOTHING was sent when it refused", svc.sent_raw is None)

    print("# mail_send — the read-back (rail 3)")
    svc = _Svc()
    _patch(svc)
    res = mail_send.send(msg, _Creds(FULL))
    check("a good send returns the server's own headers",
          res.get("headers", {}).get("From") == SCHOOL)
    check("…and it actually posted something", svc.sent_raw is not None)

    svc = _Svc(stored_from="you@gmail.com")
    _patch(svc)
    res = mail_send.send(msg, _Creds(FULL))
    check("🔴 a send whose STORED From is the personal account is detectable",
          mail_send.SCHOOL_ACCOUNT.lower() not in res["headers"]["From"].lower(),
          "main() exits non-zero on this")

    svc = _Svc(no_id=True)
    _patch(svc)
    try:
        mail_send.send(msg, _Creds(FULL))
        check("🔴 a send returning no message id is an error", False)
    except mail_send.SendError:
        check("🔴 a send returning no message id is an error", True)

    print("# mail_send — DRY RUN is the default (rail 1)")
    svc = _Svc()
    _patch(svc)
    rc = mail_send.main(["--to", SCHOOL, "--body", "dry"])
    check("no --send → exits 0", rc == 0)
    check("🔴 no --send → NOTHING reached the API", svc.sent_raw is None)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
