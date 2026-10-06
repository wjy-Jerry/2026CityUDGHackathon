from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


class Settings(BaseModel):
    """Runtime configuration loaded from environment variables."""

    openai_api_key: str | None = None
    openai_base_url: str | None = None
    design_model: str | None = None
    code_model: str | None = None
    test_model: str | None = None
    app_env: str = "dev"
    max_retries: int = 2
    llm_strict: bool = False

    @property
    def has_openai_key(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.strip())


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache
def get_settings() -> Settings:
    max_retries_raw = os.getenv("MAX_RETRIES", "2")
    try:
        max_retries = int(max_retries_raw)
    except ValueError:
        max_retries = 2
    openai_api_key = _env_value("OPENAI_API_KEY")
    llm_strict = _env_bool("LLM_STRICT", default=bool(openai_api_key))
    return Settings(
        openai_api_key=openai_api_key,
        openai_base_url=_env_value("OPENAI_BASE_URL"),
        design_model=_env_value("DESIGN_MODEL"),
        code_model=_env_value("CODE_MODEL"),
        test_model=_env_value("TEST_MODEL"),
        app_env=os.getenv("APP_ENV", "dev"),
        max_retries=max(0, max_retries),
        llm_strict=llm_strict,
    )
