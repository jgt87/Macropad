# MINI KeyBoard (VID 0x1189 / PID 0x8890) — HID configuration protocol

Reverse-engineered from the vendor app `MINI KeyBoard.exe` (internal name **HIDTester**,
a .NET Framework 4.0 WinForms app using the open-source *HidLibrary*). The `.exe` is a
plain managed assembly — not encrypted, not obfuscated, and shipped with `.pdb` symbols —
so it decompiles cleanly. (The decompiled C# is not redistributed here; this document records
everything recovered from it.)

## Device

| | |
|---|---|
| Vendor ID | `0x1189` (4489) |
| Product ID | `0x8890` |
| Composite | 4 interfaces (MI_00..MI_03) |
| **Config channel** | **MI_01** — vendor-defined HID, Usage Page `0xFF00`, Usage `0x01` |
| MI_00 / MI_02 | the actual HID keyboard(s) the OS sees when you press a key |
| MI_03 | vendor-defined (unused by the config app) |

The vendor app finds the config channel by enumerating VID `0x1189` and picking the
interface whose device path contains `mi_01` (case-insensitive).

### Report descriptor (MI_01)

```
06 00 FF   Usage Page (Vendor 0xFF00)
09 01      Usage 1
A1 01      Collection (Application)
85 03        Report ID (3)
09 02        Usage 2
15 00        Logical Min 0
26 00 FF     Logical Max 255
75 08        Report Size 8
95 40        Report Count 64
81 06        Input  (Data,Var,Rel)     <- 64-byte input report,  id 3
09 02        Usage 2
75 08 95 40  (same 64 bytes)
91 06        Output (Data,Var,Rel)     <- 64-byte output report, id 3
C0         End Collection
```

So every command is an **output report**: `[0x03][64 data bytes]` = **65 bytes total**.
Only the first **8** data bytes are meaningful; the rest are zero-padded.

## Frame layout (the 8 meaningful data bytes)

```
byte 0  key           physical key number, or a special opcode (0xA1 / 0xAA / 0xB0)
byte 1  type/layer    (layer << 4) | type      (low nibble = type, high nibble = layer)
byte 2..7             type-specific (see below)
```

`type` (low nibble of byte 1):

| type | meaning |
|------|---------|
| `1`  | keyboard key / multi-key macro |
| `2`  | multimedia (consumer control) |
| `3`  | mouse button / wheel |
| `8`  | LED backlight mode |

> The original app also supports an older firmware variant that uses Report ID 0 (no layer
> nibble in byte 1) or Report ID 2 (different multimedia codes). It auto-detects by probing
> IDs 3 → 0 → 2. **This device only defines Report ID 3**, which is what the tool targets.

## Physical key numbers

**This physical unit = 6 keys + 1 knob** (6 LEDs, one per key): keys `1`–`6`; knob =
`13` (turn one way) / `14` (press) / `15` (turn other way).

The protocol itself is generic and also covers larger models the same vendor app supports:

| number | input | number | input |
|--------|-------|--------|-------|
| 1–12 | keys | 16 | knob 2 turn left |
| 13 | knob 1 turn left | 17 | knob 2 press |
| 14 | knob 1 press | 18 | knob 2 turn right |
| 15 | knob 1 turn right | 19–21 | knob 3 left / press / right |

## Commands

### Layer switch (sent before each assignment when Report ID ≠ 0)
```
[0xA1, layer, 0,0,0,0,0,0]      layer is 1-based; 0 is coerced to 1
```

### Keyboard key / macro (type 1)
A macro is 1..5 keystrokes. Each keystroke = `(modifier, keycode)`.
Sent as several reports, then a save:
```
switch_layer(layer)
[key, (layer<<4)|1, count, 0,        mod0, 0   ]   # header (first modifier, keycode 0)
[key, (layer<<4)|1, count, 1,        mod0, key0 ]
[key, (layer<<4)|1, count, 2,        mod1, key1 ]
 ... up to keystroke `count` ...
save                                                # [0xAA, 0xAA, ...]
```
`modifier` bitmask: **Ctrl 0x01, Shift 0x02, Alt 0x04, Win/GUI 0x08**.
`keycode` = standard USB HID Keyboard usage (page 0x07): `a`=0x04 … `z`=0x1D,
`1`=0x1E … `0`=0x27, Enter 0x28, Esc 0x29, Backspace 0x2A, Tab 0x2B, Space 0x2C,
F1..F12 = 0x3A..0x45, arrows Right/Left/Down/Up = 0x4F..0x52, etc.

### Multimedia (type 2)
```
switch_layer(layer)
[key, (layer<<4)|2, code, 0]
save
```
Consumer-control codes for this firmware: Play/Pause `0xCD`, Prev `0xB6`, Next `0xB5`,
Mute `0xE2`, Vol+ `0xE9`, Vol- `0xEA`, Stop `0xB7`.

### Mouse (type 3)
```
switch_layer(layer)
[key, (layer<<4)|3, buttons, 0, 0, wheel, modifier]
save
```
`buttons`: Left 0x01, Right 0x02, Middle 0x04. `wheel`: up `0x01`, down `0xFF`.
`modifier`: Ctrl 0x01 / Shift 0x02 / Alt 0x04 (for e.g. Ctrl+scroll to zoom).

### LED backlight (type 8)
```
[0xB0, (layer<<4)|8, mode]         mode selects a firmware effect preset
save_led                            # [0xAA, 0xA1, ...]   (note: 0xA1, not 0xAA)
```
The device has **6 RGB LEDs**. Verified effect map (from a clean power-on baseline):

| mode | effect | colour |
|------|--------|--------|
| 0 | off | — |
| 1 | one LED steady (top-left) | fixed white |
| 2 | colour-cycle chase across all 6 | changes each lap |
| 3 | pulse/breathing on one LED | frozen on whatever colour the cycle was showing |
| 4+ | undefined | no-op |

**There is no colour/brightness/per-LED command.** The vendor app only ever sent the
`mode` byte (bytes 3-7 stay 0). Tested: putting RGB values in the unused bytes or changing
the target byte (0xB0→0xB1) alters the *animation* but never sets a chosen colour — colour
only comes from the built-in cycle. The one way to land on a specific colour: run mode 2,
wait for the colour you want, then switch to mode 3 to freeze it. Arbitrary/per-key RGB
would require reflashing the MCU with custom firmware. Sending undefined modes while the
controller is in a dirty state can latch odd behaviour; unplug/replug to reset.

### Save / commit
* Normal (keyboard / media / mouse): `[0xAA, 0xAA, ...]`
* LED: `[0xAA, 0xA1, ...]`

The save report tells the MCU to persist the just-sent assignment to on-board flash, so
configuration survives unplugging and works on any computer without software running.

## Notes / unknowns
* There is an **input** report (id 3, 64 bytes) but the vendor app only uses it as an
  ack; there's no observed "read current configuration" command. Assignments are
  write-only from the host's perspective.
* Byte 0 special opcodes seen: `0xA1` layer switch, `0xAA` save, `0xB0` LED target.
* Max 5 keystrokes per macro (the app's send loop only encodes cases 0–5).
