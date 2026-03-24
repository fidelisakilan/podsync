"""
Apple Music Library Downloader — TUI

Requirements:
  - cookies.txt (Netscape format) exported from music.apple.com
  - ffmpeg and mp4decrypt in PATH

Usage:
  python main.py [--cookies PATH] [--overwrite]
"""

import configparser
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def _gamdl_output_path() -> str:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(Path.home() / ".gamdl" / "config.ini")
    return cfg.get("gamdl", "output_path", fallback="./Apple Music")

import click
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Input, Label, ListItem, ListView, Log, Static

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


# ── gamdl helpers ─────────────────────────────────────────────────────────────

async def fetch_all_library_albums(api: AppleMusicApi) -> list[dict]:
    albums, offset, limit = [], 0, 100
    while True:
        resp = await api._amp_request("/v1/me/library/albums", {"limit": limit, "offset": offset})
        albums.extend(resp.get("data", []))
        if not resp.get("next"):
            break
        qs = parse_qs(urlparse(resp["next"]).query)
        offset = int(qs.get("offset", [offset + limit])[0])
    return albums


def build_downloader(api: AppleMusicApi, itunes: ItunesApi, output: str, overwrite: bool) -> AppleMusicDownloader:
    iface = AppleMusicInterface(api, itunes)
    base = AppleMusicBaseDownloader(output_path=output, temp_path="./tmp", overwrite=overwrite, save_cover=True)
    return AppleMusicDownloader(
        interface=iface,
        base_downloader=base,
        song_downloader=AppleMusicSongDownloader(base_downloader=base, interface=AppleMusicSongInterface(iface)),
        music_video_downloader=AppleMusicMusicVideoDownloader(base_downloader=base, interface=AppleMusicMusicVideoInterface(iface)),
        uploaded_video_downloader=AppleMusicUploadedVideoDownloader(base_downloader=base, interface=AppleMusicUploadedVideoInterface(iface)),
    )


# ── album tile widget ─────────────────────────────────────────────────────────

def _safe_id(album_id: str) -> str:
    return "a" + re.sub(r"[^a-zA-Z0-9]", "_", album_id)


class AlbumTile(Static):
    def __init__(self, name: str, artist: str, tracks: str):
        super().__init__(
            f"[bold]{name}[/]\n"
            f"[dim]{artist}  ·  {tracks} tracks[/]"
        )


# ── app ───────────────────────────────────────────────────────────────────────

class DownloaderApp(App):

    COMMANDS = set()
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen  { layout: vertical; background: $background; }
    #body   { layout: horizontal; height: 1fr; background: $background; }
    #left   { width: 1fr; height: 1fr; background: $background; }
    Footer  { background: $background; color: $text-muted; }

    #album-list { width: 1fr; height: 1fr; background: $background; }
    ListItem { height: 2; padding: 0 1; background: $background; }
    ListItem.selected { background: $accent 25%; border-left: solid $accent; }

    #log { width: 1fr; height: 1fr; border-left: solid $panel; padding: 0 1; background: $background; }

    #search-row {
        height: 3;
        layout: horizontal;
        align: left middle;
        padding: 0 1;
        background: $surface;
        border-top: solid $accent 50%;
    }
    #search  { width: 1fr; border: none; background: transparent; }
    #counter { width: auto; padding: 0 0 0 2; }

    ListView { scrollbar-size-vertical: 1; scrollbar-color: $panel $background; }
    Log {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        scrollbar-color: $panel $background;
        overflow-x: hidden;
    }
    """

    BINDINGS = [
        Binding("space",   "toggle",       "Toggle",       show=True),
        Binding("a",       "toggle_all",   "Toggle All",   show=True),
        Binding("d",       "download",     "Download",     show=True),
        Binding("x",       "stop",         "Stop",         show=True),
        Binding("j",       "cursor_down",  "Down",         show=False),
        Binding("k",       "cursor_up",    "Up",           show=False),
        Binding("g",       "g_key",        "gg=Top",       show=False),
        Binding("G",       "cursor_bottom","Bottom",       show=False),
        Binding("ctrl+f",  "page_down",    "Page Down",    show=False),
        Binding("ctrl+b",  "page_up",      "Page Up",      show=False),
        Binding("/",       "focus_search", "Search",       show=True),
        Binding("escape",  "blur_search",  "Esc",          show=False),
        Binding("q",       "quit",         "Quit",         show=True),
    ]

    def __init__(self, cookies: str, overwrite: bool):
        super().__init__()
        self.cookies_path = cookies
        self.output_path = _gamdl_output_path()
        self.overwrite = overwrite
        self._api: AppleMusicApi | None = None
        self._dl: AppleMusicDownloader | None = None
        self._all: list[dict] = []
        self._map: dict[str, dict] = {}
        self._visible: list[str] = []
        self._selected: set[str] = set()
        self._busy = False
        self._stop = False
        self._queue: list[dict] = []
        self._g_pressed = False

    # ── layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield ListView(id="album-list")
            yield Log(id="log", auto_scroll=True)
        with Horizontal(id="search-row"):
            yield Input(placeholder="/ to search…", id="search")
            yield Label("", id="counter")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "tokyo-night"
        self.query_one("#album-list", ListView).focus()
        self._init()


    # ── auth + load ───────────────────────────────────────────────────────────

    @work(thread=False)
    async def _init(self) -> None:
        log = self.query_one("#log", Log)
        cookies = Path(self.cookies_path)
        if not cookies.exists():
            log.write_line(f"cookies.txt not found: {cookies}")
            log.write_line("Export from music.apple.com (Netscape format).")
            return
        log.write_line("Authenticating…")
        try:
            self._api = await AppleMusicApi.create_from_netscape_cookies(cookies_path=str(cookies))
        except Exception as e:
            log.write_line(f"Auth failed: {e}")
            return
        if not self._api.active_subscription:
            log.write_line("No active Apple Music subscription.")
            return
        log.write_line(f"Signed in — {self._api.storefront.upper()}")
        self._dl = build_downloader(
            self._api,
            ItunesApi(self._api.storefront, self._api.language),
            self.output_path,
            self.overwrite,
        )
        log.write_line("Fetching library…")
        try:
            self._all = await fetch_all_library_albums(self._api)
        except Exception as e:
            log.write_line(f"Fetch failed: {e}")
            return
        self._map = {a["id"]: a for a in self._all}
        self._apply_filter("")
        log.write_line(f"{len(self._all)} albums  —  space toggle · a all · d download")

    # ── search ────────────────────────────────────────────────────────────────

    @on(Input.Changed, "#search")
    def _search(self, e: Input.Changed) -> None:
        self._apply_filter(e.value)

    def _apply_filter(self, q: str) -> None:
        q = q.strip().lower()
        self._visible = [
            a["id"] for a in self._all
            if not q
            or q in a.get("attributes", {}).get("name", "").lower()
            or q in a.get("attributes", {}).get("artistName", "").lower()
        ]
        self._rebuild_list()
        shown, total = len(self._visible), len(self._all)
        self.query_one("#counter", Label).update(f"{shown}/{total}" if q else f"{total}")

    # ── list ──────────────────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _rebuild_list(self) -> None:
        lv = self.query_one("#album-list", ListView)
        await lv.clear()
        for aid in self._visible:
            a = self._map[aid].get("attributes", {})
            item = ListItem(
                AlbumTile(
                    name=a.get("name", "?"),
                    artist=a.get("artistName", "?"),
                    tracks=str(a.get("trackCount", "?")),
                ),
                id=_safe_id(aid),
            )
            if aid in self._selected:
                item.add_class("selected")
            lv.append(item)

    def _refresh_tile(self, aid: str) -> None:
        try:
            item = self.query_one(f"#{_safe_id(aid)}", ListItem)
            if aid in self._selected:
                item.add_class("selected")
            else:
                item.remove_class("selected")
        except Exception:
            pass

    # ── vim navigation ────────────────────────────────────────────────────────

    def _lv(self) -> ListView:
        return self.query_one("#album-list", ListView)

    def action_cursor_down(self) -> None:
        self._lv().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._lv().action_cursor_up()

    def action_g_key(self) -> None:
        if self._g_pressed:
            self._g_pressed = False
            self.action_cursor_top()
        else:
            self._g_pressed = True
            self.set_timer(0.5, lambda: setattr(self, "_g_pressed", False))

    def action_cursor_top(self) -> None:
        lv = self._lv()
        lv.index = 0

    def action_cursor_bottom(self) -> None:
        lv = self._lv()
        lv.index = len(self._visible) - 1

    def action_page_down(self) -> None:
        self._lv().scroll_page_down()

    def action_page_up(self) -> None:
        self._lv().scroll_page_up()

    def on_key(self, event) -> None:
        if event.key == "enter" and self.focused is self._lv():
            event.prevent_default()
            event.stop()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_blur_search(self) -> None:
        s = self.query_one("#search", Input)
        if s.value:
            s.clear()
            self._apply_filter("")
        self._lv().focus()

    # ── selection ─────────────────────────────────────────────────────────────

    def action_toggle(self) -> None:
        lv = self._lv()
        idx = lv.index
        if idx is None or idx >= len(self._visible):
            return
        aid = self._visible[idx]
        self._selected.discard(aid) if aid in self._selected else self._selected.add(aid)
        self._refresh_tile(aid)

    def action_toggle_all(self) -> None:
        if self._selected.issuperset(self._visible):
            self._selected.difference_update(self._visible)
        else:
            self._selected.update(self._visible)
        for aid in self._visible:
            self._refresh_tile(aid)

    # ── download ──────────────────────────────────────────────────────────────

    def action_stop(self) -> None:
        if self._busy:
            self._stop = True
            self._queue.clear()
            self.query_one("#log", Log).write_line("⏹ stopping after current track…")
        else:
            self.query_one("#log", Log).write_line("Nothing is downloading.")

    def action_download(self) -> None:
        if not self._selected:
            lv = self._lv()
            idx = lv.index
            if idx is None or idx >= len(self._visible):
                self.query_one("#log", Log).write_line("No album selected.")
                return
            albums = [self._map[self._visible[idx]]]
        else:
            albums = [self._map[i] for i in self._selected if i in self._map]
        if not self._dl:
            self.query_one("#log", Log).write_line("Not ready yet.")
            return
        for aid in list(self._selected):
            self._selected.discard(aid)
            self._refresh_tile(aid)
        if self._busy:
            self._queue.extend(albums)
            self.query_one("#log", Log).write_line(f"Queued {len(albums)} album(s)  —  {len(self._queue)} in queue")
            return
        self._busy = True
        self._stop = False
        self._download(albums)

    @work(thread=False)
    async def _download(self, albums: list[dict]) -> None:
        log = self.query_one("#log", Log)
        pending = list(albums)

        while pending:
            ok = fail = 0
            for idx, entry in enumerate(pending, 1):
                if self._stop:
                    log.write_line("⏹ stopped.")
                    self._stop = False
                    self._queue.clear()
                    self._busy = False
                    return
                aid   = entry["id"]
                attrs = entry.get("attributes", {})
                title = f"{attrs.get('artistName','?')} — {attrs.get('name','?')}"
                log.write_line(f"▶ [{idx}/{len(pending)}] {title}")

                try:
                    resp = await self._api.get_library_album(aid)
                except Exception as e:
                    log.write_line(f"  fail: {e}")
                    fail += 1
                    continue

                if not resp or not resp.get("data"):
                    log.write_line("  skip: no data")
                    fail += 1
                    continue

                try:
                    items = await self._dl.get_collection_download_items(resp["data"][0])
                except Exception as e:
                    log.write_line(f"  fail: {e}")
                    fail += 1
                    continue

                for item in items:
                    track = item.media_metadata.get("attributes", {}).get("name", "?") if item.media_metadata else "?"
                    try:
                        await self._dl.download(item)
                        log.write_line(f"  ✓ {track}")
                        ok += 1
                    except Exception as e:
                        log.write_line(f"  ✗ {track}: {e}")
                        fail += 1

            log.write_line(f"finished: {ok} downloaded, {fail} skipped")

            if self._queue:
                pending = list(self._queue)
                self._queue.clear()
                log.write_line(f"▶▶ starting queued batch ({len(pending)} albums)…")
            else:
                pending = []

        self._busy = False


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command()
@click.option("--cookies",   default="./cookies.txt", show_default=True)
@click.option("--overwrite", is_flag=True, default=False)
def main(cookies, overwrite):
    """Apple Music Library Downloader — TUI"""
    DownloaderApp(cookies=cookies, overwrite=overwrite).run()


if __name__ == "__main__":
    main()
