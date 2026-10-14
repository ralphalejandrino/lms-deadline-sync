#!/usr/bin/env python3
"""Tests for bb_mirror — Blackboard → Drive names and page text (2026-10-12).

Run: ./test_bb_mirror.py   (no network, no browser)

The mirror's files land on Windows + Google Drive, so a name Windows rejects is a file that silently
never arrives — Gemini then lacks it, the exact failure the mirror exists to stop.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bb_mirror  # noqa: E402
from bb_mirror import embedded_files, fetch_embedded, html_to_text, win_safe  # noqa: E402


class Names(unittest.TestCase):
    def test_reserved_characters_go(self):
        self.assertEqual(win_safe('Week 9: Tools/Ethics? "draft" <v2>|*'), "Week 9_ Tools_Ethics_ _draft_ _v2___")

    def test_no_trailing_dot_or_space(self):
        self.assertEqual(win_safe("Lesson 1. "), "Lesson 1")

    def test_entities_decoded_and_capped(self):
        self.assertEqual(win_safe("Ethical &amp; Social"), "Ethical & Social")
        self.assertLessEqual(len(win_safe("x" * 300, 60)), 60)

    def test_empty_is_never_empty(self):
        self.assertEqual(win_safe(""), "untitled")


class PageText(unittest.TestCase):
    def test_links_and_lists_survive(self):
        t = html_to_text('<p>Submit on <b>Tuesday</b>.</p><ul><li>One whole sheet</li></ul>'
                         '<a href="https://x.edu/f.pdf">the sheet</a>')
        self.assertIn("Submit on Tuesday.", t)
        self.assertIn("• One whole sheet", t)
        self.assertIn("the sheet (https://x.edu/f.pdf)", t)

    def test_no_tags_left(self):
        self.assertNotIn("<", html_to_text("<div><span style='x'>hi</span></div>"))


# Ultra's real markup, trimmed from PROGIT1 Unit 7 (2026-10-13): the JSON is entity-escaped
# inside the attribute and the href's query string carries &amp;.
PAGE = ('<div><a data-bbfile="{&quot;displayName&quot;:&quot;p_74.jpg&quot;,&quot;mimeType&quot;:'
        '&quot;image/jpeg&quot;,&quot;render&quot;:&quot;inline&quot;}" '
        'href="https://blackboard.ubaguio.edu/bbcswebdav/pid-1/xid-1?a=1&amp;b=2"></a></div>'
        '<div><a data-bbfile="{&quot;displayName&quot;:&quot;p_75.jpg&quot;,&quot;mimeType&quot;:'
        '&quot;image/jpeg&quot;}" href="https://blackboard.ubaguio.edu/bbcswebdav/pid-1/xid-2?a=1"></a></div>'
        '<a href="https://blackboard.ubaguio.edu/bbcswebdav/pid-1/xid-1?a=1&amp;b=2">same again</a>'
        '<p><img src="/bbcswebdav/pid-9/xid-9"></p>'
        '<a data-bbfile="{&quot;displayName&quot;:&quot;Rubric.pdf&quot;,&quot;mimeType&quot;:'
        '&quot;application/pdf&quot;}" href="https://blackboard.ubaguio.edu/bbcswebdav/pid-1/xid-3"></a>'
        '<a href="https://example.com/not-blackboard.jpg">elsewhere</a>')


class Embedded(unittest.TestCase):
    """2026-10-13: PROGIT1's whole module is page images embedded in page bodies — never fetched."""

    def test_finds_every_embedded_file_in_page_order_once(self):
        f = embedded_files(PAGE)
        self.assertEqual([x["url"].rsplit("/", 1)[-1] for x in f], ["xid-1?a=1&b=2", "xid-2?a=1", "xid-9", "xid-3"])
        self.assertEqual(f[0]["mime"], "image/jpeg")
        self.assertEqual(f[0]["name"], "p_74.jpg")
        self.assertEqual(f[3]["name"], "Rubric.pdf")

    def test_plain_page_has_none(self):
        self.assertEqual(embedded_files("<p>Read chapter 2.</p><a href='https://x.edu/a.pdf'>a</a>"), [])


class _Resp:
    def __init__(self, status, ct, data):
        self.status, self.ok, self.headers, self._d = status, status == 200, {"content-type": ct}, data

    def body(self):
        return self._d


class FakeReq:
    def __init__(self, table):
        self.table, self.got = table, []

    def get(self, url, timeout=None):
        self.got.append(url)
        return self.table[url.rsplit("/", 1)[-1]]

    def fetch(self, url, method=None, timeout=None):
        return _Resp(200, "", b"")


def _jpeg(colour):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (20, 30), colour).save(b, "JPEG")
    return b.getvalue()


class FetchEmbedded(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.here = self.root / "UNIT 7"
        self.here.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_images_become_one_pdf_in_order_and_other_files_keep_their_name(self):
        req = FakeReq({"xid-1?a=1&b=2": _Resp(200, "image/jpeg", _jpeg("red")),
                       "xid-2?a=1": _Resp(200, "image/jpeg", _jpeg("blue")),
                       "xid-9": _Resp(200, "image/png", _jpeg("green")),
                       "xid-3": _Resp(200, "application/pdf", b"%PDF-1.4 rubric")})
        out = fetch_embedded(req, PAGE, self.here, "UNIT 7 (page)", self.root, "PROGIT1", lambda *_: None, [])
        self.assertEqual(sorted(out), ["UNIT 7/Rubric.pdf", "UNIT 7/UNIT 7 (pages).pdf"])
        self.assertTrue(req.got[2].startswith("https://blackboard.ubaguio.edu/bbcswebdav/pid-9"))  # relative src
        from pypdf import PdfReader
        self.assertEqual(len(PdfReader(self.here / "UNIT 7 (pages).pdf").pages), 3)
        self.assertEqual((self.here / "Rubric.pdf").read_bytes(), b"%PDF-1.4 rubric")

    def test_a_failed_download_raises_so_it_is_counted_and_retried(self):
        req = FakeReq({"xid-1?a=1&b=2": _Resp(403, "text/html", b"")})
        with self.assertRaises(RuntimeError):
            fetch_embedded(req, PAGE, self.here, "UNIT 7 (page)", self.root, "PROGIT1", lambda *_: None, [])

    def test_without_pillow_the_images_still_arrive(self):
        req = FakeReq({"xid-1?a=1&b=2": _Resp(200, "image/jpeg", b"J1"), "xid-2?a=1": _Resp(200, "image/jpeg", b"J2"),
                       "xid-9": _Resp(200, "image/png", b"P"), "xid-3": _Resp(200, "application/pdf", b"R")})
        real = bb_mirror.images_to_pdf
        bb_mirror.images_to_pdf = lambda blobs, dest: False
        try:
            out = fetch_embedded(req, PAGE, self.here, "UNIT 7 (page)", self.root, "PROGIT1", lambda *_: None, [])
        finally:
            bb_mirror.images_to_pdf = real
        self.assertIn("UNIT 7/UNIT 7 p01.jpg", out)
        self.assertIn("UNIT 7/UNIT 7 p03.png", out)
        self.assertEqual((self.here / "UNIT 7 p02.jpg").read_bytes(), b"J2")


# --- #6 (2026-10-14): TEST items keep their handout in contentHandler.instructions ----------
# Measured live: every HUMCOM1 lab (x-bb-asmt-test-link) has an EMPTY body and 1-2 embedded files
# in `instructions` — so Lab 7's docx, and every lab before it, never reached Drive.
DOCX = ('<p>Laboratory Activity 7: build an e-commerce page. Submit the repo link and the live '
        'site link.</p><a data-bbfile="{&quot;displayName&quot;:&quot;Laboratory_Activity_7.docx&quot;,'
        '&quot;mimeType&quot;:&quot;application/vnd.openxmlformats-officedocument.wordprocessingml.document&quot;}" '
        'href="https://blackboard.ubaguio.edu/bbcswebdav/pid-7/xid-77"></a>')


class FakeSess:
    def __init__(self, items, req):
        self.items, self._page = items, type("P", (), {"request": req})()

    def get(self, url):
        if url.endswith("/contents?limit=200"):
            return {"results": self.items}
        if "/attachments" in url:
            return {"results": []}
        if url.endswith("?fields=body"):
            return {"body": ""}                       # a test item's body really is empty
        return {}


class TestInstructions(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def item(self, instructions=DOCX, modified="2026-10-14T01:00:00Z"):
        return {"id": "_c7_1", "title": "Week 9- Lab 7", "modified": modified, "created": "2026-10-14",
                "contentHandler": {"id": "resource/x-bb-asmt-test-link", "instructions": instructions}}

    def run_once(self, item):
        req = FakeReq({"xid-77": _Resp(200, "application/octet-stream", b"PK docx")})
        bb_mirror.mirror_course(FakeSess([item], req), "HUMCOM1", "_1_1", self.root, lambda *_: None)
        return req

    def test_handout_and_text_reach_the_mirror(self):
        self.run_once(self.item())
        c = self.root / "HUMCOM1"
        self.assertEqual((c / "Laboratory_Activity_7.docx").read_bytes(), b"PK docx")
        self.assertIn("e-commerce page", (c / "Week 9- Lab 7.txt").read_text())

    def test_an_item_mirrored_before_the_fix_is_fetched_again(self):
        import json
        c = self.root / "HUMCOM1"
        c.mkdir()
        (c / "_manifest.json").write_text(json.dumps({"items": [
            {"id": "_c7_1", "modified": "2026-10-14T01:00:00Z", "embedded": []}]}))   # old run: nothing
        self.run_once(self.item())
        self.assertTrue((c / "Laboratory_Activity_7.docx").exists())

    def test_negative_controls(self):
        # an item with no instructions behaves as before: no files, and an unchanged one is not refetched
        req = self.run_once(self.item(instructions=""))
        self.assertEqual(req.got, [])
        req = self.run_once(self.item())          # first real pass fetches the docx once…
        req = self.run_once(self.item())          # …and an unchanged item is not fetched again
        self.assertEqual(req.got, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
