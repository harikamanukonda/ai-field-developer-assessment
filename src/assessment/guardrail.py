"""Task 3: bounded candidate buffering; never release part of a candidate early.

Supported PII: ASCII emails, US SSNs, and 13-19 digit card candidates
(with spaces/hyphens). Oversize candidates are conservatively redacted.
"""
import re

EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
SSN = re.compile(r"[0-9]{3}-[0-9]{2}-[0-9]{4}")
CARD = re.compile(r"(?:[0-9][ -]?){12,18}[0-9]")
EMAIL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.!#$%&'*+/=?^_`{|}~-@")


class StreamingRedactor:
    def __init__(self, max_candidate=512):
        self.pending = ""
        self.suppress = False
        self.max_candidate = max_candidate

    def feed(self, text: str) -> str:
        out = []
        for char in text:
            # Spaces/hyphens inside a numeric candidate must remain buffered.
            numeric = bool(self.pending) and all(c in "0123456789 -" for c in self.pending)
            member = char in EMAIL_CHARS or (numeric and char == " ")
            if self.suppress:
                if char in EMAIL_CHARS or char == " ":
                    continue
                self.suppress = False
                out.append(char)
            elif member:
                self.pending += char
                if len(self.pending) > self.max_candidate:
                    out.append("[REDACTED]")
                    self.pending = ""
                    self.suppress = True
            else:
                out.append(self._flush())
                out.append(char)
        return "".join(out)

    def _flush(self):
        text, self.pending = self.pending, ""
        text = EMAIL.sub("[REDACTED]", text)
        text = SSN.sub("[REDACTED]", text)
        return CARD.sub("[REDACTED]", text)

    def finish(self):
        self.suppress = False
        return self._flush()
