# Interconnect

Every connection in the built device, confirmed against the assembled perfboard. Pico pin numbers are physical package pins; GPxx numbers are what the firmware uses.

## Pico signals

| Signal | Pico GPIO (pin) | Connects to | Net | Type | Notes |
|---|---|---|---|---|---|
| LED 1 | GP0 (1) | 150 Ω → LED anode; cathode → GND | LED0 | PWM | 1 kHz, duty 2000/65535 |
| LED 2 | GP1 (2) | 150 Ω → LED anode; cathode → GND | LED1 | PWM | as above |
| LED 3 | GP2 (4) | 150 Ω → LED anode; cathode → GND | LED2 | PWM | as above |
| LED 4 | GP3 (5) | 150 Ω → LED anode; cathode → GND | LED3 | PWM | as above |
| LED 5 | GP4 (6) | 150 Ω → LED anode; cathode → GND | LED4 | PWM | as above |
| Button, latch output | GP15 (20) | 74HC00 pin 3 | Q | digital, rising-edge IRQ | internal pull-up enabled in firmware; the latch drives push-pull and overrides it |
| OLED SCL | GP11 (15) | OLED SCL | I2C1_SCL | I²C | block 1, 400 kHz |
| OLED SDA | GP14 (19) | OLED SDA | I2C1_SDA | I²C | block 1 |
| OLED VCC | 3V3(OUT) (36) | OLED VCC | 3V3 | power | |
| OLED GND | GND | OLED GND | GND | power | |
| Buzzer | GP8 (11) | 100 Ω → piezo → mute switch → GND | BUZZ | PWM | duty 24000/65535 while a note sounds |
| Mute sense | GP7 (10) | mute switch, second throw | MUTE | digital, internal pull-up | high = sound on, low = muted |
| USB detect | GP24 (internal) | Pico VBUS sense | VBUS_SENSE | digital | high = USB present |
| Battery sense | GP29 / ADC3 (internal) | Pico VSYS ÷ 3 divider | VSYS_ADC | analog | valid only on battery |
| Onboard LED | GP25 (internal) | onboard LED | LED_BOARD | digital | battery-boot flash, low-battery blink |
| VSYS | pin 39 | 1N5817 cathode | VSYS | power | battery feed |
| 3V3(OUT) | pin 36 | 74HC00 pin 14, both 10 kΩ pull-ups, OLED VCC | 3V3 | power | single 3.3 V logic domain |
| GND | GND pins | common star at TP4056 OUT− | GND | power | one ground node for battery, Pico, OLED, LEDs, buzzer |

## SR latch, SN74HC00N (DIP-14)

Two of the four NAND gates, cross-coupled. Gate 1 drives Q, gate 2 drives Q̄.

| 74HC00 pin | Function | Connects to |
|---|---|---|
| 1 (1A) | /S | switch NO contact, plus 10 kΩ pull-up to 3V3 |
| 2 (1B) | Q̄ feedback | pin 6 |
| 3 (1Y) | **Q output** | pin 5, and Pico GP15 |
| 4 (2A) | /R | switch NC contact, plus 10 kΩ pull-up to 3V3 |
| 5 (2B) | Q feedback | pin 3 |
| 6 (2Y) | **Q̄ output** | pin 2 |
| 7 | GND | GND |
| 14 | Vcc | 3V3 |
| 9, 10, 12, 13 | unused gate inputs | GND, so the unused CMOS inputs cannot float and oscillate |
| 8, 11 | unused gate outputs | unconnected |
| — | switch COM | GND |

At rest the wiper sits on NC, so /R is low and Q is low. On a press the wiper leaves NC before it reaches NO, so /R goes high (hold) and then /S goes low (set), and Q rises once. While the NO contact bounces, /S alternates between set and hold, both of which keep Q high, and the reset path is physically unavailable because the wiper cannot be on NC at the same time.

## Power path

| From | To | Notes |
|---|---|---|
| LiPo red lead | TP4056 B+ | cell has its own protection board |
| LiPo black lead | TP4056 B− | |
| TP4056 OUT+ | power switch COM (middle pin) | protected cell output, roughly 3.0–4.2 V |
| power switch, one outer pin | 1N5817 anode | third pin left floating; the switch is an SPDT used as SPST |
| 1N5817 cathode (banded end) | Pico VSYS, pin 39 | ORs the battery onto VSYS and blocks reverse flow |
| TP4056 OUT− | Pico GND | the common ground star |
| TP4056 USB-C | wall adapter, charging only | charged with the power switch off |
| Pico micro-USB | programming | powers VSYS through the Pico's own D1 regardless of the switch position |

Three one-way elements fence the cell: the Pico's onboard D1 conducts VBUS to VSYS only, the 1N5817 conducts battery to VSYS only, and the TP4056 conducts input to battery only.

## Buzzer and mute switch

| From | To | Notes |
|---|---|---|
| GP8 | 100 Ω series resistor | limits the capacitive inrush of the piezo |
| 100 Ω | piezo terminal A | piezo is not polarised |
| piezo terminal B | mute switch, first throw | grounding this throw enables sound |
| mute switch COM | GND | |
| mute switch, second throw | GP7 | firmware reads the switch position; the mute itself stays in hardware |

## Series and pull-up resistors

| Location | Value | Purpose |
|---|---|---|
| 74HC00 pins 1 and 4 | 10 kΩ each, to 3V3 | latch input pull-ups |
| Each LED | 150 Ω | current limit |
| Buzzer | 100 Ω | inrush limit at each PWM edge |
| GP7 mute sense | Pico internal pull-up | |
| GP15 latch input | Pico internal pull-up, enabled but not load-bearing | |

## Pin assignment history

Earlier drafts proposed I²C on GP0/GP1, then GP6/GP7; the LEDs on GP0–GP4 forced the bus onto block 1, and the final pins are GP11 and GP14. The latch output moved from GP7 to GP9 to GP10 during OLED bring-up and settled on GP15. The buzzer moved from GP8 to GP21 and back to GP8, and mute sense from GP20 to GP7. Pins were chosen for what physically fitted the board, then locked before the perfboard build. On the RP2040, GP0–GP4 land on PWM slices 0, 1 and 2 and the buzzer on slice 4, so setting the buzzer frequency never disturbs the LED dimming.
