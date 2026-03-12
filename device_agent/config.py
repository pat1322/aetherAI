"""
AetherAI — Device Agent Configuration
Edit these values or set environment variables.
"""

import os
import socket

# URL of your deployed Cloud Brain (Railway URL or localhost for dev)
CLOUD_BRAIN_URL = os.getenv(
    "AETHER_CLOUD_URL",
    "ws://localhost:8000"   # Change to wss://your-app.railway.app in production
).replace("http", "ws").replace("https", "wss")

# Unique name for this device
DEVICE_ID = os.getenv(
    "AETHER_DEVICE_ID",
    socket.gethostname()   # Defaults to your computer's hostname
)

# Must match AETHER_API_KEY set on the Cloud Brain (leave empty if no auth)
API_KEY = os.getenv("AETHER_API_KEY", "")
