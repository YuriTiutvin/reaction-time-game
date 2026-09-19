from machine import Pin as pin
from machine import I2C as i2c
from machine import PWM, Timer
import machine
import time
import random
import ssd1306 as ssd
import framebuf
import math
import os
import micropython
micropython.alloc_emergency_exception_buf(100)
random.seed(int.from_bytes(os.urandom(4), "little"))
# ---------- config ----------
BEST = 100000          # session best (RAM only; resets on power-off)
S_INTERVAL = 1000      # ms between LEDs lighting
POST_ROUND_MS = 2500   # pause after a result before the next round
CINEMA_UNDER = 100     # a new best faster than this earns the long cinematic
# battery thresholds (estimated cell volts) -- from Calc v2-6
BATT_FULL = 3.9
BATT_HALF = 3.6
BATT_CRIT = 3.4
# ---------- hardware ----------
oledi2c = i2c(1, scl=pin(11), sda=pin(14), freq=400000)
oled = ssd.SSD1306_I2C(128, 64, oledi2c)
button = pin(15, pin.IN, pin.PULL_UP)

LED_BRIGHT = 2000     # PWM duty out of 65535; lower = dimmer (tune to taste)

leds = [PWM(pin(n)) for n in range(5)]
for _l in leds:
    _l.freq(1000)     # 1 kHz -> fast enough that there's no visible flicker
    _l.duty_u16(0)

# power sensing
vbus_sense = pin(24, pin.IN)          # HIGH when USB supplies VBUS
vsys_adc = machine.ADC(29)            # GP29 = VSYS / 3
led_onboard = pin(25, pin.OUT)        # onboard LED (plain Pico)
buzzer = PWM(pin(8))
buzzer.duty_u16(0)
mute_sense = pin(7, pin.IN, pin.PULL_UP)   # HIGH = sound on, LOW = muted
# ---------- optional cinematic library ----------
try:
    import cinema
    cinema.init(oled, leds, oledi2c)
    cinema.SPEED = 3.5
except Exception:
    cinema = None
# ---------- power helpers ----------
def on_usb():
    return vbus_sense.value() == 1
def battery_volts():
    total = 0
    for _ in range(8):                # average out LED/buzzer load sag
        total += vsys_adc.read_u16()
    vsys = (total / 8) / 65535 * 3.3 * 3
    return vsys + 0.3                  # + Schottky drop -> approx cell voltage
def battery_critical():
    return (not on_usb()) and (battery_volts() <= BATT_CRIT)
# ---------- shared state ----------
time_of_press = 0
time_of_start = 0
false_start = False
BTN_IDLE = button.value()     # sampled once, so "pressed" works either polarity
def button_pressed():
    return button.value() != BTN_IDLE
# ---------- interrupt ----------
def press_detected(_):
    global false_start, time_of_press
    if time_of_start == 0:
        false_start = True
    else:
        time_of_press = time.ticks_ms()
# ---------- LED helpers ----------
def all_off():
    for led in leds:
        led.duty_u16(0)
def all_on():
    for led in leds:
        led.duty_u16(LED_BRIGHT)
# ---------- timing ----------
def wait_ms(duration):
    deadline = time.ticks_add(time.ticks_ms(), duration)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if false_start:
            return
        time.sleep_ms(5)
def sequence(hold):
    global time_of_start
    for led in leds:
        wait_ms(S_INTERVAL)
        if false_start:
            return
        led.duty_u16(LED_BRIGHT)     # was: led.on()
    wait_ms(hold)
    if false_start:
        return
    all_off()
    time_of_start = time.ticks_ms()
# ---------- sound helpers (non-blocking, timer-driven) ----------
def sound_on():
    return mute_sense.value() == 1
_seq_timer = Timer()
_seq = []
_seq_i = 0
def _seq_step(t):
    global _seq_i
    if _seq_i >= len(_seq):
        buzzer.duty_u16(0)
        return
    f, ms = _seq[_seq_i]
    _seq_i += 1
    if f > 0:
        buzzer.freq(f); buzzer.duty_u16(24000)
    else:
        buzzer.duty_u16(0)            # rest
    _seq_timer.init(mode=Timer.ONE_SHOT, period=ms, callback=_seq_step)
def play_notes(notes):
    global _seq, _seq_i
    _seq = notes; _seq_i = 0
    _seq_step(None)                   # starts immediately, returns immediately
def beep_start():
    play_notes([(1200, 50)])                                   # light chirp
def buzz_false():
    play_notes([(220, 180), (0, 20), (150, 320)])              # low "wrong"
def horn_newbest():
    play_notes([(523,130),(0,20),(659,130),(0,20),(784,130),(0,20),(1046,340)])
# ---------- display helpers ----------
def disc(cx, cy, r, c=1):
    r = int(r)
    if r < 1:
        return
    oled.ellipse(int(cx), int(cy), r, r, c, True)
def scaled_text(s, x, y, scale=2):
    w = len(s) * 8
    buf = bytearray(w)
    fb = framebuf.FrameBuffer(buf, w, 8, framebuf.MONO_HLSB)
    fb.text(s, 0, 0, 1)
    for py in range(8):
        for px in range(w):
            if fb.pixel(px, py):
                oled.fill_rect(x + px * scale, y + py * scale, scale, scale, 1)
# ---------- indicators (power = top-right, sound = top-left) ----------
def draw_bolt(x, y):
    oled.line(x + 2, y, x, y + 3, 1)
    oled.line(x, y + 3, x + 3, y + 3, 1)
    oled.line(x + 3, y + 3, x + 1, y + 6, 1)
def draw_batt_icon(level):
    x, y = 110, 0
    oled.rect(x, y, 14, 8, 1)
    oled.fill_rect(x + 14, y + 2, 2, 4, 1)          # terminal nub
    if level == "full":
        oled.fill_rect(x + 2, y + 2, 10, 4, 1)
    elif level == "half":
        oled.fill_rect(x + 2, y + 2, 5, 4, 1)
    else:                                           # low: empty + bolt
        draw_bolt(x + 5, y + 1)
def draw_usb_icon():
    x, cy = 106, 4
    oled.hline(x + 2, cy, 11, 1)                    # shaft
    disc(x + 2, cy, 1)                              # round tail
    oled.fill_rect(x + 12, cy - 1, 3, 3, 1)         # arrowhead tip
    oled.line(x + 12, cy, x + 8, cy - 3, 1)         # top prong
    disc(x + 8, cy - 3, 1)                          # circle on top prong
    oled.line(x + 12, cy, x + 8, cy + 3, 1)         # bottom prong
    oled.fill_rect(x + 7, cy + 2, 3, 3, 1)          # square on bottom prong
def draw_speaker_icon(on):
    x, y = 0, 0                                     # top-LEFT, by the switch
    oled.fill_rect(x, y + 3, 3, 3, 1)
    oled.line(x + 3, y + 3, x + 6, y, 1)
    oled.line(x + 3, y + 5, x + 6, y + 8, 1)
    oled.vline(x + 6, y, 9, 1)
    if on:
        oled.vline(x + 9, y + 2, 5, 1)             # sound waves
        oled.vline(x + 11, y, 9, 1)
    else:
        oled.line(x + 8, y, x + 13, y + 8, 1)      # mute slash
def draw_power_indicator():
    if on_usb():
        draw_usb_icon()
    else:
        v = battery_volts()
        if v > BATT_FULL:
            draw_batt_icon("full")
        elif v > BATT_HALF:
            draw_batt_icon("half")
        else:
            draw_batt_icon("low")
    draw_speaker_icon(sound_on())
def draw_footer():
    label = "BEST: --" if BEST == 100000 else "BEST: " + str(BEST) + " ms"
    x = (128 - len(label) * 8) // 2
    oled.text(label, x, 56)
def show_center(*lines):
    oled.fill(0)
    y0 = (56 - len(lines) * 12) // 2
    for i, line in enumerate(lines):
        x = (128 - len(line) * 8) // 2
        oled.text(line, x, y0 + i * 12)
    draw_footer()
    oled.show()
def show_ready():
    oled.fill(0)
    oled.text("Press button", 16, 22)
    oled.text("when ready", 24, 34)
    draw_footer()
    draw_power_indicator()
    oled.show()
def show_result(reaction, scale=3):
    num = str(reaction)
    nw = len(num) * 8 * scale
    x = (128 - (nw + 4 + 16)) // 2
    oled.fill(0)
    scaled_text(num, x, 16, scale)
    oled.text("ms", x + nw + 4, 28)
    draw_footer()
    draw_power_indicator()
    oled.show()
# ---------- critical battery ----------
def low_battery_shutdown():
    for _ in range(3):
        oled.fill(0)
        scaled_text("LOW", 28, 6, 3)
        scaled_text("BATTERY", 8, 36, 2)
        oled.show()
        led_onboard.on(); all_on()
        time.sleep_ms(450)
        oled.fill(0); oled.show()
        led_onboard.off(); all_off()
        time.sleep_ms(300)
    oled.fill(0); oled.show()
    all_off(); led_onboard.off()
    machine.deepsleep()               # true low-power off; wakes on reset/power-cycle
# ---------- false-start animation (thick, shaking X + white slams) ----------
def draw_thick_x(dx=0, dy=0, t=3):
    for o in range(-t, t + 1):
        oled.line(dx, dy + o, 127 + dx, 63 + dy + o, 1)
        oled.line(dx, 63 + dy + o, 127 + dx, dy + o, 1)
def false_start_anim():
    for _ in range(6):
        dx = random.randint(-3, 3)
        dy = random.randint(-3, 3)
        oled.fill(0)
        draw_thick_x(dx, dy, 3)
        oled.show(); all_on(); time.sleep_ms(110)
        oled.fill(1); oled.show(); all_off(); time.sleep_ms(60)
    oled.fill(0)
    draw_thick_x(0, 0, 3)
    oled.fill_rect(12, 26, 104, 12, 0)
    oled.text("FALSE START", 20, 28, 1)
    oled.fill_rect(0, 54, 128, 10, 0)
    draw_footer()
    oled.show()
def bullet_boom():
    for f in range(2):
        oled.fill(0)
        for a in range(0, 360, 30):
            rad = math.radians(a)
            L = 15 if (a // 30) % 2 else 8
            oled.line(6, 32, int(6 + L * math.cos(rad)), int(32 + L * math.sin(rad)), 1)
        disc(6, 32, 4 - f * 2)
        oled.show(); all_on(); time.sleep_ms(40)
        oled.fill(0); oled.show(); all_off(); time.sleep_ms(25)
    x = -10
    while x < 92:
        oled.fill(0)
        oled.fill_rect(x, 31, 7, 3, 1)
        oled.line(x - 18, 32, x, 32, 1)
        oled.line(x - 11, 31, x, 31, 1)
        oled.line(x - 11, 33, x, 33, 1)
        oled.pixel(x - 22, 30, 1)
        oled.pixel(x - 27, 34, 1)
        oled.show()
        x += 10
        time.sleep_ms(11)
    oled.fill(1); oled.show(); all_on(); time.sleep_ms(35)
    cx, cy = 98, 32
    for i in range(11):
        r = 4 + i * 5
        sx = cx + random.randint(-2, 2)
        sy = cy + random.randint(-2, 2)
        oled.fill(0)
        disc(sx, sy, r)
        if i > 3:
            disc(sx, sy, r - 7 - (i - 4), 0)
        for a in range(0, 360, 36):
            rad = math.radians(a + i * 7)
            d = r + 6 + i
            oled.pixel(int(sx + d * math.cos(rad)), int(sy + d * math.sin(rad) * 0.8), 1)
        oled.show()
        all_on() if i % 2 == 0 else all_off()
        time.sleep_ms(26)
    all_off()
def new_best(reaction):
    num = str(reaction)
    bullet_boom()
    for i in range(9):
        r = 4 + i * 8
        oled.fill(0)
        for a in range(0, 360, 30):
            rad = math.radians(a + i * 4)
            L = r if (a // 30) % 2 else r * 0.6
            oled.line(64, 32, int(64 + L * math.cos(rad)), int(32 + L * math.sin(rad)), 1)
        disc(64, 32, 8 - i)
        oled.show()
        time.sleep_ms(25)
    nw = len(num) * 24
    x = (128 - (nw + 4 + 16)) // 2
    for i in range(6):
        oled.fill(0)
        oled.text("NEW BEST!", 28, 2)
        scaled_text(num, x, 22, 3)
        oled.text("ms", x + nw + 4, 34)
        if i < 4:
            r = 60 - i * 14
            for a in range(0, 360, 24):
                rad = math.radians(a)
                oled.pixel(int(64 + r * math.cos(rad)), int(32 + r * math.sin(rad) * 0.8), 1)
        for _ in range(12 - i * 2):
            oled.pixel(random.randint(0, 127), random.randint(0, 63), 1)
        oled.show()
        time.sleep_ms(100)
    oled.fill(0)
    oled.rect(0, 0, 128, 64, 1)
    oled.text("NEW BEST!", 28, 6)
    scaled_text(num, x, 26, 3)
    oled.text("ms", x + nw + 4, 38)
    oled.show()
# boot: if on battery, flash the onboard LED once to show it
if not on_usb():
    led_onboard.on()
    time.sleep_ms(1000)
    led_onboard.off()

# ---------- main loop ----------
while True:
    all_off()
    if battery_critical():                 # boot-on-low and mid-session both land here
        low_battery_shutdown()             # blinks warning, then deepsleeps (no return)
    time_of_press = 0
    time_of_start = 0
    false_start = False
    hold_ms = random.randint(200, 3000)
    show_ready()
    while not button_pressed():             # wait for the player to start the round
        time.sleep_ms(10)
    while button_pressed():                 # wait for release so it isn't a false start
        time.sleep_ms(10)
    time.sleep_ms(80)

    show_center("get ready....")
    beep_start()                          # <-- light buzz at round start
    button.irq(handler=press_detected, trigger=pin.IRQ_RISING)
    sequence(hold_ms)
    if false_start:
        button.irq(handler=None)
        buzz_false()                      # <-- error buzz
        false_start_anim()
        time.sleep_ms(POST_ROUND_MS)
        continue
    while time_of_press == 0:
        pass
    button.irq(handler=None)
    reaction = time.ticks_diff(time_of_press, time_of_start)
    if reaction < BEST:
        BEST = reaction
        horn_newbest()                    # <-- happy fanfare
        played = False
        if cinema is not None:
            played = cinema.play(reaction, best=BEST, only_under=CINEMA_UNDER,
                                 abort=button_pressed)
        if not played:
            new_best(reaction)
    else:
        show_result(reaction)
    time.sleep_ms(POST_ROUND_MS)
