#!/usr/bin/env python3
"""Carry a verified Blackboard session from RE to a cloud session — encrypted, via the vault.

    ./lms.sh relay --status       what the relay holds, how old, can THIS machine read it
    ./lms.sh relay --export       (RE)    encrypt the verified cookies; commit + push if changed
    ./lms.sh relay --import       (cloud) decrypt into the scraper's cookie state file
    ./lms.sh relay --export-key   (RE)    print the key to paste as UB_RELAY_KEY
    ./lms.sh relay --rotate       (RE)    new key + re-export; the old key stops working

## Why this exists (2026-09-14)

A cloud session cannot keep Blackboard alive. Auth is Google SAML with session-only
cookies; RE keeps it warm with `ub-warm.timer` (hourly by day since 2026-09-15), and when RE was off
from 09-11 12:06 PM the Google session was dead by 09-14 12:47 AM. A cloud VM has
no process that survives between sessions, so it can never do that warming itself.

What it CAN do is use a session RE has already proven. `bb_cookies.persist()` saves
the Blackboard cookies after every verified `/users/me` 200; this module encrypts
that set and commits the ciphertext to the vault, which every cloud session clones.
`ub-warm.timer` runs the export, so the relay is at most ~4 h behind while RE is on.

## 🔴 Why this cannot create a false green

Importing cookies is not authentication. `bb_session._probe()` still gates the
source on a real `GET /users/me` returning 200 with a user id. A stale, revoked or
IP-bound cookie produces a 401 and the source is rejected exactly as before — so
the worst this module can do is fail to help, never report a session that is not
there. Every entry point returns a detail string instead of raising, for the same
reason `bb_cookies` does: a relay failure must never sink a scan.

## What leaves RE, and what does not

- **Ciphertext only** (`relay/bbstate.enc`, Fernet: AES-128-CBC + HMAC-SHA256).
- `relay/bbstate.meta.json` is plaintext and holds **no cookie values** — the save
  time, the count, the cookie NAMES, and a keyed fingerprint (HMAC with the relay
  key) so an unchanged session is not re-committed every 4 hours.
- Google cookies are never relayed — only `ubaguio.edu` records, via `bb_cookies._is_bb`.
- The key lives in `~/.config/ub-school/relay.key` (mode 600, outside the vault) on
  RE and in the cloud environment's `UB_RELAY_KEY`. `--rotate` invalidates both old
  copies at once.

## ⚠ What it does NOT solve

- RE off, asleep or without power for more than MAX_AGE (4 h) → the relay is refused
  with an explicit `🔴 RELAY STALE` line and the cloud reports Blackboard UNKNOWN.
  That is the correct outcome. Blackboard itself kills the session at ~5 h, so an
  older relay would only 401 — and a bare 401 does not say WHY.
- If Blackboard binds its session to the client IP or user agent, the cloud probe
  will 401 even on a fresh relay. Unknown until tested from a real cloud session;
  the probe will say so, and `./lms.sh bb-token --ask` is then the long-term path.
"""
import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bb_cookies

VAULT = HERE.parents[2]                      # vault/school/assets/lms → vault
RELAY_DIR = HERE / "relay"
ENC = RELAY_DIR / "bbstate.enc"
META = RELAY_DIR / "bbstate.meta.json"
KEY_FILE = Path.home() / ".config/ub-school/relay.key"
HOOK_LOCK = Path.home() / ".claude/vault-hook.lock"    # the Stop hook's lock
ENV_KEY = "UB_RELAY_KEY"
# 🔴 2026-09-15: was 72 h, and that number was about the GOOGLE session, not
# Blackboard's. Blackboard drops a relayed session ~5 h after RE's last warm —
# measured: cookies exported 8:06 AM answered /users/me at 1:11 PM and 401'd by
# 1:30 PM, after RE lost power at 10:26 AM (scheduled outage). A 72 h limit let a
# 9 h-old relay through, and the load reported a bare SESSION DEAD. Past 4 h the
# relay is now refused and called STALE, with the reason, before anything is tried.
MAX_AGE_S = 4 * 3600
AGEING_S = 2 * 3600          # RE warms hourly by day; older than this, RE has missed runs
MNL = timezone(timedelta(hours=8))


# ----------------------------------------------------------------- keys
def _fernet(key):
    from cryptography.fernet import Fernet
    return Fernet(key)


def env_key():
    v = os.environ.get(ENV_KEY, "").strip()
    return v.encode() if v else None


def local_key():
    try:
        return KEY_FILE.read_bytes().strip() or None
    except FileNotFoundError:
        return None


def _write_atomic(path, data: bytes, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


def new_key():
    from cryptography.fernet import Fernet
    k = Fernet.generate_key()
    _write_atomic(KEY_FILE, k, mode=0o600)
    return k


# -------------------------------------------------------------- payload
def _bb_cookies():
    """Blackboard cookies from the verified state file → (list, None) | (None, why)."""
    d, err = bb_cookies._read()
    if err:
        return None, err
    cs = [bb_cookies._trim(c) for c in d["cookies"] if bb_cookies._is_bb(c)]
    if not cs:
        return None, "cookie state holds no Blackboard cookies"
    return cs, None


def _fingerprint(key, cookies):
    """Keyed hash of the session's identity (name/value/domain/path, NOT expiry).

    Expiry is excluded on purpose: `persist()` rewrites session-only expiries to
    now + 7 days on every save, so hashing it would re-commit every 4 hours for a
    session that never changed. Keyed so the meta file reveals nothing offline."""
    canon = sorted((c.get("domain", ""), c.get("path", ""), c.get("name", ""),
                    c.get("value", "")) for c in cookies)
    return hmac.new(key, json.dumps(canon).encode(), hashlib.sha256).hexdigest()[:32]


def meta():
    try:
        return json.loads(META.read_text())
    except Exception:
        return {}


# --------------------------------------------------------------- export
def _git(*args, timeout=60):
    r = subprocess.run(["git", *args], cwd=VAULT, capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def _commit_and_push(stamp):
    """Commit ONLY the two relay files, then push. Detail string; never raises.

    Runs from a systemd timer, outside any Claude session, so no Stop hook will
    commit for it. Takes the Stop hook's own lock so the two never interleave."""
    import fcntl
    rel = [str(p.relative_to(VAULT)) for p in (ENC, META)]
    try:
        HOOK_LOCK.parent.mkdir(parents=True, exist_ok=True)
        with open(HOOK_LOCK, "w") as lk:
            deadline = time.time() + 25
            while True:
                try:
                    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() > deadline:
                        return "not pushed — vault lock busy (the Stop hook will pick it up)"
                    time.sleep(1)
            _git("add", "--", *rel)
            rc, out = _git("commit", "-q", "-m", f"relay: blackboard session {stamp}",
                           "--", *rel)
            if rc:
                return f"not committed — {out[:120]}"
            rc, out = _git("pull", "-q", "--rebase", "--autostash")
            if rc:
                _git("rebase", "--abort")
                return f"committed locally, NOT pushed — pull failed: {out[:100]}"
            rc, out = _git("push", "-q")
            return "committed and pushed" if rc == 0 else f"committed, push FAILED: {out[:100]}"
    except Exception as e:
        return f"not pushed — {type(e).__name__}: {e}"[:140]


def export(commit=True, now=None):
    """Encrypt the verified Blackboard cookies into the relay. Detail; never raises."""
    try:
        key = local_key()
        if not key:
            return "not exported — no relay key on this machine (./lms.sh relay --rotate)"
        cookies, err = _bb_cookies()
        if err:
            return f"not exported — {err}"
        now = int(now if now is not None else time.time())
        fp = _fingerprint(key, cookies)
        if ENC.exists() and meta().get("fingerprint") == fp:
            return "unchanged — the relay already holds this session"
        payload = json.dumps({"saved_at": now, "cookies": cookies}).encode()
        _write_atomic(ENC, _fernet(key).encrypt_at_time(payload, now))
        stamp = datetime.fromtimestamp(now, MNL).strftime("%Y-%m-%d %I:%M %p")
        _write_atomic(META, json.dumps({
            "saved_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "saved_at_manila": stamp,
            "count": len(cookies),
            "names": sorted({c.get("name", "") for c in cookies}),
            "fingerprint": fp,
            "note": "ciphertext of Blackboard cookies; no values in this file. See bb_relay.py.",
        }, indent=1).encode())
        detail = f"exported {len(cookies)} Blackboard cookies"
        if commit:
            detail += " · " + _commit_and_push(stamp)
        return detail
    except Exception as e:
        return f"not exported — {type(e).__name__}: {e}"[:160]


# --------------------------------------------------------------- import
def stale_detail(saved, age, max_age=MAX_AGE_S):
    """The one wording for a stale relay — import, status and the cloud hook all say it.

    It has to be unmissable in a cloud brief: a stale relay means Blackboard was
    NOT read, and a quiet line reads as 'nothing new' (declared-but-never-read)."""
    when = datetime.fromtimestamp(saved, MNL).strftime("%a %m-%d %I:%M %p")
    return (f"🔴 RELAY STALE — saved on RE {when}, {age / 3600:.1f}h ago "
            f"(limit {max_age / 3600:.0f}h). Blackboard drops a relayed session ~5h after "
            "RE's last warm, and RE has not warmed since — it is off, asleep or without "
            "power. Blackboard is UNKNOWN for every course, NOT clear. "
            "Fix: bring RE up (ub-warm runs hourly), then re-run.")


def import_state(key=None, now=None, max_age=MAX_AGE_S):
    """Decrypt the relay into bb_cookies.STATE_FILE. Returns (ok, detail); never raises.

    On ANY failure the state file is left exactly as it was."""
    try:
        from cryptography.fernet import InvalidToken
        key = key or env_key()
        if not key:
            return False, f"no ${ENV_KEY} in this environment"
        if not ENC.exists():
            return False, f"no relay at {ENC.name} — RE has never exported one"
        try:
            f = _fernet(key)
        except Exception:
            return False, f"${ENV_KEY} is not a valid relay key — re-copy it from RE"
        tok = ENC.read_bytes()
        try:
            saved = f.extract_timestamp(tok)      # verifies the HMAC before anything else
            data = f.decrypt(tok)
        except InvalidToken:
            return False, "relay could not be decrypted — wrong key, or the file was tampered with"
        now = int(now if now is not None else time.time())
        age = now - saved
        if age > max_age:
            return False, stale_detail(saved, age, max_age)
        d = json.loads(data)
        cookies = [bb_cookies._trim(c) for c in d.get("cookies", []) if bb_cookies._is_bb(c)]
        if not cookies:
            return False, "relay decrypted but holds no Blackboard cookies"
        st, _ = bb_cookies._read()
        st["cookies"] = [c for c in st.get("cookies", []) if not bb_cookies._is_bb(c)] + cookies
        _write_atomic(bb_cookies.STATE_FILE, json.dumps(st, indent=1).encode())
        detail = f"imported {len(cookies)} Blackboard cookies, saved {age / 3600:.1f}h ago on RE"
        if age > AGEING_S:
            detail += (" · ⚠ AGEING — RE has missed hourly warms; Blackboard drops the "
                       "session ~5h after the last one")
        return True, detail
    except Exception as e:
        return False, f"relay import raised {type(e).__name__}: {e}"[:160]


# --------------------------------------------------------------- status
def status():
    m = meta()
    print(f"# Blackboard relay — {ENC}")
    if not ENC.exists():
        print("  relay file : 🔴 none — RE has never exported (./lms.sh relay --export on RE)")
    else:
        try:
            saved = datetime.fromisoformat(m["saved_at"])
            hrs = (datetime.now(timezone.utc) - saved).total_seconds() / 3600
            secs = hrs * 3600
            mark = ("✅" if secs < AGEING_S else
                    "⚠ AGEING —" if secs < MAX_AGE_S else "🔴 STALE —")
            print(f"  relay file : {mark} {m.get('count')} cookies, saved "
                  f"{m.get('saved_at_manila')} ({hrs:.1f}h ago)")
            if secs >= MAX_AGE_S:
                print("  " + stale_detail(saved.timestamp(), secs))
        except Exception:
            print("  relay file : ⚠ present, meta unreadable")
    print(f"  local key  : {'present' if local_key() else 'absent'} ({KEY_FILE})")
    print(f"  ${ENV_KEY:12s}: {'set' if env_key() else 'not set'}")
    key = env_key() or local_key()
    if key and ENC.exists():
        try:
            from cryptography.fernet import InvalidToken
            try:
                _fernet(key).extract_timestamp(ENC.read_bytes())
                print("  decryptable: ✅ with the key on this machine")
            except InvalidToken:
                print("  decryptable: 🔴 NO — this machine's key does not match the relay")
        except Exception as e:
            print(f"  decryptable: 🔴 {type(e).__name__}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true")
    g.add_argument("--export", action="store_true")
    g.add_argument("--import", dest="do_import", action="store_true")
    g.add_argument("--export-key", action="store_true")
    g.add_argument("--rotate", action="store_true")
    ap.add_argument("--no-commit", action="store_true", help="export without git")
    a = ap.parse_args()

    if a.export:
        d = export(commit=not a.no_commit)
        print(d)
        return 0 if d.startswith(("exported", "unchanged")) else 1
    if a.do_import:
        ok, d = import_state()
        print(("✅ " if ok else "🔴 ") + d)
        return 0 if ok else 1
    if a.export_key:
        k = local_key()
        if not k:
            sys.exit("🔴 no relay key on this machine — ./lms.sh relay --rotate creates one")
        print(k.decode())
        print(f"\n☝ Paste that as {ENV_KEY} in the cloud environment settings. "
              "It decrypts the Blackboard session: never commit it.", file=sys.stderr)
        return 0
    if a.rotate:
        new_key()
        print(f"✅ new relay key written to {KEY_FILE} (mode 600)")
        print("   " + export(commit=not a.no_commit))
        print(f"   🔴 Update {ENV_KEY} in the cloud environment: ./lms.sh relay --export-key")
        return 0
    return status()


if __name__ == "__main__":
    sys.exit(main())
