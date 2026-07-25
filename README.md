# ipod-sync

One-way sync of your **Apple Music library** (songs + albums, no playlists) to an
**iPod Classic / Nano / Mini** on Linux.

Each run makes the iPod mirror your library:

1. Detects your iPod — or lists unmounted USB partitions and mounts the one you pick.
2. Signs in to Apple Music (first run opens a browser window to log in; cookies are cached).
3. Fetches every song in your library and compares against what's on the iPod.
4. Downloads only the missing tracks (staged in `tmp/`, deleted after copying).
5. Copies them to the iPod and removes tracks no longer in your library.
6. If everything succeeded, unmounts and powers off the iPod — safe to unplug.

## Requirements

- An active Apple Music subscription
- `libgpod` (`pacman -S libgpod`) plus `pkg-config` and a C compiler
- `ffmpeg` and `mp4decrypt` (AUR: `widevine-aur`) in PATH
- [uv](https://docs.astral.sh/uv/)

## Setup

```sh
uv sync
uv run playwright install chromium
```

## Usage

```sh
uv run ipod-sync            # full sync
uv run ipod-sync --dry-run  # show what would change, touch nothing
uv run ipod-sync --relogin  # force a fresh Apple Music browser login
```

State lives in `~/.apple-music-manager/` (cookies, tag-alias map, compiled
libgpod extension). If a run fails, the device is left mounted and partially
downloaded files are kept so the next run resumes where it left off.

## Credits

Apple Music API and download functionality powered by
[gamdl](https://github.com/glomatico/gamdl); iPod database access via
[libgpod](https://sourceforge.net/projects/gtkpod/).
