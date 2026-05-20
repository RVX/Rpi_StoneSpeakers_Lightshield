import pigpio
import time
import signal
import sys

# GPIOs to test
PINS = [17, 12, 27, 13, 22, 19]

# Connect to the local pigpiod daemon
pi = pigpio.pi()
if not pi.connected:
    print("❌ Can't connect to pigpiod — start it with `sudo systemctl start pigpiod`")
    sys.exit(1)

print("🔧 Initializing pins safely...")

# Ensure every pin starts clean: output LOW, no pull-up/down
for pin in PINS:
    pi.set_mode(pin, pigpio.OUTPUT)
    pi.set_pull_up_down(pin, pigpio.PUD_OFF)
    pi.write(pin, 0)

def cleanup(*args):
    print("\n🧹 Cleaning up GPIOs...")
    for pin in PINS:
        pi.write(pin, 0)
        pi.set_pull_up_down(pin, pigpio.PUD_OFF)
    pi.stop()
    sys.exit(0)

# Handle Ctrl+C safely
signal.signal(signal.SIGINT, cleanup)

print("\n🔁 Starting GPIO sequential blink test...")
print("(Press Ctrl+C to stop)\n")

try:
    while True:
        for pin in PINS:
            print(f"→ GPIO {pin} ON")
            pi.write(pin, 1)
            time.sleep(1)
            pi.write(pin, 0)
except KeyboardInterrupt:
    cleanup()
