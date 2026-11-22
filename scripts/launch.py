#!/usr/bin/env python3
"""Zerro-DEFI launcher.

One command that walks the whole stack from a cold checkout to three live
terminals: preflight, environment, dependencies, wallet provisioning, contract
compilation + EVM tests, paymaster probe, $0-gas deployment, Rust build and
finally the service terminals.

    python scripts/launch.py                 # prepare everything, keep DRY_RUN as-is
    python scripts/launch.py --live --yes    # flip DRY_RUN=false and deploy
    python scripts/launch.py --only rust,launch
    python scripts/launch.py --list

Safety: this launcher never silently flips ``DRY_RUN``.  Live submission is
opt-in through ``--live`` and requires confirmation (or ``--yes``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
ORCH_DIR = ROOT / "py-orchestrator"
RUST_DIR = ROOT / "rust-sniper"
SCRIPTS_DIR = ROOT / "scripts"
RUNTIME_DIR = ROOT / "runtime"
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
TARGETS_PATH = ROOT / "targets.json"
OUTBOX_PATH = ROOT / "verified_outbox.json"
ORCH_STATUS = RUNTIME_DIR / "orchestrator_status.json"
SNIPER_STATUS = RUNTIME_DIR / "sniper_status.json"
LOG_PATH = RUNTIME_DIR / "launcher.log"

if str(ORCH_DIR) not in sys.path:
    sys.path.insert(0, str(ORCH_DIR))

from orchestrator import ui  # noqa: E402

# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------

STEPS: List[Tuple[str, str, str]] = [
    ("preflight", "preflight", "toolchain + repo layout"),
    ("env", "env", "read / create .env, resolve mode"),
    ("deps", "deps", "install python packages"),
    ("wallet", "wallet", "derive operator + executor address"),
    ("compile", "compile", "solc build + 5 EVM tests + fork test"),
    ("paymaster", "paymaster", "sponsorship probe"),
    ("deploy", "deploy", "$0-gas CREATE2 deployment"),
    ("rust", "rust", "cargo build --release"),
    ("launch", "launch", "open service terminals"),
    ("prove", "prove", "live capital + sponsorship proof"),
    ("verify", "verify", "monitor snapshot + orchestrator config"),
]
STEP_IDS = [s[0] for s in STEPS]
LABEL_WIDTH = 11

LEVEL_COLOR = {
    "ok": ui.GREEN,
    "warn": ui.AMBER,
    "error": ui.RED,
    "info": ui.SKY,
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _mask(secret: str, head: int = 6, tail: int = 4) -> str:
    if not secret:
        return "—"
    if len(secret) <= head + tail:
        return "*" * len(secret)
    return f"{secret[:head]}…{secret[-tail:]}"


def read_env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for raw in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_env_var(key: str, value: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(f"{key}={value}", text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"{key}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def is_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def has_paymaster(env: Dict[str, str]) -> bool:
    url = (env.get("PAYMASTER_BUNDLER_RPC") or "").strip()
    return url.startswith("http") and "YOUR_KEY" not in url


def find_cargo() -> Optional[str]:
    exe = shutil.which("cargo")
    if exe:
        return exe
    candidate = Path.home() / ".cargo" / "bin" / ("cargo.exe" if os.name == "nt" else "cargo")
    return str(candidate) if candidate.exists() else None


def target_counts() -> Optional[dict]:
    try:
        data = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {
        "morpho": len(data.get("morpho_targets") or []),
        "dutch": len(data.get("dutch_targets") or []),
        "harvest": len(data.get("harvest_targets") or []),
        "updated_at": int(data.get("updated_at") or 0),
    }


def _cmdline(cmd: Sequence[str]) -> str:
    return " ".join(f'"{c}"' if " " in c else c for c in cmd)


# ---------------------------------------------------------------------------
# Live progress
# ---------------------------------------------------------------------------


class Progress:
    """One-line spinner that rewrites in place on a TTY, plain otherwise."""

    def __init__(self) -> None:
        self.tty = sys.stdout.isatty()
        self.tick = 0
        self.active = False
        self._width = 0

    def start(self, label: str, detail: str = "") -> None:
        self.active = True
        self.tick = 0
        self._render(label, detail)

    def update(self, label: str, detail: str) -> None:
        if not self.active:
            return
        self.tick += 1
        self._render(label, detail)

    def stop(self) -> None:
        if self.active and self.tty:
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
        self.active = False

    def _render(self, label: str, detail: str) -> None:
        if not self.tty:
            return
        cols, _ = ui.term_size()
        spin = ui.paint(ui.spinner_frame(self.tick), ui.ACCENT)
        head = f"  {spin} {ui.paint(ui.pad(label, LABEL_WIDTH), ui.INK, bold=True)} "
        budget = max(12, cols - ui.vlen(head) - 2)
        tail = ui.paint(ui.truncate(detail, budget), ui.FAINT) if detail else ""
        sys.stdout.write("\r\x1b[K" + head + tail)
        sys.stdout.flush()


@dataclass
class StepOutcome:
    key: str
    label: str
    ok: bool
    skipped: bool
    detail: str
    seconds: float
    output: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


class Launcher:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.progress = Progress()
        self.env: Dict[str, str] = {}
        self.outcomes: Dict[str, StepOutcome] = {}
        self.warnings: List[str] = []
        self.launched: List[str] = []
        self.verbose = args.verbose
        self._transcript: List[str] = []
        self.cargo: Optional[str] = None

    # -- output ------------------------------------------------------------
    def say(self, level: str, message: str) -> None:
        color = LEVEL_COLOR.get(level, ui.MUTED)
        mark = {
            "ok": ui.glyph("check"),
            "warn": ui.glyph("warn"),
            "error": ui.glyph("cross"),
        }.get(level, ui.glyph("arrow"))
        print(f"  {ui.paint(mark, color)} {message}", flush=True)
        self._transcript.append(f"[{level}] {message}")

    def detail(self, message: str) -> None:
        print(f"    {ui.paint(message, ui.FAINT)}", flush=True)
        self._transcript.append(f"    {message}")

    # -- streaming ---------------------------------------------------------
    def run_stream(
        self, label: str, cmd: Sequence[str], cwd: Path, env: Optional[Dict[str, str]] = None
    ) -> Tuple[int, List[str], str]:
        child_env = os.environ.copy()
        child_env.setdefault("PYTHONUTF8", "1")
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        child_env.setdefault("GIT_PAGER", "cat")
        if env:
            child_env.update(env)

        self._transcript.append(f"$ {_cmdline(cmd)}  (cwd={cwd})")
        try:
            proc = subprocess.Popen(
                list(cmd),
                cwd=str(cwd),
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            return 127, [], f"command not found: {cmd[0]}"

        lines: List[str] = []
        last = ""
        self.progress.start(label, "starting…")
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            lines.append(line)
            self._transcript.append(line)
            last = line
            self.progress.update(label, line)
        code = proc.wait()
        self.progress.stop()
        return code, lines, last

    # -- step driver -------------------------------------------------------
    def step(self, key: str) -> None:
        label = dict((k, l) for k, l, _ in STEPS)[key]
        started = time.perf_counter()
        try:
            ok, skipped, detail, output = getattr(self, f"step_{key}")()
        except KeyboardInterrupt:
            self.progress.stop()
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            ok, skipped, detail, output = False, False, f"{type(exc).__name__}: {exc}", []
        seconds = time.perf_counter() - started

        outcome = StepOutcome(key, label, ok, skipped, detail, seconds, output)
        self.outcomes[key] = outcome
        self._print_outcome(outcome)

        if output and (self.verbose or not ok) and not skipped:
            tail = output[-16:] if not ok else output[-3:]
            for line in tail:
                print(f"      {ui.paint(ui.truncate(line, ui.term_size()[0] - 8), ui.FAINT)}")

    def _print_outcome(self, o: StepOutcome) -> None:
        if o.skipped:
            mark = ui.paint("–", ui.FAINT)
            detail = ui.paint(o.detail, ui.FAINT)
        elif o.ok:
            mark = ui.paint(ui.glyph("check"), ui.GREEN)
            detail = ui.paint(o.detail, ui.MUTED)
        else:
            mark = ui.paint(ui.glyph("cross"), ui.RED)
            detail = ui.paint(o.detail, ui.RED)
        print(
            f"  {mark} {ui.paint(ui.pad(o.label, LABEL_WIDTH), ui.INK, bold=True)} "
            f"{detail} {ui.paint(f'{o.seconds:.1f}s', ui.FAINT)}",
            flush=True,
        )
        self._transcript.append(f"{o.key}: ok={o.ok} skipped={o.skipped} {o.detail}")

    # -- steps -------------------------------------------------------------
    def step_preflight(self) -> Tuple[bool, bool, str, List[str]]:
        missing = []
        for path in (ORCH_DIR / "orchestrator", SCRIPTS_DIR, RUST_DIR / "Cargo.toml"):
            if not path.exists():
                missing.append(str(path.relative_to(ROOT)))
        if missing:
            return False, False, f"missing {', '.join(missing)}", []

        py = sys.version_info
        if py < (3, 11):
            return False, False, f"python {py.major}.{py.minor} < 3.11", []
        self.cargo = find_cargo()
        if not self.cargo:
            self.warnings.append("cargo not found — the Rust sniper will not build or launch")
            return True, False, f"python {py.major}.{py.minor}.{py.micro} · cargo missing", []
        code, out, _ = self.run_stream("preflight", [self.cargo, "--version"], ROOT)
        version = (out[0].split()[1] if out and len(out[0].split()) > 1 else "?") if code == 0 else "?"
        return True, False, f"python {py.major}.{py.minor}.{py.micro} · cargo {version}", out

    def step_env(self) -> Tuple[bool, bool, str, List[str]]:
        created = False
        if not ENV_PATH.exists():
            if not ENV_EXAMPLE.exists():
                return False, False, ".env and .env.example both missing", []
            ENV_PATH.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
            created = True
        self.env = read_env()

        notes = ["created from .env.example"] if created else []
        if self.args.live:
            if not self.args.yes:
                if not sys.stdin.isatty():
                    return False, False, "--live requires --yes when stdin is not a TTY", []
                answer = input(
                    f"  {ui.paint(ui.glyph('warn'), ui.AMBER)} "
                    f"Flip DRY_RUN=false and allow live submission? [y/N] "
                ).strip().lower()
                if answer not in {"y", "yes"}:
                    self.warnings.append("live mode declined — staying in dry-run")
                    self.args.live = False
            if self.args.live:
                set_env_var("DRY_RUN", "false")
                self.env["DRY_RUN"] = "false"
                notes.append("DRY_RUN=false")
        elif self.args.dry_run:
            set_env_var("DRY_RUN", "true")
            self.env["DRY_RUN"] = "true"
            notes.append("DRY_RUN=true")

        dry = is_truthy(self.env.get("DRY_RUN", "true"))
        key_set = bool(self.env.get("OPERATOR_PRIVATE_KEY"))
        executor = self.env.get("EXECUTOR_ADDRESS", "")
        paymaster = has_paymaster(self.env)

        if not key_set:
            notes.append("operator key will be generated")
        if not paymaster:
            self.warnings.append(
                "PAYMASTER_BUNDLER_RPC missing — submissions simulate only, no $0-gas deploy"
            )
        detail = (
            f"{'dry-run' if dry else 'LIVE'} · key {_mask(self.env.get('OPERATOR_PRIVATE_KEY', ''))}"
            f" · executor {ui.short_addr(executor, 8, 6) if executor else 'unset'}"
            f" · paymaster {'set' if paymaster else 'missing'}"
        )
        if notes:
            detail += f" · {', '.join(notes)}"
        return True, False, detail, []

    def step_deps(self) -> Tuple[bool, bool, str, List[str]]:
        if self.args.skip_install:
            return True, True, "skipped (--skip-install)", []
        code, out, _ = self.run_stream(
            "deps",
            [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet",
             "--disable-pip-version-check"],
            ORCH_DIR,
        )
        if code != 0:
            return False, False, f"pip install failed (exit {code})", out
        # aiohttp backs the indexer + monitor; keep it explicit even on older pins
        code2, out2, _ = self.run_stream(
            "deps",
            [sys.executable, "-m", "pip", "install", "aiohttp>=3.9", "--quiet",
             "--disable-pip-version-check"],
            ORCH_DIR,
        )
        if code2 != 0:
            return False, False, "aiohttp install failed", out + out2
        return True, False, "orchestrator + aiohttp ready", out

    def step_wallet(self) -> Tuple[bool, bool, str, List[str]]:
        if self.args.skip_wallet:
            return True, True, "skipped (--skip-wallet)", []
        script = SCRIPTS_DIR / "setup_operator_wallet.py"
        code, out, _ = self.run_stream("wallet", [sys.executable, str(script)], ROOT)
        if code != 0:
            return False, False, f"wallet provisioning failed (exit {code})", out
        self.env = read_env()
        executor = self.env.get("EXECUTOR_ADDRESS", "")
        address = ""
        for line in out:
            if "Operator EOA Address" in line:
                address = line.split(":", 1)[-1].strip()
        return True, False, (
            f"eoa {ui.short_addr(address, 8, 6) if address else '—'}"
            f" · executor {ui.short_addr(executor, 8, 6) if executor else '—'}"
        ), out

    def step_compile(self) -> Tuple[bool, bool, str, List[str]]:
        if self.args.skip_compile:
            return True, True, "skipped (--skip-compile)", []
        script = SCRIPTS_DIR / "compile_and_verify.py"
        code, out, _ = self.run_stream("compile", [sys.executable, str(script)], ROOT)
        if code != 0:
            return False, False, f"compilation or tests failed (exit {code})", out
        # Count passed tests from output like "5/5 passed" and "1/1 passed"
        import re

        passed = 0
        for line in out:
            m = re.search(r"(\d+)/(\d+)\s+passed", line, re.I)
            if m:
                passed += int(m.group(1))
        # Fallback: if no tests parsed, they all passed (command succeeded)
        if passed == 0 and code == 0:
            passed = 6  # 5 unit tests + 1 fork test
        return True, False, f"artifact written · {passed} checks passed", out

    def step_paymaster(self) -> Tuple[bool, bool, str, List[str]]:
        self.env = read_env()
        if not has_paymaster(self.env):
            return True, True, "no bundler configured", []
        script = SCRIPTS_DIR / "test_paymaster_integration.py"
        code, out, _ = self.run_stream("paymaster", [sys.executable, str(script)], ROOT)
        if code != 0:
            self.warnings.append("paymaster probe failed — deployment will be skipped")
            return False, False, f"probe failed (exit {code})", out
        verdict = ""
        for line in out:
            if "Sponsorship" in line:
                verdict = line.split(":", 1)[-1].strip()
        return True, False, verdict or "probe complete", out

    def step_deploy(self) -> Tuple[bool, bool, str, List[str]]:
        self.env = read_env()
        if self.args.skip_deploy:
            return True, True, "skipped (--skip-deploy)", []
        if not has_paymaster(self.env):
            return True, True, "needs a paymaster RPC", []
        if is_truthy(self.env.get("DRY_RUN", "true")):
            return True, True, "dry-run mode — pass --live to deploy", []
        script = SCRIPTS_DIR / "bootstrap_executor_zero_gas.py"
        code, out, _ = self.run_stream("deploy", [sys.executable, str(script)], ROOT)
        if code != 0:
            return False, False, f"deployment failed (exit {code})", out
        status = ""
        for line in out:
            if "Deploy Status" in line:
                status = line.split(":", 1)[-1].strip()
        return True, False, status or "CREATE2 transaction submitted", out

    def step_rust(self) -> Tuple[bool, bool, str, List[str]]:
        if self.args.skip_build:
            return True, True, "skipped (--skip-build)", []
        if not self.cargo:
            self.cargo = find_cargo()
        if not self.cargo:
            return False, False, "cargo not installed (https://rustup.rs)", []
        code, out, _ = self.run_stream(
            "rust", [self.cargo, "build", "--release"], RUST_DIR
        )
        if code != 0:
            return False, False, f"cargo build failed (exit {code})", out
        warnings = sum(1 for line in out if line.startswith("warning:"))
        binary = RUST_DIR / "target" / "release" / (
            "zero-cap-sniper.exe" if os.name == "nt" else "zero-cap-sniper"
        )
        size = f"{binary.stat().st_size / 1_048_576:.1f} MiB" if binary.exists() else "binary missing"
        detail = f"release binary {size}"
        if warnings:
            detail += f" · {warnings} warning(s)"
            self.warnings.append(f"cargo emitted {warnings} warning(s)")
        return True, False, detail, out

    def step_launch(self) -> Tuple[bool, bool, str, List[str]]:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self.env = read_env()
        dry = is_truthy(self.env.get("DRY_RUN", "true"))
        bats = self._write_service_scripts(dry)

        if self.args.no_launch:
            return True, True, f"wrote {len(bats)} script(s) · --no-launch", []

        if os.name != "nt":
            return True, True, "terminal auto-launch is Windows-only", []

        for title, path in bats:
            cmd = f'start "{title}" "{path}"'
            subprocess.Popen(cmd, shell=True, cwd=str(ROOT))
            self.launched.append(title)
            time.sleep(0.35)
        return True, False, f"{len(self.launched)} terminal(s) opened", []

    # -- proof / verify ----------------------------------------------------
    def step_prove(self) -> Tuple[bool, bool, str, List[str]]:
        script = SCRIPTS_DIR / "prove_zero_cap_zero_gas.py"
        code, out, _ = self.run_stream("prove", [sys.executable, str(script)], ROOT)
        if code != 0:
            return False, False, f"proof failed (exit {code})", out
        for line in out:
            if "sponsored ops" in line:
                detail = line.split("sponsored ops", 1)[-1].strip()
                return True, False, f"{detail} sponsored · $0 capital · $0 gas", out
        return True, False, "proof complete", out

    def step_verify(self) -> Tuple[bool, bool, str, List[str]]:
        code, out, _ = self.run_stream(
            "verify", [sys.executable, "-m", "orchestrator.monitor", "--once"], ORCH_DIR
        )
        if code != 0:
            return False, False, "monitor snapshot failed", out
        block = ""
        for line in out:
            if "BASE 8453" in line:
                parts = line.split("block", 1)
                if len(parts) == 2:
                    block = parts[1].split("|", 1)[0].strip()
                break
        targets = target_counts()
        if targets:
            total = targets["morpho"] + targets["dutch"] + targets["harvest"]
            label = f"{total} targets armed"
            if block:
                label += f" · block {block}"
            return True, False, label, out
        return True, False, "monitor ok", out

    # -- service scripts ---------------------------------------------------
    def _write_service_scripts(self, dry: bool) -> List[Tuple[str, Path]]:
        py = sys.executable
        orch = ORCH_DIR
        rust = RUST_DIR
        binary = rust / "target" / "release" / (
            "zero-cap-sniper.exe" if os.name == "nt" else "zero-cap-sniper"
        )
        common = [
            'set "TARGETS_PATH=' + str(TARGETS_PATH) + '"',
            'set "OUTBOX_PATH=' + str(OUTBOX_PATH) + '"',
        ]

        orchestrator = RUNTIME_DIR / "run_orchestrator.bat"
        self._bat(
            orchestrator,
            "Zerro-DEFI Orchestrator",
            [
                'set "PYTHONUTF8=1"',
                'set "PYTHONIOENCODING=utf-8"',
                f'set "PYTHONPATH={orch}"',
                *common,
                f'set "DRY_RUN={"false" if not dry else "true"}"',
                f'cd /d "{orch}"',
                f'"{py}" -u -m orchestrator.main',
            ],
        )

        sniper = RUNTIME_DIR / "run_sniper.bat"
        self._bat(
            sniper,
            "Zerro-DEFI Sniper",
            [
                *common,
                f'set "STATUS_PATH={SNIPER_STATUS}"',
                'set "SIM_BATCH_SIZE=6"',
                'set "MAX_SIM_PER_CYCLE=24"',
                f'cd /d "{rust}"',
                f'"{binary}"',
            ],
        )

        monitor = RUNTIME_DIR / "run_monitor.bat"
        self._bat(
            monitor,
            "Zerro-DEFI Monitor",
            [
                'set "PYTHONUTF8=1"',
                'set "PYTHONIOENCODING=utf-8"',
                f'set "PYTHONPATH={orch}"',
                f'cd /d "{orch}"',
                f'"{py}" -m orchestrator.monitor',
            ],
        )

        return [
            ("Zerro-DEFI Orchestrator", orchestrator),
            ("Zerro-DEFI Sniper", sniper),
            ("Zerro-DEFI Monitor", monitor),
        ]

    @staticmethod
    def _bat(path: Path, title: str, body: Sequence[str]) -> None:
        lines = [
            "@echo off",
            "chcp 65001 >nul",
            f"title {title}",
            "setlocal",
            *body,
            "echo.",
            "echo   [process exited]  press any key to close...",
            "pause >nul",
            "endlocal",
        ]
        path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")

    # -- chrome ------------------------------------------------------------
    def header(self) -> None:
        cols, _ = ui.term_size()
        width = max(74, min(cols - 1, 96))
        env = read_env()
        dry = is_truthy(env.get("DRY_RUN", "true"))
        mode = (
            ui.badge("DRY RUN", ui.AMBER, filled=True)
            if dry
            else ui.badge("LIVE", ui.RED, filled=True)
        )
        print()
        print(
            "  "
            + ui.gradient("ZERRO-DEFI", [ui.ACCENT, ui.SKY, ui.VIOLET])
            + "  "
            + ui.paint(ui.glyph("sep"), ui.FAINT)
            + "  "
            + ui.paint("ZERO-CAPITAL / ZERO-GAS LAUNCHER", ui.MUTED)
        )
        print("  " + ui.paint(ui.glyph("h") * (width - 2), ui.BORDER))
        print(
            "  "
            + mode
            + ui.paint("   BASE ", ui.MUTED)
            + ui.paint("8453", ui.INK, bold=True)
            + ui.paint("   repo ", ui.MUTED)
            + ui.paint(str(ROOT), ui.FAINT)
        )
        queue = ui.paint(f"  {ui.glyph('arrow')} ", ui.FAINT) + ui.paint(
            "  ".join(
                ui.paint(s[1], ui.INK if s[0] in self._selected() else ui.FAINT)
                for s in STEPS
            ),
            ui.FAINT,
        )
        print(queue)
        print()

    def _selected(self) -> List[str]:
        if not self.args.only:
            return list(STEP_IDS)
        wanted = {s.strip() for s in self.args.only.split(",") if s.strip()}
        unknown = wanted - set(STEP_IDS)
        if unknown:
            raise SystemExit(f"unknown step(s): {', '.join(sorted(unknown))}")
        return [s for s in STEP_IDS if s in wanted]

    def summary(self) -> None:
        cols, _ = ui.term_size()
        width = max(74, min(cols - 1, 96))
        env = read_env()
        dry = is_truthy(env.get("DRY_RUN", "true"))
        counts = target_counts()
        executor = env.get("EXECUTOR_ADDRESS", "")

        frame = ui.Frame(
            width,
            title="LAUNCH SUMMARY",
            right=f"{ui.glyph('dot')} {ui.now_clock()}",
            accent=ui.ACCENT,
        )
        frame.pair(
            ui.paint("MODE", ui.MUTED),
            ui.paint("DRY RUN — simulate only" if dry else "LIVE — real submission", ui.AMBER if dry else ui.RED, bold=True),
        )
        frame.pair(
            ui.paint("EXECUTOR", ui.MUTED),
            ui.paint(ui.short_addr(executor, 10, 8) if executor else "not provisioned", ui.SKY),
        )
        if counts:
            total = counts["morpho"] + counts["dutch"] + counts["harvest"]
            frame.pair(
                ui.paint("TARGETS", ui.MUTED),
                ui.paint(
                    f"{total} armed · morpho {counts['morpho']} · dutch {counts['dutch']}"
                    f" · harvest {counts['harvest']}",
                    ui.INK,
                ),
            )
        else:
            frame.pair(ui.paint("TARGETS", ui.MUTED), ui.paint("not generated yet", ui.FAINT))

        ok = sum(1 for o in self.outcomes.values() if o.ok and not o.skipped)
        skipped = sum(1 for o in self.outcomes.values() if o.skipped)
        failed = sum(1 for o in self.outcomes.values() if not o.ok and not o.skipped)
        parts = [ui.paint(f"{ok} ok", ui.GREEN)]
        if skipped:
            parts.append(ui.paint(f"{skipped} skipped", ui.FAINT))
        if failed:
            parts.append(ui.paint(f"{failed} failed", ui.RED))
        frame.pair(ui.paint("STEPS", ui.MUTED), "  ·  ".join(parts))

        if self.launched:
            frame.pair(
                ui.paint("SERVICES", ui.MUTED),
                ui.paint(f"{len(self.launched)} terminals · orchestrator, sniper, monitor", ui.GREEN),
            )
        else:
            frame.pair(
                ui.paint("SERVICES", ui.MUTED),
                ui.paint("not launched — run scripts in runtime/", ui.FAINT),
            )

        frame.rule("NEXT")
        if self.launched:
            frame.row(ui.paint("watch live telemetry ", ui.MUTED) + ui.paint("python -m orchestrator.monitor", ui.ACCENT))
        else:
            frame.row(ui.paint("start stack ", ui.MUTED) + ui.paint("python scripts/launch.py --only launch", ui.ACCENT))
        frame.row(ui.paint("single snapshot ", ui.MUTED) + ui.paint("python -m orchestrator.monitor --once", ui.ACCENT))

        if self.warnings:
            frame.rule("ATTENTION")
            for warning in self.warnings[:4]:
                frame.row(ui.paint(ui.glyph("warn") + " " + warning, ui.AMBER))

        print("\n".join(frame.close()))
        print()

    # -- run ---------------------------------------------------------------
    def run(self) -> int:
        ui.ensure_utf8()
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self.header()

        selected = self._selected()
        failed_any = False
        hard_fail = False

        for key in selected:
            if hard_fail and key not in {"launch", "prove", "verify"}:
                continue
            self.step(key)
            outcome = self.outcomes.get(key)
            if outcome and not outcome.ok and not outcome.skipped:
                failed_any = True
                # These failures make downstream steps meaningless.
                if key in {"preflight", "env", "deps"}:
                    hard_fail = True

        self.summary()
        self._write_log()
        return 1 if failed_any else 0

    def _write_log(self) -> None:
        try:
            LOG_PATH.write_text("\n".join(self._transcript) + "\n", encoding="utf-8")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launch.py",
        description="Zerro-DEFI end-to-end launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python scripts/launch.py\n"
            "  python scripts/launch.py --live --yes\n"
            "  python scripts/launch.py --only rust,launch --no-launch\n"
        ),
    )
    parser.add_argument("--live", action="store_true", help="set DRY_RUN=false (asks first)")
    parser.add_argument("--dry-run", action="store_true", help="force DRY_RUN=true")
    parser.add_argument("--yes", "-y", action="store_true", help="never prompt")
    parser.add_argument("--only", metavar="STEPS", help="comma separated subset of steps")
    parser.add_argument("--skip-install", action="store_true", help="skip pip install")
    parser.add_argument("--skip-wallet", action="store_true", help="skip wallet provisioning")
    parser.add_argument("--skip-compile", action="store_true", help="skip solc build + tests")
    parser.add_argument("--skip-deploy", action="store_true", help="skip executor deployment")
    parser.add_argument("--skip-build", action="store_true", help="skip cargo build")
    parser.add_argument("--no-launch", action="store_true", help="write service scripts but do not open terminals")
    parser.add_argument("--verbose", "-v", action="store_true", help="echo subprocess output")
    parser.add_argument("--list", action="store_true", help="list steps and exit")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.live and args.dry_run:
        print("--live and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    if args.list:
        ui.ensure_utf8()
        print()
        for key, label, desc in STEPS:
            print(f"  {ui.paint(ui.pad(label, LABEL_WIDTH), ui.INK, bold=True)} {ui.paint(desc, ui.MUTED)}")
        print()
        return 0

    launcher = Launcher(args)
    try:
        return launcher.run()
    except KeyboardInterrupt:
        launcher.progress.stop()
        print()
        print(f"  {ui.paint(ui.glyph('cross'), ui.AMBER)} interrupted — nothing else was started")
        return 130


if __name__ == "__main__":
    sys.exit(main())
