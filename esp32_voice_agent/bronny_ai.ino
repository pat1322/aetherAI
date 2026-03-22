/*
 * BRONNY AI v5.7 - AetherAI Edition
 * by Patrick Perez
 *
 * v5.7 changes vs v5.6:
 *   [Boot intro]
 *   - doBootIntro(): on first boot after WiFi+Deepgram connect, calls Railway with
 *     "Hello! Briefly introduce yourself." so Bronny greets the user immediately.
 *     NOTE: Railway system prompt must define the AI persona as "Bronny" — if Bronny
 *     introduces himself as "AetherAI" or any other name, fix the system prompt on the
 *     Railway/LLM backend side, not here. The ESP32 just sends the text prompt.
 *   [Standby mode]
 *   - After STANDBY_TIMEOUT_MS (3 min) of no Railway calls, enters standby automatically.
 *   - Standby: face shows closed eyes + floating ZZZ animation; mic stays on; Deepgram
 *     WS stays connected; audio to Deepgram keeps streaming — all existing logic intact.
 *   - While in standby, transcripts are intercepted BEFORE reaching Railway. Only a wake
 *     word exits standby; everything else is silently discarded.
 *   - Wake words: "bronny", "bronnie", "brony", "brownie", "brawny", "bonnie" and
 *     "hi/hey" prefixed variants — any phrase containing these triggers wake-up.
 *   - On wake: plays jingleWake(), face returns to Idle, status resets to Listening.
 *     The wake word utterance itself is NOT sent to Railway (just wakes the device).
 *   - Standby is NOT the default mode; default is normal listening mode.
 *   - New globals: lastRailwayMs (stamped on every successful Railway call + boot intro),
 *     standbyMode flag.
 *   [Sleep face]
 *   - New face state FS_SLEEP: closed eyes (thin 4px horizontal bars), smile mouth,
 *     no bob, no eye-scale breathing.
 *   - ZZZ animation: 3 particles float upward and fade from bright cyan → dim → invisible,
 *     drawn directly to TFT (bypasses faceRedraw system).
 *     clearZzz() erases all ZZZ artifacts when exiting standby.
 *
 * Retained from v5.6: all bug fixes (A/B/C + 1-6), Railway timeout fallback, VAD cleanup,
 * thinking chime, live partials, endpointing=350ms, persistent Deepgram WS.
 *
 * Hardware:
 *   Board   : ESP32-S3 Dev Module (OPI PSRAM 8MB)
 *   Codec   : ES8311 (I2C addr 0x18)
 *   Mic     : INMP441 (I2S port 1, GPIOs 4/5/6)
 *   Display : ST7789 320x240 (HSPI)
 *
 * Libraries (Arduino Library Manager):
 *   arduino-audio-tools + arduino-audio-driver by pschatzmann
 *   Adafruit ST7789 + Adafruit GFX
 *   WebSockets by Markus Sattler
 *   ArduinoJson by Benoit Blanchon
 *
 * voice_config.h must define:
 *   WIFI_SSID_CFG, WIFI_PASS_CFG
 *   DEEPGRAM_API_KEY
 *   AETHER_URL, AETHER_API_KEY
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
extern bool faceRedraw;

// ============================================================
// CONFIG
// ============================================================
const char* WIFI_SSID = WIFI_SSID_CFG;
const char* WIFI_PASS = WIFI_PASS_CFG;

#define VOL_MAIN             0.50f
#define VOL_JINGLE           0.25f
#define TTS_COOLDOWN_MS       800
#define MP3_MAX_BYTES        (320 * 1024)
#define HEARTBEAT_MS         30000

// Deepgram persistent connection
// KeepAlive must be sent every <10s when not sending audio (during TTS playback)
#define DG_KEEPALIVE_MS      8000
#define DG_RECONNECT_MS      3000   // backoff before reconnect attempt
#define DG_CONNECT_TIMEOUT   8000   // ms to wait for initial WS connect

// If speech_final hasn't arrived within this many ms after is_final, self-trigger.
// Fixes the "transcript on screen but Railway never fires" issue caused by ambient
// background noise keeping Deepgram's endpointing timer from completing.
#define DG_FINAL_TIMEOUT_MS  700

// Standby: enter after this many ms with no Railway call (3 minutes).
#define STANDBY_TIMEOUT_MS   180000UL

static uint32_t lastHbMs        = 0;
static uint32_t vadCooldownUntil = 0;
static uint32_t lastRailwayMs   = 0;    // stamped on every successful Railway call
static bool     standbyMode     = false;

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
static String   gFooterText  = "v5.7 Ready";
static uint16_t gFooterColor = C_CY;

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
static uint8_t* mp3Buf = nullptr;
static size_t   mp3Len = 0;

static bool audioOk   = false;
static bool micOk     = false;
static bool inTtsMode = false;

static inline int16_t inmp441Sample(int32_t raw) { return (int16_t)(raw >> 14); }

// ============================================================
// AUDIO INIT
// ============================================================
// File-scope guard so audioRestart() can reset it before re-registering pins.
// (Bug 3 fix: was a local static inside audioPinsSetup(), which made it impossible
//  to reset, causing duplicate pin registration on every audioRestart() call.)
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
        while(millis()<e){mic_stream.readBytes(tmp,sizeof(tmp));yield();}
    }
}

void audioInitRec() {
    if (inTtsMode || !audioOk) {
        audioPinsSetup();
        auto cfg=i2s.defaultConfig(TX_MODE); cfg.copyFrom(ainf_rec);
        cfg.output_device=DAC_OUTPUT_ALL;
        audioOk=i2s.begin(cfg); i2s.setVolume(VOL_MAIN);
        if (audioOk){auto sc=sineGen.defaultConfig();sc.copyFrom(ainf_rec);sineGen.begin(sc);}
        inTtsMode=false;
    }
}

void audioInitTTS() {
    if (!inTtsMode) {
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
    // Bug 3 fix: reset guard so audioPinsSetup() re-registers pins cleanly.
    // Previously, brdPins.addI2C/I2S/Pin were called directly here, bypassing
    // the guard and adding duplicate pin definitions on every restart.
    audioPinsSet = false;
    audioPinsSetup();   // handles Wire.begin + all pin registration
    auto cfg=i2s.defaultConfig(TX_MODE); cfg.copyFrom(ainf_rec);
    cfg.output_device=DAC_OUTPUT_ALL;
    audioOk=i2s.begin(cfg); i2s.setVolume(VOL_MAIN);
    if(audioOk){auto sc=sineGen.defaultConfig();sc.copyFrom(ainf_rec);sineGen.begin(sc);}
}

void playTone(float hz, int ms) {
    if(!audioOk){delay(ms);return;}
    sineGen.setFrequency(hz);
    uint32_t e=millis()+ms;
    while(millis()<e){sineCopy.copy();yield();}
}
void playSil(int ms){playTone(0,ms);}

void jingleBoot(){
    audioInitRec();i2s.setVolume(VOL_JINGLE);
    float n[]={523,659,784,1047,1319,1568,2093};int d[]={100,100,100,140,260,80,280};
    for(int i=0;i<7;i++){playTone(n[i],d[i]);playSil(20);}i2s.setVolume(VOL_MAIN);
}
void jingleConnect(){
    audioInitRec();i2s.setVolume(VOL_JINGLE);
    playTone(880,100);playSil(25);playTone(1108,100);playSil(25);
    playTone(1318,200);playSil(150);i2s.setVolume(VOL_MAIN);
}
void jingleError(){
    audioInitRec();i2s.setVolume(VOL_JINGLE);
    playTone(300,200);playSil(80);playTone(220,350);playSil(200);i2s.setVolume(VOL_MAIN);
}
void jingleReady(){
    audioInitRec();i2s.setVolume(VOL_JINGLE);
    playTone(880,80);playSil(30);playTone(1318,80);playSil(30);
    playTone(1760,200);playSil(150);i2s.setVolume(VOL_MAIN);
}
void jingleWake(){
    audioInitRec();i2s.setVolume(VOL_JINGLE);
    playTone(660,80);playSil(20);playTone(1100,120);playSil(80);i2s.setVolume(VOL_MAIN);
}

// ============================================================
// JSON ESCAPE
// ============================================================
String jEsc(const String& s) {
    String o; o.reserve(s.length()+16);
    for (int i=0; i<(int)s.length(); i++) {
        unsigned char c=(unsigned char)s[i];
        if(c=='"')o+="\\\""; else if(c=='\\')o+="\\\\";
        else if(c=='\n')o+="\\n"; else if(c=='\r')o+="\\r";
        else if(c=='\t')o+="\\t"; else if(c>=0x20)o+=(char)c;
    }
    return o;
}

// ============================================================
// WIFI HELPER
// ============================================================
static void ensureWifi() {
    if (WiFi.status()!=WL_CONNECTED) {
        WiFi.reconnect(); uint32_t t=millis();
        while(WiFi.status()!=WL_CONNECTED&&millis()-t<8000){delay(300);yield();}
    }
}

static String baseUrl() {
    String u=String(AETHER_URL);
    while(u.endsWith("/"))u.remove(u.length()-1);
    return u;
}

// ============================================================
// HEARTBEAT
// ============================================================
void sendHeartbeat() {
    if(WiFi.status()!=WL_CONNECTED)return;
    // Bug C fix: pump Deepgram WS before blocking on the HTTP POST so the
    // persistent connection stays alive even if the heartbeat server is slow.
    maintainDeepgram();
    WiFiClientSecure cli; cli.setInsecure(); cli.setConnectionTimeout(8000);
    HTTPClient http;
    http.begin(cli, baseUrl()+"/bronny/heartbeat");
    http.setTimeout(8000);
    http.addHeader("Content-Type","application/json");
    int code=http.POST("{\"device\":\"bronny\",\"version\":\"5.6\"}");
    http.end();
    // Pump again after the POST in case it blocked long enough to matter.
    maintainDeepgram();
    if(code!=200)Serial.printf("[HB] fail %d\n",code);
    else Serial.println("[HB] OK");
}

// ============================================================
// RAILWAY -> POST /voice/text -> MP3
// ============================================================
bool callRailway(const String& transcript) {
    if(transcript.isEmpty())return false;
    ensureWifi(); if(WiFi.status()!=WL_CONNECTED)return false;

    if(!mp3Buf){
        mp3Buf=(uint8_t*)heap_caps_malloc(MP3_MAX_BYTES,MALLOC_CAP_SPIRAM);
        if(!mp3Buf){Serial.println("[Rail] mp3Buf alloc FAILED");return false;}
    }

    String body="{\"text\":\""+jEsc(transcript)+"\"}";
    String url=baseUrl()+"/voice/text";
    Serial.printf("[Rail] POST text='%s'\n",transcript.c_str());

    bool success=false;
    for(int attempt=1;attempt<=2&&!success;attempt++){
        if(attempt>1){Serial.println("[Rail] retry...");delay(2000);}
        WiFiClientSecure cli; cli.setInsecure(); cli.setConnectionTimeout(20000);
        HTTPClient http; http.begin(cli,url);
        http.setTimeout(45000);
        http.addHeader("Content-Type","application/json");
        http.addHeader("X-Api-Key",AETHER_API_KEY);
        int code=http.POST(body);
        Serial.printf("[Rail] HTTP %d\n",code);
        if(code==200){
            WiFiClient* stream=http.getStreamPtr();
            int clen=http.getSize();
            size_t want=(clen>0)?min((size_t)clen,(size_t)MP3_MAX_BYTES):(size_t)MP3_MAX_BYTES;
            size_t got=0; uint32_t dlEnd=millis()+35000;
            while((http.connected()||stream->available())&&got<want&&millis()<dlEnd){
                if(stream->available()){
                    size_t chunk=min((size_t)1024,want-got);
                    got+=stream->readBytes(mp3Buf+got,chunk);
                }else{delay(2);}
                // Bug A fix: pump the Deepgram WS during download so the persistent
                // connection stays alive and KeepAlives are sent. Without this, a
                // Railway response that takes >10s causes a Deepgram disconnect.
                maintainDeepgram();
                yield();
            }
            mp3Len=got; success=(got>0);
            Serial.printf("[Rail] MP3 %u bytes\n",(unsigned)mp3Len);
        }
        http.end(); yield();
    }
    return success;
}

// ============================================================
// MP3 PLAYBACK
// During playback: mic is off, maintainDeepgram() sends KeepAlive pings
// so the persistent WS stays alive for the next turn.
// ============================================================
void playMp3Smooth() {
    if(!mp3Len||!mp3Buf){Serial.println("[Play] No MP3");return;}
    Serial.printf("[Play] smooth %u bytes\n",(unsigned)mp3Len);

    mic_stream.end(); micOk=false;
    delay(60); audioInitTTS(); delay(80);

    // Bug 1 fix: removed dead pcmBuf heap_caps_malloc+free block that was here.
    // The alloc succeeded, was immediately freed, and the streaming path below
    // always ran regardless — wasting a PSRAM malloc/free every single call.

    {int16_t sil[512]={};i2s.write((uint8_t*)sil,sizeof(sil));delay(200);}

    EncodedAudioStream decoded(&i2s,&mp3Decoder);
    decoded.begin();
    MemoryStream mp3Mem(mp3Buf,mp3Len);

    const size_t BYTES_PER_TICK=512;
    while(mp3Mem.available()>0){
        size_t avail=(size_t)mp3Mem.available();
        size_t toRead=min(avail,BYTES_PER_TICK);
        uint8_t tmp[512];
        size_t got=mp3Mem.readBytes(tmp,toRead);
        if(got>0)decoded.write(tmp,got);
        maintainDeepgram();  // keepalive while playing
        animFace(); if(faceRedraw){drawFace(false);faceRedraw=false;}
        yield();
    }
    decoded.end();

    {int16_t sil[128]={};i2s.write((uint8_t*)sil,sizeof(sil));}
    delay(60);
    Serial.println("[Play] Done");

    audioInitRec(); micInit();
    if(micOk){
        uint8_t drain[512]; uint32_t e=millis()+300;
        while(millis()<e){mic_stream.readBytes(drain,sizeof(drain));yield();}
    }
}

// ============================================================
// DEEPGRAM PERSISTENT STREAMING ASR
//
// Architecture (v5.4):
//   - ONE WebSocket connection for the entire device lifetime
//   - Audio streams continuously whenever mic is active
//   - Deepgram's server-side VAD detects speech/silence
//   - speech_final=true fires -> sets pendingTranscript flag
//   - Main loop picks up pendingTranscript -> calls Railway
//   - During TTS playback (mic off) -> KeepAlive sent every 8s
//   - On unexpected disconnect -> auto-reconnect after DG_RECONNECT_MS
//
// Deepgram query params:
//   encoding=linear16     raw signed 16-bit PCM
//   sample_rate=16000     matches INMP441
//   channels=1            mono (left channel only)
//   language=en           handles Filipino-accented English
//   model=nova-2          best accuracy on free tier
//   interim_results=true  partials for TFT display
//   endpointing=350       ms silence -> speech_final (reduced from 600 in v5.5)
//   filler_words=false    suppress "uh", "um"
// ============================================================
WebSocketsClient dgWs;

// Connection state
static bool     dgConnected          = false;
static bool     dgStreaming           = false;  // true = mic active, sending audio
static uint32_t dgLastKeepalive       = 0;
static uint32_t dgLastConnectAttempt  = 0;

// Transcript state - set by callback, consumed by main loop
static bool     pendingTranscript     = false;
static String   dgFinal               = "";
static String   dgPartial             = "";
// Timestamp of the last is_final=true message with non-empty text.
// Used by loop() to self-trigger Railway if speech_final is delayed by background noise.
static uint32_t dgFinalReceivedAt     = 0;

// Conversation pipeline lock — declared here (file scope) so onDgWsEvent()
// can read it when deciding whether to restore dgStreaming on reconnect.
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

// File-scope static so it lives in BSS, not on the WS callback's stack.
// Bug C fix: StaticJsonDocument<4096> on the stack inside a WebSocket callback
// puts 4KB on a task stack that's only 8KB total — dangerously close to overflow.
static StaticJsonDocument<4096> dgJsonDoc;

static void parseDgMsg(const char* json, size_t len) {
    dgJsonDoc.clear();
    if (deserializeJson(dgJsonDoc, json, len) != DeserializationError::Ok) return;
    auto& doc = dgJsonDoc;

    const char* msgType = doc["type"] | "";

    if (strcmp(msgType, "Results") == 0) {
        const char* txt = doc["channel"]["alternatives"][0]["transcript"] | "";
        bool isFinal   = doc["is_final"]    | false;
        bool speechEnd = doc["speech_final"] | false;

        if (strlen(txt) > 0) {
            if (isFinal) {
                if (dgFinal.length() > 0) dgFinal += " ";
                dgFinal += String(txt);
                // Stamp time so loop() can self-trigger if speech_final is delayed
                // by background noise keeping Deepgram's endpointing timer running.
                dgFinalReceivedAt = millis();
                // Show final segment on footer (replaces partial)
                setFooterOnly(C_MINT, txt);
                Serial.printf("[DG] final: %s\n", txt);
            } else {
                dgPartial = String(txt);
                // Feature 5: show live partial on the footer so the user sees
                // words appear in real time (doesn't eat a scrolling log line).
                char pfx[56];
                snprintf(pfx, sizeof(pfx), "> %s", txt);
                setFooterOnly(C_LG, pfx);
            }
        }

        // speech_final = Deepgram detected natural end-of-speech (fast path)
        if (speechEnd && dgFinal.length() > 0) {
            pendingTranscript    = true;
            dgFinalReceivedAt    = 0;  // cancel the timeout — speech_final beat it
            Serial.printf("[DG] speech_final -> '%s'\n", dgFinal.c_str());
        }

    } else if (strcmp(msgType, "Metadata") == 0) {
        // Metadata arrives after CloseStream (not used in persistent mode)
        // but handle gracefully in case of edge cases
        Serial.println("[DG] Metadata received (persistent mode)");

    } else if (strcmp(msgType, "Error") == 0) {
        const char* desc = doc["description"] | "unknown";
        Serial.printf("[DG] Error: %s\n", desc);
        tftLogf(C_RD, "DG err: %.40s", desc);
    }
}

void onDgWsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_CONNECTED:
            dgConnected      = true;
            dgLastKeepalive  = millis();
            // Bug 2 fix: after an unexpected drop + reconnect, dgStreaming was left
            // false by the DISCONNECTED handler and was never restored here, so the
            // device would reconnect but stop sending audio until the next reboot.
            if (!busy) dgStreaming = true;
            tftLog(C_GR, "Deepgram: connected");
            Serial.println("[DG] WS connected");
            break;

        case WStype_TEXT:
            parseDgMsg((const char*)payload, length);
            break;

        case WStype_DISCONNECTED:
            dgConnected  = false;
            dgStreaming   = false;
            tftLog(C_WARN, "Deepgram: disconnected, reconnecting...");
            Serial.println("[DG] WS disconnected");
            break;

        case WStype_ERROR:
            dgConnected  = false;
            dgStreaming   = false;
            tftLog(C_RD, "Deepgram: WS error");
            Serial.println("[DG] WS error");
            break;

        default: break;
    }
}

// Call once on boot to establish the persistent connection
void connectDeepgram() {
    String authHdr = "Authorization: Token " + String(DEEPGRAM_API_KEY);
    dgWs.onEvent(onDgWsEvent);
    dgWs.setExtraHeaders(authHdr.c_str());
    dgWs.beginSSL("api.deepgram.com", 443, DG_PATH);
    dgLastConnectAttempt = millis();
    tftLog(C_CY, "Deepgram: connecting...");

    // Wait for initial connect
    uint32_t deadline = millis() + DG_CONNECT_TIMEOUT;
    while (!dgConnected && millis() < deadline) {
        dgWs.loop();
        yield();
    }

    if (dgConnected) {
        tftLog(C_GR, "Deepgram: ready");
    } else {
        tftLog(C_YL, "Deepgram: connect timeout (will retry)");
    }
}

// Called every loop iteration AND during playback
// Handles: WS pump, audio streaming, keepalive, reconnect
static int32_t s_rawBuf[1600 * 2];
static int16_t s_pcmBuf[1600];

void maintainDeepgram() {
    uint32_t now = millis();

    // Always pump the WS event loop
    dgWs.loop();

    // --- Reconnect if dropped ---
    if (!dgConnected && now - dgLastConnectAttempt > DG_RECONNECT_MS) {
        Serial.println("[DG] Reconnecting...");
        dgLastConnectAttempt = now;
        String authHdr = "Authorization: Token " + String(DEEPGRAM_API_KEY);
        dgWs.setExtraHeaders(authHdr.c_str());
        dgWs.beginSSL("api.deepgram.com", 443, DG_PATH);
        return;
    }

    if (!dgConnected) return;

    // --- Stream audio if mic is active ---
    if (dgStreaming && micOk) {
        int bytesRead = mic_stream.readBytes((uint8_t*)s_rawBuf, sizeof(s_rawBuf));
        int frames    = bytesRead / 8;
        if (frames > 0) {
            for (int i = 0; i < frames; i++)
                s_pcmBuf[i] = inmp441Sample(s_rawBuf[i * 2]);
            dgWs.sendBIN((uint8_t*)s_pcmBuf, frames * 2);
        }
        return;  // skip keepalive logic while streaming (audio itself keeps connection alive)
    }

    // --- KeepAlive when NOT streaming (during TTS playback or standby) ---
    // Deepgram closes idle connections after ~10s without data
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
            if(w.length()>0&&w!=fw){allSame=false;break;}
            wi=(sp<0)?lower.length():sp+1;
        }
        if(allSame&&fw.length()<=6) return true;
    }
    return false;
}

// ============================================================
// TFT LOG ZONE
// ============================================================
static void logRedraw(){
    tft.fillRect(0,LOG_Y,W,LOG_LINES*LOG_LINE_H+2,C_BK);
    int total=min(gLogCount,LOG_LINES);
    for(int i=0;i<total;i++){
        int slot=(gLogHead+i)%LOG_LINES;
        uint16_t c=gLogCol[slot];
        if(i<total-2){
            uint16_t r=((c>>11)&0x1F)>>1;
            uint16_t g=((c>>5)&0x3F)>>1;
            uint16_t b=(c&0x1F)>>1;
            c=(r<<11)|(g<<5)|b;
        }
        tft.setTextColor(c); tft.setTextSize(1);
        tft.setCursor(2,LOG_Y+1+i*LOG_LINE_H);
        tft.print(gLog[slot]);
    }
}

static void logDrawFooter(){
    tft.fillRect(0,LOG_FOOTER_Y,W,14,C_BK);
    tft.drawFastHLine(0,LOG_FOOTER_Y-1,W,0x1082);
    tft.setTextColor(gFooterColor); tft.setTextSize(1);
    int tw=(int)gFooterText.length()*6;
    tft.setCursor(W/2-tw/2,LOG_FOOTER_Y+3);
    tft.print(gFooterText);
}

void tftLog(uint16_t col, const char* msg){
    String s=String(msg);
    if((int)s.length()>53)s=s.substring(0,53);
    if(gLogCount<LOG_LINES){
        gLog[gLogCount]=s; gLogCol[gLogCount]=col; gLogCount++;
        tft.fillRect(0,LOG_Y+(gLogCount-1)*LOG_LINE_H,W,LOG_LINE_H,C_BK);
        tft.setTextColor(col); tft.setTextSize(1);
        tft.setCursor(2,LOG_Y+(gLogCount-1)*LOG_LINE_H+1);
        tft.print(s);
    } else {
        gLog[gLogHead]=s; gLogCol[gLogHead]=col;
        gLogHead=(gLogHead+1)%LOG_LINES;
        logRedraw();
    }
}

void tftLogf(uint16_t col, const char* fmt, ...){
    char buf[80]; va_list ap; va_start(ap,fmt);
    vsnprintf(buf,sizeof(buf),fmt,ap); va_end(ap);
    tftLog(col,buf);
}

void setStatus(const char* s, uint16_t c){
    gFooterText=String(s); gFooterColor=c;
    logDrawFooter();
    // Bug 4 fix: removed tftLog(c,s) call that was here.
    // setStatus was double-writing every state change ("Listening...", "Speaking...", etc.)
    // into the 4-line scrolling log, pushing out actual transcript and error messages.
    // Status belongs in the footer only. Use tftLog() explicitly when you want a log entry.
}

// Feature 5: update footer only — used for live partial transcripts so they
// appear in real time without consuming a scrolling log line.
void setFooterOnly(uint16_t c, const char* s){
    char buf[54]; strncpy(buf,s,53); buf[53]='\0';
    gFooterText=String(buf); gFooterColor=c;
    logDrawFooter();
}

// ============================================================
// FACE STATE MACHINE
// ============================================================
enum FaceState {
    FS_IDLE, FS_TALKING, FS_LISTEN, FS_THINK, FS_HAPPY, FS_SURPRISED, FS_SLEEP
};

struct FaceData {
    FaceState state=FS_IDLE;
    float bobPh=0.f; int8_t bobY=0,pBobY=0;
    bool blink=false; int blinkF=0;
    float talkPh=0.f,mOpen=0.f,pOpen=0.f;
    float listenPulse=0.f,thinkSq=0.f,happyPh=0.f;
    float surpriseScale=1.f,eyeScaleX=1.f,eyeScaleY=1.f;
    uint32_t emotionTimer=0;
    float lookX=0.f,lookY=0.f,tLookX=0.f,tLookY=0.f;
    uint32_t nextLookMs=0;
    float prevBlink=1.f,prevSX=1.f,prevSY=1.f,prevSquint=0.f;
    int8_t prevLookXi=0,prevLookYi=0;
    bool  prevSmile=false,prevTalking=false;
    int   prevMouthH=0,prevMouthY=0;
    bool  prevMouthValid=false;
} face;

bool            faceRedraw   = false;
static uint32_t lastBlink    = 0;
static uint32_t nextBlink    = 3200;
static uint32_t lastFaceAnim = 0;

// ============================================================
// ZZZ SLEEP ANIMATION
// Managed by animFace() FS_SLEEP case. Drawn directly to TFT.
// ============================================================
struct ZzzParticle {
    int16_t x, y;    // current draw position
    int16_t px, py;  // previous draw position (for erasing)
    uint8_t ph;      // 0-255 phase cycle (drives position + color)
    uint8_t sz;      // text size (1=small, 2=big)
    bool    valid;   // false until first draw (no erase on first frame)
};
static ZzzParticle s_zzz[3];

static void initZzz() {
    // Stagger phases so three ZZZs appear offset in their float cycle.
    // Start positions are to the right of the face, near the right eye.
    s_zzz[0] = { (int16_t)(FCX+22), (int16_t)(FCY+22), 0,0,   0,   1, false };
    s_zzz[1] = { (int16_t)(FCX+30), (int16_t)(FCY+10), 0,0,   85,  1, false };
    s_zzz[2] = { (int16_t)(FCX+20), (int16_t)(FCY-4),  0,0,   170, 2, false };
}

static void clearZzz() {
    // Erase any remaining ZZZ artifacts when exiting sleep.
    for (int i = 0; i < 3; i++) {
        if (!s_zzz[i].valid) continue;
        int tw = (s_zzz[i].sz == 2) ? 13 : 7;
        int th = (s_zzz[i].sz == 2) ? 17 : 9;
        tft.fillRect(s_zzz[i].px - 1, s_zzz[i].py - 1, tw + 2, th + 2, C_BK);
        s_zzz[i].valid = false;
    }
}

static void animZzz() {
    // Origin positions (phase=0). Particles drift up 55px and right 18px over full cycle.
    static const int16_t ox[3] = { FCX+22, FCX+30, FCX+20 };
    static const int16_t oy[3] = { FCY+22, FCY+10, FCY-4  };

    for (int i = 0; i < 3; i++) {
        // Erase previous frame
        if (s_zzz[i].valid) {
            int tw = (s_zzz[i].sz == 2) ? 13 : 7;
            int th = (s_zzz[i].sz == 2) ? 17 : 9;
            tft.fillRect(s_zzz[i].px - 1, s_zzz[i].py - 1, tw + 2, th + 2, C_BK);
        }
        // Advance phase (uint8_t wraps 255->0 automatically)
        s_zzz[i].ph++;
        // Compute position from phase
        s_zzz[i].x = ox[i] + (int16_t)((uint16_t)s_zzz[i].ph * 18 / 255);
        s_zzz[i].y = oy[i] - (int16_t)((uint16_t)s_zzz[i].ph * 55 / 255);
        // Clamp to face area
        if (s_zzz[i].y < 4) s_zzz[i].y = 4;
        if (s_zzz[i].x > W - 12) s_zzz[i].x = W - 12;
        // Color fades: bright -> dim -> near-invisible over the cycle
        uint16_t col = (s_zzz[i].ph < 100) ? C_CY
                     : (s_zzz[i].ph < 190) ? C_DCY
                     :                        0x0209;
        // Draw
        tft.setTextSize(s_zzz[i].sz);
        tft.setTextColor(col);
        tft.setCursor(s_zzz[i].x, s_zzz[i].y);
        tft.print("z");
        s_zzz[i].px = s_zzz[i].x;
        s_zzz[i].py = s_zzz[i].y;
        s_zzz[i].valid = true;
    }
    tft.setTextSize(1);  // always restore default text size
}

static void drawSmile(int cx,int cy){
    tft.fillCircle(cx,cy,SMILE_R,C_WH);
    tft.fillRect(cx-SMILE_R-1,cy-SMILE_R-1,(SMILE_R+1)*2+2,SMILE_R+2,C_BK);
    int innerR=SMILE_R-SMILE_TH;
    if(innerR>1){
        tft.fillCircle(cx,cy,innerR,C_BK);
        tft.fillRect(cx-innerR-1,cy-innerR-1,(innerR+1)*2+2,innerR+2,C_BK);
    }
}
static void drawOneEye(int cx,int cy,float openFrac,float squint,float scaleX,float scaleY){
    int ew=max(8,(int)(EW*scaleX));
    int eh=max(2,(int)(EH*openFrac*scaleY*(1.f-squint*0.55f)));
    int r=min(ER,min(ew/2,eh/2));
    tft.fillRoundRect(cx-ew/2,cy-eh/2,ew,eh,r,C_WH);
}
static void drawMouth(int cx,int cy,float openFrac){
    int mh=MH_CL+(int)((MH_OP-MH_CL)*openFrac);
    int r=min(MR,mh/2);
    tft.fillRoundRect(cx-MW/2,cy-mh/2,MW,mh,r,C_WH);
}
void drawFaceBg(){tft.fillRect(0,0,W,LOG_Y,C_BK);}

static void eraseEyeUnion(int nCX,int nCY,int nEW,int nEH,int oCX,int oCY,int oEW,int oEH){
    int x1=min(oCX-oEW/2,nCX-nEW/2)-1,y1=min(oCY-oEH/2,nCY-nEH/2)-1;
    int x2=max(oCX+oEW/2,nCX+nEW/2)+1,y2=max(oCY+oEH/2,nCY+nEH/2)+1;
    y1=max(y1,0);y2=min(y2,LOG_Y-1);x1=max(x1,0);x2=min(x2,W-1);
    if(x2>x1&&y2>y1)tft.fillRect(x1,y1,x2-x1,y2-y1,C_BK);
}
static void eraseMouthDirty(){
    if(!face.prevMouthValid)return;
    if(face.prevSmile){
        tft.fillRect(FCX-SMILE_R-2,face.prevMouthY-2,(SMILE_R+2)*2,SMILE_R+4,C_BK);
    } else if(face.prevTalking){
        int halfH=face.prevMouthH/2+1;
        tft.fillRect(FCX-MW/2-2,face.prevMouthY-halfH-1,MW+4,face.prevMouthH+3,C_BK);
    }
    face.prevMouthValid=false;
}

void drawFace(bool full){
    int by=face.bobY;
    int lxi=(int)(face.lookX*LOOK_X_RANGE);
    int lyi=(int)(face.lookY*LOOK_Y_RANGE);
    int lex=FCX-ESEP/2+lxi,rex=FCX+ESEP/2+lxi;
    int ey=FCY+EYO+by+lyi,my=FCY+MYO+by;

    // Sleep face: closed eyes + smile. Bob is suppressed in animFace so positions
    // are stable. Only redraw on full request or the rare position change.
    if (face.state == FS_SLEEP) {
        if (full) {
            drawFaceBg();
            // Thin closed eyes (4px height = "---")
            tft.fillRoundRect(lex-EW/2, ey-2, EW, 4, 2, C_WH);
            tft.fillRoundRect(rex-EW/2, ey-2, EW, 4, 2, C_WH);
            drawSmile(FCX, my);
            face.prevSmile=true; face.prevTalking=false;
            face.prevMouthY=my; face.prevMouthValid=true; face.pOpen=0.f;
            face.prevBlink=0.f; face.prevSX=1.f; face.prevSY=1.f;
            face.prevSquint=0.f; face.prevLookXi=0; face.prevLookYi=0;
        }
        face.pBobY=(int8_t)by;
        return;
    }

    float blinkFrac=1.f;
    if(face.blink){
        int bf=face.blinkF;
        blinkFrac=(bf<=4)?(1.f-bf/4.f):((bf-4)/5.f);
        blinkFrac=constrain(blinkFrac,0.f,1.f);
    }
    float squint=(face.state==FS_THINK)?face.thinkSq:0.f;
    float sx=face.eyeScaleX,sy=face.eyeScaleY;
    if(face.state==FS_SURPRISED){sx=face.surpriseScale;sy=face.surpriseScale;}

    int oldLex=FCX-ESEP/2+(int)face.prevLookXi;
    int oldRex=FCX+ESEP/2+(int)face.prevLookXi;
    int oldEy=FCY+EYO+(int)face.pBobY+(int)face.prevLookYi;
    int oldEW2=max(8,(int)(EW*face.prevSX));
    int oldEH2=max(2,(int)(EH*face.prevBlink*face.prevSY*(1.f-face.prevSquint*0.55f)));
    int newEW2=max(8,(int)(EW*sx));
    int newEH2=max(2,(int)(EH*blinkFrac*sy*(1.f-squint*0.55f)));

    bool posChg=(by!=face.pBobY||lxi!=(int)face.prevLookXi||lyi!=(int)face.prevLookYi);
    bool shapeChg=(fabsf(blinkFrac-face.prevBlink)>0.03f||fabsf(sx-face.prevSX)>0.02f||fabsf(squint-face.prevSquint)>0.03f);
    bool eyeChg=full||posChg||shapeChg;

    bool isTalking=(face.state==FS_TALKING);
    int newMouthH=MH_CL+(int)((MH_OP-MH_CL)*face.mOpen);
    bool mouthChg=full||(isTalking!=face.prevTalking)||(!isTalking!=face.prevSmile)||
                  (isTalking&&fabsf(face.mOpen-face.pOpen)>0.018f)||(posChg&&face.prevMouthValid);

    if(full)drawFaceBg();

    if(eyeChg){
        if(!full){
            eraseEyeUnion(lex,ey,newEW2,newEH2,oldLex,oldEy,oldEW2,oldEH2);
            eraseEyeUnion(rex,ey,newEW2,newEH2,oldRex,oldEy,oldEW2,oldEH2);
        }
        drawOneEye(lex,ey,blinkFrac,squint,sx,sy);
        drawOneEye(rex,ey,blinkFrac,squint,sx,sy);
        face.prevBlink=blinkFrac; face.prevSX=sx; face.prevSY=sy;
        face.prevSquint=squint; face.prevLookXi=(int8_t)lxi; face.prevLookYi=(int8_t)lyi;
    }

    if(mouthChg){
        if(!full)eraseMouthDirty();
        if(isTalking){
            drawMouth(FCX,my,face.mOpen);
            face.prevTalking=true; face.prevSmile=false;
            face.prevMouthH=newMouthH; face.prevMouthY=my;
            face.prevMouthValid=true; face.pOpen=face.mOpen;
        } else {
            drawSmile(FCX,my);
            face.prevSmile=true; face.prevTalking=false;
            face.prevMouthY=my; face.prevMouthValid=true; face.pOpen=0.f;
        }
    }
    face.pBobY=(int8_t)by;
}

void animFace(){
    uint32_t now=millis();
    if(now-lastFaceAnim<16)return;
    lastFaceAnim=now;
    bool ch=false;

    if(face.emotionTimer>0&&now>face.emotionTimer){face.emotionTimer=0;face.state=FS_IDLE;ch=true;}

    if(face.state!=FS_LISTEN && face.state!=FS_SLEEP){
        face.bobPh+=0.020f; if(face.bobPh>6.2832f)face.bobPh-=6.2832f;
        int8_t nb=(int8_t)roundf(sinf(face.bobPh)*BOB);
        if(nb!=face.bobY){face.bobY=nb;ch=true;}
    }
    if(face.state!=FS_SURPRISED && face.state!=FS_SLEEP){
        float bScale=1.f+sinf(face.bobPh*0.5f)*0.04f;
        if(fabsf(bScale-face.eyeScaleX)>0.004f){face.eyeScaleX=bScale;face.eyeScaleY=2.f-bScale;ch=true;}
    }

    bool canLook=(face.state==FS_IDLE||face.state==FS_HAPPY);
    if(canLook){
        if(now>=face.nextLookMs){
            face.tLookX=((float)(random(7))/3.f-1.f);
            face.tLookY=((float)(random(5))/2.f-1.f)*0.6f;
            if(random(5)==0){face.tLookX=0.f;face.tLookY=0.f;}
            face.nextLookMs=now+600+random(2000);
        }
        face.lookX+=(face.tLookX-face.lookX)*0.07f;
        face.lookY+=(face.tLookY-face.lookY)*0.07f;
        if(fabsf(face.lookX-face.tLookX)>0.01f||fabsf(face.lookY-face.tLookY)>0.01f)ch=true;
    } else {
        face.lookX*=0.85f; face.lookY*=0.85f;
        if(fabsf(face.lookX)>0.01f||fabsf(face.lookY)>0.01f)ch=true;
    }
    if(face.state==FS_THINK){face.lookX+=(0.55f-face.lookX)*0.06f;face.lookY+=(-0.5f-face.lookY)*0.06f;ch=true;}

    bool canBlink=(face.state==FS_IDLE||face.state==FS_TALKING||face.state==FS_HAPPY);
    if(canBlink&&!face.blink&&now-lastBlink>nextBlink){face.blink=true;face.blinkF=0;nextBlink=2200+(uint32_t)random(2800);}
    if(face.blink){face.blinkF++;if(face.blinkF>=9){face.blink=false;face.blinkF=0;lastBlink=now;}ch=true;}

    switch(face.state){
        case FS_TALKING:{
            face.talkPh+=0.40f; if(face.talkPh>6.2832f)face.talkPh-=6.2832f;
            float jaw=sinf(face.talkPh)*0.55f;
            float t=constrain(0.28f+jaw+sinf(face.talkPh*1.7f)*0.22f+sinf(face.talkPh*0.4f)*0.12f,0.f,1.f);
            if(fabsf(t-face.mOpen)>0.015f){face.mOpen=t;ch=true;}
            float eyePop=1.f+fabsf(jaw)*0.05f;
            if(fabsf(eyePop-face.eyeScaleY)>0.01f){face.eyeScaleY=eyePop;ch=true;}
            break;
        }
        case FS_LISTEN:{
            face.listenPulse+=0.07f; if(face.listenPulse>6.2832f)face.listenPulse-=6.2832f;
            face.lookX+=(0.0f-face.lookX)*0.05f; face.lookY+=(-0.35f-face.lookY)*0.05f;
            if(fabsf(face.lookY+0.35f)>0.01f)ch=true; break;
        }
        case FS_THINK: if(face.thinkSq<0.72f){face.thinkSq+=0.03f;ch=true;} break;
        case FS_HAPPY:{
            face.happyPh+=0.14f; if(face.happyPh>6.2832f)face.happyPh-=6.2832f;
            float hB=1.f+sinf(face.happyPh)*0.07f;
            if(fabsf(hB-face.eyeScaleY)>0.01f){face.eyeScaleY=hB;ch=true;} break;
        }
        case FS_SURPRISED:{
            if(fabsf(face.surpriseScale-1.30f)>0.01f){face.surpriseScale+=(1.30f-face.surpriseScale)*0.18f;ch=true;}
            if(face.mOpen<0.90f){face.mOpen+=0.12f;ch=true;} break;
        }
        case FS_SLEEP:{
            // Animate the floating ZZZ particles directly to TFT.
            // ZZZ are drawn outside the faceRedraw system so ch stays false here —
            // drawFace() is not called; ZZZ are self-erasing each frame.
            animZzz();
            break;
        }
        default:break;
    }
    if(ch)faceRedraw=true;
}

void setFaceIdle(){face.state=FS_IDLE;face.thinkSq=0.f;face.mOpen=0.f;face.surpriseScale=1.f;face.emotionTimer=0;faceRedraw=true;}
void setFaceTalk(){face.state=FS_TALKING;face.talkPh=0.f;face.thinkSq=0.f;face.emotionTimer=0;faceRedraw=true;}
void setFaceThink(){face.state=FS_THINK;face.thinkSq=0.f;face.emotionTimer=0;faceRedraw=true;}
void setFaceHappy(uint32_t ms=1800){face.state=FS_HAPPY;face.happyPh=0.f;face.mOpen=0.f;face.emotionTimer=millis()+ms;faceRedraw=true;}
void setFaceSleep(){
    face.state=FS_SLEEP; face.bobY=0; face.bobPh=0.f;
    face.lookX=0.f; face.lookY=0.f; face.mOpen=0.f;
    face.emotionTimer=0; faceRedraw=true;
    initZzz();   // reset ZZZ particles to starting positions
}
void startTalk(){setFaceTalk();}
void stopTalk(){setFaceIdle();}

// ============================================================
// BOOT ANIM + WIFI SCREEN
// ============================================================
static void drawHexOutline(int cx,int cy,int r,uint16_t col){
    for(int i=0;i<6;i++){
        float a1=(i*60-30)*3.14159f/180.f,a2=((i+1)*60-30)*3.14159f/180.f;
        tft.drawLine(cx+(int)(r*cosf(a1)),cy+(int)(r*sinf(a1)),cx+(int)(r*cosf(a2)),cy+(int)(r*sinf(a2)),col);
    }
}
static void drawBMonogram(int cx,int cy,bool big){
    int scale=big?1:0; int bx=cx-7-scale,by2=cy-16-scale;
    int bw=5+scale*2,bh=32+scale*2,bumpW=16+scale*2,bumpH1=14+scale,bumpH2=17+scale;
    uint16_t col=big?C_MINT:C_CY;
    tft.fillRect(bx,by2,bw,bh,col);
    tft.fillRoundRect(bx,by2,bumpW,bumpH1,5,col);
    tft.fillRoundRect(bx,by2+bh/2,bumpW+2,bumpH2,6,col);
    tft.fillRect(bx+2,by2+2,bumpW-6,bumpH1-4,C_BK);
    tft.fillRect(bx+2,by2+bh/2+2,bumpW-4,bumpH2-5,C_BK);
}
void playBootIntroAnim(int cx,int cy){
    int hexR[]={56,52,48}; uint16_t hexC[]={0x18C3,C_DCY,C_CY};
    for(int i=0;i<3;i++){drawHexOutline(cx,cy,hexR[i],hexC[i]);delay(45);yield();}
    for(int r=44;r>=2;r-=2){uint16_t s=(r>30)?C_MID:(r>18)?C_BG:C_BK;drawHexOutline(cx,cy,r,s);delay(7);yield();}
    for(int r=44;r>=2;r-=4)drawHexOutline(cx,cy,r,C_WH);delay(55);yield();
    for(int r=44;r>=2;r-=2){uint16_t s=(r>30)?C_MID:(r>18)?C_BG:C_BK;drawHexOutline(cx,cy,r,s);}
    drawBMonogram(cx,cy,true);delay(60);yield();
    tft.fillRect(cx-32,cy-20,64,40,C_BK);
    for(int r=44;r>=2;r-=2){uint16_t s=(r>30)?C_MID:(r>18)?C_BG:C_BK;drawHexOutline(cx,cy,r,s);}
    drawBMonogram(cx,cy,false);delay(80);yield();
    for(int dy=14;dy>=0;dy-=2){
        tft.fillRect(0,cy+56,W,22,C_BK);tft.setTextSize(2);tft.setTextColor(C_WH);
        tft.setCursor(cx-36,cy+60+dy);tft.print("BRONNY");delay(22);yield();
    }
    tft.fillRoundRect(cx+42,cy+58,22,16,4,C_CY);tft.setTextSize(1);tft.setTextColor(C_BK);
    tft.setCursor(cx+46,cy+64);tft.print("AI");
    for(int x=cx-50;x<=cx+50;x+=4){tft.drawFastHLine(cx-50,cy+80,x-(cx-50),C_DCY);delay(12);yield();}
    const char* credit="by Patrick Perez"; int creditW=(int)strlen(credit)*6; int creditX=W/2-creditW/2;
    for(int dy=10;dy>=0;dy-=2){
        tft.fillRect(0,cy+82,W,14,C_BK);tft.setTextSize(1);tft.setTextColor(C_LG);
        tft.setCursor(creditX,cy+86+dy);tft.print(credit);delay(25);yield();
    }
    tft.setTextColor(C_DCY);tft.setTextSize(1);tft.setCursor(creditX+creditW+6,cy+86);tft.print("v5.7");
    delay(120);yield();
}
void drawBootBar(int pct){
    if(pct>100)pct=100;
    int bx=40,bw=W-80,bh=8,by=H-16;
    tft.fillRoundRect(bx,by,bw,bh,3,0x0841);
    int fw=(int)((float)bw*pct/100.f);
    if(fw>4){
        tft.fillRoundRect(bx,by,fw,bh,3,C_DCY);
        if(fw>10)tft.fillRoundRect(bx,by,fw-4,bh,3,C_CY);
        tft.drawFastHLine(bx+2,by+1,fw-4,C_MINT);
    }
}
void drawBootLogo(){
    tft.fillScreen(C_BK);
    for(int i=0;i<50;i++){
        int x=(i*137+17)%W,y=(i*91+11)%(H-30)+5;
        uint16_t sc=(i%4==0)?C_LG:(i%4==1)?C_DG:(i%3==0)?C_DCY:(uint16_t)0x2945;
        tft.drawPixel(x,y,sc);
    }
    tft.drawRoundRect(39,H-17,W-78,10,4,C_DG);
}
void drawWifiScreen(){
    tft.fillScreen(C_BK);
    for(int i=0;i<40;i++){int x=(i*179+23)%W,y=(i*113+7)%H;tft.drawPixel(x,y,C_DG);}
    tft.fillRect(0,0,W,36,C_CARD);tft.drawFastHLine(0,36,W,C_CY);
    tft.fillCircle(18,18,7,C_CY);tft.fillCircle(18,18,3,C_BK);
    tft.setTextSize(1);
    tft.setTextColor(C_WH);tft.setCursor(32,12);tft.print("BRONNY AI");
    tft.setTextColor(C_CY);tft.setCursor(106,12);tft.print("v5.7");
    tft.setTextColor(C_LG);tft.setCursor(W-78,12);tft.print("Patrick 2026");
    int wx=W/2,wy=82;
    tft.fillCircle(wx,wy+22,5,C_CY);tft.drawCircle(wx,wy+22,14,C_CY);
    tft.drawCircle(wx,wy+22,24,C_DCY);tft.drawCircle(wx,wy+22,34,C_DG);
    tft.fillRect(wx-40,wy+22,80,50,C_BK);
    tft.fillRoundRect(20,130,W-40,28,6,C_CARD);tft.drawRoundRect(20,130,W-40,28,6,C_CY);
    tft.setTextColor(C_LG);tft.setCursor(30,136);tft.print("Network:");
    tft.setTextColor(C_WH);tft.setCursor(88,136);tft.print(WIFI_SSID);
    tft.fillRect(0,H-20,W,20,C_CARD);tft.drawFastHLine(0,H-20,W,C_DG);
    tft.setTextColor(C_LG);tft.setTextSize(1);tft.setCursor(6,H-13);tft.print("ESP32-S3");
    tft.setTextColor(C_CY);tft.setCursor(W/2-42,H-13);tft.print("Bronny AI v5.7");
    tft.setTextColor(C_LG);tft.setCursor(W-60,H-13);tft.print("2026");
}
void drawWifiStatus(const char* l1,uint16_t c1,const char* l2="",uint16_t c2=C_CY){
    tft.fillRect(0,164,W,H-20-164,C_BK);
    tft.setTextSize(2);tft.setTextColor(c1);
    int tw=(int)strlen(l1)*12;tft.setCursor(W/2-tw/2,168);tft.print(l1);
    if(strlen(l2)>0){
        tft.setTextSize(1);tft.setTextColor(c2);
        int tw2=(int)strlen(l2)*6;tft.setCursor(W/2-tw2/2,192);tft.print(l2);
    }
}
static uint8_t spinIdx=0; static uint32_t lastSpin=0;
void tickWifiSpinner(){
    uint32_t now=millis();if(now-lastSpin<180)return;lastSpin=now;
    static const char* f[]={"| ","/ ","- ","\\"};
    tft.fillRect(W/2-6,72,12,12,C_BK);
    tft.setTextSize(1);tft.setTextColor(C_CY);tft.setCursor(W/2-3,74);
    tft.print(f[spinIdx++%4]);
}

// ============================================================
// STANDBY MODE
// ============================================================

// Returns true if the transcript contains a wake word (bronny variants).
// Uses indexOf so it works mid-sentence: "ok bronny wake up" still matches.
bool isWakeWord(const String& t) {
    String s = t; s.trim(); s.toLowerCase();
    static const char* ww[] = {
        "bronny","bronnie","brony","brownie","brawny","bonnie",
        "hi bronny","hey bronny","hi bronnie","hey bronnie",
        "hi brony","hey brony","hi brownie","hey brownie",
        "hi brawny","hey brawny","hi bonnie","hey bonnie",
        nullptr
    };
    for (int i = 0; ww[i]; i++) {
        if (s.indexOf(String(ww[i])) >= 0) return true;
    }
    return false;
}

void enterStandby() {
    standbyMode = true;
    setFaceSleep();
    drawFace(true);   // full redraw: black bg + closed eyes + smile
    setStatus("Standby...", C_DCY);
    tftLog(C_DCY, "Standby — say 'Hi Bronny'");
    Serial.println("[Standby] Entering standby mode");
}

void exitStandby() {
    standbyMode     = false;
    lastRailwayMs   = millis();  // reset timer so we don't immediately re-enter standby
    clearZzz();                  // erase any floating ZZZ still on screen
    setFaceIdle();
    drawFace(true);              // full redraw with normal idle face
    jingleWake();
    setStatus("Listening...", C_CY);
    tftLog(C_GR, "Bronny: awake!");
    Serial.println("[Standby] Exiting standby mode");
}

// Called once from setup() after Deepgram connects.
// Sends a boot intro prompt to Railway so Bronny greets the user on first power-on.
// NOTE: If Bronny introduces himself as "AetherAI" instead of "Bronny", the fix is
// on the Railway backend — update the LLM system prompt to define the persona as Bronny.
void doBootIntro() {
    if (!dgConnected) {
        Serial.println("[Intro] Skipped — Deepgram not connected");
        return;
    }
    tftLog(C_CY, "Bronny: hello!");
    setFaceThink();
    // "bootup_intro" is a keyword your Railway LLM should map to a self-introduction.
    // Alternatively change this to any natural prompt your system prompt handles well.
    bool ok = callRailway("bootup_intro");
    if (ok) {
        setStatus("Speaking...", C_GR);
        startTalk();
        playMp3Smooth();
        stopTalk();
        setFaceHappy(1200);
        uint32_t e = millis() + 1200;
        while (millis() < e) {
            animFace();
            if (faceRedraw) { drawFace(false); faceRedraw = false; }
            maintainDeepgram();
            yield();
        }
    }
    setFaceIdle();
    drawFace(true);
    lastRailwayMs = millis();   // count intro as activity — don't sleep immediately
    Serial.println("[Intro] Boot intro complete");
}

// ============================================================
// CONVERSATION PIPELINE
//
// Triggered by speech_final from Deepgram (pendingTranscript flag).
// No manual VAD trigger needed. No Deepgram connect/disconnect per turn.
//
// Flow:
//   pendingTranscript=true
//     -> stop streaming (dgStreaming=false, face=Think)
//     -> Railway LLM+TTS
//     -> playMp3Smooth() (KeepAlive sent inside, mic off)
//     -> mic restart, dgStreaming=true, face=Idle
// ============================================================

void runConversation() {
    if (busy) return;
    busy = true;

    // Stop sending audio to Deepgram while we process
    dgStreaming = false;

    // Clear pending flag and grab transcript
    pendingTranscript    = false;
    dgFinalReceivedAt    = 0;   // cancel self-trigger timer — we're handling it now
    String transcript = dgFinal;
    dgFinal   = "";
    dgPartial = "";

    if (isNoise(transcript)) {
        tftLogf(C_YL, "Filtered: '%s'", transcript.c_str());
        dgStreaming = true;
        dgLastKeepalive = millis();
        if (!standbyMode) setStatus("Listening...", C_CY);
        busy = false;
        return;
    }

    // --- Standby wake word gate ---
    // In standby, ONLY a wake word breaks through. Everything else is discarded silently
    // so background conversation doesn't accidentally trigger Railway.
    if (standbyMode) {
        if (isWakeWord(transcript)) {
            Serial.printf("[Standby] Wake word detected: '%s'\n", transcript.c_str());
            exitStandby();
        } else {
            Serial.printf("[Standby] Ignoring (no wake word): '%s'\n", transcript.c_str());
        }
        dgStreaming = true;
        dgLastKeepalive = millis();
        busy = false;
        return;
    }

    tftLogf(C_MINT, "You: %s", transcript.c_str());

    // Transition face to Think first, then play the chime so they happen together.
    setFaceThink();

    // Feature 3: thinking chime — instant audio acknowledgment before Railway responds.
    {
        i2s.setVolume(0.18f);
        playTone(1047, 55); playSil(20); playTone(1319, 80);
        i2s.setVolume(VOL_MAIN);
    }

    // Railway LLM + TTS
    tftLog(C_YL, "Railway: thinking...");
    bool ok = callRailway(transcript);

    if (!ok) {
        tftLog(C_RD, "Railway failed");
        setFaceIdle();
        dgStreaming = true;
        dgLastKeepalive = millis();
        setStatus("Listening...", C_CY);
        busy = false;
        return;
    }

    // Stamp activity so standby timer resets from this moment.
    lastRailwayMs = millis();

    // Speak — maintainDeepgram() inside playMp3Smooth() sends KeepAlive
    tftLogf(C_GR, "Playing %u bytes MP3", (unsigned)mp3Len);
    setStatus("Speaking...", C_GR);
    startTalk();
    playMp3Smooth();
    stopTalk();

    setFaceHappy(1600);

    // Cooldown: drain mic buffer, then resume streaming
    vadCooldownUntil = millis() + TTS_COOLDOWN_MS;
    if (micOk) {
        uint8_t drain[512]; uint32_t de = millis() + TTS_COOLDOWN_MS;
        while (millis() < de) {
            mic_stream.readBytes(drain, sizeof(drain));
            maintainDeepgram();
            yield();
        }
    }

    // Resume streaming on the same persistent connection
    dgStreaming = true;
    // Bug 6 fix: reset keepalive timer when streaming resumes so the KeepAlive
    // logic (which only fires when NOT streaming) doesn't immediately fire a
    // spurious ping at the start of the new listening window.
    dgLastKeepalive   = millis();
    // Bug B fix: wipe ALL transcript state before releasing busy.
    // During playMp3Smooth(), maintainDeepgram() keeps pumping the WS and any
    // late-arriving is_final / speech_final events from audio Deepgram was still
    // processing can populate dgFinal and set pendingTranscript. Without clearing
    // here, the very next loop() iteration fires runConversation() again with stale
    // content from the previous turn.
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

    audioRestart(); i2s.setVolume(VOL_MAIN);
    if (audioOk){auto sc=sineGen.defaultConfig();sc.copyFrom(ainf_rec);sineGen.begin(sc);}

    micInit();

    mp3Decoder.begin();
    mp3Buf = (uint8_t*)heap_caps_malloc(MP3_MAX_BYTES, MALLOC_CAP_SPIRAM);
    if (!mp3Buf) Serial.println("[Boot] WARNING: mp3Buf alloc failed");

    // Boot animation
    drawBootLogo();
    playBootIntroAnim(W/2, H/2-32);
    drawBootBar(10); jingleBoot(); drawBootBar(55); delay(150); drawBootBar(100); delay(300);

    // WiFi
    drawWifiScreen(); drawWifiStatus("Connecting...", C_YL);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    bool connected = false; uint32_t ws = millis();
    while (millis()-ws < 18000) {
        if (WiFi.status()==WL_CONNECTED){connected=true;break;}
        tickWifiSpinner(); yield();
    }
    if (connected) {
        char ip[32]; snprintf(ip, 32, "%s", WiFi.localIP().toString().c_str());
        drawWifiStatus("Connected!", C_GR, ip, C_CY); jingleConnect(); delay(900);
    } else {
        drawWifiStatus("FAILED", C_RD, "Check config", C_RD); jingleError(); delay(2000);
    }

    // Heartbeat
    sendHeartbeat(); lastHbMs = millis();

    // Face + log zone
    tft.fillScreen(C_BK);
    drawFaceBg(); drawFace(true);
    tft.drawFastHLine(0, LOG_Y-1, W, 0x1082);
    logDrawFooter();
    jingleReady();

    tftLog(C_GR,  "Bronny AI v5.7 ready");
    tftLogf(C_CY, "WiFi: %s", WiFi.localIP().toString().c_str());
    tftLogf(C_LG, "Heap %uK  PSRAM %uK",
            esp_get_free_heap_size()/1024,
            heap_caps_get_free_size(MALLOC_CAP_SPIRAM)/1024);

    // Connect to Deepgram once — persistent for entire device lifetime
    connectDeepgram();

    // Start streaming immediately
    dgStreaming = true;
    setStatus("Listening...", C_CY);

    // Boot intro: Bronny introduces himself on first power-on.
    doBootIntro();
}

// ============================================================
// LOOP
// ============================================================
void loop() {
    uint32_t now = millis();

    // Face animation
    animFace();
    if (faceRedraw) { drawFace(false); faceRedraw = false; }

    // Heartbeat
    if (now - lastHbMs > HEARTBEAT_MS) { lastHbMs = now; sendHeartbeat(); }

    // Deepgram: pump WS, stream audio, keepalive, reconnect-on-drop
    maintainDeepgram();

    // Standby: enter after STANDBY_TIMEOUT_MS of no Railway activity.
    // Deepgram WS and mic streaming stay completely untouched.
    if (!standbyMode && !busy && lastRailwayMs > 0
            && now - lastRailwayMs > STANDBY_TIMEOUT_MS) {
        enterStandby();
    }

    // speech_final fired -> process transcript (fast path)
    // Fallback: if is_final arrived but speech_final is being delayed by background
    // noise keeping Deepgram's endpointing timer from completing, self-trigger after
    // DG_FINAL_TIMEOUT_MS so Railway never waits a minute (or forever) for a noisy room.
    if (!pendingTranscript && dgFinal.length() > 0 && dgFinalReceivedAt > 0
            && now - dgFinalReceivedAt > DG_FINAL_TIMEOUT_MS) {
        pendingTranscript = true;
        dgFinalReceivedAt = 0;
        Serial.println("[DG] speech_final timeout -> self-trigger");
    }
    if (pendingTranscript && !busy && now > vadCooldownUntil) {
        runConversation();
    }

    // Serial commands
    if (Serial.available()) {
        char c = Serial.read();
        if (c=='m'){
            // Show Deepgram connection status
            tftLogf(C_CY,"DG conn=%d stream=%d",dgConnected?1:0,dgStreaming?1:0);
        }
    }

    yield();
}
