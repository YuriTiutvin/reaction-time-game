# Development log

How the project actually unfolded, in order. The spec changed three times, two subsystems were replaced after being fully specified, and the build was proven on a breadboard before anything was committed to perfboard.

Built across the summer of 2026, June to August. The phases below are in the order they happened; they are not dated individually, because the notes they come from record sequence rather than calendar days.

## Phase 1 — Brief and first spec

The project started as the simplest thing on a list: "LED lights at a random delay, button press shows your time in ms," budgeted at about a day, with debouncing, timers and a display as the skills to exercise.

That spec lasted one conversation. The mechanic became the Formula 1 start procedure instead: five LEDs light one second apart, hold, then extinguish together after a random delay, and lights-out is the go signal. The constraints were set at the same time — cheap hobby parts, a single 3.3 V logic domain, USB power, breadboard build.

## Phase 2 — v1 hardware design

The design decisions that still stand were all made here: the RP2040 for its single 3.3 V domain and absence of strapping pins, a discrete SR latch for debounce, a changeover switch because the latch requires one, red LEDs on 150 Ω because the forward voltage has to fit under 3.3 V, and an SSD1306 over I²C.

Each value came with a calculation rather than a guess: the pull-up sizing against high-level margin, closed-contact current and settling time; the LED resistor from a stated target current; the latch's output levels against the microcontroller's input thresholds.

## Phase 3 — v1 firmware and first bring-up

The breadboard came up in the order LEDs, display, button and latch. Bring-up produced four distinct problems, in this order:

1. `ValueError: bad SCL pin` on the first display test. The RP2040's two I²C controllers are pin-locked, and the pins requested belonged to the other block.
2. A syntax error from two import styles jammed into one statement.
3. A crash from calling a function by a name it did not have, with a hard-coded number where the result should have been, and an empty function where the first-round animation should have been. The first round is always a new best, so that path always runs.
4. Headers that had not been soldered yet made intermittent contact, which is the worst possible surface to write test code on. Soldered rather than worked around.

Then two behavioural bugs that were more interesting than the crashes. The LEDs were painfully bright at full drive, fixed with PWM dimming rather than new resistors. And the hold pattern repeated on every power-up, because the random generator was never seeded and the board has no clock to seed from.

One edit introduced a bug of its own: a pasted line pulled the random-hold wait onto the end of a comment, which removed the hold and made the fifth LED appear dead when it was actually being switched off in the same instant it lit.

## Phase 4 — v2 spec: portable

The second spec turned the working game into a self-contained device: a lithium cell, charging, a fuel gauge on the display, a buzzer, a press-to-start idle state, and a persistent best time with logging.

Two of those did not survive contact with reality.

The fuel gauge was chosen, specified and wired on paper before it turned out to be out of stock everywhere. The replacement was the Pico's own VSYS ÷ 3 ADC, which cost nothing and works because of a decision made for another reason entirely — charging with the switch off means the device is always on battery whenever it is on.

The persistent best time was dropped on purpose. A lucky sub-50 ms result would have sat there permanently and made every later session pointless, so the best became a session value in RAM, and the firmware stopped needing to write to flash at all.

The charging topology changed too. The original plan tapped the Pico's own VBUS so that one port both programmed and charged; that forces the device to be powered on whenever it is charging. Charging through the charger module's own USB-C with the switch off replaced it, which gives a load-free charge and a genuine off.

## Phase 5 — Power subsystem, tested on its own

The charge subsystem was tested before it was connected to anything downstream: cell, charger, meter, wall adapter. A full cycle was watched live — 4.026 V mid-charge, 4.205 V and 4.217 V through the constant-voltage tail, termination at 4.192 V when the status LED flipped to blue, and 4.175 V five minutes later once surface charge had relaxed.

The diode and switch branch was checked the same way, dead, before it went anywhere near VSYS: diode orientation on the meter, cathode voltage with the switch closed, cathode dead with the switch open, and a continuity check to learn which terminal of the slide switch was live in which position.

That branch check is also where the Schottky measured 0.010 V instead of the expected 0.3 V, because a voltmeter is not a load.

Late in this phase the charge current was re-decided rather than simply inherited: 0.5C would be gentler on the cell, and it is one resistor, but it is an SMD resistor on a small module. Stock 1C was kept, and the module was placed so the swap stays possible.

## Phase 6 — Perfboard build

The breadboard proved the layout, so the permanent build committed to it: a bare Pico soldered flat, no socket, on the first of two stacked perfboards. Two boards rather than one, because the parts and their interconnections would not fit on a single board — the upper board carries the Pico, display, LEDs, latch and all three switches, the lower one the battery, charger and buzzer.

Two things went sideways. The standoff screws did not fit the perfboard holes, so the corners were cut off and a loop of wire was soldered around each one to make a new mounting eye. And while the battery was being soldered in, the device powered up with the switch off, showed a low-battery warning, and refused to restart on a quick flick. Replacing the ground return between the charger and the Pico with heavier wire and a reworked joint ended both symptoms; neither has returned, and the specific bad joint was never found.

## Phase 7 — Measurement

The last phase was the one that changed what this project can claim.

The latch was measured rather than assumed: 6 to 11 transitions per press on the raw contact, exactly one on the latch output, across seven presses.

The timer was measured against a scope over ten rounds, and reads 6.4 ms high on average, in the same direction every time. That was not the expected result, and it is the most useful thing the bench session produced. It is published as it stands, with the cause not yet isolated and a fix tracked as an open issue.

The battery gauge was calibrated at four charge states against a meter, which also finally answered the open question about the Schottky: 0.28 V under the running load, against 0.010 V with only a meter across it.

A 30-round session on the device, plus 30 rounds of the Human Benchmark reaction test on a computer, both played by me in the same sitting, replaced the informal times that had been quoted until then.
