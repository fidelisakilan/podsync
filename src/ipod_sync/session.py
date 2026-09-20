"""session.py — one open Apple Music + iPod session, reused by TUI commands."""
from __future__ import annotations

import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import CACHE_DIR, STAGING_DIR
from . import apple_music, device
from .auth import AuthError, ensure_cookies
from .ipod_lib import IpodDatabase, ensure_gpod_available
from .origins import Origins
from .sync import Aliases
from .ui import UI, console_for


class SessionError(Exception):
    pass


@dataclass
class Session:
    mountpoint: str
    dev: str | None
    cookies: Path
    api: "apple_music.AppleMusicApi"
    downloader: "apple_music.AppleMusicDownloader"
    aliases: Aliases
    origins: Origins
    ejected: bool = False

    @contextmanager
    def db(self):
        with IpodDatabase(self.mountpoint) as db:
            yield db

    @classmethod
    async def open(cls, ui: UI, mountpoint: str | None = None) -> "Session":
        if not ensure_gpod_available():
            raise SessionError("libgpod unavailable — install it (pacman -S libgpod)")
        if not shutil.which("ffmpeg"):
            ui.print("Warning: ffmpeg not found in PATH")

        if mountpoint is None:
            raise SessionError("No iPod mountpoint provided.")
        dev = device.device_for_mount(mountpoint)

        cookies = ensure_cookies(console_for(ui))
        try:
            api = await apple_music.create_api(cookies)
        except AuthError:
            cookies = ensure_cookies(console_for(ui), relogin=True)
            api = await apple_music.create_api(cookies)

        downloader = apple_music.build_downloader(api, CACHE_DIR, STAGING_DIR)
        aliases = Aliases.load()
        origins = Origins.load(mountpoint)
        return cls(mountpoint, dev, cookies, api, downloader, aliases, origins)
