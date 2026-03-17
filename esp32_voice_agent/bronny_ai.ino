/*
 * BRONNY AI v7.4 - AetherAI Edition
 * by Patrick Perez
 *
 * Hardware:
 *   Board   : ESP32-S3 Dev Module (OPI PSRAM 8MB)
 *   Codec   : ES8311 (I2C 0x18)
 *   Mic     : INMP441 (I2S port 1)
 *   Display : ST7789 320x240 (HSPI)
 *
 * Wiring:
 *   ES8311  PA_EN->48  DOUT->45  DIN->12  WS->13  BCLK->14
 *           MCLK->38   SCL->2   SDA->1
 *   INMP441 VDD->3.3V  GND->GND  L/R->GND
 *           WS->4  SCK->5  SD->6
 *   ST7789  DC->39  CS->47  CLK->41  MOSI->40  BLK->42
 *
 * Libraries (Arduino Library Manager):
 *   - arduino-audio-tools  by pschatzmann
 *   - arduino-audio-driver by pschatzmann
 *   - Adafruit ST7789 + Adafruit GFX Library
 *   - WebSockets by Markus Sattler
 *   - ArduinoJson by Benoit Blanchon
 *
 * Board settings:
 *   Board: ESP32S3 Dev Module
 *   PSRAM: OPI PSRAM (8MB)  <- REQUIRED
 *   USB CDC on Boot: Enabled
 *
 * Pipeline:
 *   INMP441 mic -> VAD -> DashScope Paraformer streaming ASR (WebSocket)
 *   -> transcript text -> Railway /voice/text
 *   -> Qwen LLM + edge-tts -> MP3 -> ES8311 -> speaker
 *
 * ASR Protocol (official docs):
 *   URL:  wss://dashscope.aliyuncs.com/api-ws/v1/inference
 *   Auth: Authorization: Bearer {QWEN_API_KEY}
 *   Flow:
 *     1. Connect WSS
 *     2. Send TEXT: run-task JSON
 *     3. Wait for TEXT: task-started
 *     4. Send BINARY: raw PCM chunks (mono 16-bit 16kHz)
 *     5. Send TEXT: finish-task JSON (after audio done)
 *     6. Wait for TEXT: task-finished
 *   Partial results: result-generated event, sentence_end=false
 *   Final results:   result-generated event, sentence_end=true
 */

// ============================================================
// INCLUDES
// ============================================================
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <stdarg.h>
#include <math.h>

#include "AudioTools.h"
#include "AudioTools/AudioLibs/I2SCodecStream.h"
#include "AudioTools/CoreAudio/AudioI2S/I2SStream.h"

#if __has_include("AudioTools/AudioCodecs/CodecMP3Helix.h")
  #include "AudioTools/AudioCodecs/CodecMP3Helix.h"
#elif __has_include("AudioCodecs/CodecMP3Helix.h")
  #include "AudioCodecs/CodecMP3Helix.h"
#else
  #error "CodecMP3Helix not found - install arduino-audio-tools"
#endif

#include "voice_config.h"

#ifndef QWEN_API_KEY
  #error "Add #define QWEN_API_KEY \"sk-...\" to voice_config.h"
#endif

// ============================================================
// PIN DEFINITIONS
// ============================================================
#define PIN_SDA    1
#define PIN_SCL    2
#define PIN_MCLK  38
#define PIN_BCLK  14
#define PIN_WS    13
#define PIN_DOUT  45
#define PIN_DIN   12
#define PIN_PA    48

#define PIN_MIC_WS   4
#define PIN_MIC_SCK  5
#define PIN_MIC_SD   6

#define PIN_TFT_CS    47
#define PIN_TFT_DC    39
#define PIN_TFT_BLK   42
#define PIN_TFT_CLK   41
#define PIN_TFT_MOSI  40

// ============================================================
// DISPLAY - FULL SCREEN LOG ONLY (no face)
// ============================================================
#define SCR_W   320
#define SCR_H   240

// Colours (RGB565)
#define C_BK    0x0000
#define C_WH    0xFFFF
#define C_CY    0x07FF
#define C_GR    0x07E0
#define C_RD    0xF800
#define C_YL    0xFFE0
#define C_LG    0xC618
#define C_DG    0x2965
#define C_MINT  0x3FF7

// Log colours
#define LC_OK   C_GR    // green  - success
#define LC_ASR  C_CY    // cyan   - partial transcript
#define LC_TX   C_MINT  // mint   - final transcript
#define LC_INFO C_LG    // grey   - general status
#define LC_WARN C_YL    // yellow - warning
#define LC_ERR  C_RD    // red    - error

// 12 lines at text size 1 (8px per line + 4px gap = 12px per line)
#define LOG_LINE_H   12
#define LOG_MAX_LINES  (SCR_H / LOG_LINE_H)   // = 20 lines

static String   gLogLines[20];
static uint16_t gLogColors[20];
static int      gLogCount = 0;     // lines filled so far
static int      gLogHead  = 0;     // index of oldest line (ring buffer)

SPIClass        tftSPI(HSPI);
Adafruit_ST7789 tft = Adafruit_ST7789(&tftSPI, PIN_TFT_CS, PIN_TFT_DC, -1);

// Redraw the entire log area from ring buffer
static void logRedraw() {
    tft.fillScreen(C_BK);
    int total = min(gLogCount, LOG_MAX_LINES);
    for (int i = 0; i < total; i++) {
        int idx = (gLogHead + i) % LOG_MAX_LINES;
        tft.setTextColor(gLogColors[idx]);
        tft.setTextSize(1);
        tft.setCursor(2, i * LOG_LINE_H);
        tft.print(gLogLines[idx]);
    }
}

// Push a new log line
void tftLog(uint16_t col, const char* msg) {
    String s = String(msg);
    if ((int)s.length() > 53) s = s.substring(0, 53);

    if (gLogCount < LOG_MAX_LINES) {
        gLogLines[gLogCount]  = s;
        gLogColors[gLogCount] = col;
        // Draw at bottom of current content
        tft.setTextColor(col);
        tft.setTextSize(1);
        tft.setCursor(2, gLogCount * LOG_LINE_H);
        tft.print(s);
        gLogCount++;
    } else {
        // Ring buffer full - scroll: overwrite oldest slot
        gLogLines[gLogHead]  = s;
        gLogColors[gLogHead] = col;
        gLogHead = (gLogHead + 1) % LOG_MAX_LINES;
        logRedraw();
    }
}

void tftLogf(uint16_t col, const char* fmt, ...) {
    char buf[80];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    tftLog(col, buf);
}

// Update the LAST line in-place (for live partial transcript)
void tftLogUpdate(uint16_t col, const char* msg) {
    if (gLogCount == 0) { tftLog(col, msg); return; }

    String s = String(msg);
    if ((int)s.length() > 53) s = s.substring(0, 53);

    if (gLogCount < LOG_MAX_LINES) {
        int row = gLogCount - 1;
        // Erase that line
        tft.fillRect(0, row * LOG_LINE_H, SCR_W, LOG_LINE_H, C_BK);
        gLogLines[row]  = s;
        gLogColors[row] = col;
        tft.setTextColor(col);
        tft.setTextSize(1);
        tft.setCursor(2, row * LOG_LINE_H);
        tft.print(s);
    } else {
        // In scroll mode - update last drawn line
        int lastSlot = (gLogHead + LOG_MAX_LINES - 1) % LOG_MAX_LINES;
        int row = LOG_MAX_LINES - 1;
        tft.fillRect(0, row * LOG_LINE_H, SCR_W, LOG_LINE_H, C_BK);
        gLogLines[lastSlot]  = s;
        gLogColors[lastSlot] = col;
        tft.setTextColor(col);
        tft.setTextSize(1);
        tft.setCursor(2, row * LOG_LINE_H);
        tft.print(s);
    }
}

// ============================================================
// BEHAVIOUR CONSTANTS
// ============================================================
#define VAD_THR         BRONNY_VAD_THR
#define VAD_SILENCE_MS  1400      // ms silence after speech -> send finish-task
#define MAX_RECORD_MS   12000     // absolute max recording time
#define ASR_READY_MS    6000      // max wait for task-started after connect
#define ASR_FINISH_MS   5000      // max wait for task-finished after finish-task
#define HEARTBEAT_MS    30000
#define MP3_MAX_BYTES   (320 * 1024)

// Mic chunk: 100ms of audio recommended by DashScope docs
// 16kHz * 0.1s = 1600 samples, 16-bit mono = 3200 bytes
#define CHUNK_FRAMES    1600
#define CHUNK_BYTES     (CHUNK_FRAMES * 2)

// ============================================================
// AUDIO ENGINE
// ============================================================
DriverPins      brdPins;
AudioBoard      brdDrv(AudioDriverES8311, brdPins);
I2SCodecStream  i2s(brdDrv);
I2SStream       mic_stream;

AudioInfo ainf_rec(16000, 2, 16);
AudioInfo ainf_tts(24000, 2, 16);

MP3DecoderHelix mp3Decoder;
uint8_t*        mp3Buf    = nullptr;
size_t          mp3Len    = 0;

bool audioOk   = false;
bool micOk     = false;
bool inTtsMode = false;

// ============================================================
// MISC
// ============================================================
bool     busy    = false;
uint32_t lastHbMs = 0;

// Audio sample buffers
static int32_t s_rawBuf[CHUNK_FRAMES * 2];   // 32-bit stereo from INMP441
static int16_t s_pcmBuf[CHUNK_FRAMES];       // 16-bit mono for ASR

// ============================================================
// DASHSCOPE PARAFORMER ASR STATE
// ============================================================
WebSocketsClient wsClient;

// Task states
enum AsrState { ASR_IDLE, ASR_CONNECTING, ASR_RUNNING, ASR_FINISHING, ASR_DONE, ASR_ERROR };
AsrState asrState = ASR_IDLE;

bool   asrConnected    = false;
bool   asrTaskStarted  = false;
bool   asrGotFinal     = false;
String asrPartial      = "";
String asrFinal        = "";
String asrTaskId       = "";

// ============================================================
// HELPERS
// ============================================================

// Strip trailing slash from AETHER_URL to avoid double-slash paths
static String baseUrl() {
    String u = String(AETHER_URL);
    while (u.endsWith("/")) u.remove(u.length() - 1);
    return u;
}

// Generate a 32-char hex task ID
static String makeTaskId() {
    char buf[33];
    snprintf(buf, sizeof(buf), "%08lx%08lx%08lx%08lx",
             (unsigned long)esp_random(), (unsigned long)esp_random(),
             (unsigned long)esp_random(), (unsigned long)esp_random());
    return String(buf);
}

// ============================================================
// AUDIO INIT
// ============================================================

static void audioPinsSetup() {
    static bool done = false;
    if (done) return;
    Wire.begin(PIN_SDA, PIN_SCL, 100000);
    brdPins.addI2C(PinFunction::CODEC, PIN_SCL, PIN_SDA, 0x18, 100000, Wire);
    brdPins.addI2S(PinFunction::CODEC, PIN_MCLK, PIN_BCLK, PIN_WS, PIN_DOUT, PIN_DIN);
    brdPins.addPin(PinFunction::PA, PIN_PA, PinLogic::Output);
    done = true;
}

static void audioInitRec() {
    if (inTtsMode || !audioOk) {
        audioPinsSetup();
        auto cfg = i2s.defaultConfig(TX_MODE);
        cfg.copyFrom(ainf_rec);
        cfg.output_device = DAC_OUTPUT_ALL;
        audioOk   = i2s.begin(cfg);
        i2s.setVolume(0.55f);
        inTtsMode = false;
        tftLogf(audioOk ? LC_OK : LC_ERR, "Codec REC %s", audioOk ? "OK" : "FAIL");
    }
}

static void audioInitTTS() {
    if (!inTtsMode) {
        audioPinsSetup();
        auto cfg = i2s.defaultConfig(TX_MODE);
        cfg.copyFrom(ainf_tts);
        cfg.output_device = DAC_OUTPUT_ALL;
        audioOk   = i2s.begin(cfg);
        i2s.setVolume(0.55f);
        inTtsMode = true;
    }
}

static void micInit() {
    auto cfg = mic_stream.defaultConfig(RX_MODE);
    cfg.sample_rate     = 16000;
    cfg.channels        = 2;
    cfg.bits_per_sample = 32;
    cfg.i2s_format      = I2S_STD_FORMAT;
    cfg.port_no         = 1;
    cfg.pin_ws          = PIN_MIC_WS;
    cfg.pin_bck         = PIN_MIC_SCK;
    cfg.pin_data        = PIN_MIC_SD;
    cfg.pin_mck         = -1;
    cfg.use_apll        = false;
    micOk = mic_stream.begin(cfg);
    if (micOk) {
        // Warmup drain
        uint8_t tmp[512];
        uint32_t t = millis() + 300;
        while (millis() < t) { mic_stream.readBytes(tmp, sizeof(tmp)); yield(); }
    }
    tftLogf(micOk ? LC_OK : LC_ERR, "Mic INMP441 %s", micOk ? "OK" : "FAIL");
}

// ============================================================
// WIFI
// ============================================================
static void wifiConnect() {
    tftLog(LC_INFO, "Connecting WiFi...");
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    int t = 0;
    while (WiFi.status() != WL_CONNECTED && t < 40) {
        delay(400); t++; yield();
    }
    if (WiFi.status() == WL_CONNECTED) {
        tftLogf(LC_OK, "WiFi OK  %s", WiFi.localIP().toString().c_str());
    } else {
        tftLog(LC_ERR, "WiFi FAILED - check config");
    }
}

// ============================================================
// HEARTBEAT
// ============================================================
static void sendHeartbeat() {
    if (WiFi.status() != WL_CONNECTED) return;
    WiFiClientSecure cli; cli.setInsecure(); cli.setConnectionTimeout(8000);
    HTTPClient http;
    http.begin(cli, baseUrl() + "/bronny/heartbeat");
    http.setTimeout(8000);
    http.addHeader("Content-Type", "application/json");
    int code = http.POST("{\"device\":\"bronny\",\"version\":\"7.4\"}");
    http.end();
    if (code != 200) tftLogf(LC_WARN, "Heartbeat fail HTTP %d", code);
}

// ============================================================
// DASHSCOPE PARAFORMER ASR - WebSocket
// ============================================================
//
// Official docs:
// alibabacloud.com/help/en/model-studio/websocket-for-paraformer-real-time-service
//
// URL (NO trailing slash):
//   wss://dashscope.aliyuncs.com/api-ws/v1/inference
//
// Auth header:
//   Authorization: Bearer {QWEN_API_KEY}
//
// Sequence:
//   Client sends TEXT: run-task
//   Server sends TEXT: task-started  <- must wait for this before sending audio
//   Client sends BINARY: raw PCM chunks (mono 16-bit 16kHz)
//   Client sends TEXT: finish-task   <- after audio done
//   Server sends TEXT: task-finished <- all done
//
// result-generated events arrive during audio streaming:
//   sentence_end=false -> partial (show live on TFT)
//   sentence_end=true  -> final sentence complete

static String buildRunTask(const String& tid) {
    // Official format from docs - header.streaming must be "duplex"
    // language_hints: ["en","fil"] for English + Filipino
    return String("{"
        "\"header\":{"
            "\"action\":\"run-task\","
            "\"task_id\":\"") + tid + "\","
            "\"streaming\":\"duplex\""
        "},"
        "\"payload\":{"
            "\"task_group\":\"audio\","
            "\"task\":\"asr\","
            "\"function\":\"recognition\","
            "\"model\":\"paraformer-realtime-v2\","
            "\"parameters\":{"
                "\"format\":\"pcm\","
                "\"sample_rate\":16000,"
                "\"language_hints\":[\"en\",\"fil\"]"
            "},"
            "\"input\":{}"
        "}"
    "}";
}

static String buildFinishTask(const String& tid) {
    // Must use same task_id as run-task
    return String("{"
        "\"header\":{"
            "\"action\":\"finish-task\","
            "\"task_id\":\"") + tid + "\","
            "\"streaming\":\"duplex\""
        "},"
        "\"payload\":{"
            "\"input\":{}"
        "}"
    "}";
}

// Parse TEXT frames from DashScope server
static void parseAsrEvent(const char* json, size_t len) {
    StaticJsonDocument<1024> doc;
    if (deserializeJson(doc, json, len) != DeserializationError::Ok) return;

    const char* event = doc["header"]["event"] | "";

    if (strcmp(event, "task-started") == 0) {
        asrTaskStarted = true;
        tftLog(LC_OK, "ASR ready - speak now");

    } else if (strcmp(event, "result-generated") == 0) {
        const char* txt    = doc["payload"]["output"]["sentence"]["text"] | nullptr;
        bool       sentEnd = doc["payload"]["output"]["sentence"]["sentence_end"] | false;

        if (txt && strlen(txt) > 0) {
            char disp[54];
            if (sentEnd) {
                // Final sentence - keep it on TFT
                asrFinal = String(txt);
                asrGotFinal = true;
                snprintf(disp, sizeof(disp), "> %s", txt);
                tftLog(LC_TX, disp);
            } else {
                // Partial - update last line live
                asrPartial = String(txt);
                snprintf(disp, sizeof(disp), "~ %s", txt);
                tftLogUpdate(LC_ASR, disp);
            }
        }

    } else if (strcmp(event, "task-finished") == 0) {
        // Task complete - use best result we have
        if (asrFinal.length() == 0 && asrPartial.length() > 0) {
            asrFinal = asrPartial;
            asrGotFinal = true;
            char disp[54];
            snprintf(disp, sizeof(disp), "> %s", asrFinal.c_str());
            tftLog(LC_TX, disp);
        }
        asrState = ASR_DONE;

    } else if (strcmp(event, "task-failed") == 0) {
        const char* errCode = doc["header"]["error_code"]    | "";
        const char* errMsg  = doc["header"]["error_message"] | "unknown";
        tftLogf(LC_ERR, "ASR FAIL: %s", errCode);
        tftLogf(LC_WARN, "%s", errMsg);
        asrState = ASR_ERROR;
    }
}

// WebSocket event callback
void onAsrWsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            asrConnected   = true;
            asrTaskStarted = false;
            asrState       = ASR_RUNNING;
            tftLog(LC_OK, "ASR WS connected");
            // Send run-task immediately after connection
            {
                String rt = buildRunTask(asrTaskId);
                wsClient.sendTXT(rt);
                tftLog(LC_INFO, "run-task sent");
            }
            break;

        case WStype_TEXT:
            parseAsrEvent((const char*)payload, length);
            break;

        case WStype_DISCONNECTED:
            asrConnected   = false;
            asrTaskStarted = false;
            tftLogf(LC_WARN, "ASR WS closed (state=%d)", (int)asrState);
            if (asrState == ASR_RUNNING || asrState == ASR_FINISHING) {
                // Unexpected disconnect during active session
                asrState = ASR_ERROR;
            }
            break;

        case WStype_ERROR:
            tftLog(LC_ERR, "ASR WS error");
            asrState = ASR_ERROR;
            break;

        default: break;
    }
}

// ============================================================
// RECORD + STREAM
// Connects to DashScope Paraformer, streams PCM while speaking,
// receives live partial transcripts on TFT.
// Returns true if asrFinal contains a usable transcript.
// ============================================================
bool recordAndStream() {
    // Reset state
    asrState       = ASR_CONNECTING;
    asrConnected   = false;
    asrTaskStarted = false;
    asrGotFinal    = false;
    asrFinal       = "";
    asrPartial     = "";
    asrTaskId      = makeTaskId();

    // Auth header - "Authorization: Bearer {key}" (no semicolon - standard OAuth2)
    String authHdr = "Authorization: Bearer " + String(QWEN_API_KEY);

    wsClient.onEvent(onAsrWsEvent);
    wsClient.setExtraHeaders(authHdr.c_str());

    // IMPORTANT: NO trailing slash - docs show /api-ws/v1/inference (not /inference/)
    wsClient.beginSSL("dashscope.aliyuncs.com", 443, "/api-ws/v1/inference");

    tftLog(LC_INFO, "ASR connecting...");

    // Wait for WS connection + run-task to be sent + task-started received
    // (run-task is sent in onAsrWsEvent when CONNECTED fires)
    uint32_t deadline = millis() + ASR_READY_MS;
    while (millis() < deadline) {
        wsClient.loop();
        yield();
        if (asrTaskStarted) break;
        if (asrState == ASR_ERROR) {
            tftLog(LC_ERR, "ASR failed at startup");
            wsClient.disconnect();
            return false;
        }
    }

    if (!asrTaskStarted) {
        tftLog(LC_ERR, "ASR timeout - no task-started");
        wsClient.disconnect();
        asrState = ASR_DONE;
        return false;
    }

    tftLog(LC_OK, "Listening... speak now");

    // Stream audio
    bool     voiceStarted = false;
    uint32_t silenceStart = 0;
    uint32_t recDeadline  = millis() + MAX_RECORD_MS;
    bool     finishSent   = false;

    while (millis() < recDeadline && asrState == ASR_RUNNING) {
        // Read one 100ms chunk from INMP441
        int bytesRead = mic_stream.readBytes((uint8_t*)s_rawBuf, sizeof(s_rawBuf));
        int frames    = bytesRead / 8;   // 32-bit stereo = 8 bytes per frame
        if (frames <= 0) { wsClient.loop(); yield(); continue; }

        // Convert 32-bit stereo -> 16-bit mono
        // INMP441 data on left channel; >>11 gives proper amplitude
        int32_t peak = 0;
        for (int i = 0; i < frames; i++) {
            s_pcmBuf[i] = (int16_t)(s_rawBuf[i * 2] >> 11);
            int32_t a   = abs((int32_t)s_pcmBuf[i]);
            if (a > peak) peak = a;
        }

        // VAD logic
        if (peak > VAD_THR) {
            voiceStarted = true;
            silenceStart = 0;
        } else if (voiceStarted && silenceStart == 0) {
            silenceStart = millis();
        }

        // Send raw PCM as binary WebSocket frame - no framing, just raw bytes
        wsClient.sendBIN((uint8_t*)s_pcmBuf, frames * 2);

        // Process incoming events (partials arrive here)
        wsClient.loop();

        // If sentence_end=true came in, we already got asrGotFinal=true
        // but keep sending until silence to get more sentences
        bool silenced = voiceStarted && silenceStart > 0
                     && (millis() - silenceStart) >= VAD_SILENCE_MS;

        if (silenced || millis() >= recDeadline) {
            // Send finish-task to signal end of audio
            if (!finishSent) {
                String ft = buildFinishTask(asrTaskId);
                wsClient.sendTXT(ft);
                finishSent = true;
                asrState = ASR_FINISHING;
                tftLog(LC_INFO, "Processing...");
            }
            break;
        }
    }

    if (!voiceStarted) {
        // Nobody spoke - clean up
        if (!finishSent) {
            String ft = buildFinishTask(asrTaskId);
            wsClient.sendTXT(ft);
        }
        tftLog(LC_INFO, "No voice detected");
        delay(300);
        wsClient.disconnect();
        asrState = ASR_DONE;
        return false;
    }

    // Wait for task-finished (server flushes remaining results)
    uint32_t finishDeadline = millis() + ASR_FINISH_MS;
    while (asrState == ASR_FINISHING && millis() < finishDeadline) {
        wsClient.loop();
        yield();
        delay(10);
    }

    wsClient.disconnect();
    asrConnected   = false;
    asrTaskStarted = false;
    asrState       = ASR_DONE;

    // Fallback: use partial if final never arrived
    if (asrFinal.length() == 0 && asrPartial.length() > 0) {
        asrFinal = asrPartial;
    }

    return asrFinal.length() > 0;
}

// ============================================================
// RAILWAY - POST /voice/text -> MP3
// ============================================================
static bool callRailway(const char* text) {
    if (!text || text[0] == '\0') return false;

    // Build JSON body with manual escaping (no heap allocation for JsonDocument)
    String body = "{\"text\":\"";
    for (const char* p = text; *p; p++) {
        switch (*p) {
            case '"':  body += "\\\""; break;
            case '\\': body += "\\\\"; break;
            case '\n': body += "\\n";  break;
            case '\r': body += "\\r";  break;
            default:   body += *p;
        }
    }
    body += "\"}";

    tftLog(LC_INFO, "Sending to AetherAI...");

    if (!mp3Buf) {
        mp3Buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
        if (!mp3Buf) { tftLog(LC_ERR, "MP3 buf alloc FAILED"); return false; }
    }

    bool success = false;
    for (int attempt = 1; attempt <= 2 && !success; attempt++) {
        if (attempt > 1) { tftLog(LC_WARN, "Railway retry..."); delay(2000); }

        WiFiClientSecure cli; cli.setInsecure(); cli.setConnectionTimeout(20000);
        HTTPClient http;
        http.begin(cli, baseUrl() + "/voice/text");
        http.setTimeout(45000);
        http.addHeader("Content-Type", "application/json");
        http.addHeader("X-Api-Key", AETHER_API_KEY);

        int code = http.POST(body);

        if (code == 200) {
            WiFiClient* stream = http.getStreamPtr();
            int    clen = http.getSize();
            size_t want = (clen > 0)
                ? min((size_t)clen, (size_t)MP3_MAX_BYTES)
                : (size_t)MP3_MAX_BYTES;
            size_t got = 0;
            uint32_t dlEnd = millis() + 35000;
            while ((http.connected() || stream->available()) && got < want && millis() < dlEnd) {
                if (stream->available()) {
                    size_t chunk = min((size_t)1024, want - got);
                    got += stream->readBytes(mp3Buf + got, chunk);
                } else { delay(2); }
                yield();
            }
            mp3Len  = got;
            success = (got > 0);
            tftLogf(LC_OK, "MP3 received %u bytes", (unsigned)mp3Len);
        } else {
            tftLogf(LC_ERR, "Railway HTTP %d", code);
        }
        http.end();
        yield();
    }
    return success;
}

// ============================================================
// MP3 PLAYBACK via ES8311
// ============================================================
static void playMp3() {
    if (!mp3Len || !mp3Buf) { tftLog(LC_ERR, "No MP3 to play"); return; }

    mic_stream.end();
    micOk = false;
    delay(60);
    audioInitTTS();
    delay(80);

    {
        EncodedAudioStream decoded(&i2s, &mp3Decoder);
        decoded.begin();
        MemoryStream mp3Mem(mp3Buf, mp3Len);
        StreamCopy   copier(decoded, mp3Mem);
        while (copier.copy()) { yield(); }
        decoded.end();
    }

    // Brief silence to avoid speaker click
    { int16_t sil[128] = {0}; i2s.write((uint8_t*)sil, sizeof(sil)); }
    delay(60);

    // Restore mic
    audioInitRec();
    micInit();

    // Drain echo from mic buffer
    if (micOk) {
        uint8_t drain[512];
        uint32_t e = millis() + 300;
        while (millis() < e) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
    }
}

// ============================================================
// NOISE FILTER
// ============================================================
static bool isNoise(const String& t) {
    String s = t; s.trim(); s.toLowerCase();
    if (s.length() < 3) return true;
    static const char* kw[] = {
        ".","..","...","ah","uh","hm","hmm","mm","um","huh",
        "oh","the","a","i",nullptr
    };
    for (int i = 0; kw[i]; i++)
        if (s == String(kw[i])) return true;
    return false;
}

// ============================================================
// CONVERSATION - full turn
// ============================================================
static void runConversation() {
    if (busy) return;
    busy = true;

    if (!micOk) {
        tftLog(LC_ERR, "Mic not ready");
        busy = false;
        return;
    }

    tftLog(LC_INFO, "--- Turn start ---");

    // 1. Record + stream ASR
    bool ok = recordAndStream();

    if (!ok || isNoise(asrFinal)) {
        tftLog(LC_INFO, "Nothing heard");
        busy = false;
        return;
    }

    // 2. Railway LLM + TTS
    ok = callRailway(asrFinal.c_str());
    if (!ok) {
        tftLog(LC_ERR, "Railway failed");
        busy = false;
        return;
    }

    // 3. Play response
    tftLog(LC_INFO, "Speaking...");
    playMp3();

    tftLog(LC_OK, "Done - listening for speech");
    busy = false;
}

// ============================================================
// SETUP
// ============================================================
void setup() {
    Serial.begin(115200);
    AudioToolsLogger.begin(Serial, AudioToolsLogLevel::Error);

    // TFT
    pinMode(PIN_TFT_BLK, OUTPUT);
    digitalWrite(PIN_TFT_BLK, HIGH);
    tftSPI.begin(PIN_TFT_CLK, -1, PIN_TFT_MOSI, PIN_TFT_CS);
    tft.init(240, 320);
    tft.setRotation(3);
    tft.fillScreen(C_BK);

    tftLog(LC_INFO, "=== BRONNY AI v7.4 ===");
    tftLog(LC_INFO, "Init audio codec...");
    audioInitRec();

    tftLog(LC_INFO, "Init microphone...");
    micInit();

    mp3Decoder.begin();
    mp3Buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
    tftLogf(mp3Buf ? LC_OK : LC_ERR, "PSRAM buf %s", mp3Buf ? "OK" : "FAIL");
    tftLogf(LC_INFO, "Heap %uK  PSRAM %uK",
            esp_get_free_heap_size() / 1024,
            heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024);

    wifiConnect();

    tftLog(LC_INFO, "Sending heartbeat...");
    sendHeartbeat();
    lastHbMs = millis();

    tftLogf(LC_INFO, "VAD thr=%d (speak to trigger)", BRONNY_VAD_THR);
    tftLog(LC_OK, "Ready - speak to start");
}

// ============================================================
// LOOP
// ============================================================
void loop() {
    uint32_t now = millis();

    // Periodic heartbeat
    if (now - lastHbMs > HEARTBEAT_MS) {
        lastHbMs = now;
        sendHeartbeat();
    }

    // VAD auto-trigger
    if (!busy && micOk) {
        static int32_t  vadBuf[64] = {};
        static int32_t  vadPeak    = 0;
        static uint32_t vadLogMs   = 0;

        int rd = mic_stream.readBytes((uint8_t*)vadBuf, sizeof(vadBuf));
        int f  = rd / 8;
        for (int i = 0; i < f; i++) {
            int32_t v = abs((int16_t)(vadBuf[i * 2] >> 11));
            if (v > vadPeak) vadPeak = v;
        }

        // Show mic level every 6s for calibration
        if (now - vadLogMs > 6000) {
            tftLogf(LC_INFO, "mic peak=%d  thr=%d", (int)vadPeak, VAD_THR);
            vadLogMs = now;
            vadPeak  = 0;
        }

        if (vadPeak > VAD_THR) {
            vadPeak = 0;
            runConversation();
        }
    }

    yield();
}
