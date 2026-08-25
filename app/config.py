from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_owner_ids(value: object) -> tuple[int, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(int(item) for item in value)
    if isinstance(value, str):
        return tuple(int(item.strip()) for item in value.split(",") if item.strip())
    raise ValueError("OWNER_IDS must be a comma-separated list of numeric Telegram user IDs")


OwnerIds = Annotated[tuple[int, ...], BeforeValidator(_parse_owner_ids)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(alias="BOT_TOKEN", min_length=20)
    telegram_api_id: int = Field(alias="TELEGRAM_API_ID", gt=0)
    telegram_api_hash: str = Field(alias="TELEGRAM_API_HASH", min_length=20)
    encryption_key: str = Field(alias="ENCRYPTION_KEY", min_length=32)
    owner_ids: OwnerIds = Field(alias="OWNER_IDS")

    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    database_path: Path = Field(default=Path("./data/mirror.db"), alias="DATABASE_PATH")
    temp_dir: Path = Field(default=Path("./data/tmp"), alias="TEMP_DIR")
    report_dir: Path = Field(default=Path("./data/reports"), alias="REPORT_DIR")
    status_update_seconds: int = Field(default=8, alias="STATUS_UPDATE_SECONDS", ge=5, le=60)
    sync_poll_seconds: int = Field(default=300, alias="SYNC_POLL_SECONDS", ge=60, le=86400)
    max_projects_per_owner: int = Field(default=20, alias="MAX_PROJECTS_PER_OWNER", ge=1, le=100)
    max_upload_retries: int = Field(default=4, alias="MAX_UPLOAD_RETRIES", ge=1, le=10)
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    def prepare_directories(self) -> None:
        for directory in (self.data_dir, self.temp_dir, self.report_dir):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass
