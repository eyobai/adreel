"""Runtime configuration (env vars prefixed ``ADREEL_``, optionally from ``.env``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from adreel.ai.costs import Prices

PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"
BUILTIN_TEMPLATES_DIR = PACKAGE_DIR / "templates"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ADREEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",  # e.g. ADREEL_PRICES__TTS_AZURE_PER_MCHAR=15
        extra="ignore",
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

    # --- AI providers (Phase 2) -----------------------------------------------
    # "fake"/"heuristic" providers need no keys: for tests, demos and outages.
    transcriber: Literal["elevenlabs", "fake"] = "elevenlabs"
    planner: Literal["gemini", "heuristic"] = "gemini"
    tts_standard: Literal["azure", "fake"] = "azure"
    tts_premium: Literal["elevenlabs", "azure", "fake"] = "elevenlabs"
    http_timeout_s: float = Field(120.0, gt=0)

    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    elevenlabs_stt_model: str = "scribe_v1"
    elevenlabs_tts_model: str = "eleven_v3"
    elevenlabs_voice_id: str | None = None  # required for premium voiceover
    elevenlabs_tts_language: str | None = "am"  # sent as language_code when set

    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-flash-latest"
    gemini_media_resolution: Literal["low", "medium", "high"] = "low"
    gemini_thinking_budget: int | None = Field(None, ge=0, description="None = model default")
    gemini_temperature: float = Field(0.4, ge=0, le=2)
    gemini_inline_max_mb: float = Field(15.0, gt=0, description="Bigger media goes via Files API")
    plan_attempts: int = Field(3, ge=1, le=5)

    azure_speech_key: SecretStr | None = None
    azure_speech_region: str = "eastus"
    azure_voice: str = "am-ET-MekdesNeural"  # or am-ET-AmehaNeural

    language: Literal["am", "en"] = "am"
    prices: Prices = Field(default_factory=Prices)

    # --- Telegram bot, queue, database, storage (Phase 3) ----------------------
    telegram_bot_token: SecretStr | None = None
    # Self-hosted Bot API server (local mode lifts the 20 MB download / 50 MB upload caps).
    telegram_api_base: str = "https://api.telegram.org"
    telegram_local_mode: bool = False
    # Where the local Bot API server's files are visible to the worker (shared volume).
    # Local-mode getFile returns absolute paths under the server's --dir; map them here.
    telegram_local_files_dir: Path | None = None
    telegram_server_files_dir: str = "/var/lib/telegram-bot-api"

    database_url: str = "sqlite:///adreel.db"
    redis_url: str = "redis://localhost:6379/0"
    # "rq": Redis + separate worker process (production). "inline": jobs run on a background
    # thread inside the bot process: no Redis needed (local testing, Windows).
    queue_backend: Literal["rq", "inline"] = "rq"
    queue_name: str = "adreel"
    job_timeout_s: int = Field(1200, ge=60, description="Hard limit per worker job")
    job_retries: int = Field(2, ge=0, le=5, description="Retries for transient failures")

    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: Path = Path("storage")
    s3_bucket: str | None = None
    s3_prefix: str = ""
    s3_endpoint_url: str | None = None  # e.g. Cloudflare R2, MinIO
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None

    # Product limits
    max_clips: int = Field(5, ge=1, le=10)
    max_clip_seconds: int = Field(180, ge=5)
    max_total_seconds: int = Field(300, ge=10)
    max_redos: int = Field(2, ge=0, description="Free re-renders (AI re-plan) per video")
    max_caption_fixes: int = Field(10, ge=0, description="Caption-fix re-renders per video")
    rate_limit_messages_per_min: int = Field(30, ge=1)
    rate_limit_videos_per_hour: int = Field(6, ge=1)
    status_update_interval_s: float = Field(3.0, ge=0.5)
    admin_telegram_ids: list[int] = Field(default_factory=list)

    # --- logging -------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
