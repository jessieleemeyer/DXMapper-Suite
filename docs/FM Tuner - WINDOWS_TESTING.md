# Windows / RTL-SDR test checklist

Everything here needs real hardware: Windows, an RTL-SDR dongle on WinUSB,
`rtlsdr.dll` (x64) in the project folder, an antenna, and an audio output
device. None of it can be verified on the build machine.

## Setup

- [ ] `py -3 --version` reports 3.10 or newer (64-bit)
- [ ] `py -3 -m pip install -r requirements.txt` completes
- [ ] Zadig shows **WinUSB** installed for the dongle
- [ ] `rtlsdr.dll` (and `libusb-1.0.dll`) sit next to `FM_Tuner.py`, or
      `RTLSDR_DLL` points at the DLL
- [ ] `py -3 FM_Tuner.py --list-devices` lists the dongle and audio devices

## Tuner (brief acceptance)

- [ ] The tuner launches on Windows
- [ ] RTL-SDR is detected and tunes across 87.7–108.1 MHz
- [ ] Demodulated audio plays through a selected Windows output device
- [ ] Direct tuning, wrapped 0.2 MHz stepping, seek and Stop work
- [ ] Manual gain starts at 0.0 dB and adjusts
- [ ] Live RDS decodes from received stations
- [ ] ASCII G RDS is available on both ports 7376 and 23

## Tuner (detail)

- [ ] Typing `101.2` (off grid) or `108.3` (out of band) is rejected with a
      clear message and no retune
- [ ] `+0.2` at 108.1 wraps to 87.7; `-0.2` at 87.7 wraps to 108.1
- [ ] Seek Up stops on a real station; Seek Down likewise; Stop halts a seek
- [ ] Switching output device mid-playback keeps audio running
- [ ] Stereo indicator lights on a stereo station and drops on a mono/weak one
- [ ] Gain slider changes audible noise; Automatic then Manual returns to a
      real hardware gain value
- [ ] Signal dBFS, pilot ratio and MPX deviation move with signal strength
- [ ] `snr_db` and `rssi_dbm` are reported as unavailable (not faked)
- [ ] Without the HD add-on the Audio panel says "HD decoder not installed"
      and analog plays normally
- [ ] With the add-on, a known HD station reads "HD locked" (call, MER) and
      the Channel list fills with only the HD channels found; an analog
      station next to a strong HD local reads "No HD lock"
- [ ] Auto fades to analog when HD drops (no gap/repeat); HD only goes
      silent; Analog only never plays HD; Manual stays on the chosen channel
- [ ] MPX baseband display shows programme audio below 15 kHz, a 19 kHz pilot
      on a stereo station (absent on a mono one), the 38 kHz L-R subcarrier,
      and a 57 kHz bump on an RDS station; header carrier levels track it
- [ ] Tuning to a blank channel collapses the MPX display to the noise floor
- [ ] Unplugging the dongle while running produces a clear error, not a crash

## Ports / errors

- [ ] Two clients (e.g. RDS Spy on 7376 and the monitor) both receive groups
- [ ] Port 23 in use (Telnet server) → warning only, tuner keeps working on 7376
- [ ] Running a second tuner instance → clear "port in use" error
- [ ] Removing `rtlsdr.dll` → download instructions shown, no crash

## RDS Monitor

- [ ] Monitor started before the tuner shows a waiting state and connects by
      itself once the tuner is up
- [ ] RDS arrives over TCP with no SDR, MPX or VAC input configured anywhere
- [ ] Tune / ±0.2 / Seek / Stop / gain / gain mode from the monitor move the
      tuner, and the tuner window reflects the change
- [ ] Callsign, PI, PS, RT, PTY, TP/TA and logos populate as before
- [ ] Station list accumulates stations across retunes
- [ ] Session peak signal only ever rises, and its context (frequency, station,
      time, gain) matches when the peak occurred
- [ ] Report export includes the tuner reception section and the session peak
- [ ] PI editor: search, add, edit, delete (with confirmation), save; reopening
      shows the saved mapping and a timestamped `.bak` exists
- [ ] A saved override takes effect on the next station showing that PI
- [ ] Closing the tuner leaves the monitor alive and reconnecting

## Long run

- [ ] One hour on a strong station: audio underruns stay at 0, block error rate
      stays low, memory is stable, no reconnect storms in either window
