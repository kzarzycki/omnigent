"""Sample every thread's stack for a fixed window and fold the samples into text.

Records code locations and function names only, never locals or source lines.
Nothing runs until :func:`sample_stacks` is called.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import Counter

from omnigent.util.stack_frames import walk_frames

SAMPLE_INTERVAL_S = 0.01
_MAX_SAMPLED_FRAMES = 128
_PACKAGE_MARKERS = ("site-packages" + os.sep, "omnigent" + os.sep)


def _short_path(filename: str) -> str:
    """Trim a code path to the part below site-packages or the omnigent package."""
    for marker in _PACKAGE_MARKERS:
        index = filename.rfind(marker)
        if index != -1:
            return filename[index + (len(marker) if marker.startswith("site") else 0) :]
    return os.path.basename(filename)


def sample_stacks(duration_s: float, *, interval_s: float = SAMPLE_INTERVAL_S) -> str:
    """Sample all other threads' stacks for *duration_s* and return the folded report.

    Blocks the calling thread for the window; the sampler runs on its own thread
    and leaves itself out of the samples.

    :param duration_s: Window length in seconds, e.g. ``30``.
    :param interval_s: Time between sampling rounds, e.g. ``0.01``.
    :returns: Collapsed stacks (root first, ``file:function`` joined by ``;``, then
        the sample count, most frequent first), the sample total per thread name,
        and the sampling interval actually achieved.
    """
    stacks: Counter[str] = Counter()
    per_thread: Counter[str] = Counter()
    rounds = 0
    elapsed_s = 0.0

    def sample() -> None:
        nonlocal rounds, elapsed_s
        own_ident = threading.get_ident()
        started = time.monotonic()
        next_round = started
        while (now := time.monotonic()) - started < duration_s:
            names = {thread.ident: thread.name for thread in threading.enumerate()}
            for ident, frame in sys._current_frames().items():
                if ident == own_ident:
                    continue
                locations, _ = walk_frames(
                    frame,
                    max_frames=_MAX_SAMPLED_FRAMES,
                    render=lambda code, _lineno: f"{_short_path(code.co_filename)}:{code.co_name}",
                )
                del frame
                stacks[";".join(reversed(locations))] += 1
                per_thread[names.get(ident, f"thread-{ident}")] += 1
            rounds += 1
            next_round += interval_s
            time.sleep(max(0.0, next_round - time.monotonic()))
        elapsed_s = now - started

    sampler = threading.Thread(target=sample, name="omnigent-stack-sampler", daemon=True)
    sampler.start()
    sampler.join()

    achieved_s = elapsed_s / rounds if rounds else 0.0
    lines = [f"{stack} {count}" for stack, count in stacks.most_common()]
    lines += ["", "# samples per thread"]
    lines += [f"{name} {count}" for name, count in per_thread.most_common()]
    lines += [
        "",
        f"# interval requested {interval_s * 1000:.1f} ms, achieved {achieved_s * 1000:.1f} ms "
        f"over {rounds} rounds in {elapsed_s:.1f} s",
    ]
    return "\n".join(lines) + "\n"
