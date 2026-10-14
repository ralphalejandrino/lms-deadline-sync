#!/usr/bin/env python3
"""Mirror the files INSTRUCTORS upload in each course's Google Chat space (2026-10-13).

Ralph: "add classroom and chat attachments to the mirror." Blackboard is mirrored by bb_mirror.py;
this adds Chat. Files uploaded into Chat are downloadable with the token's existing
`chat.messages.readonly` scope (tested 2026-10-13: a LITERA1 screenshot, 169,218 bytes).
Files that are Google DRIVE links (4 of 137 in Chat, and every Classroom attachment) need Drive
access, which the school token deliberately does not have since 2026-09-06 — they are listed in
the manifest as `drive_only`, never silently dropped.

Only instructors' uploads (sender @e.ubaguio.edu): classmates' photos are not course material,
and 82 of the 137 attachments sat in a non-course art space. Program-wide spaces (`BSCS 2627A`)
go to `_shared`. Videos/audio are listed, not copied (Gemini can't use them from Drive).

Output: ~/.local/share/nebula/materials/<CODE>/_chat/<YYYY-MM-DD> <file>  + _chat_manifest.json
Exit 0 ok · 1 some file failed · 2 credential unavailable.
"""
import hashlib
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gcreds  # noqa: E402
from bb_mirror import DEFAULT_OUT, win_safe  # noqa: E402
from chat_api import INSTRUCTOR_DOMAIN, code_of  # noqa: E402

PAGES = 10            # 100 messages a page, newest first: the whole semester so far


def _svc():
    from googleapiclient.discovery import build
    return build("chat", "v1", credentials=gcreds.credentials("classroom", quiet=True),
                 cache_discovery=False)


def mirror(out=DEFAULT_OUT, log=print):
    from googleapiclient.http import MediaIoBaseDownload
    svc = _svc()
    per = {}
    failed = 0
    for sp in svc.spaces().list(pageSize=100).execute().get("spaces", []):
        if sp.get("spaceType") != "SPACE":
            continue                                   # DMs: never mirrored
        name = sp.get("displayName", "")
        code = code_of(name) or ("_shared" if re.search(r"BSCS|2627A$", name) else None)
        if not code:
            continue                                   # not a course space (e.g. "Intramurals Artist")
        rec = per.setdefault(code, {"files": [], "drive_only": [], "skipped": []})
        tok = None
        for _ in range(PAGES):
            r = svc.spaces().messages().list(parent=sp["name"], pageSize=100, pageToken=tok,
                                             orderBy="createTime desc").execute()
            for m in r.get("messages", []):
                atts = m.get("attachment") or []
                if not atts:
                    continue
                email = ((m.get("sender") or {}).get("email") or "").lower()
                if not email.endswith(INSTRUCTOR_DOMAIN):
                    continue
                day = (m.get("createTime") or "")[:10]
                text = " ".join((m.get("text") or "").split())[:200]
                for a in atts:
                    fn = win_safe(a.get("contentName") or "attachment", 110)
                    info = {"space": name, "posted": day, "message": text,
                            "author": (m.get("sender") or {}).get("displayName", "")}
                    if a.get("driveDataRef"):
                        rec["drive_only"].append({**info, "file": fn})
                        continue
                    ctype = (a.get("contentType") or "").lower()
                    if ctype.startswith(("video/", "audio/")):
                        rec["skipped"].append({**info, "file": fn, "mime": ctype})
                        continue
                    ref = (a.get("attachmentDataRef") or {}).get("resourceName")
                    if not ref:
                        continue
                    # A stable 6-char tag from the attachment's own id: two different files
                    # posted the same day as "image.png" lost one of them (2026-10-13).
                    tag = hashlib.sha1(ref.encode()).hexdigest()[:6]
                    stem, dot, ext = fn.rpartition(".")
                    dest = Path(out) / code / "_chat" / (f"{day} {stem} [{tag}].{ext}" if dot else f"{day} {fn} [{tag}]")
                    if not (dest.exists() and dest.stat().st_size > 0):
                        try:
                            buf = io.BytesIO()
                            dl = MediaIoBaseDownload(buf, svc.media().download_media(resourceName=ref))
                            done = False
                            while not done:
                                _, done = dl.next_chunk()
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(buf.getvalue())
                            log(f"   + {code}/_chat/{dest.name} ({dest.stat().st_size:,} B)")
                        except Exception as e:             # one file never ends the run
                            log(f"   🔴 {code} {fn}: {type(e).__name__}: {str(e)[:80]}")
                            failed += 1
                            continue
                    rec["files"].append({**info, "path": f"_chat/{dest.name}"})
            tok = r.get("nextPageToken")
            if not tok:
                break
    for code, rec in per.items():
        d = Path(out) / code
        d.mkdir(parents=True, exist_ok=True)
        (d / "_chat_manifest.json").write_text(json.dumps(rec, indent=1))
        log(f"✅ {code} chat: {len(rec['files'])} file(s)"
            + (f" · {len(rec['drive_only'])} Drive link(s) not copied" if rec["drive_only"] else ""))
    return failed


def main():
    if not gcreds.available("classroom"):
        print("🔴 chat mirror: no school Google credential")
        return 2
    f = mirror()
    print(f"CHAT MIRROR: {f} failure(s)")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
