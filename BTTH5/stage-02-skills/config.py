"""Cấu hình project: hằng số hiển thị, giới hạn thực thi và settings đọc từ .env."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

import paths

PROJECT_NAME = "stage-02-skills"
APP_TITLE = "Stage 02: Skills"
CAPABILITY_TEXT = "Tools: list_files, read_file, write_file. Skills: catalog tự phát hiện từ workspace/skills, model tự load SKILL.md bằng read_file. Không chạy lệnh."

# Giới hạn số lần gọi model trong một lượt chat (ModelCallLimitMiddleware).
MODEL_CALL_LIMIT = 8
# Giới hạn số tool calls trong một lượt chat (ToolCallLimitMiddleware).
TOOL_CALL_LIMIT = 20
# Giới hạn số bước của LangGraph (recursion limit), không phải số tool calls.
RECURSION_LIMIT = 50
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass(frozen=True)
class Settings:
    api_keys: tuple[str, ...]
    model_name: str
    base_url: str | None

    @property
    def api_key(self) -> str:
        """First key, used for the initial model attempt."""
        return self.api_keys[0]


def is_rate_limited(exc: Exception) -> bool:
    """Rotate keys only for a provider HTTP 429 response."""
    response = getattr(exc, "response", None)
    return getattr(exc, "status_code", None) == 429 or getattr(response, "status_code", None) == 429


def load_settings() -> tuple[Settings | None, list[str]]:
    """Đọc .env của project này. Trả (settings, []) hoặc (None, danh sách biến còn thiếu)."""
    load_dotenv(paths.ENV_PATH, override=False)
    api_keys = tuple(key.strip() for key in os.getenv("OPENROUTER_API_KEY", "").split(",") if key.strip())
    model_name = os.getenv("MODEL_NAME", "").strip()
    base_url = os.getenv("OPENROUTER_BASE_URL", "").strip() or DEFAULT_OPENROUTER_BASE_URL
    missing = [name for name, value in (("OPENROUTER_API_KEY", api_keys), ("MODEL_NAME", model_name)) if not value]
    if missing:
        return None, missing
    return Settings(api_keys=api_keys, model_name=model_name, base_url=base_url), []
