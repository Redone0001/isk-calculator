"""Small always-on-top overlay for EVE Online bounty log income."""

from __future__ import annotations

import codecs
import ctypes
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


@dataclass
class FileState:
    processed_lines: int = 0
    size: int = 0
    mtime_ns: int = 0
    bounty_total: int = 0
    listener: str = ""
    listener_checked: bool = False


def default_log_directory() -> Path:
    documents = Path.home() / "Documents"
    if sys.platform == "win32":
        # Respect moved/OneDrive-backed Documents folders configured in Windows.
        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0:
            documents = Path(buffer.value)
    return documents / "EVE" / "logs" / "Gamelogs"


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
        self.log_directory = log_directory or default_log_directory()
        self.file_states: dict[Path, FileState] = {}
        self.session_files: set[Path] = set()
        self.session_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.events: deque[BountyEvent] = deque()
        self.rate_history: deque[tuple[datetime, float]] = deque()
        self.last_error: str | None = None
        self.selected_character = "__all__"
        self.character_keys: list[str] = []

        self.title("EVE ISK")
        self.geometry("350x285")
        self.minsize(330, 265)
        self.attributes("-topmost", True)
        self.configure(bg="#10151c")
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.window_minutes = tk.IntVar(value=DEFAULT_WINDOW_MINUTES)
        self.ess_enabled = tk.BooleanVar(value=True)
        self.ess_percent = tk.IntVar(value=DEFAULT_ESS_PERCENT)
        self.rate_text = tk.StringVar(value="0 ISK/h")
        self.session_text = tk.StringVar(value="Session 0.00 M ISK · 00:00")
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
        if not self.log_directory.is_dir():
            self.status_text.set(f"Waiting for {self.log_directory}")
            return

        now_epoch = datetime.now().timestamp()
        active_files: list[Path] = []

        for path in self.log_directory.iterdir():
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

        state.processed_lines = len(lines)
        state.size = size
        state.mtime_ns = mtime_ns

    def _find_listener_in_related_log(self, path: Path) -> str | None:
        character_id = character_id_from_path(path)
        if character_id is None:
            return None
        related = sorted(
            self.log_directory.glob(f"*_{character_id}.txt"),
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

    def update_display(self, record_history: bool = False) -> None:
        minutes = self._window_size()
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = now_utc - timedelta(minutes=minutes)

        # Keep a little extra history so increasing the window remains useful.
        history_cutoff = now_utc - timedelta(minutes=MAX_WINDOW_MINUTES)
        if self.events:
            self.events = deque(event for event in self.events if event.timestamp >= history_cutoff)

        total = sum(
            event.amount
            for event in self.events
            if cutoff <= event.timestamp <= now_utc + timedelta(seconds=5)
        )
        if self.ess_enabled.get():
            total = estimate_with_ess(total, self._ess_percentage())

        hourly_rate = total * (60 / minutes)
        self.rate_text.set(format_million_isk(hourly_rate))
        session_total = sum(
            self.file_states[path].bounty_total
            for path in self.session_files
            if path in self.file_states
        )
        session_duration = (now_utc - self.session_started_at).total_seconds()
        self.session_text.set(format_session_total(session_total, session_duration))
        self._update_character_dropdown(session_total)
        self.trend_text.set(f"RATE TREND · {minutes} MIN")

        if record_history:
            self.rate_history.append((now_utc, hourly_rate))
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
        points = [(when, value) for when, value in self.rate_history if when >= cutoff]

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

        values = [value for _, value in points]
        low, high = min(values), max(values)
        if high == low:
            low = 0
            high = max(high, 1)

        span_seconds = max(minutes * 60, 1)
        coordinates: list[float] = []
        for when, value in points:
            elapsed = (when - cutoff).total_seconds()
            x = padding + (elapsed / span_seconds) * (width - 2 * padding)
            y = height - padding - ((value - low) / (high - low)) * (
                height - 2 * padding
            )
            coordinates.extend((x, y))

        if len(points) == 1:
            x, y = coordinates
            canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#6ee7a0", outline="")
        else:
            canvas.create_line(
                *coordinates,
                fill="#6ee7a0",
                width=2,
                smooth=True,
                splinesteps=12,
            )


def main() -> None:
    try:
        app = IskOverlay()
        app.mainloop()
    except tk.TclError as exc:
        messagebox.showerror("EVE ISK Overlay", f"Could not start the window:\n{exc}")


if __name__ == "__main__":
    main()
