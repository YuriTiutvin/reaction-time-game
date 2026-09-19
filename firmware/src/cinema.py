"""
cinema.py -- the long-form NEW BEST animation, as a library.

USAGE
-----
    import cinema
    cinema.init(oled, leds, oledi2c, led_bright=LED_BRIGHT)

    if reaction < BEST:
        BEST = reaction
        if not cinema.play(reaction, best=BEST, only_under=100):
            new_best(reaction)           # your own short animation

play(..., abort=fn) polls fn once per frame; when it returns True the show
cuts straight to the settled title card. Pass it a button test and a long
animation stops being something the player has to sit through.

play() returns True only if it actually ran.  When it returns False it has
drawn NOTHING and changed NOTHING, so the caller can fall through to its own
animation with no cleanup and no flicker.  It returns False when:
    * init() was never called, or the panel is not 128x64
    * reaction >= only_under
    * something raised inside the animation (the traceback is printed, the
      game is not killed)

WHAT IT DOES NOT DO
-------------------
It never creates hardware, never touches your I2C object's settings, never
installs an IRQ or a timer, and never reads a global out of your main file.
It makes no assumption about what a "LED" is either: plain Pins, PWM channels
(duty_u16 or duty), or anything with .value() all work, and if yours is
something else entirely, hand init() a led_write(obj, on) of your own.
Everything it needs arrives through init() and play().  On the way out --
including on an exception -- it turns your LEDs off, clears every internal
list, resets the camera, and collects the heap.  Your loop carries on exactly
as before; the only thing left behind is the final title card on the panel,
which is what you wanted anyway.

TUNING  (set any of these from your main file at any time, before play())
------------------------------------------------------------------------
    cinema.SPEED  = 2.0     runtime knob. 1.0 = full length, 2.0 = half the
                            frames with every move scaled to match. Run
                            cinema.bench() and it prints the number to use.
    cinema.LED_BRIGHT       PWM duty used when a LED is "on". Ignored for
                            plain digital pins. Pass led_bright= to init()
                            or set it here; match whatever your game uses.
    cinema.ACTS   = (...)   which acts play; they are independent. Try
                            ("tank", "glass") for a short cut.
    cinema.PACE   = 1.0     frame-period FLOOR multiplier. This can only ever
                            slow things DOWN -- use SPEED to speed up.
    cinema.FRAME_MS = 33    that floor, in ms.
    cinema.FAST_SHOW = True use a non-allocating panel push where possible.

A note on the I2C clock: this library deliberately does not touch it, because
re-initialising a shared bus object mid-program is a good way to break the
rest of your code.  If you want the animation smoother, raise it in your own
constructor -- freq=1000000 instead of 400000 roughly halves the time every
frame spends on the wire, and your normal screens get faster too.  SSD1306 is
specced to 400 kHz, so if the panel starts showing torn rows, put it back.
"""
import time, random, math, gc
import framebuf
from array import array

# --- knobs -------------------------------------------------------------------
SPEED = 2.0
FRAME_MS = 33
PACE = 1.0
FAST_SHOW = True
ACTS = ("launch", "space", "impact", "tank", "glass")
LED_BRIGHT = 20000        # PWM duty for "on"; irrelevant for digital pins

# --- bound by init(); nothing here touches hardware until then ---------------
oled = None
LEDS = ()
HAS_ELL = False
HAS_POLY = False
_READY = False
_CANFAST = False
_ADDR = 0x3C
_VLIST = None
_SBUF = None
_FOOTER = ""
_ABORT = None
_ABORT_AT = 0
_OVERLAY = None
_CUE = None
_LEDW = None


class _Skip(Exception):
    """Raised by flip() when the caller's abort test fires."""
    pass


def _make_led_writer(objs, custom):
    """Work out how to turn one of the caller's LEDs on and off. Nothing in
    this library should care whether they are Pins, PWM channels or something
    invented last week."""
    if custom is not None:
        return custom
    if not objs:
        return None
    o = objs[0]
    if hasattr(o, "duty_u16"):                      # PWM, rp2 / esp32 style
        def w(l, on):
            l.duty_u16(LED_BRIGHT if on else 0)
        return w
    if hasattr(o, "duty"):                          # PWM, 10-bit legacy API
        def w(l, on):
            l.duty(int(LED_BRIGHT * 1023 / 65535) if on else 0)
        return w
    if hasattr(o, "on") and hasattr(o, "off"):      # plain digital Pin
        def w(l, on):
            if on:
                l.on()
            else:
                l.off()
        return w
    if hasattr(o, "value"):                         # anything else with value()
        def w(l, on):
            l.value(1 if on else 0)
        return w
    return None                                     # unknown -> leave them alone


def init(display, leds=None, i2c=None, led_bright=None, led_write=None):
    """Bind the panel and (optionally) the LED list. Returns True if this
    library can drive them. Call it once at boot, right after you create the
    display -- the capability probe writes to a scratch buffer, not to yours,
    so it is safe to call at any time, but there is no reason to call it twice.

    leds        : any sequence of LED-ish objects, any length including none.
    i2c         : accepted only so fast_show() can address the panel directly;
                  its settings are never modified.
    led_bright  : PWM duty for "on". Match your game's, or the animation will
                  be brighter than the rest of your UI (and draw more current).
    led_write   : escape hatch -- your own write_fn(led_obj, on_bool)."""
    global oled, LEDS, HAS_ELL, HAS_POLY, _READY, _CANFAST, _ADDR, _VLIST, _SBUF
    global _i2c, LED_BRIGHT, _LEDW
    oled = display
    LEDS = tuple(leds) if leds else ()
    _i2c = i2c
    if led_bright is not None:
        LED_BRIGHT = led_bright
    _LEDW = _make_led_writer(LEDS, led_write)
    if getattr(display, "width", 0) != 128 or getattr(display, "height", 0) != 64:
        _READY = False
        return False
    # probe framebuf capabilities on a scratch buffer, never on the panel
    pr = framebuf.FrameBuffer(bytearray(8), 8, 8, framebuf.MONO_VLSB)
    try:
        pr.ellipse(4, 4, 2, 2, 1); HAS_ELL = True
    except Exception:
        HAS_ELL = False
    try:
        pr.poly(0, 0, array('h', [0, 0, 4, 0, 4, 4]), 1, True); HAS_POLY = True
    except Exception:
        HAS_POLY = False
    # fast_show only for a driver we actually recognise: an SH1106 has a
    # different column offset and would come out shifted by two pixels.
    _CANFAST = ("SSD1306" in type(display).__name__
                and hasattr(display, "write_cmd")
                and hasattr(display, "buffer")
                and i2c is not None)
    if _CANFAST:
        _ADDR = getattr(display, "addr", 0x3C)
        _VLIST = (b"\x40", display.buffer)
        _SBUF = bytearray(1025)
        _SBUF[0] = 0x40
    _READY = True
    return True


def ready():
    return _READY


def _cue(name):
    """Tell the caller a beat just landed. Never lets their handler take the
    animation down with it."""
    if _CUE is None:
        return
    try:
        _CUE(name)
    except Exception:
        pass


def free():
    """Drop everything the animation allocated. play() calls this for you."""
    fx_reset()
    del MESH[:]; del JIT[:]; del SHARD[:]
    _reset_state()
    gc.collect()


def _reset_state():
    global CAMX, CAMY, CAMZ, SHK, _A, _BX, _BY, _SX, _SY, BAR, _fxacc, _tprev
    CAMX = 64.0; CAMY = 32.0; CAMZ = 1.0; SHK = 0.0
    _A = 1.0; _BX = 0.0; _BY = 0.0; _SX = 0.0; _SY = 0.0
    BAR = 0; _fxacc = 0.0
    _tprev = time.ticks_ms()


def play(reaction, best=None, only_under=0, footer=None, abort=None,
         grace_ms=700, overlay=None, cue=None):
    """Run the cinematic and finish on the title card. See module docstring.

    abort    : callable polled once a frame; True cuts to the title card.
    grace_ms : how long to ignore it after the start, so the bounce from the
               button press that just won the round cannot skip the reward.
    overlay  : optional no-arg callable drawn onto the final title card just
               before it is pushed -- battery and mute icons, say. Called once,
               never per frame, so it can afford to read an ADC.
    cue      : optional callable(name) fired at the dramatic beats --
               'launch', 'impact', 'reveal', 'fire', 'shatter', 'title'. This
               library owns no buzzer; hook your own sounds here."""
    global _FOOTER, _ABORT, _ABORT_AT, _OVERLAY, _CUE
    if not _READY:
        return False
    if only_under and reaction >= only_under:
        return False
    if footer is not None:
        _FOOTER = footer
    elif best is not None:
        _FOOTER = "BEST: " + str(best)
    else:
        _FOOTER = ""
    _reset_state()
    gc.collect()
    _ABORT = abort
    _OVERLAY = overlay
    _CUE = cue
    _ABORT_AT = time.ticks_add(time.ticks_ms(), grace_ms)
    try:
        try:
            cinematic()
            sh_title(reaction)
        except _Skip:
            pass
        title_card(reaction)          # same last frame either way
        return True
    except Exception as e:
        try:
            import sys
            sys.print_exception(e)
        except Exception:
            print("cinema failed:", e)
        return False
    finally:
        # This block is the boundary between "the animation went wrong" and
        # "the game went wrong". Nothing in it is allowed to raise.
        _ABORT = None
        _OVERLAY = None
        _CUE = None
        try:
            all_off()
        except Exception:
            pass
        try:
            free()
        except Exception:
            pass
# ============================================================================
#  FAST MATH   (RP2040 has no FPU -- tables beat math.sin every time)
# ============================================================================
NA = 64                                     # angle units in a full turn
SIN = tuple([math.sin(6.2831853 * i / NA) for i in range(NA)])
COS = tuple([math.cos(6.2831853 * i / NA) for i in range(NA)])


def rnd(a, b):
    return random.uniform(a, b)


def ri(a, b):
    return random.randint(a, b)


def clamp(v, a, b):
    return a if v < a else (b if v > b else v)


def F(n):
    """Frames for a beat, scaled by SPEED. Shots divide the beat's nominal
    length by this to get how far to advance per frame, so cutting frames
    speeds the action up instead of playing it in slow motion."""
    m = int(n / SPEED + 0.5)
    return 2 if m < 2 else m


# ============================================================================
#  FRAME PACING + LED PUNCH
# ============================================================================
_tprev = 0


# These three go through the writer init() worked out, and are wrapped so a
# LED that turns out not to behave as expected can never take the game down --
# all_off() in particular runs from play()'s finally block.
def all_off():
    if _LEDW is None:
        return
    try:
        for l in LEDS:
            _LEDW(l, False)
    except Exception:
        pass


def all_on():
    if _LEDW is None:
        return
    try:
        for l in LEDS:
            _LEDW(l, True)
    except Exception:
        pass


def led_bar(n):
    """Light-meter for on-screen brightness events. n is on a 0-5 scale and is
    rescaled to however many LEDs init() was handed, so a 3-LED or 8-LED build
    works without touching anything."""
    if _LEDW is None:
        return
    m = len(LEDS)
    if not m:
        return
    k = (n * m + 2) // 5
    try:
        for i in range(m):
            _LEDW(LEDS[i], i < k)
    except Exception:
        pass


def flip(hold=0):
    """Push the frame, then wait out the rest of the period FLOOR if there is
    any left. On a board that cannot hit FRAME_MS there never is -- which is
    why PACE cannot make anything faster. Use SPEED for that.

    Also the one place the caller's abort test is polled, so a skip lands
    within a frame no matter which shot is running."""
    global _tprev
    show()
    tgt = int((FRAME_MS + hold) * PACE / SPEED)
    d = time.ticks_diff(time.ticks_ms(), _tprev)
    if d < tgt:
        time.sleep_ms(tgt - d)
    _tprev = time.ticks_ms()
    if _ABORT is not None and time.ticks_diff(_tprev, _ABORT_AT) > 0 and _ABORT():
        raise _Skip


def beat(ms):
    time.sleep_ms(int(ms * PACE / SPEED))


# ============================================================================
#  DISPLAY PUSH
#  Several ssd1306.py builds do  i2c.writeto(addr, b'\x40' + buf)  which
#  allocates a fresh 1025-byte object every single frame. At 30 fps that is
#  ~30 kB/s of garbage on a 200 kB heap, so the GC starts stealing whole
#  frames. This path reuses one buffer and never allocates. It sets the same
#  address window the stock driver does, so oled.show() still works normally
#  afterwards -- nothing is left in a modified state.
# ============================================================================
_i2c = None


def fast_show():
    oled.write_cmd(0x21); oled.write_cmd(0); oled.write_cmd(127)
    oled.write_cmd(0x22); oled.write_cmd(0); oled.write_cmd(7)
    try:
        _i2c.writevto(_ADDR, _VLIST)
    except AttributeError:
        _SBUF[1:] = oled.buffer
        _i2c.writeto(_ADDR, _SBUF)


def show():
    if FAST_SHOW and _CANFAST:
        fast_show()
    else:
        oled.show()


# ============================================================================
#  CAMERA
#  World space -> screen. Everything an object draws goes through here, so a
#  shot change is just three numbers, and shake/zoom cost nothing extra.
# ============================================================================
CAMX = 64.0
CAMY = 32.0
CAMZ = 1.0
SHK = 0.0                # shake amplitude, decays every frame
_A = 1.0
_BX = 0.0
_BY = 0.0
_SX = 0.0
_SY = 0.0
HZY = 50.0               # world y of the horizon; parallax layers pivot here
ROLL = 0                 # retired: the dutch angle cost a branch in the
                         # hottest function in the program for very little
_RC = 1.0
_RS = 0.0


def cam(x, y, z):
    global CAMX, CAMY, CAMZ
    CAMX = x; CAMY = y; CAMZ = z


def cam_to(x, y, z, t):
    """Ease toward a target. t=0..1 per frame; 0.12 is a slow luxurious move.
    Scaled by SPEED so a move still completes in the same fraction of its
    shot when the shot has fewer frames."""
    global CAMX, CAMY, CAMZ
    t = t * SPEED
    if t > 0.85:
        t = 0.85
    CAMX += (x - CAMX) * t
    CAMY += (y - CAMY) * t
    CAMZ += (z - CAMZ) * t


def shake(a):
    global SHK
    if a > SHK:
        SHK = a


def frame_begin(clear=1):
    """Recompute the world->screen transform for this frame."""
    global _A, _BX, _BY, _SX, _SY, SHK
    if SHK > 0.25:
        _SX = rnd(-SHK, SHK); _SY = rnd(-SHK, SHK)
        SHK *= 0.80
    else:
        _SX = 0.0; _SY = 0.0; SHK = 0.0
    _A = CAMZ
    _BX = 64.0 - CAMX * CAMZ + _SX
    _BY = 32.0 - CAMY * CAMZ + _SY
    if clear:
        oled.fill(0)


def layer(p):
    """Parallax: p<1 pushes a layer into the distance (moves less, pivots on
    the horizon).  p>1 pulls it in front of the action."""
    global _BX, _BY
    _BX = 64.0 - (CAMX * p) * _A + _SX
    _BY = 32.0 - (HZY + (CAMY - HZY) * p) * _A + _SY


def layer0():
    global _BX, _BY
    _BX = 64.0 - CAMX * _A + _SX
    _BY = 32.0 - CAMY * _A + _SY


def SX(x, y):
    return int(x * _A + _BX)


def SY(x, y):
    return int(y * _A + _BY)


# ---- world-space primitives ------------------------------------------------
# Every one of these inlines the transform rather than calling SX/SY. Four
# saved Python calls per line, times ~150 primitives a frame, is worth more
# than any single drawing change in here.
def wpix(x, y, c=1):
    oled.pixel(int(x * _A + _BX), int(y * _A + _BY), c)


def wline(x1, y1, x2, y2, c=1):
    a = _A; bx = _BX; by = _BY
    oled.line(int(x1 * a + bx), int(y1 * a + by),
              int(x2 * a + bx), int(y2 * a + by), c)


def wbar(x1, y1, x2, y2, hw, c=1):
    """A limb / strut: a quad of world half-width hw, so it thickens as the
    camera pushes in instead of staying a hairline."""
    q = _A; qx = _BX; qy = _BY
    a = int(x1 * q + qx); b = int(y1 * q + qy)
    d = int(x2 * q + qx); e = int(y2 * q + qy)
    dx = d - a; dy = e - b
    L2 = dx * dx + dy * dy
    if L2 < 0.6:
        return
    L = L2 ** 0.5
    h = hw * _A
    if h < 0.7:
        h = 0.7
    ux = -dy / L * h; uy = dx / L * h
    spoly((a + ux, b + uy, d + ux, e + uy, d - ux, e - uy, a - ux, b - uy), c, True)


def wfrect(x, y, w, h, c=1):
    q = _A; a = int(x * q + _BX); b = int(y * q + _BY)
    ww = int((x + w) * q + _BX) - a; hh = int((y + h) * q + _BY) - b
    if ww < 1: ww = 1
    if hh < 1: hh = 1
    oled.fill_rect(a, b, ww, hh, c)


def wrect(x, y, w, h, c=1):
    q = _A; a = int(x * q + _BX); b = int(y * q + _BY)
    ww = int((x + w) * q + _BX) - a; hh = int((y + h) * q + _BY) - b
    if ww < 1: ww = 1
    if hh < 1: hh = 1
    oled.rect(a, b, ww, hh, c)


def fcirc(cx, cy, r, c=1):
    """Screen-space filled circle."""
    r = int(r)
    if r < 1:
        oled.pixel(int(cx), int(cy), c); return
    if HAS_ELL:
        oled.ellipse(int(cx), int(cy), r, r, c, True)
    else:
        cx = int(cx); cy = int(cy)
        for yy in range(-r, r + 1):
            dx = int((r * r - yy * yy) ** 0.5)
            oled.hline(cx - dx, cy + yy, 2 * dx + 1, c)


def circ(cx, cy, r, c=1):
    r = int(r)
    if r < 1:
        oled.pixel(int(cx), int(cy), c); return
    if HAS_ELL:
        oled.ellipse(int(cx), int(cy), r, r, c, False)
    else:
        cx = int(cx); cy = int(cy); n = 8 + r
        for i in range(n):
            a = 6.2831853 * i / n
            oled.pixel(int(cx + r * math.cos(a)), int(cy + r * math.sin(a)), c)


def ell(cx, cy, rx, ry, c=1, f=False):
    rx = int(rx); ry = int(ry)
    if rx < 1: rx = 1
    if ry < 1: ry = 1
    if HAS_ELL:
        oled.ellipse(int(cx), int(cy), rx, ry, c, f)
    else:
        n = 10 + rx
        px = int(cx + rx); py = int(cy)
        for i in range(1, n + 1):
            a = 6.2831853 * i / n
            nx = int(cx + rx * math.cos(a)); ny = int(cy + ry * math.sin(a))
            oled.line(px, py, nx, ny, c); px = nx; py = ny


def wfcirc(x, y, r, c=1):
    q = _A
    fcirc(int(x * q + _BX), int(y * q + _BY), r * q, c)


def wcirc(x, y, r, c=1):
    q = _A
    circ(int(x * q + _BX), int(y * q + _BY), r * q, c)


# Exact-size int16 buffers, allocated once per polygon size we ever use, so
# the hot path never allocates and never leans on memoryview slicing.
_PB = {}


def _pb(n):
    b = _PB.get(n)
    if b is None:
        b = array('h', [0] * n)
        _PB[n] = b
    return b


def _fill_span(pts, n, c):
    """Scanline fill -- only used if the firmware has no framebuf.poly()."""
    ymin = 64; ymax = -1
    for i in range(1, n, 2):
        v = int(pts[i])
        if v < ymin: ymin = v
        if v > ymax: ymax = v
    if ymin < 0: ymin = 0
    if ymax > 63: ymax = 63
    m = n >> 1
    for y in range(ymin, ymax + 1):
        xs = []
        for i in range(m):
            ax = pts[i * 2]; ay = pts[i * 2 + 1]
            j = (i + 1) % m
            bx = pts[j * 2]; by = pts[j * 2 + 1]
            if ay == by:
                continue
            if (ay <= y < by) or (by <= y < ay):
                xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
        xs.sort()
        k = 0
        while k + 1 < len(xs):
            x0 = int(xs[k]); x1 = int(xs[k + 1])
            oled.hline(x0, y, x1 - x0 + 1, c)
            k += 2


def spoly(pts, c=1, f=False):
    """Screen-space polygon from a flat list [x0,y0,x1,y1,...]."""
    n = len(pts)
    if n < 6:
        return
    if HAS_POLY:
        b = _pb(n)
        for i in range(n):
            b[i] = int(pts[i])
        oled.poly(0, 0, b, c, f)
    elif f:
        _fill_span(pts, n, c)
    else:
        for i in range(0, n, 2):
            j = (i + 2) % n
            oled.line(int(pts[i]), int(pts[i + 1]), int(pts[j]), int(pts[j + 1]), c)


def wpoly(pts, c=1, f=False):
    """World-space polygon from [(x,y),...]."""
    q = _A; qx = _BX; qy = _BY
    o = []
    for p in pts:
        o.append(p[0] * q + qx); o.append(p[1] * q + qy)
    spoly(o, c, f)


# ---- tone: 1-bit has no grey, so we fake three of them ---------------------
def stripe(x, y, w, h, step=2, c=1, ph=0):
    """Horizontal-line dither. step 2 ~= 50%, 3 ~= 33%, 4 ~= 25%."""
    yy = int(y) + ph
    y1 = int(y + h)
    if yy < 0: yy = ph
    while yy < y1:
        if yy >= 64: break
        oled.hline(int(x), yy, int(w), c)
        yy += step


def noise(x, y, w, h, n, c=1):
    """Stochastic dots -- reads as dust, smoke, static."""
    x = int(x); y = int(y); w = int(w); h = int(h)
    if w < 1 or h < 1:
        return
    for _ in range(n):
        oled.pixel(x + ri(0, w - 1), y + ri(0, h - 1), c)


RAG = (1.00, 0.80, 1.16, 0.88, 1.09, 0.76, 1.19, 0.93, 1.03, 0.84, 1.12, 0.97)


def blob(cx, cy, r, seed=0, f=False, c=1, sq=0.88):
    """One ragged closed shape -- a single poly call. Smoke, fire, dirt."""
    pts = []
    for i in range(10):
        a = (i * 32) // 5
        k = r * RAG[(i + seed) % 12]
        pts.append(cx + COS[a] * k)
        pts.append(cy + SIN[a] * k * sq)
    spoly(pts, c, f)


def puff(cx, cy, r, dens=1.0, c=1):
    """Smoke: ragged rim plus a little interior grain. Cheap on purpose."""
    if r < 1.6:
        return
    blob(cx, cy, r, (int(cx) >> 1) + int(cy), False, c)
    n = int(r * 0.8 * dens)
    for _ in range(n):
        a = ri(0, NA - 1); d = rnd(0.15, 0.80) * r
        oled.pixel(int(cx + COS[a] * d), int(cy + SIN[a] * d * 0.88), c)


def dim(step=2, ph=1):
    """Knock the whole frame down a stop by erasing alternate rows.
    The only honest way to get a mid-tone out of one bit."""
    yy = ph
    while yy < 64:
        oled.hline(0, yy, 128, 0)
        yy += step


def plate(pts):
    """Black keep-out under a subject so background line-art stops at its
    edge instead of running through it. This is what makes 1-bit read."""
    wpoly(pts, 0, True)


# ---- letterbox + overlays --------------------------------------------------
BAR = 0


def bars():
    if BAR > 0:
        oled.fill_rect(0, 0, 128, BAR, 0)
        oled.fill_rect(0, 64 - BAR, 128, BAR, 0)


def vignette_flash(k):
    """k=0..1 white blast from the edges inward."""
    if k <= 0:
        return
    d = int(32 * k)
    oled.fill_rect(0, 0, 128, d, 1)
    oled.fill_rect(0, 64 - d, 128, d, 1)
    oled.fill_rect(0, 0, d * 2, 64, 1)
    oled.fill_rect(128 - d * 2, 0, d * 2, 64, 1)


def whiteout(ms=70, lvl=5):
    oled.fill(1); oled.show(); led_bar(lvl); beat(ms)


# ---- background bake: draw a static layer once, blit it every frame --------
_bgbuf = bytearray(1024)
BG = framebuf.FrameBuffer(_bgbuf, 128, 64, framebuf.MONO_VLSB)


def bake():
    """Copy what is on screen into the background cache."""
    BG.blit(oled, 0, 0)


def paste(dx=0, dy=0):
    """Restore the cache (optionally offset -- free camera shake)."""
    if dx or dy:
        oled.fill(0)
    oled.blit(BG, int(dx), int(dy))

# ============================================================================
#  WORLD / TERRAIN
#  Design rule for a 1-bit 128x64 panel: the world is line-art, the SUBJECT is
#  the only solid mass in frame. That is what makes it readable.
# ============================================================================
GROUND = 50.0
# Far range peaks high in the sky. Near dunes are LOW and are filled solid
# black -- they cut the far range off so the band the actors stand in is clean.
FAR_RIDGE = ((-40, 47), (-16, 27), (6, 41), (22, 22), (40, 39), (58, 18),
             (76, 35), (94, 24), (112, 40), (130, 26), (150, 38), (174, 29),
             (200, 45))
MID_RIDGE = ((-44, 51), (-10, 45), (16, 49), (44, 43), (70, 48), (96, 42),
             (122, 47), (152, 44), (200, 51))
DUST = tuple([(ri(-40, 200), rnd(51.0, 62), rnd(0.3, 1.0)) for _ in range(12)])
STARS = tuple([(ri(0, 127), ri(0, 63), ri(1, 3)) for _ in range(40)])
# ground marks get longer and further apart toward the bottom = perspective
GRIT = tuple([(ri(-40, 200), 51.5 + b * 3.6 + rnd(-0.6, 0.6), 2 + b * 2)
              for b in range(4) for _ in range(6)])


def draw_stars(n, tw=0):
    k = 0
    for (sx, sy, b) in STARS:
        if k >= n:
            break
        k += 1
        if b == 3 and tw:
            if (tw + sx) & 7 < 4:
                oled.pixel(sx, sy, 1)
                oled.pixel(sx + 1, sy, 1); oled.pixel(sx - 1, sy, 1)
                oled.pixel(sx, sy + 1, 1); oled.pixel(sx, sy - 1, 1)
                continue
        oled.pixel(sx, sy, 1)
        if b >= 2:
            oled.pixel(sx + 1, sy, 1)


def draw_sun(t=0):
    layer(0.10)
    cx = SX(96, 16); cy = SY(96, 16)
    fcirc(cx, cy, 5 * _A if _A > 1 else 5, 1)
    r0 = int(7 * (1 if _A < 1 else _A))
    for i in range(0, NA, 8):
        j = (i + (t >> 3)) & (NA - 1)
        L = r0 + 4 + (2 if (i & 8) else 0)
        oled.line(int(cx + COS[j] * r0), int(cy + SIN[j] * r0),
                  int(cx + COS[j] * L), int(cy + SIN[j] * L), 1)


def draw_clouds():
    layer(0.18)
    for (cx, cy, w) in ((26, 14, 9), (58, 9, 6), (112, 20, 7)):
        x = SX(cx, cy); y = SY(cx, cy)
        s = _A * 0.6 + 0.4
        ell(x, y, int(w * s), int(2.4 * s), 1, True)
        ell(x - int(w * 0.5 * s), y + int(1 * s), int(w * 0.5 * s), int(1.8 * s), 1, True)
        ell(x + int(w * 0.45 * s), y, int(w * 0.4 * s), int(1.6 * s), 1, True)


def draw_birds(t):
    layer(0.30)
    for i in range(3):
        bx = 30 + i * 17 + ((t + i * 9) % 90) * 0.35
        by = 12 + i * 4 + (2 if ((t >> 2) + i) & 1 else 0)
        x = SX(bx, by); y = SY(bx, by)
        oled.line(x - 2, y + 1, x, y, 1)
        oled.line(x, y, x + 2, y + 1, 1)


def draw_far():
    layer(0.34)
    p = FAR_RIDGE
    for i in range(len(p) - 1):
        wline(p[i][0], p[i][1], p[i + 1][0], p[i + 1][1], 1)


def draw_mid():
    layer(0.62)
    p = MID_RIDGE
    m = []
    for q in p:
        m.append(q)
    m.append((200, 80)); m.append((-44, 80))
    wpoly(m, 0, True)                       # solid black mass, hides the range
    for i in range(len(p) - 1):
        wline(p[i][0], p[i][1], p[i + 1][0], p[i + 1][1], 1)


def draw_ground(t=0):
    layer0()
    y = SY(0, GROUND)
    if -2 < y < 66:
        oled.hline(0, y, 128, 1)
        oled.hline(0, y + 1, 128, 1)
    for (gx, gy, gl) in GRIT:
        a = SX(gx, gy); b = SY(gx, gy)
        if -1 < b < 64:
            oled.hline(a, b, int(gl * _A) + 1, 1)
    for (gx, gy, s) in DUST:
        wpix(gx + ((t * s) % 26) - 13, gy, 1)


def draw_rock(x, y, w, h):
    wpoly(((x, y), (x + w * 0.22, y - h), (x + w * 0.62, y - h * 0.8),
           (x + w, y - h * 0.15), (x + w * 0.8, y)), 1, True)


def draw_cactus(x, y, s=1.0):
    wfrect(x - 0.9 * s, y - 15 * s, 1.9 * s, 15 * s, 1)
    wfrect(x - 5 * s, y - 11 * s, 1.6 * s, 4 * s, 1)
    wfrect(x - 5 * s, y - 11 * s, 4 * s, 1.6 * s, 1)
    wfrect(x + 3.4 * s, y - 13 * s, 1.6 * s, 5 * s, 1)
    wfrect(x + 1.8 * s, y - 8.6 * s, 3.2 * s, 1.6 * s, 1)


def draw_bush(x, y, s=1.0):
    wfcirc(x, y - 2 * s, 2.4 * s)
    wfcirc(x + 3.4 * s, y - 3 * s, 2.6 * s)
    wfcirc(x + 6.6 * s, y - 1.8 * s, 2.2 * s)
    wfrect(x - 2 * s, y - 1.4 * s, 11 * s, 1.6 * s)


def fg_scrub(t=0):
    """Foreground silhouettes -- solid, oversized, parallax > 1. Sells depth."""
    layer(1.30)
    draw_bush(-8, GROUND + 6, 1.9)
    draw_rock(104, GROUND + 7, 24, 8)
    layer0()


# ============================================================================
#  SHELL  (solid, with a nose, driving band and fins -- rotatable)
# ============================================================================
def draw_shell(x, y, ang, s=1.0, c=1, ko=1):
    a = int(ang) & (NA - 1)
    ca = COS[a]; sa = SIN[a]

    def P(dx, dy):
        return (x + (dx * ca - dy * sa) * s, y + (dx * sa + dy * ca) * s)

    if ko and c:
        k = s * 1.0 + 1.3 / (_A if _A > 0.4 else 0.4)

        def Q(dx, dy):
            return (x + (dx * ca - dy * sa) * k, y + (dx * sa + dy * ca) * k)

        wpoly((Q(4.4, 0), Q(2.6, -1.7), Q(-4.8, -3.2), Q(-4.8, 3.2), Q(2.6, 1.7)),
              0, True)
    wpoly((P(4.2, 0), P(2.4, -1.5), P(-2.6, -1.8), P(-2.6, 1.8), P(2.4, 1.5)), c, True)
    # fins
    wpoly((P(-2.6, -1.8), P(-4.6, -3.0), P(-4.6, -1.0)), c, True)
    wpoly((P(-2.6, 1.8), P(-4.6, 3.0), P(-4.6, 1.0)), c, True)
    if s * _A > 2.0:                       # detail only when it is big enough
        p1 = P(0.6, -1.7); p2 = P(0.6, 1.7)
        wline(p1[0], p1[1], p2[0], p2[1], 0)
        p1 = P(-1.4, -1.8); p2 = P(-1.4, 1.8)
        wline(p1[0], p1[1], p2[0], p2[1], 0)


def shell_noseon(cx, cy, r, spin=0):
    """The round seen down its own axis -- the only way 'coming straight at
    the camera' reads in 2-D."""
    for k in range(4):
        a = (spin + k * 16) & (NA - 1)
        b = (a + 3) & (NA - 1); c2 = (a - 3) & (NA - 1)
        spoly((cx, cy, cx + COS[b] * r * 2.3, cy + SIN[b] * r * 2.1,
               cx + COS[c2] * r * 2.3, cy + SIN[c2] * r * 2.1), 1, True)
    fcirc(cx, cy, r * 1.22, 0)
    fcirc(cx, cy, r, 1)
    circ(cx, cy, r * 0.66, 0)
    fcirc(cx, cy, r * 0.26, 0)


# ============================================================================
#  SOLDIER + MORTAR
#  Tube is planted at (58,50) and leans up-left; the loader stands to its
#  right and drops the round in from above. Muzzle lands at (48.2, 36.1).
# ============================================================================
MB_X = 58.0                      # baseplate
MUX = -0.26                      # tube unit vector: ~75 deg, near vertical, so
MUY = -0.966                     # the round drops straight in and reads
MTL = 18.0
MZX = MB_X + MUX * MTL           # 48.2
MZY = GROUND + MUY * MTL         # 36.1
SOL_X = 65.0


def draw_mortar(recoil=0.0, keepout=1):
    bx = MB_X + MUX * -recoil; by = GROUND + MUY * -recoil
    x1 = bx + MUX * MTL; y1 = by + MUY * MTL
    mx = MB_X + MUX * MTL * 0.58; my = GROUND + MUY * MTL * 0.58
    if keepout:
        px = -MUY * 3.6; py = MUX * 3.6
        plate(((bx + px, by + py + 2), (x1 + px, y1 + py - 2),
               (x1 - px, y1 - py - 2), (bx - px, by - py + 2)))
        plate(((mx, my), (MB_X + 7.5, GROUND + 1), (MB_X - 6.0, GROUND + 1)))
    wbar(bx, by, x1, y1, 1.45)                   # tube
    wfcirc(x1, y1, 2.1)
    wfcirc(x1, y1, 1.05, 0)                      # bore
    wbar(mx, my, MB_X + 7.0, GROUND, 0.55)       # bipod
    wbar(mx, my, MB_X - 6.5, GROUND, 0.55)
    wbar(MB_X - 5.6, GROUND - 1.6, MB_X + 6.2, GROUND - 1.6, 0.35)
    wfrect(MB_X - 5.0, GROUND - 1.8, 10.0, 2.0, 1)
    wline(MB_X - 5.0, GROUND - 1.8, MB_X + 5.0, GROUND - 1.8, 0)


def draw_soldier(pose=0, hold=0, hx=None, hy=None, keepout=1):
    """pose 0 = round held over the muzzle, 1 = arms dropping, 2 = ducked."""
    g = GROUND; s = SOL_X
    if hx is None:
        if pose == 0:
            hx = MZX + 5.4; hy = MZY - 9.0
        else:
            hx = MZX + 2.0; hy = MZY - 1.0
    if pose == 2:
        if keepout:
            plate(((s - 7.0, g + 1.5), (s - 4.0, g - 12.0), (s + 1.0, g - 17.5),
                   (s + 6.5, g - 15.0), (s + 8.0, g - 6.0), (s + 8.0, g + 1.5)))
        wbar(s + 0.5, g - 6.0, s - 3.5, g - 0.3, 1.1)
        wbar(s + 1.5, g - 6.0, s + 5.5, g - 0.6, 1.1)
        wfrect(s + 4.4, g - 1.6, 3.2, 1.7, 1); wfrect(s - 5.2, g - 1.6, 3.2, 1.7, 1)
        wpoly(((s - 2.2, g - 5.0), (s + 2.6, g - 6.0),
               (s + 4.2, g - 11.4), (s - 0.6, g - 10.8)), 1, True)
        hdx = s + 2.8; hdy = g - 13.6
        wfcirc(hdx, hdy, 2.7)
        wpoly(((hdx - 3.6, hdy - 0.9), (hdx + 3.4, hdy - 1.3),
               (hdx + 3.1, hdy - 3.4), (hdx - 3.1, hdy - 3.0)), 1, True)
        wline(hdx - 3.6, hdy - 0.9, hdx + 3.4, hdy - 1.3, 0)
        wbar(s + 1.0, g - 9.0, hdx - 2.4, hdy - 0.2, 0.9)      # hands on ears
        wbar(s + 2.8, g - 9.2, hdx + 2.6, hdy - 0.2, 0.9)
        return
    if keepout:
        plate(((s - 7.5, g + 1.5), (s - 7.5, g - 14.0), (s - 4.5, g - 21.5),
               (s + 3.0, g - 21.5), (s + 5.5, g - 14.0), (s + 8.0, g + 1.5)))
        ex = s - 4.0; ey = g - 24.0
        plate(((s - 3.0, g - 16.5), (ex - 2.0, ey - 3.0), (hx + 3.5, hy - 4.0),
               (hx + 3.5, hy + 4.0), (ex + 1.5, ey + 3.5), (s - 3.0, g - 9.5)))
    wbar(s - 0.5, g - 8.0, s - 4.5, g - 0.4, 1.2)              # braced legs
    wbar(s + 1.5, g - 8.0, s + 5.0, g - 0.4, 1.2)
    wfrect(s + 3.8, g - 1.7, 3.6, 1.8, 1)
    wfrect(s - 6.2, g - 1.7, 3.6, 1.8, 1)
    wpoly(((s - 2.4, g - 6.6), (s + 2.8, g - 6.6),
           (s + 2.2, g - 15.4), (s - 2.8, g - 15.4)), 1, True)  # torso
    wline(s - 2.4, g - 11.2, s + 2.5, g - 11.6, 0)              # webbing
    wline(s - 1.0, g - 15.0, s - 1.0, g - 11.4, 0)
    hdx = s - 0.4; hdy = g - 17.6
    wfcirc(hdx, hdy, 2.3)
    wline(hdx - 2.4, hdy - 0.4, hdx + 2.4, hdy - 0.4, 0)        # brim shadow
    wpoly(((hdx - 4.2, hdy - 1.4), (hdx + 3.8, hdy - 1.4),
           (hdx + 2.8, hdy - 4.0), (hdx - 3.2, hdy - 3.8)), 1, True)
    ex = s - 4.0; ey = g - 24.0                                 # arms, bent
    wbar(s - 1.8, g - 14.0, ex, ey, 1.0)
    wbar(ex, ey, hx + 1.8, hy + 1.0, 1.0)
    wbar(s - 1.2, g - 12.2, ex + 1.4, ey + 2.2, 0.9)
    wbar(ex + 1.4, ey + 2.2, hx + 2.6, hy + 2.8, 0.9)
    if hold:
        draw_shell(hx, hy, 42, 1.15)


# ============================================================================
#  CABIN -- built from the same polygons that later become flying debris
# ============================================================================
CAB_X = 88.0
CAB_PARTS = (
    # (centroid_x, centroid_y, [(dx,dy)...])
    (CAB_X - 13, 42.0, ((-5, -8), (5, -8), (5, 8), (-5, 8))),         # wall L
    (CAB_X + 0.0, 42.0, ((-8, -8), (8, -8), (8, 8), (-8, 8))),        # wall M
    (CAB_X + 13, 42.0, ((-5, -8), (5, -8), (5, 8), (-5, 8))),         # wall R
    (CAB_X - 1.0, 30.5, ((-19, 3.5), (1, -6.5), (21, 3.5))),          # gable
    (CAB_X - 11, 28.5, ((-11, 5), (10, -5), (12, -2.5), (-9, 7.5))),  # roof L
    (CAB_X + 12, 28.5, ((-11, -5), (10, 5), (8, 7.5), (-13, -2.5))),  # roof R
    (CAB_X + 15, 22.0, ((-2.5, -5), (2.5, -5), (2.5, 5), (-2.5, 5))),  # chimney
    (CAB_X - 22, 44.0, ((-1.5, -6), (1.5, -6), (1.5, 6), (-1.5, 6))),  # post L
    (CAB_X + 22, 44.0, ((-1.5, -6), (1.5, -6), (1.5, 6), (-1.5, 6))),  # post R
    (CAB_X, 49.0, ((-24, -1.5), (24, -1.5), (24, 1.5), (-24, 1.5))),  # sill
)


CAB_PLATE = ((CAB_X - 26, GROUND + 2), (CAB_X - 26, 37), (CAB_X - 21, 36),
             (CAB_X - 1, 22.0), (CAB_X + 11.0, 31), (CAB_X + 11.0, 15.5),
             (CAB_X + 19.5, 15.5), (CAB_X + 19.5, 36), (CAB_X + 26, 37),
             (CAB_X + 26, GROUND + 2))


def draw_cabin(dmg=0, keepout=1):
    if keepout:
        plate(CAB_PLATE)
    for p in CAB_PARTS:
        pts = []
        for (dx, dy) in p[2]:
            pts.append((p[0] + dx, p[1] + dy))
        wpoly(pts, 1, True)
    # black negative-space detail: logs, door, window, chimney cap
    for yy in range(36, 50, 3):
        wline(CAB_X - 20, yy, CAB_X + 20, yy, 0)
    wfrect(CAB_X - 3.5, 41.0, 7.0, 9.0, 0)           # door
    wfrect(CAB_X - 2.8, 41.6, 5.6, 8.4, 1)
    wfrect(CAB_X - 2.0, 42.2, 4.0, 7.2, 0)
    wpix(CAB_X + 1.2, 46.0, 1)
    wfrect(CAB_X - 15, 38.0, 6.0, 5.0, 0)            # window
    wfrect(CAB_X - 14.4, 38.6, 4.8, 3.8, 1)
    wline(CAB_X - 12, 38.6, CAB_X - 12, 42.4, 0)
    wline(CAB_X - 14.4, 40.5, CAB_X - 9.6, 40.5, 0)
    wline(CAB_X + 6, 39.5, CAB_X + 6, 33.0, 0)
    wfrect(CAB_X + 12.0, 17.0, 6.0, 1.6, 1)          # chimney cap


def draw_smoke_stack(t):
    x = CAB_X + 15
    for i in range(4):
        yy = 15 - i * 4 - ((t >> 1) % 4)
        puff(SX(x + math.sin((t + i * 7) * 0.12) * 2.2, yy),
             SY(x, yy), (1.4 + i * 0.7) * (_A if _A > 1 else 1), 0.5)

# ============================================================================
#  MAIN BATTLE TANK
#  Hull + running gear are a side elevation. The TURRET is a real 3-D prism
#  projected every frame, so it can traverse and point its gun at the camera --
#  the barrel foreshortens to a bore circle all by itself.
#  Local coords: x 0 (nose) -> 45 (rear), y 0 = ground, up is negative.
# ============================================================================
IDL = (4.0, -4.2, 3.6)
SPR = (41.0, -5.4, 4.0)
BOGIE = (9.5, 16.0, 22.5, 29.0, 35.5)
ROLLER = (13.0, 22.0, 31.0, 38.0)
BAND = 1.8

HULL = ((0.5, -10.4), (2.6, -16.2), (9.5, -19.8), (27.0, -19.8),
        (31.5, -18.2), (45.0, -17.4), (45.5, -10.4))
DECK = ((9.5, -19.8), (27.0, -19.8), (26.0, -22.6), (11.0, -22.6))
EDECK = ((27.0, -19.8), (45.0, -17.4), (43.6, -20.1), (26.0, -22.6))

TURP = ((12.5, -2.4), (12.5, 2.4), (5.0, 6.2), (-8.0, 6.2),
        (-11.5, 3.4), (-11.5, -3.4), (-8.0, -6.2), (5.0, -6.2))
TRING = (20.5, -22.3)            # turret ring, local coords
TURH = 7.0
TF = 78.0                        # virtual camera distance -> perspective
TILT = 0.24                      # how far above the tank we are looking
GL = 31.0                        # gun length past the mantlet


def _belt(shrink=0.0):
    p = []
    r = SPR[2] - shrink
    for i in range(5):                       # sprocket: top -> rear -> bottom
        a = -1.5707963 + 3.1415927 * i / 4.0
        p.append((SPR[0] + r * math.cos(a), SPR[1] + r * math.sin(a)))
    r = IDL[2] - shrink
    for i in range(5):                       # idler: bottom -> front -> top
        a = 1.5707963 + 3.1415927 * i / 4.0
        p.append((IDL[0] + r * math.cos(a), IDL[1] + r * math.sin(a)))
    return p


BELT_O = tuple(_belt(0.0))
BELT_I = tuple(_belt(BAND))


BELT_P = tuple(_belt(-1.6))


def draw_running_gear(ox, oy, ph, detail=2, keepout=1):
    if keepout:
        bp = []
        for (x, y) in BELT_P:
            bp.append((ox + x, oy + y))
        wpoly(bp, 0, True)
    bo = []
    for (x, y) in BELT_O:
        bo.append((ox + x, oy + y))
    wpoly(bo, 1, True)
    bi = []
    for (x, y) in BELT_I:
        bi.append((ox + x, oy + y))
    wpoly(bi, 0, True)
    if detail and _A > 1.05:
        # track links: black ticks marching along both runs
        st = 3.0
        n = int(37.0 / st)
        off = ph % st
        for i in range(n):
            t = (off + i * st) / 37.0
            if t > 1.0:
                continue
            x = IDL[0] + (SPR[0] - IDL[0]) * t
            yt = IDL[1] - IDL[2] + (SPR[1] - SPR[2] - IDL[1] + IDL[2]) * t
            yb = IDL[1] + IDL[2] + (SPR[1] + SPR[2] - IDL[1] - IDL[2]) * t
            wline(ox + x, oy + yt, ox + x, oy + yt + BAND, 0)
            wline(ox + x, oy + yb - BAND, ox + x, oy + yb, 0)
    # road wheels: white rims on the black track void, with a spinning spoke
    a = int(ph * 2.2) & (NA - 1)
    for wx in BOGIE:
        wcirc(ox + wx, oy - 3.6, 3.0)
        if detail:
            wline(ox + wx - COS[a] * 2.2, oy - 3.6 - SIN[a] * 2.2,
                  ox + wx + COS[a] * 2.2, oy - 3.6 + SIN[a] * 2.2, 1)
        wfcirc(ox + wx, oy - 3.6, 0.8)
    wcirc(ox + IDL[0], oy + IDL[1], IDL[2] - 0.6)
    wfcirc(ox + IDL[0], oy + IDL[1], 0.9)
    # drive sprocket with teeth
    wcirc(ox + SPR[0], oy + SPR[1], SPR[2] - 0.7)
    wfcirc(ox + SPR[0], oy + SPR[1], 1.1)
    if detail:
        for k in range(8):
            b = (a + k * 8) & (NA - 1)
            wline(ox + SPR[0] + COS[b] * 2.0, oy + SPR[1] + SIN[b] * 2.0,
                  ox + SPR[0] + COS[b] * 3.6, oy + SPR[1] + SIN[b] * 3.6, 1)
        for rx in ROLLER:
            wfcirc(ox + rx, oy - 10.2, 1.2)
            wpix(ox + rx, oy - 10.2, 0)


HULL_P = ((-1.0, -9.6), (1.4, -17.4), (9.0, -21.4), (27.4, -21.4),
          (31.8, -19.6), (46.6, -18.6), (47.2, -9.6))


def draw_hull(ox, oy, detail=2, keepout=1):
    if keepout:
        pp = []
        for (x, y) in HULL_P:
            pp.append((ox + x, oy + y))
        wpoly(pp, 0, True)
        pp = []
        for (x, y) in DECK:
            pp.append((ox + x - 1.4, oy + y - 1.6))
        pp.append((ox + DECK[3][0] + 2.0, oy + DECK[3][1] - 1.6))
        wpoly(pp, 0, True)
    pts = []
    for (x, y) in HULL:
        pts.append((ox + x, oy + y))
    wpoly(pts, 1, True)
    wfrect(ox + 3.0, oy - 10.6, 36.0, 4.8, 1)              # side skirt
    d = []
    for (x, y) in DECK:
        d.append((ox + x, oy + y))
    wpoly(d, 1, True)
    e = []
    for (x, y) in EDECK:
        e.append((ox + x, oy + y))
    wpoly(e, 1, True)
    if not detail:
        return
    # --- black negative-space detail: this is what stops it being a slab ---
    wline(ox + 9.5, oy - 19.8, ox + 27.0, oy - 19.8, 0)    # deck / side break
    wline(ox + 27.0, oy - 19.8, ox + 45.0, oy - 17.4, 0)
    wline(ox + 3.0, oy - 10.5, ox + 39.0, oy - 10.5, 0)    # fender line
    for jx in (11.0, 19.0, 27.0, 34.0):                    # skirt plate joints
        wline(ox + jx, oy - 10.2, ox + jx, oy - 6.0, 0)
    wline(ox + 2.6, oy - 16.2, ox + 9.5, oy - 19.8, 0) if False else None
    wfrect(ox + 4.2, oy - 14.6, 2.2, 1.6, 0)               # headlight box
    wfrect(ox + 4.4, oy - 14.4, 1.8, 1.2, 1)
    wline(ox + 7.0, oy - 17.6, ox + 9.0, oy - 18.6, 0)     # driver hatch
    wline(ox + 7.0, oy - 17.0, ox + 9.6, oy - 18.3, 0)
    for i in range(5):                                      # engine louvres
        lx = 30.0 + i * 3.0
        wline(ox + lx, oy - 19.0, ox + lx - 1.2, oy - 21.6, 0)
    wfrect(ox + 33.0, oy - 16.6, 8.0, 4.0, 0)              # stowage bin
    wfrect(ox + 33.4, oy - 16.2, 7.2, 3.2, 1)
    wline(ox + 37.0, oy - 16.2, ox + 37.0, oy - 13.0, 0)


def _tp(u, v, w, ox, oy, ca, sa):
    ur = u * ca - v * sa
    vr = u * sa + v * ca
    s = TF / (TF - vr)
    return (ox - ur * s, oy - w * s + vr * TILT, s)


def draw_turret(ox, oy, th, rec=0.0, detail=2, muzzle=0.0, keepout=1):
    """th in NA units: 0 = gun to frame-left, NA/4 = gun aimed at the camera."""
    th = int(th) & (NA - 1)
    ca = COS[th]; sa = SIN[th]
    rx = ox + TRING[0]; ry = oy + TRING[1]
    bot = []
    top = []
    for (u, v) in TURP:
        p = _tp(u, v, 0.0, rx, ry, ca, sa); bot.append((p[0], p[1]))
        p = _tp(u * 0.86, v * 0.86, TURH, rx, ry, ca, sa); top.append((p[0], p[1]))
    n = len(TURP)
    if keepout:
        pp = []
        for (u, v) in TURP:
            p = _tp(u * 1.22, v * 1.30, -1.4, rx, ry, ca, sa)
            pp.append((p[0], p[1]))
        wpoly(pp, 0, True)
        pp = []
        for (u, v) in TURP:
            p = _tp(u * 1.06, v * 1.10, TURH + 1.5, rx, ry, ca, sa)
            pp.append((p[0], p[1]))
        wpoly(pp, 0, True)
    for i in range(n):                                   # sides
        j = (i + 1) % n
        wpoly((bot[i], bot[j], top[j], top[i]), 1, True)
    wpoly(bot, 1, True)
    wpoly(top, 1, True)
    if detail:
        wpoly(top, 0, False)                             # roof edge in black
    # ---- gun ----
    p0 = _tp(11.0 - rec, 0.0, 1.7, rx, ry, ca, sa)
    pe = _tp(11.0 - rec + GL * 0.60, 0.0, 1.7, rx, ry, ca, sa)
    pb = _tp(11.0 - rec + GL * 0.88, 0.0, 1.7, rx, ry, ca, sa)
    p1 = _tp(11.0 - rec + GL, 0.0, 1.7, rx, ry, ca, sa)
    if keepout:
        _tube(p0, p1, 2.5, 0)
        ax = SX(p1[0], p1[1]); ay = SY(p1[0], p1[1])
        fcirc(ax, ay, 3.7 * p1[2] * _A, 0)
    wfcirc(p0[0], p0[1], 3.9 * p0[2])                    # mantlet
    _tube(p0, pb, 1.45)
    _tube(pb, p1, 2.65)                                  # muzzle brake
    wfcirc(pe[0], pe[1], 2.15 * pe[2])                   # bore evacuator
    ax = SX(p1[0], p1[1]); ay = SY(p1[0], p1[1])
    fcirc(ax, ay, 2.65 * p1[2] * _A, 1)
    fcirc(ax, ay, 1.25 * p1[2] * _A, 0)                  # the bore
    if detail:
        _slot(pb, p1, 0.30, 2.6)                         # muzzle-brake ports
        _slot(pb, p1, 0.62, 2.6)
    if not detail:
        return
    # ---- turret furniture ----
    c = _tp(-4.0, 3.4, TURH, rx, ry, ca, sa)             # commander cupola
    cxp = SX(c[0], c[1]); cyp = SY(c[0], c[1])
    fcirc(cxp, cyp - int(1.6 * c[2] * _A), 2.9 * c[2] * _A, 1)
    fcirc(cxp, cyp - int(1.6 * c[2] * _A), 1.9 * c[2] * _A, 0)
    oled.line(cxp - int(2 * c[2] * _A), cyp - int(1.6 * c[2] * _A),
              cxp + int(2 * c[2] * _A), cyp - int(1.6 * c[2] * _A), 1)
    g = _tp(5.0, -4.4, TURH - 0.5, rx, ry, ca, sa)       # gunner's sight
    wfrect(g[0] - 1.4 * g[2], g[1] - 2.4 * g[2], 2.8 * g[2], 2.4 * g[2], 1)
    wfrect(g[0] - 0.9 * g[2], g[1] - 1.9 * g[2], 1.8 * g[2], 1.0 * g[2], 0)
    sl = _tp(7.5, 6.0, 3.2, rx, ry, ca, sa)              # smoke dischargers
    if sl[2] > 0.6:
        wfrect(sl[0] - 2.6 * sl[2], sl[1] - 1.6 * sl[2], 5.2 * sl[2], 2.6 * sl[2], 1)
        for k in range(3):
            wline(sl[0] - 1.4 * sl[2] + k * 1.4 * sl[2], sl[1] - 1.4 * sl[2],
                  sl[0] - 1.4 * sl[2] + k * 1.4 * sl[2], sl[1] + 0.8 * sl[2], 0)
    b1 = _tp(-11.5, 4.0, 1.0, rx, ry, ca, sa)            # stowage basket
    b2 = _tp(-18.0, 4.0, 1.0, rx, ry, ca, sa)
    wpoly(((b1[0], b1[1]), (b2[0], b2[1]),
           (b2[0], b2[1] - 4.4 * b2[2]), (b1[0], b1[1] - 4.4 * b1[2])), 1, False)
    wline((b1[0] + b2[0]) * 0.5, b1[1], (b1[0] + b2[0]) * 0.5,
          b1[1] - 4.4 * b1[2], 1)
    a0 = _tp(-9.0, 5.0, TURH, rx, ry, ca, sa)            # antenna
    wline(a0[0], a0[1], a0[0] + 1.2, a0[1] - 13.0, 1)
    wline(a0[0] + 1.2, a0[1] - 13.0, a0[0] + 2.6, a0[1] - 18.0, 1)


def _tube(pa, pb, hw, c=1):
    ax = SX(pa[0], pa[1]); ay = SY(pa[0], pa[1])
    bx = SX(pb[0], pb[1]); by = SY(pb[0], pb[1])
    dx = bx - ax; dy = by - ay
    L2 = dx * dx + dy * dy
    if L2 < 2.0:
        return
    L = L2 ** 0.5
    h = hw * (pa[2] + pb[2]) * 0.5 * _A
    if h < 1.0:
        h = 1.0
    ux = -dy / L * h; uy = dx / L * h
    spoly((ax + ux, ay + uy, bx + ux, by + uy, bx - ux, by - uy, ax - ux, ay - uy),
          c, True)


def _slot(pa, pb, f, hw, c=0):
    ax = SX(pa[0], pa[1]); ay = SY(pa[0], pa[1])
    bx = SX(pb[0], pb[1]); by = SY(pb[0], pb[1])
    dx = bx - ax; dy = by - ay
    L2 = dx * dx + dy * dy
    if L2 < 9.0:
        return
    L = L2 ** 0.5
    h = hw * (pa[2] + pb[2]) * 0.5 * _A
    ux = -dy / L * h; uy = dx / L * h
    cx = ax + dx * f; cy = ay + dy * f
    oled.line(int(cx + ux), int(cy + uy), int(cx - ux), int(cy - uy), c)


def draw_tank(ox, oy, th=0, rec=0.0, ph=0.0, detail=2, keepout=1):
    draw_running_gear(ox, oy, ph, detail, keepout)
    draw_hull(ox, oy, detail, keepout)
    draw_turret(ox, oy, th, rec, detail, 0.0, keepout)


def tank_muzzle(ox, oy, th, rec=0.0):
    """World position of the muzzle -- used to aim flashes and shells."""
    th = int(th) & (NA - 1)
    p = _tp(11.0 - rec + GL, 0.0, 1.7, ox + TRING[0], oy + TRING[1],
            COS[th], SIN[th])
    return (p[0], p[1], p[2])

# ============================================================================
#  EFFECTS
#  Everything is a flat list of floats -- no classes, no allocation churn.
# ============================================================================
DEB = []      # chunks   [x,y,vx,vy,life,size]
BOD = []      # rigid bodies [x,y,vx,vy,ang,om,localpts]
SMK = []      # smoke    [x,y,vx,vy,r,grow,life]
EMB = []      # embers   [x,y,vx,vy,life]
FIRE = []     # ground fire [x,y,h,ph]


def fx_reset():
    del DEB[:]; del BOD[:]; del SMK[:]; del EMB[:]; del FIRE[:]


def spawn_debris(cx, cy, n, spd=4.0, up=1.0):
    for _ in range(n):
        a = ri(0, NA - 1)
        v = rnd(spd * 0.35, spd)
        DEB.append([cx, cy, COS[a] * v, SIN[a] * v * 0.85 - rnd(0.4, 2.4) * up,
                    rnd(14, 30), ri(1, 2)])


def spawn_smoke(cx, cy, n, r=3.0, spd=1.0, rise=1.0, life=0):
    for _ in range(n):
        L = life if life else rnd(30, 56)
        SMK.append([cx + rnd(-4, 4), cy + rnd(-3, 3),
                    rnd(-spd, spd), -rnd(0.4, 1.3) * rise,
                    rnd(r * 0.6, r * 1.4), rnd(0.16, 0.42), L, L])


def spawn_embers(cx, cy, n):
    for _ in range(n):
        a = ri(0, NA - 1)
        EMB.append([cx, cy, COS[a] * rnd(0.3, 1.8), -rnd(0.8, 2.6), rnd(16, 40)])


def spawn_bodies(parts, cx, cy, spd=3.4):
    for p in parts:
        dx = p[0] - cx; dy = p[1] - cy
        d = (dx * dx + dy * dy) ** 0.5 + 0.001
        k = rnd(0.7, 1.5) * spd
        BOD.append([p[0], p[1], dx / d * k + rnd(-0.8, 0.8),
                    dy / d * k - rnd(1.6, 4.2), 0.0, rnd(-2.2, 2.2), p[2]])


_fxacc = 0.0


def fx_step(g=0.30, wind=0.0):
    """Runs the integrator SPEED times per rendered frame (fractional part
    carried), so particles keep real-world pace when the cinematic is cut
    down to fewer frames."""
    global _fxacc
    _fxacc += SPEED
    k = int(_fxacc)
    _fxacc -= k
    while k > 0:
        k -= 1
        _fx_once(g, wind)


def _fx_once(g, wind):
    i = len(DEB) - 1
    while i >= 0:
        d = DEB[i]
        d[0] += d[2]; d[1] += d[3]; d[3] += g; d[2] *= 0.99
        d[4] -= 1
        if d[4] <= 0 or d[1] > GROUND + 2:
            if d[1] > GROUND and d[4] > 3:
                d[1] = GROUND; d[3] = -d[3] * 0.28; d[2] *= 0.6
                d[4] -= 6
            else:
                DEB.pop(i)
        i -= 1
    i = len(BOD) - 1
    while i >= 0:
        b = BOD[i]
        b[0] += b[2]; b[1] += b[3]; b[3] += g * 1.05; b[4] += b[5]; b[2] *= 0.995
        if b[1] > GROUND + 1:
            b[1] = GROUND + 1; b[3] = -b[3] * 0.22; b[5] *= 0.4; b[2] *= 0.55
            if abs(b[3]) < 0.6:
                b[3] = 0.0; b[5] = 0.0
        i -= 1
    i = len(SMK) - 1
    while i >= 0:
        s = SMK[i]
        s[0] += s[2] + wind; s[1] += s[3]
        if s[4] < 13.0:
            s[4] += s[5]
        s[2] *= 0.97; s[3] *= 0.985; s[6] -= 1
        if s[6] <= 0:
            SMK.pop(i)
        i -= 1
    i = len(EMB) - 1
    while i >= 0:
        e = EMB[i]
        e[0] += e[2] + wind * 1.4; e[1] += e[3]; e[3] += 0.045; e[2] *= 0.98
        e[4] -= 1
        if e[4] <= 0:
            EMB.pop(i)
        i -= 1


def draw_smoke(dense=1.0):
    """Smoke ages through three looks, which is both physical and the only way
    it stays legible: fresh = solid white mass, cooling = black core with a
    white rim (so it occludes), dissipating = wispy rim only."""
    for s in SMK:
        if s[6] < 3:
            continue
        rr = s[4] * _A
        if rr < 2.0:
            continue
        cx = SX(s[0], s[1]); cy = SY(s[0], s[1])
        if cx < -rr or cx > 128 + rr or cy < -rr or cy > 64 + rr:
            continue
        sd = (int(s[0]) >> 1) + int(s[1])
        k = s[6] / s[7]
        if k > 0.66 * dense:
            blob(cx, cy, rr, sd, True, 1)                  # dense: solid
            for _ in range(2 + int(rr * 0.12)):
                a = ri(0, NA - 1); d = rnd(0.1, 0.65) * rr
                oled.pixel(int(cx + COS[a] * d), int(cy + SIN[a] * d * 0.88), 0)
        elif k > 0.30:
            blob(cx, cy, rr, sd, True, 0)                  # cooling: hollow
            blob(cx, cy, rr, sd, False, 1)
        else:
            blob(cx, cy, rr, sd, False, 1)                 # wisps only


def draw_bodies():
    for b in BOD:
        a = int(b[4]) & (NA - 1)
        ca = COS[a]; sa = SIN[a]
        pts = []
        big = []
        for (dx, dy) in b[6]:
            pts.append((b[0] + dx * ca - dy * sa, b[1] + dx * sa + dy * ca))
            ex = dx * 1.34; ey = dy * 1.34
            big.append((b[0] + ex * ca - ey * sa, b[1] + ex * sa + ey * ca))
        wpoly(big, 0, True)                       # keep-out so chunks separate
        if b[3] == 0.0 and b[5] == 0.0:
            wpoly(pts, 0, True)                   # settled wreckage reads as
            wpoly(pts, 1, False)                  # outline, not a bright slab
        else:
            wpoly(pts, 1, True)


def draw_debris():
    """Chunks as short streaks along their own velocity -- direction reads,
    and it stops them strobing between frames."""
    for d in DEB:
        x = SX(d[0], d[1]); y = SY(d[0], d[1])
        if d[5] > 1:
            oled.line(x, y, int(x - d[2] * 1.0 * _A), int(y - d[3] * 1.0 * _A), 1)
        else:
            oled.pixel(x, y, 1)


def draw_embers(t):
    for e in EMB:
        if (t + int(e[0])) & 3:
            oled.pixel(SX(e[0], e[1]), SY(e[0], e[1]), 1)


def draw_fire(t):
    for f in FIRE:
        h = f[2] * (0.62 + 0.38 * SIN[(t * 5 + int(f[3])) & (NA - 1)])
        x = f[0] + SIN[(t * 3 + int(f[3]) * 3) & (NA - 1)] * 0.6
        wpoly(((x - h * 0.42, f[1]), (x + h * 0.42, f[1]),
               (x + h * 0.12, f[1] - h * 0.6), (x, f[1] - h),
               (x - h * 0.2, f[1] - h * 0.55)), 1, True)
        wpix(x, f[1] - h * 0.45, 0)


def fireball(cx, cy, r, ragged=1, core=0):
    """One ragged blob -- a single poly call instead of a circle plus punches."""
    pts = []
    for i in range(0, NA, 4):
        k = r * (0.80 + 0.42 * ((i * 7 + int(cx)) % 5) * 0.1) if ragged else r
        pts.append((cx + COS[i] * k, cy + SIN[i] * k * 0.92))
    wpoly(pts, 1, True)
    if core > 0:
        pts = []
        for i in range(0, NA, 6):
            k = core * (0.7 + 0.5 * ((i * 3) % 4) * 0.1)
            pts.append((cx + COS[i] * k, cy + SIN[i] * k * 0.9))
        wpoly(pts, 0, True)


def shock_ring(cx, cy, r, flat=0.34):
    ell(SX(cx, cy), SY(cx, cy), r * _A, r * flat * _A, 1, False)


def air_ring(cx, cy, r):
    wcirc(cx, cy, r)


# ============================================================================
#  SCREEN CRACK MESH  --  the same mesh is the crack pattern AND the shards
# ============================================================================
NSP = 11
NRG = 4
MESH = []
JIT = []


def make_cracks(cx, cy):
    del MESH[:]
    del JIT[:]
    base = ri(0, 5)
    for i in range(NSP):
        a = 6.2831853 * (i + rnd(-0.16, 0.16)) / NSP + base * 0.1
        dx = math.cos(a); dy = math.sin(a)
        # distance to the screen box, pushed a little past the edge
        tx = ((132.0 - cx) / dx) if dx > 0.001 else (((-4.0) - cx) / dx if dx < -0.001 else 9e9)
        ty = ((68.0 - cy) / dy) if dy > 0.001 else (((-4.0) - cy) / dy if dy < -0.001 else 9e9)
        t = tx if tx < ty else ty
        sp = [(cx, cy)]
        for j in range(1, NRG + 1):
            f = (j / float(NRG)) ** 0.82
            if j < NRG:
                f += rnd(-0.07, 0.07)
            r = t * f
            sp.append((cx + dx * r + (rnd(-2.5, 2.5) if j < NRG else 0),
                       cy + dy * r + (rnd(-2.5, 2.5) if j < NRG else 0)))
        MESH.append(sp)
    for i in range(NSP):
        row = []
        for j in range(NRG + 1):
            row.append((rnd(-2.2, 2.2), rnd(-2.2, 2.2)))
        JIT.append(row)


def _crk(x0, y0, x1, y1):
    """A crack with its own black shadow, so it stays legible over the frozen
    image behind the glass without having to dim the whole picture."""
    x0 = int(x0); y0 = int(y0); x1 = int(x1); y1 = int(y1)
    oled.line(x0 + 1, y0 + 1, x1 + 1, y1 + 1, 0)
    oled.line(x0, y0, x1, y1, 1)


def draw_cracks(prog, tips=0):
    """prog 0..NRG -- radials grow outward, rings knit them together behind."""
    full = int(prog)
    frac = prog - full
    for i in range(NSP):
        sp = MESH[i]
        for j in range(min(full, NRG)):
            _crk(sp[j][0], sp[j][1], sp[j + 1][0], sp[j + 1][1])
        if full < NRG and frac > 0.02:
            a = sp[full]; b = sp[full + 1]
            ex = a[0] + (b[0] - a[0]) * frac
            ey = a[1] + (b[1] - a[1]) * frac
            _crk(a[0], a[1], ex, ey)
            if tips:
                oled.pixel(int(ex) + ri(-1, 1), int(ey) + ri(-1, 1), 1)
    for j in range(1, min(full, NRG) + 1):
        for i in range(NSP):
            a = MESH[i][j]; b = MESH[(i + 1) % NSP][j]
            mx = (a[0] + b[0]) * 0.5 + JIT[i][j][0]
            my = (a[1] + b[1]) * 0.5 + JIT[i][j][1]
            _crk(a[0], a[1], mx, my)
            _crk(mx, my, b[0], b[1])


SHARD = []


def make_shards(cx, cy):
    del SHARD[:]
    for j in range(NRG):
        for i in range(NSP):
            p = (MESH[i][j], MESH[(i + 1) % NSP][j],
                 MESH[(i + 1) % NSP][j + 1], MESH[i][j + 1])
            gx = 0.0; gy = 0.0
            for q in p:
                gx += q[0]; gy += q[1]
            gx *= 0.25; gy *= 0.25
            loc = []
            for q in p:
                loc.append((q[0] - gx, q[1] - gy))
            d = ((gx - cx) ** 2 + (gy - cy) ** 2) ** 0.5
            SHARD.append([gx, gy, rnd(-0.5, 0.5), rnd(-0.7, 0.3),
                          0.0, rnd(-0.05, 0.05), loc, int(d * 0.16 + ri(0, 4))])


def step_shards(t, dt=1.0):
    for s in SHARD:
        if t < s[7]:
            continue
        s[0] += s[2] * dt; s[1] += s[3] * dt
        s[3] += 0.52 * dt; s[4] += s[5] * dt
        s[5] *= 1.0 + 0.02 * dt


def draw_shards(t):
    alive = 0
    for s in SHARD:
        if s[1] > 96:
            continue
        alive += 1
        a = s[4]; ca = math.cos(a); sa = math.sin(a)
        o = []
        for (dx, dy) in s[6]:
            o.append(s[0] + dx * ca - dy * sa)
            o.append(s[1] + dx * sa + dy * ca)
        spoly(o, 1, False)
        if t > s[7] + 2 and len(o) >= 8:
            oled.line(int((o[0] + o[2]) * 0.5), int((o[1] + o[3]) * 0.5),
                      int((o[4] + o[6]) * 0.5), int((o[5] + o[7]) * 0.5), 1)
    return alive

# ============================================================================
#  PLANET
# ============================================================================
CONT = ((0.06, -0.36, 0.30), (0.40, 0.26, 0.36), (0.70, -0.10, 0.24),
        (0.90, 0.46, 0.20), (0.55, -0.52, 0.16))


def draw_planet(cx, cy, R, spin, lights=1):
    wcirc(cx, cy, R)
    for (u, v, rr) in CONT:
        a = u * 6.2831853 + spin
        c = math.cos(a)
        if c <= 0.10:
            continue
        wfcirc(cx + math.sin(a) * R * 0.74, cy + v * R, rr * R * c)
    # terminator: a dotted boundary, not a hard line -- a hard ellipse reads
    # as an eye at this size
    k = -0.34
    yy = -R + 1.0
    while yy <= R - 1.0:
        dxr = (R * R - yy * yy) ** 0.5
        wpix(cx + dxr * k, cy + yy, 1)
        yy += 2.4
    # polar cap
    ell(SX(cx, cy - R * 0.74), SY(cx, cy - R * 0.74),
        R * 0.34 * _A, R * 0.16 * _A, 1, True)
    # atmosphere limb
    for i in range(0, NA, 3):
        wpix(cx + COS[i] * (R + 2.4), cy + SIN[i] * (R + 2.4), 1)
    if lights:
        for kk in range(5):
            a = (kk * 13 + int(spin * 9)) & (NA - 1)
            if COS[a] < -0.30:
                wpix(cx + COS[a] * R * 0.5, cy + SIN[a] * R * 0.66, 1)


def horizon_arc(R, topy, tex=0.0, bump=0.0):
    """The desert horizon and the curve of the planet are the same primitive.
    Shrinking R bends one into the other without a cut."""
    pts = []
    x = -4
    while x <= 132:
        d = x - 64.0
        if abs(d) <= R:
            pts.append((x, topy + R - (R * R - d * d) ** 0.5))
        x += 4
    n = len(pts)
    for i in range(n - 1):
        oled.line(int(pts[i][0]), int(pts[i][1]),
                  int(pts[i + 1][0]), int(pts[i + 1][1]), 1)
    if bump > 0.03:                       # mountains riding the curve away
        k = 1
        while k + 2 < n:
            a = pts[k]; b = pts[k + 2]
            h = bump * (5 + 3 * ((k * 7) % 3))
            mx = (a[0] + b[0]) * 0.5; my = (a[1] + b[1]) * 0.5 - h
            oled.line(int(a[0]), int(a[1]), int(mx), int(my), 1)
            oled.line(int(mx), int(my), int(b[0]), int(b[1]), 1)
            k += 5
    if tex > 0.03:
        for i in range(0, n, 3):
            oled.pixel(int(pts[i][0]) + 2, int(pts[i][1] + 4 * tex), 1)
            oled.pixel(int(pts[i][0]) - 1, int(pts[i][1] + 9 * tex), 1)


# ============================================================================
#  SCENES
# ============================================================================
def scene_launch(t, pose=0, hold=1, mortar=1, recoil=0.0, fg=1):
    frame_begin()
    draw_sun(t); draw_clouds(); draw_birds(t)
    draw_far(); draw_mid(); draw_ground(t)
    layer0()
    draw_cactus(96, GROUND, 1.0)
    draw_bush(76, GROUND, 0.9)
    draw_rock(16, GROUND, 13, 5)
    draw_rock(108, GROUND, 9, 4)
    if pose >= 0:
        draw_soldier(pose, hold)
    if mortar:
        draw_mortar(recoil)
    if fg:
        fg_scrub(t)
    layer0()


def scene_target(t, cabin=1, tank=None, smoke=1):
    frame_begin()
    draw_sun(t); draw_clouds()
    draw_far(); draw_mid(); draw_ground(t)
    layer0()
    draw_cactus(26, GROUND, 1.0)
    draw_bush(46, GROUND, 0.9)
    draw_rock(120, GROUND, 12, 5)
    if tank is not None:
        draw_tank(tank[0], tank[1], tank[2], tank[3], tank[4], tank[5])
    if cabin:
        draw_cabin()
        if smoke:
            draw_smoke_stack(t)
    layer0()


# ============================================================================
#  ACT I -- THE LAUNCH
# ============================================================================
def sh_open():
    global BAR
    cam(70, 33, 1.02)
    n = F(20)
    for i in range(n):                      # bars slide in over the first beat
        BAR = int(8 * i / (n - 1.0))
        scene_launch(i, 0, 1)
        bars(); flip()
    for i in range(F(48)):                  # slow push toward the crew
        cam_to(58.0, 34.0, 1.34, 0.048)
        scene_launch(20 + i, 0, 1)
        bars(); flip()


def sh_load():
    # medium: the loader holds the round over the tube
    for i in range(F(38)):
        cam_to(58.0, 34.0, 2.00, 0.085)
        scene_launch(50 + i, 0, 1)
        bars(); flip()
    # CUT to a close-up framed with the muzzle low-right and clean sky above,
    # so the round is unmistakably falling INTO something.
    cam(MZX + 1.5, MZY - 4.5, 2.5)
    d = 19.0
    for i in range(F(34)):
        scene_launch(76 + i, -1, 0, 1, 0.0, 0)
        if d > -8.0:
            draw_shell(MZX + MUX * d, MZY + MUY * d, 42, 1.0)
        draw_mortar(0.0)
        if d > 10.0:                         # the loader's gloves, releasing
            hx = MZX + MUX * (d + 1.5); hy = MZY + MUY * (d + 1.5)
            plate(((hx + 1.5, hy - 5.5), (hx + 16.0, hy - 5.5),
                   (hx + 16.0, hy + 4.5), (hx + 1.5, hy + 4.5)))
            wbar(hx + 15.0, hy - 4.0, hx + 4.4, hy - 2.0, 1.2)
            wbar(hx + 15.0, hy + 1.4, hx + 4.4, hy + 1.8, 1.2)
            wfcirc(hx + 3.6, hy - 1.9, 1.9); wfcirc(hx + 3.6, hy + 1.9, 1.9)
        if -1.5 < d < 1.5:
            shake(1.6)
        d -= 0.72 * SPEED
        cam_to(MZX + 1.5, MZY - 4.0, 2.34, 0.05)
        bars(); flip()
    for i in range(F(15)):                   # hold on the loaded tube
        scene_launch(110 + i, -1, 0, 1, 0.0, 0)
        draw_mortar(0.0)
        bars(); flip()


def sh_fire():
    # cut wide, crew ducks: two seconds of stillness before the bang
    for i in range(F(36)):
        cam_to(58.0, 35.0, 1.70, 0.13)
        scene_launch(114 + i, 2, 0)
        bars(); flip()
    # ---- BANG ----
    _cue("launch")
    whiteout(60, 5)
    fx_reset()
    spawn_smoke(MZX, MZY, 8, 2.6, 1.4, 1.2, 30)
    spawn_debris(MB_X, GROUND, 16, 3.0, 0.4)
    sx_ = MZX; sy_ = MZY
    vx = -1.35; vy = -5.7
    shake(7.0)
    n = F(14); dt = 14.0 / n
    for i in range(n):
        q = i * dt
        cam_to(54.0, 32.0, 1.16, 0.22)       # kick-zoom out from the blast
        scene_launch(136 + i, 2, 0, 1, max(0.0, 2.4 - q * 0.5))
        r = 3 + q * 2.6
        if q < 7:
            wcirc(MZX, MZY, r)
            wcirc(MZX, MZY, r * 0.6)
            for k in range(0, NA, 6):        # muzzle star
                L = r * (1.5 if (k & 6) else 1.9)
                wline(MZX, MZY, MZX + COS[k] * L, MZY + SIN[k] * L * 0.9, 1)
        shock_ring(MB_X, GROUND, 6 + q * 4.0, 0.30)
        fx_step(0.22, 0.15)
        draw_smoke(0.8); draw_debris()
        if q > 1:
            draw_shell(sx_, sy_, 42 + (i & 1), 1.0)
            wline(sx_ - vx * 2.4, sy_ - vy * 2.4, sx_, sy_, 1)
            sx_ += vx * dt; sy_ += vy * dt; vy += 0.16 * dt
        led_bar(5 - (int(q) >> 1))
        bars(); flip()
    all_off()
    return sx_, sy_, vx, vy


def sh_watch(sx_, sy_, vx, vy):
    """A held reaction shot. Cheap in frames, and it is what makes the launch
    feel like it happened to somebody."""
    n = F(26); dt = 26.0 / n
    for i in range(n):
        q = i * dt
        cam_to(60.0, 35.0, 1.52, 0.10)
        scene_launch(150 + i, 2 if q < 15 else 1, 0, 1, 0.0, 1)
        fx_step(0.22, 0.2)
        draw_smoke(0.7)
        x = sx_ + vx * q * 0.55; y = sy_ + vy * q * 0.55 + 0.09 * q * q
        if y > -12:
            wline(x - vx * 2.6, y - vy * 2.6, x, y, 1)
            draw_shell(x, y, 42, 0.9)
        bars(); flip()


def sh_ascend(sx_, sy_, vx, vy):
    global BAR
    # camera tilts up with the round; the desert falls away, stars come in
    ns = 0
    n = F(48); dt = 48.0 / n
    for i in range(n):
        if i * dt < 30:
            cam_to(sx_ - 4, sy_ + 8, 1.02, 0.20)
        else:                                 # park the horizon near the bottom
            cam_to(sx_ - 4, 24.5, 1.02, 0.13)  # so the next shot can pick it up
        frame_begin()
        ns = int(clamp((32 - CAMY) * 0.9, 0, 40))
        draw_stars(ns, i)
        draw_sun(i)
        if ns < 20:                          # atmosphere thins out behind us
            draw_clouds()
        if ns < 8:
            draw_birds(i)
        draw_far(); draw_mid(); draw_ground(i)
        layer0()
        draw_cactus(96, GROUND, 1.0)
        draw_mortar(0.0)
        draw_soldier(2, 0)
        draw_shell(sx_, sy_, 42, 1.0)
        wline(sx_ - vx * 3.0, sy_ - vy * 3.0, sx_ - vx, sy_ - vy, 1)
        for k in range(3):                   # exhaust flecks
            wpix(sx_ - vx * (1.4 + k) + rnd(-1, 1), sy_ - vy * (1.4 + k) + rnd(-1, 1), 1)
        sx_ += vx * dt; sy_ += vy * dt; vy += 0.115 * dt
        bars(); flip()
    # ---- the horizon bends into a planet ----
    cam(64, 32, 1.0)
    n = F(42)
    for i in range(n):
        u = i / (n - 1.0)
        q = u * 41.0
        frame_begin()
        draw_stars(int(12 + 28 * u), i)
        R = 900.0 * (1.0 - u) ** 2.4 + 26.0
        topy = 58.0 - 28.0 * u
        horizon_arc(R, topy, max(0.0, 1.0 - u * 1.6),
                    max(0.0, 1.0 - u * 1.9))
        if u > 0.45:
            draw_planet(64, topy + R, R, 0.6, 0)
        yy = 40 - q * 1.5
        if yy > -8:
            draw_shell(64 + q * 0.5, yy, 42 - int(q * 0.3), 1.0)
        if BAR > 0:
            BAR = int(8 * (1.0 - u))
        bars(); flip()
    BAR = 0


# ============================================================================
#  ACT II -- ORBIT
# ============================================================================
def sh_orbit():
    global ROLL
    pcx = 64.0; pcy = 30.0; R = 26.0
    th = 0.0
    spin = 0.0
    # settle the planet into frame
    ez = clamp(0.12 * SPEED, 0.02, 0.6)
    for i in range(F(24)):
        pcy += (34.0 - pcy) * ez
        R += (17.0 - R) * ez
        frame_begin()
        draw_stars(40, i)
        draw_planet(pcx, pcy, R, spin)
        spin += 0.035 * SPEED
        flip()
    a = 46.0; b = 15.0
    trail = []
    n = F(180); dt = 180.0 / n
    for i in range(n):
        q = i * dt
        cam_to(64 + 6 * math.sin(q * 0.026), 32 + 3 * math.cos(q * 0.021),
               1.0 + 0.16 * math.sin(q * 0.018), 0.2)
        frame_begin()
        draw_stars(40, i)
        draw_planet(pcx, pcy, R, spin)
        spin += 0.030 * dt
        px = pcx + a * math.cos(th)
        py = pcy + b * math.sin(th)
        behind = math.sin(th) < 0
        dist = ((px - pcx) ** 2 + ((py - pcy) * 2.2) ** 2) ** 0.5
        trail.append((px, py))
        if len(trail) > 7:
            trail.pop(0)
        if not (behind and dist < R + 1.5):
            for k in range(len(trail) - 1):
                if k & 1:
                    wpix(trail[k][0], trail[k][1], 1)
            draw_shell(px, py, (int(th * 9.2) + 40) & (NA - 1), 1.0)
        th += 0.076 * dt
        # a moon, for scale
        wfcirc(112, 10, 3.0); wfcirc(113, 9, 1.0, 0); wfcirc(110.5, 11.5, 0.8, 0)
        flip()
    # ---- orbit decays: the round spirals in ----
    n = F(46); dt = 46.0 / n
    for i in range(n):
        u = i / (n - 1.0)
        cam_to(64, 32, 1.0 + 0.5 * u, 0.16)
        frame_begin()
        draw_stars(40, i)
        draw_planet(pcx, pcy, R, spin)
        spin += 0.05 * dt
        aa = a * (1 - u * 0.72); bb = b * (1 - u * 0.55)
        px = pcx + aa * math.cos(th)
        py = pcy + bb * math.sin(th)
        for k in range(6):                                  # decaying trail
            tt = th - k * 0.09
            wpix(pcx + aa * math.cos(tt) * (1 + k * 0.02),
                 pcy + bb * math.sin(tt) * (1 + k * 0.02), 1)
        draw_shell(px, py, (int(th * 9.2) + 40) & (NA - 1), 1.0 + u * 0.8)
        if u > 0.5:
            wcirc(px, py, 3 + (i & 3))
        th += (0.076 + u * 0.10) * dt
        flip()


def sh_reentry():
    # CU: the round comes at us through the atmosphere
    cam(64, 32, 1.0)
    n = F(60)
    for i in range(n):
        u = i / (n - 1.0)
        frame_begin()
        for k in range(16):                                # speed lines
            a = ((k * 7 + i * 3) % NA)
            d0 = 12 + ((k * 13 + i * 9) % 40)
            d1 = d0 + 10 + u * 22
            oled.line(int(64 + COS[a] * d0), int(32 + SIN[a] * d0),
                      int(64 + COS[a] * d1), int(32 + SIN[a] * d1), 1)
        # bow shock
        for k in range(3):
            ell(64, int(38 + u * 6) + k * 3, int((9 + u * 16) - k * 2),
                int((4 + u * 7) - k), 1, False)
        if u < 0.55:
            draw_shell(64, 30 - u * 2, (46 + int(i * 0.35)) & (NA - 1),
                       1.2 + u * 2.6)
        else:
            k = (u - 0.55) / 0.45
            shell_noseon(64, int(31 - u * 3), 4 + k * 9, int(i * 1.4))
        noise(48 - u * 14, 40, 34 + u * 30, 20, int(10 + u * 22))
        shake(0.6 + u * 1.6)
        led_bar(int(u * 5))
        flip()
    all_off()
    whiteout(50, 3)
    all_off()


# ============================================================================
#  ACT III -- IMPACT
# ============================================================================
def sh_impact():
    global BAR
    BAR = 6
    cam(CAB_X - 4, 34, 1.30)
    for i in range(F(46)):                     # establish the target, low angle
        cam_to(CAB_X, 33.0, 1.45, 0.04)
        scene_target(i)
        bars(); flip()
    x = CAB_X - 26.0; y = -14.0
    vx = 1.05; vy = 3.6
    for i in range(F(30)):                     # incoming
        cam_to(CAB_X - 6, clamp(y + 8, 20, 34), 1.42, 0.22)
        scene_target(34 + i)
        draw_shell(x, y, 12, 1.3)
        wline(x - vx * 5, y - vy * 5, x - vx, y - vy, 1)
        wline(x - vx * 5 + 1, y - vy * 5, x - vx + 1, y - vy, 1)
        x += vx * SPEED; y += vy * SPEED; vy += 0.10 * SPEED
        if y > 30:
            break
        bars(); flip()
    # ---- HIT ----
    ix = CAB_X - 2; iy = 36.0
    _cue("impact")
    whiteout(70, 5)
    fx_reset()
    spawn_bodies(CAB_PARTS, ix, iy, 5.2)
    spawn_debris(ix, iy, 30, 7.0, 1.0)
    spawn_smoke(ix, iy, 7, 5.0, 1.4, 1.2, 62)
    spawn_embers(ix, iy, 18)
    for k in range(3):
        FIRE.append([CAB_X - 12 + k * 12 + rnd(-2, 2), GROUND, rnd(4, 8), ri(0, 63)])
    shake(9.0)
    cam(CAB_X, 33, 0.86)                       # snap zoom OUT on the hit
    # Draw order is the whole game here: smoke is volume and sits at the back,
    # the fireball burns through it, and the chunks are being thrown AT us so
    # they go in front of everything.
    n = F(40); dt = 40.0 / n
    for i in range(n):
        q = i * dt
        cam_to(CAB_X, 32.0, 1.16, 0.055)
        scene_target(70 + i, 0)
        fx_step(0.30, 0.10)
        draw_smoke(0.9)
        if q < 16:
            rr = 9 + q * 2.4
            fireball(ix, iy - q * 0.55, rr, 1,
                     min(rr * 0.55, (q - 5) * 3.2) if q > 5 else 0)
        if q < 22:
            shock_ring(ix, GROUND, 8 + q * 6.5, 0.26)
            air_ring(ix, iy, 7 + q * 4.4)
        draw_bodies(); draw_debris()
        draw_embers(i); draw_fire(i)
        led_bar(5 - (int(q) >> 2))
        bars(); flip()
    all_off()
    return ix


def sh_aftermath(ix):
    # smoke column, camera drifts up it then settles back down
    n = F(68); s4 = max(1, int(4 / SPEED + 0.5)); s6 = max(1, int(6 / SPEED + 0.5))
    for i in range(n):
        u = i / (n - 1.0)
        cam_to(CAB_X, 30.0 - 6 * math.sin(u * 3.14), 1.30, 0.045)
        scene_target(104 + i, 0)
        fx_step(0.30, 0.12)
        if i % s4 == 0:
            spawn_smoke(ix + rnd(-4, 4), GROUND - 5, 1, 3.6, 0.5, 1.4, 46)
        if i % s6 == 0:
            spawn_embers(ix + rnd(-12, 12), GROUND - 2, 2)
        draw_smoke(0.9)
        draw_bodies(); draw_debris()
        draw_embers(i); draw_fire(i)
        bars(); flip()


# ============================================================================
#  ACT IV -- THE TANK
# ============================================================================
TKX = 64.0
TKY = GROUND


def sh_reveal():
    global BAR
    ph = 0.0
    # smoke thins; the tank resolves out of it and rolls forward
    tx = TKX + 9.0
    n = F(74); dt = 74.0 / n; s4 = max(1, int(4 / SPEED + 0.5))
    for i in range(n):
        u = i / (n - 1.0)
        cam_to(TKX + 22, 34.0, 1.02 + 0.42 * u, 0.055)
        frame_begin()
        draw_sun(i); draw_clouds()
        draw_far(); draw_mid(); draw_ground(i)
        layer0()
        draw_cactus(26, GROUND, 1.0)
        draw_rock(120, GROUND, 12, 5)
        if u > 0.06:
            draw_tank(tx, TKY, 0, 0.0, ph, 2 if u > 0.35 else 0)
            if u > 0.22:
                tx -= 0.14 * dt
                ph += 0.9 * dt
        fx_step(0.26, 0.16)
        if i % s4 == 0 and u < 0.55:
            spawn_smoke(rnd(TKX - 14, TKX + 42), GROUND + 1, 1, 3.0, 0.4, 0.5, 26)
        draw_smoke(0.6)
        draw_bodies(); draw_fire(i); draw_embers(i)
        # DISSOLVE: black noise eats the tank early and lets go of it, which
        # resolves far more cleanly than parking smoke on top of the subject
        k = (1.0 - u) ** 1.6
        noise(0, 6, 128, 52, int(230 * k), 0)
        noise(0, 26, 128, 26, int(20 * k))
        bars(); flip()
    # hold on the tank
    _cue("reveal")
    for i in range(F(30)):
        cam_to(TKX + 24, 34.0, 1.50, 0.06)
        frame_begin()
        draw_sun(i); draw_clouds(); draw_far(); draw_mid(); draw_ground(i)
        layer0()
        draw_cactus(26, GROUND, 1.0)
        draw_tank(tx, TKY, 0, 0.0, ph, 2)
        draw_fire(i); fx_step(0.30, 0.14); draw_smoke(0.35); draw_embers(i)
        bars(); flip()
    # HERO TRACK: low, close, and moving -- rear sprocket to muzzle in one
    # unbroken move. This is the shot that sells the machine.
    n = F(38); dt = 38.0 / n
    for i in range(n):
        u = i / (n - 1.0)
        e = u * u * (3 - 2 * u)
        cam(tx + 40.0 - 54.0 * e, 43.0 - 12.0 * e, 2.75 - 0.35 * e)
        frame_begin()
        draw_sun(i); draw_clouds(); draw_far(); draw_mid(); draw_ground(i)
        layer0()
        draw_tank(tx, TKY, 0, 0.0, ph, 2)
        ph += 0.5 * (1.0 - e) * dt
        draw_fire(i); fx_step(0.3, 0.14); draw_smoke(0.3)
        bars(); flip()
    return tx, ph


def sh_traverse(tx, ph):
    # push in and traverse the turret until the bore is on the lens
    th = 0.0
    n = F(68); dt = 68.0 / n
    for i in range(n):
        u = i / (n - 1.0)
        th = 16.0 * (u * u * (3 - 2 * u))          # smoothstep
        cam_to(tx + 21, 33.0, 1.85 + 0.5 * u, 0.09)
        frame_begin()
        draw_sun(i); draw_clouds(); draw_far(); draw_mid(); draw_ground(i)
        layer0()
        draw_tank(tx, TKY, th, 0.0, ph, 2)
        if u < 0.21:
            ph += 0.35 * dt
        draw_fire(i); fx_step(0.3, 0.14); draw_smoke(0.3)
        bars(); flip()
    # ECU: the bore
    mz = tank_muzzle(tx, TKY, 16)
    for i in range(F(36)):
        cam_to(mz[0], mz[1] + 1.0, 4.6, 0.13)
        frame_begin()
        draw_far(); draw_mid(); draw_ground(i)
        layer0()
        draw_tank(tx, TKY, 16, 0.0, ph, 2)
        bars(); flip()
    return th


def sh_shoot(tx, ph):
    mz = tank_muzzle(tx, TKY, 16)
    _cue("fire")
    # ---- muzzle flash, held on the ECU. Scaled to the SHOT, not the world:
    # at 4.6x zoom a 30-unit star would simply white the panel out.
    n = F(5); dt = 5.0 / n
    for i in range(n):
        q = i * dt
        frame_begin()
        draw_far(); draw_mid(); draw_ground(i)
        layer0()
        draw_tank(tx, TKY, 16, 3.6 if q > 1 else 0.0, ph, 2)
        r = 2.5 + q * 1.9
        for k in range(0, NA, 4):
            L = r * (2.2 if (k & 4) else 1.25)
            wbar(mz[0], mz[1], mz[0] + COS[k] * L, mz[1] + SIN[k] * L, 0.5)
        blob(SX(mz[0], mz[1]), SY(mz[0], mz[1]), r * 0.95 * _A, i, True, 1)
        shake(6.0)
        led_bar(5)
        bars(); flip()
    whiteout(60, 5)
    # ---- cut wide: the whole tank rocks back ----
    fx_reset()
    spawn_smoke(mz[0], mz[1], 8, 4.0, 2.2, 0.5, 26)
    spawn_debris(tx + 20, GROUND, 26, 4.4, 0.3)
    shake(7.0)
    n = F(28); dt = 28.0 / n
    for i in range(n):
        q = i * dt
        cam_to(tx + 20, 33.0, 1.45, 0.12)
        frame_begin()
        draw_sun(i); draw_clouds(); draw_far(); draw_mid(); draw_ground(i)
        layer0()
        rec = 3.6 * max(0.0, 1.0 - q / 9.0)
        draw_tank(tx, TKY, 16, rec, ph, 2)
        if q < 7:
            r = 5 + q * 4.0
            for k in range(0, NA, 5):
                wbar(mz[0], mz[1], mz[0] + COS[k] * r * 1.5,
                     mz[1] + SIN[k] * r * 0.95, 0.6)
            blob(SX(mz[0], mz[1]), SY(mz[0], mz[1]), r * 0.6 * _A, i, True, 1)
        shock_ring(tx + 22, GROUND, 10 + q * 7.0, 0.22)
        fx_step(0.3, 0.4)
        draw_smoke(1.0); draw_debris()
        led_bar(max(0, 5 - int(q)))
        bars(); flip()
    all_off()
    # ---- the round arrives at the lens ----
    n = F(26)
    for i in range(n):
        u = i / (n - 1.0)
        frame_begin()
        cam(64, 32, 1.0)
        draw_far(); draw_mid(); draw_ground(i)
        layer0()
        draw_tank(tx, TKY, 16, 0.0, ph, 0)
        draw_smoke(0.5)
        r = 2.0 + u * u * 26.0
        for k in range(3):                       # compression rings
            rr = int(r * (1.9 + k * 0.7))
            ell(64, 32, rr, int(rr * 0.88), 1, False)
        for k in range(10):                      # air tearing past
            a = (k * 6 + i * 5) & (NA - 1)
            d0 = r * 2.4 + 4
            oled.line(int(64 + COS[a] * d0), int(32 + SIN[a] * d0),
                      int(64 + COS[a] * (d0 + 12 + u * 30)),
                      int(32 + SIN[a] * (d0 + 12 + u * 30)), 1)
        shell_noseon(64, 32, r, int(i * 1.7))
        shake(1.0 + u * 5.0)
        bars(); flip()
    whiteout(90, 5)
    all_off()


# ============================================================================
#  ACT V -- THE GLASS
# ============================================================================
def sh_glass():
    global BAR
    BAR = 0
    # freeze the world behind the glass and knock it down a stop, so the
    # cracks read as being ON the lens rather than in the scene
    frame_begin()
    cam(TKX + 20, 33.0, 1.45)
    draw_sun(0); draw_clouds(); draw_far(); draw_mid(); draw_ground(0)
    layer0()
    draw_tank(TKX + 5.0, TKY, 16, 0.0, 0.0, 2)
    dim(3, 2)
    bake()
    _cue("shatter")
    make_cracks(64, 32)
    make_shards(64, 32)
    n = F(30)
    for i in range(n):                           # cracks race outward
        p = (i / (n - 1.0)) ** 0.75 * NRG
        paste()
        fcirc(64, 32, 4, 1); fcirc(64, 32, 2, 0)
        draw_cracks(p, 1)
        if i * SPEED < 4:
            shake(4.0)
            noise(0, 0, 128, 64, 30)
        flip()
    for i in range(F(22)):                       # hold: broken but standing
        paste()
        fcirc(64, 32, 4, 1); fcirc(64, 32, 2, 0)
        draw_cracks(NRG, 0)
        if i % 5 == 0:
            noise(0, 0, 128, 64, 8)
        flip()
    n = F(46); dt = 46.0 / n
    for i in range(n):                           # and it comes down
        q = i * dt
        oled.fill(0)
        step_shards(q, dt)
        alive = draw_shards(q)
        if q < 8:
            draw_cracks(NRG - q * 0.4, 0)
        flip()
        if alive == 0:
            break
    oled.fill(0); flip()
    beat(120)

# ============================================================================
#  TITLE CARD
# ============================================================================
def text_pixels(s):
    w = len(s) * 8
    buf = bytearray(w)
    fb = framebuf.FrameBuffer(buf, w, 8, framebuf.MONO_HLSB)
    fb.text(s, 0, 0, 1)
    pts = []
    for py in range(8):
        for px in range(w):
            if fb.pixel(px, py):
                pts.append((px, py))
    return pts


def blit_pixels(pts, x, y, s):
    s = int(s)
    if s < 1:
        s = 1
    x = int(x); y = int(y)
    if s == 1:
        for (px, py) in pts:
            oled.pixel(x + px, y + py, 1)
    else:
        for (px, py) in pts:
            oled.fill_rect(x + px * s, y + py * s, s, s, 1)


def scaled_text(s, x, y, scale=2):
    blit_pixels(text_pixels(s), x, y, scale)


def draw_footer():
    """Whatever string play() was given -- this library never reads your BEST."""
    if _FOOTER:
        oled.text(_FOOTER, (128 - len(_FOOTER) * 8) // 2, 56)


def sh_title(reaction):
    _cue("title")
    num = str(reaction)
    NP = text_pixels(num)
    HP = text_pixels("NEW BEST!")
    nw = len(num) * 8
    # 1) shockwave out of the dark
    n = F(9); dt = 9.0 / n
    for i in range(n):
        q = i * dt
        oled.fill(0)
        r = 4 + q * 9
        circ(64, 32, r); circ(64, 32, r - 3)
        if q < 3:
            fcirc(64, 32, 6 - q * 2)
        flip()
    # 2) rotating light sweep
    n = F(20); dt = 20.0 / n
    for i in range(n):
        q = i * dt
        oled.fill(0)
        L = 20 + q * 5
        for k in range(0, NA, 4):
            a = (k + int(q) * 2) & (NA - 1)
            m = L * (1.0 if (k & 4) else 0.62)
            oled.line(64, 32, int(64 + COS[a] * m), int(32 + SIN[a] * m), 1)
        fcirc(64, 32, max(1, 9 - q))
        if q > 10:
            blit_pixels(HP, 64 - 40, 4, 1)
        flip()
    # 3) the number slams in
    n = F(9); dt = 9.0 / n
    for i in range(n):
        s = int(9 - i * dt)
        if s < 3:
            s = 3
        jx = ri(-3, 3) if s > 3 else 0
        jy = ri(-2, 2) if s > 3 else 0
        oled.fill(0)
        for k in range(0, NA, 4):
            oled.line(64, 32, int(64 + COS[k] * 70), int(32 + SIN[k] * 70), 1)
        oled.fill_rect(6, 12, 116, 34, 0)
        blit_pixels(HP, 64 - 40, 3, 1)
        blit_pixels(NP, 64 - nw * s // 2 - 8 + jx, 32 - 4 * s + jy, s)
        flip()
    # 4) sparkle settle
    x0 = 64 - (nw * 3 + 20) // 2
    n = F(16); dt = 16.0 / n
    for i in range(n):
        q = i * dt
        oled.fill(0)
        blit_pixels(HP, 64 - 40, 3, 1)
        blit_pixels(NP, x0, 20, 3)
        oled.text("ms", x0 + nw * 3 + 4, 34)
        for _ in range(max(1, 12 - (int(q) >> 1))):
            oled.pixel(ri(0, 127), ri(0, 63), 1)
        if q < 6:
            circ(64, 32, 34 + q * 7)
        draw_footer()
        flip()
    # 5) the card locks in, border draws itself
    n = F(11)
    for i in range(n):
        u = i / (n - 1.0)
        oled.fill(0)
        blit_pixels(HP, 64 - 40, 3, 1)
        blit_pixels(NP, x0, 20, 3)
        oled.text("ms", x0 + nw * 3 + 4, 34)
        draw_footer()
        k = int(128 * u)
        oled.hline(0, 0, k, 1); oled.hline(128 - k, 63, k, 1)
        j = int(64 * u)
        oled.vline(0, 64 - j, j, 1); oled.vline(127, 0, j, 1)
        flip()
    for i in range(F(22)):                         # let it sit
        oled.fill(0)
        blit_pixels(HP, 64 - 40, 3, 1)
        blit_pixels(NP, x0, 20, 3)
        oled.text("ms", x0 + nw * 3 + 4, 34)
        draw_footer()
        oled.rect(0, 0, 128, 64, 1)
        if i % 6 == 0:
            oled.pixel(ri(2, 125), ri(2, 61), 1)
        flip()


def title_card(reaction):
    """The settled last frame. play() always ends here, whether the show ran
    to the end or the player skipped it, so the panel is never left mid-shot."""
    num = str(reaction)
    nw = len(num) * 8
    x0 = 64 - (nw * 3 + 20) // 2
    oled.fill(0)
    blit_pixels(text_pixels("NEW BEST!"), 64 - 40, 3, 1)
    blit_pixels(text_pixels(num), x0, 20, 3)
    oled.text("ms", x0 + nw * 3 + 4, 34)
    draw_footer()
    oled.rect(0, 0, 128, 64, 1)
    if _OVERLAY is not None:              # caller's status icons, drawn once
        try:
            _OVERLAY()
        except Exception:
            pass
    show()


# ============================================================================
#  THE CINEMATIC
# ============================================================================
def cinematic():
    """Acts run in order and each is independent -- drop any of them from
    ACTS at the top of the file for a shorter cut."""
    global BAR, ROLL, _tprev
    BAR = 0; ROLL = 0
    gc.collect()                      # a minute of particles: start clean
    _tprev = time.ticks_ms()
    fx_reset()
    if "launch" in ACTS:
        sh_open()
        sh_load()
        sx_, sy_, vx, vy = sh_fire()
        sh_watch(sx_, sy_, vx, vy)
        if "space" in ACTS:
            sh_ascend(sx_, sy_, vx, vy)
    if "space" in ACTS:
        if "launch" not in ACTS:
            cam(64, 32, 1.0); BAR = 0
        sh_orbit()
        sh_reentry()
    if "impact" in ACTS:
        ix = sh_impact()
        sh_aftermath(ix)
    if "tank" in ACTS:
        tx, ph = sh_reveal()
        sh_traverse(tx, ph)
        sh_shoot(tx, ph)
    if "glass" in ACTS:
        sh_glass()
    BAR = 0
    fx_reset()
    gc.collect()


# ============================================================================
#  BENCHMARK
#  Run this from the REPL to find out what a frame ACTUALLY costs on YOUR
#  board, instead of trusting anybody's estimate:
#      after boot, press Ctrl-C at the prompt, then
#      >>> import cinema; cinema.bench()
#  It prints the bus cost, three representative frames, and the SPEED value
#  that would give you the runtime you want.
# ============================================================================
NFRAMES = 1272                    # frames in the full cinematic at SPEED = 1.0


def _ms(fn, n):
    gc.collect()
    t0 = time.ticks_us()
    for _ in range(n):
        fn()
    return time.ticks_diff(time.ticks_us(), t0) / (n * 1000.0)


def bench(target_s=40):
    if not _READY:
        print("cinema.init() has not been called"); return 0
    print("SPEED =", SPEED, " FAST_SHOW =", FAST_SHOW, " fast path =", _CANFAST)
    print("--- pushing a frame to the panel ---")
    a = _ms(oled.show, 20)
    print("  driver show()    %7.2f ms" % a)
    best = a
    if _CANFAST:
        b = _ms(fast_show, 20)
        print("  fast_show()      %7.2f ms" % b)
        if b < best:
            best = b
    else:
        print("  fast_show()        n/a (driver has no write_cmd)")

    def f_tank():
        cam(TKX + 24, 34.0, 1.5); frame_begin()
        draw_sun(0); draw_clouds(); draw_far(); draw_mid(); draw_ground(0)
        layer0(); draw_tank(TKX + 9, TKY, 0, 0.0, 0.0, 2)

    def f_desert():
        cam(58, 34, 1.34); scene_launch(0, 0, 1)

    def f_space():
        cam(64, 32, 1.0); frame_begin()
        draw_stars(40, 0); draw_planet(64, 34, 17, 0.3)

    print("--- drawing one frame, nothing pushed ---")
    dt = _ms(f_tank, 8)
    dd = _ms(f_desert, 8)
    ds = _ms(f_space, 8)
    print("  tank shot        %7.2f ms" % dt)
    print("  desert shot      %7.2f ms" % dd)
    print("  space shot       %7.2f ms" % ds)
    print("--- single primitives, us each (includes call overhead) ---")
    print("  oled.line        %7.1f" % (_ms(lambda: oled.line(2, 2, 120, 60, 1), 150) * 1000))
    print("  wline            %7.1f" % (_ms(lambda: wline(2, 2, 120, 60, 1), 150) * 1000))
    print("  fill_rect        %7.1f" % (_ms(lambda: oled.fill_rect(4, 4, 40, 20, 1), 150) * 1000))
    print("  ellipse filled   %7.1f" % (_ms(lambda: fcirc(64, 32, 9, 1), 150) * 1000))
    print("  poly filled(8)   %7.1f" % (_ms(lambda: spoly((10, 10, 40, 14, 44, 40, 8, 36), 1, True), 150) * 1000))
    print("  oled.fill        %7.1f" % (_ms(lambda: oled.fill(0), 150) * 1000))

    frame = best + (dt + dd + ds) / 3.0
    total = NFRAMES * frame / 1000.0
    print("--- verdict ---")
    print("  average frame  ~ %.1f ms  (%.1f fps)" % (frame, 1000.0 / frame))
    print("  full cinematic ~ %.0f s at SPEED = 1.0" % total)
    need = total / float(target_s)
    print("  for ~%d s set   cinema.SPEED = %.1f" % (target_s, need))
    oled.fill(0)
    oled.text("frame %dms" % int(frame), 2, 6)
    oled.text("run   %ds" % int(total), 2, 22)
    oled.text("SPEED %.1f" % need, 2, 38)
    oled.text("for %ds" % target_s, 2, 50)
    show()
    return frame
