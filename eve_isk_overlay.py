#!/usr/bin/env python3
"""Small always-on-top overlay for EVE Online bounty log income."""

from __future__ import annotations

import codecs
import configparser
import ctypes
import json
import os
import re
import sys
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk


ACTIVE_FILE_SECONDS = 60
POLL_INTERVAL_MS = 1_000
DEFAULT_WINDOW_MINUTES = 10
MAX_WINDOW_MINUTES = 240
DEFAULT_ESS_PERCENT = 100
MIN_ESS_PERCENT = 100
MAX_ESS_PERCENT = 200
IMMEDIATE_PAYOUT_SHARE = 0.60
AFK_THRESHOLD_SECONDS = 120
CONFIG_FILE_ENV = "EVE_ISK_OVERLAY_CONFIG"
CONFIG_FILE_NAME = "config.ini"
HIGH_SCORE_FILE_ENV = "EVE_ISK_OVERLAY_HIGH_SCORES"
HIGH_SCORE_FILE_NAME = "high_scores.json"

BOUNTY_RE = re.compile(
    r"^\[\s*(?P<timestamp>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\]"
    r"\s+\(bounty\).*?"
    r"(?P<amount>\d[\d., '\u00a0]*)\s+ISK\b.*?"
    r"added to next bounty payout",
    re.IGNORECASE,
)
LOG_START_RE = re.compile(r"^(?P<timestamp>\d{8}_\d{6})(?:_|\.txt)")
CHARACTER_ID_RE = re.compile(r"_(?P<character_id>\d+)\.txt$", re.IGNORECASE)
LISTENER_RE = re.compile(
    r"^\s*Listener:\s*(?P<name>.+?)\s*$", re.IGNORECASE | re.MULTILINE
)


@dataclass(frozen=True)
class BountyEvent:
    timestamp: datetime
    amount: int


@dataclass(frozen=True)
class RollingStats:
    total: float
    hourly_rate: float
    active_seconds: float
    afk_seconds: float
    is_afk: bool
    afk_intervals: list[tuple[datetime, datetime]]


@dataclass
class FileState:
    processed_lines: int = 0
    size: int = 0
    mtime_ns: int = 0
    bounty_total: int = 0
    listener: str = ""
    listener_checked: bool = False


def app_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_file_path() -> Path:
    override = os.environ.get(CONFIG_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return app_directory() / CONFIG_FILE_NAME


def high_score_file_path() -> Path:
    override = os.environ.get(HIGH_SCORE_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return app_directory() / HIGH_SCORE_FILE_NAME


def default_high_scores() -> dict[str, object]:
    return {
        "version": 1,
        "all_time": {
            "rolling_10m_isk_per_hour": 0.0,
            "rolling_60m_isk_per_hour": 0.0,
            "session_total_isk": 0.0,
        },
    }


def load_high_scores(path: Path | None = None) -> dict[str, object]:
    path = path or high_score_file_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        data = default_high_scores()
        save_high_scores(data, path)
        return data
    except (OSError, json.JSONDecodeError):
        return default_high_scores()

    if not isinstance(data, dict):
        return default_high_scores()
    all_time = data.setdefault("all_time", {})
    if not isinstance(all_time, dict):
        data["all_time"] = default_high_scores()["all_time"]
        return data

    defaults = default_high_scores()["all_time"]
    for key, value in defaults.items():
        all_time.setdefault(key, value)
    data.setdefault("version", 1)
    return data


def save_high_scores(scores: dict[str, object], path: Path | None = None) -> None:
    path = path or high_score_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(scores, handle, indent=2, sort_keys=True)


def windows_documents_directory() -> Path:
    documents = Path.home() / "Documents"
    if sys.platform == "win32":
        # Respect moved/OneDrive-backed Documents folders configured in Windows.
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            documents = Path(buffer.value)
    return documents


def platform_default_log_directories() -> list[Path]:
    """Return sensible default EVE log paths for the current operating system."""
    home = Path.home()
    if sys.platform == "win32":
        return [windows_documents_directory() / "EVE" / "logs" / "Gamelogs"]

    candidates: list[Path] = []

    def add(path: Path) -> None:
        if path not in candidates:
            candidates.append(path)

    steam_roots = [
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    ]

    for steam_root in steam_roots:
        users_root = (
            steam_root
            / "steamapps"
            / "compatdata"
            / "8500"
            / "pfx"
            / "drive_c"
            / "users"
        )
        if users_root.is_dir():
            for user_dir in users_root.iterdir():
                if user_dir.is_dir():
                    add(user_dir / "Documents" / "EVE" / "logs" / "Gamelogs")
        else:
            add(users_root / "steamuser" / "Documents" / "EVE" / "logs" / "Gamelogs")

    wine_users_root = home / ".wine" / "drive_c" / "users"
    if wine_users_root.is_dir():
        for user_dir in wine_users_root.iterdir():
            if user_dir.is_dir():
                add(user_dir / "Documents" / "EVE" / "logs" / "Gamelogs")
    else:
        add(
            wine_users_root
            / os.environ.get("USER", "steamuser")
            / "Documents"
            / "EVE"
            / "logs"
            / "Gamelogs"
        )

    add(home / "Documents" / "EVE" / "logs" / "Gamelogs")
    existing = [path for path in candidates if path.is_dir()]
    return existing or candidates


def default_log_directory() -> Path:
    return platform_default_log_directories()[0]


def expand_config_path(value: str) -> Path:
    value = value.strip().strip('"').strip("'")
    value = os.path.expandvars(value)
    value = re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        value,
    )
    return Path(value).expanduser()


def split_configured_paths(value: str) -> list[Path]:
    paths: list[Path] = []
    for line in value.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        parts = [part.strip() for part in line.split(";") if part.strip()]
        for part in parts:
            path = expand_config_path(part)
            if path not in paths:
                paths.append(path)
    return paths


def create_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser(interpolation=None)
    config["logs"] = {
        "directories": "\n"
        + "\n".join(str(path) for path in platform_default_log_directories())
    }
    with path.open("w", encoding="utf-8") as handle:
        config.write(handle)


def configured_log_directories(path: Path | None = None) -> list[Path]:
    path = path or config_file_path()
    if not path.exists():
        create_default_config(path)

    config = configparser.ConfigParser(interpolation=None)
    config.read(path, encoding="utf-8")
    raw_directories = config.get("logs", "directories", fallback="")
    if not raw_directories:
        raw_directories = config.get("logs", "directory", fallback="")

    directories = split_configured_paths(raw_directories)
    return directories or platform_default_log_directories()


def parse_bounty_line(line: str) -> BountyEvent | None:
    match = BOUNTY_RE.search(line)
    if not match:
        return None

    timestamp = datetime.strptime(match.group("timestamp"), "%Y.%m.%d %H:%M:%S")
    # ISK amounts in EVE logs are whole numbers with locale-dependent separators.
    digits = re.sub(r"\D", "", match.group("amount"))
    if not digits:
        return None
    return BountyEvent(timestamp=timestamp, amount=int(digits))


def decode_log(raw: bytes) -> str:
    """Decode EVE logs, which may be UTF-8 or UTF-16 depending on the client."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")

    # A high proportion of NUL bytes is a reliable hint for BOM-less UTF-16.
    if raw[:200].count(b"\x00") > 10:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def complete_lines(text: str) -> list[str]:
    pieces = text.splitlines(keepends=True)
    if pieces and not pieces[-1].endswith(("\n", "\r")):
        pieces.pop()
    return [line.rstrip("\r\n") for line in pieces]


def format_million_isk(value: float) -> str:
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f} B ISK/h"
    return f"{value / 1_000_000:.2f} M ISK/h"


def format_short_isk_rate(value: float) -> str:
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f} B/h"
    return f"{value / 1_000_000:.2f} M/h"


def format_short_isk(value: float) -> str:
    if abs(value) >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f} B"
    return f"{value / 1_000_000:.2f} M"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3_600)
    minutes = remainder // 60
    return f"{hours:02d}:{minutes:02d}"


def log_start_time(path: Path) -> datetime | None:
    match = LOG_START_RE.match(path.name)
    if not match:
        return None
    return datetime.strptime(match.group("timestamp"), "%Y%m%d_%H%M%S")


def parse_listener(text: str) -> str | None:
    match = LISTENER_RE.search(text)
    return match.group("name").strip() if match else None


def character_id_from_path(path: Path) -> str | None:
    match = CHARACTER_ID_RE.search(path.name)
    return match.group("character_id") if match else None


def format_character_total(name: str, value: float) -> str:
    if abs(value) >= 1_000_000_000:
        amount = f"{value / 1_000_000_000:.2f} B ISK"
    else:
        amount = f"{value / 1_000_000:.2f} M ISK"
    return f"{name} — {amount}"


def format_session_total(value: float, duration_seconds: float | None = None) -> str:
    if abs(value) >= 1_000_000_000:
        text = f"Session {value / 1_000_000_000:.2f} B ISK"
    else:
        text = f"Session {value / 1_000_000:.2f} M ISK"
    if duration_seconds is not None:
        text += f" · {format_duration(duration_seconds)}"
    return text


def estimate_with_ess(paid_amount: float, ess_percent: int) -> float:
    """Keep the immediate payout fixed and scale only the delayed 40% share."""
    delayed_amount = paid_amount * ((1 - IMMEDIATE_PAYOUT_SHARE) / IMMEDIATE_PAYOUT_SHARE)
    return paid_amount + delayed_amount * (ess_percent / 100)


class IskOverlay(tk.Tk):
    def __init__(self, log_directory: Path | None = None) -> None:
        super().__init__()
        self.config_path = config_file_path()
        self.log_directories = (
            [log_directory]
            if log_directory
            else configured_log_directories(self.config_path)
        )
        self.log_directory = self.log_directories[0]
        self.high_score_path = high_score_file_path()
        self.high_scores = load_high_scores(self.high_score_path)
        self.session_high_10m = 0.0
        self.session_high_60m = 0.0
        self.session_high_total = 0.0
        self.last_bounty_at: datetime | None = None
        self.file_states: dict[Path, FileState] = {}
        self.session_files: set[Path] = set()
        self.session_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.events: deque[BountyEvent] = deque()
        self.rate_history: deque[tuple[datetime, float, bool]] = deque()
        self.last_error: str | None = None
        self.selected_character = "__all__"
        self.character_keys: list[str] = []

        self.title("EVE ISK")
        self.geometry("360x330")
        self.minsize(340, 310)
        self.attributes("-topmost", True)
        self.configure(bg="#10151c")
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.window_minutes = tk.IntVar(value=DEFAULT_WINDOW_MINUTES)
        self.ess_enabled = tk.BooleanVar(value=True)
        self.ess_percent = tk.IntVar(value=DEFAULT_ESS_PERCENT)
        self.rate_text = tk.StringVar(value="0 ISK/h")
        self.session_text = tk.StringVar(value="Session 0.00 M ISK · 00:00")
        self.high_score_text = tk.StringVar(
            value="Session highs 10m 0.00 M/h · 60m 0.00 M/h"
        )
        self.all_time_high_text = tk.StringVar(value="All-time highs loading…")
        self.trend_text = tk.StringVar(value="RATE TREND · 10 MIN")
        self.status_text = tk.StringVar(value="Starting…")
        self._build_ui()
        self.after(100, self.poll_logs)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Panel.TFrame", background="#10151c")
        style.configure(
            "Title.TLabel",
            background="#10151c",
            foreground="#8292a6",
            font=("Segoe UI", 9),
        )
        style.configure(
            "Rate.TLabel",
            background="#10151c",
            foreground="#6ee7a0",
            font=("Segoe UI Semibold", 20),
        )
        style.configure(
            "Status.TLabel",
            background="#10151c",
            foreground="#8292a6",
            font=("Segoe UI", 8),
        )
        style.configure(
            "Session.TLabel",
            background="#10151c",
            foreground="#c5d0dc",
            font=("Segoe UI", 8),
        )
        style.configure(
            "Dark.TSpinbox",
            fieldbackground="#1a222d",
            foreground="#f2f5f8",
            arrowcolor="#8292a6",
            bordercolor="#283545",
        )
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#1a222d",
            background="#1a222d",
            foreground="#f2f5f8",
            arrowcolor="#8292a6",
            bordercolor="#283545",
        )
        style.configure(
            "Dark.TCheckbutton",
            background="#10151c",
            foreground="#c5d0dc",
            font=("Segoe UI", 9),
        )
        style.map(
            "Dark.TCheckbutton",
            background=[("active", "#10151c")],
            foreground=[("active", "#f2f5f8")],
        )

        panel = ttk.Frame(self, style="Panel.TFrame", padding=(16, 12))
        panel.pack(fill="both", expand=True)

        top = ttk.Frame(panel, style="Panel.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="BOUNTY INCOME", style="Title.TLabel").pack(side="left")

        window_box = ttk.Frame(top, style="Panel.TFrame")
        window_box.pack(side="right")
        ttk.Label(window_box, text="Last", style="Title.TLabel").pack(side="left")
        spin = ttk.Spinbox(
            window_box,
            from_=1,
            to=MAX_WINDOW_MINUTES,
            width=4,
            textvariable=self.window_minutes,
            command=self.update_display,
            style="Dark.TSpinbox",
        )
        spin.pack(side="left", padx=4)
        spin.bind("<Return>", lambda _event: self.update_display())
        spin.bind("<FocusOut>", lambda _event: self.update_display())
        ttk.Label(window_box, text="min", style="Title.TLabel").pack(side="left")

        ttk.Label(panel, textvariable=self.rate_text, style="Rate.TLabel").pack(
            anchor="w", pady=(5, 0)
        )
        ttk.Label(
            panel,
            textvariable=self.high_score_text,
            style="Session.TLabel",
        ).pack(anchor="w", pady=(1, 0))
        ttk.Label(
            panel,
            textvariable=self.all_time_high_text,
            style="Status.TLabel",
        ).pack(anchor="w", pady=(0, 0))

        ess_row = ttk.Frame(panel, style="Panel.TFrame")
        ess_row.pack(fill="x", pady=(7, 0))
        ttk.Checkbutton(
            ess_row,
            text="ESS estimate",
            variable=self.ess_enabled,
            command=self.update_display,
            style="Dark.TCheckbutton",
        ).pack(side="left")
        ttk.Label(ess_row, text="at", style="Title.TLabel").pack(side="left", padx=(8, 4))
        ess_spin = ttk.Spinbox(
            ess_row,
            from_=MIN_ESS_PERCENT,
            to=MAX_ESS_PERCENT,
            width=4,
            textvariable=self.ess_percent,
            command=self.update_display,
            style="Dark.TSpinbox",
        )
        ess_spin.pack(side="left")
        ess_spin.bind("<Return>", lambda _event: self.update_display())
        ess_spin.bind("<FocusOut>", lambda _event: self.update_display())
        ttk.Label(ess_row, text="%", style="Title.TLabel").pack(side="left", padx=(3, 0))

        ttk.Label(panel, textvariable=self.trend_text, style="Status.TLabel").pack(
            anchor="w", pady=(9, 2)
        )
        self.trend_canvas = tk.Canvas(
            panel,
            height=55,
            background="#0b1016",
            highlightthickness=1,
            highlightbackground="#283545",
        )
        self.trend_canvas.pack(fill="x")
        self.trend_canvas.bind("<Configure>", lambda _event: self._draw_trend())

        ttk.Separator(panel, orient="horizontal").pack(fill="x", pady=(10, 7))
        status_row = ttk.Frame(panel, style="Panel.TFrame")
        status_row.pack(fill="x")
        ttk.Label(
            status_row,
            textvariable=self.status_text,
            style="Status.TLabel",
            anchor="w",
        ).pack(side="left")
        ttk.Label(
            status_row,
            textvariable=self.session_text,
            style="Session.TLabel",
            anchor="e",
        ).pack(side="right")

        self.character_combo = ttk.Combobox(
            panel,
            state="readonly",
            values=("Waiting for character logs…",),
            style="Dark.TCombobox",
            font=("Segoe UI", 8),
            height=8,
        )
        self.character_combo.current(0)
        self.character_combo.pack(fill="x", pady=(8, 0))
        self.character_combo.bind(
            "<<ComboboxSelected>>", self._on_character_selected
        )

    def _window_size(self) -> int:
        try:
            value = int(self.window_minutes.get())
        except (tk.TclError, ValueError):
            value = DEFAULT_WINDOW_MINUTES
        return max(1, min(value, MAX_WINDOW_MINUTES))

    def _ess_percentage(self) -> int:
        try:
            value = int(self.ess_percent.get())
        except (tk.TclError, ValueError):
            value = DEFAULT_ESS_PERCENT
        value = max(MIN_ESS_PERCENT, min(value, MAX_ESS_PERCENT))
        self.ess_percent.set(value)
        return value

    def poll_logs(self) -> None:
        try:
            self._read_active_files()
            self.last_error = None
        except OSError as exc:
            self.last_error = str(exc)
            self.status_text.set(f"Log error: {self.last_error}")

        self.update_display(record_history=True)
        if self.winfo_exists():
            self.after(POLL_INTERVAL_MS, self.poll_logs)

    def _read_active_files(self) -> None:
        existing_directories = [path for path in self.log_directories if path.is_dir()]
        if not existing_directories:
            if len(self.log_directories) == 1:
                self.status_text.set(f"Waiting for {self.log_directories[0]}")
            else:
                self.status_text.set(f"Waiting for log paths in {self.config_path.name}")
            return

        now_epoch = datetime.now().timestamp()
        active_files: list[Path] = []

        for log_directory in existing_directories:
            for path in log_directory.iterdir():
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                age_seconds = now_epoch - stat.st_mtime
                # Load enough history to populate any selectable rolling window.
                # "Active" remains the stricter 60-second status indicator.
                if age_seconds <= MAX_WINDOW_MINUTES * 60:
                    self._read_new_lines(path, stat.st_size, stat.st_mtime_ns)
                if age_seconds <= ACTIVE_FILE_SECONDS:
                    active_files.append(path)
                    if path not in self.session_files:
                        self.session_files.add(path)
                        file_started_at = log_start_time(path)
                        if file_started_at is not None:
                            self.session_started_at = min(
                                self.session_started_at, file_started_at
                            )

        if self.last_error:
            self.status_text.set(f"Log error: {self.last_error}")
        elif active_files:
            noun = "file" if len(active_files) == 1 else "files"
            self.status_text.set(f"Watching {len(active_files)} active {noun}")
        elif self.last_bounty_at is not None:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            afk_start = self.last_bounty_at + timedelta(seconds=AFK_THRESHOLD_SECONDS)
            if now_utc > afk_start:
                self.status_text.set(
                    f"AFK {format_duration((now_utc - afk_start).total_seconds())}"
                )
            else:
                self.status_text.set("Waiting for an active game log…")
        else:
            self.status_text.set("Waiting for an active game log…")

    def _read_new_lines(self, path: Path, size: int, mtime_ns: int) -> None:
        state = self.file_states.setdefault(path, FileState())
        if size == state.size and mtime_ns == state.mtime_ns:
            return

        raw = path.read_bytes()
        text = decode_log(raw)
        lines = complete_lines(text)
        if not state.listener_checked:
            state.listener = (
                parse_listener(text)
                or self._find_listener_in_related_log(path)
                or path.stem
            )
            state.listener_checked = True

        # A replaced or truncated log starts again at line zero.
        if size < state.size or len(lines) < state.processed_lines:
            state.processed_lines = 0
            state.bounty_total = 0

        for line in lines[state.processed_lines :]:
            event = parse_bounty_line(line)
            if event is not None:
                self.events.append(event)
                state.bounty_total += event.amount
                if self.last_bounty_at is None or event.timestamp > self.last_bounty_at:
                    self.last_bounty_at = event.timestamp

        state.processed_lines = len(lines)
        state.size = size
        state.mtime_ns = mtime_ns

    def _find_listener_in_related_log(self, path: Path) -> str | None:
        character_id = character_id_from_path(path)
        if character_id is None:
            return None
        related = sorted(
            path.parent.glob(f"*_{character_id}.txt"),
            key=lambda candidate: candidate.stat().st_mtime,
            reverse=True,
        )
        for candidate in related:
            if candidate == path:
                continue
            try:
                listener = parse_listener(decode_log(candidate.read_bytes()))
            except OSError:
                continue
            if listener:
                return listener
        return None

    def _afk_intervals(
        self, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        if end <= start:
            return []

        threshold = timedelta(seconds=AFK_THRESHOLD_SECONDS)
        timestamps = sorted(
            event.timestamp
            for event in self.events
            if event.timestamp <= end + timedelta(seconds=5)
        )

        previous: datetime | None = None
        intervals: list[tuple[datetime, datetime]] = []
        for timestamp in timestamps:
            if timestamp < start:
                previous = timestamp
                continue
            if previous is not None:
                afk_start = previous + threshold
                afk_end = timestamp
                if afk_end > afk_start:
                    clipped_start = max(start, afk_start)
                    clipped_end = min(end, afk_end)
                    if clipped_end > clipped_start:
                        intervals.append((clipped_start, clipped_end))
            previous = timestamp

        if previous is None and self.last_bounty_at is not None:
            previous = self.last_bounty_at

        if previous is not None:
            afk_start = previous + threshold
            if end > afk_start:
                clipped_start = max(start, afk_start)
                if end > clipped_start:
                    intervals.append((clipped_start, end))

        return intervals

    @staticmethod
    def _interval_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
        return sum((end - start).total_seconds() for start, end in intervals)

    def _rolling_stats(self, minutes: int, now_utc: datetime) -> RollingStats:
        cutoff = now_utc - timedelta(minutes=minutes)
        raw_total = sum(
            event.amount
            for event in self.events
            if cutoff <= event.timestamp <= now_utc + timedelta(seconds=5)
        )
        total = float(raw_total)
        if self.ess_enabled.get():
            total = estimate_with_ess(total, self._ess_percentage())

        afk_intervals = self._afk_intervals(cutoff, now_utc)
        afk_seconds = self._interval_seconds(afk_intervals)
        active_seconds = max(1.0, minutes * 60 - afk_seconds)
        hourly_rate = total * (3600 / active_seconds)
        is_afk = any(start <= now_utc <= end for start, end in afk_intervals)
        return RollingStats(
            total=total,
            hourly_rate=hourly_rate,
            active_seconds=active_seconds,
            afk_seconds=afk_seconds,
            is_afk=is_afk,
            afk_intervals=afk_intervals,
        )

    def _update_high_scores(
        self,
        rate_10m: float,
        rate_60m: float,
        session_total: float,
    ) -> None:
        self.session_high_10m = max(self.session_high_10m, rate_10m)
        self.session_high_60m = max(self.session_high_60m, rate_60m)
        self.session_high_total = max(self.session_high_total, session_total)

        all_time = self.high_scores.setdefault("all_time", {})
        if not isinstance(all_time, dict):
            all_time = {}
            self.high_scores["all_time"] = all_time

        changed = False
        high_values = {
            "rolling_10m_isk_per_hour": rate_10m,
            "rolling_60m_isk_per_hour": rate_60m,
            "session_total_isk": session_total,
        }
        for key, value in high_values.items():
            current = float(all_time.get(key, 0) or 0)
            if value > current:
                all_time[key] = value
                changed = True

        self.high_score_text.set(
            "Session highs "
            f"10m {format_short_isk_rate(self.session_high_10m)} · "
            f"60m {format_short_isk_rate(self.session_high_60m)} · "
            f"total {format_short_isk(self.session_high_total)}"
        )
        self.all_time_high_text.set(
            "All-time "
            "10m "
            f"{format_short_isk_rate(float(all_time.get('rolling_10m_isk_per_hour', 0) or 0))} · "
            "60m "
            f"{format_short_isk_rate(float(all_time.get('rolling_60m_isk_per_hour', 0) or 0))} · "
            f"session {format_short_isk(float(all_time.get('session_total_isk', 0) or 0))}"
        )

        if changed:
            save_high_scores(self.high_scores, self.high_score_path)

    def update_display(self, record_history: bool = False) -> None:
        minutes = self._window_size()
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        # Keep a little extra history so increasing the window remains useful.
        history_cutoff = now_utc - timedelta(minutes=MAX_WINDOW_MINUTES)
        if self.events:
            self.events = deque(
                event for event in self.events if event.timestamp >= history_cutoff
            )

        rolling_stats = self._rolling_stats(minutes, now_utc)
        self.rate_text.set(format_million_isk(rolling_stats.hourly_rate))
        session_total = sum(
            self.file_states[path].bounty_total
            for path in self.session_files
            if path in self.file_states
        )
        session_duration = (now_utc - self.session_started_at).total_seconds()
        session_afk_seconds = self._interval_seconds(
            self._afk_intervals(self.session_started_at, now_utc)
        )
        session_label = format_session_total(session_total, session_duration)
        if session_afk_seconds >= 60:
            session_label += f" · AFK {format_duration(session_afk_seconds)}"
        self.session_text.set(session_label)
        self._update_character_dropdown(session_total)

        stats_10m = self._rolling_stats(10, now_utc)
        stats_60m = self._rolling_stats(60, now_utc)
        self._update_high_scores(
            stats_10m.hourly_rate,
            stats_60m.hourly_rate,
            session_total,
        )
        self.trend_text.set(f"RATE TREND · {minutes} MIN")

        if record_history:
            self.rate_history.append(
                (now_utc, rolling_stats.hourly_rate, rolling_stats.is_afk)
            )
        history_cutoff = now_utc - timedelta(minutes=MAX_WINDOW_MINUTES)
        self.rate_history = deque(
            point for point in self.rate_history if point[0] >= history_cutoff
        )
        self._draw_trend(now_utc, minutes)

    def _on_character_selected(self, _event: tk.Event) -> None:
        index = self.character_combo.current()
        if 0 <= index < len(self.character_keys):
            self.selected_character = self.character_keys[index]

    def _update_character_dropdown(self, session_total: float) -> None:
        totals_by_listener: dict[str, int] = {}
        for path in self.session_files:
            state = self.file_states.get(path)
            if state is None:
                continue
            listener = state.listener or path.stem
            totals_by_listener[listener] = (
                totals_by_listener.get(listener, 0) + state.bounty_total
            )

        rows = [("__all__", format_character_total("All toons", session_total))]
        rows.extend(
            (listener, format_character_total(listener, total))
            for listener, total in sorted(
                totals_by_listener.items(), key=lambda item: item[0].casefold()
            )
        )

        self.character_keys = [key for key, _label in rows]
        self.character_combo.configure(values=[label for _key, label in rows])
        try:
            selected_index = self.character_keys.index(self.selected_character)
        except ValueError:
            selected_index = 0
            self.selected_character = "__all__"
        self.character_combo.current(selected_index)

    def _draw_trend(
        self, now_utc: datetime | None = None, minutes: int | None = None
    ) -> None:
        canvas = self.trend_canvas
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 4 or height < 4:
            return

        now_utc = now_utc or datetime.now(timezone.utc).replace(tzinfo=None)
        minutes = minutes or self._window_size()
        cutoff = now_utc - timedelta(minutes=minutes)
        points = [
            (when, value, is_afk)
            for when, value, is_afk in self.rate_history
            if when >= cutoff
        ]

        padding = 5
        canvas.create_line(
            padding,
            height - padding,
            width - padding,
            height - padding,
            fill="#1d2a34",
        )
        if not points:
            return

        values = [value for _, value, _is_afk in points]
        low, high = min(values), max(values)
        if high == low:
            low = 0
            high = max(high, 1)

        span_seconds = max(minutes * 60, 1)
        coordinates: list[tuple[float, float, bool]] = []
        for when, value, is_afk in points:
            elapsed = (when - cutoff).total_seconds()
            x = padding + (elapsed / span_seconds) * (width - 2 * padding)
            y = height - padding - ((value - low) / (high - low)) * (
                height - 2 * padding
            )
            coordinates.append((x, y, is_afk))

        if len(points) == 1:
            x, y, is_afk = coordinates[0]
            color = "#f59e0b" if is_afk else "#6ee7a0"
            canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")
        else:
            for previous, current in zip(coordinates, coordinates[1:]):
                x1, y1, was_afk = previous
                x2, y2, is_afk = current
                color = "#f59e0b" if was_afk or is_afk else "#6ee7a0"
                canvas.create_line(x1, y1, x2, y2, fill=color, width=2)


def main() -> None:
    try:
        app = IskOverlay()
        app.mainloop()
    except tk.TclError as exc:
        messagebox.showerror("EVE ISK Overlay", f"Could not start the window:\n{exc}")


if __name__ == "__main__":
    main()
