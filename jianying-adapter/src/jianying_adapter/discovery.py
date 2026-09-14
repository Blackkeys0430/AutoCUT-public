from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable


VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,3}$")


@dataclass(frozen=True)
class Installation:
    root: Path
    executable: Path | None
    version: str | None
    version_dir: Path | None
    videoeditor_dll: Path | None
    source: str

    def to_dict(self) -> dict[str, str | None]:
        result = asdict(self)
        return {key: str(value) if isinstance(value, Path) else value for key, value in result.items()}


@dataclass(frozen=True)
class DiscoveryReport:
    installations: tuple[Installation, ...]
    draft_root: Path | None

    def to_dict(self) -> dict[str, object]:
        return {
            "installations": [item.to_dict() for item in self.installations],
            "draft_root": str(self.draft_root) if self.draft_root else None,
        }


RegistryReader = Callable[[], Iterable[Path]]


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def default_registry_roots() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:
        return ()
    locations: list[Path] = []
    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\JianyingPro"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JianyingPro"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\JianyingPro"),
    ]
    for hive, key_name in keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for value_name in ("InstallPath", "InstallLocation", "Path"):
                    try:
                        value, _kind = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    if value:
                        locations.append(Path(os.path.expandvars(str(value))))
        except OSError:
            continue
    return tuple(locations)


def probe_installation(root: str | Path, source: str = "explicit") -> Installation:
    path = Path(root).expanduser().resolve()
    executable_candidates = [path / "JianyingPro.exe"]
    version_dirs = sorted(
        (
            child for child in path.iterdir()
            if child.is_dir() and VERSION_RE.fullmatch(child.name)
        ),
        key=lambda child: _version_key(child.name),
        reverse=True,
    ) if path.is_dir() else []
    executable = next((item for item in executable_candidates if item.is_file()), None)
    selected = next((item for item in version_dirs if (item / "videoeditor.dll").is_file()), None)
    if executable is None and selected is not None and (selected / "JianyingPro.exe").is_file():
        executable = selected / "JianyingPro.exe"
    return Installation(
        root=path,
        executable=executable,
        version=selected.name if selected else None,
        version_dir=selected,
        videoeditor_dll=(selected / "videoeditor.dll") if selected else None,
        source=source,
    )


def default_draft_root(local_app_data: str | Path | None = None) -> Path | None:
    if local_app_data is None:
        raw_local_app_data = os.environ.get("LOCALAPPDATA")
        if not raw_local_app_data:
            return None
        base = Path(raw_local_app_data)
    else:
        base = Path(local_app_data)
    return base / "JianyingPro" / "User Data" / "Projects" / "com.lveditor.draft"


def discover(
    *,
    extra_roots: Iterable[str | Path] = (),
    registry_reader: RegistryReader = default_registry_roots,
    local_app_data: str | Path | None = None,
) -> DiscoveryReport:
    candidates: list[tuple[Path, str]] = []
    candidates.extend((Path(item), "explicit") for item in extra_roots)
    candidates.extend((Path(item), "registry") for item in registry_reader())
    seen: set[str] = set()
    installations: list[Installation] = []
    for path, source in candidates:
        key = os.path.normcase(str(path.expanduser().resolve()))
        if key in seen:
            continue
        seen.add(key)
        installations.append(probe_installation(path, source))
    draft_root = default_draft_root(local_app_data)
    return DiscoveryReport(tuple(installations), draft_root)
