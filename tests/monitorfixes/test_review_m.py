"""Review M (lean/core): flush_pending's burst isolation.

Usage: [MUT_DIR=<tree>] python3 tests/monitorfixes/test_review_m.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_monitorfixes import Base, CH, U1, gmsg, quiet  # noqa: E402


class PoisonAfterAppend(Base):
    def tick(self):
        """The main loop's staging phase: flush, then write what was
        appended (only when flush_pending says it queued something)."""
        for entry in self.st.get("pending", {}).values():
            entry["first_seen"] = 0
        with quiet():
            queue = self.m.load_queue()
            n_before = len(queue)
            try:
                if self.m.flush_pending(self.st, queue):
                    self.m.queue_edit(add=queue[n_before:])
            except Exception:
                pass   # the main loop logs it and goes on

    def test_a_failure_after_the_append_loses_nothing(self):
        """enqueue_message raises after queue.append(item) (here: the
        "queued" ledger write fails once). The burst must end up in
        reply_queue.json, or stay pending to be tried again."""
        msg = gmsg(U1, "@muse hello", mention=True)
        self.stage(msg)
        real = self.m.ledger_record_many
        failed = []

        def flaky(mids, status, *a, **k):
            if status == "queued" and not failed:
                failed.append(1)
                raise OSError("No space left on device")
            return real(mids, status, *a, **k)
        self.m.ledger_record_many = flaky
        self.tick()
        self.tick()   # a retry, if it was kept
        queued = [q["message_id"] for q in self.m.load_queue()]
        pending = f"{CH}:{U1}" in self.st.get("pending", {})
        self.assertTrue(msg["id"] in queued or pending,
                        "the burst is neither queued nor pending")


if __name__ == "__main__":
    unittest.main()
