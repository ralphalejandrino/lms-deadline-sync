#!/usr/bin/env python3
"""Tests for bb_cookies — the Blackboard session-persistence layer.

Run: ./test_bb_cookies.py

The properties that actually matter here are the NEGATIVE ones. This module sits
directly upstream of "is anything due?", and the failure mode that costs a
deadline is a soft green, not a crash. So the controls below assert that a
broken state file degrades to "nothing injected" and never to "looks fine".

Every test redirects STATE_FILE into a temp dir — nothing touches the real
profile.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bb_cookies

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '🔴'} {name}" + (f"  — {detail}" if detail and not cond else ""))


class FakeCtx:
    """Minimal stand-in for a Playwright BrowserContext."""

    def __init__(self, cookies=None, add_raises=False, read_raises=False):
        self._cookies = cookies or []
        self.added = []
        self._add_raises = add_raises
        self._read_raises = read_raises

    def cookies(self):
        if self._read_raises:
            raise RuntimeError("context is closed")
        return list(self._cookies)

    def add_cookies(self, cookies):
        if self._add_raises:
            raise ValueError("bad cookie")
        self.added.extend(cookies)


def bb(name, value="v", expires=-1, domain="blackboard.ubaguio.edu"):
    return {"name": name, "value": value, "domain": domain, "path": "/",
            "expires": expires, "httpOnly": True, "secure": True, "sameSite": "None"}


def google(name="SID", expires=None):
    return {"name": name, "value": "g", "domain": ".google.com", "path": "/",
            "expires": expires if expires is not None else time.time() + 9e6,
            "httpOnly": True, "secure": True, "sameSite": "Lax"}


def write_state(obj):
    bb_cookies.STATE_FILE.write_text(obj if isinstance(obj, str) else json.dumps(obj))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="bbcookie-test-"))
    bb_cookies.STATE_FILE = tmp / "storage_state.json"
    future = time.time() + 86400
    past = time.time() - 86400

    print("\n— persist —")
    ctx = FakeCtx([bb("BbRouter"), bb("samlCookie"), google()])
    msg = bb_cookies.persist(ctx, "cdp")
    saved = json.loads(bb_cookies.STATE_FILE.read_text())
    sess_only = [c for c in saved["cookies"] if "ubaguio" in c["domain"]]
    check("session-only cookies get a real future expiry",
          all(c["expires"] > time.time() + 6 * 86400 for c in sess_only), msg)
    check("both Blackboard cookies saved", len(sess_only) == 2, str(len(sess_only)))
    check("non-Blackboard cookies are NOT harvested",
          not any("google" in c["domain"] for c in saved["cookies"]))
    check("message reports the revival count", "2 session-only" in msg, msg)

    print("\n— persist merges, never clobbers —")
    write_state({"cookies": [google("SID"), bb("BbRouter", expires=past)],
                 "origins": [{"origin": "https://x", "localStorage": []}]})
    bb_cookies.persist(FakeCtx([bb("BbRouter", value="new")]), "profile")
    saved = json.loads(bb_cookies.STATE_FILE.read_text())
    check("pre-existing Google cookie survives",
          any(c["domain"] == ".google.com" for c in saved["cookies"]))
    check("stale Blackboard cookie is replaced, not duplicated",
          len([c for c in saved["cookies"] if c["name"] == "BbRouter"]) == 1)
    check("replacement carries the new value",
          [c for c in saved["cookies"] if c["name"] == "BbRouter"][0]["value"] == "new")
    check("origins block preserved", len(saved.get("origins", [])) == 1)

    print("\n— inject —")
    write_state({"cookies": [bb("BbRouter", expires=future),
                             bb("samlCookie", expires=future), google()],
                 "origins": []})
    ctx = FakeCtx()
    msg = bb_cookies.inject(ctx)
    check("injects both saved Blackboard cookies", len(ctx.added) == 2, msg)
    check("does NOT inject Google cookies",
          not any("google" in c["domain"] for c in ctx.added))
    check("only Playwright-accepted fields are passed",
          all(set(c) <= set(bb_cookies.FIELDS) for c in ctx.added))

    print("\n— 🔴 NEGATIVE CONTROLS —")
    write_state("{ this is not json")
    ctx = FakeCtx()
    msg = bb_cookies.inject(ctx)
    check("corrupt state file → nothing injected, no raise", ctx.added == [], msg)
    check("corrupt state file is reported as unreadable", "unreadable" in msg, msg)

    bb_cookies.STATE_FILE.unlink()
    ctx = FakeCtx()
    msg = bb_cookies.inject(ctx)
    check("missing state file → nothing injected, no raise", ctx.added == [], msg)

    write_state({"cookies": [bb("BbRouter", expires=past)], "origins": []})
    ctx = FakeCtx()
    msg = bb_cookies.inject(ctx)
    check("EXPIRED saved cookie is NOT injected", ctx.added == [], msg)
    check("expiry refusal says so out loud", "expired" in msg.lower(), msg)

    write_state({"cookies": [bb("BbRouter", expires=-1)], "origins": []})
    check("session-only (-1) saved cookie is NOT injected",
          bb_cookies.inject(FakeCtx()).startswith("no cookies injected"))

    write_state({"cookies": [bb("BbRouter", expires=future)], "origins": []})
    ctx = FakeCtx(add_raises=True)
    msg = bb_cookies.inject(ctx)
    check("add_cookies raising → reported as 0 injected, no crash", "0/1" in msg, msg)

    msg = bb_cookies.persist(FakeCtx(read_raises=True), "cdp")
    check("unreadable context → not persisted, no crash",
          msg.startswith("not persisted"), msg)

    msg = bb_cookies.persist(FakeCtx([google()]), "cdp")
    check("context with no Blackboard cookies → refuses to write",
          msg == "not persisted — no Blackboard cookies in this context", msg)

    bb_cookies.STATE_FILE = tmp / "nonexistent-dir-check" / "s.json"
    msg = bb_cookies.persist(FakeCtx([bb("BbRouter")]), "cdp")
    check("missing parent directory is created, not fatal",
          msg.startswith("persisted"), msg)

    print("\n— status —")
    bb_cookies.STATE_FILE = tmp / "status.json"
    write_state({"cookies": [bb("BbRouter", expires=past)], "origins": []})
    check("all-expired is reported as EXPIRED, never as usable",
          "ALL EXPIRED" in bb_cookies.status(), bb_cookies.status())
    write_state({"cookies": [google()], "origins": []})
    check("no Blackboard cookies → 'none', not a false positive",
          bb_cookies.status().startswith("saved session: none"), bb_cookies.status())
    write_state({"cookies": [bb("BbRouter", expires=future)], "origins": []})
    check("usable cookie reports hours remaining", "usable for" in bb_cookies.status())

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  🔴 {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
