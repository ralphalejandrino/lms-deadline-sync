#!/usr/bin/env python3
"""ONE way to load a Google refresh token — environment first, file second.

## Why this exists (2026-09-14)

Four scripts built their own `Credentials` from a token file: `classroom_api`,
`chat_api`, `mail_fetch` and `cal_apply`. Only `classroom_api` had learned to
read `$UB_CLASSROOM_TOKEN` first — the one path a cloud environment can supply,
because `~/.config` never exists on a fresh VM. So a cloud session with the
variable set could read Classroom and was STILL blind to Chat and school mail,
which ride on the very same token. One loader, one rule, every caller.

## The rule

  1. the environment variable, if it is set
  2. the local file, if it exists
  3. otherwise `NoCredential`, naming both places and the fix

🔴 A variable that is SET but unparseable is an error, never a silent fall-back
to the file. Falling back would make a broken cloud setting look fine on RE —
where the file exists — and fail only in the cloud, where it is hardest to see.

The source that answered is printed on stderr (`# credential: $UB_CLASSROOM_TOKEN`)
unless the caller asks for quiet, so a reader can always tell which one it was.

    python gcreds.py --status              which source each kind would use
    python gcreds.py --export classroom    one-line JSON for a cloud env var
"""
import argparse
import json
import os
import sys
from pathlib import Path

KINDS = {
    # ONE token, school account: Classroom + gmail.readonly + chat.*.readonly.
    "classroom": ("UB_CLASSROOM_TOKEN", ".config/ub-classroom/token.json",
                  "./lms.sh classroom-auth --url"),
    # calendar.events on the PERSONAL account — different account, different token.
    "calendar": ("UB_CALENDAR_TOKEN", ".config/ub-calendar/token.json",
                 "./lms.sh cal-auth --url"),
}
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
REQUIRED = ("refresh_token", "client_id", "client_secret")


class NoCredential(RuntimeError):
    """No usable credential. The message names where it looked and the fix."""


def token_path(kind):
    # Resolved at call time, not import time, so HOME can be redirected in tests.
    return Path.home() / KINDS[kind][1]


def _env(kind):
    return os.environ.get(KINDS[kind][0], "").strip()


def available(kind):
    return bool(_env(kind)) or token_path(kind).exists()


def load(kind):
    """Return (token_dict, source_label) or raise NoCredential."""
    var, _, fix = KINDS[kind]
    raw = _env(kind)
    if raw:
        src = f"${var}"
        # The cloud environment's variable field is .env format, which mangles the
        # double quotes inside JSON — so `--export` emits base64 (2026-09-14). Accept
        # both: a value starting with "{" is JSON, anything else must decode to it.
        if not raw.startswith("{"):
            import base64
            try:
                raw = base64.b64decode(raw, validate=True).decode()
            except Exception:
                raise NoCredential(f"{src} is neither JSON nor base64-encoded JSON "
                                   "— re-export it") from None
    else:
        p = token_path(kind)
        if not p.exists():
            raise NoCredential(f"no {kind} credential: neither ${var} nor {p} is set "
                               f"— run {fix}")
        raw, src = p.read_text(), str(p)
    try:
        d = json.loads(raw)
    except ValueError as e:
        raise NoCredential(f"{src} is not valid JSON ({e}) — re-export it") from None
    if not isinstance(d, dict):
        raise NoCredential(f"{src} is not a JSON object — re-export it")
    missing = [k for k in REQUIRED if not d.get(k)]
    if missing:
        raise NoCredential(f"{src} is missing {', '.join(missing)} — re-export it")
    return d, src


def credentials(kind, default_scopes=None, quiet=False):
    """google.oauth2 Credentials for `kind`. Raises NoCredential."""
    from google.oauth2.credentials import Credentials
    d, src = load(kind)
    if not quiet:
        print(f"# credential: {src}", file=sys.stderr)
    return Credentials(
        None,
        refresh_token=d["refresh_token"],
        client_id=d["client_id"],
        client_secret=d["client_secret"],
        token_uri=d.get("token_uri") or DEFAULT_TOKEN_URI,
        scopes=d.get("scopes") or default_scopes,
    )


def export(kind):
    """One base64 line, for pasting into a cloud environment variable.

    Base64, not raw JSON: the environment dialog's .env format mangles JSON's
    double quotes. `load()` decodes it back."""
    import base64
    d, _ = load(kind)
    return base64.b64encode(json.dumps(d, separators=(",", ":")).encode()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--export", choices=sorted(KINDS))
    a = ap.parse_args()
    if a.export:
        try:
            print(export(a.export))
        except NoCredential as e:
            sys.exit(f"🔴 {e}")
        print(f"\n☝ Paste that single line as {KINDS[a.export][0]} in the cloud "
              "environment settings. It is a live credential: never commit it.",
              file=sys.stderr)
        return 0
    rc = 0
    for kind in sorted(KINDS):
        try:
            _, src = load(kind)
            print(f"  {kind:9s} : ✅ {src}")
        except NoCredential as e:
            print(f"  {kind:9s} : 🔴 {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
