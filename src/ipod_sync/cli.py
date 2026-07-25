import asyncio
import shutil
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import STAGING_DIR
from . import apple_music, device
from .auth import AuthError, ensure_cookies
from .ipod_lib import IpodDatabase, ensure_gpod_available
from .sync import Aliases, Counters, clean_staging, diff, find_orphans, read_audio_meta, track_key

console = Console()


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    )


async def _network_phase(
    cookies: Path, ipod_keys: set, aliases, counters: Counters, dry_run: bool, jobs: int
):
    console.rule("[4/6] Apple Music library")
    with console.status("Authenticating with Apple Music…"):
        api = await apple_music.create_api(cookies)
    console.print(f"Signed in — storefront {api.storefront.upper()}")
    with _progress() as progress:
        task = progress.add_task("Fetching library songs", total=None)

        def on_progress(done: int, total: int):
            progress.update(task, completed=done, total=total or None)

        songs, skipped = await apple_music.fetch_library_songs(api, on_progress)
    counters.library = len(songs)
    counters.no_catalog = skipped

    missing, stale, known_unavailable = diff(songs, ipod_keys, aliases)
    counters.missing = len(missing)
    counters.stale = len(stale)
    counters.unavailable = known_unavailable
    console.print(
        f"{len(songs)} library songs · {len(ipod_keys)} on iPod · "
        f"[cyan]{len(missing)} to download[/cyan] · [magenta]{len(stale)} to remove[/magenta]"
        + (f" · [dim]{known_unavailable} known unavailable[/dim]" if known_unavailable else "")
    )

    downloaded = []
    if not dry_run:
        console.rule("[5/6] Download missing tracks")
        if missing:
            downloaded = await _download_missing(api, missing, aliases, counters, jobs)
        else:
            console.print("Nothing to download.")
    return missing, stale, downloaded


def _scan_staging() -> dict:
    staged = {}
    music = STAGING_DIR / "music"
    if music.exists():
        for path in list(music.rglob("*.m4a")) + list(music.rglob("*.mp4")):
            meta = read_audio_meta(path)
            if meta:
                staged[track_key(meta["artist"], meta["title"])] = path
    return staged


async def _download_missing(api, missing, aliases, counters: Counters, jobs: int) -> list[tuple]:
    dl = apple_music.build_downloader(api, STAGING_DIR / "music", STAGING_DIR)
    downloaded = []
    semaphore = asyncio.Semaphore(jobs)

    staged = _scan_staging()
    pending = []
    for song in missing:
        path = staged.get(aliases.key_for(song))
        if path:
            downloaded.append((song, path))
            counters.downloaded += 1
        else:
            pending.append(song)
    if downloaded:
        console.print(f"{len(downloaded)} tracks already staged — reusing without download")
    if not pending:
        return downloaded

    with _progress() as progress:
        task = progress.add_task(f"Downloading ({jobs} workers)", total=len(pending))

        async def download_one(song, failures):
            async with semaphore:
                progress.update(task, description=f"{song.artist} — {song.title}"[:60])
                try:
                    path = await apple_music.download_song(api, dl, song.catalog_id)
                    downloaded.append((song, path))
                    counters.downloaded += 1
                    progress.advance(task)
                except apple_music.SongUnavailableError as e:
                    counters.unavailable += 1
                    aliases.mark_unavailable(song)
                    progress.console.print(
                        f"  [dim]○ {song.artist} — {song.title}: {e}[/dim]"
                    )
                    progress.advance(task)
                except Exception as e:
                    failures.append((song, e))

        for round_no in (1, 2, 3):
            failures = []
            await asyncio.gather(*(download_one(s, failures) for s in pending))
            if not failures:
                break
            pending = [s for s, _ in failures]
            if round_no < 3:
                rate_limited = any("429" in str(e) for _, e in failures)
                wait = 30 if rate_limited else 5
                progress.update(
                    task,
                    description=f"{len(pending)} failed — retrying in {wait}s (round {round_no + 1})",
                )
                await asyncio.sleep(wait)
            else:
                for song, e in failures:
                    counters.download_failed += 1
                    progress.console.print(
                        f"  [red]✗ {song.artist} — {song.title}: {str(e) or type(e).__name__}[/red]"
                    )
                    progress.advance(task)
    return downloaded


def _write_ipod(mountpoint, stale, downloaded, aliases, counters: Counters) -> None:
    with IpodDatabase(mountpoint) as db:
        purged = 0
        for _name, pl in db.list_playlists():
            db.remove_playlist(pl)
            purged += 1
        if purged:
            console.print(f"Removed {purged} leftover playlists from the iPod")

        track_map = {
            track_key(artist, title): ptr
            for (artist, title), ptr in db.build_track_map().items()
        }

        if stale:
            with _progress() as progress:
                task = progress.add_task("Removing stale tracks", total=len(stale))
                for key in stale:
                    try:
                        db.remove_track(track_map[key])
                        counters.removed += 1
                    except Exception as e:
                        counters.remove_failed += 1
                        progress.console.print(f"  [red]✗ remove {key}: {e}[/red]")
                    progress.advance(task)

        added_paths: list = []
        if downloaded:
            with _progress() as progress:
                task = progress.add_task("Copying to iPod", total=len(downloaded))
                for song, path in downloaded:
                    progress.update(
                        task, description=f"→ {song.artist} — {song.title}"[:60]
                    )
                    try:
                        meta = read_audio_meta(path)
                        if not meta:
                            raise RuntimeError("could not read audio tags")
                        db.add_track(str(path), meta)
                        aliases.record(song, track_key(meta["artist"], meta["title"]))
                        counters.added += 1
                        added_paths.append(path)
                    except Exception as e:
                        counters.add_failed += 1
                        progress.console.print(
                            f"  [red]✗ {song.artist} — {song.title}: {e}[/red]"
                        )
                    progress.advance(task)

        try:
            with console.status("Saving iPod database (flushing copied files can take minutes)…"):
                db.fix_playlist_links()
                db.save()
        except Exception as e:
            counters.save_failed = True
            console.print(f"[red]Failed to write iTunesDB: {e}[/red]")
        else:
            console.print("iPod database saved.")
            for path in added_paths:
                path.unlink(missing_ok=True)


def _summary(counters: Counters) -> None:
    table = Table(title="Sync summary")
    table.add_column("What")
    table.add_column("Count", justify="right")
    rows = [
        ("Library songs", counters.library),
        ("No catalog id (skipped)", counters.no_catalog),
        ("Missing on iPod", counters.missing),
        ("Stale on iPod", counters.stale),
        ("Downloaded", counters.downloaded),
        ("Unavailable in catalog (skipped)", counters.unavailable),
        ("Download failed", counters.download_failed),
        ("Added to iPod", counters.added),
        ("Add failed", counters.add_failed),
        ("Removed from iPod", counters.removed),
        ("Remove failed", counters.remove_failed),
    ]
    for label, value in rows:
        style = "red" if "failed" in label.lower() and value else ""
        table.add_row(label, str(value), style=style or None)
    console.print(table)


@click.command()
@click.option("--dry-run", is_flag=True, help="Show what would change, touch nothing.")
@click.option("--relogin", is_flag=True, help="Force a fresh browser login.")
@click.option("-j", "--jobs", default=4, show_default=True, type=click.IntRange(1, 8), help="Parallel downloads.")
@click.option("--clean-orphans", is_flag=True, help="Delete files on the iPod that its database doesn't reference.")
def main(dry_run: bool, relogin: bool, jobs: int, clean_orphans: bool) -> None:
    """Sync your Apple Music library (songs + albums) to an iPod Classic."""
    console.rule("[1/6] Preflight")
    if not ensure_gpod_available():
        console.print("[red]libgpod unavailable — install it (pacman -S libgpod)[/red]")
        sys.exit(1)
    if not shutil.which("ffmpeg"):
        console.print("[yellow]Warning: ffmpeg not found in PATH[/yellow]")

    console.rule("[2/6] Device")
    mountpoint = device.prompt_and_mount(console)
    if not mountpoint:
        console.print("No iPod — nothing to do.")
        sys.exit(1)
    dev = device.device_for_mount(mountpoint)
    console.print(f"iPod at {mountpoint} ({dev})")

    console.rule("[3/6] Read iPod")
    aliases = Aliases.load()
    with IpodDatabase(mountpoint) as db:
        console.print(f"Device: {db.device_name}")
        ipod_keys = {track_key(a, t) for (a, t) in db.build_track_map().keys()}
        if clean_orphans:
            with console.status("Scanning for orphaned files…"):
                orphans, orphan_bytes = find_orphans(db, mountpoint)
    console.print(f"{len(ipod_keys)} tracks on iPod")
    if clean_orphans:
        if not orphans:
            console.print("No orphaned files found.")
        elif dry_run:
            console.print(
                f"[cyan]{len(orphans)} orphaned files ({orphan_bytes / 1e9:.1f} GB) would be deleted[/cyan]"
            )
        else:
            with _progress() as progress:
                task = progress.add_task("Deleting orphaned files", total=len(orphans))
                for f in orphans:
                    f.unlink(missing_ok=True)
                    progress.advance(task)
            console.print(
                f"Deleted {len(orphans)} orphaned files — freed {orphan_bytes / 1e9:.1f} GB"
            )

    counters = Counters()
    try:
        cookies = ensure_cookies(console, relogin=relogin)
        try:
            missing, stale, downloaded = asyncio.run(
                _network_phase(cookies, ipod_keys, aliases, counters, dry_run, jobs)
            )
        except AuthError as e:
            console.print(f"[yellow]{e}[/yellow]")
            cookies = ensure_cookies(console, relogin=True)
            counters = Counters()
            missing, stale, downloaded = asyncio.run(
                _network_phase(cookies, ipod_keys, aliases, counters, dry_run, jobs)
            )
    except AuthError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Aborting — library fetch incomplete, iPod untouched: {e}[/red]")
        sys.exit(1)

    if dry_run:
        for s in missing:
            console.print(f"  [cyan]+ {s.artist} — {s.title}[/cyan]")
        for a, t in sorted(stale):
            console.print(f"  [magenta]- {a} — {t}[/magenta]")
        console.print("Dry run — nothing changed, device left mounted.")
        return

    console.rule("[6/6] Write iPod")
    _write_ipod(mountpoint, stale, downloaded, aliases, counters)
    if not downloaded and not stale:
        console.print("Tracks already in sync.")

    _summary(counters)

    if counters.total_errors == 0:
        clean_staging()
        with console.status("Ejecting — flushing remaining data to the iPod, this can take a few minutes…"):
            ejected = dev and device.eject(dev, console)
        if ejected:
            console.print("[green]Done — iPod ejected, safe to unplug.[/green]")
        else:
            console.print("[yellow]Sync OK but eject failed — unmount manually.[/yellow]")
    else:
        console.print(
            f"[red]{counters.total_errors} errors — device left mounted; "
            "staged files kept for retry on next run.[/red]"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
