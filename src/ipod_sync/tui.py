"""tui.py — full-screen Textual app: `ipod-sync` with no subcommand.

Every `:` command line reuses the click commands defined in cli.py for
argument parsing (`cmd.make_context`), then runs the matching commands.py
coroutine as a worker so the UI stays responsive.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import shlex
import textwrap
import time
import threading

import click
from textual import work
from textual.app import App, ComposeResult
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Label, ListItem, ListView, LoadingIndicator, RichLog, Static

from . import cli as cli_module
from . import device
from .commands import add, clean_orphans, eject as eject_cmd, login as login_cmd, sync, status as status_cmd
from .session import Session, SessionError
from .sync import FAVOURITES_PLAYLIST, RECENT_PLAYLIST
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


# Maps a click command name to the commands.py coroutine + how to build its
# kwargs from the parsed click.Context.params.
_COMMAND_MAP = {
    "add": lambda ui, session, p: add(ui, session, list(p["query"]), yes=p["yes"], youtube=p["youtube"]),
    "sync-add": lambda ui, session, p: sync(ui, session, hard=False, jobs=p["jobs"], yes=p["yes"]),
    "sync-hard": lambda ui, session, p: sync(ui, session, hard=True, jobs=p["jobs"], yes=p["yes"]),
    "eject": lambda ui, session, p: eject_cmd(ui, session),
    "login": lambda ui, session, p: login_cmd(ui),
    "status": lambda ui, session, p: status_cmd(ui),
    "clean-orphans": lambda ui, session, p: clean_orphans(ui, session, yes=p["yes"]),
}

_COLUMN_LABELS = ("Song", "Album", "Origin")
_ORIGIN_WIDTH = 6


def _column_widths(table_width: int) -> list[int]:
    """Split the visible table width between the columns so nothing ever
    needs a horizontal scrollbar: Origin is fixed, the rest goes 60/40."""
    padding = 2 * len(_COLUMN_LABELS)  # DataTable pads each cell by 1 on both sides
    scrollbar = 2
    rest = max(16, table_width - scrollbar - padding - _ORIGIN_WIDTH)
    song = max(8, rest * 60 // 100)
    album = max(8, rest - song)
    return [song, album, _ORIGIN_WIDTH]


def _one_line(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _song_cell(title: str, artist: str, width: int) -> Text:
    """Title on the first line, artist dimmed on the second."""
    return Text.assemble(_one_line(title, width), "\n", (_one_line(artist, width), "dim"))


ROW_LINES = 2  # every row is two lines tall so wrapping is uniform


def _wrap_cell(text: str, width: int) -> tuple[str, int]:
    """Wrap to at most ROW_LINES lines; anything longer is cut with an ellipsis."""
    lines = textwrap.wrap(text, width=width, break_long_words=True) or [""]
    if len(lines) > ROW_LINES:
        lines = lines[:ROW_LINES]
        lines[-1] = lines[-1][: max(0, width - 1)] + "…"
    return "\n".join(lines), ROW_LINES


# Commands that don't need an open Session (mount + Apple Music sign-in).
_NO_SESSION_NEEDED = {"login", "status"}


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


class IpodSyncApp(App):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { layout: vertical; }
    #panels { layout: horizontal; height: 1fr; }
    #left { width: 30; height: 1fr; border-right: solid $panel; }
    #right { width: 1fr; height: 1fr; }
    #left-header, #right-header {
        height: 1; background: $surface; color: $text-muted;
        text-style: bold; padding: 0 1;
    }
    #playlist-list { height: 1fr; }
    #track-table { height: 1fr; }
    #status-line { height: 1; padding: 0 1; border-top: solid $foreground 20%; }
    #log-pane { height: 10; border-top: solid $foreground 20%; }
    #log-pane.-hidden { display: none; }

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
    #left-header, #right-header { color: $foreground 60%; text-style: bold; background: transparent; padding: 0 1; }
    ListView, ListItem { background: transparent; }
    ListView > ListItem.--highlight { background: transparent; }
    ListView > ListItem.--highlight Static { text-style: reverse; }
    ListView:focus > ListItem.--highlight Static { text-style: bold reverse; }
    DataTable { background: transparent; }
    DataTable > .datatable--header { background: transparent; color: $foreground 60%; text-style: bold; }
    DataTable > .datatable--cursor { background: transparent; text-style: reverse; }
    DataTable:focus > .datatable--cursor { background: transparent; text-style: bold reverse; }
    DataTable > .datatable--hover { background: transparent; }
    #cmdline { background: transparent; border: none; padding: 0 1; }
    #cmdline:focus { border: none; }
    #track-table { overflow-x: hidden; }
    #cmdline { height: 1; border-top: solid $panel; }
    #cmdline.-hidden { display: none; }

    MountModal, LogModal, ConfirmModal, PickModal, StartupScreen { align: center middle; }
    StartupScreen { background: transparent; }
    #startup-box { width: 44; height: 7; border: round $foreground 40%; background: transparent; align: center middle; }
    #startup-spinner { height: 1; color: $foreground 70%; background: transparent; }
    #startup-phase { width: 100%; content-align: center middle; color: $foreground 70%; background: transparent; }
    #mount-box, #log-modal-box, #confirm-box, #pick-box {
        width: 80%; height: auto; max-height: 80%;
        border: solid $accent; background: $surface; padding: 1 2;
    }
    #log-modal-content { height: 30; }
    #pick-table { height: 20; }
    """

    BINDINGS = [
        Binding("tab", "switch_panel", "Switch", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "g_key", "Top", show=False),
        Binding("G", "cursor_bottom", "Bottom", show=False),
        Binding("ctrl+f", "page_down", "PgDn", show=False),
        Binding("ctrl+b", "page_up", "PgUp", show=False),
        Binding("colon", "open_cmdline", ":", show=True, key_display=":"),
        Binding("slash", "open_filter", "/", show=True, key_display="/"),
        Binding("a", "shortcut_add", "add", show=True),
        Binding("s", "shortcut_sync_add", "sync-add", show=True),
        Binding("S", "shortcut_sync_hard", "sync-hard", show=True),
        Binding("e", "shortcut_eject", "eject", show=True),
        Binding("x", "cancel_worker", "cancel", show=True),
        Binding("grave", "toggle_log", "log", show=True, key_display="`"),
        Binding("q", "request_quit", "quit", show=True),
        Binding("ctrl+c", "request_quit", "quit", show=False, priority=True),
        Binding("escape", "close_cmdline", "close", show=False),
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
        self._input_mode = "command"
        self._cmd = CommandLoop()
        self._activity: tuple[str, int, int] | None = None
        self._activity_started = time.monotonic()
        self._spinner_frame = 0
        self._spinner_timer = None
        self._current_future: concurrent.futures.Future | None = None

    # ── layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Horizontal(id="panels"):
            with Vertical(id="left"):
                yield Label("Playlists", id="left-header")
                yield ListView(id="playlist-list")
            with Vertical(id="right"):
                yield Label("Tracks", id="right-header")
                yield DataTable(id="track-table", cursor_type="row")
        yield Static("", id="status-line", markup=True)
        yield RichLog(id="log-pane", markup=True, classes="-hidden")
        yield Input(placeholder="", id="cmdline", classes="-hidden")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#playlist-list", ListView).focus()
        self._start()

    # ── UI plumbing used by ui.TextualUI ────────────────────────────────────

    def log_line(self, text: str) -> None:
        self._log_lines.append(text)
        self.query_one("#log-pane", RichLog).write(text)

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
        self.query_one("#status-line", Static).update(f"{text} [dim]({elapsed})[/dim]")

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

        lv = self.query_one("#playlist-list", ListView)
        await lv.clear()
        for name in self._playlists:
            await lv.append(ListItem(Static(name)))
        lv.index = 0
        self._current_playlist = 0
        self._load_table(0)

    # ── track table ──────────────────────────────────────────────────────────

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "playlist-list":
            return
        idx = event.list_view.index
        if idx is not None:
            self._current_playlist = idx
            self._load_table(idx)

    def _load_table(self, idx: int) -> None:
        table = self.query_one("#track-table", DataTable)
        table.clear(columns=True)
        widths = _column_widths(table.size.width or 100)
        for label, width in zip(_COLUMN_LABELS, widths):
            table.add_column(label, width=width)
        header = self.query_one("#right-header", Label)
        if not self.session or idx >= len(self._playlists):
            return
        name = self._playlists[idx]
        rows = self._tracks_cache
        if name != "All tracks":
            member_paths = self._members_cache.get(name, set())
            rows = [t for t in rows if t.ipod_path in member_paths]
        suffix = f"  /{self._filter}" if self._filter else ""
        header.update(f"{name} ({len(rows)}){suffix}")
        needle = self._filter.casefold()
        for t in rows:
            if needle and needle not in f"{t.artist} {t.title} {t.album}".casefold():
                continue
            origin = self.session.origins.get(t.ipod_path)
            album, _ = _wrap_cell(t.album, widths[1])
            table.add_row(_song_cell(t.title, t.artist, widths[0]), album, origin, height=ROW_LINES)

    def on_resize(self, event: events.Resize) -> None:
        timer = getattr(self, "_resize_timer", None)
        if timer is not None:
            timer.stop()
        self._resize_timer = self.set_timer(
            0.15, lambda: self._load_table(self._current_playlist)
        )

    # ── vim-ish navigation ───────────────────────────────────────────────────

    def action_switch_panel(self) -> None:
        if isinstance(self.focused, DataTable):
            self.query_one("#playlist-list", ListView).focus()
        else:
            self.query_one("#track-table", DataTable).focus()

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

    # ── command line / filter ───────────────────────────────────────────────

    def action_open_cmdline(self) -> None:
        self._open_input("command", "")

    def action_open_filter(self) -> None:
        self._open_input("filter", self._filter)

    def _open_input(self, mode: str, prefill: str) -> None:
        self._input_mode = mode
        inp = self.query_one("#cmdline", Input)
        inp.remove_class("-hidden")
        inp.placeholder = ":command" if mode == "command" else "/filter"
        inp.value = prefill
        inp.focus()
        inp.cursor_position = len(prefill)

    def action_close_cmdline(self) -> None:
        inp = self.query_one("#cmdline", Input)
        if not inp.has_class("-hidden"):
            inp.add_class("-hidden")
            inp.value = ""
            self.query_one("#track-table", DataTable).focus()
        elif isinstance(self.focused, DataTable):
            # Escape in the track list goes back to the playlist pane.
            self.query_one("#playlist-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Enter on a playlist opens its tracks.
        if event.list_view.id == "playlist-list":
            self.query_one("#track-table", DataTable).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value
        mode = self._input_mode
        self.action_close_cmdline()
        if mode == "filter":
            self._filter = value.strip()
            self._load_table(self._current_playlist)
        else:
            self._execute_line(value)

    def _execute_line(self, line: str) -> None:
        line = line.strip()
        if not line or line in (":q", "q", "quit"):
            if line:
                self.action_request_quit()
            return
        try:
            parts = shlex.split(line)
        except ValueError as e:
            self.log_line(f"[red]parse error: {e}[/red]")
            return
        if not parts:
            return
        name, args = parts[0], parts[1:]
        if name == "help":
            ctx = click.Context(cli_module.main, info_name="ipod-sync")
            self.log_line(cli_module.main.get_help(ctx))
            return
        self._dispatch(name, args)

    def _dispatch(self, name: str, args: list[str]) -> None:
        cmd = cli_module.main.commands.get(name)
        if cmd is None:
            self.log_line(f"[red]unknown command: {name}[/red]")
            return
        try:
            ctx = cmd.make_context(name, list(args))
        except click.UsageError as e:
            self.log_line(f"[red]{e}[/red]")
            return
        handler = _COMMAND_MAP.get(name)
        if handler is None:
            self.log_line(f"[red]not available in the TUI: {name}[/red]")
            return
        if name not in _NO_SESSION_NEEDED and not self.session:
            self.log_line("Not ready yet — still connecting.")
            return
        if self._current_future and not self._current_future.done():
            self.log_line("[yellow]A command is still running — press x to cancel it first.[/yellow]")
            return
        self._launch_command(name, handler, ctx.params)

    @work(exclusive=True)
    async def _launch_command(self, name: str, handler, params: dict) -> None:
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
        self._open_input("command", "add ")

    def action_shortcut_sync_add(self) -> None:
        self._dispatch("sync-add", [])

    def action_shortcut_sync_hard(self) -> None:
        self._dispatch("sync-hard", [])

    def action_shortcut_eject(self) -> None:
        self._dispatch("eject", [])

    def action_cancel_worker(self) -> None:
        fut = self._current_future
        if fut and not fut.done():
            fut.cancel()
            self.log_line("Cancelling — finishes at the next safe point…")
        else:
            self.log_line("Nothing running.")

    def action_toggle_log(self) -> None:
        pane = self.query_one("#log-pane", RichLog)
        pane.toggle_class("-hidden")
        if not pane.has_class("-hidden"):
            pane.scroll_end(animate=False)

    # ── quit ─────────────────────────────────────────────────────────────────

    def action_request_quit(self) -> None:
        self._do_quit()

    @work(exclusive=True)
    async def _do_quit(self) -> None:
        if self.session and not self.session.ejected:
            if self._current_future and not self._current_future.done():
                self.log_line("[yellow]A command is still running — press x to cancel it before quitting.[/yellow]")
                return
            should_eject = await self.push_screen_wait(ConfirmModal("Eject iPod?", default=True))
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
    IpodSyncApp(ansi_color=True).run()
