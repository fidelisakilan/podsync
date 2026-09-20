# ipod-sync

Sync your **Apple Music library** to an **iPod Classic / Nano / Mini** on Linux —
from a full-screen terminal app.

Running `ipod-sync` opens the iPod, signs in to Apple Music once, and drops
you into a full-screen TUI showing what's already on the device.

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

### The TUI

```sh
uv run ipod-sync
```

Opens the iPod, signs in, and shows a full-width table of the **iPod's
actual contents** (not the Apple Music library — that's instant), with
Artist / Album / Song columns, sorted by artist, then album, then song
title. Manually `add`ed tracks (as opposed to ones a sync brought in, see
below) are shown in cyan.

Keys:

| Key | Action |
|---|---|
| `j` / `k` | move down / up |
| `gg` / `G` | jump to top / bottom |
| `Ctrl-f` / `Ctrl-b` | page down / up |
| `Esc` | open a floating chooser for the playlist to view (All tracks / Favourites / Recently Added / …) — `j`/`k` and `Enter` to pick, `Esc` to cancel |
| `/` | filter the track table — `Esc` while filtering clears it and goes back to the full list |
| `a` | opens a search dialog: type a query, `Enter` to search, `j`/`k` (or the arrow keys) to browse results — more load automatically as you near the bottom — `Enter` on a result to add that song. `Esc` first clears the box back to a fresh search, `Esc` again cancels. The log opens automatically so you can watch it download, and closes itself when the run is clean — it stays open if anything failed, so the error doesn't vanish before you can read it |
| `s` / `S` | run `sync-add` / `sync-hard` |
| `e` | eject |
| `d` | remove the highlighted track from the iPod (confirms first) — never touches your Apple Music library |
| `x` | cancel the running command |
| `` ` `` | open the log in a dialog (`Esc`/`q` to close; the status line shows the current activity, count and elapsed time) |
| `q` | quit — asks "Eject iPod?" (default no) |

### `add`

In the TUI, `a` opens a dedicated search dialog (see the key table above) for
the common case: type a song query, `Enter` to search, move through the
Apple Music results, and press `Enter` on a result to add that song. The
chosen song is added to your Apple Music library, downloaded, and copied to
the iPod.

A song already on the iPod (matched by artist/title) is skipped entirely —
no library change, no re-download. Adding never deletes anything and never
ejects.

### Sync modes and origins

Every track added to the iPod is tagged in `iPod_Control/ipod-sync.json` as
either `sync` (put there by a sync) or `add` (put there by `add`); a track
with no entry defaults to `sync`.

- **`sync-add`** — adds any new library songs. Deletes only `sync`-tagged
  tracks that are gone from your Apple Music library; a track you added by
  hand (`add`-tagged) is never touched, even if it later disappears from
  Apple Music. Rebuilds the `Favourites` playlist to exactly mirror your
  hearted songs, and resets `Recently Added` to this run's additions (left
  alone if nothing was added).
- **`sync-hard`** — mirrors your library 1:1: deletes every unwanted track
  regardless of origin, removes every playlist except `Favourites`, and
  restamps everything left as `sync`.

Both modes preview what would be deleted and ask for confirmation.

State lives in `~/.apple-music-manager/` (cookies, tag-alias map, compiled
libgpod extension). If a run fails partway, the device is left mounted and
partially downloaded files are kept in `cache/` so the next run resumes
where it left off instead of re-downloading.

## Credits

Apple Music API and download functionality powered by
[gamdl](https://github.com/glomatico/gamdl); iPod database access via
[libgpod](https://sourceforge.net/projects/gtkpod/).
