"""origins.py — tracks whether each iPod track was put there by a sync or by
`add`, so `sync-add` never deletes something you added by hand.

Stored on the iPod itself as iPod_Control/ipod-sync.json:
    {"<ipod_path>": "sync" | "add", ...}
A path with no entry defaults to "sync" (so tracks copied by other tools, or
before this file existed, are treated as sync-owned).
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ORIGIN = "sync"
_FILENAME = "ipod-sync.json"


@dataclass
class Origins:
    mountpoint: str
    map: dict[str, str] = field(default_factory=dict)

    @property
    def _path(self) -> Path:
        return Path(self.mountpoint) / "iPod_Control" / _FILENAME

    @classmethod
    def load(cls, mountpoint: str) -> "Origins":
        origins = cls(mountpoint)
        try:
            raw = json.loads(origins._path.read_text())
            origins.map = {
                str(k): str(v) for k, v in raw.items() if v in ("sync", "add")
            }
        except Exception:
            origins.map = {}
        return origins

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.map, sort_keys=True))
        tmp.replace(self._path)

    def get(self, ipod_path: str) -> str:
        return self.map.get(ipod_path, DEFAULT_ORIGIN)

    def set(self, ipod_path: str, origin: str) -> None:
        self.map[ipod_path] = origin

    def drop(self, ipod_path: str) -> None:
        self.map.pop(ipod_path, None)

    def restamp_all(self, ipod_paths: list[str]) -> None:
        self.map = {p: "sync" for p in ipod_paths if p}
