# Design decisions

Every decision that shaped the device, including the ones that were rejected and the paths that were abandoned. The README carries the five most consequential; this file carries all of them, with the reasoning that was actually used at the time.

---

## Platform

### RP2040 (Raspberry Pi Pico) as the microcontroller — kept

Considered: an ESP32 or ESP8266, a 5 V AVR, and later an ESP32-C3.

Chosen because the whole design sits in one 3.3 V domain, which matches the 74HC00 and the OLED with no level shifting; because the Pico exposes VBUS sensing and a VSYS ÷ 3 ADC, which turned out to give the battery gauge for free; and because it has no strapping pins to trap a hand-wired board on boot. It is also a widely used part, which matters for a portfolio piece.

Held up. In a retrospective comparison the ESP32-C3 lost on all three counts — no built-in battery sensing, strapping pins on GPIO2/8/9, and exactly six PWM channels against the RP2040's sixteen, where this design uses six. The C3 only wins once connectivity is a requirement.

### 5 V AVR — rejected

Idea only. It would have put the design in a 5 V domain for no gain, and forced level considerations against parts that are happiest at 3.3 V.

### ESP32 / ESP8266 — rejected

Idea only. An unused radio is dead weight on an offline toy, and strapping pins are a boot hazard when pins are being reassigned freely on a hand-wired board.

### ESP32-C3 — rejected on review

Considered after the build, as a check on the original choice. The conclusion was that the Pico was the better fit for this device. The C3 becomes the right answer the moment a leaderboard, phone sync or over-the-air update is added.

---

## Input and debounce

### Discrete SR latch from a 74HC00 — kept

Considered: timed re-sampling in firmware, and an RC filter into a Schmitt-trigger input.

Chosen partly as a deliberate exercise in discrete logic, and partly because it is the right answer for this specific device. An RC debounce needs a time constant longer than the bounce, which adds 1–3 ms of systematic delay to every timestamp on an instrument whose output is a timestamp (calculation 9). The latch adds a gate's propagation delay, tens of nanoseconds. The cost is one extra package, two resistors, and a switch that must have a changeover contact.

Held up, and measured: 6–11 transitions per press on the raw contact, one on the latch output.

### Firmware debounce by timed re-sampling — rejected

Fewer parts and no extra package, and it remains the obvious fallback if the latch were ever dropped. Rejected because the learning goal was the point, and because software debounce moves the delay into the measurement path.

### RC filter plus Schmitt trigger — rejected

Analysed rather than dismissed. It is simpler, works with a single-throw button, and the RP2040's inputs already have Schmitt triggers. Rejected for the 1–3 ms of added latency, for the same reason as above.

### Interrupt lockout in software — rejected

Catching the first edge and masking further edges for a few milliseconds is a legitimate technique. It was rejected because the latch had already been chosen, and stacking a software debounce on top of a hardware one would have hidden the thing being demonstrated.

### 74HC00 rather than 74LS00 — kept

The HC family runs from 2 to 6 V, so it works directly on the 3.3 V rail. LS is 5 V only.

### 10 kΩ latch pull-ups — kept

1 kΩ, 10 kΩ and 100 kΩ were weighed against three criteria: high-level margin, current through a closed contact, and settling time against the shortest bounce gap. 10 kΩ gives 0.98 V of margin, 0.33 mA through a closed contact and 1.1 µs of settling against a roughly 10 µs bounce gap (calculation 1).

### SPDT snap-action micro-switch as the game button — kept

Considered: a plain tactile button, a 30 mm arcade button, and a bare micro-switch.

The tactile button was rejected outright: it is single-throw, and an SR-latch debouncer has nothing to hold state against without a changeover contact. The arcade button was the first choice, for feel, and the Omron D2F-01L was its compact fallback. The built device uses the D2F-01L.

The arcade button was dropped at ordering: it was not available from the distributor carrying the rest of the order, and elsewhere it cost more than expected. The D2F-01L was available, cheaper and more compact, and it satisfies the one electrical requirement that matters — both parts are changeover switches, and both are break-before-make, measured here at 940 µs of open circuit between the contacts. What was lost is the comfort argument that put the arcade button first; a lever micro-switch is a smaller, firmer press.

---

## Timing and game mechanics

### Lights-out as the go signal — kept

The original brief said the lights come on and the player reacts. The Formula 1 procedure is the opposite: the lights go out and the race starts. Changing it cost nothing and made the device match the convention it imitates.

### Five lights, one second apart, 200–3000 ms random hold — kept

Copied directly from the Formula 1 start procedure rather than tuned. The fixed one-second cadence does let a practised player anticipate, which is a real objection to the mechanic; it is kept because the point was to reproduce the procedure.

### Presses under 100 ms accepted rather than rejected — kept

Sprint starting treats anything under 100 ms as a guess rather than a reaction. This device accepts such a press as a result, and uses 100 ms as the threshold for its long animation instead. That is a deliberate choice for a toy played with other people, not an oversight.

### Session best in RAM, no flash persistence — kept

The original v2 spec called for a persistent best time and crash-safe logging to a file. Dropped, because a single lucky sub-50 ms result would sit there permanently and make every later session pointless. A session best resets at power-off and keeps each sitting competitive. The side effect is that the firmware never writes to flash, so flash wear and mid-write corruption stop being concerns at all.

### Persistent best and CSV logging — dropped, could return

Would make the reaction-time distribution in the test notes a by-product of playing rather than something written down by hand.

---

## Power

### 1000 mAh cell rather than 500 mAh — kept

The 1000 mAh cell accepts 1 A at 1C, which is exactly what the TP4056 delivers with its stock 1.2 kΩ program resistor. On a 500 mAh cell that same 1 A is 2C, so it would have needed a resistor swap to about 2.4 kΩ (calculation 6), which is SMD rework on a small module with no hot-air tools on hand. Two requirements converged: the cell that fits the mechanical plan is also the cell that avoids the rework.

### Protected TP4056 Type-C module — kept

Considered: a bare TP4056 without protection, an MCP73871 power-path controller, and two other TP4056 boards from a distributor.

The protected board exposes a separate OUT+ rail, which is the node the design feeds to VSYS, and its DW01A and FS8205 add a protection layer that is redundant with the cell's own. The MCP73871 was rejected because the Pico's own input diode plus the Schottky already produce power-path-like behaviour for free. One of the other two boards exposes only the battery pads, which would have meant hanging the load directly on the cell during charging.

### No power-path controller — kept

With USB present, the Pico's onboard diode carries the load from VBUS and the charger works on the cell alone, so charge termination never sees a load. With the charger's USB-C in use the switch is off and there is no load at all.

### Schottky 1N5817 for source ORing — kept

A silicon diode's 0.7 V would waste twice the headroom on a cell that only spans 3.0 to 4.2 V. Worst case with a Schottky, an empty cell still leaves VSYS about 0.9 V above the converter's minimum; with a silicon diode that margin nearly halves (calculation 5). Measured at 0.28 V under load, which confirms the figure the headroom check used.

### Power switch in the load branch, charging through the module's USB-C — kept

Considered: a switch between cell and charger, which would have blocked charging while off; and tapping the Pico's VBUS so that one port both programs and charges, which was the original plan.

Chosen because it gives a genuine off on battery, because the cell then charges with no load attached, and because it gives the charger module's own connector a purpose. The caveat is that the switch is not a master cutoff when the Pico's micro-USB is plugged in.

### Charging through the Pico's VBUS — rejected after being fully specified

It forced the device to be powered on whenever it was charging, and required a USB source that could supply the charge current plus the running board.

### Stock 1C charge current, program resistor untouched — kept, re-decided once

Revisited deliberately: 0.5C would be gentler on a small pouch cell, and the change is one SMD resistor. Kept stock because that resistor sits on a small module, the rework risks the part, and the benefit is real but modest. The module was placed so it stays accessible if that changes.

### VSYS ADC as the battery gauge — kept

The MAX17048 fuel gauge was selected, specified and then abandoned when it went out of stock everywhere. The Pico's built-in VSYS ÷ 3 ADC replaced it at no cost and no part. It only reads the cell when the device is on battery, which is always, because charging happens with the switch off. The earlier switch decision is what made this gauge viable.

Coarser than a ModelGauge percentage, and measured to read 39–62 mV high (calculation 7). Three buckets and a low-battery warning are the resolution this method actually supports.

### MAX17048 I²C fuel gauge — superseded, still the upgrade path

Out of stock at the time. It drops onto the existing I²C bus without touching anything else.

### Three-LED battery indicator — dropped

An early idea, replaced by icons on the display plus the onboard LED for the warning, which frees pins and needs no parts. No external low-battery LED was ever fitted.

### Stacking UPS module for battery and charging — rejected

A ready-made Pico UPS board would have provided cell, charger, protection and often a fuel gauge in one part. Rejected because designing the power stage was one of the reasons for building the project.

---

## Display and sound

### 0.96 inch 128×64 SSD1306 — kept

A 0.91 inch 128×32 module was considered when the larger one looked expensive. It was rejected for half the vertical space at a higher price and a long lead time. The extra rows are what let a large result number coexist with a footer and corner icons.

### Passive piezo driven straight from a GPIO — kept

An active buzzer produces one fixed tone from DC. A passive transducer takes the PWM frequency as its pitch, which is what makes three distinguishable event sounds possible. It is capacitive and low-current, so no transistor or flyback diode is needed; a magnetic buzzer would have needed both.

### Hardware mute with a sensed position — kept

Firmware mute was rejected: it costs a pin and gains nothing when a switch can simply open the circuit. When the display needed to show the mute state, the switch's spare throw was wired to a pin, so the microcontroller can read the position while the mute itself stays in hardware.

---

## Construction

### Second Pico for the permanent build — kept

The prototype board stayed on the breadboard as a prototyping unit rather than being desoldered into the final build.

### Bare Pico with no headers, soldered flat — kept

A pre-soldered Pico H would have skipped a soldering job but stands off the board and could not be soldered flat. Headers were fitted to the prototype board only.

### Direct-soldering rather than socketing — kept

Socketing hedges against an unproven layout, and the breadboard stage had already removed that risk by the time anything was committed to perfboard. Direct-soldering is also lower and mechanically firmer, which matters for a portable object. The accepted cost is that removing the Pico now means heating around 40 joints without lifting pads.

### Female-header socket — rejected

Only male-to-male headers were on hand, and a male strip is not a socket. Beyond the parts question, the removability a socket buys was already bought by breadboarding first.

### Two stacked perfboards rather than one — forced

A single board could not hold the parts and the connections between them. The upper board carries the Pico, display, LEDs, latch and all three switches; the lower board carries the battery, charger and buzzer. The battery no longer tucks under the Pico as the single-board plan intended.

### Cut corners for mounting — improvised

The perfboard's holes were too small for the standoff screws that had been ordered. Rather than drilling, the corners were cut off and a loop of wire was soldered around each one to form a mounting eye.

### Enclosure — not built

Brainstormed and left out. The stack is a finished object without it, and no enclosure work is planned.

---

## Firmware

### Interrupt-driven button capture rather than polling — kept

The light sequence blocks, so a polled loop would either miss an early press or force the sequence into a state machine. An interrupt fires regardless of what the main code is doing. It works cleanly here specifically because the latch has already removed the bounce; raw-button interrupts are usually painful for the opposite reason.

### False start detected by the presence of the start timestamp — kept

An earlier attempt rejected a round when the computed interval came out negative or zero. That cannot happen naturally and risked a hang. Checking whether the start timestamp exists yet is one comparison in the interrupt handler, and it is exact.

### Non-blocking waits and sounds — kept

`wait_ms` polls in 5 ms steps and returns early on a false start, so a cheat aborts the sequence within about 5 ms rather than at the end of a one-second sleep. The buzzer moved from blocking note sleeps to a one-shot hardware timer after the sounds made the game feel slow. Neither change affects the measurement window, since no tone plays inside it.

### Informal state handling rather than a state machine — kept, with reservations

One button carries three meanings across a round. That is tracked by which section of linear code is running and whether the interrupt is armed, not by a state variable. It is the smallest thing that works for one button, and it is the first thing that would have to change for a second player.

### PWM dimming rather than larger resistors — kept

The LEDs were uncomfortable at full drive. Dimming by duty cycle leaves the resistors and the 8.7 mA peak alone and takes one constant to tune: at 2000/65535 the average falls to a calculated 0.265 mA, measured 0.217 mA (calculations 2 and 3).

### Seeding the generator from hardware entropy — kept

Discovered as a bug: the same hold pattern replayed on every power-up and became learnable. There is no real-time clock to seed from, so the seed comes from the chip's own randomness source.

### Measurement in milliseconds rather than microseconds — kept

An early sketch used the microsecond timer. Millisecond timestamps were chosen as ample against roughly 20 ms of human round-to-round spread, and the crystal contributes 7.5 µs at a 250 ms reaction, four orders of magnitude below that (calculation 8). The scope comparison later showed the whole chain carries a +6.4 ms bias, which is the number that actually matters, and it is not something a finer timer alone would fix.

### Blocking sleeps in the light sequence — replaced

Kept working only because the button sits on an interrupt; refactored into the abortable wait.

### Full-screen invert as the new-best intro — replaced

It read as a reflection on the glass rather than as motion. Replaced by an expanding shockwave ring, then by the bullet and explosion sequence.

### Auto-starting rounds — replaced

The game ran continuously and buzzed at anyone near it. A press-to-start idle screen replaced it.

---

## Failure analysis

### Intermittent "LOW BATTERY", device on with the switch off

**Symptoms.** While the battery was being soldered in, the device powered up with the switch off, showed the low-battery warning, and shut down; a fast off-and-on would not bring it back; and the warning appeared intermittently during testing without being reproducible.

**Reasoning.** A freshly charged cell sits far above the 3.4 V threshold, so the gauge must have actually read low, which points at VSYS losing the battery momentarily rather than at the firmware. The restart behaviour is separate and benign: board capacitance holds the rail up for a second or two, so a fast power flick never produces a clean reset, and a prior deep sleep only clears on one.

**Resolution.** The ground return between the charger module and the Pico was replaced with a heavier wire and a reworked joint. Both symptoms stopped and have not returned. The specific bad joint was never isolated, and the fault has not been reproducible since, so this is recorded as a resolved symptom with a probable cause rather than a confirmed root cause.

### Fifth LED never lit and the random hold disappeared

A pasted edit pulled `wait_ms(hold)` onto the same line as a comment, so it became part of the comment. Two symptoms from one line: no random hold, and a fifth LED that lit and was switched off in the same instant, which looked like a dead output. Fixed by restoring the line.

### OLED refused to initialise

`ValueError: bad SCL pin`, because the requested I²C block did not own the requested pins. The RP2040 hard-maps each of its two I²C controllers to a fixed set of pins.

### Header pins not making contact

Before the prototype's headers were soldered, pins sat in the plated holes with an air gap and made intermittent contact, which is the worst possible surface to write test code on. Soldered rather than worked around.

---

## Calculation log

The numbers the decisions above rest on, in one place. Each is worked in context in [../hardware/README.md](../hardware/README.md) or [../firmware/notes.md](../firmware/notes.md); measured values are in [../test/README.md](../test/README.md).

**1. Latch pull-up, 10 kΩ.** High level: 3.3 − (1 µA)(10 kΩ) = 3.29 V, which is 3.29 − 2.31 = **0.98 V** above the 74HC threshold at Vcc = 3.3 V. Closed-contact current: 3.3 / 10 kΩ = **0.33 mA**. Settling with about 50 pF of node capacitance: τ = 0.5 µs, valid high at roughly 2.2τ = **1.1 µs**, against the roughly 10 µs shortest gap in a bounce train. At 1 kΩ the contact current rises tenfold for nothing; at 100 kΩ settling reaches 11 µs and starts to compete with the bounce itself.

**2. LED series resistor.** R = (3.3 − 2.0) / 0.010 = 130 Ω for a 10 mA target, so the nearest standard value 150 Ω gives (3.3 − 2.0) / 150 = **8.7 mA**. Five lit at once is **43 mA**, within the aggregate budget. A blue or true-green LED at Vf ≈ 3.2 V would leave 0.1 V across the resistor, which is why the design is red.

**3. PWM dimming.** Duty 2000/65535 = **3.05 %**. Calculated average per LED: 8.7 mA × 0.0305 = **0.265 mA**. Measured across one 150 Ω resistor: 32.5 mV, or **0.217 mA**, 18 % low, consistent with the output sagging at the default 4 mA drive strength.

**4. Buzzer series resistor.** With about 20 nF of piezo capacitance, 100 Ω limits each PWM edge to 3.3 / 100 = **33 mA** peak, decaying with RC = **2 µs**. Corner frequency 1 / (2π × 100 × 20 nF) ≈ **80 kHz**, far above the 150–1200 Hz tones, so the resistor does not dull them.

**5. Source-ORing headroom.** VSYS = Vcell − Vf. Empty cell: 3.0 − 0.28 = **2.72 V**, which is 0.92 V above the buck-boost converter's 1.8 V minimum. Full cell: 4.2 − 0.28 = **3.92 V**, inside the 1.8–5.5 V input window. With a silicon diode at 0.7 V the empty-cell case falls to 2.30 V and the margin nearly halves. Diode loss at a 70 mA load: 0.28 × 0.07 ≈ **20 mW**.

**6. Charge current.** The TP4056 sets charge current by its program resistor, I[mA] ≈ 1200 / R[kΩ]. Stock 1.2 kΩ → **1000 mA**, which is 1C on this cell and its rated maximum. 2.4 kΩ → **500 mA** = 0.5C, gentler on the cell and the deferred change.

**7. Battery gauge.** VSYS = raw / 65535 × 3.3 × 3, then cell ≈ VSYS + 0.3 V for the diode. Worked points: a 4.2 V cell gives VSYS ≈ 3.9 V, 1.30 V at the pin, raw ≈ 25 800; a 3.3 V cell gives VSYS ≈ 3.0 V, 1.0 V at the pin, raw ≈ 19 900. Measured at four charge states, the estimate lands **39–62 mV** high, because the true drop is 0.278–0.286 V rather than the fixed 0.3 V, which puts the 3.4 V shutdown at a true cell voltage near **3.34 V**.

**8. Timing budget.** `ticks_ms` resolves to **1 ms**. A 30 ppm crystal contributes 30 × 10⁻⁶ × 250 ms = **7.5 µs** at a typical reaction, against about **20 ms** of human round-to-round spread measured in one session. Neither is the limiting term: the measured end-to-end bias is **+6.4 ms**, which is why the clock never needed attention and the capture path did.

**9. RC debounce, the rejected option.** A filter has to outlast the bounce, so τ ≥ about 2 ms, and the edge the firmware sees is delayed by roughly τ to 1.5τ, or **1–3 ms**, on every single measurement. The latch's own delay is a NAND propagation, tens of nanoseconds, roughly five orders of magnitude smaller.

**10. Runtime, not claimed.** Runtime = capacity × usable fraction ÷ average current. Capacity and the usable fraction are known; the average current was never measured, so no runtime figure is published. Measuring it is an open issue.
