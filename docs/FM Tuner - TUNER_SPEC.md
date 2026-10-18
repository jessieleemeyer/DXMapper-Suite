# FM Tuner — specifications and installation notes

Standalone Windows FM receiver for RTL-SDR dongles. It owns the hardware,
demodulation, audio, RDS decoding and the display panels, and publishes
everything over localhost TCP so the RDS Monitor (or any other client) never
has to touch the radio.

---

## 1. Specifications

### Platform

| Item | Value |
|---|---|
| OS | Windows 10/11 (64-bit); the code is portable but only Windows is supported |
| Python | 3.10 or newer, 64-bit, with tkinter |
| GUI | tkinter, single window, minimum 560 × 880 px; colour-coded signal meter (red < −45 dBFS, yellow < −30 dBFS, green above) |
| Hardware | Any RTL2832U dongle (R820T/R820T2/R828D/E4000/FC00xx) on WinUSB |
| Driver | Zadig WinUSB + `rtlsdr.dll` (x64) loaded by ctypes — no pyrtlsdr |
| CPU | ~1 modern core; 960 kS/s IQ, FFTs at ~15 Hz display rate |

### Signal path

| Stage | Value |
|---|---|
| IQ sample rate | 960 kHz (`IQ_RATE`), 8-bit I/Q from the RTL2832U |
| USB read size | 131 072 bytes ≈ 68 ms per read (`READ_BYTES`) |
| Channel filter | 95-tap complex FIR low-pass, 120 kHz cutoff (Kaiser β 6), applied to the IQ before demodulation so adjacent 200 kHz channels cannot capture the discriminator; signal meter and seek read the filtered channel level |
| Demodulation | Polar discriminator, `angle(x[n] · conj(x[n−1]))` |
| Composite (MPX) rate | 240 kHz after 4× decimation — keeps the 57 kHz RDS carrier |
| Audio rate | 48 kHz after a further 5× decimation |
| Stereo | 19 kHz pilot recovery, 38 kHz L−R decode, auto mono fallback |
| Stereo lock | pilot/MPX amplitude ratio > 0.06 (`PILOT_STEREO_THRESHOLD`) |
| De-emphasis | 75 µs (US) |
| Audio output | `sounddevice` (WASAPI/DirectSound/MME), selectable device, volume 0.0–5.0; if the endpoint refuses 48 kHz stereo the stream is reopened at the device's own rate (then 44.1/48/32/22.05/16 kHz), mono, or the system default device, and resampled — the rate in use is reported as `audio_rate` |
| RDS | 57 kHz subcarrier, 1187.5 bps, poly `0x5B9`, standard block offsets, in a separate process |

### Tuning

| Item | Value |
|---|---|
| Band | 87.7 – 108.1 MHz (`BAND_MIN_TENTHS` 877 … `BAND_MAX_TENTHS` 1081) |
| Channel grid | 0.2 MHz (`STEP_TENTHS`), off-grid entries are rejected |
| Step | ± 0.2 MHz, wraps at both band edges |
| Seek | up/down, stops on a channel above −34 dBFS (`SEEK_MIN_DBFS`), 3 reads discarded after each retune |
| Stop | halts seeking, reception continues |
| PPM | frequency correction via `--ppm` |
| Gain | starts **manual at 0.0 dB**; manual (from the dongle's supported table) or auto; RTL2832 digital AGC is switched off before streaming |
| Bias-T | 4.5 V on the antenna port via `rtlsdr_set_bias_tee` (falls back to `rtlsdr_set_bias_tee_gpio`); **off at startup**, switched off again when the device is released, `--bias-t` to start it on |

### RF spectrum / HD waterfall

| Item | Value |
|---|---|
| Span | ±200 kHz around the tuned channel (`SPECTRUM_SPAN_HZ` 400 kHz) |
| Resolution | 2048-point FFT → 256 display bins |
| Waterfall | 96 rows, −95 dB to −25 dB colour scale |
| HD sidebands | ±129–198 kHz shaded and labelled |
| HD verdict | from the optional nrsc5 decoder only: "HD locked (CALL, MER)", "No HD lock", "HD lost", or "HD decoder not installed" |
| Overlays | offset ticks (−150…+150 kHz) and a "0 kHz (tuned)" centre marker |

The shaded bands only mark where the sidebands sit; the HD verdict comes from
an actual decoder lock (the sideband-level detector was removed).

### MPX baseband display

| Item | Value |
|---|---|
| Span | 0–100 kHz (`MPX_SPAN_HZ`) |
| Resolution | 4096-point FFT → 256 display bins |
| Scale | −80 dB to 0 dB, 10 kHz tick ruler, named legend bar along the bottom |
| Regions shown | 0–15 kHz L+R audio · 19 kHz pilot · 23–53 kHz L−R stereo · 57 kHz RDS · 67 kHz SCA · 92 kHz DARC |
| Measured carriers | peak within ±1.5 kHz of 19 / 38 / 57 / 67 kHz, printed in the header and published in telemetry |

### Network interfaces (all bound to 127.0.0.1)

| Port | Protocol | Purpose |
|---|---|---|
| 7376 | ASCII "G" records (`G:\r\n` + 16 hex chars) | primary RDS group stream, multiple clients |
| 23 | same | optional SDR#-`RDSOutput` compatibility port; if it is taken or blocked the tuner logs it and keeps running |
| 7373 | JSON lines, ~2 Hz | telemetry / status |
| 7374 | text or JSON commands, one JSON reply per line | remote control |

Commands: `TUNE <mhz>`, `UP`/`NEXT`, `DOWN`/`PREV`, `SEEK UP|DOWN`, `STOP`,
`START`, `SHUTDOWN`, `GAIN <db>`, `GAINMODE MANUAL|AUTO`, `BIAST ON|OFF`,
`AUDIO ON|OFF`, `STEREO ON|OFF`, `VOLUME <0.0-5.0>`, `PRESET LIST|SAVE|
RECALL|RENAME|DELETE|UP|DOWN`, `DX BANDWIDTH <56..230 kHz>`, `DX SIDE
OFF|AUTO|LOW|HIGH`, `DX CANCELLER ON|OFF`, `STATUS`, `HELP`.

Channel filter: complex FIR ahead of the discriminator, 56-230 kHz wide
(13 steps; `normal`/`narrow`/`dx` accepted as 230/180/130); with a protected side the passband edge on that
side is pulled in to 60 kHz (asymmetric). `AUTO` picks the side from the RF
level ±200 kHz (6 dB threshold, 3 dB hysteresis). The optional adjacent
canceller isolates the protected neighbour with a second FIR and subtracts
a least-squares fit of it from the channel. Telemetry carries `wide_dbfs`,
`overload` (`ok`/`warn`/`overload` from clip rate and wideband level) and
a `dx` block with passband, active side, neighbour levels and reduction.
This is adjacent-channel interference reduction, not co-channel separation.

Telemetry fields include `frequency_mhz`, `state`, `error`, `device`,
`tuner_type`, `gain_mode`, `gain_db`, `supported_gains_db`, `bias_tee`,
`bias_tee_supported`, `signal_dbfs`,
`signal_rms`, `clipping`, `stereo`, `stereo_mode`, `pilot_ratio`,
`mpx_deviation`, `mpx_pilot_db`, `mpx_stereo_db`, `mpx_rds_db`,
`mpx_sca_db`, `audio_enabled`, `audio_ready`, `audio_device`, `audio_error`,
`audio_underruns`, `volume`, `rds_ready`, `rds_level`, `hd` (decoder state),
`hd_detected`, `seeking`, `seek_result`, `reads`, the `band` limits and
`window` (the tuner window's outer rectangle `x`, `y`, `width`, `height`
plus `screen_width`/`screen_height`, or `null` when minimised/headless —
used by the RDS Monitor's Dock-to-tuner feature).
`snr_db` and `rssi_dbm` are reported as `null` — the RTL-SDR gives no
calibrated value for them, and nothing is invented.

### Command line

```
python FM_Tuner.py [-f 101.1] [-d 0] [--ppm 0]
                   [--rds-port 7376] [--legacy-rds-port 23]
                   [--telemetry-port 7373] [--command-port 7374]
                   [--no-autostart] [--bias-t] [--no-audio] [--list-devices]
```

`--legacy-rds-port 0` disables the port 23 attempt entirely.

---

## 2. Installation notes

Full step-by-step text is in `INSTALL.txt`; this is the condensed version.

1. **Python** — 3.10+ (64-bit) from <https://www.python.org/downloads/windows/>.
   Tick *Add python.exe to PATH* and keep *tcl/tk and IDLE*. If `py` is not
   recognised afterwards, use `python` in place of `py -3`.
   Python 3.14 is very new; if `numpy`/`sounddevice` have no wheel for it,
   install 3.12 (64-bit) and use that.

2. **Packages** — double-click `install.bat`, or:

   ```bat
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

   `numpy` and `sounddevice` are required; `requests` and `pillow` are only
   used for monitor station logos.

3. **USB driver** — plug the dongle in, run Zadig (<https://zadig.akeo.ie/>),
   *Options → List All Devices*, select the dongle ("Bulk-In, Interface (Interface 0)"
   or "RTL2838UHIDIR"), choose **WinUSB**, install. Repeat once per USB port.
   This replaces the DVB-T TV driver, which is expected.

4. **rtlsdr.dll** — download
   <https://github.com/rtlsdrblog/rtl-sdr-blog/releases/latest/download/Release.zip>
   and copy `rtlsdr.dll` and `libusb-1.0.dll` from its **x64** folder next to
   `FM_Tuner.py`, or point the `RTLSDR_DLL` environment variable at the DLL.
   A 32-bit DLL under 64-bit Python fails to load.

5. **Run** — the tuner is standalone; any client (the separate RDS Monitor
   program, RDS Spy, your own script) is started afterwards:

   ```bat
   python FM_Tuner.py
   ```

   Allow Python through Windows Firewall for localhost if prompted. Port 23
   needs the telnet port free, or "Run as administrator"; without it the
   tuner simply serves 7376.

Common failures and fixes are in `INSTALL.txt` and the README troubleshooting
table; the hardware acceptance checklist is in `docs/WINDOWS_TESTING.md`.
