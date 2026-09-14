from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class DecodedDocument:
    value: dict[str, Any]
    encoded: bool


@runtime_checkable
class DraftCodec(Protocol):
    def read(self, path: str | Path) -> DecodedDocument: ...

    def write(self, path: str | Path, value: dict[str, Any]) -> None: ...


class JsonCodec:
    """Plain JSON codec for synthetic fixtures and offline tests."""

    def read(self, path: str | Path) -> DecodedDocument:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise TypeError(f"Expected JSON object: {path}")
        return DecodedDocument(value=value, encoded=False)

    def write(self, path: str | Path, value: dict[str, Any]) -> None:
        Path(path).write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class _JianyingWriterCodec:
    def __init__(self, codec_module: Any, codec: Any):
        self._module = codec_module
        self._codec = codec

    def read(self, path: str | Path) -> DecodedDocument:
        value, encoded = self._module.load_json_object_with_codec(
            Path(path), content_codec=self._codec
        )
        return DecodedDocument(value=value, encoded=bool(encoded))

    def write(self, path: str | Path, value: dict[str, Any]) -> None:
        self._module.write_json_object_with_codec(
            Path(path), value, content_codec=self._codec, indent=None
        )


@dataclass(frozen=True)
class RoundtripResult:
    equal: bool
    encoded: bool
    decoded: dict[str, Any]


def roundtrip(codec: DraftCodec, value: dict[str, Any], directory: str | Path | None = None) -> RoundtripResult:
    if directory is None:
        with tempfile.TemporaryDirectory(prefix="jianying_adapter_roundtrip_") as temp:
            return roundtrip(codec, value, temp)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".jianying_adapter_roundtrip_",
        suffix=".json",
        dir=root,
    )
    os.close(descriptor)
    path = Path(temporary_name)
    try:
        codec.write(path, value)
        decoded = codec.read(path)
        return RoundtripResult(decoded.value == value, decoded.encoded, decoded.value)
    finally:
        path.unlink(missing_ok=True)


class LazyJianying11DllProvider:
    """Explicit, lazy bridge to a pinned pyJianYingDraft source and 11.x DLL.

    Construction and package import do not load external code or a DLL. Call
    :meth:`load` explicitly to do so.
    """

    def __init__(self, writer_source: str | Path, install_dir: str | Path, timeout: int = 120):
        self.writer_source = Path(writer_source).resolve()
        self.install_dir = Path(install_dir).resolve()
        self.timeout = timeout
        self._loaded: DraftCodec | None = None

    @property
    def loaded(self) -> bool:
        return self._loaded is not None

    def load(self) -> DraftCodec:
        if self._loaded is not None:
            return self._loaded
        package_root = self.writer_source / "pyJianYingDraft"
        dll = self.install_dir / "videoeditor.dll"
        if not (package_root / "draft_codec.py").is_file():
            raise FileNotFoundError(f"Pinned writer source not found: {package_root}")
        if not dll.is_file():
            raise FileNotFoundError(f"videoeditor.dll not found: {dll}")

        package = types.ModuleType("pyJianYingDraft")
        package.__path__ = [str(package_root)]
        package.__file__ = str(package_root / "__init__.py")
        sys.modules["pyJianYingDraft"] = package

        media_stub = types.ModuleType("pymediainfo")

        class DisabledMediaInfo:
            @staticmethod
            def parse(*_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("Media probing is disabled by jianying-adapter")

        media_stub.MediaInfo = DisabledMediaInfo
        sys.modules.setdefault("pymediainfo", media_stub)

        codec_module = importlib.import_module("pyJianYingDraft.draft_codec")
        crypto_module = importlib.import_module("pyJianYingDraft.draft_crypto")
        config = crypto_module.DraftCryptoConfig(
            jy_install_dir=str(self.install_dir),
            timeout=self.timeout,
            isolated=True,
            validate_roundtrip=True,
            backup=False,
        )
        real_codec = codec_module.JianyingDraftCryptoCodec(config)
        self._loaded = _JianyingWriterCodec(codec_module, real_codec)
        return self._loaded

    def explicit_roundtrip(
        self,
        value: dict[str, Any],
        directory: str | Path | None = None,
    ) -> RoundtripResult:
        return roundtrip(self.load(), value, directory)
