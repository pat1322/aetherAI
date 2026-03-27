/*
 * BRONNY AI v5.9 — Sprite Edition
 * by Patrick Perez
 *
 * v5.9 changes vs v5.8:
 *
 *   [BUG FIX 1 — Face stuck in FS_TALKING after audio ends]
 *     stopTalk() now calls drawFace() DIRECTLY (bypassing the 16ms animFace
 *     guard) immediately after decoded.end(), ensuring the canvas is cleared
 *     and blit to the TFT the instant speech ends.
 *
 *   [BUG FIX 2 — Boot intro permanently disabled]
 *     bootIntroDone left as true intentionally — setting to false causes
 *     a crash/reboot on first TTS call. Boot intro is disabled by design.
 *
 *   [BUG FIX 3 — Animation freezes during mode-switch cleanup]
 *     animFace()/drawFace() calls added after decoded.end(), after
 *     audioInitRec(), and after micInit().
 *
 *   [BUG FIX 4 — Silence primer blocks with no yield]
 *     The bare delay(180) after the silence primer write replaced with an
 *     animated yield loop.
 *
 *   [BUG FIX 5 — Retry won't fire after partial stream]
 *     Replaced with MIN_AUDIO_BYTES (1 KB) threshold.
 *
 *   [BUG FIX 6 — Fragile dual-guard in maintainDeepgram]
 *     dgStreaming explicitly set false before callRailwayStream.
 *
 *   [BUG FIX 7 — Wake-word check after noise filter in standby]
 *     runConversation() checks wake word BEFORE isNoise().
 *
 *   [BUG FIX 8 — Face frozen during VAD cooldown drain]
 *     animFace()/drawFace() added inside post-TTS mic drain loop.
 *
 *   [BUG FIX 9 — Status never returns to Listening after speaking]
 *     setStatus("Listening...", C_CY) called before returning from
 *     callRailwayStream().
 *
 *   [BUG FIX 10 — Robot stays in speaking state after audio ends]
 *     Data-gap timeout: if no bytes arrive for STREAM_DATA_GAP_MS (300ms)
 *     after audio starts, the stream loop breaks immediately so stopTalk()
 *     fires as soon as the server finishes sending, not when TCP closes.
 *
 *   [FEATURE — Log / status bar visibility toggle]
 *     Say "hide logs"    -> hides the log zone and status bar (face only).
 *     Say "show logs"    -> restores logs and status bar.
 *     "display logs" and "hide the logs" are also accepted.
 *     Default on boot: logs HIDDEN (face-only mode).
 *     All log/footer buffers are always kept up-to-date regardless of
 *     visibility, so "show logs" instantly redraws correct content.
 *     Bonus: press 'l' in the Serial Monitor to toggle logs manually.
 *
 *   [SPRITE ANIMATION ENGINE]
 *     GFXcanvas16* faceCanvas (W x LOG_Y) replaces direct-to-TFT drawing.
 *     Memory: 320 x 160 x 2 = 100 KB — routed to OPI PSRAM automatically.
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

#define VOL_MAIN              0.50f
#define VOL_JINGLE            0.25f
#define TTS_COOLDOWN_MS        800
#define HEARTBEAT_MS          30000

// Deepgram
#define DG_KEEPALIVE_MS       8000
#define DG_RECONNECT_MS       3000
#define DG_CONNECT_TIMEOUT    8000
#define DG_FINAL_TIMEOUT_MS    700

// Standby: 3 minutes idle
#define STANDBY_TIMEOUT_MS   180000UL

// Stream chunk size for HTTP -> decoder pipe
#define STREAM_CHUNK_BYTES     512

// BUG FIX 5: minimum bytes for a stream to count as successful.
#define MIN_AUDIO_BYTES       1024

// BUG FIX 10: if no new bytes arrive for this long after audio starts,
// the server is done — break the stream loop immediately.
#define STREAM_DATA_GAP_MS     300

static uint32_t lastHbMs          = 0;
static uint32_t vadCooldownUntil  = 0;
static uint32_t lastRailwayMs     = 0;
static bool     standbyMode       = false;

// BUG FIX 2: kept as true intentionally.
static bool     bootIntroDone     = true;

// ============================================================
// LOG VISIBILITY
// false = face-only mode (default on boot).
// Voice commands "hide logs" / "show logs" toggle this at runtime.
// Serial Monitor: press 'l' to toggle manually.
// ============================================================
static bool logsVisible = false;

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

#define LOG_Y        160
#define LOG_LINE_H    14
#define LOG_LINES      4
#define LOG_FOOTER_Y (H - 14)

#define FCX    160
#define FCY     72
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
static String   gFooterText  = "v5.9 Ready";
static uint16_t gFooterColor = C_CY;

static inline void blitFace() {
    if (faceCanvas) {
        tft.drawRGBBitmap(0, 0, faceCanvas->getBuffer(), W, LOG_Y);
    }
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
    cfg.sample_rate=16000; cfg.channels=2; cfg.bits_per_sample=32;
    cfg.i2s_format=I2S_STD_FORMAT; cfg.port_no=1;
    cfg.pin_ws=PIN_MIC_WS; cfg.pin_bck=PIN_MIC_SCK; cfg.pin_data=PIN_MIC_SD;
    cfg.pin_mck=-1; cfg.use_apll=false;
    micOk = mic_stream.begin(cfg);
    if (micOk) {
        uint8_t tmp[512]; uint32_t e=millis()+300;
        while(millis()<e){ mic_stream.readBytes(tmp,sizeof(tmp)); yield(); }
    }
}

void audioInitRec() {
    if (inTtsMode || !audioOk) {
        i2s.end(); delay(100);
        audioPinsSetup();
        auto cfg=i2s.defaultConfig(TX_MODE); cfg.copyFrom(ainf_rec);
        cfg.output_device=DAC_OUTPUT_ALL;
        audioOk=i2s.begin(cfg); i2s.setVolume(VOL_MAIN);
        if (audioOk){ auto sc=sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }
        inTtsMode=false;
    }
}

void audioInitTTS() {
    if (!inTtsMode) {
        i2s.end(); delay(100);
        audioPinsSetup();
        auto cfg=i2s.defaultConfig(TX_MODE); cfg.copyFrom(ainf_tts);
        cfg.output_device=DAC_OUTPUT_ALL;
        audioOk=i2s.begin(cfg); i2s.setVolume(VOL_MAIN);
        inTtsMode=true;
    }
}

void audioRestart() {
    i2s.end(); delay(150); audioOk=false; inTtsMode=false;
    Wire.end(); delay(60);
    audioPinsSet = false;
    audioPinsSetup();
    auto cfg=i2s.defaultConfig(TX_MODE); cfg.copyFrom(ainf_rec);
    cfg.output_device=DAC_OUTPUT_ALL;
    audioOk=i2s.begin(cfg); i2s.setVolume(VOL_MAIN);
    if(audioOk){ auto sc=sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }
}

void playTone(float hz, int ms) {
    if(!audioOk){ delay(ms); return; }
    sineGen.setFrequency(hz);
    uint32_t e=millis()+ms;
    while(millis()<e){ sineCopy.copy(); yield(); }
}
void playSil(int ms){ playTone(0,ms); }

void jingleBoot(){
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    float n[]={523,659,784,1047,1319,1568,2093}; int d[]={100,100,100,140,260,80,280};
    for(int i=0;i<7;i++){ playTone(n[i],d[i]); playSil(20); }
    i2s.setVolume(VOL_MAIN);
}
void jingleConnect(){
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(880,100); playSil(25); playTone(1108,100); playSil(25);
    playTone(1318,200); playSil(150); i2s.setVolume(VOL_MAIN);
}
void jingleError(){
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(300,200); playSil(80); playTone(220,350); playSil(200);
    i2s.setVolume(VOL_MAIN);
}
void jingleReady(){
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(880,80); playSil(30); playTone(1318,80); playSil(30);
    playTone(1760,200); playSil(150); i2s.setVolume(VOL_MAIN);
}
void jingleWake(){
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(660,80); playSil(20); playTone(1100,120); playSil(80);
    i2s.setVolume(VOL_MAIN);
}

// ============================================================
// JSON ESCAPE
// ============================================================
String jEsc(const String& s) {
    String o; o.reserve(s.length()+16);
    for (int i=0; i<(int)s.length(); i++) {
        unsigned char c=(unsigned char)s[i];
        if(c=='"') o+="\\\""; else if(c=='\\') o+="\\\\";
        else if(c=='\n') o+="\\n"; else if(c=='\r') o+="\\r";
        else if(c=='\t') o+="\\t"; else if(c>=0x20) o+=(char)c;
    }
    return o;
}

// ============================================================
// WIFI HELPER
// ============================================================
static void ensureWifi() {
    if (WiFi.status()!=WL_CONNECTED) {
        WiFi.reconnect(); uint32_t t=millis();
        while(WiFi.status()!=WL_CONNECTED && millis()-t<8000){ delay(300); yield(); }
    }
}

static String baseUrl() {
    String u=String(AETHER_URL);
    while(u.endsWith("/")) u.remove(u.length()-1);
    return u;
}

// ============================================================
// HEARTBEAT
// ============================================================
void sendHeartbeat() {
    if(WiFi.status()!=WL_CONNECTED) return;
    maintainDeepgram();
    WiFiClientSecure cli; cli.setInsecure(); cli.setConnectionTimeout(8000);
    HTTPClient http;
    http.begin(cli, baseUrl()+"/bronny/heartbeat");
    http.setTimeout(8000);
    http.addHeader("Content-Type","application/json");
    int code=http.POST("{\"device\":\"bronny\",\"version\":\"5.9\"}");
    http.end();
    maintainDeepgram();
    if(code!=200) Serial.printf("[HB] fail %d\n",code);
    else          Serial.println("[HB] OK");
}

// ============================================================
// FORCE FACE REDRAW HELPER
// ============================================================
static inline void forceDrawFace() {
    faceRedraw = true;
    drawFace(false);
    faceRedraw = false;
}

// ============================================================
// TFT LOG ZONE
//
// All draw calls are gated by logsVisible.
// Buffer state is always updated so "show logs" repaints correctly.
// ============================================================
static void logRedraw() {
    if (!logsVisible) return;
    tft.fillRect(0, LOG_Y, W, LOG_LINES*LOG_LINE_H+2, C_BK);
    int total = min(gLogCount, LOG_LINES);
    for (int i = 0; i < total; i++) {
        int slot = (gLogHead+i) % LOG_LINES;
        uint16_t c = gLogCol[slot];
        if (i < total-2) {
            uint16_t r=((c>>11)&0x1F)>>1;
            uint16_t g=((c>>5)&0x3F)>>1;
            uint16_t b=(c&0x1F)>>1;
            c=(r<<11)|(g<<5)|b;
        }
        tft.setTextColor(c); tft.setTextSize(1);
        tft.setCursor(2, LOG_Y+1+i*LOG_LINE_H);
        tft.print(gLog[slot]);
    }
}

static void logDrawFooter() {
    if (!logsVisible) return;
    tft.fillRect(0, LOG_FOOTER_Y, W, 14, C_BK);
    tft.drawFastHLine(0, LOG_FOOTER_Y-1, W, 0x1082);
    tft.setTextColor(gFooterColor); tft.setTextSize(1);
    int tw = (int)gFooterText.length()*6;
    tft.setCursor(W/2-tw/2, LOG_FOOTER_Y+3);
    tft.print(gFooterText);
}

void tftLog(uint16_t col, const char* msg) {
    // Always store in circular buffer regardless of visibility.
    String s = String(msg);
    if ((int)s.length() > 53) s = s.substring(0, 53);
    if (gLogCount < LOG_LINES) {
        gLog[gLogCount] = s; gLogCol[gLogCount] = col; gLogCount++;
        if (logsVisible) {
            tft.fillRect(0, LOG_Y+(gLogCount-1)*LOG_LINE_H, W, LOG_LINE_H, C_BK);
            tft.setTextColor(col); tft.setTextSize(1);
            tft.setCursor(2, LOG_Y+(gLogCount-1)*LOG_LINE_H+1);
            tft.print(s);
        }
    } else {
        gLog[gLogHead] = s; gLogCol[gLogHead] = col;
        gLogHead = (gLogHead+1) % LOG_LINES;
        logRedraw();
    }
}

void tftLogf(uint16_t col, const char* fmt, ...) {
    char buf[80]; va_list ap; va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
    tftLog(col, buf);
}

void setStatus(const char* s, uint16_t c) {
    // Always update state; only paint when visible.
    gFooterText = String(s); gFooterColor = c;
    logDrawFooter();
}

void setFooterOnly(uint16_t c, const char* s) {
    char buf[54]; strncpy(buf, s, 53); buf[53] = '\0';
    gFooterText = String(buf); gFooterColor = c;
    logDrawFooter();
}

// ============================================================
// LOG VISIBILITY CONTROL
//
// setLogsVisible(true)  — paint full log buffer + footer onto TFT.
// setLogsVisible(false) — black-fill the log+footer zone.
// ============================================================
void setLogsVisible(bool visible) {
    logsVisible = visible;
    if (visible) {
        logRedraw();
        logDrawFooter();
        Serial.println("[UI] Logs shown");
    } else {
        tft.fillRect(0, LOG_Y, W, H - LOG_Y, C_BK);
        Serial.println("[UI] Logs hidden");
    }
}

// ============================================================
// LOG COMMAND DETECTOR
//
// Returns  1 = "show logs" command
//         -1 = "hide logs" command
//          0 = not a log command
// ============================================================
static int checkLogCommand(const String& transcript) {
    String s = transcript; s.trim(); s.toLowerCase();

    // Hide triggers
    if (s.indexOf("hide log")     >= 0) return -1;
    if (s.indexOf("hide the log") >= 0) return -1;
    if (s.indexOf("remove log")   >= 0) return -1;
    if (s.indexOf("clear log")    >= 0) return -1;

    // Show triggers
    if (s.indexOf("show log")     >= 0) return 1;
    if (s.indexOf("display log")  >= 0) return 1;
    if (s.indexOf("show the log") >= 0) return 1;

    return 0;
}

// ============================================================
// STREAMING RAILWAY CALL
// ============================================================
bool callRailwayStream(const String& transcript) {
    if (transcript.isEmpty()) return false;
    ensureWifi();
    if (WiFi.status() != WL_CONNECTED) return false;

    String body = "{\"text\":\"" + jEsc(transcript) + "\"}";
    String url  = baseUrl() + "/voice/text";
    Serial.printf("[Rail] POST text='%s'\n", transcript.c_str());

    bool gotAudio = false;

    for (int attempt = 1; attempt <= 2; attempt++) {
        if (attempt > 1) {
            Serial.println("[Rail] retry...");
            delay(2000);
        }

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

        if (code != 200) {
            http.end();
            continue;
        }

        mic_stream.end();
        micOk = false;
        delay(60);

        audioInitTTS();
        delay(80);

        // BUG FIX 4: animated silence primer
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
        int  contentLen     = http.getSize();
        size_t totalRead    = 0;
        uint32_t deadline   = millis() + 35000;
        bool talkStarted    = false;
        uint8_t buf[STREAM_CHUNK_BYTES];

        // BUG FIX 10: data-gap timeout
        uint32_t lastDataMs = 0;

        while (millis() < deadline) {

            size_t avail = (size_t)stream->available();
            if (avail > 0) {
                size_t toRead = min(avail, (size_t)sizeof(buf));
                size_t got    = stream->readBytes(buf, toRead);

                if (got > 0) {
                    if (!talkStarted) {
                        setStatus("Speaking...", C_GR);
                        startTalk();
                        talkStarted = true;
                    }
                    decoded.write(buf, got);
                    totalRead += got;
                    lastDataMs = millis();
                }
            } else {
                delay(2);
            }

            // BUG FIX 10: exit immediately when server stops sending
            if (talkStarted && lastDataMs > 0
                    && millis() - lastDataMs > STREAM_DATA_GAP_MS) {
                Serial.printf("[Rail] Data gap %ums — stream done\n",
                              (unsigned)(millis() - lastDataMs));
                break;
            }

            if (!http.connected() && stream->available() == 0) break;
            if (contentLen > 0 && (int)totalRead >= contentLen)   break;

            maintainDeepgram();
            animFace();
            if (faceRedraw) { drawFace(false); faceRedraw = false; }
            yield();
        }

        // BUG FIX 1: instant face clear
        stopTalk();
        forceDrawFace();

        // BUG FIX 9: footer before blocking cleanup
        setStatus("Listening...", C_CY);

        decoded.end();

        { int16_t sil[128] = {}; i2s.write((uint8_t*)sil, sizeof(sil)); }

        // BUG FIX 3
        animFace();
        if (faceRedraw) { drawFace(false); faceRedraw = false; }

        http.end();
        Serial.printf("[Rail] Stream complete: %u bytes\n", (unsigned)totalRead);

        if (totalRead >= MIN_AUDIO_BYTES) {
            gotAudio = true;
            break;
        }
        Serial.printf("[Rail] Attempt %d incomplete (%u bytes < %u), retrying\n",
                      attempt, (unsigned)totalRead, (unsigned)MIN_AUDIO_BYTES);
    }

    audioInitRec();

    // BUG FIX 3
    animFace();
    if (faceRedraw) { drawFace(false); faceRedraw = false; }

    micInit();

    // BUG FIX 3
    animFace();
    if (faceRedraw) { drawFace(false); faceRedraw = false; }

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
// DEEPGRAM PERSISTENT STREAMING ASR
// ============================================================
WebSocketsClient dgWs;

static bool     dgConnected          = false;
static bool     dgStreaming           = false;
static uint32_t dgLastKeepalive       = 0;
static uint32_t dgLastConnectAttempt  = 0;

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
    "&model=nova-2"
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
        tftLogf(C_RD, "DG err: %.40s", desc);
    }
}

void onDgWsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            dgConnected     = true;
            dgLastKeepalive = millis();
            if (!busy) dgStreaming = true;
            tftLog(C_GR, "Deepgram: connected");
            Serial.println("[DG] WS connected");
            break;
        case WStype_TEXT:
            parseDgMsg((const char*)payload, length);
            break;
        case WStype_DISCONNECTED:
            dgConnected = false;
            dgStreaming  = false;
            tftLog(C_WARN, "Deepgram: disconnected, reconnecting...");
            Serial.println("[DG] WS disconnected");
            break;
        case WStype_ERROR:
            dgConnected = false;
            dgStreaming  = false;
            tftLog(C_RD, "Deepgram: WS error");
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
    tftLog(C_CY, "Deepgram: connecting...");
    uint32_t deadline = millis() + DG_CONNECT_TIMEOUT;
    while (!dgConnected && millis() < deadline) { dgWs.loop(); yield(); }
    if (dgConnected) tftLog(C_GR, "Deepgram: ready");
    else             tftLog(C_YL, "Deepgram: connect timeout (will retry)");
}

static int32_t s_rawBuf[1600 * 2];
static int16_t s_pcmBuf[1600];

void maintainDeepgram() {
    uint32_t now = millis();
    dgWs.loop();

    if (!dgConnected && now - dgLastConnectAttempt > DG_RECONNECT_MS) {
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
        int frames    = bytesRead / 8;
        if (frames > 0) {
            for (int i = 0; i < frames; i++)
                s_pcmBuf[i] = inmp441Sample(s_rawBuf[i * 2]);
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
    String s=t; s.trim();
    if(s.length()<3) return true;
    static const char* nw[]={
        "...","..",".", "ah","uh","hm","hmm","mm","um","huh",
        "oh","ow","beep","boop","ding","dong","ping","ring",
        "the","a","i",nullptr
    };
    String lower=s; lower.toLowerCase();
    for(int i=0;nw[i];i++) if(lower==String(nw[i])) return true;
    int spaceIdx=lower.indexOf(' ');
    if(spaceIdx>0){
        String fw=lower.substring(0,spaceIdx);
        bool allSame=true; int wi=0;
        while(wi<(int)lower.length()){
            int sp=lower.indexOf(' ',wi);
            String w=(sp<0)?lower.substring(wi):lower.substring(wi,sp);
            w.trim();
            if(w.length()>0 && w!=fw){ allSame=false; break; }
            wi=(sp<0)?lower.length():sp+1;
        }
        if(allSame && fw.length()<=6) return true;
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
// ZZZ SLEEP ANIMATION — sprite version
// ============================================================
struct ZzzParticle {
    int16_t x, y;
    uint8_t ph;
    uint8_t sz;
};
static ZzzParticle s_zzz[3];
static const int16_t ZZZ_OX[3] = { FCX+22, FCX+30, FCX+20 };
static const int16_t ZZZ_OY[3] = { FCY+22, FCY+10, FCY-4  };

static void initZzz() {
    s_zzz[0] = { ZZZ_OX[0], ZZZ_OY[0], 0,   1 };
    s_zzz[1] = { ZZZ_OX[1], ZZZ_OY[1], 85,  1 };
    s_zzz[2] = { ZZZ_OX[2], ZZZ_OY[2], 170, 2 };
}

static void clearZzz() {}

static void updateZzz() {
    for (int i = 0; i < 3; i++) {
        s_zzz[i].ph++;
        int16_t nx = ZZZ_OX[i] + (int16_t)((uint16_t)s_zzz[i].ph * 18 / 255);
        int16_t ny = ZZZ_OY[i] - (int16_t)((uint16_t)s_zzz[i].ph * 55 / 255);
        s_zzz[i].x = constrain(nx, 0, W-12);
        s_zzz[i].y = constrain(ny, 4, LOG_Y-1);
    }
}

static void renderZzz() {
    if (!faceCanvas) return;
    for (int i = 0; i < 3; i++) {
        uint16_t col = (s_zzz[i].ph < 100) ? C_CY
                     : (s_zzz[i].ph < 190) ? C_DCY
                     :                        (uint16_t)0x0209;
        faceCanvas->setTextSize(s_zzz[i].sz);
        faceCanvas->setTextColor(col);
        faceCanvas->setCursor(s_zzz[i].x, s_zzz[i].y);
        faceCanvas->print("z");
    }
    faceCanvas->setTextSize(1);
}

// ============================================================
// SPRITE FACE DRAW HELPERS
// ============================================================
static void drawSmileSprite(int cx, int cy) {
    faceCanvas->fillCircle(cx, cy, SMILE_R, C_WH);
    faceCanvas->fillRect(cx-SMILE_R-1, cy-SMILE_R-1, (SMILE_R+1)*2+2, SMILE_R+2, C_BK);
    int innerR = SMILE_R - SMILE_TH;
    if (innerR > 1) {
        faceCanvas->fillCircle(cx, cy, innerR, C_BK);
        faceCanvas->fillRect(cx-innerR-1, cy-innerR-1, (innerR+1)*2+2, innerR+2, C_BK);
    }
}

static void drawOneEyeSprite(int cx, int cy, float openFrac, float squint,
                              float scaleX, float scaleY) {
    int ew = max(8, (int)(EW  * scaleX));
    int eh = max(2, (int)(EH  * openFrac * scaleY * (1.f - squint * 0.55f)));
    int r  = min(ER, min(ew/2, eh/2));
    faceCanvas->fillRoundRect(cx-ew/2, cy-eh/2, ew, eh, r, C_WH);
}

static void drawMouthSprite(int cx, int cy, float openFrac) {
    int mh = MH_CL + (int)((MH_OP - MH_CL) * openFrac);
    int r  = min(MR, mh/2);
    faceCanvas->fillRoundRect(cx-MW/2, cy-mh/2, MW, mh, r, C_WH);
}

void drawFaceBg() {
    if (faceCanvas) faceCanvas->fillScreen(C_BK);
    else            tft.fillRect(0, 0, W, LOG_Y, C_BK);
}

// ============================================================
// drawFace  — sprite edition
// ============================================================
void drawFace(bool /*full*/) {
    if (!faceCanvas) return;

    faceCanvas->fillScreen(C_BK);

    int by  = face.bobY;
    int lxi = (int)(face.lookX * LOOK_X_RANGE);
    int lyi = (int)(face.lookY * LOOK_Y_RANGE);
    int lex = FCX - ESEP/2 + lxi;
    int rex = FCX + ESEP/2 + lxi;
    int ey  = FCY + EYO + by + lyi;
    int my  = FCY + MYO + by;

    if (face.state == FS_SLEEP) {
        faceCanvas->fillRoundRect(lex-EW/2, ey-2, EW, 4, 2, C_WH);
        faceCanvas->fillRoundRect(rex-EW/2, ey-2, EW, 4, 2, C_WH);
        drawSmileSprite(FCX, my);
        renderZzz();
        blitFace();
        return;
    }

    float blinkFrac = 1.f;
    if (face.blink) {
        int bf  = face.blinkF;
        blinkFrac = (bf <= 4) ? (1.f - bf/4.f) : ((bf-4)/5.f);
        blinkFrac = constrain(blinkFrac, 0.f, 1.f);
    }
    float squint = (face.state == FS_THINK) ? face.thinkSq : 0.f;
    float sx = face.eyeScaleX;
    float sy = face.eyeScaleY;
    if (face.state == FS_SURPRISED) { sx = face.surpriseScale; sy = face.surpriseScale; }

    drawOneEyeSprite(lex, ey, blinkFrac, squint, sx, sy);
    drawOneEyeSprite(rex, ey, blinkFrac, squint, sx, sy);

    if (face.state == FS_TALKING) {
        drawMouthSprite(FCX, my, face.mOpen);
    } else {
        drawSmileSprite(FCX, my);
    }

    blitFace();
}

// ============================================================
// animFace  — state update only, no direct TFT writes
// ============================================================
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
        face.lookX += (0.55f  - face.lookX) * 0.06f;
        face.lookY += (-0.5f  - face.lookY) * 0.06f;
        ch = true;
    }

    bool canBlink = (face.state==FS_IDLE || face.state==FS_TALKING || face.state==FS_HAPPY);
    if (canBlink && !face.blink && now - lastBlink > nextBlink) {
        face.blink  = true;
        face.blinkF = 0;
        nextBlink   = 2200 + (uint32_t)random(2800);
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
        case FS_LISTEN: {
            face.listenPulse += 0.07f;
            if (face.listenPulse > 6.2832f) face.listenPulse -= 6.2832f;
            face.lookX += (0.0f  - face.lookX) * 0.05f;
            face.lookY += (-0.35f - face.lookY) * 0.05f;
            if (fabsf(face.lookY + 0.35f) > 0.01f) ch = true;
            break;
        }
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
            updateZzz();
            ch = true;
            break;
        default: break;
    }

    if (ch) faceRedraw = true;
}

void setFaceIdle()  {
    face.state=FS_IDLE; face.thinkSq=0.f; face.mOpen=0.f;
    face.surpriseScale=1.f; face.emotionTimer=0; faceRedraw=true;
}
void setFaceTalk()  {
    face.state=FS_TALKING; face.talkPh=0.f; face.thinkSq=0.f;
    face.emotionTimer=0; faceRedraw=true;
}
void setFaceThink() {
    face.state=FS_THINK; face.thinkSq=0.f; face.emotionTimer=0; faceRedraw=true;
}
void setFaceHappy(uint32_t ms=1800) {
    face.state=FS_HAPPY; face.happyPh=0.f; face.mOpen=0.f;
    face.emotionTimer=millis()+ms; faceRedraw=true;
}
void setFaceSleep() {
    face.state=FS_SLEEP; face.bobY=0; face.bobPh=0.f;
    face.lookX=0.f; face.lookY=0.f; face.mOpen=0.f;
    face.emotionTimer=0; faceRedraw=true;
    initZzz();
}
void startTalk() { setFaceTalk(); }
void stopTalk()  { setFaceIdle(); }

// ============================================================
// BOOT ANIM + WIFI SCREEN
// ============================================================
static void drawHexOutline(int cx,int cy,int r,uint16_t col){
    for(int i=0;i<6;i++){
        float a1=(i*60-30)*3.14159f/180.f, a2=((i+1)*60-30)*3.14159f/180.f;
        tft.drawLine(cx+(int)(r*cosf(a1)),cy+(int)(r*sinf(a1)),
                     cx+(int)(r*cosf(a2)),cy+(int)(r*sinf(a2)),col);
    }
}
static void drawBMonogram(int cx,int cy,bool big){
    int scale=big?1:0; int bx=cx-7-scale, by2=cy-16-scale;
    int bw=5+scale*2, bh=32+scale*2, bumpW=16+scale*2, bumpH1=14+scale, bumpH2=17+scale;
    uint16_t col=big?C_MINT:C_CY;
    tft.fillRect(bx,by2,bw,bh,col);
    tft.fillRoundRect(bx,by2,bumpW,bumpH1,5,col);
    tft.fillRoundRect(bx,by2+bh/2,bumpW+2,bumpH2,6,col);
    tft.fillRect(bx+2,by2+2,bumpW-6,bumpH1-4,C_BK);
    tft.fillRect(bx+2,by2+bh/2+2,bumpW-4,bumpH2-5,C_BK);
}
void playBootIntroAnim(int cx,int cy){
    int hexR[]={56,52,48}; uint16_t hexC[]={0x18C3,C_DCY,C_CY};
    for(int i=0;i<3;i++){ drawHexOutline(cx,cy,hexR[i],hexC[i]); delay(45); yield(); }
    for(int r=44;r>=2;r-=2){ uint16_t s=(r>30)?C_MID:(r>18)?C_BG:C_BK; drawHexOutline(cx,cy,r,s); delay(7); yield(); }
    for(int r=44;r>=2;r-=4) drawHexOutline(cx,cy,r,C_WH);
    delay(55); yield();
    for(int r=44;r>=2;r-=2){ uint16_t s=(r>30)?C_MID:(r>18)?C_BG:C_BK; drawHexOutline(cx,cy,r,s); }
    drawBMonogram(cx,cy,true); delay(60); yield();
    tft.fillRect(cx-32,cy-20,64,40,C_BK);
    for(int r=44;r>=2;r-=2){ uint16_t s=(r>30)?C_MID:(r>18)?C_BG:C_BK; drawHexOutline(cx,cy,r,s); }
    drawBMonogram(cx,cy,false); delay(80); yield();
    for(int dy=14;dy>=0;dy-=2){
        tft.fillRect(0,cy+56,W,22,C_BK); tft.setTextSize(2); tft.setTextColor(C_WH);
        tft.setCursor(cx-36,cy+60+dy); tft.print("BRONNY"); delay(22); yield();
    }
    tft.fillRoundRect(cx+42,cy+58,22,16,4,C_CY);
    tft.setTextSize(1); tft.setTextColor(C_BK);
    tft.setCursor(cx+46,cy+64); tft.print("AI");
    for(int x=cx-50;x<=cx+50;x+=4){ tft.drawFastHLine(cx-50,cy+80,x-(cx-50),C_DCY); delay(12); yield(); }
    const char* credit="by Patrick Perez";
    int creditW=(int)strlen(credit)*6; int creditX=W/2-creditW/2;
    for(int dy=10;dy>=0;dy-=2){
        tft.fillRect(0,cy+82,W,14,C_BK); tft.setTextSize(1); tft.setTextColor(C_LG);
        tft.setCursor(creditX,cy+86+dy); tft.print(credit); delay(25); yield();
    }
    tft.setTextColor(C_DCY); tft.setTextSize(1);
    tft.setCursor(creditX+creditW+6,cy+86); tft.print("v5.9");
    delay(120); yield();
}
void drawBootBar(int pct){
    if(pct>100) pct=100;
    int bx=40,bw=W-80,bh=8,by=H-16;
    tft.fillRoundRect(bx,by,bw,bh,3,0x0841);
    int fw=(int)((float)bw*pct/100.f);
    if(fw>4){
        tft.fillRoundRect(bx,by,fw,bh,3,C_DCY);
        if(fw>10) tft.fillRoundRect(bx,by,fw-4,bh,3,C_CY);
        tft.drawFastHLine(bx+2,by+1,fw-4,C_MINT);
    }
}
void drawBootLogo(){
    tft.fillScreen(C_BK);
    for(int i=0;i<50;i++){
        int x=(i*137+17)%W, y=(i*91+11)%(H-30)+5;
        uint16_t sc=(i%4==0)?C_LG:(i%4==1)?C_DG:(i%3==0)?C_DCY:(uint16_t)0x2945;
        tft.drawPixel(x,y,sc);
    }
    tft.drawRoundRect(39,H-17,W-78,10,4,C_DG);
}
void drawWifiScreen(){
    tft.fillScreen(C_BK);
    for(int i=0;i<40;i++){ int x=(i*179+23)%W, y=(i*113+7)%H; tft.drawPixel(x,y,C_DG); }
    tft.fillRect(0,0,W,36,C_CARD); tft.drawFastHLine(0,36,W,C_CY);
    tft.fillCircle(18,18,7,C_CY); tft.fillCircle(18,18,3,C_BK);
    tft.setTextSize(1);
    tft.setTextColor(C_WH);  tft.setCursor(32,12);    tft.print("BRONNY AI");
    tft.setTextColor(C_CY);  tft.setCursor(106,12);   tft.print("v5.9");
    tft.setTextColor(C_LG);  tft.setCursor(W-78,12);  tft.print("Patrick 2026");
    int wx=W/2, wy=82;
    tft.fillCircle(wx,wy+22,5,C_CY);
    tft.drawCircle(wx,wy+22,14,C_CY);
    tft.drawCircle(wx,wy+22,24,C_DCY);
    tft.drawCircle(wx,wy+22,34,C_DG);
    tft.fillRect(wx-40,wy+22,80,50,C_BK);
    tft.fillRoundRect(20,130,W-40,28,6,C_CARD);
    tft.drawRoundRect(20,130,W-40,28,6,C_CY);
    tft.setTextColor(C_LG); tft.setCursor(30,136); tft.print("Network:");
    tft.setTextColor(C_WH); tft.setCursor(88,136); tft.print(WIFI_SSID);
    tft.fillRect(0,H-20,W,20,C_CARD); tft.drawFastHLine(0,H-20,W,C_DG);
    tft.setTextColor(C_LG); tft.setTextSize(1); tft.setCursor(6,H-13);  tft.print("ESP32-S3");
    tft.setTextColor(C_CY);                    tft.setCursor(W/2-42,H-13); tft.print("Bronny AI v5.9");
    tft.setTextColor(C_LG);                    tft.setCursor(W-60,H-13); tft.print("2026");
}
void drawWifiStatus(const char* l1,uint16_t c1,const char* l2="",uint16_t c2=C_CY){
    tft.fillRect(0,164,W,H-20-164,C_BK);
    tft.setTextSize(2); tft.setTextColor(c1);
    int tw=(int)strlen(l1)*12; tft.setCursor(W/2-tw/2,168); tft.print(l1);
    if(strlen(l2)>0){
        tft.setTextSize(1); tft.setTextColor(c2);
        int tw2=(int)strlen(l2)*6; tft.setCursor(W/2-tw2/2,192); tft.print(l2);
    }
}
static uint8_t spinIdx=0; static uint32_t lastSpin=0;
void tickWifiSpinner(){
    uint32_t now=millis(); if(now-lastSpin<180) return; lastSpin=now;
    static const char* f[]={ "| ","/ ","- ","\\" };
    tft.fillRect(W/2-6,72,12,12,C_BK);
    tft.setTextSize(1); tft.setTextColor(C_CY);
    tft.setCursor(W/2-3,74); tft.print(f[spinIdx++%4]);
}

// ============================================================
// STANDBY MODE
// ============================================================
bool isWakeWord(const String& t) {
    String s=t; s.trim(); s.toLowerCase();
    static const char* ww[]={
        "bronny","bronnie","brony","brownie","brawny","bonnie",
        "hi bronny","hey bronny","hi bronnie","hey bronnie",
        "hi brony","hey brony","hi brownie","hey brownie",
        "hi brawny","hey brawny","hi bonnie","hey bonnie",
        nullptr
    };
    for(int i=0; ww[i]; i++) {
        if(s.indexOf(String(ww[i])) >= 0) return true;
    }
    return false;
}

void enterStandby() {
    standbyMode = true;
    setFaceSleep();
    drawFace(true);
    setStatus("Standby...", C_DCY);
    tftLog(C_DCY, "Standby — say 'Hi Bronny'");
    Serial.println("[Standby] Entering standby mode");
}

void exitStandby() {
    standbyMode   = false;
    lastRailwayMs = millis();
    clearZzz();
    setFaceIdle();
    drawFace(true);
    jingleWake();
    setStatus("Listening...", C_CY);
    tftLog(C_GR, "Bronny: awake!");
    Serial.println("[Standby] Exiting standby mode");
}

// ============================================================
// BOOT INTRO
// ============================================================
void doBootIntro() {
    if (!dgConnected) {
        Serial.println("[Intro] Skipped — Deepgram not connected");
        return;
    }
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
            maintainDeepgram();
            yield();
        }
    }

    stopTalk();
    setFaceIdle();
    forceDrawFace();
    lastRailwayMs   = millis();
    dgStreaming     = true;
    dgLastKeepalive = millis();
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

    // ── Log visibility commands — handled locally, no Railway call ───
    // Checked first, before standby, noise filter, and Railway.
    int logCmd = checkLogCommand(transcript);
    if (logCmd != 0) {
        setLogsVisible(logCmd == 1);
        dgStreaming     = true;
        dgLastKeepalive = millis();
        busy            = false;
        return;
    }

    // BUG FIX 7: wake-word check BEFORE noise filter in standby.
    if (standbyMode) {
        if (isWakeWord(transcript)) {
            Serial.printf("[Standby] Wake word: '%s'\n", transcript.c_str());
            exitStandby();
        } else {
            Serial.printf("[Standby] Ignored: '%s'\n", transcript.c_str());
        }
        dgStreaming     = true;
        dgLastKeepalive = millis();
        busy            = false;
        return;
    }

    // Active conversation: noise-filter before sending to Railway.
    if (isNoise(transcript)) {
        tftLogf(C_YL, "Filtered: '%s'", transcript.c_str());
        dgStreaming     = true;
        dgLastKeepalive = millis();
        setStatus("Listening...", C_CY);
        busy = false;
        return;
    }

    tftLogf(C_MINT, "You: %s", transcript.c_str());
    setFaceThink();

    // Thinking chime
    {
        i2s.setVolume(0.18f);
        playTone(1047,55); playSil(20); playTone(1319,80);
        i2s.setVolume(VOL_MAIN);
    }

    tftLog(C_YL, "Railway: thinking...");

    bool ok = callRailwayStream(transcript);

    if (!ok) {
        tftLog(C_RD, "Railway failed");
        stopTalk();
        forceDrawFace();
        dgStreaming     = true;
        dgLastKeepalive = millis();
        setStatus("Listening...", C_CY);
        busy = false;
        return;
    }

    lastRailwayMs = millis();
    setFaceHappy(1600);

    // BUG FIX 8: animate face during VAD cooldown drain loop.
    vadCooldownUntil = millis() + TTS_COOLDOWN_MS;
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
    if (audioOk){ auto sc=sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }

    micInit();

    // Logs visible during boot for diagnostics — hidden at the end of setup().
    logsVisible = true;

    drawBootLogo();
    playBootIntroAnim(W/2, H/2-32);
    drawBootBar(10); jingleBoot(); drawBootBar(55); delay(150); drawBootBar(100); delay(300);

    drawWifiScreen(); drawWifiStatus("Connecting...", C_YL);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    bool connected=false; uint32_t ws=millis();
    while (millis()-ws < 18000) {
        if (WiFi.status()==WL_CONNECTED){ connected=true; break; }
        tickWifiSpinner(); yield();
    }
    if (connected) {
        char ip[32]; snprintf(ip,32,"%s",WiFi.localIP().toString().c_str());
        drawWifiStatus("Connected!",C_GR,ip,C_CY); jingleConnect(); delay(900);
    } else {
        drawWifiStatus("FAILED",C_RD,"Check config",C_RD); jingleError(); delay(2000);
    }

    sendHeartbeat(); lastHbMs=millis();

    tft.fillScreen(C_BK);
    drawFaceBg(); drawFace(true);
    tft.drawFastHLine(0, LOG_Y-1, W, 0x1082);
    logDrawFooter();
    jingleReady();

    tftLog(C_GR,  "Bronny AI v5.9 ready");
    tftLogf(C_CY, "WiFi: %s", WiFi.localIP().toString().c_str());
    tftLogf(C_LG, "Heap %uK  PSRAM %uK",
            esp_get_free_heap_size()/1024,
            heap_caps_get_free_size(MALLOC_CAP_SPIRAM)/1024);

    connectDeepgram();

    dgStreaming = true;
    setStatus("Listening...", C_CY);

    // ── Switch to face-only mode (default) ───────────────────────────
    // Boot diagnostics are done. Buffers keep all messages so
    // "show logs" or "display logs" will repaint them instantly.
    setLogsVisible(false);
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

    if (!bootIntroDone && !busy && dgConnected) {
        bootIntroDone = true;
        doBootIntro();
        return;
    }

    if (!standbyMode && !busy && lastRailwayMs > 0
            && now - lastRailwayMs > STANDBY_TIMEOUT_MS) {
        enterStandby();
    }

    if (!pendingTranscript && dgFinal.length() > 0 && dgFinalReceivedAt > 0
            && now - dgFinalReceivedAt > DG_FINAL_TIMEOUT_MS) {
        pendingTranscript = true;
        dgFinalReceivedAt = 0;
        Serial.println("[DG] speech_final timeout -> self-trigger");
    }
    if (pendingTranscript && !busy && now > vadCooldownUntil) {
        runConversation();
    }

    if (Serial.available()) {
        char c = Serial.read();
        // 'm' — print Deepgram connection status
        if (c=='m') tftLogf(C_CY, "DG conn=%d stream=%d", dgConnected?1:0, dgStreaming?1:0);
        // 'l' — toggle log visibility from Serial Monitor
        if (c=='l') setLogsVisible(!logsVisible);
    }

    yield();
}
