/*
 * ╔══════════════════════════════════════════════════════════════╗
 * ║         BRONNY AI  v7.1  —  AetherAI Edition                 ║
 * ║         by Patrick Perez                                     ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  Hardware                                                    ║
 * ║    Board   : ESP32-S3 Dev Module  (OPI PSRAM 8MB)            ║
 * ║    Codec   : ES8311  (I2C addr 0x18) — speaker output        ║
 * ║    Mic     : INMP441  (I2S port 1)                           ║
 * ║    Display : ST7789  320×240  (HSPI)                         ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  Wiring                                                      ║
 * ║    ES8311 codec                                              ║
 * ║      PA_EN→48  DOUT→45  DIN→12  WS→13  BCLK→14               ║
 * ║      MCLK→38   SCL→2    SDA→1                                ║
 * ║    INMP441 mic                                               ║
 * ║      VDD→3.3V  GND→GND  L/R→GND                              ║
 * ║      WS→4  SCK→5  SD→6                                       ║
 * ║    ST7789 TFT  (HSPI)                                        ║
 * ║      DC→39  CS→47  CLK→41  MOSI→40  BLK→42                   ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  Required Libraries (Arduino Library Manager)                ║
 * ║    • arduino-audio-tools   by pschatzmann                    ║
 * ║    • arduino-audio-driver  by pschatzmann                    ║
 * ║    • Adafruit ST7789  + Adafruit GFX Library                 ║
 * ║    • WebSockets  by Markus Sattler                           ║
 * ║    • ArduinoJson by Benoit Blanchon                          ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  Arduino IDE Board Settings                                  ║
 * ║    Board  : ESP32S3 Dev Module                               ║
 * ║    PSRAM  : OPI PSRAM  (8MB)  ← REQUIRED                     ║
 * ║    USB CDC on Boot : Enabled                                 ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  Screen layout (320×240 landscape)                           ║
 * ║    ┌────────────────────────────┐ y=0                        ║
 * ║    │  [LEFT EYE]  [RIGHT EYE]   │ y=42-94  (FCY=68)          ║
 * ║    │         [MOUTH]            │ y=106-160                  ║
 * ║    ├─────────── separator ──────┤ y=161                      ║
 * ║    │  TFT log line 1 (oldest)   │ y=163                      ║
 * ║    │  TFT log line 2            │                            ║
 * ║    │  TFT log line 3            │                            ║
 * ║    │  TFT log line 4 (newest)   │ y=211                      ║
 * ║    ├────────────────────────────┤                            ║
 * ║    │   ···· island bar ····     │ y=220-236                  ║
 * ║    └────────────────────────────┘ y=240                      ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  BytePlus credential mapping (voice_config.h)                ║
 * ║    BYTEPLUS_APP_ID   = APP ID         → JSON app.appid       ║
 * ║    BYTEPLUS_TOKEN    = ACCESS TOKEN   → Auth header +        ║
 * ║                                         JSON app.token       ║
 * ║    BYTEPLUS_CLUSTER  = API Resource ID→ JSON app.cluster     ║
 * ║    Auth header format: Bearer; {ACCESS_TOKEN}                ║
 * ║    (semicolon required — BytePlus specific)                  ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  v7.1 changes vs v7.0                                        ║
 * ║    • ALL output goes to TFT scrolling log (no Serial)        ║
 * ║    • Face compacted to FCY=68 to fit 4-line log below        ║
 * ║    • Auth header corrected: Bearer; {TOKEN}                  ║
 * ║    • Cluster uses API Resource ID from BytePlus console      ║
 * ║    • Error codes 1001/1002 show actionable hint on screen    ║
 * ╚══════════════════════════════════════════════════════════════╝
 */

// ============================================================
// INCLUDES
// ============================================================
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>       // "WebSockets" by Markus Sattler
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
  #error "CodecMP3Helix not found — install arduino-audio-tools"
#endif

#include "voice_config.h"

// ============================================================
// PIN DEFINITIONS
// ============================================================

// ES8311 codec
#define PIN_SDA    1
#define PIN_SCL    2
#define PIN_MCLK  38
#define PIN_BCLK  14
#define PIN_WS    13
#define PIN_DOUT  45
#define PIN_DIN   12
#define PIN_PA    48
#define ES_ADDR   0x18

// INMP441 microphone (I2S port 1)
#define PIN_MIC_WS   4
#define PIN_MIC_SCK  5
#define PIN_MIC_SD   6

// ST7789 TFT (HSPI)
#define PIN_TFT_CS    47
#define PIN_TFT_DC    39
#define PIN_TFT_BLK   42
#define PIN_TFT_CLK   41
#define PIN_TFT_MOSI  40

// ============================================================
// DISPLAY CONSTANTS
// ============================================================
#define W   320
#define H   240

// RGB565 colour palette
#define C_BK    0x0000
#define C_WH    0xFFFF
#define C_CY    0x07FF
#define C_DCY   0x0455
#define C_GR    0x07E0
#define C_RD    0xF800
#define C_YL    0xFFE0
#define C_LG    0xC618
#define C_DG    0x2965
#define C_ORG   0xFD20
#define C_PURP  0x901F
#define C_MINT  0x3FF7

// TFT log colour shortcuts
#define LC_INFO  C_DG
#define LC_ASR   C_CY
#define LC_OK    C_GR
#define LC_WARN  C_YL
#define LC_ERR   C_RD
#define LC_STAT  C_LG
#define LC_TX    C_MINT

// ============================================================
// FACE + LOG LAYOUT
// ============================================================
#define FCX  160    // face centre X
#define FCY   68    // face centre Y  (compact — leaves room for log)

// TFT scrolling log zone (between face and island bar)
#define LOG_Y        163   // top of log area
#define LOG_LINE_H    12   // px per line (font=1 → 8px + 4 leading)
#define LOG_LINES      4   // visible lines

// Island status bar (bottom of screen)
#define ISL_W  200
#define ISL_H   16
#define ISL_X  ((W - ISL_W) / 2)
#define ISL_Y  (H - ISL_H - 4)    // = 220
#define ISL_R    8

// ============================================================
// BEHAVIOUR
// ============================================================
#define VAD_THR         BRONNY_VAD_THR
#define VAD_SILENCE_MS  1600
#define MAX_RECORD_MS   12000
#define ASR_CONNECT_MS  7000
#define ASR_FINAL_MS    5000
#define HEARTBEAT_MS    30000
#define MP3_MAX_BYTES   (320 * 1024)
#define CHUNK_FRAMES    320
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

static bool audioOk   = false;
static bool micOk     = false;
static bool inTtsMode = false;

static MP3DecoderHelix mp3Decoder;
static uint8_t*        mp3Buf = nullptr;
static size_t          mp3Len = 0;

// ============================================================
// DISPLAY OBJECTS
// ============================================================
SPIClass        tftSPI(HSPI);
Adafruit_ST7789 tft = Adafruit_ST7789(&tftSPI, PIN_TFT_CS, PIN_TFT_DC, -1);
static bool     tftReady = false;

// ============================================================
// TFT SCROLLING LOG  (4-line, newest at bottom)
// ============================================================
static String   gLog[LOG_LINES];
static uint16_t gLogCol[LOG_LINES];

// Render all 4 lines into the log zone
static void _renderLog() {
    if (!tftReady) return;
    tft.fillRect(0, LOG_Y, W, LOG_LINES * LOG_LINE_H + 2, C_BK);
    for (int i = 0; i < LOG_LINES; i++) {
        if (gLog[i].length() == 0) continue;
        // Dim older lines (i=0 oldest → darkest, i=LOG_LINES-1 newest → full)
        uint16_t c = gLogCol[i];
        if (i < LOG_LINES - 2) {
            uint16_t r = ((c >> 11) & 0x1F) >> 1;
            uint16_t g = ((c >>  5) & 0x3F) >> 1;
            uint16_t b = ( c        & 0x1F) >> 1;
            c = (r << 11) | (g << 5) | b;
        }
        tft.setTextColor(c);
        tft.setTextSize(1);
        tft.setCursor(2, LOG_Y + 1 + i * LOG_LINE_H);
        tft.print(gLog[i]);
    }
}

// Push a new log line — scroll oldest off
void tftLog(uint16_t col, const char* msg) {
    for (int i = 0; i < LOG_LINES - 1; i++) {
        gLog[i]    = gLog[i + 1];
        gLogCol[i] = gLogCol[i + 1];
    }
    String s = String(msg);
    if ((int)s.length() > 53) s = s.substring(0, 53);
    gLog[LOG_LINES - 1]    = s;
    gLogCol[LOG_LINES - 1] = col;
    _renderLog();
}

// Printf-style wrapper
void tftLogf(uint16_t col, const char* fmt, ...) {
    char buf[80];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    tftLog(col, buf);
}

// ============================================================
// FACE STATE MACHINE
// ============================================================
enum FaceState { FS_IDLE, FS_LISTENING, FS_THINKING, FS_SPEAKING, FS_ERROR };
static FaceState gFaceState = FS_IDLE;
static bool      faceRedraw = false;

// Animation state
static float    bobPhase    = 0.f;
static float    bobY        = 0.f;
static float    eyeOpenL    = 1.f;
static float    eyeOpenR    = 1.f;
static float    tgtEyeOpen  = 1.f;
static float    mouthOpen   = 0.f;
static float    talkPh      = 0.f;
static uint32_t lastFaceMs  = 0;
static uint32_t lastBlinkMs = 0;
static uint32_t nextBlinkMs = 3000;
static bool     blinking    = false;
static int      blinkFrame  = 0;

// Previous-frame tracking for incremental redraw
static float     prevBobY      = -99.f;
static float     prevOpenL     = -1.f;
static float     prevOpenR     = -1.f;
static FaceState prevFaceState = FS_IDLE;

// Island bar
static String   islandText  = "Booting...";
static uint16_t islandColor = C_DCY;

// ============================================================
// STREAMING ASR STATE
// ============================================================
WebSocketsClient wsClient;

enum AsrState {
    ASR_IDLE, ASR_CONNECTING, ASR_STREAMING,
    ASR_WAITING_FINAL, ASR_DONE, ASR_ERROR
};
static AsrState asrState     = ASR_IDLE;
static bool     asrConnected = false;
static String   asrPartial   = "";
static String   asrFinal     = "";
static bool     asrGotFinal  = false;
static uint32_t reqCounter   = 0;

// ============================================================
// GLOBAL AUDIO BUFFERS  (static — keep off stack)
// ============================================================
static int32_t s_rawBuf[CHUNK_FRAMES * 2];
static int16_t s_pcmBuf[CHUNK_FRAMES];
static uint8_t s_audioPkt[8 + CHUNK_BYTES + 4];
static uint8_t s_configPkt[8 + 700];

// ============================================================
// MISC GLOBALS
// ============================================================
static bool     busy     = false;
static uint32_t lastHbMs = 0;

// ============================================================
// FORWARD DECLARATIONS
// ============================================================
void drawFace(bool full);
void animFace();
void drawIslandBar();
void setStatus(const char* s, uint16_t c);
void setFaceState(FaceState s);
void onAsrEvent(WStype_t type, uint8_t* payload, size_t length);

// ============================================================
// AUDIO — INIT HELPERS
// ============================================================

void audioPinsSetup() {
    static bool done = false;
    if (done) return;
    Wire.begin(PIN_SDA, PIN_SCL, 100000);
    brdPins.addI2C(PinFunction::CODEC, PIN_SCL, PIN_SDA, ES_ADDR, 100000, Wire);
    brdPins.addI2S(PinFunction::CODEC, PIN_MCLK, PIN_BCLK, PIN_WS, PIN_DOUT, PIN_DIN);
    brdPins.addPin(PinFunction::PA, PIN_PA, PinLogic::Output);
    done = true;
}

void audioInitRec() {
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

void audioInitTTS() {
    if (!inTtsMode) {
        audioPinsSetup();
        auto cfg = i2s.defaultConfig(TX_MODE);
        cfg.copyFrom(ainf_tts);
        cfg.output_device = DAC_OUTPUT_ALL;
        audioOk   = i2s.begin(cfg);
        i2s.setVolume(0.55f);
        inTtsMode = true;
        tftLogf(audioOk ? LC_OK : LC_ERR, "Codec TTS %s", audioOk ? "OK" : "FAIL");
    }
}

void micInit() {
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
        uint8_t tmp[512];
        uint32_t e = millis() + 350;
        while (millis() < e) { mic_stream.readBytes(tmp, sizeof(tmp)); yield(); }
    }
    tftLogf(micOk ? LC_OK : LC_ERR, "Mic INMP441 %s", micOk ? "OK" : "FAIL");
}

// ============================================================
// DISPLAY — ISLAND BAR
// ============================================================

void drawIslandBar() {
    if (!tftReady) return;
    tft.fillRect(ISL_X - 2, ISL_Y - 2, ISL_W + 4, ISL_H + 4, C_BK);
    tft.fillRoundRect(ISL_X, ISL_Y, ISL_W, ISL_H, ISL_R, C_BK);
    tft.drawRoundRect(ISL_X, ISL_Y, ISL_W, ISL_H, ISL_R, islandColor);
    tft.fillCircle(ISL_X + 10, ISL_Y + ISL_H / 2, 3, islandColor);
    tft.setTextSize(1);
    tft.setTextColor(islandColor);
    int tw = (int)islandText.length() * 6;
    int tx = ISL_X + (ISL_W - tw) / 2 + 7;
    int ty = ISL_Y + (ISL_H - 8) / 2;
    tft.setCursor(tx, ty);
    tft.print(islandText);
}

void setStatus(const char* s, uint16_t c) {
    islandText  = String(s);
    islandColor = c;
    drawIslandBar();
}

// ============================================================
// DISPLAY — ROBOT FACE
// ============================================================

void drawEye(int cx, int cy, float openFrac, uint16_t col, FaceState state) {
    int ew = 90;
    int eh = max(2, (int)(52 * openFrac));
    int er = min(18, min(ew / 2, eh / 2));
    tft.fillRoundRect(cx - ew / 2, cy - eh / 2, ew, eh, er, col);
    if (state == FS_THINKING && eh > 10) {
        // Squint: black out upper half
        tft.fillRect(cx - ew / 2 + 4, cy - eh / 2, ew - 8, eh / 2 + 2, C_BK);
    }
}

void drawMouth(int cx, int my, FaceState state, float mOpen) {
    switch (state) {
        case FS_IDLE:
        case FS_THINKING: {
            // Smile: bottom half of circle
            tft.fillCircle(cx, my, 27, C_WH);
            tft.fillRect(cx - 29, my - 29, 58, 29, C_BK);
            tft.fillCircle(cx, my, 19, C_BK);
            break;
        }
        case FS_LISTENING: {
            // Neutral line
            tft.fillRoundRect(cx - 30, my - 5, 60, 10, 5, C_WH);
            break;
        }
        case FS_SPEAKING: {
            // Animated open oval
            int mh = 8 + (int)(mOpen * 30);
            int mr = min(16, mh / 2);
            tft.fillRoundRect(cx - 28, my - mh / 2, 56, mh, mr, C_WH);
            if (mh > 10)
                tft.fillRoundRect(cx - 20, my - mh / 2 + 3, 40, mh - 6, mr - 2, C_BK);
            break;
        }
        case FS_ERROR: {
            // Frown
            tft.fillCircle(cx, my + 20, 23, C_RD);
            tft.fillRect(cx - 25, my - 4, 50, 24, C_BK);
            tft.fillCircle(cx, my + 20, 15, C_BK);
            break;
        }
    }
}

void drawFace(bool full) {
    if (!tftReady) return;
    int by  = (int)bobY;
    int lex = FCX - 52;
    int rex = FCX + 52;
    int ey  = FCY + by;
    int my  = FCY + 58 + by;

    uint16_t eyeCol = (gFaceState == FS_ERROR) ? C_RD : C_WH;

    if (full) {
        // Full redraw: clear face area only (never touch log or island zones)
        tft.fillRect(0, 0, W, LOG_Y, C_BK);
    } else {
        // Incremental: only redraw if something actually changed
        bool eyeChg   = fabsf(bobY - prevBobY)   > 0.4f
                      || fabsf(eyeOpenL - prevOpenL) > 0.02f
                      || fabsf(eyeOpenR - prevOpenR) > 0.02f;
        bool stateChg = (gFaceState != prevFaceState);
        if (!eyeChg && !stateChg) return;

        // Erase old eye and mouth positions
        int pby = (int)prevBobY;
        tft.fillRect(lex - 47, FCY + pby - 34, 94, 68, C_BK);
        tft.fillRect(rex - 47, FCY + pby - 34, 94, 68, C_BK);
        tft.fillRect(FCX - 36, FCY + 24 + pby, 72, 54, C_BK);
    }

    drawEye(lex, ey, eyeOpenL, eyeCol, gFaceState);
    drawEye(rex, ey, eyeOpenR, eyeCol, gFaceState);
    drawMouth(FCX, my, gFaceState, mouthOpen);

    prevBobY      = bobY;
    prevOpenL     = eyeOpenL;
    prevOpenR     = eyeOpenR;
    prevFaceState = gFaceState;
}

// ============================================================
// FACE ANIMATION  (~60 fps)
// ============================================================

void animFace() {
    uint32_t now = millis();
    if (now - lastFaceMs < 16) return;
    lastFaceMs = now;
    bool ch = false;

    // Gentle vertical bob
    float bobSpd = (gFaceState == FS_THINKING) ? 0.013f : 0.022f;
    bobPhase += bobSpd;
    if (bobPhase > 6.2832f) bobPhase -= 6.2832f;
    float nb = sinf(bobPhase) * 5.f;
    if (fabsf(nb - bobY) > 0.2f) { bobY = bobY + (nb - bobY) * 0.3f; ch = true; }

    // Blink
    if (!blinking && now - lastBlinkMs > nextBlinkMs) {
        blinking    = true;
        blinkFrame  = 0;
        nextBlinkMs = 2500 + (uint32_t)random(2500);
    }
    if (blinking) {
        float bf = (blinkFrame <= 4) ? (1.f - blinkFrame / 4.f)
                                      : ((blinkFrame - 4) / 5.f);
        tgtEyeOpen = constrain(bf, 0.f, 1.f);
        if (++blinkFrame >= 10) {
            blinking    = false;
            tgtEyeOpen  = 1.f;
            lastBlinkMs = now;
        }
        ch = true;
    }

    // Smooth eye open/close
    float dL = tgtEyeOpen - eyeOpenL;
    float dR = tgtEyeOpen - eyeOpenR;
    if (fabsf(dL) > 0.01f) { eyeOpenL += dL * 0.3f; ch = true; }
    if (fabsf(dR) > 0.01f) { eyeOpenR += dR * 0.3f; ch = true; }

    // Talking mouth animation
    if (gFaceState == FS_SPEAKING) {
        talkPh += 0.42f;
        if (talkPh > 6.2832f) talkPh -= 6.2832f;
        float tm = 0.35f + sinf(talkPh) * 0.45f + sinf(talkPh * 2.3f) * 0.18f;
        mouthOpen = constrain(tm, 0.f, 1.f);
        ch = true;
    } else {
        if (mouthOpen > 0.01f) { mouthOpen *= 0.82f; ch = true; }
    }

    if (ch) faceRedraw = true;
}

void setFaceState(FaceState s) {
    gFaceState = s;
    tgtEyeOpen = 1.f;
    faceRedraw = true;
}

// ============================================================
// WIFI
// ============================================================

void wifiConnect() {
    tftLog(LC_STAT, "Connecting WiFi...");
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    int tries = 0;
    while (WiFi.status() != WL_CONNECTED && tries < 40) {
        delay(400);
        if (tries % 8 == 0) {
            tftLogf(LC_STAT, "WiFi attempt %d/40", tries + 1);
        }
        tries++;
        yield();
    }
    if (WiFi.status() == WL_CONNECTED) {
        tftLogf(LC_OK, "WiFi OK  %s", WiFi.localIP().toString().c_str());
    } else {
        tftLog(LC_ERR, "WiFi FAIL  check config");
    }
}

// ============================================================
// HEARTBEAT  → Railway /bronny/heartbeat
// ============================================================

void sendHeartbeat() {
    if (WiFi.status() != WL_CONNECTED) return;
    WiFiClientSecure cli;
    cli.setInsecure();
    cli.setConnectionTimeout(8000);
    HTTPClient http;
    http.begin(cli, String(AETHER_URL) + "/bronny/heartbeat");
    http.setTimeout(8000);
    http.addHeader("Content-Type", "application/json");
    int code = http.POST("{\"device\":\"bronny\",\"version\":\"7.1\"}");
    http.end();
    if (code != 200) {
        tftLogf(LC_WARN, "Heartbeat fail %d", code);
    }
    // Success heartbeats are silent to avoid log spam every 30s
}

// ============================================================
// BYTEPLUS STREAMING ASR — BINARY WEBSOCKET PROTOCOL
// ============================================================
//
// Full protocol reference:
//   https://docs.byteplus.com/en/docs/speech/docs-real-time-speech-recog
//
// Binary frame structure:
//   [Byte 0] 0x11 = protocol version 1, header size 4 bytes
//   [Byte 1] message type + flags:
//       0x10 = full_client_request  (first frame — sends config JSON)
//       0x20 = audio_only (non-last chunk)
//       0x22 = audio_only (LAST chunk — signals end of audio)
//       0x90 = full_server_response (incoming from BytePlus)
//       0xF0 = server error frame
//   [Byte 2] serialization (0x10=JSON, 0x00=raw) + compression (0x00=none)
//   [Byte 3] 0x00 reserved
//   [Bytes 4-7] payload length big-endian uint32
//   [Bytes 8+]  payload
//
// Authentication (Token method — simplest, recommended by BytePlus docs):
//   Authorization: Bearer; {ACCESS_TOKEN}
//   Note the semicolon after "Bearer" — required by BytePlus

static size_t _buildConfigPkt(uint8_t* buf, size_t bufSz, const String& json) {
    size_t jLen  = json.length();
    size_t total = 8 + jLen;
    if (total > bufSz) return 0;
    buf[0] = 0x11;
    buf[1] = 0x10;   // full_client_request, no flags
    buf[2] = 0x10;   // JSON serialization, no compression
    buf[3] = 0x00;
    buf[4] = (jLen >> 24) & 0xFF;
    buf[5] = (jLen >> 16) & 0xFF;
    buf[6] = (jLen >>  8) & 0xFF;
    buf[7] =  jLen        & 0xFF;
    memcpy(buf + 8, json.c_str(), jLen);
    return total;
}

static size_t _buildAudioPkt(uint8_t* buf, size_t bufSz,
                              const uint8_t* pcm, size_t pcmLen, bool isLast) {
    size_t total = 8 + pcmLen;
    if (total > bufSz) return 0;
    buf[0] = 0x11;
    buf[1] = isLast ? 0x22 : 0x20;   // last vs non-last
    buf[2] = 0x00;                    // raw bytes, no compression
    buf[3] = 0x00;
    buf[4] = (pcmLen >> 24) & 0xFF;
    buf[5] = (pcmLen >> 16) & 0xFF;
    buf[6] = (pcmLen >>  8) & 0xFF;
    buf[7] =  pcmLen        & 0xFF;
    memcpy(buf + 8, pcm, pcmLen);
    return total;
}

static void _parseAsrResponse(const uint8_t* data, size_t len) {
    if (len < 8) return;

    uint8_t msgType = (data[1] >> 4) & 0x0F;
    uint8_t flags   =  data[1]       & 0x0F;
    uint8_t serial  = (data[2] >> 4) & 0x0F;

    // Server error frame
    if (msgType == 0x0F) {
        tftLog(LC_ERR, "ASR: protocol error");
        asrState = ASR_ERROR;
        return;
    }

    // Determine payload offset (optional 4-byte sequence if flags bit 1 set)
    size_t offset = 4;
    if (flags & 0x02) offset += 4;
    if (offset + 4 > len) return;

    uint32_t payloadSize = ((uint32_t)data[offset]     << 24)
                         | ((uint32_t)data[offset + 1] << 16)
                         | ((uint32_t)data[offset + 2] <<  8)
                         |  (uint32_t)data[offset + 3];
    offset += 4;

    if (payloadSize == 0 || offset + payloadSize > len) return;
    if (serial != 0x01) return;   // only handle JSON payloads

    StaticJsonDocument<2048> doc;
    DeserializationError err = deserializeJson(doc, data + offset, payloadSize);
    if (err) {
        tftLogf(LC_ERR, "ASR JSON: %s", err.c_str());
        return;
    }

    int code = doc["code"]     | -1;
    int seq  = doc["sequence"] | 0;

    if (code == 1000) {
        const char* topText = nullptr;
        bool        partial = true;

        JsonArray results = doc["result"].as<JsonArray>();
        if (!results.isNull() && results.size() > 0) {
            topText = results[0]["text"] | nullptr;
            JsonArray utts = results[0]["utterances"].as<JsonArray>();
            if (!utts.isNull() && utts.size() > 0) {
                bool allDef = true;
                for (JsonObject u : utts)
                    if (!(u["definite"] | false)) { allDef = false; break; }
                partial = !allDef;
            } else {
                partial = (seq >= 0);
            }
        }

        if (topText && strlen(topText) > 0) {
            if (!partial || seq < 0) {
                // Final transcript — show in mint colour
                asrFinal    = String(topText);
                asrGotFinal = true;
                char disp[54];
                snprintf(disp, sizeof(disp), "> %s", topText);
                tftLog(LC_TX, disp);
            } else {
                // Partial — show in cyan while speaking
                asrPartial = String(topText);
                char disp[54];
                snprintf(disp, sizeof(disp), "~ %s", topText);
                tftLog(LC_ASR, disp);
            }
        }

        // Negative sequence → server is done
        if (seq < 0 && asrFinal.length() == 0 && asrPartial.length() > 0) {
            asrFinal    = asrPartial;
            asrGotFinal = true;
            char disp[54];
            snprintf(disp, sizeof(disp), "> %s", asrFinal.c_str());
            tftLog(LC_TX, disp);
        }

    } else if (code == 1013) {
        tftLog(LC_WARN, "ASR: silent audio");
        asrState = ASR_DONE;

    } else if (code == 1002) {
        // Auth failed — show specific fix on TFT
        tftLog(LC_ERR, "ASR: auth fail (1002)");
        tftLog(LC_WARN, "Try API KEY as TOKEN");
        asrState = ASR_ERROR;

    } else if (code == 1001) {
        // Invalid parameter — usually wrong cluster name
        tftLog(LC_ERR, "ASR: bad param (1001)");
        tftLog(LC_WARN, "Check CLUSTER in cfg");
        asrState = ASR_ERROR;

    } else if (code != -1) {
        tftLogf(LC_ERR, "ASR code=%d", code);
        asrState = ASR_ERROR;
    }
}

// WebSocket event handler (called by wsClient.loop())
void onAsrEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            asrConnected = true;
            asrState     = ASR_STREAMING;
            tftLog(LC_OK, "ASR connected");
            break;

        case WStype_BIN:
            _parseAsrResponse(payload, length);
            break;

        case WStype_DISCONNECTED:
            asrConnected = false;
            if (asrState != ASR_DONE) asrState = ASR_DONE;
            tftLog(LC_STAT, "ASR disconnected");
            break;

        case WStype_ERROR:
            tftLog(LC_ERR, "ASR WS error");
            asrState = ASR_ERROR;
            break;

        default: break;
    }
}

// ── Build the full_client_request JSON ────────────────────────────────────────
//
// BytePlus credential mapping:
//   app.appid   = BYTEPLUS_APP_ID      (APP ID from console, e.g. "6834823881")
//   app.token   = BYTEPLUS_TOKEN       (ACCESS TOKEN from console)
//   app.cluster = BYTEPLUS_CLUSTER     (API Resource ID, e.g. "volc.bigasr.sauc.duration")
//
// This MUST match what's in your voice_config.h.
//
static String _buildConfigJson() {
    String reqid = "bronny_" + String(++reqCounter);
    return String("{\"app\":{"
        "\"appid\":\""   + String(BYTEPLUS_APP_ID)  + "\","
        "\"token\":\""   + String(BYTEPLUS_TOKEN)   + "\","
        "\"cluster\":\"" + String(BYTEPLUS_CLUSTER) + "\""
    "},"
    "\"user\":{\"uid\":\"bronny\"},"
    "\"audio\":{"
        "\"format\":\"raw\","
        "\"rate\":16000,"
        "\"bits\":16,"
        "\"channel\":1,"
        "\"language\":\"" + String(BYTEPLUS_LANGUAGE) + "\""
    "},"
    "\"request\":{"
        "\"reqid\":\""     + reqid + "\","
        "\"workflow\":\"audio_in,resample,partition,vad,fe,decode\","
        "\"sequence\":1,"
        "\"nbest\":1,"
        "\"show_utterances\":true,"
        "\"result_type\":\"single\""
    "}}");
}

static bool _isNoise(const String& t) {
    String s = t; s.trim(); s.toLowerCase();
    if (s.length() < 3) return true;
    static const char* kW[] = {
        ".","..","...","ah","uh","hm","hmm","mm","um",
        "huh","oh","the","a","i",nullptr
    };
    for (int i = 0; kW[i]; i++)
        if (s == String(kW[i])) return true;
    return false;
}

// ============================================================
// RECORD + STREAM
// Opens WSS to BytePlus, streams 20 ms PCM chunks, receives
// partial transcripts live on TFT.
// Returns true if asrFinal has a usable transcript.
// ============================================================
bool recordAndStream() {
    asrState     = ASR_CONNECTING;
    asrConnected = false;
    asrGotFinal  = false;
    asrFinal     = "";
    asrPartial   = "";

    // ── Authentication header ───────────────────────────────────────────
    // BytePlus Token method (from official docs):
    //   Authorization: Bearer; {access_token}
    //                         ^ semicolon + space — BytePlus specific
    String authHdr = "Authorization: Bearer; " + String(BYTEPLUS_TOKEN);

    // ── Connect WSS ─────────────────────────────────────────────────────
    wsClient.onEvent(onAsrEvent);
    wsClient.setExtraHeaders(authHdr.c_str());
    wsClient.beginSSL(BYTEPLUS_ASR_HOST, 443, BYTEPLUS_ASR_PATH);

    tftLogf(LC_STAT, "ASR→%s", BYTEPLUS_ASR_HOST);

    uint32_t deadline = millis() + ASR_CONNECT_MS;
    while (asrState == ASR_CONNECTING && millis() < deadline) {
        wsClient.loop();
        animFace();
        if (faceRedraw) { drawFace(false); faceRedraw = false; }
        delay(8);
    }

    if (!asrConnected || asrState != ASR_STREAMING) {
        tftLog(LC_ERR, "ASR timeout  no connect");
        wsClient.disconnect();
        asrState = ASR_DONE;
        return false;
    }

    // ── Send config frame (full_client_request) ─────────────────────────
    String configJson = _buildConfigJson();
    size_t cfgLen = _buildConfigPkt(s_configPkt, sizeof(s_configPkt), configJson);
    if (cfgLen == 0) {
        tftLog(LC_ERR, "ASR config too large");
        wsClient.disconnect();
        return false;
    }
    wsClient.sendBIN(s_configPkt, cfgLen);
    tftLog(LC_OK, "Listening  speak now...");

    // ── Stream audio chunks ──────────────────────────────────────────────
    bool     voiceStarted = false;
    uint32_t silenceStart = 0;
    uint32_t recDeadline  = millis() + MAX_RECORD_MS;

    while (millis() < recDeadline && !asrGotFinal && asrState == ASR_STREAMING) {
        int bytesRead = mic_stream.readBytes((uint8_t*)s_rawBuf, sizeof(s_rawBuf));
        int frames    = bytesRead / 8;   // 32-bit stereo → 8 bytes per frame
        if (frames <= 0) { wsClient.loop(); yield(); continue; }

        // Convert 32-bit stereo → 16-bit mono
        // INMP441 on left channel, >>11 scales to audible 16-bit range
        int32_t peak = 0;
        for (int i = 0; i < frames; i++) {
            s_pcmBuf[i] = (int16_t)(s_rawBuf[i * 2] >> 11);
            int32_t a   = abs(s_pcmBuf[i]);
            if (a > peak) peak = a;
        }

        // VAD
        if (peak > VAD_THR) {
            voiceStarted = true;
            silenceStart = 0;
        } else if (voiceStarted && silenceStart == 0) {
            silenceStart = millis();
        }

        bool isLast = false;
        if (voiceStarted && silenceStart > 0 &&
            (millis() - silenceStart) >= VAD_SILENCE_MS) {
            isLast = true;
        }
        if (millis() >= recDeadline) isLast = true;

        // Build binary audio frame and send
        size_t pktLen = _buildAudioPkt(s_audioPkt, sizeof(s_audioPkt),
                                       (uint8_t*)s_pcmBuf, frames * 2, isLast);
        if (pktLen > 0) wsClient.sendBIN(s_audioPkt, pktLen);

        // Process incoming WS events (partials arrive here)
        wsClient.loop();

        // Keep face animated
        animFace();
        if (faceRedraw) { drawFace(false); faceRedraw = false; }

        if (isLast) {
            tftLog(LC_STAT, "Processing...");
            asrState = ASR_WAITING_FINAL;
            break;
        }
    }

    if (!voiceStarted) {
        tftLog(LC_STAT, "No voice detected");
        wsClient.disconnect();
        asrState = ASR_DONE;
        return false;
    }

    // ── Wait for final transcript ────────────────────────────────────────
    uint32_t finalDL = millis() + ASR_FINAL_MS;
    while (!asrGotFinal && asrState != ASR_ERROR && millis() < finalDL) {
        wsClient.loop();
        animFace();
        if (faceRedraw) { drawFace(false); faceRedraw = false; }
        delay(12);
    }

    wsClient.disconnect();
    asrConnected = false;
    asrState     = ASR_DONE;

    // Use partial as fallback if final never arrived
    if (asrFinal.length() == 0 && asrPartial.length() > 0) {
        asrFinal = asrPartial;
    }

    return asrFinal.length() > 0;
}

// ============================================================
// RAILWAY  POST /voice/text → returns MP3
// ============================================================
bool callRailway(const char* text) {
    if (!text || text[0] == '\0') return false;

    // Manually JSON-escape the transcript to avoid a heap allocation
    String body = "{\"text\":\"";
    for (const char* p = text; *p; p++) {
        switch (*p) {
            case '"':  body += "\\\""; break;
            case '\\': body += "\\\\"; break;
            case '\n': body += "\\n";  break;
            case '\r': body += "\\r";  break;
            case '\t': body += "\\t";  break;
            default:   body += *p;     break;
        }
    }
    body += "\"}";

    String url = String(AETHER_URL) + "/voice/text";
    tftLog(LC_STAT, "AetherAI thinking...");

    if (!mp3Buf)
        mp3Buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
    if (!mp3Buf) {
        tftLog(LC_ERR, "MP3 buf alloc fail");
        return false;
    }

    bool success = false;

    for (int attempt = 1; attempt <= 2 && !success; attempt++) {
        if (attempt > 1) {
            tftLog(LC_WARN, "Retry 2/2...");
            delay(2000);
        }

        WiFiClientSecure cli;
        cli.setInsecure();
        cli.setConnectionTimeout(20000);

        HTTPClient http;
        http.begin(cli, url);
        http.setTimeout(45000);
        http.addHeader("Content-Type", "application/json");
        http.addHeader("X-Api-Key",    AETHER_API_KEY);

        int code = http.POST(body);

        if (code == 200) {
            WiFiClient* stream = http.getStreamPtr();
            int    clen = http.getSize();
            size_t want = (clen > 0) ? min((size_t)clen, (size_t)MP3_MAX_BYTES)
                                      : (size_t)MP3_MAX_BYTES;
            size_t got  = 0;
            uint32_t dlEnd = millis() + 35000;

            while ((http.connected() || stream->available()) && got < want && millis() < dlEnd) {
                if (stream->available()) {
                    size_t chunk = min((size_t)1024, want - got);
                    got += stream->readBytes(mp3Buf + got, chunk);
                } else {
                    delay(2);
                }
                yield();
            }
            mp3Len  = got;
            success = (got > 0);
            tftLogf(LC_OK, "Got MP3  %u B", (unsigned)mp3Len);
        } else {
            tftLogf(LC_ERR, "Railway HTTP %d", code);
        }
        http.end();
        yield();
    }
    return success;
}

// ============================================================
// MP3 PLAYBACK  via ES8311
// ============================================================
void playMp3() {
    if (!mp3Len || !mp3Buf) {
        tftLog(LC_ERR, "No MP3 to play");
        return;
    }

    // Switch to TTS sample rate
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

        while (copier.copy()) {
            animFace();
            if (faceRedraw) { drawFace(false); faceRedraw = false; }
            yield();
        }
        decoded.end();
    }

    // Trailing silence to prevent speaker click
    { int16_t sil[128]; memset(sil, 0, sizeof(sil)); i2s.write((uint8_t*)sil, sizeof(sil)); }
    delay(60);

    // Restore recording mode
    audioInitRec();
    micInit();

    // Drain any echo picked up during playback
    if (micOk) {
        uint8_t drain[512];
        uint32_t end = millis() + 300;
        while (millis() < end) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
    }
}

// ============================================================
// CONVERSATION — full turn
// ============================================================
void runConversation() {
    if (busy) return;
    busy = true;

    // 1. Listen + stream ASR
    setFaceState(FS_LISTENING);
    setStatus("Listening...", C_GR);

    if (!micOk) {
        tftLog(LC_ERR, "Mic not ready");
        setStatus("Mic error", C_RD);
        setFaceState(FS_ERROR);
        delay(2000);
        setFaceState(FS_IDLE);
        setStatus("Ready", C_CY);
        busy = false;
        return;
    }

    bool gotTranscript = recordAndStream();

    if (!gotTranscript || _isNoise(asrFinal)) {
        tftLog(LC_STAT, "Nothing recognized");
        setFaceState(FS_IDLE);
        setStatus("Ready", C_CY);
        busy = false;
        return;
    }

    // 2. Railway LLM + TTS
    setFaceState(FS_THINKING);
    setStatus("Thinking...", C_PURP);

    bool ok = callRailway(asrFinal.c_str());
    if (!ok) {
        tftLog(LC_ERR, "Server failed");
        setStatus("Server error", C_RD);
        setFaceState(FS_ERROR);
        delay(2500);
        setFaceState(FS_IDLE);
        setStatus("Ready", C_CY);
        busy = false;
        return;
    }

    // 3. Speak
    setFaceState(FS_SPEAKING);
    setStatus("Speaking...", C_GR);
    playMp3();

    // Done
    setFaceState(FS_IDLE);
    setStatus("Ready", C_CY);
    busy = false;
}

// ============================================================
// SETUP
// ============================================================
void setup() {
    // Suppress AudioTools library logs — TFT is our only output
    Serial.begin(115200);  // required by AudioTools internals; we don't print to it
    AudioToolsLogger.begin(Serial, AudioToolsLogLevel::Error);

    // ── TFT init ───────────────────────────────────────────────────────
    pinMode(PIN_TFT_BLK, OUTPUT);
    digitalWrite(PIN_TFT_BLK, HIGH);
    tftSPI.begin(PIN_TFT_CLK, -1, PIN_TFT_MOSI, PIN_TFT_CS);
    tft.init(240, 320);
    tft.setRotation(3);
    tft.fillScreen(C_BK);
    tftReady = true;

    // ── Boot splash (clears once face is drawn) ────────────────────────
    tft.setTextSize(2);
    tft.setTextColor(C_CY);
    tft.setCursor((W - 10 * 12) / 2, 40);
    tft.print("BRONNY AI");
    tft.setTextSize(1);
    tft.setTextColor(C_DCY);
    tft.setCursor((W - 5 * 6) / 2, 70);
    tft.print("v7.1");
    tft.setTextColor(C_DG);
    tft.setCursor((W - 16 * 6) / 2, 86);
    tft.print("by Patrick Perez");

    // Separator line above log zone
    tft.drawFastHLine(0, LOG_Y - 2, W, C_DCY);

    // ── Audio codec ────────────────────────────────────────────────────
    tftLog(LC_STAT, "Init codec...");
    audioInitRec();

    // ── Microphone ─────────────────────────────────────────────────────
    micInit();

    // ── MP3 decoder + PSRAM buffer ─────────────────────────────────────
    mp3Decoder.begin();
    mp3Buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
    tftLogf(mp3Buf ? LC_OK : LC_ERR, "PSRAM buf %s", mp3Buf ? "OK" : "FAIL");

    tftLogf(LC_STAT, "Heap %uK  PSRAM %uK",
            esp_get_free_heap_size() / 1024,
            heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024);

    // ── WiFi ───────────────────────────────────────────────────────────
    setStatus("WiFi...", C_YL);
    wifiConnect();
    setStatus(WiFi.isConnected() ? "Online" : "No WiFi",
              WiFi.isConnected() ? C_GR    : C_RD);
    delay(400);

    // ── Initial heartbeat ──────────────────────────────────────────────
    // First heartbeat registers Bronny as online in command center
    // and creates the "[Bronny] Device connected" task in the task list
    tftLog(LC_STAT, "Heartbeat...");
    sendHeartbeat();
    lastHbMs = millis();

    // ── Draw face ──────────────────────────────────────────────────────
    tft.fillRect(0, 0, W, LOG_Y, C_BK);   // clear splash text
    setFaceState(FS_IDLE);
    drawFace(true);
    setStatus("Ready", C_CY);

    // ── Final ready message ────────────────────────────────────────────
    tftLogf(LC_STAT, "VAD thr=%d  speak to start", VAD_THR);
    tftLog(LC_OK,   "Bronny v7.1 ready");
}

// ============================================================
// LOOP
// ============================================================
void loop() {
    uint32_t now = millis();

    // ── Face animation ────────────────────────────────────────────────
    animFace();
    if (faceRedraw) { drawFace(false); faceRedraw = false; }

    // ── Periodic heartbeat ────────────────────────────────────────────
    if (now - lastHbMs > HEARTBEAT_MS) {
        lastHbMs = now;
        sendHeartbeat();
    }

    // ── VAD auto-trigger ──────────────────────────────────────────────
    if (!busy && micOk) {
        static int32_t  vadBuf[32];
        static int32_t  vadPeak  = 0;
        static uint32_t vadLogMs = 0;

        int rd     = mic_stream.readBytes((uint8_t*)vadBuf, sizeof(vadBuf));
        int frames = rd / 8;
        for (int i = 0; i < frames; i++) {
            int32_t v = abs((int16_t)(vadBuf[i * 2] >> 11));
            if (v > vadPeak) vadPeak = v;
        }

        // Periodic mic level display — useful for VAD threshold tuning
        // Shows every 8 s so it doesn't spam the log
        if (now - vadLogMs > 8000) {
            tftLogf(LC_STAT, "mic peak=%d thr=%d", (int)vadPeak, VAD_THR);
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
