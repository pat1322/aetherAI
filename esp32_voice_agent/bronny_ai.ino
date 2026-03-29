/*
 * BRONNY AI v1.2
 * by Patrick Perez
 *
 * Hardware:
 *   Board   : ESP32-S3 Dev Module (OPI PSRAM 8MB)
 *   Codec   : ES8311 (I2C addr 0x18)
 *   Mic     : INMP441 (I2S port 1, GPIOs 4/5/6)
 *   Display : ST7789 320x240 (HSPI)
 *   LED     : WS2812B on GPIO 48 (built-in)
 *
 * v1.2 Changes:
 *   - BOOT button (GPIO 0) now toggles log panel (replaces voice commands)
 *   - "party on"  → Audio visualiser + party-lights mode
 *   - "party off" → Returns to Bronny face; LED turns off
 *
 * NOTE: GPIO 48 is shared between PIN_PA (audio amp enable) and the
 *       built-in WS2812B LED.  In party mode the PA is idle, so the
 *       NeoPixel takes over.  If your board wires PA to a different GPIO
 *       update PIN_PA accordingly.
 */

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

#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <Adafruit_NeoPixel.h>     // ← NEW (party LED)
#include <arduinoFFT.h>            // ← NEW (party visualiser)  v2.x
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <math.h>

#include "voice_config.h"

// ============================================================
// FORWARD DECLARATIONS
// ============================================================
void   setStatus(const char* s, uint16_t c);
void   setFooterOnly(uint16_t c, const char* s);
void   tftLog(uint16_t col, const char* msg);
void   tftLogf(uint16_t col, const char* fmt, ...);
void   animFace();
void   drawFace(bool full);
void   maintainDeepgram();
void   startTalk();
void   stopTalk();
void   setLogsVisible(bool visible);
extern bool faceRedraw;

// ============================================================
// CONFIG
// ============================================================
const char* WIFI_SSID = WIFI_SSID_CFG;
const char* WIFI_PASS = WIFI_PASS_CFG;

#define VOL_MAIN             0.50f
#define VOL_JINGLE           0.25f
#define MIC_GAIN_SHIFT       12
#define TTS_COOLDOWN_MS       800
#define HEARTBEAT_MS         30000
#define DG_KEEPALIVE_MS      8000
#define DG_RECONNECT_MS      3000
#define DG_CONNECT_TIMEOUT   8000
#define DG_FINAL_TIMEOUT_MS   700
#define STANDBY_TIMEOUT_MS  180000UL
#define STREAM_CHUNK_BYTES    512
#define MIN_AUDIO_BYTES      1024
#define STREAM_DATA_GAP_MS    500

static uint32_t lastHbMs         = 0;
static uint32_t vadCooldownUntil = 0;
static uint32_t lastRailwayMs    = 0;
static bool     standbyMode      = false;
static bool     bootIntroDone    = true;
static uint32_t bootReadyAt      = 0;
static bool     logsVisible      = false;

// ============================================================
// PINS
// ============================================================
#define PIN_SDA  1
#define PIN_SCL  2
#define PIN_MCLK 38
#define PIN_BCLK 14
#define PIN_WS   13
#define PIN_DOUT 45
#define PIN_DIN  12
#define PIN_PA   48          // also WS2812B; PA is idle during party mode
#define ES_ADDR  0x18

#define PIN_MIC_WS   4
#define PIN_MIC_SCK  5
#define PIN_MIC_SD   6

#define PIN_CS   47
#define PIN_DC   39
#define PIN_BLK  42
#define PIN_CLK  41
#define PIN_MOSI 40

#define PIN_BOOT 0           // ← NEW: built-in BOOT button (active LOW)

// ============================================================
// DISPLAY + SPRITE CANVAS
// ============================================================
SPIClass        tftSPI(HSPI);
Adafruit_ST7789 tft = Adafruit_ST7789(&tftSPI, PIN_CS, PIN_DC, -1);

static GFXcanvas16* faceCanvas = nullptr;

#define W  320
#define H  240

#define C_BK    0x0000
#define C_BG    0x0209
#define C_MID   0x0412
#define C_CY    0x07FF
#define C_DCY   0x0455
#define C_WH    0xFFFF
#define C_LG    0xC618
#define C_DG    0x39E7
#define C_GR    0x07E0
#define C_RD    0xF800
#define C_YL    0xFFE0
#define C_MINT  0x3FF7
#define C_WARN  C_YL
#define C_CARD  0x18C3

static uint16_t dimCol(uint16_t c, int factor);

// Declared here so checkBootButton() and maintainDeepgram() can see it
// before the full PARTY MODE section (which is defined after Deepgram).
bool partyMode = false;

#define LOG_Y        160
#define LOG_LINE_H    14
#define LOG_LINES      4
#define LOG_FOOTER_Y (H - 14)

#define FCX    160
static int faceCY    = 72;
static int faceBlitY = 0;
#define BOB      4
#define EW      112
#define EH       64
#define ER       22
#define ESEP    138
#define EYO     -22
#define MW       72
#define MH_CL     6
#define MH_OP    30
#define MR        8
#define MYO      56
#define SMILE_R     22
#define SMILE_TH     7
#define LOOK_X_RANGE  12
#define LOOK_Y_RANGE   6

static String   gLog[LOG_LINES];
static uint16_t gLogCol[LOG_LINES];
static int      gLogCount = 0;
static int      gLogHead  = 0;
static String   gFooterText  = "v1.2 Ready";
static uint16_t gFooterColor = C_CY;

static inline void blitFace() {
    if (faceCanvas)
        tft.drawRGBBitmap(0, faceBlitY, faceCanvas->getBuffer(), W, LOG_Y);
}

// ============================================================
// ════════════════════════════════════════════════════════════
//  BOOT BUTTON  — NEW SECTION
// ════════════════════════════════════════════════════════════
// ============================================================

static bool     lastBootBtnState = HIGH;
static uint32_t bootBtnDebounceTs = 0;

// Call from loop() — toggles log visibility on each button press.
// Disabled while in party mode (no log panel in party view).
static void checkBootButton() {
    if (partyMode) return;                          // no logs in party mode
    bool state = digitalRead(PIN_BOOT);
    if (state == LOW && lastBootBtnState == HIGH
            && (millis() - bootBtnDebounceTs) > 300) {
        bootBtnDebounceTs = millis();
        setLogsVisible(!logsVisible);
        Serial.printf("[BTN] Logs %s\n", logsVisible ? "shown" : "hidden");
    }
    lastBootBtnState = state;
}

// ============================================================
// AUDIO ENGINE
// ============================================================
AudioInfo ainf_rec(16000, 2, 16);
AudioInfo ainf_tts(24000, 2, 16);

DriverPins     brdPins;
AudioBoard     brdDrv(AudioDriverES8311, brdPins);
I2SCodecStream i2s(brdDrv);
I2SStream      mic_stream;

SineWaveGenerator<int16_t>    sineGen(32000);
GeneratedSoundStream<int16_t> sineSrc(sineGen);
StreamCopy                    sineCopy(i2s, sineSrc);

MP3DecoderHelix mp3Decoder;

bool audioOk   = false;
bool micOk     = false;
static bool inTtsMode = false;

inline int16_t inmp441Sample(int32_t raw) {
    int32_t s = raw >> MIC_GAIN_SHIFT;
    if (s >  32767) s =  32767;
    if (s < -32768) s = -32768;
    return (int16_t)s;
}

// ============================================================
// AUDIO INIT
// ============================================================
static bool audioPinsSet = false;

void audioPinsSetup() {
    if (!audioPinsSet) {
        Wire.begin(PIN_SDA, PIN_SCL, 100000);
        brdPins.addI2C(PinFunction::CODEC, PIN_SCL, PIN_SDA, ES_ADDR, 100000, Wire);
        brdPins.addI2S(PinFunction::CODEC, PIN_MCLK, PIN_BCLK, PIN_WS, PIN_DOUT, PIN_DIN);
        brdPins.addPin(PinFunction::PA, PIN_PA, PinLogic::Output);
        audioPinsSet = true;
    }
}

void micInit() {
    auto cfg = mic_stream.defaultConfig(RX_MODE);
    cfg.sample_rate     = 16000;
    cfg.channels        = 1;
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
        uint32_t e = millis() + 300;
        while (millis() < e) { mic_stream.readBytes(tmp, sizeof(tmp)); yield(); }
    }
}

void audioInitRec() {
    if (inTtsMode || !audioOk) {
        i2s.end(); delay(100);
        audioPinsSetup();
        auto cfg = i2s.defaultConfig(TX_MODE);
        cfg.copyFrom(ainf_rec);
        cfg.output_device = DAC_OUTPUT_ALL;
        audioOk = i2s.begin(cfg);
        i2s.setVolume(VOL_MAIN);
        if (audioOk) {
            auto sc = sineGen.defaultConfig();
            sc.copyFrom(ainf_rec);
            sineGen.begin(sc);
        }
        inTtsMode = false;
    }
}

void audioInitTTS() {
    if (!inTtsMode) {
        i2s.end(); delay(100);
        audioPinsSetup();
        auto cfg = i2s.defaultConfig(TX_MODE);
        cfg.copyFrom(ainf_tts);
        cfg.output_device = DAC_OUTPUT_ALL;
        audioOk = i2s.begin(cfg);
        i2s.setVolume(VOL_MAIN);
        inTtsMode = true;
    }
}

void audioRestart() {
    i2s.end(); delay(150);
    audioOk = false; inTtsMode = false;
    Wire.end(); delay(60);
    audioPinsSet = false;
    audioPinsSetup();
    auto cfg = i2s.defaultConfig(TX_MODE);
    cfg.copyFrom(ainf_rec);
    cfg.output_device = DAC_OUTPUT_ALL;
    audioOk = i2s.begin(cfg);
    i2s.setVolume(VOL_MAIN);
    if (audioOk) {
        auto sc = sineGen.defaultConfig();
        sc.copyFrom(ainf_rec);
        sineGen.begin(sc);
    }
}

void playTone(float hz, int ms) {
    if (!audioOk) { delay(ms); return; }
    sineGen.setFrequency(hz);
    uint32_t e = millis() + ms;
    while (millis() < e) { sineCopy.copy(); yield(); }
}
void playSil(int ms) { playTone(0, ms); }

void jingleBoot() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    float n[] = {523, 659, 784, 1047, 1319, 1568, 2093};
    int   d[] = {100, 100, 100,  140,  260,   80,  280};
    for (int i = 0; i < 7; i++) { playTone(n[i], d[i]); playSil(20); }
    i2s.setVolume(VOL_MAIN);
}
void jingleConnect() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(880, 100); playSil(25); playTone(1108, 100); playSil(25);
    playTone(1318, 200); playSil(150);
    i2s.setVolume(VOL_MAIN);
}
void jingleError() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(300, 200); playSil(80); playTone(220, 350); playSil(200);
    i2s.setVolume(VOL_MAIN);
}
void jingleReady() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(880, 80); playSil(30); playTone(1318, 80); playSil(30);
    playTone(1760, 200); playSil(150);
    i2s.setVolume(VOL_MAIN);
}
void jingleWake() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(660, 80); playSil(20); playTone(1100, 120); playSil(80);
    i2s.setVolume(VOL_MAIN);
}
// Party-start jingle
void jingleParty() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    float n[] = {523, 659, 784, 1047, 784, 1047, 1319};
    int   d[] = { 80,  80,  80,  140,  80,   80,  260};
    for (int i = 0; i < 7; i++) { playTone(n[i], d[i]); playSil(15); }
    i2s.setVolume(VOL_MAIN);
}

// ============================================================
// UTILITIES
// ============================================================
String jEsc(const String& s) {
    String o; o.reserve(s.length() + 16);
    for (int i = 0; i < (int)s.length(); i++) {
        unsigned char c = (unsigned char)s[i];
        if      (c == '"')  o += "\\\"";
        else if (c == '\\') o += "\\\\";
        else if (c == '\n') o += "\\n";
        else if (c == '\r') o += "\\r";
        else if (c == '\t') o += "\\t";
        else if (c >= 0x20) o += (char)c;
    }
    return o;
}

static void ensureWifi() {
    if (WiFi.status() != WL_CONNECTED) {
        WiFi.reconnect();
        uint32_t t = millis();
        while (WiFi.status() != WL_CONNECTED && millis() - t < 8000) { delay(300); yield(); }
    }
}

static String baseUrl() {
    String u = String(AETHER_URL);
    while (u.endsWith("/")) u.remove(u.length() - 1);
    return u;
}

static inline void forceDrawFace() {
    faceRedraw = true;
    drawFace(false);
    faceRedraw = false;
}

// ============================================================
// HEARTBEAT
// ============================================================
void sendHeartbeat() {
    if (WiFi.status() != WL_CONNECTED) return;
    maintainDeepgram();
    WiFiClientSecure cli; cli.setInsecure(); cli.setConnectionTimeout(8000);
    HTTPClient http;
    http.begin(cli, baseUrl() + "/bronny/heartbeat");
    http.setTimeout(8000);
    http.addHeader("Content-Type", "application/json");
    int code = http.POST("{\"device\":\"bronny\",\"version\":\"1.2\"}");
    http.end();
    maintainDeepgram();
    if (code != 200) Serial.printf("[HB] fail %d\n", code);
    else             Serial.println("[HB] OK");
}

// ============================================================
// TFT LOG ZONE
// ============================================================
static void logRedraw() {
    if (!logsVisible) return;
    tft.drawFastHLine(0, LOG_Y, W, C_CY);
    tft.fillRect(0, LOG_Y + 1, W, LOG_LINES * LOG_LINE_H + 3, C_MID);
    int total = min(gLogCount, LOG_LINES);
    for (int i = 0; i < total; i++) {
        int slot = (gLogHead + i) % LOG_LINES;
        uint16_t c = gLogCol[slot];
        int ly = LOG_Y + 2 + i * LOG_LINE_H;
        tft.fillRect(0, ly + 1, 3, LOG_LINE_H - 3, c);
        tft.setTextColor(c); tft.setTextSize(1);
        tft.setCursor(7, ly + 3);
        tft.print(gLog[slot]);
    }
}

static void logDrawFooter() {
    if (!logsVisible) return;
    tft.fillRect(0, LOG_FOOTER_Y, W, H - LOG_FOOTER_Y, C_BG);
    tft.drawFastHLine(0, LOG_FOOTER_Y, W, C_DCY);
    tft.setTextColor(gFooterColor); tft.setTextSize(1);
    int tw = (int)gFooterText.length() * 6;
    tft.setCursor(W / 2 - tw / 2, LOG_FOOTER_Y + 4);
    tft.print(gFooterText);
}

void tftLog(uint16_t col, const char* msg) {
    String s = String(msg);
    if ((int)s.length() > 53) s = s.substring(0, 53);
    if (gLogCount < LOG_LINES) {
        gLog[gLogCount] = s; gLogCol[gLogCount] = col; gLogCount++;
        if (logsVisible) {
            int i  = gLogCount - 1;
            int ly = LOG_Y + 2 + i * LOG_LINE_H;
            tft.fillRect(0, ly, W, LOG_LINE_H - 1, C_MID);
            tft.fillRect(0, ly + 1, 3, LOG_LINE_H - 3, col);
            tft.setTextColor(col); tft.setTextSize(1);
            tft.setCursor(7, ly + 3);
            tft.print(s);
        }
    } else {
        gLog[gLogHead] = s; gLogCol[gLogHead] = col;
        gLogHead = (gLogHead + 1) % LOG_LINES;
        logRedraw();
    }
}

void tftLogf(uint16_t col, const char* fmt, ...) {
    char buf[80]; va_list ap; va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
    tftLog(col, buf);
}

void setStatus(const char* s, uint16_t c) {
    gFooterText = String(s); gFooterColor = c;
    logDrawFooter();
}

void setFooterOnly(uint16_t c, const char* s) {
    char buf[54]; strncpy(buf, s, 53); buf[53] = '\0';
    gFooterText = String(buf); gFooterColor = c;
    logDrawFooter();
}

void setLogsVisible(bool visible) {
    logsVisible = visible;
    if (visible) {
        faceCY    = 72;
        faceBlitY = 0;
        drawFace(true);
        logRedraw();
        logDrawFooter();
        Serial.println("[UI] Logs shown");
    } else {
        faceCY    = 80;
        faceBlitY = 40;
        tft.fillRect(0, 0, W, faceBlitY, C_BK);
        tft.fillRect(0, LOG_Y, W, H - LOG_Y, C_BK);
        drawFace(true);
        Serial.println("[UI] Logs hidden");
    }
}

// ============================================================
// RAILWAY STREAMING CALL
// ============================================================
bool callRailwayStream(const String& transcript) {
    if (transcript.isEmpty()) return false;
    if (!audioOk) { Serial.println("[Rail] Skipped — audio not ready"); return false; }
    ensureWifi();
    if (WiFi.status() != WL_CONNECTED) return false;

    String body = "{\"text\":\"" + jEsc(transcript) + "\"}";
    String url  = baseUrl() + "/voice/text";
    Serial.printf("[Rail] POST text='%s'\n", transcript.c_str());

    bool gotAudio = false;

    for (int attempt = 1; attempt <= 2; attempt++) {
        if (attempt > 1) { Serial.println("[Rail] retry..."); delay(2000); }

        WiFiClientSecure cli;
        cli.setInsecure();
        cli.setConnectionTimeout(20000);
        HTTPClient http;
        http.begin(cli, url);
        http.setTimeout(45000);
        http.addHeader("Content-Type", "application/json");
        http.addHeader("X-Api-Key", AETHER_API_KEY);

        int code = http.POST(body);
        Serial.printf("[Rail] HTTP %d\n", code);

        if (code != 200) { http.end(); continue; }

        mic_stream.end();
        micOk = false;
        delay(60);

        audioInitTTS();
        delay(80);

        {
            int16_t sil[256] = {};
            uint32_t primerEnd = millis() + 180;
            while (millis() < primerEnd) {
                i2s.write((uint8_t*)sil, sizeof(sil));
                animFace();
                if (faceRedraw) { drawFace(false); faceRedraw = false; }
                yield();
            }
        }

        mp3Decoder.begin();
        EncodedAudioStream decoded(&i2s, &mp3Decoder);
        decoded.begin();

        WiFiClient* stream  = http.getStreamPtr();
        int    contentLen   = http.getSize();
        size_t totalRead    = 0;
        uint32_t deadline   = millis() + 35000;
        bool talkStarted    = false;
        uint32_t lastDataMs = 0;
        uint8_t buf[STREAM_CHUNK_BYTES];

        while (millis() < deadline) {
            size_t avail = (size_t)stream->available();
            if (avail > 0) {
                size_t got = stream->readBytes(buf, min(avail, (size_t)sizeof(buf)));
                if (got > 0) {
                    if (!talkStarted) {
                        setStatus("Speaking...", C_GR);
                        startTalk();
                        talkStarted = true;
                    }
                    decoded.write(buf, got);
                    totalRead  += got;
                    lastDataMs  = millis();
                }
            } else {
                delay(2);
            }

            if (talkStarted && lastDataMs > 0 && millis() - lastDataMs > STREAM_DATA_GAP_MS) {
                Serial.printf("[Rail] Data gap %ums — done\n", (unsigned)(millis() - lastDataMs));
                break;
            }
            if (!http.connected() && stream->available() == 0) break;
            if (contentLen > 0 && (int)totalRead >= contentLen)   break;

            maintainDeepgram();
            animFace();
            if (faceRedraw) { drawFace(false); faceRedraw = false; }
            yield();
        }

        stopTalk();
        forceDrawFace();
        setStatus("Listening...", C_CY);

        decoded.end();
        { int16_t sil[128] = {}; i2s.write((uint8_t*)sil, sizeof(sil)); }

        animFace();
        if (faceRedraw) { drawFace(false); faceRedraw = false; }

        http.end();
        Serial.printf("[Rail] Stream complete: %u bytes\n", (unsigned)totalRead);

        if (totalRead >= MIN_AUDIO_BYTES) { gotAudio = true; break; }
        Serial.printf("[Rail] Attempt %d incomplete (%u < %u), retrying\n",
                      attempt, (unsigned)totalRead, (unsigned)MIN_AUDIO_BYTES);
    }

    audioInitRec();
    animFace(); if (faceRedraw) { drawFace(false); faceRedraw = false; }

    micInit();
    animFace(); if (faceRedraw) { drawFace(false); faceRedraw = false; }

    if (micOk) {
        uint8_t drain[512];
        uint32_t e = millis() + 300;
        while (millis() < e) {
            mic_stream.readBytes(drain, sizeof(drain));
            animFace();
            if (faceRedraw) { drawFace(false); faceRedraw = false; }
            yield();
        }
    }

    return gotAudio;
}

// ============================================================
// DEEPGRAM CONNECTION SCREEN
// ============================================================
static void drawDgAnim(int cx, int cy, int spin) {
    tft.fillRect(cx - 40, cy - 40, 80, 80, C_BK);
    static const int radii[3] = { 14, 26, 38 };
    for (int i = 0; i < 3; i++) {
        bool lit = (i == spin);
        tft.drawCircle(cx, cy, radii[i], lit ? C_CY : C_DG);
        if (lit) {
            tft.drawCircle(cx, cy, radii[i] - 1, dimCol(C_CY, 4));
            tft.drawCircle(cx, cy, radii[i] + 1, dimCol(C_CY, 2));
        }
    }
    tft.fillCircle(cx, cy, 5, (spin == 0) ? C_CY : C_DG);
}

static void drawDgStatus(const char* msg, uint16_t col) {
    tft.fillRect(0, 162, W, 22, C_BK);
    tft.setTextSize(2); tft.setTextColor(col);
    int tw = (int)strlen(msg) * 12;
    tft.setCursor(W / 2 - tw / 2, 164);
    tft.print(msg);
}

void drawDgScreen() {
    tft.fillScreen(C_BK);
    for (int i = 0; i < 70; i++) {
        int x = (i * 211 + 19) % W;
        int y = (i * 97  + 13) % (H - 28) + 5;
        uint16_t col = (i % 5 == 0) ? C_WH  :
                       (i % 4 == 0) ? C_CY  :
                       (i % 3 == 0) ? C_DCY : C_DG;
        tft.drawPixel(x, y, col);
    }
    tft.fillRect(0, 0, W, 30, 0x18C3);
    tft.drawFastHLine(0, 0,  W, C_CY);
    tft.drawFastHLine(0, 30, W, C_DCY);
    tft.fillCircle(14, 15, 5, C_CY); tft.fillCircle(14, 15, 2, C_BK);
    tft.setTextColor(C_WH); tft.setTextSize(1); tft.setCursor(25, 10); tft.print("BRONNY AI");
    tft.setTextColor(C_CY);                     tft.setCursor(82, 10); tft.print("v1.2");
    tft.fillRoundRect(W - 100, 6, 96, 18, 4, C_DCY);
    tft.drawRoundRect(W - 100, 6, 96, 18, 4, C_CY);
    tft.setTextColor(C_CY); tft.setCursor(W - 94, 12); tft.print("SPEECH ENGINE");
    tft.setTextSize(2); tft.setTextColor(C_WH);
    tft.setCursor(W / 2 - 54, 40); tft.print("DEEPGRAM");
    tft.setTextSize(1); tft.setTextColor(C_DCY);
    tft.setCursor(W / 2 - 33, 62); tft.print("nova-3  \xB7  ASR");
    tft.drawFastHLine(W/2 - 60, 74, 120, C_DG);
    drawDgAnim(W / 2, 118, 0);
    drawDgStatus("Connecting...", C_YL);
    tft.fillRect(0, H - 20, W, 20, 0x18C3);
    tft.drawFastHLine(0, H - 20, W, C_DCY);
    tft.setTextColor(C_DG);  tft.setTextSize(1);
    tft.setCursor(W / 2 - 48, H - 13); tft.print("Bronny AI v1.2");
}

// ============================================================
// DEEPGRAM PERSISTENT STREAMING ASR
// ============================================================
WebSocketsClient dgWs;

bool     dgConnected          = false;
bool     dgStreaming           = false;
uint32_t dgLastKeepalive       = 0;
uint32_t dgLastConnectAttempt  = 0;

static bool     pendingTranscript     = false;
static String   dgFinal               = "";
static String   dgPartial             = "";
static uint32_t dgFinalReceivedAt     = 0;

static bool busy = false;

static const char* DG_PATH =
    "/v1/listen"
    "?encoding=linear16"
    "&sample_rate=16000"
    "&channels=1"
    "&language=en"
    "&model=nova-3"
    "&interim_results=true"
    "&endpointing=350"
    "&filler_words=false";

static StaticJsonDocument<4096> dgJsonDoc;

static void parseDgMsg(const char* json, size_t len) {
    dgJsonDoc.clear();
    if (deserializeJson(dgJsonDoc, json, len) != DeserializationError::Ok) return;
    auto& doc = dgJsonDoc;

    const char* msgType = doc["type"] | "";

    if (strcmp(msgType, "Results") == 0) {
        const char* txt = doc["channel"]["alternatives"][0]["transcript"] | "";
        bool isFinal    = doc["is_final"]     | false;
        bool speechEnd  = doc["speech_final"] | false;

        if (strlen(txt) > 0) {
            if (isFinal) {
                if (dgFinal.length() > 0) dgFinal += " ";
                dgFinal += String(txt);
                dgFinalReceivedAt = millis();
                setFooterOnly(C_MINT, txt);
                Serial.printf("[DG] final: %s\n", txt);
            } else {
                dgPartial = String(txt);
                char pfx[56];
                snprintf(pfx, sizeof(pfx), "> %s", txt);
                setFooterOnly(C_LG, pfx);
            }
        }
        if (speechEnd && dgFinal.length() > 0) {
            pendingTranscript = true;
            dgFinalReceivedAt = 0;
            Serial.printf("[DG] speech_final -> '%s'\n", dgFinal.c_str());
        }
    } else if (strcmp(msgType, "Error") == 0) {
        const char* desc = doc["description"] | "unknown";
        Serial.printf("[DG] Error: %s\n", desc);
    }
}

void onDgWsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            dgConnected     = true;
            dgLastKeepalive = millis();
            if (!busy) dgStreaming = true;
            Serial.println("[DG] WS connected");
            break;
        case WStype_TEXT:
            parseDgMsg((const char*)payload, length);
            break;
        case WStype_DISCONNECTED:
            dgConnected = false; dgStreaming = false;
            Serial.println("[DG] WS disconnected");
            break;
        case WStype_ERROR:
            dgConnected = false; dgStreaming = false;
            Serial.println("[DG] WS error");
            break;
        default: break;
    }
}

void connectDeepgram() {
    String authHdr = "Authorization: Token " + String(DEEPGRAM_API_KEY);
    dgWs.onEvent(onDgWsEvent);
    dgWs.setExtraHeaders(authHdr.c_str());
    dgWs.beginSSL("api.deepgram.com", 443, DG_PATH);
    dgLastConnectAttempt = millis();

    uint32_t deadline  = millis() + DG_CONNECT_TIMEOUT;
    uint32_t lastAnim  = millis();
    int      spin      = 0;
    while (!dgConnected && millis() < deadline) {
        dgWs.loop();
        if (millis() - lastAnim > 280) {
            spin = (spin + 1) % 3;
            drawDgAnim(W / 2, 118, spin);
            lastAnim = millis();
        }
        yield();
    }

    if (dgConnected) {
        drawDgAnim(W / 2, 118, 0);
        drawDgStatus("Connected!", C_GR);
    } else {
        drawDgAnim(W / 2, 118, 0);
        drawDgStatus("Timed Out", C_WARN);
        Serial.println("[DG] Connect timeout");
    }
    delay(700); yield();
}

static int32_t s_rawBuf[1600];
static int16_t s_pcmBuf[1600];

void maintainDeepgram() {
    uint32_t now = millis();
    dgWs.loop();

    if (!dgConnected && now - dgLastConnectAttempt > DG_RECONNECT_MS) {
        if (busy) return;
        Serial.println("[DG] Reconnecting...");
        dgLastConnectAttempt = now;
        String authHdr = "Authorization: Token " + String(DEEPGRAM_API_KEY);
        dgWs.setExtraHeaders(authHdr.c_str());
        dgWs.beginSSL("api.deepgram.com", 443, DG_PATH);
        return;
    }
    if (!dgConnected) return;

    if (dgStreaming && micOk) {
        // ── In party mode partyProcessAudio() handles mic reads AND DG sends
        if (!partyMode) {
            int bytesRead = mic_stream.readBytes((uint8_t*)s_rawBuf, sizeof(s_rawBuf));
            int frames = bytesRead / 4;
            if (frames > 0) {
                for (int i = 0; i < frames; i++)
                    s_pcmBuf[i] = inmp441Sample(s_rawBuf[i]);
                dgWs.sendBIN((uint8_t*)s_pcmBuf, frames * 2);
            }
        }
        return;
    }

    if (now - dgLastKeepalive > DG_KEEPALIVE_MS) {
        dgWs.sendTXT("{\"type\":\"KeepAlive\"}");
        dgLastKeepalive = now;
        Serial.println("[DG] KeepAlive sent");
    }
}

// ============================================================
// ════════════════════════════════════════════════════════════
//  PARTY MODE
// ════════════════════════════════════════════════════════════
// ============================================================

// ── Party LED ────────────────────────────────────────────────
#define PARTY_LED_PIN    48
#define PARTY_LED_COUNT   1
#define PARTY_LED_BRIGHT 210

Adafruit_NeoPixel partyLed(PARTY_LED_COUNT, PARTY_LED_PIN, NEO_GRB + NEO_KHZ800);

// ── Party visualiser constants ────────────────────────────────
#define PARTY_FFT_SIZE    256
#define PARTY_NUM_BANDS    24
#define PARTY_MODE_SEC     30
#define PARTY_GAIN         14.0f

#define VIZ_W         320
#define VIZ_H         240
#define VIZ_BAR_W      12
#define VIZ_BAR_GAP     1
#define VIZ_BAR_STRIDE (VIZ_BAR_W + VIZ_BAR_GAP)
#define VIZ_ORIGIN_X  ((VIZ_W - PARTY_NUM_BANDS * VIZ_BAR_STRIDE + VIZ_BAR_GAP) / 2)
#define VIZ_C_BG       0x0841

// ── Party state ───────────────────────────────────────────────
// partyMode is declared as a global above (before checkBootButton/maintainDeepgram)
static uint8_t  partyVizMode   = 0;
static uint8_t  partyLedMode   = 0;
static uint32_t partyModeTs    = 0;

// ── FFT buffers ───────────────────────────────────────────────
static float partyVR[PARTY_FFT_SIZE], partyVI[PARTY_FFT_SIZE];
ArduinoFFT<float> partyFFT(partyVR, partyVI, PARTY_FFT_SIZE, 16000.0f);

static int pBinLo[PARTY_NUM_BANDS], pBinHi[PARTY_NUM_BANDS];

// ── Band / peak smoothing ─────────────────────────────────────
static float pSmBand [PARTY_NUM_BANDS] = {};
static float pSmPeak [PARTY_NUM_BANDS] = {};
static int   pPeakTmr[PARTY_NUM_BANDS] = {};
static float pLevel = 0.0f, pBass = 0.0f;
static bool  pBeat  = false;
static float pAvgLevel  = 0.0f;
static uint32_t pLastBeatMs = 0;

// ── Previous-frame delta buffers ─────────────────────────────
static float pPrevH [PARTY_NUM_BANDS] = {};
static float pPrevP [PARTY_NUM_BANDS] = {};
static float pPrevMH[PARTY_NUM_BANDS] = {};
static int   pPrevLvlPx = 0;
static float pFlashAmt  = 0.0f;
static float pHueBase   = 0.0f;

// ── LED state ─────────────────────────────────────────────────
static float    pLedHue    = 0.0f;
static float    pLedSmooth = 0.0f;
static uint32_t pStrobeMs  = 0;

// ── Mic / PCM buffers ─────────────────────────────────────────
static int32_t partyRawBuf[PARTY_FFT_SIZE];
static int16_t partyPcmBuf[PARTY_FFT_SIZE];

// ── Build logarithmic band map ────────────────────────────────
static void buildPartyBandMap() {
    const float lo = 1.5f, hi = PARTY_FFT_SIZE / 2.0f - 1.0f;
    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        pBinLo[b] = max(1, (int)(lo * powf(hi / lo, (float)b         / PARTY_NUM_BANDS)));
        pBinHi[b] = max(pBinLo[b] + 1,
                        (int)(lo * powf(hi / lo, (float)(b + 1) / PARTY_NUM_BANDS)));
    }
}

// ── HSV → RGB565 ──────────────────────────────────────────────
static uint16_t p_hsvTo565(float h, float s, float v) {
    h = fmodf(h + 720.0f, 360.0f);
    float c = v * s, x = c * (1.0f - fabsf(fmodf(h / 60.0f, 2.0f) - 1.0f)), m = v - c;
    float r, g, b;
    if      (h <  60) { r = c+m; g = x+m; b = m;   }
    else if (h < 120) { r = x+m; g = c+m; b = m;   }
    else if (h < 180) { r = m;   g = c+m; b = x+m; }
    else if (h < 240) { r = m;   g = x+m; b = c+m; }
    else if (h < 300) { r = x+m; g = m;   b = c+m; }
    else              { r = c+m; g = m;   b = x+m; }
    return ((uint16_t)(r * 31) << 11) | ((uint16_t)(g * 63) << 5) | (uint16_t)(b * 31);
}

// ── HSV → NeoPixel uint32_t ───────────────────────────────────
static uint32_t p_neoHSV(float h, float s, float v) {
    h = fmodf(h + 720.0f, 360.0f);
    float c = v * s, x = c * (1.0f - fabsf(fmodf(h / 60.0f, 2.0f) - 1.0f)), m = v - c;
    float r, g, b;
    if      (h <  60) { r = c+m; g = x+m; b = m;   }
    else if (h < 120) { r = x+m; g = c+m; b = m;   }
    else if (h < 180) { r = m;   g = c+m; b = x+m; }
    else if (h < 240) { r = m;   g = x+m; b = c+m; }
    else if (h < 300) { r = x+m; g = m;   b = c+m; }
    else              { r = c+m; g = m;   b = x+m; }
    return partyLed.Color((uint8_t)(r * 255), (uint8_t)(g * 255), (uint8_t)(b * 255));
}

// ── Bar colour ────────────────────────────────────────────────
static uint16_t p_barColor(int band, float norm, float hBase) {
    float h = hBase + (float)band * 6.0f + norm * 55.0f;
    float v = 0.28f + norm * 0.72f;
    return p_hsvTo565(h, 1.0f, v);
}

// ── Audio: read mic, FFT, update bands, feed DG ───────────────
static void partyProcessAudio() {
    if (!micOk) return;
    int need = PARTY_FFT_SIZE * (int)sizeof(int32_t);
    while (mic_stream.available() > need * 3) {
        int32_t discard[64];
        mic_stream.readBytes((uint8_t*)discard, sizeof(discard));
    }
    if (mic_stream.available() < need) return;
    int got = mic_stream.readBytes((uint8_t*)partyRawBuf, need);
    if (got < need) return;

    if (dgConnected) {
        for (int i = 0; i < PARTY_FFT_SIZE; i++)
            partyPcmBuf[i] = inmp441Sample(partyRawBuf[i]);
        dgWs.sendBIN((uint8_t*)partyPcmBuf, PARTY_FFT_SIZE * 2);
    }

    for (int i = 0; i < PARTY_FFT_SIZE; i++) {
        float s = (float)(partyRawBuf[i] >> 14) / 32768.0f;
        float w = 0.5f * (1.0f - cosf(TWO_PI * i / (PARTY_FFT_SIZE - 1)));
        partyVR[i] = s * w; partyVI[i] = 0.0f;
    }
    partyFFT.compute(FFTDirection::Forward);
    partyFFT.complexToMagnitude();

    float rawB[PARTY_NUM_BANDS], totLevel = 0.0f;
    const float ns = PARTY_GAIN / (float)PARTY_FFT_SIZE;
    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        float sum = 0.0f;
        int   cnt = pBinHi[b] - pBinLo[b];
        for (int k = pBinLo[b]; k < pBinHi[b]; k++) sum += partyVR[k];
        rawB[b] = constrain(sum * ns / (float)cnt, 0.0f, 1.0f);
        totLevel += rawB[b];
    }
    totLevel = constrain(totLevel / PARTY_NUM_BANDS, 0.0f, 1.0f);

    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        pSmBand[b] = (rawB[b] > pSmBand[b])
                   ? 0.70f * rawB[b] + 0.30f * pSmBand[b]
                   : pSmBand[b] * 0.80f;
        if (pSmBand[b] >= pSmPeak[b]) { pSmPeak[b] = pSmBand[b]; pPeakTmr[b] = 35; }
        else if (pPeakTmr[b] > 0)     { --pPeakTmr[b]; }
        else                           { pSmPeak[b] *= 0.93f; }
    }

    float bass = 0.0f;
    for (int b = 0; b < 5; b++) bass += pSmBand[b];
    bass /= 5.0f;
    pAvgLevel = 0.95f * pAvgLevel + 0.05f * totLevel;
    uint32_t now = millis();
    pBeat = (bass > pAvgLevel * 1.6f) && (bass > 0.06f) && ((now - pLastBeatMs) > 200);
    if (pBeat) pLastBeatMs = now;
    pLevel = totLevel; pBass = bass;
}

// ── VIZ MODE 0 — Spectrum ─────────────────────────────────────
static void partyDrawSpectrum() {
    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        int x      = VIZ_ORIGIN_X + b * VIZ_BAR_STRIDE;
        int newPx  = constrain((int)(pSmBand[b] * VIZ_H), 0, VIZ_H);
        int peakPx = constrain((int)(pSmPeak[b] * VIZ_H), 0, VIZ_H);
        int oldPx  = constrain((int)(pPrevH[b]  * VIZ_H), 0, VIZ_H);
        int oldPPx = constrain((int)(pPrevP[b]  * VIZ_H), 0, VIZ_H);
        if (oldPx > newPx)
            tft.fillRect(x, VIZ_H - oldPx, VIZ_BAR_W, oldPx - newPx, VIZ_C_BG);
        if (oldPPx > 2)
            tft.fillRect(x, VIZ_H - oldPPx - 2, VIZ_BAR_W, 3, VIZ_C_BG);
        if (newPx > 0) {
            for (int s = 0; s < 5; s++) {
                int y0 = s * newPx / 5, y1 = (s + 1) * newPx / 5;
                if (y1 <= y0) continue;
                float norm = (float)(y0 + y1) * 0.5f / (float)VIZ_H;
                tft.fillRect(x, VIZ_H - y1, VIZ_BAR_W, y1 - y0, p_barColor(b, norm, pHueBase));
            }
        }
        if (peakPx > 3)
            tft.fillRect(x, VIZ_H - peakPx - 1, VIZ_BAR_W, 2, 0xFFFF);
        pPrevH[b] = pSmBand[b]; pPrevP[b] = pSmPeak[b];
    }
}

// ── VIZ MODE 1 — Mirror ───────────────────────────────────────
static void partyDrawMirror() {
    const int midY = VIZ_H / 2, hMax = VIZ_H / 2 - 2;
    tft.drawFastHLine(0, midY, VIZ_W, 0x2945);
    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        int x    = VIZ_ORIGIN_X + b * VIZ_BAR_STRIDE;
        int newH = constrain((int)(pSmBand[b] * hMax), 0, hMax);
        int oldH = constrain((int)(pPrevMH[b] * hMax), 0, hMax);
        if (oldH > newH) {
            tft.fillRect(x, midY - oldH,     VIZ_BAR_W, oldH - newH, VIZ_C_BG);
            tft.fillRect(x, midY + newH + 1, VIZ_BAR_W, oldH - newH, VIZ_C_BG);
        }
        if (newH > 0) {
            for (int s = 0; s < 4; s++) {
                int h0 = s * newH / 4, h1 = (s + 1) * newH / 4;
                if (h1 <= h0) continue;
                float norm = (float)(h0 + h1) * 0.5f / (float)hMax;
                uint16_t col = p_barColor(b, norm, pHueBase + 180.0f);
                tft.fillRect(x, midY - h1,     VIZ_BAR_W, h1 - h0, col);
                tft.fillRect(x, midY + h0 + 1, VIZ_BAR_W, h1 - h0, col);
            }
        }
        pPrevMH[b] = pSmBand[b];
    }
}

// ── Beat flash ────────────────────────────────────────────────
static void partyHandleBeatFlash() {
    if (pBeat) pFlashAmt = 1.0f;
    if (pFlashAmt > 0.04f) {
        uint16_t fc = p_hsvTo565(pHueBase + 40.0f, 0.9f, pFlashAmt * 0.55f);
        tft.fillRect(0, 0, 4, VIZ_H, fc);
        tft.fillRect(VIZ_W - 4, 0, 4, VIZ_H, fc);
        pFlashAmt *= 0.50f;
    }
}

// ── Level meter ───────────────────────────────────────────────
static void partyDrawLevelMeter() {
    int newPx = constrain((int)(pLevel * (VIZ_W - 2)), 0, VIZ_W - 2);
    if (newPx == pPrevLvlPx) return;
    if (newPx > pPrevLvlPx)
        tft.fillRect(pPrevLvlPx + 1, VIZ_H - 2, newPx - pPrevLvlPx, 2,
                     p_hsvTo565(pHueBase, 1.0f, 0.85f));
    else
        tft.fillRect(newPx + 1, VIZ_H - 2, pPrevLvlPx - newPx, 2, VIZ_C_BG);
    pPrevLvlPx = newPx;
}

// ── LED effects ───────────────────────────────────────────────
static void partyUpdateLED() {
    uint32_t now = millis();
    uint32_t color;
    switch (partyLedMode) {
        case 0:
            pLedHue    = fmodf(pLedHue + 2.2f, 360.0f);
            pLedSmooth = 0.65f * pLedSmooth + 0.35f * (pLevel * 2.8f + 0.08f);
            color = p_neoHSV(pLedHue, 1.0f, constrain(pLedSmooth, 0.0f, 1.0f));
            break;
        case 1:
            if (pBeat) { color = partyLed.Color(255,255,255); pStrobeMs = now; }
            else {
                float t = 1.0f - constrain((float)(now - pStrobeMs) / 110.0f, 0.0f, 1.0f);
                if (t > 0.02f) { uint8_t br = (uint8_t)(t*255); color = partyLed.Color(br,br,br); }
                else color = p_neoHSV(pHueBase, 1.0f, constrain(pBass*0.45f+0.05f, 0.0f, 1.0f));
            }
            break;
        case 2: {
            float r=0,g=0,bv=0;
            for (int b=0;  b< 5;              b++) r  += pSmBand[b];
            for (int b=5;  b<15;              b++) g  += pSmBand[b];
            for (int b=15; b<PARTY_NUM_BANDS; b++) bv += pSmBand[b];
            r  = constrain(r /5.0f *1.6f,0,1); g  = constrain(g /10.0f*1.6f,0,1);
            bv = constrain(bv/9.0f *1.6f,0,1);
            color = partyLed.Color((uint8_t)(r*255),(uint8_t)(g*255),(uint8_t)(bv*255));
            break;
        }
        case 3: default:
            if (pBeat||(now-pStrobeMs>110)){pLedHue=(float)random(360);pStrobeMs=now;}
            color = p_neoHSV(pLedHue,1.0f,(pBeat||((now-pStrobeMs)<55))?1.0f:0.30f);
            break;
    }
    partyLed.setPixelColor(0, color);
    partyLed.show();
}

// ── Clear screen + delta buffers ─────────────────────────────
static void partyClearScreen() {
    tft.fillScreen(VIZ_C_BG);
    memset(pPrevH, 0, sizeof(pPrevH)); memset(pPrevP, 0, sizeof(pPrevP));
    memset(pPrevMH, 0, sizeof(pPrevMH));
    pPrevLvlPx = 0; pFlashAmt = 0.0f;
}

// ── Enter / exit ──────────────────────────────────────────────
static void enterPartyMode() {
    partyMode = true; partyVizMode = 0; partyLedMode = 0; partyModeTs = millis();
    memset(pSmBand,0,sizeof(pSmBand)); memset(pSmPeak,0,sizeof(pSmPeak));
    memset(pPeakTmr,0,sizeof(pPeakTmr));
    pAvgLevel=0; pLastBeatMs=0; pHueBase=0; pLedHue=0; pLedSmooth=0;
    partyClearScreen();
    tft.setTextColor(0x07FF); tft.setTextSize(1);
    tft.setCursor(4, 4);
    partyLed.begin(); partyLed.setBrightness(PARTY_LED_BRIGHT);
    partyLed.setPixelColor(0, 0); partyLed.show();
    dgStreaming = true; dgLastKeepalive = millis();
    Serial.println("[Party] ON");
}

static void exitPartyMode() {
    partyMode = false;
    partyLed.setPixelColor(0, 0); partyLed.show();
    tft.fillScreen(C_BK);
    if (logsVisible) { faceCY = 72; faceBlitY = 0; }
    else             { faceCY = 80; faceBlitY = 40; tft.fillRect(0,0,W,faceBlitY,C_BK); }
    drawFace(true);
    if (logsVisible) { logRedraw(); logDrawFooter(); }
    dgStreaming = true; dgLastKeepalive = millis();
    setFaceListen(); setStatus("Listening...", C_CY);
    Serial.println("[Party] OFF");
}

// ── Main party tick ───────────────────────────────────────────
static void partyLoop() {
    // maintainDeepgram() keeps WS alive AND handles auto-reconnect if the
    // connection drops during party mode. It skips the mic read when
    // partyMode==true, so partyProcessAudio() below handles audio safely.
    maintainDeepgram();
    partyProcessAudio();
    pHueBase = fmodf(pHueBase + 0.55f, 360.0f);
    if ((millis() - partyModeTs) > (uint32_t)PARTY_MODE_SEC * 1000UL) {
        partyVizMode = (partyVizMode + 1) % 2;
        partyLedMode = (partyLedMode + 1) % 4;
        partyModeTs  = millis();
        partyClearScreen();
        Serial.printf("[Party] viz=%d led=%d\n", partyVizMode, partyLedMode);
    }
    switch (partyVizMode) { case 0: partyDrawSpectrum(); break; case 1: partyDrawMirror(); break; }
    partyHandleBeatFlash();
    partyDrawLevelMeter();
    partyUpdateLED();
    delay(16);
}

// ── Check transcript for party commands ───────────────────────
static int checkPartyCommand(const String& transcript) {
    String s = transcript; s.trim(); s.toLowerCase();
    if (s.indexOf("party on")   >= 0) return  1;
    if (s.indexOf("party mode") >= 0) return  1;
    if (s.indexOf("party off")  >= 0) return -1;
    if (s.indexOf("stop party") >= 0) return -1;
    if (s.indexOf("end party")  >= 0) return -1;
    return 0;
}

// ============================================================
// NOISE FILTER
// ============================================================
bool isNoise(const String& t) {
    String s = t; s.trim();
    if (s.length() < 3) return true;

    static const char* nw[] = {
        "...", "..", ".", "ah", "uh", "hm", "hmm", "mm", "um", "huh",
        "oh", "ow", "beep", "boop", "ding", "dong", "ping", "ring",
        "the", "a", "i", nullptr
    };
    String lower = s; lower.toLowerCase();
    for (int i = 0; nw[i]; i++)
        if (lower == String(nw[i])) return true;

    int spaceIdx = lower.indexOf(' ');
    if (spaceIdx > 0) {
        String fw = lower.substring(0, spaceIdx);
        bool allSame = true; int wi = 0;
        while (wi < (int)lower.length()) {
            int sp = lower.indexOf(' ', wi);
            String w = (sp < 0) ? lower.substring(wi) : lower.substring(wi, sp);
            w.trim();
            if (w.length() > 0 && w != fw) { allSame = false; break; }
            wi = (sp < 0) ? lower.length() : sp + 1;
        }
        if (allSame && fw.length() <= 6) return true;
    }
    return false;
}

// ============================================================
// FACE STATE MACHINE
// ============================================================
enum FaceState {
    FS_IDLE, FS_TALKING, FS_LISTEN, FS_THINK, FS_HAPPY, FS_SURPRISED, FS_SLEEP
};

struct FaceData {
    FaceState state       = FS_IDLE;
    float  bobPh          = 0.f;
    int8_t bobY           = 0;
    bool   blink          = false;
    int    blinkF         = 0;
    float  talkPh         = 0.f;
    float  mOpen          = 0.f;
    float  listenPulse    = 0.f;
    float  thinkSq        = 0.f;
    float  happyPh        = 0.f;
    float  surpriseScale  = 1.f;
    float  eyeScaleX      = 1.f;
    float  eyeScaleY      = 1.f;
    uint32_t emotionTimer = 0;
    float  lookX          = 0.f;
    float  lookY          = 0.f;
    float  tLookX         = 0.f;
    float  tLookY         = 0.f;
    uint32_t nextLookMs   = 0;
} face;

bool            faceRedraw   = false;
static uint32_t lastBlink    = 0;
static uint32_t nextBlink    = 3200;
static uint32_t lastFaceAnim = 0;

// ============================================================
// ZZZ SLEEP ANIMATION
// ============================================================
struct ZzzParticle { int16_t x, y; uint8_t ph, sz; };
static ZzzParticle s_zzz[3];
static const int16_t ZZZ_OX[3] = { FCX+22, FCX+30, FCX+20 };
static int16_t       ZZZ_OY[3] = { 0, 0, 0 };

static void initZzz() {
    ZZZ_OY[0] = faceCY + 22;
    ZZZ_OY[1] = faceCY + 10;
    ZZZ_OY[2] = faceCY - 4;
    s_zzz[0] = { ZZZ_OX[0], ZZZ_OY[0], 0,   1 };
    s_zzz[1] = { ZZZ_OX[1], ZZZ_OY[1], 85,  1 };
    s_zzz[2] = { ZZZ_OX[2], ZZZ_OY[2], 170, 2 };
}

static void updateZzz() {
    for (int i = 0; i < 3; i++) {
        s_zzz[i].ph++;
        s_zzz[i].x = constrain(
            (int16_t)(ZZZ_OX[i] + (uint16_t)s_zzz[i].ph * 18 / 255), 0, W - 12);
        s_zzz[i].y = constrain(
            (int16_t)(ZZZ_OY[i] - (uint16_t)s_zzz[i].ph * 55 / 255), 4, LOG_Y - 1);
    }
}

static void renderZzz() {
    if (!faceCanvas) return;
    for (int i = 0; i < 3; i++) {
        uint16_t col = (s_zzz[i].ph < 100) ? C_CY
                     : (s_zzz[i].ph < 190) ? C_DCY
                     : (uint16_t)0x0209;
        faceCanvas->setTextSize(s_zzz[i].sz);
        faceCanvas->setTextColor(col);
        faceCanvas->setCursor(s_zzz[i].x, s_zzz[i].y);
        faceCanvas->print("z");
    }
    faceCanvas->setTextSize(1);
}

// ============================================================
// FACE DRAW HELPERS
// ============================================================
static void drawSmileSprite(int cx, int cy) {
    faceCanvas->fillCircle(cx, cy, SMILE_R, C_WH);
    faceCanvas->fillRect(cx - SMILE_R - 1, cy - SMILE_R - 1,
                         (SMILE_R + 1) * 2 + 2, SMILE_R + 2, C_BK);
    int innerR = SMILE_R - SMILE_TH;
    if (innerR > 1) {
        faceCanvas->fillCircle(cx, cy, innerR, C_BK);
        faceCanvas->fillRect(cx - innerR - 1, cy - innerR - 1,
                             (innerR + 1) * 2 + 2, innerR + 2, C_BK);
    }
}

static void drawOneEyeSprite(int cx, int cy, float openFrac, float squint,
                              float scaleX, float scaleY) {
    int ew = max(8, (int)(EW * scaleX));
    int eh = max(2, (int)(EH * openFrac * scaleY * (1.f - squint * 0.55f)));
    int r  = min(ER, min(ew / 2, eh / 2));
    faceCanvas->fillRoundRect(cx - ew / 2, cy - eh / 2, ew, eh, r, C_WH);
}

static void drawMouthSprite(int cx, int cy, float openFrac) {
    int mh = MH_CL + (int)((MH_OP - MH_CL) * openFrac);
    int r  = min(MR, mh / 2);
    faceCanvas->fillRoundRect(cx - MW / 2, cy - mh / 2, MW, mh, r, C_WH);
}

void drawFaceBg() {
    if (faceCanvas) {
        faceCanvas->fillScreen(C_BK);
    } else {
        tft.fillRect(0, faceBlitY, W, LOG_Y, C_BK);
    }
}

void drawFace(bool /*full*/) {
    if (!faceCanvas) return;
    faceCanvas->fillScreen(C_BK);

    int by  = face.bobY;
    int lxi = (int)(face.lookX * LOOK_X_RANGE);
    int lyi = (int)(face.lookY * LOOK_Y_RANGE);
    int lex = FCX - ESEP / 2 + lxi;
    int rex = FCX + ESEP / 2 + lxi;
    int ey  = faceCY + EYO + by + lyi;
    int my  = faceCY + MYO + by;

    if (face.state == FS_SLEEP) {
        faceCanvas->fillRoundRect(lex - EW / 2, ey - 2, EW, 4, 2, C_WH);
        faceCanvas->fillRoundRect(rex - EW / 2, ey - 2, EW, 4, 2, C_WH);
        drawSmileSprite(FCX, my);
        renderZzz();
        blitFace();
        return;
    }

    float blinkFrac = 1.f;
    if (face.blink) {
        int bf = face.blinkF;
        blinkFrac = (bf <= 4) ? (1.f - bf / 4.f) : ((bf - 4) / 5.f);
        blinkFrac = constrain(blinkFrac, 0.f, 1.f);
    }
    float squint = (face.state == FS_THINK) ? face.thinkSq : 0.f;
    float sx = face.eyeScaleX;
    float sy = face.eyeScaleY;
    if (face.state == FS_SURPRISED) { sx = face.surpriseScale; sy = face.surpriseScale; }
    if (face.state == FS_LISTEN) sy *= (0.82f + sinf(face.listenPulse) * 0.18f);

    drawOneEyeSprite(lex, ey, blinkFrac, squint, sx, sy);
    drawOneEyeSprite(rex, ey, blinkFrac, squint, sx, sy);

    if (face.state == FS_TALKING || face.state == FS_SURPRISED)
        drawMouthSprite(FCX, my, face.mOpen);
    else
        drawSmileSprite(FCX, my);

    blitFace();
}

void animFace() {
    uint32_t now = millis();
    if (now - lastFaceAnim < 16) return;
    lastFaceAnim = now;
    bool ch = false;

    if (face.emotionTimer > 0 && now > face.emotionTimer) {
        face.emotionTimer = 0; face.state = FS_IDLE; ch = true;
    }
    if (face.state != FS_LISTEN && face.state != FS_SLEEP) {
        face.bobPh += 0.020f;
        if (face.bobPh > 6.2832f) face.bobPh -= 6.2832f;
        int8_t nb = (int8_t)roundf(sinf(face.bobPh) * BOB);
        if (nb != face.bobY) { face.bobY = nb; ch = true; }
    }
    if (face.state != FS_SURPRISED && face.state != FS_SLEEP) {
        float bScale = 1.f + sinf(face.bobPh * 0.5f) * 0.04f;
        if (fabsf(bScale - face.eyeScaleX) > 0.004f) {
            face.eyeScaleX = bScale; face.eyeScaleY = 2.f - bScale; ch = true;
        }
    }

    bool canLook = (face.state == FS_IDLE || face.state == FS_HAPPY);
    if (canLook) {
        if (now >= face.nextLookMs) {
            face.tLookX = ((float)(random(7)) / 3.f - 1.f);
            face.tLookY = ((float)(random(5)) / 2.f - 1.f) * 0.6f;
            if (random(5) == 0) { face.tLookX = 0.f; face.tLookY = 0.f; }
            face.nextLookMs = now + 600 + random(2000);
        }
        face.lookX += (face.tLookX - face.lookX) * 0.07f;
        face.lookY += (face.tLookY - face.lookY) * 0.07f;
        if (fabsf(face.lookX - face.tLookX) > 0.01f ||
            fabsf(face.lookY - face.tLookY) > 0.01f) ch = true;
    } else {
        face.lookX *= 0.85f; face.lookY *= 0.85f;
        if (fabsf(face.lookX) > 0.01f || fabsf(face.lookY) > 0.01f) ch = true;
    }
    if (face.state == FS_THINK) {
        face.lookX += (0.55f - face.lookX) * 0.06f;
        face.lookY += (-0.5f - face.lookY) * 0.06f;
        ch = true;
    }

    bool canBlink = (face.state == FS_IDLE || face.state == FS_TALKING ||
                     face.state == FS_HAPPY || face.state == FS_LISTEN);
    if (canBlink && !face.blink && now - lastBlink > nextBlink) {
        face.blink = true; face.blinkF = 0;
        nextBlink = 2200 + (uint32_t)random(2800);
    }
    if (face.blink) {
        face.blinkF++;
        if (face.blinkF >= 9) { face.blink = false; face.blinkF = 0; lastBlink = now; }
        ch = true;
    }

    switch (face.state) {
        case FS_TALKING: {
            face.talkPh += 0.40f;
            if (face.talkPh > 6.2832f) face.talkPh -= 6.2832f;
            float jaw = sinf(face.talkPh) * 0.55f;
            float t = constrain(0.28f + jaw
                              + sinf(face.talkPh * 1.7f) * 0.22f
                              + sinf(face.talkPh * 0.4f) * 0.12f, 0.f, 1.f);
            if (fabsf(t - face.mOpen) > 0.015f) { face.mOpen = t; ch = true; }
            float eyePop = 1.f + fabsf(jaw) * 0.05f;
            if (fabsf(eyePop - face.eyeScaleY) > 0.01f) { face.eyeScaleY = eyePop; ch = true; }
            break;
        }
        case FS_LISTEN:
            face.listenPulse += 0.07f;
            if (face.listenPulse > 6.2832f) face.listenPulse -= 6.2832f;
            face.lookX += (0.0f   - face.lookX) * 0.05f;
            face.lookY += (-0.35f - face.lookY) * 0.05f;
            if (fabsf(face.lookY + 0.35f) > 0.01f) ch = true;
            ch = true;
            break;
        case FS_THINK:
            if (face.thinkSq < 0.72f) { face.thinkSq += 0.03f; ch = true; }
            break;
        case FS_HAPPY: {
            face.happyPh += 0.14f;
            if (face.happyPh > 6.2832f) face.happyPh -= 6.2832f;
            float hB = 1.f + sinf(face.happyPh) * 0.07f;
            if (fabsf(hB - face.eyeScaleY) > 0.01f) { face.eyeScaleY = hB; ch = true; }
            break;
        }
        case FS_SURPRISED:
            if (fabsf(face.surpriseScale - 1.30f) > 0.01f) {
                face.surpriseScale += (1.30f - face.surpriseScale) * 0.18f; ch = true;
            }
            if (face.mOpen < 0.90f) { face.mOpen += 0.12f; ch = true; }
            break;
        case FS_SLEEP:
            updateZzz(); ch = true;
            break;
        default: break;
    }

    if (ch) faceRedraw = true;
}

void setFaceIdle() {
    face.state = FS_IDLE; face.thinkSq = 0.f; face.mOpen = 0.f;
    face.surpriseScale = 1.f; face.emotionTimer = 0; faceRedraw = true;
}
void setFaceTalk() {
    face.state = FS_TALKING; face.talkPh = 0.f; face.thinkSq = 0.f;
    face.emotionTimer = 0; faceRedraw = true;
}
void setFaceThink() {
    face.state = FS_THINK; face.thinkSq = 0.f; face.emotionTimer = 0; faceRedraw = true;
}
void setFaceHappy(uint32_t ms = 1800) {
    face.state = FS_HAPPY; face.happyPh = 0.f; face.mOpen = 0.f;
    face.emotionTimer = millis() + ms; faceRedraw = true;
}
void setFaceListen() {
    face.state = FS_LISTEN; face.listenPulse = 0.f; face.thinkSq = 0.f;
    face.mOpen = 0.f; face.emotionTimer = 0; faceRedraw = true;
}
void setFaceSurprised(uint32_t ms = 1200) {
    face.state = FS_SURPRISED; face.surpriseScale = 1.f; face.mOpen = 0.f;
    face.emotionTimer = millis() + ms; faceRedraw = true;
}
void setFaceSleep() {
    face.state = FS_SLEEP; face.bobY = 0; face.bobPh = 0.f;
    face.lookX = 0.f; face.lookY = 0.f; face.mOpen = 0.f;
    face.emotionTimer = 0; faceRedraw = true; initZzz();
}
void startTalk() { setFaceTalk(); }
void stopTalk()  { setFaceIdle(); }

// ============================================================
// BOOT ANIMATION
// ============================================================
static GFXcanvas16* bootLogo = nullptr;
#define BLOGO_SZ  128
#define BLOGO_X   ((W - BLOGO_SZ) / 2)
#define BLOGO_Y   14

static inline void blitLogo() {
    if (bootLogo)
        tft.drawRGBBitmap(BLOGO_X, BLOGO_Y, bootLogo->getBuffer(), BLOGO_SZ, BLOGO_SZ);
}

static uint16_t dimCol(uint16_t c, int factor) {
    if (factor <= 0) return 0;
    if (factor >= 8) return c;
    uint16_t r = ((c >> 11) & 0x1F) * factor / 8;
    uint16_t g = ((c >> 5)  & 0x3F) * factor / 8;
    uint16_t b = (c & 0x1F) * factor / 8;
    return (r << 11) | (g << 5) | b;
}

static inline int lerpi(int a, int b, int f, int fmax) {
    if (fmax <= 0) return b;
    if (f <= 0)    return a;
    if (f >= fmax) return b;
    return a + (b - a) * f / fmax;
}

static void drawBootStars() {
    for (int i = 0; i < 80; i++) {
        int x = (i * 137 + 11) % W;
        int y = (i * 93  + 7)  % (H - 28) + 5;
        uint16_t col = (i % 5 == 0) ? C_WH  :
                       (i % 4 == 0) ? C_CY  :
                       (i % 3 == 0) ? C_DCY : C_DG;
        tft.drawPixel(x, y, col);
        if (i % 6 == 0) tft.drawPixel(x + 1, y, dimCol(col, 3));
    }
}

static void logoDrawB(int sc) {
    if (!bootLogo || sc <= 0) return;
    bootLogo->fillScreen(C_BK);
    int bh   = sc * 10;
    int bsw  = sc * 3 / 2;
    int bmpW = sc * 6;
    int bmpH = bh / 2;
    int br   = max(2, sc);
    int bx   = BLOGO_SZ/2 - bmpW/2;
    int by   = BLOGO_SZ/2 - bh/2;
    if (sc >= 5) {
        bootLogo->drawCircle(64, 64, 52, C_DCY);
        bootLogo->drawCircle(64, 64, 54, dimCol(C_CY, 3));
        bootLogo->drawCircle(64, 64, 56, dimCol(C_CY, 1));
    }
    bootLogo->fillRect(bx, by, bsw, bh, C_CY);
    bootLogo->fillRoundRect(bx, by, bmpW, bmpH + br/2, br, C_CY);
    if (bmpW - bsw - br > 2 && bmpH - br*2 > 2)
        bootLogo->fillRoundRect(bx + bsw, by + br,
                                bmpW - bsw - br, bmpH - br + br/2 - br, max(1, br - 2), C_BK);
    bootLogo->fillRoundRect(bx, by + bmpH, bmpW + sc/2, bmpH, br, C_MINT);
    if (bmpW - bsw - br > 2 && bmpH - br*2 > 2)
        bootLogo->fillRoundRect(bx + bsw, by + bmpH + br,
                                bmpW - bsw - br + sc/2, bmpH - br*2, max(1, br - 2), C_BK);
}

static void logoDrawRobot(int morph) {
    if (!bootLogo) return;
    bootLogo->fillScreen(C_BK);
    int m = min(max(morph, 0), 20);
    bootLogo->drawCircle(64, 64, 52, C_DCY);
    bootLogo->drawCircle(64, 64, 54, dimCol(C_CY, 3));
    bootLogo->drawCircle(64, 64, 56, dimCol(C_CY, 1));
    int fx0 = lerpi(58, 20, m, 20), fy0 = lerpi(24, 22, m, 20);
    int fx1 = lerpi(104, 108, m, 20), fy1 = lerpi(104, 96, m, 20);
    int fw  = fx1 - fx0, fh = fy1 - fy0;
    bootLogo->fillRoundRect(fx0, fy0, fw, fh, 8, 0x0412);
    bootLogo->drawRoundRect(fx0, fy0, fw, fh, 8, C_CY);
    bootLogo->drawRoundRect(fx0 + 1, fy0 + 1, fw - 2, fh - 2, 7, dimCol(C_CY, 4));
    int lx0 = lerpi(58, 28, m, 20), ly0 = lerpi(24, 38, m, 20);
    int lew = lerpi(48, 28, m, 20), leh = lerpi(44, 20, m, 20), ler = lerpi(8, 4, m, 20);
    bootLogo->fillRoundRect(lx0, ly0, lew, leh, ler, C_WH);
    int rx0 = lerpi(58, 72, m, 20), ry0 = lerpi(64, 38, m, 20);
    int rew = lerpi(50, 28, m, 20), reh = lerpi(40, 20, m, 20), rer = lerpi(8, 4, m, 20);
    bootLogo->fillRoundRect(rx0, ry0, rew, reh, rer, C_WH);
    if (m > 10) {
        int pa  = m - 10, pr = lerpi(0, 5, pa, 10);
        int lcx = lx0 + lew / 2, lcy = ly0 + leh / 2;
        int rcx = rx0 + rew / 2, rcy = ry0 + reh / 2;
        if (pr > 0) {
            bootLogo->fillCircle(lcx, lcy, pr, C_BK);
            bootLogo->fillCircle(rcx, rcy, pr, C_BK);
            if (pr >= 3) {
                bootLogo->drawPixel(lcx + 1, lcy - 1, C_WH);
                bootLogo->drawPixel(rcx + 1, rcy - 1, C_WH);
            }
        }
        if (pa > 5) {
            bootLogo->drawCircle(lcx, lcy, pr + 2, dimCol(C_CY, 3));
            bootLogo->drawCircle(rcx, rcy, pr + 2, dimCol(C_CY, 3));
        }
    }
    if (m < 10) {
        int sw = lerpi(12, 0, m, 10);
        if (sw > 0) {
            uint16_t sc = dimCol(C_CY, lerpi(8, 1, m, 10));
            int sx = 64 - sw / 2;
            bootLogo->fillRect(sx, lerpi(24, 22, m, 10), sw, lerpi(80, 74, m, 10), sc);
        }
    }
    if (m > 6) {
        int aa = m - 6, atip = lerpi(fy0, fy0 - 16, aa, 14), abal = lerpi(0, 4, aa, 14);
        bootLogo->drawFastVLine(64, atip, fy0 - atip, C_CY);
        if (abal > 0) {
            bootLogo->fillCircle(64, atip, abal, C_CY);
            if (abal >= 3) bootLogo->fillCircle(64, atip, 2, C_WH);
        }
    }
    if (m > 14) {
        int ma = m - 14, mx_c = (fx0 + fx1) / 2;
        int mw = lerpi(0, 32, ma, 6), my = lerpi(fy1 + 6, fy1 - 12, ma, 6);
        if (mw > 2) bootLogo->fillRoundRect(mx_c - mw / 2, my, mw, 6, 3, C_WH);
    }
}

void drawBootBar(int pct) {
    if (pct > 100) pct = 100;
    const int BX = 40, BY = H - 14, BW = W - 80, BH = 7;
    tft.fillRoundRect(BX - 1, BY - 1, BW + 2, BH + 2, 4, 0x18C3);
    tft.fillRoundRect(BX, BY, BW, BH, 3, 0x0841);
    int fw = (int)((float)BW * pct / 100.f);
    if (fw > 3) {
        tft.fillRoundRect(BX, BY, fw, BH, 3, C_CY);
        if (fw > 6) tft.fillRoundRect(BX, BY, fw - 3, BH, 3, C_MINT);
        tft.drawFastHLine(BX, BY, fw, C_WH);
    }
}

void drawBootLogo() {
    tft.fillScreen(C_BK);
    drawBootStars();
    bootLogo = new GFXcanvas16(BLOGO_SZ, BLOGO_SZ);
    if (!bootLogo || !bootLogo->getBuffer()) {
        Serial.println("[Boot] Canvas alloc failed");
        bootLogo = nullptr;
    } else {
        logoDrawB(8);
        blitLogo();
    }
    drawBootBar(0);
}

void playBootIntroAnim(int /*cx*/, int /*cy*/) {
    if (!bootLogo) return;
    const int TY = BLOGO_Y + BLOGO_SZ + 4;
    for (int f = 0; f < 14; f++) { logoDrawB(lerpi(1, 8, f, 13)); blitLogo(); delay(16); yield(); }
    logoDrawB(8); blitLogo();
    for (int f = 0; f < 22; f++) { logoDrawRobot(f * 20 / 21); blitLogo(); delay(16); yield(); }
    logoDrawRobot(20); blitLogo();
    for (int f = 0; f < 10; f++) {
        logoDrawRobot(20);
        int r = 52 + f * 3;
        if (r < 70) bootLogo->drawCircle(64, 64, r, dimCol(C_CY, max(1, 7 - f)));
        blitLogo(); delay(20); yield();
    }
    for (int f = 0; f < 18; f++) {
        int ty = TY + max(0, 18 - f);
        tft.fillRect(0, TY, W, 46, C_BK);
        int bright = min(8, f + 1);
        tft.setTextSize(3); tft.setTextColor(dimCol(C_WH, bright));
        tft.setCursor(W / 2 - 54, ty); tft.print("BRONNY");
        if (f > 8) {
            tft.fillRoundRect(W / 2 + 58, ty - 1, 30, 18, 4, C_CY);
            tft.setTextSize(1); tft.setTextColor(C_BK);
            tft.setCursor(W / 2 + 63, ty + 5); tft.print("AI");
        }
        if (f > 12) {
            const char* credit = "by Patrick Perez  v1.2";
            tft.setTextSize(1); tft.setTextColor(dimCol(C_LG, min((f - 12) * 2, 8)));
            tft.setCursor(W / 2 - (int)strlen(credit) * 3, ty + 26);
            tft.print(credit);
        }
        delay(16); yield();
    }
}

// ============================================================
// WIFI SCREEN
// ============================================================
void drawWifiScreen() {
    tft.fillScreen(C_BK);
    for (int i = 0; i < 70; i++) {
        int x = (i * 137 + 31) % W;
        int y = (i * 93  + 17) % (H - 28) + 5;
        uint16_t col = (i % 5 == 0) ? C_WH :
                       (i % 4 == 0) ? C_CY :
                       (i % 3 == 0) ? C_DCY : C_DG;
        tft.drawPixel(x, y, col);
    }
    tft.fillRect(0, 0, W, 30, 0x18C3);
    tft.drawFastHLine(0, 0,  W, C_CY);
    tft.drawFastHLine(0, 30, W, C_DCY);
    tft.fillCircle(14, 15, 5, C_CY); tft.fillCircle(14, 15, 2, C_BK);
    tft.setTextColor(C_WH); tft.setTextSize(1); tft.setCursor(25, 10); tft.print("BRONNY AI");
    tft.setTextColor(C_CY);                     tft.setCursor(82, 10); tft.print("v1.2");
    tft.fillRoundRect(W - 88, 6, 84, 18, 4, C_DCY);
    tft.drawRoundRect(W - 88, 6, 84, 18, 4, C_CY);
    tft.setTextColor(C_CY); tft.setCursor(W - 82, 12); tft.print("NETWORK SETUP");
    int ix = W / 2, iy = 78;
    tft.drawCircle(ix, iy + 20, 36, C_DG);
    tft.drawCircle(ix, iy + 20, 26, C_DCY);
    tft.drawCircle(ix, iy + 20, 16, C_CY);
    tft.fillCircle(ix, iy + 20,  6, C_CY);
    tft.fillRect(ix - 40, iy + 20, 80, 44, C_BK);
    tft.fillRoundRect(16, 130, W - 32, 26, 5, 0x18C3);
    tft.drawRoundRect(16, 130, W - 32, 26, 5, C_CY);
    tft.setTextColor(C_DCY); tft.setTextSize(1); tft.setCursor(28, 137); tft.print("Network:");
    tft.setTextColor(C_WH);                      tft.setCursor(86, 137); tft.print(WIFI_SSID);
    tft.fillRect(0, H - 22, W, 22, 0x18C3);
    tft.drawFastHLine(0, H - 22, W, C_DCY);
    tft.setTextColor(C_DG);  tft.setTextSize(1); tft.setCursor(6, H - 14);       tft.print("ESP32-S3");
    tft.setTextColor(C_DCY);                     tft.setCursor(W/2-48, H - 14);  tft.print("Bronny AI v1.2");
    tft.setTextColor(C_DG);                      tft.setCursor(W - 72, H - 14);  tft.print("Patrick 2026");
}

void drawWifiStatus(const char* l1, uint16_t c1, const char* l2 = "", uint16_t c2 = C_CY) {
    tft.fillRect(0, 160, W, H - 22 - 160, C_BK);
    tft.setTextSize(2); tft.setTextColor(c1);
    int tw = (int)strlen(l1) * 12; tft.setCursor(W / 2 - tw / 2, 166); tft.print(l1);
    if (strlen(l2) > 0) {
        tft.setTextSize(1); tft.setTextColor(c2);
        int tw2 = (int)strlen(l2) * 6; tft.setCursor(W / 2 - tw2 / 2, 192); tft.print(l2);
    }
}

static uint8_t spinIdx = 0; static uint32_t lastSpin = 0;
void tickWifiSpinner() {
    uint32_t now = millis(); if (now - lastSpin < 250) return; lastSpin = now;
    static const char* frames[] = { "|", "/", "-", "\\" };
    tft.fillRect(W / 2 - 4, 113, 8, 10, C_BK);
    tft.setTextSize(1); tft.setTextColor(C_CY);
    tft.setCursor(W / 2 - 3, 114); tft.print(frames[spinIdx++ % 4]);
}

// ============================================================
// STANDBY MODE
// ============================================================
bool isWakeWord(const String& t) {
    String s = t; s.trim(); s.toLowerCase();
    static const char* ww[] = {
        "bronny", "bronnie", "brony", "brownie", "brawny", "bonnie",
        "hi bronny", "hey bronny", "hi bronnie", "hey bronnie",
        "hi brony", "hey brony", "hi brownie", "hey brownie",
        "hi brawny", "hey brawny", "hi bonnie", "hey bonnie",
        nullptr
    };
    for (int i = 0; ww[i]; i++)
        if (s.indexOf(String(ww[i])) >= 0) return true;
    return false;
}

void enterStandby() {
    standbyMode = true;
    setFaceSleep(); drawFace(true);
    setStatus("Standby...", C_DCY);
    tftLog(C_CY, "Standby — say 'Hi Bronny'");
    Serial.println("[Standby] Entering standby mode");
}

void exitStandby() {
    standbyMode   = false;
    lastRailwayMs = millis();
    setFaceSurprised(600); drawFace(true);
    jingleWake();
    setStatus("Listening...", C_CY);
    tftLog(C_GR, "Bronny: awake!");
    Serial.println("[Standby] Exiting standby mode");
}

// ============================================================
// BOOT INTRO
// ============================================================
void doBootIntro() {
    if (!dgConnected) { Serial.println("[Intro] Skipped — Deepgram not connected"); return; }
    tftLog(C_CY, "Bronny: hello!");
    setFaceThink();
    dgStreaming = false;
    bool ok = callRailwayStream("bootup_intro");
    if (!ok) tftLog(C_RD, "Intro: Railway failed");
    if (ok) {
        setFaceHappy(1200);
        uint32_t e = millis() + 1200;
        while (millis() < e) {
            animFace();
            if (faceRedraw) { drawFace(false); faceRedraw = false; }
            maintainDeepgram(); yield();
        }
    }
    stopTalk(); setFaceIdle(); forceDrawFace();
    lastRailwayMs   = millis();
    dgStreaming     = true;
    dgLastKeepalive = millis();
    setFaceListen();
    setStatus("Listening...", C_CY);
    Serial.println("[Intro] Boot intro complete");
}

// ============================================================
// CONVERSATION PIPELINE
// ============================================================
void runConversation() {
    if (busy) return;
    busy = true;
    dgStreaming = false;

    pendingTranscript = false;
    dgFinalReceivedAt = 0;
    String transcript = dgFinal;
    dgFinal   = "";
    dgPartial = "";

    // ── Party commands (highest priority) ────────────────────
    int partyCmd = checkPartyCommand(transcript);

    if (partyCmd == 1 && !partyMode) {
        // "party on"
        tftLogf(C_YL, "Party: ON");
        jingleParty();
        enterPartyMode();
        // enterPartyMode() already set dgStreaming=true
        busy = false;
        return;
    }

    if (partyCmd == -1 && partyMode) {
        // "party off"
        exitPartyMode();
        dgStreaming     = true;
        dgLastKeepalive = millis();
        busy = false;
        return;
    }

    // If still in party mode but command was not party-related, ignore transcript
    if (partyMode) {
        dgStreaming     = true;
        dgLastKeepalive = millis();
        busy = false;
        return;
    }

    // ── Standby check ─────────────────────────────────────────
    if (standbyMode) {
        if (isWakeWord(transcript)) {
            Serial.printf("[Standby] Wake word: '%s'\n", transcript.c_str());
            exitStandby();
        } else {
            Serial.printf("[Standby] Ignored: '%s'\n", transcript.c_str());
        }
        dgStreaming = true; dgLastKeepalive = millis();
        busy = false; return;
    }

    // ── Noise filter ──────────────────────────────────────────
    if (isNoise(transcript)) {
        tftLogf(C_YL, "Filtered: '%s'", transcript.c_str());
        dgStreaming = true; dgLastKeepalive = millis();
        setStatus("Listening...", C_CY);
        busy = false; return;
    }

    // ── Normal conversation ───────────────────────────────────
    tftLogf(C_MINT, "You: %s", transcript.c_str());
    setFaceThink();

    i2s.setVolume(0.18f);
    playTone(1047, 55); playSil(20); playTone(1319, 80);
    i2s.setVolume(VOL_MAIN);

    tftLog(C_YL, "Railway: thinking...");
    bool ok = callRailwayStream(transcript);

    if (!ok) {
        tftLog(C_RD, "Railway failed");
        stopTalk(); forceDrawFace();
        dgStreaming = true; dgLastKeepalive = millis();
        setFaceListen();
        setStatus("Listening...", C_CY);
        busy = false; return;
    }

    lastRailwayMs    = millis();
    vadCooldownUntil = millis() + TTS_COOLDOWN_MS;
    setFaceHappy(1600);

    if (micOk) {
        uint8_t drain[512];
        uint32_t de = millis() + TTS_COOLDOWN_MS;
        while (millis() < de) {
            mic_stream.readBytes(drain, sizeof(drain));
            maintainDeepgram();
            animFace();
            if (faceRedraw) { drawFace(false); faceRedraw = false; }
            yield();
        }
    }

    dgStreaming       = true;
    dgLastKeepalive   = millis();
    pendingTranscript = false;
    dgFinal           = "";
    dgPartial         = "";
    dgFinalReceivedAt = 0;
    setFaceListen();
    setStatus("Listening...", C_CY);
    busy = false;
}

// ============================================================
// SETUP
// ============================================================
void setup() {
    Serial.begin(115200);
    AudioToolsLogger.begin(Serial, AudioToolsLogLevel::Warning);
    delay(400);

    pinMode(PIN_BLK, OUTPUT); digitalWrite(PIN_BLK, HIGH);
    pinMode(PIN_PA,  OUTPUT); digitalWrite(PIN_PA,  LOW);
    pinMode(PIN_BOOT, INPUT_PULLUP);    // ← BOOT button

    tftSPI.begin(PIN_CLK, -1, PIN_MOSI, PIN_CS);
    tft.init(240, 320); tft.setRotation(3); tft.fillScreen(C_BK);

    faceCanvas = new GFXcanvas16(W, LOG_Y);
    if (!faceCanvas || !faceCanvas->getBuffer()) {
        Serial.println("[Sprite] FATAL: canvas alloc failed.");
        faceCanvas = nullptr;
    } else {
        faceCanvas->fillScreen(C_BK);
    }

    buildPartyBandMap();     // ← build FFT band map for party mode

    audioRestart(); i2s.setVolume(VOL_MAIN);
    if (audioOk) { auto sc = sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }

    micInit();
    logsVisible = true;

    drawBootLogo();
    playBootIntroAnim(W / 2, H / 2 - 32);
    drawBootBar(10); jingleBoot(); drawBootBar(55); delay(150); drawBootBar(100); delay(300);

    delete bootLogo; bootLogo = nullptr;

    drawWifiScreen(); drawWifiStatus("Connecting...", C_YL);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    bool connected = false; uint32_t ws = millis();
    while (millis() - ws < 18000) {
        if (WiFi.status() == WL_CONNECTED) { connected = true; break; }
        tickWifiSpinner(); yield();
    }
    if (connected) {
        char ip[32]; snprintf(ip, 32, "%s", WiFi.localIP().toString().c_str());
        drawWifiStatus("Connected!", C_GR, ip, C_CY); jingleConnect(); delay(900);
    } else {
        drawWifiStatus("FAILED", C_RD, "Check config", C_RD); jingleError(); delay(2000);
    }

    sendHeartbeat(); lastHbMs = millis();

    drawDgScreen();
    connectDeepgram();
    dgStreaming = true;

    tft.fillScreen(C_BK);
    setLogsVisible(false);
    jingleReady();

    tftLog(C_GR, "Bronny AI v1.2 ready");
    tftLogf(C_CY, "WiFi: %s", WiFi.localIP().toString().c_str());
    tftLogf(C_LG, "Heap %uK  PSRAM %uK",
            esp_get_free_heap_size() / 1024,
            heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024);
    tftLog(C_DCY, "BOOT btn = toggle logs");

    setFaceListen();
    setStatus("Listening...", C_CY);

    lastRailwayMs = millis();
    bootReadyAt   = millis() + 2000;
}

// ============================================================
// LOOP
// ============================================================
void loop() {
    uint32_t now = millis();

    // ── PARTY MODE fast path ──────────────────────────────────
    if (partyMode) {
        partyLoop();

        // Heartbeat still runs in party mode
        if (now - lastHbMs > HEARTBEAT_MS) { lastHbMs = now; sendHeartbeat(); }

        // Check if DG has a pending transcript (e.g. "party off")
        if (!pendingTranscript && dgFinal.length() > 0 && dgFinalReceivedAt > 0
                && now - dgFinalReceivedAt > DG_FINAL_TIMEOUT_MS) {
            pendingTranscript = true;
            dgFinalReceivedAt = 0;
        }
        if (pendingTranscript && !busy && now > vadCooldownUntil)
            runConversation();

        yield();
        return;
    }

    // ── NORMAL MODE ───────────────────────────────────────────
    checkBootButton();   // ← BOOT button toggles log panel

    animFace();
    if (faceRedraw) { drawFace(false); faceRedraw = false; }

    if (now - lastHbMs > HEARTBEAT_MS) { lastHbMs = now; sendHeartbeat(); }

    maintainDeepgram();

    if (!bootIntroDone && !busy && dgConnected && now > bootReadyAt) {
        bootIntroDone = true;
        doBootIntro(); return;
    }

    if (!standbyMode && !busy && lastRailwayMs > 0 && now - lastRailwayMs > STANDBY_TIMEOUT_MS)
        enterStandby();

    if (!pendingTranscript && dgFinal.length() > 0 && dgFinalReceivedAt > 0
            && now - dgFinalReceivedAt > DG_FINAL_TIMEOUT_MS) {
        pendingTranscript = true;
        dgFinalReceivedAt = 0;
        Serial.println("[DG] speech_final timeout -> self-trigger");
    }

    if (pendingTranscript && !busy && now > vadCooldownUntil)
        runConversation();

    if (Serial.available()) {
        char c = Serial.read();
        if (c == 'm') tftLogf(C_CY, "DG conn=%d stream=%d", dgConnected ? 1 : 0, dgStreaming ? 1 : 0);
        if (c == 'l') setLogsVisible(!logsVisible);
        if (c == 'p') { if (!partyMode) enterPartyMode(); else exitPartyMode(); }
    }

    yield();
}
