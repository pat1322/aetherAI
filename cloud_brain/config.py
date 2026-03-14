"""
AetherAI — Configuration
All settings loaded from environment variables.
Copy .env.example -> .env and fill in your values.

Changes
───────
FIX A  DB_PATH now uses an absolute path anchored to this file's location
       so the database is always created in the correct place regardless of
       the process working directory (fixes relative-path fragility on Railway).

FIX B  Added QWEN_VISION_MODEL — the model used for vision-loop screenshot
       analysis. Set this to a vision-capable model like qwen-vl-plus or
       qwen-vl-max in Railway env vars. Defaults to QWEN_MODEL (text-only
       fallback) if not set.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

# Absolute path to the project root (parent of cloud_brain/)
_PROJECT_ROOT = Path(__file__).parent.parent


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
    # FIX B: separate vision model (qwen-vl-plus recommended for vision loops)
    QWEN_VISION_MODEL: str = field(
        default_factory=lambda: os.getenv(
            "QWEN_VISION_MODEL",
            os.getenv("QWEN_MODEL", "qwen-turbo")   # falls back to text model
        )
    )

    # ── Security ──────────────────────────────────────────────────────────────
    API_KEY: str = field(default_factory=lambda: os.getenv("AETHER_API_KEY", ""))

    # ── Server ────────────────────────────────────────────────────────────────
    PORT: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    HOST: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))

    # ── Database (FIX A: absolute path anchored to project root) ──────────────
    DB_PATH: str = field(
        default_factory=lambda: os.getenv(
            "DB_PATH",
            str(_PROJECT_ROOT / "database" / "aether.db")
        )
    )

    # ── Task settings ─────────────────────────────────────────────────────────
    MAX_STEPS_PER_TASK: int = 10
    STEP_TIMEOUT_SECONDS: int = 60

    TASK_RETENTION_DAYS: int = field(
        default_factory=lambda: int(os.getenv("TASK_RETENTION_DAYS", "30"))
    )

settings = Settings()
