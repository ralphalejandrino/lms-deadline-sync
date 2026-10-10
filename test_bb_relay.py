#!/usr/bin/env python3
"""Tests for bb_relay — the encrypted Blackboard session relay (RE → cloud).

Run: ./test_bb_relay.py   (or ./lms.sh test)

What matters here is what must NEVER happen: a cookie value leaking into the
plaintext meta file, a Google cookie riding along, a wrong key or tampered file
being accepted, a stale relay being used, or a failed import damaging the state
file. Every path is redirected into a temp dir, and export runs with
commit=False — nothing touches the real profile, key, or git.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import bb_cookies
import bb_relay

PASS, FAIL = [], []
NOW = 1_789_300_000
SECRET = "BBROUTER-SECRET-VALUE-9f8e7d"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '🔴'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def cookie(name, value, domain="blackboard.ubaguio.edu"):
    return {"name": name, "value": value, "domain": domain, "path": "/",
            "expires": NOW + 7 * 86400, "httpOnly": True, "secure": True, "sameSite": "Lax"}


def sandbox():
    tmp = Path(tempfile.mkdtemp(prefix="relay-test-"))
    bb_relay.RELAY_DIR = tmp / "relay"
    bb_relay.ENC = bb_relay.RELAY_DIR / "bbstate.enc"
    bb_relay.META = bb_relay.RELAY_DIR / "bbstate.meta.json"
    bb_relay.KEY_FILE = tmp / "cfg" / "relay.key"
    bb_cookies.STATE_FILE = tmp / "profile" / "storage_state.json"
    os.environ.pop(bb_relay.ENV_KEY, None)
    return tmp


def write_state(cookies):
    bb_cookies.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    bb_cookies.STATE_FILE.write_text(json.dumps({"cookies": cookies, "origins": []}))


def state_cookies():
    return json.loads(bb_cookies.STATE_FILE.read_text())["cookies"]


print("# bb_relay — export")
sandbox()
write_state([cookie("BbRouter", SECRET)])
d = bb_relay.export(commit=False, now=NOW)
check("no key on the machine → not exported", d.startswith("not exported") and "key" in d, d)
check("…and no relay file was written", not bb_relay.ENC.exists())

sandbox()
key = bb_relay.new_key()
check("new_key() writes the key file mode 600",
      (bb_relay.KEY_FILE.stat().st_mode & 0o777) == 0o600)
write_state([cookie("BbRouter", SECRET), cookie("JSESSIONID", "jsess-1"),
             cookie("SID", "GOOGLE-SECRET", domain=".google.com")])
d = bb_relay.export(commit=False, now=NOW)
check("export with key + cookies → exported", d.startswith("exported 2"), d)
m = json.loads(bb_relay.META.read_text())
check("meta records count and names", m["count"] == 2 and m["names"] == ["BbRouter", "JSESSIONID"], m)
meta_txt = bb_relay.META.read_text()
check("🔴 meta holds NO cookie values", SECRET not in meta_txt and "jsess-1" not in meta_txt)
check("🔴 ciphertext holds no plaintext value", SECRET.encode() not in bb_relay.ENC.read_bytes())
payload = json.loads(bb_relay._fernet(key).decrypt(bb_relay.ENC.read_bytes()))
names = {c["name"] for c in payload["cookies"]}
check("🔴 the Google cookie was NOT relayed", "SID" not in names and names == {"BbRouter", "JSESSIONID"}, names)

d = bb_relay.export(commit=False, now=NOW + 3600)
check("same session again → unchanged (no re-commit every 4 h)", d.startswith("unchanged"), d)

write_state([cookie("BbRouter", SECRET, ), cookie("JSESSIONID", "jsess-1")])
st = state_cookies()
st[0]["expires"] = NOW + 9 * 86400                       # only the expiry moved
write_state(st)
d = bb_relay.export(commit=False, now=NOW + 7200)
check("expiry-only change → still unchanged", d.startswith("unchanged"), d)

write_state([cookie("BbRouter", "ROTATED-VALUE"), cookie("JSESSIONID", "jsess-1")])
d = bb_relay.export(commit=False, now=NOW + 10800)
check("cookie value changed → exported again", d.startswith("exported"), d)

sandbox()
bb_relay.new_key()
write_state([cookie("SID", "g", domain=".google.com")])
d = bb_relay.export(commit=False, now=NOW)
check("state with no Blackboard cookies → not exported", d.startswith("not exported"), d)

print("# bb_relay — import")
sandbox()
key = bb_relay.new_key()
write_state([cookie("BbRouter", SECRET), cookie("JSESSIONID", "jsess-1")])
bb_relay.export(commit=False, now=NOW)
# the cloud side: no state file yet, a Google cookie already present from elsewhere
write_state([cookie("SID", "keep-me", domain=".google.com")])
ok, d = bb_relay.import_state(key=key, now=NOW + 3600)
got = {c["name"]: c["value"] for c in state_cookies()}
check("right key, fresh relay → imported", ok and d.startswith("imported 2"), d)
check("imported cookies land in the state file", got.get("BbRouter") == SECRET)
check("existing non-Blackboard cookies are preserved (merge)", got.get("SID") == "keep-me")

os.environ[bb_relay.ENV_KEY] = key.decode()
bb_cookies.STATE_FILE.unlink()
ok, d = bb_relay.import_state(now=NOW + 60)
check("key taken from $UB_RELAY_KEY when not passed", ok, d)
os.environ.pop(bb_relay.ENV_KEY)

ok, d = bb_relay.import_state(key=key, now=NOW + 3 * 3600)
check("3 h old (inside Blackboard's ~5 h life) → still imported", ok, d)

print("# bb_relay — the negative controls")
before = bb_cookies.STATE_FILE.read_text()

ok, d = bb_relay.import_state(now=NOW)
check("no key anywhere → refused", not ok and bb_relay.ENV_KEY in d, d)

from cryptography.fernet import Fernet
ok, d = bb_relay.import_state(key=Fernet.generate_key(), now=NOW + 60)
check("🔴 wrong key → refused", not ok and "wrong key" in d, d)

ok, d = bb_relay.import_state(key=b"not-a-fernet-key", now=NOW + 60)
check("malformed key → refused", not ok and "not a valid" in d, d)

ok, d = bb_relay.import_state(key=key, now=NOW + bb_relay.MAX_AGE_S + 3600)
check("🔴 relay older than MAX_AGE → refused, says UNKNOWN", not ok and "UNKNOWN" in d, d)

# 2026-09-15: a 8.9 h relay was ACCEPTED under the old 72 h limit, then 401'd,
# and the load said only "SESSION DEAD". It must now be refused, by name.
ok, d = bb_relay.import_state(key=key, now=NOW + int(8.9 * 3600))
check("🔴 the real 09-15 case, 8.9 h old → refused as STALE", not ok and "RELAY STALE" in d, d)
check("🔴 …and the refusal says why and what to do",
      "RE" in d and "NOT clear" in d and "bring RE up" in d, d)
ok, d = bb_relay.import_state(key=key, now=NOW + int(4.5 * 3600))
check("🔴 4.5 h old (past the 4 h limit) → refused as STALE", not ok and "RELAY STALE" in d, d)
check("MAX_AGE_S sits below Blackboard's measured ~5 h session life",
      bb_relay.MAX_AGE_S < 5 * 3600, bb_relay.MAX_AGE_S)

good = bb_relay.ENC.read_bytes()
tampered = bytearray(good)
tampered[40] = ord("A") if tampered[40] != ord("A") else ord("B")
bb_relay.ENC.write_bytes(bytes(tampered))
ok, d = bb_relay.import_state(key=key, now=NOW + 60)
check("🔴 tampered ciphertext → refused", not ok and "tampered" in d, d)
bb_relay.ENC.write_bytes(good)

check("🔴 every refused import left the state file untouched",
      bb_cookies.STATE_FILE.read_text() == before)

bb_relay.ENC.unlink()
ok, d = bb_relay.import_state(key=key, now=NOW)
check("no relay file → refused", not ok and "never exported" in d, d)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
