# UNDERWATER RESONANCES FOR J.C.STUDIO
# USING THE CUSTOM SHIELD FROM STONE SPEAKERS
# VMG 2025

import pigpio
import time
import signal
from datetime import datetime

# GPIO pins — OUT1 to OUT8 in order
LED_PINS = [4, 18, 17, 27, 22, 5, 12, 13]

# Sequences: brightness (0–100%), frequency (Hz), duration (seconds) per step
BRIGHTNESS_SEQ = [ 25,  15,  15,   8,  10,   9,   8,   6,  25,   5,  17,   8,  11,  14,  19,   9,  16,  13,   8,  18,   8,   9,  20,   5,   6,   7,  12,   9,   8,   3]
FREQUENCY_SEQ  = [800, 800, 800, 750, 700, 600, 500, 600, 180, 700, 600, 400, 450, 650, 550, 750, 800, 600, 500, 300, 400, 250, 150, 200, 300, 400, 500, 600, 700, 800]
TIMING_SEQ     = [ 10,   5,   8,  15,  12,   7,  10,   9,  12,   4,   9,  10,   6,  11,  10,   5,  12,   8,   4,   6,   5,  11,  12,   9,  16,   9,  10,  12,   7,   9]

MIN_BRIGHTNESS  = 0
MAX_BRIGHTNESS  = 100
MIN_FREQUENCY   = 170
MAX_FREQUENCY   = 800
UPDATE_INTERVAL = 0.05   # seconds between PWM updates (~20 Hz refresh)


def ease_in_out(t):
    return 2 * t * t if t < 0.5 else -1 + (4 - 2 * t) * t


def set_all(pi, brightness, freq):
    """Apply brightness (0–100%) and frequency to all pins at once."""
    dc = max(0,             min(255,           int(brightness * 2.55)))
    f  = max(MIN_FREQUENCY, min(MAX_FREQUENCY, int(freq)))
    for pin in LED_PINS:
        pi.set_PWM_frequency(pin, f)
        pi.set_PWM_dutycycle(pin, dc)


def failsafe(pi):
    """Museum failsafe: leave all outputs at 25% / 800Hz on any stop."""
    print("\nFailsafe: 25% brightness, 800Hz on all outputs.")
    for pin in LED_PINS:
        pi.set_PWM_frequency(pin, 800)
        pi.set_PWM_dutycycle(pin, int(25 * 2.55))


def run(pi):
    steps      = min(len(BRIGHTNESS_SEQ), len(FREQUENCY_SEQ), len(TIMING_SEQ))
    brightness = float(BRIGHTNESS_SEQ[0])
    freq       = float(FREQUENCY_SEQ[0])

    print(f"=== UNDERWATER LIGHTING — {steps} steps, looping ===")
    print(f"    Outputs : {LED_PINS}")
    print(f"    Range   : {MIN_BRIGHTNESS}% – {MAX_BRIGHTNESS}%\n")

    while True:
        for step in range(steps):
            target_b = float(BRIGHTNESS_SEQ[step])
            target_f = float(FREQUENCY_SEQ[step])
            duration = TIMING_SEQ[step]

            print(f"\n--- Step {step+1}/{steps} | {int(target_b)}%  {int(target_f)}Hz  {duration}s ---")

            start_b    = brightness
            start_f    = freq
            start_time = time.monotonic()

            while True:
                elapsed = time.monotonic() - start_time
                if elapsed >= duration:
                    break
                e = ease_in_out(elapsed / duration)

                brightness = max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS,
                                 start_b + (target_b - start_b) * e))
                freq       = max(MIN_FREQUENCY,  min(MAX_FREQUENCY,
                                 start_f + (target_f - start_f) * e))

                set_all(pi, brightness, freq)
                print(f"\r[{datetime.now().strftime('%H:%M:%S')}]  "
                      f"{int(brightness):3d}%   {int(freq):4d} Hz", end='', flush=True)
                time.sleep(UPDATE_INTERVAL)

            # Snap to exact target values at end of step
            brightness = target_b
            freq       = target_f
            set_all(pi, brightness, freq)
            print(f"\r[{datetime.now().strftime('%H:%M:%S')}]  "
                  f"{int(brightness):3d}%   {int(freq):4d} Hz  done")


def _sigterm(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _sigterm)

if __name__ == "__main__":
    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("Cannot connect to pigpiod — is the daemon running?")
    try:
        run(pi)
    except KeyboardInterrupt:
        pass
    finally:
        failsafe(pi)
        pi.stop()

