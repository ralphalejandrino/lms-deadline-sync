#!/usr/bin/env python3
"""chat_mirror: instructors' uploads only, DMs and non-course spaces never, Drive links listed.
Run: ./test_chat_mirror.py  (no network: the Chat service is faked)"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import chat_mirror  # noqa: E402


class _X:
    def __init__(self, v): self.v = v
    def execute(self): return self.v


class FakeChat:
    SPACES = [{"name": "spaces/L", "spaceType": "SPACE", "displayName": "IAA1_LITERA1_2627A"},
              {"name": "spaces/A", "spaceType": "SPACE", "displayName": "Intramurals Artist"},
              {"name": "spaces/D", "spaceType": "DIRECT_MESSAGE", "displayName": ""}]
    MSGS = {"spaces/L": [
        {"createTime": "2026-10-07T01:00:00Z", "text": "brief", "sender": {"email": "jz@e.ubaguio.edu", "displayName": "Instructor H"},
         "attachment": [{"contentName": "brief.pdf", "contentType": "application/pdf", "attachmentDataRef": {"resourceName": "r1"}},
                        {"contentName": "Rubric", "driveDataRef": {"driveFileId": "d1"}}]},
        {"createTime": "2026-10-07T02:00:00Z", "text": "my photo", "sender": {"email": "kid@s.ubaguio.edu"},
         "attachment": [{"contentName": "selfie.png", "contentType": "image/png", "attachmentDataRef": {"resourceName": "r2"}}]}],
        "spaces/A": [{"createTime": "2026-10-07T03:00:00Z", "sender": {"email": "t@e.ubaguio.edu"},
                      "attachment": [{"contentName": "art.png", "contentType": "image/png", "attachmentDataRef": {"resourceName": "r3"}}]}],
        "spaces/D": [{"createTime": "2026-10-07T04:00:00Z", "sender": {"email": "t@e.ubaguio.edu"},
                      "attachment": [{"contentName": "dm.pdf", "contentType": "application/pdf", "attachmentDataRef": {"resourceName": "r4"}}]}]}

    def __init__(self): self.downloaded = []
    def spaces(self): return self
    def list(self, pageSize=100, parent=None, pageToken=None, orderBy=None):
        return _X({"spaces": self.SPACES} if parent is None else {"messages": self.MSGS[parent]})
    def messages(self): return self
    def media(self): return self
    def download_media(self, resourceName):
        self.downloaded.append(resourceName)
        return resourceName


class FakeDL:
    def __init__(self, buf, req): self.buf = buf
    def next_chunk(self):
        self.buf.write(b"data")
        return None, True


class Mirror(unittest.TestCase):
    def test_same_name_same_day_keeps_both(self):
        fake = FakeChat()
        two = [{"contentName": "image.png", "contentType": "image/png", "attachmentDataRef": {"resourceName": r}} for r in ("a", "b")]
        fake.MSGS = {**FakeChat.MSGS, "spaces/L": [{"createTime": "2026-09-21T01:00:00Z", "text": "",
                     "sender": {"email": "jz@e.ubaguio.edu"}, "attachment": two}]}
        out = Path(tempfile.mkdtemp())
        with mock.patch.object(chat_mirror, "_svc", return_value=fake), \
                mock.patch("googleapiclient.http.MediaIoBaseDownload", FakeDL):
            chat_mirror.mirror(out, log=lambda *_: None)
        self.assertEqual(len(list((out / "LITERA1" / "_chat").iterdir())), 2)


    def test_only_instructor_files_in_course_spaces(self):
        fake = FakeChat()
        out = Path(tempfile.mkdtemp())
        with mock.patch.object(chat_mirror, "_svc", return_value=fake), \
                mock.patch("googleapiclient.http.MediaIoBaseDownload", FakeDL):
            self.assertEqual(chat_mirror.mirror(out, log=lambda *_: None), 0)
        self.assertEqual(fake.downloaded, ["r1"])                 # not the student's, art space, or DM
        man = json.loads((out / "LITERA1" / "_chat_manifest.json").read_text())
        self.assertEqual(len(man["files"]), 1)
        self.assertRegex(man["files"][0]["path"], r"^_chat/2026-10-07 brief \[[0-9a-f]{6}\]\.pdf$")
        self.assertEqual([f["file"] for f in man["drive_only"]], ["Rubric"])   # listed, not dropped
        self.assertFalse((out / "Intramurals Artist").exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
