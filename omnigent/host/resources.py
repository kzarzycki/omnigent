"""Sample this machine's load and memory for the host resources report."""

from __future__ import annotations

import os
import re
import subprocess
import sys

from omnigent.host.frames import HostResourcesFrame

_BYTES_PER_MB = 1024 * 1024
_KB_PER_MB = 1024
_VM_STAT_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_PAGES = re.compile(r"^(Pages free|Pages inactive):\s+(\d+)\.?\s*$", re.MULTILINE)


def parse_meminfo(text: str) -> tuple[int, int]:
    """Return ``(total_mb, available_mb)`` from Linux ``/proc/meminfo`` text.

    :param text: Contents of ``/proc/meminfo``, e.g. ``"MemTotal: 16000000 kB\\n..."``.
    :raises ValueError: If ``MemTotal`` or ``MemAvailable`` is missing.
    """
    fields: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[name.strip()] = int(parts[0])
    try:
        return fields["MemTotal"] // _KB_PER_MB, fields["MemAvailable"] // _KB_PER_MB
    except KeyError as exc:
        raise ValueError(f"/proc/meminfo has no {exc.args[0]} line") from exc


def parse_vm_stat(text: str) -> int:
    """Return available MB (free plus inactive pages) from macOS ``vm_stat`` output.

    :param text: ``vm_stat`` stdout, whose header names the page size.
    :raises ValueError: If the page size or either page count is missing.
    """
    page_size = _VM_STAT_PAGE_SIZE.search(text)
    pages = dict(_VM_STAT_PAGES.findall(text))
    if page_size is None or len(pages) != 2:
        raise ValueError("vm_stat output lacks page size, free or inactive pages")
    available_pages = int(pages["Pages free"]) + int(pages["Pages inactive"])
    return available_pages * int(page_size.group(1)) // _BYTES_PER_MB


def _command_output(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=10).stdout


def _memory_mb() -> tuple[int, int] | None:
    if sys.platform.startswith("linux"):
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            return parse_meminfo(meminfo.read())
    if sys.platform == "darwin":
        total = int(_command_output(["sysctl", "-n", "hw.memsize"]))
        return total // _BYTES_PER_MB, parse_vm_stat(_command_output(["vm_stat"]))
    return None


def sample_host_resources() -> HostResourcesFrame | None:
    """Sample load, CPU count and memory; ``None`` on a platform without them.

    Blocking (reads ``/proc`` or runs ``vm_stat``), so call it off the event loop.
    """
    # ponytail: Linux and macOS only; Windows has no load average to report.
    memory = _memory_mb()
    cpu_count = os.cpu_count()
    if memory is None or cpu_count is None:
        return None
    return HostResourcesFrame(
        load_1m=os.getloadavg()[0],
        cpu_count=cpu_count,
        memory_total_mb=memory[0],
        memory_available_mb=memory[1],
    )
