import pigpio
import time

# GPIO pins for the 6 LEDs
LED_PINS = [12, 13, 17, 19, 27, 22]

# Connect to local pigpio daemon
pi = pigpio.pi()

# Set all pins to max brightness (100%)
for pin in LED_PINS:
    pi.set_PWM_frequency(pin, 800)  # Use a safe high frequency
    pi.set_PWM_dutycycle(pin, 255)  # 255 = 100% duty cycle

try:
    print("All LEDs set to maximum. Press Ctrl+C to exit.")
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("Turning off LEDs...")
finally:
    for pin in LED_PINS:
        pi.set_PWM_dutycycle(pin, 0)
    pi.stop()
