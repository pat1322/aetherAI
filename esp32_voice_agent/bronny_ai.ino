/*
 * BRONNY AI v5.0 - AetherAI Edition
 * by Patrick Perez
 *
 * Based on v4.8 (face, animations, standby, boot screen all retained)
 *
 * v5.0 changes vs v4.8:
 *   - Removed VAD auto-calibration. VAD_THR fixed at 2000.
 *   - Removed direct Qwen chat + TTS calls.
 *   - Pipeline: recordVAD -> Qwen STT -> Railway /voice/text -> MP3 -> play
 *   - Railway credentials from voice_config.h (AETHER_URL + AETHER_API_KEY)
 *   - MP3 decoded via CodecMP3Helix and played through ES8311
 *
 * Hardware:
 *   Board   : ESP32-S3 Dev Module
 *   Codec   : ES8311 (I2C addr 0x18)
 *   Mic     : INMP441 (I2S port 1, GPIOs 4/5/6)
 *   Display : ST7789 320x240 (HSPI)
 *
 * Libraries:
 *   arduino-audio-tools + arduino-audio-driver by pschatzmann
 *   Adafruit ST7789 + Adafruit GFX
 *   ArduinoJson by Benoit Blanchon
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
#include <ArduinoJson.h>
#include <math.h>

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
// CONFIG
// ============================================================
const char* WIFI_SSID = WIFI_SSID_CFG;
const char* WIFI_PASS = WIFI_PASS_CFG;
const char* QWEN_KEY  = QWEN_API_KEY;

#define VOL_MAIN    0.50f
#define VOL_JINGLE  0.25f

static int VAD_THR = 2000;     // fixed - no auto-calibration

// Timing
#define VAD_SILENCE_MS      1500
#define VAD_SILENCE_WAKE_MS  600
#define MAX_RECORD_MS       7000
#define WAKE_RECORD_MS      2000
#define PRE_ROLL_MS          300
#define STANDBY_TIMEOUT_MS  4000
#define GLITCH_CLIP_RATIO   0.25f
#define TTS_COOLDOWN_MS      800

#define MP3_MAX_BYTES  (320 * 1024)

static volatile bool isSpeaking = false;

// Standby
enum BronnyMode { MODE_ACTIVE, MODE_STANDBY };
static BronnyMode bronnyMode      = MODE_ACTIVE;
static uint32_t   lastVoiceTime   = 0;
static uint32_t   lastConvEndTime = 0;

// ============================================================
// ENDPOINTS
// ============================================================
// STT only - chat+TTS now handled by Railway
const char* URL_CHAT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions";

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

// Island bar
#define ISL_W   138
#define ISL_H    15
#define ISL_X   ((W - ISL_W) / 2)
#define ISL_Y   (H - ISL_H - 5)
#define ISL_R    7

// Face geometry (unchanged from v4.8)
#define FCX    160
#define FCY     88
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
#define MYO      58
#define SMILE_R     22
#define SMILE_TH     7
#define LOOK_X_RANGE  12
#define LOOK_Y_RANGE   6

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

MP3DecoderHelix mp3Decoder;
static uint8_t* mp3Buf = nullptr;
static size_t   mp3Len = 0;

static bool audioOk   = false;
static bool micOk     = false;
static bool inTtsMode = false;

// ============================================================
// GLITCH-FILTERED RMS
// ============================================================
static int32_t lastValidRMS = 0;

static int32_t filteredRMS(const int32_t* rawFrames, int frameCount) {
    if (frameCount <= 0) return lastValidRMS;
    int     clipped = 0;
    int64_t sq      = 0;
    int     n       = frameCount;
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

static inline int16_t inmp441Sample(int32_t raw) {
    return (int16_t)(raw >> 14);
}

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
        uint8_t tmp[512];
        uint32_t e = millis() + 300;
        while (millis() < e) { mic_stream.readBytes(tmp, sizeof(tmp)); yield(); }
    }
}

void audioInitRec() {
    if (inTtsMode || !audioOk) {
        audioPinsSetup();
        auto cfg = i2s.defaultConfig(TX_MODE);
        cfg.copyFrom(ainf_rec);
        cfg.output_device = DAC_OUTPUT_ALL;
        audioOk   = i2s.begin(cfg);
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
        audioPinsSetup();
        auto cfg = i2s.defaultConfig(TX_MODE);
        cfg.copyFrom(ainf_tts);
        cfg.output_device = DAC_OUTPUT_ALL;
        audioOk   = i2s.begin(cfg);
        i2s.setVolume(VOL_MAIN);
        inTtsMode = true;
    }
}

void audioRestart() {
    i2s.end(); delay(150);
    audioOk = false; inTtsMode = false;
    Wire.end(); delay(60);
    Wire.begin(PIN_SDA, PIN_SCL, 100000);
    brdPins.addI2C(PinFunction::CODEC, PIN_SCL, PIN_SDA, ES_ADDR, 100000, Wire);
    brdPins.addI2S(PinFunction::CODEC, PIN_MCLK, PIN_BCLK, PIN_WS, PIN_DOUT, PIN_DIN);
    brdPins.addPin(PinFunction::PA, PIN_PA, PinLogic::Output);
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

// Tone helpers (kept for jingles)
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
    playTone(1318,200); playSil(150);
    i2s.setVolume(VOL_MAIN);
}
void jingleError() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(300,200); playSil(80); playTone(220,350); playSil(200);
    i2s.setVolume(VOL_MAIN);
}
void jingleReady() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(880,80); playSil(30); playTone(1318,80); playSil(30);
    playTone(1760,200); playSil(150);
    i2s.setVolume(VOL_MAIN);
}
void jingleWake() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(660,80); playSil(20); playTone(1100,120); playSil(80);
    i2s.setVolume(VOL_MAIN);
}

// ============================================================
// BASE-64  (used for STT WAV encoding)
// ============================================================
static const char B64T[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

size_t b64Enc(const uint8_t* src, size_t sLen, char* dst) {
    size_t o = 0;
    for (size_t i = 0; i < sLen; i += 3) {
        uint32_t b = (uint32_t)src[i] << 16;
        if (i+1 < sLen) b |= (uint32_t)src[i+1] << 8;
        if (i+2 < sLen) b |= (uint32_t)src[i+2];
        dst[o++] = B64T[(b>>18)&63]; dst[o++] = B64T[(b>>12)&63];
        dst[o++] = (i+1<sLen) ? B64T[(b>>6)&63] : '=';
        dst[o++] = (i+2<sLen) ? B64T[b&63]      : '=';
    }
    dst[o] = '\0'; return o;
}

// ============================================================
// WAV HEADER
// ============================================================
void wavHeader(uint8_t* h, uint32_t pcmB) {
    auto le4=[&](int o,uint32_t v){h[o]=v;h[o+1]=v>>8;h[o+2]=v>>16;h[o+3]=v>>24;};
    auto le2=[&](int o,uint16_t v){h[o]=v;h[o+1]=v>>8;};
    memcpy(h,"RIFF",4); le4(4,pcmB+36); memcpy(h+8,"WAVEfmt ",8);
    le4(16,16); le2(20,1); le2(22,1); le4(24,16000);
    le4(28,32000); le2(32,2); le2(34,16);
    memcpy(h+36,"data",4); le4(40,pcmB);
}

// ============================================================
// JSON ESCAPE
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
        else if (c < 0x20)  { }
        else                o += (char)c;
    }
    return o;
}

// ============================================================
// HTTP HELPERS
// ============================================================
static char authBuf[80];
void makeAuth() { snprintf(authBuf, sizeof(authBuf), "Bearer %s", QWEN_KEY); }

static void ensureWifi() {
    if (WiFi.status() != WL_CONNECTED) {
        WiFi.reconnect();
        uint32_t t = millis();
        while (WiFi.status() != WL_CONNECTED && millis()-t < 8000)
            { delay(300); yield(); }
    }
}

String httpPost(const char* url, String body) {
    ensureWifi();
    if (WiFi.status() != WL_CONNECTED) return "";
    WiFiClientSecure cli; cli.setInsecure(); cli.setTimeout(40);
    HTTPClient http; http.setTimeout(40000);
    if (!http.begin(cli, url)) return "";
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", authBuf);
    int code = http.POST(body);
    String ret = "";
    if (code == 200 || code == 201) ret = http.getString();
    http.end();
    return ret;
}

// ============================================================
// STT  (Qwen3-asr-flash - unchanged from v4.8)
// ============================================================
String callSTT(int16_t* pcm, int samples) {
    if (!pcm || samples <= 0) return "";
    uint32_t pcmB = (uint32_t)samples * 2, wavB = 44 + pcmB;
    uint8_t* wavBuf = (uint8_t*)heap_caps_malloc(wavB, MALLOC_CAP_SPIRAM);
    if (!wavBuf) wavBuf = (uint8_t*)malloc(wavB);
    if (!wavBuf) return "";
    wavHeader(wavBuf, pcmB);
    memcpy(wavBuf + 44, pcm, pcmB);
    size_t b64Cap = ((wavB + 2) / 3) * 4 + 8;
    char* b64Buf = (char*)heap_caps_malloc(b64Cap, MALLOC_CAP_SPIRAM);
    if (!b64Buf) b64Buf = (char*)malloc(b64Cap);
    if (!b64Buf) { free(wavBuf); return ""; }
    b64Enc(wavBuf, wavB, b64Buf);
    free(wavBuf);
    String body;
    body.reserve(strlen(b64Buf) + 300);
    body = "{\"model\":\"qwen3-asr-flash\","
           "\"messages\":[{\"role\":\"user\",\"content\":["
           "{\"type\":\"input_audio\",\"input_audio\":"
           "{\"data\":\"data:audio/wav;base64,";
    body += b64Buf;
    free(b64Buf);
    body += "\"}}]}],"
            "\"asr_options\":{\"enable_itn\":false,\"language\":\"en\"}}";
    String resp = httpPost(URL_CHAT, body);
    body = "";
    if (resp.isEmpty()) return "";
    DynamicJsonDocument doc(4096);
    if (deserializeJson(doc, resp)) return "";
    const char* txt = doc["choices"][0]["message"]["content"];
    if (!txt) return "";
    String t = String(txt); t.trim();
    return t;
}

// ============================================================
// RAILWAY  POST /voice/text -> MP3
// Sends the transcript text to Railway, receives MP3 audio back.
// Stores MP3 in PSRAM buffer (mp3Buf / mp3Len).
// ============================================================
static String baseUrl() {
    String u = String(AETHER_URL);
    while (u.endsWith("/")) u.remove(u.length() - 1);
    return u;
}

bool callRailway(const String& transcript) {
    if (transcript.isEmpty()) return false;
    ensureWifi();
    if (WiFi.status() != WL_CONNECTED) return false;

    // Allocate MP3 buffer in PSRAM on first call
    if (!mp3Buf) {
        mp3Buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
        if (!mp3Buf) { Serial.println("[Rail] mp3Buf alloc FAILED"); return false; }
    }

    // Build JSON body
    String body = "{\"text\":\"" + jEsc(transcript) + "\"}";
    String url  = baseUrl() + "/voice/text";
    Serial.printf("[Rail] POST %s  text='%s'\n", url.c_str(), transcript.c_str());

    bool success = false;

    for (int attempt = 1; attempt <= 2 && !success; attempt++) {
        if (attempt > 1) { Serial.println("[Rail] retry 2/2..."); delay(2000); }

        WiFiClientSecure cli; cli.setInsecure(); cli.setConnectionTimeout(20000);
        HTTPClient http;
        http.begin(cli, url);
        http.setTimeout(45000);
        http.addHeader("Content-Type", "application/json");
        http.addHeader("X-Api-Key", AETHER_API_KEY);

        int code = http.POST(body);
        Serial.printf("[Rail] HTTP %d\n", code);

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
                } else {
                    delay(2);
                }
                yield();
            }
            mp3Len  = got;
            success = (got > 0);
            Serial.printf("[Rail] MP3 received %u bytes\n", (unsigned)mp3Len);
        } else {
            Serial.printf("[Rail] Error response: %s\n", http.getString().substring(0, 200).c_str());
        }
        http.end();
        yield();
    }
    return success;
}

// ============================================================
// MP3 PLAYBACK via ES8311
// Decodes mp3Buf using CodecMP3Helix and plays through ES8311.
// Face animation runs during playback (lip-sync via FS_TALKING state).
// ============================================================
void playMp3() {
    if (!mp3Len || !mp3Buf) { Serial.println("[Play] No MP3 data"); return; }
    Serial.printf("[Play] Playing %u bytes\n", (unsigned)mp3Len);

    // Stop mic, switch codec to TTS sample rate (24kHz)
    mic_stream.end();
    micOk = false;
    delay(60);
    audioInitTTS();
    delay(80);

    // Brief silence so codec PLL locks before first audio
    { int16_t sil[512] = {}; i2s.write((uint8_t*)sil, sizeof(sil)); delay(200); }

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

    // Trailing silence to avoid speaker click
    { int16_t sil[128] = {}; i2s.write((uint8_t*)sil, sizeof(sil)); }
    delay(60);

    Serial.println("[Play] Done");

    // Restore mic
    audioInitRec();
    micInit();
    // Drain echo picked up during playback
    if (micOk) {
        uint8_t drain[512];
        uint32_t e = millis() + 300;
        while (millis() < e) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
    }
}

// ============================================================
// RECORDING (unchanged from v4.8)
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
    int16_t* preRoll = (int16_t*)malloc(preRollSamp * 2);
    int prHead = 0;
    bool prFull = false;
    bool speaking = false;
    uint32_t silStart = 0;
    uint32_t start    = millis();
    int32_t  rawBuf[256];
    while (millis() - start < (uint32_t)maxMs && recLen < maxSamp) {
        int rd     = mic_stream.readBytes((uint8_t*)rawBuf, sizeof(rawBuf));
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
                if (millis() - silStart >= silMs) break;
            } else silStart = 0;
        }
        yield();
    }
    if (preRoll) free(preRoll);
    return (recLen > 16000 / 4) && (recPeak > (int32_t)eGate);
}

// ============================================================
// WAKE WORD (unchanged from v4.8)
// ============================================================
bool checkWakeWord() {
    if (!micOk) return false;
    bool got = recordVAD(WAKE_RECORD_MS, true);
    if (!got) return false;
    String t = callSTT(recBuf, recLen);
    if (t.isEmpty()) return false;
    String lower = t; lower.toLowerCase();
    return lower.indexOf("hi bot")   >= 0 ||
           lower.indexOf("hey bot")  >= 0 ||
           lower.indexOf("hi, bot")  >= 0 ||
           lower.indexOf("hey, bot") >= 0 ||
           lower.indexOf("hibot")    >= 0 ||
           lower.indexOf("heybot")   >= 0;
}

// ============================================================
// NOISE FILTER (unchanged from v4.8)
// ============================================================
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
    int spaceIdx = lower.indexOf(' ');
    if (spaceIdx > 0) {
        String firstWord = lower.substring(0, spaceIdx);
        bool allSame = true; int wi = 0;
        while (wi < (int)lower.length()) {
            int sp = lower.indexOf(' ', wi);
            String w = (sp < 0) ? lower.substring(wi) : lower.substring(wi, sp);
            w.trim();
            if (w.length() > 0 && w != firstWord) { allSame = false; break; }
            wi = (sp < 0) ? lower.length() : sp + 1;
        }
        if (allSame && firstWord.length() <= 6) return true;
    }
    return false;
}

// ============================================================
// ISLAND BAR (unchanged from v4.8)
// ============================================================
static String   islandText  = "Ready";
static uint16_t islandColor = C_CY;

static uint16_t dim10(uint16_t c) {
    uint8_t r = (c >> 11) & 0x1F;
    uint8_t g = (c >>  5) & 0x3F;
    uint8_t b = (c      ) & 0x1F;
    r = (uint8_t)(r * 10 / 100);
    g = (uint8_t)(g * 10 / 100);
    b = (uint8_t)(b * 10 / 100);
    return (uint16_t)((r << 11) | (g << 5) | b);
}

void drawIslandBar() {
    tft.fillRect(ISL_X - 2, ISL_Y - 2, ISL_W + 4, ISL_H + 4, C_BK);
    uint16_t dimC   = dim10(islandColor);
    uint16_t dimCx2 = dim10(dim10(islandColor));
    tft.drawRoundRect(ISL_X - 1, ISL_Y - 1, ISL_W + 2, ISL_H + 2, ISL_R + 1, dimCx2);
    tft.fillRoundRect(ISL_X, ISL_Y, ISL_W, ISL_H, ISL_R, C_BK);
    tft.drawRoundRect(ISL_X, ISL_Y, ISL_W, ISL_H, ISL_R, dimC);
    tft.fillCircle(ISL_X + 9, ISL_Y + ISL_H / 2, 2, dimC);
    tft.setTextSize(1);
    tft.setTextColor(dimC);
    int tw = (int)islandText.length() * 6;
    int tx = ISL_X + (ISL_W - tw) / 2 + 5;
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
// FACE STATE MACHINE (unchanged from v4.8)
// ============================================================
enum FaceState {
    FS_IDLE, FS_TALKING, FS_LISTEN, FS_THINK,
    FS_STANDBY, FS_SLEEP, FS_HAPPY, FS_SURPRISED
};

struct FaceData {
    FaceState state    = FS_IDLE;
    float  bobPh       = 0.f;
    int8_t bobY        = 0;
    int8_t pBobY       = 0;
    bool   blink       = false;
    int    blinkF      = 0;
    float  talkPh      = 0.f;
    float  mOpen       = 0.f;
    float  pOpen       = 0.f;
    float  listenPulse = 0.f;
    float  thinkSq     = 0.f;
    float  happyPh     = 0.f;
    float  surpriseScale = 1.f;
    float  eyeScaleX   = 1.0f;
    float  eyeScaleY   = 1.0f;
    uint32_t emotionTimer      = 0;
    uint32_t standbyEnteredMs  = 0;
    float  lookX = 0.f, lookY = 0.f;
    float  tLookX = 0.f, tLookY = 0.f;
    uint32_t nextLookMs = 0;
    float  sleepLid    = 0.f;
    float   prevBlink  = 1.0f;
    float   prevSX     = 1.0f;
    float   prevSY     = 1.0f;
    float   prevSquint = 0.0f;
    int8_t  prevLookXi = 0;
    int8_t  prevLookYi = 0;
    bool    prevSmile  = false;
} face;

bool            faceRedraw   = false;
static uint32_t lastBlink    = 0;
static uint32_t nextBlink    = 3200;
static uint32_t lastFaceAnim = 0;

struct ZzzState {
    bool    active       = false;
    uint32_t startMs     = 0;
    uint32_t nextSpawnMs = 0;
    int8_t  prevX[3]     = {0,0,0};
    int8_t  prevY[3]     = {0,0,0};
    int     prevAbsY[3]  = {0,0,0};
    int     prevAbsX[3]  = {0,0,0};
    bool    prevDrawn[3] = {false,false,false};
} zzz;

static void drawSmile(int cx, int cy) {
    tft.fillCircle(cx, cy, SMILE_R, C_WH);
    tft.fillRect(cx - SMILE_R - 1, cy - SMILE_R - 1,
                 (SMILE_R + 1) * 2 + 2, SMILE_R + 2, C_BK);
    int innerR = SMILE_R - SMILE_TH;
    if (innerR > 1) {
        tft.fillCircle(cx, cy, innerR, C_BK);
        tft.fillRect(cx - innerR - 1, cy - innerR - 1,
                     (innerR + 1) * 2 + 2, innerR + 2, C_BK);
    }
}

static void eraseSmile(int cx, int cy) {
    tft.fillRect(cx - SMILE_R - 2, cy - 2,
                 (SMILE_R + 2) * 2, SMILE_R + 4, C_BK);
}

static void drawOneEye(int cx, int cy, float openFrac, float squint,
                       float scaleX, float scaleY) {
    int ew = max(8, (int)(EW * scaleX));
    int eh = max(2, (int)(EH * openFrac * scaleY * (1.f - squint * 0.55f)));
    int r  = min(ER, min(ew / 2, eh / 2));
    tft.fillRoundRect(cx - ew/2, cy - eh/2, ew, eh, r, C_WH);
}

static void drawMouth(int cx, int cy, float openFrac) {
    int mh = MH_CL + (int)((MH_OP - MH_CL) * openFrac);
    int r  = min(MR, mh / 2);
    tft.fillRoundRect(cx - MW/2, cy - mh/2, MW, mh, r, C_WH);
}

void drawFaceBg() { tft.fillRect(0, 0, W, ISL_Y, C_BK); }

static void eraseEyeUnion(int newCX, int newCY, int newEW, int newEH,
                           int oldCX, int oldCY, int oldEW, int oldEH) {
    int x1 = min(oldCX - oldEW/2, newCX - newEW/2) - 1;
    int y1 = min(oldCY - oldEH/2, newCY - newEH/2) - 1;
    int x2 = max(oldCX + oldEW/2, newCX + newEW/2) + 1;
    int y2 = max(oldCY + oldEH/2, newCY + newEH/2) + 1;
    y1 = max(y1, 0); y2 = min(y2, ISL_Y - 1);
    x1 = max(x1, 0); x2 = min(x2, W - 1);
    if (x2 > x1 && y2 > y1)
        tft.fillRect(x1, y1, x2 - x1, y2 - y1, C_BK);
}

void drawFace(bool full) {
    int   by  = face.bobY;
    int   lxi = (int)(face.lookX * LOOK_X_RANGE);
    int   lyi = (int)(face.lookY * LOOK_Y_RANGE);
    int   lex = FCX - ESEP / 2 + lxi;
    int   rex = FCX + ESEP / 2 + lxi;
    int   ey  = FCY + EYO + by + lyi;
    int   my  = FCY + MYO + by;

    float blinkFrac = 1.f;
    if (face.blink) {
        int bf = face.blinkF;
        blinkFrac = (bf <= 4) ? (1.f - bf / 4.f) : ((bf - 4) / 5.f);
        blinkFrac = constrain(blinkFrac, 0.f, 1.f);
    }
    if (face.state == FS_SLEEP) {
        float maxOpen = 0.38f - face.sleepLid * 0.22f;
        blinkFrac = min(blinkFrac, maxOpen);
    }

    float squint = 0.f;
    if (face.state == FS_THINK) squint = face.thinkSq;
    if (face.state == FS_SLEEP) squint = 0.45f + face.sleepLid * 0.2f;

    float sx = face.eyeScaleX;
    float sy = face.eyeScaleY;
    if (face.state == FS_SURPRISED) { sx = face.surpriseScale; sy = face.surpriseScale; }

    int oldLex = FCX - ESEP/2 + (int)face.prevLookXi;
    int oldRex = FCX + ESEP/2 + (int)face.prevLookXi;
    int oldEy  = FCY + EYO + (int)face.pBobY + (int)face.prevLookYi;
    int oldEW2 = max(8, (int)(EW * face.prevSX));
    int oldEH2 = max(2, (int)(EH * face.prevBlink * face.prevSY
                               * (1.f - face.prevSquint * 0.55f)));
    int newEW2 = max(8, (int)(EW * sx));
    int newEH2 = max(2, (int)(EH * blinkFrac * sy * (1.f - squint * 0.55f)));

    bool posChg   = (by != face.pBobY || lxi != (int)face.prevLookXi
                                       || lyi != (int)face.prevLookYi);
    bool shapeChg = (fabsf(blinkFrac - face.prevBlink) > 0.03f
                  || fabsf(sx - face.prevSX) > 0.02f
                  || fabsf(squint - face.prevSquint) > 0.03f);
    bool eyeChg   = full || posChg || shapeChg;

    bool isTalking = (face.state == FS_TALKING);
    bool showSmile = !isTalking && (face.state != FS_SLEEP);
    bool mouthChg  = full
                  || (showSmile != face.prevSmile)
                  || (posChg && (isTalking || face.prevSmile))
                  || (isTalking && fabsf(face.mOpen - face.pOpen) > 0.015f)
                  || (isTalking && !face.prevSmile && face.pOpen == 0.f);

    if (eyeChg) {
        if (!full) {
            eraseEyeUnion(lex, ey, newEW2, newEH2, oldLex, oldEy, oldEW2, oldEH2);
            eraseEyeUnion(rex, ey, newEW2, newEH2, oldRex, oldEy, oldEW2, oldEH2);
        }
        drawOneEye(lex, ey, blinkFrac, squint, sx, sy);
        drawOneEye(rex, ey, blinkFrac, squint, sx, sy);
        face.prevBlink  = blinkFrac;
        face.prevSX     = sx;
        face.prevSY     = sy;
        face.prevSquint = squint;
        face.prevLookXi = (int8_t)lxi;
        face.prevLookYi = (int8_t)lyi;
    }

    if (mouthChg) {
        if (!full) {
            int oldMy = FCY + MYO + (int)face.pBobY;
            tft.fillRect(FCX - MW/2 - 2, oldMy - MH_OP/2 - 2,
                         MW + 4, MH_OP + SMILE_R + 8, C_BK);
        }
        if (isTalking) {
            drawMouth(FCX, my, face.mOpen);
            face.pOpen = face.mOpen;
        } else if (showSmile) {
            drawSmile(FCX, my);
            face.pOpen = 0.f;
        } else {
            face.pOpen = 0.f;
        }
        face.prevSmile = showSmile && !isTalking;
    }

    face.pBobY = (int8_t)by;
}

void animFace() {
    uint32_t now = millis();
    if (now - lastFaceAnim < 16) return;
    lastFaceAnim = now;
    bool ch = false;

    if (face.emotionTimer > 0 && now > face.emotionTimer) {
        face.emotionTimer = 0;
        face.state = FS_IDLE;
        face.sleepLid = 0.f;
        ch = true;
    }

    if (face.state == FS_STANDBY) {
        if (face.standbyEnteredMs == 0) face.standbyEnteredMs = now;
        if (now - face.standbyEnteredMs > 4000) {
            face.state = FS_SLEEP;
            face.sleepLid = 0.f;
            ch = true;
        }
    }

    float bobSpeed = (face.state == FS_SLEEP)   ? 0.004f
                   : (face.state == FS_STANDBY) ? 0.007f
                   :                              0.020f;
    int   bobAmp   = (face.state == FS_SLEEP || face.state == FS_STANDBY) ? 2 : BOB;
    if (face.state != FS_LISTEN) {
        face.bobPh += bobSpeed;
        if (face.bobPh > 6.2832f) face.bobPh -= 6.2832f;
        int8_t nb = (int8_t)roundf(sinf(face.bobPh) * bobAmp);
        if (nb != face.bobY) { face.bobY = nb; ch = true; }
    }

    if (face.state != FS_SURPRISED && face.state != FS_SLEEP) {
        float bScale = 1.f + sinf(face.bobPh * 0.5f) * 0.04f;
        if (fabsf(bScale - face.eyeScaleX) > 0.004f) {
            face.eyeScaleX = bScale;
            face.eyeScaleY = 2.f - bScale;
            ch = true;
        }
    }

    bool canLook = (face.state == FS_IDLE || face.state == FS_HAPPY
                 || face.state == FS_STANDBY || face.state == FS_SLEEP);
    if (canLook) {
        if (now >= face.nextLookMs) {
            float rng = (face.state == FS_SLEEP || face.state == FS_STANDBY) ? 0.45f : 1.0f;
            face.tLookX = ((float)(random(7)) / 3.f - 1.f) * rng;
            face.tLookY = ((float)(random(5)) / 2.f - 1.f) * rng * 0.6f;
            if (random(5) == 0) { face.tLookX = 0.f; face.tLookY = 0.f; }
            uint32_t holdMs = (face.state == FS_SLEEP) ? 3000 + random(3000)
                                                        : 600  + random(2000);
            face.nextLookMs = now + holdMs;
        }
        float spd = (face.state == FS_SLEEP) ? 0.025f : 0.07f;
        face.lookX += (face.tLookX - face.lookX) * spd;
        face.lookY += (face.tLookY - face.lookY) * spd;
        if (fabsf(face.lookX - face.tLookX) > 0.01f
         || fabsf(face.lookY - face.tLookY) > 0.01f) ch = true;
    } else {
        face.lookX *= 0.85f;
        face.lookY *= 0.85f;
        if (fabsf(face.lookX) > 0.01f || fabsf(face.lookY) > 0.01f) ch = true;
    }

    if (face.state == FS_THINK) {
        face.lookX += (0.55f - face.lookX) * 0.06f;
        face.lookY += (-0.5f - face.lookY) * 0.06f;
        ch = true;
    }

    bool canBlink = (face.state == FS_IDLE || face.state == FS_TALKING
                  || face.state == FS_STANDBY || face.state == FS_HAPPY
                  || face.state == FS_SLEEP);
    uint32_t blinkGap = (face.state == FS_SLEEP)   ? nextBlink + 6000
                      : (face.state == FS_STANDBY) ? nextBlink + 4000
                      :                              nextBlink;
    if (canBlink && !face.blink && now - lastBlink > blinkGap) {
        face.blink = true; face.blinkF = 0;
        nextBlink = 2200 + (uint32_t)random(2800);
    }
    if (face.blink) {
        int maxBF = (face.state == FS_SLEEP) ? 14 : 9;
        face.blinkF++;
        if (face.blinkF >= maxBF) { face.blink = false; face.blinkF = 0; lastBlink = now; }
        ch = true;
    }

    switch (face.state) {
        case FS_TALKING: {
            face.talkPh += 0.38f;
            if (face.talkPh > 6.2832f) face.talkPh -= 6.2832f;
            float jaw     = sinf(face.talkPh) * 0.70f;
            float flutter = sinf(face.talkPh * 2.3f) * 0.18f;
            float t = constrain(0.30f + jaw + flutter, 0.f, 1.f);
            if (fabsf(t - face.mOpen) > 0.015f) { face.mOpen = t; ch = true; }
            float eyePop = 1.f + fabsf(jaw) * 0.06f;
            if (fabsf(eyePop - face.eyeScaleY) > 0.01f) { face.eyeScaleY = eyePop; ch = true; }
            break;
        }
        case FS_LISTEN: {
            face.listenPulse += 0.07f;
            if (face.listenPulse > 6.2832f) face.listenPulse -= 6.2832f;
            face.lookX += (0.0f - face.lookX) * 0.05f;
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
            float hBounce = 1.f + sinf(face.happyPh) * 0.07f;
            if (fabsf(hBounce - face.eyeScaleY) > 0.01f) { face.eyeScaleY = hBounce; ch = true; }
            break;
        }
        case FS_SURPRISED: {
            float target = 1.30f;
            if (fabsf(face.surpriseScale - target) > 0.01f) {
                face.surpriseScale += (target - face.surpriseScale) * 0.18f;
                ch = true;
            }
            if (face.mOpen < 0.90f) { face.mOpen += 0.12f; ch = true; }
            break;
        }
        case FS_SLEEP: {
            if (face.sleepLid < 1.0f) { face.sleepLid += 0.008f; ch = true; }
            else face.sleepLid = 1.0f;
            face.eyeScaleX = 1.f; face.eyeScaleY = 1.f;
            break;
        }
        case FS_STANDBY:
            if (face.mOpen > 0.01f) { face.mOpen *= 0.92f; ch = true; }
            else face.mOpen = 0.f;
            break;
        default: break;
    }

    if (ch) faceRedraw = true;
}

void setFaceIdle() {
    face.state = FS_IDLE; face.thinkSq = 0.f; face.mOpen = 0.f;
    face.surpriseScale = 1.f; face.emotionTimer = 0;
    face.sleepLid = 0.f; face.standbyEnteredMs = 0;
    faceRedraw = true;
}
void setFaceTalk() {
    face.state = FS_TALKING; face.talkPh = 0.f;
    face.thinkSq = 0.f; face.emotionTimer = 0;
    face.sleepLid = 0.f;
    faceRedraw = true;
}
void setFaceListen() {
    face.state = FS_LISTEN; face.listenPulse = 0.f;
    face.bobPh = 0.f; face.bobY = 0; face.emotionTimer = 0;
    face.sleepLid = 0.f;
    faceRedraw = true;
}
void setFaceThink() {
    face.state = FS_THINK; face.thinkSq = 0.f;
    face.emotionTimer = 0; face.sleepLid = 0.f;
    faceRedraw = true;
}
void setFaceStandby() {
    face.state = FS_STANDBY; face.mOpen = 0.f;
    face.emotionTimer = 0; face.standbyEnteredMs = 0;
    face.sleepLid = 0.f;
    faceRedraw = true;
}
void setFaceHappy(uint32_t ms = 1800) {
    face.state        = FS_HAPPY;
    face.happyPh      = 0.f;
    face.mOpen        = 0.f;
    face.emotionTimer = millis() + ms;
    face.sleepLid     = 0.f;
    faceRedraw = true;
}
void setFaceSurprised(uint32_t ms = 700) {
    face.state         = FS_SURPRISED;
    face.surpriseScale = 1.0f;
    face.mOpen         = 0.2f;
    face.emotionTimer  = millis() + ms;
    face.sleepLid      = 0.f;
    faceRedraw = true;
}

void startTalk() { setFaceTalk(); }
void stopTalk()  { setFaceIdle(); }

// ZZZ animation (unchanged from v4.8)
void tickZzz(uint32_t now) {
    bool isSleep = (face.state == FS_SLEEP);
    if (!isSleep) {
        for (int i = 0; i < 3; i++) {
            if (zzz.prevDrawn[i]) {
                tft.setTextSize(i == 2 ? 2 : 1);
                tft.fillRect(zzz.prevAbsX[i] - 1, zzz.prevAbsY[i] - 1,
                             (i == 2 ? 12 : 7) + 2, (i == 2 ? 14 : 8) + 2, C_BK);
                zzz.prevDrawn[i] = false;
            }
        }
        zzz.active = false;
        zzz.nextSpawnMs = now + 1000;
        return;
    }
    if (!zzz.active) {
        if (now < zzz.nextSpawnMs) return;
        zzz.active  = true;
        zzz.startMs = now;
    }
    uint32_t elapsed  = now - zzz.startMs;
    uint32_t totalDur = 4200;
    if (elapsed >= totalDur) {
        for (int i = 0; i < 3; i++) {
            if (zzz.prevDrawn[i]) {
                tft.fillRect(zzz.prevAbsX[i] - 1, zzz.prevAbsY[i] - 1,
                             (i == 2 ? 14 : 8), (i == 2 ? 16 : 10), C_BK);
                zzz.prevDrawn[i] = false;
            }
        }
        zzz.active = false;
        zzz.nextSpawnMs = now + 1000 + random(800);
        return;
    }
    int baseX = FCX + ESEP/2 + EW/2 - 14;
    int baseY = FCY + EYO - EH/2 - 6;
    for (int i = 0; i < 3; i++) {
        uint32_t spawnDelay = (uint32_t)(i * 900);
        if (elapsed < spawnDelay) continue;
        uint32_t zElapsed = elapsed - spawnDelay;
        float    t = (float)zElapsed / 2800.f;
        if (t > 1.f) t = 1.f;
        int zx = baseX + i * 10 + (int)(t * 10);
        int zy = baseY - i * 10  - (int)(t * 28);
        if (zzz.prevDrawn[i]) {
            int sz = (i == 2) ? 2 : 1;
            tft.fillRect(zzz.prevAbsX[i] - 1, zzz.prevAbsY[i] - 1,
                         sz * 6 + 4, sz * 8 + 4, C_BK);
        }
        if (zy < 4 || zy > ISL_Y - 12 || zx < 4 || zx > W - 14) {
            zzz.prevDrawn[i] = false; continue;
        }
        uint16_t col = (t < 0.35f) ? C_CY : (t < 0.65f) ? C_DCY : C_DG;
        int sz = (i == 2) ? 2 : 1;
        tft.setTextSize(sz);
        tft.setTextColor(col);
        tft.setCursor(zx, zy);
        tft.print("z");
        zzz.prevDrawn[i] = true;
        zzz.prevAbsX[i]  = zx;
        zzz.prevAbsY[i]  = zy;
    }
}

// ============================================================
// HEX HELPER
// ============================================================
static void drawHexOutline(int cx, int cy, int r, uint16_t col) {
    for (int i = 0; i < 6; i++) {
        float a1 = (i * 60 - 30) * 3.14159f / 180.f;
        float a2 = ((i+1) * 60 - 30) * 3.14159f / 180.f;
        int x1 = cx + (int)(r * cosf(a1)), y1 = cy + (int)(r * sinf(a1));
        int x2 = cx + (int)(r * cosf(a2)), y2 = cy + (int)(r * sinf(a2));
        tft.drawLine(x1, y1, x2, y2, col);
    }
}

// ============================================================
// BOOT INTRO ANIMATION (unchanged from v4.8)
// ============================================================
static void drawBMonogram(int cx, int cy, bool big) {
    int scale = big ? 1 : 0;
    int bx = cx - 7 - scale, by2 = cy - 16 - scale;
    int bw = 5 + scale*2, bh = 32 + scale*2;
    int bumpW = 16 + scale*2, bumpH1 = 14 + scale, bumpH2 = 17 + scale;
    int rTop = 5, rBot = 6;
    uint16_t col = big ? C_MINT : C_CY;
    tft.fillRect(bx, by2, bw, bh, col);
    tft.fillRoundRect(bx, by2, bumpW, bumpH1, rTop, col);
    tft.fillRoundRect(bx, by2 + bh/2, bumpW + 2, bumpH2, rBot, col);
    tft.fillRect(bx + 2, by2 + 2, bumpW - 6, bumpH1 - 4, C_BK);
    tft.fillRect(bx + 2, by2 + bh/2 + 2, bumpW - 4, bumpH2 - 5, C_BK);
}

void playBootIntroAnim(int cx, int cy) {
    int hexR[]      = {56, 52, 48};
    uint16_t hexC[] = {0x18C3, C_DCY, C_CY};
    for (int i = 0; i < 3; i++) { drawHexOutline(cx, cy, hexR[i], hexC[i]); delay(45); yield(); }
    for (int r = 44; r >= 2; r -= 2) {
        uint16_t shade = (r > 30) ? C_MID : (r > 18) ? C_BG : C_BK;
        drawHexOutline(cx, cy, r, shade); delay(7); yield();
    }
    for (int r = 44; r >= 2; r -= 4) drawHexOutline(cx, cy, r, C_WH);
    delay(55); yield();
    for (int r = 44; r >= 2; r -= 2) {
        uint16_t shade = (r > 30) ? C_MID : (r > 18) ? C_BG : C_BK;
        drawHexOutline(cx, cy, r, shade);
    }
    drawBMonogram(cx, cy, true); delay(60); yield();
    tft.fillRect(cx - 32, cy - 20, 64, 40, C_BK);
    for (int r = 44; r >= 2; r -= 2) {
        uint16_t shade = (r > 30) ? C_MID : (r > 18) ? C_BG : C_BK;
        drawHexOutline(cx, cy, r, shade);
    }
    drawBMonogram(cx, cy, false); delay(80); yield();

    for (int dy = 14; dy >= 0; dy -= 2) {
        tft.fillRect(0, cy + 56, W, 22, C_BK);
        tft.setTextSize(2); tft.setTextColor(C_WH);
        tft.setCursor(cx - 36, cy + 60 + dy); tft.print("BRONNY");
        delay(22); yield();
    }
    tft.fillRoundRect(cx + 42, cy + 58, 22, 16, 4, C_CY);
    tft.setTextSize(1); tft.setTextColor(C_BK);
    tft.setCursor(cx + 46, cy + 64); tft.print("AI");
    for (int x = cx - 50; x <= cx + 50; x += 4) {
        tft.drawFastHLine(cx - 50, cy + 80, x - (cx - 50), C_DCY); delay(12); yield();
    }
    const char* credit = "by Patrick Perez";
    int creditW = (int)strlen(credit) * 6;
    int creditX = W / 2 - creditW / 2;
    for (int dy = 10; dy >= 0; dy -= 2) {
        tft.fillRect(0, cy + 82, W, 14, C_BK);
        tft.setTextSize(1); tft.setTextColor(C_LG);
        tft.setCursor(creditX, cy + 86 + dy); tft.print(credit);
        delay(25); yield();
    }
    tft.setTextColor(C_DCY); tft.setTextSize(1);
    tft.setCursor(creditX + creditW + 6, cy + 86); tft.print("v5.0");
    delay(120); yield();
}

void drawBootBar(int pct) {
    if (pct > 100) pct = 100;
    int bx = 40, bw = W - 80, bh = 8, by = H - 16;
    tft.fillRoundRect(bx, by, bw, bh, 3, 0x0841);
    int fw = (int)((float)bw * pct / 100.f);
    if (fw > 4) {
        tft.fillRoundRect(bx, by, fw, bh, 3, C_DCY);
        if (fw > 10) tft.fillRoundRect(bx, by, fw - 4, bh, 3, C_CY);
        tft.drawFastHLine(bx + 2, by + 1, fw - 4, C_MINT);
    }
}

void drawBootLogo() {
    tft.fillScreen(C_BK);
    for (int i = 0; i < 50; i++) {
        int x = (i * 137 + 17) % W;
        int y = (i *  91 + 11) % (H - 30) + 5;
        uint16_t sc = (i % 4 == 0) ? C_LG : (i % 4 == 1) ? C_DG
                    : (i % 3 == 0) ? C_DCY : (uint16_t)0x2945;
        tft.drawPixel(x, y, sc);
    }
    tft.drawRoundRect(39, H - 17, W - 78, 10, 4, C_DG);
}

// ============================================================
// WIFI SCREEN (unchanged from v4.8)
// ============================================================
void drawWifiScreen() {
    tft.fillScreen(C_BK);
    for (int i = 0; i < 40; i++) {
        int x = (i * 179 + 23) % W; int y = (i * 113 + 7) % H;
        tft.drawPixel(x, y, C_DG);
    }
    tft.fillRect(0, 0, W, 36, C_CARD);
    tft.drawFastHLine(0, 36, W, C_CY);
    tft.fillCircle(18, 18, 7, C_CY); tft.fillCircle(18, 18, 3, C_BK);
    tft.setTextSize(1);
    tft.setTextColor(C_WH); tft.setCursor(32, 12); tft.print("BRONNY AI");
    tft.setTextColor(C_CY); tft.setCursor(106, 12); tft.print("v5.0");
    tft.setTextColor(C_LG); tft.setCursor(W - 78, 12); tft.print("Patrick 2026");
    int wx = W / 2, wy = 82;
    tft.fillCircle(wx, wy + 22, 5, C_CY);
    tft.drawCircle(wx, wy + 22, 14, C_CY);
    tft.drawCircle(wx, wy + 22, 24, C_DCY);
    tft.drawCircle(wx, wy + 22, 34, C_DG);
    tft.fillRect(wx - 40, wy + 22, 80, 50, C_BK);
    tft.fillRoundRect(20, 130, W - 40, 28, 6, C_CARD);
    tft.drawRoundRect(20, 130, W - 40, 28, 6, C_CY);
    tft.setTextColor(C_LG); tft.setCursor(30, 136); tft.print("Network:");
    tft.setTextColor(C_WH); tft.setCursor(88, 136); tft.print(WIFI_SSID);
    tft.fillRect(0, H - 20, W, 20, C_CARD);
    tft.drawFastHLine(0, H - 20, W, C_DG);
    tft.setTextColor(C_LG); tft.setTextSize(1);
    tft.setCursor(6, H - 13); tft.print("ESP32-S3");
    tft.setTextColor(C_CY);
    tft.setCursor(W/2 - 42, H - 13); tft.print("Bronny AI v5.0");
    tft.setTextColor(C_LG); tft.setCursor(W - 60, H - 13); tft.print("2026");
}

void drawWifiStatus(const char* line1, uint16_t c1,
                    const char* line2 = "", uint16_t c2 = C_CY) {
    tft.fillRect(0, 164, W, H - 20 - 164, C_BK);
    tft.setTextSize(2); tft.setTextColor(c1);
    int tw = (int)strlen(line1) * 12;
    tft.setCursor(W / 2 - tw / 2, 168); tft.print(line1);
    if (strlen(line2) > 0) {
        tft.setTextSize(1); tft.setTextColor(c2);
        int tw2 = (int)strlen(line2) * 6;
        tft.setCursor(W / 2 - tw2 / 2, 192); tft.print(line2);
    }
}

static uint8_t  spinIdx  = 0;
static uint32_t lastSpin = 0;
void tickWifiSpinner() {
    uint32_t now = millis();
    if (now - lastSpin < 180) return;
    lastSpin = now;
    static const char* frames[] = { "|", "/", "-", "\\" };
    tft.fillRect(W/2 - 6, 72, 12, 12, C_BK);
    tft.setTextSize(1); tft.setTextColor(C_CY);
    tft.setCursor(W/2 - 3, 74);
    tft.print(frames[spinIdx++ % 4]);
}

// ============================================================
// STANDBY
// ============================================================
void enterStandby() {
    bronnyMode = MODE_STANDBY;
    setFaceStandby();
    setStatus("Standby | Hi Bot", C_GREY);
}

void exitStandby() {
    bronnyMode = MODE_ACTIVE;
    lastVoiceTime = millis();
    face.sleepLid = 0.f;
    face.standbyEnteredMs = 0;
    tickZzz(millis());
    setFaceSurprised(600);
    setStatus("Ready", C_CY);
    jingleWake();
}

// ============================================================
// CONVERSATION PIPELINE  (v5.0 - Railway replaces Qwen chat+TTS)
// recordVAD -> Qwen STT -> Railway /voice/text -> MP3 -> play
// ============================================================
static bool     busy             = false;
static uint32_t vadCooldownUntil = 0;

void runConversation() {
    if (busy) return;
    busy = true;
    lastVoiceTime = millis();

    // 1. Listen
    setFaceListen();
    setStatus("Listening...", C_GR);
    if (micOk) {
        uint8_t drain[512];
        uint32_t de = millis() + 150;
        while (millis() < de) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
    }

    bool got = recordVAD(MAX_RECORD_MS, false);
    if (!got) {
        setFaceIdle(); setStatus("Ready", C_CY);
        busy = false; return;
    }
    lastVoiceTime = millis();

    // 2. STT - Qwen transcribes audio to text
    setFaceThink();
    setStatus("Transcribing...", C_YL);
    String transcript = callSTT(recBuf, recLen);
    Serial.printf("[STT] '%s'\n", transcript.c_str());

    if (transcript.isEmpty() || isNoise(transcript, recLen)) {
        setFaceIdle(); setStatus("Ready", C_CY);
        busy = false; return;
    }

    // 3. Railway - send transcript, receive MP3 (LLM + TTS on server)
    setStatus("Thinking...", C_YL);
    bool ok = callRailway(transcript);

    if (!ok) {
        Serial.println("[Conv] Railway failed");
        setStatus("Server error", C_RD);
        delay(1500);
        setFaceIdle(); setStatus("Ready", C_CY);
        busy = false; return;
    }

    // 4. Play MP3 response
    setStatus("Speaking...", C_GR);
    startTalk();
    playMp3();
    stopTalk();

    setFaceHappy(1600);

    // Drain mic during cooldown (prevents echo triggering VAD)
    vadCooldownUntil = millis() + TTS_COOLDOWN_MS;
    if (micOk) {
        uint8_t drain[512];
        uint32_t de = millis() + TTS_COOLDOWN_MS;
        while (millis() < de) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
    }
    vadCooldownUntil = millis() + TTS_COOLDOWN_MS;

    lastConvEndTime = millis();
    lastVoiceTime   = millis();
    setStatus("Ready", C_CY);
    busy = false;
}

// ============================================================
// SETUP
// ============================================================
void setup() {
    Serial.begin(115200);
    AudioToolsLogger.begin(Serial, AudioToolsLogLevel::Warning);
    delay(400);
    makeAuth();

    pinMode(PIN_BLK, OUTPUT); digitalWrite(PIN_BLK, HIGH);
    pinMode(PIN_PA,  OUTPUT); digitalWrite(PIN_PA,  LOW);

    tftSPI.begin(PIN_CLK, -1, PIN_MOSI, PIN_CS);
    tft.init(240, 320); tft.setRotation(3); tft.fillScreen(C_BK);

    audioRestart();
    i2s.setVolume(VOL_MAIN);
    if (audioOk) { auto sc=sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }

    micInit();

    // Allocate MP3 buffer in PSRAM
    mp3Decoder.begin();
    mp3Buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
    if (!mp3Buf) Serial.println("[Boot] WARNING: mp3Buf PSRAM alloc failed");

    // Boot animation
    drawBootLogo();
    int bCX = W / 2, bCY = H / 2 - 32;
    playBootIntroAnim(bCX, bCY);
    drawBootBar(10);
    jingleBoot();
    drawBootBar(55); delay(150);
    drawBootBar(100); delay(300);

    // WiFi
    drawWifiScreen();
    drawWifiStatus("Connecting...", C_YL);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    bool connected = false;
    uint32_t ws = millis();
    while (millis() - ws < 18000) {
        if (WiFi.status() == WL_CONNECTED) { connected = true; break; }
        tickWifiSpinner();
        yield();
    }
    if (connected) {
        char ipStr[32]; snprintf(ipStr, 32, "%s", WiFi.localIP().toString().c_str());
        drawWifiStatus("Connected!", C_GR, ipStr, C_CY);
        jingleConnect(); delay(900);
    } else {
        drawWifiStatus("FAILED", C_RD, "Check config", C_RD);
        jingleError(); delay(2000);
    }

    // Face screen
    audioRestart();
    i2s.setVolume(VOL_MAIN);
    { auto sc=sineGen.defaultConfig(); sc.copyFrom(ainf_rec); sineGen.begin(sc); }
    tft.fillScreen(C_BK);
    drawFaceBg();
    drawFace(true);
    drawIslandBar();

    jingleReady();
    Serial.printf("[Boot] Ready  VAD_THR=%d  Heap=%u  PSRAM=%u\n",
                  VAD_THR, esp_get_free_heap_size(),
                  heap_caps_get_free_size(MALLOC_CAP_SPIRAM));

    lastVoiceTime   = millis();
    lastConvEndTime = millis();
    setStatus("Ready", C_CY);
}

// ============================================================
// LOOP
// Serial: + / - = adjust VAD_THR   m = mic peak
// ============================================================
void loop() {
    uint32_t now = millis();

    animFace();
    if (faceRedraw) { drawFace(false); faceRedraw = false; }
    tickZzz(now);

    if (!busy && !isSpeaking && micOk && now > vadCooldownUntil) {
        int32_t sb[32];
        int rd    = mic_stream.readBytes((uint8_t*)sb, sizeof(sb));
        int frames = rd / 8;
        bool peak  = false;
        for (int f = 0; f < frames; f++) {
            if (abs(inmp441Sample(sb[f*2])) > VAD_THR) { peak = true; break; }
        }

        if (bronnyMode == MODE_ACTIVE) {
            if (lastConvEndTime > 0 &&
                now - lastConvEndTime > STANDBY_TIMEOUT_MS &&
                now - lastVoiceTime  > STANDBY_TIMEOUT_MS) {
                enterStandby();
            } else if (peak) {
                lastVoiceTime = now;
                runConversation();
            }
        } else {
            if (peak) {
                setStatus("Listening...", C_GREY);
                if (checkWakeWord()) {
                    exitStandby();
                    if (micOk) {
                        uint8_t drain[512];
                        uint32_t de = millis() + 300;
                        while (millis() < de) { mic_stream.readBytes(drain, sizeof(drain)); yield(); }
                    }
                    runConversation();
                } else {
                    setStatus("Standby | Hi Bot", C_GREY);
                }
            }
        }
    }

    if (Serial.available()) {
        char c = Serial.read();
        if      (c == '+') { VAD_THR = min(8000, VAD_THR+100); Serial.printf("VAD_THR=%d\n", VAD_THR); }
        else if (c == '-') { VAD_THR = max(300,  VAD_THR-100); Serial.printf("VAD_THR=%d\n", VAD_THR); }
        else if (c == 'm') {
            int32_t tb[256]; int pk = 0;
            for (int p = 0; p < 20; p++) {
                mic_stream.readBytes((uint8_t*)tb, sizeof(tb));
                for (int f = 0; f < 32; f++) {
                    int v = abs(inmp441Sample(tb[f*2]));
                    if (v > pk) pk = v;
                }
                yield();
            }
            Serial.printf("[MIC] peak=%d  THR=%d\n", pk, VAD_THR);
        }
    }

    yield();
}
