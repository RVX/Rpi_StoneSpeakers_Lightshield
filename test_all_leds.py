#!/usr/bin/env python3
"""
test_all_leds.py — MONA Stone Speakers hardware test
  Phase 1: All LEDs full brightness for 3 seconds
  Phase 2: Sequential sweep back and forth (Knight Rider) — 3 full passes
  Phase 3: Breath effect — smooth sine ramp 0→max→0 on all channels, loops until Ctrl+C
GPIO layout:
  LED outputs (MOSFETs): GPIO4, GPIO18, GPIO17, GPIO27, GPIO22, GPIO5, GPIO12, GPIO13
  Warning indicators:    GPIO26 (L1), GPIO19 (L2), GPIO6 (L3)
"""

import pigpio
import time
import sys
import math

# --- Pin definitions ---
LED_PINS   = [4, 18, 17, 27, 22, 5, 12, 13]   # MOSFET outputs, OUT1–OUT8
WARN_PINS  = [26, 19, 6]                        # L1, L2, L3

ALL_PINS   = LED_PINS + WARN_PINS

PWM_FREQ   = 800    # Hz
PWM_MAX    = 255    # full brightness
PWM_DIM    = 30     # dim trail brightness

SWEEP_DELAY    = 0.12   # seconds between sweep steps (slower = more visible)
SWEEP_PASSES   = 3      # number of full back-and-forth passes before breath
BREATH_STEPS   = 200    # resolution of each breath cycle (higher = smoother)
BREATH_PERIOD  = 2.5    # seconds for one full inhale + exhale
BREATH_FLOOR   = 4      # minimum brightness during exhale (0 = fully off)
BLACKOUT_SEC   = 1.5    # dark pause between phases


def all_off(pi):
    for pin in ALL_PINS:
        pi.set_PWM_dutycycle(pin, 0)


def fade_down(pi):
    """Quadratic fade from current brightness (assumed full) to zero over ~0.4s."""
    steps = 40
    for s in range(steps, -1, -1):
        duty = int((s / steps) ** 2 * PWM_MAX)
        for pin in ALL_PINS:
            pi.set_PWM_dutycycle(pin, duty)
        time.sleep(0.01)
    all_off(pi)


def blackout(pi, fade=False, label=None):
    """Pause in darkness between phases. Set fade=True only when coming from full brightness."""
    if fade:
        fade_down(pi)   # smooth ramp down — only call when LEDs are actually on
    else:
        all_off(pi)     # pins already off; just ensure clean state
    if label:
        print(f"  --- {label} ---")
    time.sleep(BLACKOUT_SEC)


def all_on(pi, duty=PWM_MAX):
    for pin in ALL_PINS:
        pi.set_PWM_dutycycle(pin, duty)


def setup(pi):
    for pin in ALL_PINS:
        pi.set_pull_up_down(pin, pigpio.PUD_DOWN)   # hold gate LOW when no script driving it
        pi.set_PWM_frequency(pin, PWM_FREQ)
        pi.set_PWM_dutycycle(pin, 0)


def phase1_all_on(pi, duration=4.0):
    """Light everything to maximum."""
    print(f"[Phase 1] All LEDs ON at 100% for {duration}s ...")
    all_on(pi, PWM_MAX)
    time.sleep(duration)


def phase2_sweep(pi, passes=SWEEP_PASSES):
    """Knight Rider sweep across all 11 channels, back and forth for N passes."""
    print(f"[Phase 2] Sequential sweep — {passes} pass(es) ...")

    # Build the full sweep sequence: forward then reverse (no repeat of endpoints)
    sequence = ALL_PINS + ALL_PINS[-2:0:-1]

    for p in range(passes):
        for i, _ in enumerate(sequence):
            all_off(pi)
            # One bright head + one dim neighbour on each side only
            for offset, duty in [(-1, PWM_DIM), (0, PWM_MAX), (1, PWM_DIM)]:
                idx = i + offset
                if 0 <= idx < len(sequence):
                    pi.set_PWM_dutycycle(sequence[idx], duty)
            time.sleep(SWEEP_DELAY)

    all_off(pi)


def phase3_breath(pi, cycles=5):
    """Smooth sine-curve breath on all channels simultaneously for N cycles."""
    print(f"[Phase 3] Breath effect — {cycles} cycles ...")

    step_delay = BREATH_PERIOD / BREATH_STEPS
    range_span  = PWM_MAX - BREATH_FLOOR

    for _ in range(cycles):
        for step in range(BREATH_STEPS):
            angle = (step / BREATH_STEPS) * math.pi
            duty  = BREATH_FLOOR + int(math.sin(angle) * range_span)
            for pin in ALL_PINS:
                pi.set_PWM_dutycycle(pin, duty)
            time.sleep(step_delay)
        time.sleep(0.4)   # brief pause at the bottom between breaths


def main():
    pi = pigpio.pi()
    if not pi.connected:
        print("ERROR: Cannot connect to pigpiod. Is it running?")
        print("  sudo systemctl start pigpiod")
        sys.exit(1)

    print("=== MONA Stone Speakers — LED Hardware Test ===")
    print(f"  MOSFET outputs : {LED_PINS}")
    print(f"  Warning LEDs   : {WARN_PINS}")
    print("  Looping phases 1 → 2 → 3 → 1 ...  Ctrl+C to stop\n")

    try:
        setup(pi)
        loop = 0
        while True:
            loop += 1
            print(f"\n=== Loop {loop} ===")
            phase1_all_on(pi)
            blackout(pi, fade=True,  label="-- blackout --")
            phase2_sweep(pi)
            blackout(pi, fade=False, label="-- blackout --")
            phase3_breath(pi)
            blackout(pi, fade=False, label="-- blackout --")
    except KeyboardInterrupt:
        pass
    finally:
        print("\nCleaning up — all LEDs off.")
        all_off(pi)
        pi.stop()


if __name__ == "__main__":
    main()
