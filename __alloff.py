import pigpio
pi = pigpio.pi()
for p in [4, 18, 17, 27, 22, 5, 12, 13, 26, 19, 6]:
    pi.set_PWM_dutycycle(p, 0)
pi.stop()
print("ALL OFF")
