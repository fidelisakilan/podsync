# podsync

Sync your entire Apple Music library to an iPod Classic, Nano, or Mini from the terminal.

## Screenshots

![Library view](screenshots/library.png)
![Playlist view](screenshots/playlist.png)

## Setup

1. Clone the repository

```
git clone https://github.com/youruser/podsync
cd podsync
```

2. Install system dependencies (Arch Linux)

```
sudo pacman -S libgpod ffmpeg
yay -S widevine-aur
```

3. Install Python dependencies (requires Python 3.13 and [uv](https://github.com/astral-sh/uv))

```
uv sync
```

4. Get your Apple Music cookies. Install the [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) Chrome extension, open music.apple.com while signed in, and export cookies in Netscape format.

5. Place the exported file as `cookies.txt` in the project folder, or pass its path via `--cookies`.

6. Mount your iPod by opening your file manager (Nautilus, Thunar, etc.) and clicking the iPod in the sidebar.

7. Run the app

```
uv run python ipod_sync.py
```

## Options

```
--cookies PATH    Path to cookies.txt (default: ./cookies.txt)
--overwrite       Re-download albums that already exist locally
```

## Usage

The app loads your full library on startup. Press `s` to start a sync. The app downloads any missing albums, then copies new tracks to the iPod and recreates your playlists on the device.

Navigation:

| Key | Action |
|-----|--------|
| Tab | Switch between playlists and track panes |
| j / k | Move cursor down / up |
| Enter | Focus track pane |
| Escape / Backspace | Return to playlist pane |
| g g | Jump to top |
| G | Jump to bottom |
| Ctrl+f / Ctrl+b | Page down / up |
| s | Start sync |
| x | Stop sync |
| / | Open log viewer |
| q | Quit |

The iPod connection status and storage usage are shown in the bottom right. The device must be mounted before starting a sync.

## Notes

Downloaded music is stored in the path configured in `~/.gamdl/config.ini` under `output_path`. Defaults to `./Apple Music`.

A download cache is kept at `~/.apple-music-manager/cache.json` to skip already-completed albums on subsequent runs.

## Credits

Apple Music API and download functionality powered by [gamdl](https://github.com/glomatico/gamdl).

## System dependencies

| Package | Purpose |
|---------|---------|
| libgpod | Read and write iPod iTunesDB |
| ffmpeg | Audio processing |
| mp4decrypt (widevine-aur) | Decrypt downloaded tracks |

## Python dependencies

| Package | Purpose |
|---------|---------|
| gamdl | Apple Music API and downloader |
| textual | Terminal UI framework |
| mutagen | Read audio file metadata |
| cffi | C bindings for libgpod |
