import pigpio
import time
import signal
import sys

# Define your GPIO pins in the desired order
PINS = [17, 12, 27, 13, 22, 19]

# Connect to local pigpiod daemon
pi = pigpio.pi()
if not pi.connected:
    print("❌ Can't connect to pigpiod — make sure it's running (`sudo systemctl start pigpiod`).")
    sys.exit(1)

# Set up pins as outputs
for pin in PINS:
    pi.set_mode(pin, pigpio.OUTPUT)
    pi.write(pin, 0)

def cleanup(*args):
    print("\nCleaning up GPIOs...")
    for pin in PINS:
        pi.write(pin, 0)
    pi.stop()
    sys.exit(0)

# Handle Ctrl+C
signal.signal(signal.SIGINT, cleanup)

print("🔁 Starting GPIO sequential blink test...\n(Press Ctrl+C to stop)\n")

try:
    while True:
        for pin in PINS:
            print(f"→ GPIO {pin} ON")
            pi.write(pin, 1)
            time.sleep(1)
            pi.write(pin, 0)
except KeyboardInterrupt:
    cleanup()
