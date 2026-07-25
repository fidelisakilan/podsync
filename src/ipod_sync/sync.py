import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_DIR, STAGING_DIR
from .apple_music import LibrarySong

STATE_PATH = APP_DIR / "state.json"

Key = tuple[str, str]


def track_key(artist: str, title: str) -> Key:
    return (artist.strip().casefold(), title.strip().casefold())


def read_audio_meta(path: Path) -> dict:
    try:
        from mutagen.mp4 import MP4

        audio = MP4(str(path))

        def tag(key, default=""):
            v = audio.get(key)
            return str(v[0]) if v else default

        def itag(key, default=0):
            v = audio.get(key)
            if v:
                raw = v[0]
                return int(raw[0]) if isinstance(raw, tuple) else int(raw)
            return default

        year_raw = tag("\xa9day")
        return {
            "title": tag("\xa9nam"),
            "artist": tag("\xa9ART"),
            "album": tag("\xa9alb"),
            "genre": tag("\xa9gen"),
            "composer": tag("\xa9wrt"),
            "albumartist": tag("aART"),
            "year": int(year_raw[:4]) if year_raw else 0,
            "track_nr": itag("trkn"),
            "tracklen": int(audio.info.length * 1000),
            "bitrate": int(audio.info.bitrate),
            "samplerate": int(audio.info.sample_rate),
            "size": path.stat().st_size,
        }
    except Exception:
        return {}


@dataclass
class Counters:
    library: int = 0
    no_catalog: int = 0
    missing: int = 0
    stale: int = 0
    downloaded: int = 0
    unavailable: int = 0
    download_failed: int = 0
    added: int = 0
    add_failed: int = 0
    removed: int = 0
    remove_failed: int = 0
    save_failed: bool = False

    @property
    def total_errors(self) -> int:
        return (
            self.download_failed
            + self.add_failed
            + self.remove_failed
            + (1 if self.save_failed else 0)
        )


@dataclass
class Aliases:
    map: dict[str, Key] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Aliases":
        try:
            raw = json.loads(STATE_PATH.read_text())["aliases"]
            return cls({cid: tuple(key) for cid, key in raw.items()})
        except Exception:
            return cls()

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"aliases": {c: list(k) for c, k in self.map.items()}}))
        tmp.replace(STATE_PATH)

    def key_for(self, song: LibrarySong) -> Key:
        return self.map.get(song.catalog_id) or track_key(song.artist, song.title)

    def record(self, song: LibrarySong, file_key: Key) -> None:
        if file_key != track_key(song.artist, song.title):
            self.map[song.catalog_id] = file_key
            self.save()


def diff(
    songs: list[LibrarySong], ipod_keys: set[Key], aliases: Aliases
) -> tuple[list[LibrarySong], set[Key]]:
    wanted = {aliases.key_for(s) for s in songs}
    missing = [s for s in songs if aliases.key_for(s) not in ipod_keys]
    stale = ipod_keys - wanted
    return missing, stale


def clean_staging() -> None:
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR, ignore_errors=True)
