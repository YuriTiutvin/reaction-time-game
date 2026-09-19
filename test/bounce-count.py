from machine import Pin
from array import array
import time

PIN_LATCH = 6     # 74HC00 pin 3, Q  (clean)
PIN_RAW   = 7    # 74HC00 pin 1, S_bar / NO contact (raw, bouncy)

latch = Pin(PIN_LATCH, Pin.IN)               # HC push-pull output; no pull needed
raw   = Pin(PIN_RAW,   Pin.IN, Pin.PULL_UP)  # parallels the external 10k, harmless


def count_compare(window_ms=40):

    edges = array('i', [0, 0])   # [raw, latch]

    def raw_cb(p):   edges[0] += 1
    def latch_cb(p): edges[1] += 1

    raw.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=raw_cb)
    latch.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=latch_cb)
    print("Press the button. Ctrl-C to stop.\n")
    try:
        while True:
            edges[0] = 0
            edges[1] = 0
            while edges[0] == 0 and edges[1] == 0:    # wait for a press to start
                time.sleep_ms(1)
            time.sleep_ms(window_ms)                   # let the bounce train finish
            print("raw contact: {:2d} transitions   |   latch Q: {:2d} transition(s)"
                  .format(edges[0], edges[1]))
            while latch.value() == 1:                  # wait for release
                time.sleep_ms(5)
            time.sleep_ms(window_ms)                    # swallow the release bounce
    except KeyboardInterrupt:
        raw.irq(handler=None)
        latch.irq(handler=None)
        print("\nstopped")


if __name__ == "__main__":
    count_compare()
