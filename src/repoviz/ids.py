"""Stable, collision-resistant identifiers.

Identifiers are derived from a *canonical identity key* (for example
``path:file:src/pkg/mod.py``) using BLAKE2b over length-prefixed parts, so that

* the same entity receives the same ID in every snapshot (required for diffs),
* distinct keys that merely differ in punctuation never collide
  (``a/b_c`` and ``a_b/c`` produce unrelated IDs), and
* IDs are safe to use verbatim as Mermaid node identifiers
  (``[a-z]+_[0-9a-f]+``).

80 bits of hash are used by default.  :class:`IdRegistry` detects the
(astronomically unlikely) case of two keys mapping to one ID within a snapshot
and transparently falls back to the full 128-bit digest.
"""

from __future__ import annotations

import hashlib

DEFAULT_HEX_LENGTH = 20
FULL_HEX_LENGTH = 32


def stable_hash(*parts: str, length: int = DEFAULT_HEX_LENGTH) -> str:
    """Hash ``parts`` unambiguously (each part is length-prefixed)."""
    digest = hashlib.blake2b(digest_size=16)
    for part in parts:
        data = part.encode("utf-8", "surrogateescape")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()[:length]


def make_id(prefix: str, key: str, length: int = DEFAULT_HEX_LENGTH) -> str:
    """Return an identifier such as ``file_3f9a...`` for an identity key."""
    return f"{prefix}_{stable_hash(prefix, key, length=length)}"


def edge_id(relationship: str, source_id: str, target_id: str, length: int = DEFAULT_HEX_LENGTH) -> str:
    return f"e_{stable_hash('edge', relationship, source_id, target_id, length=length)}"


def cycle_id(level: str, member_ids: list[str] | tuple[str, ...] | set[str]) -> str:
    return f"cy_{stable_hash('cycle', level, *sorted(member_ids))}"


def content_hash(data: bytes) -> str:
    """Git-compatible blob hash, so worktree content compares with index/tree blobs."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def text_fingerprint(text: str) -> str:
    return stable_hash("text", text, length=FULL_HEX_LENGTH)


class IdCollisionError(RuntimeError):
    pass


class IdRegistry:
    """Tracks key -> ID assignments and resolves collisions deterministically."""

    def __init__(self) -> None:
        self._by_id: dict[str, str] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self.collisions: list[tuple[str, str, str]] = []

    def get(self, prefix: str, key: str) -> str:
        cached = self._by_key.get((prefix, key))
        if cached is not None:
            return cached
        ident = make_id(prefix, key)
        owner = self._by_id.get(ident)
        if owner is not None and owner != key:
            self.collisions.append((ident, owner, key))
            ident = make_id(prefix, key, length=FULL_HEX_LENGTH)
            if self._by_id.get(ident, key) != key:  # pragma: no cover - 128-bit collision
                raise IdCollisionError(f"unresolvable identifier collision for {key!r}")
        self._by_id[ident] = key
        self._by_key[(prefix, key)] = ident
        return ident
