"""
AetherAI — Configuration  (Stage 6 — Layer 2)

Stage 6 additions
─────────────────
TTS_VOICE — edge-tts voice name for the ESP32 voice agent and browser TTS.
            Default: en-US-AriaNeural (good quality, natural cadence).
            List all voices: python -m edge_tts --list-voices

All Stage 5 fixes retained:
  FIX A  DB_PATH uses absolute path anchored to project root
  FIX B  QWEN_VISION_MODEL added separately from QWEN_MODEL
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

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
    QWEN_VISION_MODEL: str = field(
        default_factory=lambda: os.getenv(
            "QWEN_VISION_MODEL",
            os.getenv("QWEN_MODEL", "qwen-turbo")
        )
    )

    # ── Security ──────────────────────────────────────────────────────────────
    API_KEY: str = field(default_factory=lambda: os.getenv("AETHER_API_KEY", ""))

    # ── Server ────────────────────────────────────────────────────────────────
    PORT: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    HOST: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))

    # ── Database ──────────────────────────────────────────────────────────────
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

    # ── Stage 6: Voice / TTS ──────────────────────────────────────────────────
    TTS_VOICE: str = field(
        default_factory=lambda: os.getenv("TTS_VOICE", "en-US-AriaNeural")
    )


settings = Settings()
