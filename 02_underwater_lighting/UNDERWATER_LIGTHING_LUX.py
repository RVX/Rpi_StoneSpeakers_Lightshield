# ══════════════════════════════════════════════════════════════════════════════
# UNDERWATER RESONANCES — LUX-ADAPTIVE LED CONTROLLER
# For Julian Charrière / Studio — Correr, Venice 2026
# ══════════════════════════════════════════════════════════════════════════════
#
# SCRIPT
# ─────────────────────
# Controls 6 LED channels via hardware PWM (pigpio) on a Raspberry Pi 4.
# It runs a pre-composed 30-step sequence that slowly varies both
# LED brightness and strobe frequency, creating an underwater tremor effect.
#
# On top of the sequence, a BH1750 ambient light sensor (I2C) continuously
# reads the room brightness. When the space is dark, a brightness FLOOR is
# raised automatically so the LEDs never disappear — the installation always
# stays visible, regardless of the lighting conditions in the room.
#
# LUX 2 BRIGHTNESS FLOOR MAPPING
# ──────────────────────────────────────
#   Room lux < LUX_DARK_THRESHOLD  (default  50 lux) → floor = BOOST_MAX (45%)
#   Room lux > LUX_BRIGHT_THRESHOLD (default 500 lux) → floor = 0% (no boost)
#   In between → linear interpolation
#
#   The LED effective brightness is always:
#       effective = max(sequence_value, lux_brightness_floor)
#   So dark-sequence steps are boosted up, but the artistic shape is preserved.
#
# THREADS
# ───────
#   strobe_effect  — toggles LEDs on/off every 1 ms (creates the shimmer)
#   lux_monitor    — reads the BH1750 sensor every 2 s, updates the floor
#   run_sequence   — drives the 30-step brightness/frequency progression
#
# FAILSAFE
# ────────
#   On any stop or crash all LED channels snap immediately to 25% brightness at 800 Hz (never off).
#   If the lux sensor is missing or fails mid-run, the brightness floor drops to 0 — the
#   sequence runs at its original artistic values with no boost applied.
#
# HARDWARE
# ────────
#   LED GPIO pins : 12, 13, 17, 19, 27, 22
#   BH1750 I2C    : port 1, address 0x23 (ADDR→GND) or 0x5C (ADDR→3.3V)
#   Requires      : pigpiod daemon running, smbus2 Python library, I2C enabled
#
# TUNING
# ──────
#   LUX_DARK_THRESHOLD   — lux level below which full boost applies  (default 50)
#   LUX_BRIGHT_THRESHOLD — lux level above which no boost applies   (default 500)
#   BOOST_MAX_BRIGHTNESS — floor % in total darkness                (default 45)
#   SENSOR_POLL_INTERVAL — seconds between lux reads                (default 2)
#
# DEPENDENCIES
# ────────────
#   sudo raspi-config nonint do_i2c 0   (enable I2C)
#   pip install smbus2                  (or: sudo apt install python3-smbus2)
#   pigpio built from source + pigpiod service running
#   See: https://github.com/joan2937/pigpio
#
# Lux sensor code adapted from:
#   https://github.com/RVX/Rpi_Lux_Monitor_BH1750_Oled
#
# VMG 2025 / 2026
# ══════════════════════════════════════════════════════════════════════════════

import pigpio
import time
import threading
import smbus2
from datetime import datetime

# ── BH1750 Lux Sensor ─────────────────────────────────────────────────────────
I2C_PORT         = 1
BH1750_ADDRESSES = [0x23, 0x5C]  # ADDR pin → GND = 0x23, → 3.3V = 0x5C
BH1750_MODE      = 0x10           # Continuous high-resolution mode
SENSOR_POLL_INTERVAL = 2.0        # Seconds between lux reads

# ── Lux → Brightness Floor Mapping ───────────────────────────────────────────
# The lux value is mapped to a minimum LED brightness floor so the space
# never goes below a visible level when ambient light is low.
#
#   lux <= LUX_DARK_THRESHOLD   → floor = BOOST_MAX_BRIGHTNESS  (very dark room)
#   lux >= LUX_BRIGHT_THRESHOLD → floor = BOOST_MIN_BRIGHTNESS  (bright room, no boost)
#   in between                  → linear interpolation
#
# Tune these to match the real installation environment.
LUX_DARK_THRESHOLD   = 50.0   # lux — below this, start boosting
LUX_BRIGHT_THRESHOLD = 500.0  # lux — above this, sequence runs without boost
BOOST_MAX_BRIGHTNESS = 45     # % floor applied in a very dark room
BOOST_MIN_BRIGHTNESS = 0      # % floor applied in a bright room (no boost)

# ── GPIO / pigpio ──────────────────────────────────────────────────────────────
pi       = pigpio.pi()
LED_PINS = [12, 13, 17, 19, 27, 22]

# Pull all MOSFET gate pins LOW on startup — prevents floating-gate partial conduction
# on pins not yet driven by this script (and persists until pigpiod restarts)
_ALL_MOSFET_PINS = [4, 18, 17, 27, 22, 5, 12, 13, 26, 19, 6]
for _pin in _ALL_MOSFET_PINS:
    pi.set_pull_up_down(_pin, pigpio.PUD_DOWN)
    pi.set_PWM_dutycycle(_pin, 0)

# ── Sequence (same as original UNDERWATER_LIGTHING.py) ───────────────────────
brightness_sequence = [
    25, 15, 15,  8, 10,  9,  8,  6, 25,  5,
    17,  8, 11, 14, 19,  9, 16, 13,  8, 18,
     8,  9, 20,  5,  6,  7, 12,  9,  8,  3,
]
timing_sequence = [
    10,  5,  8, 15, 12,  7, 10,  9, 12,  4,
     9, 10,  6, 11, 10,  5, 12,  8,  4,  6,
     5, 11, 12,  9, 16,  9, 10, 12,  7,  9,
]
frequency_sequence = [
    800, 800, 800, 750, 700, 600, 500, 600, 180, 700,
    600, 400, 450, 650, 550, 750, 800, 600, 500, 300,
    400, 250, 150, 200, 300, 400, 500, 600, 700, 800,
]

# ── Limits ─────────────────────────────────────────────────────────────────────
MIN_BRIGHTNESS = 5
MAX_BRIGHTNESS = 60   # Raised from 25 to allow lux-driven boost above original max
MIN_FREQUENCY  = 170
MAX_FREQUENCY  = 800

# ── Shared runtime state ───────────────────────────────────────────────────────
pwm_frequency        = 500
brightness           = 25
lux_brightness_floor = 0      # Updated live by the lux monitor thread
current_lux          = None   # Latest averaged lux reading (for display only)
running              = True


# ── Sensor helpers ─────────────────────────────────────────────────────────────

def read_lux_single(bus, addr):
    """Return lux float from one BH1750 sensor, or None on I2C error."""
    try:
        data = bus.read_i2c_block_data(addr, BH1750_MODE, 2)
        return (data[0] << 8 | data[1]) / 1.2
    except Exception:
        return None


def read_lux_average(bus):
    """Poll all BH1750 addresses. Returns (avg_lux, [per_sensor_readings]).
    avg_lux is None only if ALL sensors fail."""
    readings = [read_lux_single(bus, addr) for addr in BH1750_ADDRESSES]
    valid    = [v for v in readings if v is not None]
    avg      = sum(valid) / len(valid) if valid else None
    return avg, readings


def lux_to_floor(lux):
    """Map ambient lux to a minimum LED brightness percentage (0–BOOST_MAX)."""
    if lux is None:
        # Sensor failure: no boost, sequence runs at its original values
        return BOOST_MIN_BRIGHTNESS
    if lux >= LUX_BRIGHT_THRESHOLD:
        return BOOST_MIN_BRIGHTNESS
    if lux <= LUX_DARK_THRESHOLD:
        return BOOST_MAX_BRIGHTNESS
    # Linear interpolation between the two thresholds
    t = (lux - LUX_DARK_THRESHOLD) / (LUX_BRIGHT_THRESHOLD - LUX_DARK_THRESHOLD)
    return int(BOOST_MAX_BRIGHTNESS + (BOOST_MIN_BRIGHTNESS - BOOST_MAX_BRIGHTNESS) * t)


def lux_monitor(bus):
    """Background thread: read lux every SENSOR_POLL_INTERVAL seconds and
    update the global brightness floor."""
    global lux_brightness_floor, current_lux
    while running:
        lux, _ = read_lux_average(bus)
        current_lux          = lux
        lux_brightness_floor = lux_to_floor(lux)
        time.sleep(SENSOR_POLL_INTERVAL)


# ── Easing ─────────────────────────────────────────────────────────────────────

def ease_in_out(t):
    if t < 0.5:
        return 2 * t ** 2
    return -1 + (4 - 2 * t) * t


# ── LED control ────────────────────────────────────────────────────────────────

def effective_brightness():
    """Clamp the current brightness up to the lux floor, then cap at MAX."""
    return min(max(brightness, lux_brightness_floor), MAX_BRIGHTNESS)


def strobe_effect():
    """High-frequency strobe: alternates duty cycle at ~1 ms intervals.
    Uses the lux-adjusted brightness so the floor is respected here too."""
    while running:
        eff = effective_brightness()
        for pin in LED_PINS:
            pi.set_PWM_frequency(pin, pwm_frequency)
            pi.set_PWM_dutycycle(pin, int(eff * 2.55))
        time.sleep(0.001)
        for pin in LED_PINS:
            pi.set_PWM_dutycycle(pin, 0)
        time.sleep(0.001)


def run_sequence():
    global brightness, pwm_frequency
    num_steps = min(
        len(brightness_sequence),
        len(timing_sequence),
        len(frequency_sequence),
    )

    while running:
        for step in range(num_steps):
            target_brightness = brightness_sequence[step]
            target_frequency  = frequency_sequence[step]
            duration          = timing_sequence[step]

            lux_label = f"{current_lux:.1f}" if current_lux is not None else "n/a"
            print(f"\n--- Step {step + 1}/{num_steps} ---")
            print(
                f"Target: {target_brightness}%  Freq: {target_frequency} Hz  "
                f"Duration: {duration}s  |  Lux: {lux_label}  Floor: {lux_brightness_floor}%"
            )

            start_brightness = brightness
            start_frequency  = pwm_frequency
            start_time       = time.time()

            while time.time() - start_time < duration:
                elapsed  = time.time() - start_time
                progress = min(elapsed / duration, 1.0)

                # Interpolate sequence values
                brightness    = start_brightness + (target_brightness - start_brightness) * ease_in_out(progress)
                brightness    = max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, brightness))
                pwm_frequency = start_frequency + (target_frequency - start_frequency) * ease_in_out(progress)
                pwm_frequency = max(MIN_FREQUENCY, min(MAX_FREQUENCY, int(pwm_frequency)))

                # Apply lux floor: in a dark room, clamp brightness up to the floor
                eff = effective_brightness()
                for pin in LED_PINS:
                    pi.set_PWM_dutycycle(pin, int(eff * 2.55))
                    pi.set_PWM_frequency(pin, int(pwm_frequency))

                lux_label = f"{current_lux:.1f}" if current_lux is not None else "n/a"
                print(
                    f"\r[{datetime.now().strftime('%H:%M:%S')}]  "
                    f"Seq: {int(brightness)}%  Eff: {int(eff)}%  "
                    f"Freq: {pwm_frequency} Hz  |  Lux: {lux_label}  Floor: {lux_brightness_floor}%",
                    end="",
                )
                time.sleep(0.05)

            # Snap to final values
            brightness    = target_brightness
            pwm_frequency = target_frequency
            eff = effective_brightness()
            for pin in LED_PINS:
                pi.set_PWM_dutycycle(pin, int(eff * 2.55))
                pi.set_PWM_frequency(pin, int(pwm_frequency))

            print(f"\nFinal  Seq: {int(brightness)}%  Eff: {int(eff)}%  Freq: {pwm_frequency} Hz")


def failsafe_mode():
    print("\nFailsafe: 25% brightness, 800 Hz")
    for pin in LED_PINS:
        pi.set_PWM_dutycycle(pin, int(25 * 2.55))
        pi.set_PWM_frequency(pin, 800)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Attempt to open I2C bus for the lux sensor
    bus = None
    lux_thread = None
    try:
        bus = smbus2.SMBus(I2C_PORT)
        print(f"BH1750 sensor bus opened on I2C port {I2C_PORT}")
    except Exception as e:
        print(f"WARNING: Could not open I2C bus: {e}")
        print("Running without lux sensor — brightness floor fixed at 0 (no boost).")

    try:
        strobe_thread = threading.Thread(target=strobe_effect, daemon=True)
        strobe_thread.start()

        if bus is not None:
            lux_thread = threading.Thread(target=lux_monitor, args=(bus,), daemon=True)
            lux_thread.start()

        run_sequence()

    finally:
        failsafe_mode()
        running = False
        strobe_thread.join(timeout=2)
        if lux_thread is not None:
            lux_thread.join(timeout=2)
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass
        pi.stop()
