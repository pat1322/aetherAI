/*
 * FluxGarage_RoboEyes_TFT.h — Adapted for Bronny AI
 *
 * Based on RoboEyesTFT_eSPI by Youssef Tech, inspired by
 * FluxGarage RoboEyes by Dennis Hoelscher.
 *
 * Bronny additions:
 *   - Animated mouth: wider oval, only visible when talking, follows eyes
 *   - Sleepy mode for standby (half-closed droopy eyes, flat eyelid, floating ZZZ)
 *   - PSRAM sprite, safe row-by-row push with yield
 *   - Smoother lerp-based animation (LERPF_POS / LERPF_SIZE)
 *   - centerLocked flag: keeps eyes centred in non-idle states
 *   - For best FPS set SPI_FREQUENCY to 27000000 in User_Setup.h
 */

#ifndef _FLUXGARAGE_ROBOEYES_TFT_H
#define _FLUXGARAGE_ROBOEYES_TFT_H

#include <TFT_eSPI.h>

#define MOOD_DEFAULT  0
#define MOOD_TIRED    1
#define MOOD_ANGRY    2
#define MOOD_HAPPY    3

#define ROBO_ON  1
#define ROBO_OFF 0

#define POS_DEFAULT 0
#define POS_N  1
#define POS_NE 2
#define POS_E  3
#define POS_SE 4
#define POS_S  5
#define POS_SW 6
#define POS_W  7
#define POS_NW 8

class FluxGarage_RoboEyes {
public:
    TFT_eSPI   *tft;
    TFT_eSprite *sprite;

    int screenWidth, screenHeight, pushYOffset;
    uint16_t bgColor, mainColor;
    int frameInterval;
    unsigned long fpsTimer;

    bool tired, angry, happy, curious, cyclops;
    bool eyeL_open, eyeR_open;

    int eyeLwidthDefault, eyeLheightDefault;
    int eyeLwidthCurrent, eyeLheightCurrent, eyeLwidthNext, eyeLheightNext, eyeLheightOffset;
    uint8_t eyeLborderRadiusDefault, eyeLborderRadiusCurrent, eyeLborderRadiusNext;
    int eyeRwidthDefault, eyeRheightDefault;
    int eyeRwidthCurrent, eyeRheightCurrent, eyeRwidthNext, eyeRheightNext, eyeRheightOffset;
    uint8_t eyeRborderRadiusDefault, eyeRborderRadiusCurrent, eyeRborderRadiusNext;

    int eyeLxDefault, eyeLyDefault, eyeLx, eyeLy, eyeLxNext, eyeLyNext;
    int eyeRxDefault, eyeRyDefault, eyeRx, eyeRy, eyeRxNext, eyeRyNext;

    uint8_t eyelidsTiredHeight, eyelidsTiredHeightNext;
    uint8_t eyelidsAngryHeight, eyelidsAngryHeightNext;
    uint8_t eyelidsHappyBottomOffset, eyelidsHappyBottomOffsetNext;
    int spaceBetweenDefault, spaceBetweenCurrent, spaceBetweenNext;

    bool hFlicker, hFlickerAlternate; uint8_t hFlickerAmplitude;
    bool vFlicker, vFlickerAlternate; uint8_t vFlickerAmplitude;
    bool autoblinker; int blinkInterval, blinkIntervalVariation; unsigned long blinktimer;
    bool idleMode; int idleInterval, idleIntervalVariation; unsigned long idleAnimationTimer;
    bool confused; unsigned long confusedAnimationTimer; int confusedAnimationDuration; bool confusedToggle;
    bool laugh; unsigned long laughAnimationTimer; int laughAnimationDuration; bool laughToggle;
    bool blinkingActive; unsigned long blinkCloseDurationTimer; int blinkCloseDuration;

    // Mouth
    bool  talking;
    float talkPhase;
    float mouthOpenCurrent;
    int   mouthRadiusW;
    int   mouthRadiusH;
    int   mouthYOffset;

    // Sleepy
    bool  sleepy;
    float sleepyPhase;

    // ── NEW: center-lock flag ─────────────────────────────────
    // When true, non-idle states are pinned to the default centre
    // position so only the idle wander makes the eyes move around.
    bool centerLocked;

    // ── NEW: ZZZ bubble animation (sleep mode) ────────────────
    struct ZzzBubble { float x, y, vy, life; uint8_t sz; };
    ZzzBubble _zzz[3];
    uint32_t  _zzzSpawn;   // millis() timestamp for next spawn
    uint8_t   _zzzNext;    // which bubble index (0-2) to spawn next

    // ─────────────────────────────────────────────────────────────
    FluxGarage_RoboEyes(TFT_eSPI &display, int w, int h, int yOff = 0)
      : tft(&display), sprite(nullptr), screenWidth(w), screenHeight(h), pushYOffset(yOff),
        bgColor(TFT_BLACK), mainColor(TFT_WHITE), frameInterval(20), fpsTimer(0),
        tired(false), angry(false), happy(false), curious(false), cyclops(false),
        eyeL_open(false), eyeR_open(false),
        eyeLwidthDefault(90), eyeLheightDefault(60),
        eyeLwidthCurrent(90), eyeLheightCurrent(1), eyeLwidthNext(90), eyeLheightNext(60), eyeLheightOffset(0),
        eyeLborderRadiusDefault(18), eyeLborderRadiusCurrent(18), eyeLborderRadiusNext(18),
        eyeRwidthDefault(90), eyeRheightDefault(60),
        eyeRwidthCurrent(90), eyeRheightCurrent(1), eyeRwidthNext(90), eyeRheightNext(60), eyeRheightOffset(0),
        eyeRborderRadiusDefault(18), eyeRborderRadiusCurrent(18), eyeRborderRadiusNext(18),
        eyelidsTiredHeight(0), eyelidsTiredHeightNext(0),
        eyelidsAngryHeight(0), eyelidsAngryHeightNext(0),
        eyelidsHappyBottomOffset(0), eyelidsHappyBottomOffsetNext(0),
        spaceBetweenDefault(24), spaceBetweenCurrent(24), spaceBetweenNext(24),
        hFlicker(false), hFlickerAlternate(false), hFlickerAmplitude(2),
        vFlicker(false), vFlickerAlternate(false), vFlickerAmplitude(10),
        autoblinker(false), blinkInterval(3), blinkIntervalVariation(2), blinktimer(0),
        idleMode(false), idleInterval(3), idleIntervalVariation(2), idleAnimationTimer(0),
        confused(false), confusedAnimationTimer(0), confusedAnimationDuration(500), confusedToggle(true),
        laugh(false), laughAnimationTimer(0), laughAnimationDuration(500), laughToggle(true),
        blinkingActive(false), blinkCloseDurationTimer(0), blinkCloseDuration(200),
        talking(false), talkPhase(0.f), mouthOpenCurrent(0.f),
        mouthRadiusW(22), mouthRadiusH(6), mouthYOffset(48),
        sleepy(false), sleepyPhase(0.f),
        centerLocked(false), _zzzSpawn(0), _zzzNext(0)
    {
        memset(_zzz, 0, sizeof(_zzz));
        recalcDefaults();
    }

    void begin(byte fps = 50) {
        sprite = new TFT_eSprite(tft);
        sprite->setAttribute(PSRAM_ENABLE, true);
        sprite->setColorDepth(16);
        sprite->createSprite(screenWidth, screenHeight);
        sprite->fillSprite(bgColor);
        eyeLheightCurrent = 1; eyeRheightCurrent = 1;
        frameInterval = 1000 / fps;
        memset(_zzz, 0, sizeof(_zzz));
        _zzzSpawn = 0; _zzzNext = 0;
    }

    void update() {
        if (millis() - fpsTimer >= (unsigned long)frameInterval) {
            drawEyes();
            safePushSprite();
            fpsTimer = millis();
        }
    }

    // ── Setters ───────────────────────────────────────────────
    void setFramerate(byte fps) { frameInterval = 1000 / fps; }
    void setPushYOffset(int y)  { pushYOffset = y; }
    void setColors(uint16_t m, uint16_t bg) { mainColor = m; bgColor = bg; }
    void setWidth(int l, int r)       { eyeLwidthNext=l; eyeRwidthNext=r; eyeLwidthDefault=l; eyeRwidthDefault=r; }
    void setHeight(int l, int r)      { eyeLheightNext=l; eyeRheightNext=r; eyeLheightDefault=l; eyeRheightDefault=r; }
    void setBorderradius(uint8_t l, uint8_t r) { eyeLborderRadiusNext=l; eyeRborderRadiusNext=r; eyeLborderRadiusDefault=l; eyeRborderRadiusDefault=r; }
    void setSpacebetween(int s) { spaceBetweenNext=s; spaceBetweenDefault=s; }

    void setMood(uint8_t mood) { tired=(mood==MOOD_TIRED); angry=(mood==MOOD_ANGRY); happy=(mood==MOOD_HAPPY); }

    void setPosition(uint8_t pos) {
        int cx = getConstraintX(), cy = getConstraintY();
        switch (pos) {
            case POS_N:  eyeLxNext=cx/2;  eyeLyNext=0;    break;
            case POS_NE: eyeLxNext=cx;    eyeLyNext=0;    break;
            case POS_E:  eyeLxNext=cx;    eyeLyNext=cy/2; break;
            case POS_SE: eyeLxNext=cx;    eyeLyNext=cy;   break;
            case POS_S:  eyeLxNext=cx/2;  eyeLyNext=cy;   break;
            case POS_SW: eyeLxNext=0;     eyeLyNext=cy;   break;
            case POS_W:  eyeLxNext=0;     eyeLyNext=cy/2; break;
            case POS_NW: eyeLxNext=0;     eyeLyNext=0;    break;
            default:     eyeLxNext=eyeLxDefault; eyeLyNext=eyeLyDefault; break;
        }
    }

    void setAutoblinker(bool on, int iv=3, int var=2) {
        autoblinker=on; blinkInterval=iv; blinkIntervalVariation=var;
        blinktimer=millis()+iv*1000UL+random(var)*1000UL; blinkingActive=false;
    }
    void setIdleMode(bool on, int iv=3, int var=2) { idleMode=on; idleInterval=iv; idleIntervalVariation=var; }
    void setCuriosity(bool on) { curious = on; }
    void setCyclops(bool on)   { cyclops = on; }

    // ── NEW: pin/release eyes to the default centre position ──
    void setCenterLocked(bool on) {
        centerLocked = on;
        if (on) { eyeLxNext = eyeLxDefault; eyeLyNext = eyeLyDefault; }
    }

    void close() {
        eyeLheightNext=1; eyeRheightNext=1;
        eyeL_open=eyeR_open=false;
        eyeLborderRadiusNext=eyeRborderRadiusNext=0;
    }
    void open()  {
        eyeL_open=eyeR_open=true;
        eyeLheightNext=eyeLheightDefault; eyeRheightNext=eyeRheightDefault;
        eyeLborderRadiusNext=eyeLborderRadiusDefault; eyeRborderRadiusNext=eyeRborderRadiusDefault;
    }

    void anim_confused() { confused = true; }
    void anim_laugh()    { laugh = true; }
    void setHFlicker(bool on, uint8_t a=2)  { hFlicker=on; hFlickerAmplitude=a; }
    void setVFlicker(bool on, uint8_t a=10) { vFlicker=on; vFlickerAmplitude=a; }

    void setTalking(bool on) { talking = on; if (!on) talkPhase = 0.f; }
    void setMouthSize(int rw, int rh, int yOff) { mouthRadiusW=rw; mouthRadiusH=rh; mouthYOffset=yOff; }

    // ── Sleepy mode: heavy-lidded droopy eyes for standby ─────
    void setSleepy(bool on) {
        sleepy = on;
        if (on) {
            sleepyPhase = 0.f;
            tired       = false;           // sleepy uses its own flat eyelid
            idleMode    = false;
            centerLocked = true;           // never wander while asleep
            // target 45 % of default height — just barely open
            eyeLheightNext = eyeLheightDefault * 45 / 100;
            eyeRheightNext = eyeRheightDefault * 45 / 100;
            eyeL_open = eyeR_open = true;
            // Reset ZZZ sequence
            memset(_zzz, 0, sizeof(_zzz));
            _zzzNext  = 0;
            _zzzSpawn = millis() + 1400;   // first Z after 1.4 s
        } else {
            tired       = false;
            centerLocked = false;
            eyeLheightNext = eyeLheightDefault;
            eyeRheightNext = eyeRheightDefault;
            // Fade out any lingering Zs
            memset(_zzz, 0, sizeof(_zzz));
        }
    }

private:
    // ── Lerp speed constants (tune here for feel) ─────────────
    // LERPF_POS  — how quickly eyes glide to a new x/y position
    // LERPF_SIZE — how quickly width/height/radius changes
    // LERPF_LID  — how quickly eyelid overlays animate
    static constexpr float LERPF_POS  = 0.13f;
    static constexpr float LERPF_SIZE = 0.35f;
    static constexpr float LERPF_LID  = 0.35f;

    // Smooth integer lerp — never gets stuck at 1-pixel away
    static int _slerpI(int cur, int tgt, float f) {
        if (cur == tgt) return cur;
        int diff = tgt - cur;
        int step = (int)roundf((float)diff * f);
        if (step == 0) step = (diff > 0) ? 1 : -1;
        cur += step;
        if (diff > 0 && cur > tgt) cur = tgt;
        if (diff < 0 && cur < tgt) cur = tgt;
        return cur;
    }

    // Dim an RGB565 colour by factor 0.0-1.0
    uint16_t _dimColor(uint16_t c, float f) {
        if (f <= 0.0f) return bgColor;
        if (f >= 1.0f) return c;
        uint8_t r = (uint8_t)(((c >> 11) & 0x1F) * f);
        uint8_t g = (uint8_t)(((c >> 5 ) & 0x3F) * f);
        uint8_t b = (uint8_t)((c        & 0x1F) * f);
        return (uint16_t)(r << 11) | (uint16_t)(g << 5) | b;
    }

    void recalcDefaults() {
        eyeLxDefault = (screenWidth - (eyeLwidthDefault + spaceBetweenDefault + eyeRwidthDefault)) / 2;
        eyeLyDefault = (screenHeight - eyeLheightDefault) / 2;
        eyeLx=eyeLxDefault; eyeLy=eyeLyDefault; eyeLxNext=eyeLx; eyeLyNext=eyeLy;
        eyeRxDefault = eyeLxDefault + eyeLwidthDefault + spaceBetweenDefault;
        eyeRyDefault = eyeLyDefault;
        eyeRx=eyeRxDefault; eyeRy=eyeRyDefault; eyeRxNext=eyeRx; eyeRyNext=eyeRy;
    }

    int getConstraintX() { return screenWidth - eyeLwidthCurrent - spaceBetweenCurrent - eyeRwidthCurrent; }
    int getConstraintY() { return screenHeight - eyeLheightDefault; }

    void safePushSprite() {
        if (!sprite) return;
        uint16_t* buf = (uint16_t*)sprite->getPointer();
        if (!buf) return;
        tft->startWrite();
        tft->setAddrWindow(0, pushYOffset, screenWidth, screenHeight);
        for (int y = 0; y < screenHeight; y++) {
            tft->pushPixels(buf + y * screenWidth, screenWidth);
            if ((y & 31) == 31) yield();
        }
        tft->endWrite();
    }

    // ── ZZZ: advance physics for all live bubbles ─────────────
    void _updateZzz() {
        // Move existing bubbles
        for (int i = 0; i < 3; i++) {
            if (_zzz[i].life > 0.01f) {
                _zzz[i].y    -= _zzz[i].vy;   // float upward
                _zzz[i].x    += 0.30f;         // gentle rightward drift
                _zzz[i].life -= 0.009f;         // fade over ~111 frames ≈ 2.2 s
                if (_zzz[i].life < 0.0f) _zzz[i].life = 0.0f;
            }
        }

        // Spawn a new Z bubble on schedule
        if (millis() >= _zzzSpawn) {
            for (int i = 0; i < 3; i++) {
                if (_zzz[i].life <= 0.01f) {
                    // Always spawn from the same origin (top-right of right eye).
                    // Previous Zs have drifted far away by the time we spawn the next,
                    // so they appear staggered naturally.
                    float sx = cyclops
                        ? (float)(eyeLx + eyeLwidthCurrent) + 6.f
                        : (float)(eyeRx + eyeRwidthCurrent) - 10.f;
                    float sy = cyclops
                        ? (float)(eyeLy + eyeLheightCurrent / 2) - 4.f
                        : (float)(eyeRy + eyeRheightCurrent  / 3) - 4.f;

                    _zzz[i].x    = sx;
                    _zzz[i].y    = sy;
                    // Progressive speed: first Z slowest, each faster so they spread out
                    _zzz[i].vy   = 0.80f + (float)_zzzNext * 0.18f;
                    _zzz[i].life = 1.0f;
                    // Progressive size: 1 → 2 → 3 (small, medium, big)
                    _zzz[i].sz   = (uint8_t)(_zzzNext + 1);

                    _zzzNext = (_zzzNext + 1) % 3;
                    // 1100 ms between each Z; 3000 ms pause after third before next cycle
                    _zzzSpawn = millis() + (_zzzNext == 0 ? 3000UL : 1100UL);
                    break;
                }
            }
        }
    }

    // ── ZZZ: render live bubbles onto the sprite ──────────────
    void _drawZzz() {
        for (int i = 0; i < 3; i++) {
            if (_zzz[i].life <= 0.05f) continue;
            uint16_t col = _dimColor(mainColor, _zzz[i].life * 0.92f);
            int tx = (int)_zzz[i].x;
            int ty = (int)_zzz[i].y;
            int ts = (int)_zzz[i].sz;
            // Each character cell is ts*6 wide × ts*8 tall — clamp to sprite
            if (tx < 0 || ty < 0 ||
                tx + ts * 6 > screenWidth ||
                ty + ts * 8 > screenHeight) continue;
            // drawChar bypasses the cursor/print machinery — always works with
            // the built-in font regardless of any prior font state.
            sprite->drawChar(tx, ty, 'Z', col, bgColor, ts);
        }
    }

    // ── Main draw routine (called every frame) ────────────────
    void drawEyes() {

        // ── 1. Centre-lock: override idle/position targets ────
        if (centerLocked) {
            eyeLxNext = eyeLxDefault;
            eyeLyNext = eyeLyDefault;
        }

        // ── 2. Sleepy oscillation (improved: slower breathing) ─
        if (sleepy) {
            sleepyPhase += 0.010f;               // ~12.6 s per full cycle (very peaceful)
            if (sleepyPhase > 6.2832f) sleepyPhase -= 6.2832f;
            int base  = eyeLheightDefault * 45 / 100;
            int drift = (int)(sinf(sleepyPhase) * 10.f);
            eyeLheightNext = max(8, base + drift);
            eyeRheightNext = eyeLheightNext;
        }

        // ── 3. Curiosity height offsets ───────────────────────
        if (curious) {
            eyeLheightOffset = (eyeLxNext <= 10) ? 8 :
                               (eyeLxNext >= (getConstraintX()-10) && cyclops) ? 8 : 0;
            eyeRheightOffset = (eyeRxNext >= screenWidth-eyeRwidthCurrent-10) ? 8 : 0;
        } else {
            eyeLheightOffset = eyeRheightOffset = 0;
        }

        // ── 4. Smooth height transitions ──────────────────────
        eyeLheightCurrent = _slerpI(eyeLheightCurrent, eyeLheightNext + eyeLheightOffset, LERPF_SIZE);
        eyeRheightCurrent = _slerpI(eyeRheightCurrent, eyeRheightNext + eyeRheightOffset, LERPF_SIZE);

        // Re-open after a blink reaches closed
        if (eyeL_open && eyeLheightCurrent <= 2 + eyeLheightOffset) eyeLheightNext = eyeLheightDefault;
        if (eyeR_open && eyeRheightCurrent <= 2 + eyeRheightOffset) eyeRheightNext = eyeRheightDefault;

        // ── 5. Width / spacing transitions ────────────────────
        eyeLwidthCurrent    = _slerpI(eyeLwidthCurrent,    eyeLwidthNext,    LERPF_SIZE);
        eyeRwidthCurrent    = _slerpI(eyeRwidthCurrent,    eyeRwidthNext,    LERPF_SIZE);
        spaceBetweenCurrent = _slerpI(spaceBetweenCurrent, spaceBetweenNext, LERPF_SIZE);

        // ── 6. Position transitions (slow, fluid glide) ───────
        // The Y target bakes in vertical centring so the eye stays
        // centred even while blinking/shrinking.
        int eyeLyTarget = eyeLyNext + (eyeLheightDefault - eyeLheightCurrent) / 2
                                    - eyeLheightOffset / 2;
        eyeRxNext = eyeLxNext + eyeLwidthCurrent + spaceBetweenCurrent;
        eyeRyNext = eyeLyNext;
        int eyeRyTarget = eyeRyNext + (eyeRheightDefault - eyeRheightCurrent) / 2
                                    - eyeRheightOffset / 2;

        eyeLx = _slerpI(eyeLx, eyeLxNext,   LERPF_POS);
        eyeLy = _slerpI(eyeLy, eyeLyTarget,  LERPF_POS);
        eyeRx = _slerpI(eyeRx, eyeRxNext,   LERPF_POS);
        eyeRy = _slerpI(eyeRy, eyeRyTarget,  LERPF_POS);

        // ── 7. Border radius transitions ──────────────────────
        eyeLborderRadiusCurrent = (uint8_t)_slerpI(eyeLborderRadiusCurrent, eyeLborderRadiusNext, LERPF_SIZE);
        eyeRborderRadiusCurrent = (uint8_t)_slerpI(eyeRborderRadiusCurrent, eyeRborderRadiusNext, LERPF_SIZE);

        // ── 8. Auto-blink (suppressed in sleepy mode) ─────────
        if (autoblinker && !blinkingActive && !sleepy && millis() >= blinktimer) {
            close(); blinkingActive = true;
            blinkCloseDurationTimer = millis() + blinkCloseDuration;
            blinktimer = millis() + blinkInterval*1000UL + random(blinkIntervalVariation)*1000UL;
        }
        if (blinkingActive && millis() >= blinkCloseDurationTimer) { open(); blinkingActive = false; }

        // ── 9. Laugh animation ────────────────────────────────
        if (laugh) {
            if (laughToggle) {
                setVFlicker(true, 5); laughAnimationTimer = millis(); laughToggle = false;
            } else if (millis() >= laughAnimationTimer + laughAnimationDuration) {
                setVFlicker(false, 0); laughToggle = true; laugh = false;
            }
        }

        // ── 10. Confused animation ────────────────────────────
        if (confused) {
            if (confusedToggle) {
                setHFlicker(true, 20); confusedAnimationTimer = millis(); confusedToggle = false;
            } else if (millis() >= confusedAnimationTimer + confusedAnimationDuration) {
                setHFlicker(false, 0); confusedToggle = true; confused = false;
            }
        }

        // ── 11. Idle wander (centre-lock blocks this) ─────────
        if (idleMode && !centerLocked && millis() >= idleAnimationTimer) {
            eyeLxNext = random(getConstraintX());
            eyeLyNext = random(getConstraintY());
            idleAnimationTimer = millis() + idleInterval*1000UL + random(idleIntervalVariation)*1000UL;
        }

        // ── 12. Flicker effects ───────────────────────────────
        if (hFlicker) { int d = hFlickerAlternate ? hFlickerAmplitude : -hFlickerAmplitude; eyeLx+=d; eyeRx+=d; hFlickerAlternate=!hFlickerAlternate; }
        if (vFlicker) { int d = vFlickerAlternate ? vFlickerAmplitude : -vFlickerAmplitude; eyeLy+=d; eyeRy+=d; vFlickerAlternate=!vFlickerAlternate; }
        if (cyclops)  { eyeRwidthCurrent=0; eyeRheightCurrent=0; spaceBetweenCurrent=0; }

        // ── 13. Clear sprite and draw eyes ────────────────────
        sprite->fillSprite(bgColor);
        sprite->fillRoundRect(eyeLx, eyeLy, eyeLwidthCurrent, eyeLheightCurrent, eyeLborderRadiusCurrent, mainColor);
        if (!cyclops) sprite->fillRoundRect(eyeRx, eyeRy, eyeRwidthCurrent, eyeRheightCurrent, eyeRborderRadiusCurrent, mainColor);

        // ── 14. Eyelid overlays ───────────────────────────────

        // — TIRED eyelid (diagonal triangle from top-left; NOT drawn in sleepy) —
        if (tired && !sleepy) eyelidsTiredHeightNext = eyeLheightCurrent * 55 / 100;
        else if (!sleepy)     eyelidsTiredHeightNext = 0;
        // smooth the tired eyelid even when transitioning in/out of sleepy
        eyelidsTiredHeight = (uint8_t)_slerpI(eyelidsTiredHeight, eyelidsTiredHeightNext, LERPF_LID);
        if (!sleepy && eyelidsTiredHeight > 0) {
            sprite->fillTriangle(eyeLx, eyeLy-1, eyeLx+eyeLwidthCurrent, eyeLy-1,
                                 eyeLx, eyeLy+eyelidsTiredHeight-1, bgColor);
            if (!cyclops) sprite->fillTriangle(eyeRx, eyeRy-1, eyeRx+eyeRwidthCurrent, eyeRy-1,
                                               eyeRx+eyeRwidthCurrent, eyeRy+eyelidsTiredHeight-1, bgColor);
        }

        // — SLEEPY flat eyelid (horizontal bar dropping from top — more natural) —
        if (sleepy) {
            eyelidsTiredHeight = 0;   // keep in sync so transitions look clean
            int lidH = eyeLheightCurrent * 58 / 100;
            if (lidH > 0) {
                sprite->fillRect(eyeLx - 1, eyeLy - 1, eyeLwidthCurrent + 2, lidH + 1, bgColor);
                if (!cyclops) sprite->fillRect(eyeRx - 1, eyeRy - 1, eyeRwidthCurrent + 2, lidH + 1, bgColor);
            }
        }

        // — ANGRY eyelid —
        if (angry) eyelidsAngryHeightNext = eyeLheightCurrent / 2; else eyelidsAngryHeightNext = 0;
        eyelidsAngryHeight = (uint8_t)_slerpI(eyelidsAngryHeight, eyelidsAngryHeightNext, LERPF_LID);
        sprite->fillTriangle(eyeLx, eyeLy-1, eyeLx+eyeLwidthCurrent, eyeLy-1,
                             eyeLx+eyeLwidthCurrent, eyeLy+eyelidsAngryHeight-1, bgColor);
        if (!cyclops) sprite->fillTriangle(eyeRx, eyeRy-1, eyeRx+eyeRwidthCurrent, eyeRy-1,
                                           eyeRx, eyeRy+eyelidsAngryHeight-1, bgColor);

        // — HAPPY bottom offset (smile squint) —
        if (happy) eyelidsHappyBottomOffsetNext = eyeLheightCurrent / 2; else eyelidsHappyBottomOffsetNext = 0;
        eyelidsHappyBottomOffset = (uint8_t)_slerpI(eyelidsHappyBottomOffset, eyelidsHappyBottomOffsetNext, LERPF_LID);
        sprite->fillRoundRect(eyeLx-1, (eyeLy+eyeLheightCurrent)-eyelidsHappyBottomOffset+1,
                              eyeLwidthCurrent+2, eyeLheightDefault, eyeLborderRadiusCurrent, bgColor);
        if (!cyclops) sprite->fillRoundRect(eyeRx-1, (eyeRy+eyeRheightCurrent)-eyelidsHappyBottomOffset+1,
                                            eyeRwidthCurrent+2, eyeRheightDefault, eyeRborderRadiusCurrent, bgColor);

        // ── 15. Floating ZZZ (only in sleepy mode) ───────────
        if (sleepy) {
            _updateZzz();
            _drawZzz();
        }

        // ── 16. Mouth (only when talking or fading out) ───────
        if (talking || mouthOpenCurrent > 0.02f) drawMouth();
    }

    void drawMouth() {
        // Centre: horizontally between both eyes, below them
        int eyesCenterX = (eyeLx + eyeLwidthCurrent/2 + eyeRx + eyeRwidthCurrent/2) / 2;
        int eyesCenterY = (eyeLy + eyeRy) / 2 + eyeLheightDefault / 2;
        int mCX = eyesCenterX;
        int mCY = eyesCenterY + mouthYOffset;
        if (mCY + mouthRadiusH * 5 >= screenHeight)
            mCY = screenHeight - mouthRadiusH * 5 - 2;

        // Jaw animation
        float targetOpen = 0.f;
        if (talking) {
            talkPhase += 0.42f;
            if (talkPhase > 6.2832f) talkPhase -= 6.2832f;
            float jaw = sinf(talkPhase)        * 0.50f
                      + sinf(talkPhase * 1.7f) * 0.28f
                      + sinf(talkPhase * 0.4f) * 0.14f;
            targetOpen = constrain(0.25f + jaw, 0.05f, 1.0f);
        }
        mouthOpenCurrent += (targetOpen - mouthOpenCurrent) * 0.30f;
        if (!talking && mouthOpenCurrent < 0.03f) { mouthOpenCurrent = 0.f; return; }

        // Oval shape: width fixed, height grows with jaw open amount.
        // Corner radius = half the height so it stays a true oval at any size.
        int mW   = mouthRadiusW * 2;
        int mH   = mouthRadiusH * 2
                 + (int)(mouthOpenCurrent * (float)(mouthRadiusH * 8));
        int corn = mH / 2;   // full oval corners
        int mX   = mCX - mouthRadiusW;
        int mY   = mCY - mH / 2;

        // Solid white filled oval — no inner shadow
        sprite->fillRoundRect(mX, mY, mW, mH, corn, mainColor);
    }
};

#endif
