"""Portable local scheduler contract plus macOS launchd configuration helpers.

The engine is invoked as a subprocess through the stable research CLI boundary.
No scheduler is installed or started by importing this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import plistlib
import subprocess
import sys

DEFAULT_CONFIG = Path.home() / ".config" / "universal-video-pipeline" / "research-schedule.json"
LABEL = "com.universal-video-pipeline.research"


@dataclass(frozen=True)
class ScheduleConfig:
    daily_time: str = "08:00"
    timezone: str = "Asia/Kolkata"
    categories: tuple[str, ...] = ("technology_ai", "business_economy", "science", "entertainment")
    coverage: str = "india_and_global"
    paused: bool = False


def validate_config(config: ScheduleConfig) -> ScheduleConfig:
    try:
        hour, minute = map(int, config.daily_time.split(":"))
        time(hour, minute)
    except (ValueError, TypeError):
        raise ValueError("daily_time must use 24-hour HH:MM format") from None
    try:
        ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone must be a valid IANA timezone") from None
    if config.coverage not in {"india", "global", "india_and_global"}:
        raise ValueError("coverage must be india, global, or india_and_global")
    if not config.categories or any(not c.strip() for c in config.categories):
        raise ValueError("at least one non-empty category is required")
    return config


def load_config(path: str | Path = DEFAULT_CONFIG) -> ScheduleConfig:
    p = Path(path)
    if not p.exists():
        return validate_config(ScheduleConfig())
    raw = json.loads(p.read_text())
    return validate_config(ScheduleConfig(raw.get("daily_time", "08:00"), raw.get("timezone", "Asia/Kolkata"),
        tuple(raw.get("categories", ScheduleConfig().categories)), raw.get("coverage", "india_and_global"),
        bool(raw.get("paused", False))))


def save_config(config: ScheduleConfig, path: str | Path = DEFAULT_CONFIG) -> None:
    validate_config(config)
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"daily_time": config.daily_time, "timezone": config.timezone,
        "categories": list(config.categories), "coverage": config.coverage, "paused": config.paused}, indent=2) + "\n")


def next_run(config: ScheduleConfig, now: datetime | None = None) -> datetime:
    zone = ZoneInfo(config.timezone)
    local_now = (now or datetime.now(zone)).astimezone(zone)
    hour, minute = map(int, config.daily_time.split(":"))
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def plist(config_path: str | Path, *, python: str = sys.executable,
          project_dir: str | Path = Path(__file__).resolve().parent) -> dict:
    project = str(Path(project_dir).resolve())
    return {"Label": LABEL, "ProgramArguments": [python, str(Path(project) / "research_cli.py"),
            "schedule-tick", "--config", str(Path(config_path).expanduser())],
            "WorkingDirectory": project, "StartInterval": 60,
            "StandardOutPath": str(Path(project) / "output" / "research-scheduler.log"),
            "StandardErrorPath": str(Path(project) / "output" / "research-scheduler-error.log"),
            "RunAtLoad": False, "KeepAlive": False}


def write_plist(destination: str | Path, config: ScheduleConfig, config_path: str | Path,
                *, project_dir: str | Path = Path(__file__).resolve().parent) -> Path:
    dest = Path(destination).expanduser(); dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as stream: plistlib.dump(plist(config_path, project_dir=project_dir), stream)
    return dest


def install_launchd(plist_path: str | Path, *, launch_dir: str | Path | None = None) -> Path:
    """Explicit action only: copy a reviewed plist into the user's LaunchAgents."""
    source = Path(plist_path).expanduser()
    destination = Path(launch_dir or (Path.home() / "Library" / "LaunchAgents")) / f"{LABEL}.plist"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    subprocess.run(["launchctl", "load", str(destination)], check=True, capture_output=True)
    return destination


def remove_launchd(*, launch_dir: str | Path | None = None) -> bool:
    destination = Path(launch_dir or (Path.home() / "Library" / "LaunchAgents")) / f"{LABEL}.plist"
    if not destination.exists(): return False
    subprocess.run(["launchctl", "unload", str(destination)], check=False, capture_output=True)
    destination.unlink()
    return True


class LocalScheduler:
    """Simple portable due-run poller. launchd and future schedulers can call run_due()."""
    def __init__(self, config: ScheduleConfig, *, db_path=None, lock_path=None):
        self.config = validate_config(config)
        self.db_path = db_path
        import pipeline_store
        self.lock_path = Path(lock_path or (Path(self.db_path or pipeline_store.DEFAULT_DATABASE).parent / "research.lock"))

    def run_due(self, now: datetime | None = None) -> bool:
        if self.config.paused: return False
        now = now or datetime.now(ZoneInfo(self.config.timezone))
        local_now = now.astimezone(ZoneInfo(self.config.timezone))
        hour, minute = map(int, self.config.daily_time.split(":"))
        if (local_now.hour, local_now.minute) < (hour, minute): return False
        import pipeline_store
        db = pipeline_store.connect(self.db_path or pipeline_store.DEFAULT_DATABASE)
        try:
            for row in db.execute("SELECT started_at,status FROM research_job_runs ORDER BY started_at DESC LIMIT 20"):
                try: prior = datetime.fromisoformat(row["started_at"]).astimezone(ZoneInfo(self.config.timezone))
                except ValueError: continue
                if prior.date() == local_now.date() and row["status"] == "completed": return False
        finally: db.close()
        from research_cli import run_research
        return run_research(config=self.config, db_path=self.db_path, lock_path=self.lock_path, send=True)
