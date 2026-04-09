"""
ipod_lib.py — libgpod wrapper via cffi API mode.

Builds _gpod_cffi.so on first call to ensure_gpod_available().
Requires libgpod to be installed (pacman -S libgpod).
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

_BUILD_DIR = Path.home() / ".apple-music-manager" / "gpod_build"

# ── embedded C helpers ────────────────────────────────────────────────────────

_C_SOURCE = r"""
#include <gpod-1.0/gpod/itdb.h>
#include <sys/statvfs.h>
#include <string.h>
#include <stdio.h>

static char _last_err[4096];

const char* gpod_last_error(void)  { return _last_err; }
void        gpod_clear_error(void) { _last_err[0] = 0; }

Itdb_iTunesDB* gpod_open(const char *mountpoint) {
    GError *e = NULL;
    Itdb_iTunesDB *db = itdb_parse(mountpoint, &e);
    if (e) {
        snprintf(_last_err, sizeof _last_err, "%s", e->message);
        g_error_free(e);
        return NULL;
    }
    _last_err[0] = 0;
    return db;
}

int gpod_save(Itdb_iTunesDB *db) {
    GError *e = NULL;
    gboolean ok = itdb_write(db, &e);
    if (e) {
        snprintf(_last_err, sizeof _last_err, "%s", e->message);
        g_error_free(e);
        return 0;
    }
    return (int)ok;
}

long long gpod_free_bytes(const char *mp) {
    struct statvfs st;
    return statvfs(mp, &st) == 0
        ? (long long)st.f_bavail * (long long)st.f_frsize
        : -1LL;
}

const char* gpod_device_name(Itdb_iTunesDB *db) {
    Itdb_Playlist *mpl = itdb_playlist_mpl(db);
    return (mpl && mpl->name) ? mpl->name : "";
}

int gpod_track_count(Itdb_iTunesDB *db) {
    return (int)g_list_length(db->tracks);
}

Itdb_Track* gpod_track_at(Itdb_iTunesDB *db, int idx) {
    GList *l = g_list_nth(db->tracks, (guint)idx);
    return l ? (Itdb_Track *)l->data : NULL;
}

int gpod_has_track(Itdb_iTunesDB *db, const char *artist, const char *title) {
    for (GList *l = db->tracks; l; l = l->next) {
        Itdb_Track *t = (Itdb_Track *)l->data;
        if (t->artist && t->title &&
            strcmp(t->artist, artist) == 0 &&
            strcmp(t->title,  title)  == 0)
            return 1;
    }
    return 0;
}

Itdb_Track* gpod_find_track(Itdb_iTunesDB *db, const char *artist, const char *title) {
    for (GList *l = db->tracks; l; l = l->next) {
        Itdb_Track *t = (Itdb_Track *)l->data;
        if (t->artist && t->title &&
            strcmp(t->artist, artist) == 0 &&
            strcmp(t->title,  title)  == 0)
            return t;
    }
    return NULL;
}

/* Add a track: copies src file to iPod and registers in iTunesDB.
   Track is also added to the master playlist automatically.
   Returns the new Itdb_Track* or NULL on failure (check gpod_last_error). */
Itdb_Track* gpod_add_track(Itdb_iTunesDB *db, const char *src,
                            const char *title,       const char *artist,
                            const char *album,       const char *genre,
                            const char *composer,    const char *albumartist,
                            int size, int tracklen, int track_nr,
                            int bitrate, int samplerate, int year) {
    Itdb_Track *t = itdb_track_new();
    if (!t) {
        snprintf(_last_err, sizeof _last_err, "itdb_track_new() returned NULL");
        return NULL;
    }

    t->title       = g_strdup(title       ? title       : "");
    t->artist      = g_strdup(artist      ? artist      : "");
    t->album       = g_strdup(album       ? album       : "");
    t->genre       = g_strdup(genre       ? genre       : "");
    t->composer    = g_strdup(composer    ? composer    : "");
    t->albumartist = g_strdup(albumartist ? albumartist : "");
    t->size        = (guint32)size;
    t->tracklen    = tracklen;
    t->track_nr    = track_nr;
    t->bitrate     = bitrate;
    t->samplerate  = (guint16)samplerate;
    t->year        = year;

    /* Must add to DB before copying (so track knows its itdb/device). */
    itdb_track_add(db, t, -1);

    Itdb_Playlist *mpl = itdb_playlist_mpl(db);
    if (mpl) itdb_playlist_add_track(mpl, t, -1);

    GError *e = NULL;
    if (!itdb_cp_track_to_ipod(t, src, &e)) {
        snprintf(_last_err, sizeof _last_err, "cp_track_to_ipod failed: %s",
                 e ? e->message : "unknown error");
        if (e) g_error_free(e);
        return NULL;
    }
    _last_err[0] = 0;
    return t;
}

/* Get or create a named playlist (non-smart). */
Itdb_Playlist* gpod_ensure_playlist(Itdb_iTunesDB *db, const char *name) {
    Itdb_Playlist *pl = itdb_playlist_by_name(db, (gchar *)name);
    if (!pl) {
        pl = itdb_playlist_new(name, FALSE);
        itdb_playlist_add(db, pl, -1);
    }
    return pl;
}

void gpod_playlist_add_track(Itdb_Playlist *pl, Itdb_Track *track) {
    if (!itdb_playlist_contains_track(pl, track))
        itdb_playlist_add_track(pl, track, -1);
}

/* Remove all tracks from a playlist (does not delete the tracks from the iPod). */
void gpod_playlist_clear(Itdb_Playlist *pl) {
    while (pl->members)
        itdb_playlist_remove_track(pl, (Itdb_Track *)pl->members->data);
}

/* Replace invalid UTF-8 bytes with '?'; always returns a valid UTF-8 heap string. */
static char* _sanitize_utf8(char *s) {
    if (!s) return g_strdup("");
    if (g_utf8_validate(s, -1, NULL)) return s;
    GString *out = g_string_new(NULL);
    const char *p = s;
    const char *inv;
    while (*p) {
        if (g_utf8_validate(p, -1, &inv)) {
            g_string_append(out, p);
            break;
        }
        if (inv > p)
            g_string_append_len(out, p, inv - p);
        g_string_append_c(out, '?');
        p = inv + 1;
    }
    g_free(s);
    return g_string_free(out, FALSE);
}

/* Sanitize all track and playlist strings in the database to valid UTF-8. */
void gpod_sanitize_strings(Itdb_iTunesDB *db) {
    for (GList *l = db->tracks; l; l = l->next) {
        Itdb_Track *t = (Itdb_Track *)l->data;
        t->title       = _sanitize_utf8(t->title);
        t->artist      = _sanitize_utf8(t->artist);
        t->album       = _sanitize_utf8(t->album);
        t->genre       = _sanitize_utf8(t->genre);
        t->composer    = _sanitize_utf8(t->composer);
        t->albumartist = _sanitize_utf8(t->albumartist);
    }
    for (GList *l = db->playlists; l; l = l->next) {
        Itdb_Playlist *pl = (Itdb_Playlist *)l->data;
        if (pl->name)
            pl->name = _sanitize_utf8(pl->name);
    }
}

/* Remove broken playlist members: track->itdb != db OR not in master playlist. */
void gpod_fix_playlist_links(Itdb_iTunesDB *db) {
    Itdb_Playlist *mpl = itdb_playlist_mpl(db);
    for (GList *l = db->playlists; l; l = l->next) {
        Itdb_Playlist *pl = (Itdb_Playlist *)l->data;
        GList *m = pl->members;
        while (m) {
            GList *next = m->next;
            Itdb_Track *t = (Itdb_Track *)m->data;
            gboolean bad = (!t || t->itdb != db ||
                            (mpl && !itdb_playlist_contains_track(mpl, t)));
            if (bad) {
                pl->members = g_list_remove(pl->members, t);
                if (pl->num > 0) pl->num--;
            }
            m = next;
        }
    }
}

/* Delete a track from the iPod: removes the file, unregisters from all playlists and DB. */
void gpod_remove_track(Itdb_iTunesDB *db, Itdb_Track *track) {
    char *path = itdb_filename_on_ipod(track);
    if (path) {
        remove(path);
        g_free(path);
    }
    itdb_track_remove(track);
}

const char* gpod_track_title(Itdb_Track *t)  { return t->title  ? t->title  : ""; }
const char* gpod_track_artist(Itdb_Track *t) { return t->artist ? t->artist : ""; }
const char* gpod_track_album(Itdb_Track *t)  { return t->album  ? t->album  : ""; }
"""

_CDEF = """
typedef struct _Itdb_iTunesDB Itdb_iTunesDB;
typedef struct _Itdb_Track    Itdb_Track;
typedef struct _Itdb_Playlist Itdb_Playlist;

const char*    gpod_last_error(void);
void           gpod_clear_error(void);
Itdb_iTunesDB* gpod_open(const char *mountpoint);
int            gpod_save(Itdb_iTunesDB *db);
void           itdb_free(Itdb_iTunesDB *db);
long long      gpod_free_bytes(const char *mp);
const char*    gpod_device_name(Itdb_iTunesDB *db);
int            gpod_track_count(Itdb_iTunesDB *db);
Itdb_Track*    gpod_track_at(Itdb_iTunesDB *db, int idx);
int            gpod_has_track(Itdb_iTunesDB *db, const char *artist, const char *title);
Itdb_Track*    gpod_find_track(Itdb_iTunesDB *db, const char *artist, const char *title);
Itdb_Track*    gpod_add_track(Itdb_iTunesDB *db, const char *src,
                               const char *title,    const char *artist,
                               const char *album,    const char *genre,
                               const char *composer, const char *albumartist,
                               int size, int tracklen, int track_nr,
                               int bitrate, int samplerate, int year);
Itdb_Playlist* gpod_ensure_playlist(Itdb_iTunesDB *db, const char *name);
void           gpod_playlist_add_track(Itdb_Playlist *pl, Itdb_Track *track);
void           gpod_playlist_clear(Itdb_Playlist *pl);
void           gpod_sanitize_strings(Itdb_iTunesDB *db);
void           gpod_fix_playlist_links(Itdb_iTunesDB *db);
void           gpod_remove_track(Itdb_iTunesDB *db, Itdb_Track *track);
const char*    gpod_track_title(Itdb_Track *t);
const char*    gpod_track_artist(Itdb_Track *t);
const char*    gpod_track_album(Itdb_Track *t);
"""

# ── module-level state ────────────────────────────────────────────────────────

ffi = None
lib = None
_GPOD_AVAILABLE = False
_GPOD_ERROR = ""


def _get_pkg_config():
    cflags = shlex.split(subprocess.check_output(
        ["pkg-config", "--cflags", "libgpod-1.0"],
        text=True, stderr=subprocess.DEVNULL,
    ).strip())
    libs_raw = shlex.split(subprocess.check_output(
        ["pkg-config", "--libs", "libgpod-1.0"],
        text=True, stderr=subprocess.DEVNULL,
    ).strip())
    libraries = [a[2:] for a in libs_raw if a.startswith("-l")]
    lib_dirs  = [a[2:] for a in libs_raw if a.startswith("-L")]
    return cflags, libraries, lib_dirs


def ensure_gpod_available() -> bool:
    """Build (if needed) and load the _gpod_cffi extension.  Returns True on success."""
    global ffi, lib, _GPOD_AVAILABLE, _GPOD_ERROR

    if _GPOD_AVAILABLE:
        return True

    # Ensure build dir is on sys.path before trying to import.
    if str(_BUILD_DIR) not in sys.path:
        sys.path.insert(0, str(_BUILD_DIR))

    # Fast path: already compiled from a previous run.
    try:
        import _gpod_cffi as _m
        ffi = _m.ffi
        lib = _m.lib
        _GPOD_AVAILABLE = True
        return True
    except ImportError:
        pass

    # Compile now (first run).
    print("Building libgpod extension (first time only)…", flush=True)
    try:
        from cffi import FFI as _FFI
        _ffi = _FFI()
        cflags, libraries, lib_dirs = _get_pkg_config()
        _ffi.set_source(
            "_gpod_cffi",
            _C_SOURCE,
            libraries=libraries,
            library_dirs=lib_dirs,
            extra_compile_args=cflags,
        )
        _ffi.cdef(_CDEF)
        _BUILD_DIR.mkdir(parents=True, exist_ok=True)
        _ffi.compile(tmpdir=str(_BUILD_DIR), verbose=False)
        print("Build complete.", flush=True)
    except Exception as e:
        _GPOD_ERROR = str(e)
        print(f"Build failed: {e}", flush=True)
        return False

    try:
        import _gpod_cffi as _m
        ffi = _m.ffi
        lib = _m.lib
        _GPOD_AVAILABLE = True
        return True
    except ImportError as e:
        _GPOD_ERROR = str(e)
        return False


# ── high-level Python class ───────────────────────────────────────────────────

class IpodDatabase:
    """Context manager for an iPod's iTunesDB."""

    def __init__(self, mountpoint: str):
        self.mountpoint = mountpoint
        self._db = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        if not _GPOD_AVAILABLE:
            raise RuntimeError(f"libgpod not available: {_GPOD_ERROR}")
        db = lib.gpod_open(self.mountpoint.encode())
        if db == ffi.NULL:
            err = ffi.string(lib.gpod_last_error()).decode(errors="replace")
            raise RuntimeError(f"Failed to open iPod database at {self.mountpoint!r}: {err}")
        self._db = db
        lib.gpod_sanitize_strings(self._db)

    def save(self) -> None:
        if not lib.gpod_save(self._db):
            err = ffi.string(lib.gpod_last_error()).decode(errors="replace")
            raise RuntimeError(f"Failed to write iTunesDB: {err}")

    def close(self) -> None:
        if self._db:
            lib.itdb_free(self._db)
            self._db = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def device_name(self) -> str:
        return ffi.string(lib.gpod_device_name(self._db)).decode(errors="replace")

    @property
    def free_bytes(self) -> int:
        return lib.gpod_free_bytes(self.mountpoint.encode())

    # ── track operations ──────────────────────────────────────────────────────

    def track_exists(self, artist: str, title: str) -> bool:
        return bool(lib.gpod_has_track(
            self._db,
            artist.encode("utf-8"),
            title.encode("utf-8"),
        ))

    def find_track(self, artist: str, title: str):
        t = lib.gpod_find_track(
            self._db,
            artist.encode("utf-8"),
            title.encode("utf-8"),
        )
        return None if t == ffi.NULL else t

    def add_track(self, src_file: str, meta: dict):
        """Copy src_file to iPod and register it.  Returns track pointer or raises."""
        t = lib.gpod_add_track(
            self._db,
            src_file.encode("utf-8"),
            (meta.get("title")       or "").encode("utf-8"),
            (meta.get("artist")      or "").encode("utf-8"),
            (meta.get("album")       or "").encode("utf-8"),
            (meta.get("genre")       or "").encode("utf-8"),
            (meta.get("composer")    or "").encode("utf-8"),
            (meta.get("albumartist") or "").encode("utf-8"),
            int(meta.get("size",       0)),
            int(meta.get("tracklen",   0)),
            int(meta.get("track_nr",   0)),
            int(meta.get("bitrate",    0)),
            int(meta.get("samplerate", 44100)),
            int(meta.get("year",       0)),
        )
        if t == ffi.NULL:
            err = ffi.string(lib.gpod_last_error()).decode(errors="replace")
            raise RuntimeError(err)
        return t

    # ── playlist operations ───────────────────────────────────────────────────

    def ensure_playlist(self, name: str):
        pl = lib.gpod_ensure_playlist(self._db, name.encode("utf-8"))
        if pl == ffi.NULL:
            raise RuntimeError(f"Failed to create playlist {name!r}")
        return pl

    def add_track_to_playlist(self, track, playlist) -> None:
        lib.gpod_playlist_add_track(playlist, track)

    def clear_playlist(self, playlist) -> None:
        lib.gpod_playlist_clear(playlist)

    def remove_track(self, track) -> None:
        lib.gpod_remove_track(self._db, track)

    def fix_playlist_links(self) -> None:
        lib.gpod_fix_playlist_links(self._db)

    # ── bulk helpers ──────────────────────────────────────────────────────────

    def build_track_map(self) -> dict[tuple[str, str], object]:
        """Return {(artist, title): track_ptr} for all existing iPod tracks."""
        result = {}
        n = lib.gpod_track_count(self._db)
        for i in range(n):
            t = lib.gpod_track_at(self._db, i)
            if t == ffi.NULL:
                continue
            artist = ffi.string(lib.gpod_track_artist(t)).decode(errors="replace")
            title  = ffi.string(lib.gpod_track_title(t)).decode(errors="replace")
            result[(artist, title)] = t
        return result
