# RDS Monitor

Real-time RDS display: station logos, callsign from PI, PS, Long PS (15A,
shown under PS only when decoded), PTY, PTYN,
RadioText and RT+, TP/TA/music-speech flags, clock time, signal quality and
confidence, session and lifetime station counters, and report export.

RT+ appears as a panel of labelled rows (Title, Artist, Album, Now, Next,
Host, Station, Website, phone/SMS/email...) showing only the fields the
station actually tags text for; View > Show RT+ Items hides it. Reports
list the same fields plus the raw tag data.

This is a **standalone program** — one file, `RDS Monitor.py`. It never opens
radio hardware: no RTL-SDR, no MPX/virtual-audio-cable input, no sound card.
RDS comes either from a `.spy` file or over localhost TCP.

```
FM Tuner (separate program) ──┬── 127.0.0.1:7376  RDS groups (ASCII G)
                              ├── 127.0.0.1:7373  telemetry
                              └── 127.0.0.1:7374  commands
                                       ▲
                                       └── RDS Monitor (TCP client)
```

---

## 1. Install

See **INSTALL.txt**. Short version: install Python 3.10+ (64-bit) with
"Add python.exe to PATH" and tcl/tk, then double-click `install.bat` for the
two optional logo packages (`requests`, `pillow`). Nothing else is required.

## 2. Run

**Every day:** double-click **Start Radio.pyw** (in the Radio ZIP) and it opens
all three programs with no console windows. The full illustrated guide to
every screen and button is **OWNERS_MANUAL.pdf**; the short one is
**QUICK_START.pdf**.

To start the monitor on its own, or with options:

```bat
cd path\to\RDS-Monitor
python "RDS Monitor.py"
```

The quotes matter — the file name contains a space.

```
  --spy-file   path to the .spy file
  --watch      folder watched for the .spy file
  --logo-dir   station logo folder
  --pi-file    PI code override file
  --input      tuner (default: connect to the FM Tuner at launch) or spy
```

## 3. Input modes (Input menu)

| Mode | Source |
|---|---|
| **Spy File (RDS Spy)** | a `.spy` file, polled as it grows (`--input spy`) |
| **FM Tuner (TCP)** | the FM Tuner program: groups from 7376, telemetry from 7373, commands to 7374 (default at launch; retries until the tuner is running) |
| **RDS Input (other TCP/UDP source)** | any other ASCII G source, e.g. SDR# RDSOutput on port 23 |

In **FM Tuner** mode a bar appears under the header with the tuned frequency
in large text (e.g. `Tuned 101.1 MHz`), the tuner state (signal in the tuner's units - S-units or dBFS,
stereo/mono, HD, gain, Bias-T) and buttons for Tune, −0.2, +0.2, Seek Down,
Seek Up, Stop and Start, plus `Audio` and `Bias-T` tick boxes. Rolling the
mouse wheel over the Tuned frequency tunes one channel (the tuner's region step) per notch. `Bias-T`
switches the tuner's 4.5 V antenna-port supply for a powered LNA or active
antenna — it asks first, and only works when the tuner's `rtlsdr.dll`
supports it. The frequency follows the tuner, whether it was changed here
or in the tuner's own window. If the tuner is not running the readout shows
`--.- MHz` and the link keeps retrying, so the tuner can be started or
restarted at any time.

**Station counters:** `Lifetime` is the number of distinct PI codes in DX
Mapper's current log (the first one ticked in Logs…), so it follows the log
and HD2-HD8 don't count extra. Without a DX Mapper log it falls back to every
distinct PI ever heard (the station-count file); only then does Tools > Reset
Lifetime Station Counter zero it. `Session` counts since launch.
Tools > Clear Spy File Data also zeroes both counters.

**Dock to FM Tuner** (the `Dock` button in the tuner bar, or View > Dock to
FM Tuner) snaps this window to the right of, left of, or below the tuner
window and follows it when the tuner is moved or resized (height is matched
when side-by-side, width when below; it shrinks to fit the screen if there
is no room). The two stay separate programs — the tuner just publishes its
window position in telemetry. `Undocked` restores the previous size.

The main display is a wide 16:9 layout: logo / callsign / city / station
clock on the left, PI / PTY / PS, now-playing (RT+ or parsed from RadioText),
temporary cover art and RadioText in the middle, RT+ fields on the right,
the signal-quality panel across the bottom, and TMC traffic messages in a
scrolling ticker along the very bottom (point at it to pause it).
Cover art is fetched from the iTunes Search API only when the track changes
and is kept in memory only — nothing is written to disk. While HD is
locked, the box shows the album art the station itself sends over HD for
the channel being played instead (falls back to online art without it). TMC shows the
event phrase, raw location code (no US location table is available),
extent, direction and duration.

Under the title/artist, **YTM / SPOT / AMZ / APL** search the song on
YouTube Music, Spotify, Amazon Music or Apple Music in your browser; they
light up in each service's colour only while a song is showing (RT+ or
RadioText), grey for promos.
The green **SEQ>** line under the decode badges is the last 24 RDS group
types in arrival order (a transmitter fingerprint); it is sent to DX
Mapper with each log (Log Book column Group Sequence).

**Range, distance and compass bearing** to the station's transmitter appear under the city/state line, e.g.
`Local · 8.3 mi  WNW (298°)`. The range uses DX Mapper's tiers: Local 0–40 mi,
Extended local 41–80 mi, DX 81+ mi (or the distances set in DX Mapper's Home…
when it sits next to this folder), and in reports. Coordinates come from the station's
Wikipedia page, or from the FCC FM Query database when Wikipedia has no page
or no coordinates (it covers every licensed US station and translator, e.g.
WXYZ → Springfield, IL, 26.5 mi NE). You have two receive locations under **Tools**:
**GPS Location (USB GPS dongle)...** (optional) reads NMEA from a USB GPS
dongle's COM port (Auto tries each port at 9600/4800/38400/115200), shows the
fix and offers Set as Home / Use as Away location. "Keep the Away location
updated from GPS while traveling" saves Away as "GPS" at most once a minute
after moving 0.5 mi (settings `gps_port`, `gps_follow_away`). Needs pyserial.

**Set Home Location...** (asked once on first start) and **Set Away Location
(DX on the go)...** with a name such as a trip or hilltop. **Use Home location**
/ **Use Away location** picks which one distances are measured from; it is
saved to `settings\monitor_settings.json`. Each **Log to Map** sends the active
location with the station, and DX Mapper follows it.

**Station logos** are looked for in the logo folder first, then downloaded
from Wikipedia (page image, then infobox), then from the station's Facebook
page picture — the Facebook page is found from the station website linked
on Wikipedia. Manual Logo Lookup also lets you paste a Facebook page URL
directly when nothing is found automatically.

**Group Analyzer** (View > Open Group Analyzer Window) has tabs for Group Statistics (0A–15B counts,
proportion and rate), **ODA AID** (applications announced in 3A groups: AID,
name such as RT+ / TMC / eRT, carrier group, proportion, rate; every group
is decoded even without a 3A so weak stations give all they can, and only
standard ODAs are decoded; unregistered AIDs from damaged groups show here
but DX Mapper doesn't list them), Graph,
Group Content HEX and **RDS2 Stream 1–3** (each with its own statistics,
group stream, detector grid and HEX view, fed from the tuner's `G1:`–`G3:`
records — synthetic verification only). The group stream keeps scrolling
indefinitely, trimming old rows at the top; it follows the newest row while
you are at the bottom and stays put while you scroll up to inspect. The
**DI (0A/0B)** panel shows the last received DI segment address and bit,
its interpretation and whether the station's 4-segment DI cycle is
complete.

**Stereo Status** in reports keeps three different things apart: *Tuner
audio mode* (what the tuner is outputting), *19 kHz pilot* (whether the
stereo pilot is physically detected) and *RDS DI station flag* (what the
broadcaster declares in group 0A). They can legitimately disagree — a
station may declare mono in DI while transmitting a pilot — and the report
says so when they do.

The previous native RTL-SDR input and MPX/VAC input have been removed — that
work is done by the tuner program, which is what stops the audio and RDS from
skipping while the monitor redraws.

### HD Radio (from the FM Tuner's optional decoder)

When the FM Tuner has its optional nrsc5 HD add-on, its telemetry carries an
`hd` object and the monitor shows an **HD Radio** section: lock / no lock /
lost lock, MER (colour-coded: green 10 dB+ good, yellow 6-10 dB fair, red
under 6 dB weak) and BER, station name, slogan, message, facility ID and
location, HD1–HD8 names and types, now playing (title/artist/album/genre),
emergency alerts, and HD data counts. The Group Analyzer window has an
**HD Data** tab, HD's version of Group Statistics: counts and proportion per
kind of HD data (sync, SIS, PSD per HD channel, audio, LOT, alerts,
traffic/weather images) plus a log of recent HD data. The RDS tabs stay
RDS-only. Without the add-on the line reads
`decoder not installed (optional add-on)`.

**HD logos:** a station logo sent over HD replaces the logo in the logo
folder. HD1 shares the station's regular `CALL.png`; HD2 and up are saved as
`CALL-HD2.png`, `CALL-HD3.png`, … The logo it replaced is kept as
`CALL.before-hd.png`. Album/artist art and traffic/weather maps are shown
only while current and never saved. HD program info goes on to the DX
Mapper with Log to Map, which logs HD2–HD8 as their own stations.

## 3b. Log to Map (DX Mapper)

The **Log to Map** button under the call sign (or **Ctrl+L**, or
*Tools > Log Current Station to DX Mapper*) sends the station on screen
to the separate **DX Mapper** program on `127.0.0.1:7375`: PI, call, MHz, PS,
RT, PTY, signal, RDS level, usable info %, block %, AF, location.
The button shows *Logged* or *Map not running*.

The monitor also tells DX Mapper the Tuner's frequency every time you
retune (and every 20 s), RDS or not. DX Mapper's **Now hearing** switches
straight away, and a station with no RDS shows as the one you logged on
that frequency (or asks which one, if there are several). Nothing is
logged by this.

*Tools > Auto-log to DX Mapper* is **off by default**. When
on, it logs each station once its usable info reaches 60% (saved as
`auto_map_log` in `settings/monitor_settings.json`). While the station keeps
decoding it sends an update every 5 minutes, so the log entry picks up what
decodes over time instead of keeping only the first decode. It also updates
sooner (at most every 30 s) whenever something new decodes: a new group type
or ODA even without a 3A, a late 3A, more AFs or EON networks, RT+, TMC,
Long PS, CT, PTYN. Song changes alone don't trigger an update.

*Tools > └ Auto-log HD catches too* is a sub-option (off by default, only
available while Auto-log is on; saved as `auto_hd_log`). When the FM Tuner's
HD decoder holds lock on a station for 5 seconds, the station is logged
right away with its HD info (call, slogan, HD1-HD8 channels, MER), even if
the RDS isn't at 60% yet. While HD stays locked it sends an update every
5 minutes too. A lost lock starts the 5-second wait over.

## 4. Files

```
RDS Monitor.py               the program
INSTALL.txt                  plain-text setup steps
install.bat                  installs the optional logo packages
requirements.txt             requests, pillow (both optional)
data/pi_codes.txt            seed PI -> callsign list (first run only)
data/recording.spy           a sample capture for Spy File mode
settings\                    YOUR files (not in the ZIP - see below)
rds_tcp23_to_spy_bridge.py   helper: writes an ASCII G TCP stream to a .spy
```

Everything you edit or the program saves for you lives in the `settings`
folder next to the program: `stations.txt`, `station_count.txt`,
`monitor_settings.json` (Home/Away locations) and `logos\` (logo cache).

**Station names come from DX Mapper's log first.** The monitor reads every
DX Mapper log (`settings\dx_log.db` and `settings\Logs\*.db`, read only) for
PI -> call letters, city, coordinates and slogan, and notices an edit there
within a second. Make fixes in DX Mapper (Edit info or the Log Book). The
old Set Callsign / Reload PI Overrides / Open PI Codes File menu items are
gone. `stations.txt` is now only a fallback for PIs the log doesn't have.

`stations.txt` is the fallback station list, one line per station:
`PI CALLSIGN | City, ST | lat lon | Slogan` (the slogan part is optional,
e.g. `1234 WXYZ | Springfield, IL | 39.80 -89.64 | The Rock`; it shows in
italics under the call letters). DX Mapper shares this file: slogans and
call fixes saved there land on the PI's line, and its Update all
button fills the file from your whole log. It gives the PI -> callsign override
and the city/distance shown on screen and in reports, so a station in the
file is never looked up on the web. Stations the program looks up itself
are appended; existing lines are never rewritten, so edit it freely.
Only DX Mapper (when you change a station's call letters there) replaces
the callsign on that PI's line. The monitor notices any change
to the file within a second and relabels the station on screen, reloading
its logo and city, with no restart. An old
`pi_codes.txt` (just `PI CALLSIGN`) is adopted as-is on first run. The folder is never in the ZIP,
so to upgrade just unzip the new build over the old folder - `settings` is
left untouched. `--pi-file`, `--logo-dir`, `--spy-file` still override them.

Reports are saved to the `reports` folder next to the program by default (created on first save; the dialog lets you pick elsewhere).
