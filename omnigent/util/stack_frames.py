"""Walk a thread's frames into bounded location strings, never reading locals or source."""

from __future__ import annotations

from collections.abc import Callable
from types import CodeType, FrameType


def walk_frames(
    frame: FrameType | None,
    *,
    max_frames: int,
    render: Callable[[CodeType, int], str],
    byte_budget: int | None = None,
) -> tuple[list[str], bool]:
    """Render frames innermost first until a frame or byte limit is reached.

    :param frame: Innermost frame to start from, or ``None`` for no stack.
    :param max_frames: Most frames to render.
    :param render: Builds one frame's text from its code object and line number.
    :param byte_budget: Most UTF-8 bytes the rendered frames may total, or
        ``None`` for no byte limit.
    :returns: The rendered frames, and whether older frames were left out.
    """
    rendered: list[str] = []
    remaining_bytes = byte_budget
    try:
        while frame is not None and len(rendered) < max_frames:
            text = render(frame.f_code, frame.f_lineno)
            if remaining_bytes is not None:
                text_bytes = len(text.encode("utf-8", errors="replace"))
                if text_bytes > remaining_bytes:
                    break
                remaining_bytes -= text_bytes
            rendered.append(text)
            frame = frame.f_back
        return rendered, frame is not None
    finally:
        del frame
