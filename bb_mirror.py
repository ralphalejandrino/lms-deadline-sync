#!/usr/bin/env python3
"""Mirror EVERY Blackboard course's content (files + page text) into a local tree, incrementally.

WHY (Ralph, 2026-10-12): "organize the drive so gemini has proper context — I used nebula the whole
day today and there were times that it had incomplete context." The Drive held only what a session
had happened to download by hand: the MATHMW1 "Relations and Functions" PPT and the activity sheet
due 10-13 were on Blackboard and nowhere on Drive, so Gemini could not see the task it was asked
about. `bb_pull_content.py` downloads named content ids on request; this walks every course.

Output (default ~/.local/share/nebula/materials):
    <CODE>/<Blackboard folder>/<sub folder>/<file>          attachments, original names
    <CODE>/<Blackboard folder>/<item title>.txt             the text of a Blackboard page /
                                                            assignment instructions (HTML → text)
    <CODE>/_manifest.json                                   every item, link and file, for the guide

Incremental: an attachment already on disk with the same size is not fetched again; a page's text
is re-read only when Blackboard's `modified` stamp changes. Never deletes anything.

Fails loud: exit 1 if any course or file failed (a partial mirror must not read as complete),
exit 2 if the Blackboard session is dead.

    LD_LIBRARY_PATH=$HOME/pwlibs/_extracted/usr/lib/x86_64-linux-gnu \\
      ~/CRM/.venv/bin/python bb_mirror.py [--out DIR] [CODE ...]
"""
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bb_session import SessionDead, open_session  # noqa: E402
from school_config import BASE, BB_HOST, CODES  # noqa: E402

DEFAULT_OUT = Path.home() / ".local/share/nebula/materials"
MAX_BYTES = 100 * 1024 * 1024          # Gemini's per-file ceiling; bigger is listed, not copied
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def win_safe(name, cap=90):
    """A name Windows + Drive accept: no reserved characters, no trailing dot/space."""
    s = _BAD.sub("_", html.unescape(name or "")).strip().rstrip(". ")
    s = re.sub(r"\s+", " ", s)
    return (s[:cap].rstrip(". ") or "untitled")


class _Text(HTMLParser):
    """Blackboard page HTML → readable text, links kept as `text (url)`."""
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.out, self.href = [], None

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("• ")
        if tag == "a":
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.out.append(f" ({self.href})")
            self.href = None
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        self.out.append(d)


def html_to_text(body):
    p = _Text()
    p.feed(body or "")
    t = "".join(p.out)
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", t).strip()


class _Embeds(HTMLParser):
    """Files placed INSIDE a page body rather than attached to it (2026-10-13).

    Ultra's editor inserts an uploaded file as `<a data-bbfile='{json}' href=".../bbcswebdav/...">`
    (or an `<img src>`). They are not `/attachments`, so the mirror never fetched them — and
    PROGIT1's whole Learning Module is nothing but these: 9 units of page scans that reached
    Drive as a list of links that expire within a day."""

    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("a", "img"):
            return
        a = dict(attrs)
        try:
            meta = json.loads(a.get("data-bbfile") or "{}")
        except ValueError:
            meta = {}
        url = (a.get("href") if tag == "a" else a.get("src")) or meta.get("resourceUrl")
        if url and "bbcswebdav" in url:
            self.found.append({"url": url, "mime": (meta.get("mimeType") or "").lower(),
                               "name": meta.get("displayName") or meta.get("linkName") or ""})


def embedded_files(body):
    """[{url, mime, name}] in page order, each URL once."""
    p = _Embeds()
    p.feed(body or "")
    seen, out = set(), []
    for f in p.found:
        if f["url"] not in seen:
            seen.add(f["url"])
            out.append(f)
    return out


def images_to_pdf(blobs, dest):
    """Bundle page images into ONE PDF, in order — Gemini reads a 12-page PDF; it will not
    usefully open twelve loose JPGs. False when Pillow is missing (a bare cloud venv)."""
    try:
        from PIL import Image
    except ImportError:
        return False
    import io
    ims = [Image.open(io.BytesIO(b)).convert("RGB") for b in blobs]
    ims[0].save(dest, save_all=True, append_images=ims[1:], resolution=96)
    return True


def fetch_embedded(req, body, here, label, croot, code, log, skipped):
    """Download one page's embedded files → [paths relative to the course root].

    Page images become ONE `<label> (pages).pdf`; any other embedded file is saved under its own
    name. Raises on a failed download so the caller counts it and retries on the next run."""
    from urllib.parse import urljoin
    found = embedded_files(body)
    if not found:
        return []
    base = win_safe(label.removesuffix(" (page)"), 80)
    images, out = [], []
    for i, f in enumerate(found, 1):
        if f["mime"].startswith(("video/", "audio/")):
            skipped.append({"title": base, "file": f["name"], "mime": f["mime"]})
            continue
        url = urljoin(BB_HOST + "/", f["url"])
        if not f["mime"].startswith("image/"):
            h = req.fetch(url, method="HEAD", timeout=60_000)
            size = int(h.headers.get("content-length") or 0) if h.ok else 0
            if size > MAX_BYTES:
                skipped.append({"title": base, "file": f["name"], "bytes": size})
                continue
        r = req.get(url, timeout=300_000)
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status} on embedded file {i} of {len(found)}")
        ct = (r.headers.get("content-type") or f["mime"]).split(";")[0].strip().lower()
        data = r.body()
        if ct.startswith("image/"):
            images.append((data, ct))
            continue
        dest = here / win_safe(f["name"] or f"{base} file {i}", 120)
        dest.write_bytes(data)
        out.append(str(dest.relative_to(croot)))
    if images:
        pdf = here / f"{base} (pages).pdf"
        if images_to_pdf([d for d, _ in images], pdf):
            out.append(str(pdf.relative_to(croot)))
        else:                                   # no Pillow: loose images beat nothing
            for n, (d, ct) in enumerate(images, 1):
                ext = ct.split("/")[-1].replace("jpeg", "jpg")
                dest = here / f"{base} p{n:02d}.{ext}"
                dest.write_bytes(d)
                out.append(str(dest.relative_to(croot)))
    log(f"   + {code}/{(here / base).relative_to(croot)}: {len(images)} embedded image(s) → "
        f"{len(out)} file(s)")
    return out


def course_map(sess):
    """{CODE: courseId} for the semester's real courses (the _EVAL shells are skipped)."""
    out = {}
    for m in sess.get(f"{BASE}/users/{sess.uid}/courses?limit=100").get("results", []):
        c = sess.get(f"{BASE}/courses/{m['courseId']}")
        cid_txt = f"{c.get('courseId', '')} {c.get('name', '')}"
        if "_EVAL" in cid_txt.upper():
            continue
        code = next((k for k in CODES if k in cid_txt.upper()), None)
        if code and code not in out:
            out[code] = c["id"]
    return out


def mirror_course(sess, code, cid, root, log):
    req = sess._page.request
    croot = root / code
    croot.mkdir(parents=True, exist_ok=True)
    mf = croot / "_manifest.json"
    old = json.loads(mf.read_text()) if mf.exists() else {}
    seen_mod = {e["id"]: e.get("modified") for e in old.get("items", [])}
    seen_emb = {e["id"]: e.get("embedded") for e in old.get("items", [])}   # [paths] or None
    seen_instr = {e["id"]: e.get("instr") for e in old.get("items", [])}
    items, files, links, skipped, failed = [], [], [], [], 0

    def walk(parent, path):
        nonlocal failed
        url = f"{BASE}/courses/{cid}/contents" + (f"/{parent}/children" if parent else "")
        d = sess.get(url + "?limit=200")
        if d.get("_status"):
            log(f"   🔴 {code} {'/'.join(path) or '(root)'}: HTTP {d['_status']}")
            failed += 1
            return
        for it in d.get("results", []):
            title = it.get("title") or it.get("id")
            handler = (it.get("contentHandler") or {}).get("id", "")
            avail = (it.get("availability") or {}).get("available")
            rec = {"id": it["id"], "title": title, "handler": handler, "folder": "/".join(path),
                   "created": it.get("created"), "modified": it.get("modified"), "available": avail}
            items.append(rec)
            if handler == "resource/x-bb-folder" or it.get("hasChildren"):
                walk(it["id"], path + [win_safe(title, 60)])
                if handler == "resource/x-bb-folder":
                    continue
            if avail == "No":
                continue                                  # hidden from students: nothing to fetch
            if handler == "resource/x-bb-externallink":
                u = (it.get("contentHandler") or {}).get("url")
                if u:
                    links.append({"title": title, "url": u, "folder": "/".join(path)})
                continue
            here = croot.joinpath(*path) if path else croot
            here.mkdir(parents=True, exist_ok=True)
            # Page text / assignment instructions — re-read only when Blackboard says it changed.
            # Ultra wraps a page's text in an item literally titled "ultraDocumentBody"; name the
            # file after its folder instead, or Gemini sees seven files with the same name.
            label = (path[-1] + " (page)") if title == "ultraDocumentBody" and path else title
            txt = here / f"{win_safe(label, 80)}.txt"
            changed = seen_mod.get(it["id"]) != it.get("modified")
            emb_old = seen_emb.get(it["id"])
            # Embedded files: re-read when the page changed, when this item predates the
            # embedded pass (None), or when a file it produced has gone missing.
            # A TEST item (x-bb-asmt-test-link) has an EMPTY body: its handout text and files live
            # in contentHandler.instructions (measured live 2026-10-14 — every HUMCOM1 lab, Lab 7's
            # docx included, never reached Drive). The listing already carries it; read both.
            instr = (it.get("contentHandler") or {}).get("instructions") or ""
            rec["instr"] = bool(instr)
            emb_due = (changed or emb_old is None or not all((croot / p).exists() for p in emb_old)
                       or (bool(instr) and not seen_instr.get(it["id"])))     # mirrored before this fix
            raw = None
            if changed or not txt.exists() or emb_due:
                raw = sess.get(f"{BASE}/courses/{cid}/contents/{it['id']}?fields=body").get("body") or ""
                if instr:
                    raw = f"{raw}\n{instr}" if raw else instr
            if raw is not None and (changed or not txt.exists()):
                body = html_to_text(raw)
                if len(body) >= 40:
                    head = (f"{code} — {title}\nBlackboard: {'/'.join(path) or '(top level)'}"
                            f" · posted {(it.get('created') or '')[:10]}\n\n")
                    txt.write_text(head + body + "\n", encoding="utf-8")
            if txt.exists():
                files.append({"path": str(txt.relative_to(croot)), "title": title, "kind": "page text",
                              "content": it["id"], "created": it.get("created")})
            if emb_due:
                try:
                    emb_old = fetch_embedded(req, raw, here, label, croot, code, log, skipped)
                except Exception as e:     # one bad page must not end the whole mirror
                    log(f"   🔴 {code} {label} embedded: {type(e).__name__}: {str(e)[:80]}")
                    failed += 1
                    emb_old = None         # retried next run
            rec["embedded"] = emb_old
            for p in emb_old or []:
                files.append({"path": p, "title": label.removesuffix(" (page)"), "kind": "embedded",
                              "content": it["id"], "created": it.get("created")})
            att = sess.get(f"{BASE}/courses/{cid}/contents/{it['id']}/attachments")
            for a in att.get("results") or []:
                fn = win_safe(a.get("fileName") or a["id"], 120)
                dest = here / fn
                if dest.exists() and dest.stat().st_size > 0:
                    files.append({"path": str(dest.relative_to(croot)), "title": title,
                                  "kind": "file", "content": it["id"], "created": it.get("created")})
                    continue
                url = f"{BASE}/courses/{cid}/contents/{it['id']}/attachments/{a['id']}/download"
                mime = (a.get("mimeType") or "").lower()
                if mime.startswith(("video/", "audio/")):
                    # 2026-10-13: a 500 MB+ orientation .mp4 was buffered whole and the 6 AM sweep
                    # was killed at its 30-min limit. Gemini can't use a lecture video from Drive.
                    skipped.append({"title": title, "file": fn, "mime": mime, "folder": "/".join(path)})
                    continue
                try:
                    # Size first: a 500 MB+ lecture video overflowed Playwright's buffer on
                    # 2026-10-12 and Gemini reads nothing over ~100 MB anyway. List it, skip it.
                    size = None
                    h = req.fetch(url, method="HEAD", timeout=60_000)
                    if h.ok:
                        size = int(h.headers.get("content-length") or 0) or None
                    if size and size > MAX_BYTES:
                        skipped.append({"title": title, "file": fn, "bytes": size,
                                        "folder": "/".join(path)})
                        log(f"   ⏭ {code} {fn}: {size / 1e6:.0f} MB — too big for Gemini, listed only")
                        continue
                    r = req.get(url, timeout=300_000)
                    if r.status != 200:
                        log(f"   🔴 {code} {fn}: HTTP {r.status}")
                        failed += 1
                        continue
                    dest.write_bytes(r.body())
                except Exception as e:    # one bad file must not end the whole mirror
                    log(f"   🔴 {code} {fn}: {type(e).__name__}: {str(e)[:80]}")
                    failed += 1
                    continue
                log(f"   + {code}/{dest.relative_to(croot)} ({dest.stat().st_size:,} B)")
                files.append({"path": str(dest.relative_to(croot)), "title": title, "kind": "file",
                              "content": it["id"], "created": it.get("created")})

    walk(None, [])
    mf.write_text(json.dumps({"code": code, "course_id": cid, "items": items, "files": files,
                              "links": links, "skipped": skipped, "failed": failed}, indent=1))
    return len(files), failed


def main():
    args = sys.argv[1:]
    out = DEFAULT_OUT
    if "--out" in args:
        i = args.index("--out")
        out = Path(args[i + 1])
        del args[i:i + 2]
    want = [a.upper() for a in args]
    total = bad = 0
    try:
        with open_session() as sess:
            cm = course_map(sess)
            for code, cid in sorted(cm.items()):
                if want and code not in want:
                    continue
                try:
                    n, f = mirror_course(sess, code, cid, out, print)
                except SessionDead:
                    raise
                except Exception as e:            # a course that breaks is a failure, not a crash
                    print(f"   🔴 {code}: {type(e).__name__}: {str(e)[:160]}")
                    n, f = 0, 1
                print(f"{'✅' if not f else '🔴'} {code}: {n} file(s){f' · {f} FAILED' if f else ''}")
                total, bad = total + n, bad + f
    except SessionDead as e:
        print(e.report())
        return 2
    print(f"MIRROR: {total} file(s) · {bad} failure(s) → {out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
