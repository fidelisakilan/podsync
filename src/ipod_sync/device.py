import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console


def find_ipod_mount() -> str | None:
    user = os.environ.get("USER", "")
    for base in [Path(f"/run/media/{user}"), Path("/media"), Path("/mnt")]:
        if not base.exists():
            continue
        try:
            for entry in base.iterdir():
                if (entry / "iPod_Control").exists():
                    return str(entry)
        except PermissionError:
            continue
    return None


@dataclass
class Candidate:
    path: str
    label: str
    size: int
    fstype: str

    @property
    def size_gb(self) -> str:
        return f"{self.size / 1e9:.1f} GB"


def list_candidates() -> list[Candidate]:
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,PATH,LABEL,SIZE,TRAN,MOUNTPOINT,TYPE,FSTYPE"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return []
    candidates = []
    for disk in json.loads(out.stdout).get("blockdevices", []):
        if disk.get("tran") != "usb":
            continue
        for part in disk.get("children", []) or []:
            size = int(part.get("size") or 0)
            if part.get("type") == "part" and not part.get("mountpoint") and size > 100e6:
                candidates.append(
                    Candidate(
                        path=part["path"],
                        label=part.get("label") or "",
                        size=size,
                        fstype=part.get("fstype") or "",
                    )
                )
    return sorted(candidates, key=lambda c: c.size, reverse=True)


def mount(dev: str) -> str:
    out = subprocess.run(
        ["udisksctl", "mount", "-b", dev], capture_output=True, text=True
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or out.stdout.strip())
    m = re.search(r" at (.+?)\.?\s*$", out.stdout)
    if not m:
        raise RuntimeError(f"could not parse mountpoint from: {out.stdout.strip()}")
    return m.group(1)


def device_for_mount(mountpoint: str) -> str | None:
    out = subprocess.run(
        ["findmnt", "-no", "SOURCE", mountpoint], capture_output=True, text=True
    )
    return out.stdout.strip() or None


def is_read_only(mountpoint: str) -> bool:
    return bool(os.statvfs(mountpoint).f_flag & os.ST_RDONLY)


def print_read_only_hint(mountpoint: str, console: Console) -> None:
    dev = device_for_mount(mountpoint) or "the device"
    console.print(f"[red]{mountpoint} is mounted read-only.[/red]")
    console.print(
        "Journaled HFS+ volumes mount read-only on Linux unless forced:\n"
        f"  udisksctl unmount -b {dev}\n"
        "  sudo mkdir -p /mnt/ipod\n"
        f"  sudo mount -t hfsplus -o rw,force,uid=$(id -u),gid=$(id -g) {dev} /mnt/ipod\n"
        "Then rerun ipod-sync.\n"
        "To avoid this every time, disable HFS+ journaling on the iPod from a Mac:\n"
        "  diskutil disableJournal /Volumes/iPod"
    )


def eject(dev: str, console: Console) -> bool:
    os.sync()
    out = subprocess.run(
        ["udisksctl", "unmount", "-b", dev], capture_output=True, text=True
    )
    if out.returncode != 0:
        console.print(f"[red]Unmount failed: {out.stderr.strip()}[/red]")
        return False
    out = subprocess.run(
        ["udisksctl", "power-off", "-b", dev], capture_output=True, text=True
    )
    if out.returncode != 0:
        console.print(f"[yellow]Power-off failed: {out.stderr.strip()}[/yellow]")
        return False
    return True
