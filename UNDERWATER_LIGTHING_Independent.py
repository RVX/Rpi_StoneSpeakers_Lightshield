# UNDERWATER RESONANCES FOR J.C.STUDIO
# USING THE CUSTOM SHIELD FROM STONE SPEAKERS
# VMG 2025

import pigpio
import time
import threading
from datetime import datetime

# Initialize pigpio
pi = pigpio.pi()

# Define the GPIO pins for LEDs (GPIO12, GPIO13, GPIO19)
LED_PINS = [12, 13, 19]

# Set initial PWM frequency and duty cycle
pwm_frequency = 500  # Set initial PWM frequency to 500 Hz
brightness = 25  # Start with lowest brightness (25%)
running = True

# Custom brightness sequence (non-linear, hard-coded)
brightness_sequence = [25, 15, 15, 8, 10, 9, 8, 6, 25, 5, 17, 8, 11, 14, 19, 9, 16, 13, 8, 18, 8, 9, 20, 5, 6, 7, 12, 9, 8, 3]

# Custom timing sequence (in seconds, hard-coded)
timing_sequence = [10, 5, 8, 15, 12, 7, 10, 9, 12, 4, 9, 10, 6, 11, 10, 5, 12, 8, 4, 6, 5, 11, 12, 9, 16, 9, 10, 12, 7, 9]

# Custom frequency sequence to correspond to brightness steps (hard-coded)
frequency_sequence = [800, 800, 800, 750, 700, 600, 500, 600, 180, 700, 600, 400, 450, 650, 550, 750, 800, 600, 500, 300, 400, 250, 150, 200, 300, 400, 500, 600, 700, 800]

# Minimum and maximum limits
MIN_BRIGHTNESS = 5  # Minimum brightness
MAX_BRIGHTNESS = 25  # Maximum brightness
MIN_FREQUENCY = 170  # Minimum frequency
MAX_FREQUENCY = 800  # Maximum frequency

# Nonlinear easing function for brightness and frequency
def ease_in_out(t):
    """Nonlinear easing function."""
    if t < 0.5:
        return 2 * t ** 2
    else:
        return -1 + (4 - 2 * t) * t

# Function to handle strobe effect with high-frequency PWM
def strobe_effect():
    while running:
        for pin in LED_PINS:
            pi.set_PWM_frequency(pin, pwm_frequency)  # Set the PWM frequency for each pin
            pi.set_PWM_dutycycle(pin, int(brightness * 2.55))  # Adjust brightness (0-255 for pigpio)
        time.sleep(0.001)  # Delay to create strobe effect

        for pin in LED_PINS:
            pi.set_PWM_dutycycle(pin, 0)  # Turn off LED
        time.sleep(0.001)  # Delay to create strobe effect

# Function to run the brightness and frequency sequences continuously
def run_sequence():
    global brightness, pwm_frequency
    num_steps = min(len(brightness_sequence), len(timing_sequence), len(frequency_sequence))

    while running:  # Loop to restart the sequence
        for step in range(num_steps):
            target_brightness = brightness_sequence[step]
            target_frequency = frequency_sequence[step]
            duration = timing_sequence[step]

            print(f"\n--- Step {step + 1}/{num_steps} ---")
            print(f"Target Brightness: {target_brightness}%, Target Frequency: {target_frequency}Hz, Duration: {duration}s")

            # Brightness and Frequency Transition
            start_brightness = brightness
            start_frequency = pwm_frequency
            start_time = time.time()
            
            while time.time() - start_time < duration:
                elapsed = time.time() - start_time
                progress = min(elapsed / duration, 1)

                # Ease in/out brightness
                brightness = start_brightness + (target_brightness - start_brightness) * ease_in_out(progress)
                brightness = max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, brightness))

                # Ease in/out frequency
                pwm_frequency = start_frequency + (target_frequency - start_frequency) * ease_in_out(progress)
                pwm_frequency = max(MIN_FREQUENCY, min(MAX_FREQUENCY, int(pwm_frequency)))
                
                # Apply the new brightness and frequency
                for pin in LED_PINS:
                    if pin == 19:
                        # Increase brightness for GPIO 19, clamp to 100%
                        bright = min(brightness * 4, 100)
                    else:
                        bright = brightness
                    pi.set_PWM_dutycycle(pin, int(bright * 2.55))
                    pi.set_PWM_frequency(pin, int(pwm_frequency))
                
                print(f"\r[{datetime.now().strftime('%H:%M:%S')}]: Brightness: {int(brightness)}% (GPIO19: {int(min(brightness*1.5,100))}%), Frequency: {pwm_frequency}Hz", end='')

                time.sleep(0.05)

            # Final adjustment
            for pin in LED_PINS:
                if pin == 19:
                    bright = min(target_brightness * 1.5, 100)
                else:
                    bright = target_brightness
                pi.set_PWM_dutycycle(pin, int(bright * 2.55))
                pi.set_PWM_frequency(pin, int(target_frequency))
            
            print(f"\nFinal Brightness: {int(target_brightness)}% (GPIO19: {int(min(target_brightness*1.5,100))}%), Frequency: {target_frequency}Hz")

# Ensure failsafe mechanism: Set LEDs to 25% brightness and 800Hz if the script fails or stops
def failsafe_mode():
    print("\nSetting failsafe mode: 25% brightness and 800Hz")
    for pin in LED_PINS:
        pi.set_PWM_dutycycle(pin, int(25 * 2.55))  # Set brightness to 25%
        pi.set_PWM_frequency(pin, 800)  # Set frequency to 800Hz

if __name__ == "__main__":
    try:
        # Start strobing in a separate thread
        strobe_thread = threading.Thread(target=strobe_effect)
        strobe_thread.start()

        # Start the sequence automatically without menu interaction
        run_sequence()

    finally:
        # Set failsafe mode when script stops or fails
        failsafe_mode()
        running = False
        strobe_thread.join()  # Ensure the strobe thread is terminated
        pi.stop()  # Stop the pigpio daemon

