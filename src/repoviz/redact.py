"""Detection and redaction of credential-like values in source text.

Everything that copies source text into an output (review excerpts, diff
hunks, analyzer evidence) passes through :func:`redact`, so a secret committed
by mistake is flagged but never re-published in a report.
"""

from __future__ import annotations

import re

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    # Bounded repetitions keep this linear on long identifier-like lines.
    re.compile(r"""(?i)\b[\w-]{0,40}(?:api[_-]?key|secret|passw(?:or)?d|token|credential)[\w-]{0,40}\s{0,10}[:=]\s{0,10}"""
               r"""(['"])([^'"\s]{8,500})\1"""),
]
_PLACEHOLDER = re.compile(r"(?i)(changeme|example|dummy|placeholder|xxxx|\$\{|<[^>]+>|your[_-]|redacted|test|fake|\*\*\*)")
_QUICK = re.compile(r"(?i)(AKIA|PRIVATE KEY|ghp_|gho_|ghu_|ghs_|github_pat_|sk-|xox|key|secret|passw|token|credential)")
#: The same test as ``_QUICK`` with plain substring checks: about 7x faster, and every excerpt of every analysis
#: goes through it.  A prefilter only: a hit runs the real patterns.
_MARKERS = ("akia", "private key", "ghp_", "gho_", "ghu_", "ghs_", "github_pat_", "sk-", "xox", "key", "secret",
            "passw", "token", "credential")


def _maybe_secret(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _MARKERS)


def contains_secret(text: str) -> bool:
    """True when ``text`` contains a credential-like value that is not an obvious placeholder."""
    if not _maybe_secret(text):
        return False
    for pat in SECRET_PATTERNS:
        m = pat.search(text)
        if m and not _PLACEHOLDER.search(m.group(0)):
            return True
    return False


def redact(text: str) -> str:
    """Hide credential values, keeping a short prefix so a reviewer can recognise them."""
    if not text or not _maybe_secret(text):
        return text

    def hide(m: re.Match[str]) -> str:
        if m.re.groups >= 2:  # key = "value": keep the key, hide the value
            value = m.group(2)
            return m.group(0).replace(value, value[:3] + "…[redacted]")
        return m.group(0)[:6] + "…[redacted]"

    for pat in SECRET_PATTERNS:
        text = pat.sub(hide, text)
    return text
