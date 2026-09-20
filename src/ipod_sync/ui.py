"""ui.py — UI abstraction used by commands.py inside the full-screen Textual app."""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading

from typing import Any, Awaitable, Callable, Protocol, Sequence

from rich.console import Console
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label

# A pick row pairs the display columns (already formatted strings) with the
# underlying object that gets returned when the row is selected.
PickRow = tuple[tuple[str, ...], Any]
FetchPage = Callable[[int], Awaitable[list[PickRow]]]

_STATUS_STYLE = {
    "✓": "green",
    "=": "dim",
    "✗": "red",
    "+": "cyan",
    "-": "magenta",
    "○": "yellow",
}


def _song_text(status: str, artist: str, title: str, detail: str) -> str:
    text = f"{status} {artist} — {title}"
    if detail:
        text += f"  ({detail})"
    return text


def console_for(ui: "UI") -> Console:
    """Best-effort Console for functions (device.py, auth.py) that print
    directly."""
    return getattr(ui, "console", None) or Console()


class UI(Protocol):
    def print(self, text: str) -> None: ...

    def rule(self, title: str) -> None: ...

    def progress(self, desc: str, done: int, total: int) -> None: ...

    def song_line(self, status: str, artist: str, title: str, detail: str = "") -> None: ...

    async def pick(
        self, title: str, header: Sequence[str], fetch_page: FetchPage
    ) -> list[Any]: ...

    async def confirm(self, msg: str, default: bool = False) -> bool: ...


# ── Textual modal screens (generic — reused by tui.py) ─────────────────────


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("enter", "default", "Default"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "No"),
    ]

    def __init__(self, message: str, default: bool = True):
        super().__init__()
        self._message = message
        self._default = default

    def compose(self):
        with Vertical(id="confirm-box"):
            yield Label(self._message, id="confirm-message")
            yield Label(
                "[bold]y[/bold]es / [bold]n[/bold]o  (Enter = yes)"
                if self._default
                else "[bold]y[/bold]es / [bold]n[/bold]o  (Enter = no)",
                id="confirm-hint",
            )

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_default(self) -> None:
        self.dismiss(self._default)

    def action_no(self) -> None:
        self.dismiss(False)


class PickModal(ModalScreen[list]):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("space", "toggle", "Toggle"),
        Binding("enter", "confirm", "Confirm"),
        Binding("n", "next_page", "Next"),
        Binding("p", "prev_page", "Prev"),
        Binding("escape", "skip", "Skip"),
    ]

    def __init__(self, title: str, header: Sequence[str], fetch_page: FetchPage):
        super().__init__()
        self._title = title
        self._header = list(header)
        self._fetch_page = fetch_page
        self._page = 0
        self._cache: dict[int, list[PickRow]] = {}
        self._selected: dict[int, set[int]] = {}

    def compose(self):
        with Vertical(id="pick-box"):
            yield Label(self._title, id="pick-title")
            yield DataTable(id="pick-table", cursor_type="row")
            yield Label("", id="pick-hint")

    async def on_mount(self) -> None:
        table = self.query_one("#pick-table", DataTable)
        table.add_column(" ")
        for h in self._header:
            table.add_column(h)
        await self._goto(0)

    async def _goto(self, page: int) -> None:
        if page not in self._cache:
            self._cache[page] = list(await self._fetch_page(page))
        if not self._cache[page] and page > 0:
            self._cache.pop(page)
            return
        self._page = page
        self._render_rows()
        self.query_one("#pick-hint", Label).update(
            f"page {page + 1} — space select · enter confirm · n/p page · esc skip"
        )

    def _render_rows(self) -> None:
        table = self.query_one("#pick-table", DataTable)
        table.clear()
        rows = self._cache.get(self._page, [])
        sel = self._selected.get(self._page, set())
        if not rows:
            table.add_row("", "no results", "", "")
            return
        for i, (cols, _item) in enumerate(rows):
            mark = "*" if i in sel else " "
            table.add_row(mark, *cols)

    def action_cursor_down(self) -> None:
        self.query_one("#pick-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#pick-table", DataTable).action_cursor_up()

    def action_toggle(self) -> None:
        table = self.query_one("#pick-table", DataTable)
        row = table.cursor_row
        rows = self._cache.get(self._page, [])
        if row is None or row >= len(rows):
            return
        sel = self._selected.setdefault(self._page, set())
        if row in sel:
            sel.remove(row)
        else:
            sel.add(row)
        self._render_rows()

    def action_next_page(self) -> None:
        self.run_worker(self._goto(self._page + 1), exclusive=True)

    def action_prev_page(self) -> None:
        if self._page > 0:
            self.run_worker(self._goto(self._page - 1), exclusive=True)

    def action_confirm(self) -> None:
        picked = []
        for page, idxs in self._selected.items():
            rows = self._cache.get(page, [])
            for i in sorted(idxs):
                if i < len(rows):
                    picked.append(rows[i][1])
        self.dismiss(picked)

    def action_skip(self) -> None:
        self.dismiss([])


class TextualUI:
    """Full-screen Textual UI adapter.

    Commands run on a *background* thread (see tui.CommandLoop) so that no
    blocking call — tag scans, file copies to the iPod, iTunesDB saves,
    ffmpeg — can ever stall the screen. Every method here therefore marshals
    onto the app thread with `call_from_thread`; `pick`/`confirm` push a modal
    on the app thread and await its result back on the command loop.

    `app` must implement `log_line(text)` and `set_progress(desc, done, total)`
    (see tui.IpodSyncApp).
    """

    mount_reminder = False

    def __init__(self, app):
        self.app = app
        self._app_thread = threading.get_ident()

    # ── marshalling ──────────────────────────────────────────────────────────

    def _call(self, fn, *args):
        if threading.get_ident() == self._app_thread:
            return fn(*args)
        return self.app.call_from_thread(fn, *args)

    async def _modal(self, screen) -> Any:
        """Push `screen` on the app thread; resolve with its dismiss value."""
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def _push() -> None:
            self.app.push_screen(screen, callback=fut.set_result)

        self._call(_push)
        return await asyncio.wrap_future(fut)

    # ── UI protocol ──────────────────────────────────────────────────────────

    def print(self, text: str) -> None:
        self._call(self.app.log_line, text)

    def rule(self, title: str) -> None:
        self._call(self.app.log_line, f"── {title} " + "─" * max(0, 40 - len(title)))

    def progress(self, desc: str, done: int, total: int) -> None:
        self._call(self.app.set_progress, desc, done, total)

    def song_line(self, status: str, artist: str, title: str, detail: str = "") -> None:
        self._call(self.app.log_line, _song_text(status, artist, title, detail))

    async def pick(
        self, title: str, header: Sequence[str], fetch_page: FetchPage
    ) -> list[Any]:
        # The modal awaits fetch_page on the app loop, but the Apple Music
        # client lives on the command loop — bridge each page request across.
        cmd_loop = asyncio.get_running_loop()

        async def bridged(page: int):
            cf = asyncio.run_coroutine_threadsafe(fetch_page(page), cmd_loop)
            return await asyncio.wrap_future(cf)

        return await self._modal(PickModal(title, header, bridged))

    async def confirm(self, msg: str, default: bool = False) -> bool:
        return await self._modal(ConfirmModal(msg, default))
