#!/usr/bin/env python3
import pigpio
import time
import signal
import sys

# List of GPIO pins to test
PINS = [17, 12, 27, 13, 22, 19]

# PWM duty cycle steps (0–255)
STEPS = [0, 36, 72, 108, 144, 180, 216, 255]
STEP_DELAY = 0.3  # seconds between brightness steps
PIN_DELAY = 1.0   # pause between pins

pi = pigpio.pi()
if not pi.connected:
    print("❌ Could not connect to pigpio daemon. Is pigpiod running?")
    sys.exit(1)

# --- Safety cleanup on exit ---
def cleanup(*_):
    print("\n🧹 Cleaning up GPIOs (set all LOW)...")
    for pin in PINS:
        pi.set_PWM_dutycycle(pin, 0)
        pi.set_mode(pin, pigpio.OUTPUT)
    pi.stop()
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# --- Initialize all pins to LOW ---
for pin in PINS:
    pi.set_mode(pin, pigpio.OUTPUT)
    pi.set_PWM_dutycycle(pin, 0)

print("🚦 Starting PWM sequential test (Trixie-compatible)")
time.sleep(1)

try:
    while True:
        for pin in PINS:
            print(f"\n--- Testing GPIO {pin} ---")
            # Ramp brightness up
            for duty in STEPS:
                pi.set_PWM_dutycycle(pin, duty)
                print(f"GPIO {pin}: {int((duty/255)*100)}%")
                time.sleep(STEP_DELAY)

            # Ramp brightness down
            for duty in reversed(STEPS):
                pi.set_PWM_dutycycle(pin, duty)
                time.sleep(STEP_DELAY)

            # Turn off before moving to next pin
            pi.set_PWM_dutycycle(pin, 0)
            time.sleep(PIN_DELAY)

except KeyboardInterrupt:
    cleanup()
