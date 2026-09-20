# ipod-sync

Sync your **Apple Music library** to an **iPod Classic / Nano / Mini** on Linux —
either from a full-screen terminal app, or as one-shot zsh commands.

Running `ipod-sync` with no arguments opens the iPod, signs in to Apple Music
once, and drops you into a full-screen TUI showing what's already on the
device. Every action is also available as a plain subcommand
(`ipod-sync sync-add`, `ipod-sync add "some song"`, …) for scripting or a
quick one-off change.

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
| `:` | open the command line (`shlex`-split, same commands as the CLI) |
| `a` | opens a search dialog: type a query, `Enter` to search, `j`/`k` (or the arrow keys) to browse results — more load automatically as you near the bottom — `Enter` on a result to add that song. `Esc` first clears the box back to a fresh search, `Esc` again cancels. The log opens automatically so you can watch it download, and closes itself when the run is clean — it stays open if anything failed, so the error doesn't vanish before you can read it |
| `s` / `S` | run `sync-add` / `sync-hard` |
| `e` | eject |
| `d` | remove the highlighted track from the iPod (confirms first) — never touches your Apple Music library |
| `x` | cancel the running command |
| `` ` `` | open the log in a dialog (`Esc`/`q` to close; the status line shows the current activity, count and elapsed time) |
| `q` or `:q` | quit — asks "Eject iPod?" (default no) |

Every `:` command below also works one-shot from zsh.

### One-shot commands

```sh
uv run ipod-sync add "sparkle"                       # search Apple Music, pick from a list
uv run ipod-sync add "https://music.apple.com/…"     # song / album / playlist URL
uv run ipod-sync add "https://youtu.be/…"             # search Apple Music using the video's title
uv run ipod-sync add --youtube "https://youtu.be/…"   # download straight from YouTube instead
uv run ipod-sync sync-add                             # add new library songs; safe, additive
uv run ipod-sync sync-hard                            # mirror the library 1:1 + Favourites
uv run ipod-sync eject
uv run ipod-sync login                                # force a fresh Apple Music browser login
uv run ipod-sync status                               # is an iPod connected, what's on it
uv run ipod-sync clean-orphans                        # delete files iTunesDB doesn't reference
```

Add `--yes` to `add`, `sync-add`, `sync-hard`, or `clean-orphans` to skip the
confirmation prompt that appears whenever a command would delete something.
A one-shot command leaves the iPod mounted afterwards — run `ipod-sync eject`
when you're done (syncs no longer auto-eject).

### `add`

In the TUI, `a` opens a dedicated search dialog (see the key table above) for
the common case: type a song query, `Enter` on a result adds it. `:add` and
the one-shot `ipod-sync add` subcommand instead take one or more text queries
or URLs:

- **Text** → a paged pick list (5 at a time, multi-select with `Space`,
  `Enter` confirms the selection, `n`/`p` to page). The chosen songs are
  added to your Apple Music library, downloaded, and copied to the iPod.
- **Apple Music song URL** → added to your library directly, no picker.
- **Apple Music album URL** → the whole album is added to your library and
  pushed to the iPod.
- **Apple Music playlist URL** → pushed to the iPod only — your library is
  left untouched.
- **Apple Music artist URL** → rejected (too broad).
- **YouTube video URL** → probes the video, builds an `artist title` query
  from its tags, and runs the same Apple Music pick list as a text search
  (nothing is downloaded from YouTube).
- **YouTube video URL with `--youtube`** → downloads audio straight from
  YouTube instead (no Apple Music Premium assumed, so typically Opus
  ≤160 kbps or AAC 128 kbps), transcodes once to AAC 256 kbps, shows you the
  source format and derived tags, and asks for confirmation before adding it
  to the iPod. Not added to your Apple Music library.
- **YouTube playlist URL** → rejected.

A song already on the iPod (matched by artist/title) is skipped entirely —
no library change, no re-download. `add` never deletes anything and never
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

Both modes preview what would be deleted and ask for confirmation unless
`--yes` is passed.

State lives in `~/.apple-music-manager/` (cookies, tag-alias map, compiled
libgpod extension). If a run fails partway, the device is left mounted and
partially downloaded files are kept in `cache/` so the next run resumes
where it left off instead of re-downloading.

## Credits

Apple Music API and download functionality powered by
[gamdl](https://github.com/glomatico/gamdl); iPod database access via
[libgpod](https://sourceforge.net/projects/gtkpod/).
