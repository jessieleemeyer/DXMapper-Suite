# DX Mapper

Interactive FM DX log map. A standalone third program alongside the
**FM Tuner** and the **RDS Monitor**. It never opens radio hardware.

```
FM Tuner ──(7376/7373/7374)──> RDS Monitor ──[Log button]──(7375)──> DX Mapper ──> map window (7380)
```

## Start

**Every day:** double-click **Start Radio.pyw** (in the Radio ZIP) and it opens
all three programs with no console windows. The full illustrated guide to
every screen and button is **OWNERS_MANUAL.pdf**; the short one is
**QUICK_START.pdf**.

Double-click **DX Mapper.pyw** (or **Start DX Mapper.bat**). No console or
command prompt: DX Mapper opens in its own program window. On first start it
sets up the window component (pywebview) by itself, which needs the internet
once; if that is not possible the map opens in your web browser instead, with
a small DX Mapper window to keep open (Quit there to close DX Mapper).
Problems are shown in message boxes and written to settings/dx_mapper.log.

## Logging stations

* **RDS Monitor > Log to Map** button (under the call sign), **Ctrl+L**, or
  *Tools > Log Current Station to DX Mapper*. It sends the station on screen:
  PI, call, MHz, PS, RT, PTY, signal dBFS, RDS level, usable info %, block %,
  AF list, location/coordinates.
  It also sends the FM Tuner's *View > Signal units* choice: DX Mapper shows
  signals (Station Details, log lists, Excel copy) as S-units (the default)
  or dBFS to match; S-unit text typed or imported is read back as dBFS.
* **Auto-logging is OFF by default.** *Tools > Auto-log to DX Mapper
  (experimental)* turns it on. It then logs a station once its usable info
  reaches 60%, then sends an update every 5 minutes while it keeps decoding
  (sooner when something new decodes). Its sub-option *Auto-log HD catches
  too* also logs a station once HD holds lock for 5 seconds, with updates
  every 5 minutes while HD stays locked. Leave it off until manual logging
  checks out. Each update refreshes the same log entry with the newer report
  instead of adding a new one, so the entry fills in over time, and doesn't
  jump the map.
* The status bar in the RDS Monitor says *logged*, *logged again* or
  *not running*.

Station coordinates come from the record, the RDS Monitor's
`settings/stations.txt`, the FCC FM Query (transmitter site, from the call
letters), or else the station's City + State (OpenStreetMap). Lookups run
automatically in the background and are retried on the next start if the PC
was offline. You can also type coordinates into the station's Edit box.

If your log has a distance column (e.g. *Calibrated Distance*), the details
window compares it with the map's distance and shows **⚠** when they differ by
more than 30 mi / 15 %; the sidebar counts those as "to check".

## Map

* **Normal** (OpenStreetMap, default), **Terrain** (Esri World Topo with
  Esri hillshade shading on top, so hills look raised) and
  **Satellite** (Esri World Imagery). All are free with no account; the last
  one you picked is remembered.
* Marker colour = distance band; two ring checkboxes, each remembered: **Local / Ext rings** at your Local and Extended local
  distances (solid blue, from Home…) and **100–400 mi rings**: solid at 100/200/300/400, dashed at 150/250/350;
  **Coverage when tuned** draws the FCC service contour (60 dBu; 54 class B,
  57 class B1) of the station you're tuned to, saved in settings\Coverage
  for 30 days; **Auto-center map** (on by default) does the auto-fit below,
  untick it to keep the map where you put it;
  **County / state lines** (on by default) draws US county and state
  borders over all three maps (Census shapes shipped with DX Mapper, works
  offline); **Weather radar** (off by default) lays live NWS NEXRAD radar
  from the Iowa Environmental Mesonet over the map, refreshed every 5
  minutes, to spot storms and fronts; every checkbox is remembered;
  **Conditions…** opens free skip-condition sites in your browser: the
  Hepburn tropo forecast, DXMaps live E-skip and PropQuest E-skip MUF;
  **Hide my location** (remembered) is for sharing screenshots: it hides
  the Home/Away marker, both sets of rings, the lines from home and every
  distance/direction on screen, and shows one Local-size circle shifted
  10–20 mi off-center (fixed random direction) so it gives your general
  area without its middle being your house; untick it and everything comes
  back (the log keeps all distances); **Screenshot** turns Hide my
  location on, saves a picture of the whole window in settings\Screenshots
  (the folder opens), then puts the box back the way it was;
  picking a station keeps your tuner location centred, draws a line out to
  the transmitter and zooms so both fit.
* **Click any marker** (or list/table row) for the station details (a scrolling now-playing bar shows while it's the station on the air): PI, call,
  PS, PTY, location, distance/bearing, first/last logged, times logged, best
  signal/usable info/blocks, FCC licensee/ERP/HAAT/class, edits,
  and every log entry.
* **Station logo** at the very top of the details, above the call letters,
  taken from the RDS Monitor's settings\logos folder. If the monitor has no
  logo for that call, a **Download logo** button looks it up online.
  **Change logo** shows every logo found online (Wikipedia, Facebook, the
  network's page, the station's website) to pick from, or upload your own;
  your pick replaces the old logo and nothing automatic changes it back.
  A station with no logo of its own gets its network's (e.g. King of Kings
  Radio). Facebook's grey "no photo" person and Wikipedia studio/tower
  photos are skipped. Download logo searches
  (Wikipedia, then the station's Facebook page, like the monitor does) and
  saves it into that folder, so the RDS Monitor shows it too. An HD2-HD8
  entry tries its own logo from the channel's website listed on Wikipedia
  (e.g. `WBUL-HD2.png`) and otherwise shows the main station's. A logo
  decoded off the HD signal replaces the online one; online lookups never
  replace a logo that's already there.
  A translator (e.g. W293BK relaying WTCO) shows its own call letters
  but uses the logo of the station it relays (WTCO.png).
  The parent can be an AM station (WTCO 1450 AM -> FM translator W294CO),
  found from the FCC record, WTFDA's "Relay of", Wikipedia or Fed by.
  An HD2-HD8 channel that simulcasts an AM station (Wikipedia: "HD2:
  Simulcast of WTCO 1450 AM") keeps its own call but uses the AM's logo;
  otherwise type the AM's call in that HD channel's Fed by.
* **HD Channels**: HD1 is the analog station's own entry; HD2-HD8 get their
  own (WXYZ-HD2). A station with HD gets 1 in the Log Book HD column and an
  HD Channels line (e.g. `HD1, HD2, HD3`); the details link each HD entry
  both ways. Channels from the station's Wikipedia infobox that you haven't
  logged show without a link (the page is rechecked monthly).
* **Translators**: a translator's details show **Fed by** (the station or HD
  channel it relays, e.g. WBUL-HD2 for W253BK 98.5) and the parent lists its
  translators, all clickable. It comes from the FCC record and the
  Wikipedia translator list; type it in Edit > Fed by when it's missing.
* **Slogan / branding** (e.g. "Hot Country 93.5") under the call letters,
  above the Decode badges. It comes from your log's Slogan/Branding column.
  A station without one gets the "Branding" line from its Wikipedia page,
  marked "from Wikipedia" because Wikipedia isn't always current. Type your
  own in Edit > Slogan to replace it (clear the box to remove it). A slogan
  from your log or typed in is never replaced by Wikipedia.
  Every station is also looked up once on the WTFDA FM database
  (db.wtfda.org), in the background and 30 s apart as the site asks; a
  catch with only a PI is looked up by PI. WTFDA fills blanks only (slogan,
  PI, PS, RadioText, city, transmitter site) and ranks above Wikipedia but
  below your decodes, your edits and the FCC. Its RadioText is replaced as
  soon as the station's own decodes. Station Details shows what WTFDA lists.
  Slogans are shared with the RDS Monitor through its settings\stations.txt
  (`PI CALL | City, ST | lat lon | Slogan`), which shows them under the call
  letters too. **Update all** (sidebar) goes through your whole log
  in the background. First it fills stations.txt for every station with a PI:
  call, city, coordinates and slogan (looking up missing slogans on
  Wikipedia, paced). Lines you edited by hand are kept: it only fills empty
  parts, and a PI whose call you set differently in the monitor is left
  alone. Then it looks up a logo for every station that has none yet,
  saving it in the RDS Monitor's settings\logos folder (HD2-HD8 use the
  analog station's logo; logos you already have are never replaced). Last
  it looks up a live stream link and website for every station missing one
  (streams run alongside the other steps). A box under the buttons shows a
  progress bar for Details, Logos and Streams, then a summary, with any
  errors in red (a lookup that couldn't reach the internet isn't saved and
  is tried again next time).
* **Stream** (station list, details, Log Book) opens the station's live
  internet stream in your web browser; **Tune** tunes the FM Tuner off the
  air. **Website** (details, Log Book) opens the station's own site.
  Stream links come from Radio Browser, then iHeartRadio, then TuneIn, then
  the station's Wikipedia infobox (Webcast / Website lines), and last the
  Listen Live link on the station's website,
  matched by call letters (and frequency/HD channel when the listing gives
  one). A station is looked up once when it is first logged; Update all
  retries the ones not found. A Live Stream column in an imported sheet is
  kept.
* **Analog / HD / Location sections**: the details split into an Analog
  section titled with the call letters (WXYZ), an HD section titled with -HD
  added (WXYZ-HD; an HD2 entry shows WXYZ-HD2) and a Location section. The
  Analog section's **Decoded RDS** list and the HD section's **Decoded HD**
  list show every item that can be decoded (PS, RT, RT+, PTYN, AF, EON, TMC,
  CT, TP/TA, DI...; HD audio, channel name, program type, song/artist,
  album, album art, station logo, station name, slogan, message, facility
  ID, location, alerts, traffic and weather maps) with a green check once
  any log observed it and a grey dash until then. Only the check shows; the
  actual decodes are in **Full report**, the latest complete RDS Monitor
  report as plain text. The HD section also shows the best MER (green /
  yellow / red) and last HD lock. HD1 stays in the analog station's entry.
  Click a section heading to fold it; it stays folded.
* **Decode badges** at the top of the details, like the RDS Monitor's:
  PS, RT, PTY, PTYN, RT+, TMC(n), AF(n), EON(n) and any other decoded ODA
  (only defined ODAs are listed; unregistered AIDs, usually a damaged group
  on a weak signal, are hidden; an announced known app such as iTunes
  Tagging gets its own ✓ line in Decoded RDS),
  taken from the logs' RDS reports. Only what was actually decoded gets a
  badge: green = complete, yellow = partly received. The full report is
  under each log's Report button.
* **Tune** (next to the frequency in the details) asks the FM Tuner to tune
  to that station over its command port 7374 (`TUNE 104.10`). The FM Tuner
  still owns the radio; the RDS Monitor follows the new frequency from the
  tuner's telemetry as usual. If the FM Tuner isn't running you get a message
  instead.
* **Editing the call letters** of a station (Edit, then Save) also writes
  `PI CALL` into the RDS Monitor's `settings/stations.txt`, for that PI only.
  The rest of that line (city, coordinates) and every other station stay as
  they are. The running RDS Monitor picks it up within a second or two and
  relabels the station, with the logo and city for the new call. Handy for
  stations with an unmapped or bogus PI.
* The window title shows how many unique stations you've logged, e.g.
  "DX Mapper - 152 stations" (each station counts once).

## Views

| View | Shows |
|------|-------|
| **Current** | Stations logged from the RDS Monitor in the last 1/3/6/12/24 h or since DX Mapper opened. Updates live. |
| **History** | All time, today, yesterday, last 7/30 days, this year, or picked dates, plus a list of the days you logged (click a day to replay it). |
| **All Log** | Table under the map with every log entry, sortable and searchable. Click a row to show it on the map. |
| **Log Book** | Button in the sidebar. Opens your log book in its own window: one row per station with your sheet's columns (Frequency, Call Letters, HD, Slogan / Format, PI Code, City, State, Calibrated Distance, Range Type, Owner / Licensee, Time Decoding, RT+ Capabilities, RadioText, Live Stream with a Stream button, Website with a Website button, Source = the dongle(s) that made the catches, newest first, sent by the tuner with each log, and Notes = your own reception notes like "crystal clear", which import never fills and Clean up never deletes; last comes Group Sequence = the RDS group order fingerprint the RDS Monitor sends with each log). After RadioText come the **Decoded RDS** check columns, the same checklist as Station Details: PI, PS, PTY, RT, RT+, PTYN, Long PS, AF, EON, TMC, CT, TP, TA, DI, ticked ✓ once any of your RDS reports decoded it (they can't be typed in). Click one's heading to bring the ticked stations to the top, or type its name (e.g. "TMC") in the search box to show only those stations. Search (any column; "ky" finds Kentucky and "kentucky" finds KY), sort by any heading, filter Locals / Extended locals / DX / HD subchannels, Print. **Columns…** picks which columns show and print, saved in settings (Excel export keeps every column). Click a station to show it on the map. Cells edit like Excel: pick one or several (drag / Shift / Ctrl), type and press Enter to put the value in every picked cell, Delete clears them, **Undo my edits** restores what DX Mapper fills in. **Clean up** (with a progress bar) tidies RadioText and **permanently deletes** the old imported notes and any imported columns that aren't Log Book columns. It asks first and saves a copy as `settings\Backups\Before Clean up <date>.db`; to undo, open **Backup…** and restore that file. RadioText holds the station's pattern ("Song by Artist on The Wave"); promos, sports and church lines are dropped and jumbled or real-song RadioText is cleaned. The **Classic \| Modern** switch (remembered) flips to a card view: a forgiving search (frequency, call, city, PI; "wxz" finds WXYZ), a now-playing bar, Local / Extended Local / DX Catches tabs with counts plus **Show all** (every station as cards, borders keep their distance colour), logo cards with PI / PTY / RT / RT+ / iTunes / TMC lights (iTunes = the station announces iTunes Tagging, ODA C3B0), and a station panel with logo, live song, Tune, Show on map, Stream / Website, the same sections as Station Details (Decode badges, Log book, Full report, Analog / HD checklists, Location, Reception, FCC, WTFDA, Translators, Log history), the Wikipedia summary (cached in `settings\wiki_summaries.json`), Sister stations & same market (also in the map's Station Details, just above Edit: Wikipedia's sister stations plus logged stations with the same owner, and the FCC's FM stations for the city plus logged ones from the same city/state; logged ones are links, cached a month in `settings\neighbors.json`) and an Edit info box with every typed Log Book column that saves like the table. **Delete station** (under Edit info) and the × on each Log history entry ask in a confirmation popup first. One entry per station: when a station is moved onto another entry's call letters and frequency (an FCC fix, a catch or an edit), the two are merged at once. **Duplicates…** lists stations listed more than once: the same call letters or PI twice on one frequency (as the Log Book shows them, typed-in cells included) or again on another frequency by splatter (translators the FCC knows are left alone) and merges the ticked ones, saving `settings\Backups\Before Duplicates <date>.db` first. Auto logs of a PI already logged elsewhere join that station unless the FCC lists a translator there; pressing Log in the RDS Monitor always logs it where you're tuned. It follows the map: any station the map opens (a click, Jump to new logs, Follow tuner) opens in the panel. **Open station card** in Station Details opens that station's card in the Modern view (opening the Log Book in your browser if needed); **Show on map** in the panel goes the other way. |

With **All** selected, the station list is split into **Locals** and
**Extended local** (by frequency, like a preset list), **DX** (in your Sort
order) and **No location yet**. Click a heading to fold it away. Every row has its own **Tune** button
that tunes the FM Tuner in one click, without opening the details.

Under the view tabs, **All / Local / Ext local / DX** narrows any view
(markers, list and All Log table). **Local** = 0–40 miles from home (in
regularly), **Extended local** = 41–80 miles (in sometimes), **DX** = 81+
miles. The Log Book's Range Type column uses the same tiers unless your
imported sheet already says otherwise. Change the miles under **Home…**.

**Home follows the RDS Monitor's active location** – its **Home** or, when you
switch the monitor to **Away** for DXing on the go, its Away location (read
from its `settings/monitor_settings.json`). Change or switch it in the RDS
Monitor's *Tools* menu and the map, distances and Local/DX follow within a few
seconds. Only if no RDS Monitor folder is found next to DX Mapper can you type
a home in **Home…**.

**Every log remembers where you were** (Home or Away). The All Log table, the
station's history and the Excel copy show *Logged from* with the distance and
direction from that spot, so a catch made away keeps its real distance. When
importing an old log, choose **Logged from** (Home / Away / not recorded).

## Add station (no RDS)

**Add station…** in the sidebar logs a station that sends no RDS. It reads
the FM Tuner's frequency (or type one) and lists every FCC station on it,
closest first, with city, type (FM / LPFM / translator), ERP and distance,
so locals are at the top and DX further down. Two stations can share a
frequency near the edge of coverage, so you click **Add** on the one you
hear. Typing narrows the list: call letters, a slogan, "City, ST", a state
("ky" or "Kentucky") or a region ("central ky"). The search uses the FCC
FM Query plus your log's and Wikipedia's slogans. A station added by hand
stays as entered: RDS of a different station on that frequency becomes its
own entry, and RDS of the same station only fills blanks such as the PI.

With no RDS, **Now hearing** follows the Tuner's frequency: one logged
station there is shown as "(no RDS)" with the map pulse, coverage and
Follow tuner; several (fringe area) get buttons so you pick which one you
hear (never guessed); none shows an **Add station…** button. RDS always
takes over when it decodes, and nothing is logged by itself.

## Import

Imports run in the background: once the file's headers are recognised the
Import window closes and the sidebar shows "Importing … n of N" and then
"Looking up locations: n left" while you keep using the map. The column
window only appears when no Call/PI column is recognised. Live logging takes
priority: the import works in small steps and slows down while you're
logging, live logs are saved and mapped at once (only they make the map jump),
and their location lookups go ahead of imported stations.

Location lookups check 4 stations with the FCC at once; a station whose city
was looked up before goes on the map straight away (the FCC transmitter site
replaces it when found).

**Import…** accepts:

* Google Sheets: paste the link (sheet shared *Anyone with the link: Viewer*)
  or use *File > Download > Microsoft Excel (.xlsx)* (or CSV) and pick the
  file. Every tab with log headers is read; a *Sheet* column records the tab.
* Excel `.xlsx` workbooks.
* Any CSV/TSV/semicolon file. Columns are detected (Freq, Call, PI, PS,
  City, State, Country, Lat/Lon, Date, Time, Mode, PTY, Signal) and
  you can change each one before importing. Your sheet's log book columns
  (HD, Slogan / Format, Calibrated Distance, Range Type, Owner / Licensee,
  Time Decoding, RT+ Capabilities, RadioText) go into the station itself and
  the Log Book. Notes and any other columns are not imported (use **Clean up** in the Log Book to delete ones an older version imported).

**An import only fills blanks.** A station already in your log keeps what it
has (your own decodes and edits: call, PI, PS, PTY, slogan, RadioText and
every other column); the sheet only adds what's missing, plus its log entries.
* RDS Spy `.spy` files and RDS Spy-style `.txt` group logs.
* RDS Monitor report `.txt` files.

Re-importing merges into existing stations (same call/PI within 0.05 MHz).

**HD stations:** a row with a call like `WKQX-HD2`, a frequency like
`101.1 HD2` or an HD column saying `HD2` becomes its own station, separate
from the analog one. Imported HD stations keep exactly what your sheet says:
lineups change, so no Wikipedia name is put on a past catch. An HD station
logged later with no slogan gets its name from the *Subchannels* line of
the analog station's Wikipedia page (e.g. WKQX: "HD2: The Loop").
When you catch an imported HD station live, what you catch (its name and
RadioText) replaces the sheet's, like call letters; blank parts of a catch
never erase anything. Analog stations keep your sheet's RadioText.
With the FM Tuner's optional HD decoder, a real HD lock sent on by the RDS
Monitor logs each decoded HD2–HD8 channel as its own station (`WKQX-HD2`,
named from the decoder); HD1 stays with the analog station.

## Several logs

**Logs…** (top left, under the feed) lists your logs. Each is its own file:
the first is `settings\dx_log.db` ("Main" until you rename it), the others
are `settings\Logs\<name>.db` with their own `.xlsx` and
`settings\Backups\<name>` backups, so deleting one never touches the rest.

Tick several to show them together on the map and in the Log Book. The
**first one ticked** gets new catches, edits and imports; the rest are view
only. A station in two ticked logs shows once: info from the first log,
blanks filled from the others, and Log history lists every log's entries
tagged with the log name (each keeps where it was heard). A station only in a
view-only log shows with its log's badge; **Copy to <first log>** copies it
over (blanks only), and **→ <first log>** on one of its log entries copies
that catch. Either one copies the whole HD family (main + HD2–HD8) and the
family's entries from the same catch. The log taking catches can't be renamed
or deleted until another log is ticked first.

**Also log new catches into the other ticked logs** (in Logs…, **off by
default**) puts every new catch into all ticked logs. Ticking it opens a
warning popup: only **Turn it on** turns it on; **Keep it off**, closing it
or Esc leave it off. Edits, deletes and imports still go only to the first log.

## Backup

**Backup…** (top bar) keeps a **master backup** of your whole log – every
station and every log ever made, never trimmed – in
`settings\Backups\DX Master Backup.db` (plus a `.xlsx` copy you can open). It
is refreshed after every log and import. Put your Google Drive folder (e.g.
`G:\My Drive\DX Mapper`) in **Also copy it to** and the same master backup is
written there, so Drive keeps it off the PC. **Daily copies** (off by default)
also keeps one dated copy per day for the last 30 days, as undo points.

After a reinstall, open **Backup… > Restore** and point it at the folder that
holds `DX Master Backup.db` (e.g. your Drive folder) to bring the whole log back.

## Files (in `settings/`)

* `dx_log.db`: your first log (SQLite). `Logs\` holds your other logs.
* `DX Log.xlsx`: **Excel copy of the log**, rewritten automatically after
  every change. It has a *Stations* sheet and a *Log History* sheet. If the file
  is open in Excel, it is saved again as soon as you close it. Use
  **Export Excel** for a dated copy at any time.
  **⚠ It is a copy for viewing: edits typed into it in Excel are NOT read
  back and get overwritten. Make changes in DX Mapper, or bring an edited
  copy in with Import… (Excel or Google Sheets).** The same bold warning is
  on the top row of every sheet.
* `logger_settings.json`: map choice, Local / Extended local miles, the Log
  Book columns you turned off (and home, used only
  without an RDS Monitor folder).

## Now hearing

While the RDS Monitor is decoding a station it tells DX Mapper (every 10 s and
on every change). The map shows that transmitter as a pulsing pink "Now
hearing" marker with a line from the active Home/Away location, and the
sidebar shows call, MHz, PS, city and distance. Nothing is saved until you
press Log in the RDS Monitor. It disappears 45 s after the monitor stops.

Tick **Follow tuner** (sidebar, off by default, remembered) and the map moves
to whatever station the tuner is on and opens its details, even if it is
already in your log. It follows once per station change, so you can still
click around while you stay tuned. Unlogged stations with a known location
are just centred on the map.

## Ports

| Port | Use |
|------|-----|
| 7375 | Feed from the RDS Monitor (newline-delimited JSON, local only) |
| 7380 | Map web page/API (local only) |
| 7381-7382 | Extra map tile ports (faster first load) |
| 7374 | FM Tuner command port, used by the Tune button (DX Mapper connects to it) |

Options: `--feed-port`, `--web-port`, `--monitor-dir`, `--db`, `--excel`,
`--no-browser`.

## Tests

```
py -3 -m unittest discover -s tests -q
```

## Map tile cache

Every map tile shown (Normal, Terrain, Satellite) is saved in
`settings/TileCache`, so places already viewed load from disk and still work
offline. Saved tiles are kept for good
(failed downloads are retried automatically). Delete the folder to
free space.
