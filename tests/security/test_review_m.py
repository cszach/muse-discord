"""Review M (lean/core): the secret filter on threads and uploads.

Usage: [MUT_DIR=<tree>] python3 tests/security/test_review_m.py"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_secrets as ts  # noqa: E402
from test_secrets import CH, TOKEN, quiet, token_like  # noqa: E402

FOREIGN = token_like()   # another bot's token, read by the worker


class Tool(unittest.TestCase):
    def thread(self, *args):
        import test_discord_api as td
        td.D.TOKEN = TOKEN
        td.write_owner_items(td.ITEMS)
        td.D.GENERATED_DIR = os.path.dirname(td.D.OWNER_ITEMS_FILE)
        td.D.api_multipart = lambda path, payload, f: td.CALLS.append(
            ("POST", path, payload)) or {"id": "1"}
        td.CALLS.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                td.D.main(["discord-api.py", *td.SERVER, "thread",
                           td.GEN_CH, *args])
            except SystemExit:
                pass
        self.assertFalse([c for c in td.CALLS if c[0] == "POST"],
                         "the thread was posted")

    def test_thread_text_with_a_token_after_a_title(self):
        # Scanning only title + text: "Notes" + "<token>" glues the
        # token's first segment to the title, so the token shape is lost.
        self.thread("Notes", FOREIGN)

    def test_thread_secret_split_between_text_and_upload(self):
        import test_discord_api as td
        upload = os.path.join(os.path.dirname(td.D.OWNER_ITEMS_FILE),
                              "half.txt")
        with open(upload, "w") as f:
            f.write(TOKEN[30:])
        try:
            self.thread("t", "see " + TOKEN[:30], upload)
        finally:
            os.remove(upload)


class Upload(ts.tb.Base):
    def test_secret_split_between_text_and_file(self):
        m = self.mod
        m.TOKEN = TOKEN
        half = os.path.join(m.tmp, "half.txt")
        with open(half, "w") as f:
            f.write(TOKEN[30:])
        opened = []

        def urlopen(*a, **k):
            opened.append(a)
            raise OSError("sent")
        with mock.patch.object(m.urllib.request, "urlopen", urlopen), \
                quiet():
            try:
                m.send_reply_with_files(CH, "see " + TOKEN[:30],
                                        [(half, "half.txt")])
            except (m.SecretBlocked, OSError):
                pass
        self.assertEqual(opened, [], "the upload was sent")


if __name__ == "__main__":
    unittest.main()
