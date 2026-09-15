import asyncio

import click
from rich.console import Console

from . import commands
from .session import Session, SessionError
from .ui import RichUI

console = Console()


async def _with_session(ui: RichUI, body) -> None:
    try:
        session = await Session.open(ui)
    except SessionError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    await body(session)


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Apple Music <-> iPod Classic sync.

    Run with no subcommand for the full-screen TUI, or use one of the
    subcommands below as a one-shot zsh command.
    """
    if ctx.invoked_subcommand is None:
        from .tui import run_tui

        run_tui()


@main.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
@click.option(
    "--youtube",
    is_flag=True,
    help="For a YouTube URL, download it directly instead of searching Apple Music.",
)
def add(query: tuple[str, ...], yes: bool, youtube: bool) -> None:
    """Add songs to the iPod: a text search, an Apple Music URL, or a YouTube URL."""

    async def run() -> None:
        ui = RichUI(console, yes=yes)
        await _with_session(ui, lambda session: commands.add(ui, session, list(query), yes=yes, youtube=youtube))

    asyncio.run(run())


@main.command("sync-add")
@click.option("-j", "--jobs", default=4, show_default=True, type=click.IntRange(1, 8), help="Parallel downloads.")
@click.option("--yes", is_flag=True, help="Don't ask for confirmation before deleting.")
def sync_add(jobs: int, yes: bool) -> None:
    """Add new library songs; delete only sync-flagged tracks gone from Apple Music."""

    async def run() -> None:
        ui = RichUI(console, yes=yes)
        await _with_session(ui, lambda session: commands.sync(ui, session, hard=False, jobs=jobs, yes=yes))

    asyncio.run(run())


@main.command("sync-hard")
@click.option("-j", "--jobs", default=4, show_default=True, type=click.IntRange(1, 8), help="Parallel downloads.")
@click.option("--yes", is_flag=True, help="Don't ask for confirmation before deleting.")
def sync_hard(jobs: int, yes: bool) -> None:
    """Mirror the library 1:1 + Favourites; delete every other playlist."""

    async def run() -> None:
        ui = RichUI(console, yes=yes)
        await _with_session(ui, lambda session: commands.sync(ui, session, hard=True, jobs=jobs, yes=yes))

    asyncio.run(run())


@main.command()
def eject() -> None:
    """Eject the iPod."""
    asyncio.run(commands.eject(RichUI(console)))


@main.command()
def login() -> None:
    """Force a fresh Apple Music browser login."""
    asyncio.run(commands.login(RichUI(console)))


@main.command()
def status() -> None:
    """Show whether an iPod is connected, and what's on it."""
    asyncio.run(commands.status(RichUI(console)))


@main.command("clean-orphans")
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
def clean_orphans(yes: bool) -> None:
    """Delete files on the iPod that its database doesn't reference."""
    asyncio.run(commands.clean_orphans(RichUI(console, yes=yes), yes=yes))


if __name__ == "__main__":
    main()
