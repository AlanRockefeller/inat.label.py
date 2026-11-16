"""Private, bounded job diagnostics. Never persist arbitrary generator output.

The generator prints label contents as well as diagnostics. An allowlist of
fixed diagnostic summaries is safer than attempting to redact free-form output.
"""
import re
from collections import deque


class FailureExcerpt:
    """Keep only diagnostic categories, never source text, paths or values."""

    def __init__(self):
        self.lines = deque(maxlen=8)
        self.suppressed_lines = 0

    def add(self, line):
        text = re.sub(r"\x1b\[[0-9;]*m", "", line[:4096]).strip()
        summary = None
        status = re.search(r"\bHTTP(?: error)?[ :]+([1-5][0-9]{2})\b", text, re.I)
        if status:
            summary = "upstream HTTP " + status.group(1)
        elif "limiting requests" in text:
            summary = "upstream rate limited"
        elif text == "No valid observations found.":
            summary = "no valid observations"
        elif text == "Traceback (most recent call last):":
            summary = "Python traceback"
        elif re.match(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):", text):
            # Even exception class names can be chosen by input; record no name.
            summary = "Python exception (message redacted)"
        elif text.startswith("usage:") or re.match(r"^inat\.label\.py: error:", text):
            summary = "generator argument error (details redacted)"
        elif re.match(r"^Error(?: fetching|:).*\b(?:observation|Observation)\b", text):
            summary = "observation unavailable (details redacted)"
        if summary is None:
            self.suppressed_lines += 1
        elif not self.lines or self.lines[-1] != summary:
            self.lines.append(summary)

    def fields(self):
        return {
            "failure_excerpt": "\n".join(self.lines)[:512],
            "failure_excerpt_redacted": True,
            "suppressed_output_lines": self.suppressed_lines,
        }
