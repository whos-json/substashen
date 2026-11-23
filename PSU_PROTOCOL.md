# MSI MPG Ai1300TS PCIE5: USB HID protocol

How this file was built:
- The frame format and command list come from statically decompiling MSI Center's
  `C:\Program Files (x86)\MSI\MSI Center\Power Supply Unit\API_Power Supply Unit.dll` (class `API_PSUMonitor.cPSU`).
- The READ_ALL layout was checked against live HWiNFO values on 2026-11-23, using only passive listening.

## Device
- USB VID `0x0DB0`, PID `0xAA6F`, product "MPG Ai1300TS". Interface 0, a single top-level collection.
- Usage page `0xFF00`, usage `0x01`.
- Input and output reports are both 65 bytes: report ID 0 plus 64 data bytes. There are no feature reports.
- MSI Center wraps every transaction in the named mutex `Global\MSI_PSU_Mutex`. Any other program that sends requests must take it too.

## Frames
- **Request:** `[0]=00`, `[1]=51` (READ), `[2]=command`, then all zeros.
- **Response:** `[1]` and `[2]` echo the request. Data starts at `[3]`. `[3]=FE` means busy/NAK. There is no checksum.
- **Byte `[1]` values:**
  - `0x50` = WRITE. Never send it.
  - `0xFA` = handshake. MSI sends `FA 51` first.
- **Values:** 16-bit little-endian, PMBus LINEAR11.
  - `exp = signed(hi >> 3)` (5 bits)
  - `mant = ((hi & 7) << 8) | lo`
  - `value = mant * 2**exp`

## 0xE0 READ_ALL (verified live)
| Offset | Reading | Unit |
|---|---|---|
| 3 / 5 / 7 | 12V / 5V / 3.3V current | A |
| 9..19 | 12V-2x6 cable 1, pins 1-6 | A |
| 21..31 | 12V-2x6 cable 2, pins 1-6 | A |
| 33 | Output power | W |
| 35 | Efficiency | % |
| 37 | Temperature | °C |
| 39 | Fan | rpm |
| 41 / 43 / 45 | 12V / 5V / 3.3V voltage | V |

## Other read commands (from the decompile; not yet checked live)
- **`0xC0`, OCP config:**
  - `[3]` enable
  - `[4,5]` OCP point
  - `[6,7]` pin-difference threshold
  - `[8..10]` trigger and warning times
- **`0xC1`, OCP state per cable:**
  - `[3]` status: 0 normal, 1 OCP point, 2 current difference, 3 OCP 18 A
  - then runtime u32, total time u32, 6 pin currents
  - the second cable's block starts at `[24]`
- **`0xE1`, alert flags:** `[3..20]`, in this order: OCP 12V, OCP 5V, OCP 3.3V, the 12 pin OCPs, OPP, OTP, fan.
- **`0x42`, fan duty:** actual, calculated, set, zero-fan.
- **`0xD0` / `0xD1`, runtime:** current and total, u32 seconds.
- **`0x10`–`0x13`:** manufacturer ID, model, revision, serial.

## Sending requests yourself (from MeltAlarm, which was checked on a real Ai1300TS)
- After a cold boot the PSU answers no reads until a host has sent the handshake `00 FA 51` once.
  MSI Center sends this handshake when it starts.
- Before writing a request, drain any pending input. Then ignore replies whose echo doesn't match:
  replies to MSI Center and HWiNFO arrive on every open handle.
- Deadlines: 500 ms for a reply, 2 s for the mutex.
- A reply of `<op> <reg> FE` followed by all zeros means busy. A lone `FE` can be real data.
- `C0` read reply on this unit (the PSU's own GPU Safeguard+ settings, per the reference capture):
  enabled, 12 A per pin, 5.5 A imbalance, 20 s trigger, 180 s cutoff.
  The PSU therefore has its own hardware cutoff above our 8 A software alert.

## Energy data (verified 2026-11-23)
- The PSU stores only two counters, both u32 little-endian seconds at `[3..6]`:
  - `51 D0`: time since power-up. This is MSI Center's "Daily Activity".
  - `51 D1`: lifetime. This is MSI Center's "Total Time".
  - Both read live in 6 ms and matched MSI Center's `Profile.cfg` to the second.
- The PSU stores **no kWh**. MSI Center calculates "Total kWh" as its own average P_out × lifetime hours. It keeps that total and the weekly chart in
  `C:\ProgramData\MSI\MSI Center\Power Supply Unit\Profile\Profile.cfg`
  (`[Data]` `kWh_Total`, `Wattage_Total`, `Efficiency_Total`, `Count`; `[WeeklyChart]` `MM-dd-yyyy=kWh`).
- `Global\MSI_PSU_Mutex` is created by MSI's service with rights that normal (non-admin) programs don't get:
  OpenMutex returns access denied. Substashen uses the lock when it's allowed and otherwise retries reads.
  A collision can only make a read fail; it can't change anything.

## Prior art
- **MeltAlarm** (Rust, read-only, tested on an Ai1300TS): https://github.com/DeniDoman/meltalarm
- **RTXLens** (C#): https://github.com/Codeh4ck/RTXLens
- **msi-psu-monitor** (Python, Linux; register map; untested on TS hardware): https://github.com/boboom66/msi-psu-monitor

## Never send
Every frame with `[1]=0x50`, specifically:
- `41` fan mode
- `43` zero-fan
- `C0` OCP settings
- `C2` buzzer
- `F1` save settings to non-volatile memory

Frames with `[1]=01` or `02` are LED writes; never send those either.
