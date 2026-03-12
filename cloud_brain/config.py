"""
AetherAI — Configuration
All settings loaded from environment variables.
Copy .env.example -> .env and fill in your values.
"""

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # ── Qwen API (DashScope) ──────────────────────────────────────────────────
    QWEN_API_KEY: str = field(default_factory=lambda: os.getenv("QWEN_API_KEY", ""))
    QWEN_BASE_URL: str = field(
        default_factory=lambda: os.getenv(
            "QWEN_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
    )
    QWEN_MODEL: str = field(
        default_factory=lambda: os.getenv("QWEN_MODEL", "qwen-turbo")
    )

    # ── Security ──────────────────────────────────────────────────────────────
    API_KEY: str = field(default_factory=lambda: os.getenv("AETHER_API_KEY", ""))

    # ── Server ────────────────────────────────────────────────────────────────
    PORT: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    HOST: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))

    # ── Database ──────────────────────────────────────────────────────────────
    DB_PATH: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "../database/aether.db")
    )

    # ── Task settings ─────────────────────────────────────────────────────────
    MAX_STEPS_PER_TASK: int = 10
    STEP_TIMEOUT_SECONDS: int = 60


settings = Settings()
