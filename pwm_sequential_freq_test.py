#!/usr/bin/env python3
"""
==================================================
PWM Sequential + Frequency Test for Raspberry Pi 
=================================================
Author: Victor Mazon Gardoqui October 2025
Platform: Raspberry Pi 4 (Debian Trixie)
Dependencies: pigpio (daemon must be running: sudo systemctl start pigpiod)

DESCRIPTION:
------------
This script sequentially tests 6 GPIO pins on a Raspberry Pi 4 and a custom shield by varying
their PWM duty cycle and frequency. Each pin lights up an LED (or drives
a connected circuit) in turn, increasing brightness in 8 steps from 0% to 100%,
then decreasing again, across several frequencies. Done for verifying
PWM behavior and hardware connections on a custom shield.

OPERATION:
-----------
1. Connects to the pigpio daemon (required for accurate PWM).
2. Iterates through a predefined list of GPIO pins.
3. For each pin:
    - Cycles through a range of PWM frequencies (200 Hz → 2000 Hz).
    - For each frequency, gradually increases the duty cycle in 8 steps
      from 0% (off) to 100% (fully on), then back down again.
    - Displays the current frequency and duty cycle as text in the terminal.
4. After all frequencies are tested for one pin, it turns the pin off
   and moves to the next one.
5. When the program is stopped (Ctrl + C), all GPIO pins are set LOW safely.

VARIABLES:
-----------
PINS         → GPIO numbers used for testing: [LED17, COB12, LED27, COB13, LED22, COB19]
STEPS        → PWM duty cycle steps (0–255 range)
FREQUENCIES  → PWM frequencies to test
STEP_DELAY   → Time between brightness changes
PIN_DELAY    → Delay before moving to next pin
FREQ_DELAY   → Pause between frequency changes

USAGE:
------
1. Ensure pigpio is installed and the daemon is running:
       sudo apt install pigpio -y
       sudo systemctl start pigpiod
2. Run this script:
       python3 pwm_sequential_freq_test.py
3. Stop anytime with Ctrl + C. The script cleans up automatically.


GPIO PINS USED (in sequential order):
---------------------------------------------------------
| Order | GPIO Number | Physical Pin | Notes             |
|--------|--------------|--------------|-------------------|
|   1    | GPIO 17      | Pin 11       | General purpose   |
|   2    | GPIO 12      | Pin 32       | Hardware PWM-capable |
|   3    | GPIO 27      | Pin 13       | Standard GPIO     |
|   4    | GPIO 13      | Pin 33       | Hardware PWM-capable |
|   5    | GPIO 22      | Pin 15       | Standard GPIO     |
|   6    | GPIO 19      | Pin 35       | Hardware PWM-capable |
---------------------------------------------------------
=====================================================================
"""

import pigpio
import time
import signal
import sys

# GPIO pins to test
PINS = [17, 12, 27, 13, 22, 19]

# PWM duty cycle steps (0–255)
STEPS = [0, 36, 72, 108, 144, 180, 216, 255]

# Delay times
STEP_DELAY = 0.3   # delay between brightness levels
PIN_DELAY = 1.0    # delay between pins
FREQ_DELAY = 0.5   # delay when changing frequency

# Frequencies to test (in Hz)
FREQUENCIES = [200, 500, 800, 1000, 1500, 2000]

# Connect to pigpiod
pi = pigpio.pi()
if not pi.connected:
    print("ERROR: Could not connect to pigpio daemon. Is pigpiod running?")
    sys.exit(1)

# Cleanup handler
def cleanup(*_):
    print("\nCLEANUP: Setting all GPIO pins to LOW...")
    for pin in PINS:
        pi.set_PWM_dutycycle(pin, 0)
        pi.set_mode(pin, pigpio.OUTPUT)
    pi.stop()
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)

# Initialize pins to safe state
for pin in PINS:
    pi.set_mode(pin, pigpio.OUTPUT)
    pi.set_PWM_dutycycle(pin, 0)

print("Starting PWM sequential + frequency test (plain text version)")
time.sleep(1)

try:
    while True:
        for pin in PINS:
            print(f"\nTesting GPIO {pin}")
            for freq in FREQUENCIES:
                pi.set_PWM_frequency(pin, freq)
                print(f"  Frequency set to {freq} Hz")

                # Ramp brightness up
                for duty in STEPS:
                    pi.set_PWM_dutycycle(pin, duty)
                    percent = int((duty / 255) * 100)
                    print(f"    Duty cycle: {percent:>3}% at {freq} Hz")
                    time.sleep(STEP_DELAY)

                # Ramp down
                for duty in reversed(STEPS):
                    pi.set_PWM_dutycycle(pin, duty)
                    time.sleep(STEP_DELAY)

                # Pause before changing frequency
                time.sleep(FREQ_DELAY)

            # Turn off before moving to next pin
            pi.set_PWM_dutycycle(pin, 0)
            time.sleep(PIN_DELAY)

except KeyboardInterrupt:
    cleanup()
