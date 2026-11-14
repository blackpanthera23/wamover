"""Safety helpers: hashing and read-only source guards."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


class SourceModifiedError(RuntimeError):
    """Raised if a source file changed while we were working with it."""


def assert_unchanged(path: Path, expected_sha256: str) -> None:
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise SourceModifiedError(
            f"Source file {path} changed during processing "
            f"(expected {expected_sha256}, got {actual})."
        )
