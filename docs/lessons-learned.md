# Lessons learned

Each entry is something I could not explain before this project and can explain now, grouped by concept rather than by the order I hit them.

The hardest part of the build was laying out and soldering the 74HC00 latch on perfboard. The legs are close together, the latch needs cross-connections between gates plus two pull-ups and three wires to an external switch, and all of that had to fit in a small area without bridges. Working out a layout that kept every joint reachable for rework was more of the work than the soldering itself.

---

## Timing

**Resolution is not accuracy.** Resolution is the smallest tick you can read; accuracy is whether that tick is as long as it claims, which is set by the clock source. A crystal at 30 ppm contributes 7.5 µs of error at a 250 ms reaction, and a ceramic resonator at 0.5 % contributes 1.25 ms. Human round-to-round spread in the same sitting is about 20 ms. The clock was never the limiting factor here, which is why matching rigour to the decision matters: the effort belonged in the debounce and the false-start logic, not in the oscillator.

**The whole chain has to be measured, not the timer.** The device stamps milliseconds, and I assumed the rest of the path was negligible because every element in it is fast. Measured against a scope, it reads 6.4 ms high on average and never low. Whatever the cause, the lesson is that the thing to characterise is the path from stimulus to timestamp, not the counter in the middle of it.

**A scheduled interrupt handler is not the electrical edge.** MicroPython's `Pin.irq` defaults to a handler that runs when the interpreter next gets control. For counting presses that difference is invisible; for timestamping them it is part of the measurement.

**Other instruments measure their own latency too.** A computer reaction test records the press after a display pipeline and an input stack have each added their own delay, which is a large part of why the same player scored a 251 ms median there and 202 ms here in the same sitting. Fidelity ranks as purpose-built device, then computer with a key, then a phone touchscreen.

## Switches and debounce

**Contact bounce is mechanical.** Contacts chatter for a few milliseconds as they close. Measured on this switch: 6 to 11 transitions per press.

**A NAND SR latch and why it needs a changeover switch.** Two cross-coupled NAND gates hold a state. Once set, the fed-back output pins the latch, so input wobble cannot unset it. The reason bounce cannot get through is mechanical, not logical: an SPDT wiper breaks the old contact before it makes the new one, so the reset path is physically unavailable while the set contact bounces, and the forbidden both-inputs-low state cannot occur either. A single-throw button has no second contact for the latch to hold against, so it cannot be debounced this way at all.

**Schmitt trigger.** An input with two thresholds and a dead band between them, so a slow or noisy edge has to commit before it flips. It is what rescues an RC-filtered bounce signal, and the RP2040's inputs have it by default, which makes it free noise immunity even where it is not doing the real work.

**Switch vocabulary is two axes, not one.** Single-throw against changeover, and momentary against maintained. The game button has to be momentary and changeover; the power and mute switches have to be maintained.

## Microcontroller I/O

**Pins have two current ceilings.** Per-pin, set by the output transistor and bond wire, and aggregate, set by the shared supply pins. Both exist on every chip.

**Drive strength sets output impedance, not a limit.** The RP2040 offers 2, 4, 8 and 12 mA settings and defaults to 4 mA. Asking for 8.7 mA at the default setting does not blow anything up; the pin simply sags, which is the most likely reason the measured LED current came in 18 % under the calculation.

**LED forward voltage eats the rail.** R = (V − Vf) / I. On 3.3 V a red LED's 2.0 V leaves usable headroom, while blue or true green at 3.0–3.4 V barely light. PWM at a low duty dims by averaging, without touching the resistor or the peak current, and perceived brightness is far from linear in duty.

**Hardware I²C is pin-locked per controller.** The RP2040 has two I²C blocks, each hard-mapped to a fixed set of pins, and SDA and SCL must both belong to the block being opened. `SoftI2C` bit-bangs anywhere if it ever has to.

**PWM slices are shared.** Pins map onto slices in pairs, and the frequency belongs to the slice. The LEDs and the buzzer landed on different slices, which is why changing a tone never disturbed the dimming.

## Power

**The Pico's rail, end to end.** USB 5 V arrives at VBUS, passes an onboard diode to VSYS, and a buck-boost converter makes 3.3 V from anything between 1.8 and 5.5 V. That is why a LiPo can feed VSYS directly with no boost stage, and why the battery can never back-feed the USB rail. The Pico has no charger of its own.

**Diode ORing.** A diode lets the higher source feed the rail and blocks reverse flow. A Schottky's low drop preserves headroom that a silicon diode's 0.7 V would waste on a cell that only spans 3.0 to 4.2 V. Cathode toward the load.

**A component parameter only means something at its operating point.** I measured the Schottky with nothing but a voltmeter across it and got 0.010 V, not the 0.3 V I expected — because a meter draws microamps, and forward voltage climbs with current. Under the running load the same diode measures 0.28 V. Datasheets quote forward voltage at a stated test current for exactly this reason. The same habit applies to a cell: read its settled resting voltage, not the value the charger is holding it at.

**Lithium charging is constant current, then constant voltage, then stop.** A cell has no internal regulation, so a charger IC holds the current until 4.2 V, then holds the voltage while the current tapers, and terminates near a tenth of the charge rate. Charge current is set by one program resistor, roughly 1200 divided by its value in kilohms. "1C" means the cell's rated maximum, so charging at 1C sits at the ceiling rather than inside it.

**Two guards at the top of charge, at different voltages, on purpose.** The charger terminates at 4.2 V and is the only thing that acts in normal operation. The protection board does nothing until about 4.28 V and exists for the case where the charger has failed; its "4.00 V" figure is the recovery point of a hysteresis window, not a charge ceiling.

**Nominal voltage is a label, not a limit.** A single cell is roughly 4.2 V full, 3.7 V mid and 3.0 V empty. The 3.7 V on the label is the representative middle used for capacity maths. Charging to 4.2 V is correct, not abuse.

**A charger is not a power-path controller.** Hanging a load on the cell during charging corrupts termination sensing, because the charger cannot tell load current from charge current. This design sidesteps it twice: the Pico's own diode carries the load when USB is present, and the charger's port is only used with the switch off.

**Fuel gauging, two ways.** A ModelGauge IC reads true state of charge over I²C and handles the flat middle of the discharge curve and load sag. Reading voltage through an ADC costs nothing and gives a coarse, one-sided estimate — measured here at 39 to 62 mV high, which is fine for three buckets and not enough for a percentage.

**Capacitors keep a board alive after the switch opens.** Decoupling holds the rail for a second or two, so a fast off-and-on never gives a clean reset. Combined with a deep sleep that only clears on reset, that fully explains a device that will not come back after a quick flick.

## Firmware craft

**Blocking calls are a design decision.** A blocking sleep freezes everything for its duration. That is fine for the light sequence only because the press arrives on an interrupt; it was not fine for sounds, which is why the buzzer moved to a timer-driven sequencer that returns immediately.

**Random needs entropy, and an RTC-less board has none at boot.** An unseeded generator replays the same sequence every power-up, which I found by noticing the hold pattern was learnable. There is no real-time clock, so the seed has to come from the chip's hardware randomness.

**A one-bit display is just primitives.** Nothing appears until the frame buffer is pushed. The built-in font is fixed at 8 px, but rendering it into a small buffer and drawing an N×N block per lit pixel gives scaled text with no font library. Animation is position, velocity, gravity and a little z-ordering.

**Persistence I learned and chose not to use.** RAM resets at power-off; keeping a value means writing to the flash filesystem, wrapping the first read in a handler for the file not existing yet, writing only when the value changes to limit wear, and writing only between rounds so a power cut cannot corrupt it mid-write.

## Process

**A value is downstream of a spec.** I twice reached for a number before stating what it had to achieve — a 220 Ω LED resistor before a target current, and "a small ESP" before a requirement. The resistor follows from the target current and the forward voltage; the part follows from the constraints.

**The same job can live in hardware or software, at different cost.** Debounce, mute and charge current each had a hardware and a software form. Choosing where a job lives, and paying the right price for it, is the actual skill.

**Decisions interact.** Charging with the switch off is what made the free VSYS gauge valid. The 1000 mAh cell is what removed the SMD rework. Neither was the reason for the original decision.

**Sourcing shapes the design.** The fuel gauge went out of stock and changed the gauge architecture. Availability and price moved the charger board, the cell and the display.

**Mechanical edits introduce bugs.** A comment that swallowed a line produced two unrelated-looking symptoms, and two import styles jammed together produced a syntax error. Paste carefully and read the result.

---

## Firsts

First time doing each of these: designing and hand-wiring a discrete logic circuit; driving an SSD1306 over I²C and doing frame-buffer graphics; MicroPython interrupts, PWM, ADC and hardware timers; designing a lithium charge and power path with source selection, a true-off switch, battery sensing and a low-battery shutdown; and soldering headers and a full mixed build. Using a multimeter to separate a firmware fault from a connection fault was not new, but this project is where it became routine, along with reading a two-channel capture to settle a design claim.
