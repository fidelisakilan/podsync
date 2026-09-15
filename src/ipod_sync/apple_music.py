import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from gamdl.api import AppleMusicApi, ItunesApi
from gamdl.api.constants import AMP_API_URL
from gamdl.downloader import (
    AppleMusicBaseDownloader,
    AppleMusicDownloader,
    AppleMusicMusicVideoDownloader,
    AppleMusicSongDownloader,
    AppleMusicUploadedVideoDownloader,
    NotStreamable,
)
from gamdl.downloader.constants import (
    ALBUM_MEDIA_TYPE,
    ARTIST_MEDIA_TYPE,
    PLAYLIST_MEDIA_TYPE,
    SONG_MEDIA_TYPE,
)
from gamdl.interface import (
    AppleMusicInterface,
    AppleMusicMusicVideoInterface,
    AppleMusicSongInterface,
    AppleMusicUploadedVideoInterface,
)

from .auth import AuthError

async def _patched_get_token(self) -> str:
    response = await self.client.get("https://music.apple.com")
    home_page = response.text

    index_js_uri_match = re.search(
        r"/(assets/index-legacy[~-][^/\"]+\.js)",
        home_page,
    )
    if not index_js_uri_match:
        raise Exception("index.js URI not found in Apple Music homepage")
    index_js_uri = index_js_uri_match.group(1)

    response = await self.client.get(f"https://music.apple.com/{index_js_uri}")
    index_js_page = response.text

    token_match = re.search(
        r'"(eyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+)"',
        index_js_page,
    )
    if not token_match:
        raise Exception("Token not found in index.js page")
    return token_match.group(1)


AppleMusicApi._get_token = _patched_get_token


@dataclass(frozen=True)
class LibrarySong:
    catalog_id: str
    artist: str
    title: str
    album: str
    favourite: bool = False
    duration_ms: int = 0


async def create_api(cookies_path: Path) -> AppleMusicApi:
    try:
        api = await AppleMusicApi.create_from_netscape_cookies(
            cookies_path=str(cookies_path)
        )
    except Exception as e:
        raise AuthError(f"Authentication failed: {e}")
    if not api.active_subscription:
        raise AuthError("No active Apple Music subscription for this account.")
    return api


async def fetch_library_songs(
    api: AppleMusicApi, on_progress: Callable[[int, int], None], jobs: int = 8
) -> tuple[list[LibrarySong], int]:
    limit = 100
    params = {"limit": limit, "offset": 0, "extend": "inFavorites"}
    first = await api._amp_request("/v1/me/library/songs", params)
    total = first.get("meta", {}).get("total", 0)
    fetched = len(first.get("data", []))
    on_progress(fetched, total)

    semaphore = asyncio.Semaphore(jobs)

    async def fetch_page(offset: int) -> list[dict]:
        nonlocal fetched
        async with semaphore:
            for attempt in (1, 2):
                try:
                    resp = await api._amp_request(
                        "/v1/me/library/songs",
                        {"limit": limit, "offset": offset, "extend": "inFavorites"},
                    )
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(3)
            data = resp.get("data", [])
            fetched += len(data)
            on_progress(fetched, total)
            return data

    pages = [first.get("data", [])]
    pages += await asyncio.gather(
        *(fetch_page(offset) for offset in range(limit, total, limit))
    )

    songs: list[LibrarySong] = []
    seen: set[str] = set()
    skipped = 0
    for page in pages:
        for item in page:
            attr = item.get("attributes", {})
            catalog_id = attr.get("playParams", {}).get("catalogId")
            if not catalog_id:
                skipped += 1
                continue
            catalog_id = str(catalog_id)
            if catalog_id in seen:
                continue
            seen.add(catalog_id)
            songs.append(
                LibrarySong(
                    catalog_id=catalog_id,
                    artist=attr.get("artistName", ""),
                    title=attr.get("name", ""),
                    album=attr.get("albumName", ""),
                    favourite=bool(attr.get("inFavorites", False)),
                    duration_ms=int(attr.get("durationInMillis", 0) or 0),
                )
            )
    return songs, skipped


def build_downloader(
    api: AppleMusicApi, output_path: Path, temp_path: Path
) -> AppleMusicDownloader:
    itunes = ItunesApi(api.storefront, api.language)
    iface = AppleMusicInterface(api, itunes)
    base = AppleMusicBaseDownloader(
        output_path=str(output_path),
        temp_path=str(temp_path),
        overwrite=False,
        save_cover=False,
        silent=True,
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


class SongUnavailableError(Exception):
    pass


async def download_song(
    api: AppleMusicApi, dl: AppleMusicDownloader, catalog_id: str
) -> Path:
    try:
        resp = await api.get_song(catalog_id)
    except Exception as e:
        if "40400" in str(e) or "Resource Not Found" in str(e):
            raise SongUnavailableError("removed from Apple Music catalog")
        raise
    if not resp or not resp.get("data"):
        raise SongUnavailableError("not found in Apple Music catalog")
    try:
        item = await dl.get_single_download_item(resp["data"][0])
        if item.final_path and Path(item.final_path).exists():
            return Path(item.final_path)
        await dl.download(item)
    except NotStreamable:
        raise SongUnavailableError("not streamable")
    if not item.final_path or not Path(item.final_path).exists():
        raise RuntimeError("download produced no file")
    return Path(item.final_path)


async def search_songs(
    api: AppleMusicApi, term: str, limit: int = 5, offset: int = 0
) -> list[LibrarySong]:
    """Search the Apple Music catalog for songs matching `term`."""
    resp = await api.get_search_results(term, types="songs", limit=limit, offset=offset)
    items = resp.get("results", {}).get("songs", {}).get("data", [])
    songs = []
    for item in items:
        catalog_id = str(item.get("id", ""))
        if not catalog_id:
            continue
        attr = item.get("attributes", {})
        songs.append(
            LibrarySong(
                catalog_id=catalog_id,
                artist=attr.get("artistName", ""),
                title=attr.get("name", ""),
                album=attr.get("albumName", ""),
                duration_ms=int(attr.get("durationInMillis", 0) or 0),
            )
        )
    return songs


async def add_to_library(api: AppleMusicApi, ids: list[str], kind: str = "songs") -> None:
    """POST catalog ids into the signed-in user's Apple Music library.

    `kind` is "songs" or "albums". Expects HTTP 202; raises otherwise.
    """
    if not ids:
        return
    resp = await api.client.post(
        f"{AMP_API_URL}/v1/me/library",
        params={f"ids[{kind}]": ",".join(ids)},
        headers={"media-user-token": api.media_user_token or ""},
    )
    if resp.status_code != 202:
        raise RuntimeError(
            f"add to library failed ({resp.status_code}): {resp.text[:200]}"
        )


class UnsupportedUrlError(ValueError):
    pass


async def resolve_url(
    api: AppleMusicApi, dl: AppleMusicDownloader, url: str
) -> tuple[str, list[LibrarySong], str | None]:
    """Classify an Apple Music URL and fetch its songs.

    Returns (kind, songs, album_id) where kind is one of "song", "album",
    "playlist". Raises UnsupportedUrlError for artist/other URLs, or if
    nothing streamable is found.
    """
    url_info = dl.get_url_info(url)
    if url_info is None:
        raise UnsupportedUrlError("not an Apple Music URL")

    effective_type = "song" if url_info.sub_id else (url_info.type or url_info.library_type)
    if not effective_type:
        raise UnsupportedUrlError("could not determine URL type")
    if effective_type in ARTIST_MEDIA_TYPE:
        raise UnsupportedUrlError("artist URLs are not supported")
    if effective_type in SONG_MEDIA_TYPE:
        kind = "song"
    elif effective_type in ALBUM_MEDIA_TYPE:
        kind = "album"
    elif effective_type in PLAYLIST_MEDIA_TYPE:
        kind = "playlist"
    else:
        raise UnsupportedUrlError(f"unsupported URL type: {effective_type!r}")

    items = await dl.get_download_queue(url_info)
    if not items:
        raise UnsupportedUrlError("nothing found at that URL")

    songs: list[LibrarySong] = []
    for item in items:
        meta = item.media_metadata or {}
        if meta.get("type") not in SONG_MEDIA_TYPE:
            continue
        catalog_id = str(meta.get("id", ""))
        if not catalog_id:
            continue
        attr = meta.get("attributes", {})
        songs.append(
            LibrarySong(
                catalog_id=catalog_id,
                artist=attr.get("artistName", ""),
                title=attr.get("name", ""),
                album=attr.get("albumName", ""),
                duration_ms=int(attr.get("durationInMillis", 0) or 0),
            )
        )
    if not songs:
        raise UnsupportedUrlError("no downloadable songs found at that URL")

    album_id = (url_info.id or url_info.library_id) if kind == "album" else None
    return kind, songs, album_id
