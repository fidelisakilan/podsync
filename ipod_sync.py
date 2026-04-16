"""
iPod Sync — TUI

Downloads your full Apple Music library (albums + playlists) and syncs
everything to a connected iPod Classic / Nano / Mini.

Requirements:
  - cookies.txt (Netscape format) from music.apple.com
  - libgpod installed (pacman -S libgpod)
  - ffmpeg and mp4decrypt in PATH

Usage:
  python ipod_sync.py [--cookies PATH] [--overwrite]
"""

import asyncio
import configparser
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import click
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable, Footer, Label, ListItem, ListView,
    Log, ProgressBar, Static,
)

from gamdl.api import AppleMusicApi, ItunesApi
from gamdl.downloader import (
    AppleMusicBaseDownloader,
    AppleMusicDownloader,
    AppleMusicMusicVideoDownloader,
    AppleMusicSongDownloader,
    AppleMusicUploadedVideoDownloader,
)
from gamdl.interface import (
    AppleMusicInterface,
    AppleMusicMusicVideoInterface,
    AppleMusicSongInterface,
    AppleMusicUploadedVideoInterface,
)


# ── path / config helpers ─────────────────────────────────────────────────────

def _gamdl_output_path() -> str:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(Path.home() / ".gamdl" / "config.ini")
    raw = cfg.get("gamdl", "output_path", fallback="./Apple Music")
    return str(Path(raw).expanduser().resolve())


def _sanitize(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|;]', "_", s).strip()


def _album_complete(output_path: str, artist: str, album: str, track_count: int) -> bool:
    base = Path(output_path)
    for folder in [
        base / _sanitize(artist) / _sanitize(album),
        base / "Compilations" / _sanitize(album),
    ]:
        if folder.is_dir():
            audio = [f for f in folder.iterdir() if f.suffix in (".m4a", ".mp4")]
            if len(audio) >= track_count:
                return True
    return False


# ── cache ─────────────────────────────────────────────────────────────────────

CACHE_PATH = Path.home() / ".apple-music-manager" / "cache.json"


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
    return {"version": 1, "albums": {}, "playlist_tracks": {}}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(CACHE_PATH)


def _cache_album(cache: dict, album_id: str, artist: str, album: str) -> None:
    cache["albums"][album_id] = {
        "completed_at": datetime.now().isoformat(),
        "artist": artist,
        "album": album,
    }
    _save_cache(cache)


# ── gamdl helpers ─────────────────────────────────────────────────────────────

async def _fetch_all_library_playlists(api: AppleMusicApi) -> list[dict]:
    playlists, offset, limit = [], 0, 100
    while True:
        resp = await api._amp_request(
            "/v1/me/library/playlists", {"limit": limit, "offset": offset}
        )
        for item in resp.get("data", []):
            playlists.append({
                "id":   item["id"],
                "name": item.get("attributes", {}).get("name", "Untitled"),
            })
        if not resp.get("next"):
            break
        qs = parse_qs(urlparse(resp["next"]).query)
        offset = int(qs.get("offset", [offset + limit])[0])
    return playlists


async def _fetch_playlist_tracks(api: AppleMusicApi, playlist_id: str) -> list[dict]:
    tracks = []
    try:
        resp = await api.get_library_playlist(playlist_id, include="tracks", limit=100)
    except Exception:
        return tracks
    if not resp or not resp.get("data"):
        return tracks
    pl_data = resp["data"][0]
    rel = pl_data.get("relationships", {}).get("tracks", {})
    tracks.extend(rel.get("data", []))
    next_url = rel.get("next")
    while next_url:
        try:
            more = await api._amp_request(next_url)
            tracks.extend(more.get("data", []))
            next_url = more.get("next")
        except Exception:
            break
    return tracks


def _build_downloader(
    api: AppleMusicApi, itunes: ItunesApi, output: str, overwrite: bool
) -> AppleMusicDownloader:
    iface = AppleMusicInterface(api, itunes)
    base  = AppleMusicBaseDownloader(
        output_path=output, temp_path="./tmp",
        overwrite=overwrite, save_cover=True,
    )
    return AppleMusicDownloader(
        interface=iface,
        base_downloader=base,
        song_downloader=AppleMusicSongDownloader(
            base_downloader=base, interface=AppleMusicSongInterface(iface)
        ),
        music_video_downloader=AppleMusicMusicVideoDownloader(
            base_downloader=base, interface=AppleMusicMusicVideoInterface(iface)
        ),
        uploaded_video_downloader=AppleMusicUploadedVideoDownloader(
            base_downloader=base, interface=AppleMusicUploadedVideoInterface(iface)
        ),
    )


# ── iPod detection ────────────────────────────────────────────────────────────

def _find_ipod_mount() -> str | None:
    user = os.environ.get("USER", "")
    for base in [Path(f"/run/media/{user}"), Path("/media"), Path("/mnt")]:
        if not base.exists():
            continue
        try:
            for entry in base.iterdir():
                if (entry / "iPod_Control").exists():
                    return str(entry)
        except PermissionError:
            continue
    return None


# ── audio metadata ────────────────────────────────────────────────────────────

def _read_audio_meta(path: Path) -> dict:
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
            "title":       tag("\xa9nam"),
            "artist":      tag("\xa9ART"),
            "album":       tag("\xa9alb"),
            "genre":       tag("\xa9gen"),
            "composer":    tag("\xa9wrt"),
            "albumartist": tag("aART"),
            "year":        int(year_raw[:4]) if year_raw else 0,
            "track_nr":    itag("trkn"),
            "tracklen":    int(audio.info.length * 1000),
            "bitrate":     int(audio.info.bitrate),
            "samplerate":  int(audio.info.sample_rate),
            "size":        path.stat().st_size,
        }
    except Exception:
        return {}


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_duration(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def _safe_id(raw: str) -> str:
    return "x" + re.sub(r"[^a-zA-Z0-9]", "_", raw)


# ── log modal ─────────────────────────────────────────────────────────────────

class LogModal(ModalScreen):
    """Popup log viewer — press ESC or / to close."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("/",      "dismiss", "Close"),
        Binding("q",      "dismiss", "Close"),
    ]

    def __init__(self, lines: list[str]) -> None:
        super().__init__()
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="log-modal-box"):
            yield Label("Log  (ESC or / to close)", id="log-modal-title")
            yield Log(id="log-modal-content", auto_scroll=True)

    def on_mount(self) -> None:
        w = self.query_one("#log-modal-content", Log)
        for line in self._lines:
            w.write_line(line)
        w.scroll_end(animate=False)

    def append_line(self, line: str) -> None:
        self.query_one("#log-modal-content", Log).write_line(line)


# ── app ───────────────────────────────────────────────────────────────────────

class IpodSyncApp(App):

    COMMANDS = set()
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { layout: vertical; background: $background; }

    #panels {
        layout: horizontal;
        height: 1fr;
        background: $background;
    }

    #left {
        width: 36;
        height: 1fr;
        border-right: solid $panel;
        background: $background;
    }

    #right {
        width: 1fr;
        height: 1fr;
        background: $background;
    }

    #left-header, #right-header {
        height: 1;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
        text-style: bold;
    }

    #playlist-list {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-color: $panel $background;
    }

    ListItem {
        height: 1;
        padding: 0 1;
        background: $background;
    }

    /* cursor only visible when pane is focused — identical style for both panes */
    ListView > ListItem.-highlight {
        background: transparent;
        border-left: none;
        text-style: none;
        color: $text;
    }

    ListView:focus > ListItem.-highlight {
        background: $primary 20%;
        text-style: none;
        color: $text;
    }

    ListView:focus > ListItem.-highlight > Static {
        background: transparent;
        color: $text;
    }

    #track-table {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-color: $panel $background;
        background: $background;
    }

    DataTable .datatable--cursor {
        background: transparent;
        color: $text;
    }

    DataTable:focus .datatable--cursor {
        background: $primary 20%;
        color: $text;
        border-left: solid $primary;
    }

    /* ── bottom bar ── */
    #bottom-bar {
        width: 100%;
        height: 3;
        border-top: solid $panel;
        background: $background;
        layout: horizontal;
        align: left middle;
        padding: 0 2;
    }

    #phase-label {
        width: auto;
        text-style: bold;
        color: $text;
        padding: 0 1 0 0;
    }

    #progress-count {
        width: auto;
        color: $text-muted;
        padding: 0 1;
    }

    #progress-bar { width: 25; height: 1; min-width: 0; }

    #progress-pct {
        width: 5;
        color: $text-muted;
        padding: 0 0 0 1;
    }

    #bar-spacer { width: 1fr; }

    #ipod-status {
        width: 26;
        text-align: right;
        color: $text-muted;
    }

    #ipod-status.ipod-connected { color: $success; }

    Footer { background: $background; color: $text-muted; }

    /* ── log modal ── */
    LogModal { align: center middle; }

    #log-modal-box {
        width: 90%;
        height: 80%;
        border: solid $accent;
        background: $surface;
        padding: 0 1;
    }

    #log-modal-title {
        height: 1;
        text-style: bold;
        color: $accent;
        padding: 0 1;
    }

    #log-modal-content { height: 1fr; }
    """

    BINDINGS = [
        Binding("tab",    "switch_panel",  "Switch Panel",  show=True),
        Binding("j",      "cursor_down",   "Down",          show=False),
        Binding("k",      "cursor_up",     "Up",            show=False),
        Binding("g",      "g_key",         "gg=Top",        show=False),
        Binding("G",      "cursor_bottom", "Bottom",        show=False),
        Binding("ctrl+f", "page_down",     "Page Down",     show=False),
        Binding("ctrl+b", "page_up",       "Page Up",       show=False),
        Binding("/",      "show_log",      "Log",           show=True),
        Binding("s",      "sync",          "Sync All",      show=True),
        Binding("x",      "stop",          "Stop",          show=True),
        Binding("q",      "quit",          "Quit",          show=True),
    ]

    def __init__(self, cookies: str, overwrite: bool):
        super().__init__()
        self.cookies_path = cookies
        self.output_path  = _gamdl_output_path()
        self.overwrite    = overwrite

        self._api:  AppleMusicApi | None        = None
        self._dl:   AppleMusicDownloader | None = None
        self._albums:    list[dict] = []
        self._playlists: list[dict] = []
        self._ipod_mount: str | None = None
        self._fetching = True   # True while _init is still running
        self._busy   = False
        self._stop   = False
        self._g_pressed = False
        self._log_lines: list[str] = []

    # ── layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="panels"):
            with Vertical(id="left"):
                yield Label("Playlists", id="left-header")
                yield ListView(id="playlist-list")
            with Vertical(id="right"):
                yield Label("Tracks / Albums", id="right-header")
                yield DataTable(id="track-table", show_header=False, cursor_type="row")
        with Horizontal(id="bottom-bar"):
            yield Label("Starting…", id="phase-label")
            yield Label("", id="progress-count")
            yield ProgressBar(
                id="progress-bar", total=100,
                show_eta=False, show_percentage=False,
            )
            yield Label("", id="progress-pct")
            yield Static("", id="bar-spacer")
            yield Label("○ iPod", id="ipod-status")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "catppuccin-mocha"
        self.sub_title = "iPod: not connected"
        self.query_one("#playlist-list", ListView).focus()
        self._init()
        self.set_interval(2.0, self._poll_ipod)

    # ── progress + log helpers ────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        """Store a log line (viewable via / popup)."""
        self._log_lines.append(message)
        for screen in self.screen_stack:
            if isinstance(screen, LogModal):
                screen.append_line(message)
                break

    def _set_progress(
        self, phase: str = "", current: int = 0, total: int = 0
    ) -> None:
        """Update the phase label, progress bar, and count label."""
        if phase:
            try:
                self.query_one("#phase-label", Label).update(phase)
            except Exception:
                pass
        try:
            pb  = self.query_one("#progress-bar", ProgressBar)
            cnt = self.query_one("#progress-count", Label)
            pct = self.query_one("#progress-pct", Label)
            if total > 0:
                pb.update(total=total, progress=current)
                cnt.update(f"{current}/{total}")
                pct.update(f"{current * 100 // total}%")
            else:
                pb.update(total=None)
                cnt.update(f"{current}" if current else "")
                pct.update("")
        except Exception:
            pass

    # ── auth + fetch ──────────────────────────────────────────────────────────

    @work(thread=False)
    async def _init(self) -> None:
        cookies = Path(self.cookies_path)
        if not cookies.exists():
            self._log(f"cookies.txt not found: {cookies}")
            self._set_progress("Error")
            self._fetching = False
            return

        self._set_progress("Authenticating…")
        self._log("Authenticating…")
        try:
            self._api = await AppleMusicApi.create_from_netscape_cookies(
                cookies_path=str(cookies)
            )
        except Exception as e:
            self._log(f"Auth failed: {e}")
            self._set_progress("Auth failed")
            self._fetching = False
            return

        if not self._api.active_subscription:
            self._log("No active Apple Music subscription.")
            self._set_progress("No subscription")
            self._fetching = False
            return

        self._log(f"Signed in — {self._api.storefront.upper()}")

        self._dl = _build_downloader(
            self._api,
            ItunesApi(self._api.storefront, self._api.language),
            self.output_path,
            self.overwrite,
        )

        # ── fetch albums ──────────────────────────────────────────────────────
        self._set_progress("Fetching library", 0, 0)
        self._log("Fetching library albums…")
        try:
            albums, offset, limit, album_total = [], 0, 100, 0
            while True:
                resp = await self._api._amp_request(
                    "/v1/me/library/albums", {"limit": limit, "offset": offset}
                )
                # Grab the total from the first response
                if album_total == 0:
                    album_total = resp.get("meta", {}).get("total", 0)
                batch = resp.get("data", [])
                albums.extend(batch)
                self._set_progress("Fetching library", len(albums), album_total)
                self._log(f"  {len(albums)}/{album_total or '?'} albums")
                if not resp.get("next"):
                    break
                qs = parse_qs(urlparse(resp["next"]).query)
                offset = int(qs.get("offset", [offset + limit])[0])
            self._albums = albums
        except Exception as e:
            self._log(f"Album fetch failed: {e}")
            self._set_progress("Fetch failed")
            self._fetching = False
            return

        # ── fetch playlists ───────────────────────────────────────────────────
        self._set_progress("Fetching playlists", 0, 0)
        self._log("Fetching playlists…")
        try:
            pl_meta = await asyncio.wait_for(
                _fetch_all_library_playlists(self._api), timeout=30
            )
        except asyncio.TimeoutError:
            self._log("Playlist list timed out — continuing without playlists.")
            pl_meta = []
        except Exception as e:
            self._log(f"Playlist fetch failed: {e}")
            pl_meta = []

        self._playlists = []
        for i, pm in enumerate(pl_meta, 1):
            self._set_progress("Fetching playlists", i, len(pl_meta))
            self._log(f"  Playlist {i}/{len(pl_meta)}: {pm['name']}")
            try:
                tracks = await asyncio.wait_for(
                    _fetch_playlist_tracks(self._api, pm["id"]), timeout=10
                )
            except asyncio.TimeoutError:
                self._log(f"    timed out — skipping")
                tracks = []
            except Exception as e:
                self._log(f"    error: {e}")
                tracks = []
            self._playlists.append({"id": pm["id"], "name": pm["name"], "tracks": tracks})

        self._fetching = False
        self._rebuild_playlist_panel()
        total_pl_tracks = sum(len(p["tracks"]) for p in self._playlists)
        summary = (
            f"Ready — {len(self._albums)} albums · "
            f"{len(self._playlists)} playlists · "
            f"{total_pl_tracks} playlist tracks"
        )
        self._log(summary)
        self._set_progress("Ready  [s] sync", len(self._albums), len(self._albums))

    # ── playlist panel ────────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _rebuild_playlist_panel(self) -> None:
        lv = self.query_one("#playlist-list", ListView)
        await lv.clear()
        lv.append(ListItem(
            Static(f"Library ({len(self._albums)} albums)"), id="pl_library",
        ))
        for pl in self._playlists:
            lv.append(ListItem(
                Static(f"{pl['name']}  ({len(pl['tracks'])})"),
                id=_safe_id("pl_" + pl["id"]),
            ))
        lv.index = 0
        self._load_right_panel(0)

    @on(ListView.Highlighted, "#playlist-list")
    def _on_playlist_select(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is not None:
            self._load_right_panel(idx)

    def _load_right_panel(self, playlist_idx: int) -> None:
        dt  = self.query_one("#track-table", DataTable)
        hdr = self.query_one("#right-header", Label)
        dt.clear(columns=True)
        if playlist_idx == 0:
            hdr.update(f"Albums ({len(self._albums)})")
            dt.add_column("artist", width=28)
            dt.add_column("album")
            dt.add_column("n", width=5)
            for a in self._albums:
                attrs  = a.get("attributes", {})
                dt.add_row(
                    Text(attrs.get("artistName", "?"), style="dim"),
                    Text(attrs.get("name", "?")),
                    Text(str(attrs.get("trackCount", "?")), style="dim"),
                )
        else:
            idx = playlist_idx - 1
            if idx >= len(self._playlists):
                return
            pl = self._playlists[idx]
            hdr.update(f"{pl['name']} ({len(pl['tracks'])} tracks)")
            dt.add_column("artist", width=28)
            dt.add_column("title")
            dt.add_column("dur", width=6)
            for t in pl["tracks"]:
                attr = t.get("attributes", {})
                dt.add_row(
                    Text(attr.get("artistName", "?"), style="dim"),
                    Text(attr.get("name", "?")),
                    Text(_fmt_duration(attr.get("durationInMillis", 0)), style="dim"),
                )

    # ── iPod detection ────────────────────────────────────────────────────────

    def _poll_ipod(self) -> None:
        mount = _find_ipod_mount()
        if mount != self._ipod_mount:
            self._ipod_mount = mount
            try:
                lbl = self.query_one("#ipod-status", Label)
                if mount:
                    try:
                        st = os.statvfs(mount)
                        used_gb  = (st.f_blocks - st.f_bavail) * st.f_frsize / 1e9
                        total_gb = st.f_blocks * st.f_frsize / 1e9
                        lbl.update(f"● iPod  {used_gb:.1f}/{total_gb:.1f} GB")
                    except OSError:
                        lbl.update("● iPod")
                    lbl.add_class("ipod-connected")
                else:
                    lbl.update("○ iPod")
                    lbl.remove_class("ipod-connected")
            except Exception:
                pass
            if mount:
                self._log(f"iPod connected at {mount}")
            else:
                self.sub_title = "iPod: not connected"

    # ── log modal ─────────────────────────────────────────────────────────────

    def action_show_log(self) -> None:
        self.push_screen(LogModal(list(self._log_lines)))

    # ── navigation ────────────────────────────────────────────────────────────

    def _go_to_tracks(self) -> None:
        self.query_one("#track-table", DataTable).focus()

    def _go_to_playlists(self) -> None:
        self.query_one("#playlist-list", ListView).focus()

    def action_switch_panel(self) -> None:
        if isinstance(self.focused, DataTable):
            self._go_to_playlists()
        else:
            self._go_to_tracks()

    def action_cursor_down(self) -> None:
        f = self.focused
        if isinstance(f, (DataTable, ListView)):
            f.action_cursor_down()

    def action_cursor_up(self) -> None:
        f = self.focused
        if isinstance(f, (DataTable, ListView)):
            f.action_cursor_up()

    def action_g_key(self) -> None:
        if self._g_pressed:
            self._g_pressed = False
            f = self.focused
            if isinstance(f, DataTable):
                f.move_cursor(row=0)
            elif isinstance(f, ListView):
                f.index = 0
        else:
            self._g_pressed = True
            self.set_timer(0.5, lambda: setattr(self, "_g_pressed", False))

    def action_cursor_bottom(self) -> None:
        f = self.focused
        if isinstance(f, DataTable):
            f.move_cursor(row=f.row_count - 1)
        elif isinstance(f, ListView):
            f.index = max(0, len(f._nodes) - 1)

    def action_page_down(self) -> None:
        f = self.focused
        if isinstance(f, (DataTable, ListView)):
            f.scroll_page_down()

    def action_page_up(self) -> None:
        f = self.focused
        if isinstance(f, (DataTable, ListView)):
            f.scroll_page_up()

    def on_key(self, event) -> None:
        if event.key == "enter":
            if isinstance(self.focused, ListView) and self.focused.id == "playlist-list":
                self._go_to_tracks()
            event.prevent_default()
            event.stop()
        elif event.key in ("escape", "backspace"):
            if isinstance(self.focused, DataTable):
                self._go_to_playlists()
                event.prevent_default()
                event.stop()

    # ── sync ──────────────────────────────────────────────────────────────────

    def action_stop(self) -> None:
        if self._busy:
            self._stop = True
            self._log("⏹ stopping after current track…")
        else:
            self._log("Nothing running.")

    def action_sync(self) -> None:
        if self._busy:
            self._log("Sync already in progress.")
            return
        if self._fetching:
            self._log("Still fetching library — please wait.")
            return
        if not self._dl:
            self._log("Not ready yet — waiting for library load.")
            return
        self._busy = True
        self._stop = False
        self._run_sync()

    @work(thread=False)
    async def _run_sync(self) -> None:

        # ── Phase 1: download all albums ──────────────────────────────────────
        self._set_progress("Phase 1: Downloading", 0, len(self._albums))
        self._log("Phase 1: downloading library")
        cache = _load_cache()
        file_manifest: dict[str, list[str]] = cache.setdefault("file_manifest", {})
        ok = fail = skip = 0
        total = len(self._albums)

        for idx, entry in enumerate(self._albums, 1):
            if idx % 20 == 0:
                await asyncio.sleep(0)

            if self._stop:
                break

            aid      = entry["id"]
            attrs    = entry.get("attributes", {})
            artist   = attrs.get("artistName", "?")
            name     = attrs.get("name", "?")
            n_tracks = attrs.get("trackCount", 0)

            if not self.overwrite and (
                aid in cache["albums"]
                or _album_complete(self.output_path, artist, name, n_tracks)
            ):
                skip += 1
                self._set_progress(current=idx, total=total)
                if skip % 50 == 0:
                    self._log(f"  skipped {skip} already-downloaded… ({idx}/{total})")
                continue

            self._set_progress(current=idx, total=total)
            self._log(f"▶ [{idx}/{total}] {artist} — {name}")

            try:
                resp = await self._api.get_library_album(aid)
            except Exception as e:
                self._log(f"  fail: {e}")
                fail += 1
                continue

            if not resp or not resp.get("data"):
                fail += 1
                continue

            try:
                items = await self._dl.get_collection_download_items(resp["data"][0])
            except Exception as e:
                self._log(f"  fail: {e}")
                fail += 1
                continue

            album_files: list[str] = []
            for tidx, item in enumerate(items, 1):
                if self._stop:
                    break
                track_name = (
                    item.media_metadata.get("attributes", {}).get("name", "?")
                    if item.media_metadata else "?"
                )
                if not self.overwrite and item.final_path and Path(item.final_path).exists():
                    album_files.append(str(item.final_path))
                    ok += 1
                    continue
                try:
                    await self._dl.download(item)
                    if item.final_path:
                        album_files.append(str(item.final_path))
                    self._log(f"  ✓ [{tidx}/{len(items)}] {track_name}")
                    ok += 1
                except Exception as e:
                    self._log(f"  ✗ [{tidx}/{len(items)}] {track_name}: {e}")
                    fail += 1

            file_manifest[aid] = album_files
            _cache_album(cache, aid, artist, name)

        if self._stop:
            self._log(f"⏹ stopped.  downloaded={ok} failed={fail} skipped={skip}")
            self._set_progress("Stopped", total, total)
            self._busy = False
            self._stop = False
            return

        self._log(f"Phase 1 done — downloaded={ok} failed={fail} skipped={skip}")

        # ── Cleanup: remove albums that left the Apple Music library ──────────
        # Build playlist key set first so we don't delete tracks still in a playlist
        playlist_track_keys: set[tuple[str, str]] = set()
        for pl in self._playlists:
            for t_data in pl["tracks"]:
                attr = t_data.get("attributes", {})
                playlist_track_keys.add((attr.get("artistName", ""), attr.get("name", "")))

        current_album_ids = {a["id"] for a in self._albums}
        stale_album_ids = [aid for aid in list(cache["albums"].keys())
                           if aid not in current_album_ids]
        if stale_album_ids:
            self._log(f"Removing {len(stale_album_ids)} albums no longer in library…")
            removed_files = 0
            for aid in stale_album_ids:
                for fpath in file_manifest.get(aid, []):
                    try:
                        p = Path(fpath)
                        if not p.exists():
                            continue
                        meta = _read_audio_meta(p)
                        if meta:
                            key = (meta.get("artist", ""), meta.get("title", ""))
                            if key in playlist_track_keys:
                                continue  # still referenced by a playlist — keep it
                        p.unlink()
                        removed_files += 1
                    except Exception as e:
                        self._log(f"  warning: could not delete {fpath}: {e}")
                del cache["albums"][aid]
                file_manifest.pop(aid, None)
            if removed_files:
                self._log(f"  deleted {removed_files} local files for removed albums")
            _save_cache(cache)

        # ── Phase 1.5: download playlist-only tracks ──────────────────────────
        cache = _load_cache()
        pt_cache: dict[str, str] = cache.setdefault("playlist_tracks", {})
        # catalogId → local file path

        # Build set of catalog IDs still needed by current playlists
        wanted_catalog_ids: dict[str, str] = {}  # catalogId → title
        for pl in self._playlists:
            for t_data in pl["tracks"]:
                attr = t_data.get("attributes", {})
                catalog_id = (
                    attr.get("playParams", {}).get("catalogId")
                    or (t_data["id"] if not t_data["id"].startswith("i.") else None)
                )
                if catalog_id:
                    wanted_catalog_ids[catalog_id] = attr.get("name", "")

        # Delete playlist-only files no longer wanted by any playlist
        orphaned = {cid: path for cid, path in pt_cache.items()
                    if cid not in wanted_catalog_ids}
        if orphaned:
            self._log(f"Removing {len(orphaned)} playlist tracks no longer in any playlist…")
            for cid, path in orphaned.items():
                try:
                    p = Path(path)
                    if p.exists():
                        p.unlink()
                        self._log(f"  deleted {p.name}")
                except Exception as e:
                    self._log(f"  failed to delete {path}: {e}")
                del pt_cache[cid]
            _save_cache(cache)

        # Find which wanted tracks aren't in local files yet
        base = Path(self.output_path)
        local_titles: set[str] = set()
        for f in list(base.rglob("*.m4a")) + list(base.rglob("*.mp4")):
            meta = _read_audio_meta(f)
            if meta:
                local_titles.add(meta.get("title", "").lower())

        missing: dict[str, str] = {
            cid: title for cid, title in wanted_catalog_ids.items()
            if title.lower() not in local_titles
        }

        if missing:
            self._log(f"Phase 1.5: downloading {len(missing)} playlist-only tracks")
            self._set_progress("Phase 1.5: Playlist tracks", 0, len(missing))
            pl_ok = pl_fail = 0
            for pidx, (catalog_id, title) in enumerate(missing.items(), 1):
                if self._stop:
                    break
                self._set_progress(current=pidx, total=len(missing))
                try:
                    song_resp = await self._api.get_song(catalog_id)
                    if not song_resp or not song_resp.get("data"):
                        pl_fail += 1
                        continue
                    item = await self._dl.get_single_download_item(song_resp["data"][0])
                    if item.final_path and Path(item.final_path).exists() and not self.overwrite:
                        pt_cache[catalog_id] = str(item.final_path)
                        pl_ok += 1
                        continue
                    await self._dl.download(item)
                    if item.final_path:
                        pt_cache[catalog_id] = str(item.final_path)
                        _save_cache(cache)
                    self._log(f"  ✓ {title}")
                    pl_ok += 1
                except Exception as e:
                    self._log(f"  ✗ {title}: {e}")
                    pl_fail += 1
            self._log(f"Phase 1.5 done — downloaded={pl_ok} failed={pl_fail}")

        # ── Phase 2: sync to iPod ─────────────────────────────────────────────
        if not self._ipod_mount:
            self._log("No iPod detected — connect your iPod and press [s] to sync again.")
            self._set_progress("Ready  [s] sync", total, total)
            self._busy = False
            return

        # Compute desired state once: local files (= current library) + playlist tracks.
        # This single set drives all iPod cleanup decisions.
        base = Path(self.output_path)
        audio_files = list(base.rglob("*.m4a")) + list(base.rglob("*.mp4"))
        desired_keys: set[tuple[str, str]] = set(playlist_track_keys)
        for f in audio_files:
            meta = _read_audio_meta(f)
            if meta:
                desired_keys.add((meta.get("artist", ""), meta.get("title", "")))
        self._log(f"Desired state: {len(desired_keys)} tracks (library + playlists)")

        self._log(f"Phase 2: syncing to iPod at {self._ipod_mount}")
        await self._sync_to_ipod(audio_files, desired_keys)

        self._busy = False

    async def _sync_to_ipod(
        self,
        audio_files: list,
        desired_keys: set[tuple[str, str]],
    ) -> None:
        from ipod_lib import IpodDatabase, _GPOD_AVAILABLE, _GPOD_ERROR

        if not _GPOD_AVAILABLE:
            self._log(f"libgpod not available: {_GPOD_ERROR}")
            return

        n_files = len(audio_files)
        self._log(f"Found {n_files} local audio files")
        self._set_progress("Phase 2: Syncing iPod", 0, n_files)

        try:
            db = IpodDatabase(self._ipod_mount)
            await asyncio.to_thread(db.open)
        except Exception as e:
            self._log(f"Failed to open iPod: {e}")
            return

        try:
            self._log("Reading existing iPod tracks…")
            ipod_map = await asyncio.to_thread(db.build_track_map)
            self._log(f"iPod has {len(ipod_map)} existing tracks")

            # Remove iPod tracks no longer in the Apple Music library or any playlist
            stale = [(key, t) for key, t in ipod_map.items() if key not in desired_keys]
            if stale:
                self._log(f"Removing {len(stale)} tracks from iPod (no longer in library or playlists)…")
                for key, t in stale:
                    artist, title = key
                    self._log(f"  - {artist!r} — {title!r}")
                    await asyncio.to_thread(db.remove_track, t)
                    del ipod_map[key]
                self._log(f"Removed {len(stale)} stale tracks")

            added = skipped = failed = 0
            for i, f in enumerate(audio_files, 1):
                if self._stop:
                    break
                if i % 20 == 0:
                    await asyncio.sleep(0)
                self._set_progress(current=i, total=n_files)

                meta = _read_audio_meta(f)
                if not meta:
                    continue
                key = (meta.get("artist", ""), meta.get("title", ""))
                if key in ipod_map:
                    skipped += 1
                    continue
                try:
                    t = await asyncio.to_thread(db.add_track, str(f), meta)
                    ipod_map[key] = t
                    added += 1
                    if added % 10 == 0:
                        self._log(f"  copied {added} tracks…  (skipped {skipped})")
                except Exception as e:
                    self._log(f"  ✗ {f.name}: {e}")
                    failed += 1

            self._log(f"Tracks — added={added} skipped={skipped} failed={failed}")

            # Build title-only fallback map for artist-name mismatches
            title_map: dict[str, object] = {}
            for (artist, title), t in ipod_map.items():
                title_map.setdefault(title.lower(), t)

            # Remove iPod playlists not present in Apple Music library
            apple_music_names = {pl["name"] for pl in self._playlists}
            ipod_playlists = await asyncio.to_thread(db.list_playlists)
            removed_pls = []
            for pl_name, pl_ptr in ipod_playlists:
                if pl_name not in apple_music_names:
                    await asyncio.to_thread(db.remove_playlist, pl_ptr)
                    removed_pls.append(pl_name)
            if removed_pls:
                self._log(f"Removed {len(removed_pls)} iPod-only playlists: {removed_pls}")

            n_pl = len(self._playlists)
            self._set_progress("Phase 2: Playlists", 0, n_pl)
            self._log(f"Syncing {n_pl} playlists…")
            for pi, pl_data in enumerate(self._playlists, 1):
                if self._stop:
                    break
                self._set_progress(current=pi, total=n_pl)
                pl_name = pl_data["name"]
                try:
                    pl = db.ensure_playlist(pl_name)
                    db.clear_playlist(pl)
                    pl_added = 0
                    missed = []
                    for t_data in pl_data["tracks"]:
                        attr  = t_data.get("attributes", {})
                        key   = (attr.get("artistName", ""), attr.get("name", ""))
                        title = attr.get("name", "")
                        track = ipod_map.get(key) or title_map.get(title.lower())
                        if track:
                            db.add_track_to_playlist(track, pl)
                            pl_added += 1
                        else:
                            missed.append(key)
                    self._log(f"  ✓ {pl_name!r}: {pl_added}/{pl_added + len(missed)} tracks")
                    for artist, title in missed[:5]:
                        self._log(f"    miss: {artist!r} — {title!r}")
                except Exception as e:
                    self._log(f"  ✗ {pl_name!r}: {e}")

            self._log("Writing iTunesDB…")
            self._set_progress("Writing iTunesDB…", n_pl, n_pl)
            db.fix_playlist_links()
            await asyncio.to_thread(db.save)
            self._log("✓ Sync complete — eject your iPod safely.")
            self._set_progress("✓ Sync complete", n_pl, n_pl)

        except Exception as e:
            self._log(f"Sync failed: {e}")
            self._set_progress("Sync failed")
        finally:
            db.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command()
@click.option("--cookies",   default="./cookies.txt", show_default=True,
              help="Netscape-format cookies from music.apple.com")
@click.option("--overwrite", is_flag=True, default=False,
              help="Re-download even if already exists locally")
def main(cookies: str, overwrite: bool) -> None:
    """Apple Music → iPod Sync TUI"""
    from ipod_lib import ensure_gpod_available
    ensure_gpod_available()
    IpodSyncApp(cookies=cookies, overwrite=overwrite).run()


if __name__ == "__main__":
    main()
