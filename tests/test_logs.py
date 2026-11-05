"""Fuobe Connect's log (logs.py): every line in plain ASCII, so a log written to a file or a pipe in any Windows
language takes it."""

from __future__ import annotations

import io
import logging
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)

from fuobe_connect.logs import NAME, ascii_text, get_log  # noqa: E402

BULLETS = "a•••@example.com"


class LogTest(unittest.TestCase):
    def logged(self, encoding: str, write) -> str:
        """What the pack's log writes through a handler whose stream takes only `encoding`; fails on a logging error."""
        out = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict", write_through=True)
        handler = logging.StreamHandler(out)
        failed = []
        handler.handleError = lambda record: failed.append(sys.exc_info()[1])
        log = get_log()
        level, propagate = log.level, log.propagate
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
        try:
            write(log)
        finally:
            log.removeHandler(handler)
            log.setLevel(level)
            log.propagate = propagate
        self.assertEqual(failed, [], encoding)
        return out.buffer.getvalue().decode(encoding)

    def test_masked_account_in_every_windows_language(self):
        line = f"Connected to https://fuobe.com as {BULLETS}. Your Fuobe jobs now run on this ComfyUI."
        for encoding in ("cp932", "cp936", "cp949", "cp950", "cp1251", "cp1252", "cp437", "ascii"):
            text = self.logged(encoding, lambda log: log.info("[Fuobe Connect] %s", line))
            self.assertIn("as a***@example.com.", text, encoding)

    def test_marks_and_other_text(self):
        text = self.logged("cp932", lambda log: log.warning("[Fuobe Connect] %s", "Settings → Fuobe Connect — “Studio” … スタジオ"))
        self.assertIn('Settings > Fuobe Connect - "Studio" ... \\u30b9\\u30bf\\u30b8\\u30aa', text)

    def test_a_traceback(self):
        def write(log):
            try:
                raise RuntimeError(f"no answer from {BULLETS}")
            except RuntimeError:
                log.exception("[Fuobe Connect] Couldn't reach %s", "スタジオ")

        text = self.logged("cp949", write)
        self.assertIn("RuntimeError: no answer from a***@example.com", text)
        self.assertIn("Couldn't reach \\u30b9", text)

    def test_text_is_formatted_once(self):
        text = self.logged("ascii", lambda log: log.info("%s and %d%%", "100% done", 5))
        self.assertIn("100% done and 5%", text)
        self.assertEqual(self.logged("ascii", lambda log: log.info("50% off")).strip(), "50% off")

    def test_ascii_text(self):
        self.assertEqual(ascii_text("plain [text] 100%"), "plain [text] 100%")
        self.assertEqual(ascii_text(BULLETS), "a***@example.com")
        self.assertEqual(ascii_text("·←‘x’– "), "-<'x'- ")
        self.assertTrue(ascii_text("\U0001f3ac é").isascii())

    def test_one_filter(self):
        for _ in range(3):
            log = get_log()
        self.assertIs(log, logging.getLogger(NAME))
        self.assertEqual(sum(1 for f in log.filters if getattr(f, "fuobe_ascii", False)), 1)

    def test_every_module_logs_through_it(self):
        """No module of the pack logs through another logger (only logs.py makes it)."""
        folder = os.path.join(PACK, "fuobe_connect")
        for name in os.listdir(folder):
            if not name.endswith(".py") or name == "logs.py":
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                source = f.read()
            self.assertIsNone(re.search(r"\blogging\.", source), name)
            if re.search(r"\blog\.(?:info|warning|error|exception)\(", source):
                self.assertIn("log = get_log()", source, name)


if __name__ == "__main__":
    unittest.main()
