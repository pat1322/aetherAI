/*
 * ═══════════════════════════════════════════════════════════════
 *  BRONNY AI  v2.2
 *  by Patrick Perez
 * ═══════════════════════════════════════════════════════════════
 *
 *  Hardware:
 *    Board   : ESP32-S3 Dev Module (OPI PSRAM 8MB)
 *    Codec   : ES8311 (I2C addr 0x18)
 *    Mic     : INMP441 (I2S port 1 — GPIOs 4/5/6)
 *    Display : ST7789  320×240 (HSPI)
 *    LED     : WS2812B on GPIO 48 (built-in)
 *
 *  KEY FIXES vs v1.2:
 *    ✔ Enum state machine — no scattered bool flags
 *    ✔ Fixed char[] transcript buffers — eliminates heap
 *      fragmentation from String appends on every DG message
 *    ✔ Global EncodedAudioStream (gTtsDecoded) — v1.2 put this
 *      on the local stack inside callRailwayStream(), causing
 *      ~1 KB heap fragment every single TTS call
 *    ✔ Two dedicated global WiFiClientSecure (gHttpCli / gVideoCli)
 *      — never allocated on task or function stacks
 *    ✔ jEscBuf() — JSON escape into a fixed char buffer, no String
 *    ✔ audioInitVideo() — 44100 Hz / 1-ch init for MJPEG audio
 *    ✔ MJPEG video mode with full enter/exit lifecycle
 *    ✔ g_audioTaskRunning flag — safe Core-0 audio task coordination
 *    ✔ setSwapBytes(true/false) toggle for JPEGDEC compatibility
 *    ✔ dgWs.disconnect() before reconnect — prevents TLS state leak
 *    ✔ Watchdog-safe yield() in all long-running loops
 *    ✔ Crash breadcrumbs in RTC RAM survive reboots
 *
 *  NOTE: GPIO 48 is shared between PA enable and the WS2812B.
 *        v2.2: gpio_reset_pin() reclaims GPIO 48 from NeoPixel RMT
 *        in audioInitVideo() and exitPartyMode() so PA is properly
 *        enabled for video audio output.
 * ═══════════════════════════════════════════════════════════════
 */

// TFT_eSPI MUST come first — prevents GPIO symbol clash with audio_driver
#include <SPI.h>
#include <FS.h>
#include <TFT_eSPI.h>
#include <JPEGDEC.h>

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

#include <Adafruit_NeoPixel.h>
#include <arduinoFFT.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <math.h>
#include "esp_system.h"
#include "driver/gpio.h"
#include "voice_config.h"

// ╔══════════════════════════════════════════════════════════════╗
// ║  FORWARD DECLARATIONS                                        ║
// ╚══════════════════════════════════════════════════════════════╝
void        setStatus(const char* s, uint16_t c);
void        setFooterOnly(uint16_t c, const char* s);
void        tftLog(uint16_t col, const char* msg);
void        tftLogf(uint16_t col, const char* fmt, ...);
void        maintainDeepgram();
void        setLogsVisible(bool visible);
void        setFaceListen();
void        setFaceIdle();
void        setFaceThink();
void        setFaceTalk();
void        setFaceHappy();
void        setFaceSurprised();
void        setFaceSleep();
void        startTalk();
void        stopTalk();
static void audioInitRec();
static void audioInitTTS();
static void audioInitVideo();
static void micInit();
static void enterPartyMode();
static void executeBronnyCommand(JsonVariant cmd);  // NEW v2.1
static void playAudioFromUrl(const String& url);    // NEW v2.1
static void tickStandaloneLed(uint32_t now);        // NEW v2.1
static void exitPartyMode();
static void enterVideoMode(const char* jobId);

// ╔══════════════════════════════════════════════════════════════╗
// ║  CONFIG & TUNING                                             ║
// ╚══════════════════════════════════════════════════════════════╝
const char* WIFI_SSID = WIFI_SSID_CFG;
const char* WIFI_PASS = WIFI_PASS_CFG;

#define VOL_MAIN             0.50f
#define VOL_JINGLE           0.25f
#define VOL_VIDEO            0.40f
#define MIC_GAIN_SHIFT       14
#define TTS_COOLDOWN_MS      800
#define HEARTBEAT_MS         30000
#define DG_KEEPALIVE_MS      8000
#define DG_RECONNECT_MS      3000
#define DG_CONNECT_TIMEOUT   8000
#define DG_FINAL_TIMEOUT_MS  2500
#define STANDBY_TIMEOUT_MS   180000UL
#define MIN_AUDIO_BYTES      1024
#define STREAM_DATA_GAP_MS   500
#define STREAM_CHUNK         512

// Video
#define VIDEO_POLL_MS        5000
#define VIDEO_MJPEG_BUF      (256 * 1024)  // 256 KB in PSRAM
#define VIDEO_PREFILL_BYTES  (128 * 1024)  // 128 KB pre-fill
#define VIDEO_STALL_MS       15000
#define VIDEO_PRIME_MS       350
#define VIDEO_TARGET_FPS     20

// Transcript buffers
#define DG_FINAL_MAX   512
#define DG_PARTIAL_MAX 256

// ╔══════════════════════════════════════════════════════════════╗
// ║  STATE MACHINE                                               ║
// ╚══════════════════════════════════════════════════════════════╝
typedef enum : uint8_t {
    ST_BOOT    = 0,
    ST_LISTEN  = 1,   // idle, mic streaming to DG
    ST_THINK   = 2,   // waiting for Railway response
    ST_SPEAK   = 3,   // streaming TTS audio
    ST_STANDBY = 4,   // low-power idle, wake-word listening
    ST_PARTY   = 5,   // audio visualiser + LED mode
    ST_VIDEO   = 6,   // MJPEG video playback
} AppState;

static AppState gState = ST_BOOT;

static inline bool isListening() { return gState == ST_LISTEN; }
static inline bool isBusy()      { return gState == ST_THINK || gState == ST_SPEAK; }
static inline bool isPartyMode() { return gState == ST_PARTY; }
static inline bool isStandby()   { return gState == ST_STANDBY; }
static inline bool isVideoMode() { return gState == ST_VIDEO; }
static inline void setState(AppState s) { gState = s; }

// ╔══════════════════════════════════════════════════════════════╗
// ║  PINS                                                        ║
// ╚══════════════════════════════════════════════════════════════╝
#define PIN_SDA      1
#define PIN_SCL      2
#define PIN_MCLK    38
#define PIN_BCLK    14
#define PIN_WS      13
#define PIN_DOUT    45
#define PIN_DIN     12
#define PIN_PA      48   // shared with WS2812B; amp off during party/video
#define ES_ADDR   0x18
#define PIN_MIC_WS   4
#define PIN_MIC_SCK  5
#define PIN_MIC_SD   6
#define PIN_BLK     42
#define PIN_BOOT     0

// ╔══════════════════════════════════════════════════════════════╗
// ║  DISPLAY + COLOUR PALETTE                                    ║
// ╚══════════════════════════════════════════════════════════════╝
TFT_eSPI tft = TFT_eSPI();
JPEGDEC  jpeg;

#define W   320
#define H   240

#define C_BK   0x0000
#define C_BG   0x0209
#define C_MID  0x0412
#define C_CY   0x07FF
#define C_DCY  0x0455
#define C_WH   0xFFFF
#define C_LG   0xC618
#define C_DG   0x39E7
#define C_GR   0x07E0
#define C_RD   0xF800
#define C_YL   0xFFE0
#define C_MINT 0x3FF7
#define C_WARN C_YL
#define C_CARD 0x18C3
#define C_PNK  0xF81F   // magenta — video mode accent

static uint16_t dimCol(uint16_t c, int f);  // defined further below

// ╔══════════════════════════════════════════════════════════════╗
// ║  ROBOEYES                                                    ║
// ╚══════════════════════════════════════════════════════════════╝
#define LOG_Y        160
#define LOG_LINE_H    14
#define LOG_LINES      4
#define LOG_FOOTER_Y  (H - 14)

#include "FluxGarage_RoboEyes_TFT.h"
FluxGarage_RoboEyes roboEyes(tft, W, LOG_Y, 0);
static int faceBlitY = 0;

// ╔══════════════════════════════════════════════════════════════╗
// ║  TFT LOG BUFFER                                              ║
// ╚══════════════════════════════════════════════════════════════╝
static String   gLog[LOG_LINES];
static uint16_t gLogCol[LOG_LINES];
static int      gLogCount    = 0;
static int      gLogHead     = 0;
static String   gFooterText  = "v2.0";
static uint16_t gFooterColor = C_CY;
static bool     logsVisible  = false;

// ╔══════════════════════════════════════════════════════════════╗
// ║  TIMERS & FLAGS                                              ║
// ╚══════════════════════════════════════════════════════════════╝
static uint32_t lastHbMs         = 0;
static uint32_t vadCooldownUntil = 0;
static uint32_t lastRailwayMs    = 0;
static uint32_t lastVideoPollMs  = 0;
static uint32_t lastMemCheckMs   = 0;
static bool     bootIntroDone    = true;
static uint32_t bootReadyAt      = 0;

// ╔══════════════════════════════════════════════════════════════╗
// ║  RTC CRASH BREADCRUMBS  (survive panics across reboots)      ║
// ╚══════════════════════════════════════════════════════════════╝
#define CRASH_MAGIC 0xDEADBEEF
RTC_DATA_ATTR static uint32_t rtcCrashMagic   = 0;
RTC_DATA_ATTR static char     rtcCrashMsg[48] = {0};

static void setCrashPoint(const char* w) {
    rtcCrashMagic = CRASH_MAGIC;
    strncpy(rtcCrashMsg, w, sizeof(rtcCrashMsg) - 1);
    rtcCrashMsg[sizeof(rtcCrashMsg) - 1] = '\0';
}
static void clearCrashPoint() { rtcCrashMagic = 0; rtcCrashMsg[0] = '\0'; }

// ╔══════════════════════════════════════════════════════════════╗
// ║  GLOBAL NETWORK CLIENTS  (permanently off-stack)             ║
// ╚══════════════════════════════════════════════════════════════╝
// WiFiClientSecure ≈ 3-4 KB each.  Keeping global prevents them
// from ever landing on a function or FreeRTOS task stack and
// blowing the stack.  Sequential flow guarantees no concurrency.
static WiFiClientSecure gHttpCli;      // heartbeat + TTS Railway stream
static WiFiClientSecure gVideoCli;     // video poll (checkCurrentJob etc.)
static WiFiClientSecure gVidMjpegCli;  // MJPEG video stream in playVideo()
static WiFiClientSecure gVidAudioCli;  // MP3 audio stream in videoAudioTaskFn()
static WiFiClientSecure gMediaCli;     // NEW v2.1 — /bronny/media YouTube streaming

// ╔══════════════════════════════════════════════════════════════╗
// ║  AUDIO ENGINE                                                ║
// ╚══════════════════════════════════════════════════════════════╝
static AudioInfo ainf_rec(16000, 2, 16);
static AudioInfo ainf_tts(24000, 2, 16);
static AudioInfo ainf_vid(44100, 2, 16);  // stereo — ES8311 I2S needs both channels

DriverPins     brdPins;
AudioBoard     brdDrv(AudioDriverES8311, brdPins);
I2SCodecStream i2s(brdDrv);
I2SStream      mic_stream;

// ── TTS decoder — GLOBAL so it is NEVER stack-allocated ────────
// v1.x placed EncodedAudioStream on the local stack inside
// callRailwayStream().  That caused a ~1 KB heap fragment on
// every single TTS call, eventually corrupting the heap.
static MP3DecoderHelix    gMp3Dec;
static EncodedAudioStream gTtsDecoded(&i2s, &gMp3Dec);

// Sine tone generator (jingles + inter-frame silence padding)
static SineWaveGenerator<int16_t>    sineGen(32000);
static GeneratedSoundStream<int16_t> sineSrc(sineGen);
static StreamCopy                    sineCopy(i2s, sineSrc);

static bool audioOk   = false;
static bool micOk     = false;
static bool inTtsMode = false;

// ╔══════════════════════════════════════════════════════════════╗
// ║  REMOTE CONTROL STATE  (NEW v2.1)                            ║
// ╚══════════════════════════════════════════════════════════════╝
// Set by executeBronnyCommand() when the /bronny/control panel
// sends a command.  Delivered via the heartbeat response JSON.

static uint8_t  remoteVizMode    = 0;       // 0=spectrum, 1=mirror
static uint8_t  remoteLedMode    = 0;       // 0-3 maps to partyLedMode
static uint8_t  remoteSpeed      = 6;       // 1-10
static uint32_t remoteLedColor   = 0xFF3CA0; // packed 0xRRGGBB
static bool     autoPartyCycle   = true;    // false = hold UI selection, no auto-cycle

// Standalone LED — active outside party mode (from 'led' command)
static bool     ledStandaloneActive = false;
static uint8_t  ledStandaloneMode   = 0;    // 1=rainbow,2=pulse,3=breathe,4=strobe,5=solid
static float    ledStandaloneHue    = 0.f;
static uint32_t ledStandaloneLastMs = 0;

// Media playback state (play/pause/stop)
static bool     mediaPlaying     = false;
static bool     mediaPaused      = false;
static String   mediaCurrentUrl  = "";

static inline int16_t inmp441Sample(int32_t raw) {
    int32_t s = raw >> MIC_GAIN_SHIFT;
    if (s >  32767) s =  32767;
    if (s < -32768) s = -32768;
    return (int16_t)s;
}

// ── Codec pin setup (idempotent) ────────────────────────────────
static bool audioPinsSet = false;
static void audioPinsSetup() {
    if (audioPinsSet) return;
    Wire.begin(PIN_SDA, PIN_SCL, 100000);
    brdPins.addI2C(PinFunction::CODEC, PIN_SCL, PIN_SDA, ES_ADDR, 100000, Wire);
    brdPins.addI2S(PinFunction::CODEC, PIN_MCLK, PIN_BCLK, PIN_WS, PIN_DOUT, PIN_DIN);
    brdPins.addPin(PinFunction::PA, PIN_PA, PinLogic::Output);
    audioPinsSet = true;
}

// ── Microphone init (INMP441 on I2S port 1) ────────────────────
static void micInit() {
    auto cfg        = mic_stream.defaultConfig(RX_MODE);
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
        static uint8_t tmp[512];
        uint32_t e = millis() + 300;
        while (millis() < e) { mic_stream.readBytes(tmp, sizeof(tmp)); yield(); }
    }
}

// ── Codec mode transitions ──────────────────────────────────────
static void audioInitRec() {
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

static void audioInitTTS() {
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

// audioInitVideo — 44100 Hz / stereo for MJPEG MP3 audio task
static void audioInitVideo() {
    i2s.end(); delay(100);
    audioPinsSetup();
    auto cfg = i2s.defaultConfig(TX_MODE);
    cfg.copyFrom(ainf_vid);
    cfg.output_device = DAC_OUTPUT_ALL;
    audioOk = i2s.begin(cfg);
    i2s.setVolume(VOL_VIDEO);
    // GPIO 48 is shared between PA enable and WS2812B (NeoPixel).
    // After partyLed.begin() the RMT peripheral owns the GPIO-matrix
    // routing for pin 48, making digitalWrite() ineffective.
    // gpio_reset_pin() disconnects any peripheral from the matrix and
    // restores direct GPIO control, then we drive the pin HIGH to
    // enable the power-amplifier for video audio output.
    gpio_reset_pin((gpio_num_t)PIN_PA);
    gpio_set_direction((gpio_num_t)PIN_PA, GPIO_MODE_OUTPUT);
    gpio_set_level((gpio_num_t)PIN_PA, 1);
    inTtsMode = false;
}

static void audioRestart() {
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

static void playTone(float hz, int ms) {
    if (!audioOk) { delay(ms); return; }
    sineGen.setFrequency(hz);
    uint32_t e = millis() + ms;
    while (millis() < e) { sineCopy.copy(); yield(); }
}
static void playSil(int ms) { playTone(0, ms); }

// ── Jingles ─────────────────────────────────────────────────────
static void jingleBoot() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    const float n[]={523,659,784,1047,1319,1568,2093};
    const int   d[]={100,100,100, 140, 260,  80, 280};
    for (int i=0;i<7;i++){playTone(n[i],d[i]);playSil(20);}
    i2s.setVolume(VOL_MAIN);
}
static void jingleConnect() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(880,100);playSil(25);playTone(1108,100);playSil(25);
    playTone(1318,200);playSil(150);
    i2s.setVolume(VOL_MAIN);
}
static void jingleError() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(300,200);playSil(80);playTone(220,350);playSil(200);
    i2s.setVolume(VOL_MAIN);
}
static void jingleReady() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(880,80);playSil(30);playTone(1318,80);playSil(30);
    playTone(1760,200);playSil(150);
    i2s.setVolume(VOL_MAIN);
}
static void jingleWake() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    playTone(660,80);playSil(20);playTone(1100,120);playSil(80);
    i2s.setVolume(VOL_MAIN);
}
static void jingleParty() {
    audioInitRec(); i2s.setVolume(VOL_JINGLE);
    const float n[]={523,659,784,1047,784,1047,1319};
    const int   d[]={ 80, 80, 80, 140, 80,  80, 260};
    for (int i=0;i<7;i++){playTone(n[i],d[i]);playSil(15);}
    i2s.setVolume(VOL_MAIN);
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  VIDEO MODE GLOBALS                                          ║
// ╚══════════════════════════════════════════════════════════════╝
static uint8_t*      mjpegBuf            = nullptr; // 256 KB PSRAM
static char          gVidJobId[32]       = {0};
static volatile bool g_vidPlaying        = false;
static volatile bool g_vidAudioReady     = false;
static volatile bool g_vidStartPlayback  = false;
static volatile bool g_audioTaskRunning  = false;

// ╔══════════════════════════════════════════════════════════════╗
// ║  UTILITIES                                                   ║
// ╚══════════════════════════════════════════════════════════════╝

// JSON-escape src into a fixed dst buffer — no String heap allocation
static void jEscBuf(const char* src, char* dst, size_t dstLen) {
    size_t j = 0;
    for (size_t i = 0; src[i] && j < dstLen - 3; i++) {
        unsigned char c = (unsigned char)src[i];
        if      (c == '"' ) { dst[j++]='\\'; dst[j++]='"';  }
        else if (c == '\\') { dst[j++]='\\'; dst[j++]='\\'; }
        else if (c == '\n') { dst[j++]='\\'; dst[j++]='n';  }
        else if (c == '\r') { dst[j++]='\\'; dst[j++]='r';  }
        else if (c == '\t') { dst[j++]='\\'; dst[j++]='t';  }
        else if (c >= 0x20) { dst[j++]=(char)c; }
    }
    dst[j] = '\0';
}

static void ensureWifi() {
    if (WiFi.status() != WL_CONNECTED) {
        WiFi.reconnect();
        uint32_t t = millis();
        while (WiFi.status() != WL_CONNECTED && millis()-t < 8000)
            { delay(300); yield(); }
    }
}

static String baseUrl() {
    String u = String(AETHER_URL);
    while (u.endsWith("/")) u.remove(u.length()-1);
    return u;
}

static int wordCount(const char* s) {
    int n=0; bool in=false;
    for (; *s; s++) {
        if (*s != ' ') { if (!in) { n++; in=true; } }
        else in = false;
    }
    return n;
}

static inline void forceDrawFace() { roboEyes.update(); }

static uint16_t dimCol(uint16_t c, int f) {
    if (f <= 0) return 0;
    if (f >= 8) return c;
    return (uint16_t)((((c>>11)&0x1F)*f/8)<<11) |
           (uint16_t)((((c>> 5)&0x3F)*f/8)<< 5) |
           (uint16_t)( ((c& 0x1F)*f/8));
}
static inline int lerpi(int a, int b, int f, int fm) {
    if(fm<=0)return b; if(f<=0)return a; if(f>=fm)return b;
    return a+(b-a)*f/fm;
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  TFT LOG ZONE                                                ║
// ╚══════════════════════════════════════════════════════════════╝
static void logRedraw() {
    if (!logsVisible) return;
    tft.drawFastHLine(0, LOG_Y, W, C_CY);
    tft.fillRect(0, LOG_Y+1, W, LOG_LINES*LOG_LINE_H+3, C_MID);
    int total = min(gLogCount, LOG_LINES);
    for (int i=0; i<total; i++) {
        int slot = (gLogHead+i) % LOG_LINES;
        uint16_t c = gLogCol[slot];
        int ly = LOG_Y+2+i*LOG_LINE_H;
        tft.fillRect(0, ly+1, 3, LOG_LINE_H-3, c);
        tft.setTextColor(c); tft.setTextSize(1);
        tft.setCursor(7, ly+3);
        tft.print(gLog[slot]);
    }
}

static void logDrawFooter() {
    if (!logsVisible) return;
    tft.fillRect(0, LOG_FOOTER_Y, W, H-LOG_FOOTER_Y, C_BG);
    tft.drawFastHLine(0, LOG_FOOTER_Y, W, C_DCY);
    tft.setTextColor(gFooterColor); tft.setTextSize(1);
    int tw = (int)gFooterText.length() * 6;
    tft.setCursor(W/2-tw/2, LOG_FOOTER_Y+4);
    tft.print(gFooterText);
}

void tftLog(uint16_t col, const char* msg) {
    String s = String(msg);
    if ((int)s.length() > 53) s = s.substring(0,53);
    if (gLogCount < LOG_LINES) {
        gLog[gLogCount]=s; gLogCol[gLogCount]=col; gLogCount++;
        if (logsVisible) {
            int i  = gLogCount-1;
            int ly = LOG_Y+2+i*LOG_LINE_H;
            tft.fillRect(0,ly, W, LOG_LINE_H-1, C_MID);
            tft.fillRect(0,ly+1, 3, LOG_LINE_H-3, col);
            tft.setTextColor(col); tft.setTextSize(1);
            tft.setCursor(7,ly+3);
            tft.print(s);
        }
    } else {
        gLog[gLogHead]=s; gLogCol[gLogHead]=col;
        gLogHead=(gLogHead+1)%LOG_LINES;
        logRedraw();
    }
}

void tftLogf(uint16_t col, const char* fmt, ...) {
    char buf[80]; va_list ap; va_start(ap,fmt);
    vsnprintf(buf,sizeof(buf),fmt,ap); va_end(ap);
    tftLog(col,buf);
}

void setStatus(const char* s, uint16_t c) {
    gFooterText=String(s); gFooterColor=c;
    logDrawFooter();
}
void setFooterOnly(uint16_t c, const char* s) {
    char buf[54]; strncpy(buf,s,53); buf[53]='\0';
    gFooterText=String(buf); gFooterColor=c;
    logDrawFooter();
}
void setLogsVisible(bool visible) {
    logsVisible = visible;
    if (visible) {
        faceBlitY = 0;
        roboEyes.setPushYOffset(0);
        roboEyes.update();
        logRedraw();
        logDrawFooter();
    } else {
        faceBlitY = 40;
        roboEyes.setPushYOffset(faceBlitY);
        tft.fillRect(0, 0, W, faceBlitY, C_BK);
        tft.fillRect(0, LOG_Y, W, H-LOG_Y, C_BK);
        roboEyes.update();
    }
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  FACE WRAPPERS  (RoboEyes)                                   ║
// ╚══════════════════════════════════════════════════════════════╝
void setFaceIdle() {
    roboEyes.setSleepy(false);
    roboEyes.setMood(MOOD_DEFAULT);
    roboEyes.open();
    roboEyes.setTalking(false);
    roboEyes.setCenterLocked(false);
    roboEyes.setIdleMode(true, 2, 2);
}
void setFaceTalk() {
    roboEyes.setMood(MOOD_HAPPY);
    roboEyes.setTalking(true);
    roboEyes.setIdleMode(false);
    roboEyes.setCenterLocked(true);
}
void setFaceThink() {
    roboEyes.setMood(MOOD_TIRED);
    roboEyes.setTalking(false);
    roboEyes.setIdleMode(false);
    roboEyes.setCenterLocked(true);
}
void setFaceHappy() {
    roboEyes.setMood(MOOD_HAPPY);
    roboEyes.anim_laugh();
    roboEyes.setTalking(false);
    roboEyes.setIdleMode(false);
    roboEyes.setCenterLocked(true);
}
void setFaceListen() {
    roboEyes.setSleepy(false);
    roboEyes.setMood(MOOD_DEFAULT);
    roboEyes.setTalking(false);
    roboEyes.setCenterLocked(false);
    roboEyes.setIdleMode(true, 2, 2);
}
void setFaceSurprised() {
    roboEyes.anim_confused();
    roboEyes.setTalking(false);
    roboEyes.setIdleMode(false);
    roboEyes.setCenterLocked(true);
}
void setFaceSleep() {
    roboEyes.setTalking(false);
    roboEyes.setSleepy(true);
}
void startTalk() { setFaceTalk(); }
void stopTalk()  { setFaceIdle(); }

// ╔══════════════════════════════════════════════════════════════╗
// ║  BOOT BUTTON  (GPIO 0 — toggle log panel)                   ║
// ╚══════════════════════════════════════════════════════════════╝
static bool     lastBootBtn   = HIGH;
static uint32_t lastBootBtnMs = 0;

static void checkBootButton() {
    if (isPartyMode() || isVideoMode()) return;
    bool s = digitalRead(PIN_BOOT);
    if (s == LOW && lastBootBtn == HIGH && millis()-lastBootBtnMs > 300) {
        lastBootBtnMs = millis();
        setLogsVisible(!logsVisible);
    }
    lastBootBtn = s;
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  NOISE FILTER                                                ║
// ╚══════════════════════════════════════════════════════════════╝
static bool isNoise(const char* t) {
    if (!t || strlen(t) < 3) return true;

    // Build lowercase, trimmed copy
    char low[DG_FINAL_MAX];
    int li = 0;
    for (const char* p = t; *p && li < (int)sizeof(low)-1; p++, li++)
        low[li] = (char)tolower((uint8_t)*p);
    low[li] = '\0';
    const char* s = low;
    while (*s == ' ') s++;
    if (strlen(s) < 3) return true;

    // Single-word noise list
    static const char* nw[] = {
        "...","..",".", "ah","uh","hm","hmm","mm","um","huh",
        "oh","ow","beep","boop","ding","dong","ping","ring",
        "the","a","i", nullptr
    };
    for (int i=0; nw[i]; i++)
        if (strcmp(s, nw[i]) == 0) return true;

    // Repeated-single-word filter  (e.g. "the the the")
    const char* sp = strchr(s, ' ');
    if (sp) {
        char fw[32]={0};
        int fl = (int)(sp-s); if(fl>31) fl=31;
        strncpy(fw, s, fl);
        bool allSame = true;
        const char* p = s;
        while (*p) {
            const char* e = strchr(p,' ');
            char w[32]={0};
            if (e) { int wl=(int)(e-p); if(wl>31)wl=31; strncpy(w,p,wl); }
            else   strncpy(w,p,31);
            // trim
            char* wt = w;
            while (*wt==' ') wt++;
            if (*wt && strcmp(wt,fw)!=0) { allSame=false; break; }
            p = e ? e+1 : p+strlen(p);
        }
        if (allSame && strlen(fw) <= 6) return true;
    }
    return false;
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  HEARTBEAT                                                   ║
// ╚══════════════════════════════════════════════════════════════╝
// Separate JSON doc for heartbeat (keeps it off dgJsonDoc)
static StaticJsonDocument<2048> hbDoc;

static void sendHeartbeat() {
    if (WiFi.status() != WL_CONNECTED) return;
    gHttpCli.setInsecure();
    gHttpCli.setConnectionTimeout(8000);
    HTTPClient http;
    http.begin(gHttpCli, baseUrl() + "/bronny/heartbeat");
    http.setTimeout(8000);
    http.addHeader("Content-Type","application/json");

    int code = http.POST("{\"device\":\"bronny\",\"version\":\"2.1\"}");

    // Always consume the response body so the server's command queue is
    // properly drained.  executeBronnyCommand() guards against executing
    // unsafe commands during video/party mode internally.
    if (code == HTTP_CODE_OK) {
        String body = http.getString();
        hbDoc.clear();
        if (deserializeJson(hbDoc, body) == DeserializationError::Ok) {
            JsonArray cmds = hbDoc["commands"].as<JsonArray>();
            for (JsonVariant cmd : cmds) {
                executeBronnyCommand(cmd);
            }
        }
    }

    http.end();
    gHttpCli.stop();
}


// ╔══════════════════════════════════════════════════════════════╗
// ║  DEEPGRAM STREAMING ASR                                      ║
// ╚══════════════════════════════════════════════════════════════╝
static WebSocketsClient dgWs;

static bool     dgConnected         = false;
static bool     dgStreaming          = false;
static uint32_t dgLastKeepalive      = 0;
static uint32_t dgLastConnectAttempt = 0;

// Fixed-size transcript buffers — no String heap churn on every DG message
static char  gDgFinal[DG_FINAL_MAX]   = {0};
static char  gDgPartial[DG_PARTIAL_MAX] = {0};
static uint32_t dgFinalReceivedAt = 0;
static bool     pendingTranscript = false;

// Mic → DG PCM conversion buffers (static = permanently off-stack)
static int32_t s_rawBuf[400];
static int16_t s_pcmBuf[400];

static StaticJsonDocument<4096> dgJsonDoc;

static const char* DG_PATH =
    "/v1/listen"
    "?encoding=linear16&sample_rate=16000&channels=1"
    "&language=en&model=nova-3"
    "&interim_results=true&endpointing=1500"
    "&utterance_end_ms=2000&filler_words=false";

static void parseDgMsg(const char* json, size_t len) {
    dgJsonDoc.clear();
    if (deserializeJson(dgJsonDoc, json, len) != DeserializationError::Ok) return;
    auto& doc = dgJsonDoc;

    const char* msgType = doc["type"] | "";
    if (strcmp(msgType, "Results") != 0) return;

    const char* txt  = doc["channel"]["alternatives"][0]["transcript"] | "";
    bool isFinal     = doc["is_final"]     | false;
    bool speechEnd   = doc["speech_final"] | false;
    if (!txt || !*txt) return;

    if (isFinal) {
        // Append to running final with a space — safe, bounded
        size_t cur = strlen(gDgFinal);
        if (cur > 0 && cur < DG_FINAL_MAX - 2) {
            gDgFinal[cur]   = ' ';
            gDgFinal[cur+1] = '\0';
        }
        strncat(gDgFinal, txt, DG_FINAL_MAX - strlen(gDgFinal) - 1);
        dgFinalReceivedAt = millis();
        setFooterOnly(C_MINT, txt);
    } else {
        strncpy(gDgPartial, txt, DG_PARTIAL_MAX-1);
        gDgPartial[DG_PARTIAL_MAX-1] = '\0';
        char pfx[DG_PARTIAL_MAX+4];
        snprintf(pfx, sizeof(pfx), "> %s", txt);
        setFooterOnly(C_LG, pfx);
    }

    // Commit immediately on speech_final if ≥ 2 words captured
    if (speechEnd && strlen(gDgFinal) > 0 && wordCount(gDgFinal) >= 2) {
        pendingTranscript = true;
        dgFinalReceivedAt = 0;
    }
}

static void onDgWsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            dgConnected     = true;
            dgLastKeepalive = millis();
            if (gState == ST_LISTEN || gState == ST_STANDBY) dgStreaming = true;
            break;
        case WStype_TEXT:
            parseDgMsg((const char*)payload, length);
            break;
        case WStype_DISCONNECTED:
        case WStype_ERROR:
            dgConnected = false; dgStreaming = false;
            break;
        default: break;
    }
}

void maintainDeepgram() {
    uint32_t now = millis();
    dgWs.loop();

    // During video the WebSocket just idles — no audio, no reconnect
    if (isVideoMode()) return;

    if (!dgConnected) {
        if (now - dgLastConnectAttempt > DG_RECONNECT_MS) {
            dgLastConnectAttempt = now;
            // Explicit disconnect before reconnect prevents TLS state leak
            dgWs.disconnect();
            delay(30);
            String authHdr = "Authorization: Token " + String(DEEPGRAM_API_KEY);
            dgWs.onEvent(onDgWsEvent);
            dgWs.setExtraHeaders(authHdr.c_str());
            dgWs.beginSSL("api.deepgram.com", 443, DG_PATH);
        }
        return;
    }

    // Stream mic PCM to Deepgram when in a listening state
    if (dgStreaming && micOk && !isBusy()) {
        if (!isPartyMode()) {  // party handles its own audio in partyProcessAudio()
            int avail = mic_stream.available();
            if (avail > 0) {
                int toRead = min(avail, (int)sizeof(s_rawBuf));
                int got    = mic_stream.readBytes((uint8_t*)s_rawBuf, toRead);
                int frames = got / 4;
                if (frames > 0) {
                    for (int i=0; i<frames; i++)
                        s_pcmBuf[i] = inmp441Sample(s_rawBuf[i]);
                    dgWs.sendBIN((uint8_t*)s_pcmBuf, frames * 2);
                }
            }
        }
        return;
    }

    // Keepalive ping when not streaming
    if (now - dgLastKeepalive > DG_KEEPALIVE_MS) {
        dgWs.sendTXT("{\"type\":\"KeepAlive\"}");
        dgLastKeepalive = now;
    }
}

// ── Deepgram connection screen ──────────────────────────────────
static void drawDgAnim(int cx, int cy, int spin) {
    tft.fillRect(cx-40, cy-40, 80, 80, C_BK);
    static const int radii[3]={14,26,38};
    for (int i=0; i<3; i++) {
        bool lit = (i==spin);
        tft.drawCircle(cx,cy,radii[i], lit?C_CY:C_DG);
        if (lit) {
            tft.drawCircle(cx,cy,radii[i]-1,dimCol(C_CY,4));
            tft.drawCircle(cx,cy,radii[i]+1,dimCol(C_CY,2));
        }
    }
    tft.fillCircle(cx,cy,5, spin==0?C_CY:C_DG);
}
static void drawDgStatus(const char* msg, uint16_t col) {
    tft.fillRect(0,162,W,22,C_BK);
    tft.setTextSize(2); tft.setTextColor(col);
    int tw=strlen(msg)*12;
    tft.setCursor(W/2-tw/2,164); tft.print(msg);
}
static void drawDgScreen() {
    tft.fillScreen(C_BK);
    for (int i=0;i<70;i++) {
        int x=(i*211+19)%W, y=(i*97+13)%(H-28)+5;
        uint16_t c=(i%5==0)?C_WH:(i%4==0)?C_CY:(i%3==0)?C_DCY:C_DG;
        tft.drawPixel(x,y,c);
    }
    tft.fillRect(0,0,W,30,C_CARD);
    tft.drawFastHLine(0,0,W,C_CY); tft.drawFastHLine(0,30,W,C_DCY);
    tft.fillCircle(14,15,5,C_CY); tft.fillCircle(14,15,2,C_BK);
    tft.setTextColor(C_WH);tft.setTextSize(1);tft.setCursor(25,10);tft.print("BRONNY AI");
    tft.setTextColor(C_CY);tft.setCursor(82,10);tft.print("v2.0");
    tft.fillRoundRect(W-100,6,96,18,4,C_DCY);tft.drawRoundRect(W-100,6,96,18,4,C_CY);
    tft.setTextColor(C_CY);tft.setCursor(W-94,12);tft.print("SPEECH ENGINE");
    tft.setTextSize(2);tft.setTextColor(C_WH);
    tft.setCursor(W/2-54,40);tft.print("DEEPGRAM");
    tft.setTextSize(1);tft.setTextColor(C_DCY);
    tft.setCursor(W/2-33,62);tft.print("nova-3  \xB7  ASR");
    tft.drawFastHLine(W/2-60,74,120,C_DG);
    drawDgAnim(W/2,118,0);
    drawDgStatus("Connecting...",C_YL);
    tft.fillRect(0,H-20,W,20,C_CARD);tft.drawFastHLine(0,H-20,W,C_DCY);
    tft.setTextColor(C_DG);tft.setTextSize(1);
    tft.setCursor(W/2-54,H-13);tft.print("Bronny AI v2.0");
}
static void connectDeepgram() {
    String authHdr = "Authorization: Token " + String(DEEPGRAM_API_KEY);
    dgWs.onEvent(onDgWsEvent);
    dgWs.setExtraHeaders(authHdr.c_str());
    dgWs.beginSSL("api.deepgram.com", 443, DG_PATH);
    dgLastConnectAttempt = millis();

    uint32_t deadline=millis()+DG_CONNECT_TIMEOUT;
    uint32_t lastAnim=millis(); int spin=0;
    while (!dgConnected && millis()<deadline) {
        dgWs.loop();
        if (millis()-lastAnim>280) {
            spin=(spin+1)%3;
            drawDgAnim(W/2,118,spin);
            lastAnim=millis();
        }
        yield();
    }
    drawDgAnim(W/2,118,0);
    drawDgStatus(dgConnected?"Connected!":"Timed Out", dgConnected?C_GR:C_WARN);
    delay(700); yield();
}

static void   partyLoop();
static void   videoLoop();
static int    checkPartyCommand(const char* t);
static String checkCurrentJob();   // polls /video/current

// ╔══════════════════════════════════════════════════════════════╗
// ║  RAILWAY STREAMING TTS                                       ║
// ╚══════════════════════════════════════════════════════════════╝
// Key changes from v1.2:
//   • Takes const char* instead of const String&
//   • Uses global gTtsDecoded — never stack-allocated
//   • Uses jEscBuf() — no String heap allocation for JSON body
//   • http.POST(uint8_t*, size_t) — avoids one more String copy
static bool callRailwayStream(const char* transcript) {
    if (!transcript || !*transcript) return false;
    if (!audioOk) return false;
    ensureWifi();
    if (WiFi.status() != WL_CONNECTED) return false;

    // Build JSON body entirely in fixed char buffers
    static char escaped[DG_FINAL_MAX + 32];
    static char body[DG_FINAL_MAX + 64];
    jEscBuf(transcript, escaped, sizeof(escaped));
    snprintf(body, sizeof(body), "{\"text\":\"%s\"}", escaped);

    String url = baseUrl() + "/voice/text";

    // All large locals are static — they never land on the stack.
    static uint8_t  railBuf[STREAM_CHUNK];
    static int16_t  primerSil[256];   // 512 B silence primer
    static int16_t  trailSil[128];    // 256 B silence trail
    static uint8_t  drainBuf[512];    // mic drain after TTS
    memset(primerSil, 0, sizeof(primerSil));
    memset(trailSil,  0, sizeof(trailSil));

    bool gotAudio = false;

    for (int attempt = 1; attempt <= 2; attempt++) {
        if (attempt > 1) { delay(2000); }

        setCrashPoint("Rail:connect");
        gHttpCli.setInsecure();
        gHttpCli.setConnectionTimeout(20000);
        HTTPClient http;
        http.begin(gHttpCli, url);
        http.setTimeout(45000);
        http.addHeader("Content-Type", "application/json");
        http.addHeader("X-Api-Key", AETHER_API_KEY);

        setCrashPoint("Rail:POST");
        // Use raw bytes overload — avoids constructing a temporary String
        int code = http.POST((uint8_t*)body, strlen(body));
        if (code != 200) {
            tftLogf(C_YL, "Rail: HTTP %d", code);
            http.end();
            continue;
        }

        // Stop mic before reinitialising the codec
        setCrashPoint("Rail:micStop");
        mic_stream.end();
        micOk = false;
        delay(60);

        setCrashPoint("Rail:audioTTS");
        audioInitTTS();
        delay(80);

        // Brief silence primer so codec DAC path stabilises
        {
            uint32_t primerEnd = millis() + 180;
            while (millis() < primerEnd) {
                i2s.write((uint8_t*)primerSil, sizeof(primerSil));
                roboEyes.update();
                yield();
            }
        }

        // ── Start global TTS decoder ─────────────────────────
        // gMp3Dec + gTtsDecoded are globals — no heap alloc here.
        setCrashPoint("Rail:mp3begin");
        gMp3Dec.begin();
        gTtsDecoded.begin();

        WiFiClient* stream  = http.getStreamPtr();
        int    contentLen   = http.getSize();
        size_t totalRead    = 0;
        uint32_t deadline   = millis() + 35000;
        bool   talkStarted  = false;
        uint32_t lastDataMs = 0;

        setState(ST_SPEAK);
        setCrashPoint("Rail:streaming");

        while (millis() < deadline) {
            size_t avail = (size_t)stream->available();
            if (avail > 0) {
                size_t got = stream->readBytes(
                    railBuf, min(avail, (size_t)sizeof(railBuf)));
                if (got > 0) {
                    if (!talkStarted) {
                        setStatus("Speaking...", C_GR);
                        startTalk();
                        talkStarted = true;
                    }
                    gTtsDecoded.write(railBuf, got);
                    totalRead  += got;
                    lastDataMs  = millis();
                }
            } else {
                delay(2);
            }

            if (talkStarted && lastDataMs > 0
                    && millis() - lastDataMs > STREAM_DATA_GAP_MS) break;
            if (!http.connected() && stream->available() == 0)   break;
            if (contentLen > 0 && (int)totalRead >= contentLen)   break;

            maintainDeepgram();
            roboEyes.update();
            yield();
        }

        setCrashPoint("Rail:streamDone");
        stopTalk();
        forceDrawFace();
        setStatus("Listening...", C_CY);

        gTtsDecoded.end();
        // Write trailing silence so the last MP3 frame fully clocks out
        i2s.write((uint8_t*)trailSil, sizeof(trailSil));
        roboEyes.update();

        http.end();
        gHttpCli.stop();   // fully release TLS session — frees ~3 KB

        if (totalRead >= MIN_AUDIO_BYTES) { gotAudio = true; break; }
        tftLogf(C_YL, "Rail: retry (got %uB)", (unsigned)totalRead);
    }

    // Restore rec-mode codec and re-open mic
    setCrashPoint("Rail:audioRec");
    audioInitRec();
    roboEyes.update();

    setCrashPoint("Rail:micInit");
    micInit();
    roboEyes.update();

    // Drain mic input ring-buffer: removes any echo/noise
    // that accumulated while the codec was in TX-only TTS mode.
    if (micOk) {
        uint32_t e = millis() + 300;
        while (millis() < e) {
            mic_stream.readBytes(drainBuf, sizeof(drainBuf));
            roboEyes.update();
            yield();
        }
    }

    clearCrashPoint();
    return gotAudio;
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  STANDBY MODE                                                ║
// ╚══════════════════════════════════════════════════════════════╝
static bool isWakeWord(const char* t) {
    if (!t || !*t) return false;
    // Lowercase copy
    char low[DG_FINAL_MAX];
    int li = 0;
    for (const char* p = t; *p && li < (int)sizeof(low)-1; p++, li++)
        low[li] = (char)tolower((uint8_t)*p);
    low[li] = '\0';

    static const char* ww[] = {
        "bronny","bronnie","brony","brownie","brawny","bonnie",
        "hi bronny","hey bronny","hi bronnie","hey bronnie",
        "hi brony","hey brony","hi brownie","hey brownie",
        "hi brawny","hey brawny","hi bonnie","hey bonnie",
        nullptr
    };
    for (int i = 0; ww[i]; i++)
        if (strstr(low, ww[i]) != nullptr) return true;
    return false;
}

static void enterStandby() {
    setCrashPoint("standby:face");
    setFaceSleep();
    roboEyes.update();
    setState(ST_STANDBY);
    setStatus("Standby...", C_DCY);
    tftLog(C_CY, "Standby — say 'Hi Bronny'");
    clearCrashPoint();
}

static void exitStandby() {
    lastRailwayMs = millis();
    // Explicitly clear sleepy flags before calling setFaceSurprised()
    // so the eyes actually open rather than staying half-closed.
    roboEyes.setSleepy(false);
    roboEyes.open();
    setFaceSurprised();
    roboEyes.update();
    jingleWake();
    ensureWifi();
    setState(ST_LISTEN);
    setStatus("Listening...", C_CY);
    tftLog(C_GR, "Bronny: awake!");
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  BOOT ANIMATION                                              ║
// ╚══════════════════════════════════════════════════════════════╝
static TFT_eSprite* bootLogo = nullptr;
#define BLOGO_SZ 128
#define BLOGO_X  ((W - BLOGO_SZ) / 2)
#define BLOGO_Y  14

static void blitLogo() {
    if (!bootLogo) return;
    uint16_t* buf = (uint16_t*)bootLogo->getPointer();
    if (!buf) return;
    tft.startWrite();
    tft.setAddrWindow(BLOGO_X, BLOGO_Y, BLOGO_SZ, BLOGO_SZ);
    for (int y = 0; y < BLOGO_SZ; y++) {
        tft.pushPixels(buf + y * BLOGO_SZ, BLOGO_SZ);
        if ((y & 15) == 15) yield();
    }
    tft.endWrite();
}

static void drawBootStars() {
    for (int i = 0; i < 80; i++) {
        int x = (i*137+11) % W;
        int y = (i*93 +7)  % (H-28) + 5;
        uint16_t col = (i%5==0)?C_WH:(i%4==0)?C_CY:(i%3==0)?C_DCY:C_DG;
        tft.drawPixel(x, y, col);
        if (i%6==0) tft.drawPixel(x+1, y, dimCol(col,3));
    }
}

static void logoDrawB(int sc) {
    if (!bootLogo || sc <= 0) return;
    bootLogo->fillSprite(C_BK);
    int bh  = sc*10, bsw = sc*3/2, bmpW = sc*6, bmpH = bh/2, br = max(2,sc);
    int bx  = BLOGO_SZ/2 - bmpW/2, by = BLOGO_SZ/2 - bh/2;
    if (sc >= 5) {
        bootLogo->drawCircle(64,64,52,C_DCY);
        bootLogo->drawCircle(64,64,54,dimCol(C_CY,3));
        bootLogo->drawCircle(64,64,56,dimCol(C_CY,1));
    }
    bootLogo->fillRect(bx, by, bsw, bh, C_CY);
    bootLogo->fillRoundRect(bx, by, bmpW, bmpH+br/2, br, C_CY);
    if (bmpW-bsw-br > 2 && bmpH-br*2 > 2)
        bootLogo->fillRoundRect(bx+bsw, by+br, bmpW-bsw-br, bmpH-br+br/2-br, max(1,br-2), C_BK);
    bootLogo->fillRoundRect(bx, by+bmpH, bmpW+sc/2, bmpH, br, C_MINT);
    if (bmpW-bsw-br > 2 && bmpH-br*2 > 2)
        bootLogo->fillRoundRect(bx+bsw, by+bmpH+br, bmpW-bsw-br+sc/2, bmpH-br*2, max(1,br-2), C_BK);
}

static void logoDrawRobot(int morph) {
    if (!bootLogo) return;
    bootLogo->fillSprite(C_BK);
    int m = constrain(morph, 0, 20);
    bootLogo->drawCircle(64,64,52,C_DCY);
    bootLogo->drawCircle(64,64,54,dimCol(C_CY,3));
    bootLogo->drawCircle(64,64,56,dimCol(C_CY,1));

    int fx0=lerpi(58,20,m,20), fy0=lerpi(24,22,m,20);
    int fx1=lerpi(104,108,m,20), fy1=lerpi(104,96,m,20);
    int fw=fx1-fx0, fh=fy1-fy0;
    bootLogo->fillRoundRect(fx0,fy0,fw,fh,8,0x0412);
    bootLogo->drawRoundRect(fx0,fy0,fw,fh,8,C_CY);
    bootLogo->drawRoundRect(fx0+1,fy0+1,fw-2,fh-2,7,dimCol(C_CY,4));

    int lx0=lerpi(58,28,m,20), ly0=lerpi(24,38,m,20);
    int lew=lerpi(48,28,m,20), leh=lerpi(44,20,m,20), ler=lerpi(8,4,m,20);
    bootLogo->fillRoundRect(lx0,ly0,lew,leh,ler,C_WH);

    int rx0=lerpi(58,72,m,20), ry0=lerpi(24,38,m,20);
    int rew=lerpi(50,28,m,20), reh=lerpi(40,20,m,20), rer=lerpi(8,4,m,20);
    bootLogo->fillRoundRect(rx0,ry0,rew,reh,rer,C_WH);

    if (m > 10) {
        int pa=m-10, pr=lerpi(0,5,pa,10);
        int lcx=lx0+lew/2, lcy=ly0+leh/2;
        int rcx=rx0+rew/2, rcy=ry0+reh/2;
        if (pr > 0) {
            bootLogo->fillCircle(lcx,lcy,pr,C_BK);
            bootLogo->fillCircle(rcx,rcy,pr,C_BK);
            if (pr >= 3) {
                bootLogo->drawPixel(lcx+1,lcy-1,C_WH);
                bootLogo->drawPixel(rcx+1,rcy-1,C_WH);
            }
        }
        if (pa > 5) {
            bootLogo->drawCircle(lcx,lcy,pr+2,dimCol(C_CY,3));
            bootLogo->drawCircle(rcx,rcy,pr+2,dimCol(C_CY,3));
        }
    }
    if (m < 10) {
        int sw=lerpi(12,0,m,10);
        if (sw > 0) {
            uint16_t sc2=dimCol(C_CY,lerpi(8,1,m,10));
            bootLogo->fillRect(64-sw/2,lerpi(24,22,m,10),sw,lerpi(80,74,m,10),sc2);
        }
    }
    if (m > 6) {
        int aa=m-6;
        int atip=lerpi(fy0,fy0-16,aa,14), abal=lerpi(0,4,aa,14);
        bootLogo->drawFastVLine(64,atip,fy0-atip,C_CY);
        if (abal > 0) {
            bootLogo->fillCircle(64,atip,abal,C_CY);
            if (abal >= 3) bootLogo->fillCircle(64,atip,2,C_WH);
        }
    }
    if (m > 14) {
        int ma=m-14, mx_c=(fx0+fx1)/2;
        int mw=lerpi(0,32,ma,6), my=lerpi(fy1+6,fy1-12,ma,6);
        if (mw > 2) bootLogo->fillRoundRect(mx_c-mw/2,my,mw,6,3,C_WH);
    }
}

static void drawBootBar(int pct) {
    if (pct > 100) pct = 100;
    const int BX=40, BY=H-14, BW=W-80, BH=7;
    tft.fillRoundRect(BX-1,BY-1,BW+2,BH+2,4,C_CARD);
    tft.fillRoundRect(BX,BY,BW,BH,3,0x0841);
    int fw = (int)((float)BW*pct/100.f);
    if (fw > 3) {
        tft.fillRoundRect(BX,BY,fw,BH,3,C_CY);
        if (fw > 6) tft.fillRoundRect(BX,BY,fw-3,BH,3,C_MINT);
        tft.drawFastHLine(BX,BY,fw,C_WH);
    }
}

static void drawBootLogo() {
    tft.fillScreen(C_BK);
    drawBootStars();
    bootLogo = new TFT_eSprite(&tft);
    if (!bootLogo) { return; }
    bootLogo->setAttribute(PSRAM_ENABLE, true);
    if (!bootLogo->createSprite(BLOGO_SZ, BLOGO_SZ)) {
        delete bootLogo; bootLogo = nullptr; return;
    }
    logoDrawB(8);
    blitLogo();
    drawBootBar(0);
}

static void playBootIntroAnim(int /*cx*/, int /*cy*/) {
    if (!bootLogo) return;
    const int TY = BLOGO_Y + BLOGO_SZ + 4;
    for (int f=0;f<14;f++) { logoDrawB(lerpi(1,8,f,13)); blitLogo(); delay(16); yield(); }
    logoDrawB(8); blitLogo();
    for (int f=0;f<22;f++) { logoDrawRobot(f*20/21); blitLogo(); delay(16); yield(); }
    logoDrawRobot(20); blitLogo();
    for (int f=0;f<10;f++) {
        logoDrawRobot(20);
        int r=52+f*3;
        if (r<70) bootLogo->drawCircle(64,64,r,dimCol(C_CY,max(1,7-f)));
        blitLogo(); delay(20); yield();
    }
    for (int f=0;f<18;f++) {
        int ty = TY + max(0,18-f);
        tft.fillRect(0, TY, W, 46, C_BK);
        int bright = min(8,f+1);
        tft.setTextSize(3); tft.setTextColor(dimCol(C_WH,bright));
        tft.setCursor(W/2-54, ty); tft.print("BRONNY");
        if (f > 8) {
            tft.fillRoundRect(W/2+58,ty-1,30,18,4,C_CY);
            tft.setTextSize(1); tft.setTextColor(C_BK);
            tft.setCursor(W/2+63,ty+5); tft.print("AI");
        }
        if (f > 12) {
            const char* credit = "by Patrick Perez  v2.0";
            tft.setTextSize(1);
            tft.setTextColor(dimCol(C_LG, min((f-12)*2,8)));
            tft.setCursor(W/2-(int)strlen(credit)*3, ty+26);
            tft.print(credit);
        }
        delay(16); yield();
    }
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  WIFI SCREEN                                                 ║
// ╚══════════════════════════════════════════════════════════════╝
static void drawWifiScreen() {
    tft.fillScreen(C_BK);
    for (int i=0;i<70;i++){
        int x=(i*137+31)%W, y=(i*93+17)%(H-28)+5;
        uint16_t col=(i%5==0)?C_WH:(i%4==0)?C_CY:(i%3==0)?C_DCY:C_DG;
        tft.drawPixel(x,y,col);
    }
    tft.fillRect(0,0,W,30,C_CARD);
    tft.drawFastHLine(0,0,W,C_CY); tft.drawFastHLine(0,30,W,C_DCY);
    tft.fillCircle(14,15,5,C_CY); tft.fillCircle(14,15,2,C_BK);
    tft.setTextColor(C_WH);tft.setTextSize(1);tft.setCursor(25,10);tft.print("BRONNY AI");
    tft.setTextColor(C_CY);tft.setCursor(82,10);tft.print("v2.0");
    tft.fillRoundRect(W-88,6,84,18,4,C_DCY);tft.drawRoundRect(W-88,6,84,18,4,C_CY);
    tft.setTextColor(C_CY);tft.setCursor(W-82,12);tft.print("NETWORK SETUP");
    int ix=W/2, iy=78;
    tft.drawCircle(ix,iy+20,36,C_DG);
    tft.drawCircle(ix,iy+20,26,C_DCY);
    tft.drawCircle(ix,iy+20,16,C_CY);
    tft.fillCircle(ix,iy+20, 6,C_CY);
    tft.fillRect(ix-40,iy+20,80,44,C_BK);
    tft.fillRoundRect(16,130,W-32,26,5,C_CARD);
    tft.drawRoundRect(16,130,W-32,26,5,C_CY);
    tft.setTextColor(C_DCY);tft.setTextSize(1);tft.setCursor(28,137);tft.print("Network:");
    tft.setTextColor(C_WH);tft.setCursor(86,137);tft.print(WIFI_SSID);
    tft.fillRect(0,H-22,W,22,C_CARD);tft.drawFastHLine(0,H-22,W,C_DCY);
    tft.setTextColor(C_DG);tft.setTextSize(1);tft.setCursor(6,H-14);tft.print("ESP32-S3");
    tft.setTextColor(C_DCY);tft.setCursor(W/2-48,H-14);tft.print("Bronny AI v2.0");
    tft.setTextColor(C_DG);tft.setCursor(W-72,H-14);tft.print("Patrick 2025");
}

static void drawWifiStatus(const char* l1, uint16_t c1,
                           const char* l2 = "", uint16_t c2 = C_CY) {
    tft.fillRect(0,160,W,H-22-160,C_BK);
    tft.setTextSize(2); tft.setTextColor(c1);
    int tw=(int)strlen(l1)*12; tft.setCursor(W/2-tw/2,166); tft.print(l1);
    if (strlen(l2) > 0) {
        tft.setTextSize(1); tft.setTextColor(c2);
        int tw2=(int)strlen(l2)*6; tft.setCursor(W/2-tw2/2,192); tft.print(l2);
    }
}

static uint8_t  wifiSpinIdx  = 0;
static uint32_t wifiLastSpin = 0;
static void tickWifiSpinner() {
    uint32_t now=millis(); if (now-wifiLastSpin < 250) return; wifiLastSpin=now;
    static const char* frames[]={"|","/","-","\\"};
    tft.fillRect(W/2-4,113,8,10,C_BK);
    tft.setTextSize(1); tft.setTextColor(C_CY);
    tft.setCursor(W/2-3,114); tft.print(frames[wifiSpinIdx++%4]);
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  BOOT INTRO  (optional — disabled by default)               ║
// ╚══════════════════════════════════════════════════════════════╝
static void doBootIntro() {
    if (!dgConnected) return;
    tftLog(C_CY, "Bronny: hello!");
    setFaceThink();
    dgStreaming = false;
    bool ok = callRailwayStream("bootup_intro");
    if (!ok) tftLog(C_RD, "Intro: Railway failed");
    if (ok) {
        setFaceHappy();
        uint32_t e = millis() + 1200;
        while (millis() < e) {
            roboEyes.update();
            maintainDeepgram();
            yield();
        }
    }
    stopTalk(); setFaceIdle(); forceDrawFace();
    lastRailwayMs   = millis();
    dgStreaming     = true;
    dgLastKeepalive = millis();
    setFaceListen();
    setState(ST_LISTEN);
    setStatus("Listening...", C_CY);
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  CONVERSATION PIPELINE                                       ║
// ╚══════════════════════════════════════════════════════════════╝
static void runConversation() {
    if (isBusy()) return;
    setState(ST_THINK);
    dgStreaming = false;

    pendingTranscript = false;
    dgFinalReceivedAt = 0;

    // Snapshot transcript into a local static buffer and clear globals
    static char transcript[DG_FINAL_MAX];
    strncpy(transcript, gDgFinal, DG_FINAL_MAX-1);
    transcript[DG_FINAL_MAX-1] = '\0';
    // Trim leading/trailing spaces
    {
        char* e = transcript + strlen(transcript) - 1;
        while (e > transcript && *e == ' ') *e-- = '\0';
        char* s = transcript;
        while (*s == ' ') s++;
        if (s != transcript) memmove(transcript, s, strlen(s)+1);
    }
    gDgFinal[0]   = '\0';
    gDgPartial[0] = '\0';

    // ── Party voice commands (highest priority) ────────────
    int partyCmd = checkPartyCommand(transcript);
    if (partyCmd == 1 && !isPartyMode()) {
        tftLog(C_YL, "Party: ON");
        jingleParty();
        enterPartyMode();
        // enterPartyMode sets state and dgStreaming
        return;
    }
    if (partyCmd == -1 && isPartyMode()) {
        exitPartyMode();
        dgStreaming     = true;
        dgLastKeepalive = millis();
        setState(ST_LISTEN);
        return;
    }
    if (isPartyMode()) {
        // Non-party command received in party mode — just ignore
        dgStreaming     = true;
        dgLastKeepalive = millis();
        setState(ST_LISTEN);
        return;
    }

    // ── Standby wake-word check ────────────────────────────
    if (isStandby()) {
        if (isWakeWord(transcript)) {
            exitStandby();
            // Drain mic ring-buffer so jingle echo doesn't reach DG
            if (micOk) {
                static uint8_t drain[512];
                uint32_t de = millis() + 400;
                while (millis() < de) {
                    mic_stream.readBytes(drain, sizeof(drain));
                    maintainDeepgram();
                    roboEyes.update();
                    yield();
                }
            }
            gDgFinal[0]       = '\0';
            gDgPartial[0]     = '\0';
            pendingTranscript = false;
            dgFinalReceivedAt = 0;
        } else {
            Serial.printf("[Standby] Ignored: '%s'\n", transcript);
        }
        dgStreaming = true; dgLastKeepalive = millis();
        setState(ST_LISTEN);
        return;
    }

    // ── Noise filter ───────────────────────────────────────
    if (isNoise(transcript)) {
        tftLogf(C_YL, "Filtered: '%s'", transcript);
        dgStreaming = true; dgLastKeepalive = millis();
        setStatus("Listening...", C_CY);
        setState(ST_LISTEN);
        return;
    }

    // ── Normal conversation ────────────────────────────────
    tftLogf(C_MINT, "You: %s", transcript);
    setFaceThink();

    // Subtle two-note "thinking" chime
    i2s.setVolume(0.18f);
    audioInitRec();   // ensure rec-mode for sine gen
    playTone(1047, 55); playSil(20); playTone(1319, 80);
    i2s.setVolume(VOL_MAIN);

    tftLog(C_YL, "Railway: thinking...");
    bool ok = callRailwayStream(transcript);

    if (!ok) {
        tftLog(C_RD, "Railway: failed");
        stopTalk(); forceDrawFace();
        dgStreaming     = true;
        dgLastKeepalive = millis();
        setFaceListen();
        setStatus("Listening...", C_CY);
        setState(ST_LISTEN);
        return;
    }

    lastRailwayMs    = millis();
    vadCooldownUntil = millis() + TTS_COOLDOWN_MS;
    setFaceHappy();

    // Drain mic during TTS cooldown so loudspeaker output
    // does not loop back as a new transcript.
    if (micOk) {
        static uint8_t drain[512];
        uint32_t de = millis() + TTS_COOLDOWN_MS;
        while (millis() < de) {
            mic_stream.readBytes(drain, sizeof(drain));
            maintainDeepgram();
            roboEyes.update();
            yield();
        }
    }

    dgStreaming       = true;
    dgLastKeepalive   = millis();
    pendingTranscript = false;
    gDgFinal[0]       = '\0';
    gDgPartial[0]     = '\0';
    dgFinalReceivedAt = 0;
    setFaceListen();
    setStatus("Listening...", C_CY);
    setState(ST_LISTEN);
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  TFT DIAGNOSTICS  (no serial monitor needed)                 ║
// ╚══════════════════════════════════════════════════════════════╝
static void showResetReason() {
    esp_reset_reason_t r = esp_reset_reason();
    const char* label = "UNKNOWN"; uint16_t col = C_YL;
    switch (r) {
        case ESP_RST_POWERON:  label="Power-on";       col=C_GR;  break;
        case ESP_RST_SW:       label="SW reset";        col=C_CY;  break;
        case ESP_RST_PANIC:    label="PANIC/Crash!";    col=C_RD;  break;
        case ESP_RST_INT_WDT:  label="INT Watchdog!";   col=C_RD;  break;
        case ESP_RST_TASK_WDT: label="Task Watchdog!";  col=C_RD;  break;
        case ESP_RST_WDT:      label="Watchdog!";       col=C_RD;  break;
        case ESP_RST_BROWNOUT: label="BROWNOUT!";       col=C_RD;  break;
        case ESP_RST_SDIO:     label="SDIO reset";      col=C_YL;  break;
        default: break;
    }
    char buf[54];
    snprintf(buf, sizeof(buf), "Boot: %s", label);
    tftLog(col, buf);
    if (r == ESP_RST_PANIC && rtcCrashMagic == CRASH_MAGIC && rtcCrashMsg[0]) {
        char buf2[54];
        snprintf(buf2, sizeof(buf2), "At: %s", rtcCrashMsg);
        tftLog(C_RD, buf2);
    }
    clearCrashPoint();
}

static void showBootMemory() {
    uint32_t heap  = heap_caps_get_free_size(MALLOC_CAP_8BIT);
    uint32_t psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    char buf[54];
    snprintf(buf, sizeof(buf), "H:%uKB  P:%uKB", heap/1024, psram/1024);
    tftLog(C_DCY, buf);
}

static void checkMemory(uint32_t now) {
    if (now - lastMemCheckMs < 60000UL) return;
    lastMemCheckMs = now;
    uint32_t heap  = heap_caps_get_free_size(MALLOC_CAP_8BIT);
    uint32_t psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    if (!logsVisible) {
        char buf[54];
        snprintf(buf,sizeof(buf),"H:%uK P:%uK",heap/1024,psram/1024);
        setStatus(buf, heap < 30000 ? C_RD : C_DCY);
    }
    if (heap  < 20000) tftLogf(C_RD, "LOW HEAP: %uKB!", heap/1024);
    if (psram < 50000) tftLogf(C_RD, "LOW PSRAM: %uKB!", psram/1024);
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  SETUP                                                       ║
// ╚══════════════════════════════════════════════════════════════╝
void setup() {
    Serial.begin(115200);
    AudioToolsLogger.begin(Serial, AudioToolsLogLevel::Warning);
    delay(400);

    pinMode(PIN_BLK,  OUTPUT); digitalWrite(PIN_BLK,  HIGH);
    pinMode(PIN_PA,   OUTPUT); digitalWrite(PIN_PA,   LOW);
    pinMode(PIN_BOOT, INPUT_PULLUP);

    tft.init(); tft.setRotation(1); tft.fillScreen(C_BK);

    // ── RoboEyes ──────────────────────────────────────────
    setCrashPoint("setup:roboEyes");
    roboEyes.begin(50);
    roboEyes.setAutoblinker(true, 3, 2);
    roboEyes.setIdleMode(true, 3, 2);
    roboEyes.setCuriosity(true);
    roboEyes.setColors(C_WH, C_BK);

    // ── MJPEG buffer — allocate FIRST before anything else
    //    fragments PSRAM.  If this fails, video mode is silently
    //    disabled (mjpegBuf stays nullptr, poll skips).
    setCrashPoint("setup:mjpegBuf");
    if (psramFound()) {
        mjpegBuf = (uint8_t*)ps_malloc(VIDEO_MJPEG_BUF);
        if (mjpegBuf)
            Serial.printf("[Init] mjpegBuf: %u KB PSRAM\n", VIDEO_MJPEG_BUF/1024);
        else
            Serial.println("[Init] WARN: mjpegBuf alloc failed");
    } else {
        Serial.println("[Init] WARN: No PSRAM");
    }

    // ── Audio + Mic ───────────────────────────────────────
    setCrashPoint("setup:audio");
    audioRestart();
    i2s.setVolume(VOL_MAIN);
    if (audioOk) {
        auto sc = sineGen.defaultConfig();
        sc.copyFrom(ainf_rec);
        sineGen.begin(sc);
    }

    setCrashPoint("setup:mic");
    micInit();
    logsVisible = true;

    // ── Boot animation ────────────────────────────────────
    drawBootLogo();
    playBootIntroAnim(W/2, H/2-32);
    drawBootBar(10);
    jingleBoot();
    drawBootBar(55); delay(150);
    drawBootBar(100); delay(300);

    if (bootLogo) { bootLogo->deleteSprite(); delete bootLogo; bootLogo = nullptr; }

    // ── WiFi ──────────────────────────────────────────────
    setCrashPoint("setup:wifi");
    drawWifiScreen();
    drawWifiStatus("Connecting...", C_YL);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    bool connected = false;
    uint32_t ws = millis();
    while (millis()-ws < 18000) {
        if (WiFi.status() == WL_CONNECTED) { connected = true; break; }
        tickWifiSpinner();
        yield();
    }
    if (connected) {
        char ip[32]; snprintf(ip,32,"%s",WiFi.localIP().toString().c_str());
        drawWifiStatus("Connected!", C_GR, ip, C_CY);
        jingleConnect(); delay(900);
    } else {
        drawWifiStatus("FAILED", C_RD, "Check config", C_RD);
        jingleError(); delay(2000);
    }

    // ── Heartbeat ─────────────────────────────────────────
    setCrashPoint("setup:heartbeat");
    sendHeartbeat();
    lastHbMs = millis();

    // ── Deepgram ──────────────────────────────────────────
    setCrashPoint("setup:deepgram");
    drawDgScreen();
    connectDeepgram();
    dgStreaming = true;

    clearCrashPoint();   // setup fully complete — clear breadcrumb

    // ── Initial face + log ────────────────────────────────
    tft.fillScreen(C_BK);
    setLogsVisible(false);
    jingleReady();

    tftLog(C_GR,  "Bronny AI v2.0 ready");
    tftLogf(C_CY, "WiFi: %s", WiFi.localIP().toString().c_str());
    tftLog(C_LG,  "Say something or 'party on'");
    tftLog(C_DCY, "BOOT btn = toggle logs");
    showResetReason();
    showBootMemory();

    setState(ST_LISTEN);
    setFaceListen();
    setStatus("Listening...", C_CY);

    lastRailwayMs = millis();
    bootReadyAt   = millis() + 2000;
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  MAIN LOOP                                                   ║
// ╚══════════════════════════════════════════════════════════════╝
void loop() {
    uint32_t now = millis();

    // ── VIDEO MODE fast-path ──────────────────────────────
    // videoLoop() defined in Part 3.
    if (isVideoMode()) {
        videoLoop();
        if (now - lastHbMs > HEARTBEAT_MS) { lastHbMs = now; sendHeartbeat(); }
        yield();
        return;
    }

    // ── PARTY MODE fast-path ──────────────────────────────
    // partyLoop() defined in Part 3.
    if (isPartyMode()) {
        partyLoop();
        if (now - lastHbMs > HEARTBEAT_MS) { lastHbMs = now; sendHeartbeat(); }
        // Check for "party off" (or other voice command)
        if (!pendingTranscript && gDgFinal[0] && dgFinalReceivedAt > 0
                && now - dgFinalReceivedAt > DG_FINAL_TIMEOUT_MS) {
            pendingTranscript = true;
            dgFinalReceivedAt = 0;
        }
        if (pendingTranscript && !isBusy() && now > vadCooldownUntil)
            runConversation();
        yield();
        return;
    }

    // ── NORMAL / STANDBY MODE ─────────────────────────────
    checkBootButton();
    roboEyes.update();
    checkMemory(now);

    if (now - lastHbMs > HEARTBEAT_MS) {
        lastHbMs = now;
        sendHeartbeat();
        roboEyes.update();
    }

    maintainDeepgram();
    roboEyes.update();

    // Boot intro (disabled by default — set bootIntroDone=false to enable)
    if (!bootIntroDone && !isBusy() && dgConnected && now > bootReadyAt) {
        bootIntroDone = true;
        doBootIntro();
        return;
    }

    // Auto-standby after STANDBY_TIMEOUT_MS of silence
    if (!isStandby() && !isBusy()
            && lastRailwayMs > 0
            && now - lastRailwayMs > STANDBY_TIMEOUT_MS)
        enterStandby();

    // DG timeout commit — fires for transcripts that never got
    // speech_final (e.g. the user stopped mid-sentence)
    if (!pendingTranscript && gDgFinal[0] && dgFinalReceivedAt > 0
            && now - dgFinalReceivedAt > DG_FINAL_TIMEOUT_MS) {
        pendingTranscript = true;
        dgFinalReceivedAt = 0;
    }

    if (pendingTranscript && !isBusy() && now > vadCooldownUntil)
        runConversation();

    // ── VIDEO POLL  ───────────────────────────────────────
    // Only poll when in normal listen state, not busy, and the
    // MJPEG buffer actually allocated.  checkCurrentJob() is in Part 3.
    if (isListening() && !isBusy() && mjpegBuf != nullptr
            && now - lastVideoPollMs > VIDEO_POLL_MS) {
        lastVideoPollMs = now;
        String jobId = checkCurrentJob();
        if (jobId.length() > 0) {
            tftLogf(C_PNK, "Video: %s", jobId.c_str());
            enterVideoMode(jobId.c_str());
            return;
        }
    }

    // ── STANDALONE LED TICK (NEW v2.1) — defined after party section ──
    tickStandaloneLed(now);

    // ── Serial debug shortcuts ────────────────────────────
    if (Serial.available()) {
        char c = Serial.read();
        // 'm' — memory + DG status dump
        if (c == 'm') {
            uint32_t h = heap_caps_get_free_size(MALLOC_CAP_8BIT);
            uint32_t p = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
            tftLogf(C_CY,"DG=%d str=%d st=%d H=%uK P=%uK",
                    dgConnected?1:0, dgStreaming?1:0,
                    (int)gState, h/1024, p/1024);
        }
        // 'l' — toggle logs
        if (c == 'l') setLogsVisible(!logsVisible);
        // 'p' — toggle party mode
        if (c == 'p') {
            if (!isPartyMode()) { jingleParty(); enterPartyMode(); }
            else                  exitPartyMode();
        }
    }

    yield();
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  PARTY MODE                                                  ║
// ╚══════════════════════════════════════════════════════════════╝

// ── WS2812B LED (GPIO 48, shared with PA enable) ────────────────
#define PARTY_LED_PIN    48
#define PARTY_LED_COUNT   1
#define PARTY_LED_BRIGHT 210

static Adafruit_NeoPixel partyLed(PARTY_LED_COUNT, PARTY_LED_PIN,
                                   NEO_GRB + NEO_KHZ800);

// ── FFT / visualiser constants ──────────────────────────────────
#define PARTY_FFT_SIZE   256
#define PARTY_NUM_BANDS   24
#define PARTY_MODE_SEC    30     // seconds per visualiser / LED mode
#define PARTY_GAIN        14.0f

#define VIZ_W           320
#define VIZ_H           240
#define VIZ_BAR_W        12
#define VIZ_BAR_GAP       1
#define VIZ_BAR_STRIDE   (VIZ_BAR_W + VIZ_BAR_GAP)
#define VIZ_ORIGIN_X     ((VIZ_W - PARTY_NUM_BANDS * VIZ_BAR_STRIDE + VIZ_BAR_GAP) / 2)
#define VIZ_C_BG         0x0841

// ── Party state ─────────────────────────────────────────────────
static uint8_t  partyVizMode = 0;
static uint8_t  partyLedMode = 0;
static uint32_t partyModeTs  = 0;

// ── FFT buffers (PSRAM-backed via global alloc) ─────────────────
static float partyVR[PARTY_FFT_SIZE], partyVI[PARTY_FFT_SIZE];
static ArduinoFFT<float> partyFFT(partyVR, partyVI,
                                   PARTY_FFT_SIZE, 16000.0f);

static int   pBinLo[PARTY_NUM_BANDS];
static int   pBinHi[PARTY_NUM_BANDS];

// ── Band smoothing + peak hold ──────────────────────────────────
static float pSmBand [PARTY_NUM_BANDS] = {};
static float pSmPeak [PARTY_NUM_BANDS] = {};
static int   pPeakTmr[PARTY_NUM_BANDS] = {};
static float pLevel      = 0.0f;
static float pBass       = 0.0f;
static bool  pBeat       = false;
static float pAvgLevel   = 0.0f;
static uint32_t pLastBeatMs = 0;

// ── Per-frame delta buffers (partial-erase rendering) ───────────
static float pPrevH [PARTY_NUM_BANDS] = {};
static float pPrevP [PARTY_NUM_BANDS] = {};
static float pPrevMH[PARTY_NUM_BANDS] = {};
static int   pPrevLvlPx = 0;
static float pFlashAmt  = 0.0f;
static float pHueBase   = 0.0f;

// ── LED state ───────────────────────────────────────────────────
static float    pLedHue    = 0.0f;
static float    pLedSmooth = 0.0f;
static uint32_t pStrobeMs  = 0;

// ── Mic PCM buffers for party audio (static = off stack) ────────
static int32_t partyRawBuf[PARTY_FFT_SIZE];
static int16_t partyPcmBuf[PARTY_FFT_SIZE];

// ── Logarithmic band map ────────────────────────────────────────
// Called lazily inside enterPartyMode() on first use.
static void buildPartyBandMap() {
    const float lo = 1.5f, hi = PARTY_FFT_SIZE / 2.0f - 1.0f;
    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        pBinLo[b] = max(1, (int)(lo * powf(hi/lo, (float)b        /PARTY_NUM_BANDS)));
        pBinHi[b] = max(pBinLo[b] + 1,
                        (int)(lo * powf(hi/lo, (float)(b+1)/PARTY_NUM_BANDS)));
    }
}

// ── HSV → RGB565 ────────────────────────────────────────────────
static uint16_t p_hsvTo565(float h, float s, float v) {
    h = fmodf(h + 720.f, 360.f);
    float c = v*s, x = c*(1.f - fabsf(fmodf(h/60.f, 2.f) - 1.f)), m = v-c;
    float r, g, b;
    if      (h <  60) { r=c+m; g=x+m; b=m;   }
    else if (h < 120) { r=x+m; g=c+m; b=m;   }
    else if (h < 180) { r=m;   g=c+m; b=x+m; }
    else if (h < 240) { r=m;   g=x+m; b=c+m; }
    else if (h < 300) { r=x+m; g=m;   b=c+m; }
    else              { r=c+m; g=m;   b=x+m; }
    return ((uint16_t)(r*31)<<11) | ((uint16_t)(g*63)<<5) | (uint16_t)(b*31);
}

// ── HSV → NeoPixel uint32_t ─────────────────────────────────────
static uint32_t p_neoHSV(float h, float s, float v) {
    h = fmodf(h + 720.f, 360.f);
    float c = v*s, x = c*(1.f - fabsf(fmodf(h/60.f, 2.f) - 1.f)), m = v-c;
    float r, g, b;
    if      (h <  60) { r=c+m; g=x+m; b=m;   }
    else if (h < 120) { r=x+m; g=c+m; b=m;   }
    else if (h < 180) { r=m;   g=c+m; b=x+m; }
    else if (h < 240) { r=m;   g=x+m; b=c+m; }
    else if (h < 300) { r=x+m; g=m;   b=c+m; }
    else              { r=c+m; g=m;   b=x+m; }
    return partyLed.Color((uint8_t)(r*255), (uint8_t)(g*255), (uint8_t)(b*255));
}

// ── Bar colour: cohesive hue family, always ≥ 45% brightness ────
static uint16_t p_barColor(int band, float norm, float hBase) {
    float h = fmodf(hBase + (float)band * 3.5f + norm * 25.f, 360.f);
    float s = 0.65f + norm * 0.35f;
    float v = 0.45f + norm * 0.55f;
    return p_hsvTo565(h, s, v);
}

// ── Read mic, run FFT, update bands, forward PCM to Deepgram ────
static void partyProcessAudio() {
    if (!micOk) return;
    int need = PARTY_FFT_SIZE * (int)sizeof(int32_t);

    // Drain excess to prevent latency buildup
    while (mic_stream.available() > need * 3) {
        int32_t discard[64];
        mic_stream.readBytes((uint8_t*)discard, sizeof(discard));
    }
    if (mic_stream.available() < need) return;
    int got = mic_stream.readBytes((uint8_t*)partyRawBuf, need);
    if (got < need) return;

    // Forward 16-bit PCM to Deepgram for voice commands
    if (dgConnected) {
        for (int i = 0; i < PARTY_FFT_SIZE; i++)
            partyPcmBuf[i] = inmp441Sample(partyRawBuf[i]);
        dgWs.sendBIN((uint8_t*)partyPcmBuf, PARTY_FFT_SIZE * 2);
    }

    // Hann-windowed FFT
    for (int i = 0; i < PARTY_FFT_SIZE; i++) {
        float s = (float)(partyRawBuf[i] >> 14) / 32768.f;
        float w = 0.5f * (1.f - cosf(TWO_PI * i / (PARTY_FFT_SIZE - 1)));
        partyVR[i] = s * w;  partyVI[i] = 0.f;
    }
    partyFFT.compute(FFTDirection::Forward);
    partyFFT.complexToMagnitude();

    // Collapse bins into logarithmic bands
    float rawB[PARTY_NUM_BANDS], totLevel = 0.f;
    const float ns = PARTY_GAIN / (float)PARTY_FFT_SIZE;
    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        float sum = 0.f;
        int   cnt = pBinHi[b] - pBinLo[b];
        for (int k = pBinLo[b]; k < pBinHi[b]; k++) sum += partyVR[k];
        rawB[b] = constrain(sum * ns / (float)cnt, 0.f, 1.f);
        totLevel += rawB[b];
    }
    totLevel = constrain(totLevel / PARTY_NUM_BANDS, 0.f, 1.f);

    // Asymmetric smoothing: fast attack, slow decay
    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        pSmBand[b] = (rawB[b] > pSmBand[b])
                   ? 0.70f * rawB[b] + 0.30f * pSmBand[b]
                   : pSmBand[b] * 0.80f;
        if      (pSmBand[b] >= pSmPeak[b]) { pSmPeak[b] = pSmBand[b]; pPeakTmr[b] = 35; }
        else if (pPeakTmr[b] > 0)          { --pPeakTmr[b]; }
        else                               { pSmPeak[b] *= 0.93f; }
    }

    // Bass energy + beat detection
    float bass = 0.f;
    for (int b = 0; b < 5; b++) bass += pSmBand[b];
    bass /= 5.f;
    pAvgLevel = 0.95f * pAvgLevel + 0.05f * totLevel;
    uint32_t now = millis();
    pBeat = (bass > pAvgLevel * 1.6f) && (bass > 0.06f)
            && ((now - pLastBeatMs) > 200);
    if (pBeat) pLastBeatMs = now;
    pLevel = totLevel;  pBass = bass;
}

// ── Visualiser: upward spectrum bars with peak dots ─────────────
static void partyDrawSpectrum() {
    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        int x      = VIZ_ORIGIN_X + b * VIZ_BAR_STRIDE;
        int newPx  = constrain((int)(pSmBand[b] * VIZ_H), 0, VIZ_H);
        int peakPx = constrain((int)(pSmPeak[b] * VIZ_H), 0, VIZ_H);
        int oldPx  = constrain((int)(pPrevH[b]  * VIZ_H), 0, VIZ_H);
        int oldPPx = constrain((int)(pPrevP[b]  * VIZ_H), 0, VIZ_H);

        // Erase only the shrunk region — avoids full-bar clear flicker
        if (oldPx > newPx)
            tft.fillRect(x, VIZ_H - oldPx, VIZ_BAR_W, oldPx - newPx, VIZ_C_BG);
        if (oldPPx > 1)
            tft.drawFastHLine(x, VIZ_H - oldPPx - 1, VIZ_BAR_W, VIZ_C_BG);

        // Draw bar with 8-segment gradient
        if (newPx > 0) {
            const int SEGS = 8;
            for (int s = 0; s < SEGS; s++) {
                int y0 = s * newPx / SEGS,  y1 = (s+1) * newPx / SEGS;
                if (y1 <= y0) continue;
                float norm = (float)(y0 + y1) * 0.5f / (float)VIZ_H;
                tft.fillRect(x, VIZ_H - y1, VIZ_BAR_W, y1 - y0,
                             p_barColor(b, norm, pHueBase));
            }
        }
        // White peak marker
        if (peakPx > 2)
            tft.drawFastHLine(x, VIZ_H - peakPx - 1, VIZ_BAR_W, 0xFFFF);

        pPrevH[b] = pSmBand[b];  pPrevP[b] = pSmPeak[b];
    }
}

// ── Visualiser: mirrored top + bottom bars ──────────────────────
static void partyDrawMirror() {
    const int midY = VIZ_H / 2, hMax = VIZ_H / 2 - 4;
    // Centre divider line
    tft.drawFastHLine(0, midY, VIZ_W,
                      p_hsvTo565(fmodf(pHueBase + 120.f, 360.f), 0.55f, 0.30f));
    for (int b = 0; b < PARTY_NUM_BANDS; b++) {
        int x    = VIZ_ORIGIN_X + b * VIZ_BAR_STRIDE;
        int newH = constrain((int)(pSmBand[b] * hMax), 0, hMax);
        int oldH = constrain((int)(pPrevMH[b] * hMax), 0, hMax);
        if (oldH > newH) {
            tft.fillRect(x, midY - oldH,      VIZ_BAR_W, oldH - newH, VIZ_C_BG);
            tft.fillRect(x, midY + newH + 1,  VIZ_BAR_W, oldH - newH, VIZ_C_BG);
        }
        if (newH > 0) {
            const int SEGS = 5;
            for (int s = 0; s < SEGS; s++) {
                int h0 = s * newH / SEGS,  h1 = (s+1) * newH / SEGS;
                if (h1 <= h0) continue;
                float norm = (float)(h0 + h1) * 0.5f / (float)hMax;
                uint16_t col = p_barColor(b, norm, pHueBase);
                tft.fillRect(x, midY - h1,    VIZ_BAR_W, h1 - h0, col);
                tft.fillRect(x, midY + h0 + 1, VIZ_BAR_W, h1 - h0, col);
            }
        }
        pPrevMH[b] = pSmBand[b];
    }
}

// ── Beat flash: horizontal bars at top and bottom ───────────────
static void partyHandleBeatFlash() {
    if (pBeat) pFlashAmt = 1.0f;
    if (pFlashAmt > 0.04f) {
        uint16_t fc = p_hsvTo565(fmodf(pHueBase + 180.f, 360.f),
                                  0.70f, pFlashAmt * 0.70f);
        tft.fillRect(0,         0, VIZ_W, 3, fc);
        tft.fillRect(0, VIZ_H - 3, VIZ_W, 3, fc);
        pFlashAmt *= 0.52f;
    }
}

// ── Master level meter at bottom edge ───────────────────────────
static void partyDrawLevelMeter() {
    int newPx = constrain((int)(pLevel * (VIZ_W - 2)), 0, VIZ_W - 2);
    if (newPx == pPrevLvlPx) return;
    if (newPx > pPrevLvlPx)
        tft.fillRect(pPrevLvlPx + 1, VIZ_H - 2, newPx - pPrevLvlPx, 2,
                     p_hsvTo565(fmodf(pHueBase + 90.f, 360.f), 0.80f, 0.95f));
    else
        tft.fillRect(newPx + 1, VIZ_H - 2, pPrevLvlPx - newPx, 2, VIZ_C_BG);
    pPrevLvlPx = newPx;
}

// ── LED effects — 4 modes ───────────────────────────────────────
static void partyUpdateLED() {
    uint32_t now = millis();
    uint32_t color;
    switch (partyLedMode) {
        case 0:   // Hue cycle, brightness tracks level
            pLedHue    = fmodf(pLedHue + 1.8f, 360.f);
            pLedSmooth = 0.70f * pLedSmooth + 0.30f * (pLevel * 2.2f + 0.45f);
            color = p_neoHSV(pLedHue, 1.f, constrain(pLedSmooth, 0.45f, 1.f));
            break;
        case 1:   // Beat strobe: white flash on beat, hue glow otherwise
            pLedHue = fmodf(pLedHue + 1.8f, 360.f);
            if (pBeat) { color = partyLed.Color(255,255,255); pStrobeMs = now; }
            else {
                float t = 1.f - constrain((float)(now-pStrobeMs)/110.f, 0.f, 1.f);
                if (t > 0.02f) {
                    uint8_t br = (uint8_t)(t * 255);
                    color = partyLed.Color(br, br, br);
                } else {
                    color = p_neoHSV(pLedHue, 1.f,
                                     constrain(pBass * 0.55f + 0.45f, 0.45f, 1.f));
                }
            }
            break;
        case 2: { // RGB splits across bass/mid/treble
            float r=0, g=0, bv=0;
            for (int b=0;  b< 5;              b++) r  += pSmBand[b];
            for (int b=5;  b<15;              b++) g  += pSmBand[b];
            for (int b=15; b<PARTY_NUM_BANDS; b++) bv += pSmBand[b];
            r  = constrain(r  / 5.f  * 1.6f, 0.39f, 1.f);
            g  = constrain(g  / 10.f * 1.6f, 0.39f, 1.f);
            bv = constrain(bv / 9.f  * 1.6f, 0.39f, 1.f);
            color = partyLed.Color((uint8_t)(r*255),(uint8_t)(g*255),(uint8_t)(bv*255));
            break;
        }
        case 3: default:  // Random hue snap on each beat
            if (pBeat || (now - pStrobeMs > 110)) {
                pLedHue = (float)random(360);
                pStrobeMs = now;
            }
            color = p_neoHSV(pLedHue, 1.f,
                             (pBeat || ((now - pStrobeMs) < 55)) ? 1.f : 0.50f);
            break;
    }
    partyLed.setPixelColor(0, color);
    partyLed.show();
}

// ── Clear screen and reset per-frame delta state ────────────────
static void partyClearScreen() {
    tft.fillScreen(VIZ_C_BG);
    memset(pPrevH,  0, sizeof(pPrevH));
    memset(pPrevP,  0, sizeof(pPrevP));
    memset(pPrevMH, 0, sizeof(pPrevMH));
    pPrevLvlPx = 0;  pFlashAmt = 0.f;
}

// ── Enter party mode ────────────────────────────────────────────
static void enterPartyMode() {
    // One-time band map init — safe to call multiple times (idempotent)
    static bool bandMapReady = false;
    if (!bandMapReady) { buildPartyBandMap(); bandMapReady = true; }

    setState(ST_PARTY);
    // Use UI-selected modes when autoPartyCycle is off, else start from 0
    partyVizMode = autoPartyCycle ? 0 : remoteVizMode;
    partyLedMode = autoPartyCycle ? 0 : remoteLedMode;
    partyModeTs  = millis();

    memset(pSmBand,  0, sizeof(pSmBand));
    memset(pSmPeak,  0, sizeof(pSmPeak));
    memset(pPeakTmr, 0, sizeof(pPeakTmr));
    pAvgLevel = 0;  pLastBeatMs = 0;
    pHueBase  = 0;  pLedHue = 0;  pLedSmooth = 0;

    partyClearScreen();

    partyLed.begin();
    partyLed.setBrightness(PARTY_LED_BRIGHT);
    partyLed.setPixelColor(0, 0);
    partyLed.show();

    dgStreaming     = true;
    dgLastKeepalive = millis();

    Serial.println("[Party] ON");
}

// ── Exit party mode ─────────────────────────────────────────────
static void exitPartyMode() {
    partyLed.setPixelColor(0, 0);
    partyLed.show();
    // Release GPIO 48 from NeoPixel's RMT peripheral so the PA amplifier
    // can be re-enabled by audioInitVideo() / audioInitRec() later.
    gpio_reset_pin((gpio_num_t)PARTY_LED_PIN);

    tft.fillScreen(C_BK);
    if (logsVisible) {
        faceBlitY = 0;
        roboEyes.setPushYOffset(0);
    } else {
        faceBlitY = 40;
        roboEyes.setPushYOffset(40);
        tft.fillRect(0, 0, W, faceBlitY, C_BK);
        tft.fillRect(0, LOG_Y, W, H - LOG_Y, C_BK);
    }
    roboEyes.update();
    if (logsVisible) { logRedraw(); logDrawFooter(); }

    dgStreaming     = true;
    dgLastKeepalive = millis();
    setState(ST_LISTEN);
    setFaceListen();
    setStatus("Listening...", C_CY);

    Serial.println("[Party] OFF");
}

// ── Party main tick ─────────────────────────────────────────────
static void partyLoop() {
    // DG keepalive + silent reconnect if connection dropped
    maintainDeepgram();
    partyProcessAudio();

    pHueBase = fmodf(pHueBase + 0.55f, 360.f);

    // Cycle visualiser + LED mode every PARTY_MODE_SEC seconds (only in auto mode)
    if (autoPartyCycle && (millis() - partyModeTs) > (uint32_t)PARTY_MODE_SEC * 1000UL) {
        partyVizMode = (partyVizMode + 1) % 2;
        partyLedMode = (partyLedMode + 1) % 4;
        partyModeTs  = millis();
        partyClearScreen();
        Serial.printf("[Party] viz=%d led=%d\n", partyVizMode, partyLedMode);
    }

    switch (partyVizMode) {
        case 0: partyDrawSpectrum(); break;
        case 1: partyDrawMirror();   break;
    }
    partyHandleBeatFlash();
    partyDrawLevelMeter();
    partyUpdateLED();
    delay(16);
}

// ── Voice command parser ─────────────────────────────────────────
// Returns  1 → "party on",  -1 → "party off",  0 → not a party command
static int checkPartyCommand(const char* t) {
    if (!t || !*t) return 0;
    // Lowercase copy
    char low[DG_FINAL_MAX];
    int li = 0;
    for (const char* p = t; *p && li < (int)sizeof(low)-1; p++, li++)
        low[li] = (char)tolower((uint8_t)*p);
    low[li] = '\0';

    if (strstr(low, "party on")   != nullptr) return  1;
    if (strstr(low, "party mode") != nullptr) return  1;
    if (strstr(low, "party off")  != nullptr) return -1;
    if (strstr(low, "stop party") != nullptr) return -1;
    if (strstr(low, "end party")  != nullptr) return -1;
    return 0;
}

// ╔══════════════════════════════════════════════════════════════╗
// ║  REMOTE CONTROL — HELPERS (NEW v2.1)                         ║
// ╚══════════════════════════════════════════════════════════════╝

static String urlEncode(const String& s) {
    String out; out.reserve(s.length() * 3);
    for (int i = 0; i < (int)s.length(); i++) {
        char ch = s[i];
        if ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') ||
            (ch >= '0' && ch <= '9') || ch == '-' || ch == '_' ||
            ch == '.' || ch == '~') {
            out += ch;
        } else {
            char hex[4];
            snprintf(hex, sizeof(hex), "%%%02X", (unsigned char)ch);
            out += hex;
        }
    }
    return out;
}

// Map UI visualizer string → partyVizMode index
static uint8_t vizNameToMode(const char* name) {
    if (!name) return 0;
    if (strcmp(name,"mirror")==0 || strcmp(name,"wave")==0) return 1;
    return 0;  // spectrum / bars / default
}

// Map UI LED mode string → partyLedMode index (0-3)
static uint8_t ledNameToMode(const char* name) {
    if (!name) return 0;
    if (strcmp(name,"strobe")==0 || strcmp(name,"strobe sync")==0) return 1;
    if (strcmp(name,"rgb")==0    || strcmp(name,"chase")==0)        return 2;
    if (strcmp(name,"random")==0 || strcmp(name,"sparkle")==0)      return 3;
    return 0;  // rainbow cycle
}

// Parse "#rrggbb" hex colour string → uint32_t 0xRRGGBB
static uint32_t hexColorToU32(const char* hex) {
    if (!hex || !*hex) return 0xFF3CA0;
    const char* p = (hex[0] == '#') ? hex + 1 : hex;
    return (uint32_t)strtol(p, nullptr, 16);
}

// ── Stream MP3 from an HTTPS URL — same pipeline as callRailwayStream() ─────
// Used by the 'play' command to fetch audio from /bronny/media.
static void playAudioFromUrl(const String& url) {
    if (url.isEmpty()) return;
    Serial.printf("[Media] %s\n", url.c_str());
    mediaPlaying = true; mediaPaused = false; mediaCurrentUrl = url;

    audioInitTTS();
    setCrashPoint("media:http");
    gMediaCli.setInsecure();
    gMediaCli.setConnectionTimeout(15000);
    HTTPClient http;
    http.begin(gMediaCli, url);
    http.addHeader("X-Api-Key", AETHER_API_KEY);
    http.setTimeout(600000);

    int code = http.GET();
    Serial.printf("[Media] HTTP %d\n", code);
    if (code != HTTP_CODE_OK) {
        tftLogf(C_RD, "Media: HTTP %d", code);
        http.end(); gMediaCli.stop();
        audioInitRec(); micInit();
        mediaPlaying = false; mediaCurrentUrl = "";
        return;
    }

    setCrashPoint("media:decode");
    gMp3Dec.begin(); gTtsDecoded.begin();
    WiFiClient* stream  = http.getStreamPtr();
    uint32_t deadline   = millis() + 600000UL;
    bool     started    = false;
    uint32_t lastDataMs = 0;

    setState(ST_SPEAK);
    tftLog(C_PNK, "Media: streaming...");

    static uint8_t mediaBuf[512];
    while (millis() < deadline && mediaPlaying) {
        if (mediaPaused) { delay(50); maintainDeepgram(); yield(); continue; }
        size_t avail = (size_t)stream->available();
        if (avail > 0) {
            size_t got = stream->readBytes(mediaBuf, min(avail, (size_t)sizeof(mediaBuf)));
            if (got > 0) {
                if (!started) { startTalk(); started = true; }
                gTtsDecoded.write(mediaBuf, got);
                lastDataMs = millis();
            }
        } else { delay(2); }
        if (started && lastDataMs > 0 && millis() - lastDataMs > STREAM_DATA_GAP_MS * 3) break;
        if (!http.connected() && stream->available() == 0) break;
        maintainDeepgram(); roboEyes.update(); yield();
    }

    setCrashPoint("media:done");
    stopTalk(); forceDrawFace();
    gTtsDecoded.end();
    const uint8_t sil[16] = {};
    i2s.write(sil, sizeof(sil));
    http.end(); gMediaCli.stop();
    mediaPlaying = false; mediaCurrentUrl = "";
    audioInitRec(); micInit();
    if (micOk) {
        static uint8_t localDrain[512];
        uint32_t e = millis() + 300;
        while (millis() < e) { mic_stream.readBytes(localDrain, sizeof(localDrain)); roboEyes.update(); yield(); }
    }
    clearCrashPoint();
    setFaceListen(); setStatus("Listening...", C_CY); setState(ST_LISTEN);
    tftLog(C_GR, "Media: done");
}

// ── Dispatch a single remote command received in the heartbeat JSON ──────────
static void executeBronnyCommand(JsonVariant cmd) {
    // Skip ALL commands during video playback — executing them interrupts
    // the audio codec (audioInitTTS, i2s.setVolume, partyLed.begin on GPIO 48)
    // and silences the MP3 audio stream running on Core 0.
    // Commands are already consumed from the server queue; user must resend after.
    if (isVideoMode()) {
        Serial.printf("[Ctrl] Skipping '%s' — video playing\n", cmd["command"] | "");
        return;
    }

    const char* command = cmd["command"] | "";
    Serial.printf("[Ctrl] %s\n", command);

    if (strcmp(command, "volume") == 0) {
        // Don't change volume during party mode (disrupts audio visualiser levels)
        if (isPartyMode()) return;
        int vol = constrain(cmd["value"] | 70, 0, 100);
        i2s.setVolume(vol / 100.0f);
        tftLogf(C_CY, "Vol: %d%%", vol);
    }

    else if (strcmp(command, "brightness") == 0) {
        int pct = constrain(cmd["value"] | 100, 0, 100);
        // PIN_BLK (42) is the TFT backlight pin — analogWrite uses LEDC internally
        analogWrite(PIN_BLK, map(pct, 0, 100, 0, 255));
        tftLogf(C_CY, "Brightness: %d%%", pct);
    }

    else if (strcmp(command, "wake") == 0) {
        if (isStandby()) {
            setState(ST_LISTEN);
            setFaceListen();
            dgStreaming     = dgConnected;
            lastRailwayMs   = millis();
            setStatus("Listening...", C_CY);
            tftLog(C_GR, "Remote wake");
        }
    }

    else if (strcmp(command, "sleep") == 0) {
        setStatus("Going to sleep...", C_CY); delay(700);
        setFaceSleep(); roboEyes.update();
        setState(ST_STANDBY); setStatus("Standby...", C_DCY);
        dgStreaming = false;
        tftLog(C_DCY, "Remote sleep");
    }

    else if (strcmp(command, "restart") == 0) {
        setStatus("Restarting...", C_CY);
        tftLog(C_YL, "Remote restart"); delay(500);
        ESP.restart();
    }

    else if (strcmp(command, "party") == 0) {
        bool active = cmd["active"] | false;
        if (active) {
            remoteVizMode  = vizNameToMode(cmd["visualizer"] | "bars");
            remoteLedMode  = ledNameToMode(cmd["led_mode"]   | "rainbow");
            remoteSpeed    = constrain(cmd["speed"] | 6, 1, 10);
            remoteLedColor = hexColorToU32(cmd["color"] | "#ff3ca0");
            autoPartyCycle = false;
            partyVizMode   = remoteVizMode;
            partyLedMode   = remoteLedMode;
            if (!isPartyMode()) { jingleParty(); enterPartyMode(); }
        } else {
            autoPartyCycle = true;
            if (isPartyMode()) exitPartyMode();
        }
    }

    else if (strcmp(command, "led") == 0) {
        const char* mode  = cmd["mode"]  | "off";
        remoteLedColor    = hexColorToU32(cmd["color"] | "#ff3ca0");
        remoteSpeed       = constrain(cmd["speed"] | 5, 1, 10);
        if (strcmp(mode, "off") == 0) {
            ledStandaloneActive = false;
            if (!isPartyMode()) { partyLed.setPixelColor(0, 0); partyLed.show(); }
        } else {
            if (!ledStandaloneActive && !isPartyMode()) {
                partyLed.begin(); partyLed.setBrightness(PARTY_LED_BRIGHT);
            }
            ledStandaloneActive = true;
            if      (strcmp(mode,"rainbow")==0 || strcmp(mode,"meteor")==0) ledStandaloneMode = 1;
            else if (strcmp(mode,"pulse")  ==0)                             ledStandaloneMode = 2;
            else if (strcmp(mode,"breathe")==0 || strcmp(mode,"sparkle")==0) ledStandaloneMode = 3;
            else if (strcmp(mode,"strobe") ==0)                             ledStandaloneMode = 4;
            else if (strcmp(mode,"solid")  ==0)                             ledStandaloneMode = 5;
            else                                                             ledStandaloneMode = 1;
        }
        tftLogf(C_PNK, "LED: %s spd=%d", mode, remoteSpeed);
    }

    else if (strcmp(command, "play") == 0) {
        const char* ytUrl   = cmd["url"]     | "";
        const char* mode    = cmd["mode"]    | "audio";
        bool        rainbow = cmd["rainbow"] | false;
        if (strlen(ytUrl) > 0) {
            String fetchUrl = String(baseUrl()) + "/bronny/media?url=" +
                              urlEncode(String(ytUrl)) + "&mode=" + String(mode);
            tftLogf(C_PNK, "Play: %.28s", ytUrl);
            if (rainbow && !isPartyMode()) {
                remoteLedColor = 0xFF3CA0; remoteSpeed = 7;
                ledStandaloneActive = true; ledStandaloneMode = 1;
                partyLed.begin(); partyLed.setBrightness(PARTY_LED_BRIGHT);
            }
            playAudioFromUrl(fetchUrl);
        }
    }

    else if (strcmp(command, "pause") == 0)  { mediaPaused = true;  tftLog(C_YL, "Media: paused"); }
    else if (strcmp(command, "resume") == 0) { mediaPaused = false; tftLog(C_GR, "Media: resumed"); }

    else if (strcmp(command, "stop") == 0) {
        mediaPlaying = false; mediaPaused = false;
        if (ledStandaloneActive) { partyLed.setPixelColor(0,0); partyLed.show(); ledStandaloneActive = false; }
        tftLog(C_YL, "Media: stopped");
    }

    else if (strcmp(command, "seek") == 0) {
        // Streams can't be seeked; restart from beginning
        if (!mediaCurrentUrl.isEmpty()) { tftLog(C_CY, "Media: seek→restart"); playAudioFromUrl(mediaCurrentUrl); }
    }
}


// ── Standalone LED tick — called from loop() every frame (NEW v2.1) ──────────
// Defined here (after party section) because it uses p_neoHSV and partyLed.
static void tickStandaloneLed(uint32_t now) {
    if (!ledStandaloneActive || isPartyMode()) return;
    uint32_t intervalMs = (uint32_t)map(remoteSpeed, 1, 10, 80, 8);
    if (now - ledStandaloneLastMs < intervalMs) return;
    ledStandaloneLastMs = now;
    uint32_t color = 0;
    float t;
    uint8_t lr, lg, lb;
    switch (ledStandaloneMode) {
        case 1: // Rainbow
            ledStandaloneHue = fmodf(ledStandaloneHue + 2.5f, 360.f);
            color = p_neoHSV(ledStandaloneHue, 1.f, 1.f);
            break;
        case 2: // Pulse — hue cycles, brightness sine-waves
            ledStandaloneHue = fmodf(ledStandaloneHue + 1.5f, 360.f);
            t = 0.5f + 0.5f * sinf(now * 0.003f);
            color = p_neoHSV(ledStandaloneHue, 1.f, t);
            break;
        case 3: // Breathe — solid colour, brightness fades
            t  = 0.5f + 0.5f * sinf(now * 0.002f);
            lr = (uint8_t)(((remoteLedColor >> 16) & 0xFF) * t);
            lg = (uint8_t)(((remoteLedColor >>  8) & 0xFF) * t);
            lb = (uint8_t)(( remoteLedColor        & 0xFF) * t);
            color = partyLed.Color(lr, lg, lb);
            break;
        case 4: // Strobe
            color = ((now % (uint32_t)map(remoteSpeed, 1, 10, 500, 50)) < 30)
                     ? 0xFFFFFF : 0;
            break;
        case 5: // Solid colour from picker
            color = partyLed.Color(
                (remoteLedColor >> 16) & 0xFF,
                (remoteLedColor >>  8) & 0xFF,
                 remoteLedColor        & 0xFF);
            break;
        default: color = 0; break;
    }
    partyLed.setPixelColor(0, color);
    partyLed.show();
}


// ╔══════════════════════════════════════════════════════════════╗
// ║  VIDEO MODE                                                  ║
// ╚══════════════════════════════════════════════════════════════╝

// ── JPEGDEC frame callback ───────────────────────────────────────
static int jpegDrawCallback(JPEGDRAW* pDraw) {
    tft.pushImage(pDraw->x, pDraw->y,
                  pDraw->iWidth, pDraw->iHeight,
                  pDraw->pPixels);
    return 1;
}

// ── Video polling helpers (all use gVideoCli) ────────────────────
// gVideoCli is global so these never put a WiFiClientSecure on the stack.
// Each call does: setInsecure → begin → request → end → stop.
// Sequential by design — never called concurrently.

static String checkCurrentJob() {
    if (WiFi.status() != WL_CONNECTED) return "";
    gVideoCli.setInsecure();
    gVideoCli.setConnectionTimeout(8000);
    HTTPClient http;
    http.begin(gVideoCli, baseUrl() + "/video/current");
    http.addHeader("X-Api-Key", AETHER_API_KEY);
    http.setTimeout(8000);
    if (http.GET() != 200) { http.end(); gVideoCli.stop(); return ""; }
    String resp = http.getString();
    http.end(); gVideoCli.stop();
    StaticJsonDocument<128> doc;
    if (deserializeJson(doc, resp) != DeserializationError::Ok) return "";
    if (!(doc["ready"] | false)) return "";
    return doc["job_id"] | "";
}

static String getJobTitle(const char* jobId) {
    if (WiFi.status() != WL_CONNECTED) return "";
    gVideoCli.setInsecure();
    gVideoCli.setConnectionTimeout(8000);
    HTTPClient http;
    http.begin(gVideoCli, baseUrl() + "/video/status/" + String(jobId));
    http.addHeader("X-Api-Key", AETHER_API_KEY);
    http.setTimeout(8000);
    if (http.GET() != 200) { http.end(); gVideoCli.stop(); return ""; }
    String resp = http.getString();
    http.end(); gVideoCli.stop();
    StaticJsonDocument<256> doc;
    if (deserializeJson(doc, resp) != DeserializationError::Ok) return "";
    return doc["title"] | "";
}

static void clearCurrentJob() {
    if (WiFi.status() != WL_CONNECTED) return;
    gVideoCli.setInsecure();
    gVideoCli.setConnectionTimeout(8000);
    HTTPClient http;
    http.begin(gVideoCli, baseUrl() + "/video/current/clear");
    http.addHeader("X-Api-Key", AETHER_API_KEY);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(8000);
    http.POST((uint8_t*)"{}", 2);
    http.end(); gVideoCli.stop();
}

// ── Core-0 audio streaming task ──────────────────────────────────
// Runs on Core 0 while Core 1 decodes video frames.
// gVidAudioCli is a global — no TLS state bleed between plays.
static void videoAudioTaskFn(void*) {
    g_audioTaskRunning = true;

    static MP3DecoderHelix    taskMp3;
    static EncodedAudioStream taskDecoded(&i2s, &taskMp3);
    static uint8_t            audioBuf[2048];

    gVidAudioCli.setInsecure();
    gVidAudioCli.setConnectionTimeout(15000);

    HTTPClient http;
    String url = baseUrl() + "/video/stream/" + String(gVidJobId) + ".mp3";
    http.begin(gVidAudioCli, url);
    http.addHeader("X-Api-Key", AETHER_API_KEY);
    http.setTimeout(600000);

    int code = http.GET();
    Serial.printf("[VidAudio] HTTP %d\n", code);

    if (code == 200) {
        taskMp3.begin();
        // Prime with correct format BEFORE begin() so the internal
        // setAudioInfo() call uses {44100,2,16} instead of {0,0,0}.
        // Passing {0,0,0} corrupts the ES8311 codec state and silences audio.
        taskDecoded.setAudioInfo(ainf_vid);
        taskDecoded.begin();
        WiFiClient* s = http.getStreamPtr();

        g_vidAudioReady = true;
        while (!g_vidStartPlayback && g_vidPlaying) vTaskDelay(1);

        // Re-assert volume + PA after the first write: the MP3 decoder fires
        // setAudioInfo({44100,2,16}) synchronously on the first frame, which
        // reinitialises the codec and resets the volume register to its
        // post-reset default (potentially muted). Do this from the audio task
        // so it happens after the codec re-init, not before.
        bool volSet = false;
        while (g_vidPlaying) {
            int avail = s->available();
            if (avail > 0) {
                int got = s->readBytes(audioBuf,
                              min(avail, (int)sizeof(audioBuf)));
                if (got > 0) {
                    taskDecoded.write(audioBuf, got);
                    if (!volSet) {
                        i2s.setVolume(VOL_VIDEO);
                        gpio_set_level((gpio_num_t)PIN_PA, 1);
                        volSet = true;
                    }
                }
            } else {
                if (!http.connected() && s->available() == 0) break;
                vTaskDelay(1);
            }
        }
        taskDecoded.end();
        // Audio stream ended — tell video loop to stop
        g_vidPlaying = false;
    } else {
        // ── CRITICAL FIX ─────────────────────────────────────────
        // Previous code set g_vidPlaying = false here, which killed
        // the video loop before a single frame was decoded — this is
        // why video almost always returned to the face immediately.
        // Now we signal ready but leave g_vidPlaying alone so the
        // video continues to play silently.
        Serial.println("[VidAudio] HTTP fail — video plays silently");
        g_vidAudioReady = true;
    }

    http.end();
    gVidAudioCli.stop();
    g_audioTaskRunning = false;
    Serial.println("[VidAudio] Task exit");
    vTaskDelete(NULL);
}

// ── Buffering progress bar ───────────────────────────────────────
static void drawVidBufferBar(int filled, int total) {
    const int BX = 20, BY = H/2 + 28, BW = W - 40, BH = 6;
    tft.drawRect(BX-1, BY-1, BW+2, BH+2, C_DG);
    int fw = (int)((float)filled / total * BW);
    if (fw > 0) tft.fillRect(BX, BY, min(fw, BW), BH, C_CY);
    char pct[8]; snprintf(pct, sizeof(pct), "%d%%",
                          (int)((float)filled / total * 100));
    tft.setTextSize(1); tft.setTextColor(C_CY, TFT_BLACK);
    tft.setCursor((W - (int)strlen(pct)*6) / 2, BY + 12);
    tft.print(pct);
}

// ── Core video decode loop ───────────────────────────────────────
// Opens the MJPEG stream, pre-fills the buffer, spawns the audio
// task, then decodes and displays frames until the stream ends.
// Blocks until complete. Called from enterVideoMode().
static bool playVideo() {
    if (!mjpegBuf) return false;

    // Use global gVidMjpegCli — no stale TLS state from previous plays.
    gVidMjpegCli.setInsecure();
    gVidMjpegCli.setConnectionTimeout(20000);

    HTTPClient vhttp;
    vhttp.begin(gVidMjpegCli, baseUrl() + "/video/stream/" + String(gVidJobId) + ".mjpeg");
    vhttp.addHeader("X-Api-Key", AETHER_API_KEY);
    vhttp.setTimeout(600000);

    int code = vhttp.GET();
    Serial.printf("[Video] MJPEG HTTP %d\n", code);
    if (code != 200) {
        tft.fillScreen(TFT_BLACK);
        tft.setTextSize(1); tft.setTextColor(C_RD, TFT_BLACK);
        char msg[32]; snprintf(msg, sizeof(msg), "HTTP error %d", code);
        tft.setCursor(W/2 - (int)strlen(msg)*3, H/2);
        tft.print(msg);
        vhttp.end(); gVidMjpegCli.stop();
        g_vidPlaying = false;
        return false;
    }

    WiFiClient* vs = vhttp.getStreamPtr();

    // Server prepends a 1-byte FPS header (see video_routes.py)
    uint8_t streamFps = VIDEO_TARGET_FPS;
    vs->readBytes(&streamFps, 1);
    if (streamFps == 0 || streamFps > 60) streamFps = VIDEO_TARGET_FPS;
    const uint32_t frameMs = 1000 / streamFps;
    Serial.printf("[Video] %u fps  frameMs=%u ms\n", streamFps, frameMs);

    // ── Pre-fill ────────────────────────────────────────────
    int bytesInBuf = 0;
    tft.fillScreen(TFT_BLACK);
    tft.setTextSize(2); tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setCursor(max(0, (W - 12*12) / 2), H/2 - 22);
    tft.print("Buffering...");

    int lastPct = -1;
    uint32_t prefillEnd = millis() + 8000;
    while (bytesInBuf < (int)VIDEO_PREFILL_BYTES && millis() < prefillEnd) {
        int avail = vs->available();
        if (avail > 0) {
            int toRead = min(avail, (int)VIDEO_MJPEG_BUF - bytesInBuf);
            if (toRead > 0)
                bytesInBuf += vs->readBytes(mjpegBuf + bytesInBuf, toRead);
        }
        int pct = (int)((float)bytesInBuf / VIDEO_PREFILL_BYTES * 100);
        if (pct != lastPct) { drawVidBufferBar(bytesInBuf, VIDEO_PREFILL_BYTES); lastPct=pct; }
        yield();
    }
    Serial.printf("[Video] Pre-filled %d bytes\n", bytesInBuf);

    // ── Spawn Core-0 audio task ──────────────────────────────
    tft.fillScreen(TFT_BLACK);
    tft.setTextSize(1); tft.setTextColor(C_CY, TFT_BLACK);
    tft.setCursor(W/2 - 42, H/2);
    tft.print("Starting audio...");

    xTaskCreatePinnedToCore(videoAudioTaskFn, "vid_audio",
                            32768, NULL, 2, NULL, 0);

    uint32_t t0 = millis();
    while (!g_vidAudioReady && millis() - t0 < 8000) { delay(50); yield(); }

    // taskDecoded.begin() on Core 0 calls setAudioInfo() on i2s, which may
    // reset the codec volume and PA state. Re-assert both from Core 1 now
    // that the audio task has finished its begin() sequence.
    gpio_set_level((gpio_num_t)PIN_PA, 1);
    i2s.setVolume(VOL_VIDEO);

    tft.fillScreen(TFT_BLACK);
    g_vidStartPlayback = true;
    delay(VIDEO_PRIME_MS);   // let audio fill its pipeline before first frame

    // ── Main decode loop ─────────────────────────────────────
    uint32_t playStartMs   = millis();
    uint32_t frameCount    = 0;
    uint32_t lastByteMs    = millis();
    uint32_t fpsFrames     = 0;
    uint32_t fpsWindowMs   = millis();
    uint32_t lastHbVideoMs = millis();  // heartbeat timer for video session

    while (g_vidPlaying) {

        // ── Periodic heartbeat ────────────────────────────────
        // playVideo() blocks loop() for the entire video duration, so the
        // normal 30-second heartbeat in loop() never fires.  After 60 s
        // the cloud brain marks Bronny offline and the web UI shows offline.
        // Sending every 25 s keeps last_seen fresh on the server.
        if (millis() - lastHbVideoMs >= 25000) {
            lastHbVideoMs = millis();
            sendHeartbeat();
        }

        // Top-up buffer from HTTP stream
        int avail = vs->available();
        if (avail > 0) {
            int toRead = min(avail, (int)VIDEO_MJPEG_BUF - bytesInBuf);
            if (toRead > 0) {
                int got = vs->readBytes(mjpegBuf + bytesInBuf, toRead);
                bytesInBuf += got;
                if (got > 0) lastByteMs = millis();
            }
        }

        // Search for JPEG SOI (FF D8) and EOI (FF D9) markers.
        // CRITICAL FIX: only capture the FIRST SOI found (frameStart == -1 guard).
        // Previous code overwrote frameStart on every FF D8, so an embedded
        // JFIF/APP marker inside compressed data would point frameStart at a
        // mid-frame offset, causing JPEGDEC to try decoding a partial frame → crash.
        int frameStart = -1, frameEnd = -1;
        for (int i = 0; i < bytesInBuf - 1; i++) {
            if (mjpegBuf[i] != 0xFF) continue;
            if      (mjpegBuf[i+1] == 0xD8 && frameStart == -1) { frameStart = i; }
            else if (mjpegBuf[i+1] == 0xD9 && frameStart != -1) { frameEnd = i + 1; break; }
        }

        if (frameStart != -1 && frameEnd != -1) {
            frameCount++;  fpsFrames++;

            // Frame-rate pacing: keep video in sync with audio clock
            uint32_t elapsedMs  = millis() - playStartMs;
            uint32_t expectedMs = frameCount * frameMs;
            if (elapsedMs <= expectedMs + frameMs * 10) {
                while (millis() - playStartMs < expectedMs) yield();

                int frameSize = frameEnd - frameStart + 1;
                if (jpeg.openRAM(mjpegBuf + frameStart, frameSize,
                                 jpegDrawCallback)) {
                    jpeg.decode(max(0, (W - jpeg.getWidth())  / 2),
                                max(0, (H - jpeg.getHeight()) / 2),
                                0);
                    jpeg.close();
                }
            }

            // Slide unprocessed bytes to the front of the buffer
            int remaining = bytesInBuf - frameEnd - 1;
            if (remaining > 0) memmove(mjpegBuf, mjpegBuf + frameEnd + 1, remaining);
            bytesInBuf = max(0, remaining);

            if (millis() - fpsWindowMs >= 5000) {
                Serial.printf("[Video] %.1f fps\n", fpsFrames / 5.0f);
                fpsFrames = 0;  fpsWindowMs = millis();
            }
            // Yield after every decoded frame — feeds the Task WDT and lets
            // the WiFi stack service its TX/RX queues between frames.
            yield();

        } else {
            // No complete frame in buffer — check for end-of-stream or stall
            if ((!vhttp.connected() && vs->available() == 0) ||
                    (millis() - lastByteMs) > VIDEO_STALL_MS) {
                Serial.println("[Video] Stream ended or stalled");
                break;
            }
            yield();
        }
    } // end while(g_vidPlaying)

    g_vidPlaying = false;
    vhttp.end();
    gVidMjpegCli.stop();
    Serial.printf("[Video] Done — %u frames\n", frameCount);
    return frameCount > 0;
}

// ── Enter video mode ─────────────────────────────────────────────
static void enterVideoMode(const char* jobId) {
    if (!mjpegBuf) {
        tftLog(C_RD, "Video: no PSRAM buf — skipped");
        return;
    }

    setState(ST_VIDEO);

    // Stop mic and DG audio streaming for the video duration.
    // DG WebSocket stays open but just idles (keepalive handled by
    // maintainDeepgram in exitVideoMode path).
    dgStreaming = false;
    if (micOk) { mic_stream.end();  micOk = false; }
    delay(60);

    // Clear any pending transcript so a voice command from before
    // the video doesn't fire right after it ends.
    gDgFinal[0]       = '\0';
    gDgPartial[0]     = '\0';
    pendingTranscript = false;
    dgFinalReceivedAt = 0;

    // Fetch display title before consuming the job
    String title = getJobTitle(jobId);
    if (title.length() > 26) title = title.substring(0, 23) + "...";

    // "Now Playing" splash screen
    tft.fillScreen(C_BK);
    tft.fillRect(0, 0, W, 30, C_CARD);
    tft.drawFastHLine(0,  0, W, C_PNK);
    tft.drawFastHLine(0, 30, W, dimCol(C_PNK, 3));
    tft.setTextColor(C_PNK);  tft.setTextSize(1);
    tft.setCursor(8, 11);     tft.print("\x10 NOW PLAYING");
    tft.setTextColor(C_WH);   tft.setTextSize(2);
    tft.setCursor(max(0, (W - 5*12) / 2), H/2 - 20);
    tft.print("VIDEO");
    if (title.length() > 0) {
        tft.setTextSize(1);  tft.setTextColor(C_CY);
        tft.setCursor(max(0, (W - (int)title.length()*6) / 2), H/2 + 12);
        tft.print(title);
    }
    delay(900);

    // Mark job consumed BEFORE playback starts so a crash mid-video
    // does not replay the same clip on the next reboot.
    clearCurrentJob();

    // Copy job ID into global so videoAudioTaskFn can read it
    strncpy(gVidJobId, jobId, sizeof(gVidJobId) - 1);
    gVidJobId[sizeof(gVidJobId) - 1] = '\0';

    // Switch codec to 44100 Hz / mono for MP3 audio task
    setCrashPoint("video:audioInit");
    audioInitVideo();
    delay(100);

    // CRITICAL: JPEGDEC outputs RGB565 with bytes in the order
    // TFT_eSPI's pushImage() expects when setSwapBytes(true).
    // Failing to toggle this back on exit inverts all UI colours.
    tft.setSwapBytes(true);

    // Arm volatile flags before spawning the task
    g_vidPlaying       = true;
    g_vidAudioReady    = false;
    g_vidStartPlayback = false;
    g_audioTaskRunning = false;

    // Play — blocks until stream ends or stalls
    setCrashPoint("video:play");
    bool ok = playVideo();

    // One automatic retry on connection failure.
    // CRITICAL: must wait for the previous audio task to fully exit before
    // retrying — spawning a second task while the first is still running
    // causes two tasks to write to i2s simultaneously → immediate panic.
    if (!ok) {
        g_vidPlaying = false;
        // Close the audio stream client BEFORE waiting for the task — this
        // makes the audio task's readBytes() / http.connected() checks fail
        // immediately, so it exits within milliseconds instead of waiting
        // for the TCP timeout.  Closing AFTER the wait left the task alive
        // past the 3-second timeout, causing two tasks to write to i2s.
        gVidAudioCli.stop();
        gVidMjpegCli.stop();
        uint32_t wt = millis();
        while (g_audioTaskRunning && millis() - wt < 4000) vTaskDelay(10);
        delay(800);

        tft.fillScreen(C_BK);
        tft.setTextSize(1); tft.setTextColor(C_YL, C_BK);
        tft.setCursor(W/2 - 30, H/2); tft.print("Retrying...");

        g_vidPlaying       = true;
        g_vidAudioReady    = false;
        g_vidStartPlayback = false;
        g_audioTaskRunning = false;
        playVideo();
    }
    clearCrashPoint();

    exitVideoMode();
}

// ── Exit video mode ──────────────────────────────────────────────
static void exitVideoMode() {
    // Tell audio task to stop
    g_vidPlaying = false;

    // Wait up to 3 s for Core-0 audio task to exit before touching i2s
    uint32_t t = millis();
    while (g_audioTaskRunning && millis() - t < 3000) vTaskDelay(10);

    // Free TLS contexts before reinitialising the codec — each active
    // WiFiClientSecure holds ~50 KB of heap for its mbedTLS context.
    gVidMjpegCli.stop();
    gVidAudioCli.stop();

    // Restore byte order for RoboEyes / normal TFT_eSPI drawing
    tft.setSwapBytes(false);
    tft.fillScreen(C_BK);

    // Restore audio codec to 16000 Hz stereo receive mode.
    // audioInitRec() has an idempotency guard (if inTtsMode || !audioOk).
    // After audioInitVideo() both flags are false/true, making it a no-op.
    // Force the reinit by setting inTtsMode=true first.
    setCrashPoint("video:exitAudio");
    inTtsMode = true;
    audioInitRec();
    delay(80);

    // Re-open microphone
    setCrashPoint("video:exitMic");
    micInit();
    delay(80);

    // Restore face layout (respect current log visibility)
    if (logsVisible) {
        faceBlitY = 0;
        roboEyes.setPushYOffset(0);
    } else {
        faceBlitY = 40;
        roboEyes.setPushYOffset(40);
        tft.fillRect(0, 0, W, faceBlitY, C_BK);
        tft.fillRect(0, LOG_Y, W, H - LOG_Y, C_BK);
    }
    roboEyes.update();
    if (logsVisible) { logRedraw(); logDrawFooter(); }

    // Force DG reconnect on the next maintainDeepgram() tick.
    // DG almost certainly disconnected during the video (no keepalive
    // was sent), so we set dgLastConnectAttempt=0 to trigger an
    // immediate reconnect attempt rather than waiting DG_RECONNECT_MS.
    dgConnected          = false;
    dgStreaming          = false;
    dgLastConnectAttempt = 0;

    clearCrashPoint();
    setState(ST_LISTEN);
    setFaceListen();
    lastRailwayMs = millis();
    setStatus("Listening...", C_CY);
    tftLog(C_MINT, "Video done — reconnecting");
}

// ── videoLoop safety stub ────────────────────────────────────────
// enterVideoMode() blocks inside playVideo() until the stream ends,
// so loop() should never re-enter videoLoop() while a video is
// active.  This stub handles the (impossible in normal operation)
// case where it does, by forcing a clean exit.
static void videoLoop() {
    Serial.println("[Video] Unexpected videoLoop entry — forcing exit");
    g_vidPlaying = false;
    exitVideoMode();
}
