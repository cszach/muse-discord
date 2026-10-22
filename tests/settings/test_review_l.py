"""Review L (PR #53, lean/modules): bin/settings.py and bin/telemetry.py
were made to "run under .venv like the other tools" through
setup_common's re-exec. The re-exec compares realpaths, and a .venv made
by bin/install.sh (python3 -m venv: bin/python3 is a symlink to that
same python3) has the same realpath as the Python that lacks PyYAML, so
it never re-runs.

Usage: [MUT_DIR=<tree>] python3 tests/settings/test_review_l.py"""
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, scratch  # noqa: E402

OWNER = "11100000000000000001"


class VenvReexec(unittest.TestCase):
    def test_bin_tools_rerun_under_a_symlinked_venv(self):
        # "This Python lacks PyYAML": the same interpreter without its
        # site-packages (-S).
        r = subprocess.run([sys.executable, "-S", "-c", "import yaml"],
                           capture_output=True)
        if r.returncode == 0:
            self.skipTest("PyYAML imports even without site-packages")
        pkg = scratch("review-l-venv-")
        shutil.copytree(os.path.join(MUT_DIR, "bin"),
                        os.path.join(pkg, "bin"))
        shutil.copytree(os.path.join(MUT_DIR, "plugins"),
                        os.path.join(pkg, "plugins"))
        for f in ("muse_settings.py", "telemetry.py"):
            shutil.copy(os.path.join(MUT_DIR, f), pkg)
        # .venv as bin/install.sh makes it (python3 -m venv), with the
        # packages (here: the system's) and its .muse-ok marker.
        venv = os.path.join(pkg, ".venv")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip",
                        venv], check=True, capture_output=True)
        self.assertTrue(os.path.islink(os.path.join(venv, "bin",
                                                    "python3")))
        import yaml   # the packages "installed" in it: a .pth to them
        site = os.path.join(venv, "lib", "python%d.%d" % sys.version_info[:2],
                            "site-packages")
        with open(os.path.join(site, "review_l.pth"), "w") as f:
            f.write(os.path.dirname(os.path.dirname(yaml.__file__)) + "\n")
        open(os.path.join(venv, ".muse-ok"), "w").close()
        cfg = os.path.join(pkg, "config.yaml")
        with open(cfg, "w") as f:
            f.write(f'version: 1\nowners: ["{OWNER}"]\n'
                    f'token_from: env:MUSE_TEST_TOKEN\n')
        for tool, args in (("settings.py", ["--config", cfg, "validate"]),
                           ("telemetry.py", ["--config", cfg, "status"])):
            r = subprocess.run(
                [sys.executable, "-S", os.path.join(pkg, "bin", tool)]
                + args, capture_output=True, text=True, timeout=60)
            self.assertNotIn("No module named 'yaml'", r.stderr, tool)
            self.assertEqual(r.returncode, 0, f"{tool}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
