/*
 * ╔══════════════════════════════════════════════════════════════╗
 * ║           AetherAI — ESP32-S3 Voice Agent  v1.0              ║
 * ║                  by Patrick Perez  © 2026                    ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  Hardware (same wiring as HW Diagnostics sketch):            ║
 * ║  ES8311 codec : PA_EN→48  DOUT→45  DIN→12  WS→13             ║
 * ║                 BCLK→14   MCLK→38  SCL→2   SDA→1             ║
 * ║  INMP441 mic  : VDD→3.3V  GND→GND  L/R→GND                   ║
 * ║                 WS→4      SCK→5    SD→6                      ║
 * ║  TFT ST7789   : DC→39  CS→47  CLK→41  SDA→40  BLK→42         ║
 * ║  TRIGGER      : BOOT button (GPIO0, built-in, active LOW)    ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  Libraries required:                                         ║
 * ║   arduino-audio-tools  (pschatzmann/arduino-audio-tools)     ║
 * ║   arduino-audio-driver (pschatzmann/arduino-audio-driver)    ║
 * ║   Adafruit ST7789  +  Adafruit GFX Library                   ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  Board settings:                                             ║
 * ║   Board: ESP32S3 Dev Module   USB CDC on Boot: Enabled       ║
 * ║   PSRAM: OPI PSRAM (8MB)                                     ║
 * ╠══════════════════════════════════════════════════════════════╣
 * ║  Operation:                                                  ║
 * ║   Hold BOOT button → recording starts (red ring + VU meter)  ║
 * ║   Release button   → sends WAV to AetherAI cloud             ║
 * ║   AI processes     → plays MP3 response through speaker      ║
 * ╚══════════════════════════════════════════════════════════════╝
 */

#include "voice_config.h"

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include "AudioTools.h"
#include "AudioTools/AudioLibs/I2SCodecStream.h"
#include "AudioTools/CoreAudio/AudioI2S/I2SStream.h"
#include "AudioCodecs/CodecMP3Helix.h"

// ═══════════════════════════════════════════════════════════════
//  PIN DEFINITIONS  (identical to HW Diagnostics sketch)
// ═══════════════════════════════════════════════════════════════
#define PIN_SDA        1
#define PIN_SCL        2
#define PIN_MCLK      38
#define PIN_BCLK      14
#define PIN_WS        13
#define PIN_DOUT      45
#define PIN_DIN       12
#define PIN_PA_EN     48
#define ES8311_ADDR   0x18

#define PIN_MIC_WS     4
#define PIN_MIC_SCK    5
#define PIN_MIC_SD     6

#define PIN_TFT_CS    47
#define PIN_TFT_DC    39
#define PIN_TFT_RST   -1
#define PIN_TFT_BLK   42
#define PIN_TFT_CLK   41
#define PIN_TFT_MOSI  40

#define PIN_BUTTON     0   // BOOT button, active LOW

// ═══════════════════════════════════════════════════════════════
//  AUDIO CONFIG
// ═══════════════════════════════════════════════════════════════
#define MIC_SAMPLE_RATE   16000
#define MIC_CHANNELS      2       // INMP441 always outputs stereo frames
#define MIC_BITS          32      // INMP441 data is 24-bit in 32-bit frames
#define SPK_SAMPLE_RATE   24000   // edge-tts MP3 output rate
#define SPK_CHANNELS      2
#define SPK_BITS          16

// ═══════════════════════════════════════════════════════════════
//  DISPLAY CONFIG
// ═══════════════════════════════════════════════════════════════
#define SCR_W  320
#define SCR_H  240

// Colour palette — same as HW Diagnostics
#define C_BG      0x0841
#define C_PANEL   0x1082
#define C_BORDER  0x2945
#define C_CYAN    0x07FF
#define C_LIME    0x87E0
#define C_RED     0xF820
#define C_ORG     0xFD40
#define C_YEL     0xFFE0
#define C_WHT     0xFFFF
#define C_DIM     0x4228
#define C_TEAL    0x0410
#define C_PURPLE  0x801F
#define C_PINK    0xF81F

// Layout constants
#define HDR_H     18
#define MAIN_Y    20     // top of main area
#define MAIN_H   170     // height of main area
#define INFO_Y   192     // transcript/status line
#define INFO_H    36
#define FTR_Y    230     // footer

// ═══════════════════════════════════════════════════════════════
//  STATE MACHINE
// ═══════════════════════════════════════════════════════════════
enum AgentState {
    ST_BOOT      = 0,
    ST_WIFI      = 1,
    ST_IDLE      = 2,
    ST_RECORDING = 3,
    ST_UPLOADING = 4,
    ST_THINKING  = 5,
    ST_SPEAKING  = 6,
    ST_ERROR     = 7,
};

static AgentState currentState = ST_BOOT;
static char       infoLine[80]  = "";     // transcript / status text on screen
static char       errorMsg[80]  = "";
static uint32_t   errorTime     = 0;

// ═══════════════════════════════════════════════════════════════
//  AUDIO OBJECTS
// ═══════════════════════════════════════════════════════════════
DriverPins        my_pins;
AudioBoard        audio_board(AudioDriverES8311, my_pins);
I2SCodecStream    i2s_stream(audio_board);   // ES8311 — playback
I2SStream         mic_stream;                 // INMP441 — recording

// MP3 decoding pipeline
MP3DecoderHelix   mp3_decoder;

// ═══════════════════════════════════════════════════════════════
//  DISPLAY
// ═══════════════════════════════════════════════════════════════
SPIClass            tftSPI(HSPI);
Adafruit_ST7789     tft = Adafruit_ST7789(&tftSPI, PIN_TFT_CS, PIN_TFT_DC, PIN_TFT_RST);

// ═══════════════════════════════════════════════════════════════
//  PSRAM BUFFERS
// ═══════════════════════════════════════════════════════════════
static uint8_t*  wav_buf     = nullptr;   // recording buffer (WAV header + PCM)
static size_t    wav_len     = 0;
static uint8_t*  mp3_buf     = nullptr;   // HTTP response buffer (MP3)
static size_t    mp3_len     = 0;

#define WAV_MAX_BYTES   (MAX_RECORD_SECS * MIC_SAMPLE_RATE * 2 + 44)   // 16-bit mono
#define MP3_MAX_BYTES   (300 * 1024)   // 300 KB — ~15s @ 160kbps

// ═══════════════════════════════════════════════════════════════
//  WAV HEADER WRITER
// ═══════════════════════════════════════════════════════════════
static void write_wav_header(uint8_t* buf, uint32_t pcm_bytes,
                              uint32_t sr, uint16_t ch, uint16_t bits) {
    uint32_t byte_rate   = sr * ch * (bits / 8);
    uint16_t block_align = ch * (bits / 8);
    uint32_t data_size   = pcm_bytes;
    uint32_t riff_size   = 36 + data_size;

    auto w32 = [](uint8_t* p, uint32_t v) {
        p[0]=v; p[1]=v>>8; p[2]=v>>16; p[3]=v>>24;
    };
    auto w16 = [](uint8_t* p, uint16_t v) {
        p[0]=v; p[1]=v>>8;
    };

    memcpy(buf,      "RIFF", 4); w32(buf+4,  riff_size);
    memcpy(buf+8,    "WAVE", 4);
    memcpy(buf+12,   "fmt ", 4); w32(buf+16, 16);
    w16(buf+20, 1);               // PCM
    w16(buf+22, ch);
    w32(buf+24, sr);
    w32(buf+28, byte_rate);
    w16(buf+32, block_align);
    w16(buf+34, bits);
    memcpy(buf+36, "data", 4);   w32(buf+40, data_size);
}

// ═══════════════════════════════════════════════════════════════
//  TFT HELPERS
// ═══════════════════════════════════════════════════════════════
static void drawHeader(bool wifi_ok) {
    tft.fillRect(0, 0, SCR_W, HDR_H, C_PANEL);
    tft.drawFastHLine(0, HDR_H, SCR_W, C_CYAN);

    tft.setTextColor(C_CYAN); tft.setTextSize(1);
    tft.setCursor(4, 5);
    tft.print("AetherAI Voice Agent");

    tft.setTextColor(C_DIM);
    tft.setCursor(200, 5);
    tft.print("ESP32-S3  v1.0");

    // WiFi indicator dot
    tft.fillCircle(SCR_W - 8, 9, 3, wifi_ok ? C_LIME : C_RED);
}

static void drawFooter() {
    tft.drawFastHLine(0, FTR_Y, SCR_W, C_BORDER);
    tft.fillRect(0, FTR_Y, SCR_W, SCR_H - FTR_Y, C_PANEL);
    tft.setTextColor(C_DIM); tft.setTextSize(1);
    tft.setCursor(4, FTR_Y + 2);
    tft.print("Patrick Perez 2026  \xb7  Hold BOOT to speak");
}

static void drawInfoLine(const char* text, uint16_t col = C_DIM) {
    tft.fillRect(0, INFO_Y, SCR_W, INFO_H, C_BG);
    if (!text || !text[0]) return;
    tft.setTextColor(col); tft.setTextSize(1);
    tft.setCursor(4, INFO_Y + 4);

    // Word-wrap at SCR_W - 8 px (chars at size 1 = 6px wide)
    char tmp[80]; strncpy(tmp, text, 79); tmp[79] = 0;
    int maxChars = (SCR_W - 8) / 6;
    if ((int)strlen(tmp) > maxChars) {
        // First line
        char line1[maxChars + 1];
        strncpy(line1, tmp, maxChars); line1[maxChars] = 0;
        tft.print(line1);
        tft.setCursor(4, INFO_Y + 18);
        tft.setTextColor(C_DIM);
        tft.print(tmp + maxChars);
    } else {
        tft.print(tmp);
    }
}

// ── IDLE state: hexagon logo + shimmer ───────────────────────────────────────
static uint8_t  idlePhase   = 0;
static uint32_t idleLastMs  = 0;

static void drawIdle() {
    tft.fillRect(0, MAIN_Y, SCR_W, MAIN_H, C_BG);

    int cx = SCR_W / 2, cy = MAIN_Y + MAIN_H / 2;

    // Outer glow ring (animated colour)
    uint16_t ringCol = (idlePhase < 128) ? C_TEAL : C_CYAN;
    tft.drawCircle(cx, cy, 52, ringCol);
    tft.drawCircle(cx, cy, 53, C_BORDER);

    // Hexagon ⬡ approximation (6 lines)
    int r = 40;
    float pi6 = 3.14159f / 6.0f;
    for (int i = 0; i < 6; i++) {
        float a1 = i * 2 * 3.14159f / 6 + pi6;
        float a2 = (i + 1) * 2 * 3.14159f / 6 + pi6;
        tft.drawLine(
            cx + (int)(r * cos(a1)), cy + (int)(r * sin(a1)),
            cx + (int)(r * cos(a2)), cy + (int)(r * sin(a2)),
            C_CYAN
        );
    }

    // Centre "A"
    tft.setTextColor(C_CYAN); tft.setTextSize(3);
    tft.setCursor(cx - 9, cy - 12);
    tft.print("A");

    // Label
    tft.setTextColor(C_WHT); tft.setTextSize(1);
    tft.setCursor(cx - 30, cy + 50);
    tft.print("Hold BOOT to speak");
}

// ── RECORDING state: pulsing red ring + VU bars ───────────────────────────────
static void drawRecording(int16_t peak, uint32_t elapsed_ms) {
    tft.fillRect(0, MAIN_Y, SCR_W, MAIN_H, C_BG);

    int cx = SCR_W / 2, cy = MAIN_Y + MAIN_H / 2;

    // Pulsing red outer ring
    bool blink = (millis() / 300) % 2;
    tft.drawCircle(cx, cy, 52, blink ? C_RED : C_BORDER);
    tft.drawCircle(cx, cy, 51, blink ? C_RED : C_BORDER);

    // REC indicator
    tft.fillCircle(cx - 26, cy - 38, 5, blink ? C_RED : C_BORDER);
    tft.setTextColor(blink ? C_RED : C_BORDER); tft.setTextSize(1);
    tft.setCursor(cx - 18, cy - 42);
    tft.print("REC");

    // Timer countdown
    int remaining = MAX_RECORD_SECS - (int)(elapsed_ms / 1000);
    if (remaining < 0) remaining = 0;
    tft.setTextColor(C_YEL); tft.setTextSize(3);
    char tbuf[4]; snprintf(tbuf, 4, "%d", remaining);
    tft.setCursor(cx - 9, cy - 14);
    tft.print(tbuf);

    // VU meter bar at bottom of main area (5 segments)
    int vu_y   = MAIN_Y + MAIN_H - 30;
    int vu_w   = 200;
    int vu_x   = cx - vu_w / 2;
    int seg_w  = vu_w / 5;
    int level  = map(abs(peak), 0, 3000, 0, 5);
    if (level > 5) level = 5;

    tft.fillRect(vu_x - 2, vu_y - 2, vu_w + 4, 18, C_PANEL);
    for (int i = 0; i < 5; i++) {
        uint16_t col = (i < level)
            ? (i < 3 ? C_LIME : i < 4 ? C_YEL : C_RED)
            : C_BORDER;
        tft.fillRect(vu_x + i * seg_w + 1, vu_y, seg_w - 2, 14, col);
    }
}

// ── UPLOADING state: arc progress ────────────────────────────────────────────
static void drawUploading(int progress) {  // progress 0..100
    tft.fillRect(0, MAIN_Y, SCR_W, MAIN_H, C_BG);
    int cx = SCR_W / 2, cy = MAIN_Y + MAIN_H / 2 - 10;

    // Background circle
    tft.drawCircle(cx, cy, 40, C_BORDER);

    // Arc (approximate with dots)
    int deg = map(progress, 0, 100, 0, 360);
    for (int a = -90; a < -90 + deg; a += 4) {
        float rad = a * 3.14159f / 180.0f;
        int x = cx + (int)(40 * cos(rad));
        int y = cy + (int)(40 * sin(rad));
        tft.fillCircle(x, y, 2, C_CYAN);
    }

    tft.setTextColor(C_CYAN); tft.setTextSize(1);
    tft.setCursor(cx - 18, cy + 50);
    tft.print("Sending audio...");
}

// ── THINKING state: three bouncing dots ──────────────────────────────────────
static void drawThinking() {
    tft.fillRect(0, MAIN_Y, SCR_W, MAIN_H, C_BG);
    int cx = SCR_W / 2, cy = MAIN_Y + MAIN_H / 2;

    uint32_t t    = millis();
    int      dot0 = (t / 300) % 3;

    for (int i = 0; i < 3; i++) {
        int x    = cx - 20 + i * 20;
        int yOff = (i == dot0) ? -8 : 0;
        tft.fillCircle(x, cy + yOff, 7, (i == dot0) ? C_CYAN : C_BORDER);
    }

    tft.setTextColor(C_DIM); tft.setTextSize(1);
    tft.setCursor(cx - 30, cy + 28);
    tft.print("Processing...");
}

// ── SPEAKING state: animated waveform bars ───────────────────────────────────
static void drawSpeaking() {
    tft.fillRect(0, MAIN_Y, SCR_W, MAIN_H, C_BG);
    int cx  = SCR_W / 2, cy = MAIN_Y + MAIN_H / 2;
    int num = 12;
    int bw  = 10;
    int gap = 4;
    int x0  = cx - (num * (bw + gap)) / 2;

    uint32_t t = millis();
    for (int i = 0; i < num; i++) {
        float phase = (float)(t % 800) / 800.0f * 2 * 3.14159f;
        float h     = 20.0f + 20.0f * abs(sin(phase + i * 0.5f));
        int   bh    = (int)h;
        uint16_t col = (i % 3 == 0) ? C_CYAN : (i % 3 == 1) ? C_TEAL : C_BORDER;
        tft.fillRect(x0 + i * (bw + gap), cy - bh / 2, bw, bh, col);
    }

    tft.setTextColor(C_LIME); tft.setTextSize(1);
    tft.setCursor(cx - 18, cy + 36);
    tft.print("Speaking...");
}

// ── ERROR state ───────────────────────────────────────────────────────────────
static void drawError(const char* msg) {
    tft.fillRect(0, MAIN_Y, SCR_W, MAIN_H, C_BG);
    int cx = SCR_W / 2, cy = MAIN_Y + MAIN_H / 2;

    tft.setTextColor(C_RED); tft.setTextSize(4);
    tft.setCursor(cx - 12, cy - 30);
    tft.print("X");

    tft.setTextColor(C_RED); tft.setTextSize(1);
    tft.setCursor(cx - (strlen(msg) * 3), cy + 16);
    tft.print(msg);
}

// ── WIFI connecting ───────────────────────────────────────────────────────────
static void drawWifi(int attempt) {
    tft.fillRect(0, MAIN_Y, SCR_W, MAIN_H, C_BG);
    int cx = SCR_W / 2, cy = MAIN_Y + MAIN_H / 2;

    // WiFi symbol (3 arcs)
    for (int r = 10; r <= 30; r += 10) {
        uint16_t col = (attempt % 3 >= r / 10) ? C_CYAN : C_BORDER;
        tft.drawCircle(cx, cy + 10, r, col);
    }
    tft.fillCircle(cx, cy + 10, 3, C_CYAN);

    tft.setTextColor(C_DIM); tft.setTextSize(1);
    tft.setCursor(cx - 30, cy + 50);
    char buf[32]; snprintf(buf, 32, "Connecting... %d", attempt);
    tft.print(buf);
}

// ═══════════════════════════════════════════════════════════════
//  AUDIO INIT
// ═══════════════════════════════════════════════════════════════
static bool initCodecPlayback() {
    Wire.begin(PIN_SDA, PIN_SCL, 100000);
    delay(30);

    my_pins.addI2C(PinFunction::CODEC, PIN_SCL, PIN_SDA, ES8311_ADDR, 100000, Wire);
    my_pins.addI2S(PinFunction::CODEC, PIN_MCLK, PIN_BCLK, PIN_WS, PIN_DOUT, PIN_DIN);
    my_pins.addPin(PinFunction::PA, PIN_PA_EN, PinLogic::Output);

    AudioInfo spk_info(SPK_SAMPLE_RATE, SPK_CHANNELS, SPK_BITS);
    auto cfg = i2s_stream.defaultConfig(TX_MODE);
    cfg.copyFrom(spk_info);
    cfg.output_device = DAC_OUTPUT_ALL;

    if (!i2s_stream.begin(cfg)) {
        Serial.println("[AUDIO] ES8311 codec init FAILED");
        return false;
    }
    i2s_stream.setVolume(0.7f);
    Serial.println("[AUDIO] ES8311 codec ready (TX 24kHz)");
    return true;
}

static bool initMic() {
    auto cfg = mic_stream.defaultConfig(RX_MODE);
    cfg.sample_rate     = MIC_SAMPLE_RATE;
    cfg.channels        = MIC_CHANNELS;
    cfg.bits_per_sample = MIC_BITS;
    cfg.i2s_format      = I2S_STD_FORMAT;
    cfg.port_no         = 1;
    cfg.pin_ws          = PIN_MIC_WS;
    cfg.pin_bck         = PIN_MIC_SCK;
    cfg.pin_data        = PIN_MIC_SD;
    cfg.pin_mck         = -1;
    cfg.use_apll        = false;

    if (!mic_stream.begin(cfg)) {
        Serial.println("[AUDIO] INMP441 mic init FAILED");
        return false;
    }
    Serial.println("[AUDIO] INMP441 mic ready (RX 16kHz)");
    return true;
}

// ═══════════════════════════════════════════════════════════════
//  RECORDING
// ═══════════════════════════════════════════════════════════════
/*
 * Records mono 16-bit PCM into wav_buf (including 44-byte WAV header).
 * Records until button is released OR max_secs reached.
 * The INMP441 outputs stereo 32-bit frames; we take the left channel
 * (every other int32) and right-shift by 14 to get int16 — same
 * technique as the HW Diagnostics sketch.
 *
 * Returns the total WAV byte count (header + PCM data).
 */
static size_t recordAudio(uint16_t max_secs) {
    const size_t max_samples = max_secs * MIC_SAMPLE_RATE;
    int32_t      raw_buf[256];  // stereo int32 — 128 stereo pairs = 128 mono samples
    int16_t*     pcm_start = (int16_t*)(wav_buf + 44);   // PCM after WAV header
    size_t       pcm_count = 0;

    uint32_t t_start  = millis();
    int16_t  live_peak = 0;

    Serial.printf("[REC] Recording up to %u seconds...\n", max_secs);

    while (digitalRead(PIN_BUTTON) == LOW
           && pcm_count < max_samples
           && (millis() - t_start) < (uint32_t)(max_secs * 1000))
    {
        int rd = mic_stream.readBytes((uint8_t*)raw_buf, sizeof(raw_buf));
        if (rd <= 0) { delay(2); continue; }

        // raw_buf contains stereo int32: [L, R, L, R ...]
        // Each frame is 2 × int32 = 8 bytes.  rd bytes → rd/8 stereo frames.
        int frames = rd / 8;
        for (int i = 0; i < frames && pcm_count < max_samples; i++) {
            int16_t s = (int16_t)(raw_buf[i * 2] >> 14);  // left channel only
            pcm_start[pcm_count++] = s;
            if (abs(s) > abs(live_peak)) live_peak = s;
        }

        // Refresh VU / timer every ~50ms
        uint32_t elapsed = millis() - t_start;
        drawRecording(live_peak, elapsed);
        live_peak = 0;
    }

    if (pcm_count == 0) {
        Serial.println("[REC] No samples captured");
        return 0;
    }

    // Write WAV header
    write_wav_header(wav_buf, pcm_count * 2, MIC_SAMPLE_RATE, 1, 16);
    size_t total = 44 + pcm_count * 2;
    Serial.printf("[REC] Captured %u samples → %u bytes WAV\n", pcm_count, total);
    return total;
}

// ═══════════════════════════════════════════════════════════════
//  CLOUD UPLOAD + RESPONSE
// ═══════════════════════════════════════════════════════════════
/*
 * POSTs the WAV buffer to POST /voice/chat.
 * Receives an MP3 response into mp3_buf.
 * Fills transcript (URL-decoded X-Transcript header) and spoken_text.
 * Returns HTTP status code, or -1 on connection failure.
 */
static int sendToCloud(size_t wav_size,
                       char* transcript_out, size_t t_max,
                       char* spoken_out,     size_t s_max) {
    transcript_out[0] = 0;
    spoken_out[0]     = 0;
    mp3_len           = 0;

    String url = String(AETHER_URL) + "/voice/chat";
    Serial.printf("[HTTP] POST %s (%u bytes WAV)\n", url.c_str(), wav_size);

    WiFiClientSecure client;
    client.setInsecure();   // skip cert check — acceptable for personal Railway deploy

    HTTPClient http;
    http.begin(client, url);
    http.addHeader("Content-Type", "audio/wav");
    http.addHeader("X-Api-Key",    AETHER_API_KEY);
    http.setTimeout(30000);   // 30s — STT + LLM + TTS can take ~5–15s total

    // Animated upload progress (simulate 0→80% during POST, 80→100 during response)
    drawUploading(10);

    int code = http.POST(wav_buf, wav_size);
    Serial.printf("[HTTP] Response code: %d\n", code);

    if (code == 200) {
        drawUploading(85);

        // Read transcript header
        String transcript_hdr = http.header("X-Transcript");
        String spoken_hdr     = http.header("X-Response-Text");
        // Headers are URL-encoded — do a simple URL decode
        _urlDecode(transcript_hdr).toCharArray(transcript_out, t_max);
        _urlDecode(spoken_hdr).toCharArray(spoken_out, s_max);

        Serial.printf("[HTTP] Transcript: %s\n", transcript_out);
        Serial.printf("[HTTP] Spoken: %s\n", spoken_out);

        // Read MP3 body
        WiFiClient* stream = http.getStreamPtr();
        int         total  = http.getSize();
        if (total < 0 || total > (int)MP3_MAX_BYTES) total = MP3_MAX_BYTES;

        size_t got = 0;
        while (http.connected() && got < (size_t)total) {
            size_t avail = stream->available();
            if (avail) {
                size_t chunk = min(avail, (size_t)(MP3_MAX_BYTES - got));
                got += stream->readBytes(mp3_buf + got, chunk);
            } else {
                delay(2);
            }
        }
        mp3_len = got;
        drawUploading(100);
        Serial.printf("[HTTP] MP3 received: %u bytes\n", mp3_len);
    }

    http.end();
    return code;
}

// ── Simple URL-decode helper ──────────────────────────────────────────────────
static String _urlDecode(const String& encoded) {
    String decoded;
    decoded.reserve(encoded.length());
    for (size_t i = 0; i < encoded.length(); i++) {
        if (encoded[i] == '%' && i + 2 < encoded.length()) {
            char hi = encoded[i + 1], lo = encoded[i + 2];
            auto h2d = [](char c) -> int {
                if (c >= '0' && c <= '9') return c - '0';
                if (c >= 'A' && c <= 'F') return c - 'A' + 10;
                if (c >= 'a' && c <= 'f') return c - 'a' + 10;
                return 0;
            };
            decoded += (char)((h2d(hi) << 4) | h2d(lo));
            i += 2;
        } else if (encoded[i] == '+') {
            decoded += ' ';
        } else {
            decoded += encoded[i];
        }
    }
    return decoded;
}

// ═══════════════════════════════════════════════════════════════
//  MP3 PLAYBACK
// ═══════════════════════════════════════════════════════════════
static void playMp3() {
    if (!mp3_len || !mp3_buf) {
        Serial.println("[PLAY] No MP3 data");
        return;
    }

    Serial.printf("[PLAY] Playing %u bytes MP3\n", mp3_len);

    MemoryStream      mp3_mem(mp3_buf, mp3_len);
    EncodedAudioStream decoded(&i2s_stream, &mp3_decoder);
    StreamCopy        mp3_copier(decoded, mp3_mem);

    decoded.begin();

    uint32_t t = millis();
    while (mp3_copier.copy()) {
        // Refresh speaking animation every ~40ms
        if (millis() - t > 40) {
            drawSpeaking();
            t = millis();
        }
    }

    decoded.end();
    Serial.println("[PLAY] Playback complete");
}

// ═══════════════════════════════════════════════════════════════
//  WIFI
// ═══════════════════════════════════════════════════════════════
static bool connectWifi() {
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.printf("[WIFI] Connecting to %s\n", WIFI_SSID);

    for (int i = 0; i < 30; i++) {
        if (WiFi.status() == WL_CONNECTED) {
            Serial.printf("[WIFI] Connected, IP: %s\n",
                          WiFi.localIP().toString().c_str());
            return true;
        }
        drawWifi(i);
        delay(500);
    }
    Serial.println("[WIFI] Connection FAILED");
    return false;
}

// ═══════════════════════════════════════════════════════════════
//  BOOT SPLASH
// ═══════════════════════════════════════════════════════════════
static void bootSplash() {
    tft.fillScreen(C_BG);

    // Scan-line wipe
    for (int y = 0; y < SCR_H; y += 3) {
        tft.drawFastHLine(0, y, SCR_W, C_TEAL);
        if (y % 18 == 0) delay(2);
    }
    delay(80);
    tft.fillScreen(C_BG);

    // Corner brackets
    int cx = SCR_W / 2, cy = SCR_H / 2, bl = 20;
    for (int r = 8; r <= 60; r += 5) {
        tft.drawFastHLine(cx-r, cy-r, bl, C_CYAN);
        tft.drawFastVLine(cx-r, cy-r, bl, C_CYAN);
        tft.drawFastHLine(cx+r-bl, cy-r, bl, C_CYAN);
        tft.drawFastVLine(cx+r, cy-r, bl, C_CYAN);
        tft.drawFastHLine(cx-r, cy+r, bl, C_CYAN);
        tft.drawFastVLine(cx-r, cy+r-bl, bl, C_CYAN);
        tft.drawFastHLine(cx+r-bl, cy+r, bl, C_CYAN);
        tft.drawFastVLine(cx+r, cy+r-bl, bl, C_CYAN);
        delay(22);
    }

    // Logo text fade in
    tft.fillRect(cx-72, cy-22, 144, 44, C_BG);
    uint16_t fade[] = { 0x0410, 0x0451, 0x04B2, 0x0593, 0x07FF };
    for (auto c : fade) {
        tft.fillRect(cx-72, cy-22, 144, 44, C_BG);
        tft.setTextColor(c); tft.setTextSize(3);
        tft.setCursor(cx - 54, cy - 20); tft.print("AetherAI");
        tft.setTextColor(0x4228); tft.setTextSize(1);
        tft.setCursor(cx - 36, cy + 8);  tft.print("ESP32 Voice Agent");
        delay(55);
    }
    delay(600);

    for (int y = 0; y <= SCR_H; y += 6) {
        tft.fillRect(0, y, SCR_W, 6, C_BG);
        delay(5);
    }
}

// ═══════════════════════════════════════════════════════════════
//  SETUP
// ═══════════════════════════════════════════════════════════════
void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("\n[BOOT] AetherAI Voice Agent starting...");

    // ── TFT ──
    pinMode(PIN_TFT_BLK, OUTPUT); digitalWrite(PIN_TFT_BLK, HIGH);
    tftSPI.begin(PIN_TFT_CLK, -1, PIN_TFT_MOSI, PIN_TFT_CS);
    tft.init(240, 320);
    tft.setRotation(3);
    tft.fillScreen(0x0000);
    bootSplash();

    // ── BUTTON ──
    pinMode(PIN_BUTTON, INPUT_PULLUP);
    Serial.println("[BOOT] BOOT button configured (GPIO0)");

    // ── PSRAM ──
    if (!psramFound()) {
        Serial.println("[BOOT] WARNING: PSRAM not detected — large buffers may fail");
    } else {
        Serial.printf("[BOOT] PSRAM free: %u bytes\n", ESP.getFreePsram());
    }
    wav_buf = (uint8_t*)heap_caps_malloc(WAV_MAX_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    mp3_buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!wav_buf || !mp3_buf) {
        Serial.println("[BOOT] FATAL: PSRAM buffer allocation failed");
        // Fallback to internal RAM for small buffers
        if (!wav_buf) wav_buf = (uint8_t*)malloc(WAV_MAX_BYTES);
        if (!mp3_buf) mp3_buf = (uint8_t*)malloc(MP3_MAX_BYTES);
    }
    Serial.printf("[BOOT] Buffers: WAV=%u KB  MP3=%u KB\n",
                  WAV_MAX_BYTES / 1024, MP3_MAX_BYTES / 1024);

    // ── AUDIO ──
    pinMode(PIN_PA_EN, OUTPUT); digitalWrite(PIN_PA_EN, LOW);
    bool codec_ok = initCodecPlayback();
    bool mic_ok   = initMic();

    // ── WIFI ──
    currentState = ST_WIFI;
    tft.fillScreen(C_BG);
    drawHeader(false);
    drawFooter();

    bool wifi_ok = connectWifi();

    // ── Draw initial UI ──
    tft.fillScreen(C_BG);
    drawHeader(wifi_ok);
    drawFooter();

    if (!wifi_ok) {
        snprintf(errorMsg, 80, "WiFi failed - check config");
        currentState = ST_ERROR;
        drawError(errorMsg);
        drawInfoLine(errorMsg, C_RED);
        return;
    }

    if (!codec_ok) {
        snprintf(errorMsg, 80, "Codec init failed");
        currentState = ST_ERROR;
        drawError(errorMsg);
        drawInfoLine(errorMsg, C_RED);
        return;
    }

    if (!mic_ok) {
        snprintf(errorMsg, 80, "Mic init failed");
        currentState = ST_ERROR;
        drawError(errorMsg);
        drawInfoLine(errorMsg, C_RED);
        return;
    }

    currentState = ST_IDLE;
    drawIdle();
    drawInfoLine("Ready — hold BOOT to speak", C_DIM);
    Serial.println("[BOOT] Setup complete — entering main loop");
}

// ═══════════════════════════════════════════════════════════════
//  MAIN LOOP
// ═══════════════════════════════════════════════════════════════
void loop() {

    // ── ERROR: auto-return to IDLE after 3 seconds ────────────────────────────
    if (currentState == ST_ERROR) {
        if (millis() - errorTime > 3000) {
            currentState = ST_IDLE;
            tft.fillScreen(C_BG);
            drawHeader(WiFi.status() == WL_CONNECTED);
            drawFooter();
            drawIdle();
            drawInfoLine("Ready — hold BOOT to speak", C_DIM);
        }
        return;
    }

    // ── IDLE: pulse animation + wait for button ────────────────────────────────
    if (currentState == ST_IDLE) {
        // Animate idle shimmer every 40ms
        if (millis() - idleLastMs > 40) {
            idlePhase   = (idlePhase + 4) % 256;
            idleLastMs  = millis();
            drawIdle();
        }

        // Button pressed → start recording
        if (digitalRead(PIN_BUTTON) == LOW) {
            delay(30);   // debounce
            if (digitalRead(PIN_BUTTON) == LOW) {
                currentState = ST_RECORDING;
                tft.fillScreen(C_BG);
                drawHeader(WiFi.status() == WL_CONNECTED);
                drawFooter();
                drawInfoLine("Listening... release to send", C_RED);
                Serial.println("[STATE] → RECORDING");
            }
        }
        return;
    }

    // ── RECORDING: capture mic until button released ───────────────────────────
    if (currentState == ST_RECORDING) {
        wav_len = recordAudio(MAX_RECORD_SECS);

        currentState = ST_UPLOADING;
        tft.fillScreen(C_BG);
        drawHeader(WiFi.status() == WL_CONNECTED);
        drawFooter();
        Serial.println("[STATE] → UPLOADING");

        if (wav_len == 0) {
            snprintf(errorMsg, 80, "No audio captured");
            currentState = ST_ERROR;
            errorTime    = millis();
            drawError(errorMsg);
            drawInfoLine(errorMsg, C_RED);
            return;
        }

        // Reconnect WiFi if dropped
        if (WiFi.status() != WL_CONNECTED) {
            drawInfoLine("WiFi reconnecting...", C_YEL);
            WiFi.reconnect();
            uint32_t t = millis();
            while (WiFi.status() != WL_CONNECTED && millis() - t < 10000) delay(200);
            if (WiFi.status() != WL_CONNECTED) {
                snprintf(errorMsg, 80, "WiFi lost");
                currentState = ST_ERROR;
                errorTime    = millis();
                drawError(errorMsg);
                drawInfoLine(errorMsg, C_RED);
                return;
            }
        }

        char transcript[160] = "";
        char spoken[160]     = "";
        int  code = sendToCloud(wav_len, transcript, 160, spoken, 160);

        if (code != 200) {
            snprintf(errorMsg, 80, "Server error %d", code);
            currentState = ST_ERROR;
            errorTime    = millis();
            drawError(errorMsg);
            drawInfoLine(errorMsg, C_RED);
            return;
        }

        // ── Show what was heard ──
        if (SHOW_TRANSCRIPT && transcript[0]) {
            drawInfoLine(transcript, C_CYAN);
        }

        currentState = ST_THINKING;
        tft.fillScreen(C_BG);
        drawHeader(true);
        drawFooter();
        drawThinking();
        Serial.println("[STATE] → THINKING (brief — then SPEAKING)");
        delay(200);   // tiny pause so user sees the "thinking" state

        // ── Play response ──
        currentState = ST_SPEAKING;
        tft.fillScreen(C_BG);
        drawHeader(true);
        drawFooter();
        if (SHOW_TRANSCRIPT && spoken[0]) {
            drawInfoLine(spoken, C_LIME);
        }
        Serial.println("[STATE] → SPEAKING");
        playMp3();

        // ── Return to IDLE ──
        currentState = ST_IDLE;
        idlePhase    = 0;
        tft.fillScreen(C_BG);
        drawHeader(WiFi.status() == WL_CONNECTED);
        drawFooter();
        drawIdle();
        if (SHOW_TRANSCRIPT && transcript[0]) {
            drawInfoLine(transcript, C_DIM);
        } else {
            drawInfoLine("Ready — hold BOOT to speak", C_DIM);
        }
        Serial.println("[STATE] → IDLE");
        return;
    }

    delay(10);
}
