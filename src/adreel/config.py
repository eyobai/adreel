"""Runtime configuration (env vars prefixed ``ADREEL_``, optionally from ``.env``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"
BUILTIN_TEMPLATES_DIR = PACKAGE_DIR / "templates"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ADREEL_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- binaries / limits -------------------------------------------------
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    ffmpeg_threads: int = Field(0, ge=0, description="0 = let ffmpeg decide")
    ffmpeg_timeout_s: int = Field(900, ge=10, description="Hard timeout per ffmpeg call")

    # --- paths ---------------------------------------------------------------
    work_dir: Path = Path(".work")
    fonts_dir: Path = ASSETS_DIR / "fonts"
    face_model_path: Path = ASSETS_DIR / "models" / "blaze_face_full_range.tflite"
    # Extra template directories (JSON list in env, e.g. '["/srv/templates"]').
    # Built-in templates are always loaded; extra dirs may override them by id.
    templates_dirs: list[Path] = Field(default_factory=list)

    # --- rendering -----------------------------------------------------------
    face_tracking: bool = True
    watermark_text: str = "Made with AdReel"

    # --- logging -------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
