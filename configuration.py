"""Environment-backed local configuration; application code can replace providers later."""
from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    media_root: Path
    user_identity: str


def load_settings() -> Settings:
    root = Path(__file__).resolve().parent
    return Settings(
        database_path=Path(os.environ.get("PIPELINE_DATABASE", root / "output" / "pipeline.sqlite3")).expanduser(),
        media_root=Path(os.environ.get("PIPELINE_MEDIA_ROOT", root / "output")).expanduser(),
        user_identity=os.environ.get("PIPELINE_USER", "local-user"),
    )
