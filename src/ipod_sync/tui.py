"""tui.py — full-screen Textual app for `ipod-sync`."""
from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import time
import threading

from textual import work
from textual.app import App, ComposeResult
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, ListItem, ListView, LoadingIndicator, RichLog, Static

from . import apple_music
from . import device
from .commands import (
    add_songs,
    eject as eject_cmd,
    remove_track,
    sync,
)
from .session import Session, SessionError
from .sync import FAVOURITES_PLAYLIST, RECENT_PLAYLIST, track_key
from .ui import ConfirmModal, TextualUI

class CommandLoop:
    """A dedicated thread running its own asyncio loop for every command.

    Commands mix async network calls with plenty of blocking work (tag scans,
    file copies onto the iPod, iTunesDB saves, ffmpeg). Running them here
    means none of it can ever stall the screen thread. The Apple Music
    client is created on this loop too, so all HTTP stays on one loop.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="ipod-commands", daemon=True
        )
        self._thread.start()

    def submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


# Manually `add`ed tracks (as opposed to ones a sync brought in) are picked
# out by colour instead of a dedicated column.
ADDED_STYLE = "bold cyan"
ADDED_LEGEND = "[bold cyan]cyan[/bold cyan] = added manually"

_COLUMN_LABELS = ("Artist", "Album", "Song")
_COLUMN_WEIGHTS = (28, 32, 40)  # percent of the remaining width, left to right


def _column_widths(table_width: int) -> list[int]:
    """Split the visible table width between the columns so nothing ever
    needs a horizontal scrollbar."""
    padding = 2 * len(_COLUMN_LABELS)  # DataTable pads each cell by 1 on both sides
    scrollbar = 2
    rest = max(24, table_width - scrollbar - padding)
    widths = [max(8, rest * w // 100) for w in _COLUMN_WEIGHTS[:-1]]
    widths.append(max(8, rest - sum(widths)))
    return widths


def _one_line(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _cell(text: str, width: int, added: bool) -> Text:
    return Text(_one_line(text, width), style=ADDED_STYLE if added else "")


def _fmt_duration(ms: int) -> str:
    total = ms // 1000
    return f"{total // 60}:{total % 60:02d}"


class StartupScreen(ModalScreen[None]):
    """Centred overlay shown while the session opens; dismissed when ready."""

    def __init__(self) -> None:
        super().__init__()
        self._phase = "Starting…"

    def compose(self) -> ComposeResult:
        with Vertical(id="startup-box"):
            yield LoadingIndicator(id="startup-spinner")
            yield Label(self._phase, id="startup-phase")

    def set_phase(self, text: str) -> None:
        self._phase = text
        if self.is_mounted:
            try:
                self.query_one("#startup-phase", Label).update(text)
            except Exception:
                pass


class MountModal(ModalScreen[str | None]):
    """Shown at startup when no iPod is detected: rescan, mount, or quit."""

    BINDINGS = [
        Binding("r", "rescan", "Rescan"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._candidates: list = []

    def compose(self) -> ComposeResult:
        with Vertical(id="mount-box"):
            yield Label("No iPod detected.", id="mount-title")
            yield Static("", id="mount-list")
            yield Label("[r] rescan   [1-9] mount a listed device   [q] quit", id="mount-hint")

    def on_mount(self) -> None:
        self._rescan()

    def _rescan(self) -> None:
        mountpoint = device.find_ipod_mount()
        if mountpoint:
            self.dismiss(mountpoint)
            return
        self._candidates = device.list_candidates()
        lines = [
            f"{i + 1}. {c.path}  {c.label}  {c.size_gb}  {c.fstype}"
            for i, c in enumerate(self._candidates)
        ]
        self.query_one("#mount-list", Static).update(
            "\n".join(lines) or "No unmounted USB partitions found."
        )

    def action_rescan(self) -> None:
        self._rescan()

    def action_quit_app(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        ch = event.character
        if ch and ch.isdigit():
            idx = int(ch) - 1
            if 0 <= idx < len(self._candidates):
                try:
                    mountpoint = device.mount(self._candidates[idx].path)
                    self.dismiss(mountpoint)
                except RuntimeError as e:
                    self.query_one("#mount-hint", Label).update(f"Mount failed: {e}")


class LogModal(ModalScreen):
    """Full log history — Esc or q to close."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
    ]

    def __init__(self, lines: list[str]) -> None:
        super().__init__()
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="log-modal-box"):
            yield Label("Log  (Esc or q to close)", id="log-modal-title")
            yield RichLog(id="log-modal-content", markup=True)

    def on_mount(self) -> None:
        w = self.query_one("#log-modal-content", RichLog)
        for line in self._lines:
            w.write(line)


class PlaylistChooser(ModalScreen[int | None]):
    """Floating, centred chooser for the current playlist (Esc from the
    track table opens this instead of a permanent side pane)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "confirm", "Select"),
    ]

    def __init__(self, playlists: list[str], current: int) -> None:
        super().__init__()
        self._playlists = playlists
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="chooser-box"):
            yield Label("Playlists  (j/k move · enter select · esc cancel)", id="chooser-title")
            yield ListView(id="chooser-list")

    async def on_mount(self) -> None:
        lv = self.query_one("#chooser-list", ListView)
        for name in self._playlists:
            await lv.append(ListItem(Static(name)))
        lv.index = self._current
        lv.focus()

    def action_cursor_down(self) -> None:
        self.query_one("#chooser-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#chooser-list", ListView).action_cursor_up()

    def action_confirm(self) -> None:
        self.dismiss(self.query_one("#chooser-list", ListView).index)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.list_view.index)


class AddSearchModal(ModalScreen[list]):
    """`a` opens this: type a query and press Enter to search Apple Music,
    then Enter on a result adds that song straight to the iPod."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    _LOAD_MORE_THRESHOLD = 3  # start fetching once this many rows from the bottom

    def __init__(self, search_fn, ipod_keys: frozenset = frozenset()) -> None:
        """`search_fn(query, page) -> Awaitable[list[LibrarySong]]` — already
        bridged onto whatever loop the Apple Music client lives on.
        `ipod_keys` is `{track_key(artist, title), ...}` for what's already
        on the iPod, so matching results can be flagged instead of quietly
        re-added."""
        super().__init__()
        self._search_fn = search_fn
        self._ipod_keys = ipod_keys
        self._query = ""
        self._next_page = 0
        self._results: list = []
        self._loading = False
        self._exhausted = False

    def compose(self) -> ComposeResult:
        with Vertical(id="add-box"):
            yield Label("Add — search Apple Music", id="add-title")
            yield Input(placeholder="search query…", id="add-query")
            yield DataTable(id="add-results", cursor_type="row")
            yield Label("Enter = search · Esc = cancel", id="add-hint")

    def on_mount(self) -> None:
        table = self.query_one("#add-results", DataTable)
        table.add_column(" ")
        table.add_column("Artist")
        table.add_column("Title")
        table.add_column("Album")
        table.add_column("Duration")
        table.display = False
        self.query_one("#add-query", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        query = event.value.strip()
        if not query:
            return
        self._query = query
        self._next_page = 0
        self._exhausted = False
        self.run_worker(self._search(reset=True), exclusive=True)

    async def _search(self, reset: bool) -> None:
        if self._loading or self._exhausted:
            return
        self._loading = True
        query = self._query
        hint = self.query_one("#add-hint", Label)
        hint.update("Searching…" if reset else "Loading more…")
        try:
            results = await self._search_fn(query, self._next_page)
        except Exception as e:
            self._loading = False
            if query == self._query:
                hint.update(f"[red]search failed: {e}[/red]")
            return
        self._loading = False
        if query != self._query:
            return  # a newer search superseded this one — drop stale results

        table = self.query_one("#add-results", DataTable)
        if reset:
            table.clear()
            self._results = []

        if not results:
            self._exhausted = True
            if reset:
                table.display = False
                hint.update("No results — try another search.")
            else:
                hint.update(f'"{query}" — {len(self._results)} results  ·  j/k move · enter add · esc clear')
            return

        self._next_page += 1
        self._results.extend(results)
        table.display = True
        for s in results:
            on_ipod = track_key(s.artist, s.title) in self._ipod_keys
            style = "dim" if on_ipod else ""
            table.add_row(
                Text("=" if on_ipod else "", style="dim"),
                Text(s.artist, style=style),
                Text(s.title, style=style),
                Text(s.album, style=style),
                Text(_fmt_duration(s.duration_ms), style=style),
            )
        hint.update(
            f'"{query}" — {len(self._results)} results  ·  '
            "j/k move · enter add · esc clear · [dim]= already on iPod[/dim]"
        )
        if reset:
            table.focus()

    def _maybe_load_more(self) -> None:
        table = self.query_one("#add-results", DataTable)
        if (
            table.cursor_row is not None
            and table.cursor_row >= len(self._results) - self._LOAD_MORE_THRESHOLD
            and not self._loading
            and not self._exhausted
        ):
            self.run_worker(self._search(reset=False), exclusive=False)

    def action_cursor_down(self) -> None:
        self.query_one("#add-results", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#add-results", DataTable).action_cursor_up()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Fires for any cursor move (j/k, arrow keys, mouse) — load the next
        # page once the cursor nears the bottom of what's loaded so far.
        self._maybe_load_more()

    def action_cancel(self) -> None:
        inp = self.query_one("#add-query", Input)
        table = self.query_one("#add-results", DataTable)
        if inp.value or table.display:
            # First Escape: back out to a clean query box, same dialog.
            inp.value = ""
            table.clear()
            table.display = False
            self._results = []
            self.query_one("#add-hint", Label).update("Enter = search · Esc = cancel")
            inp.focus()
            return
        # Second Escape, from an already-empty box: cancel the dialog.
        self.dismiss([])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if 0 <= event.cursor_row < len(self._results):
            self.dismiss([self._results[event.cursor_row]])


class IpodSyncApp(App):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { layout: vertical; }
    #header {
        height: 1; background: $surface; color: $text-muted;
        text-style: bold; padding: 0 1;
    }
    #track-table { height: 1fr; overflow-x: hidden; }
    #status-line { height: 1; padding: 0 1; border-top: solid $foreground 20%; }
    /* Terminal-native look: no filled backgrounds, reverse video for the cursor. */
    * {
        scrollbar-color: $foreground 25%;
        scrollbar-color-hover: $foreground 45%;
        scrollbar-color-active: $foreground 65%;
        scrollbar-background: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-background-active: transparent;
        scrollbar-corner-color: transparent;
        scrollbar-size-vertical: 1;
    }
    Screen, Horizontal, Vertical, RichLog, Input, Label, Static { background: transparent; }
    Header, Footer, FooterKey { background: transparent; }
    Header { color: $foreground; text-style: bold; }
    FooterKey .footer-key--key { color: $foreground; text-style: bold; background: transparent; }
    FooterKey .footer-key--description { color: $foreground 60%; background: transparent; }
    #header { color: $foreground 60%; text-style: bold; background: transparent; padding: 0 1; }
    ListView, ListItem { background: transparent; }
    DataTable { background: transparent; }
    DataTable > .datatable--header { background: transparent; color: $foreground 60%; text-style: bold; }
    DataTable > .datatable--hover { background: transparent; }
    /* .datatable--cursor and ListItem's highlighted state are left to
       Textual's own defaults: a real filled tile using the block-cursor
       tokens (ansi_magenta under ansi_color=True), not a hardcoded hex. */
    #filterline { background: transparent; border: none; padding: 0 1; }
    #filterline:focus { border: none; }
    #filterline { height: 1; border-top: solid $panel; }
    #filterline.-hidden { display: none; }

    MountModal, LogModal, ConfirmModal, PickModal, StartupScreen, PlaylistChooser, AddSearchModal { align: center middle; }
    StartupScreen { background: transparent; }
    #startup-box { width: 44; height: 7; border: round $foreground 40%; background: transparent; align: center middle; }
    #startup-spinner { height: 1; color: $foreground 70%; background: transparent; }
    #startup-phase { width: 100%; content-align: center middle; color: $foreground 70%; background: transparent; }
    #mount-box, #log-modal-box, #confirm-box, #pick-box, #chooser-box, #add-box {
        width: 80%; height: auto; max-height: 80%;
        border: solid $accent; background: $surface; padding: 1 2;
    }
    #log-modal-content { height: 30; }
    #pick-table { height: 20; }
    #chooser-box { width: 44; max-height: 70%; }
    #chooser-list { height: auto; max-height: 20; }
    #add-results { height: 14; }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "g_key", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        Binding("ctrl+f", "page_down", "PgDn", show=False),
        Binding("ctrl+b", "page_up", "PgUp", show=False),
        Binding("slash", "open_filter", "/", show=True, key_display="/"),
        Binding("a", "shortcut_add", "add", show=True),
        Binding("s", "shortcut_sync_add", "sync-add", show=True),
        Binding("S", "shortcut_sync_hard", "sync-hard", show=True),
        Binding("e", "shortcut_eject", "eject", show=True),
        Binding("d", "remove_track", "remove", show=True),
        Binding("x", "cancel_worker", "cancel", show=True),
        Binding("grave_accent", "toggle_log", "log", show=True, key_display="`"),
        Binding("L", "toggle_log", "log", show=False),
        Binding("q", "request_quit", "quit", show=True),
        Binding("ctrl+c", "request_quit", "quit", show=False, priority=True),
        Binding("escape", "close_filter", "close", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session: Session | None = None
        self._playlists: list[str] = ["All tracks", FAVOURITES_PLAYLIST, RECENT_PLAYLIST]
        self._tracks_cache: list = []
        self._members_cache: dict[str, set[str]] = {}
        self._current_playlist = 0
        self._g_pressed = False
        self._log_lines: list[str] = []
        self._filter = ""
        self._table_rows: list = []
        self._cmd = CommandLoop()
        self._activity: tuple[str, int, int] | None = None
        self._activity_started = time.monotonic()
        self._spinner_frame = 0
        self._spinner_timer = None
        self._current_future: concurrent.futures.Future | None = None
        self._last_outcome = ""

    # ── layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Label("", id="header")
        yield DataTable(id="track-table", cursor_type="row")
        yield Static("", id="status-line", markup=True)
        yield Input(placeholder="", id="filterline", classes="-hidden")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#track-table", DataTable).focus()
        self._start()

    # ── UI plumbing used by ui.TextualUI ────────────────────────────────────

    def log_line(self, text: str) -> None:
        self._log_lines.append(text)
        if isinstance(self.screen, LogModal):
            self.screen.query_one("#log-modal-content", RichLog).write(text)

    def set_progress(self, desc: str, done: int, total: int) -> None:
        self._activity = (desc, done, total)
        self._render_status()

    # ── status line (Claude-Code-style spinner + elapsed time) ──────────────

    _SPINNER = ("·", "•", "●", "•")

    def _begin_activity(self, name: str) -> None:
        self._activity = (name, 0, 0)
        self._activity_started = time.monotonic()
        self._spinner_frame = 0
        if self._spinner_timer is None:
            self._spinner_timer = self.set_interval(0.15, self._tick_status)
        self._render_status()

    def _end_activity(self, text: str) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._activity = None
        elapsed = self._fmt_elapsed()
        self._last_outcome = f"{text} [dim]({elapsed})[/dim]"
        self.query_one("#status-line", Static).update(self._last_outcome)

    def _flash(self, text: str, seconds: float = 3.0) -> None:
        """Show `text` on the always-visible status line even when nothing
        is "running" and the log pane is hidden."""
        self.query_one("#status-line", Static).update(text)
        if not self._activity:
            self.set_timer(
                seconds,
                lambda: self.query_one("#status-line", Static).update(self._last_outcome),
            )

    def _tick_status(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(self._SPINNER)
        self._render_status()

    def _fmt_elapsed(self) -> str:
        secs = int(time.monotonic() - self._activity_started)
        return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"

    def _render_status(self) -> None:
        if not self._activity:
            return
        desc, done, total = self._activity
        count = f" {done:,}/{total:,}" if total else (f" {done:,}" if done else "")
        frame = self._SPINNER[self._spinner_frame]
        self.query_one("#status-line", Static).update(
            f"[bold]{frame}[/bold] {desc}…{count} [dim]({self._fmt_elapsed()})[/dim]"
        )

    async def _run_on_cmd(self, coro):
        """Run a coroutine on the command loop and await its result here."""
        return await asyncio.wrap_future(self._cmd.submit(coro))

    # ── startup ──────────────────────────────────────────────────────────────

    @work(exclusive=True)
    async def _start(self) -> None:
        mountpoint = device.find_ipod_mount()
        if not mountpoint:
            mountpoint = await self.push_screen_wait(MountModal())
            if not mountpoint:
                self.exit()
                return
        self.log_line(f"iPod at {mountpoint}")
        splash = StartupScreen()
        self.push_screen(splash)
        self._begin_activity("Signing in to Apple Music")
        splash.set_phase("Signing in to Apple Music…")
        ui = TextualUI(self)
        try:
            self.session = await self._run_on_cmd(Session.open(ui, mountpoint=mountpoint))
        except SessionError as e:
            splash.dismiss()
            self.log_line(f"[red]{e}[/red]")
            self._end_activity(f"[red]✗ {e}[/red]")
            return
        except Exception as e:
            splash.dismiss()
            self.log_line(f"[red]{e}[/red]")
            self._end_activity(f"[red]✗ Could not connect: {e}[/red]")
            return
        self.log_line("Signed in.")
        self._activity = ("Reading iPod", 0, 0)
        splash.set_phase("Reading the iPod…")
        try:
            await self._rebuild()
        finally:
            splash.dismiss()
        self._end_activity("[green]✓[/green] Ready")
        # Start inside "All tracks" rather than on the playlist menu.
        self.query_one("#track-table", DataTable).focus()

    async def _rebuild(self) -> None:
        if not self.session:
            return
        session = self.session

        async def _collect():
            with session.db() as db:
                return (
                    db.tracks(),
                    {name: db.playlist_members(pl) for name, pl in db.list_playlists()},
                    db.device_name,
                    db.free_bytes / 1e9,
                )

        tracks, members, device_name, free_gb = await self._run_on_cmd(_collect())
        names = list(members)
        self._tracks_cache = tracks
        self._members_cache = members
        storefront = getattr(self.session.api, "storefront", "").upper()
        self.title = f'iPod "{device_name}"'
        self.sub_title = f"{len(tracks):,} tracks · {free_gb:.1f} GB free · Signed in ({storefront})"

        self._playlists = ["All tracks", FAVOURITES_PLAYLIST, RECENT_PLAYLIST]
        for n in names:
            if n not in (FAVOURITES_PLAYLIST, RECENT_PLAYLIST):
                self._playlists.append(n)

        if self._current_playlist >= len(self._playlists):
            self._current_playlist = 0
        self._load_table(self._current_playlist)

    # ── track table ──────────────────────────────────────────────────────────

    def _current_rows(self) -> list:
        """Tracks for the active playlist, narrowed by the active filter,
        sorted by artist, then album, then song title."""
        if not self.session or self._current_playlist >= len(self._playlists):
            return []
        name = self._playlists[self._current_playlist]
        rows = self._tracks_cache
        if name != "All tracks":
            member_paths = self._members_cache.get(name, set())
            rows = [t for t in rows if t.ipod_path in member_paths]
        needle = self._filter.casefold()
        if needle:
            rows = [t for t in rows if needle in f"{t.artist} {t.title} {t.album}".casefold()]
        return sorted(rows, key=lambda t: (t.artist.casefold(), t.album.casefold(), t.title.casefold()))

    def _load_table(self, idx: int) -> None:
        table = self.query_one("#track-table", DataTable)
        table.clear(columns=True)
        widths = _column_widths(table.size.width or 100)
        for label, width in zip(_COLUMN_LABELS, widths):
            table.add_column(label, width=width)
        header = self.query_one("#header", Label)
        if not self.session or idx >= len(self._playlists):
            self._table_rows = []
            return
        name = self._playlists[idx]
        rows = self._current_rows()
        self._table_rows = rows
        suffix = f"  /{self._filter}" if self._filter else ""
        header.update(f"{name} ({len(rows)}){suffix}  ·  {ADDED_LEGEND}")
        for t in rows:
            added = self.session.origins.get(t.ipod_path) == "add"
            table.add_row(
                _cell(t.artist, widths[0], added),
                _cell(t.album, widths[1], added),
                _cell(t.title, widths[2], added),
            )

    def on_resize(self, event: events.Resize) -> None:
        timer = getattr(self, "_resize_timer", None)
        if timer is not None:
            timer.stop()
        self._resize_timer = self.set_timer(
            0.15, lambda: self._load_table(self._current_playlist)
        )

    # ── vim-ish navigation ───────────────────────────────────────────────────

    def action_cursor_down(self) -> None:
        f = self.focused
        if hasattr(f, "action_cursor_down"):
            f.action_cursor_down()

    def action_cursor_up(self) -> None:
        f = self.focused
        if hasattr(f, "action_cursor_up"):
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
            f.move_cursor(row=max(0, f.row_count - 1))
        elif isinstance(f, ListView):
            f.index = max(0, len(f.children) - 1)

    def action_page_down(self) -> None:
        f = self.focused
        if hasattr(f, "action_page_down"):
            f.action_page_down()

    def action_page_up(self) -> None:
        f = self.focused
        if hasattr(f, "action_page_up"):
            f.action_page_up()

    # ── filter ──────────────────────────────────────────────────────────────

    def action_open_filter(self) -> None:
        inp = self.query_one("#filterline", Input)
        inp.remove_class("-hidden")
        inp.placeholder = "/filter"
        inp.value = self._filter
        inp.focus()
        inp.cursor_position = len(self._filter)

    def _hide_filter_input(self) -> None:
        inp = self.query_one("#filterline", Input)
        inp.add_class("-hidden")
        inp.value = ""
        self.query_one("#track-table", DataTable).focus()

    def action_close_filter(self) -> None:
        inp = self.query_one("#filterline", Input)
        if not inp.has_class("-hidden"):
            self._hide_filter_input()
            if self._filter:
                self._filter = ""
                self._load_table(self._current_playlist)
            return
        if self._filter:
            self._filter = ""
            self._load_table(self._current_playlist)
            self.query_one("#track-table", DataTable).focus()
            return
        if isinstance(self.focused, DataTable):
            # Escape on the track table opens the floating playlist chooser.
            self._open_playlist_chooser()

    @work(exclusive=True)
    async def _open_playlist_chooser(self) -> None:
        choice = await self.push_screen_wait(
            PlaylistChooser(self._playlists, self._current_playlist)
        )
        if choice is not None and choice != self._current_playlist:
            self._current_playlist = choice
            self._load_table(choice)
        self.query_one("#track-table", DataTable).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filterline":
            return
        value = event.value
        self._hide_filter_input()
        self._filter = value.strip()
        self._load_table(self._current_playlist)

    @work(exclusive=True)
    async def _launch_command(self, name: str, handler, params: dict) -> None:
        """Fire-and-forget entry point for callers that are *not* already
        running inside a worker (key-binding actions). Callers already
        inside a worker (e.g. a modal's follow-up dispatch) should await
        `_run_command` directly instead — nesting `@work(exclusive=True)`
        calls in the same group cancels the outer one before the inner
        worker's body ever runs."""
        await self._run_command(name, handler, params)

    async def _run_command(self, name: str, handler, params: dict) -> None:
        ui = TextualUI(self)
        self._begin_activity(name)
        self._current_future = self._cmd.submit(handler(ui, self.session, params))
        outcome = f"[green]✓[/green] {name} done"
        try:
            await asyncio.wrap_future(self._current_future)
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            outcome = f"[yellow]○ {name} cancelled[/yellow]"
            self.log_line(f"[yellow]{name} cancelled.[/yellow]")
        except Exception as e:
            outcome = f"[red]✗ {name} failed: {e}[/red]"
            self.log_line(f"[red]{name} failed: {e}[/red]")
        finally:
            self._current_future = None
            self._end_activity(outcome)
        if name not in ("login", "status"):
            await self._rebuild()

    # ── shortcuts ────────────────────────────────────────────────────────────

    def action_shortcut_add(self) -> None:
        if not self.session:
            self._flash("[yellow]Not ready yet — still connecting.[/yellow]")
            return
        if self._current_future and not self._current_future.done():
            self._flash("[yellow]A command is still running — press x to cancel it.[/yellow]")
            return
        self._open_add_search()

    @work(exclusive=True)
    async def _open_add_search(self) -> None:
        async def search_fn(query: str, page: int):
            return await self._run_on_cmd(
                apple_music.search_songs(self.session.api, query, limit=8, offset=page * 8)
            )

        ipod_keys = frozenset(track_key(t.artist, t.title) for t in self._tracks_cache)
        picked = await self.push_screen_wait(AddSearchModal(search_fn, ipod_keys))
        self.query_one("#track-table", DataTable).focus()
        if picked:
            # Show the log live while it downloads/adds; the user closes it
            # themselves (`` ` ``/Esc/q) same as any other time it's open.
            self.push_screen(LogModal(self._log_lines))
            await self._run_command(
                "add", lambda ui, session, p: add_songs(ui, session, p["songs"]), {"songs": picked}
            )

    def action_shortcut_sync_add(self) -> None:
        self._run_tui_action(
            "sync-add",
            lambda ui, session, p: sync(ui, session, hard=False, jobs=4, yes=False),
            {},
        )

    def action_shortcut_sync_hard(self) -> None:
        self._run_tui_action(
            "sync-hard",
            lambda ui, session, p: sync(ui, session, hard=True, jobs=4, yes=False),
            {},
        )

    def action_shortcut_eject(self) -> None:
        self._run_tui_action("eject", lambda ui, session, p: eject_cmd(ui, session), {})

    def _run_tui_action(self, name: str, handler, params: dict) -> None:
        if not self.session:
            self._flash("[yellow]Not ready yet — still connecting.[/yellow]")
            return
        if self._current_future and not self._current_future.done():
            self._flash("[yellow]A command is still running — press x to cancel it.[/yellow]")
            return
        self._launch_command(name, handler, params)

    def action_remove_track(self) -> None:
        f = self.focused
        if not (isinstance(f, DataTable) and f.id == "track-table" and f.cursor_row is not None):
            return
        if f.cursor_row >= len(self._table_rows):
            return
        if not self.session:
            self._flash("[yellow]Not ready yet — still connecting.[/yellow]")
            return
        if self._current_future and not self._current_future.done():
            self._flash("[yellow]A command is still running — press x to cancel it.[/yellow]")
            return
        self._confirm_remove_track(self._table_rows[f.cursor_row])

    @work(exclusive=True)
    async def _confirm_remove_track(self, track) -> None:
        ok = await self.push_screen_wait(
            ConfirmModal(f'Remove "{track.title}" by {track.artist} from the iPod?', default=False)
        )
        if not ok:
            return
        await self._run_command(
            "remove",
            lambda ui, session, p: remove_track(ui, session, p["ipod_path"]),
            {"ipod_path": track.ipod_path},
        )

    def action_cancel_worker(self) -> None:
        fut = self._current_future
        if fut and not fut.done():
            fut.cancel()
            self.log_line("Cancelling — finishes at the next safe point…")
        else:
            self.log_line("Nothing running.")

    def action_toggle_log(self) -> None:
        if isinstance(self.screen, LogModal):
            self.screen.dismiss()
        else:
            self.push_screen(LogModal(self._log_lines))

    # ── quit ─────────────────────────────────────────────────────────────────

    def action_request_quit(self) -> None:
        self._do_quit()

    @work(exclusive=True)
    async def _do_quit(self) -> None:
        if self.session and not self.session.ejected:
            if self._current_future and not self._current_future.done():
                self.log_line("[yellow]A command is still running — press x to cancel it before quitting.[/yellow]")
                return
            should_eject = await self.push_screen_wait(ConfirmModal("Eject iPod?", default=False))
            if should_eject:
                # Stay on screen with a loader; leave only once the eject succeeded.
                self._begin_activity("Ejecting — flushing data to the iPod")
                try:
                    ok = await self._run_on_cmd(eject_cmd(TextualUI(self), self.session))
                except Exception as e:
                    ok = False
                    self.log_line(f"[red]eject failed: {e}[/red]")
                if not ok:
                    self._end_activity("[red]✗ Eject failed — see log (`), unmount manually or retry with q[/red]")
                    return
                self._end_activity("[green]✓[/green] Ejected — safe to unplug")
                await asyncio.sleep(0.6)
        self._cmd.stop()
        self.exit()


def run_tui() -> None:
    if len(sys.argv) > 1:
        print(
            "ipod-sync only supports the TUI now. Run `ipod-sync`, then use the in-app keys.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    IpodSyncApp(ansi_color=True).run()
