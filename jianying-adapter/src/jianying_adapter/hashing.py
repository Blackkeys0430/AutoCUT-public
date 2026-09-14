from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON-compatible data deterministically without normalization."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def object_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: str | Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class TreeFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class TreeFingerprint:
    file_count: int
    size_bytes: int
    tree_sha256: str
    files: tuple[TreeFile, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_count": self.file_count,
            "size_bytes": self.size_bytes,
            "tree_sha256": self.tree_sha256,
            "files": [asdict(item) for item in self.files],
        }


def _tree_records(root: Path) -> Iterable[TreeFile]:
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        yield TreeFile(
            path=path.relative_to(root).as_posix(),
            size=path.stat().st_size,
            sha256=file_sha256(path),
        )


def tree_fingerprint(root: str | Path) -> TreeFingerprint:
    root_path = Path(root)
    if not root_path.is_dir():
        raise NotADirectoryError(root_path)
    records = tuple(_tree_records(root_path))
    lines = [f"{item.path}\t{item.size}\t{item.sha256}" for item in records]
    aggregate = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return TreeFingerprint(
        file_count=len(records),
        size_bytes=sum(item.size for item in records),
        tree_sha256=aggregate,
        files=records,
    )

