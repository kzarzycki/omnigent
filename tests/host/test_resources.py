"""Parsing of the host resources report on Linux and macOS."""

from __future__ import annotations

import pytest

from omnigent.host.frames import HostResourcesFrame, decode_host_frame, encode_host_frame
from omnigent.host.resources import parse_meminfo, parse_vm_stat

_MEMINFO = """\
MemTotal:       16252928 kB
MemFree:          812344 kB
MemAvailable:    6291456 kB
Buffers:          120000 kB
HugePages_Total:       0
"""

# Trimmed real ``vm_stat`` output from an Apple Silicon Mac (16 KiB pages).
_VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               12800.
Pages active:                            400000.
Pages inactive:                           51200.
Pages speculative:                         3000.
Pages wired down:                         90000.
"""


def test_parse_meminfo_reads_total_and_available_in_mb() -> None:
    assert parse_meminfo(_MEMINFO) == (15872, 6144)


def test_parse_meminfo_without_mem_available_raises() -> None:
    with pytest.raises(ValueError, match="MemAvailable"):
        parse_meminfo("MemTotal:       16252928 kB\n")


def test_parse_vm_stat_counts_free_plus_inactive_pages() -> None:
    # (12800 + 51200) pages * 16384 bytes = 1000 MiB; active and speculative are excluded.
    assert parse_vm_stat(_VM_STAT) == 1000


def test_parse_vm_stat_without_page_size_raises() -> None:
    with pytest.raises(ValueError, match="page size"):
        parse_vm_stat("Pages free: 1.\nPages inactive: 2.\n")


def test_resources_frame_round_trip() -> None:
    original = HostResourcesFrame(
        load_1m=3.42, cpu_count=8, memory_total_mb=15872, memory_available_mb=6144
    )
    assert decode_host_frame(encode_host_frame(original)) == original


def test_resources_frame_rejects_non_numeric_load() -> None:
    with pytest.raises(ValueError, match="load_1m"):
        decode_host_frame(
            '{"kind": "host.resources", "load_1m": "high", "cpu_count": 8,'
            ' "memory_total_mb": 1, "memory_available_mb": 1}'
        )
