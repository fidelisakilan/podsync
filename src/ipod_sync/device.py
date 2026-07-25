import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table


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


def prompt_and_mount(console: Console) -> str | None:
    while True:
        mountpoint = find_ipod_mount()
        if mountpoint:
            return mountpoint

        candidates = list_candidates()
        if not candidates:
            console.print("No iPod mounted and no unmounted USB partitions found.")
            choice = Prompt.ask(
                "Connect your iPod, then \\[r]escan or \\[q]uit",
                choices=["r", "q"],
                default="r",
            )
            if choice == "q":
                return None
            continue

        table = Table(title="Unmounted USB partitions")
        table.add_column("#")
        table.add_column("Device")
        table.add_column("Label")
        table.add_column("Size")
        table.add_column("FS")
        for i, c in enumerate(candidates, 1):
            table.add_row(str(i), c.path, c.label, c.size_gb, c.fstype)
        console.print(table)

        choices = [str(i) for i in range(1, len(candidates) + 1)] + ["r", "q"]
        choice = Prompt.ask(
            "Mount which device? (\\[r]escan / \\[q]uit)", choices=choices, default="1"
        )
        if choice == "q":
            return None
        if choice == "r":
            continue

        dev = candidates[int(choice) - 1].path
        try:
            mountpoint = mount(dev)
        except RuntimeError as e:
            console.print(f"[red]Mount failed: {e}[/red]")
            continue
        if (Path(mountpoint) / "iPod_Control").exists():
            console.print(f"Mounted {dev} at {mountpoint}")
            return mountpoint
        console.print(
            f"[yellow]{dev} mounted at {mountpoint} but has no iPod_Control/ — not an iPod.[/yellow]"
        )
