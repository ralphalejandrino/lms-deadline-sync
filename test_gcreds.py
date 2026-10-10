#!/usr/bin/env python3
"""Tests for gcreds — the single Google-token loader.

Run: ./test_gcreds.py   (or ./lms.sh test)

The property that matters most is a NEGATIVE one: a cloud variable that is set
but broken must fail loud, and must never be papered over by a good local file.
Every test runs with HOME redirected into a temp dir and both env vars cleared,
so nothing reads or touches the real tokens.
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import gcreds

PASS, FAIL = [], []
GOOD = {"refresh_token": "r-tok", "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "sec", "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"]}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '🔴'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def fresh_home():
    tmp = tempfile.mkdtemp(prefix="gcreds-test-")
    os.environ["HOME"] = tmp
    for var, _, _ in gcreds.KINDS.values():
        os.environ.pop(var, None)
    return Path(tmp)


def write_file(kind, data):
    p = gcreds.token_path(kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return p


def raises_nocred(fn):
    try:
        fn()
    except gcreds.NoCredential as e:
        return str(e)
    return None


real_home = os.environ.get("HOME")
try:
    print("# gcreds — source resolution")
    fresh_home()
    msg = raises_nocred(lambda: gcreds.load("classroom"))
    check("neither env nor file → NoCredential", msg is not None)
    check("…and the message names BOTH places",
          bool(msg) and "$UB_CLASSROOM_TOKEN" in msg and "token.json" in msg, msg or "")
    check("available() is False with nothing set", not gcreds.available("classroom"))

    fresh_home()
    p = write_file("classroom", GOOD)
    d, src = gcreds.load("classroom")
    check("file only → loads from the file", src == str(p) and d == GOOD, src)
    check("available() is True with file only", gcreds.available("classroom"))

    fresh_home()
    os.environ["UB_CLASSROOM_TOKEN"] = json.dumps({**GOOD, "refresh_token": "from-env"})
    d, src = gcreds.load("classroom")
    check("env only → loads from env", src == "$UB_CLASSROOM_TOKEN" and d["refresh_token"] == "from-env")

    fresh_home()
    write_file("classroom", GOOD)
    os.environ["UB_CLASSROOM_TOKEN"] = json.dumps({**GOOD, "refresh_token": "from-env"})
    d, src = gcreds.load("classroom")
    check("env AND file → env wins", d["refresh_token"] == "from-env", src)

    print("# gcreds — the negative controls")
    fresh_home()
    write_file("classroom", GOOD)                       # a perfectly good file…
    os.environ["UB_CLASSROOM_TOKEN"] = "{not json"      # …and a broken variable
    msg = raises_nocred(lambda: gcreds.load("classroom"))
    check("🔴 env set but INVALID → NoCredential, NOT a silent fall-back to the file",
          msg is not None and "$UB_CLASSROOM_TOKEN" in msg, msg or "fell back")

    fresh_home()
    os.environ["UB_CLASSROOM_TOKEN"] = json.dumps({k: v for k, v in GOOD.items()
                                                   if k != "refresh_token"})
    msg = raises_nocred(lambda: gcreds.load("classroom"))
    check("missing refresh_token → NoCredential naming the field",
          bool(msg) and "refresh_token" in msg, msg or "")

    fresh_home()
    os.environ["UB_CLASSROOM_TOKEN"] = "   "
    check("whitespace-only env counts as UNSET", not gcreds.available("classroom"))

    fresh_home()
    write_file("classroom", "[1, 2]")
    check("a JSON array is rejected", raises_nocred(lambda: gcreds.load("classroom")) is not None)

    fresh_home()
    os.environ["UB_CALENDAR_TOKEN"] = json.dumps(GOOD)
    check("kinds are independent: calendar set does not satisfy classroom",
          not gcreds.available("classroom") and gcreds.available("calendar"))

    print("# gcreds — credentials() and export()")
    fresh_home()
    os.environ["UB_CALENDAR_TOKEN"] = json.dumps({k: v for k, v in GOOD.items()
                                                  if k not in ("token_uri", "scopes")})
    c = gcreds.credentials("calendar", default_scopes=["x-scope"], quiet=True)
    check("credentials(): default token_uri filled in", c.token_uri == gcreds.DEFAULT_TOKEN_URI)
    check("credentials(): default scopes used when token has none", list(c.scopes or []) == ["x-scope"])
    check("credentials(): refresh token carried", c.refresh_token == "r-tok")

    fresh_home()
    write_file("classroom", GOOD)
    out = gcreds.export("classroom")
    import base64
    check("export(): single line", "\n" not in out)
    check("export(): no quotes or spaces (survives a .env field)",
          not any(ch in out for ch in "\"' "), out[:40])
    check("export(): base64 that round-trips to the same token",
          json.loads(base64.b64decode(out)) == GOOD)

    fresh_home()
    os.environ["UB_CLASSROOM_TOKEN"] = out
    d, src = gcreds.load("classroom")
    check("an exported (base64) value loads back from env", d == GOOD and src == "$UB_CLASSROOM_TOKEN")

    os.environ["UB_CLASSROOM_TOKEN"] = "definitely!!not~base64"
    check("env that is neither JSON nor base64 → NoCredential",
          raises_nocred(lambda: gcreds.load("classroom")) is not None)

    os.environ["UB_CLASSROOM_TOKEN"] = base64.b64encode(b"not json either").decode()
    check("base64 of non-JSON → NoCredential",
          raises_nocred(lambda: gcreds.load("classroom")) is not None)

    print("# gcreds — every Google-token caller routes through it")
    for name in ("classroom_api.py", "chat_api.py", "mail_fetch.py", "cal_apply.py"):
        src = (HERE / name).read_text()
        bypass = re.search(r"json\.loads\(\s*TOKEN\.read_text\(\)\s*\)", src)
        check(f"{name} does not parse the token file itself", bypass is None,
              "found json.loads(TOKEN.read_text()) — use gcreds")
        check(f"{name} imports gcreds", "import gcreds" in src)
finally:
    if real_home is not None:
        os.environ["HOME"] = real_home

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
