//! Minimal ANSI dashboard toolkit for the sniper console (no external crates).

use std::io::{self, IsTerminal, Write};
use std::sync::OnceLock;

pub const RESET: &str = "\x1b[0m";

pub const INK: &str = "#e6edf3";
pub const MUTED: &str = "#8b98a5";
pub const FAINT: &str = "#475467";
pub const BORDER: &str = "#243244";
pub const ACCENT: &str = "#2dd4bf";
pub const SKY: &str = "#60a5fa";
pub const GREEN: &str = "#34d399";
pub const AMBER: &str = "#fbbf24";
pub const RED: &str = "#f87171";
pub const VIOLET: &str = "#a78bfa";

static COLOR: OnceLock<bool> = OnceLock::new();
static UNICODE: OnceLock<bool> = OnceLock::new();

#[cfg(windows)]
mod win {
    #[repr(C)]
    pub struct Coord {
        pub x: i16,
        pub y: i16,
    }

    #[repr(C)]
    pub struct SmallRect {
        pub left: i16,
        pub top: i16,
        pub right: i16,
        pub bottom: i16,
    }

    #[repr(C)]
    pub struct ConsoleScreenBufferInfo {
        pub size: Coord,
        pub cursor_position: Coord,
        pub attributes: u16,
        pub window: SmallRect,
        pub maximum_window_size: Coord,
    }

    #[link(name = "kernel32")]
    extern "system" {
        pub fn GetStdHandle(n_std_handle: u32) -> *mut core::ffi::c_void;
        pub fn GetConsoleMode(handle: *mut core::ffi::c_void, mode: *mut u32) -> i32;
        pub fn SetConsoleMode(handle: *mut core::ffi::c_void, mode: u32) -> i32;
        pub fn GetConsoleScreenBufferInfo(
            handle: *mut core::ffi::c_void,
            info: *mut ConsoleScreenBufferInfo,
        ) -> i32;
    }
}

#[cfg(windows)]
fn enable_vt() -> bool {
    unsafe {
        let handle = win::GetStdHandle(0xFFFF_FFF5);
        let mut mode: u32 = 0;
        if win::GetConsoleMode(handle, &mut mode) == 0 {
            return false;
        }
        win::SetConsoleMode(handle, mode | 0x0004) != 0
    }
}

#[cfg(not(windows))]
fn enable_vt() -> bool {
    true
}

pub fn color_enabled() -> bool {
    *COLOR.get_or_init(|| {
        if std::env::var_os("NO_COLOR").is_some() || std::env::var_os("ZERRO_NO_COLOR").is_some() {
            return false;
        }
        if std::env::var_os("FORCE_COLOR").is_some() {
            return true;
        }
        if !io::stdout().is_terminal() {
            return false;
        }
        enable_vt()
    })
}

pub fn unicode_enabled() -> bool {
    *UNICODE.get_or_init(|| {
        if std::env::var_os("ZERRO_ASCII").is_some() {
            return false;
        }
        if std::env::var_os("ZERRO_FORCE_UNICODE").is_some() {
            return true;
        }
        // Redirected logs stay ASCII so any editor can read them.
        io::stdout().is_terminal()
    })
}

fn rgb(hex: &str) -> (u8, u8, u8) {
    let clean = hex.trim_start_matches('#');
    (
        u8::from_str_radix(&clean[0..2], 16).unwrap_or(255),
        u8::from_str_radix(&clean[2..4], 16).unwrap_or(255),
        u8::from_str_radix(&clean[4..6], 16).unwrap_or(255),
    )
}

pub fn fg(hex: &str) -> String {
    let (r, g, b) = rgb(hex);
    format!("\x1b[38;2;{};{};{}m", r, g, b)
}

pub fn paint(text: &str, color: &str, bold: bool) -> String {
    if !color_enabled() || text.is_empty() {
        return text.to_string();
    }
    if bold {
        format!("{}\x1b[1m{}{}", fg(color), text, RESET)
    } else {
        format!("{}{}{}", fg(color), text, RESET)
    }
}

pub fn strip_ansi(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '\x1b' {
            i += 1;
            if i < chars.len() && chars[i] == '[' {
                i += 1;
                while i < chars.len() && !chars[i].is_ascii_alphabetic() {
                    i += 1;
                }
                if i < chars.len() {
                    i += 1;
                }
            }
            continue;
        }
        out.push(chars[i]);
        i += 1;
    }
    out
}

pub fn vlen(text: &str) -> usize {
    strip_ansi(text).chars().count()
}

/// Transliterate UI glyphs so redirected logs are readable in any editor.
pub fn to_ascii(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '·' => out.push('|'),
            '•' => out.push('*'),
            '→' => out.push_str("->"),
            '←' => out.push_str("<-"),
            '…' => out.push_str("..."),
            '✓' => out.push('+'),
            '✗' => out.push('x'),
            '●' => out.push('*'),
            '○' => out.push('o'),
            '▲' => out.push('!'),
            '⚡' => out.push('*'),
            '—' | '–' => out.push('-'),
            '≤' => out.push_str("<="),
            '≥' => out.push_str(">="),
            '█' => out.push('#'),
            '░' => out.push('.'),
            '▓' => out.push('='),
            '│' => out.push('|'),
            '─' => out.push('-'),
            '╭' | '╮' | '╰' | '╯' | '├' | '┤' => out.push('+'),
            _ if ch.is_ascii() => out.push(ch),
            _ => out.push('?'),
        }
    }
    out
}

pub fn truncate(text: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }
    let plain = strip_ansi(text);
    if plain.chars().count() <= width {
        return text.to_string();
    }
    let ellipsis = if unicode_enabled() { "…" } else { "." };
    let keep = width.saturating_sub(1);
    let cut: String = plain.chars().take(keep).collect();
    format!("{}{}", cut, ellipsis)
}

pub fn pad_to(text: &str, width: usize) -> String {
    let gap = width.saturating_sub(vlen(text));
    format!("{}{}", text, " ".repeat(gap))
}

pub fn glyph(name: &str) -> &'static str {
    let uni = unicode_enabled();
    match name {
        "tl" => if uni { "╭" } else { "+" },
        "tr" => if uni { "╮" } else { "+" },
        "bl" => if uni { "╰" } else { "+" },
        "br" => if uni { "╯" } else { "+" },
        "h" => if uni { "─" } else { "-" },
        "v" => if uni { "│" } else { "|" },
        "lt" => if uni { "├" } else { "+" },
        "rt" => if uni { "┤" } else { "+" },
        "dot" => if uni { "●" } else { "*" },
        "full" => if uni { "█" } else { "#" },
        "empty" => if uni { "░" } else { "." },
        "check" => if uni { "✓" } else { "+" },
        "cross" => if uni { "✗" } else { "x" },
        "bolt" => if uni { "⚡" } else { "*" },
        _ => "?",
    }
}

pub fn meter(fraction: f64, width: usize, color: &str) -> String {
    let frac = fraction.clamp(0.0, 1.0);
    let filled = (frac * width as f64).round() as usize;
    format!(
        "{}{}",
        paint(&glyph("full").repeat(filled), color, false),
        paint(&glyph("empty").repeat(width.saturating_sub(filled)), FAINT, false)
    )
}

pub fn human_int(value: u64) -> String {
    let digits = value.to_string();
    let mut out = String::new();
    for (idx, ch) in digits.chars().enumerate() {
        if idx > 0 && (digits.len() - idx) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    out
}

pub fn human_secs(seconds: u64) -> String {
    if seconds < 60 {
        return format!("{}s", seconds);
    }
    let minutes = seconds / 60;
    if minutes < 60 {
        return format!("{}m{:02}s", minutes, seconds % 60);
    }
    let hours = minutes / 60;
    if hours < 24 {
        return format!("{}h{:02}m", hours, minutes % 60);
    }
    format!("{}d{:02}h", hours / 24, hours % 24)
}

pub fn usd(value: f64) -> String {
    format!("${:.4}", value)
}

pub fn short_addr(address: &str, head: usize, tail: usize) -> String {
    if address.len() <= head + tail + 2 {
        return address.to_string();
    }
    format!("{}…{}", &address[..head], &address[address.len() - tail..])
}

pub fn utc_clock() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
        % 86_400;
    format!("{:02}:{:02}:{:02}", secs / 3600, (secs % 3600) / 60, secs % 60)
}

pub fn term_size() -> (usize, usize) {
    if let Ok(cols) = std::env::var("COLUMNS") {
        if let Ok(value) = cols.parse::<usize>() {
            if value > 40 {
                return (value, 34);
            }
        }
    }
    #[cfg(windows)]
    unsafe {
        let handle = win::GetStdHandle(0xFFFF_FFF5);
        let mut info: win::ConsoleScreenBufferInfo = std::mem::zeroed();
        if win::GetConsoleScreenBufferInfo(handle, &mut info) != 0 {
            let cols = (info.window.right - info.window.left + 1).max(0) as usize;
            let rows = (info.window.bottom - info.window.top + 1).max(0) as usize;
            if cols > 40 {
                return (cols, rows.max(20));
            }
        }
    }
    (104, 34)
}

// ---------------------------------------------------------------------------
// Boxed frame
// ---------------------------------------------------------------------------

pub struct Frame {
    width: usize,
    lines: Vec<String>,
}

impl Frame {
    pub fn new(width: usize, title: &str, right: &str) -> Self {
        let width = width.max(64);
        let left_pad = glyph("h").to_string();
        let title_s = if title.is_empty() {
            String::new()
        } else {
            format!(" {} ", title)
        };
        let right_s = if right.is_empty() {
            String::new()
        } else {
            format!(" {} ", right)
        };
        let fill = width
            .saturating_sub(4)
            .saturating_sub(vlen(&title_s) + vlen(&right_s));
        let top = format!(
            "{}{}{}{}{}{}{}",
            paint(glyph("tl"), BORDER, false),
            paint(&left_pad, BORDER, false),
            paint(&title_s, ACCENT, true),
            paint(&glyph("h").repeat(fill), BORDER, false),
            paint(&right_s, MUTED, false),
            paint(glyph("h"), BORDER, false),
            paint(glyph("tr"), BORDER, false),
        );
        Frame {
            width,
            lines: vec![top],
        }
    }

    fn inner(&self) -> usize {
        self.width - 4
    }

    fn wrap(&self, content: &str) -> String {
        format!(
            "{} {} {}",
            paint(glyph("v"), BORDER, false),
            content,
            paint(glyph("v"), BORDER, false)
        )
    }

    pub fn row(&mut self, content: &str) {
        let body = truncate(content, self.inner());
        let line = self.wrap(&pad_to(&body, self.inner()));
        self.lines.push(line);
    }

    pub fn pair(&mut self, left: &str, right: &str) {
        let right_s = truncate(right, self.inner() / 2);
        let left_budget = self.inner().saturating_sub(vlen(&right_s) + 1);
        let left_s = truncate(left, left_budget);
        let gap = self
            .inner()
            .saturating_sub(vlen(&left_s) + vlen(&right_s))
            .max(1);
        let line = self.wrap(&format!("{}{}{}", left_s, " ".repeat(gap), right_s));
        self.lines.push(line);
    }

    pub fn blank(&mut self) {
        let line = self.wrap(&" ".repeat(self.inner()));
        self.lines.push(line);
    }

    pub fn rule(&mut self, label: &str) {
        let inner = self.inner();
        let line = if label.is_empty() {
            format!(
                "{}{}{}",
                paint(glyph("lt"), BORDER, false),
                paint(&glyph("h").repeat(self.width - 2), BORDER, false),
                paint(glyph("rt"), BORDER, false)
            )
        } else {
            let text = format!(" {} ", label);
            let remaining = inner.saturating_sub(vlen(&text) + 1);
            format!(
                "{}{}{}{}{}",
                paint(glyph("lt"), BORDER, false),
                paint(glyph("h"), BORDER, false),
                paint(&text, MUTED, true),
                paint(&glyph("h").repeat(remaining), BORDER, false),
                paint(glyph("rt"), BORDER, false)
            )
        };
        self.lines.push(line);
    }

    pub fn close(mut self) -> Vec<String> {
        self.lines.push(format!(
            "{}{}{}",
            paint(glyph("bl"), BORDER, false),
            paint(&glyph("h").repeat(self.width - 2), BORDER, false),
            paint(glyph("br"), BORDER, false)
        ));
        self.lines
    }
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

pub struct Screen {
    tty: bool,
    active: bool,
    last_plain: Vec<String>,
    last_emit: std::time::Instant,
    log_interval: f64,
}

impl Screen {
    pub fn new() -> Self {
        Screen {
            tty: io::stdout().is_terminal() && color_enabled(),
            active: false,
            last_plain: Vec::new(),
            last_emit: std::time::Instant::now() - std::time::Duration::from_secs(3600),
            log_interval: std::env::var("ZERRO_LOG_INTERVAL")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(5.0),
        }
    }

    pub fn enter(&mut self) {
        if self.active || !self.tty {
            return;
        }
        self.active = true;
        print!("\x1b[?25l\x1b[?1049h\x1b[2J\x1b[H");
        let _ = io::stdout().flush();
    }

    pub fn draw(&mut self, lines: &[String]) {
        if !self.tty {
            if self.last_emit.elapsed().as_secs_f64() < self.log_interval {
                return;
            }
            let plain: Vec<String> = lines.iter().map(|l| to_ascii(&strip_ansi(l))).collect();
            if plain != self.last_plain {
                for line in &plain {
                    println!("{}", line);
                }
                let _ = io::stdout().flush();
                self.last_plain = plain;
                self.last_emit = std::time::Instant::now();
            }
            return;
        }
        let (cols, rows) = term_size();
        let mut buf = String::from("\x1b[H");
        for line in lines.iter().take(rows.saturating_sub(1)) {
            buf.push_str(&truncate(line, cols));
            buf.push_str("\x1b[K\r\n");
        }
        buf.push_str("\x1b[0J");
        print!("{}", buf);
        let _ = io::stdout().flush();
    }

    pub fn leave(&mut self) {
        if !self.active {
            return;
        }
        self.active = false;
        print!("\x1b[?1049l\x1b[?25h");
        let _ = io::stdout().flush();
    }
}

impl Drop for Screen {
    fn drop(&mut self) {
        self.leave();
    }
}
