"""Shared test setup: every test runs in a temp HOME. Nothing touches your real ~/.local-first or launchd."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class TempHome(unittest.TestCase):
    ENV = ("HOME", "LOCAL_FIRST_HOME", "LOCAL_FIRST_CONFIG", "LOCAL_FIRST_NO_NOTIFY", "LOCALJOB_NO_LAUNCHCTL")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        os.environ.update(HOME=self.home, LOCAL_FIRST_HOME=os.path.join(self.home, ".local-first"),
                          LOCAL_FIRST_NO_NOTIFY="1", LOCALJOB_NO_LAUNCHCTL="1")
        os.environ.pop("LOCAL_FIRST_CONFIG", None)
        self.lf = os.environ["LOCAL_FIRST_HOME"]

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)
        return path
