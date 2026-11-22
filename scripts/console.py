"""Shared console helpers for standalone scripts (stdlib only).

Adds ``py-orchestrator`` to ``sys.path`` and re-exports the terminal toolkit so
scripts never need third-party UI dependencies.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_ORCH = ROOT / "py-orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

from orchestrator.ui import (  # noqa: E402,F401
    ACCENT,
    AMBER,
    BORDER,
    FAINT,
    GREEN,
    INK,
    MUTED,
    RED,
    SKY,
    VIOLET,
    WHITE,
    Frame,
    Screen,
    badge,
    dot,
    ensure_utf8,
    gradient,
    glyph,
    human_int,
    human_secs,
    log,
    meter,
    paint,
    rule,
    short_addr,
    strip_ansi,
    term_size,
    to_ascii,
    usd,
    vlen,
)

__all__ = [
    "ROOT", "ACCENT", "AMBER", "BORDER", "FAINT", "GREEN", "INK", "MUTED",
    "RED", "SKY", "VIOLET", "WHITE", "Frame", "Screen", "badge", "dot",
    "ensure_utf8", "gradient", "glyph", "human_int", "human_secs", "log",
    "meter", "paint", "rule", "short_addr", "strip_ansi", "term_size",
    "to_ascii", "usd", "vlen",
    "heading", "status", "panel", "kv", "width",
]

_MARKS = {
    "ok": (glyph("check"), GREEN),
    "info": (glyph("arrow"), SKY),
    "warn": (glyph("warn"), AMBER),
    "error": (glyph("cross"), RED),
    "skip": ("-", FAINT),
}


def width(default: int = 84) -> int:
    cols, _ = term_size()
    return max(64, min(cols - 1, default))


def heading(title: str, subtitle: str = "", accent: str = ACCENT) -> None:
    """Wordmark-style header used by one-shot scripts."""
    print()
    print(
        "  "
        + gradient(title.upper(), [accent, SKY, VIOLET])
        + (("  " + paint(glyph("sep"), FAINT) + "  " + paint(subtitle, MUTED)) if subtitle else "")
    )
    print("  " + paint(glyph("h") * (width() - 2), BORDER))


def status(
    level: str,
    label: str,
    detail: str = "",
    seconds: float = None,
    label_width: int = 14,
) -> None:
    """Aligned single-line status, mirroring the launcher's step lines."""
    mark, color = _MARKS.get(level, _MARKS["info"])
    body = f"  {paint(mark, color)} {paint(pad_label(label, label_width), INK, bold=True)}"
    if detail:
        body += " " + paint(str(detail), MUTED if level != "error" else RED)
    if seconds is not None:
        body += " " + paint(f"{seconds:.1f}s", FAINT)
    print(body, flush=True)


def pad_label(text: str, size: int) -> str:
    from orchestrator.ui import pad

    return pad(text, size)


def kv(key: str, value: str, key_width: int = 16) -> None:
    """Indented key/value line for provisioning reports."""
    print(
        "    "
        + paint(pad_label(key, key_width), MUTED)
        + paint(str(value), INK),
        flush=True,
    )


def panel(title: str, rows, accent: str = ACCENT) -> None:
    """Compact boxed summary; ``rows`` is an iterable of (key, value) pairs."""
    frame = Frame(width(), title=title, accent=accent)
    for key, value in rows:
        frame.pair(paint(str(key), MUTED), str(value))
    print("\n".join(frame.close()))
    print()


class Timer:
    """Tiny context timer returning elapsed seconds."""

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.elapsed = time.perf_counter() - self._start
        return False
