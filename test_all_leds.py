#!/usr/bin/env python3
"""
test_all_leds.py — MONA Stone Speakers hardware test
  Phase 1: All LEDs full brightness for 3 seconds
  Phase 2: Sequential sweep back and forth (Knight Rider) until Ctrl+C
GPIO layout:
  LED outputs (MOSFETs): GPIO4, GPIO18, GPIO17, GPIO27, GPIO22, GPIO5, GPIO12, GPIO13
  Warning indicators:    GPIO26 (L1), GPIO19 (L2), GPIO6 (L3)
"""

import pigpio
import time
import sys

# --- Pin definitions ---
LED_PINS   = [4, 18, 17, 27, 22, 5, 12, 13]   # MOSFET outputs, OUT1–OUT8
WARN_PINS  = [26, 19, 6]                        # L1, L2, L3

ALL_PINS   = LED_PINS + WARN_PINS

PWM_FREQ   = 800    # Hz
PWM_MAX    = 255    # full brightness
PWM_DIM    = 30     # dim trail brightness

SWEEP_DELAY = 0.07  # seconds between steps


def all_off(pi):
    for pin in ALL_PINS:
        pi.set_PWM_dutycycle(pin, 0)


def all_on(pi, duty=PWM_MAX):
    for pin in ALL_PINS:
        pi.set_PWM_dutycycle(pin, duty)


def setup(pi):
    for pin in ALL_PINS:
        pi.set_PWM_frequency(pin, PWM_FREQ)
        pi.set_PWM_dutycycle(pin, 0)


def phase1_all_on(pi, duration=3.0):
    """Light everything to maximum."""
    print(f"[Phase 1] All LEDs ON at 100% for {duration}s ...")
    all_on(pi, PWM_MAX)
    time.sleep(duration)
    all_off(pi)
    time.sleep(0.3)


def phase2_sweep(pi):
    """Knight Rider sweep across all 11 channels, back and forth."""
    print("[Phase 2] Sequential sweep — press Ctrl+C to stop\n")

    # Build the full sweep sequence: forward then reverse (no repeat of endpoints)
    sequence = ALL_PINS + ALL_PINS[-2:0:-1]

    try:
        while True:
            for i, active_pin in enumerate(sequence):
                all_off(pi)
                # Dim the neighbours for a tail effect
                for offset, duty in [(-2, PWM_DIM // 2), (-1, PWM_DIM), (0, PWM_MAX), (1, PWM_DIM), (2, PWM_DIM // 2)]:
                    idx = i + offset
                    if 0 <= idx < len(sequence):
                        pi.set_PWM_dutycycle(sequence[idx], duty)
                time.sleep(SWEEP_DELAY)
    except KeyboardInterrupt:
        pass


def main():
    pi = pigpio.pi()
    if not pi.connected:
        print("ERROR: Cannot connect to pigpiod. Is it running?")
        print("  sudo systemctl start pigpiod")
        sys.exit(1)

    print("=== MONA Stone Speakers — LED Hardware Test ===")
    print(f"  MOSFET outputs : {LED_PINS}")
    print(f"  Warning LEDs   : {WARN_PINS}")
    print()

    try:
        setup(pi)
        phase1_all_on(pi, duration=3.0)
        phase2_sweep(pi)
    finally:
        print("\nCleaning up — all LEDs off.")
        all_off(pi)
        pi.stop()


if __name__ == "__main__":
    main()
