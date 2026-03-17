/*
 * ╔══════════════════════════════════════════════════════════╗
 * ║         BRONNY AI  v7.0  —  AetherAI Edition             ║
 * ║         by Patrick Perez                                 ║
 * ╠══════════════════════════════════════════════════════════╣
 * ║  Hardware                                                ║
 * ║    Board   : ESP32-S3 Dev Module  (OPI PSRAM 8MB)        ║
 * ║    Codec   : ES8311  (I2C addr 0x18) — speaker output    ║
 * ║    Mic     : INMP441  (I2S port 1)                       ║
 * ║    Display : ST7789  320×240  (HSPI)                     ║
 * ║    Button  : GPIO0 (BOOT, active LOW — not used in v7)   ║
 * ╠══════════════════════════════════════════════════════════╣
 * ║  Wiring                                                  ║
 * ║    ES8311 codec                                          ║
 * ║      PA_EN→48  DOUT→45  DIN→12  WS→13  BCLK→14           ║
 * ║      MCLK→38   SCL→2    SDA→1                            ║
 * ║    INMP441 mic                                           ║
 * ║      VDD→3.3V  GND→GND  L/R→GND                          ║
 * ║      WS→4  SCK→5  SD→6                                   ║
 * ║    ST7789 TFT  (HSPI)                                    ║
 * ║      DC→39  CS→47  CLK→41  MOSI→40  BLK→42               ║
 * ╠══════════════════════════════════════════════════════════╣
 * ║  Required Libraries (Arduino Library Manager)            ║
 * ║    • arduino-audio-tools  by pschatzmann                 ║
 * ║    • arduino-audio-driver by pschatzmann                 ║
 * ║    • Adafruit ST7789  + Adafruit GFX Library             ║
 * ║    • WebSockets by Markus Sattler  (arduinoWebSockets)   ║
 * ║    • ArduinoJson  by Benoit Blanchon                     ║
 * ╠══════════════════════════════════════════════════════════╣
 * ║  Arduino IDE Board Settings                              ║
 * ║    Board:  ESP32S3 Dev Module                            ║
 * ║    PSRAM:  OPI PSRAM  (8MB) ← REQUIRED                   ║
 * ║    USB CDC on Boot: Enabled                              ║
 * ╠══════════════════════════════════════════════════════════╣
 * ║  Streaming Pipeline                                      ║
 * ║    INMP441 → VAD → BytePlus streaming ASR WebSocket      ║
 * ║    → partial transcripts shown on TFT in real-time       ║
 * ║    → final transcript → Railway /voice/text              ║
 * ║    → Qwen LLM + edge-tts → MP3 → ES8311 → speaker        ║
 * ║                                                          ║
 * ║  Command Center                                          ║
 * ║    • Bronny badge goes ONLINE when WiFi connects         ║
 * ║    • Each conversation creates a task in the task list   ║
 * ║    • Transcript visible in task detail                   ║
 * ║    • Heartbeat every 30 s keeps badge alive              ║
 * ╚══════════════════════════════════════════════════════════╝
 */

// ============================================================
// INCLUDES
// ============================================================
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>       // arduinoWebSockets by Markus Sattler
#include <ArduinoJson.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <math.h>

#include "AudioTools.h"
#include "AudioTools/AudioLibs/I2SCodecStream.h"
#include "AudioTools/CoreAudio/AudioI2S/I2SStream.h"

#if __has_include("AudioTools/AudioCodecs/CodecMP3Helix.h")
  #include "AudioTools/AudioCodecs/CodecMP3Helix.h"
#elif __has_include("AudioCodecs/CodecMP3Helix.h")
  #include "AudioCodecs/CodecMP3Helix.h"
#else
  #error "CodecMP3Helix.h not found — install arduino-audio-tools"
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
#define C_DG    0x39E7
#define C_ORG   0xFD20
#define C_PURP  0x901F
#define C_MINT  0x3FF7

// Face geometry
#define FCX  160      // face centre X
#define FCY   92      // face centre Y (eyes at this Y)

// Island status bar (bottom)
#define ISL_W  200
#define ISL_H   16
#define ISL_X  ((W - ISL_W) / 2)
#define ISL_Y  (H - ISL_H - 4)
#define ISL_R    8

// Transcript text zone (between face and island bar)
#define TXT_Y  176
#define TXT_H   40

// ============================================================
// BEHAVIOUR CONSTANTS
// ============================================================
#define VAD_THR         BRONNY_VAD_THR  // from voice_config.h
#define VAD_SILENCE_MS  1600            // ms of silence after speech → end
#define MAX_RECORD_MS   12000           // absolute max recording per turn
#define ASR_CONNECT_MS  7000            // WS connection timeout
#define ASR_FINAL_MS    5000            // wait-for-final timeout after last chunk
#define HEARTBEAT_MS    30000           // heartbeat interval to Railway
#define MP3_MAX_BYTES   (320 * 1024)    // 320 KB ~ 15 s @ 160 kbps
#define CHUNK_FRAMES    320             // 20 ms @ 16 kHz
#define CHUNK_BYTES     (CHUNK_FRAMES * 2) // 16-bit mono = 640 bytes

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

// MP3 playback
static MP3DecoderHelix  mp3Decoder;
static uint8_t*         mp3Buf = nullptr;
static size_t           mp3Len = 0;

// ============================================================
// DISPLAY
// ============================================================
SPIClass        tftSPI(HSPI);
Adafruit_ST7789 tft = Adafruit_ST7789(&tftSPI, PIN_TFT_CS, PIN_TFT_DC, -1);

// ============================================================
// FACE STATE MACHINE
// ============================================================
enum FaceState { FS_IDLE, FS_LISTENING, FS_THINKING, FS_SPEAKING, FS_ERROR };
static FaceState  gFaceState   = FS_IDLE;
static bool       faceRedraw   = false;

// Animation variables
static float  bobPhase   = 0.f;
static float  bobY       = 0.f;
static float  eyeOpenL   = 1.f;   // 0=closed, 1=open
static float  eyeOpenR   = 1.f;
static float  tgtEyeOpen = 1.f;
static float  mouthOpen  = 0.f;
static float  talkPh     = 0.f;
static uint32_t lastFaceMs  = 0;
static uint32_t lastBlinkMs = 0;
static uint32_t nextBlinkMs = 3000;
static bool     blinking    = false;
static int      blinkFrame  = 0;

// Island bar
static String   islandText  = "Booting...";
static uint16_t islandColor = C_DCY;

// ============================================================
// STREAMING ASR STATE
// ============================================================
WebSocketsClient wsClient;

enum AsrState { ASR_IDLE, ASR_CONNECTING, ASR_STREAMING, ASR_WAITING_FINAL, ASR_DONE, ASR_ERROR };
static AsrState  asrState     = ASR_IDLE;
static bool      asrConnected = false;
static String    asrPartial   = "";
static String    asrFinal     = "";
static bool      asrGotFinal  = false;
static uint32_t  reqCounter   = 0;

// ============================================================
// GLOBAL AUDIO BUFFERS  (not on stack — safer for large arrays)
// ============================================================
static int32_t  s_rawBuf[CHUNK_FRAMES * 2];           // 32-bit stereo from INMP441
static int16_t  s_pcmBuf[CHUNK_FRAMES];               // 16-bit mono for ASR
static uint8_t  s_audioPkt[8 + CHUNK_BYTES + 4];      // binary ASR frame (header+size+audio)
static uint8_t  s_configPkt[8 + 600];                 // binary ASR frame for config JSON

// ============================================================
// MISC
// ============================================================
static bool      busy         = false;
static uint32_t  lastHbMs     = 0;

// ============================================================
// FORWARD DECLARATIONS
// ============================================================
void drawFace(bool full);
void animFace();
void drawIslandBar();
void setStatus(const char* s, uint16_t c);
void setFaceState(FaceState s);
void showTranscript(const String& text);
void onAsrEvent(WStype_t type, uint8_t* payload, size_t length);

// ============================================================
// AUDIO INIT
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
        Serial.printf("[Audio] initRec %s\n", audioOk ? "OK" : "FAIL");
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
        Serial.printf("[Audio] initTTS %s\n", audioOk ? "OK" : "FAIL");
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
        // Warm-up drain
        uint8_t tmp[512];
        uint32_t e = millis() + 350;
        while (millis() < e) { mic_stream.readBytes(tmp, sizeof(tmp)); yield(); }
    }
    Serial.printf("[Mic]   INMP441 %s\n", micOk ? "OK" : "FAIL");
}

// ============================================================
// DISPLAY — ISLAND BAR
// ============================================================

void drawIslandBar() {
    tft.fillRect(ISL_X - 2, ISL_Y - 2, ISL_W + 4, ISL_H + 4, C_BK);
    tft.fillRoundRect(ISL_X,     ISL_Y,     ISL_W,     ISL_H,     ISL_R, C_BK);
    tft.drawRoundRect(ISL_X,     ISL_Y,     ISL_W,     ISL_H,     ISL_R, islandColor);
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
// DISPLAY — TRANSCRIPT ZONE
// ============================================================

void showTranscript(const String& text) {
    tft.fillRect(0, TXT_Y, W, TXT_H, C_BK);
    if (text.length() == 0) return;
    tft.setTextSize(1);
    tft.setTextColor(C_LG);

    // Fit into 2 lines of 52 chars each
    int maxChars = 52;
    String line1 = "", line2 = "";
    if ((int)text.length() <= maxChars) {
        line1 = text;
    } else {
        // Try to break at a space near the midpoint
        int breakAt = text.lastIndexOf(' ', maxChars);
        if (breakAt < 10) breakAt = maxChars;
        line1 = text.substring(0, breakAt);
        line2 = text.substring(breakAt + 1, min((int)text.length(), breakAt + 1 + maxChars));
    }

    tft.setCursor(4, TXT_Y + 2);
    tft.print(line1);
    if (line2.length() > 0) {
        tft.setCursor(4, TXT_Y + 14);
        tft.print(line2);
    }
}

// ============================================================
// DISPLAY — ROBOT FACE
// ============================================================

/*
 * Face layout (landscape 320×240):
 *   Eyes:  centre at (108, FCY) and (212, FCY)
 *          size 90×52, corner radius 18
 *   Mouth: centre at (160, FCY+58)
 *   Bob:   ±5 px vertical sine wave
 */

static float prevBobY   = 0.f;
static float prevOpenL  = 1.f;
static float prevOpenR  = 1.f;
static FaceState prevFaceState = FS_IDLE;

void drawEye(int cx, int cy, float openFrac, uint16_t col, FaceState state) {
    int ew = 90;
    int eh = max(2, (int)(52 * openFrac));
    int er = min(18, min(ew / 2, eh / 2));
    tft.fillRoundRect(cx - ew / 2, cy - eh / 2, ew, eh, er, col);

    // Thinking squint: fill upper half black
    if (state == FS_THINKING && eh > 10) {
        tft.fillRect(cx - ew / 2 + 4, cy - eh / 2, ew - 8, eh / 2 + 2, C_BK);
    }
}

void drawMouth(int cx, int my, FaceState state, float mOpen) {
    switch (state) {
        case FS_IDLE:
        case FS_THINKING: {
            // Smile: bottom half of a circle
            tft.fillCircle(cx, my, 27, C_WH);
            tft.fillRect(cx - 29, my - 29, 58, 29, C_BK);  // mask top half
            tft.fillCircle(cx, my, 19, C_BK);               // hollow centre
            break;
        }
        case FS_LISTENING: {
            // Neutral line
            tft.fillRoundRect(cx - 30, my - 5, 60, 10, 5, C_WH);
            break;
        }
        case FS_SPEAKING: {
            // Animated open oval — height driven by mOpen [0..1]
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
    int by  = (int)bobY;
    int lex = FCX - 52;
    int rex = FCX + 52;
    int ey  = FCY + by;
    int my  = FCY + 58 + by;

    uint16_t eyeCol = (gFaceState == FS_ERROR) ? C_RD : C_WH;

    if (full) {
        tft.fillRect(0, 0, W, TXT_Y, C_BK);
    } else {
        // Incremental erase: only redraw regions that changed
        bool eyeChg   = fabsf(bobY - prevBobY) > 0.4f
                      || fabsf(eyeOpenL - prevOpenL) > 0.02f
                      || fabsf(eyeOpenR - prevOpenR) > 0.02f;
        bool stateChg = (gFaceState != prevFaceState);

        if (!eyeChg && !stateChg) return;

        // Erase eye regions
        int prevBy = (int)prevBobY;
        tft.fillRect(lex - 47, FCY + prevBy - 34, 94, 68, C_BK);
        tft.fillRect(rex - 47, FCY + prevBy - 34, 94, 68, C_BK);
        // Erase mouth region
        tft.fillRect(FCX - 36, FCY + 24 + prevBy, 72, 54, C_BK);
    }

    // Draw eyes
    drawEye(lex, ey, eyeOpenL, eyeCol, gFaceState);
    drawEye(rex, ey, eyeOpenR, eyeCol, gFaceState);

    // Draw mouth
    drawMouth(FCX, my, gFaceState, mouthOpen);

    prevBobY      = bobY;
    prevOpenL     = eyeOpenL;
    prevOpenR     = eyeOpenR;
    prevFaceState = gFaceState;
}

// ============================================================
// FACE ANIMATION — call every frame from main loop
// ============================================================

void animFace() {
    uint32_t now = millis();
    if (now - lastFaceMs < 16) return;   // ~60 fps cap
    lastFaceMs = now;
    bool ch = false;

    // Bob — gentle vertical sine
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
            blinking   = false;
            tgtEyeOpen = 1.f;
            lastBlinkMs = now;
        }
        ch = true;
    }

    // Smooth eye open
    float dL = tgtEyeOpen - eyeOpenL;
    float dR = tgtEyeOpen - eyeOpenR;
    if (fabsf(dL) > 0.01f) { eyeOpenL += dL * 0.3f; ch = true; }
    if (fabsf(dR) > 0.01f) { eyeOpenR += dR * 0.3f; ch = true; }

    // Talking mouth
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
    Serial.printf("[WiFi]  Connecting to %s", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    int tries = 0;
    while (WiFi.status() != WL_CONNECTED && tries < 40) {
        delay(400);
        Serial.print(".");
        yield();
        tries++;
    }
    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("\n[WiFi]  Connected — IP: %s\n", WiFi.localIP().toString().c_str());
    } else {
        Serial.println("\n[WiFi]  FAILED — check SSID/password in voice_config.h");
    }
}

// ============================================================
// HEARTBEAT  → Railway /bronny/heartbeat  (keeps badge ONLINE)
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
    int code = http.POST("{\"device\":\"bronny\",\"version\":\"7.0\"}");
    http.end();
    Serial.printf("[HB]    → %d\n", code);
}

// ============================================================
// BYTEPLUS STREAMING ASR — BINARY WEBSOCKET PROTOCOL
// ============================================================
//
// Protocol reference: https://docs.byteplus.com/en/docs/speech/docs-real-time-speech-recog
//
// Each WebSocket binary frame = [4-byte header][4-byte payload_size][payload]
//
// Byte 0: 0x11 = protocol version 1, header size 4
// Byte 1: message type + flags
//         0x10 = full_client_request (first packet with config JSON)
//         0x20 = audio_only_request  (non-last audio chunk)
//         0x22 = audio_only_request  (LAST audio chunk — signals end)
//         0x90 = full_server_response (incoming from BytePlus)
//         0xF0 = error response (incoming)
// Byte 2: serialization + compression
//         0x10 = JSON + no compression (for config)
//         0x00 = none + no compression (for audio)
// Byte 3: 0x00 (reserved)
// Bytes 4–7: payload length (big-endian uint32)
// Bytes 8+:  payload

// Build the first packet (config JSON with app/audio/request params)
static size_t buildConfigPacket(uint8_t* buf, size_t bufSize, const String& json) {
    size_t jLen = json.length();
    size_t total = 8 + jLen;
    if (total > bufSize) return 0;
    buf[0] = 0x11;  // version=1, headerSize=4
    buf[1] = 0x10;  // full_client_request, no flags
    buf[2] = 0x10;  // JSON serialization, no compression
    buf[3] = 0x00;  // reserved
    buf[4] = (jLen >> 24) & 0xFF;
    buf[5] = (jLen >> 16) & 0xFF;
    buf[6] = (jLen >> 8)  & 0xFF;
    buf[7] =  jLen        & 0xFF;
    memcpy(buf + 8, json.c_str(), jLen);
    return total;
}

// Build an audio-only packet (raw PCM, no header on the PCM itself)
static size_t buildAudioPacket(uint8_t* buf, size_t bufSize,
                                const uint8_t* pcm, size_t pcmLen, bool isLast) {
    size_t total = 8 + pcmLen;
    if (total > bufSize) return 0;
    buf[0] = 0x11;
    buf[1] = isLast ? 0x22 : 0x20;  // last or non-last
    buf[2] = 0x00;  // no serialization (raw bytes), no compression
    buf[3] = 0x00;
    buf[4] = (pcmLen >> 24) & 0xFF;
    buf[5] = (pcmLen >> 16) & 0xFF;
    buf[6] = (pcmLen >> 8)  & 0xFF;
    buf[7] =  pcmLen        & 0xFF;
    memcpy(buf + 8, pcm, pcmLen);
    return total;
}

// Parse a binary server response frame
static void parseAsrResponse(const uint8_t* data, size_t len) {
    if (len < 8) return;

    uint8_t msgType = (data[1] >> 4) & 0x0F;
    uint8_t flags   = data[1] & 0x0F;
    uint8_t serial  = (data[2] >> 4) & 0x0F;

    // Error frame
    if (msgType == 0x0F) {
        Serial.printf("[ASR]   Server error frame  len=%d\n", (int)len);
        asrState = ASR_ERROR;
        return;
    }

    // Determine payload offset:
    //   4 bytes header
    //   +4 bytes optional sequence number if flags bit 1 (0x02) is set
    //   +4 bytes payload size
    size_t offset = 4;
    if (flags & 0x02) offset += 4;  // skip optional sequence number

    if (offset + 4 > len) return;
    uint32_t payloadSize = ((uint32_t)data[offset]     << 24)
                         | ((uint32_t)data[offset + 1] << 16)
                         | ((uint32_t)data[offset + 2] << 8)
                         |  (uint32_t)data[offset + 3];
    offset += 4;

    if (payloadSize == 0 || offset + payloadSize > len) return;

    // Deserialise JSON payload (only when serialization = JSON, i.e. serial = 0x01)
    if (serial != 0x01) return;

    StaticJsonDocument<2048> doc;
    DeserializationError err = deserializeJson(doc, data + offset, payloadSize);
    if (err) {
        Serial.printf("[ASR]   JSON error: %s\n", err.c_str());
        return;
    }

    int  code    = doc["code"]     | -1;
    int  seq     = doc["sequence"] | 0;
    Serial.printf("[ASR]   code=%d seq=%d\n", code, seq);

    if (code == 1000) {
        // Extract the recognised text
        const char* topText = nullptr;
        bool        partial = true;

        // Try result[0].text (primary field)
        JsonArray results = doc["result"].as<JsonArray>();
        if (!results.isNull() && results.size() > 0) {
            topText = results[0]["text"] | nullptr;

            // Determine partial vs final via utterances[].definite
            JsonArray utts = results[0]["utterances"].as<JsonArray>();
            if (!utts.isNull() && utts.size() > 0) {
                bool allDef = true;
                for (JsonObject u : utts) {
                    if (!(u["definite"] | false)) { allDef = false; break; }
                }
                partial = !allDef;
            } else {
                // No utterances: use sequence sign to decide
                partial = (seq >= 0);
            }
        }

        if (topText && strlen(topText) > 0) {
            if (!partial || seq < 0) {
                // Final transcript
                asrFinal    = String(topText);
                asrGotFinal = true;
                Serial.printf("[ASR]   FINAL: %s\n", topText);
                showTranscript(asrFinal);
            } else {
                // Partial — show on TFT in real-time
                asrPartial = String(topText);
                Serial.printf("[ASR]   partial: %s\n", topText);
                showTranscript(asrPartial);
            }
        }

        // A negative sequence number always means the server is done
        if (seq < 0 && asrFinal.length() == 0 && asrPartial.length() > 0) {
            asrFinal    = asrPartial;
            asrGotFinal = true;
            Serial.printf("[ASR]   FINAL (neg seq): %s\n", asrFinal.c_str());
        }

    } else if (code == 1013) {
        // Silent audio — not an error, just nothing recognised
        Serial.println("[ASR]   Silent audio (code 1013)");
        asrState = ASR_DONE;
    } else if (code != -1) {
        Serial.printf("[ASR]   Error code=%d\n", code);
        asrState = ASR_ERROR;
    }
}

// WebSocket event handler (registered with wsClient.onEvent)
void onAsrEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            asrConnected = true;
            asrState     = ASR_STREAMING;
            Serial.println("[ASR]   WS connected");
            break;

        case WStype_BIN:
            // Server sends binary frames — parse them
            parseAsrResponse(payload, length);
            break;

        case WStype_TEXT:
            // Unexpected text frame — log it
            Serial.printf("[ASR]   Unexpected TEXT: %.*s\n", (int)min(length,(size_t)200), payload);
            break;

        case WStype_DISCONNECTED:
            asrConnected = false;
            if (asrState != ASR_DONE) asrState = ASR_DONE;
            Serial.println("[ASR]   WS disconnected");
            break;

        case WStype_ERROR:
            Serial.println("[ASR]   WS error");
            asrState = ASR_ERROR;
            break;

        default:
            break;
    }
}

// ── Build config JSON ──────────────────────────────────────────────────────
static String buildConfigJson() {
    String reqid = String("bronny_") + String(++reqCounter);
    String j = "{\"app\":{\"appid\":\"" + String(BYTEPLUS_APP_ID)
             + "\",\"token\":\""        + String(BYTEPLUS_TOKEN)
             + "\",\"cluster\":\""      + String(BYTEPLUS_CLUSTER)
             + "\"},\"user\":{\"uid\":\"bronny\"},"
               "\"audio\":{\"format\":\"raw\",\"rate\":16000,\"bits\":16,\"channel\":1,"
               "\"language\":\"" + String(BYTEPLUS_LANGUAGE) + "\"},"
               "\"request\":{\"reqid\":\"" + reqid + "\","
               "\"workflow\":\"audio_in,resample,partition,vad,fe,decode\","
               "\"sequence\":1,\"nbest\":1,\"show_utterances\":true,"
               "\"result_type\":\"single\"}}";
    return j;
}

// ── Noise filter ────────────────────────────────────────────────────────────
static bool isNoise(const String& t) {
    String s = t; s.trim(); s.toLowerCase();
    if (s.length() < 3) return true;
    static const char* kWords[] = {
        ".", "..", "...", "ah", "uh", "hm", "hmm", "mm", "um", "huh",
        "oh", "the", "a", "i", nullptr
    };
    for (int i = 0; kWords[i]; i++)
        if (s == String(kWords[i])) return true;
    return false;
}

// ============================================================
// RECORD + STREAM  — core streaming ASR function
// Connects BytePlus WebSocket, streams PCM chunks while recording,
// receives partial transcripts in real-time, returns final text in asrFinal.
// Returns true if a usable transcript was obtained.
// ============================================================
bool recordAndStream() {
    // ── Reset ASR state ────────────────────────────────────────
    asrState     = ASR_CONNECTING;
    asrConnected = false;
    asrGotFinal  = false;
    asrFinal     = "";
    asrPartial   = "";

    // ── Build auth header — BytePlus uses "Bearer; {token}" (semicolon) ──
    String authHdr = "Authorization: Bearer; " + String(BYTEPLUS_TOKEN);

    // ── Open WebSocket connection ──────────────────────────────
    wsClient.onEvent(onAsrEvent);
    wsClient.setExtraHeaders(authHdr.c_str());
    wsClient.beginSSL(BYTEPLUS_ASR_HOST, 443, BYTEPLUS_ASR_PATH);

    Serial.printf("[ASR]   Connecting wss://%s%s\n", BYTEPLUS_ASR_HOST, BYTEPLUS_ASR_PATH);

    // Wait up to ASR_CONNECT_MS for connection
    uint32_t deadline = millis() + ASR_CONNECT_MS;
    while (asrState == ASR_CONNECTING && millis() < deadline) {
        wsClient.loop();
        delay(8);
    }
    if (!asrConnected || asrState != ASR_STREAMING) {
        Serial.println("[ASR]   Connection timeout or error");
        wsClient.disconnect();
        asrState = ASR_DONE;
        return false;
    }

    // ── Send config / full-client-request ─────────────────────
    String configJson = buildConfigJson();
    size_t cfgLen = buildConfigPacket(s_configPkt, sizeof(s_configPkt), configJson);
    if (cfgLen == 0) {
        Serial.println("[ASR]   Config JSON too large");
        wsClient.disconnect();
        return false;
    }
    wsClient.sendBIN(s_configPkt, cfgLen);
    Serial.println("[ASR]   Config sent — streaming audio...");

    // ── Stream audio chunks while recording ───────────────────
    bool     voiceStarted = false;
    uint32_t silenceStart = 0;
    uint32_t recDeadline  = millis() + MAX_RECORD_MS;

    while (millis() < recDeadline && !asrGotFinal && asrState == ASR_STREAMING) {
        // Read one 20 ms chunk from INMP441
        int bytesRead = mic_stream.readBytes((uint8_t*)s_rawBuf, sizeof(s_rawBuf));
        int frames    = bytesRead / 8;  // 8 bytes per frame: 32-bit left + 32-bit right
        if (frames <= 0) { wsClient.loop(); yield(); continue; }

        // Convert 32-bit left channel → 16-bit mono (INMP441 >> 11 for proper amplitude)
        int32_t peak = 0;
        for (int i = 0; i < frames; i++) {
            s_pcmBuf[i] = (int16_t)(s_rawBuf[i * 2] >> 11);
            int32_t a = abs(s_pcmBuf[i]);
            if (a > peak) peak = a;
        }

        // VAD: track speech start and silence
        if (peak > VAD_THR) {
            voiceStarted = true;
            silenceStart = 0;
        } else if (voiceStarted && silenceStart == 0) {
            silenceStart = millis();
        }

        bool isLast = false;
        if (voiceStarted && silenceStart > 0 && (millis() - silenceStart) >= VAD_SILENCE_MS) {
            isLast = true;
        }
        if (millis() >= recDeadline) isLast = true;

        // Build and send binary audio packet
        size_t pktLen = buildAudioPacket(s_audioPkt, sizeof(s_audioPkt),
                                         (uint8_t*)s_pcmBuf, frames * 2, isLast);
        if (pktLen > 0) wsClient.sendBIN(s_audioPkt, pktLen);

        // Process incoming WS events (partial transcripts arrive here)
        wsClient.loop();

        // Keep face animated during recording
        animFace();
        if (faceRedraw) { drawFace(false); faceRedraw = false; }

        if (isLast) {
            Serial.println("[ASR]   Last chunk sent");
            asrState = ASR_WAITING_FINAL;
            break;
        }
    }

    if (!voiceStarted) {
        Serial.println("[ASR]   No voice detected");
        wsClient.disconnect();
        asrState = ASR_DONE;
        return false;
    }

    // ── Wait for final transcript (up to ASR_FINAL_MS) ────────
    uint32_t finalDeadline = millis() + ASR_FINAL_MS;
    while (!asrGotFinal && asrState != ASR_ERROR && millis() < finalDeadline) {
        wsClient.loop();
        animFace();
        if (faceRedraw) { drawFace(false); faceRedraw = false; }
        delay(12);
    }

    wsClient.disconnect();
    asrConnected = false;
    asrState     = ASR_DONE;

    // Fallback: use partial if final never arrived
    if (asrFinal.length() == 0 && asrPartial.length() > 0) {
        asrFinal = asrPartial;
        Serial.printf("[ASR]   Using partial as final: %s\n", asrFinal.c_str());
    }

    return asrFinal.length() > 0;
}

// ============================================================
// RAILWAY API — POST /voice/text → get MP3 back
// ============================================================
bool callRailway(const char* text) {
    if (!text || text[0] == '\0') return false;

    // JSON-escape the transcript
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
    Serial.printf("[Rail]  POST %s  body=%s\n", url.c_str(), body.c_str());

    // Allocate MP3 buffer in PSRAM (once)
    if (!mp3Buf)
        mp3Buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
    if (!mp3Buf) {
        Serial.println("[Rail]  mp3Buf alloc FAILED");
        return false;
    }

    bool success = false;

    for (int attempt = 1; attempt <= 2 && !success; attempt++) {
        if (attempt > 1) { Serial.println("[Rail]  Retry 2/2..."); delay(2000); }

        WiFiClientSecure cli;
        cli.setInsecure();
        cli.setConnectionTimeout(20000);

        HTTPClient http;
        http.begin(cli, url);
        http.setTimeout(45000);
        http.addHeader("Content-Type", "application/json");
        http.addHeader("X-Api-Key",    AETHER_API_KEY);

        int code = http.POST(body);
        Serial.printf("[Rail]  HTTP %d\n", code);

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
            Serial.printf("[Rail]  MP3 %u bytes received\n", (unsigned)mp3Len);
        } else {
            Serial.printf("[Rail]  Error: %s\n", http.getString().c_str());
        }
        http.end();
        yield();
    }
    return success;
}

// ============================================================
// MP3 PLAYBACK via ES8311
// ============================================================
void playMp3() {
    if (!mp3Len || !mp3Buf) {
        Serial.println("[Play]  No MP3 data");
        return;
    }
    Serial.printf("[Play]  %u bytes\n", (unsigned)mp3Len);

    // Switch from recording to TTS mode
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

    // Brief silence to prevent click
    { int16_t sil[128]; memset(sil, 0, sizeof(sil)); i2s.write((uint8_t*)sil, sizeof(sil)); }
    delay(60);

    Serial.println("[Play]  Done");

    // Restore mic
    audioInitRec();
    micInit();

    // Drain echo from mic buffer
    if (micOk) {
        uint8_t drain[512];
        uint32_t end = millis() + 300;
        while (millis() < end) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
    }
}

// ============================================================
// CONVERSATION — full turn: listen → ASR → LLM → speak
// ============================================================
void runConversation() {
    if (busy) return;
    busy = true;
    Serial.println("[Conv]  --- Turn start ---");

    // 1. Listen + stream ASR
    setFaceState(FS_LISTENING);
    setStatus("Listening...", C_GR);
    showTranscript("");

    if (!micOk) {
        setStatus("Mic error", C_RD);
        setFaceState(FS_ERROR);
        delay(2000);
        setFaceState(FS_IDLE);
        setStatus("Ready", C_CY);
        busy = false;
        return;
    }

    bool gotTranscript = recordAndStream();

    if (!gotTranscript || isNoise(asrFinal)) {
        Serial.printf("[Conv]  No speech / noise  final='%s'\n", asrFinal.c_str());
        showTranscript("");
        setFaceState(FS_IDLE);
        setStatus("Ready", C_CY);
        busy = false;
        return;
    }

    Serial.printf("[Conv]  Transcript: %s\n", asrFinal.c_str());
    showTranscript(asrFinal);  // final transcript stays visible

    // 2. Call Railway (LLM + TTS)
    setFaceState(FS_THINKING);
    setStatus("Thinking...", C_PURP);

    bool ok = callRailway(asrFinal.c_str());

    if (!ok) {
        Serial.println("[Conv]  Railway failed");
        setStatus("Server error", C_RD);
        setFaceState(FS_ERROR);
        delay(2500);
        showTranscript("");
        setFaceState(FS_IDLE);
        setStatus("Ready", C_CY);
        busy = false;
        return;
    }

    // 3. Speak the response
    setFaceState(FS_SPEAKING);
    setStatus("Speaking...", C_GR);
    playMp3();

    // Done
    showTranscript("");
    setFaceState(FS_IDLE);
    setStatus("Ready", C_CY);
    Serial.println("[Conv]  --- Turn end ---");
    busy = false;
}

// ============================================================
// SETUP
// ============================================================
void setup() {
    Serial.begin(115200);
    delay(400);
    Serial.println("\n\n===== BRONNY AI v7.0 =====");
    Serial.printf("[Mem]   Heap: %u  PSRAM: %u\n",
                  esp_get_free_heap_size(),
                  heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    // ── TFT ─────────────────────────────────────────────────
    pinMode(PIN_TFT_BLK, OUTPUT);
    digitalWrite(PIN_TFT_BLK, HIGH);
    tftSPI.begin(PIN_TFT_CLK, -1, PIN_TFT_MOSI, PIN_TFT_CS);
    tft.init(240, 320);
    tft.setRotation(3);
    tft.fillScreen(C_BK);

    // Boot splash
    tft.setTextSize(2);
    tft.setTextColor(C_CY);
    int bx = (W - 10 * 12) / 2;
    tft.setCursor(bx, 85);
    tft.print("BRONNY AI");
    tft.setTextSize(1);
    tft.setTextColor(C_DCY);
    tft.setCursor((W - 5 * 6) / 2, 114);
    tft.print("v7.0");
    tft.setTextColor(C_DG);
    tft.setCursor((W - 16 * 6) / 2, 130);
    tft.print("by Patrick Perez");

    // ── Audio ────────────────────────────────────────────────
    AudioToolsLogger.begin(Serial, AudioToolsLogLevel::Warning);
    audioInitRec();

    // ── Mic ──────────────────────────────────────────────────
    micInit();

    // ── MP3 decoder + PSRAM buffer ───────────────────────────
    mp3Decoder.begin();
    mp3Buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
    if (!mp3Buf) Serial.println("[Boot]  WARNING: mp3Buf PSRAM alloc failed");

    // ── WiFi ─────────────────────────────────────────────────
    tft.fillRect(0, 150, W, 16, C_BK);
    tft.setTextSize(1); tft.setTextColor(C_YL);
    tft.setCursor(52, 152);
    tft.print("Connecting to WiFi...");
    wifiConnect();

    tft.fillRect(0, 150, W, 16, C_BK);
    if (WiFi.status() == WL_CONNECTED) {
        tft.setTextColor(C_GR);
        tft.setCursor(52, 152);
        tft.printf("WiFi: %s", WiFi.localIP().toString().c_str());
    } else {
        tft.setTextColor(C_RD);
        tft.setCursor(52, 152);
        tft.print("WiFi FAILED");
    }
    delay(1000);

    // ── Initial heartbeat → tells command center "Bronny is online" ──
    sendHeartbeat();
    lastHbMs = millis();

    // ── Draw face & status bar ───────────────────────────────
    tft.fillScreen(C_BK);
    setFaceState(FS_IDLE);
    drawFace(true);
    setStatus("Ready", C_CY);

    Serial.printf("[Boot]  Ready  heap=%u  psram=%u\n",
                  esp_get_free_heap_size(),
                  heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    Serial.println("[VAD]   Serial monitor shows 'peak=XXX thr=XXX' — adjust VAD_THR in voice_config.h if needed");
    Serial.println("[Cmds]  t=manual trigger  h=heartbeat  +=louder -=quieter info");
}

// ============================================================
// LOOP
// ============================================================
void loop() {
    uint32_t now = millis();

    // ── Face animation ────────────────────────────────────────
    animFace();
    if (faceRedraw) { drawFace(false); faceRedraw = false; }

    // ── Heartbeat ─────────────────────────────────────────────
    if (now - lastHbMs > HEARTBEAT_MS) {
        lastHbMs = now;
        sendHeartbeat();
    }

    // ── VAD trigger ───────────────────────────────────────────
    if (!busy && micOk) {
        static int32_t  vadBuf[32];
        static int32_t  vadPeak   = 0;
        static uint32_t vadLogMs  = 0;

        int rd = mic_stream.readBytes((uint8_t*)vadBuf, sizeof(vadBuf));
        int frames = rd / 8;
        for (int i = 0; i < frames; i++) {
            int32_t v = abs((int16_t)(vadBuf[i * 2] >> 11));
            if (v > vadPeak) vadPeak = v;
        }

        // Print mic level every 4 s for calibration reference
        if (now - vadLogMs > 4000) {
            Serial.printf("[VAD]   peak=%d  thr=%d\n", (int)vadPeak, VAD_THR);
            vadLogMs = now;
            vadPeak  = 0;
        }

        if (vadPeak > VAD_THR) {
            vadPeak = 0;
            runConversation();
        }
    }

    // ── Serial debug commands ─────────────────────────────────
    if (Serial.available()) {
        char c = Serial.read();
        switch (c) {
            case 't':
                Serial.println("[DBG]   Manual trigger");
                runConversation();
                break;
            case 'h':
                Serial.println("[DBG]   Manual heartbeat");
                sendHeartbeat();
                break;
            case '+':
                Serial.printf("[DBG]   VAD_THR adjustment: raise threshold in voice_config.h  (current hw thr=%d)\n", VAD_THR);
                break;
            case '-':
                Serial.printf("[DBG]   VAD_THR adjustment: lower threshold in voice_config.h  (current hw thr=%d)\n", VAD_THR);
                break;
            case 'i':
                Serial.printf("[DBG]   Heap=%u  PSRAM=%u  WiFi=%s  Mic=%s  Audio=%s\n",
                              esp_get_free_heap_size(),
                              heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                              WiFi.isConnected() ? "OK" : "FAIL",
                              micOk  ? "OK" : "FAIL",
                              audioOk ? "OK" : "FAIL");
                break;
        }
    }

    yield();
}
