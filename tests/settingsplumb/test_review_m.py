"""Review M (lean/core): SettingsCtx.write never makes `by` an owner.

Usage: [MUT_DIR=<tree>] python3 tests/settingsplumb/test_review_m.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "monitorfixes"))
from test_monitorfixes import Base, U1  # noqa: E402


class SettingsWrite(Base):
    def test_write_by_a_non_owner_is_not_an_owners_change(self):
        """A non-owner is an admin, refused outside a server (admins only
        make me quieter there)."""
        ctx = self.m.SettingsCtx()
        with self.assertRaises(self.m.MS.SettingsError):
            ctx.write("all_servers.who.replies", "conversational", U1)


if __name__ == "__main__":
    unittest.main()
