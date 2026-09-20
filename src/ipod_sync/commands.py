"""commands.py — async operations run by the ipod-sync TUI.

Each function takes a `UI` plus whatever else it needs, so the TUI can run the
same code path from key bindings and dialogs.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Sequence

from . import APP_DIR, CACHE_DIR
from . import apple_music, device
from .apple_music import LibrarySong
from .ipod_lib import IpodDatabase
from .session import Session
from .sync import (
    FAVOURITES_PLAYLIST,
    RECENT_PLAYLIST,
    clean_staging,
    plan_sync,
    prune_empty_dirs,
    read_audio_meta,
    track_key,
)
from .ui import UI, console_for

__all__ = ["add_songs", "remove_track", "sync", "eject"]


def _find_ipod_path(db: IpodDatabase, track) -> str:
    """The ipod_path of a just-added track (tracks() re-scans the whole DB;
    fine for the handful of tracks a single command touches)."""
    for ti in db.tracks():
        if ti.ptr == track:
            return ti.ipod_path
    return ""


CACHE_INDEX_PATH = APP_DIR / "cache-index.json"


def _scan_cache(ui: UI | None = None) -> dict[tuple[str, str], Path]:
    """Map (artist, title) → cached file for every track in cache/.

    Reading tags from thousands of files takes minutes, so the result is
    memoised in cache-index.json keyed by path with (mtime, size); only new
    or changed files are opened on later runs.
    """
    cached: dict[tuple[str, str], Path] = {}
    if not CACHE_DIR.exists():
        return cached
    try:
        index: dict = json.loads(CACHE_INDEX_PATH.read_text())
    except Exception:
        index = {}

    files = list(CACHE_DIR.rglob("*.m4a")) + list(CACHE_DIR.rglob("*.mp4"))
    fresh: dict = {}
    dirty = False
    for i, path in enumerate(files, 1):
        try:
            st = path.stat()
        except OSError:
            continue
        key = str(path)
        entry = index.get(key)
        if not entry or entry[0] != st.st_mtime_ns or entry[1] != st.st_size:
            meta = read_audio_meta(path)
            entry = [st.st_mtime_ns, st.st_size, meta.get("artist", ""), meta.get("title", "")] if meta else None
            dirty = True
            if ui and (i % 200 == 0 or i == len(files)):
                ui.progress("Scanning cache", i, len(files))
        if entry:
            fresh[key] = entry
            cached[track_key(entry[2], entry[3])] = path
    if dirty or len(fresh) != len(index):
        try:
            CACHE_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_INDEX_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(fresh))
            tmp.replace(CACHE_INDEX_PATH)
        except OSError:
            pass
    return cached


async def _download_songs(
    ui: UI,
    session: Session,
    songs: list[LibrarySong],
    scan_cache: bool,
    jobs: int = 4,
) -> list[tuple[LibrarySong, Path]]:
    """Shared retry-loop downloader used by add and sync flows."""
    if not songs:
        return []
    downloaded: list[tuple[LibrarySong, Path]] = []
    pending = list(songs)

    if scan_cache:
        cached = _scan_cache(ui)
        still_pending = []
        for song in pending:
            path = cached.get(session.aliases.key_for(song))
            if path:
                downloaded.append((song, path))
            else:
                still_pending.append(song)
        pending = still_pending
        if downloaded:
            ui.print(f"{len(downloaded)} tracks already cached — reusing without download")

    if not pending:
        return downloaded

    semaphore = asyncio.Semaphore(jobs)
    total = len(pending)
    done = 0

    async def download_one(song: LibrarySong, failures: list) -> None:
        nonlocal done
        async with semaphore:
            try:
                path = await apple_music.download_song(
                    session.api, session.downloader, song.catalog_id
                )
                downloaded.append((song, path))
            except apple_music.SongUnavailableError as e:
                session.aliases.mark_unavailable(song)
                ui.song_line("○", song.artist, song.title, str(e))
            except Exception as e:
                failures.append((song, e))
            finally:
                done += 1
                ui.progress("Downloading", done, total)

    for round_no in (1, 2, 3):
        failures: list = []
        await asyncio.gather(*(download_one(s, failures) for s in pending))
        if not failures:
            break
        pending = [s for s, _ in failures]
        if round_no < 3:
            rate_limited = any("429" in str(e) for _, e in failures)
            wait = 30 if rate_limited else 5
            ui.print(f"{len(pending)} failed — retrying in {wait}s (round {round_no + 1})")
            await asyncio.sleep(wait)
        else:
            for song, e in failures:
                ui.song_line("✗", song.artist, song.title, str(e) or type(e).__name__)
    return downloaded


# ── add ──────────────────────────────────────────────────────────────────────


def _dedupe_key(song: LibrarySong):
    return song.catalog_id or ("yt", track_key(song.artist, song.title))


async def add_songs(ui: UI, session: Session, songs: Sequence[LibrarySong]) -> None:
    """Add already-resolved library songs straight to the iPod, skipping
    query resolution — used by the TUI's `a` search-and-add dialog."""
    deduped = {_dedupe_key(s): s for s in songs}
    await _commit_songs(ui, session, list(deduped.values()))


async def _commit_songs(ui: UI, session: Session, songs: Sequence[LibrarySong]) -> None:
    if not songs:
        ui.print("Nothing to add.")
        return

    with session.db() as db:
        ipod_keys = {track_key(t.artist, t.title) for t in db.tracks()}
        wanted: list[LibrarySong] = []
        for song in songs:
            if session.aliases.key_for(song) in ipod_keys:
                ui.song_line("=", song.artist, song.title, "already on iPod")
            else:
                wanted.append(song)

        if not wanted:
            ui.print("Nothing new — everything is already on the iPod.")
            return

        song_ids = sorted({song.catalog_id for song in wanted if song.catalog_id})
        if song_ids:
            try:
                await apple_music.add_to_library(session.api, song_ids, "songs")
            except Exception as e:
                ui.print(f"[warning] could not add songs to library: {e}")

        downloaded = await _download_songs(ui, session, wanted, scan_cache=False)
        downloaded_map = {song: path for song, path in downloaded}

        recent_pl = db.ensure_playlist(RECENT_PLAYLIST)
        added_any = False
        for song in wanted:
            path = downloaded_map.get(song)
            if path is None:
                continue  # download failed or never available; already reported
            try:
                meta = read_audio_meta(path)
                if not meta:
                    raise RuntimeError("could not read audio tags")
                track = db.add_track(str(path), meta)
                if song.catalog_id:
                    session.aliases.record(song, track_key(meta["artist"], meta["title"]))
                ipod_path = _find_ipod_path(db, track)
                session.origins.set(ipod_path, "add")
                db.add_track_to_playlist(track, recent_pl)
                added_any = True
                ui.song_line("✓", song.artist, song.title, "added")
            except Exception as e:
                ui.song_line("✗", song.artist, song.title, str(e))

        if added_any:
            db.fix_playlist_links()
            db.save()
            session.origins.save()
        prune_empty_dirs(session.mountpoint)

    if ui.mount_reminder:
        ui.print("iPod left mounted — use e when done.")


async def remove_track(ui: UI, session: Session, ipod_path: str) -> None:
    """Remove one track from the iPod (file + iTunesDB entry). Never touches
    the Apple Music library — used by the TUI's song-column `d` key."""
    with session.db() as db:
        match = next((t for t in db.tracks() if t.ipod_path == ipod_path), None)
        if match is None:
            ui.print("✗ track no longer on the iPod")
            return
        db.remove_track(match.ptr)
        session.origins.drop(match.ipod_path)
        db.fix_playlist_links()
        db.save()
    session.origins.save()
    prune_empty_dirs(session.mountpoint)
    ui.song_line("✓", match.artist, match.title, "removed from iPod")


# ── sync (sync-add / sync-hard) ─────────────────────────────────────────────


async def sync(
    ui: UI,
    session: Session,
    hard: bool = False,
    jobs: int = 4,
    yes: bool = False,
) -> None:
    """`sync-add` (hard=False) or `sync-hard` (hard=True)."""
    ui.rule("Apple Music library")
    songs, skipped = await apple_music.fetch_library_songs(
        session.api, lambda done, total: ui.progress("Fetching library", done, total)
    )
    ui.print(
        f"{len(songs)} library songs"
        + (f" ({skipped} without a catalog id, skipped)" if skipped else "")
    )

    with session.db() as db:
        repaired, repair_errors = db.repair_layout()
        if repaired:
            db.save()
            prune_empty_dirs(session.mountpoint)
            ui.print(f"Repaired {repaired} tracks back into iPod_Control/Music")
        for err in repair_errors:
            ui.print(f"✗ repair {err}")

        ipod_tracks = db.tracks()
        plan = plan_sync(songs, ipod_tracks, session.aliases, session.origins, hard)

        drop_playlists = []
        if hard:
            drop_playlists = [
                name for name, _pl in db.list_playlists() if name != FAVOURITES_PLAYLIST
            ]

        ui.print(
            f"{len(plan.missing)} to download · {len(plan.to_delete)} to remove"
            + (f" · {plan.known_unavailable} known unavailable" if plan.known_unavailable else "")
        )
        if hard and drop_playlists:
            ui.print(f"Playlists to remove (sync-hard): {', '.join(drop_playlists)}")

        if (plan.to_delete or drop_playlists) and not yes:
            msg = f"Delete {len(plan.to_delete)} track(s) from the iPod"
            if drop_playlists:
                msg += f" and {len(drop_playlists)} playlist(s)"
            msg += "?"
            if not await ui.confirm(msg, default=False):
                ui.print("Aborted — nothing changed.")
                return

        downloaded = await _download_songs(ui, session, plan.missing, scan_cache=True, jobs=jobs)

        removed = remove_failed = 0
        for t in plan.to_delete:
            try:
                db.remove_track(t.ptr)
                session.origins.drop(t.ipod_path)
                removed += 1
            except Exception as e:
                remove_failed += 1
                ui.print(f"✗ remove {t.artist} — {t.title}: {e}")

        added = add_failed = 0
        added_tracks = []
        for song, path in downloaded:
            try:
                meta = read_audio_meta(path)
                if not meta:
                    raise RuntimeError("could not read audio tags")
                track = db.add_track(str(path), meta)
                session.aliases.record(song, track_key(meta["artist"], meta["title"]))
                ipod_path = _find_ipod_path(db, track)
                session.origins.set(ipod_path, "sync")
                added_tracks.append(track)
                added += 1
                ui.song_line("✓", song.artist, song.title, "added")
            except Exception as e:
                add_failed += 1
                ui.song_line("✗", song.artist, song.title, str(e))

        # Favourites: exact mirror of library songs with inFavorites=true.
        fav_pl = db.ensure_playlist(FAVOURITES_PLAYLIST)
        db.clear_playlist(fav_pl)
        current_tracks = db.tracks()
        by_key: dict[tuple[str, str], object] = {}
        for t in current_tracks:
            by_key[track_key(t.artist, t.title)] = t.ptr
        fav_keys = {track_key(s.artist, s.title) for s in songs if s.favourite}
        fav_added = 0
        for key in fav_keys:
            ptr = by_key.get(key)
            if ptr is not None:
                db.add_track_to_playlist(ptr, fav_pl)
                fav_added += 1
        ui.print(f"Favourites: {fav_added}/{len(fav_keys)} matched on iPod")

        if hard:
            for name, pl in db.list_playlists():
                if name != FAVOURITES_PLAYLIST:
                    db.remove_playlist(pl)
            session.origins.restamp_all([t.ipod_path for t in db.tracks()])
        elif added_tracks:
            recent_pl = db.ensure_playlist(RECENT_PLAYLIST)
            db.clear_playlist(recent_pl)
            for track in added_tracks:
                db.add_track_to_playlist(track, recent_pl)

        try:
            db.fix_playlist_links()
            db.save()
        except Exception as e:
            ui.print(f"✗ Failed to write iTunesDB: {e}")
            return

    session.origins.save()
    prune_empty_dirs(session.mountpoint)

    total_errors = remove_failed + add_failed
    ui.print(
        f"Summary — downloaded {len(downloaded)}, added {added} (failed {add_failed}), "
        f"removed {removed} (failed {remove_failed})"
    )
    if total_errors == 0:
        clean_staging()
    if ui.mount_reminder:
        ui.print("iPod left mounted — use e when done.")


# ── eject ───────────────────────────────────────────────────────────────────


async def eject(ui: UI, session: Session | None = None) -> bool:
    mountpoint = session.mountpoint if session else device.find_ipod_mount()
    if not mountpoint:
        ui.print("No iPod connected.")
        return False
    dev = session.dev if session else device.device_for_mount(mountpoint)
    if not dev:
        ui.print("Could not determine the block device for this mountpoint.")
        return False
    ok = device.eject(dev, console_for(ui))
    if ok:
        if session:
            session.ejected = True
        ui.print("iPod ejected — safe to unplug.")
    else:
        ui.print("Eject failed — unmount manually.")
    return ok
