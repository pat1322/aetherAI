"""
AetherAI — Device Agent Configuration

FIX 2: No secrets in source code.
Values are read from environment variables with sensible defaults.
For local dev, set these in your shell or a .env file.
For production, set them in Railway / your deployment environment.

  AETHER_CLOUD_URL   — WebSocket URL of your cloud brain
  AETHER_DEVICE_ID   — Unique name for this machine
  AETHER_API_KEY     — Must match AETHER_API_KEY set in the cloud brain
"""

import os

CLOUD_BRAIN_URL = os.getenv("AETHER_CLOUD_URL", "wss://aetherai.up.railway.app")
DEVICE_ID       = os.getenv("AETHER_DEVICE_ID", "my-device")
API_KEY         = os.getenv("AETHER_API_KEY",   "")
