/*
 * ╔══════════════════════════════════════════════════════════╗
 * ║           BRONNY AI  v6.0  AetherAI Edition              ║
 * ║           Developed by Patrick Perez                     ║
 * ╠══════════════════════════════════════════════════════════╣
 * ║  Hardware (unchanged)                                    ║
 * ║    Board   : ESP32-S3 Dev Module                         ║
 * ║    Codec   : ES8311  (I2C addr 0x18) — TX/Speaker        ║
 * ║    Mic     : INMP441 (I2S port 1, GPIOs 4/5/6)           ║
 * ║    Display : ST7789  320x240  (HSPI)                     ║
 * ╠══════════════════════════════════════════════════════════╣
 * ║  v6.0  AetherAI Railway integration                      ║
 * ║   • All face animation, VAD, jingles, standby kept.      ║
 * ║   • callSTT + callChat + callTTS replaced with a         ║
 * ║     single POST /voice/chat to Railway.                  ║
 * ║   • Receives MP3 bytes + X-Transcript header.            ║
 * ║   • MP3DecoderHelix pre-allocated at boot to avoid       ║
 * ║     heap fragmentation crash on first speak.             ║
 * ║   • edge-tts Microsoft neural voices via Railway.        ║
 * ║   • Wake word still energy-based (no extra API key).     ║
 * ╠══════════════════════════════════════════════════════════╣
 * ║  FIX LOG                                                 ║
 * ║   v6.1  inmp441Sample: raw>>14 → raw>>11 (8× louder,     ║
 * ║          correct INMP441 24-bit-in-32 extraction)        ║
 * ║         STT_TRAIL_MS trail capture added to recordVAD()  ║
 * ║          so final syllable is never cut off.             ║
 * ║         Credentials moved to voice_config.h (gitignore)  ║
 * ║         stt_client: sensevoice-v1 (multilingual)         ║
 * ╚══════════════════════════════════════════════════════════╝
 */

#include "esp_task_wdt.h"   // watchdog control — must be first
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
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <math.h>

// ============================================================
// CREDENTIALS — loaded from voice_config.h (gitignored)
// Copy voice_config.h.example → voice_config.h and fill in.
// NEVER hardcode credentials here.
// ============================================================
#include "voice_config.h"

// ============================================================
// FORWARD DECLARATIONS
// ============================================================
bool   recordVAD(int maxMs, bool shortCapture);
void   setStatus(const char* s, uint16_t c);
void   drawIslandBar();
void   animFace();
void   drawFace(bool full);
extern bool faceRedraw;

typedef void (*SseCB)(const String& jsonLine, void* ctx);

// ============================================================
// WATCHDOG HELPER
// Feeds the task watchdog. Safe to call even after deinit.
// Place before/after any call that can block > 2 seconds.
// ============================================================
#define WDT_FEED() do { esp_task_wdt_reset(); yield(); } while(0)

// ============================================================
// CONFIG
// ============================================================
#define MP3_MAX_BYTES (320 * 1024)   // 300 KB — ~15 s @ 160kbps

#define VOL_MAIN    0.50f
#define VOL_JINGLE  0.25f

static int VAD_THR = 5500;

#define VAD_SILENCE_MS       1500
#define VAD_SILENCE_WAKE_MS   600
#define MAX_RECORD_MS        7000
#define WAKE_RECORD_MS        2000
#define PRE_ROLL_MS            300
#define STANDBY_TIMEOUT_MS    4000
#define GLITCH_CLIP_RATIO     0.25f
#define TTS_COOLDOWN_MS        800
// FIX: trail capture — keep recording this many ms after silence is detected
// so the final syllable of the last word is never cut off before upload.
#define STT_TRAIL_MS          400

static volatile bool isSpeaking = false;

enum BronnyMode { MODE_ACTIVE, MODE_STANDBY };
static BronnyMode bronnyMode      = MODE_ACTIVE;
static uint32_t   lastVoiceTime   = 0;
static uint32_t   lastConvEndTime = 0;

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
// DISPLAY
// ============================================================
SPIClass tftSPI(HSPI);
Adafruit_ST7789 tft = Adafruit_ST7789(&tftSPI, PIN_CS, PIN_DC, -1);

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
#define C_ORG   0xFD20
#define C_GREY  0x7BEF
#define C_MINT  0x3FF7
#define C_DKBG  0x10A2
#define C_CARD  0x18C3
#define C_PINK  0xFC18
#define C_PURP  0x901F

// ── Island bar ───────────────────────────────────────────────
#define ISL_W   138
#define ISL_H    15
#define ISL_X   ((W - ISL_W) / 2)
#define ISL_Y   (H - ISL_H - 5)
#define ISL_R    7

// ── Face geometry — lowered to center on screen ──────────────
#define FCX    160
#define FCY    108
#define BOB      4

// Eye geometry
#define EW      112
#define EH       64
#define ER       22
#define ESEP    138
#define EYO     -22

// Mouth geometry
#define MW       72
#define MH_CL     6
#define MH_OP    30
#define MR        8
#define MYO      58

// Smile
#define SMILE_R     26
#define SMILE_TH     8

// Look-around range
#define LOOK_X_RANGE  14
#define LOOK_Y_RANGE   7

// ============================================================
// AUDIO ENGINE
// ============================================================
AudioInfo   ainf_rec(16000, 2, 16);
AudioInfo   ainf_tts(24000, 2, 16);

DriverPins     brdPins;
AudioBoard     brdDrv(AudioDriverES8311, brdPins);
I2SCodecStream i2s(brdDrv);
I2SStream      mic_stream;

SineWaveGenerator<int16_t>    sineGen(32000);
GeneratedSoundStream<int16_t> sineSrc(sineGen);
StreamCopy                    sineCopy(i2s, sineSrc);

static bool audioOk   = false;
static bool micOk     = false;
static bool inTtsMode = false;

// ============================================================
// GLITCH-FILTERED RMS
// ============================================================
static int32_t lastValidRMS = 0;

static int32_t filteredRMS(const int32_t* rawFrames, int frameCount) {
    if (frameCount <= 0) return lastValidRMS;
    int clipped = 0; int64_t sq = 0; int n = frameCount;
    for (int f = 0; f < n; f++) {
        int16_t s = (int16_t)(rawFrames[f * 2] >> 14);
        if (abs(s) >= 32000) clipped++;
        sq += (int32_t)s * s;
    }
    if ((float)clipped / n > GLITCH_CLIP_RATIO) return lastValidRMS;
    int32_t rms = (int32_t)sqrtf((float)sq / n);
    lastValidRMS = rms;
    return rms;
}

// FIX: raw >> 11 instead of raw >> 14.
// INMP441 outputs 24-bit audio left-aligned in a 32-bit I2S slot.
// >> 14 was shifting too far right, producing very quiet audio that
// Paraformer/SenseVoice could barely pick up. >> 11 correctly extracts
// the upper 16 bits of the 24-bit payload (8x louder, no clipping on
// normal speech levels).
static inline int16_t inmp441Sample(int32_t raw) { return (int16_t)(raw >> 11); }

// ============================================================
// AUDIO INIT
// ============================================================
void audioPinsSetup() {
    static bool pinsSet = false;
    if (!pinsSet) {
        Wire.begin(PIN_SDA, PIN_SCL, 100000);
        brdPins.addI2C(PinFunction::CODEC, PIN_SCL, PIN_SDA, ES_ADDR, 100000, Wire);
        brdPins.addI2S(PinFunction::CODEC, PIN_MCLK, PIN_BCLK, PIN_WS, PIN_DOUT, PIN_DIN);
        brdPins.addPin(PinFunction::PA, PIN_PA, PinLogic::Output);
        pinsSet = true;
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
        // 500 ms warm-up: lets INMP441 I2S DMA stabilise and flushes
        // the initial DC-offset transient that confuses VAD.
        uint8_t tmp[512];
        uint32_t e = millis() + 500;
        while (millis() < e) { mic_stream.readBytes(tmp, sizeof(tmp)); yield(); }
    }
}

void audioInitRec() {
    if (inTtsMode || !audioOk) {
        WDT_FEED();
        audioPinsSetup();
        auto cfg = i2s.defaultConfig(TX_MODE);
        cfg.copyFrom(ainf_rec);
        cfg.output_device = DAC_OUTPUT_ALL;
        audioOk   = i2s.begin(cfg);
        i2s.setVolume(VOL_MAIN);
        WDT_FEED();
        if (audioOk) { auto sc = sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }
        inTtsMode = false;
    }
}

void audioInitTTS() {
    if (!inTtsMode) {
        WDT_FEED();
        audioPinsSetup();
        auto cfg = i2s.defaultConfig(TX_MODE);
        cfg.copyFrom(ainf_tts);
        cfg.output_device = DAC_OUTPUT_ALL;
        audioOk   = i2s.begin(cfg);
        i2s.setVolume(VOL_MAIN);
        WDT_FEED();
        inTtsMode = true;
    }
}

void audioRestart() {
    WDT_FEED();
    i2s.end(); delay(150);
    audioOk = false; inTtsMode = false;
    Wire.end(); delay(60);
    Wire.begin(PIN_SDA, PIN_SCL, 100000);
    brdPins.addI2C(PinFunction::CODEC, PIN_SCL, PIN_SDA, ES_ADDR, 100000, Wire);
    brdPins.addI2S(PinFunction::CODEC, PIN_MCLK, PIN_BCLK, PIN_WS, PIN_DOUT, PIN_DIN);
    brdPins.addPin(PinFunction::PA, PIN_PA, PinLogic::Output);
    WDT_FEED();
    auto cfg = i2s.defaultConfig(TX_MODE);
    cfg.copyFrom(ainf_rec);
    cfg.output_device = DAC_OUTPUT_ALL;
    audioOk = i2s.begin(cfg);
    i2s.setVolume(VOL_MAIN);
    WDT_FEED();
    if (audioOk) { auto sc = sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }
}

// ── Tone helpers ─────────────────────────────────────────────
void playTone(float hz, int ms) {
    if (!audioOk) { delay(ms); return; }
    sineGen.setFrequency(hz);
    uint32_t e = millis() + ms;
    while (millis() < e) { sineCopy.copy(); yield(); }
}
void playSil(int ms) { playTone(0, ms); }

void jingleBoot() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    float n[] = {523,659,784,1047,1319,1568,2093};
    int   d[] = {100,100,100,140,260,80,280};
    for (int i = 0; i < 7; i++) { playTone(n[i], d[i]); playSil(20); }
    i2s.setVolume(VOL_MAIN);
}
void jingleConnect() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(880,100); playSil(25); playTone(1108,100); playSil(25);
    playTone(1318,200); playSil(150); i2s.setVolume(VOL_MAIN);
}
void jingleError() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(300,200); playSil(80); playTone(220,350); playSil(200);
    i2s.setVolume(VOL_MAIN);
}
void jingleReady() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(880,80); playSil(30); playTone(1318,80); playSil(30);
    playTone(1760,200); playSil(150); i2s.setVolume(VOL_MAIN);
}
void jingleWake() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(660,80); playSil(20); playTone(1100,120); playSil(80);
    i2s.setVolume(VOL_MAIN);
}
void jingleTune() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(440,60); playSil(20); playTone(880,120); playSil(80);
    i2s.setVolume(VOL_MAIN);
}


static const int8_t B64D[256] = {
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,62,-1,-1,-1,63,
    52,53,54,55,56,57,58,59,60,61,-1,-1,-1, 0,-1,-1,
    -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9,10,11,12,13,14,
    15,16,17,18,19,20,21,22,23,24,25,-1,-1,-1,-1,-1,
    -1,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,
    41,42,43,44,45,46,47,48,49,50,51,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,
    -1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1
};

// WAV HEADER
void wavHeader(uint8_t* h, uint32_t pcmB) {
    auto le4=[&](int o,uint32_t v){h[o]=v;h[o+1]=v>>8;h[o+2]=v>>16;h[o+3]=v>>24;};
    auto le2=[&](int o,uint16_t v){h[o]=v;h[o+1]=v>>8;};
    memcpy(h,"RIFF",4); le4(4,pcmB+36); memcpy(h+8,"WAVEfmt ",8);
    le4(16,16); le2(20,1); le2(22,1); le4(24,16000);
    le4(28,32000); le2(32,2); le2(34,16);
    memcpy(h+36,"data",4); le4(40,pcmB);
}


// HTTP HELPERS
static void ensureWifi() {
    if (WiFi.status() != WL_CONNECTED) {
        WiFi.reconnect();
        uint32_t t = millis();
        while (WiFi.status() != WL_CONNECTED && millis()-t < 8000)
            { delay(300); yield(); }
    }
}

// ============================================================
// AETHER AI  — global MP3 state
// ============================================================
#if __has_include("AudioTools/AudioCodecs/CodecMP3Helix.h")
  #include "AudioTools/AudioCodecs/CodecMP3Helix.h"
#elif __has_include("AudioCodecs/CodecMP3Helix.h")
  #include "AudioCodecs/CodecMP3Helix.h"
#endif

static MP3DecoderHelix   mp3Decoder;
static uint8_t*  mp3_buf = nullptr;
static size_t    mp3_len = 0;

// ============================================================
// callAetherVoice — send WAV → receive transcript + MP3
// Returns true if we got MP3 bytes to play.
// transcript[] is filled with what the user said.
// ============================================================
bool callAetherVoice(int16_t* pcm, int samples,
                     char* transcript, size_t tMax) {
    if (!pcm || samples <= 0) return false;
    if (transcript) transcript[0] = 0;

    // Build WAV in PSRAM
    uint32_t pcmB = (uint32_t)samples * 2;
    uint32_t wavB = 44 + pcmB;
    uint8_t* wavBuf = (uint8_t*)heap_caps_malloc(wavB, MALLOC_CAP_SPIRAM);
    if (!wavBuf) wavBuf = (uint8_t*)malloc(wavB);
    if (!wavBuf) return false;
    wavHeader(wavBuf, pcmB);
    memcpy(wavBuf + 44, pcm, pcmB);

    ensureWifi();
    if (WiFi.status() != WL_CONNECTED) { free(wavBuf); return false; }

    String url = String(AETHER_URL) + "/voice/chat";
    Serial.printf("[AETHER] POST %s  (%u bytes WAV)\n", url.c_str(), wavB);
    Serial.printf("[AETHER] Free heap: %u  PSRAM: %u\n",
                  esp_get_free_heap_size(),
                  heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    bool success = false;

    for (int attempt = 1; attempt <= 2 && !success; attempt++) {
        if (attempt > 1) { Serial.println("[AETHER] Retry 2/2..."); delay(2000); }

        WiFiClientSecure cli;
        cli.setInsecure();
        cli.setConnectionTimeout(20000);
        cli.setHandshakeTimeout(15);

        HTTPClient http;
        http.begin(cli, url);
        http.setTimeout(40000);

        const char* hdrs[] = { "X-Transcript", "X-Response-Text" };
        http.collectHeaders(hdrs, 2);
        http.addHeader("Content-Type",   "audio/wav");
        http.addHeader("X-Api-Key",      AETHER_API_KEY);

        WDT_FEED();
        int code = http.POST(wavBuf, wavB);
        WDT_FEED();
        Serial.printf("[AETHER] Response: %d\n", code);

        if (code == 200) {
            // Read response headers
            String th = http.header("X-Transcript");
            String sh = http.header("X-Response-Text");
            if (transcript && tMax > 1) {
                strncpy(transcript, th.c_str(), tMax - 1);
                transcript[tMax - 1] = 0;
            }
            Serial.printf("[AETHER] Heard:  %s\n", th.c_str());
            Serial.printf("[AETHER] Spoken: %s\n", sh.c_str());

            // Allocate MP3 buffer in PSRAM
            if (!mp3_buf) {
                mp3_buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
            }
            if (!mp3_buf) { http.end(); break; }

            // Read MP3 body
            WiFiClient* stream = http.getStreamPtr();
            int    clen = http.getSize();
            size_t got  = 0;

            if (clen > 0) {
                size_t want = min((size_t)clen, (size_t)MP3_MAX_BYTES);
                uint32_t dlDeadline = millis() + 30000;
                while (http.connected() && got < want && millis() < dlDeadline) {
                    size_t avail = stream->available();
                    if (avail) {
                        size_t chunk = min(avail, want - got);
                        got += stream->readBytes(mp3_buf + got, chunk);
                    } else delay(1);
                    WDT_FEED();
                }
            } else {
                uint32_t dlDeadline = millis() + 30000;
                while ((http.connected() || stream->available())
                       && got < (size_t)MP3_MAX_BYTES
                       && millis() < dlDeadline) {
                    if (stream->available()) {
                        got += stream->readBytes(mp3_buf + got,
                               min((size_t)512, (size_t)MP3_MAX_BYTES - got));
                    } else delay(1);
                    WDT_FEED();
                }
            }
            mp3_len = got;
            Serial.printf("[AETHER] MP3: %u bytes\n", mp3_len);
            success = (mp3_len > 0);
        } else {
            String body = http.getString();
            Serial.printf("[AETHER] Error body: %s\n", body.c_str());
        }
        http.end();
        WDT_FEED();
    }

    free(wavBuf);
    return success;
}

// ============================================================
// playMp3 — decode MP3 from PSRAM, play via ES8311
// ============================================================
void playMp3() {
    if (!mp3_len || !mp3_buf) {
        Serial.println("[PLAY] No MP3 data");
        return;
    }

    Serial.printf("[PLAY] %u bytes  heap: %u\n", mp3_len, esp_get_free_heap_size());

    // Stop mic I2S — frees DMA buffers so decoder malloc has room
    mic_stream.end();
    micOk = false;

    WDT_FEED();
    audioInitTTS();   // switch codec to 24 kHz FIRST
    WDT_FEED();

    // Codec PLL lock: write silence before first sample
    { int16_t ks[512]; memset(ks, 0, sizeof(ks)); i2s.write((uint8_t*)ks, sizeof(ks)); }
    delay(120);
    WDT_FEED();

    {
        EncodedAudioStream decoded(&i2s, &mp3Decoder);
        decoded.begin();

        MemoryStream mp3Mem(mp3_buf, mp3_len);
        StreamCopy   copier(decoded, mp3Mem);

        uint32_t lastFaceMs = millis();
        while (copier.copy()) {
            if (millis() - lastFaceMs > 16) {
                animFace();
                if (faceRedraw) { drawFace(false); faceRedraw = false; }
                lastFaceMs = millis();
            }
            WDT_FEED();
        }
        decoded.end();
    }

    // Trailing silence flush
    { int16_t sil[256]; memset(sil, 0, sizeof(sil)); i2s.write((uint8_t*)sil, sizeof(sil)); }
    delay(60);

    Serial.println("[PLAY] Done");

    // Switch codec back to REC mode and restart mic
    WDT_FEED();
    audioInitRec();
    WDT_FEED();
    micInit();
    if (micOk) {
        uint8_t drain[512];
        uint32_t drainEnd = millis() + 200;
        while (millis() < drainEnd) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
    }
    isSpeaking = false;
}


// ============================================================
// RECORDING
// ============================================================
#define MAX_SAMP_BYTES (MAX_RECORD_MS * 16000 / 1000 * 2)
static int16_t* recBuf  = nullptr;
static int      recLen  = 0;
static int32_t  recPeak = 0;

bool recordVAD(int maxMs, bool shortCapture = false) {
    if (!micOk) return false;
    if (!recBuf) {
        recBuf = (int16_t*)heap_caps_malloc(MAX_SAMP_BYTES, MALLOC_CAP_SPIRAM);
        if (!recBuf) recBuf = (int16_t*)malloc(MAX_SAMP_BYTES);
    }
    if (!recBuf) return false;
    int maxSamp = MAX_SAMP_BYTES / 2;
    recLen = 0; recPeak = 0;
    uint32_t silMs = shortCapture ? VAD_SILENCE_WAKE_MS : VAD_SILENCE_MS;
    float    eGate = shortCapture ? (VAD_THR * 1.2f) : (VAD_THR * 1.5f);
    const int preRollSamp = PRE_ROLL_MS * 16000 / 1000;
    int16_t*  preRoll = (int16_t*)malloc(preRollSamp * 2);
    int prHead = 0; bool prFull = false;
    bool speaking = false; uint32_t silStart = 0;
    uint32_t start = millis();
    int32_t rawBuf[256];

    while (millis() - start < (uint32_t)maxMs && recLen < maxSamp) {
        int rd = mic_stream.readBytes((uint8_t*)rawBuf, sizeof(rawBuf));
        int frames = rd / 8;
        if (frames <= 0) { yield(); continue; }
        int32_t rms = filteredRMS(rawBuf, frames);
        if (rms > recPeak) recPeak = rms;
        if (!speaking) {
            if (preRoll) {
                for (int f = 0; f < frames; f++) {
                    preRoll[prHead % preRollSamp] = inmp441Sample(rawBuf[f * 2]);
                    prHead++;
                    if (prHead >= preRollSamp) prFull = true;
                }
            }
            if (rms > VAD_THR) {
                speaking = true; silStart = 0;
                if (preRoll) {
                    int prCount = prFull ? preRollSamp : (prHead % preRollSamp);
                    int prStart = prFull ? (prHead % preRollSamp) : 0;
                    for (int p = 0; p < prCount && recLen < maxSamp; p++)
                        recBuf[recLen++] = preRoll[(prStart + p) % preRollSamp];
                }
            }
        } else {
            for (int f = 0; f < frames && recLen < maxSamp; f++)
                recBuf[recLen++] = inmp441Sample(rawBuf[f * 2]);
            if (rms < VAD_THR) {
                if (silStart == 0) silStart = millis();
                if (millis() - silStart >= silMs) {
                    // FIX: trail capture — keep recording STT_TRAIL_MS more ms
                    // after silence is detected so the final syllable of the
                    // last word is fully captured before we stop and upload.
                    uint32_t trailEnd = millis() + STT_TRAIL_MS;
                    while (millis() < trailEnd && recLen < maxSamp) {
                        int trd = mic_stream.readBytes((uint8_t*)rawBuf, sizeof(rawBuf));
                        int tframes = trd / 8;
                        for (int f = 0; f < tframes && recLen < maxSamp; f++)
                            recBuf[recLen++] = inmp441Sample(rawBuf[f * 2]);
                        yield();
                    }
                    break;
                }
            } else silStart = 0;
        }
        yield();
    }
    if (preRoll) free(preRoll);
    return (recLen > 16000 / 4) && (recPeak > (int32_t)eGate);
}

// WAKE WORD — energy-based
bool checkWakeWord() {
    if (!micOk) return false;
    bool got = recordVAD(WAKE_RECORD_MS, true);
    return got && recLen > 4800;
}

// NOISE FILTER
bool isNoise(const String& t, int recSamples = 0) {
    String s = t; s.trim();
    if (s.length() < 3) return true;
    if (recSamples > 0 && recSamples < 8000) return true;
    static const char* noiseWords[] = {
        "...", "..", ".", "ah", "uh", "hm", "hmm", "mm", "um", "huh",
        "oh", "ow", "beep", "boop", "ding", "dong", "ping", "ring",
        "the", "a", "i", nullptr
    };
    String lower = s; lower.toLowerCase();
    for (int i = 0; noiseWords[i]; i++)
        if (lower == String(noiseWords[i])) return true;
    return false;
}

// ============================================================
// STATUS ISLAND BAR
// ============================================================
static String   islandText  = "Ready";
static uint16_t islandColor = C_CY;

static uint16_t dim10(uint16_t c) {
    uint8_t r=(c>>11)&0x1F, g=(c>>5)&0x3F, b=c&0x1F;
    return (uint16_t)(((r*10/100)<<11)|((g*10/100)<<5)|(b*10/100));
}

void drawIslandBar() {
    tft.fillRect(ISL_X - 2, ISL_Y - 2, ISL_W + 4, ISL_H + 4, C_BK);
    uint16_t dimC   = dim10(islandColor);
    uint16_t dimCx2 = dim10(dim10(islandColor));
    tft.drawRoundRect(ISL_X-1, ISL_Y-1, ISL_W+2, ISL_H+2, ISL_R+1, dimCx2);
    tft.fillRoundRect(ISL_X, ISL_Y, ISL_W, ISL_H, ISL_R, C_BK);
    tft.drawRoundRect(ISL_X, ISL_Y, ISL_W, ISL_H, ISL_R, dimC);
    tft.fillCircle(ISL_X + 9, ISL_Y + ISL_H / 2, 2, dimC);
    tft.setTextSize(1); tft.setTextColor(dimC);
    int tw = (int)islandText.length() * 6;
    int tx = ISL_X + (ISL_W - tw) / 2 + 5;
    int ty = ISL_Y + (ISL_H - 8) / 2;
    tft.setCursor(tx, ty); tft.print(islandText);
}

void setStatus(const char* s, uint16_t c) {
    islandText = String(s); islandColor = c; drawIslandBar();
}

// ============================================================
// ══ ROBOT FACE v5 ══════════════════════════════════════════
// ============================================================

enum FaceState {
    FS_IDLE, FS_TALKING, FS_LISTEN, FS_THINK,
    FS_STANDBY, FS_SLEEP, FS_HAPPY, FS_SURPRISED
};

enum MouthMode { MM_NONE, MM_SMILE, MM_NEUTRAL, MM_OPEN, MM_O };

struct FaceData {
    FaceState state         = FS_IDLE;
    float  bobPh            = 0.f;
    float  bobY             = 0.f;
    float  tgtBobY          = 0.f;
    bool   blink            = false;
    int    blinkF           = 0;
    bool   winkLeft         = false;
    bool   winkRight        = false;
    float  talkPh           = 0.f;
    float  mOpen            = 0.f;
    float  tgtMOpen         = 0.f;
    MouthMode mouthMode     = MM_SMILE;
    MouthMode tgtMouthMode  = MM_SMILE;
    float  mouthModeT       = 1.f;
    float  eyeSX            = 1.f, eyeSY = 1.f;
    float  tgtESX           = 1.f, tgtESY = 1.f;
    float  eyeOpenL         = 1.f, eyeOpenR = 1.f;
    float  tgtOpenL         = 1.f, tgtOpenR = 1.f;
    float  squint           = 0.f, tgtSquint = 0.f;
    float  lookX = 0.f, lookY = 0.f;
    float  tLookX = 0.f, tLookY = 0.f;
    uint32_t nextLookMs     = 0;
    float  sleepLid         = 0.f;
    uint32_t emotionTimer   = 0;
    uint32_t standbyMs      = 0;
    float  surpriseScale    = 1.f;
    float  happyPh          = 0.f;
    float  listenPulse      = 0.f;
    float  thinkSq          = 0.f;
    float  prevBobY         = 0.f;
    float  prevOpenL        = 1.f, prevOpenR = 1.f;
    float  prevSX           = 1.f, prevSY    = 1.f;
    float  prevSquint       = 0.f;
    float  prevLookX        = 0.f, prevLookY = 0.f;
    MouthMode prevMouth     = MM_SMILE;
    float  prevMOpen        = 0.f;
} face;

bool            faceRedraw   = false;
static uint32_t lastBlink    = 0;
static uint32_t nextBlink    = 3200;
static uint32_t lastFaceAnim = 0;

// ── ZZZ state ─────────────────────────────────────────────────
struct ZzzState {
    bool     active        = false;
    uint32_t startMs       = 0;
    uint32_t nextSpawnMs   = 0;
    int      prevAbsX[4]   = {};
    int      prevAbsY[4]   = {};
    bool     prevDrawn[4]  = {};
} zzz;

// ── Eye geometry helpers ──────────────────────────────────────
static void eyeBounds(int cx, int cy,
                      float openFrac, float squint, float sx, float sy,
                      int& x0, int& y0, int& ew, int& eh)
{
    ew = max(8,  (int)(EW * sx));
    eh = max(2,  (int)(EH * openFrac * sy * (1.f - squint * 0.55f)));
    x0 = cx - ew/2;
    y0 = cy - eh/2;
}

static void eraseEyeUnion(int ax0,int ay0,int aew,int aeh,
                           int bx0,int by0,int bew,int beh)
{
    int x1 = min(ax0, bx0) - 1;
    int y1 = min(ay0, by0) - 1;
    int x2 = max(ax0+aew, bx0+bew) + 1;
    int y2 = max(ay0+aeh, by0+beh) + 1;
    x1=max(x1,0); y1=max(y1,0);
    x2=min(x2,W-1); y2=min(y2,ISL_Y-1);
    if (x2>x1 && y2>y1) tft.fillRect(x1,y1,x2-x1,y2-y1,C_BK);
}

static void drawOneEye(int cx, int cy,
                       float openFrac, float squint, float sx, float sy,
                       float lidDroop,
                       bool sleeping)
{
    int ew = max(8,  (int)(EW * sx));
    int eh = max(2,  (int)(EH * openFrac * sy * (1.f - squint * 0.55f)));
    int r  = min(ER, min(ew/2, eh/2));
    tft.fillRoundRect(cx-ew/2, cy-eh/2, ew, eh, r, C_WH);

    if (lidDroop > 0.02f) {
        int lidH = (int)(eh * 0.65f * lidDroop);
        if (lidH > 0) {
            for (int row = 0; row < lidH; row++) {
                int lx0 = cx - ew/2 + r;
                int lx1 = cx + ew/2 - r;
                int ly  = cy - eh/2 + row;
                if (ly >= cy - eh/2 && ly <= cy + eh/2)
                    tft.drawFastHLine(lx0, ly, lx1-lx0, C_BK);
            }
        }
    }
}

static void drawSmile(int cx, int cy) {
    tft.fillCircle(cx, cy, SMILE_R, C_WH);
    tft.fillRect(cx - SMILE_R - 1, cy - SMILE_R - 1,
                 (SMILE_R + 1)*2 + 2, SMILE_R + 2, C_BK);
    int inner = SMILE_R - SMILE_TH;
    if (inner > 2) {
        tft.fillCircle(cx, cy, inner, C_BK);
        tft.fillRect(cx - inner - 1, cy - inner - 1,
                     (inner + 1)*2 + 2, inner + 2, C_BK);
    }
}

static void drawNeutral(int cx, int cy) {
    tft.fillRoundRect(cx - MW/2, cy - 3, MW, 6, 3, C_WH);
}

static void drawOpenMouth(int cx, int cy, float openFrac) {
    int mh = MH_CL + (int)((MH_OP - MH_CL) * openFrac);
    int r  = min(MR, mh/2);
    tft.fillRoundRect(cx - MW/2, cy - mh/2, MW, mh, r, C_WH);
}

static void drawOMouth(int cx, int cy, float openFrac) {
    int r = 8 + (int)(14 * openFrac);
    tft.fillCircle(cx, cy, r, C_WH);
    tft.fillCircle(cx, cy, max(2, r-5), C_BK);
}

void drawFaceBg() { tft.fillRect(0, 0, W, ISL_Y, C_BK); }

void drawFace(bool full) {
    int by  = (int)face.bobY;
    int lxi = (int)(face.lookX * LOOK_X_RANGE);
    int lyi = (int)(face.lookY * LOOK_Y_RANGE);

    int lex = FCX - ESEP/2 + lxi;
    int rex = FCX + ESEP/2 + lxi;
    int ey  = FCY + EYO + by + lyi;
    int my  = FCY + MYO + by;

    float blinkL = face.eyeOpenL;
    float blinkR = face.eyeOpenR;
    if (face.blink) {
        int bf = face.blinkF;
        float bf_frac = (bf <= 4) ? (1.f - bf/4.f) : ((bf-4)/5.f);
        bf_frac = constrain(bf_frac, 0.f, 1.f);
        if (face.winkLeft)       blinkL = bf_frac;
        else if (face.winkRight) blinkR = bf_frac;
        else                     { blinkL = bf_frac; blinkR = bf_frac; }
    }
    float lidDroop = face.sleepLid;
    if (face.state == FS_SLEEP) {
        float maxOpen = 0.38f;
        blinkL = min(blinkL, maxOpen);
        blinkR = min(blinkR, maxOpen);
    }

    float sq  = face.squint;
    float sx  = face.eyeSX;
    float sy  = face.eyeSY;

    int newEW, newEH, nx0, ny0;
    eyeBounds(lex, ey, blinkL, sq, sx, sy, nx0, ny0, newEW, newEH);
    int newEW2, newEH2, nx2, ny2;
    eyeBounds(rex, ey, blinkR, sq, sx, sy, nx2, ny2, newEW2, newEH2);

    int prevBy = (int)face.prevBobY;
    int prevLxi= (int)(face.prevLookX * LOOK_X_RANGE);
    int prevLyi= (int)(face.prevLookY * LOOK_Y_RANGE);
    int olex   = FCX - ESEP/2 + prevLxi;
    int orex   = FCX + ESEP/2 + prevLxi;
    int oey    = FCY + EYO + prevBy + prevLyi;
    int oEW, oEH, ox0, oy0;
    eyeBounds(olex, oey, face.prevOpenL, face.prevSquint, face.prevSX, face.prevSY, ox0, oy0, oEW, oEH);
    int oEW2, oEH2, ox2, oy2;
    eyeBounds(orex, oey, face.prevOpenR, face.prevSquint, face.prevSX, face.prevSY, ox2, oy2, oEW2, oEH2);

    bool eyeChg = full
        || fabsf(face.bobY   - face.prevBobY)  > 0.5f
        || fabsf(face.lookX  - face.prevLookX) > 0.01f
        || fabsf(face.lookY  - face.prevLookY) > 0.01f
        || fabsf(blinkL      - face.prevOpenL) > 0.02f
        || fabsf(blinkR      - face.prevOpenR) > 0.02f
        || fabsf(sx          - face.prevSX)    > 0.01f
        || fabsf(sy          - face.prevSY)    > 0.01f
        || fabsf(sq          - face.prevSquint)> 0.02f;

    if (eyeChg) {
        if (!full) {
            eraseEyeUnion(nx0, ny0, newEW, newEH, ox0, oy0, oEW, oEH);
            eraseEyeUnion(nx2, ny2, newEW2, newEH2, ox2, oy2, oEW2, oEH2);
        }
        drawOneEye(lex, ey, blinkL, sq, sx, sy, lidDroop,
                   face.state == FS_SLEEP || face.state == FS_STANDBY);
        drawOneEye(rex, ey, blinkR, sq, sx, sy, lidDroop,
                   face.state == FS_SLEEP || face.state == FS_STANDBY);
        face.prevBobY  = face.bobY;
        face.prevOpenL = blinkL;
        face.prevOpenR = blinkR;
        face.prevSX    = sx; face.prevSY = sy;
        face.prevSquint= sq;
        face.prevLookX = face.lookX;
        face.prevLookY = face.lookY;
    }

    MouthMode curMode = face.mouthMode;
    bool mChg = full
        || (curMode != face.prevMouth)
        || (curMode == MM_OPEN && fabsf(face.mOpen - face.prevMOpen) > 0.02f)
        || (curMode == MM_O    && fabsf(face.mOpen - face.prevMOpen) > 0.02f)
        || fabsf(face.bobY - face.prevBobY) > 0.5f;

    if (mChg) {
        if (!full) {
            int oldMy = FCY + MYO + (int)face.prevBobY;
            tft.fillRect(FCX - MW/2 - SMILE_R - 2, oldMy - MH_OP/2 - SMILE_R - 2,
                         MW + (SMILE_R+2)*2, MH_OP + SMILE_R*2 + 8, C_BK);
        }
        switch (curMode) {
            case MM_SMILE:   drawSmile(FCX, my);            break;
            case MM_NEUTRAL: drawNeutral(FCX, my);          break;
            case MM_OPEN:    drawOpenMouth(FCX, my, face.mOpen); break;
            case MM_O:       drawOMouth(FCX, my, face.mOpen);    break;
            case MM_NONE:    break;
        }
        face.prevMouth = curMode;
        face.prevMOpen = face.mOpen;
    }
}

void animFace() {
    uint32_t now = millis();
    if (now - lastFaceAnim < 16) return;
    uint32_t dt = now - lastFaceAnim;
    lastFaceAnim = now;
    bool ch = false;

    if (face.emotionTimer > 0 && now > face.emotionTimer) {
        face.emotionTimer = 0;
        face.state = FS_IDLE;
        face.tgtOpenL = face.tgtOpenR = 1.f;
        face.tgtSquint = 0.f;
        face.tgtMouthMode = MM_SMILE;
        face.sleepLid = 0.f;
        ch = true;
    }

    if (face.state == FS_STANDBY) {
        if (face.standbyMs == 0) face.standbyMs = now;
        if (now - face.standbyMs > 4000) {
            face.state = FS_SLEEP;
            face.sleepLid = 0.f;
            face.tgtMouthMode = MM_NONE;
            ch = true;
        }
    }

    if (face.mouthMode != face.tgtMouthMode) {
        face.mouthMode = face.tgtMouthMode;
        ch = true;
    }

    float bobSpeed = (face.state==FS_SLEEP)   ? 0.004f
                   : (face.state==FS_STANDBY) ? 0.007f
                   :                            0.020f;
    float bobAmp   = (face.state==FS_SLEEP || face.state==FS_STANDBY) ? 2.f : (float)BOB;
    if (face.state != FS_LISTEN) {
        face.bobPh += bobSpeed;
        if (face.bobPh > 6.2832f) face.bobPh -= 6.2832f;
        face.tgtBobY = sinf(face.bobPh) * bobAmp;
    }
    float newBob = face.bobY + (face.tgtBobY - face.bobY) * 0.22f;
    if (fabsf(newBob - face.bobY) > 0.1f) { face.bobY = newBob; ch = true; }

    if (face.state != FS_SURPRISED && face.state != FS_SLEEP) {
        float bS = 1.f + sinf(face.bobPh * 0.5f) * 0.04f;
        face.tgtESX = bS; face.tgtESY = 2.f - bS;
    }
    face.eyeSX += (face.tgtESX - face.eyeSX) * 0.12f;
    face.eyeSY += (face.tgtESY - face.eyeSY) * 0.12f;
    if (fabsf(face.eyeSX-face.prevSX)>0.005f || fabsf(face.eyeSY-face.prevSY)>0.005f) ch=true;

    face.squint += (face.tgtSquint - face.squint) * 0.10f;
    if (fabsf(face.squint - face.prevSquint) > 0.005f) ch = true;

    face.eyeOpenL += (face.tgtOpenL - face.eyeOpenL) * 0.15f;
    face.eyeOpenR += (face.tgtOpenR - face.eyeOpenR) * 0.15f;
    if (fabsf(face.eyeOpenL-face.prevOpenL)>0.01f || fabsf(face.eyeOpenR-face.prevOpenR)>0.01f) ch=true;

    bool canLook = (face.state==FS_IDLE || face.state==FS_HAPPY
                 || face.state==FS_STANDBY || face.state==FS_SLEEP);
    if (canLook) {
        if (now >= face.nextLookMs) {
            float rng = (face.state==FS_SLEEP || face.state==FS_STANDBY) ? 0.35f : 1.f;
            face.tLookX = ((float)(random(7))/3.f - 1.f) * rng;
            face.tLookY = ((float)(random(5))/2.f - 1.f) * rng * 0.5f;
            if (random(4)==0) { face.tLookX = 0.f; face.tLookY = 0.f; }
            uint32_t hold = (face.state==FS_SLEEP) ? 3000+random(3000) : 500+random(2000);
            face.nextLookMs = now + hold;
        }
        float sp = (face.state==FS_SLEEP) ? 0.02f : 0.06f;
        face.lookX += (face.tLookX - face.lookX) * sp;
        face.lookY += (face.tLookY - face.lookY) * sp;
        if (fabsf(face.lookX-face.prevLookX)>0.01f || fabsf(face.lookY-face.prevLookY)>0.01f) ch=true;
    } else if (face.state==FS_THINK) {
        face.lookX += (0.55f - face.lookX) * 0.06f;
        face.lookY += (-0.5f - face.lookY) * 0.06f;
        ch = true;
    } else {
        face.lookX *= 0.88f;
        face.lookY *= 0.88f;
        if (fabsf(face.lookX)>0.01f || fabsf(face.lookY)>0.01f) ch=true;
    }

    bool canBlink = (face.state==FS_IDLE || face.state==FS_TALKING
                  || face.state==FS_STANDBY || face.state==FS_HAPPY
                  || face.state==FS_SLEEP);
    uint32_t blinkGap = (face.state==FS_SLEEP) ? nextBlink+6000
                      : (face.state==FS_STANDBY) ? nextBlink+4000
                      : nextBlink;
    if (canBlink && !face.blink && now - lastBlink > blinkGap) {
        face.blink = true; face.blinkF = 0;
        bool doWink = (random(100) < 15);
        face.winkLeft  = doWink && (random(2) == 0);
        face.winkRight = doWink && !face.winkLeft;
        nextBlink = 2200 + (uint32_t)random(2800);
    }
    if (face.blink) {
        int maxBF = (face.state==FS_SLEEP) ? 14 : 9;
        face.blinkF++;
        if (face.blinkF >= maxBF) {
            face.blink=false; face.blinkF=0;
            face.winkLeft=false; face.winkRight=false;
            lastBlink=now;
        }
        ch = true;
    }

    switch (face.state) {

        case FS_TALKING: {
            face.talkPh += 0.38f;
            if (face.talkPh > 6.2832f) face.talkPh -= 6.2832f;
            float jaw     = sinf(face.talkPh) * 0.70f;
            float flutter = sinf(face.talkPh * 2.3f) * 0.18f;
            float tOpen   = constrain(0.30f + jaw + flutter, 0.f, 1.f);
            face.mOpen += (tOpen - face.mOpen) * 0.35f;
            face.mouthMode = MM_OPEN;
            float eyePop = 1.f + fabsf(jaw) * 0.06f;
            face.tgtESY = eyePop;
            ch = true;
            break;
        }

        case FS_LISTEN: {
            face.listenPulse += 0.07f;
            if (face.listenPulse > 6.2832f) face.listenPulse -= 6.2832f;
            face.lookX += (0.0f  - face.lookX) * 0.05f;
            face.lookY += (-0.35f - face.lookY) * 0.05f;
            face.tgtSquint = 0.1f;
            face.tgtESX = 1.05f;
            face.mouthMode = MM_NEUTRAL;
            ch = true;
            break;
        }

        case FS_THINK:
            face.tgtSquint = 0.72f;
            face.mouthMode = MM_NEUTRAL;
            ch = true;
            break;

        case FS_HAPPY: {
            face.happyPh += 0.14f;
            if (face.happyPh > 6.2832f) face.happyPh -= 6.2832f;
            face.tgtSquint  = 0.30f;
            face.tgtESX     = 1.08f;
            face.tgtESY = 1.f + sinf(face.happyPh) * 0.09f;
            face.mouthMode = MM_SMILE;
            ch = true;
            break;
        }

        case FS_SURPRISED: {
            face.tgtSquint = 0.f;
            face.tgtESX = face.surpriseScale;
            face.tgtESY = face.surpriseScale;
            float tgtScale = 1.35f;
            face.surpriseScale += (tgtScale - face.surpriseScale) * 0.18f;
            face.mOpen += (0.90f - face.mOpen) * 0.20f;
            face.mouthMode = MM_O;
            face.tgtOpenL = face.tgtOpenR = 1.f;
            ch = true;
            break;
        }

        case FS_SLEEP: {
            if (face.sleepLid < 1.0f) { face.sleepLid += 0.006f; ch=true; }
            else face.sleepLid = 1.0f;
            face.tgtESX = face.tgtESY = 1.f;
            face.tgtSquint = 0.5f;
            face.mouthMode = MM_NONE;
            break;
        }

        case FS_STANDBY:
            face.mouthMode = MM_NEUTRAL;
            if (face.mOpen > 0.01f) { face.mOpen *= 0.92f; ch=true; }
            break;

        default:
            face.tgtSquint = 0.f;
            face.tgtESX = face.tgtESY = 1.f;
            face.mouthMode = MM_SMILE;
            break;
    }

    if (ch) faceRedraw = true;
}

// ── State setters ─────────────────────────────────────────────
void setFaceIdle() {
    face.state=FS_IDLE; face.thinkSq=0.f; face.mOpen=0.f;
    face.tgtSquint=0.f; face.surpriseScale=1.f; face.emotionTimer=0;
    face.sleepLid=0.f; face.standbyMs=0;
    face.tgtMouthMode=MM_SMILE; face.tgtOpenL=face.tgtOpenR=1.f;
    faceRedraw=true;
}
void setFaceTalk() {
    face.state=FS_TALKING; face.talkPh=0.f; face.tgtSquint=0.f;
    face.emotionTimer=0; face.sleepLid=0.f;
    face.mouthMode=MM_OPEN; face.tgtMouthMode=MM_OPEN;
    faceRedraw=true;
}
void setFaceListen() {
    face.state=FS_LISTEN; face.listenPulse=0.f;
    face.bobPh=0.f; face.bobY=0.f; face.emotionTimer=0;
    face.sleepLid=0.f; face.tgtSquint=0.1f;
    face.mouthMode=MM_NEUTRAL; face.tgtMouthMode=MM_NEUTRAL;
    faceRedraw=true;
}
void setFaceThink() {
    face.state=FS_THINK; face.tgtSquint=0.f;
    face.emotionTimer=0; face.sleepLid=0.f;
    face.mouthMode=MM_NEUTRAL; face.tgtMouthMode=MM_NEUTRAL;
    faceRedraw=true;
}
void setFaceStandby() {
    face.state=FS_STANDBY; face.mOpen=0.f;
    face.emotionTimer=0; face.standbyMs=0;
    face.sleepLid=0.f; face.tgtSquint=0.f;
    face.mouthMode=MM_NEUTRAL; face.tgtMouthMode=MM_NEUTRAL;
    faceRedraw=true;
}
void setFaceSleep() {
    face.state=FS_SLEEP; face.mOpen=0.f;
    face.emotionTimer=0; face.standbyMs=millis();
    face.mouthMode=MM_NONE; face.tgtMouthMode=MM_NONE;
    faceRedraw=true;
}
void setFaceHappy(uint32_t ms=1800) {
    face.state=FS_HAPPY; face.happyPh=0.f; face.mOpen=0.f;
    face.emotionTimer=millis()+ms; face.sleepLid=0.f;
    face.tgtSquint=0.30f;
    face.mouthMode=MM_SMILE; face.tgtMouthMode=MM_SMILE;
    faceRedraw=true;
}
void setFaceSurprised(uint32_t ms=700) {
    face.state=FS_SURPRISED; face.surpriseScale=1.0f; face.mOpen=0.2f;
    face.emotionTimer=millis()+ms; face.sleepLid=0.f;
    face.tgtSquint=0.f; face.tgtOpenL=face.tgtOpenR=1.f;
    face.mouthMode=MM_O; face.tgtMouthMode=MM_O;
    faceRedraw=true;
}
void startTalk() { setFaceTalk(); }
void stopTalk()  { setFaceIdle(); }

// ── ZZZ animation ────────────────────────────────────────────
void tickZzz(uint32_t now) {
    bool isSleep = (face.state == FS_SLEEP);

    if (!isSleep) {
        for (int i=0;i<4;i++) {
            if (zzz.prevDrawn[i]) {
                tft.setTextSize(3);
                tft.fillRect(zzz.prevAbsX[i]-2, zzz.prevAbsY[i]-2, 22+4, 26+4, C_BK);
                zzz.prevDrawn[i]=false;
            }
        }
        zzz.active=false;
        zzz.nextSpawnMs=now+1000;
        return;
    }

    if (!zzz.active) {
        if (now < zzz.nextSpawnMs) return;
        zzz.active=true; zzz.startMs=now;
    }

    uint32_t elapsed = now - zzz.startMs;
    uint32_t totalDur = 5000;

    if (elapsed >= totalDur) {
        for (int i=0;i<4;i++) {
            if (zzz.prevDrawn[i]) {
                int sz = (i>=2) ? 3 : 2;
                tft.fillRect(zzz.prevAbsX[i]-2, zzz.prevAbsY[i]-2,
                             sz*6+8, sz*8+8, C_BK);
                zzz.prevDrawn[i]=false;
            }
        }
        zzz.active=false;
        zzz.nextSpawnMs=now+800+random(800);
        return;
    }

    int baseX = FCX + ESEP/2 + EW/2 - 10;
    int baseY = FCY + EYO - EH/2 - 8;

    for (int i=0; i<4; i++) {
        uint32_t spawnDelay = (uint32_t)(i * 800);
        if (elapsed < spawnDelay) continue;
        uint32_t zElapsed = elapsed - spawnDelay;
        float    zDur = 3200.f;
        float    t = constrain((float)zElapsed / zDur, 0.f, 1.f);

        int zx = baseX + i*14 + (int)(t * 14 + sinf(t*3.14f)*8);
        int zy = baseY - i*14  - (int)(t * 38);

        if (zzz.prevDrawn[i]) {
            int sz = (i >= 2) ? 3 : 2;
            tft.fillRect(zzz.prevAbsX[i]-2, zzz.prevAbsY[i]-2,
                         sz*6+8, sz*8+8, C_BK);
        }
        if (zy < 4 || zy > ISL_Y-18 || zx < 4 || zx > W-20) {
            zzz.prevDrawn[i]=false; continue;
        }

        uint16_t col = (t < 0.3f) ? C_CY : (t < 0.6f) ? C_DCY : C_DG;

        int sz = (i >= 2) ? 3 : 2;
        tft.setTextSize(sz);
        tft.setTextColor(col);
        tft.setCursor(zx, zy);
        tft.print("z");

        zzz.prevDrawn[i]=true;
        zzz.prevAbsX[i]=zx;
        zzz.prevAbsY[i]=zy;
    }
}

// ============================================================
// HEX / BOOT HELPERS
// ============================================================
static void drawHexOutline(int cx, int cy, int r, uint16_t col) {
    for (int i=0;i<6;i++) {
        float a1=(i*60-30)*3.14159f/180.f, a2=((i+1)*60-30)*3.14159f/180.f;
        int x1=cx+(int)(r*cosf(a1)), y1=cy+(int)(r*sinf(a1));
        int x2=cx+(int)(r*cosf(a2)), y2=cy+(int)(r*sinf(a2));
        tft.drawLine(x1,y1,x2,y2,col);
    }
}
static void drawBMonogram(int cx, int cy, bool big) {
    int scale=big?1:0;
    int bx=cx-7-scale, by2=cy-16-scale;
    int bw=5+scale*2, bh=32+scale*2;
    int bumpW=16+scale*2, bumpH1=14+scale, bumpH2=17+scale;
    int rTop=5, rBot=6;
    uint16_t col=big?C_MINT:C_CY;
    tft.fillRect(bx,by2,bw,bh,col);
    tft.fillRoundRect(bx,by2,bumpW,bumpH1,rTop,col);
    tft.fillRoundRect(bx,by2+bh/2,bumpW+2,bumpH2,rBot,col);
    tft.fillRect(bx+2,by2+2,bumpW-6,bumpH1-4,C_BK);
    tft.fillRect(bx+2,by2+bh/2+2,bumpW-4,bumpH2-5,C_BK);
}
void playBootIntroAnim(int cx, int cy) {
    int hexR[]={56,52,48}; uint16_t hexC[]={0x18C3,C_DCY,C_CY};
    for (int i=0;i<3;i++) { drawHexOutline(cx,cy,hexR[i],hexC[i]); delay(45); yield(); }
    for (int r=44;r>=2;r-=2) { uint16_t s=(r>30)?C_MID:(r>18)?C_BG:C_BK; drawHexOutline(cx,cy,r,s); delay(7); yield(); }
    for (int r=44;r>=2;r-=4) drawHexOutline(cx,cy,r,C_WH);
    delay(55); yield();
    for (int r=44;r>=2;r-=2) { uint16_t s=(r>30)?C_MID:(r>18)?C_BG:C_BK; drawHexOutline(cx,cy,r,s); }
    drawBMonogram(cx,cy,true); delay(60); yield();
    tft.fillRect(cx-32,cy-20,64,40,C_BK);
    for (int r=44;r>=2;r-=2) { uint16_t s=(r>30)?C_MID:(r>18)?C_BG:C_BK; drawHexOutline(cx,cy,r,s); }
    drawBMonogram(cx,cy,false); delay(80); yield();
    for (int dy=14;dy>=0;dy-=2) {
        tft.fillRect(0,cy+56,W,22,C_BK);
        tft.setTextSize(2); tft.setTextColor(C_WH);
        tft.setCursor(cx-36,cy+60+dy); tft.print("BRONNY");
        delay(22); yield();
    }
    tft.fillRoundRect(cx+42,cy+58,22,16,4,C_CY);
    tft.setTextSize(1); tft.setTextColor(C_BK);
    tft.setCursor(cx+46,cy+64); tft.print("AI");
    for (int x=cx-50;x<=cx+50;x+=4) { tft.drawFastHLine(cx-50,cy+80,x-(cx-50),C_DCY); delay(12); yield(); }
    const char* credit="by Patrick Perez";
    int creditW=(int)strlen(credit)*6, creditX=W/2-creditW/2;
    for (int dy=10;dy>=0;dy-=2) {
        tft.fillRect(0,cy+82,W,14,C_BK);
        tft.setTextSize(1); tft.setTextColor(C_LG);
        tft.setCursor(creditX,cy+86+dy); tft.print(credit);
        delay(25); yield();
    }
    tft.setTextColor(C_DCY); tft.setTextSize(1);
    tft.setCursor(creditX+creditW+6,cy+86); tft.print("v6.1");
    delay(120); yield();
}
void drawBootBar(int pct) {
    if (pct>100) pct=100;
    int bx=40,bw=W-80,bh=8,by=H-16;
    tft.fillRoundRect(bx,by,bw,bh,3,0x0841);
    int fw=(int)((float)bw*pct/100.f);
    if (fw>4) {
        tft.fillRoundRect(bx,by,fw,bh,3,C_DCY);
        if (fw>10) tft.fillRoundRect(bx,by,fw-4,bh,3,C_CY);
        tft.drawFastHLine(bx+2,by+1,fw-4,C_MINT);
    }
}
void drawBootLogo() {
    tft.fillScreen(C_BK);
    for (int i=0;i<50;i++) {
        int x=(i*137+17)%W, y=(i*91+11)%(H-30)+5;
        uint16_t sc=(i%4==0)?C_LG:(i%4==1)?C_DG:(i%3==0)?C_DCY:0x2945;
        tft.drawPixel(x,y,sc);
    }
    tft.drawRoundRect(39,H-17,W-78,10,4,C_DG);
}

// ============================================================
// WIFI / VAD SCREENS
// ============================================================
void drawWifiScreen() {
    tft.fillScreen(C_BK);
    for (int i=0;i<40;i++) { int x=(i*179+23)%W, y=(i*113+7)%H; tft.drawPixel(x,y,C_DG); }
    tft.fillRect(0,0,W,36,C_CARD); tft.drawFastHLine(0,36,W,C_CY);
    tft.fillCircle(18,18,7,C_CY); tft.fillCircle(18,18,3,C_BK);
    tft.setTextSize(1); tft.setTextColor(C_WH); tft.setCursor(32,12); tft.print("BRONNY AI");
    tft.setTextColor(C_CY); tft.setCursor(106,12); tft.print("v6.1");
    tft.setTextColor(C_LG); tft.setCursor(W-78,12); tft.print("Patrick 2026");
    int wx=W/2, wy=82;
    tft.fillCircle(wx,wy+22,5,C_CY);
    tft.drawCircle(wx,wy+22,14,C_CY); tft.drawCircle(wx,wy+22,24,C_DCY); tft.drawCircle(wx,wy+22,34,C_DG);
    tft.fillRect(wx-40,wy+22,80,50,C_BK);
    tft.fillRoundRect(20,130,W-40,28,6,C_CARD); tft.drawRoundRect(20,130,W-40,28,6,C_CY);
    tft.setTextSize(1); tft.setTextColor(C_LG); tft.setCursor(30,136); tft.print("Network:");
    tft.setTextColor(C_WH); tft.setCursor(88,136); tft.print(WIFI_SSID);
    tft.fillRect(0,H-20,W,20,C_CARD); tft.drawFastHLine(0,H-20,W,C_DG);
    tft.setTextColor(C_LG); tft.setTextSize(1); tft.setCursor(6,H-13); tft.print("ESP32-S3");
    tft.setTextColor(C_CY);
    int fw2=(int)(strlen("Bronny AI v6.1")*6);
    tft.setCursor(W/2-fw2/2,H-13); tft.print("Bronny AI v6.1");
    tft.setTextColor(C_LG); tft.setCursor(W-60,H-13); tft.print("2026");
}
void drawWifiStatus(const char* line1,uint16_t c1,const char* line2="",uint16_t c2=C_CY) {
    tft.fillRect(0,164,W,H-20-164,C_BK);
    tft.setTextSize(2); tft.setTextColor(c1);
    int tw=(int)strlen(line1)*12; tft.setCursor(W/2-tw/2,168); tft.print(line1);
    if (strlen(line2)>0) {
        tft.setTextSize(1); tft.setTextColor(c2);
        int tw2=(int)strlen(line2)*6; tft.setCursor(W/2-tw2/2,192); tft.print(line2);
    }
}
static uint8_t spinIdx=0; static uint32_t lastSpin=0;
void tickWifiSpinner() {
    uint32_t now=millis(); if (now-lastSpin<180) return; lastSpin=now;
    static const char* fr[]={" |"," /"," -"," \\"};
    tft.fillRect(W/2-6,72,12,12,C_BK);
    tft.setTextSize(1); tft.setTextColor(C_CY); tft.setCursor(W/2-3,74); tft.print(fr[spinIdx++%4]);
}

#define TUNE_BG_X    20
#define TUNE_BG_Y    90
#define TUNE_BG_W    (W-40)
#define TUNE_BG_H    44
#define TUNE_SAMPLE_MS  4000
#define TUNE_MAX_BARS   40
static int     tuneBarVals[TUNE_MAX_BARS];
static int     tuneBarCount=0;
static int32_t tuneMaxRMS=0;

void drawTuneScreen() {
    tft.fillScreen(C_BK);
    for (int i=0;i<40;i++) { int x=(i*173+11)%W, y=(i*107+7)%H; tft.drawPixel(x,y,C_DG); }
    tft.fillRect(0,0,W,36,C_CARD); tft.drawFastHLine(0,36,W,C_YL);
    tft.fillCircle(18,18,7,C_YL); tft.fillCircle(18,18,3,C_BK);
    tft.setTextSize(1); tft.setTextColor(C_WH); tft.setCursor(32,9); tft.print("BRONNY AI");
    tft.setTextColor(C_YL); tft.setCursor(110,9); tft.print("MIC CALIBRATION");
    tft.setTextColor(C_LG); tft.setCursor(32,22); tft.print("v6.1");
    tft.fillRoundRect(20,44,W-40,38,6,C_CARD); tft.drawRoundRect(20,44,W-40,38,6,C_YL);
    tft.setTextColor(C_WH); tft.setTextSize(1); tft.setCursor(28,50); tft.print("Measuring ambient noise...");
    tft.setTextColor(C_LG); tft.setCursor(28,64); tft.print("Stay quiet for 4 seconds");
    tft.drawRoundRect(TUNE_BG_X,TUNE_BG_Y,TUNE_BG_W,TUNE_BG_H,4,C_DG);
    tft.setTextColor(C_LG); tft.setTextSize(1); tft.setCursor(TUNE_BG_X+3,TUNE_BG_Y+4); tft.print("AMBIENT RMS");
    tft.fillRect(0,H-20,W,20,C_CARD); tft.drawFastHLine(0,H-20,W,C_DG);
    tft.setTextColor(C_LG); tft.setTextSize(1); tft.setCursor(6,H-13); tft.print("Auto-tune VAD threshold");
}
void drawTuneBar(int idx, int normH) {
    int barW=(TUNE_BG_W-8)/TUNE_MAX_BARS; if (barW<1) barW=1;
    int x=TUNE_BG_X+4+idx*barW, maxBH=TUNE_BG_H-18;
    int bh=max(1,normH*maxBH/100), y=TUNE_BG_Y+6+(maxBH-bh);
    tft.fillRect(x,TUNE_BG_Y+6,barW-1,maxBH,C_BK);
    uint16_t bc=(normH>60)?C_YL:(normH>30)?C_CY:C_DCY;
    tft.fillRect(x,y,barW-1,bh,bc);
}
void drawTuneProgress(int pct) {
    int px=TUNE_BG_X,pw=TUNE_BG_W,ph=8,py=TUNE_BG_Y+TUNE_BG_H+8;
    tft.fillRoundRect(px,py,pw,ph,3,0x0841);
    int fw=(int)((float)pw*pct/100.f);
    if (fw>4) { tft.fillRoundRect(px,py,fw,ph,3,C_DCY); if (fw>8) tft.fillRoundRect(px,py,fw-3,ph,3,C_YL); tft.drawFastHLine(px+2,py+1,fw-4,C_WH); }
}
void drawTuneResult(int thr) {
    tft.fillRect(0,152,W,H-20-152,C_BK);
    tft.fillRoundRect(20,154,W-40,50,8,C_CARD); tft.drawRoundRect(20,154,W-40,50,8,C_GR);
    tft.setTextColor(C_WH); tft.setTextSize(1); tft.setCursor(30,162); tft.print("Calibration complete!");
    tft.setTextColor(C_GR); tft.setTextSize(1); tft.setCursor(30,175); tft.print("VAD threshold set to:");
    tft.setTextColor(C_WH); tft.setTextSize(2);
    char buf[16]; snprintf(buf,16,"%d",thr); tft.setCursor(30,188); tft.print(buf);
    tft.setTextColor(C_CY); tft.setTextSize(1); tft.setCursor(30+(int)strlen(buf)*12+4,194); tft.print("(auto-tuned)");
}
void runVADAutoTune() {
    if (!micOk) return;
    drawTuneScreen();
    memset(tuneBarVals,0,sizeof(tuneBarVals)); tuneBarCount=0; tuneMaxRMS=0;
    { uint8_t discard[512]; uint32_t warmEnd=millis()+1000; while (millis()<warmEnd) { mic_stream.readBytes(discard,sizeof(discard)); yield(); } }
    int32_t rawBuf[256]; int32_t runningPeak=0;
    uint32_t sampleStart=millis(), nextBarTime=sampleStart;
    int barInterval=TUNE_SAMPLE_MS/TUNE_MAX_BARS;
    while (millis()-sampleStart<TUNE_SAMPLE_MS) {
        int rd=mic_stream.readBytes((uint8_t*)rawBuf,sizeof(rawBuf));
        int frames=rd/8;
        if (frames>0) {
            int32_t rms=filteredRMS(rawBuf,frames);
            if (rms>runningPeak) runningPeak=rms;
            if (rms>tuneMaxRMS) tuneMaxRMS=rms;
        }
        if (millis()>=nextBarTime && tuneBarCount<TUNE_MAX_BARS) {
            int normH=min(100,(int)(runningPeak*100/3000));
            tuneBarVals[tuneBarCount]=normH; drawTuneBar(tuneBarCount,normH);
            tuneBarCount++; runningPeak=0; nextBarTime+=barInterval;
        }
        drawTuneProgress((int)(millis()-sampleStart)*100/TUNE_SAMPLE_MS);
        yield();
    }
    drawTuneProgress(100);
    int newThr=(int)(tuneMaxRMS*2.5f);
    newThr=max(600,min(3500,newThr));
    VAD_THR=newThr;
    Serial.printf("[TUNE] peak=%d  VAD_THR=%d\n",(int)tuneMaxRMS,VAD_THR);
    drawTuneResult(newThr);
    jingleTune(); delay(1200);
}

// ============================================================
// STANDBY
// ============================================================
void enterStandby() {
    bronnyMode=MODE_STANDBY;
    setFaceStandby();
    setStatus("Standby | Hi Bot", C_GREY);
}
void exitStandby() {
    bronnyMode=MODE_ACTIVE; lastVoiceTime=millis();
    face.sleepLid=0.f; face.standbyMs=0;
    tickZzz(millis());
    setFaceSurprised(600);
    setStatus("Ready", C_CY);
    jingleWake();
}

// ============================================================
// CONVERSATION PIPELINE
// ============================================================
static bool     busy             = false;
static uint32_t vadCooldownUntil = 0;

void runConversation() {
    if (busy) return;
    busy = true;
    lastVoiceTime = millis();
    setFaceListen();
    setStatus("Listening...", C_GR);

    if (micOk) {
        uint8_t drain[512]; uint32_t de = millis() + 150;
        while (millis() < de) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
    }

    bool got = recordVAD(MAX_RECORD_MS, false);
    if (!got) { setFaceIdle(); setStatus("Ready", C_CY); busy = false; return; }

    lastVoiceTime = millis();
    setFaceThink();
    setStatus("Sending...", C_YL);
    WDT_FEED();

    char transcript[180] = "";
    bool ok = callAetherVoice(recBuf, recLen, transcript, sizeof(transcript));

    if (!ok || isNoise(String(transcript), recLen)) {
        setFaceIdle();
        setStatus("Ready", C_CY);
        busy = false;
        return;
    }

    if (transcript[0]) setStatus(transcript, C_CY);

    isSpeaking = true;
    setStatus("Speaking...", C_GR);
    startTalk();
    playMp3();
    stopTalk();

    setFaceHappy(1600);
    vadCooldownUntil = millis() + TTS_COOLDOWN_MS;

    if (micOk) {
        uint8_t drain[512]; uint32_t de = millis() + TTS_COOLDOWN_MS;
        while (millis() < de) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
    }
    vadCooldownUntil   = millis() + TTS_COOLDOWN_MS;
    lastConvEndTime    = lastVoiceTime = millis();
    setStatus("Ready", C_CY);
    busy = false;
}

// ============================================================
// SETUP
// ============================================================
void setup() {
    esp_task_wdt_deinit();

    Serial.begin(115200);
    delay(400);

    Serial.println("\n===== BRONNY AI v6.1 — AetherAI Edition =====");
    Serial.printf("[MEM] Free heap   : %u bytes\n", esp_get_free_heap_size());
    Serial.printf("[MEM] Free PSRAM  : %u bytes\n", heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    Serial.printf("[MEM] Min heap    : %u bytes\n", esp_get_minimum_free_heap_size());
    if (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) < 100000) {
        Serial.println("[WARN] Low PSRAM — audio buffers may fail!");
    }
    AudioToolsLogger.begin(Serial, AudioToolsLogLevel::Warning);
    delay(400);

    pinMode(PIN_BLK,OUTPUT); digitalWrite(PIN_BLK,HIGH);
    pinMode(PIN_PA, OUTPUT); digitalWrite(PIN_PA, LOW);

    tftSPI.begin(PIN_CLK,-1,PIN_MOSI,PIN_CS);
    tft.init(240,320); tft.setRotation(3); tft.fillScreen(C_BK);

    audioRestart();
    WDT_FEED();
    i2s.setVolume(VOL_MAIN);
    if (audioOk) { auto sc=sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }
    micInit();

    Serial.printf("[BOOT] SRAM before decoder: %u\n", esp_get_free_heap_size());
    mp3Decoder.begin();
    mp3_buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
    Serial.printf("[BOOT] SRAM after  decoder: %u  PSRAM left: %u\n",
                  esp_get_free_heap_size(),
                  heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    if (!mp3_buf) Serial.println("[BOOT] WARNING: mp3_buf alloc failed!");

    drawBootLogo();
    int bCX=W/2, bCY=H/2-32;
    playBootIntroAnim(bCX,bCY);
    drawBootBar(10); jingleBoot();
    drawBootBar(55); delay(150);
    drawBootBar(100); delay(300);

    drawWifiScreen();
    drawWifiStatus("Connecting...",C_YL);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    bool connected=false;
    uint32_t ws=millis();
    while (millis()-ws<18000) {
        if (WiFi.status()==WL_CONNECTED) { connected=true; break; }
        WDT_FEED();
        tickWifiSpinner(); yield();
    }
    if (connected) {
        char ipStr[32]; snprintf(ipStr,32,"%s",WiFi.localIP().toString().c_str());
        drawWifiStatus("Connected!",C_GR,ipStr,C_CY);
        jingleConnect(); delay(900);
    } else {
        drawWifiStatus("FAILED",C_RD,"Check SSID / Password",C_RD);
        jingleError(); delay(2000);
    }

    audioRestart(); WDT_FEED(); i2s.setVolume(VOL_MAIN);
    { auto sc=sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }
    runVADAutoTune();

    tft.fillScreen(C_BK);
    drawFaceBg();
    drawFace(true);
    drawIslandBar();
    jingleReady();

    if (connected) {
        jingleReady();
        setFaceHappy(2000);
        delay(400);
    }

    lastVoiceTime=lastConvEndTime=millis();
    setStatus("Ready",C_CY);
}

// ============================================================
// LOOP
// Serial: +/- VAD_THR  t=TTS test  w=standby  s=wake
//         m=mic peak   r=re-calibrate
// ============================================================
void loop() {
    uint32_t now=millis();
    animFace();
    if (faceRedraw) { drawFace(false); faceRedraw=false; }
    tickZzz(now);

    if (!busy && !isSpeaking && micOk && now>vadCooldownUntil) {
        int32_t sb[32];
        int rd=mic_stream.readBytes((uint8_t*)sb,sizeof(sb));
        int frames=rd/8;
        bool peak=false;
        for (int f=0;f<frames;f++) {
            if (abs(inmp441Sample(sb[f*2]))>VAD_THR) { peak=true; break; }
        }

        if (bronnyMode==MODE_ACTIVE) {
            if (lastConvEndTime>0 &&
                now-lastConvEndTime>STANDBY_TIMEOUT_MS &&
                now-lastVoiceTime >STANDBY_TIMEOUT_MS) {
                enterStandby();
            } else if (peak) {
                lastVoiceTime=now;
                runConversation();
            }
        } else {
            if (peak) {
                setStatus("Listening...",C_GREY);
                if (checkWakeWord()) {
                    exitStandby();
                    if (micOk) {
                        uint8_t drain[512]; uint32_t de=millis()+300;
                        while (millis()<de) { mic_stream.readBytes(drain,sizeof(drain)); yield(); }
                    }
                    runConversation();
                } else {
                    setStatus("Standby | Hi Bot",C_GREY);
                }
            }
        }
    }

    if (Serial.available()) {
        char c=Serial.read();
        if      (c=='+') { VAD_THR=min(8000,VAD_THR+100); Serial.printf("VAD_THR=%d\n",VAD_THR); }
        else if (c=='-') { VAD_THR=max(300, VAD_THR-100); Serial.printf("VAD_THR=%d\n",VAD_THR); }
        else if (c=='t') { setStatus("Test...",C_YL); if(callAetherVoice(recBuf,max(recLen,1600),nullptr,0)){ startTalk(); playMp3(); stopTalk(); setFaceHappy(1600); } }
        else if (c=='w') { enterStandby(); Serial.println("[Standby ON]"); }
        else if (c=='s') { exitStandby();  Serial.println("[Standby OFF]"); }
        else if (c=='r') {
            Serial.println("[Re-running VAD calibration]");
            WDT_FEED();
            runVADAutoTune();
            tft.fillScreen(C_BK); drawFaceBg(); drawFace(true); drawIslandBar();
            Serial.printf("VAD_THR=%d\n",VAD_THR);
        }
        else if (c=='m') {
            int32_t tb[256]; int pk=0;
            for (int p=0;p<20;p++) {
                mic_stream.readBytes((uint8_t*)tb,sizeof(tb));
                for (int f=0;f<32;f++) { int v=abs(inmp441Sample(tb[f*2])); if(v>pk) pk=v; }
                yield();
            }
            Serial.printf("[MIC] peak=%d  THR=%d\n",pk,VAD_THR);
        }
    }
    yield();
}
