from functools import lru_cache
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


class Settings(BaseModel):
    # Final judging should use `gemini`. `demo` exists only for offline testing.
    llm_mode: str = os.getenv("LLM_MODE", "gemini").strip().lower()

    # Multi-key support. Put keys in GEMINI_API_KEYS separated by commas.
    # GEMINI_API_KEY remains supported for one-key setups.
    gemini_api_keys_raw: str = os.getenv("GEMINI_API_KEYS", "").strip()
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()

    gemini_model: str = os.getenv(
        "GEMINI_MODEL",
        "gemini-3.5-flash-lite",
    ).strip()

    gemini_base_url: str = os.getenv(
        "GEMINI_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta",
    ).strip().rstrip("/")

    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "10"))
    llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "1"))
    gemini_max_key_attempts: int = int(os.getenv("GEMINI_MAX_KEY_ATTEMPTS", "5"))
    port: int = int(os.getenv("PORT", "1337"))

    @property
    def gemini_api_keys(self) -> list[str]:
        values: list[str] = []

        if self.gemini_api_keys_raw:
            # Support comma, semicolon or newline separated values.
            normalized = (
                self.gemini_api_keys_raw
                .replace(";", ",")
                .replace("\r", ",")
                .replace("\n", ",")
            )
            values.extend(part.strip() for part in normalized.split(","))

        if self.gemini_api_key:
            values.append(self.gemini_api_key)

        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value and value not in seen:
                seen.add(value)
                output.append(value)

        return output


@lru_cache
def get_settings() -> Settings:
    return Settings()
