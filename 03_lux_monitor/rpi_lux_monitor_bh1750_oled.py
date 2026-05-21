import sys
import time
import signal
import subprocess
import smbus2
import getpass
import socket
from collections import deque
from luma.core.interface.serial import i2c
from luma.core.render import canvas
from luma.oled.device import ssd1306

# Minimal commands after editing this file:
# - Run once after service-file changes: sudo systemctl daemon-reload
# - Stop monitor: sudo systemctl stop rpi_lux_monitor_bh1750_oled.service
# - Start monitor: sudo systemctl start rpi_lux_monitor_bh1750_oled.service
# - Apply code changes quickly: sudo systemctl restart rpi_lux_monitor_bh1750_oled.service

# Hardware setup
I2C_PORT = 1  # I2C bus number on Raspberry Pi (usually 1).
OLED_ADDR = 0x3C  # OLED I2C address (common: 0x3C or 0x3D).
# BH1750 addresses: ADDR pin to GND = 0x23, ADDR pin to 3.3V = 0x5C.
# Add or remove addresses from this list to support 1, 2, or (with a TCA9548A) 3+ sensors.
BH1750_ADDRESSES = [0x23, 0x5C] # use 0x23 for first sensor, 0x5C for second sensor
BH1750_MODE = 0x10  # BH1750 continuous high-resolution mode.

# Data/history tuning
HISTORY_SIZE = 64  # Number of lux samples kept for trend graph.

# Refresh timing
REFRESH_SECONDS = 0.8  # Main loop period: screen + lux update rate.
SYSINFO_REFRESH_SECONDS = 5.0  # Update period for user/IP/CPU temp.

# Light thresholds
LOW_LUX_THRESHOLD = 15.0  # Show low-light alert below this lux level.
NIGHT_LUX_THRESHOLD = 5.0  # Switch to dim contrast below this lux level.
LUX_FOR_MAX_CONTRAST = 120.0  # Lux level where display reaches MAX_CONTRAST.
CONTRAST_GAMMA = 0.70  # <1.0 brightens faster, >1.0 brightens slower.

# Display brightness
MIN_CONTRAST = 1  # Min contrast level of screen in night mode.
MAX_CONTRAST = 200  # Max contrast level of screen in day mode.

# Trend graph geometry (right strip, x=87-127, y=26-63)
GRAPH_X = 87       # left edge of graph strip
GRAPH_Y_BASE = 63  # bottom pixel of graph
GRAPH_H = 37       # max bar height in pixels
GRAPH_W = 128 - GRAPH_X  # 41 pixels wide → 41 samples visible

RUNNING = True


def handle_stop_signal(signum, frame):
    # Allow clean shutdown when systemd stops the service.
    del signum, frame
    global RUNNING
    RUNNING = False


def init_oled():
    # If your OLED address changes, update OLED_ADDR above.
    serial = i2c(port=I2C_PORT, address=OLED_ADDR)
    return ssd1306(serial)


def init_sensor_bus():
    return smbus2.SMBus(I2C_PORT)


def get_primary_ip():
    # UDP connect is lightweight and avoids spawning shell commands each loop.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except Exception:
        return "0.0.0.0"


def get_cpu_temp():
    # Fast path on Linux/Raspberry Pi; fallback keeps compatibility on other setups.
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r", encoding="utf-8") as f:
            milli_c = f.read().strip()
        return f"{int(milli_c) / 1000:.1f} C"
    except Exception:
        try:
            result = subprocess.run(
                ["vcgencmd", "measure_temp"],
                capture_output=True,
                text=True,
                check=True,
            )
            raw = result.stdout.strip()
            if "=" in raw:
                raw = raw.split("=", 1)[1]
            temp_num = raw.split("'", 1)[0].strip()
            return f"{temp_num} C" if temp_num else "0 C"
        except Exception:
            return "0 C"


def get_sys_info():
    user_host = f"{getpass.getuser()}@{socket.gethostname()}"
    ip = get_primary_ip()
    temp = get_cpu_temp()
    return user_host, ip, temp


def read_lux_single(bus, addr):
    # Returns lux float from one sensor, or None on I2C error.
    try:
        data = bus.read_i2c_block_data(addr, BH1750_MODE, 2)
        return (data[0] << 8 | data[1]) / 1.2
    except Exception:
        return None


def read_lux_average(bus):
    # Poll every address in BH1750_ADDRESSES and return (avg, readings_list).
    # avg is None only if ALL sensors fail; individual entries are None on per-sensor error.
    readings = [read_lux_single(bus, addr) for addr in BH1750_ADDRESSES]
    valid = [v for v in readings if v is not None]
    avg = sum(valid) / len(valid) if valid else None
    return avg, readings


def trim_text(text, max_chars):
    # Keep text inside 128x64 without overlapping other fields.
    if max_chars <= 1:
        return text[:1]
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "~"


def compute_contrast_from_lux(lux):
    # Map ambient light to display contrast so brightness changes are easier to notice.
    if lux is None:
        return MAX_CONTRAST

    if lux <= NIGHT_LUX_THRESHOLD:
        return MIN_CONTRAST

    if lux >= LUX_FOR_MAX_CONTRAST:
        return MAX_CONTRAST

    span = LUX_FOR_MAX_CONTRAST - NIGHT_LUX_THRESHOLD
    if span <= 0:
        return MAX_CONTRAST

    normalized = (lux - NIGHT_LUX_THRESHOLD) / span
    curved = normalized ** CONTRAST_GAMMA
    return int(round(MIN_CONTRAST + (MAX_CONTRAST - MIN_CONTRAST) * curved))


def draw_dashboard(device, history, user_info, ip, temp, lux, lux_readings, blink_state):
    # Layout (128x64 OLED, yellow zone = y 0-15, blue zone = y 16-63):
    #   y= 0 : user@host (left)   temp (right)      ← yellow zone
    #   y=18 : full-width IP                         ← blue zone, same as original
    #   y=34 : L1:xxxx   L2:xxxx  (x 0-86)          ← left column
    #   y=50 : Av:xxx.x LX        (x 0-86)          ← left column
    #   x=87-127, y=26-63 : right-strip trend graph  ← see GRAPH_* constants
    with canvas(device) as draw:
        draw.text((0, 0), trim_text(user_info, 15), fill="white")
        draw.text((90, 0), trim_text(temp, 6), fill="white")
        draw.text((0, 18), trim_text(ip, 21), fill="white")

        # Individual readings — always shown so sensor faults are immediately visible.
        l1 = lux_readings[0]  # always present; len(BH1750_ADDRESSES) >= 1
        l2 = lux_readings[1] if len(lux_readings) > 1 else None
        l1_str = f"L1:{int(round(l1))}" if l1 is not None else "L1:NoS"
        l2_str = f"L2:{int(round(l2))}" if l2 is not None else "L2:NoS"
        draw.text((0, 34), l1_str, fill="white")
        draw.text((45, 34), l2_str, fill="white")

        if lux is None:
            draw.text((0, 50), "SENSOR ERROR", fill="white")
            return blink_state

        if lux < LOW_LUX_THRESHOLD:
            if blink_state:
                draw.text((0, 50), f"!LOW! {lux:.1f} LX", fill="white")
            return not blink_state

        draw.text((0, 50), f"Av:{lux:.1f} LX", fill="white")

        # Right-strip graph: last GRAPH_W samples, one pixel per sample.
        recent = list(history)[-GRAPH_W:]
        points = []
        for i, val in enumerate(recent):
            x = GRAPH_X + i
            y_height = int(min((val / 1000.0) * GRAPH_H, GRAPH_H))
            y = GRAPH_Y_BASE - y_height
            points.append((x, y))

        if len(points) > 1:
            draw.line(points, fill="white", joint="curve")

        return blink_state


def main():
    print("OLED monitor iniciado.", flush=True)

    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)

    try:
        device = init_oled()
        bus = init_sensor_bus()
    except Exception as exc:
        print(f"Error inicializando hardware: {exc}", flush=True)
        return 1

    history = deque([0.0] * HISTORY_SIZE, maxlen=HISTORY_SIZE)
    blink_state = True
    # Cache slow-changing data; initialise timestamp so the first loop
    # iteration does not trigger a redundant second call to get_sys_info().
    user_info, ip, temp = get_sys_info()
    last_sysinfo_update = time.monotonic()

    try:
        while RUNNING:
            now = time.monotonic()
            if now - last_sysinfo_update >= SYSINFO_REFRESH_SECONDS:
                # Update user/IP/temp less frequently than lux sampling.
                user_info, ip, temp = get_sys_info()
                last_sysinfo_update = now

            lux, lux_readings = read_lux_average(bus)

            if lux is not None:
                history.append(lux)
            else:
                # Keep graph flowing even on temporary sensor failures.
                history.append(0.0)

            try:
                device.contrast(compute_contrast_from_lux(lux))
            except Exception as exc:
                print(f"No se pudo ajustar contraste: {exc}", flush=True)

            blink_state = draw_dashboard(device, history, user_info, ip, temp, lux, lux_readings, blink_state)
            time.sleep(REFRESH_SECONDS)
    finally:
        try:
            bus.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())