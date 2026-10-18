# FM Tuner (RTL-SDR)

A standalone Windows FM receiver built on an RTL-SDR dongle: tuning, FM
demodulation, audio output, live RDS decoding, an RF spectrum/waterfall and an
MPX baseband display, plus localhost TCP services other programs can read.

This is a **separate program** from the RDS Monitor. It runs on its own and
needs nothing else installed. The RDS Monitor (its own folder / ZIP) is an
optional client that connects to the ports below.

```
   RTL-SDR ──USB──> FM Tuner ──┬── 127.0.0.1:7376  RDS ASCII G  (primary)
                               ├── 127.0.0.1:23    RDS ASCII G  (compat)
                               ├── 127.0.0.1:7373  JSON-lines telemetry
                               └── 127.0.0.1:7374  command channel
```

---

## 1. Install

Full step-by-step instructions: **INSTALL.txt** (Python, packages, Zadig
driver, rtlsdr.dll). Short version:

1. Install Python 3.10+ (64-bit) from python.org, ticking "Add python.exe to
   PATH" and keeping "tcl/tk and IDLE" (tkinter).
2. Double-click `install.bat` (installs `numpy` and `sounddevice`).
3. Run [Zadig](https://zadig.akeo.ie/) once and set the dongle's driver to
   **WinUSB**.
4. Copy the x64 `rtlsdr.dll` and `libusb-1.0.dll` from
   [rtl-sdr-blog Release.zip](https://github.com/rtlsdrblog/rtl-sdr-blog/releases/latest/download/Release.zip)
   next to `FM_Tuner.py` (or point `RTLSDR_DLL` at the DLL).

**HackRF (optional):** install [PothosSDR](https://downloads.myriadrf.org/builds/PothosSDR/)
(SoapySDR), then pick the HackRF in the GAIN panel's **Radio** list. See
INSTALL.txt step 4a.

## 2. Run

**Every day:** double-click **Start Radio.pyw** (in the Radio ZIP) and it opens
all three programs with no console windows. The full illustrated guide to
every screen and button is **OWNERS_MANUAL.pdf**; the short one is
**QUICK_START.pdf**.

To start the tuner on its own, or with options:

```bat
cd path\to\FM-Tuner
python FM_Tuner.py          :: or: python FM_Tuner.py
```

Options:

```
  -f, --frequency 101.1      start frequency (on the chosen region's band)
  -d, --device 0             RTL-SDR device index
      --ppm 0                frequency correction
      --rds-port 7376        primary ASCII G port
      --legacy-rds-port 23   second ASCII G port (0 disables it)
      --telemetry-port 7373  JSON-lines telemetry
      --command-port 7374    command channel
      --no-autostart         open without starting reception
      --no-audio             start muted
      --rds2                 start with the RDS2 stream decoders on (default off)
      --list-devices         list RTL-SDR and audio devices, then exit
```

Port 23 is optional: if something else holds it (often the Windows Telnet
Server) the tuner logs it and keeps running on 7376.

## 3. Controls

Direct frequency entry (validated against the chosen region's band and step),
`-0.2` / `+0.2` with wrap at the band edges, Seek Up/Down, HD Seek up/down
(stops only where the HD decoder locks; waits up to 8 s on each station it
hears; hidden, in the tuner and on the remote page, until the HD decoder is
added). Without the decoder the Audio panel shows only Add HD decoder...;
outside the Americas region every HD control is hidden (no HD Radio there), Stop/Start, manual
or automatic gain (**starts manual at 0.0 dB**), audio on/off, stereo/mono,
Windows output-device selection and volume, live PI/PS/RT.

**Remember gain per frequency** (tick box right under Manual/Automatic, off by
default, saved in settings) with a **Save gain** button next to it: while
it's on and gain is Manual, set the gain you want and click **Save gain**
to keep it for the tuned frequency (Log: "[gain] 99.7 MHz: saved 20.0 dB").
Moving the slider, + / - or the mouse wheel never saves on its own. The
saved gain comes back whenever you tune there again (by hand, preset, seek
or remote) and on Start; the Log shows "[gain] 99.7 MHz: remembered
20.0 dB". A frequency you never saved keeps whatever gain you had. Save
gain with the box unticked or on Auto just tells you what to do. Auto
gain saves and recalls nothing. Saved gains: `settings\gain_memory.json`. The mouse wheel moves
the gain and volume sliders (gain one step at a time), and over the big
frequency in the TUNING panel it tunes one channel per notch.

**Region** (menu; asked on the first start of a new install, saved in
settings): Americas 87.7–108.1 MHz in 0.2 MHz steps on odd frequencies,
Europe / most of the world 87.5–108.0 MHz in 0.1 MHz steps, Japan
76.0–95.0 MHz in 0.1 MHz steps, or Eastern Europe OIRT 65.90–74.00 MHz in
0.03 MHz steps. The ◀/▶ buttons, mouse wheel, arrow keys, seek, the
frequency box and the phone remote all follow it. An existing install
keeps Americas. Presets on another region's band are kept.

### Audio output

The demodulated audio is produced at 48 kHz. If the chosen Windows endpoint
refuses that (MME/DirectSound endpoints often report *Invalid sample rate*),
the tuner reopens it at the rate the device does accept — its own default,
then 44.1/48/32/22.05/16 kHz — falls back to mono and then to the system
default device, and resamples to match. The status line shows the rate in
use, e.g. `audio ready (stereo, 44100 Hz, underruns 0)`, and `audio_rate`
appears in telemetry.

If you hear nothing, check the `Output device` box is your speakers and not
an HDMI/"Display Audio" endpoint; `python FM_Tuner.py --list-devices` prints
what Windows reports.

### SCA subcarrier audio (Listen to: Main / SCA 67 kHz / SCA 92 kHz)

The `Listen to` row in the Audio panel switches the speakers between the
main programme and an analogue **SCA** subcarrier at 67 or 92 kHz (reading
services, background music, ethnic/religious feeds). SCA is a second, narrow
FM signal inside the composite, so the tuner demodulates it straight from the
MPX it already has: mix down by 67/92 kHz, ±7.5 kHz low-pass, FM
discriminate, 5 kHz audio low-pass, 150 µs de-emphasis. Output is mono.

Beside the selector a live readout shows `SCA PRESENT  -xx dB` (green) or
`no SCA carrier` (red) — the subcarrier's level relative to the whole MPX.
Most US stations today carry nothing analogue at 67/92 kHz (digital or
scrambled SCAs and HD Radio replaced them), so expect "no SCA carrier" on
the majority of channels; it only tells you what the station is sending.
The TCP command is `SOURCE MAIN|SCA67|SCA92`, and `audio_source`,
`sca_level_db` and `sca_present` appear in telemetry. Verified with
synthetic SCA signals only; real off-air SCA reception has not been tested.

### Panel layout

The window is built from panels (Tuning, Presets, Signal, RF spectrum + MPX,
Gain, Antenna power, Audio, Live RDS, Localhost services, Log). By default
only the tuner panels are shown; the **View** menu ticks the others on and
off (`Tuner only` = Tuning, Presets and Live RDS; `Show all panels`).
Panels sit in two equal columns: drag any panel by its title bar into either
column, above or below the others (a green line shows where it will drop).
`×` hides a panel; `View > Default columns` puts them back in the standard
split. `View > Always on top` pins the window over other programs - handy
in tuner-only mode. `View > Size` (100-200 %) scales the whole UI for QHD /
4K monitors (takes effect on restart). Columns, order, visibility, size and
window geometry are saved to
`settings/tuner_layout.json` on shutdown and restored at launch. `View >
Layout presets` saves the current arrangement under a name (e.g. "TV 4K",
"QHD desk") and recalls or deletes it later.

`View > Signal units` shows the signal as S-units (S1-S9, then S9+10 ...)
or dB (dBFS); the RDS Monitor and the remote page follow it. The dongle only
measures dBFS, so S-units are relative (S1 at -70 dBFS, 6 dB each, S9 at
-22 dBFS), not calibrated.

All user files - station presets, layout, layout presets - live in the
`settings` folder next to the program. To upgrade, copy that one folder into
the new version's folder (files left in the old locations by earlier builds
are moved in automatically).
Delete it to return to the default.

### Presets

The **PRESETS** row holds as many stations as you like. `+ Save current`
stores whatever you are tuned to, named after the station's callsign/PS when
RDS has decoded one (otherwise the frequency); clicking a preset tunes to it.
Right-click a preset to select it, then `Rename`, `Delete` or the arrows to
move it along the row.

They are written to `settings/tuner_presets.json` next to the program (your
home folder if that is not writable), so they survive restarts and can be
edited by hand. The `settings` folder is not part of the ZIP; copy it into a
new build to keep your presets and layout.
Presets are also published in
telemetry and can be driven remotely:

```
PRESET LIST                     PRESET SAVE [mhz] [name]
PRESET RECALL <n|name|mhz>      PRESET RENAME <n|name> <new name>
PRESET DELETE <n|name|mhz>      PRESET UP|DOWN <n|name>
```

### Arrow-key hotkeys

| Key | Does |
|-----|------|
| Up arrow | Tune up one channel (region step) |
| Down arrow | Tune down one channel (region step) |
| Left arrow | Previous preset (from the first preset it wraps to the last) |
| Right arrow | Next preset (from the last preset it wraps to the first) |

They work anywhere in the tuner window except while you are typing in a box.
Switch them off with *View > Arrow keys*. The same next/previous preset
commands are `PRESET NEXT` / `PRESET PREV` on the command port.

### Remote (phone / tablet / PC) - whole-home audio

The **REMOTE** panel lets any phone or tablet (iPhone, iPad or Android) or
another PC or Mac on your home Wi-Fi control the tuner and listen to it, so one
dongle can play in every room. Nothing is installed on the phone; it is a web
page served by the tuner, so it works in Safari, Chrome or any other browser.

1. Tick **Allow remotes on my home network**. The panel shows the address
   (for example `http://192.168.1.20:7377/`) and a **QR code**.
2. On the phone or tablet, point the camera at the QR code and open the
   link. On another PC or Mac, type the address into a web browser.
3. The first time, Windows may ask about the firewall: tick **Private
   networks** and click **Allow**.
4. On the remote page press **Listen on this device** for the audio.

The page shows the station logo, call letters, slogan, city, range and miles
(from the RDS Monitor and DX Mapper when they are running; otherwise the
tuner's own PS and RadioText), now playing, frequency, stereo/HD badges, and
has -0.2/+0.2, Seek, HD Seek, previous/next preset, Tune to MHz, Stop and the
preset buttons. A phone's lock-screen next/previous buttons change presets.
**Log this station** logs the catch to DX Mapper (like the RDS Monitor's
Log to Map, without the full RDS report), for DXing from another room; the
RDS Monitor and DX Mapper must be running.

- Everyone hears the same station: there is one tuner, so a change from any
  remote changes it for all of them.
- Remote audio is about 1-3 seconds behind the tuner PC.
- **Play on this PC** (shown while the remote is on) turns the tuner PC's own
  speakers off while remotes keep playing. Switching the remote off turns the
  PC's sound back on.
- Only devices on your home network (private addresses) are answered, and
  the page can only tune, seek and recall presets - it cannot change gain,
  delete presets or shut the tuner down. The setting is remembered.

### Bias-T (powered antenna / LNA)

The **ANTENNA POWER** panel has a `Bias-T (4.5 V on antenna port)` checkbox.
It is **off at startup**, asks for confirmation before switching on, and is
switched off again whenever the dongle is released, so the port is never left
live. `python FM_Tuner.py --bias-t` starts with it on, and `BIAST ON|OFF` on
port 7374 does the same remotely (the RDS Monitor's tuner bar has a `Bias-T`
box that sends it).

Only switch it on with a powered LNA or active antenna connected — a
DC-shorted antenna or splitter can damage the dongle. Bias-T needs the
rtl-sdr-blog build of `rtlsdr.dll`; with an older DLL that has no
`rtlsdr_set_bias_tee` the panel shows "not supported by this rtlsdr.dll".

### DX / interference filter (weak station next to a strong local)

`View > DX / interference filter` shows a panel (hidden by default) for the
99.7-beside-99.9 case — a distant station 200 kHz from a strong local:

- **Bandwidth** — the IF (channel) width, 56 to 230 kHz (default 230):
  - `56-80 kHz` deep DX: a weak station right beside (200 kHz from) a strong
    local — its sidebands are cut out entirely; audio is thin and mono, but
    call letters/IDs come through. Too narrow for RDS or stereo.
  - `110-130 kHz` everyday DXing: clean mono and still wide enough to decode
    RDS PI/PS reliably without adjacent-channel bleed.
  - `150-180 kHz` clear slot: full-fidelity mono or light stereo, fast RDS.
  - `200-230 kHz` broadcast standard; open to at least 200 kHz to see a
    station's HD Radio (IBOC) sidebands on the spectrum display.

  The dongle runs at 960 kHz here, so all widths fit; narrower passbands
  reject more of the neighbour's splatter at the cost of treble/stereo/RDS.
- **Protect from** — `Off`, `Auto`, `Low side (-200k)`, `High side (+200k)`.
  Cuts the passband short (to ±60 kHz) on the chosen side only, so the wanted
  station keeps its full width on the clean side. On 99.7 with 99.9 blasting,
  pick **High side**. `Auto` compares the RF level ±200 kHz and protects
  whichever neighbour is 6 dB or more above the other.
- **Canceller** — when a side is protected, subtracts an adaptive estimate of
  the neighbour that still leaks through the filter edge. Optional; the status
  line shows how many dB it is removing.

The status line reports the actual passband, active side, both neighbour
levels and the canceller reduction. The same settings are available on port
7374 (`DX BANDWIDTH|SIDE|CANCELLER …`) and appear in telemetry as `dx`.

**ADC readout** (right of the signal bar): `ADC ok`, `ADC near clip` or
**OVERLOAD** with the wideband level in dBFS. A strong local (under 10 miles)
plus an LNA easily saturates the 8-bit dongle, and once it clips no filter
can undo the damage — reduce tuner gain to 0 dB, lower the LNA gain, or put
an attenuator / FM notch in line. Limits to be clear about: one dongle cannot
separate two stations on the *same* frequency; this reduces adjacent-channel
interference and gives you the diagnostics to see when the front end, not the
DSP, is the bottleneck.

### Radio (RTL-SDR or HackRF)

The **Radio** list at the top of the GAIN panel shows every RTL-SDR and,
when SoapySDR (PothosSDR) is installed, every SoapySDR radio such as a
HackRF. Pick one and the tuner switches to it (restarting if it was
running); **Find** looks again after plugging one in. The choice is saved in
`settings/tuner_layout.json`. A SoapySDR radio is run at a whole multiple of
960 kHz (a HackRF at 1.92 MHz, as it isn't rated below 2 MHz), filtered to
about 1 MHz and sliced to 960 kHz, so the rest of the tuner sees the same
stream as from an RTL-SDR. HackRF gain is 0-102 dB (LNA in 8 dB steps, then
VGA in 2 dB steps, RF amp off). Radios that can't sample at a multiple of
960 kHz (some Airspy models) aren't supported yet. The Log Book Source column
shows the radio's name.

### RF waterfall

The panel shows ±200 kHz around the tuned channel with offset ticks and a
"0 kHz (tuned)" marker. The waterfall scrolls down: the newest line is at
the top. The two shaded blue bands are where HD Radio (IBOC)
digital sidebands live (≈ ±129–198 kHz). The header shows the HD decoder's
verdict: **HD locked** (with call and MER), **No HD lock**, **HD lost** or
**HD decoder not installed**. Only a real decoder lock counts as HD — the old
sideband detector was removed because a strong neighbour's sideband made
adjacent channels look HD.

### HD Radio (optional)

The FM Tuner can decode HD Radio with the open-source **nrsc5** library. The
decoder is **not included** in this download; analog FM works the same
without it ("HD decoder not installed").

**Getting the decoder.** Get or build nrsc5 yourself from
https://github.com/theori-io/nrsc5 (Windows: `libnrsc5.dll`; you also need
its `support/nrsc5.py`). HD Radio is a patented/trademarked system; check
the rules where you live before using it.

**Installing it.** In the Audio panel click **Add HD decoder…** and pick
`libnrsc5.dll` (or a ZIP holding `libnrsc5.dll` and `nrsc5.py`). It is
copied into `settings\hd\`, which updates never touch, so you do it
once. You can also copy the files there yourself and restart the tuner.

**Using it** (Audio panel):

- **Auto** — plays HD once it has locked steadily, fades back to analog when
  HD drops.
- **Manual** — plays the channel you pick and never switches by itself.
- **HD only** — digital audio only; silent when there's no HD.
- **Analog only** — never plays HD audio; the decoder still reads the
  station name and song info.
- **Channel** — lists Analog plus only the HD channels the decoder has
  actually found on this frequency (e.g. HD1, HD2 "The Loop"). It empties on
  every retune.
- **HD channel buttons** — the same choice as one-click buttons in the
  Tuning panel, under the tune/seek buttons: Analog plus a button for each
  HD channel this station has (a channel shows up as soon as its name or
  audio decodes), labelled with the decoded name, e.g. `HD2 · The Loop`.
  The one playing is green. The row only appears on a station with HD and
  clears on every retune. Picking an HD channel while in Analog only
  switches to Manual.
- **Analog is live** — no delay buffer, so manual tuning gives instant
  feedback in every mode. HD runs a few seconds behind analog, so when HD
  drops the fade back to analog skips ahead by that much.

**What the decoder sends on** (telemetry `hd` object, read by the RDS
Monitor): lock/lost lock, MER/BER (the audio panel colours MER green 10 dB+
good, yellow 6-10 dB fair, red under 6 dB weak), station name, slogan, message, facility
ID and location, HD1–HD8 names/types, title/artist/album/genre, emergency
alerts, station logos, album art and traffic/weather maps (shown while
current, never kept), and per-type data counts plus a short event log.
Station logos are saved by the RDS Monitor: HD1 shares the regular
`CALL.png`; HD2 and up are `CALL-HD2.png`, `CALL-HD3.png`, …

Tested so far only on nrsc5's recorded KUT sample (fed through the tuner's
own sample rate) — not yet on a live off-air HD station.

### MPX baseband display

Below the waterfall is the demodulated composite, 0–100 kHz. Everything that
can live in it is labelled three ways: shaded regions with a legend bar under
the ruler, a dashed marker line with name + frequency above the plot for each
carrier, and a status strip beneath listing every carrier with its live level
and a green dot for **present** / grey for **not seen**:

| Marker | What it is |
|---|---|
| L+R audio 0–15k | Mono L+R programme audio |
| 19k pilot | Stereo pilot — present means the station is transmitting stereo |
| 38k L−R (23–53k) | L−R stereo subcarrier (DSB around 38 kHz) |
| 57k RDS | **Classic RDS** subcarrier — a visible bump means RDS is on air |
| 66.5k RDS2-1, 71.25k RDS2-2, 76k RDS2-3 | The three RDS2 subcarriers |
| 67k SCA | Analogue SCA subcarrier (see *SCA subcarrier audio*) |
| 92k SCA/DARC | Second SCA slot, also used by DARC data |

"Present" means the carrier's level is at least 10 dB above the median of the
baseband frame; the pilot/38k and 57k RDS dots follow the stereo and RDS
decoders' own lock instead. All levels are also published in telemetry
(`mpx_pilot_db`, `mpx_stereo_db`, `mpx_rds_db`, `mpx_rds2_db`, `mpx_sca_db`,
`mpx_sca92_db`), so "no RDS transmitted" can be told apart from "RDS too
weak to decode".

### RDS decoding

The 57 kHz subcarrier is taken from the full 240 kHz multiplex (never from
speaker audio): 54-60 kHz bandpass, mixed to baseband with a free-running
57 kHz, 2.4 kHz lowpass, then a biphase (Manchester) matched filter at the
recovered symbol timing, a BPSK axis estimate, differential decode and
syndrome sync with single-bit error correction. It runs in its own OS
process so it cannot stall the audio.

The carrier is recovered from the RDS signal itself - squaring the complex
baseband cancels the data and leaves a tone at twice the carrier offset,
which is measured per block and de-rotated away. Locking to the 19 kHz
pilot instead (tripling its phase) only works on a strong pilot: a weak
one contributes three times its own phase noise, and mono stations carry
RDS without any pilot at all. Offsets up to 200 Hz of dongle clock error
are tracked.

Block sync uses the offset words from the RDS standard (0x0FC, 0x198,
0x168, 0x350, 0x1B4) against a `checkword XOR CRC(data)` syndrome.
Implementations that compute the syndrome by running the block through a
shift register quote a different set (0x3D8, 0x3D4, ...); mixing the two
conventions decodes synthetic signals perfectly and no real station at
all. `tests/offair_mpx_3s.i16` is 3 s of a real multiplex kept as a
regression against exactly that.

Two numbers under the AUDIO line say what the decoder is seeing. `RDS
carrier` is how far the recovered symbols sit from the decision threshold,
and `sync hits` counts valid block syndromes per 1000 bits: under 10 is
what random noise scores, a decodable station reads about 40 or better.
Low on both means the subcarrier is not reaching the decoder (check the
57 kHz level in the MPX header and raise the gain); a good carrier with
few hits means symbols are being recovered but blocks are not aligning.
`Record MPX (30 s / 1 min / 5 min)` capture the composite MPX to a
`mpx_*` file in the working directory (or your user folder if that is not
writable). The default format is a true MPX WAV - 16-bit mono at 240 kHz,
+-75 kHz deviation at 90% of full scale - which RDS Spy, SDR# and audio
tools open directly (1 min is about 29 MB, 5 min about 145 MB); the dropdown switches to raw
float32 (`.f32`, the exact samples the decoder is fed). A `REC m:ss / m:ss (left)` timer runs beside the
buttons while capturing; `Stop` ends a capture early and keeps the file.

IQ is pulled from the dongle with `rtlsdr_read_async`, so USB keeps
streaming while the demodulator works and the multiplex has no gaps -
gaps break both RDS block sync and audio continuity. Very old `rtlsdr.dll`
builds without the async entry points fall back to synchronous reads and
log a warning; use the RTL-SDR Blog DLL if you see it.

---

## 4. TCP interfaces

**ASCII G** (7376, and 23 when free) — one record per decoded group, the
format RDS Spy consumes; several clients may connect at once:

```
G:\r\n694A20E220574156\r\n\r\n
```

**RDS2** — the `RDS2 streams` tick box in the LIVE RDS panel (**off by
default**, remembered in `tuner_layout.json`) runs three extra decoders on
the 66.5 / 71.25 / 76 kHz subcarriers. Every RDS stream has its own
process: classic RDS always gets a core to itself, and each RDS2 decoder
only ever drops its own chunks if the PC cannot keep up (`RDS2 skipped` in
the status line; `dropped` counts classic RDS chunks and should stay 0). A
stream that produces no groups for 8 s drops to a sniff cycle (1.5 s in 6)
so stations without RDS2 cost almost nothing. Their groups go out on 7376
only, tagged with the stream number (`G1:` .. `G3:`), so RDS Spy and other
classic clients on port 23 never see them. The status line shows per-stream
sync hits and the group count. This
has been verified with a synthetic RDS2 multiplex only — almost no US
station transmits RDS2 yet, so treat it as experimental until it has been
seen against a real one. HD Radio is decoded only with the optional add-on
(see HD Radio (optional)).

**Telemetry** (7373) — one JSON object per line, ~2 Hz: frequency, state,
device, gain mode/gain, signal RMS and dBFS, stereo/pilot, MPX deviation,
clipping, audio state, RDS fields, `hd` (decoder state, see HD Radio), `mpx_pilot_db`,
`mpx_stereo_db`, `mpx_rds_db`, `mpx_sca_db`, seek state, ports, server errors.
RDS2: `rds2_enabled`, `rds2_levels`, `rds2_block_hits`, `rds2_groups`,
`rds2_skipped`; classic RDS queue drops: `rds_dropped`.
`snr_db` and `rssi_dbm` are reported as `null` — the RTL-SDR path cannot
measure them accurately, so they are not invented.

**Commands** (7374) — one line in, one JSON line out:

```
TUNE <mhz>      UP | NEXT        DOWN | PREV     SEEK UP | SEEK DOWN
SEEK HD UP | SEEK HD DOWN
STOP            START            SHUTDOWN        GAIN <db>
GAINMODE MANUAL | AUTO           BIAST ON | OFF  AUDIO ON | OFF
STEREO ON | OFF                  RDS2 ON | OFF
VOLUME <0.0-5.0>                 STATUS          HELP
PRESET LIST | SAVE | RECALL | RENAME | DELETE | UP | DOWN
DX BANDWIDTH NORMAL|NARROW|DX    DX SIDE OFF|AUTO|LOW|HIGH    DX CANCELLER ON|OFF
```

Try it by hand:

```bat
python -c "import socket;s=socket.create_connection(('127.0.0.1',7374));s.sendall(b'STATUS\r\n');print(s.recv(65536).decode())"
```

---

**Remote page** (7377, off until ticked in the REMOTE panel; home network
only): `/` page, `/api/status` JSON, `POST /api/cmd {"cmd": "SEEK UP"}`
(TUNE, UP, DOWN, SEEK [HD] UP|DOWN, STOP, PRESET NEXT|PREV|RECALL n,
HD PROGRAM n), `/api/logo`, `/audio.wav` (endless 16-bit PCM WAV of the
tuner's final audio, after HD blending).

## 5. Files

```
FM_Tuner.py              launcher                fm_tuner/    tuner package
INSTALL.txt              plain-text setup steps  tests/       code-level tests
install.bat              installs the packages
requirements.txt         numpy, sounddevice
docs/TUNER_SPEC.md       full spec + install notes
docs/WINDOWS_TESTING.md  hardware test checklist
```

## 6. Testing without a dongle

```bat
python tests\test_suite.py                        :: code-level tests
python tests\fake_tuner.py tests\recording.spy --loop
```

`fake_tuner.py` starts the real TCP services and replays a recorded .spy
capture through them, so a client (the RDS Monitor, RDS Spy, your own script)
can be exercised with no hardware attached.

While tuning by step, seek or typed frequency, any preset on the tuned frequency lights up blue; a right-click-selected preset shows green.
