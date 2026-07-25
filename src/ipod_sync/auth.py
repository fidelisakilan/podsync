import shutil
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path

from rich.console import Console

from . import APP_DIR, PROJECT_ROOT

COOKIES_PATH = APP_DIR / "cookies.txt"


class AuthError(Exception):
    pass


def ensure_cookies(console: Console, relogin: bool = False) -> Path:
    if relogin:
        browser_login(console)
        return COOKIES_PATH
    if COOKIES_PATH.exists():
        return COOKIES_PATH
    legacy = PROJECT_ROOT / "cookies.txt"
    if legacy.exists():
        APP_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(legacy, COOKIES_PATH)
        console.print(f"Imported existing cookies from {legacy}")
        return COOKIES_PATH
    browser_login(console)
    return COOKIES_PATH


def browser_login(console: Console) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise AuthError(
            "playwright is not installed — run: uv sync && uv run playwright install chromium"
        )

    console.print("Opening a browser window — log in to Apple Music there…")
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=False)
            except Exception as e:
                raise AuthError(
                    f"Could not launch Chromium ({e}) — run: uv run playwright install chromium"
                )
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://music.apple.com")
            console.print("Waiting for login to complete…")
            while not any(c["name"] == "media-user-token" for c in context.cookies()):
                page.wait_for_timeout(2000)
            _write_netscape(context.cookies(), COOKIES_PATH)
            browser.close()
    except AuthError:
        raise
    except Exception:
        raise AuthError("Browser was closed before login completed.")
    console.print(f"[green]Logged in — cookies saved to {COOKIES_PATH}[/green]")


def _write_netscape(cookies: list[dict], path: Path) -> None:
    jar = MozillaCookieJar(str(path))
    for c in cookies:
        domain = c.get("domain", "")
        expires = int(c["expires"]) if c.get("expires", -1) > 0 else None
        jar.set_cookie(
            Cookie(
                version=0,
                name=c["name"],
                value=c["value"],
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=c.get("path", "/"),
                path_specified=True,
                secure=bool(c.get("secure")),
                expires=expires,
                discard=expires is None,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    jar.save(ignore_discard=True, ignore_expires=True)
