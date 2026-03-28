/*
 * BRONNY AI v1.1
 * by Patrick Perez
 *
 * Hardware:
 *   Board   : ESP32-S3 Dev Module (OPI PSRAM 8MB)
 *   Codec   : ES8311 (I2C addr 0x18)
 *   Mic     : INMP441 (I2S port 1, GPIOs 4/5/6)
 *   Display : ST7789 320x240 (HSPI)
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
#define TTS_COOLDOWN_MS       800
#define HEARTBEAT_MS         30000

#define DG_KEEPALIVE_MS      8000
#define DG_RECONNECT_MS      3000
#define DG_CONNECT_TIMEOUT   8000
#define DG_FINAL_TIMEOUT_MS   700

#define STANDBY_TIMEOUT_MS  180000UL
#define STREAM_CHUNK_BYTES    512
#define MIN_AUDIO_BYTES      1024

// FIX 5: Increased from 300ms to 500ms to avoid false early exits on
// slow/congested networks where TCP packet gaps exceed 300ms.
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
#define PIN_PA   48
#define ES_ADDR  0x18

#define PIN_MIC_WS   4
#define PIN_MIC_SCK  5
#define PIN_MIC_SD   6

#define PIN_CS   47
#define PIN_DC   39
#define PIN_BLK  42
#define PIN_CLK  41
#define PIN_MOSI 40

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

// Forward declaration — defined in boot section
static uint16_t dimCol(uint16_t c, int factor);

#define LOG_Y        160
#define LOG_LINE_H    14
#define LOG_LINES      4
#define LOG_FOOTER_Y (H - 14)

#define FCX    160
// faceCY and faceBlitY are runtime-adjustable:
//   logs visible  → faceCY=72,  faceBlitY=0  (face in top 160px, log zone below)
//   logs hidden   → faceCY=80,  faceBlitY=40 (face canvas shifted down, centered in full H=240)
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
static String   gFooterText  = "v1.1 Ready";
static uint16_t gFooterColor = C_CY;

static inline void blitFace() {
    if (faceCanvas)
        tft.drawRGBBitmap(0, faceBlitY, faceCanvas->getBuffer(), W, LOG_Y);
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

static bool audioOk   = false;
static bool micOk     = false;
static bool inTtsMode = false;

static inline int16_t inmp441Sample(int32_t raw) { return (int16_t)(raw >> 14); }

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
    // FIX 7: INMP441 is a mono microphone. Changed from channels=2 to
    // channels=1. The L/R pin tied to GND selects the left channel output.
    // This halves the I2S buffer size and eliminates the unused right-channel
    // reads that were previously discarded in maintainDeepgram().
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

// ============================================================
// UTILITIES
// ============================================================
String jEsc(const String& s) {
    // UTF-8 multi-byte sequences pass through correctly: lead bytes are
    // 0xC2-0xFF and continuation bytes are 0x80-0xBF, all > 0x20.
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
    int code = http.POST("{\"device\":\"bronny\",\"version\":\"1.1\"}");
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
    // Separator line at top of log zone (just below canvas)
    tft.drawFastHLine(0, LOG_Y, W, C_CY);
    // Log area background
    tft.fillRect(0, LOG_Y + 1, W, LOG_LINES * LOG_LINE_H + 3, C_MID);
    int total = min(gLogCount, LOG_LINES);
    for (int i = 0; i < total; i++) {
        int slot = (gLogHead + i) % LOG_LINES;
        uint16_t c = gLogCol[slot];
        // Progressively dim older entries
        if      (i < total - 2) c = dimCol(c, 2);
        else if (i < total - 1) c = dimCol(c, 4);
        int ly = LOG_Y + 2 + i * LOG_LINE_H;
        // Colored left accent bar
        tft.fillRect(0, ly + 1, 3, LOG_LINE_H - 3, c);
        // Entry text
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
        // Face moves up: canvas blit at y=0, FCY=72
        faceCY    = 72;
        faceBlitY = 0;
        // Clear the strip that was above the canvas while hidden (y=0..39)
        // The canvas redraw will cover it, so just force redraw
        drawFace(true);
        // Draw log zone (separator, bg, entries, footer)
        logRedraw();
        logDrawFooter();
        Serial.println("[UI] Logs shown");
    } else {
        // Face moves down: canvas blit at y=40, FCY=80 → center = 40+80 = 120 = H/2
        faceCY    = 80;
        faceBlitY = 40;
        // Clear strip above repositioned canvas (y=0..39)
        tft.fillRect(0, 0, W, faceBlitY, C_BK);
        // Clear log zone (y=160..239)
        tft.fillRect(0, LOG_Y, W, H - LOG_Y, C_BK);
        // Redraw face at new centered position
        drawFace(true);
        Serial.println("[UI] Logs hidden");
    }
}

static int checkLogCommand(const String& transcript) {
    String s = transcript; s.trim(); s.toLowerCase();
    if (s.indexOf("hide log")     >= 0) return -1;
    if (s.indexOf("hide the log") >= 0) return -1;
    if (s.indexOf("remove log")   >= 0) return -1;
    if (s.indexOf("clear log")    >= 0) return -1;
    if (s.indexOf("show log")     >= 0) return 1;
    if (s.indexOf("display log")  >= 0) return 1;
    if (s.indexOf("show the log") >= 0) return 1;
    return 0;
}

// ============================================================
// RAILWAY STREAMING CALL
// ============================================================
bool callRailwayStream(const String& transcript) {
    if (transcript.isEmpty()) return false;
    // FIX 2: Guard against calling TTS before audio hardware is ready.
    if (!audioOk) {
        Serial.println("[Rail] Skipped — audio not ready");
        return false;
    }
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

// Animated ring indicator at cx,cy — spin 0/1/2 = which ring is "lit"
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

// Update the status text area only
static void drawDgStatus(const char* msg, uint16_t col) {
    tft.fillRect(0, 162, W, 22, C_BK);
    tft.setTextSize(2); tft.setTextColor(col);
    int tw = (int)strlen(msg) * 12;
    tft.setCursor(W / 2 - tw / 2, 164);
    tft.print(msg);
}

// Draw the full DG connection screen (static parts)
void drawDgScreen() {
    tft.fillScreen(C_BK);
    // Stars
    for (int i = 0; i < 70; i++) {
        int x = (i * 211 + 19) % W;
        int y = (i * 97  + 13) % (H - 28) + 5;
        uint16_t col = (i % 5 == 0) ? C_WH  :
                       (i % 4 == 0) ? C_CY  :
                       (i % 3 == 0) ? C_DCY : C_DG;
        tft.drawPixel(x, y, col);
    }
    // Header bar (same style as WiFi screen)
    tft.fillRect(0, 0, W, 30, 0x18C3);
    tft.drawFastHLine(0, 0,  W, C_CY);
    tft.drawFastHLine(0, 30, W, C_DCY);
    tft.fillCircle(14, 15, 5, C_CY); tft.fillCircle(14, 15, 2, C_BK);
    tft.setTextColor(C_WH); tft.setTextSize(1); tft.setCursor(25, 10); tft.print("BRONNY AI");
    tft.setTextColor(C_CY);                     tft.setCursor(82, 10); tft.print("v1.1");
    tft.fillRoundRect(W - 100, 6, 96, 18, 4, C_DCY);
    tft.drawRoundRect(W - 100, 6, 96, 18, 4, C_CY);
    tft.setTextColor(C_CY); tft.setCursor(W - 94, 12); tft.print("SPEECH ENGINE");

    // Section title
    tft.setTextSize(2); tft.setTextColor(C_WH);
    tft.setCursor(W / 2 - 54, 40); tft.print("DEEPGRAM");
    tft.setTextSize(1); tft.setTextColor(C_DCY);
    tft.setCursor(W / 2 - 33, 62); tft.print("nova-3  \xB7  ASR");

    // Divider line
    tft.drawFastHLine(W/2 - 60, 74, 120, C_DG);

    // Initial animated rings
    drawDgAnim(W / 2, 118, 0);

    // Initial status
    drawDgStatus("Connecting...", C_YL);

    // Footer
    tft.fillRect(0, H - 20, W, 20, 0x18C3);
    tft.drawFastHLine(0, H - 20, W, C_DCY);
    tft.setTextColor(C_DG);  tft.setTextSize(1);
    tft.setCursor(W / 2 - 48, H - 13); tft.print("Bronny AI v1.1");
}

// ============================================================
// DEEPGRAM PERSISTENT STREAMING ASR
// ============================================================
WebSocketsClient dgWs;

static bool     dgConnected         = false;
static bool     dgStreaming          = false;
static uint32_t dgLastKeepalive      = 0;
static uint32_t dgLastConnectAttempt = 0;

static bool     pendingTranscript    = false;
static String   dgFinal              = "";
static String   dgPartial            = "";
static uint32_t dgFinalReceivedAt    = 0;

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
            // FIX 4: Clear the timestamp here so the loop() timeout path
            // cannot also fire on the same utterance.
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

    // Animate on the DG screen while waiting
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
        Serial.println("[DG] WS connected");
    } else {
        drawDgAnim(W / 2, 118, 0);
        drawDgStatus("Timed Out", C_WARN);
        Serial.println("[DG] Connect timeout");
    }
    delay(700); yield();
}

// FIX 7: Mono mic — buffer is now 1600 mono frames (int32_t each = 6400 bytes).
// Previously 1600 stereo frames (int32_t pairs = 12800 bytes), halving waste.
static int32_t s_rawBuf[1600];
static int16_t s_pcmBuf[1600];

void maintainDeepgram() {
    uint32_t now = millis();
    dgWs.loop();

    if (!dgConnected && now - dgLastConnectAttempt > DG_RECONNECT_MS) {
        // FIX 3: Do not attempt reconnect while an active Railway call is
        // in progress. When busy clears the timer will already be expired
        // so reconnection fires on the very next maintainDeepgram() call.
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
        int bytesRead = mic_stream.readBytes((uint8_t*)s_rawBuf, sizeof(s_rawBuf));
        // FIX 7: Mono frame = 4 bytes (32-bit). Previously divided by 8
        // (stereo frame). Access sample directly at s_rawBuf[i] — no stride.
        int frames = bytesRead / 4;
        if (frames > 0) {
            for (int i = 0; i < frames; i++)
                s_pcmBuf[i] = inmp441Sample(s_rawBuf[i]);
            dgWs.sendBIN((uint8_t*)s_pcmBuf, frames * 2);
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
static int16_t       ZZZ_OY[3] = { 0, 0, 0 };  // set in initZzz() using faceCY

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
        // No canvas — clear the face region directly
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

    drawOneEyeSprite(lex, ey, blinkFrac, squint, sx, sy);
    drawOneEyeSprite(rex, ey, blinkFrac, squint, sx, sy);

    // FIX 1: FS_SURPRISED now uses drawMouthSprite (open mouth) instead of
    // drawSmileSprite (closed smile), matching the intended surprised expression.
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

    bool canBlink = (face.state == FS_IDLE || face.state == FS_TALKING || face.state == FS_HAPPY);
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

// FIX 1: setFaceIdle / setFaceTalk already existed; added setFaceListen and
// setFaceSurprised so all six non-sleep FaceState values are reachable.
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
// FIX 1: Previously unreachable — now called whenever Deepgram resumes streaming.
void setFaceListen() {
    face.state = FS_LISTEN; face.listenPulse = 0.f; face.thinkSq = 0.f;
    face.mOpen = 0.f; face.emotionTimer = 0; faceRedraw = true;
}
// FIX 1: Previously unreachable — now called in exitStandby() for a brief
// startled reaction before transitioning back to idle via emotionTimer.
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

// 128x128 canvas for the animated logo area only (~32KB PSRAM).
// Background, text, and bar are drawn directly on the tft.
static GFXcanvas16* bootLogo = nullptr;
#define BLOGO_SZ  128
#define BLOGO_X   ((W - BLOGO_SZ) / 2)   // = 96
#define BLOGO_Y   14

static inline void blitLogo() {
    if (bootLogo)
        tft.drawRGBBitmap(BLOGO_X, BLOGO_Y, bootLogo->getBuffer(), BLOGO_SZ, BLOGO_SZ);
}

// Dim a 565 color by factor/8 (0=black, 8=full brightness)
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

// Scatter stars on the tft background (called once per screen)
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

// ── Draw letter B into bootLogo canvas ───────────────────────────────────
// sc 1-8: scale (8 = full size filling ~80px height in 128px canvas)
static void logoDrawB(int sc) {
    if (!bootLogo || sc <= 0) return;
    bootLogo->fillScreen(C_BK);

    int bh   = sc * 10;          // height (80 at sc=8)
    int bsw  = sc * 3 / 2;       // stem width (12 at sc=8)
    int bmpW = sc * 6;            // bump width (48 at sc=8)
    int bmpH = bh / 2;            // half-height per bump (40)
    int br   = max(2, sc);        // corner radius (8 at sc=8)
    int bx   = BLOGO_SZ/2 - bmpW/2; // center on total B width (40 at sc=8)
    int by   = BLOGO_SZ/2 - bh/2;   // top of letter (24)

    // Glow rings (only at larger scales to avoid clutter)
    if (sc >= 5) {
        bootLogo->drawCircle(64, 64, 52, C_DCY);
        bootLogo->drawCircle(64, 64, 54, dimCol(C_CY, 3));
        bootLogo->drawCircle(64, 64, 56, dimCol(C_CY, 1));
    }

    // Vertical stem
    bootLogo->fillRect(bx, by, bsw, bh, C_CY);
    // Top bump
    bootLogo->fillRoundRect(bx, by, bmpW, bmpH + br/2, br, C_CY);
    if (bmpW - bsw - br > 2 && bmpH - br*2 > 2)
        bootLogo->fillRoundRect(bx + bsw, by + br,
                                bmpW - bsw - br, bmpH - br + br/2 - br, max(1, br - 2), C_BK);
    // Bottom bump (mint accent)
    bootLogo->fillRoundRect(bx, by + bmpH, bmpW + sc/2, bmpH, br, C_MINT);
    if (bmpW - bsw - br > 2 && bmpH - br*2 > 2)
        bootLogo->fillRoundRect(bx + bsw, by + bmpH + br,
                                bmpW - bsw - br + sc/2, bmpH - br*2, max(1, br - 2), C_BK);
}

// ── Draw robot face into bootLogo canvas ─────────────────────────────────
// morph 0 = B-bounding layout, morph 20 = full robot face
static void logoDrawRobot(int morph) {
    if (!bootLogo) return;
    bootLogo->fillScreen(C_BK);
    int m = min(max(morph, 0), 20);

    // Glow rings always present
    bootLogo->drawCircle(64, 64, 52, C_DCY);
    bootLogo->drawCircle(64, 64, 54, dimCol(C_CY, 3));
    bootLogo->drawCircle(64, 64, 56, dimCol(C_CY, 1));

    // Face box: lerps from B bounding box to robot face
    int fx0 = lerpi(58, 20, m, 20);
    int fy0 = lerpi(24, 22, m, 20);
    int fx1 = lerpi(104, 108, m, 20);
    int fy1 = lerpi(104, 96, m, 20);
    int fw  = fx1 - fx0, fh = fy1 - fy0;
    bootLogo->fillRoundRect(fx0, fy0, fw, fh, 8, 0x0412);
    bootLogo->drawRoundRect(fx0, fy0, fw, fh, 8, C_CY);
    bootLogo->drawRoundRect(fx0 + 1, fy0 + 1, fw - 2, fh - 2, 7, dimCol(C_CY, 4));

    // Left eye: B top bump (x=58..106, y=24..68) → robot left eye (x=28..56, y=38..58)
    int lx0 = lerpi(58, 28, m, 20);
    int ly0 = lerpi(24, 38, m, 20);
    int lew = lerpi(48, 28, m, 20);
    int leh = lerpi(44, 20, m, 20);
    int ler = lerpi(8,   4, m, 20);
    bootLogo->fillRoundRect(lx0, ly0, lew, leh, ler, C_WH);

    // Right eye: B bottom bump (x=58..108, y=64..104) → robot right eye (x=72..100, y=38..58)
    int rx0 = lerpi(58, 72, m, 20);
    int ry0 = lerpi(64, 38, m, 20);
    int rew = lerpi(50, 28, m, 20);
    int reh = lerpi(40, 20, m, 20);
    int rer = lerpi(8,   4, m, 20);
    bootLogo->fillRoundRect(rx0, ry0, rew, reh, rer, C_WH);

    // Pupils: appear in second half of transform
    if (m > 10) {
        int pa  = m - 10;
        int pr  = lerpi(0, 5, pa, 10);
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
        // Inner eye glow ring
        if (pa > 5) {
            bootLogo->drawCircle(lcx, lcy, pr + 2, dimCol(C_CY, 3));
            bootLogo->drawCircle(rcx, rcy, pr + 2, dimCol(C_CY, 3));
        }
    }

    // Stem: visible early, dissolves as face box takes over
    if (m < 10) {
        int sw = lerpi(12, 0, m, 10);
        if (sw > 0) {
            uint16_t sc = dimCol(C_CY, lerpi(8, 1, m, 10));
            int sx = 64 - sw / 2;
            bootLogo->fillRect(sx, lerpi(24, 22, m, 10), sw, lerpi(80, 74, m, 10), sc);
        }
    }

    // Antenna: shoots up from face top after m > 6
    if (m > 6) {
        int aa   = m - 6;
        int atip = lerpi(fy0, fy0 - 16, aa, 14);
        int abal = lerpi(0, 4, aa, 14);
        bootLogo->drawFastVLine(64, atip, fy0 - atip, C_CY);
        if (abal > 0) {
            bootLogo->fillCircle(64, atip, abal, C_CY);
            if (abal >= 3) bootLogo->fillCircle(64, atip, 2, C_WH);
        }
    }

    // Mouth: slides in from below face after m > 14
    if (m > 14) {
        int ma   = m - 14;
        int mx_c = (fx0 + fx1) / 2;
        int mw   = lerpi(0, 32, ma, 6);
        int my   = lerpi(fy1 + 6, fy1 - 12, ma, 6);
        if (mw > 2) bootLogo->fillRoundRect(mx_c - mw / 2, my, mw, 6, 3, C_WH);
    }
}

// ── Progress bar (pure tft, no canvas) ───────────────────────────────────
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

// ── Initial static frame ─────────────────────────────────────────────────
void drawBootLogo() {
    tft.fillScreen(C_BK);
    drawBootStars();

    bootLogo = new GFXcanvas16(BLOGO_SZ, BLOGO_SZ);
    if (!bootLogo || !bootLogo->getBuffer()) {
        Serial.println("[Boot] Canvas alloc failed");
        bootLogo = nullptr;
    } else {
        Serial.println("[Boot] Canvas OK");
        logoDrawB(8);
        blitLogo();
    }
    drawBootBar(0);
}

// ── Animated intro: B transforms into robot face ─────────────────────────
void playBootIntroAnim(int /*cx*/, int /*cy*/) {
    if (!bootLogo) return;
    const int TY = BLOGO_Y + BLOGO_SZ + 4;   // y=146 — text area below logo

    // Phase 1: B scales up (14 frames, ~224ms)
    for (int f = 0; f < 14; f++) {
        int sc = lerpi(1, 8, f, 13);
        logoDrawB(sc);
        blitLogo();
        delay(16); yield();
    }
    // Hold 1 frame at full B
    logoDrawB(8); blitLogo();

    // Phase 2: B morphs into robot face (22 frames, ~352ms)
    for (int f = 0; f < 22; f++) {
        logoDrawRobot(f * 20 / 21);
        blitLogo();
        delay(16); yield();
    }
    logoDrawRobot(20); blitLogo();  // final settled frame

    // Phase 4: Glow pulse (10 frames, ~200ms)
    for (int f = 0; f < 10; f++) {
        logoDrawRobot(20);
        // Extra ring pulse expands outward within canvas
        int r = 52 + f * 3;
        if (r < 70) bootLogo->drawCircle(64, 64, r, dimCol(C_CY, max(1, 7 - f)));
        blitLogo();
        delay(20); yield();
    }

    // Phase 5: "BRONNY AI" text slides up + credit line (18 frames, ~288ms)
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
            const char* credit = "by Patrick Perez  v1.1";
            tft.setTextSize(1); tft.setTextColor(dimCol(C_LG, min((f - 12) * 2, 8)));
            tft.setCursor(W / 2 - (int)strlen(credit) * 3, ty + 26);
            tft.print(credit);
        }
        delay(16); yield();
    }
}

// ============================================================
// WIFI SCREEN  (direct tft only, no canvas, v3.2 style)
// ============================================================
void drawWifiScreen() {
    tft.fillScreen(C_BK);

    // Scatter stars on background
    for (int i = 0; i < 70; i++) {
        int x = (i * 137 + 31) % W;
        int y = (i * 93  + 17) % (H - 28) + 5;
        uint16_t col = (i % 5 == 0) ? C_WH :
                       (i % 4 == 0) ? C_CY :
                       (i % 3 == 0) ? C_DCY : C_DG;
        tft.drawPixel(x, y, col);
    }

    // Header bar
    tft.fillRect(0, 0, W, 30, 0x18C3);
    tft.drawFastHLine(0, 0,  W, C_CY);
    tft.drawFastHLine(0, 30, W, C_DCY);
    // Brand dot
    tft.fillCircle(14, 15, 5, C_CY);
    tft.fillCircle(14, 15, 2, C_BK);
    // Name + version
    tft.setTextColor(C_WH); tft.setTextSize(1); tft.setCursor(25, 10); tft.print("BRONNY AI");
    tft.setTextColor(C_CY);                     tft.setCursor(82, 10); tft.print("v1.1");
    // Right badge
    tft.fillRoundRect(W - 88, 6, 84, 18, 4, C_DCY);
    tft.drawRoundRect(W - 88, 6, 84, 18, 4, C_CY);
    tft.setTextColor(C_CY); tft.setCursor(W - 82, 12); tft.print("NETWORK SETUP");

    // WiFi icon — concentric arcs, bottom half clipped
    int ix = W / 2, iy = 78;
    tft.drawCircle(ix, iy + 20, 36, C_DG);
    tft.drawCircle(ix, iy + 20, 26, C_DCY);
    tft.drawCircle(ix, iy + 20, 16, C_CY);
    tft.fillCircle(ix, iy + 20,  6, C_CY);
    tft.fillRect(ix - 40, iy + 20, 80, 44, C_BK);  // clip lower half

    // SSID card
    tft.fillRoundRect(16, 130, W - 32, 26, 5, 0x18C3);
    tft.drawRoundRect(16, 130, W - 32, 26, 5, C_CY);
    tft.setTextColor(C_DCY); tft.setTextSize(1); tft.setCursor(28, 137); tft.print("Network:");
    tft.setTextColor(C_WH);                      tft.setCursor(86, 137); tft.print(WIFI_SSID);

    // Footer bar
    tft.fillRect(0, H - 22, W, 22, 0x18C3);
    tft.drawFastHLine(0, H - 22, W, C_DCY);
    tft.setTextColor(C_DG);  tft.setTextSize(1); tft.setCursor(6, H - 14);       tft.print("ESP32-S3");
    tft.setTextColor(C_DCY);                     tft.setCursor(W/2-48, H - 14);  tft.print("Bronny AI v1.1");
    tft.setTextColor(C_DG);                      tft.setCursor(W - 72, H - 14);  tft.print("Patrick 2026");
}

void drawWifiStatus(const char* l1, uint16_t c1, const char* l2 = "", uint16_t c2 = C_CY) {
    // Status area between SSID card (y=156) and footer (y=H-22=218)
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
    // Spinner sits inside the blank area below the WiFi arcs (y≈115)
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
    tftLog(C_DCY, "Standby — say 'Hi Bronny'");
    Serial.println("[Standby] Entering standby mode");
}

void exitStandby() {
    standbyMode   = false;
    lastRailwayMs = millis();
    // FIX 1: Show a brief surprised expression on wake-up before settling
    // into listen state. emotionTimer auto-transitions to FS_IDLE after 600ms.
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
    // FIX 1: Transition to FS_LISTEN now that Deepgram is streaming again.
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

    int logCmd = checkLogCommand(transcript);
    if (logCmd != 0) {
        setLogsVisible(logCmd == 1);
        dgStreaming = true; dgLastKeepalive = millis();
        setFaceListen();
        busy = false; return;
    }

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

    if (isNoise(transcript)) {
        tftLogf(C_YL, "Filtered: '%s'", transcript.c_str());
        dgStreaming = true; dgLastKeepalive = millis();
        setStatus("Listening...", C_CY);
        busy = false; return;
    }

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
    // FIX 1: Transition to FS_LISTEN now that Deepgram is streaming again.
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

    tftSPI.begin(PIN_CLK, -1, PIN_MOSI, PIN_CS);
    tft.init(240, 320); tft.setRotation(3); tft.fillScreen(C_BK);

    faceCanvas = new GFXcanvas16(W, LOG_Y);
    if (!faceCanvas || !faceCanvas->getBuffer()) {
        Serial.println("[Sprite] FATAL: canvas alloc failed. Check PSRAM board settings.");
        faceCanvas = nullptr;
    } else {
        faceCanvas->fillScreen(C_BK);
        Serial.println("[Sprite] Canvas OK");
    }

    audioRestart(); i2s.setVolume(VOL_MAIN);
    if (audioOk) { auto sc = sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }

    micInit();
    logsVisible = true;

    drawBootLogo();
    playBootIntroAnim(W / 2, H / 2 - 32);
    drawBootBar(10); jingleBoot(); drawBootBar(55); delay(150); drawBootBar(100); delay(300);

    // Free boot canvas — no longer needed, reclaim PSRAM for face animation
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

    // Deepgram connection screen — shown before switching to main face screen
    drawDgScreen();
    connectDeepgram();
    dgStreaming = true;

    // Transition to main face screen with logs hidden (default)
    // setLogsVisible(false) repositions face to centered before drawing
    tft.fillScreen(C_BK);
    setLogsVisible(false);   // sets faceCY=80, faceBlitY=40, draws face centered
    jingleReady();

    tftLog(C_GR, "Bronny AI v1.1 ready");
    tftLogf(C_CY, "WiFi: %s", WiFi.localIP().toString().c_str());
    tftLogf(C_LG, "Heap %uK  PSRAM %uK",
            esp_get_free_heap_size() / 1024,
            heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024);

    setFaceListen();
    setStatus("Listening...", C_CY);

    bootReadyAt = millis() + 2000;
}

// ============================================================
// LOOP
// ============================================================
void loop() {
    uint32_t now = millis();

    animFace();
    if (faceRedraw) { drawFace(false); faceRedraw = false; }

    if (now - lastHbMs > HEARTBEAT_MS) { lastHbMs = now; sendHeartbeat(); }

    maintainDeepgram();

    // FIX 2: Gate boot intro on bootReadyAt to ensure codec is stable.
    if (!bootIntroDone && !busy && dgConnected && now > bootReadyAt) {
        bootIntroDone = true;
        doBootIntro(); return;
    }

    if (!standbyMode && !busy && lastRailwayMs > 0 && now - lastRailwayMs > STANDBY_TIMEOUT_MS)
        enterStandby();

    // FIX 4: Set dgFinalReceivedAt = 0 in the timeout path so that the
    // speech_final callback (which also checks pendingTranscript == false)
    // cannot double-fire for the same utterance.
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
    }

    yield();
}
