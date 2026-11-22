"""Terminal UI toolkit for every Zerro-DEFI CLI surface (stdlib only).

Provides truecolor ANSI helpers, boxed panels, a flicker-free full-screen
renderer and a shared event log.  When stdout is not a TTY (piped to a file,
CI, etc.) every helper degrades gracefully to plain text.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
import unicodedata
from collections import deque
from typing import Callable, Deque, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "INK", "MUTED", "FAINT", "BORDER", "PANEL", "ACCENT", "SKY", "GREEN",
    "AMBER", "RED", "VIOLET", "WHITE",
    "color_enabled", "unicode_enabled", "ensure_utf8", "paint", "strip_ansi", "to_ascii",
    "vlen", "truncate", "pad", "gradient", "rule",
    "Frame", "Screen", "EventLog", "EVENTS", "log", "set_sink", "clear_sink",
    "glyph", "dot", "badge", "meter", "sparkline",
    "human_int", "human_secs", "ago", "usd", "gwei", "short_addr", "pct",
    "term_size", "now_clock", "spinner_frame",
]

# ---------------------------------------------------------------------------
# Palette (truecolor, tuned for dark terminals)
# ---------------------------------------------------------------------------

INK = "#e6edf3"
MUTED = "#8b98a5"
FAINT = "#475467"
BORDER = "#243244"
PANEL = "#0f1720"
ACCENT = "#2dd4bf"
SKY = "#60a5fa"
GREEN = "#34d399"
AMBER = "#fbbf24"
RED = "#f87171"
VIOLET = "#a78bfa"
WHITE = "#f8fafc"

RESET = "\x1b[0m"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_COLOR: Optional[bool] = None
_UNICODE: Optional[bool] = None

_LEVEL_COLORS = {
    "info": SKY,
    "ok": GREEN,
    "warn": AMBER,
    "error": RED,
    "trade": ACCENT,
    "muted": FAINT,
}


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------


def _enable_windows_vt() -> bool:
    """Turn on ENABLE_VIRTUAL_TERMINAL_PROCESSING for the current console."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def color_enabled() -> bool:
    global _COLOR
    if _COLOR is None:
        if os.getenv("NO_COLOR") is not None or os.getenv("ZERRO_NO_COLOR"):
            _COLOR = False
        elif os.getenv("FORCE_COLOR"):
            _COLOR = True
        elif not sys.stdout.isatty():
            _COLOR = False
        else:
            _COLOR = _enable_windows_vt()
    return _COLOR


def unicode_enabled() -> bool:
    global _UNICODE
    if _UNICODE is None:
        if os.getenv("ZERRO_ASCII"):
            _UNICODE = False
        elif os.getenv("ZERRO_FORCE_UNICODE"):
            _UNICODE = True
        else:
            enc = (getattr(sys.stdout, "encoding", "") or "").lower()
            # Redirected logs stay ASCII so every editor/console can read them.
            _UNICODE = sys.stdout.isatty() and "utf" in enc
    return _UNICODE


def ensure_utf8() -> None:
    """Reconfigure stdout/stderr to UTF-8 where supported (Windows safety)."""
    if os.name == "nt" and sys.stdout.isatty():
        try:
            import ctypes

            # Match the UTF-8 stream encoding so box glyphs render correctly.
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    global _UNICODE
    _UNICODE = None


# ---------------------------------------------------------------------------
# Low level painting
# ---------------------------------------------------------------------------


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _fg(hex_color: str) -> str:
    r, g, b = _rgb(hex_color)
    return f"\x1b[38;2;{r};{g};{b}m"


def _bg(hex_color: str) -> str:
    r, g, b = _rgb(hex_color)
    return f"\x1b[48;2;{r};{g};{b}m"


def paint(
    text: str,
    color: Optional[str] = None,
    *,
    bold: bool = False,
    dim: bool = False,
    italic: bool = False,
    underline: bool = False,
    bg: Optional[str] = None,
) -> str:
    """Wrap ``text`` in ANSI styling (no-op when color is unavailable)."""
    if not color_enabled() or not text:
        return text
    codes: List[str] = []
    if color:
        codes.append(_fg(color))
    if bg:
        codes.append(_bg(bg))
    if bold:
        codes.append("\x1b[1m")
    if dim:
        codes.append("\x1b[2m")
    if italic:
        codes.append("\x1b[3m")
    if underline:
        codes.append("\x1b[4m")
    if not codes:
        return text
    return "".join(codes) + text + RESET


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


_ASCII_MAP = {
    "·": "|", "•": "*", "→": "->", "←": "<-", "…": "...", "✓": "+", "✗": "x",
    "●": "*", "○": "o", "▲": "!", "⚡": "*", "—": "-", "–": "-", "≤": "<=",
    "≥": ">=", "▸": ">", "█": "#", "░": ".", "▓": "=", "│": "|", "─": "-",
    "╭": "+", "╮": "+", "╰": "+", "╯": "+", "├": "+", "┤": "+", "\u00a0": " ",
}


def to_ascii(text: str) -> str:
    """Transliterate UI glyphs so redirected logs are readable everywhere."""
    out = []
    for ch in text:
        if ord(ch) < 128:
            out.append(ch)
        else:
            out.append(_ASCII_MAP.get(ch, "?"))
    return "".join(out)


def _char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vlen(text: str) -> int:
    """Visible width of a string, ignoring ANSI escapes."""
    return sum(_char_width(ch) for ch in strip_ansi(text))


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    """ANSI-aware truncation to ``width`` visible columns."""
    if width <= 0:
        return ""
    if vlen(text) <= width:
        return text
    budget = width - _char_width(ellipsis)
    out: List[str] = []
    used = 0
    i = 0
    while i < len(text):
        match = _ANSI_RE.match(text, i)
        if match:
            out.append(match.group(0))
            i = match.end()
            continue
        ch = text[i]
        cw = _char_width(ch)
        if used + cw > budget:
            break
        out.append(ch)
        used += cw
        i += 1
    out.append(ellipsis)
    if color_enabled():
        out.append(RESET)
    return "".join(out)


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad to ``width`` visible columns."""
    gap = width - vlen(text)
    if gap <= 0:
        return truncate(text, width)
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def _lerp(a: str, b: str, t: float) -> Tuple[int, int, int]:
    ar, ag, ab = _rgb(a)
    br, bg_, bb = _rgb(b)
    return (
        round(ar + (br - ar) * t),
        round(ag + (bg_ - ag) * t),
        round(ab + (bb - ab) * t),
    )


def gradient(text: str, stops: Sequence[str]) -> str:
    """Per-character truecolor gradient across ``stops``."""
    if not color_enabled() or not text or not stops:
        return text
    if len(stops) == 1:
        return paint(text, stops[0], bold=True)
    chars = list(text)
    span = max(1, len(chars) - 1)
    out: List[str] = []
    for idx, ch in enumerate(chars):
        if ch == " ":
            out.append(ch)
            continue
        pos = idx / span * (len(stops) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(stops) - 1)
        r, g, b = _lerp(stops[lo], stops[hi], pos - lo)
        out.append(f"\x1b[38;2;{r};{g};{b}m{ch}")
    out.append(RESET)
    return "".join(out)


# ---------------------------------------------------------------------------
# Glyphs & small widgets
# ---------------------------------------------------------------------------


def glyph(name: str) -> str:
    uni = {
        "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯",
        "h": "─", "v": "│", "lt": "├", "rt": "┤",
        "dot": "●", "ring": "○", "check": "✓", "cross": "✗",
        "arrow": "▸", "warn": "▲", "bolt": "⚡", "sep": "·",
        "full": "█", "empty": "░", "half": "▓",
    }
    ascii_ = {
        "tl": "+", "tr": "+", "bl": "+", "br": "+",
        "h": "-", "v": "|", "lt": "+", "rt": "+",
        "dot": "*", "ring": "o", "check": "+", "cross": "x",
        "arrow": ">", "warn": "!", "bolt": "*", "sep": "|",
        "full": "#", "empty": ".", "half": "=",
    }
    return (uni if unicode_enabled() else ascii_).get(name, name)


def dot(state: str) -> str:
    """Colored status dot for a semantic state."""
    colors = {
        "live": GREEN, "ok": GREEN, "running": GREEN, "closed": GREEN,
        "idle": MUTED, "warn": AMBER, "stale": AMBER, "degraded": AMBER,
        "error": RED, "open": RED, "dead": RED,
    }
    return paint(glyph("dot"), colors.get(state, MUTED))


def badge(text: str, color: str, *, filled: bool = False) -> str:
    label = f" {text} "
    if filled:
        return paint(label, "#0b0f14", bold=True, bg=color)
    return paint(label, color, bold=True)


def meter(fraction: float, width: int, color: str = ACCENT, empty: str = FAINT) -> str:
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = round(fraction * width)
    return (
        paint(glyph("full") * filled, color)
        + paint(glyph("empty") * (width - filled), empty)
    )


def sparkline(values: Sequence[float], color: str = ACCENT) -> str:
    if not values:
        return paint("·" * 8, FAINT)
    bars = "▁▂▃▄▅▆▇█" if unicode_enabled() else "._-=+*#@"
    lo = min(values)
    hi = max(values)
    span = (hi - lo) or 1.0
    out = []
    for value in values:
        idx = int((value - lo) / span * (len(bars) - 1))
        out.append(bars[max(0, min(idx, len(bars) - 1))])
    return paint("".join(out), color)


def rule(width: int, color: str = BORDER, label: str = "") -> str:
    if not label:
        return paint(glyph("h") * width, color)
    text = f" {label} "
    remaining = max(0, width - vlen(text))
    left = remaining // 2
    return paint(glyph("h") * left, color) + paint(text, MUTED, bold=True) + paint(
        glyph("h") * (remaining - left), color
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def human_int(value: float, decimals: int = 0) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def human_secs(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def ago(timestamp: float) -> str:
    if not timestamp:
        return "never"
    return human_secs(time.time() - timestamp) + " ago"


def usd(value: float, decimals: int = 4) -> str:
    if value is None:
        return "—"
    return f"${value:,.{decimals}f}"


def gwei(wei: float) -> str:
    return f"{wei / 1e9:,.4f} gwei"


def short_addr(address: str, head: int = 6, tail: int = 4) -> str:
    if not address:
        return "—"
    if len(address) <= head + tail + 2:
        return address
    return f"{address[:head]}…{address[-tail:]}"


def pct(value: float, decimals: int = 1) -> str:
    return f"{value * 100:.{decimals}f}%"


def term_size(default: Tuple[int, int] = (104, 34)) -> Tuple[int, int]:
    size = shutil.get_terminal_size(default)
    return size.columns, size.lines


def now_clock() -> str:
    return time.strftime("%H:%M:%S")


def spinner_frame(tick: int) -> str:
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if unicode_enabled() else "|/-\\"
    return frames[tick % len(frames)]


# ---------------------------------------------------------------------------
# Boxed frame
# ---------------------------------------------------------------------------


class Frame:
    """A rounded box that auto-pads every row to an exact visible width."""

    def __init__(self, width: int, title: str = "", right: str = "", accent: str = ACCENT):
        self.width = max(46, int(width))
        self.accent = accent
        self.lines: List[str] = []
        self._top(title, right)

    # -- internals ---------------------------------------------------------
    @property
    def _inner(self) -> int:
        return self.width - 4

    def _wrap(self, content: str) -> str:
        b = paint(glyph("v"), BORDER)
        return b + " " + content + " " + b

    def _top(self, title: str, right: str) -> None:
        title_s = f" {title} " if title else ""
        right_s = f" {right} " if right else ""
        fill = max(0, self.width - 4 - vlen(title_s) - vlen(right_s))
        line = (
            paint(glyph("tl"), BORDER)
            + paint(glyph("h"), BORDER)
            + paint(title_s, self.accent, bold=True)
            + paint(glyph("h") * fill, BORDER)
            + paint(right_s, MUTED)
            + paint(glyph("h"), BORDER)
            + paint(glyph("tr"), BORDER)
        )
        self.lines.append(line)

    # -- public ------------------------------------------------------------
    def row(self, content: str = "", color: Optional[str] = None, bold: bool = False) -> "Frame":
        content = truncate(content, self._inner)
        if color:
            content = paint(content, color, bold=bold)
        self.lines.append(self._wrap(pad(content, self._inner)))
        return self

    def pair(self, left: str, right: str, left_color: Optional[str] = None,
             right_color: Optional[str] = MUTED) -> "Frame":
        right_s = truncate(right, max(0, self._inner // 2))
        left_budget = self._inner - vlen(right_s) - 1
        left_s = truncate(left, left_budget)
        if left_color:
            left_s = paint(left_s, left_color)
        if right_color:
            right_s = paint(right_s, right_color)
        gap = max(1, self._inner - vlen(left_s) - vlen(right_s))
        self.lines.append(self._wrap(left_s + " " * gap + right_s))
        return self

    def blank(self) -> "Frame":
        self.lines.append(self._wrap(" " * self._inner))
        return self

    def rule(self, label: str = "") -> "Frame":
        if label:
            text = f" {label} "
            remaining = max(0, self.width - 3 - vlen(text))
            body = (
                paint(glyph("h"), BORDER)
                + paint(text, MUTED, bold=True)
                + paint(glyph("h") * remaining, BORDER)
            )
            self.lines.append(
                paint(glyph("lt"), BORDER) + body + paint(glyph("rt"), BORDER)
            )
        else:
            self.lines.append(
                paint(glyph("lt"), BORDER)
                + paint(glyph("h") * (self.width - 2), BORDER)
                + paint(glyph("rt"), BORDER)
            )
        return self

    def close(self) -> List[str]:
        self.lines.append(
            paint(glyph("bl"), BORDER)
            + paint(glyph("h") * (self.width - 2), BORDER)
            + paint(glyph("br"), BORDER)
        )
        return list(self.lines)


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


class EventLog:
    """Bounded ring buffer of timestamped events."""

    def __init__(self, capacity: int = 400) -> None:
        self.items: Deque[Tuple[float, str, str, str]] = deque(maxlen=capacity)

    def add(self, level: str, component: str, message: str) -> None:
        self.items.append((time.time(), level, component, str(message)))

    def tail(self, count: int) -> List[Tuple[float, str, str, str]]:
        return list(self.items)[-count:]

    def clear(self) -> None:
        self.items.clear()

    def format(self, item: Tuple[float, str, str, str], width: int) -> str:
        ts, level, component, message = item
        stamp = time.strftime("%H:%M:%S", time.localtime(ts))
        mark = {
            "ok": glyph("check"), "error": glyph("cross"), "warn": glyph("warn"),
            "trade": glyph("bolt"),
        }.get(level, "·")
        head = (
            paint(stamp, FAINT)
            + " "
            + paint(mark, _LEVEL_COLORS.get(level, MUTED))
            + " "
            + paint(f"{component:<11}", MUTED)
        )
        body_budget = max(8, width - vlen(head) - 1)
        return head + " " + paint(truncate(message, body_budget), INK)


EVENTS = EventLog()

_SINK: Optional[Callable[[str, str, str], None]] = None


def set_sink(sink: Optional[Callable[[str, str, str], None]]) -> None:
    global _SINK
    _SINK = sink


def clear_sink() -> None:
    set_sink(None)


def log(component: str, message: str, level: str = "info") -> None:
    """Record an event and mirror it to the console when no dashboard is live."""
    EVENTS.add(level, component, message)
    if _SINK is not None:
        _SINK(level, component, str(message))
        return
    mark = {
        "ok": glyph("check"), "error": glyph("cross"), "warn": glyph("warn"),
        "trade": glyph("bolt"),
    }.get(level, "·")
    print(
        paint(now_clock(), FAINT)
        + " "
        + paint(mark, _LEVEL_COLORS.get(level, MUTED))
        + " "
        + paint(f"{component:<11}", MUTED)
        + " "
        + to_ascii(str(message)),
        flush=True,
    )


# ---------------------------------------------------------------------------
# Full screen renderer
# ---------------------------------------------------------------------------


class Screen:
    """Alternate-screen renderer with cursor-home redraws (no flicker)."""

    def __init__(self, alternate: bool = True) -> None:
        self.tty = sys.stdout.isatty() and color_enabled()
        self.alternate = alternate and self.tty
        self.active = False
        self._last_plain: List[str] = []
        self._last_emit = 0.0
        try:
            self._log_interval = float(os.getenv("ZERRO_LOG_INTERVAL", "5"))
        except ValueError:
            self._log_interval = 5.0

    def __enter__(self) -> "Screen":
        self.enter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.leave()

    def enter(self) -> None:
        if self.active:
            return
        self.active = True
        if self.tty:
            out = "\x1b[?25l"
            if self.alternate:
                out += "\x1b[?1049h"
            out += "\x1b[2J\x1b[H"
            sys.stdout.write(out)
            sys.stdout.flush()

    def leave(self) -> None:
        if not self.active:
            return
        self.active = False
        if self.tty:
            out = ""
            if self.alternate:
                out += "\x1b[?1049l"
            out += "\x1b[?25h"
            sys.stdout.write(out)
            sys.stdout.flush()

    def draw(self, lines: Iterable[str]) -> None:
        rows = list(lines)
        if not self.tty:
            # Redirected output: append-only snapshots, throttled and ASCII-safe.
            now = time.time()
            if now - self._last_emit < self._log_interval:
                return
            plain = [to_ascii(strip_ansi(row).rstrip()) for row in rows]
            if plain != self._last_plain:
                print("\n".join(plain), flush=True)
                self._last_plain = plain
                self._last_emit = now
            return
        cols, height = term_size()
        clipped = rows[: max(4, height - 1)]
        buf = ["\x1b[H"]
        for row in clipped:
            buf.append(truncate(row, cols))
            buf.append("\x1b[K\r\n")
        buf.append("\x1b[0J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Boot card used by service entrypoints
# ---------------------------------------------------------------------------


def banner_block(subtitle: str, width: int = 74) -> List[str]:
    """Compact wordmark + tagline used above dashboards and launchers."""
    title = gradient("ZERRO-DEFI", [ACCENT, SKY, VIOLET])
    spaced = " ".join(title) if not color_enabled() else title
    lines = [
        "",
        "  " + spaced + "  " + paint("·", FAINT) + "  " + paint(subtitle, MUTED),
        "  " + paint(glyph("h") * (width - 2), BORDER),
        "",
    ]
    return lines
