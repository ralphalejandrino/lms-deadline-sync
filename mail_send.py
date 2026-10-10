#!/usr/bin/env python3
"""Send mail AS the school account, student@s.ubaguio.edu.

    ./lms.sh send --to X --subject S --body-file B          # DRY RUN, prints it
    ./lms.sh send --to X --subject S --body-file B --send   # actually sends
    ./lms.sh send ... --attach FILE --attach FILE           # attachments
    ./lms.sh send --self-test --send                        # probe to himself

## Why this exists (2026-09-24)

Every academic email had to be pasted by hand into the school mailbox, because
the Gmail MCP connector authenticates as Ralph's PERSONAL account and cannot set
a send-as alias -- the API does not expose one. This token already IS the school
account, so sending from it is the only path that puts `student@s.ubaguio.edu`
in the From line without a browser or an App Password (and App Passwords need
2FA, which Ralph has deliberately left off).

Added on his instruction, 2026-09-24. `gmail.send` joined `classroom_auth.SCOPES`
in the same change; a token minted before that date will NOT have it and this
script says so rather than failing at the API.

## 🔴 THE THREE RAILS

1. **DRY RUN IS THE DEFAULT.** Nothing leaves without an explicit `--send`.
   A missing flag prints the message and exits 0 -- it does not "helpfully" send.
2. **IDENTITY IS ASSERTED, NEVER ASSUMED.** `getProfile` must return the school
   address before a single byte is sent, the same guard `mail_fetch.fetch_api`
   uses. Sending from the wrong account is the one failure here that cannot be
   taken back, and it is exactly the shape of the MCP connector's own bug --
   which SENT four job applications while reporting them as drafts
   ([[gmail-create-draft-sends]]).
3. **THE SENT MESSAGE IS READ BACK.** A 200 with an id is not proof of what was
   sent; the message is re-fetched and its From/To/Subject printed from the
   SERVER's copy, not from what we think we posted.

⚠ **The scope is not standing permission.** `context-loader`'s invariants say
mail WRITES are opt-in per request: Ralph says yes in the turn that sends. This
script removes the mechanical blocker only. Do not wire it into a sweep, a loop
or a hook.

⚠ **Sending is irreversible.** There is no unsend here -- Gmail's undo lives in
the web UI and does not apply to an API send.
"""
from __future__ import annotations

import argparse
import base64
import os
import mimetypes
import sys
from email.message import EmailMessage
from pathlib import Path

import gcreds

SCHOOL_ACCOUNT = os.environ.get("LMS_SCHOOL_ACCOUNT", "student@s.ubaguio.edu")
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

# Ralph's standing subject for anything UB. The `email-draft` skill owns the
# rule; it is repeated here only as a DEFAULT, never as a second source of
# truth -- pass --subject to override.
DEFAULT_SUBJECT = os.environ.get("LMS_MAIL_SUBJECT", "SECTION - Surname, Given Name M.")


class SendError(RuntimeError):
    pass


def _credentials():
    if not gcreds.available("classroom"):
        raise SendError(
            "no token: neither $UB_CLASSROOM_TOKEN nor ~/.config/ub-classroom/token.json\n"
            "   fix: ./lms.sh classroom-auth --url"
        )
    cr = gcreds.credentials("classroom", quiet=True)
    scopes = list(cr.scopes or [])
    if scopes and SEND_SCOPE not in scopes:
        raise SendError(
            "THIS TOKEN CANNOT SEND -- it predates the gmail.send scope.\n"
            f"   has: {', '.join(s.rsplit('/', 1)[-1] for s in scopes)}\n"
            "   fix: re-consent once, which mints a token carrying it:\n"
            "     ./lms.sh classroom-auth --url     (open it, approve)\n"
            "     ./lms.sh classroom-auth --code <code>\n"
            "   ⚠ Re-consent REPLACES the token; the old one keeps working until then."
        )
    return cr


def build_message(to, subject, body, attachments=(), cc=None, sender=SCHOOL_ACCOUNT):
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)

    for p in attachments:
        p = Path(p)
        if not p.is_file():
            raise SendError(f"attachment not found: {p}")
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(p.read_bytes(), maintype=maintype,
                           subtype=subtype, filename=p.name)
    return msg


def send(msg, cr, verify=True):
    """POST the message, then read the SERVER's copy back. Returns that copy."""
    from googleapiclient.discovery import build as gbuild

    svc = gbuild("gmail", "v1", credentials=cr, cache_discovery=False)

    # Rail 2. Before anything leaves.
    who = svc.users().getProfile(userId="me").execute().get("emailAddress", "")
    if who.lower() != SCHOOL_ACCOUNT.lower():
        raise SendError(f"WRONG ACCOUNT: Gmail API is {who}, expected {SCHOOL_ACCOUNT}")

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    mid = sent.get("id")
    if not mid:
        raise SendError(f"send returned no message id: {sent!r}")

    if not verify:
        return {"id": mid, "labelIds": sent.get("labelIds", [])}

    # Rail 3. What the server actually stored, not what we think we posted.
    back = svc.users().messages().get(
        userId="me", id=mid, format="metadata",
        metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
    ).execute()
    hdrs = {h["name"]: h["value"]
            for h in back.get("payload", {}).get("headers", [])}
    return {"id": mid, "threadId": back.get("threadId"),
            "labelIds": back.get("labelIds", []), "headers": hdrs,
            "snippet": back.get("snippet", "")}


def _preview(msg, attachments):
    line = "─" * 66
    print(line)
    for h in ("From", "To", "Cc", "Subject"):
        if msg[h]:
            print(f"{h + ':':<9}{msg[h]}")
    if attachments:
        print(f"{'Attach:':<9}" + ", ".join(Path(a).name for a in attachments))
    print(line)
    print(msg.get_content().rstrip())
    print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Send mail as the school account.")
    ap.add_argument("--to", action="append", default=[])
    ap.add_argument("--cc", action="append", default=[])
    ap.add_argument("--subject", default=DEFAULT_SUBJECT)
    ap.add_argument("--body-file", type=Path)
    ap.add_argument("--body", default=None)
    ap.add_argument("--attach", action="append", default=[])
    ap.add_argument("--send", action="store_true",
                    help="actually send. Without it this is a DRY RUN.")
    ap.add_argument("--self-test", action="store_true",
                    help="probe addressed to the school account itself")
    a = ap.parse_args(argv)

    if a.self_test:
        a.to = a.to or [SCHOOL_ACCOUNT]
        a.subject = "lms.sh send — self test"
        a.body = a.body or (
            "Negative control for ./lms.sh send.\n\n"
            "If this arrived, the school account can send through the API and "
            "the From line above is the school address rather than the personal "
            "one. Safe to delete.\n"
        )

    if not a.to:
        ap.error("--to is required")
    body = a.body if a.body is not None else (
        a.body_file.read_text(encoding="utf-8") if a.body_file else None)
    if body is None:
        ap.error("one of --body or --body-file is required")

    try:
        msg = build_message(a.to, a.subject, body, a.attach, a.cc or None)
    except SendError as e:
        sys.exit(f"🔴 {e}")

    _preview(msg, a.attach)

    if not a.send:
        print("⚠ DRY RUN — nothing was sent. Re-run with --send to send it.")
        return 0

    try:
        cr = _credentials()
        res = send(msg, cr)
    except SendError as e:
        sys.exit(f"🔴 {e}")

    h = res.get("headers", {})
    print("✅ SENT — read back from the server:")
    print(f"   id      : {res['id']}")
    print(f"   From    : {h.get('From', '(absent)')}")
    print(f"   To      : {h.get('To', '(absent)')}")
    print(f"   Subject : {h.get('Subject', '(absent)')}")
    print(f"   labels  : {', '.join(res.get('labelIds', [])) or '(none)'}")
    if SCHOOL_ACCOUNT.lower() not in h.get("From", "").lower():
        sys.exit("🔴 From line is NOT the school account — investigate before sending more.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
