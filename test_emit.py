#!/usr/bin/env python3
"""Tests for the Nebula Phase 0 full-body emit (scan.EMIT, chat DMs, mail bodies).

Run: ./test_emit.py

The properties that matter:
  * OFF by default — scan with EMIT=None records nothing (no behaviour change);
  * a DM is flagged `instructor_thread` ONLY when a staff (@e.ubaguio.edu) account
    posted in it — that flag is what keeps classmate DMs away from the free API;
  * a failed DM read is RECORDED as an error row, never silently dropped;
  * mail bodies decode (plain first, HTML de-tagged as fallback);
  * the JSONL file is written atomically with a run header.
"""
import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import chat_api
import mail_fetch
import scan
import study_load

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '🔴'} {name}" + (f"  — {detail}" if detail and not cond else ""))


# ---- fakes for the Chat API -------------------------------------------------
class _Exec:
    def __init__(self, v): self.v = v
    def execute(self):
        if isinstance(self.v, Exception):
            raise self.v
        return self.v


class _Msgs:
    def __init__(self, by_space): self.by_space = by_space
    def list(self, parent, **kw): return _Exec(self.by_space[parent])


class _Spaces:
    def __init__(self, by_space): self.m = _Msgs(by_space)
    def messages(self): return self.m


class FakeSvc:
    def __init__(self, by_space): self.s = _Spaces(by_space)
    def spaces(self): return self.s


def msg(email, text, name="spaces/x/messages/1"):
    return {"name": name, "text": text, "createTime": "2026-10-10T03:12:00Z",
            "sender": {"displayName": "Someone", "email": email}}


print("# EMIT is OFF by default")
scan.EMIT = None
scan._emit(source="bb_announce", id="x", body="quiz tomorrow")
check("EMIT=None records nothing and does not raise", scan.EMIT is None)

print("\n# keyword floor flag")
scan.EMIT = []
scan._emit(source="bb_announce", id="a", body="Quiz on Monday, bring pens")
scan._emit(source="bb_announce", id="b", body="Good morning class, have a nice day")
scan._emit(source="classroom", id="c", body="fill this https://forms.gle/abc")
check("exam word → keyword_hit True", scan.EMIT[0]["keyword_hit"] is True)
check("pleasantry → keyword_hit False", scan.EMIT[1]["keyword_hit"] is False)
check("Google Form link → keyword_hit True", scan.EMIT[2]["keyword_hit"] is True)
check("full body kept, not truncated", scan.EMIT[0]["body"] == "Quiz on Monday, bring pens")
scan.EMIT = None

print("\n# DM privacy flag")
sp_staff = {"name": "spaces/dm1", "spaceType": "DIRECT_MESSAGE", "spaceUri": "u1"}
sp_peer = {"name": "spaces/dm2", "spaceType": "DIRECT_MESSAGE", "spaceUri": "u2"}
sp_bad = {"name": "spaces/dm3", "spaceType": "DIRECT_MESSAGE"}
svc = FakeSvc({
    "spaces/dm1": {"messages": [msg("student@s.ubaguio.edu", "ok po"),
                                msg("instructor.g@e.ubaguio.edu", "exam stays 5-6pm")]},
    "spaces/dm2": {"messages": [msg("20236495@s.ubaguio.edu", "group no. is 4")]},
    "spaces/dm3": RuntimeError("403"),
})
rec = []
chat_api._dm_records(svc, sp_staff, 25, rec)
chat_api._dm_records(svc, sp_peer, 25, rec)
chat_api._dm_records(svc, sp_bad, 25, rec)
staff = [r for r in rec if r.get("url") == "u1"]
peer = [r for r in rec if r.get("url") == "u2"]
err = [r for r in rec if r.get("error")]
check("instructor DM → every message instructor_thread=True",
      len(staff) == 2 and all(r["instructor_thread"] is True for r in staff))
check("🔴 classmate DM → instructor_thread=False",
      len(peer) == 1 and peer[0]["instructor_thread"] is False)
check("🔴 a failed DM read is recorded as an error row", len(err) == 1 and err[0]["source"] == "chat_dm")
check("DM rows carry no course", all(r.get("course") is None for r in staff + peer))
check("DM rows are source chat_dm", all(r["source"] == "chat_dm" for r in rec))

print("\n# mail body decoding")
b64 = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
plain = {"mimeType": "multipart/alternative", "parts": [
    {"mimeType": "text/plain", "body": {"data": b64("Quiz moved to Wed 10 AM")}},
    {"mimeType": "text/html", "body": {"data": b64("<p>Quiz moved</p>")}}]}
html_only = {"mimeType": "text/html", "body": {"data": b64("<p>Bring <b>pens</b></p>")}}
check("text/plain preferred", mail_fetch._plain_body(plain) == "Quiz moved to Wed 10 AM")
check("HTML de-tagged when no plain part", "Bring" in mail_fetch._plain_body(html_only)
      and "<b>" not in mail_fetch._plain_body(html_only))
check("empty payload → empty string", mail_fetch._plain_body({}) == "")
css_plain = {"mimeType": "multipart/alternative", "parts": [
    {"mimeType": "text/plain", "body": {"data": b64("@import url(http://f.css); img { max-width: 600px; } "
                                                     + "a { x: y; } " * 20 + " Week 9 reporting")}},
    {"mimeType": "text/html", "body": {"data": b64("<style>a{x:y}</style><p>Week 9 Group Reporting due</p>")}}]}
got = mail_fetch._plain_body(css_plain)
check("🔴 CSS-shaped plain part → the HTML text is used instead",
      got.startswith("Week 9 Group Reporting") and "@import" not in got, got[:60])

print("\n# atomic JSONL")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "sub" / "items.jsonl"
    study_load._write_emit(p, [{"source": "fb", "id": "1", "body": "ñ é"}],
                           {"when": "2026-10-11T14:00", "complete": True})
    lines = p.read_text().splitlines()
    head = json.loads(lines[0])
    check("header line first", head["_run"]["rows"] == 1 and head["_run"]["complete"] is True)
    check("unicode kept", json.loads(lines[1])["body"] == "ñ é")
    check("no .tmp left behind", not any(x.suffix == ".tmp" for x in p.parent.iterdir()))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
