"""Fuobe Connect's log: one logger for the whole pack, and every line it writes in plain ASCII.

A console, a file or a pipe takes ASCII in every Windows language, so a line with a masked account (a•••@... is
written a***@...) or a computer's name never stops a log handler. The pack's own marks become their nearest ASCII (a
bullet "*", an arrow ">"); any other character becomes a \\u escape. Only the log line changes: the Connect to Fuobe
window in ComfyUI shows the text as it is.
"""

from __future__ import annotations

import logging

NAME = "fuobe_connect"

_MARKS = str.maketrans(
    {
        "•": "*",  # bullet
        "·": "-",  # middle dot
        "→": ">",  # arrow
        "←": "<",
        "…": "...",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        " ": " ",
    }
)


def ascii_text(text) -> str:
    """The text in ASCII: the marks above as their nearest ASCII, anything else as a \\u escape."""
    return str(text).translate(_MARKS).encode("ascii", "backslashreplace").decode("ascii")


class AsciiLines(logging.Filter):
    """Writes each record's message, and its traceback, in ASCII before any handler sees it."""

    fuobe_ascii = True

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = f"{record.msg!r} {record.args!r}"
        record.msg = ascii_text(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = ascii_text(record.exc_text)
        if record.stack_info:
            record.stack_info = ascii_text(record.stack_info)
        return True


def get_log() -> logging.Logger:
    """The pack's logger, with AsciiLines on it once."""
    log = logging.getLogger(NAME)
    if not any(getattr(f, "fuobe_ascii", False) for f in log.filters):
        log.addFilter(AsciiLines())
    return log
