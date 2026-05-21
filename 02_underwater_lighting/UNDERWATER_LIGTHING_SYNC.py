# UNDERWATER RESONANCES FOR J.C.STUDIO
# USING THE CUSTOM SHIELD FROM STONE SPEAKERS
# VMG 2025 — SYNC FLASH VERSION
#
# All outputs run the same ambient sequence continuously.
# At SYNC_SECOND of every minute (wall clock), every Pi in the
# installation fires the same sync flash simultaneously — no
# network coordination needed; they all read the same clock.

import pigpio
import time
import signal
import random
from datetime import datetime

# GPIO pins — OUT1 to OUT8 in order
LED_PINS = [4, 18, 17, 27, 22, 5, 12, 13]

# Sequences: brightness (0–100%), frequency (Hz), duration (seconds) per step
BRIGHTNESS_SEQ = [100,   0, 100,   0,  10,  3,  40,  11,  1,  80,  12,  25,  9, 100,   0,   9,  16,  13,   8,  18,   8,   9,  20,   5,   6,   7,  12,   9,   8,   25]
FREQUENCY_SEQ  = [800, 800, 800, 750, 700, 600, 500, 600, 180, 700, 600, 400, 450, 650, 550, 750, 800, 600, 500, 300, 400, 250, 150, 200, 300, 400, 500, 600, 700, 800]
TIMING_SEQ     = [ 2,   5,   3,  15,  12,   7,  10,   9,  12,   4,   9,  10,   6,  11,  10,   5,  12,   8,   4,   6,   5,  11,  12,   9,  16,   9,  10,  12,   7,   9]

MIN_BRIGHTNESS  = 0
MAX_BRIGHTNESS  = 100
MIN_FREQUENCY   = 170
MAX_FREQUENCY   = 800
UPDATE_INTERVAL = 0.05   # seconds between PWM updates (~20 Hz refresh)

# --- Sync flash config --------------------------------------------------------
SYNC_SECOND = 0   # wall-clock second that triggers (0 = top of every minute)
#
# Storm archetypes (chosen randomly each minute):
#   quick_zap     — single sharp flash, brief afterflicker, gone in <1s
#   full_storm    — dramatic multi-phase tempest (2–4s)
#   double_strike — two powerful strikes separated by dark silence
#   strobe_burst  — rapid electrical pulses, electric feel (1–2s)
#   creeping      — builds from dim precursors up to full strike then dies
#   long_rumble   — many small flashes spread over several seconds
# ------------------------------------------------------------------------------


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


def _thunder_burst(pi, flashes, brightness_range, on_range, off_range):
    """Fire a random cluster of flashes — core of the storm effect."""
    for _ in range(flashes):
        b = random.uniform(*brightness_range)
        set_all(pi, b, 800)
        time.sleep(random.uniform(*on_range))
        set_all(pi, 0, 800)
        time.sleep(random.uniform(*off_range))


def sync_flash(pi, current_brightness):
    """
    Each minute a random storm archetype is chosen so no two events look alike.
    Rise-back duration is also randomised so even the recovery is unpredictable.
    """
    archetype = random.choice([
        "quick_zap",
        "full_storm",
        "double_strike",
        "strobe_burst",
        "creeping",
        "long_rumble",
    ])
    rise_dur = random.uniform(0.3, 2.8)   # random recovery speed every time

    print(f"\n  *** STORM [{archetype}] from {int(current_brightness)}% ***")

    # All archetypes start with an instant cut to black
    set_all(pi, 0, 800)

    if archetype == "quick_zap":
        # Blink and gone — visitors might not even catch it
        time.sleep(random.uniform(0.02, 0.08))
        set_all(pi, random.uniform(80, 100), 800)
        time.sleep(random.uniform(0.05, 0.12))
        set_all(pi, 0, 800)
        _thunder_burst(pi,
                       flashes=random.randint(2, 4),
                       brightness_range=(30, 70),
                       on_range=(0.02, 0.05),
                       off_range=(0.01, 0.04))

    elif archetype == "full_storm":
        # Dramatic multi-phase tempest
        time.sleep(random.uniform(0.05, 0.15))
        set_all(pi, 100, 800)
        time.sleep(random.uniform(0.10, 0.22))
        set_all(pi, 0, 800)
        time.sleep(random.uniform(0.02, 0.07))
        _thunder_burst(pi,
                       flashes=random.randint(5, 12),
                       brightness_range=(60, 100),
                       on_range=(0.02, 0.07),
                       off_range=(0.01, 0.05))
        time.sleep(random.uniform(0.08, 0.20))
        set_all(pi, random.uniform(40, 80), 800)
        time.sleep(random.uniform(0.07, 0.18))
        set_all(pi, 0, 800)
        time.sleep(random.uniform(0.04, 0.10))
        _thunder_burst(pi,
                       flashes=random.randint(3, 8),
                       brightness_range=(15, 55),
                       on_range=(0.02, 0.06),
                       off_range=(0.03, 0.15))

    elif archetype == "double_strike":
        # Two powerful strikes separated by ominous dark silence
        time.sleep(random.uniform(0.04, 0.10))
        set_all(pi, random.uniform(80, 100), 800)
        time.sleep(random.uniform(0.10, 0.20))
        set_all(pi, 0, 800)
        time.sleep(random.uniform(0.18, 0.45))   # long dark gap — tension
        set_all(pi, random.uniform(70, 100), 800)
        time.sleep(random.uniform(0.12, 0.25))
        set_all(pi, 0, 800)
        time.sleep(random.uniform(0.03, 0.08))
        _thunder_burst(pi,
                       flashes=random.randint(2, 5),
                       brightness_range=(20, 60),
                       on_range=(0.02, 0.05),
                       off_range=(0.02, 0.10))

    elif archetype == "strobe_burst":
        # Rapid electrical pulses — harsh, industrial feel
        time.sleep(random.uniform(0.02, 0.06))
        count = random.randint(8, 20)
        for _ in range(count):
            set_all(pi, random.uniform(50, 100), 800)
            time.sleep(random.uniform(0.02, 0.06))
            set_all(pi, 0, 800)
            time.sleep(random.uniform(0.01, 0.04))

    elif archetype == "creeping":
        # Dim precursors build slowly to a full strike, then fade away
        time.sleep(random.uniform(0.05, 0.12))
        _thunder_burst(pi,
                       flashes=random.randint(2, 4),
                       brightness_range=(8, 30),
                       on_range=(0.03, 0.09),
                       off_range=(0.05, 0.15))
        time.sleep(random.uniform(0.08, 0.15))
        # crescendo — brightness rises in steps
        for level in [35, 55, 75, 100]:
            b = max(5, min(100, level + random.uniform(-12, 12)))
            set_all(pi, b, 800)
            time.sleep(random.uniform(0.04, 0.09))
            set_all(pi, 0, 800)
            time.sleep(random.uniform(0.02, 0.06))
        # aftermath — dying embers
        _thunder_burst(pi,
                       flashes=random.randint(3, 6),
                       brightness_range=(10, 45),
                       on_range=(0.02, 0.06),
                       off_range=(0.05, 0.18))

    elif archetype == "long_rumble":
        # Many small flashes spread over several seconds, slowly dimming
        time.sleep(random.uniform(0.05, 0.15))
        total = random.randint(12, 22)
        for i in range(total):
            b = max(5, min(100, random.uniform(75 - i * 3, 100 - i * 2)))
            set_all(pi, b, 800)
            time.sleep(random.uniform(0.02, 0.06))
            set_all(pi, 0, 800)
            # gaps get progressively longer as storm fades
            time.sleep(random.uniform(0.03 + i * 0.015, 0.10 + i * 0.025))

    # Fade back up — duration varies each storm so recovery feels different too
    steps = max(1, int(rise_dur / UPDATE_INTERVAL))
    for i in range(steps + 1):
        set_all(pi, current_brightness * i / steps, 800)
        time.sleep(rise_dur / steps)

    print(f"  *** STORM [{archetype}] over — resuming at {int(current_brightness)}% ***")


def run(pi):
    steps      = min(len(BRIGHTNESS_SEQ), len(FREQUENCY_SEQ), len(TIMING_SEQ))
    brightness = float(BRIGHTNESS_SEQ[0])
    freq       = float(FREQUENCY_SEQ[0])
    last_sync_minute = -1   # tracks which minute we last fired

    print(f"=== UNDERWATER SYNC — {steps} steps, looping ===")
    print(f"    Outputs    : {LED_PINS}")
    print(f"    Range      : {MIN_BRIGHTNESS}% – {MAX_BRIGHTNESS}%")
    print(f"    Sync flash : every minute at second :{SYNC_SECOND:02d}  (thunderstorm)\n")

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

                # --- Sync check (wall clock) ---
                now = datetime.now()
                this_minute = now.hour * 60 + now.minute
                if now.second == SYNC_SECOND and this_minute != last_sync_minute:
                    last_sync_minute = this_minute
                    sync_start = time.monotonic()
                    sync_flash(pi, brightness)
                    # Extend step timer by however long sync took so the
                    # step resumes exactly where it left off
                    start_time += time.monotonic() - sync_start
                    elapsed = time.monotonic() - start_time

                e = ease_in_out(elapsed / duration) if duration > 0 else 1.0

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
