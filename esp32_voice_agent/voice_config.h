/*
 * ╔══════════════════════════════════════════════════════════════╗
 * ║        AetherAI ESP32 Voice Agent — Config Template          ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  SETUP:                                                      ║
 * ║   1. Copy this file → voice_config.h                         ║
 * ║   2. Fill in your WiFi and Railway values below              ║
 * ║   3. voice_config.h is in .gitignore — never committed       ║
 * ╚══════════════════════════════════════════════════════════════╝
 */

#pragma once

// ── WiFi ─────────────────────────────────────────────────────────────────────
#define WIFI_SSID       "FLORES_PLDT"
#define WIFI_PASSWORD   "@Capricorn051"

// ── AetherAI Cloud Brain ──────────────────────────────────────────────────────
// Your Railway deployment URL — no trailing slash
// Example: "https://aetherai.up.railway.app"
#define AETHER_URL      "https://aetherai.up.railway.app/"

// Must match AETHER_API_KEY set in Railway environment variables
// Leave blank ("") if you have not set an API key on the server
#define AETHER_API_KEY  "5a3da8910b3bd313d2aab4acb3e47ab0a3c9139898844041ae0145f16c06f0a7"

// ── Recording ─────────────────────────────────────────────────────────────────
// Maximum recording duration in seconds (1–10 recommended)
// Longer = more PSRAM used and longer upload time
#define MAX_RECORD_SECS  5

// ── Display ───────────────────────────────────────────────────────────────────
// Show transcript text on TFT after each response (1 = yes, 0 = no)
#define SHOW_TRANSCRIPT  1
