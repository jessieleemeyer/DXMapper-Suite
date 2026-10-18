#!/usr/bin/env python3
"""
RDS Monitor - Real-time RDS Spy Display
=======================================
Watches a recording.spy file and displays decoded RDS data in a GUI window:
  - Station logos (from the settings\\logos cache, downloaded if missing)
  - Call letters (decoded from PI code via NRSC-4-B algorithm)
  - PI code
  - PS (Program Service name)
  - PTY (Program Type, US RBDS definitions)
  - PTYN (Program Type Name)
  - RadioText (RT)
  - RT+ (RadioText Plus) items — Song Title and Artist
  - TP/TA/Music-Speech indicators
  - Clock Time/Date
  - TMC traffic messages (8A, ALERT-C event/location/extent/duration)
  - Signal quality and confidence indicators

Input modes (no SDR or audio hardware is opened by this program):
  1. Spy File — reads a .spy file from RDS Spy (default)
  2. FM Tuner (TCP) — RDS groups from the standalone FM Tuner on
     127.0.0.1:7376, tuner telemetry on 7373, tuner commands on 7374
  3. RDS Input — any other ASCII G source over TCP/UDP (e.g. SDR# RDSOutput
     on port 23)

Usage:
    python "RDS Monitor.py"
    python "RDS Monitor.py" --spy-file "C:\\RDS Spy\\recording.spy"
    python "RDS Monitor.py" --logo-dir "D:\\Logos" --watch "C:\\RDS Spy"
"""

import argparse
import datetime
import json
import math
import os
import re
import shutil
import sys
import time
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # one-folder suite: hide the console, copy old settings in once
    import suite_start
    suite_start.startup(Path(__file__).resolve().parent)
except ImportError:
    pass
import tmc  # noqa: E402  (sibling module, ships with this file)
import gps  # noqa: E402  (optional USB GPS dongle)
import nowplaying  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# All user-editable files live in `settings\` next to the program (it is not
# in the ZIP, so unzipping a new build over the old folder leaves them
# alone). `data\pi_codes.txt` seeds the PI list when there is none yet.
APP_DIR = Path(__file__).resolve().parent
SETTINGS_DIR = APP_DIR / "settings"
REPORTS_DIR = APP_DIR / "reports"


def _settings_file(name, *legacy, seed=None):
    """Path of `name` in SETTINGS_DIR; adopts a legacy copy or a seed file."""
    target = SETTINGS_DIR / name
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return str(target)
    if not target.exists():
        for old in legacy:
            try:
                if Path(old).is_file():
                    shutil.move(str(old), str(target))
                    break
            except OSError:
                continue
    if not target.exists() and seed is not None and Path(seed).is_file():
        try:
            shutil.copy(str(seed), str(target))
        except OSError:
            pass
    return str(target)


def _settings_logo_dir():
    target = SETTINGS_DIR / "logos"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(target)


# The free Lite build sets this to True (no reports or Group Analyzer).
LITE = False

DEFAULT_WATCH_DIR = str(SETTINGS_DIR)
DEFAULT_SPY_FILENAME = "recording.spy"
DEFAULT_SPY_FILE = str(SETTINGS_DIR / DEFAULT_SPY_FILENAME)
DEFAULT_LOGO_DIR = _settings_logo_dir()
DEFAULT_STATION_COUNT_FILE = _settings_file(
    "station_count.txt", APP_DIR / "station_count.txt")
DEFAULT_SETTINGS_FILE = _settings_file(
    "monitor_settings.json", APP_DIR / "monitor_settings.json")
# Placeholder (middle of the US) until the user sets a home location.
DEFAULT_HOME_LOCATION = (39.8283, -98.5795)
POLL_INTERVAL = 1.5  # seconds between file checks

LOGO_EXTENSIONS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp"]

# PTY code table (US RBDS / NRSC-4-B definitions)
PTY_TABLE = {
    0: "No Program Type", 1: "News", 2: "Information", 3: "Sports",
    4: "Talk", 5: "Rock", 6: "Classic Rock", 7: "Adult Hits",
    8: "Soft Rock", 9: "Top 40", 10: "Country", 11: "Oldies",
    12: "Soft", 13: "Nostalgia", 14: "Jazz", 15: "Classical",
    16: "Rhythm and Blues", 17: "Soft Rhythm and Blues", 18: "Foreign Language", 19: "Religious Music",
    20: "Religious Talk", 21: "Personality", 22: "Public", 23: "College",
    24: "(unused)", 25: "(unused)", 26: "(unused)", 27: "(unused)",
    28: "(unused)", 29: "Weather", 30: "Emergency Test", 31: "Emergency",
}

# RT+ content types (ODM-201 / IEC 62106 RadioText Plus class codes 0-63).
RTP_ITEM_TYPES = {
    0: "Dummy", 1: "Title", 2: "Album", 3: "Track Number",
    4: "Artist", 5: "Composition", 6: "Movement", 7: "Conductor",
    8: "Composer", 9: "Band", 10: "Comment", 11: "Genre",
    12: "News", 13: "News Local", 14: "Stockmarket", 15: "Sport",
    16: "Lottery", 17: "Horoscope", 18: "Daily Diversion", 19: "Health",
    20: "Event", 21: "Scene", 22: "Cinema", 23: "TV",
    24: "Date/Time", 25: "Weather", 26: "Traffic", 27: "Alarm",
    28: "Advertisement", 29: "URL", 30: "Other Info",
    31: "Station Name (short)", 32: "Station Name (long)",
    33: "Programme Now", 34: "Programme Next", 35: "Programme Part",
    36: "Programme Host", 37: "Editorial Staff", 38: "Frequency",
    39: "Homepage", 40: "Subchannel", 41: "Phone Hotline",
    42: "Phone Studio", 43: "Phone Other", 44: "SMS Studio",
    45: "SMS Other", 46: "Email Hotline", 47: "Email Studio",
    48: "Email Other", 49: "MMS Other", 50: "Chat", 51: "Chat Centre",
    52: "Vote Question", 53: "Vote Centre", 54: "(rfu)", 55: "(rfu)",
    56: "Private 1", 57: "Private 2", 58: "Private 3", 59: "Place",
    60: "Appointment", 61: "Identifier", 62: "Purchase", 63: "Get Data",
}

# Order RT+ fields are shown in: what is playing first, then programme and
# contact details.  Types not listed keep their class order after these.
RTP_DISPLAY_ORDER = [
    1, 4, 2, 3, 9, 8, 7, 5, 6, 11, 10,
    33, 34, 35, 36, 37, 32, 31, 38, 39, 40,
]

# Title and artist are also shown in the big display lines.
RTP_TITLE_TYPES = (1,)
RTP_ARTIST_TYPES = (4,)

# Listener-facing labels for the RT+ panel rows (class code -> label).
# Anything not listed falls back to its RTP_ITEM_TYPES name.
RTP_FIELD_LABELS = {
    1: "Title", 4: "Artist", 2: "Album", 3: "Track", 9: "Band",
    8: "Composer", 7: "Conductor", 5: "Composition", 6: "Movement",
    11: "Genre", 10: "Comment",
    33: "Now", 34: "Next", 35: "Part", 36: "Host", 37: "Editorial",
    32: "Station", 31: "Station (short)", 38: "Frequency", 39: "Website",
    40: "Subchannel", 41: "Phone (hotline)", 42: "Phone (studio)",
    43: "Phone", 44: "SMS (studio)", 45: "SMS", 46: "Email (hotline)",
    47: "Email (studio)", 48: "Email", 29: "URL", 25: "Weather",
    26: "Traffic", 12: "News", 13: "Local News", 15: "Sport",
    28: "Advert", 30: "Info",
}

# Colour per RT+ field group: now playing / programme / station & contact.
RTP_GROUP_MUSIC = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
RTP_GROUP_PROGRAMME = (33, 34, 35, 36, 37, 12, 13, 15, 25, 26)


def rtp_field_colour(itype):
    if itype in RTP_GROUP_MUSIC:
        return "#ffb86c" if itype in RTP_TITLE_TYPES else (
            "#bd93f9" if itype in RTP_ARTIST_TYPES else "#f1fa8c")
    if itype in RTP_GROUP_PROGRAMME:
        return "#8be9fd"
    return "#50fa7b"

# Rows kept in the Group Analyzer's rolling stream and HEX views.
LIVE_TEXT_ROWS = 500

# Distinct TMC messages remembered per station (oldest dropped first).
TMC_MAX_MESSAGES = 60

# Newest groups considered when deciding which station is on air.
RECENT_PI_WINDOW = 24

# How long a TMC message stays on screen after it was last heard, by the
# ALERT-C duration/persistence code (ISO 14819-1 table). Code 0 has no
# explicit duration, so it is kept only while the station keeps repeating
# it; code 7 ("rest of the day") expires at local midnight.
TMC_PERSISTENCE_S = {0: 15 * 60, 1: 15 * 60, 2: 30 * 60, 3: 60 * 60,
                     4: 2 * 3600, 5: 3 * 3600, 6: 4 * 3600}
# Hard ceiling — nothing survives longer than this without being re-sent.
TMC_MAX_AGE_S = 24 * 3600


def _tmc_state(station):
    t = station.get("tmc")
    if t is None:
        t = {"sys": None, "group_type": None, "messages": {}, "order": [],
             "groups": 0, "tuning": 0, "seq": 0, "last_ci": None}
        station["tmc"] = t
    return t


def tmc_reset(station):
    """Forget every TMC message for a station (decoder stopped / retuned)."""
    if station.get("tmc") is not None:
        station["tmc"] = None


def tmc_reset_all(stations):
    for st in stations.values():
        tmc_reset(st)


def tmc_expires_at(msg, heard=None):
    """Wall-clock time (epoch seconds) at which a message is stale."""
    heard = msg.get("heard", 0.0) if heard is None else heard
    dur = msg.get("duration")
    if dur == 7:
        local = time.localtime(heard)
        midnight = time.mktime((local.tm_year, local.tm_mon, local.tm_mday + 1,
                                0, 0, 0, 0, 0, -1))
        return min(midnight, heard + TMC_MAX_AGE_S)
    return heard + TMC_PERSISTENCE_S.get(dur, TMC_PERSISTENCE_S[0])


def tmc_expire(station, now=None):
    """Drop messages whose persistence has run out. Returns number removed."""
    t = station.get("tmc")
    if not t:
        return 0
    now = time.time() if now is None else now
    stale = [k for k, m in t["messages"].items() if now >= tmc_expires_at(m)]
    for k in stale:
        t["messages"].pop(k, None)
        if t["last_ci"] == k:
            t["last_ci"] = None
    if stale:
        t["order"] = [k for k in t["order"] if k in t["messages"]]
    return len(stale)


def _tmc_add_group(station, b2_val, b3_val, b4_val, now=None):
    """Fold one 8A group into the station's TMC message list."""
    t = _tmc_state(station)
    t["groups"] += 1
    g = tmc.decode_group(b2_val, b3_val, b4_val)
    kind = g["kind"]
    if kind == "tuning":
        t["tuning"] += 1
        return
    if kind == "next":
        key = t["last_ci"]
        msg = t["messages"].get(key) if key is not None else None
        if msg is not None:
            msg["extra_groups"] = msg.get("extra_groups", 0) + 1
        return
    key = tmc.message_key(g)
    t["seq"] += 1
    msg = t["messages"].get(key)
    if msg is None:
        if len(t["order"]) >= TMC_MAX_MESSAGES:
            oldest = t["order"].pop(0)
            t["messages"].pop(oldest, None)
        msg = dict(g)
        msg["count"] = 0
        msg["extra_groups"] = 0
        t["messages"][key] = msg
        t["order"].append(key)
    else:
        msg.update({k: v for k, v in g.items() if k != "kind"} )
        if kind == "single":
            msg["kind"] = "single"
    msg["count"] += 1
    msg["seq"] = t["seq"]
    msg["heard"] = now if now is not None else time.time()
    msg["text"] = tmc.event_text(msg["event"])
    t["last_ci"] = key if kind == "first" else None


def tmc_messages(station, newest_first=True, now=None):
    """Current (unexpired) TMC messages for a station, newest first."""
    t = station.get("tmc")
    if not t:
        return []
    tmc_expire(station, now)
    msgs = [t["messages"][k] for k in t["order"] if k in t["messages"]]
    msgs.sort(key=lambda m: m.get("seq", 0), reverse=newest_first)
    return msgs

# ODA application IDs that carry RT+ tags.
RTP_AIDS = (0x4BD7, 0x4BD8)

# Registered ODA application identifiers (RDS Forum / NRSC list).
ODA_APP_NAMES = {
    0x0093: "Cross referencing DAB within RDS",
    0x0BCB: "Leisure & Practical Info for Drivers",
    0x0C24: "ELECTRABEL-DSM 7",
    0x0CC1: "Wireless Playground broadcast control",
    0x0D45: "RDS-TMC: ALERT-C test",
    0x0D8B: "ELECTRABEL-DSM 18",
    0x0E2C: "ELECTRABEL-DSM 3",
    0x0E31: "ELECTRABEL-DSM 13",
    0x0F87: "ELECTRABEL-DSM 2",
    0x125F: "I-FM-RDS for fixed and mobile devices",
    0x1BDA: "ELECTRABEL-DSM 1",
    0x1C5E: "ELECTRABEL-DSM 20",
    0x1C68: "ITIS In-vehicle data base",
    0x1CB1: "ELECTRABEL-DSM 10",
    0x1D47: "ELECTRABEL-DSM 4",
    0x1DC2: "CITIBUS 4",
    0x1DC5: "Encrypted TTI using ALERT-Plus",
    0x1E8F: "ELECTRABEL-DSM 17",
    0x4AA1: "RASANT",
    0x4AB7: "ELECTRABEL-DSM 9",
    0x4BA2: "ELECTRABEL-DSM 5",
    0x4BD7: "RadioText Plus (RT+)",
    0x4BD8: "RT+ for enhanced RadioText (eRT+)",
    0x4C59: "CITIBUS 2",
    0x4D87: "Radio Commerce System (RCS)",
    0x4D95: "ELECTRABEL-DSM 16",
    0x4D9A: "ELECTRABEL-DSM 11",
    0x5757: "Personal weather station",
    0x6363: "Hybradio RDS-Net",
    0x6365: "RDS2 9-bit AF lists",
    0x6552: "Enhanced RadioText (eRT)",
    0x6A7A: "Warning receiver",
    0x7373: "Enhanced early warning system",
    0xA112: "NL Alert system",
    0xA911: "Data FM Selective Multipoint Messaging",
    0xABCF: "RF Power Monitoring",
    0xC350: "NRSC Song Title and Artist",
    0xC3A1: "Personal Radio Service",
    0xC3B0: "iTunes Tagging",
    0xC3C3: "NAVTEQ Traffic Plus",
    0xC4D4: "eEAS",
    0xC549: "Smart Grid Broadcast Channel",
    0xC563: "ID Logic",
    0xC737: "Utility Message Channel (UMC)",
    0xCB73: "CITIBUS 1",
    0xCB97: "CITIBUS 3",
    0xCC21: "CITIBUS 5",
    0xCD46: "RDS-TMC: ALERT-C",
    0xCD47: "RDS-TMC: ALERT-C",
    0xE123: "APS Gateway",
    0xE1C1: "Action code",
    0xE319: "Fixed and mobile devices",
    0xE411: "Beacon downlink",
    0xE440: "ELECTRABEL-DSM 14",
    0xE4A6: "ELECTRABEL-DSM 19",
    0xE5D7: "ELECTRABEL-DSM 7",
    0xE911: "EAS open protocol",
    0xFF7F: "RFT: Station logo (RDS2)",
}


def oda_app_name(aid):
    return ODA_APP_NAMES.get(aid, "Unknown / unregistered")


# Group types with a fixed meaning in the RDS/RBDS standard; everything
# else is an ODA carrier (or free for ODA use).
STANDARD_GROUPS = {"0A", "0B", "1A", "1B", "2A", "2B", "3A", "4A", "10A",
                   "14A", "14B", "15A", "15B"}
GROUP_COLOURS = {"basic": "#0f0", "oda": "#ffb347", "other": "#8be9fd"}


def long_ps_text(segments):
    """(text, complete) for Long PS segments {seg: 4 bytes}. Only the run
    of segments from 0 counts; '' when nothing valid UTF-8 has arrived."""
    raw, complete = b"", False
    for seg in range(8):
        chunk = segments.get(seg)
        if chunk is None:
            break
        chunk = bytes(chunk)
        if b"\r" in chunk:
            raw += chunk[:chunk.index(b"\r")]
            complete = True
            break
        raw += chunk
    else:
        complete = True
    raw = raw.rstrip(b"\x00 ")
    for cut in range(4):  # a partial name may end mid-character
        try:
            text = raw[:len(raw) - cut].decode("utf-8")
            break
        except UnicodeDecodeError:
            if complete:
                return "", False
    else:
        return "", False
    if not text.strip() or not all(c.isprintable() for c in text):
        return "", False
    return text.strip(), complete


def lps_add_group(station, b2_val, b3_val, b4_val):
    """Add one 15A Long PS group to a station."""
    seg = b2_val & 0x7
    chunk = bytes(((b3_val >> 8) & 0xFF, b3_val & 0xFF,
                   (b4_val >> 8) & 0xFF, b4_val & 0xFF))
    segs = station.setdefault("lps_segments", {})
    segs[seg] = chunk
    station["long_ps"], station["long_ps_complete"] = long_ps_text(segs)


def group_class(gt, oda_carriers=()):
    """'basic' for 0/2/15 (PS/RT/tuning), 'oda' for ODA groups (TMC 8A,
    3A announcements and any 3A-announced carrier), 'other' for the rest
    (CT, EON, PIN, PTYN...)."""
    if gt in oda_carriers or gt == "8A" or gt == "3A":
        return "oda"
    if gt not in STANDARD_GROUPS:
        return "oda"
    if gt[0] in "02" or gt in ("15A", "15B"):
        return "basic"
    return "other"


def parse_oda_announcement(b2_val, b4_val):
    """Decode a type 3A group: returns (aid, carrier group type string) —
    block 2 bits 4..1 = application group type, bit 0 = version, block 4 = AID.
    Carrier '0A' means the application uses no dedicated group (AID only)."""
    app_type = (b2_val >> 1) & 0xF
    app_version = "A" if (b2_val & 0x1) == 0 else "B"
    return b4_val & 0xFFFF, f"{app_type}{app_version}"


def stable_pi(previous, recent_pis, min_groups=3):
    """Pick the station to show from the PIs of the latest groups.

    Off-air decoders pass the odd group whose PI has a bit error; following
    the newest PI blindly would flip the display (and reset the live
    analyzer) on every one.  Switch only when a different PI has clearly
    taken over the recent groups."""
    if not recent_pis:
        return previous
    counts = {}
    for pi in recent_pis:
        counts[pi] = counts.get(pi, 0) + 1
    best = max(counts, key=lambda pi: (counts[pi], pi == previous))
    if previous is None or previous not in counts:
        return best
    if best != previous and counts[best] >= min_groups \
            and counts[best] > counts[previous]:
        return best
    return previous


class OdaTracker:
    """Per-station tally of ODA applications advertised in 3A groups and how
    often their carrier groups actually appear, for the analyzer's ODA AID tab."""

    def __init__(self):
        self.reset()

    def reset(self):
        import time as _time
        self.started = _time.time()
        self.total = 0
        self.group_counts = {}   # "8A" -> count
        self.apps = {}           # aid -> {"carrier": "8A", "announced": n}

    def add_group(self, gt, b2_val=None, b4_val=None):
        self.total += 1
        self.group_counts[gt] = self.group_counts.get(gt, 0) + 1
        if gt == "3A" and b2_val is not None and b4_val is not None:
            aid, carrier = parse_oda_announcement(b2_val, b4_val)
            app = self.apps.setdefault(aid, {"carrier": carrier, "announced": 0})
            app["carrier"] = carrier
            app["announced"] += 1

    def rows(self, now=None):
        """[(aid, name, carrier, count, proportion 0-1, rate/s)] sorted by AID."""
        import time as _time
        elapsed = max((now or _time.time()) - self.started, 1e-3)
        out = []
        for aid in sorted(self.apps):
            app = self.apps[aid]
            carrier = app["carrier"]
            count = self.group_counts.get(carrier, 0) if carrier != "0A" else 0
            prop = count / self.total if self.total else 0.0
            out.append((aid, oda_app_name(aid), carrier, count, prop,
                        count / elapsed))
        tmc_8a = self.group_counts.get("8A", 0)
        if tmc_8a and not any(app["carrier"] == "8A" for app in self.apps.values()):
            # 8A is the dedicated TMC group: show it even when the station
            # never announces the AID in 3A.
            prop = tmc_8a / self.total if self.total else 0.0
            out.append((tmc.TMC_AID, "RDS-TMC: ALERT-C (8A, no 3A announcement)",
                        "8A", tmc_8a, prop, tmc_8a / elapsed))
        return out


# --- Alternative Frequencies (type 0A block 3) -------------------------
# Each block carries two AF codes: 1-204 = 87.6 + 0.1*(n-1) MHz,
# 205 filler, 224-249 = "list follows, n = code-224 frequencies",
# 250 = an LF/MF frequency follows, everything else unused.
AF_MAX_LIST = 25


def af_code_to_mhz(code):
    if 1 <= code <= 204:
        return round(87.5 + 0.1 * code, 1)
    return None


def _af_state(station):
    a = station.get("af")
    if a is None:
        a = {"count": None,     # frequencies announced by the header code
             "freqs": [],       # unique MHz values in order received
             "pairs": [],       # raw (a, b) MHz pairs, for method-B detection
             "method": None,    # "A", "B" or None while undecided
             "tuned": None,     # method B: the frequency paired with all others
             "blocks": 0}
        station["af"] = a
    return a


def af_add_block(station, b3_val):
    """Fold one 0A block-3 word into the station's AF list."""
    hi, lo = (b3_val >> 8) & 0xFF, b3_val & 0xFF
    if hi == 0 and lo == 0:
        return
    a = _af_state(station)
    a["blocks"] += 1
    if 224 <= hi <= 249:
        n = hi - 224
        if a["count"] != n:
            a.update({"count": n, "freqs": [], "pairs": [], "method": None,
                      "tuned": None})
        f = af_code_to_mhz(lo)
        if f is not None and f not in a["freqs"]:
            a["freqs"].append(f)
        return
    fa, fb = af_code_to_mhz(hi), af_code_to_mhz(lo)
    for f in (fa, fb):
        if f is not None and f not in a["freqs"]:
            a["freqs"].append(f)
    if fa is not None and fb is not None:
        pair = (fa, fb)
        if pair not in a["pairs"]:
            a["pairs"].append(pair)
            if len(a["pairs"]) > AF_MAX_LIST:
                a["pairs"].pop(0)
        # Method B: every pair contains the tuned frequency
        if len(a["pairs"]) >= 2:
            common = set(a["pairs"][0])
            for p in a["pairs"][1:]:
                common &= set(p)
            if len(common) == 1:
                a["method"] = "B"
                a["tuned"] = common.pop()
            elif not common:
                a["method"] = "A"
    if len(a["freqs"]) > AF_MAX_LIST + 1:
        del a["freqs"][:-(AF_MAX_LIST + 1)]


DI_SEGMENT_NAMES = {0: "dynamic PTY", 1: "compression",
                    2: "artificial head", 3: "stereo"}


DI_STEREO = 0x1           # d0
DI_ARTIFICIAL_HEAD = 0x2  # d1
DI_COMPRESSED = 0x4       # d2
DI_DYNAMIC_PTY = 0x8      # d3


def di_update(st, b2_val):
    """Assemble the DI flags from a type 0 group: block 2 bit 2 carries one
    DI bit, addressed by the PS segment number in bits 1-0.  Segment 3
    carries d0 (stereo), 2 carries d1 (artificial head), 1 carries d2
    (compression) and 0 carries d3 (dynamic PTY).  st["di"] is the standard
    d3..d0 value (0-15).  Returns the (segment, bit, complete) tuple for
    this group."""
    seg = b2_val & 0x3
    bit = (b2_val >> 2) & 0x1
    segs = st.setdefault("di_segments", {})
    segs[seg] = bit
    complete = len(segs) == 4
    if 3 in segs:
        st["di"] = sum(v << (3 - s) for s, v in segs.items())
    st["di_dynamic_pty"] = bool(segs[0]) if 0 in segs else None
    st["di_last"] = (seg, bit, complete)
    return seg, bit, complete


def di_bits(di):
    """Decode a standard DI value bit by bit into its four flags."""
    return {"stereo": bool(di & DI_STEREO),
            "artificial_head": bool(di & DI_ARTIFICIAL_HEAD),
            "compressed": bool(di & DI_COMPRESSED),
            "dynamic_pty": bool(di & DI_DYNAMIC_PTY)}


def di_flag_text(di):
    """Human-readable RDS DI (Decoder Identification) flags."""
    b = di_bits(di)
    flags = ["stereo" if b["stereo"] else "mono"]
    if b["artificial_head"]:
        flags.append("artificial head")
    if b["compressed"]:
        flags.append("compression")
    if b["dynamic_pty"]:
        flags.append("dynamic PTY")
    return ", ".join(flags)


def stereo_status_lines(station):
    """Report block keeping the three stereo-related values apart: what the
    tuner is outputting, whether the 19 kHz pilot is physically present, and
    what the station declares in its RDS DI bits."""
    pilot = station.get("tuner_stereo")
    mode = station.get("tuner_stereo_mode")
    pilot_db = station.get("pilot_db")
    di = station.get("di")
    if pilot is None and mode is None and di is None:
        return []
    lines = ["", "  --- Stereo Status ---"]
    if mode == "mono":
        audio = "mono (stereo decoding switched off)"
    elif pilot is None:
        audio = "(unknown)"
    elif pilot:
        audio = "stereo"
    else:
        audio = "mono fallback (no pilot lock)"
    lines.append(f"  Tuner audio mode    = {audio}")
    if pilot is None:
        pilot_text = "(unknown)"
    else:
        pilot_text = "detected / locked" if pilot else "not detected"
        if isinstance(pilot_db, (int, float)):
            pilot_text += f" ({pilot_db:.1f} dB)"
    lines.append(f"  19 kHz pilot        = {pilot_text}")
    if di is None:
        lines.append("  RDS DI station flag = (none received)")
        return lines
    b = di_bits(di)
    segs = station.get("di_segments") or {}
    cycle = ("complete DI cycle" if len(segs) == 4
             else f"partial DI cycle, {len(segs)}/4 bits")
    lines.append(f"  RDS DI station flag = "
                 f"{'stereo' if b['stereo'] else 'mono'}"
                 f" (station declaration, {cycle})")
    lines.append(f"  DI audio processing = "
                 f"{'compressed' if b['compressed'] else 'not compressed'}; "
                 f"{'artificial-head' if b['artificial_head'] else 'not artificial-head'}")
    lines.append(f"  DI PTY mode         = "
                 f"{'dynamic' if b['dynamic_pty'] else 'static'}")
    if pilot is not None and b["stereo"] != bool(pilot):
        if pilot:
            why = ("expected mismatch: tuner pilot is locked, but\n"
                   "                        station RDS DI declares mono")
        else:
            why = ("mismatch: station RDS DI declares stereo, but\n"
                   "                        no pilot is being received")
        lines.append(f"  Status              = {why}")
    elif pilot is not None:
        lines.append("  Status              = tuner and station DI agree")
    return lines


def gain_status_lines(station):
    """Report line for the tuner's RF gain: manual setting, or the gain the
    tuner AGC reports while in automatic mode (if the driver reports it)."""
    mode = station.get("tuner_gain_mode")
    if not mode:
        return []
    gain = station.get("tuner_gain_db")
    db = f"{gain:.1f} dB" if isinstance(gain, (int, float)) else None
    if mode == "manual":
        text = f"manual {db}" if db else "manual"
    else:
        text = f"automatic ({db} reported)" if db else "automatic (not reported)"
    return [f"  Tuner gain          = {text}"]


# Signal units follow the FM Tuner's View > Signal units choice (sent
# in its telemetry); S-units are relative: S1 at -70 dBFS, 6 dB each.
_SIGNAL_UNITS = ["s"]


def mhz_text(freq, tel=None):
    """Frequency with the tuner region's decimals (101.1, OIRT 66.44)."""
    dec = ((tel or {}).get("band") or {}).get("decimals", 1)
    return f"{freq:.{dec if dec in (1, 2) else 1}f}"


def signal_text(dbfs, tel=None):
    """A signal in dBFS as the tuner shows it: 'S7', 'S9+12' or dBFS."""
    units = (tel or {}).get("signal_units")
    if units in ("s", "db"):
        _SIGNAL_UNITS[0] = units
    if not isinstance(dbfs, (int, float)):
        return None
    if _SIGNAL_UNITS[0] != "s":
        return f"{dbfs:.1f} dBFS"
    s = (dbfs + 70.0) / 6.0 + 1
    if s < 1:
        return "S0"
    if s < 9.5:
        return f"S{int(round(s))}"
    return f"S9+{int(round((s - 9) * 6.0))}"


HD_MER_GOOD_DB = 10.0       # MER at/above this is solid HD (green)
HD_MER_FAIR_DB = 6.0        # below this HD drops out a lot (red)


def hd_mer_grade(mer):
    """(word, colour) for an HD MER in dB: green good, yellow fair, red weak."""
    if not isinstance(mer, (int, float)):
        return "", "#f8f8f2"
    if mer >= HD_MER_GOOD_DB:
        return "good", "#50fa7b"
    if mer >= HD_MER_FAIR_DB:
        return "fair", "#f1fa8c"
    return "weak", "#ff5555"


def hd_art_path(hd):
    """Album art the station is sending over HD for the channel being
    played, while HD is locked; None otherwise."""
    if not isinstance(hd, dict) or not hd.get("synced"):
        return None
    programs = hd.get("programs") or []
    prog = hd.get("audio_program") or 0
    path = next((p.get("art_path") for p in programs
                 if p.get("program") == prog), None)
    if not path and not programs:
        path = hd.get("art_path")
    return path if path and os.path.isfile(path) else None


def hd_status_lines(station):
    """HD Radio section of the report, from the tuner's HD decoder. Only a
    real decoder lock counts as HD; the decoder is an optional add-on."""
    hd = station.get("hd")
    if not isinstance(hd, dict):
        return []
    if not hd.get("installed"):
        return ["  HD Radio           = decoder not installed (optional add-on)"]
    if not hd.get("synced"):
        return ["  HD Radio           = "
                + ("lost lock" if hd.get("ever_synced") else "no lock")]
    mer, ber = hd.get("mer_db"), hd.get("ber")
    quality = ", ".join(x for x in (
        f"MER {mer:.1f} dB" if isinstance(mer, (int, float)) else None,
        f"BER {ber:.4f}" if isinstance(ber, (int, float)) else None) if x)
    lines = ["  HD Radio           = locked" + (f" ({quality})" if quality else ""),
             f"    playing          = {hd.get('playing') or 'analog'}"
             f" (mode {hd.get('mode') or 'auto'})"]
    for key, label in (("call", "station name"), ("slogan", "slogan"),
                       ("message", "message"), ("facility_id", "facility ID"),
                       ("country", "country")):
        if hd.get(key):
            lines.append(f"    {label:<16} = {hd[key]}")
    loc = hd.get("location")
    if loc:
        lines.append(f"    {'location':<16} = {loc[0]:.4f}, {loc[1]:.4f}")
    for prog in hd.get("programs") or []:
        desc = " ".join(x for x in (prog.get("name"), f"({prog['type']})"
                                    if prog.get("type") else None) if x)
        lines.append(f"    {prog.get('hd', '?'):<16} = {desc or '(no name yet)'}")
        song = " - ".join(x for x in (prog.get("title"), prog.get("artist"))
                          if x)
        if song:
            lines.append(f"      now playing    = {song}"
                         + (f" [{prog['album']}]" if prog.get("album") else ""))
    if hd.get("alert"):
        cats = ", ".join(hd.get("alert_categories") or [])
        lines.append(f"    ALERT            = {hd['alert']}"
                     + (f" ({cats})" if cats else ""))
    logos = sorted((hd.get("logos") or {}).keys())
    if logos:
        lines.append(f"    {'station logos':<16} = {', '.join(logos)}"
                     " (kept: HD1 as CALL, HD2+ as CALL-HD#)")
    showing = [label for key, label in (("art_path", "album art"),
                                        ("traffic_path", "traffic map"),
                                        ("weather_path", "weather map"))
               if hd.get(key)]
    if showing:
        lines.append(f"    {'showing now':<16} = {', '.join(showing)}"
                     " (not saved)")
    counts = hd.get("counts") or {}
    if counts:
        lines.append("    HD data          = " + "  ".join(
            f"{k}:{v}" for k, v in counts.items()))
    for ts, text in (hd.get("log") or [])[-8:]:
        lines.append(f"      {time.strftime('%H:%M:%S', time.localtime(ts))}"
                     f"  {text}")
    return lines


def hd_panel_rows(hd):
    """[(label, text, colour)] for the main window's HD Radio panel."""
    if not isinstance(hd, dict) or not hd.get("installed"):
        return []
    if not hd.get("synced"):
        return []  # the panel only shows while HD is locked
    mer, ber = hd.get("mer_db"), hd.get("ber")
    status = "  ".join(x for x in (
        "locked",
        f"BER {ber:.4f}" if isinstance(ber, (int, float)) else "") if x)
    rows = [("Status", status, "#50fa7b")]
    if isinstance(mer, (int, float)):
        word, colour = hd_mer_grade(mer)
        rows.append(("MER", f"{mer:.1f} dB  {word}", colour))
    rows += [
            ("Playing", f"{hd.get('playing') or 'analog'}"
             f" ({(hd.get('mode') or 'auto').replace('_', ' ')})", "#8be9fd")]
    for key, label in (("call", "Station"), ("slogan", "Slogan"),
                       ("message", "Message")):
        if hd.get(key):
            rows.append((label, str(hd[key]), "#f8f8f2"))
    for prog in hd.get("programs") or []:
        desc = " ".join(x for x in (prog.get("name"), f"({prog['type']})"
                                    if prog.get("type") else None) if x)
        song = " - ".join(x for x in (prog.get("title"), prog.get("artist"))
                          if x)
        rows.append((prog.get("hd", "HD?"), "\n".join(
            x for x in (desc, song) if x) or "(no name yet)", "#ffb86c"))
    if hd.get("alert"):
        rows.append(("ALERT", str(hd["alert"]), "#ff5555"))
    return rows


def hd_analyzer_view(hd):
    """(heading, [(type, proportion bar, count)], [log lines]) for the
    analyzer's HD Data tab: the HD equivalent of Group Statistics."""
    if not isinstance(hd, dict):
        return "Waiting for the FM Tuner...", [], []
    if not hd.get("installed"):
        return "HD decoder not installed (optional add-on)", [], []
    if hd.get("synced"):
        mer, ber = hd.get("mer_db"), hd.get("ber")
        head = "HD locked" + "".join(x for x in (
            f" - {hd['call']}" if hd.get("call") else "",
            f"   MER {mer:.1f} dB" if isinstance(mer, (int, float)) else "",
            f"   BER {ber:.4f}" if isinstance(ber, (int, float)) else ""))
    else:
        head = "HD lost lock" if hd.get("ever_synced") \
            else "No HD lock on this frequency"
    counts = {k: v for k, v in (hd.get("counts") or {}).items()
              if isinstance(v, (int, float)) and v > 0}
    total = sum(counts.values())
    rows = []
    for label in sorted(counts, key=lambda k: (-counts[k], k)):
        pct = counts[label] / total
        filled = int(pct * 16)
        rows.append((label, "\u2588" * filled + "\u2591" * (16 - filled)
                     + f" {pct * 100:5.1f}%", int(counts[label])))
    log = [f"{time.strftime('%H:%M:%S', time.localtime(ts))}  {text}"
           for ts, text in (hd.get("log") or [])[-200:]]
    return head, rows, log


HD_LOGO_EXTS = (".png", ".jpg", ".jpeg", ".gif")


def adopt_hd_logo(callsign, hd, logo_dir):
    """Keep the station logos an HD station broadcasts. HD1 shares the
    analog station's logo (CALL.png; the logo it replaces is kept once as
    CALL.before-hd.<ext>); HD2-HD8 get CALL-HD2.png ... CALL-HD8.png.
    Album art and maps are not kept. True if the station's logo changed."""
    logos = dict((hd or {}).get("logos") or {})
    if (hd or {}).get("logo_path") and "HD1" not in logos:
        logos["HD1"] = hd["logo_path"]
    call = (callsign or "").split("-")[0].upper()
    if not call or not logos:
        return False
    folder = Path(logo_dir)
    try:  # logos chosen in DX Mapper's logo picker stay
        picked = set((folder / "picked.txt").read_text().upper().split())
    except OSError:
        picked = set()
    changed = False
    for tag, path in sorted(logos.items()):
        if not re.fullmatch(r"HD[1-8]", tag) or not path \
                or not os.path.isfile(path):
            continue
        try:
            data = Path(path).read_bytes()
            ext = os.path.splitext(path)[1].lower() or ".png"
            folder.mkdir(parents=True, exist_ok=True)
            if tag == "HD1":
                if call in picked:
                    continue
                changed |= _replace_logo(folder, call, ext, data, backup=True)
            else:
                _replace_logo(folder, f"{call}-{tag}", ext, data, backup=False)
        except OSError:
            continue
    return changed


def _replace_logo(folder, stem, ext, data, backup):
    target = folder / f"{stem}{ext}"
    if target.is_file() and target.read_bytes() == data:
        return False
    for old in folder.glob(f"{stem}.*"):
        if old.suffix.lower() not in HD_LOGO_EXTS or ".before-hd" in old.name:
            continue
        if backup:
            saved = old.with_name(f"{stem}.before-hd{old.suffix}")
            if not saved.exists():
                shutil.copy2(old, saved)
        if old != target:
            old.unlink()
    target.write_bytes(data)
    return True


# --- EON (Enhanced Other Networks, type 14A/14B) ------------------------
# Block 4 is the other network's PI; block 2's low 4 bits pick what block
# 3 carries: 0-3 PS (2 chars each), 4 AF list (method A pairs), 5-9 mapped
# frequency pairs (tuned, other), 12 linkage, 13 PTY + TA, 14 PIN.
# Type 14B only carries the other network's TP/TA switch.
def _eon_state(station, on_pi):
    eon = station.get("eon")
    if eon is None:
        eon = {}
        station["eon"] = eon
    net = eon.get(on_pi)
    if net is None:
        net = {"ps_segments": {}, "ps": "", "af": [], "mapped": [],
               "pty": None, "ta": False, "tp": False, "pin": None,
               "linkage": None, "groups": 0}
        eon[on_pi] = net
    return net


def eon_add_group(station, version, b2_val, b3_val, b4_val):
    """Fold one 14A/14B group into the station's EON table."""
    if b4_val in (0, None):
        return
    on_pi = f"{b4_val:04X}"
    net = _eon_state(station, on_pi)
    net["groups"] += 1
    net["tp"] = bool((b2_val >> 4) & 1)
    if version == 1:
        net["ta"] = bool((b2_val >> 3) & 1)
        return
    if b3_val is None:
        return
    variant = b2_val & 0xF
    if variant <= 3:
        net["ps_segments"][variant] = (chr((b3_val >> 8) & 0xFF), chr(b3_val & 0xFF))
        chars = [' '] * 8
        for seg, pair in net["ps_segments"].items():
            chars[seg * 2], chars[seg * 2 + 1] = pair
        net["ps"] = ''.join(c if 32 <= ord(c) < 127 else ' ' for c in chars).rstrip()
    elif variant == 4:
        for f in (af_code_to_mhz((b3_val >> 8) & 0xFF), af_code_to_mhz(b3_val & 0xFF)):
            if f is not None and f not in net["af"]:
                net["af"].append(f)
    elif 5 <= variant <= 9:
        tuned = af_code_to_mhz((b3_val >> 8) & 0xFF)
        other = af_code_to_mhz(b3_val & 0xFF)
        if tuned is not None and other is not None and (tuned, other) not in net["mapped"]:
            net["mapped"].append((tuned, other))
    elif variant == 12:
        net["linkage"] = b3_val
    elif variant == 13:
        net["pty"] = (b3_val >> 11) & 0x1F
        net["ta"] = bool(b3_val & 1)
    elif variant == 14:
        net["pin"] = b3_val


USABLE_WINDOW = 64  # groups (~6 s): a field must have refreshed within this


def usable_info_score(data):
    """0-100: how much of the station's information is currently being
    decoded (PS/RT/PTY/CT/AF/RT+/TMC/EON assembled recently), scaled by
    block yield.  Independent of raw block-error rate so a strong but
    data-poor station and a weak but complete one both read sensibly."""
    stats = data.get("stats", {})
    total_groups = stats.get("total_groups", 0)
    total_blocks = stats.get("total_blocks", 0)
    bad_blocks = stats.get("bad_blocks", 0)
    af_f, _af_exp, af_done = af_summary(data)
    eon = data.get("eon") or {}
    tmc = data.get("tmc") or {}
    fresh = data.get("fresh", {})

    def _fresh(field):
        n = fresh.get(field)
        return n is not None and total_groups - n <= USABLE_WINDOW

    items = [
        (20, 1.0 if total_groups else 0.0),
        (25, len(data.get("ps_segments", {})) / 4 if _fresh("ps") else 0.0),
        (20, len(data.get("rt_segments", {})) / 16 if _fresh("rt") else 0.0),
        (10, 1.0 if _fresh("pty") else 0.0),
        (5, 1.0 if _fresh("ct") or data.get("date_time") is not None else 0.0),
        (5, (1.0 if af_done else 0.5) if _fresh("af") else 0.0),
        (5, 1.0 if data.get("rtp_items") and _fresh("rt") else 0.0),
        (5, 1.0 if tmc.get("messages") and _fresh("tmc") else 0.0),
        (5, 1.0 if eon and _fresh("eon") else 0.0),
    ]
    yield_f = 1.0 - (bad_blocks / total_blocks if total_blocks else 1.0)
    return int(sum(w * min(1.0, max(0.0, f)) for w, f in items)
               * (0.5 + 0.5 * yield_f))


def eon_lines(station):
    """Report lines describing every other network announced via EON."""
    eon = station.get("eon") or {}
    out = []
    for on_pi in sorted(eon):
        net = eon[on_pi]
        pty = PTY_TABLE.get(net["pty"], "?") if net["pty"] is not None else "-"
        out.append(f"    PI {on_pi}  PS={net['ps'] or '-':<8}  PTY={pty}  "
                   f"TP={'Yes' if net['tp'] else 'No'}  TA={'Yes' if net['ta'] else 'No'}"
                   f"  ({net['groups']} groups)")
        if net["af"]:
            out.append("      AF      = " + ", ".join(f"{f:.1f}" for f in net["af"]))
        if net["mapped"]:
            out.append("      Mapped  = " + ", ".join(
                f"{t:.1f}->{o:.1f}" for t, o in net["mapped"]))
        if net["pin"] is not None:
            out.append(f"      PIN     = 0x{net['pin']:04X}")
        if net["linkage"] is not None:
            out.append(f"      Linkage = 0x{net['linkage']:04X}")
    return out


def af_summary(station):
    """(frequencies, expected, complete) for the station's AF list.

    `expected` is the number the station announced (None if no header
    code has been heard yet); `complete` is True once that many distinct
    frequencies have arrived. Method-B lists count the tuned frequency
    once plus n/2 alternatives.
    """
    a = station.get("af")
    if not a or not a["freqs"]:
        return [], None, False
    freqs = list(a["freqs"])
    if a["method"] == "B" and a["tuned"] in freqs:
        freqs.remove(a["tuned"])
        freqs.insert(0, a["tuned"])
    n = a["count"]
    expected = None
    if n:
        expected = n // 2 + 1 if a["method"] == "B" else n
    complete = expected is not None and len(freqs) >= expected
    return freqs, expected, complete


def _carries_rtp(station, group_type, version):
    """True if this group carries RT+ tags for the station.

    The carrier is whatever the station registered in 3A; 11A is the
    usual choice and 12A is also common, so both are read until a
    registration has been seen.
    """
    registered = station.get("rtp_group_type")
    if registered is not None:
        return (group_type == registered and
                version == station.get("rtp_group_version", 0))
    return version == 0 and group_type in (11, 12)


def rtp_marker_text(rt, marker):
    """Return the RadioText slice an RT+ marker tags, or "" if it has none.

    A marker only means something while the RadioText it points into is
    still there: stations register item types they never fill in, and the
    markers outlive the RT they were sent with.  Both cases give an empty
    or out-of-range slice, which is how unused fields are dropped.
    """
    if not rt or not isinstance(marker, dict):
        return ""
    start = marker.get("start")
    length = marker.get("length")
    if not isinstance(start, int) or start >= len(rt):
        return ""
    if isinstance(length, int):
        # RT+ length is "characters after the first", per ODM-201.
        end = min(start + length + 1, len(rt))
    else:
        end = len(rt)
    text = rt[start:end].strip()
    return ''.join(c for c in text if c.isprintable()).strip()


def rtp_fields(station):
    """Tagged RT+ fields that actually carry text, in display order."""
    rt = station.get("rt", "")
    markers = station.get("rtp_markers", {})
    found = []
    for itype, marker in markers.items():
        if itype == 0x00:  # dummy: used to pad a group with no second tag
            continue
        text = rtp_marker_text(rt, marker)
        if text:
            name = marker.get("type_name") or RTP_ITEM_TYPES.get(
                itype, f"Unknown(0x{itype:02X})")
            found.append((itype, name, text))

    def sort_key(field):
        itype = field[0]
        if itype in RTP_DISPLAY_ORDER:
            return (0, RTP_DISPLAY_ORDER.index(itype))
        return (1, itype)

    found.sort(key=sort_key)
    return found


# US state names → abbreviations (for station location lookup)
US_STATES = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR',
    'California': 'CA', 'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE',
    'Florida': 'FL', 'Georgia': 'GA', 'Hawaii': 'HI', 'Idaho': 'ID',
    'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA', 'Kansas': 'KS',
    'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
    'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS',
    'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV',
    'New Hampshire': 'NH', 'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY',
    'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK',
    'Oregon': 'OR', 'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC',
    'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT',
    'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV',
    'Wisconsin': 'WI', 'Wyoming': 'WY', 'District of Columbia': 'DC',
}


def _city_state_from_extract(extract):
    """'City, ST' from a Wikipedia summary extract, or None."""
    state_pattern = "|".join(re.escape(s) for s in US_STATES.keys())
    patterns = [rf"{prefix}([A-Z][\w.]+(?:\s+[A-Z][\w.]+)*),\s+({state_pattern})"
                for prefix in (r"licensed to\s+", r"in\s+", r"based in\s+",
                               r"serving\s+", r"station in\s+")]
    patterns.append(rf"([A-Z][\w.]+(?:\s+[A-Z][\w.]+)*),\s+({state_pattern})")
    for pat in patterns:
        match = re.search(pat, extract)
        if match:
            city = match.group(1).strip()
            state_name = match.group(2).strip()
            return f"{city}, {US_STATES.get(state_name, state_name)}"
    return None


def _dms_to_deg(deg, minutes, seconds, hemisphere):
    value = float(deg) + float(minutes) / 60.0 + float(seconds) / 3600.0
    return -value if hemisphere.upper() in ("S", "W") else value


FCC_FMQ_URL = "https://transition.fcc.gov/fcc-bin/fmq"


def parse_fcc_fmq(text, callsign):
    """Best licensed record from an FCC FM Query pipe-delimited listing.

    Returns {"location", "lat", "lon"} or None.  Prefers the LIC
    (licensed) record for the exact callsign over construction permits and
    applications, and skips records without coordinates.
    """
    callsign = callsign.upper()
    best = None
    for line in text.splitlines():
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 28 or not fields[1]:
            continue
        call = fields[1].upper()
        if call.split("-")[0] != callsign:
            continue
        status = fields[9]
        try:
            lat = _dms_to_deg(fields[20], fields[21], fields[22], fields[19])
            lon = _dms_to_deg(fields[24], fields[25], fields[26], fields[23])
        except ValueError:
            continue
        city = fields[10].title() if fields[10] else ""
        state = fields[11]
        location = f"{city}, {state}" if city and state else None
        rank = 0 if status == "LIC" else 1
        if best is None or rank < best[0]:
            best = (rank, {"location": location, "lat": lat, "lon": lon})
    return best[1] if best else None


def lookup_station_geo_fcc(callsign):
    """Transmitter coordinates and community of licence from the FCC
    FM Query database (covers every licensed US FM station/translator)."""
    try:
        import requests
        r = requests.get(FCC_FMQ_URL, params={"call": callsign, "list": "4"},
                         timeout=10, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            return None
        return parse_fcc_fmq(r.text, callsign)
    except Exception:
        return None


def lookup_station_geo(callsign):
    """Station city/state and transmitter coordinates.

    Wikipedia first (its city/state wording is nicer); when it has no
    page or no coordinates, the FCC FM Query fills in the transmitter
    coordinates (and city/state if still missing).

    Returns {"location": "City, ST" or None, "lat": float or None,
    "lon": float or None} or None when nothing was found.  Cached by
    callsign for the life of the program.
    """
    if not callsign or len(callsign) < 3:
        return None
    callsign = callsign.upper()
    cache = lookup_station_geo._cache
    if callsign in cache:
        return cache[callsign]
    local = get_stations().get(callsign)
    if local:
        cache[callsign] = dict(local)
        return cache[callsign]

    result = None
    try:
        import requests
        import urllib.parse

        for title in [f"{callsign} (FM)", f"{callsign}-FM",
                      f"{callsign} (radio)", callsign]:
            url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
                   + urllib.parse.quote(title))
            try:
                r = requests.get(url, timeout=8,
                                 headers={"User-Agent": USER_AGENT})
            except Exception:
                continue
            if r.status_code != 200:
                time.sleep(0.3)
                continue
            data = r.json()
            extract = data.get("extract", "")
            coords = data.get("coordinates") or {}
            lat, lon = coords.get("lat"), coords.get("lon")
            location = _city_state_from_extract(extract) if extract else None
            if location or lat is not None:
                result = {"location": location,
                          "lat": float(lat) if lat is not None else None,
                          "lon": float(lon) if lon is not None else None}
            break  # page found
    except Exception:
        pass

    if result is None or result.get("lat") is None:
        fcc = lookup_station_geo_fcc(callsign)
        if fcc:
            if result is None:
                result = fcc
            else:
                result["lat"], result["lon"] = fcc["lat"], fcc["lon"]
                result["location"] = result["location"] or fcc["location"]

    cache[callsign] = result
    return result


lookup_station_geo._cache = {}


def lookup_station_location(callsign):
    """Look up station city and state from Wikipedia. Cached by callsign."""
    geo = lookup_station_geo(callsign)
    return geo["location"] if geo else None


COMPASS_POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass_point(bearing_deg):
    """16-point compass name for a bearing in degrees (0 = north)."""
    idx = int((bearing_deg % 360.0) / 22.5 + 0.5) % 16
    return COMPASS_POINTS[idx]


def distance_bearing(lat1, lon1, lat2, lon2):
    """Great-circle (miles, initial bearing degrees) from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2)
    miles = 3958.7613 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    y = math.sin(dlam) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
         - math.sin(phi1) * math.cos(phi2) * math.cos(dlam))
    bearing = math.degrees(math.atan2(y, x)) % 360.0
    return miles, bearing


def station_range_text(home, geo):
    """'42.3 mi  NE (047°)' from home (lat, lon) to the station, or ''."""
    if not home or not geo or geo.get("lat") is None:
        return ""
    miles, bearing = distance_bearing(home[0], home[1], geo["lat"], geo["lon"])
    return f"{miles:.1f} mi  {compass_point(bearing)} ({bearing:03.0f}\u00b0)"


def mapper_radii():
    """(local, extended local) miles from DX Mapper's Home… settings when it
    sits in the same folder (one-folder suite) or next to it, else 40 / 80 -
    so both programs agree."""
    paths = [APP_DIR / "settings" / "logger_settings.json"] \
        if (APP_DIR / "DX Mapper.py").is_file() else []
    paths += [APP_DIR.parent / n / "settings" / "logger_settings.json"
              for n in ("DX-Mapper", "DX Mapper", "DX_Mapper")]
    for path in paths:
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        local = float(data.get("local_radius_mi") or 40)
        if "extended_radius_mi" not in data and local == 100:
            return 40.0, 80.0  # DX Mapper's old untouched default
        return local, max(local, float(data.get("extended_radius_mi") or 80))
    return 40.0, 80.0


def range_tier(miles, local_mi=40, ext_mi=80):
    """Local / Extended local / DX by distance from home, like DX Mapper."""
    if miles is None:
        return ""
    return ("Local" if miles <= local_mi else
            "Extended local" if miles <= ext_mi else "DX")


def station_tier_text(home, geo, radii=(40, 80)):
    """'Extended local · 66.2 mi  SSE (163°)', or ''."""
    rng = station_range_text(home, geo)
    if not rng:
        return ""
    miles = distance_bearing(home[0], home[1], geo["lat"], geo["lon"])[0]
    return f"{range_tier(miles, *radii)} \u00b7 {rng}"


# ===========================================================================
# DX Mapper link (Log button -> 127.0.0.1:7375, newline-delimited JSON)
# ===========================================================================

DX_MAP_HOST = "127.0.0.1"
DX_MAP_PORT = 7375
AUTO_LOG_MIN_USABLE = 60       # auto-log only well-decoded stations
AUTO_LOG_REPEAT_SECONDS = 300  # then an update every 5 minutes while decoding,
AUTO_LOG_UPDATE_SECONDS = 30   # or sooner when something new decoded (30 s apart)
HD_LOG_STEADY_SECONDS = 5      # HD must hold lock this long to auto-log


def _printable(text):
    return "".join(c if c.isprintable() or c == " " else " "
                   for c in (text or "")).strip()


def build_map_log(pi, data, geo=None, kind="log", rx=None):
    """JSON-ready record of the station as currently decoded."""
    stats = data.get("stats", {})
    total = stats.get("total_blocks", 0)
    bad = stats.get("bad_blocks", 0)
    rec = {
        "type": kind,
        "pi": (pi or "").upper(),
        "callsign": data.get("callsign") or None,
        "freq_mhz": data.get("freq_mhz"),
        "ps": _printable(data.get("ps")) or None,
        "ptyn": _printable(data.get("ptyn")) or None,
        "rt": _printable(data.get("rt")) or None,
        # song = RT carries the song (DX Mapper keeps its pattern), promo =
        # ads / sports / church / contact lines it never keeps.
        "rt_kind": ("song" if data.get("title") or data.get("artist")
                    else nowplaying.rt_kind(data.get("rt"), (
                        data.get("ps"), data.get("callsign"),
                        station_slogan(data.get("callsign")))))
                   if data.get("rt") else None,
        "pty": data.get("pty"),
        "pty_name": PTY_TABLE.get(data.get("pty")) if data.get("pty") is not None else None,
        "signal_dbfs": data.get("signal_dbfs"),
        "signal_peak_dbfs": data.get("signal_peak_dbfs"),
        "signal_units": _SIGNAL_UNITS[0],  # the tuner's View > Signal units
        "rds_level_db": data.get("rds_level_db"),
        "usable_info": usable_info_score(data),
        "block_pct": round((total - bad) / total * 100, 1) if total else None,
        "good_blocks": total - bad,
        "total_blocks": total,
        "stereo": data.get("tuner_stereo"),
        "dongle": data.get("dongle") or None,  # the SDR that heard it
        "group_seq": group_fingerprint(data) or None,
        "ts": time.time(),
    }
    af_freqs, _exp, _done = af_summary(data)
    if af_freqs:
        rec["af"] = [round(f, 1) for f in af_freqs]
    if data.get("title") or data.get("artist"):
        rec["now_playing"] = " - ".join(
            x for x in (data.get("artist"), data.get("title")) if x)
        # DX Mapper keeps the RadioText as a pattern without the song.
        rec["artist"] = _printable(data.get("artist")) or None
        rec["title"] = _printable(data.get("title")) or None
    if geo:
        rec["location"] = geo.get("location") or None
        if geo.get("lat") is not None:
            rec["lat"], rec["lon"] = geo["lat"], geo["lon"]
    hd = data.get("hd")
    if isinstance(hd, dict) and hd.get("synced"):
        # Real HD decoder lock: each HD1-HD8 can become its own Log Book row.
        rec["hd"] = {k: hd.get(k) for k in (
            "call", "slogan", "message", "facility_id", "country", "mer_db",
            "ber", "audio_program", "playing") if hd.get(k) is not None}
        rec["hd"]["programs"] = [
            {k: p.get(k) for k in ("hd", "program", "name", "type", "title",
                                   "artist", "album", "genre")
             if p.get(k) is not None}
            for p in hd.get("programs") or []]
    if rx:
        # Where the station was logged from (Home or Away).
        rec["rx_lat"], rec["rx_lon"], rec["rx_name"] = rx[0], rx[1], rx[2]
    return {k: v for k, v in rec.items() if v is not None}


def send_to_map(record, host=DX_MAP_HOST, port=DX_MAP_PORT, timeout=3.0):
    """Send one record; returns the logger's reply dict or raises OSError."""
    import socket
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(record) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    try:
        return json.loads(buf.decode("utf-8") or "{}")
    except ValueError:
        raise OSError("DX Mapper sent an unreadable reply")


def _load_flag(key, path=None):
    try:
        with open(path or DEFAULT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return bool(json.load(f).get(key, False))
    except (OSError, ValueError, AttributeError):
        return False


def _save_flag(key, enabled, path=None):
    path = path or DEFAULT_SETTINGS_FILE
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        pass
    data[key] = bool(enabled)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_auto_map_log(path=None):
    """Auto-logging to the map stays off unless switched on in Tools."""
    return _load_flag("auto_map_log", path)


def save_auto_map_log(enabled, path=None):
    _save_flag("auto_map_log", enabled, path)


def load_auto_hd_log(path=None):
    """Auto-log HD catches (a sub-option of auto-log), off by default."""
    return _load_flag("auto_hd_log", path)


def save_auto_hd_log(enabled, path=None):
    _save_flag("auto_hd_log", enabled, path)


def map_now_due(pi, data, last, now=None, every=10.0):
    """Send a "now hearing" update to the DX Mapper? On a station change,
    otherwise every `every` seconds so the map knows it is still on."""
    now = time.time() if now is None else now
    key = (pi.upper(), data.get("callsign") or "", data.get("freq_mhz"))
    if last.get("key") != key or now - last.get("ts", 0) >= every:
        last["key"], last["ts"] = key, now
        return True
    return False


def map_tuned_due(freq, last, now=None, every=20.0):
    """Tell the DX Mapper the tuned frequency? On every retune (so it
    switches at once, RDS or not), otherwise every `every` seconds."""
    now = time.time() if now is None else now
    if freq is None:
        return False
    freq = round(float(freq), 1)
    if last.get("freq") != freq or now - last.get("ts", 0) >= every:
        last["freq"], last["ts"] = freq, now
        return True
    return False


GROUP_SEQ_LEN = 24  # newest group types kept for the sequence fingerprint


def group_fingerprint(data):
    """The newest group types in the order they arrived, e.g.
    "0A 0A 2A 0A 0A 2A 3A ..." - each station's encoder repeats its own
    pattern, so it works like a transmitter fingerprint. "" if none yet."""
    return " ".join((data.get("stats") or {}).get("group_seq") or [])


def decode_signature(data):
    """What has been decoded for a station so far, ignoring things that change
    all the time (song text, signal, TMC message churn). A new value means
    the station sent something new: a group type, a late 3A, more AFs or
    EON networks, RT+, TMC, Long PS, CT, PTYN..."""
    af = data.get("af") or {}
    eon = data.get("eon") or {}
    return (
        (data.get("ps") or "").strip(), data.get("pty"),
        (data.get("ptyn") or "").strip(),
        data.get("long_ps") or "", bool(data.get("long_ps_complete")),
        len(af.get("freqs") or []),
        tuple(sorted((k, (n.get("ps") or "").strip()) for k, n in eon.items())),
        bool(data.get("rtp_items")), data.get("rtp_decoded_from"),
        bool((data.get("tmc") or {}).get("messages")),
        tuple(sorted(str(a) for a in data.get("oda_aids") or [])),
        tuple(sorted(data.get("oda_raw") or {})),
        tuple(sorted(str(g) for g in
                     (data.get("stats") or {}).get("group_counts") or {})),
        bool(data.get("date_time")), data.get("di"),
        bool(data.get("tp")), bool(data.get("ta")),
    )


def auto_log_due(pi, data, last_sent, now=None, last_sig=None):
    """True when a station is decoded well enough to auto-log right now.
    While you stay on it, it is due again as soon as something new decodes,
    so the same log entry keeps filling in."""
    if not pi or not data.get("callsign"):
        return False
    if usable_info_score(data) < AUTO_LOG_MIN_USABLE:
        return False
    now = time.time() if now is None else now
    since = now - last_sent.get(pi.upper(), 0)
    if since >= AUTO_LOG_REPEAT_SECONDS:
        return True
    return (last_sig is not None and pi.upper() in last_sig
            and since >= AUTO_LOG_UPDATE_SECONDS
            and decode_signature(data) != last_sig[pi.upper()])


def hd_log_due(pi, data, hd_since, hd_sent, now=None):
    """True when the station's HD has held lock HD_LOG_STEADY_SECONDS and it
    was not logged in the last AUTO_LOG_REPEAT_SECONDS. hd_since (PI -> time
    lock was first seen) is kept up to date here: a lost lock, or another
    station, starts the steady wait over."""
    now = time.time() if now is None else now
    key = (pi or "").upper()
    hd = data.get("hd") if data else None
    for other in [k for k in hd_since if k != key]:
        del hd_since[other]
    if not key or not isinstance(hd, dict) or not hd.get("synced"):
        hd_since.pop(key, None)
        return False
    start = hd_since.setdefault(key, now)
    if not data.get("callsign") or now - start < HD_LOG_STEADY_SECONDS:
        return False
    return now - hd_sent.get(key, 0) >= AUTO_LOG_REPEAT_SECONDS


def load_home_location(path=None):
    """(lat, lon) saved by Tools > Set Home Location, else the built-in default."""
    path = path or DEFAULT_SETTINGS_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data["home_lat"]), float(data["home_lon"])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_HOME_LOCATION


def _read_settings(path=None):
    try:
        with open(path or DEFAULT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _update_settings(changes, path=None):
    data = _read_settings(path)
    data.update(changes)
    with open(path or DEFAULT_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_locations(path=None):
    """Home and Away receive locations (Tools menu).

    {"home": (lat, lon), "home_set": bool, "away": (lat, lon) or None,
     "away_name": str, "active": "home" | "away"}
    """
    data = _read_settings(path)
    home, home_set = DEFAULT_HOME_LOCATION, False
    try:
        home, home_set = (float(data["home_lat"]), float(data["home_lon"])), True
    except (KeyError, TypeError, ValueError):
        pass
    away = None
    try:
        away = float(data["away_lat"]), float(data["away_lon"])
    except (KeyError, TypeError, ValueError):
        pass
    active = "away" if data.get("active_location") == "away" and away else "home"
    return {"home": home, "home_set": home_set, "away": away,
            "away_name": str(data.get("away_name") or "Away"),
            "active": active, "home_prompted": bool(data.get("home_prompted"))}


def save_away_location(lat, lon, name="Away", path=None):
    _update_settings({"away_lat": float(lat), "away_lon": float(lon),
                      "away_name": (name or "Away").strip() or "Away"}, path)


def save_active_location(which, path=None):
    _update_settings({"active_location": "away" if which == "away" else "home"},
                     path)


def active_location(path=None):
    """(lat, lon, label) of the location distances are measured from."""
    loc = load_locations(path)
    if loc["active"] == "away":
        name = loc["away_name"]
        label = name if name.lower().startswith("away") else f"Away: {name}"
        return loc["away"] + (label,)
    return loc["home"] + ("Home",)


def save_home_location(lat, lon, path=None):
    path = path or DEFAULT_SETTINGS_FILE
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        pass
    data["home_lat"], data["home_lon"] = float(lat), float(lon)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ===========================================================================
# RDS Spy .spy File Parsing
# ===========================================================================

def parse_spy_line(line):
    """Parse a single .spy file line. Returns (b1, b2, b3, b4) as hex strings."""
    line = re.sub(r'#S[1-3]\s*', '', line)
    line = re.sub(r'@.*$', '', line)
    line = re.sub(r'\s+', '', line)
    if len(line) < 16:
        return None
    blocks = [line[i:i+4] for i in range(0, 16, 4)]
    for block in blocks:
        if block == '----':
            continue
        if not re.match(r'^[0-9A-Fa-f]{4}$', block):
            return None
    return tuple(blocks)


# ===========================================================================
# PI Code → Callsign (NRSC-4-B)
# ===========================================================================

# Custom PI code overrides file — user-editable
# Format: one per line, PI_CODE CALLSIGN (e.g., A1B2 WXYZ)
# Lines starting with # are comments
# stations.txt: one line per station -
#   PI CALLSIGN | City, ST | lat lon | Slogan
# (the slogan part is optional; DX Mapper shares this file and fills it)
# DX Mapper's log comes first (edit stations there); this file only fills
# PIs the log doesn't have yet. Loaded for city/distance too; the program
# appends stations it looks up but never rewrites an existing line, so
# hand edits are kept. Old pi_codes.txt files (PI CALLSIGN) are read as-is.
DEFAULT_PI_OVERRIDES_FILE = _settings_file(
    "stations.txt", SETTINGS_DIR / "pi_codes.txt",
    seed=APP_DIR / "data" / "pi_codes.txt")
DEFAULT_STATIONS_FILE = DEFAULT_PI_OVERRIDES_FILE


def load_pi_overrides(file_path=DEFAULT_PI_OVERRIDES_FILE):
    """Load user-defined PI code to callsign mappings from a text file."""
    overrides = {}
    errors = []
    try:
        # Use utf-8-sig to handle BOM from Windows Notepad
        with open(file_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split("|")[0].split()
                if len(parts) < 2:
                    errors.append(f"Line {line_num}: not enough fields")
                    continue
                pi = parts[0].upper().replace('0x', '').replace('0X', '')
                callsign = parts[1].upper()
                if len(pi) != 4:
                    errors.append(f"Line {line_num}: PI code '{pi}' is not 4 chars")
                    continue
                if len(callsign) < 3:
                    errors.append(f"Line {line_num}: callsign '{callsign}' too short")
                    continue
                overrides[pi] = callsign
    except FileNotFoundError:
        # File doesn't exist yet — that's OK, return empty dict
        pass
    except Exception as e:
        pass
    return overrides


def mapper_log_files():
    """DX Mapper's log databases (its first log, then settings\\Logs\\*.db),
    in the one-folder suite or a DX-Mapper folder next to this one."""
    dirs = [SETTINGS_DIR] if (APP_DIR / "DX Mapper.py").is_file() else []
    dirs += [APP_DIR.parent / n / "settings"
             for n in ("DX-Mapper", "DX Mapper", "DX_Mapper")]
    for d in dirs:
        first = d / "dx_log.db"
        if first.is_file():
            logs = d / "Logs"
            more = sorted(logs.glob("*.db"), key=lambda q: q.name.lower()) \
                if logs.is_dir() else []
            return [first] + more
    return []


_MAPPER_LOG = {"stamp": None, "data": ({}, {})}


def mapper_current_log():
    """The log DX Mapper writes new catches into (the first one ticked)."""
    files = mapper_log_files()
    if not files:
        return None
    first = files[0]
    try:
        cfg = json.loads((first.parent / "logger_settings.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    shown = cfg.get("logs_shown") or []
    main = cfg.get("main_log_name") or "Main"
    if shown and isinstance(shown[0], str) and shown[0] != main:
        other = first.parent / "Logs" / f"{shown[0]}.db"
        if other.is_file():
            return other
    return first


_MAPPER_COUNT = {"stamp": None, "count": None}


def mapper_station_count():
    """Unique PIs in DX Mapper's current log, or None when there's no log.
    Re-read only when the log or DX Mapper's settings change."""
    path = mapper_current_log()
    if path is None:
        return None
    stamp = _file_stamps([path, mapper_log_files()[0].parent
                          / "logger_settings.json"])
    if stamp == _MAPPER_COUNT["stamp"]:
        return _MAPPER_COUNT["count"]
    import sqlite3
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                              uri=True, timeout=2)
        try:
            rows = con.execute("SELECT pi FROM stations").fetchall()
        finally:
            con.close()
    except (sqlite3.Error, ValueError):
        return _MAPPER_COUNT["count"]
    pis = {(pi or "").strip().upper() for (pi,) in rows}
    count = sum(1 for pi in pis if re.fullmatch(r"[0-9A-F]{4}", pi))
    _MAPPER_COUNT.update(stamp=stamp, count=count)
    return count


def _file_stamps(paths):
    out = []
    for q in paths:
        try:
            st = os.stat(q)
            out.append((str(q), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(q), None, None))
    return tuple(out)


def mapper_log():
    """({PI: callsign}, {callsign: geo}) from DX Mapper's logs, read only.
    The log is where stations get fixed, so it wins over stations.txt.
    Re-read only when a log file changes."""
    files = mapper_log_files()
    stamp = _file_stamps(files)
    if stamp == _MAPPER_LOG["stamp"]:
        return _MAPPER_LOG["data"]
    import sqlite3
    by_pi, by_call = {}, {}
    for path in files:
        try:
            con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro",
                                  uri=True, timeout=2)
        except (sqlite3.Error, ValueError):
            continue
        try:
            rows = []
            for cols in ("fcc, wtfda", "fcc, NULL", "NULL, NULL"):  # older logs
                try:
                    rows = con.execute(
                        "SELECT pi, callsign, location, city, state, lat, lon,"
                        f" extra, {cols} FROM stations WHERE callsign IS NOT"
                        " NULL AND callsign != '' ORDER BY last_heard DESC"
                    ).fetchall()
                    break
                except sqlite3.Error:
                    continue
        finally:
            con.close()
        for pi, call, location, city, state, lat, lon, extra, fcc, wtfda in rows:
            call = call.strip().upper()
            if len(call) < 3 or " " in call:
                continue
            pi = (pi or "").strip().upper()
            try:
                extra = json.loads(extra or "{}")
            except ValueError:
                extra = {}
            try:
                fcc = json.loads(fcc or "{}") or {}
            except ValueError:
                fcc = {}
            # A translator sends its parent's PI: the PI stays the parent's.
            try:
                wtfda = json.loads(wtfda or "{}") or {}
            except ValueError:
                wtfda = {}
            relay = (isinstance(fcc, dict) and fcc.get("relays")) or any(
                str(k).strip().lower() == "fed by" for k in (extra or {})) or (
                isinstance(wtfda, dict) and wtfda.get("relay_of")
                and not str(wtfda["relay_of"]).upper().startswith(call))
            hd = re.search(r"-HD[2-8]$", call)
            if not hd and not relay and re.fullmatch(r"[0-9A-F]{4}", pi):
                by_pi.setdefault(pi, call)
            slogan = next((str(v).strip() for k, v in (extra or {}).items()
                           if re.search(r"slogan|branding", k, re.I)
                           and isinstance(v, (str, int, float))
                           and str(v).strip()), None)
            place = ", ".join(x for x in (city, state) if x) if city \
                else (location or None)
            geo = {"location": place or None, "lat": lat, "lon": lon,
                   "slogan": slogan}
            have = by_call.setdefault(call, geo)
            for k, v in geo.items():
                if have.get(k) in (None, "") and v not in (None, ""):
                    have[k] = v
    _MAPPER_LOG.update(stamp=stamp, data=(by_pi, by_call))
    return by_pi, by_call


def parse_station_line(line):
    """'PI CALL | City, ST | lat lon | Slogan' -> (pi, callsign, geo) or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    fields = [f.strip() for f in line.split("|")]
    head = fields[0].split()
    if len(head) < 2:
        return None
    pi, callsign = head[0].upper(), head[1].upper()
    location = fields[1] if len(fields) > 1 and fields[1] else None
    lat = lon = None
    if len(fields) > 2:
        nums = fields[2].replace(",", " ").split()
        try:
            lat, lon = float(nums[0]), float(nums[1])
        except (IndexError, ValueError):
            lat = lon = None
    slogan = fields[3] if len(fields) > 3 and fields[3] else None
    return pi, callsign, {"location": location, "lat": lat, "lon": lon,
                          "slogan": slogan}


def load_stations(file_path=None):
    """{callsign: geo} from stations.txt (only lines with a city or coords)."""
    file_path = file_path or DEFAULT_STATIONS_FILE
    stations = {}
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                parsed = parse_station_line(line)
                if parsed and (parsed[2]["location"] or parsed[2]["slogan"]
                               or parsed[2]["lat"] is not None):
                    stations[parsed[1]] = parsed[2]
    except OSError:
        pass
    return stations


_STATIONS = None


def get_stations():
    """{callsign: geo}: DX Mapper's logs first, stations.txt fills the rest."""
    global _STATIONS
    if _STATIONS is None:
        stations = load_stations()
        for call, geo in mapper_log()[1].items():
            have = stations.setdefault(call, {})
            for k, v in geo.items():
                if v not in (None, ""):
                    have[k] = v
        _STATIONS = {c: g for c, g in stations.items()
                     if g.get("location") or g.get("slogan")
                     or g.get("lat") is not None}
    return _STATIONS


def station_slogan(callsign):
    """The station's slogan/branding from stations.txt, or None."""
    return (get_stations().get((callsign or "").upper()) or {}).get("slogan")


def station_in_file(callsign, file_path=None):
    file_path = file_path or DEFAULT_STATIONS_FILE
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                parsed = parse_station_line(line)
                if parsed and parsed[1] == callsign.upper():
                    return True
    except OSError:
        pass
    return False


def remember_station(pi, callsign, geo, file_path=None):
    """Append a looked-up station to stations.txt unless it is already there.

    Existing lines are never touched, so anything the user edited wins.
    """
    file_path = file_path or DEFAULT_STATIONS_FILE
    if not callsign or station_in_file(callsign, file_path):
        return False
    geo = geo or {}
    coords = ""
    if geo.get("lat") is not None and geo.get("lon") is not None:
        coords = f"{geo['lat']:.5f} {geo['lon']:.5f}"
    line = f"{(pi or '----').upper():<4} {callsign.upper():<6} | " \
           f"{geo.get('location') or ''} | {coords}\n"
    try:
        new_file = not os.path.exists(file_path)
        with open(file_path, "a", encoding="utf-8") as f:
            if new_file:
                f.write("# PI CALLSIGN | City, ST | lat lon\n")
                f.write("# Edit freely - the program only appends new stations.\n")
            f.write(line)
    except OSError:
        return False
    if _STATIONS is not None and (geo.get("location") or geo.get("lat") is not None):
        _STATIONS[callsign.upper()] = {"location": geo.get("location"),
                                       "lat": geo.get("lat"), "lon": geo.get("lon")}
    return True


def set_station_callsign(pi, callsign, geo=None, file_path=None):
    """User override: rewrite (or add) the stations.txt line for `pi`.

    This is the one place an existing line is changed, because the user
    asked for it. City/coords come from `geo` when given, otherwise the
    old line's are kept.
    """
    file_path = file_path or DEFAULT_STATIONS_FILE
    pi, callsign = pi.upper(), callsign.upper()
    lines = []
    try:
        with open(file_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = ["# PI CALLSIGN | City, ST | lat lon",
                 "# Edit freely - the program only appends new stations."]
    old_geo = {}
    kept = []
    for line in lines:
        parsed = parse_station_line(line)
        if parsed and parsed[0] == pi:
            old_geo = parsed[2]
            continue
        kept.append(line)
    geo = geo or old_geo
    coords = ""
    if geo.get("lat") is not None and geo.get("lon") is not None:
        coords = f"{geo['lat']:.5f} {geo['lon']:.5f}"
    slogan = geo.get("slogan") or old_geo.get("slogan")
    kept.append(f"{pi:<4} {callsign:<6} | {geo.get('location') or ''} | {coords}"
                + (f" | {slogan}" if slogan else ""))
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
    except OSError:
        return False
    global _STATIONS, _PI_OVERRIDES
    _STATIONS = None
    _PI_OVERRIDES = None
    return True


# Global cache of PI overrides
_PI_OVERRIDES = None


def get_pi_overrides():
    """Get PI overrides, loading from file on first access.
    Also re-loads if cache is empty (file may have been created later)."""
    global _PI_OVERRIDES
    if _PI_OVERRIDES is None or len(_PI_OVERRIDES) == 0:
        _PI_OVERRIDES = {**load_pi_overrides(), **mapper_log()[0]}
    return _PI_OVERRIDES


def load_station_count(file_path=DEFAULT_STATION_COUNT_FILE):
    """Load the set of unique PI codes seen over the program's lifetime."""
    pis = set()
    try:
        with open(file_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                # Each line is just a PI code (4 hex chars)
                pi = line.upper().replace('0x', '').replace('0X', '')
                if len(pi) == 4:
                    pis.add(pi)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return pis


def save_station_count(pis, file_path=DEFAULT_STATION_COUNT_FILE):
    """Save the set of unique PI codes to the station count file."""
    try:
        import time
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(f"# Unique stations identified (lifetime count)\n")
            f.write(f"# Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# Total unique stations: {len(pis)}\n")
            f.write(f"# Reset by deleting this file or using Tools > Reset Station Counter\n")
            for pi in sorted(pis):
                f.write(f"{pi}\n")
    except Exception:
        pass


_STATIONS_FILE_SEEN = {"checked": None, "stamp": None}


def stations_file_changed(file_path=None, min_interval=1.0, now=None):
    """True once each time stations.txt or one of DX Mapper's logs changes
    on disk (a call fixed in DX Mapper), after dropping the cached copies.
    Looks at the file at most once per min_interval seconds."""
    global _STATIONS, _PI_OVERRIDES
    now = time.monotonic() if now is None else now
    seen = _STATIONS_FILE_SEEN
    if seen["checked"] is not None and now - seen["checked"] < min_interval:
        return False
    first = seen["checked"] is None
    seen["checked"] = now
    try:
        st = os.stat(file_path or DEFAULT_STATIONS_FILE)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    if file_path is None:  # DX Mapper's logs count too (an edit there)
        stamp = (stamp, _file_stamps(mapper_log_files()))
    changed = not first and stamp != seen["stamp"]
    seen["stamp"] = stamp
    if changed:
        _STATIONS = None
        _PI_OVERRIDES = None
    return changed


def pi_to_callsign(pi_code):
    """Convert RDS PI code to callsign. Checks user overrides first, then NRSC-4-B."""
    try:
        pi_str = pi_code.upper().replace('0x', '').replace('0X', '')
    except (ValueError, TypeError):
        return None

    # Check user overrides FIRST (before hex parsing, since some
    # PI codes may not be valid hex but are still in the override file)
    overrides = get_pi_overrides()
    if pi_str in overrides:
        return overrides[pi_str]

    # NRSC-4-B algorithm for standard US callsigns
    try:
        pi = int(pi_str, 16)
    except (ValueError, TypeError):
        return None
    K_BASE, W_BASE = 0x1000, 0x54A8
    if K_BASE <= pi < W_BASE:
        decimal = pi - K_BASE
        prefix = "K"
    elif W_BASE <= pi <= 0x994F:
        decimal = pi - W_BASE
        prefix = "W"
    else:
        return None
    l3 = decimal // 676
    rem = decimal % 676
    l2 = rem // 26
    l1 = rem % 26
    if l3 > 25 or l2 > 25 or l1 > 25:
        return None
    return prefix + chr(65+l3) + chr(65+l2) + chr(65+l1)


# ===========================================================================
# RDS Group Decoder
# ===========================================================================

def assemble_rt(segments):
    """RadioText from {position: char}. A carriage return (0x0D) ends the
    text: anything after it is left over from an older, longer RT and is
    dropped, so a shorter new RT doesn't get the old tail stuck on."""
    end = min((p for p, ch in segments.items() if ch == "\r"), default=None)
    if end is not None:
        for p in [p for p in segments if p > end]:
            del segments[p]
    size = end if end is not None else max(segments) + 1
    chars = [" "] * size
    for pos, ch in segments.items():
        if pos < size:
            chars[pos] = ch
    return "".join(chars).rstrip()


def decode_rds_groups(file_path, existing_stations=None, start_byte=0, incremental=False):
    """
    Decode all RDS data from a .spy file.
    Returns dict keyed by PI code.
    If incremental=True, does NOT reset stats for existing stations (accumulates).
    """
    stations = existing_stations if existing_stations is not None else {}
    line_num = 0
    # Reset per-read stats for existing stations (only on full read, not incremental)
    if not incremental:
        for pi, st in stations.items():
            st["last_seen"] = 0
            st["stats"] = {
                "total_groups": 0,
                "total_blocks": 0,
                "bad_blocks": 0,
                "group_counts": {},
                "ps_total": 4,
                "rt_total": 16,
                "ptyn_total": 2,
            }

    def get_station(pi):
        if pi not in stations:
            callsign = pi_to_callsign(pi)
            stations[pi] = {
                "callsign": callsign,
                "ps_segments": {},
                "ps": "",
                "pty": None,
                "rt_segments": {},
                "rt": "",
                "rt_ab": None,
                "rtp_items": [],
                "rtp_markers": {},  # item_type -> {start, length, toggle, running}
                "rtp_group_type": None,  # group type carrying RT+ data (from type 3A ODA registration)
                "rtp_group_version": 0,  # version (A/B) of that group type
                "rtp_active": False,  # RT+ registered via type 3A
                "oda_aids": [],  # list of (aid, app_group_type) from type 3A
                "di": None,  # Decoder Identification bits (3 bits)
                "tp": False,  # Traffic Program flag
                "ta": False,  # Traffic Announcement flag
                "music_speech": None,  # True=Music, False=Speech, None=unknown
                "date_time": None,  # {date_str, time_str, offset_str}
                "tmc": None,  # {sys: {...}, messages: {key: msg}, order: [key], groups: n}
                "ptyn_segments": {},  # type 10A: segment (0-1) -> 4 chars
                "ptyn_ab": None,
                "ptyn": "",  # Program Type Name (8 chars)
                "lps_segments": {},  # type 15A: segment (0-7) -> 4 bytes
                "long_ps": "",  # Long PS (up to 32 bytes UTF-8)
                "long_ps_complete": False,
                "af": None,  # Alternative Frequencies, see af_add_block
                "eon": None,  # other networks from 14A/14B, see eon_add_group
                "last_seen": 0,  # line number (higher = more recent)
                "fresh": {},  # field -> stats.total_groups at last update
                "oda_raw": {},  # "5A" -> {"count": n, "samples": ["b2 b3 b4", ...]}
                "rtp_decoded_from": None,  # group type that actually yielded RT+ tags
                # Confidence / statistics
                "stats": {
                    "total_groups": 0,
                    "total_blocks": 0,
                    "bad_blocks": 0,
                    "group_counts": {},  # group_type -> count
                    "ps_total": 4,  # expected segments
                    "rt_total": 16,  # expected segments (2A)
                    "ptyn_total": 2,  # expected segments
                },
            }
        return stations[pi]

    try:
        byte_pos = start_byte
        with open(file_path, 'r', errors='ignore') as f:
            if start_byte > 0:
                f.seek(start_byte)
                # Discard first partial line (we started mid-line)
                discarded = f.readline()
                byte_pos += len(discarded.encode('utf-8', errors='ignore'))
            for line in f:
                line_num += 1
                byte_pos += len(line.encode('utf-8', errors='ignore'))
                blocks = parse_spy_line(line)
                if not blocks:
                    continue
                b1, b2, b3, b4 = blocks
                if b1 == '----':
                    continue

                pi = b1.upper()
                st = get_station(pi)
                # Use byte offset as last_seen for monotonic ordering across incremental reads
                if incremental:
                    st["last_seen"] = byte_pos
                else:
                    st["last_seen"] = line_num  # track line position, not wall clock

                # --- Confidence: track block stats ---
                stats = st["stats"]
                stats["total_groups"] += 1
                for blk in (b2, b3, b4):
                    stats["total_blocks"] += 1
                    if blk == '----':
                        stats["bad_blocks"] += 1

                # Skip if block 2 is bad — can't determine group type
                if b2 == '----':
                    continue

                b2_val = int(b2, 16)
                group_type = (b2_val >> 12) & 0xF
                version = (b2_val >> 11) & 0x1
                pty = (b2_val >> 5) & 0x1F
                st["pty"] = pty
                st["fresh"]["pty"] = stats["total_groups"]

                # Track group type counts
                gt_key = f"{group_type}{'A' if version == 0 else 'B'}"
                stats["group_counts"][gt_key] = stats["group_counts"].get(gt_key, 0) + 1
                seq = stats.setdefault("group_seq", [])
                seq.append(gt_key)
                del seq[:-GROUP_SEQ_LEN]

                # TP (Traffic Program) flag — bit 10 of block 2 in all groups
                tp_flag = (b2_val >> 10) & 0x1
                st["tp"] = bool(tp_flag)

                # --- Type 0A: PS name + TA + Music/Speech + DI ---
                # Per RDS standard and confirmed by Si4701 example data:
                # Block 3 = AF (Alternative Frequencies) data
                # Block 4 = PS characters (2 × 8 bits)
                # Block 2 bits 1-0 = PS segment address
                if group_type == 0:
                    # TA (Traffic Announcement) flag — bit 4
                    ta_flag = (b2_val >> 4) & 0x1
                    st["ta"] = bool(ta_flag)
                    # Music/Speech flag — bit 3 (0=Speech, 1=Music)
                    ms_flag = (b2_val >> 3) & 0x1
                    st["music_speech"] = bool(ms_flag)  # True=Music, False=Speech
                    di_update(st, b2_val)
                if group_type == 0 and version == 0:
                    if b3 != '----':
                        af_add_block(st, int(b3, 16))
                        st["fresh"]["af"] = stats["total_groups"]
                    if b4 != '----':
                        b4_val = int(b4, 16)
                        # Skip zero-value block 4 (no PS data)
                        # Some stations send type 0A groups with block 4 = 0x0000
                        # which would overwrite good PS segments with null chars
                        if b4_val != 0:
                            ps_seg = b2_val & 0x3  # segment address in block 2 bits 1-0
                            c1 = chr((b4_val >> 8) & 0xFF)  # PS characters in block 4
                            c2 = chr(b4_val & 0xFF)
                            st["ps_segments"][ps_seg] = (c1, c2)
                            st["fresh"]["ps"] = stats["total_groups"]
                            # Reassemble PS
                            chars = [' '] * 8
                            for seg, (ch1, ch2) in st["ps_segments"].items():
                                pos = seg * 2
                                if pos < 8:
                                    chars[pos] = ch1
                                if pos + 1 < 8:
                                    chars[pos+1] = ch2
                            st["ps"] = ''.join(chars).rstrip()

                # --- Type 2A: RadioText (64 chars) ---
                elif group_type == 2 and version == 0:
                    ab_flag = (b2_val >> 4) & 0x1
                    rt_seg = b2_val & 0xF  # 0-15
                    if ab_flag != st["rt_ab"]:
                        # AB flag changed — clear RT and start fresh
                        st["rt_ab"] = ab_flag
                        st["rt_segments"] = {}
                    if b3 != '----':
                        b3_val = int(b3, 16)
                        c1 = chr((b3_val >> 8) & 0xFF)
                        c2 = chr(b3_val & 0xFF)
                        st["rt_segments"][rt_seg * 4] = c1
                        st["rt_segments"][rt_seg * 4 + 1] = c2
                    if b4 != '----':
                        b4_val = int(b4, 16)
                        c3 = chr((b4_val >> 8) & 0xFF)
                        c4 = chr(b4_val & 0xFF)
                        st["rt_segments"][rt_seg * 4 + 2] = c3
                        st["rt_segments"][rt_seg * 4 + 3] = c4
                        st["fresh"]["rt"] = stats["total_groups"]
                    # Reassemble RT
                    if st["rt_segments"]:
                        st["rt"] = assemble_rt(st["rt_segments"])

                # --- Type 2B: RadioText (32 chars) ---
                elif group_type == 2 and version == 1:
                    ab_flag = (b2_val >> 4) & 0x1
                    rt_seg = b2_val & 0x7  # 0-7
                    if ab_flag != st["rt_ab"]:
                        st["rt_ab"] = ab_flag
                        st["rt_segments"] = {}
                    if b4 != '----':
                        b4_val = int(b4, 16)
                        c1 = chr((b4_val >> 8) & 0xFF)
                        c2 = chr(b4_val & 0xFF)
                        st["rt_segments"][rt_seg * 2] = c1
                        st["rt_segments"][rt_seg * 2 + 1] = c2
                        st["fresh"]["rt"] = stats["total_groups"]
                    if st["rt_segments"]:
                        st["rt"] = assemble_rt(st["rt_segments"])

                # --- Type 3A: ODA registration (including RT+, TMC) ---
                # Block 2 low 5 bits name the group type carrying the
                # application, block 3 is the application message and
                # block 4 the 16-bit AID (RT+ 0x4BD7, TMC 0xCD46).
                elif group_type == 3 and version == 0:
                    if b3 != '----' and b4 != '----':
                        b3_val = int(b3, 16)
                        app_id = int(b4, 16)
                        app_type = (b2_val >> 1) & 0xF
                        app_version = b2_val & 0x1
                        aid_entry = (app_id, app_type, app_version)
                        if aid_entry not in st["oda_aids"]:
                            st["oda_aids"].append(aid_entry)
                        if app_id in RTP_AIDS:
                            st["rtp_active"] = True
                            st["rtp_group_type"] = app_type
                            st["rtp_group_version"] = app_version
                        if app_id == tmc.TMC_AID:
                            t = _tmc_state(st)
                            t["group_type"] = app_type
                            t["sys"] = tmc.decode_system_info(b3_val)

                # --- Type 8A: TMC (ALERT-C traffic messages) ---
                elif group_type == 8 and version == 0:
                    if b3 != '----' and b4 != '----':
                        _tmc_add_group(st, b2_val, int(b3, 16), int(b4, 16))
                        st["fresh"]["tmc"] = stats["total_groups"]

                # --- Type 4A: Clock Time and Date ---
                # The 17-bit Modified Julian Date and the UTC time are split
                # across all three remaining blocks:
                #   block 2 bits 1-0   MJD bits 16-15
                #   block 3 bits 15-1  MJD bits 14-0
                #   block 3 bit 0      hour bit 4
                #   block 4 bits 15-12 hour bits 3-0
                #   block 4 bits 11-6  minute
                #   block 4 bit 5      sign of the local time offset
                #   block 4 bits 4-0   local time offset in half hours
                elif group_type == 4 and version == 0:
                    if b3 != '----' and b4 != '----':
                        b3_val = int(b3, 16)
                        b4_val = int(b4, 16)
                        mjd = ((b2_val & 0x3) << 15) | (b3_val >> 1)
                        hour = ((b3_val & 0x1) << 4) | (b4_val >> 12)
                        minute = (b4_val >> 6) & 0x3F
                        half_hours = b4_val & 0x1F
                        if (b4_val >> 5) & 0x1:
                            half_hours = -half_hours

                        # MJD 15079 = 1 Jan 1970; anything before that or
                        # an out-of-range time is a corrupt group.
                        if (15079 <= mjd <= 88000 and hour < 24
                                and minute < 60):
                            utc = (datetime.datetime(1858, 11, 17) +
                                   datetime.timedelta(days=mjd, hours=hour,
                                                      minutes=minute))
                            local = utc + datetime.timedelta(
                                minutes=30 * half_hours)
                            sign = '+' if half_hours >= 0 else '-'
                            mag = abs(half_hours)
                            st["date_time"] = {
                                "date_str": local.strftime("%a %b %d, %Y"),
                                "time_str": local.strftime("%H:%M"),
                                "offset_str":
                                    f"UTC{sign}{mag // 2}:{mag % 2 * 30:02d}",
                                "utc_str": utc.strftime("%H:%M UTC"),
                            }
                            st["fresh"]["ct"] = stats["total_groups"]

                # --- Type 10A: PTYN (Program Type Name) ---
                # Block 2 bit 0 = segment address (0 or 1), bit 4 = A/B flag.
                # Blocks 3 and 4 each carry two characters, so one group
                # delivers four of the eight PTYN characters.
                elif group_type == 10 and version == 0:
                    if b3 != '----' and b4 != '----':
                        b3_val = int(b3, 16)
                        b4_val = int(b4, 16)
                        ptyn_seg = b2_val & 0x1
                        ab_flag = (b2_val >> 4) & 0x1
                        if st.get("ptyn_ab") not in (None, ab_flag):
                            st["ptyn_segments"] = {}   # text changed
                        st["ptyn_ab"] = ab_flag
                        seg_chars = (chr((b3_val >> 8) & 0xFF), chr(b3_val & 0xFF),
                                     chr((b4_val >> 8) & 0xFF), chr(b4_val & 0xFF))
                        st["ptyn_segments"][ptyn_seg] = seg_chars
                        st["fresh"]["ptyn"] = stats["total_groups"]
                        chars = [' '] * 8
                        for seg, seg_text in st["ptyn_segments"].items():
                            for i, ch in enumerate(seg_text):
                                pos = seg * 4 + i
                                if pos < 8:
                                    chars[pos] = ch
                        st["ptyn"] = ''.join(chars).rstrip()

                # --- Type 15A: Long PS (RDS2, up to 32 bytes of UTF-8) ---
                # Block 2 bits 2-0 = segment address (0-7); blocks 3 and 4
                # carry four bytes. 0x0D ends a name shorter than 32 bytes.
                elif group_type == 15 and version == 0:
                    if b3 != '----' and b4 != '----':
                        b3_val = int(b3, 16)
                        b4_val = int(b4, 16)
                        lps_add_group(st, b2_val, b3_val, b4_val)
                        if st["long_ps"]:
                            st["fresh"]["long_ps"] = stats["total_groups"]

                elif group_type == 14:
                    b3_val = int(b3, 16) if b3 != '----' else None
                    b4_val = int(b4, 16) if b4 != '----' else None
                    eon_add_group(st, version, b2_val, b3_val, b4_val)
                    st["fresh"]["eon"] = stats["total_groups"]

                # --- RT+ (RadioText Plus) item data ---
                # Carried in whichever group type the station registered in
                # 3A; 11A is the usual choice and 12A is also common, so
                # both are accepted before a registration has been seen.
                # One group carries two tags packed across the last five
                # bits of block 2 and all of blocks 3 and 4:
                #   toggle(1) running(1) type1(6) start1(6) len1(6)
                #   type2(6) start2(6) len2(5)
                elif _carries_rtp(st, group_type, version):
                    if b3 != '----' and b4 != '----':
                        b3_val = int(b3, 16)
                        b4_val = int(b4, 16)
                        toggle = (b2_val >> 4) & 0x1
                        running = (b2_val >> 3) & 0x1

                        item_type_1 = ((b2_val & 0x7) << 3) | (b3_val >> 13)
                        start_1 = (b3_val >> 7) & 0x3F
                        length_1 = (b3_val >> 1) & 0x3F
                        item_type_2 = ((b3_val & 0x1) << 5) | (b4_val >> 11)
                        start_2 = (b4_val >> 5) & 0x3F
                        length_2 = b4_val & 0x1F

                        for itype, start, length in (
                                (item_type_1, start_1, length_1),
                                (item_type_2, start_2, length_2)):
                            if itype == 0:  # dummy class: no tag in this slot
                                continue
                            type_name = RTP_ITEM_TYPES.get(
                                itype, f"Unknown({itype})")
                            st["rtp_markers"][itype] = {
                                "start": start,
                                "length": length,
                                "running": running,
                                "toggle": toggle,
                                "type_name": type_name,
                            }
                            already = any(i["type"] == itype
                                          for i in st["rtp_items"])
                            if not already:
                                st["rtp_items"].append({
                                    "type": itype,
                                    "type_name": type_name,
                                    "toggle": toggle,
                                    "running": running,
                                })
                            st["rtp_decoded_from"] = f"{group_type}{'A' if version == 0 else 'B'}"
                            st["fresh"]["rtp"] = stats["total_groups"]

                # --- Anything else: ODA-capable group with no payload decoder.
                # Keep count and a few raw samples as DX evidence.
                else:
                    gt_name = f"{group_type}{'A' if version == 0 else 'B'}"
                    if group_class(gt_name) == "oda":
                        raw = st["oda_raw"].setdefault(gt_name, {"count": 0, "samples": []})
                        raw["count"] += 1
                        if len(raw["samples"]) < 3 and b3 != '----' and b4 != '----':
                            raw["samples"].append(f"{b2} {b3} {b4}")

    except Exception as e:
        print(f"Error reading {file_path}: {e}")

    # After decoding, extract title/artist text from RT using RT+ markers
    for pi, st in stations.items():
        rt = st.get("rt", "")
        markers = st.get("rtp_markers", {})
        st["title"] = ""
        st["artist"] = ""

        # Item type 0x01 = Title, 0x02 = Artist; 0x13/0x15 are the "Song"
        # variants some stations send instead.
        for t in RTP_TITLE_TYPES:
            text = rtp_marker_text(rt, markers.get(t))
            if text:
                st["title"] = text
                break

        for t in RTP_ARTIST_TYPES:
            text = rtp_marker_text(rt, markers.get(t))
            if text:
                st["artist"] = text
                break

        st["rtp_fields"] = rtp_fields(st)

        st["now_playing_source"] = "RT+" if (st["title"] or st["artist"]) else ""
        # Fallback: parse RT text for "Artist - Title" / "Title by Artist"
        if not st["title"] and not st["artist"] and rt:
            artist, title = nowplaying.parse_now_playing(
                rt, (st.get("ps"), st.get("callsign"),
                     station_slogan(st.get("callsign"))))
            if artist or title:
                st["artist"] = artist
                st["title"] = title
                st["now_playing_source"] = "RT"

    return stations


# ===========================================================================
# Logo Cache
# ===========================================================================

def find_existing_logo(callsign, logo_dir):
    """Find an existing logo file in the cache directory."""
    logo_dir = Path(logo_dir)
    callsign = callsign.upper()
    for ext in LOGO_EXTENSIONS:
        for name in [f"{callsign}{ext}", f"{callsign}{ext.upper()}"]:
            path = logo_dir / name
            if path.exists() and path.stat().st_size > 0:
                return path
    if logo_dir.exists():
        for f in logo_dir.iterdir():
            if f.stem.upper() == callsign and f.suffix.lower() in LOGO_EXTENSIONS:
                if f.stat().st_size > 0:
                    return f
    return None


# ===========================================================================
# Logo Download (from station_logo_lookup.py)
# ===========================================================================

import requests

USER_AGENT = ("RDSMonitor/1.0 "
              "(https://github.com/fm-dx; contact: j.rds.monitor@gmail.com)")
TIMEOUT = 15


def _api_get(url, params=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": USER_AGENT})
            if r.status_code == 429:
                time.sleep(min(2 ** (attempt + 1), 10))
                continue
            return r
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(1)
            continue
    return None


def download_logo_for(callsign, logo_dir):
    """Look up and download a logo for a station. Saves as JPG. Returns Path or None."""
    import urllib.parse
    import tempfile

    WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
    WIKIPEDIA_REST = "https://en.wikipedia.org/api/rest_v1"

    # Always save as .png
    local_png = Path(logo_dir) / f"{callsign}.png"

    # Tier 0: the infobox's own "logo" picture (its "image" is a photo)
    avoid = set()
    try:
        logo_url, avoid = _wiki_logo_fields(callsign)
        if logo_url and _download_and_convert_to_png(logo_url, local_png):
            return local_png
    except Exception:
        pass

    # Tier 1: Wikipedia pageimages
    time.sleep(0.5)
    params = {
        "action": "query", "titles": callsign, "prop": "pageimages",
        "pithumbsize": 500, "pilicense": "any", "format": "json",
    }
    try:
        r = _api_get(WIKIPEDIA_API, params=params)
        if r:
            data = r.json()
            pages = data.get("query", {}).get("pages", {})
            for pid, page in pages.items():
                if pid == "-1":
                    break
                thumb = page.get("thumbnail", {})
                if thumb and thumb.get("source"):
                    url = thumb["source"].split("?")[0]
                    if not _is_photo(url, avoid) and \
                            _download_and_convert_to_png(url, local_png):
                        return local_png
    except Exception:
        pass

    # Tier 2: Wikipedia HTML infobox
    time.sleep(0.5)
    try:
        page_url = f"{WIKIPEDIA_REST}/page/html/{urllib.parse.quote(callsign)}"
        r = _api_get(page_url)
        if r and r.status_code != 404:
            imgs = re.findall(r'<img[^>]+src="([^"]+)"', r.text)
            for src in imgs[:10]:
                if src.startswith("//"):
                    src = "https:" + src
                if any(s in src.lower() for s in ["icon", "commons-logo", "ambox",
                        "portal", "stub", "wikimedia", "search"]):
                    continue
                if src.endswith(".svg") or _is_photo(src, avoid):
                    continue
                if _download_and_convert_to_png(src, local_png):
                    return local_png
    except Exception:
        pass

    # Tier 3: Facebook page picture, page found via the station website
    time.sleep(0.5)
    try:
        page = find_station_facebook_page(callsign)
        if page and download_facebook_logo(page, local_png):
            return local_png
    except Exception:
        pass

    return None


FACEBOOK_GRAPH_PICTURE = "https://graph.facebook.com/{page}/picture?type=large"
_FACEBOOK_SKIP = {"sharer", "sharer.php", "share", "plugins", "dialog", "tr",
                  "login", "policies", "help", "pages", "profile.php", "groups",
                  "events", "hashtag", "watch", "marketplace", "privacy"}


def facebook_page_from_text(text):
    """First facebook.com/<page> slug or URL in text, or None."""
    for m in re.finditer(r"(?:https?://)?(?:www\.|m\.)?(?:facebook|fb)\.com/([A-Za-z0-9._-]{2,})",
                         text):
        slug = m.group(1).rstrip(".")
        # facebook.com/2008/fbml is page code, not a page
        if slug.lower() not in _FACEBOOK_SKIP and not re.fullmatch(r"\d{1,5}", slug):
            return slug
    return None


def station_websites(callsign):
    """Official website URLs listed on the station's Wikipedia page."""
    params = {"action": "query", "titles": callsign, "prop": "extlinks",
              "ellimit": 100, "format": "json"}
    r = _api_get("https://en.wikipedia.org/w/api.php", params=params)
    if not r:
        return []
    urls = []
    for page in r.json().get("query", {}).get("pages", {}).values():
        for link in page.get("extlinks", []):
            url = link.get("*", "")
            low = url.lower()
            if not low.startswith("http"):
                continue
            if any(s in low for s in ("fcc.gov", "fccdata", "radio-locator",
                                      "wikipedia", "wikimedia", "archive.org",
                                      "nielsen", "radiodiscussions")):
                continue
            urls.append(url)
    return urls


def find_station_facebook_page(callsign):
    """Facebook page slug for a station: Wikipedia links, then the station
    website's own Facebook link.  None when nothing was found."""
    urls = station_websites(callsign)
    for url in urls:
        slug = facebook_page_from_text(url)
        if slug:
            return slug
    for url in urls[:3]:
        try:
            r = requests.get(url, timeout=TIMEOUT,
                             headers={"User-Agent": "Mozilla/5.0"})
            slug = facebook_page_from_text(r.text)
            if slug:
                return slug
        except Exception:
            continue
    return None


def download_facebook_logo(page, output_path):
    """Save a Facebook page's profile picture as PNG. Accepts a slug or URL."""
    slug = facebook_page_from_text(page) or page.strip().strip("/")
    if not slug:
        return False
    # Pages without a photo only have Facebook's grey person placeholder.
    r = _api_get(FACEBOOK_GRAPH_PICTURE.format(page=slug).split("?")[0],
                 params={"type": "large", "redirect": "false"})
    try:
        data = r.json().get("data") or {}
    except Exception:
        return False
    if data.get("is_silhouette") or not data.get("url"):
        return False
    return _download_and_convert_to_png(data["url"], output_path)


# Words in a picture's file name that mean a photo, not a logo.
_PHOTO_WORDS = ("tower", "studio", "building", "office", "transmitter",
                "antenna", "facility", "facilities", "headquarters", "mast",
                "site", "photo", "sign")


def _wiki_file_name(v):
    """'File:WKQQ logo.png' / a Wikimedia (thumb) URL -> 'wkqq logo.png'."""
    import urllib.parse
    v = urllib.parse.unquote(str(v or "")).split("?")[0]
    v = re.sub(r"^\[\[|\]\]$", "", v.strip()).split("|")[0]
    v = re.sub(r"^(?:file|image):", "", v.strip(), flags=re.I)
    v = re.sub(r"^\d+px-", "", v.rstrip("/").rsplit("/", 1)[-1])
    return v.replace("_", " ").strip().lower()


def _is_photo(url, avoid):
    """A facility photo (the infobox's "image", or a tower/studio file)?"""
    name = _wiki_file_name(url)
    return bool(name) and (name in avoid or (
        "logo" not in name and any(w in name for w in _PHOTO_WORDS)))


def _wiki_logo_fields(callsign):
    """(infobox logo file URL or None, infobox photo file names)."""
    r = _api_get("https://en.wikipedia.org/w/api.php", params={
        "action": "parse", "page": callsign, "prop": "wikitext",
        "section": 0, "redirects": 1, "format": "json"})
    try:
        text = r.json().get("parse", {}).get("wikitext", {}).get("*", "")
    except Exception:
        return None, set()
    fields = dict(re.findall(r"^[ \t]*\|[ \t]*(\w+)[ \t]*=[ \t]*(.*?)[ \t]*$",
                             text, re.M))
    avoid = {_wiki_file_name(fields[k]) for k in ("image", "photo")
             if fields.get(k)} - {""}
    logo = _wiki_file_name(fields.get("logo"))
    if not logo or not re.search(r"\.(png|jpe?g|gif|webp|svg)$", logo):
        return None, avoid
    r = _api_get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "titles": "File:" + logo, "prop": "imageinfo",
        "iiprop": "url", "iiurlwidth": 500, "format": "json"})
    try:
        for page in r.json().get("query", {}).get("pages", {}).values():
            for info in page.get("imageinfo") or []:
                src = info.get("thumburl") or info.get("url")
                if src:
                    return src.split("?")[0], avoid
    except Exception:
        pass
    return None, avoid


def _download_and_convert_to_png(url, output_path):
    """Download an image from URL and save it as PNG. Returns True on success."""
    headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*"}
    if "wikimedia.org" in url:
        headers["Referer"] = "https://en.wikipedia.org/"

    # Download to a temp file first
    tmp_path = str(output_path) + ".tmp"
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=TIMEOUT, stream=True, headers=headers)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            break
        except Exception:
            if attempt < 2:
                time.sleep(1)
            else:
                # Clean up temp file on final failure
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                return False
    else:
        return False

    # Convert to PNG using Pillow
    try:
        from PIL import Image
        img = Image.open(tmp_path)
        # Preserve RGBA if the source has transparency, otherwise use RGB
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.save(str(output_path), "PNG")
        os.remove(tmp_path)
        return True
    except Exception:
        # If conversion fails, try keeping the original file
        try:
            os.rename(tmp_path, str(output_path))
            return True
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False


class RFtapInput:
    """Network RDS data receiver supporting both ASCII G Protocol and binary RFtap.

    The SDR# RDSOutput plugin sends RDS data as ASCII "G Protocol" text over
    TCP port 23 (the same protocol RDS Spy uses).  Binary RFtap packets over
    UDP are also supported for GNU Radio / Wireshark workflows.

    ASCII G Protocol format (RDS Spy / SDR# RDSOutput):
        G:\r\nAAAABBBBCCCCDDDD\r\n\r\n
      where AAAA=PI, BBBB=block2, CCCC=block3, DDDD=block4 (4 hex chars each)
      Bad blocks are replaced with ----.

    Binary RFtap format (rftap.github.io):
        Header: magic(4) length32(u16) flags(u16) + optional fields
        Payload: 4 blocks x 2 bytes big-endian

    RDS groups are pushed to a thread-safe queue as .spy-format strings
    ("AAAA BBBB CCCC DDDD") for the existing decoder to consume.
    """

    RFTAP_MAGIC = b'RFta'  # 0x52467461 little-endian

    # RFtap flags bitmask (LSB first)
    FLAG_DLT = 0x0001
    FLAG_FREQ = 0x0002
    FLAG_NOMFREQ = 0x0004
    FLAG_FREQOFS = 0x0008
    FLAG_ISDBM = 0x0010
    FLAG_POWER = 0x0020
    FLAG_NOISE = 0x0040
    FLAG_SNR = 0x0080
    FLAG_QUAL = 0x0100
    FLAG_ISUNIXTIME = 0x0200
    FLAG_TIME = 0x0400
    FLAG_DURATION = 0x0800
    FLAG_LOCATION = 0x1000

    def __init__(self):
        import queue as _q
        self.enabled = False
        self.transport = "tcp"        # TCP is the default (SDR# RDSOutput uses TCP)
        self.host = "127.0.0.1"
        self.port = 23               # Port 23 per SDR# RDSOutput / RDS Spy convention
        self.data_format = "auto"    # "auto", "ascii_g", or "rftap"
        self._socket = None
        self._thread = None
        self._stop_flag = False
        self._conn = None            # active TCP connection

        # Thread-safe queue of .spy-format RDS group strings
        self.group_queue = _q.Queue()
        self.rds2_queue = _q.Queue()  # (stream 1-3, spy_line) from G1:-G3: records
        self.rds2_payloads = 0

        # Stats
        self.total_packets = 0
        self.valid_packets = 0
        self.invalid_packets = 0
        self.rds_payloads = 0
        self.last_packet_time = None
        self._recent_times = []
        self._detected_format = None  # set after auto-detection

        # RF metadata from last valid RFtap packet
        self.nominal_frequency = None
        self.frequency = None
        self.snr = None
        self.signal_power = None
        self.noise_power = None
        self.signal_quality = None
        self.rf_metadata_available = False

        # Error state
        self.last_error = None

    # ---- Binary RFtap parsing ----

    def parse_rftap(self, data):
        """Parse a binary RFtap packet.  Returns dict or None if invalid."""
        import struct

        if len(data) < 8:
            return None

        magic = data[0:4]
        if magic != self.RFTAP_MAGIC:
            return None

        length32 = struct.unpack('<H', data[4:6])[0]
        flags = struct.unpack('<H', data[6:8])[0]

        if length32 < 2:
            return None

        header_len = length32 * 4
        if header_len > len(data):
            return None

        offset = 8
        result = {'flags': flags}

        if flags & self.FLAG_DLT:
            if offset + 4 <= header_len:
                result['dlt'] = struct.unpack('<I', data[offset:offset+4])[0]
            offset += 4
        if flags & self.FLAG_FREQ:
            if offset + 8 <= header_len:
                result['freq'] = struct.unpack('<d', data[offset:offset+8])[0]
            offset += 8
        if flags & self.FLAG_NOMFREQ:
            if offset + 8 <= header_len:
                result['nomfreq'] = struct.unpack('<d', data[offset:offset+8])[0]
            offset += 8
        if flags & self.FLAG_FREQOFS:
            if offset + 8 <= header_len:
                result['freqofs'] = struct.unpack('<d', data[offset:offset+8])[0]
            offset += 8
        if flags & self.FLAG_POWER:
            if offset + 4 <= header_len:
                result['power'] = struct.unpack('<f', data[offset:offset+4])[0]
            offset += 4
        if flags & self.FLAG_NOISE:
            if offset + 4 <= header_len:
                result['noise'] = struct.unpack('<f', data[offset:offset+4])[0]
            offset += 4
        if flags & self.FLAG_SNR:
            if offset + 4 <= header_len:
                result['snr'] = struct.unpack('<f', data[offset:offset+4])[0]
            offset += 4
        if flags & self.FLAG_QUAL:
            if offset + 4 <= header_len:
                result['qual'] = struct.unpack('<f', data[offset:offset+4])[0]
            offset += 4
        if flags & self.FLAG_TIME:
            if offset + 16 <= header_len:
                result['timeint'] = struct.unpack('<d', data[offset:offset+8])[0]
                result['timefrac'] = struct.unpack('<d', data[offset+8:offset+16])[0]
            offset += 16
        if flags & self.FLAG_DURATION:
            if offset + 8 <= header_len:
                result['duration'] = struct.unpack('<d', data[offset:offset+8])[0]
            offset += 8
        if flags & self.FLAG_LOCATION:
            if offset + 24 <= header_len:
                result['lat'] = struct.unpack('<d', data[offset:offset+8])[0]
                result['lon'] = struct.unpack('<d', data[offset+8:offset+16])[0]
                result['alt'] = struct.unpack('<d', data[offset+16:offset+24])[0]
            offset += 24

        payload = data[header_len:]
        if len(payload) >= 8:
            b1, b2, b3, b4 = struct.unpack('>4H', payload[:8])
            result['rds_group'] = (b1, b2, b3, b4)
        else:
            result['rds_group'] = None

        return result

    # ---- ASCII G Protocol parsing ----

    def parse_ascii_g(self, text):
        """Parse ASCII G Protocol text.  Returns list of (b1,b2,b3,b4) tuples
        for the classic (stream 0) groups only."""
        return [blocks for stream, blocks in self.parse_ascii_g_streams(text)
                if stream == 0]

    def parse_ascii_g_streams(self, text):
        """Parse ASCII G Protocol text.  Returns list of (stream, (b1,b2,b3,b4)).

        Supports:
          RDS Spy:   G:\r\nAAAABBBBCCCCDDDD\r\n\r\n          (stream 0)
          RDS2:      G1:\r\nAAAABBBBCCCCDDDD\r\n\r\n         (streams 1-3, FM Tuner)
          xdr-gtk:   GAAAABBBBCCCCDDDDEE\n  (EE = optional error flags)
        """
        import re
        groups = []

        # Pattern 1: RDS Spy format: "G:" (or "G1:".."G3:") CRLF, then 16 hex chars
        for m in re.finditer(r'G([1-3]?):\s*\r?\n([0-9A-Fa-f\-]{16})', text):
            stream = int(m.group(1) or 0)
            hex_str = m.group(2)
            blocks = []
            ok = True
            for i in range(0, 16, 4):
                part = hex_str[i:i+4]
                if '-' in part:
                    ok = False
                    break
                try:
                    blocks.append(int(part, 16))
                except ValueError:
                    ok = False
                    break
            if ok and len(blocks) == 4:
                groups.append((stream, tuple(blocks)))

        # Pattern 2: xdr-gtk format: G followed immediately by 16-18 hex chars + newline
        for m in re.finditer(r'G([0-9A-Fa-f]{16,18})\s*\n', text):
            hex_str = m.group(1)[:16]
            try:
                b1 = int(hex_str[0:4], 16)
                b2 = int(hex_str[4:8], 16)
                b3 = int(hex_str[8:12], 16)
                b4 = int(hex_str[12:16], 16)
                groups.append((0, (b1, b2, b3, b4)))
            except ValueError:
                pass

        return groups

    def _queue_groups(self, stream_groups):
        """Route parsed (stream, blocks) records: stream 0 to the RDS decoder
        queue, RDS2 streams 1-3 to the analyzer-only queue."""
        for stream, (b1, b2, b3, b4) in stream_groups:
            spy_line = f"{b1:04X} {b2:04X} {b3:04X} {b4:04X}"
            try:
                if stream == 0:
                    self.group_queue.put_nowait(spy_line)
                else:
                    self.rds2_payloads += 1
                    self.rds2_queue.put_nowait((stream, spy_line))
            except Exception:
                pass

    # ---- Packet processing ----

    def _process_raw(self, data):
        """Process raw data from the network.  Auto-detects format."""
        import time as _time
        self.total_packets += 1
        now = _time.time()
        self.last_packet_time = now
        self._recent_times.append(now)
        cutoff = now - 60
        self._recent_times = [t for t in self._recent_times if t > cutoff]

        fmt = self.data_format

        # Auto-detect format from first packet
        if fmt == "auto" and self._detected_format is None:
            if len(data) >= 4 and data[:4] == self.RFTAP_MAGIC:
                self._detected_format = "rftap"
            elif len(data) >= 1 and (data[0:1] == b'G'):
                self._detected_format = "ascii_g"
            else:
                try:
                    text = data.decode('ascii', errors='ignore')
                    if 'G:' in text or text.startswith('G'):
                        self._detected_format = "ascii_g"
                    elif 'RFta' in text:
                        self._detected_format = "rftap"
                except Exception:
                    pass

        if fmt == "auto":
            fmt = self._detected_format or "ascii_g"

        if fmt == "rftap":
            result = self.parse_rftap(data)
            if result is None:
                self.invalid_packets += 1
                return
            self.valid_packets += 1

            self.rf_metadata_available = False
            for key, attr in [('nomfreq', 'nominal_frequency'), ('freq', 'frequency'),
                              ('snr', 'snr'), ('power', 'signal_power'),
                              ('noise', 'noise_power'), ('qual', 'signal_quality')]:
                if key in result:
                    setattr(self, attr, result[key])
                    self.rf_metadata_available = True

            if result.get('rds_group'):
                self.rds_payloads += 1
                b1, b2, b3, b4 = result['rds_group']
                spy_line = f"{b1:04X} {b2:04X} {b3:04X} {b4:04X}"
                try:
                    self.group_queue.put_nowait(spy_line)
                except Exception:
                    pass

        elif fmt == "ascii_g":
            try:
                text = data.decode('ascii', errors='ignore')
            except Exception:
                text = ''

            groups = self.parse_ascii_g_streams(text)
            if groups:
                self.valid_packets += 1
                self.rds_payloads += len(groups)
                self._queue_groups(groups)
            else:
                self.valid_packets += 1

    # ---- UDP listener ----

    def _listen_udp(self):
        """UDP listener -- one datagram = one packet (RFtap or ASCII)."""
        import socket
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socket.bind((self.host, self.port))
            self._socket.settimeout(1.0)
        except Exception as e:
            self.last_error = f"Bind failed: {e}"
            return

        while not self._stop_flag:
            try:
                data, addr = self._socket.recvfrom(4096)
                self._process_raw(data)
            except socket.timeout:
                continue
            except Exception as e:
                if not self._stop_flag:
                    self.last_error = str(e)

    # ---- TCP listener ----

    def _listen_tcp(self):
        """
        TCP client for SDR# RDSOutput.

        SDR# RDSOutput is the server. RDS Monitor connects outward to SDR#
        at the configured host and port and receives the continuous RDS stream.
        """
        import socket
        import time as _time

        while not self._stop_flag:
            conn = None

            try:
                self._socket = socket.socket(
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                )
                self._socket.settimeout(5.0)

                # RDS Monitor is the TCP client.
                self._socket.connect((self.host, self.port))

                self._conn = self._socket
                conn = self._conn
                conn.settimeout(1.0)

                self.last_error = None
                buffer = b""

                while not self._stop_flag:
                    try:
                        data = conn.recv(4096)

                        if not data:
                            raise ConnectionError(
                                "SDR# RDSOutput closed the TCP connection"
                            )

                        buffer += data

                        if self._detected_format is None and buffer:
                            stripped = buffer.lstrip()

                            if (
                                len(stripped) >= 4
                                and stripped[:4] == self.RFTAP_MAGIC
                            ):
                                self._detected_format = "rftap"
                            else:
                                self._detected_format = "ascii_g"

                        fmt = self.data_format
                        if fmt == "auto":
                            fmt = self._detected_format or "ascii_g"

                        if fmt == "rftap":
                            while True:
                                magic_pos = buffer.find(self.RFTAP_MAGIC)

                                if magic_pos < 0:
                                    buffer = buffer[-3:]
                                    break

                                if magic_pos > 0:
                                    buffer = buffer[magic_pos:]

                                if len(buffer) < 8:
                                    break

                                next_magic = buffer.find(
                                    self.RFTAP_MAGIC,
                                    4,
                                )

                                if next_magic <= 0:
                                    break

                                packet = buffer[:next_magic]
                                buffer = buffer[next_magic:]
                                self._process_raw(packet)

                        else:
                            try:
                                text = buffer.decode(
                                    "ascii",
                                    errors="ignore",
                                )
                            except Exception:
                                text = ""

                            last_newline = text.rfind("\n")

                            if last_newline >= 0:
                                complete_text = text[:last_newline + 1]
                                groups = self.parse_ascii_g_streams(
                                    complete_text)

                                if groups:
                                    self.total_packets += 1
                                    self.valid_packets += 1
                                    self.rds_payloads += len(groups)

                                    now = _time.time()
                                    self.last_packet_time = now
                                    self._recent_times.append(now)

                                    cutoff = now - 60
                                    self._recent_times = [
                                        t
                                        for t in self._recent_times
                                        if t > cutoff
                                    ]

                                    self._queue_groups(groups)

                                consumed = len(
                                    complete_text.encode(
                                        "ascii",
                                        errors="ignore",
                                    )
                                )
                                buffer = buffer[consumed:]

                            elif len(buffer) > 65536:
                                buffer = buffer[-65536:]

                    except socket.timeout:
                        continue

            except Exception as e:
                if not self._stop_flag:
                    self.last_error = f"TCP client error: {e}"

                    for _ in range(20):
                        if self._stop_flag:
                            break
                        _time.sleep(0.25)

            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

                self._conn = None

                if self._socket is not None:
                    try:
                        self._socket.close()
                    except Exception:
                        pass

                self._socket = None
    # ---- Lifecycle ----

    def start(self):
        """Start the listener."""
        self._stop_flag = False
        self.last_error = None
        self._detected_format = None
        if self.transport == "tcp":
            self._thread = threading.Thread(target=self._listen_tcp, daemon=True)
        else:
            self._thread = threading.Thread(target=self._listen_udp, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the listener."""
        self._stop_flag = True
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._socket = None
        self._conn = None

    # ---- Status ----

    def get_status(self):
        """Return current status string."""
        import time as _time
        if not self.enabled:
            return "disabled"
        if self.last_error:
            return "error"
        if not self.last_packet_time:
            return "waiting"
        age = _time.time() - self.last_packet_time
        if age > 10:
            return "stale"
        if age < 5:
            if self.rds_payloads > 0:
                return "receiving_rds"
            return "receiving"
        return "stale"

    def get_packet_rate(self):
        """Return packets per second (last 10 seconds)."""
        import time as _time
        if not self._recent_times:
            return 0.0
        now = _time.time()
        recent = [t for t in self._recent_times if t > now - 10]
        return len(recent) / 10.0 if recent else 0.0

    def get_last_packet_age(self):
        """Return seconds since last packet, or None."""
        import time as _time
        if not self.last_packet_time:
            return None
        return _time.time() - self.last_packet_time

    def drain_rds2_groups(self, max_items=500):
        """Drain queued RDS2 groups as (stream, spy_line) tuples."""
        groups = []
        while len(groups) < max_items:
            try:
                groups.append(self.rds2_queue.get_nowait())
            except Exception:
                break
        return groups

    def drain_groups(self, max_items=500):
        """Drain all queued RDS groups as .spy-format strings."""
        groups = []
        while len(groups) < max_items:
            try:
                groups.append(self.group_queue.get_nowait())
            except Exception:
                break
        return groups


# ===========================================================================
# Tuner link — telemetry and control for the standalone FM Tuner
# ===========================================================================

TUNER_HOST = "127.0.0.1"
TUNER_RDS_PORT = 7376        # ASCII G group stream
TUNER_TELEMETRY_PORT = 7373  # JSON-lines status
TUNER_COMMAND_PORT = 7374    # command channel


class TunerLink:
    """Client for the standalone FM Tuner's telemetry and command ports.

    Telemetry is a JSON object per line on port 7373; commands are single
    lines on port 7374 answering with one JSON object.  RDS groups arrive
    separately through RFtapInput on port 7376.
    """

    def __init__(self, host=TUNER_HOST,
                 telemetry_port=TUNER_TELEMETRY_PORT,
                 command_port=TUNER_COMMAND_PORT):
        self.host = host
        self.telemetry_port = telemetry_port
        self.command_port = command_port
        self.telemetry = {}
        self.connected = False
        self.last_error = None
        self.last_update = None
        self._thread = None
        self._stop_flag = False
        self._socket = None

    def start(self):
        import threading
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag = True
        try:
            if self._socket:
                self._socket.close()
        except Exception:
            pass
        self._socket = None
        self.connected = False

    def _run(self):
        import socket
        import json
        import time as _time
        while not self._stop_flag:
            try:
                sock = socket.create_connection(
                    (self.host, self.telemetry_port), timeout=3.0)
                sock.settimeout(1.0)
                self._socket = sock
                self.connected = True
                self.last_error = None
                buf = b""
                while not self._stop_flag:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line.decode("utf-8", "replace"))
                        except Exception:
                            continue
                        if isinstance(data, dict):
                            self.telemetry = data
                            self.last_update = _time.time()
            except Exception as e:
                self.last_error = str(e)
            finally:
                self.connected = False
                try:
                    if self._socket:
                        self._socket.close()
                except Exception:
                    pass
                self._socket = None
            if self._stop_flag:
                break
            _time.sleep(2.0)

    def send_command(self, command, timeout=3.0):
        """Send one command line to the tuner and return the parsed reply."""
        import socket
        import json
        try:
            with socket.create_connection(
                    (self.host, self.command_port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                sock.sendall((command.strip() + "\n").encode("utf-8"))
                buf = b""
                while b"\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                line = buf.split(b"\n", 1)[0].strip()
                if not line:
                    return None
                return json.loads(line.decode("utf-8", "replace"))
        except Exception as e:
            self.last_error = str(e)
            return None

    @property
    def frequency_mhz(self):
        value = self.telemetry.get("frequency_mhz")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def summary(self):
        """One-line description of the tuner state for the status bar."""
        tel = self.telemetry
        if not tel:
            return "Tuner: waiting for telemetry"
        parts = []
        freq = self.frequency_mhz
        if freq is not None:
            parts.append(f"{mhz_text(freq, tel)} MHz")
        if tel.get("state"):
            parts.append(str(tel["state"]))
        if tel.get("signal_dbfs") is not None:
            parts.append(signal_text(tel["signal_dbfs"], tel))
        parts.append("pilot locked (stereo)" if tel.get("stereo")
                     else "no pilot (mono)")
        if tel.get("mpx_rds_db") is not None:
            parts.append(f"RDS {tel['mpx_rds_db']:.1f} dB")
        if tel.get("hd_detected"):
            hd_call = (tel.get("hd") or {}).get("call")
            parts.append(f"HD ({hd_call})" if hd_call else "HD")
        return "Tuner: " + ", ".join(parts)


# ===========================================================================
# GUI
# ===========================================================================

def run_gui(spy_file, logo_dir, watch_dir=None, spy_filename="recording.spy",
            start_input="tuner"):
    """Run a single-station live tkinter GUI monitoring the .spy file."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    try:
        from PIL import Image, ImageTk
        HAS_PIL = True
    except ImportError:
        HAS_PIL = False
        print("Pillow not installed. Logo images may not display.")
        print("Install with: pip install Pillow")

    root = tk.Tk()
    root.title("RDS Monitor")
    root.configure(bg="#1a1a2e")
    root.minsize(960, 540)

    # --- Menu bar ---
    menubar = tk.Menu(root, bg="#0f3460", fg="#ccc", activebackground="#1a5276",
                      activeforeground="#fff", borderwidth=0)

    # State variables for menu toggles
    show_confidence = tk.BooleanVar(value=True)
    show_rtplus = tk.BooleanVar(value=True)
    show_tmc = tk.BooleanVar(value=True)
    show_art = tk.BooleanVar(value=True)
    show_badges = tk.BooleanVar(value=True)
    show_logos = tk.BooleanVar(value=True)
    dark_mode = tk.BooleanVar(value=True)
    spy_file_var = tk.StringVar(value=spy_file)
    logo_dir_var = tk.StringVar(value=logo_dir)
    input_mode = tk.StringVar(value="spy")  # "spy", "tuner", "rftap"
    tuner_freq_var = tk.StringVar(value="--.- MHz")

    rftap_input = None  # RFtapInput instance (RDS group stream)
    tuner_link = None   # TunerLink instance (telemetry + commands)

    # RFtap status indicator
    rftap_status_var = tk.StringVar(value="RFtap: \u25cb Disabled")

    # Input menu
    input_menu = tk.Menu(menubar, tearoff=0, bg="#0f3460", fg="#ccc",
                        activebackground="#1a5276", activeforeground="#fff")
    input_menu.add_radiobutton(label="Spy File (RDS Spy)",
                               variable=input_mode, value="spy",
                               command=lambda: _switch_to_spy())
    input_menu.add_radiobutton(label="FM Tuner (TCP)",
                               variable=input_mode, value="tuner",
                               command=lambda: _switch_to_tuner())
    input_menu.add_radiobutton(label="RDS Input (other TCP/UDP source)...",
                               variable=input_mode, value="rftap",
                               command=lambda: _switch_to_rftap())
    menubar.add_cascade(label="Input", menu=input_menu)

    # File menu
    file_menu = tk.Menu(menubar, tearoff=0, bg="#0f3460", fg="#ccc",
                        activebackground="#1a5276", activeforeground="#fff")
    file_menu.add_command(label="Open Spy File...",
                          command=lambda: _open_spy_file())
    file_menu.add_command(label="Set Logo Directory...",
                          command=lambda: _set_logo_dir())
    if not LITE:
        file_menu.add_separator()
        file_menu.add_command(label="Generate Report (All Stations)...",
                              command=lambda: _export_data())
        file_menu.add_command(label="Generate Report (Current Station)...",
                              command=lambda: _export_data(current_only=True))
    file_menu.add_separator()
    file_menu.add_command(label="Exit", command=root.quit)
    menubar.add_cascade(label="File", menu=file_menu)

    # View menu
    view_menu = tk.Menu(menubar, tearoff=0, bg="#0f3460", fg="#ccc",
                        activebackground="#1a5276", activeforeground="#fff")
    view_menu.add_checkbutton(label="Show Confidence Panel",
                              variable=show_confidence,
                              command=lambda: _toggle_confidence())
    view_menu.add_checkbutton(label="Show RT+ Items",
                              variable=show_rtplus,
                              command=lambda: _toggle_rtplus())
    view_menu.add_checkbutton(label="Show TMC Traffic Messages",
                              variable=show_tmc,
                              command=lambda: _refresh_tmc_now())
    view_menu.add_checkbutton(label="Show Cover Art (online, not saved)",
                              variable=show_art,
                              command=lambda: _refresh_art_now())
    view_menu.add_checkbutton(label="Show TP/TA/Music Badges",
                              variable=show_badges,
                              command=lambda: _toggle_badges())
    view_menu.add_checkbutton(label="Show Station Logos",
                              variable=show_logos,
                              command=lambda: _toggle_logos())
    view_menu.add_separator()
    view_menu.add_checkbutton(label="Dark Mode",
                              variable=dark_mode,
                              command=lambda: _toggle_dark_mode())
    menubar.add_cascade(label="View", menu=view_menu)

    auto_map_log_var = tk.BooleanVar(value=load_auto_map_log())
    auto_hd_log_var = tk.BooleanVar(value=load_auto_hd_log())

    # Tools menu
    tools_menu = tk.Menu(menubar, tearoff=0, bg="#0f3460", fg="#ccc",
                         activebackground="#1a5276", activeforeground="#fff")
    tools_menu.add_command(label="Download Missing Logos",
                           command=lambda: _download_missing_logos())
    tools_menu.add_command(label="Manual Logo Lookup...",
                           command=lambda: _manual_logo_lookup())
    rx_choice_var = tk.StringVar(value=load_locations()["active"])
    tools_menu.add_command(label="Set Home Location...",
                           command=lambda: _set_home_location())
    tools_menu.add_command(label="Set Away Location (DX on the go)...",
                           command=lambda: _set_away_location())
    tools_menu.add_radiobutton(label="Use Home location", value="home",
                               variable=rx_choice_var,
                               command=lambda: _choose_location("home"))
    tools_menu.add_radiobutton(label="Use Away location", value="away",
                               variable=rx_choice_var,
                               command=lambda: _choose_location("away"))
    tools_menu.add_command(label="GPS Location (USB GPS dongle)...",
                           command=lambda: _gps_window())
    tools_menu.add_separator()
    tools_menu.add_command(label="Log Current Station to DX Mapper",
                           accelerator="Ctrl+L",
                           command=lambda: _log_to_map())
    tools_menu.add_checkbutton(
        label="Auto-log to DX Mapper (off by default)",
        variable=auto_map_log_var,
        command=lambda: (save_auto_map_log(auto_map_log_var.get()),
                         _sync_hd_log_menu()))
    tools_menu.add_checkbutton(
        label="    \u2514 Auto-log HD catches too",
        variable=auto_hd_log_var,
        command=lambda: save_auto_hd_log(auto_hd_log_var.get()))
    hd_log_menu_index = tools_menu.index(tk.END)

    def _sync_hd_log_menu():
        tools_menu.entryconfig(
            hd_log_menu_index,
            state=tk.NORMAL if auto_map_log_var.get() else tk.DISABLED)

    _sync_hd_log_menu()
    tools_menu.add_separator()
    tools_menu.add_command(label="Clear Spy File Data",
                           command=lambda: _clear_spy_data())
    tools_menu.add_separator()
    tools_menu.add_command(label="Reset Lifetime Station Counter",
                           command=lambda: _reset_station_counter())
    tools_menu.add_command(label="Reset Session Station Counter",
                           command=lambda: _reset_session_counter())
    menubar.add_cascade(label="Tools", menu=tools_menu)

    # Help menu
    help_menu = tk.Menu(menubar, tearoff=0, bg="#0f3460", fg="#ccc",
                        activebackground="#1a5276", activeforeground="#fff")
    help_menu.add_command(label="RDS Reference",
                          command=lambda: _show_reference())
    help_menu.add_command(label="About RDS Monitor",
                          command=lambda: _show_about())
    menubar.add_cascade(label="Help", menu=help_menu)

    root.config(menu=menubar)

    # --- Menu action functions ---
    def _ask_latlon(title, prompt, current):
        from tkinter import simpledialog
        initial = f"{current[0]:.5f}, {current[1]:.5f}" if current else ""
        while True:
            text = simpledialog.askstring(title, prompt, initialvalue=initial,
                                          parent=root)
            if text is None:
                return None
            try:
                lat_s, lon_s = text.replace(";", ",").split(",")
                lat, lon = float(lat_s), float(lon_s)
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    return lat, lon
            except ValueError:
                pass
            messagebox.showerror(title, "Enter two numbers: latitude, longitude")
            initial = text

    def _apply_location():
        lat, lon, label = active_location()
        home_location[0] = (lat, lon)
        location_label[0] = label
        rx_choice_var.set("away" if label != "Home" else "home")
        home_changed[0] = True
        status_var.set(f"Distances from {label}: {lat:.4f}, {lon:.4f}")

    def _set_home_location():
        loc = load_locations()
        ll = _ask_latlon(
            "Home Location",
            "Your HOME latitude, longitude (decimal degrees, e.g. 39.7817, -89.6501).\n"
            "Used for distance and bearing, and by DX Mapper.",
            loc["home"])
        if ll is None:
            return
        try:
            save_home_location(*ll)
        except OSError as e:
            messagebox.showwarning("Home Location", f"Could not save: {e}")
            return
        _apply_location()

    def _set_away_location(switch=True):
        from tkinter import simpledialog
        loc = load_locations()
        ll = _ask_latlon(
            "Away Location",
            "Where you are DXing from (latitude, longitude),\n"
            "e.g. a hilltop, a park or a trip.",
            loc["away"] or loc["home"])
        if ll is None:
            return False
        name = simpledialog.askstring(
            "Away Location", "Name for this location (e.g. Hilltop, Florida trip):",
            initialvalue="" if loc["away_name"] == "Away" else loc["away_name"],
            parent=root)
        try:
            save_away_location(ll[0], ll[1], name or "Away")
            if switch:
                save_active_location("away")
        except OSError as e:
            messagebox.showwarning("Away Location", f"Could not save: {e}")
            return False
        _apply_location()
        return True

    def _choose_location(which):
        if which == "away" and not load_locations()["away"]:
            if not _set_away_location():
                rx_choice_var.set("home")
            return
        try:
            save_active_location(which)
        except OSError as e:
            messagebox.showwarning("Location", f"Could not save: {e}")
        _apply_location()

    gps_state = {"reader": None, "saved": None, "saved_time": 0.0, "win": None}

    def _gps_start(port=None):
        if gps_state["reader"]:
            gps_state["reader"].stop()
        port = port or _read_settings().get("gps_port") or "Auto"
        gps_state["reader"] = gps.GpsReader(port).start()

    def _gps_follow_on():
        return bool(_read_settings().get("gps_follow_away"))

    def _gps_save(which, fix, quiet=False):
        try:
            if which == "home":
                save_home_location(fix["lat"], fix["lon"])
            else:
                save_away_location(fix["lat"], fix["lon"], "GPS")
                save_active_location("away")
        except OSError as e:
            if not quiet:
                messagebox.showwarning("GPS Location", f"Could not save: {e}")
            return
        gps_state["saved"], gps_state["saved_time"] = (fix["lat"], fix["lon"]), time.time()
        _apply_location()

    def _gps_poll():
        r = gps_state["reader"]
        if r and r.fix and _gps_follow_on() and time.time() - r.fix_time < 10 \
                and gps.should_follow(gps_state["saved"], gps_state["saved_time"], r.fix):
            _gps_save("away", r.fix, quiet=True)
        root.after(2000, _gps_poll)

    def _gps_window():
        if gps_state["win"] is not None and gps_state["win"].winfo_exists():
            gps_state["win"].lift()
            return
        from tkinter import ttk
        if not gps_state["reader"] or not gps_state["reader"].running:
            _gps_start()
        win = tk.Toplevel(root)
        gps_state["win"] = win
        win.title("GPS Location")
        win.transient(root)
        win.resizable(False, False)
        pad = {"padx": 10, "pady": 4}
        tk.Label(win, text="Optional: plug in a USB GPS dongle to set your location from it.",
                 justify="left").grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        tk.Label(win, text="Port:").grid(row=1, column=0, sticky="w", **pad)
        ports = ["Auto"] + [f"{p} - {d}" if d != p else p for p, d in gps.list_ports()]
        port_var = tk.StringVar(value=_read_settings().get("gps_port") or "Auto")
        box = ttk.Combobox(win, textvariable=port_var, values=ports, width=34, state="readonly")
        box.grid(row=1, column=1, sticky="w", **pad)

        def _retry(*_):
            port = port_var.get().split(" - ")[0]
            try:
                _update_settings({"gps_port": port})
            except OSError:
                pass
            _gps_start(port)
        box.bind("<<ComboboxSelected>>", _retry)
        tk.Button(win, text="Retry", command=_retry).grid(row=1, column=2, **pad)
        status = tk.StringVar()
        fix_text = tk.StringVar()
        tk.Label(win, textvariable=status, fg="#555", wraplength=420, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", **pad)
        tk.Label(win, textvariable=fix_text, font=("Consolas", 12)).grid(
            row=3, column=0, columnspan=3, sticky="w", **pad)
        btns = tk.Frame(win)
        btns.grid(row=4, column=0, columnspan=3, sticky="w", **pad)
        home_btn = tk.Button(btns, text="Set as Home",
                             command=lambda: _gps_save("home", gps_state["reader"].fix))
        away_btn = tk.Button(btns, text="Use as Away location",
                             command=lambda: _gps_save("away", gps_state["reader"].fix))
        home_btn.pack(side="left", padx=(0, 8))
        away_btn.pack(side="left")
        follow_var = tk.BooleanVar(value=_gps_follow_on())

        def _follow():
            try:
                _update_settings({"gps_follow_away": bool(follow_var.get())})
            except OSError:
                pass
            if follow_var.get():
                gps_state["saved"] = None
        tk.Checkbutton(win, text="Keep the Away location updated from GPS while traveling",
                       variable=follow_var, command=_follow).grid(
            row=5, column=0, columnspan=3, sticky="w", **pad)
        if not gps.have_pyserial():
            tk.Label(win, fg="#a33", justify="left",
                     text="GPS needs the pyserial add-on: run Install All.bat once.").grid(
                row=6, column=0, columnspan=3, sticky="w", **pad)
        tk.Button(win, text="Close", command=win.destroy).grid(row=7, column=2, sticky="e", **pad)

        def _refresh():
            if not win.winfo_exists():
                return
            r = gps_state["reader"]
            status.set(r.status if r else "")
            fresh = bool(r and r.fix and time.time() - r.fix_time < 10)
            if fresh:
                f = r.fix
                extra = f"   {f['sats']} sats" if f.get("sats") else ""
                fix_text.set(f"{f['lat']:.5f}, {f['lon']:.5f}{extra}")
            else:
                fix_text.set("no fix yet")
            for b in (home_btn, away_btn):
                b.config(state="normal" if fresh else "disabled")
            win.after(500, _refresh)
        _refresh()

    def _first_run_home():
        loc = load_locations()
        if loc["home_set"] or loc["home_prompted"]:
            return
        if messagebox.askyesno(
                "Home Location",
                "Set up your home location now?\n"
                "It is used for distance and bearing to stations and by DX Mapper.\n"
                "(Tools > Set Home Location... any time; an Away location is there too.)",
                parent=root):
            _set_home_location()
        try:
            _update_settings({"home_prompted": True})
        except OSError:
            pass

    def _open_spy_file():
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Open Spy File",
            filetypes=[("Spy files", "*.spy"), ("All files", "*.*")],
            initialdir=str(Path(spy_file_var.get()).parent) if spy_file_var.get() else "."
        )
        if path:
            spy_file_var.set(path)
            nonlocal spy_file
            spy_file = path
            status_var.set(f"Spy file: {Path(path).name}")
            last_mtime[0] = 0
            last_size[0] = 0

    def _set_logo_dir():
        from tkinter import filedialog
        path = filedialog.askdirectory(
            title="Select Logo Directory",
            initialdir=logo_dir_var.get() if logo_dir_var.get() else "."
        )
        if path:
            logo_dir_var.set(path)
            nonlocal logo_dir
            logo_dir = path
            status_var.set(f"Logo dir: {Path(path).name}")

    def _export_data(current_only=False, to_text=False):
        """Save a report - or, with to_text, return it (sent with Log to Map)."""
        from tkinter import filedialog, messagebox
        import time as _time

        if not stations_data[0]:
            if to_text:
                return None
            messagebox.showinfo("Report", "No station data available.")
            return

        if current_only:
            pi = current_pi[0]
            if not pi or pi not in stations_data[0]:
                if to_text:
                    return None
                messagebox.showinfo("Report", "No current station selected.")
                return
            report_stations = [(pi, stations_data[0][pi])]
            label = "Current Station"
        if not current_only:
            if session_stations[0]:
                report_stations = sorted(session_stations[0].items(),
                                         key=lambda kv: kv[1].get("last_seen", 0),
                                         reverse=True)
            else:
                report_stations = sorted(stations_data[0].items(),
                                         key=lambda kv: kv[1].get("last_seen", 0),
                                         reverse=True)
            label = "All Stations"

        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = "" if to_text else filedialog.asksaveasfilename(
            title=f"Save RDS Report ({label})",
            initialdir=str(REPORTS_DIR),
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile=f"rds_report_{label.lower().replace(' ', '_')}_{_time.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        if not path and not to_text:
            return

        lines = []
        sep = "=" * 78
        lines.append(sep)
        lines.append(f"RDS Monitor Report — {label}")
        lines.append(f"Created: {_time.strftime('%Y/%m/%d %H:%M:%S')}")
        if tuner_link is not None:
            lines.append(tuner_link.summary())
        lines.append(sep)

        for idx, (pi, data) in enumerate(report_stations):
            if idx > 0:
                lines.append("")
                lines.append("-" * 78)

            callsign = data.get("callsign") or "(unknown)"
            ps = data.get("ps", "")
            clean_ps = "".join(c if c.isprintable() or c == " " else " " for c in ps).strip()
            ptyn = data.get("ptyn", "")
            clean_ptyn = "".join(c if c.isprintable() or c == " " else " " for c in ptyn).strip()
            rt = data.get("rt", "")
            clean_rt = "".join(c if c.isprintable() or c == " " else " " for c in rt).strip()
            title = data.get("title", "")
            artist = data.get("artist", "")

            stats = data.get("stats", {})
            gc = stats.get("group_counts", {})
            total_groups = stats.get("total_groups", 0)
            total_blocks = stats.get("total_blocks", 0)
            bad_blocks = stats.get("bad_blocks", 0)
            good_blocks = total_blocks - bad_blocks
            quality_pct = f"{good_blocks}/{total_blocks}" if total_blocks else "0/0"
            block_pct = f"{good_blocks / total_blocks * 100:.1f}%" if total_blocks else "0%"

            # ========= BASIC IDENTIFICATION =========
            lines.append(f"\nStation: {callsign}  (PI: {pi})")
            lines.append(f"\n  --- Basic Identification ---")
            lines.append(f"  PI Code    = {pi} (0x{int(pi, 16):04X})")
            lines.append(f"  Callsign   = {callsign}")
            freq_mhz = data.get("freq_mhz")
            lines.append(f"  Frequency  = {freq_mhz:.1f} MHz"
                         if freq_mhz else "  Frequency  = (unknown)")
            geo = lookup_station_geo._cache.get((callsign or "").upper())
            if geo:
                if geo.get("location"):
                    lines.append(f"  Location   = {geo['location']}")
                if geo.get("lat") is not None:
                    lines.append(f"  Transmitter= {geo['lat']:.4f}, {geo['lon']:.4f}")
                    lat, lon, where = active_location()
                    rng = station_range_text((lat, lon), geo)
                    if rng:
                        lines.append(f"  Distance   = {rng} from {where}")
            sig = data.get("signal_dbfs")
            if isinstance(sig, (int, float)):
                sig_line = f"  Signal     = {signal_text(sig)}"
                peak = data.get("signal_peak_dbfs")
                if isinstance(peak, (int, float)):
                    sig_line += f"  (session peak {signal_text(peak)})"
                lines.append(sig_line)
            else:
                lines.append("  Signal     = (unknown)")
            rds_db = data.get("rds_level_db")
            if isinstance(rds_db, (int, float)):
                lines.append(f"  RDS Level  = {rds_db:.1f} dB (57 kHz subcarrier)")
            lines.append(f"  PS Name    = {clean_ps or '(none)'}")
            if data.get("long_ps"):
                lines.append(f"  Long PS    = {data['long_ps']}"
                             + ("" if data.get("long_ps_complete") else
                                f"  (partial, {len(data.get('lps_segments') or {})}/8 segments)"))
            pty_val = data.get('pty')
            pty_text = PTY_TABLE.get(pty_val, 'None')
            lines.append(f"  PTY        = {pty_val} ({pty_text})" if pty_val is not None else f"  PTY        = None")
            if clean_ptyn:
                lines.append(f"  PTYN       = {clean_ptyn}")
            else:
                lines.append(f"  PTYN       = (none)")
            af_freqs, af_expected, af_complete = af_summary(data)
            if af_freqs:
                af_state = data.get("af") or {}
                lines.append("  AF list    = " + ", ".join(f"{f:.1f} MHz" for f in af_freqs))
                lines.append(f"  AF status  = {len(af_freqs)}"
                             f"{'/' + str(af_expected) if af_expected else ''} "
                             f"{'complete' if af_complete else 'partial'}"
                             f"{', method ' + af_state['method'] if af_state.get('method') else ''}")
            else:
                lines.append(f"  AF list    = (none)")
            eon_report = eon_lines(data)
            if eon_report:
                lines.append(f"  EON        = {len(data['eon'])} other network(s)")
                lines.extend(eon_report)
            else:
                lines.append(f"  EON        = (none)")
            if title:
                lines.append(f"  Song Title = {title}")
            if artist:
                lines.append(f"  Song Artist= {artist}")
            if title or artist:
                src = data.get("now_playing_source") or "RT"
                lines.append(f"  Now Playing source = "
                             f"{'RT+ tags' if src == 'RT+' else 'parsed from RadioText'}")

            # ========= FLAGS & INDICATORS =========
            lines.append(f"\n  --- Flags & Indicators ---")
            tp = data.get("tp", False)
            ta = data.get("ta", False)
            lines.append(f"  TP         = {'1 (Traffic Program)' if tp else '0'}")
            lines.append(f"  TA         = {'1 (Traffic Announcement)' if ta else '0'}")
            ms = data.get("music_speech")
            ms_text = "Music" if ms is True else ("Speech" if ms is False else "---")
            lines.append(f"  M/S        = {ms_text}")

            # DI (Decoder Identification)
            di = data.get("di")
            if di is not None:
                b = di_bits(di)
                segs = data.get("di_segments") or {}
                state = ("complete" if len(segs) == 4
                         else f"partial ({len(segs)}/4 segments)")
                lines.append(f"  DI         = {di} (d3..d0 = {di:04b}) - {state}")
                lines.append(f"    d0 stereo          = {int(b['stereo'])}"
                             f" ({'stereo' if b['stereo'] else 'mono'})")
                lines.append(f"    d1 artificial head = {int(b['artificial_head'])}"
                             f" ({'artificial-head' if b['artificial_head'] else 'not artificial-head'})")
                lines.append(f"    d2 compression     = {int(b['compressed'])}"
                             f" ({'compressed' if b['compressed'] else 'not compressed'})")
                lines.append(f"    d3 dynamic PTY     = {int(b['dynamic_pty'])}"
                             f" ({'dynamic' if b['dynamic_pty'] else 'static'} PTY)")
            else:
                lines.append(f"  DI         = (none)")
            stereo_lines = stereo_status_lines(data)
            hd_lines = hd_status_lines(data) + gain_status_lines(data)
            if stereo_lines or hd_lines:
                lines.extend(stereo_lines + hd_lines + [""])

            dt = data.get("date_time")
            if dt:
                lines.append(f"  Date/Time  = {dt.get('date_str', '')} {dt.get('time_str', '')} {dt.get('offset_str', '')}")
                if dt.get('utc_str'):
                    lines.append(f"  Station UTC= {dt['utc_str']}")
            else:
                lines.append(f"  Date/Time  = (none)")

            # ========= RADIOTEXT =========
            lines.append(f"\n  --- Radiotext ---")
            lines.append(f"  RT         = {clean_rt or '(none)'}")
            lines.append(f"  RT A/B     = {'B' if data.get('rt_ab') else 'A'}" if data.get("rt_ab") is not None else f"  RT A/B     = (none)")
            rt_segs = data.get("rt_segments", {})
            lines.append(f"  RT chars   = {len(rt_segs)}/64 received")
            # Show which character positions are filled
            if rt_segs:
                filled = sorted(rt_segs.keys())
                ranges = []
                start = filled[0]
                end = filled[0]
                for p in filled[1:]:
                    if p == end + 1:
                        end = p
                    else:
                        ranges.append(f"{start}-{end}" if start != end else f"{start}")
                        start = p
                        end = p
                ranges.append(f"{start}-{end}" if start != end else f"{start}")
                lines.append(f"  RT filled  = {', '.join(ranges)}")

            # ========= RT+ (RadioText Plus) =========
            lines.append(f"\n  --- RT+ (RadioText Plus) ---")
            rtp_active = data.get("rtp_active", False)
            rtp_group_type = data.get("rtp_group_type")
            lines.append(f"  RT+ Active = {'Yes' if rtp_active else 'No'}")
            if rtp_group_type is not None:
                carrier_ver = 'A' if data.get("rtp_group_version", 0) == 0 else 'B'
                lines.append(f"  Carrier    = {rtp_group_type}{carrier_ver}")
            rtp_items = data.get("rtp_items", [])
            rtp_markers = data.get("rtp_markers", {})
            tagged = rtp_fields(data)
            if tagged:
                lines.append("  Fields:")
                for itype, name, text in tagged:
                    label = RTP_FIELD_LABELS.get(itype, name)
                    lines.append(f"    {label:<16}= {text}")
            if rtp_items:
                lines.append("  Raw tags:")
                for item in rtp_items:
                    itype = item.get("type", 0)
                    tname = item.get("type_name", "Unknown")
                    toggle = item.get("toggle", 0)
                    running = item.get("running", 0)
                    marker = rtp_markers.get(itype, {})
                    start = marker.get("start", "?")
                    length = marker.get("length", "?")
                    item_text = rtp_marker_text(clean_rt, marker) or "?"
                    lines.append(f"    Type 0x{itype:02X} ({tname}):")
                    lines.append(f"      Text    = {item_text}")
                    lines.append(f"      Start   = {start}")
                    lines.append(f"      Length  = {length}")
                    lines.append(f"      Toggle  = {toggle}")
                    lines.append(f"      Running = {running}")
            else:
                lines.append(f"  (no RT+ items decoded)")

            # ========= TMC =========
            lines.append(f"\n  --- TMC (Traffic Message Channel) ---")
            tmc_state = data.get("tmc")
            if tmc_state:
                sysinfo = tmc_state.get("sys") or {}
                if sysinfo.get("variant") == 0:
                    scope = ', '.join(n for n, k in (("international", "international"),
                                                     ("national", "national"),
                                                     ("regional", "regional"),
                                                     ("urban", "urban")) if sysinfo.get(k))
                    lines.append(f"  Location table = {sysinfo.get('ltn')}"
                                 f"  AFI = {'Yes' if sysinfo.get('afi') else 'No'}"
                                 f"  Mode = {sysinfo.get('mode')}"
                                 f"  Scope = {scope or '-'}")
                lines.append(f"  8A groups      = {tmc_state.get('groups', 0)}"
                             f"  (tuning info: {tmc_state.get('tuning', 0)})")
                msgs = tmc_messages(data)
                lines.append(f"  Messages       = {len(msgs)}")
                for m in msgs:
                    lines.append(f"    [{m['event']:4d}] x{m['count']:<3d} {tmc.format_message(m)}")
                lines.append("  (location codes need the provider's location table; shown raw)")
            else:
                lines.append(f"  (no TMC groups decoded)")

            # ========= ODA: decoded / announced / raw =========
            oda_aids = data.get("oda_aids", [])
            announced_groups = {f"{gt}{'A' if ver == 0 else 'B'}"
                                for _aid, gt, ver in oda_aids}

            lines.append(f"\n  --- Decoded ODA applications ---")
            decoded_any = False
            if data.get("rtp_items"):
                decoded_any = True
                src = data.get("rtp_decoded_from") or "?"
                lines.append(f"  RT+ (RadioText Plus) -- decoded from {src}")
                for itype, name, text in rtp_fields(data):
                    lines.append(f"    {RTP_FIELD_LABELS.get(itype, name)}: \"{text}\"")
                lines.append(f"    Status: payload decoded ({len(data['rtp_items'])} tag types)")
            tmc_state = data.get("tmc") or {}
            if tmc_state.get("messages"):
                decoded_any = True
                lines.append(f"  RDS-TMC (ALERT-C) -- decoded from 8A")
                lines.append(f"    Messages: {len(tmc_state['messages'])}  "
                             f"({tmc_state.get('groups', 0)} groups)")
                lines.append(f"    Status: payload decoded")
            if not decoded_any:
                lines.append(f"  (none)")

            lines.append(f"\n  --- Announced ODA registrations (3A) ---")
            if oda_aids:
                for aid, app_gt, app_ver in oda_aids:
                    group = f"{app_gt}{'A' if app_ver == 0 else 'B'}"
                    lines.append(f"  0x{aid:04X} -> {group} -- {oda_app_name(aid)}")
                    lines.append(f"    Status: announced")
            else:
                lines.append(f"  (none)")

            lines.append(f"\n  --- Raw ODA-capable groups (no payload decoder) ---")
            oda_raw = data.get("oda_raw", {})
            if oda_raw:
                for gt_name in sorted(oda_raw, key=lambda g: (int(g[:-1]), g[-1])):
                    raw = oda_raw[gt_name]
                    note = " (announced in 3A)" if gt_name in announced_groups else ""
                    lines.append(f"  {gt_name}: {raw['count']} groups{note}")
                    for smp in raw["samples"]:
                        lines.append(f"    raw: {smp}")
            else:
                lines.append(f"  (none)")

            # ========= PS SEGMENT DETAIL =========
            lines.append(f"\n  --- PS Segment Detail ---")
            ps_segs = data.get("ps_segments", {})
            if ps_segs:
                for seg in sorted(ps_segs.keys()):
                    ch1, ch2 = ps_segs[seg]
                    ch1_disp = ch1 if ch1.isprintable() or ch1 == ' ' else f'0x{ord(ch1):02X}'
                    ch2_disp = ch2 if ch2.isprintable() or ch2 == ' ' else f'0x{ord(ch2):02X}'
                    lines.append(f"  Seg {seg}: [{ch1_disp}][{ch2_disp}]")
            else:
                lines.append(f"  (none received)")

            # ========= PTYN SEGMENT DETAIL =========
            lines.append(f"\n  --- PTYN Segment Detail ---")
            ptyn_segs = data.get("ptyn_segments", {})
            if ptyn_segs:
                for seg in sorted(ptyn_segs.keys()):
                    cells = "".join(
                        f"[{ch if ch.isprintable() or ch == ' ' else f'0x{ord(ch):02X}'}]"
                        for ch in ptyn_segs[seg])
                    lines.append(f"  Seg {seg} (chars {seg * 4 + 1}-{seg * 4 + 4}): {cells}")
            else:
                lines.append(f"  (none received)")

            # ========= DECODE QUALITY =========
            lines.append(f"\n  --- Decode Quality ---")
            lines.append(f"  Blocks     = {good_blocks}/{total_blocks} good ({block_pct})")
            lines.append(f"  Usable info= {usable_info_score(data)}% (signal quality & confidence)")
            lines.append(f"  Bad blocks = {bad_blocks}")
            lines.append(f"  PS segs    = {len(ps_segs)}/4")
            lines.append(f"  RT chars   = {len(rt_segs)}/64")
            lines.append(f"  PTYN segs  = {len(ptyn_segs)}/2")
            lines.append(f"  Last seen  = line {data.get('last_seen', '?')}")

            # ========= GROUP STATISTICS =========
            all_types = [f"{i}{v}" for i in range(16) for v in ("A", "B")]
            total = sum(gc.values()) if gc else 0
            lines.append(f"\n  --- Group Statistics ({total} total groups) ---")
            for gt in all_types:
                cnt = gc.get(gt, 0)
                pct = (cnt / total * 100) if total else 0
                lines.append(f"    {gt:>3s} = {pct:5.1f}%  ({cnt})")

        lines.append("")
        lines.append(sep)
        lines.append(f"Total stations in report: {len(report_stations)}")
        lines.append(sep)

        if to_text:
            return "\n".join(lines)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        messagebox.showinfo("Report", f"Report saved to {path}\n\n{len(report_stations)} station(s) included.")

    def _toggle_confidence():
        if show_confidence.get():
            conf_frame.grid()
        else:
            conf_frame.grid_remove()

    def _toggle_rtplus():
        if show_rtplus.get():
            rtp_label.grid()
        else:
            rtp_label.grid_remove()

    def _toggle_badges():
        if show_badges.get():
            tp_label.pack(side=tk.LEFT, padx=2)
            ta_label.pack(side=tk.LEFT, padx=2)
            ms_label.pack(side=tk.LEFT, padx=2)
        else:
            tp_label.pack_forget()
            ta_label.pack_forget()
            ms_label.pack_forget()

    def _toggle_logos():
        if show_logos.get():
            logo_label.grid()
        else:
            logo_label.grid_remove()

    def _toggle_dark_mode():
        if dark_mode.get():
            root.configure(bg="#1a1a2e")
            display.configure(bg="#16213e")
        else:
            root.configure(bg="#f0f0f0")
            display.configure(bg="#ffffff")

    def _download_missing_logos():
        if not stations_data[0]:
            status_var.set("No stations loaded")
            return
        for pi, data in stations_data[0].items():
            cs = data.get("callsign")
            if cs and cs not in downloaded_callsigns:
                existing = find_existing_logo(cs, logo_dir)
                if not existing:
                    downloaded_callsigns.add(cs)
                    root.after(100, lambda c=cs: _download_and_show(c))
        status_var.set("Downloading missing logos...")

    def _manual_logo_lookup():
        from tkinter import simpledialog, messagebox
        cs = simpledialog.askstring("Manual Logo Lookup",
                                    "Enter callsign:")
        if cs:
            cs = cs.upper().strip()
            existing = find_existing_logo(cs, logo_dir)
            if existing:
                logo_path_ref[0] = str(existing)
                _load_logo(str(existing))
                messagebox.showinfo("Logo", f"Found logo for {cs}")
            else:
                result = messagebox.askyesno("Logo",
                    f"No logo found for {cs}. Download from Wikipedia / Facebook?")
                if result:
                    local = download_logo_for(cs, logo_dir)
                    if local:
                        logo_path_ref[0] = str(local)
                        _load_logo(str(local))
                        messagebox.showinfo("Logo", f"Downloaded logo for {cs}")
                        return
                    page = simpledialog.askstring(
                        "Facebook Logo",
                        f"No logo found for {cs}.\n"
                        "Enter the station's Facebook page (URL or name), "
                        "or leave blank to skip:",
                        parent=root)
                    if page and page.strip():
                        local_png = Path(logo_dir) / f"{cs}.png"
                        if download_facebook_logo(page, local_png):
                            logo_path_ref[0] = str(local_png)
                            _load_logo(str(local_png))
                            messagebox.showinfo("Logo", f"Downloaded Facebook logo for {cs}")
                            return
                    messagebox.showwarning("Logo", f"No logo found for {cs}")

    def _clear_spy_data():
        from tkinter import messagebox
        result = messagebox.askyesno("Clear Data",
            "Clear all decoded station data? The spy file will not be modified.")
        if result:
            stations_data[0] = {}
            session_stations[0] = {}
            session_start_size[0] = 0
            session_spy_lines.clear()
            session_pis.clear()
            if session_count_var:
                session_count_var.set("Session: 0")
            _zero_lifetime_counter(set())
            last_mtime[0] = 0
            last_size[0] = 0
            main_last_byte[0] = 0
            status_var.set("Station data cleared")

    def _reset_session_counter():
        from tkinter import messagebox
        result = messagebox.askyesno("Reset Session Counter",
            f"Reset the session unique station counter?\n"
            f"Current count: {len(session_pis)} stations\n"
            f"This only resets the counter — the lifetime count is unaffected.")
        if result:
            session_pis.clear()
            if session_count_var:
                session_count_var.set("Session: 0")
            status_var.set("Session station counter reset to 0")

    def _reset_station_counter():
        from tkinter import messagebox
        if mapper_station_count() is not None:
            messagebox.showinfo("Reset Lifetime Counter",
                "Lifetime now counts the unique PI codes in your current "
                "DX Mapper log (the first one ticked in Logs...), so there "
                "is nothing to reset here.\n\nIt goes up as stations are "
                "logged and down if you delete them in DX Mapper.",
                parent=root)
            return
        result = messagebox.askyesno("Reset Lifetime Counter",
            f"Reset the lifetime unique station counter?\n"
            f"Current count: {len(lifetime_pis)} stations\n"
            f"This will delete the station count file and start counting from zero.\n"
            f"The session counter is unaffected.")
        if result:
            # Stations already on screen must not re-enter the count on
            # the next refresh; only stations heard from now on do.
            _zero_lifetime_counter(set(stations_data[0].keys()))
            status_var.set("Lifetime station counter reset to 0")

    def _zero_lifetime_counter(already_seen):
        lifetime_pis.clear()
        lifetime_counted.clear()
        lifetime_counted.update(already_seen)
        try:
            Path(DEFAULT_STATION_COUNT_FILE).unlink()
        except Exception:
            pass
        if lifetime_count_var:
            lifetime_count_var.set(_lifetime_text())

    def _show_reference():
        from tkinter import messagebox
        ref = (
            "RDS REFERENCE\n\n"
            "PI - Program Identification (hex). NRSC-4-B: K=0x1000-0x54A7, W=0x54A8-0x994F\n\n"
            "PS - Program Service name (8 chars, type 0A)\n\n"
            "PTY (US RBDS): 0=None 1=News 2=Info 3=Sports 4=Talk 5=Rock\n"
            "  6=Classic Rock 7=Adult Hits 8=Soft Rock 9=Top 40 10=Country\n"
            "  11=Oldies 12=Soft 13=Nostalgia 14=Jazz 15=Classical\n"
            "  16=R&B 17=Soft R&B 18=Foreign 19=Religious Music\n"
            "  20=Religious Talk 21=Personality 22=Public 23=College\n"
            "  29=Weather 30=Emergency Test 31=Emergency\n\n"
            "TP - Traffic Program (blue)\n"
            "TA - Traffic Announcement (red)\n"
            "MUSIC/SPEECH - Content type (green/purple)\n\n"
            "RT - RadioText (type 2A/2B, up to 64 chars)\n"
            "RT+ - RadioText Plus (type 3A): Title/Artist tags\n"
            "PTYN - Program Type Name (type 10A, 8 chars)\n"
            "CT - Clock Time/Date (type 4A)\n\n"
            "Group Types:\n"
            "  0A: PS, PTY, TP, TA, Music/Speech\n"
            "  2A: RadioText (64 chars)\n"
            "  2B: RadioText (32 chars)\n"
            "  3A: RT+ item tags\n"
            "  4A: Clock Time/Date\n"
            "  10A: PTYN\n\n"
            "Logo cache: " + logo_dir + "\n"
            "Station names: DX Mapper's log, then " + DEFAULT_STATIONS_FILE + "\n"
            "Spy file: " + spy_file_var.get()
        )
        messagebox.showinfo("RDS Reference", ref)

    def _show_about():
        from tkinter import messagebox
        messagebox.showinfo("About RDS Monitor",
            "RDS Monitor v1.0\n\n"
            "Real-time RDS Spy file decoder with station logos,\n"
            "RT+ song title/artist extraction, TP/TA/Music-Speech\n"
            "indicators, PTYN, clock time, and signal quality.\n\n"
            "PI codes decoded via NRSC-4-B algorithm with\n"
            "user-defined overrides.\n\n"
            "Logo sources: Wikipedia (pageimages API)\n"
            "Logo format: PNG\n\n"
            "Ideas or problems? Email j.rds.monitor@gmail.com")

    # --- Input source functions ---
    def _switch_to_spy():
        """Switch to spy file input mode."""
        if rftap_input:
            rftap_input.stop()
        if tuner_link:
            tuner_link.stop()
        _reset_tmc("input changed")
        tuner_bar.grid_remove()
        tuner_freq_var.set("--.- MHz")
        input_mode.set("spy")
        status_var.set("Input: Spy File")
        last_mtime[0] = 0
        last_size[0] = 0
        rftap_status_var.set("RFtap: \u25cb Disabled")
        try:
            rftap_status_label.config(fg="#888")
        except Exception:
            pass

    def _switch_to_tuner():
        """Take RDS and status from the standalone FM Tuner over localhost TCP."""
        nonlocal rftap_input, tuner_link

        if rftap_input:
            rftap_input.stop()
        _reset_tmc("input changed")

        rftap_input = RFtapInput()
        rftap_input.transport = "tcp"
        rftap_input.host = TUNER_HOST
        rftap_input.port = TUNER_RDS_PORT
        rftap_input.data_format = "ascii_g"
        rftap_input.enabled = True
        rftap_input.start()

        if tuner_link is None:
            tuner_link = TunerLink()
        tuner_link.start()

        input_mode.set("tuner")
        tuner_bar.grid()
        status_var.set(
            f"FM Tuner: {TUNER_HOST}:{TUNER_RDS_PORT} \u2014 waiting for data")

    def _switch_to_rftap():
        """Switch to RFtap/RDS input mode (RDS data from SDR# RDSOutput plugin)."""
        nonlocal rftap_input
        from tkinter import simpledialog, messagebox

        # Show settings dialog
        dlg = tk.Toplevel(root)
        dlg.title("RFtap / RDS Input Settings")
        dlg.configure(bg="#0f3460")
        dlg.transient(root)
        dlg.grab_set()

        tk.Label(dlg, text="RDS Input from SDR#",
                 fg="#e94560", bg="#0f3460",
                 font=("Segoe UI", 12, "bold")).pack(padx=12, pady=(12, 8))

        tk.Label(dlg, text="Receives RDS data from SDR# RDSOutput plugin\nor binary RFtap from GNU Radio.",
                 fg="#aaa", bg="#0f3460",
                 font=("Segoe UI", 9)).pack(padx=12, pady=(0, 8))

        frame = tk.Frame(dlg, bg="#0f3460")
        frame.pack(padx=12, pady=4)

        # Transport
        tk.Label(frame, text="Transport:", fg="#ccc", bg="#0f3460",
                 font=("Segoe UI", 10)).grid(row=0, column=0, sticky="e", pady=4)
        transport_var = tk.StringVar(value="tcp")
        tk.Radiobutton(frame, text="TCP", variable=transport_var, value="tcp",
                      bg="#0f3460", fg="#ccc", selectcolor="#0f3460",
                      activebackground="#0f3460", activeforeground="#ccc"
                      ).grid(row=0, column=1, sticky="w", pady=4)
        tk.Radiobutton(frame, text="UDP", variable=transport_var, value="udp",
                      bg="#0f3460", fg="#ccc", selectcolor="#0f3460",
                      activebackground="#0f3460", activeforeground="#ccc"
                      ).grid(row=0, column=2, sticky="w", pady=4)

        # Host
        tk.Label(frame, text="Host:", fg="#ccc", bg="#0f3460",
                 font=("Segoe UI", 10)).grid(row=1, column=0, sticky="e", pady=4)
        host_var = tk.StringVar(value="127.0.0.1")
        tk.Entry(frame, textvariable=host_var, width=20,
                font=("Consolas", 10)).grid(row=1, column=1, columnspan=2, sticky="w", pady=4)

        # Port
        tk.Label(frame, text="Port:", fg="#ccc", bg="#0f3460",
                 font=("Segoe UI", 10)).grid(row=2, column=0, sticky="e", pady=4)
        port_var = tk.StringVar(value=str(TUNER_RDS_PORT))
        tk.Entry(frame, textvariable=port_var, width=10,
                font=("Consolas", 10)).grid(row=2, column=1, columnspan=2, sticky="w", pady=4)
        tk.Label(frame, text="(FM Tuner: 7376 or 23 \u2014 SDR# RDSOutput: 23)",
                 fg="#888", bg="#0f3460",
                 font=("Segoe UI", 8)).grid(row=3, column=1, columnspan=2, sticky="w")

        # Format
        tk.Label(frame, text="Format:", fg="#ccc", bg="#0f3460",
                 font=("Segoe UI", 10)).grid(row=4, column=0, sticky="e", pady=4)
        format_var = tk.StringVar(value="auto")
        tk.Radiobutton(frame, text="Auto-detect", variable=format_var, value="auto",
                      bg="#0f3460", fg="#ccc", selectcolor="#0f3460",
                      activebackground="#0f3460", activeforeground="#ccc"
                      ).grid(row=4, column=1, sticky="w", pady=4)
        tk.Radiobutton(frame, text="ASCII G Protocol", variable=format_var, value="ascii_g",
                      bg="#0f3460", fg="#ccc", selectcolor="#0f3460",
                      activebackground="#0f3460", activeforeground="#ccc"
                      ).grid(row=5, column=1, sticky="w")
        tk.Radiobutton(frame, text="Binary RFtap", variable=format_var, value="rftap",
                      bg="#0f3460", fg="#ccc", selectcolor="#0f3460",
                      activebackground="#0f3460", activeforeground="#ccc"
                      ).grid(row=6, column=1, sticky="w")

        # Buttons
        btn_frame = tk.Frame(dlg, bg="#0f3460")
        btn_frame.pack(padx=12, pady=(8, 12))

        def _start_rftap():
            nonlocal rftap_input
            try:
                port = int(port_var.get().strip())
                if port < 1 or port > 65535:
                    raise ValueError
            except ValueError:
                messagebox.showerror("RFtap", "Port must be a number 1-65535")
                return

            dlg.destroy()

            # Stop any existing RFtap input
            if rftap_input:
                rftap_input.stop()
            _reset_tmc("input changed")

            # Create and start new RFtap input
            rftap_input = RFtapInput()
            rftap_input.transport = transport_var.get()
            rftap_input.host = host_var.get().strip() or "127.0.0.1"
            rftap_input.port = port
            rftap_input.data_format = format_var.get()
            rftap_input.enabled = True
            rftap_input.start()

            input_mode.set("rftap")
            if tuner_link:
                tuner_link.stop()
            tuner_bar.grid_remove()
            tuner_freq_var.set("--.- MHz")
            status_var.set(f"RFtap: {transport_var.get().upper()} {host_var.get()}:{port} \u2014 waiting for data")

        def _cancel_rftap():
            dlg.destroy()
            input_mode.set("spy")

        tk.Button(btn_frame, text="Start RFtap", command=_start_rftap,
                 bg="#1e8449", fg="white", font=("Segoe UI", 10, "bold"),
                 padx=12, pady=4).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_frame, text="Cancel", command=_cancel_rftap,
                 bg="#555", fg="white", font=("Segoe UI", 10),
                 padx=12, pady=4).pack(side=tk.LEFT, padx=4)

        dlg.update_idletasks()
        dlg.geometry(f"+{(root.winfo_rootx() + 100)}+{(root.winfo_rooty() + 100)}")

    # Use grid so the content area expands with the window
    root.grid_rowconfigure(0, weight=0)  # header
    root.grid_rowconfigure(1, weight=0)  # tuner bar
    root.grid_rowconfigure(2, weight=1)  # content
    root.grid_columnconfigure(0, weight=1)

    # Load lifetime unique station count (persists across restarts)
    lifetime_pis = load_station_count()
    lifetime_counted = set()  # PIs already merged into (or excluded from) the count

    def _lifetime_text():
        """Unique PIs in DX Mapper's current log; without a log, every PI
        this Monitor has decoded (the old count)."""
        n = mapper_station_count()
        return f"Lifetime: {len(lifetime_pis) if n is None else n}"

    # --- Header bar ---
    header = tk.Frame(root, bg="#0f3460")
    header.grid(row=0, column=0, sticky="ew")
    header.grid_propagate(False)
    header.config(height=44)

    tk.Label(header, text="RDS Monitor", fg="#e94560", bg="#0f3460",
             font=("Segoe UI", 13, "bold")).pack(side=tk.LEFT, padx=14, pady=8)

    live_frame = tk.Frame(header, bg="#0f3460")
    live_frame.pack(side=tk.LEFT, padx=(0, 8))
    live_dot = tk.Label(live_frame, text="\u25cf", fg="#e94560", bg="#0f3460",
                        font=("Segoe UI", 10))
    live_dot.pack(side=tk.LEFT)
    tk.Label(live_frame, text="LIVE", fg="#e94560", bg="#0f3460",
             font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(2, 0))

    # Lifetime unique station counter
    lifetime_count_var = tk.StringVar(value=_lifetime_text())
    tk.Label(header, textvariable=lifetime_count_var, fg="#50fa7b", bg="#0f3460",
             font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(4, 0))

    # Session unique station counter (since program open, resettable)
    session_pis = set()
    session_count_var = tk.StringVar(value="Session: 0")
    tk.Label(header, textvariable=session_count_var, fg="#8be9fd", bg="#0f3460",
             font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(4, 0))

    status_var = tk.StringVar(value="Starting...")
    tk.Label(header, textvariable=status_var, fg="#888", bg="#0f3460",
             font=("Segoe UI", 9)).pack(side=tk.RIGHT, padx=14)

    # --- DX Mapper: Log button (manual) + optional auto-log ----------
    map_log_var = tk.StringVar(value="\U0001F4CD Log to Map")
    auto_map_sent = {}  # PI -> time last auto-logged
    auto_map_sig = {}  # PI -> decode_signature() at that auto-log
    hd_map_since = {}  # PI -> time HD lock was first seen
    hd_map_sent = {}  # PI -> time last auto-logged for its HD

    def _map_log_done(label, reply, error, auto):
        if error is not None:
            msg = (f"DX Mapper: not running on port {DX_MAP_PORT} "
                   f"({error})")
            status_var.set(msg)
            if not auto:
                map_log_var.set("\u26A0 Map not running")
                messagebox.showwarning(
                    "Log to Map",
                    f"Could not reach the DX Mapper on "
                    f"{DX_MAP_HOST}:{DX_MAP_PORT}.\n\n"
                    f"Start 'DX Mapper' and press Log again.\n\n({error})")
        elif not reply.get("ok"):
            status_var.set(f"DX Mapper: {reply.get('error', 'not logged')}")
            if not auto:
                map_log_var.set("\u26A0 Not logged")
        else:
            what = "auto-logged" if auto else (
                "new station logged" if reply.get("new") else "logged again")
            status_var.set(f"DX Mapper: {label} {what}")
            if not auto:
                map_log_var.set("\u2714 Logged")
        if not auto:
            root.after(2500, lambda: map_log_var.set("\U0001F4CD Log to Map"))

    map_now_sent = {}
    map_now_busy = [False]

    def _send_now_hearing(pi, data):
        """Tell the DX Mapper what is being decoded (shown there, not logged)."""
        if map_now_busy[0]:
            return
        callsign = data.get("callsign") or ""
        geo = lookup_station_geo._cache.get(callsign.upper()) if callsign else None
        record = build_map_log(pi, data, geo, kind="now",
                               rx=home_location[0] + (location_label[0],))
        slogan = station_slogan(callsign) if callsign else None
        if slogan:
            record["slogan"] = slogan  # for the FM Tuner's remote page
        map_now_busy[0] = True

        def _worker():
            try:
                send_to_map(record, timeout=1.0)
            except (OSError, ValueError):
                pass  # DX Mapper not running - nothing to show
            finally:
                map_now_busy[0] = False

        threading.Thread(target=_worker, daemon=True).start()

    def _log_to_map(auto=False, hd=False):
        """Push the station on screen to the DX Mapper (port 7375)."""
        pi = current_pi[0]
        data = stations_data[0].get(pi) if pi else None
        if not data:
            if not auto:
                messagebox.showinfo("Log to Map", "No station decoded yet.")
            return
        callsign = data.get("callsign") or ""
        geo = lookup_station_geo._cache.get(callsign.upper()) if callsign else None
        record = build_map_log(pi, data, geo, kind="auto" if auto else "log",
                               rx=home_location[0] + (location_label[0],))
        try:
            report = _export_data(current_only=True, to_text=True)
            if report:
                record["report"] = report  # DX Mapper reads the details out of it
        except Exception as e:
            print(f"[!] report for DX Mapper: {e}")
        label = f"{callsign or pi}" + (
            f" {data['freq_mhz']:.1f}" if data.get("freq_mhz") else "")
        if auto:
            auto_map_sent[pi.upper()] = time.time()
            auto_map_sig[pi.upper()] = decode_signature(data)
            if hd:
                hd_map_sent[pi.upper()] = time.time()
                label += " (HD)"
        else:
            map_log_var.set("\u2026 Logging")

        def _worker():
            reply = error = None
            try:
                reply = send_to_map(record)
            except OSError as e:
                error = e.strerror or str(e) or type(e).__name__
            root.after(0, lambda: _map_log_done(label, reply, error, auto))

        threading.Thread(target=_worker, daemon=True).start()

    root.bind_all("<Control-l>", lambda e: _log_to_map())
    root.bind_all("<Control-L>", lambda e: _log_to_map())

    # RFtap status indicator
    rftap_status_label = tk.Label(header, textvariable=rftap_status_var,
                                   fg="#888", bg="#0f3460",
                                   font=("Segoe UI", 9, "bold"))
    rftap_status_label.pack(side=tk.RIGHT, padx=(0, 4))

    # --- FM Tuner control bar (visible in tuner mode) ---
    tuner_bar = tk.Frame(root, bg="#0d1b2a")
    tuner_bar.grid(row=1, column=0, sticky="ew")
    tuner_bar.grid_remove()  # hidden until tuner mode is selected

    tk.Label(tuner_bar, text="Tuned", fg="#888", bg="#0d1b2a",
             font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(14, 4), pady=3)
    tuner_freq_label = tk.Label(tuner_bar, textvariable=tuner_freq_var, fg="#50fa7b",
                                bg="#0d1b2a", font=("Consolas", 16, "bold"))
    tuner_freq_label.pack(side=tk.LEFT, padx=(0, 12), pady=3)

    tuner_state_var = tk.StringVar(value="tuner: not connected")
    tk.Label(tuner_bar, textvariable=tuner_state_var, fg="#8be9fd", bg="#0d1b2a",
             font=("Segoe UI", 9)).pack(side=tk.RIGHT, padx=14, pady=3)

    tuner_entry_var = tk.StringVar(value="")

    def _tuner_command(command):
        """Send a command to the tuner and report the outcome."""
        if tuner_link is None:
            status_var.set("Tuner: not connected (Input > FM Tuner)")
            return None
        reply = tuner_link.send_command(command)
        if reply is None:
            status_var.set(f"Tuner: no reply to '{command}' "
                           f"(is FM_Tuner.py running?)")
        elif not reply.get("ok", False):
            status_var.set(f"Tuner: {reply.get('error', 'command failed')}")
        else:
            status_var.set(f"Tuner: {command}")
        return reply

    def _tuner_tune():
        text = tuner_entry_var.get().strip()
        if not text:
            return
        try:
            freq_mhz = float(text)
        except ValueError:
            status_var.set("Tuner: enter a frequency like 101.1")
            return
        _tuner_command(f"TUNE {freq_mhz:g}")

    tk.Entry(tuner_bar, textvariable=tuner_entry_var, width=7,
             font=("Consolas", 10), justify="center").pack(side=tk.LEFT, padx=2, pady=3)
    tk.Label(tuner_bar, text="MHz", fg="#888", bg="#0d1b2a",
             font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 6), pady=3)

    def _tuner_wheel(event):
        """Mouse wheel over the Tuned frequency: one channel (region step) per notch."""
        if getattr(event, "num", None) in (4, 5):
            up = event.num == 4
        elif event.delta:
            up = event.delta > 0
        else:
            return "break"
        _tuner_command("UP" if up else "DOWN")
        return "break"

    for _seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        tuner_freq_label.bind(_seq, _tuner_wheel)

    for _label, _cmd in (("Tune", None), ("\u25bc", "DOWN"), ("\u25b2", "UP"),
                         ("\u25bc Seek", "SEEK DOWN"), ("\u25b2 Seek", "SEEK UP"),
                         ("Stop", "STOP"), ("Start", "START")):
        tk.Button(tuner_bar, text=_label,
                  command=(_tuner_tune if _cmd is None
                           else (lambda c=_cmd: _tuner_command(c))),
                  bg="#e94560" if _cmd is None else "#1a5276", fg="white",
                  font=("Segoe UI", 9, "bold"), relief=tk.FLAT, padx=8,
                  cursor="hand2").pack(side=tk.LEFT, padx=3, pady=3)

    tuner_audio_var = tk.BooleanVar(value=True)
    tk.Checkbutton(tuner_bar, text="Audio", variable=tuner_audio_var,
                   command=lambda: _tuner_command(
                       "AUDIO ON" if tuner_audio_var.get() else "AUDIO OFF"),
                   bg="#0d1b2a", fg="white", font=("Segoe UI", 9, "bold"),
                   selectcolor="#1a5276", activebackground="#0d1b2a",
                   activeforeground="white", cursor="hand2").pack(side=tk.LEFT,
                                                                  padx=(10, 2), pady=3)

    tuner_bias_var = tk.BooleanVar(value=False)

    def _tuner_bias():
        """Ask the tuner to switch its 4.5 V antenna bias-tee on or off."""
        on = tuner_bias_var.get()
        if on and not messagebox.askokcancel(
                "Bias-T",
                "Switch on 4.5 V DC on the tuner's antenna port?\n\n"
                "Only with a powered LNA or active antenna connected."):
            tuner_bias_var.set(False)
            return
        reply = _tuner_command("BIAST ON" if on else "BIAST OFF")
        if reply is None or not reply.get("ok"):
            tuner_bias_var.set(False)
            if reply is not None:
                messagebox.showerror("Bias-T", str(reply.get("error", reply)))

    tk.Checkbutton(tuner_bar, text="Bias-T", variable=tuner_bias_var,
                   command=_tuner_bias,
                   bg="#0d1b2a", fg="white", font=("Segoe UI", 9, "bold"),
                   selectcolor="#1a5276", activebackground="#0d1b2a",
                   activeforeground="white", cursor="hand2").pack(side=tk.LEFT,
                                                                  padx=(6, 2), pady=3)

    # --- Dock to the FM Tuner window -------------------------------------
    # The tuner publishes its window rectangle in telemetry ("window"); when
    # docked, this window snaps to that edge and follows the tuner around.
    DOCK_CHOICES = (("none", "Undocked"), ("right", "Right of tuner"),
                    ("left", "Left of tuner"), ("below", "Below tuner"))
    dock_mode = tk.StringVar(value="none")
    dock_last_target = [None]
    dock_free_size = [None]   # window size before docking, restored after

    def _outer_frame():
        """(left/right border, title-bar height) of this window's WM frame.

        geometry() reports the outer frame corner, winfo_rootx/y the client
        corner; the difference is the border / title bar.
        """
        m = re.search(r"([+-]-?\d+)([+-]-?\d+)$", root.geometry())
        if not m:
            return 0, 0
        gx = int(m.group(1).lstrip("+"))
        gy = int(m.group(2).lstrip("+"))
        return (max(0, root.winfo_rootx() - gx),
                max(0, root.winfo_rooty() - gy))

    def _dock_follow():
        mode = dock_mode.get()
        rect = None
        if mode != "none" and tuner_link is not None and tuner_link.telemetry:
            rect = tuner_link.telemetry.get("window")
        if not rect:
            dock_last_target[0] = None
            root.after(300, _dock_follow)
            return
        try:
            fx, fy = _outer_frame()
            tx, ty = int(rect["x"]), int(rect["y"])
            tw, th = int(rect["width"]), int(rect["height"])
            sw = int(rect.get("screen_width") or root.winfo_screenwidth())
            sh = int(rect.get("screen_height") or root.winfo_screenheight())
            min_w, min_h = root.minsize()
            my_w, my_h = dock_free_size[0] or (root.winfo_width(),
                                               root.winfo_height())
            if mode == "below":
                # Same width as the tuner, in the space left under it.
                w = max(min_w, tw - 2 * fx)
                h = max(min_h, min(my_h, sh - (ty + th) - fy - fx))
                x, y = tx, ty + th
            else:
                # Same height as the tuner; shrink to the room on that side.
                h = max(min_h, th - fy - fx)
                room = (sw - (tx + tw)) if mode == "right" else tx
                w = max(min_w, min(my_w, room - 2 * fx))
                if mode == "right":
                    x, y = tx + tw, ty
                else:
                    x, y = tx - (w + 2 * fx), ty
            x = max(0, min(x, sw - (w + 2 * fx)))
            y = max(0, min(y, sh - (h + fy + fx)))
            target = (w, h, x, y)
            if target != dock_last_target[0]:
                root.geometry(f"{w}x{h}+{x}+{y}")
                dock_last_target[0] = target
        except (tk.TclError, KeyError, TypeError, ValueError):
            pass
        root.after(300, _dock_follow)

    def _set_dock(mode):
        dock_mode.set(mode)
        dock_last_target[0] = None
        if mode == "none":
            if dock_free_size[0]:
                root.geometry("{}x{}".format(*dock_free_size[0]))
            dock_free_size[0] = None
        elif dock_free_size[0] is None:
            dock_free_size[0] = (root.winfo_width(), root.winfo_height())
        if mode != "none" and (tuner_link is None or not tuner_link.telemetry
                               or not tuner_link.telemetry.get("window")):
            status_var.set("Dock: waiting for the FM Tuner window "
                           "(start FM_Tuner.py, Input > FM Tuner)")
        dock_button.config(
            text="Dock \u25be" if mode == "none" else f"Docked {mode} \u25be",
            bg="#1a5276" if mode == "none" else "#1e8449")

    dock_menu = tk.Menu(root, tearoff=0, bg="#0f3460", fg="#ccc",
                        activebackground="#1a5276", activeforeground="#fff")
    for _mode, _label in DOCK_CHOICES:
        dock_menu.add_radiobutton(label=_label, variable=dock_mode, value=_mode,
                                  command=lambda m=_mode: _set_dock(m))
    dock_button = tk.Menubutton(tuner_bar, text="Dock \u25be", menu=dock_menu,
                                bg="#1a5276", fg="white", relief=tk.FLAT,
                                font=("Segoe UI", 9, "bold"), padx=8,
                                activebackground="#2874a6",
                                activeforeground="white", cursor="hand2")
    dock_button.pack(side=tk.LEFT, padx=(10, 2), pady=3)
    view_menu.add_separator()
    view_menu.add_cascade(label="Dock to FM Tuner", menu=dock_menu)
    root.after(1500, _dock_follow)

    # --- Single station display (fills the window) ---
    display = tk.Frame(root, bg="#16213e")
    display.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)
    # Wide (16:9) layout: three columns side by side, confidence strip below.
    #   left   = logo, callsign, city/state, station clock
    #   centre = PI/PTY, PS, now playing + cover art, RadioText
    #   right  = RT+ fields, TMC traffic
    display.grid_rowconfigure(0, weight=1)
    display.grid_rowconfigure(1, weight=0)  # Confidence
    display.grid_columnconfigure(0, weight=2, uniform="cols")
    display.grid_columnconfigure(1, weight=3, uniform="cols")
    display.grid_columnconfigure(2, weight=3, uniform="cols")

    left_col = tk.Frame(display, bg="#16213e")
    left_col.grid(row=0, column=0, sticky="nsew", padx=(4, 8))
    left_col.grid_columnconfigure(0, weight=1)
    mid_col = tk.Frame(display, bg="#16213e")
    mid_col.grid(row=0, column=1, sticky="nsew", padx=8)
    mid_col.grid_columnconfigure(0, weight=1)
    mid_col.grid_rowconfigure(3, weight=1)  # RT expands
    right_col = tk.Frame(display, bg="#16213e")
    right_col.grid(row=0, column=2, sticky="nsew", padx=(8, 4))
    right_col.grid_columnconfigure(0, weight=1)
    right_col.grid_rowconfigure(2, weight=1)

    # Logo — large, centered in the left column
    logo_label = tk.Label(left_col, bg="#0f3460", text="No Logo", fg="#555",
                          font=("Segoe UI", 10))
    logo_label.grid(row=0, column=0, pady=(12, 8))

    # Callsign — big bold text
    callsign_frame = tk.Frame(left_col, bg="#16213e")
    callsign_frame.grid(row=1, column=0, pady=(0, 2))
    callsign_var = tk.StringVar(value="---")
    call_lbl = tk.Label(callsign_frame, textvariable=callsign_var, fg="#e94560",
                        bg="#16213e", font=("Segoe UI", 28, "bold"))
    call_lbl.pack()
    # Slogan / branding from stations.txt — under the callsign, only if known
    slogan_var = tk.StringVar(value="")
    slogan_lbl = tk.Label(callsign_frame, textvariable=slogan_var, fg="#ffd27f",
                          bg="#16213e", font=("Segoe UI", 13, "italic"))
    # Location (city, state) — under callsign
    location_var = tk.StringVar(value="")
    tk.Label(callsign_frame, textvariable=location_var, fg="#888", bg="#16213e",
             font=("Segoe UI", 12)).pack()
    # Station clock (4A CT) — under the location
    clock_var = tk.StringVar(value="")
    tk.Label(callsign_frame, textvariable=clock_var, fg="#f1fa8c", bg="#16213e",
             font=("Segoe UI", 11)).pack()
    # Distance / compass bearing from home to the transmitter
    range_var = tk.StringVar(value="")
    tk.Label(callsign_frame, textvariable=range_var, fg="#50fa7b", bg="#16213e",
             font=("Segoe UI", 12, "bold")).pack()
    tk.Button(callsign_frame, textvariable=map_log_var,
              command=lambda: _log_to_map(), bg="#e94560", fg="white",
              activebackground="#ff6b81", activeforeground="white",
              font=("Segoe UI", 10, "bold"), relief=tk.FLAT, padx=12,
              cursor="hand2").pack(pady=(4, 2))
    # Alternative frequencies (0A) — under the clock, hidden when none
    af_var = tk.StringVar(value="")
    af_label = tk.Label(callsign_frame, textvariable=af_var, fg="#8be9fd",
                        bg="#16213e", font=("Segoe UI", 10), justify="center",
                        wraplength=300)
    af_label.pack()
    eon_var = tk.StringVar(value="")
    eon_label = tk.Label(callsign_frame, textvariable=eon_var, fg="#8be9fd",
                         bg="#16213e", font=("Segoe UI", 9), justify="center",
                         wraplength=300)
    eon_label.pack()

    # PI / PTY info + indicator badges
    info_frame = tk.Frame(mid_col, bg="#16213e")
    info_frame.grid(row=0, column=0, pady=(12, 8), sticky="ew")
    info_frame.grid_columnconfigure(0, weight=1)

    info_var = tk.StringVar(value="")
    tk.Label(info_frame, textvariable=info_var, fg="#888", bg="#16213e",
             font=("Segoe UI", 11)).grid(row=0, column=0, pady=0)

    # PS
    ps_var = tk.StringVar(value="")
    tk.Label(mid_col, textvariable=ps_var, fg="#ccc", bg="#16213e",
             font=("Consolas", 14, "bold")).grid(row=1, column=0, pady=2,
                                                   sticky="ew")

    # Now playing: cover art (memory-only) beside Title / Artist
    np_frame = tk.Frame(mid_col, bg="#16213e")
    np_frame.grid(row=2, column=0, pady=(8, 4), sticky="ew")
    np_frame.grid_columnconfigure(1, weight=1)
    ART_PX = 140
    art_label = tk.Label(np_frame, bg="#16213e", text="", fg="#555",
                         font=("Segoe UI", 8), width=0)
    art_label.grid(row=0, column=0, rowspan=2, padx=(8, 10), sticky="n")
    art_label.grid_remove()
    art_img_ref = [None]
    art_shown_key = [None]

    title_var = tk.StringVar(value="")
    tk.Label(np_frame, textvariable=title_var, fg="#ffb86c", bg="#16213e",
             font=("Segoe UI", 15, "bold"), anchor="w", justify="left",
             wraplength=340).grid(row=0, column=1, pady=(4, 0), sticky="sw")

    artist_var = tk.StringVar(value="")
    tk.Label(np_frame, textvariable=artist_var, fg="#bd93f9", bg="#16213e",
             font=("Segoe UI", 13), anchor="w", justify="left",
             wraplength=340).grid(row=1, column=1, pady=(0, 4), sticky="nw")

    # Search the song on the music services: on only while a song is known.
    music_frame = tk.Frame(np_frame, bg="#16213e")
    music_frame.grid(row=2, column=1, pady=(0, 4), sticky="w")
    music_btns = {}
    music_urls = [{}]
    # Each service's own colour while a song is showing, grey when not.
    music_colors = {"YTM": ("#ff0033", "#fff"), "SPOT": ("#1db954", "#000"),
                    "AMZ": ("#00a8e1", "#000"), "APL": ("#fa2d48", "#fff")}
    music_off = ("#2a2f45", "#556")

    def _open_music(short):
        url = music_urls[0].get(short)
        if url:
            webbrowser.open(url)

    for _short, _name, _url in nowplaying.MUSIC_SEARCH:
        _on_bg, _on_fg = music_colors.get(_short, ("#1f6feb", "#fff"))
        _b = tk.Button(music_frame, text=_short, state=tk.DISABLED,
                       font=("Segoe UI", 8, "bold"), bg=music_off[0],
                       fg=_on_fg, activebackground=_on_bg,
                       activeforeground=_on_fg,
                       disabledforeground=music_off[1], relief=tk.FLAT, padx=6,
                       pady=0, cursor="hand2",
                       command=lambda s=_short: _open_music(s))
        _b.pack(side=tk.LEFT, padx=(0, 4))
        music_btns[_short] = _b

    def _update_music_buttons(title, artist):
        music_urls[0] = nowplaying.music_search_urls(artist, title)
        state = tk.NORMAL if music_urls[0] else tk.DISABLED
        for _short, _b in music_btns.items():
            if str(_b.cget("state")) != state:
                _bg = (music_colors.get(_short, ("#1f6feb",))[0]
                       if state == tk.NORMAL else music_off[0])
                _b.config(state=state, bg=_bg)

    art_cache = nowplaying.ArtCache(
        on_update=lambda: root.after(0, _refresh_art_now))

    def _show_art(data, key):
        if not HAS_PIL:
            art_label.grid_remove()
            return
        try:
            img = nowplaying.image_from_bytes(data)
            img.thumbnail((ART_PX, ART_PX))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            art_label.grid_remove()
            return
        art_label.config(image=photo, text="")
        art_img_ref[0] = photo
        art_shown_key[0] = key
        art_label.grid()

    hd_art_now = [None]

    def _update_art(title, artist):
        """Show the station's HD album art while HD is locked, otherwise
        cached online art for the track (kicking off a fetch if new)."""
        hd_art = hd_art_now[0] if show_art.get() and HAS_PIL else None
        if hd_art:
            try:
                key = ("hd", hd_art, os.stat(hd_art).st_mtime_ns)
                if art_shown_key[0] == key and art_img_ref[0] is not None:
                    return
                _show_art(Path(hd_art).read_bytes(), key)
                return
            except OSError:
                pass
        if not (title or artist) or not show_art.get() or not HAS_PIL:
            art_label.grid_remove()
            art_label.config(image="")
            art_img_ref[0] = None
            art_shown_key[0] = None
            return
        key = nowplaying.track_key(artist, title)
        if art_shown_key[0] == key and art_img_ref[0] is not None:
            return
        data = art_cache.get(artist, title)
        if data:
            _show_art(data, key)
        else:
            art_label.config(image="", text="")
            art_img_ref[0] = None
            art_shown_key[0] = None
            art_label.grid_remove()

    def _refresh_art_now():
        _update_art(title_var.get(), artist_var.get())

    # RadioText — expands to fill available space
    rt_var = tk.StringVar(value="")
    rt_label = tk.Label(mid_col, textvariable=rt_var, fg="#8be9fd", bg="#16213e",
                        font=("Consolas", 12), anchor="n", justify="center",
                        wraplength=420)
    rt_label.grid(row=3, column=0, pady=(12, 4), sticky="nsew")

    # RT+ items — one labelled row per tagged field the station fills in
    rtp_label = tk.Frame(right_col, bg="#0d1b2a", relief=tk.SUNKEN, bd=1)
    rtp_label.grid(row=0, column=0, pady=(12, 4), padx=4, sticky="new")
    rtp_label.grid_remove()  # shown once a station tags RT+ fields
    rtp_label.grid_columnconfigure(1, weight=1)
    rtp_header_var = tk.StringVar(value="RT+ (RadioText Plus)")
    tk.Label(rtp_label, textvariable=rtp_header_var, fg="#5c7da0",
             bg="#0d1b2a", font=("Segoe UI", 8, "bold"), anchor="w"
             ).grid(row=0, column=0, columnspan=2, padx=8, pady=(6, 2),
                    sticky="w")
    rtp_rows = []  # (label widget, value widget) pairs, reused between refreshes
    rtp_var = tk.StringVar(value="")  # summary of the shown fields (for reports/tests)

    def _render_rtp_fields(fields):
        """Show each RT+ field on its own labelled row.

        `fields` is a list of (label, text, colour). Rows are created on
        demand and hidden when unused so the panel never flickers.
        """
        while len(rtp_rows) < len(fields):
            r = len(rtp_rows) + 1
            lab = tk.Label(rtp_label, text="", fg="#5c7da0", bg="#0d1b2a",
                           font=("Segoe UI", 9, "bold"), anchor="e", width=16)
            val = tk.Label(rtp_label, text="", fg="#50fa7b", bg="#0d1b2a",
                           font=("Segoe UI", 10), anchor="w", justify="left",
                           wraplength=260)
            lab.grid(row=r, column=0, padx=(8, 6), pady=1, sticky="ne")
            val.grid(row=r, column=1, padx=(0, 8), pady=1, sticky="w")
            rtp_rows.append((lab, val))
        for idx, (lab, val) in enumerate(rtp_rows):
            if idx < len(fields):
                label, text, colour = fields[idx]
                lab.config(text=label)
                val.config(text=text, fg=colour)
                lab.grid()
                val.grid()
            else:
                lab.grid_remove()
                val.grid_remove()
        if fields:
            rtp_label.grid()
            rtp_var.set("   |   ".join(f"{label}: {text}"
                                        for label, text, _ in fields))
        else:
            rtp_label.grid_remove()
            rtp_var.set("")

    # TMC traffic messages — a ticker along the bottom of the window, so RT+
    # rows can't push it out of view; hidden until a station sends 8A
    tmc_bar = tk.Frame(root, bg="#0d1b2a", relief=tk.SUNKEN, bd=1)
    tmc_bar.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
    tmc_bar.grid_remove()
    tmc_bar.grid_columnconfigure(1, weight=1)
    tmc_header_var = tk.StringVar(value="TMC")
    tk.Label(tmc_bar, textvariable=tmc_header_var, fg="#5c7da0", bg="#0d1b2a",
             font=("Segoe UI", 8, "bold")).grid(row=0, column=0, padx=(8, 6))
    tmc_canvas = tk.Canvas(tmc_bar, height=24, bg="#0d1b2a", highlightthickness=0)
    tmc_canvas.grid(row=0, column=1, sticky="ew")
    tmc_item = tmc_canvas.create_text(0, 12, text="", anchor="w",
                                      fill="#ffb86c", font=("Segoe UI", 10))
    tmc_scroll = {"text": "", "job": None, "paused": False}
    shown_station = [None]  # station dict currently on the display

    def _tmc_step():
        tmc_scroll["job"] = None
        if not tmc_scroll["text"]:
            return
        if not tmc_scroll["paused"]:
            tmc_canvas.move(tmc_item, -2, 0)
            box = tmc_canvas.bbox(tmc_item)
            if box and box[2] < 0:
                tmc_canvas.coords(tmc_item, tmc_canvas.winfo_width(), 12)
        tmc_scroll["job"] = root.after(30, _tmc_step)

    def _tmc_pause(paused):
        tmc_scroll["paused"] = paused
    tmc_canvas.bind("<Enter>", lambda e: _tmc_pause(True))  # hover to read
    tmc_canvas.bind("<Leave>", lambda e: _tmc_pause(False))

    def _refresh_tmc_now():
        _render_tmc(shown_station[0])

    def _render_tmc(station):
        msgs = tmc_messages(station) if (station and show_tmc.get()) else []
        if not msgs:
            tmc_scroll["text"] = ""
            tmc_bar.grid_remove()
            return
        sysinfo = (station.get("tmc") or {}).get("sys") or {}
        hdr = f"TMC TRAFFIC ({len(msgs)})"
        if sysinfo.get("ltn") is not None:
            hdr += f" \u00b7 table {sysinfo['ltn']}"
        tmc_header_var.set(hdr)
        text = "      \u2022      ".join(
            tmc.format_message(m)
            + (f"  (x{m['count']})" if m["count"] > 1 else "") for m in msgs)
        if text != tmc_scroll["text"]:
            if not tmc_scroll["text"]:  # start at the right edge
                tmc_canvas.coords(tmc_item, max(tmc_canvas.winfo_width(), 300), 12)
            tmc_scroll["text"] = text
            tmc_canvas.itemconfigure(tmc_item, text=text)
        tmc_bar.grid()
        if tmc_scroll["job"] is None:
            tmc_scroll["job"] = root.after(30, _tmc_step)

    # HD Radio panel, from the tuner's HD decoder (hidden without the add-on)
    hd_frame = tk.Frame(right_col, bg="#0d1b2a", relief=tk.SUNKEN, bd=1)
    hd_frame.grid(row=2, column=0, pady=(4, 4), padx=4, sticky="new")
    hd_frame.grid_remove()
    hd_frame.grid_columnconfigure(1, weight=1)
    tk.Label(hd_frame, text="HD RADIO", fg="#5c7da0", bg="#0d1b2a",
             font=("Segoe UI", 8, "bold"), anchor="w").grid(
        row=0, column=0, columnspan=2, padx=8, pady=(6, 2), sticky="w")
    hd_rows = []

    def _render_hd(station):
        fields = hd_panel_rows((station or {}).get("hd"))
        while len(hd_rows) < len(fields):
            r = len(hd_rows) + 1
            lab = tk.Label(hd_frame, text="", fg="#5c7da0", bg="#0d1b2a",
                           font=("Segoe UI", 9, "bold"), anchor="ne", width=8)
            val = tk.Label(hd_frame, text="", bg="#0d1b2a",
                           font=("Segoe UI", 10), anchor="w", justify="left",
                           wraplength=300)
            lab.grid(row=r, column=0, padx=(8, 6), pady=1, sticky="ne")
            val.grid(row=r, column=1, padx=(0, 8), pady=1, sticky="w")
            hd_rows.append((lab, val))
        for idx, (lab, val) in enumerate(hd_rows):
            if idx < len(fields):
                label, text, colour = fields[idx]
                lab.config(text=label)
                val.config(text=text, fg=colour)
                lab.grid()
                val.grid()
            else:
                lab.grid_remove()
                val.grid_remove()
        if fields:
            hd_frame.grid()
        else:
            hd_frame.grid_remove()

    # --- Confidence / Signal Quality section ---
    conf_frame = tk.Frame(display, bg="#0d1b2a", relief=tk.SUNKEN, bd=1)
    conf_frame.grid(row=1, column=0, columnspan=3, pady=(4, 8), padx=4,
                    sticky="ew")
    conf_frame.grid_columnconfigure(0, weight=1)

    # Title bar
    tk.Label(conf_frame, text="SIGNAL QUALITY & CONFIDENCE", fg="#5c7da0", bg="#0d1b2a",
             font=("Segoe UI", 8, "bold"), anchor="w").grid(row=0, column=0, padx=8, pady=(6, 2), sticky="w")

    # Quality bar (canvas)
    bar_row = tk.Frame(conf_frame, bg="#0d1b2a")
    bar_row.grid(row=1, column=0, padx=8, pady=2, sticky="ew")
    bar_row.grid_columnconfigure(0, weight=1)
    bar_canvas = tk.Canvas(bar_row, height=22, bg="#1a1a2e", highlightthickness=0)
    bar_canvas.grid(row=0, column=0, sticky="ew")
    usable_var = tk.StringVar(value="0%")
    usable_label = tk.Label(bar_row, textvariable=usable_var, fg="#888", bg="#0d1b2a",
                            font=("Segoe UI", 16, "bold"), width=5, anchor="e")
    usable_label.grid(row=0, column=1, padx=(8, 0))

    # Quality text
    quality_var = tk.StringVar(value="")
    tk.Label(conf_frame, textvariable=quality_var, fg="#888", bg="#0d1b2a",
             font=("Consolas", 8), anchor="w").grid(row=2, column=0, padx=8, pady=(0, 4), sticky="w")

    # Color-coded decoding quality indicators for PS, PTY, PTYN
    # Green = fully decoded, Yellow = partial, Red = none
    decode_frame = tk.Frame(conf_frame, bg="#0d1b2a")
    decode_frame.grid(row=3, column=0, padx=8, pady=(0, 4), sticky="w")
    tk.Label(decode_frame, text="Indicators:", fg="#5c7da0", bg="#0d1b2a",
             font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT, padx=(0, 6))

    # TP/TA/Music-Speech badges (moved here from info area)
    tp_label = tk.Label(decode_frame, text=" TP ", bg="#333", fg="#666",
                        font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    tp_label.pack(side=tk.LEFT, padx=2)
    ta_label = tk.Label(decode_frame, text=" TA ", bg="#333", fg="#666",
                        font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    ta_label.pack(side=tk.LEFT, padx=2)
    ms_label = tk.Label(decode_frame, text=" --- ", bg="#333", fg="#666",
                        font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    ms_label.pack(side=tk.LEFT, padx=2)
    # Separator
    tk.Label(decode_frame, text="|", fg="#333", bg="#0d1b2a",
             font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=4)
    # Decode quality badges
    tk.Label(decode_frame, text="Decode:", fg="#5c7da0", bg="#0d1b2a",
             font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT, padx=(0, 6))
    ps_q_label = tk.Label(decode_frame, text=" PS ", bg="#333", fg="#888",
                          font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    ps_q_label.pack(side=tk.LEFT, padx=2)
    rt_q_label = tk.Label(decode_frame, text=" RT ", bg="#333", fg="#888",
                          font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    rt_q_label.pack(side=tk.LEFT, padx=2)
    pty_q_label = tk.Label(decode_frame, text=" PTY ", bg="#333", fg="#888",
                           font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    pty_q_label.pack(side=tk.LEFT, padx=2)
    ptyn_q_label = tk.Label(decode_frame, text=" PTYN ", bg="#333", fg="#888",
                            font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    ptyn_q_label.pack(side=tk.LEFT, padx=2)
    rtp_q_label = tk.Label(decode_frame, text=" RT+ ", bg="#333", fg="#888",
                           font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    rtp_q_label.pack(side=tk.LEFT, padx=2)
    tmc_q_label = tk.Label(decode_frame, text=" TMC ", bg="#333", fg="#888",
                           font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    tmc_q_label.pack(side=tk.LEFT, padx=2)
    af_q_label = tk.Label(decode_frame, text=" AF ", bg="#333", fg="#888",
                          font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    af_q_label.pack(side=tk.LEFT, padx=2)
    eon_q_label = tk.Label(decode_frame, text=" EON ", bg="#333", fg="#888",
                           font=("Segoe UI", 8, "bold"), padx=4, pady=1)
    eon_q_label.pack(side=tk.LEFT, padx=2)

    # Group sequence fingerprint: the newest group types in arrival order
    seq_var = tk.StringVar(value="SEQ> (waiting for groups)")
    tk.Label(conf_frame, textvariable=seq_var, fg="#39ff14", bg="#05080d",
             font=("Consolas", 9, "bold"), anchor="w", padx=6, pady=2).grid(
        row=4, column=0, padx=8, pady=(0, 4), sticky="w")

    # Service completeness bars
    services_var = tk.StringVar(value="")
    services_label = tk.Label(conf_frame, textvariable=services_var, fg="#888", bg="#0d1b2a",
             font=("Consolas", 8), anchor="w", justify=tk.LEFT)
    services_label.grid(row=5, column=0, padx=8, pady=(0, 4), sticky="w")

    # Group type counts
    groups_var = tk.StringVar(value="")
    groups_label = tk.Label(conf_frame, textvariable=groups_var, fg="#888", bg="#0d1b2a",
             font=("Consolas", 8), anchor="w")
    groups_label.grid(row=6, column=0, padx=8, pady=(0, 6), sticky="w")

    # --- State ---
    last_mtime = [0]
    last_size = [0]
    main_last_byte = [0]  # byte offset for incremental main decode
    downloaded_callsigns = set()
    current_callsign = [None]
    location_callsign = [None]  # separate tracker for location lookups
    _loc = active_location()
    home_location = [_loc[:2]]  # (lat, lon) of the active Home/Away location
    location_label = [_loc[2]]
    home_changed = [False]
    current_pi = [None]  # current station PI for report
    recent_pis = []      # PIs of the newest network groups, for stable_pi
    session_decoded_at = [0.0]
    logo_path_ref = [None]
    logo_img_ref = [None]  # keep reference to prevent GC
    stations_data = [{}]  # shared ref for menu functions
    session_stations = [{}]  # stations received since program open (for all-stations report)
    session_start_size = [0]  # spy file size when first detected (session boundary)
    session_spy_lines = []  # accumulating list of spy lines for MPX mode
    # Network input decodes a fresh temp file each poll, so the byte offsets
    # decode_rds_groups records restart every time.  Stamp live stations with
    # a counter above any file offset so a station left over from a spy file
    # can never look more recent than the one the tuner is feeding us.
    net_seen = [10 ** 12]

    # Pulsing live indicator
    live_state = [True]
    def pulse_live():
        live_state[0] = not live_state[0]
        live_dot.config(fg="#e94560" if live_state[0] else "#0f3460")
        root.after(700, pulse_live)
    root.after(700, pulse_live)

    # Resize logo when window changes size
    def _on_resize(event):
        if logo_path_ref[0]:
            _load_logo(logo_path_ref[0])
        # Update wraplength for text labels to match window width
        w = max(event.width - 40, 200)
        rt_label.config(wraplength=w)
        for child in display.winfo_children():
            try:
                if child.cget("wraplength"):
                    child.config(wraplength=w)
            except Exception:
                pass
    display.bind("<Configure>", _on_resize)

    def _load_logo(path):
        """Load a logo image sized to the current window."""
        try:
            if not HAS_PIL:
                photo = tk.PhotoImage(file=path)
                logo_label.config(image=photo, text="")
                logo_img_ref[0] = photo
                return
            img = Image.open(path)
            # Fit the logo inside the left column (2/8 of the display width)
            # so it is never clipped, and keep it to the top third.
            win_w = root.winfo_width()
            win_h = root.winfo_height()
            col_w = left_col.winfo_width()
            if col_w <= 1:
                col_w = int(win_w * 0.25)
            max_w = max(80, col_w - 16)
            max_h = max(80, int(win_h * 0.32))
            img.thumbnail((max_w, max_h))
            photo = ImageTk.PhotoImage(img)
            logo_label.config(image=photo, text="")
            logo_img_ref[0] = photo
        except Exception:
            pass

    def _download_and_show(callsign):
        """Download a logo and display it."""
        try:
            local = download_logo_for(callsign, logo_dir)
            if local:
                logo_path_ref[0] = str(local)
                _load_logo(str(local))
        except Exception:
            pass
        finally:
            downloaded_callsigns.discard(callsign)

    tmc_tuner_key = [None]  # (frequency, running?) the TMC lists belong to

    def _reset_tmc(reason):
        """Clear TMC for every station — the decoder stopped or retuned."""
        cleared = sum(1 for st in stations_data[0].values() if st.get("tmc"))
        tmc_reset_all(stations_data[0])
        tmc_reset_all(session_stations[0])
        if cleared:
            _refresh_tmc_now()
            status_var.set(f"TMC cleared ({reason})")

    def _track_tmc_lifetime(tel, freq):
        running = str(tel.get("state", "")).lower() not in (
            "", "stopped", "error", "idle", "?")
        key = (freq, running)
        prev = tmc_tuner_key[0]
        tmc_tuner_key[0] = key
        if prev is None or prev == key:
            return
        if prev[1] and not running:
            _reset_tmc("decoder stopped")
        elif prev[0] != freq and freq is not None:
            _reset_tmc(f"retuned to {freq:.1f} MHz")

    map_tuned_sent = {}

    def _send_tuned(freq):
        """Pass the Tuner's frequency to the DX Mapper (works with no RDS)."""
        if map_now_busy[0] or not map_tuned_due(freq, map_tuned_sent):
            return
        map_now_busy[0] = True

        def _worker():
            try:
                send_to_map({"type": "tuned", "freq_mhz": round(float(freq), 1)},
                            timeout=1.0)
            except (OSError, ValueError):
                pass  # DX Mapper not running
            finally:
                map_now_busy[0] = False

        threading.Thread(target=_worker, daemon=True).start()

    def _update_tuner_state():
        """Refresh the tuned-frequency readout from tuner telemetry."""
        if tuner_link is None:
            tuner_freq_var.set("--.- MHz")
            tuner_state_var.set("tuner: not connected")
            return
        freq = tuner_link.frequency_mhz
        _send_tuned(freq)
        tuner_freq_var.set("--.- MHz" if freq is None
                           else f"{mhz_text(freq, tuner_link.telemetry)} MHz")
        tel = tuner_link.telemetry
        if tel:
            _track_tmc_lifetime(tel, freq)
        if not tuner_link.connected and not tel:
            tuner_state_var.set(
                f"tuner: connecting to {tuner_link.host}:{tuner_link.telemetry_port}")
            return
        bits = [str(tel.get("state", "?"))]
        if tel.get("signal_dbfs") is not None:
            bits.append(signal_text(tel["signal_dbfs"], tel))
        bits.append("stereo" if tel.get("stereo") else "mono")
        if tel.get("hd_detected"):
            hd_call = (tel.get("hd") or {}).get("call")
            bits.append(f"HD ({hd_call})" if hd_call else "HD")
        if tel.get("gain_db") is not None:
            bits.append(f"gain {tel['gain_db']:.1f} dB")
        elif tel.get("gain_mode"):
            bits.append(f"gain {tel['gain_mode']}")
        if tel.get("bias_tee"):
            bits.append("Bias-T 4.5 V")
        if "bias_tee" in tel and bool(tel["bias_tee"]) != tuner_bias_var.get():
            tuner_bias_var.set(bool(tel["bias_tee"]))
        tuner_state_var.set("tuner: " + ", ".join(bits))

    def _net_status(detail):
        """Set the status bar for a network input source."""
        if input_mode.get() == "tuner" and tuner_link is not None:
            status_var.set(f"{tuner_link.summary()} \u2014 {detail}")
        else:
            status_var.set(f"RFtap: {detail}")

    hd_latest = [None]  # newest HD decoder telemetry, for the HD Data tab

    def refresh_display():
        """Parse input source and show the most recent station."""
        if input_mode.get() in ("tuner", "rftap"):
            # Network mode -- RDS groups arrive from the FM Tuner (or another
            # ASCII G / RFtap source); nothing is demodulated locally.
            if input_mode.get() == "tuner":
                _update_tuner_state()
            if rftap_input is None or not rftap_input.enabled:
                status_var.set("RDS input not started. Use the Input menu.")
                return

            # Update RFtap status indicator
            rftap_status = rftap_input.get_status()
            status_map = {
                "disabled":      ("RFtap: \u25cb Disabled", "#888"),
                "waiting":       ("RFtap: \u25cc Waiting", "#f1c40f"),
                "receiving":     ("RFtap: \u25cf Receiving", "#3498db"),
                "receiving_rds": ("RFtap: \u25cf RDS", "#2ecc71"),
                "stale":         ("RFtap: \u25cf Stale", "#e67e22"),
                "error":         ("RFtap: \u2715 Error", "#e74c3c"),
            }
            status_text, status_color = status_map.get(
                rftap_status, ("RFtap: ?", "#888"))
            rftap_status_var.set(status_text)
            try:
                rftap_status_label.config(fg=status_color)
            except Exception:
                pass

            # Drain queued RDS groups
            new_groups = rftap_input.drain_groups(500)
            if new_groups:
                # Accumulate for session report and the Group Analyzer
                for line in new_groups:
                    session_spy_lines.append(line)
                live_pending_lines.extend(new_groups)
                for line in new_groups:
                    parts = line.split()
                    if parts:
                        recent_pis.append(parts[0].upper())
                        if len(recent_pis) > RECENT_PI_WINDOW:
                            del recent_pis[0]
            live_pending_rds2.extend(rftap_input.drain_rds2_groups(500))

            if new_groups:
                # Write to temp spy file and decode
                import tempfile
                tmp = Path(tempfile.gettempdir()) / "rds_rftap_live.spy"
                with open(tmp, "w") as f:
                    for line in new_groups:
                        f.write(line + "\n")
                stations = decode_rds_groups(str(tmp),
                    existing_stations=stations_data[0], incremental=True)
                heard_on = None
                tel = {}
                if tuner_link is not None:
                    heard_on = tuner_link.frequency_mhz
                    tel = tuner_link.telemetry or {}
                if heard_on is None and rftap_input.nominal_frequency:
                    heard_on = rftap_input.nominal_frequency / 1e6
                signal_dbfs = tel.get("signal_dbfs")
                rds_db = tel.get("mpx_rds_db")
                for line in new_groups:
                    pi = line.split()[0].upper() if line.split() else ""
                    if pi in stations:
                        net_seen[0] += 1
                        st_entry = stations[pi]
                        st_entry["last_seen"] = net_seen[0]
                        if heard_on is not None:
                            st_entry["freq_mhz"] = heard_on
                        if isinstance(signal_dbfs, (int, float)):
                            st_entry["signal_dbfs"] = signal_dbfs
                            peak = st_entry.get("signal_peak_dbfs")
                            if peak is None or signal_dbfs > peak:
                                st_entry["signal_peak_dbfs"] = signal_dbfs
                        if isinstance(rds_db, (int, float)):
                            st_entry["rds_level_db"] = rds_db
                        if tel.get("stereo") is not None:
                            st_entry["tuner_stereo"] = bool(tel["stereo"])
                        if tel.get("stereo_mode") is not None:
                            st_entry["tuner_stereo_mode"] = tel["stereo_mode"]
                        if isinstance(tel.get("mpx_pilot_db"), (int, float)):
                            st_entry["pilot_db"] = tel["mpx_pilot_db"]
                        if tel.get("dongle") or tel.get("device"):
                            st_entry["dongle"] = tel.get("dongle") or tel["device"]
                        if tel.get("gain_mode"):
                            st_entry["tuner_gain_mode"] = tel["gain_mode"]
                            st_entry["tuner_gain_db"] = tel.get("gain_db")
                        if isinstance(tel.get("hd"), dict):
                            st_entry["hd_detected"] = tel.get("hd_detected")
                            st_entry["hd"] = tel["hd"]
                            hd_latest[0] = tel["hd"]
                            if adopt_hd_logo(st_entry.get("callsign"),
                                             tel["hd"], logo_dir):
                                current_callsign[0] = None  # reload logo
            else:
                stations = {}
                # No new groups -- show waiting status
                rate = rftap_input.get_packet_rate()
                age = rftap_input.get_last_packet_age()
                age_str = f"{age:.1f}s ago" if age is not None else "never"
                freq_str = ""
                if rftap_input.nominal_frequency:
                    freq_str = f", freq={rftap_input.nominal_frequency/1e6:.1f}MHz"
                _net_status(
                    f"{rftap_input.total_packets} pkts, "
                    f"{rftap_input.rds_payloads} RDS, "
                    f"{rate:.1f} pkt/s, last={age_str}{freq_str}"
                )
                return

            if not stations:
                rate = rftap_input.get_packet_rate()
                age = rftap_input.get_last_packet_age()
                age_str = f"{age:.1f}s ago" if age is not None else "never"
                _net_status(
                    f"{rftap_input.total_packets} pkts, "
                    f"{rftap_input.rds_payloads} RDS, "
                    f"{rate:.1f} pkt/s, last={age_str}"
                )
                return

            stations_data[0] = stations
            if session_spy_lines and time.time() - session_decoded_at[0] >= 1.0:
                session_decoded_at[0] = time.time()
                tmp2 = Path(tempfile.gettempdir()) / "rds_session.spy"
                with open(tmp2, "w") as f:
                    f.write("\n".join(session_spy_lines[-5000:]) + "\n")
                session_stations[0] = decode_rds_groups(str(tmp2),
                    existing_stations=session_stations[0], incremental=True)

            # Update status bar with link stats
            rate = rftap_input.get_packet_rate()
            _net_status(
                f"{rftap_input.total_packets} pkts, "
                f"{rftap_input.rds_payloads} RDS, {rate:.1f} pkt/s"
            )

        else:
            # Spy file input mode
            spy_path = Path(spy_file)
            if not spy_path.exists():
                status_var.set(f"Waiting for {spy_path.name}...")
                return

            mtime = spy_path.stat().st_mtime
            size = spy_path.stat().st_size
            if mtime == last_mtime[0] and size == last_size[0]:
                return

            # Record session start position on first detection
            if session_start_size[0] == 0:
                session_start_size[0] = size

            last_mtime[0] = mtime
            last_size[0] = size

            # Incremental decode: only read new bytes since last poll
            # This prevents re-reading the entire file (which grows over time)
            if main_last_byte[0] == 0 or size < main_last_byte[0]:
                # First read or file was truncated — full read from start
                stations = decode_rds_groups(str(spy_path), existing_stations={})
                main_last_byte[0] = size
            else:
                # Incremental: only read new data since last byte position
                stations = decode_rds_groups(str(spy_path),
                    existing_stations=stations_data[0],
                    start_byte=main_last_byte[0],
                    incremental=True)
                main_last_byte[0] = size
            if not stations:
                status_var.set("No stations found.")
                return
            stations_data[0] = stations  # share with menu functions

            # Session decode also incremental
            session_stations[0] = decode_rds_groups(str(spy_path),
                existing_stations=session_stations[0],
                start_byte=session_start_size[0],
                incremental=True)

        # Pick the most recently active station (highest line number = most recent)
        latest_pi = max(stations.keys(),
                        key=lambda p: stations[p]["last_seen"])
        if input_mode.get() in ("tuner", "rftap"):
            held = stable_pi(current_pi[0], recent_pis)
            if held in stations:
                latest_pi = held
        current_pi[0] = latest_pi  # track for report

        # Update lifetime unique station count (from all decoded stations)
        new_pis = set(stations.keys()) - lifetime_pis - lifetime_counted
        lifetime_counted.update(stations.keys())
        if new_pis:
            lifetime_pis.update(new_pis)
            save_station_count(lifetime_pis)
        # Update session unique station count (from session-only data, since program open)
        new_session_pis = set(session_stations[0].keys()) - session_pis
        if new_session_pis:
            session_pis.update(new_session_pis)
        if lifetime_count_var:
            lifetime_count_var.set(_lifetime_text())
        if session_count_var:
            session_count_var.set(f"Session: {len(session_pis)}")
        data = stations[latest_pi]
        callsign = data.get("callsign") or ""

        # Update callsign
        cs_text = callsign if callsign else "(unknown)"
        callsign_var.set(cs_text)
        slogan = station_slogan(callsign) if callsign else None
        if slogan != (slogan_var.get() or None):
            slogan_var.set(slogan or "")
            if slogan:
                slogan_lbl.pack(after=call_lbl)
            else:
                slogan_lbl.pack_forget()

        # Look up station location (city, state, coordinates) when callsign changes
        if location_callsign[0] != callsign or home_changed[0]:
            location_callsign[0] = callsign
            home_changed[0] = False
            location_var.set("")
            range_var.set("")
            if callsign and callsign != "(unknown)":
                geo = lookup_station_geo(callsign)
                if geo:
                    location_var.set(geo["location"] or "")
                    rng = station_tier_text(home_location[0], geo,
                                            mapper_radii())
                    if rng and location_label[0] != "Home":
                        rng += f" from {location_label[0]}"
                    range_var.set(rng or "")
                remember_station(latest_pi, callsign, geo)

        if map_now_due(latest_pi, data, map_now_sent):
            _send_now_hearing(latest_pi, data)
        hd_due = hd_log_due(latest_pi, data, hd_map_since, hd_map_sent)
        if auto_map_log_var.get():
            if hd_due and auto_hd_log_var.get():
                _log_to_map(auto=True, hd=True)
            elif auto_log_due(latest_pi, data, auto_map_sent,
                              last_sig=auto_map_sig):
                _log_to_map(auto=True)

        # PI + PTY + PTYN + Date/Time
        pty_text = f"PI: {latest_pi}"
        if data["pty"] is not None:
            pty_name = PTY_TABLE.get(data["pty"], f"Unknown({data['pty']})")
            pty_text += f"  |  PTY: {pty_name}"
        ptyn = data.get("ptyn", "")
        if ptyn:
            # Clean non-printable chars
            clean_ptyn = ''.join(c if c.isprintable() or c == ' ' else ' ' for c in ptyn).strip()
            if clean_ptyn:
                pty_text += f"  |  PTYN: {clean_ptyn}"
        info_var.set(pty_text)
        dt = data.get("date_time")
        if dt:
            clock_var.set(f"Station time: {dt['date_str']} {dt['time_str']} "
                          f"({dt['offset_str']})")
        else:
            clock_var.set("")

        af_freqs, af_expected, af_complete = af_summary(data)
        if af_freqs:
            af_txt = "AF: " + "  ".join(f"{f:.1f}" for f in af_freqs)
            if af_expected:
                af_txt += f"   ({len(af_freqs)}/{af_expected})"
            af_var.set(af_txt)
        else:
            af_var.set("")
        eon = data.get("eon") or {}
        if eon:
            parts = []
            for on_pi in sorted(eon):
                net = eon[on_pi]
                tag = net["ps"] or on_pi
                if net["af"]:
                    tag += f" {net['af'][0]:.1f}"
                elif net["mapped"]:
                    tag += f" {net['mapped'][0][1]:.1f}"
                if net["ta"]:
                    tag += " TA"
                parts.append(tag)
            eon_var.set("EON: " + "  |  ".join(parts))
        else:
            eon_var.set("")

        # TP badge (Traffic Program — station carries traffic info)
        if data.get("tp"):
            tp_label.config(text=" TP ", bg="#1a5276", fg="#5dade2")
        else:
            tp_label.config(text=" TP ", bg="#333", fg="#666")

        # TA badge (Traffic Announcement — currently in progress)
        if data.get("ta"):
            ta_label.config(text=" TA ", bg="#e74c3c", fg="#fff")
        else:
            ta_label.config(text=" TA ", bg="#333", fg="#666")

        # Music/Speech badge
        ms = data.get("music_speech")
        if ms is True:
            ms_label.config(text=" MUSIC ", bg="#1e8449", fg="#a9dfbf")
        elif ms is False:
            ms_label.config(text=" SPEECH ", bg="#7d3c98", fg="#d2b4de")
        else:
            ms_label.config(text=" --- ", bg="#333", fg="#666")

        # PS — only show once all 4 segments are received (complete PS)
        ps = data.get("ps", "")
        ps_segs = data.get("ps_segments", {})
        ps_lines = []
        if ps and len(ps_segs) == 4:
            clean_ps = ''.join(c if c.isprintable() or c == ' ' else ' '
                               for c in ps).strip()
            ps_lines.append(f"PS: {clean_ps}")
        if data.get("long_ps") and data.get("long_ps_complete"):
            ps_lines.append(f"Long PS: {data['long_ps']}")
        ps_var.set("\n".join(ps_lines))

        # RadioText — show the full decoded RT buffer once all segments received
        rt = data.get("rt", "")
        rt_segs = data.get("rt_segments", {})
        # Type 2A has 16 segments (64 chars), type 2B has 8 segments (32 chars)
        if rt and len(rt_segs) >= 8:
            clean_rt = ''.join(c if c.isprintable() or c == ' ' else ' '
                               for c in rt).strip()
            rt_var.set(clean_rt if clean_rt else "")
        else:
            rt_var.set("")

        # Song Title and Artist (from RT+ markers or RT text fallback)
        title = data.get("title", "")
        artist = data.get("artist", "")
        title_var.set(title if title else "")
        artist_var.set(artist if artist else "")
        hd_art_now[0] = hd_art_path(data.get("hd"))
        _update_art(title, artist)
        _update_music_buttons(title, artist)

        # RT+ — one labelled row per item type the station actually tagged
        # text with, grouped: now playing, then programme, then station/contact.
        rows = []
        for itype, name, text in data.get("rtp_fields", []):
            rows.append((RTP_FIELD_LABELS.get(itype, name), text,
                         rtp_field_colour(itype)))
        if rows and show_rtplus.get():
            _render_rtp_fields(rows)
        else:
            _render_rtp_fields([])

        shown_station[0] = data
        _render_tmc(data)
        _render_hd(data)

        # --- Confidence / Signal Quality ---
        stats = data.get("stats", {})
        total_blocks = stats.get("total_blocks", 0)
        bad_blocks = stats.get("bad_blocks", 0)
        total_groups = stats.get("total_groups", 0)

        # Block quality percentage (shown in the text line only)
        if total_blocks > 0:
            good_pct = int((1 - bad_blocks / total_blocks) * 100)
        else:
            good_pct = 0

        usable_pct = usable_info_score(data)

        # Draw usable-info bar
        bar_canvas.delete("all")
        bar_w = bar_canvas.winfo_width() or 400
        bar_h = 22
        bar_canvas.create_rectangle(0, 0, bar_w, bar_h, fill="#1a1a2e", outline="")
        fill_w = int(bar_w * usable_pct / 100)
        if usable_pct >= 75:
            bar_color = "#2ecc71"  # green
        elif usable_pct >= 50:
            bar_color = "#f1c40f"  # yellow
        elif usable_pct >= 25:
            bar_color = "#e67e22"  # orange
        else:
            bar_color = "#e74c3c"  # red
        if fill_w > 0:
            bar_canvas.create_rectangle(0, 0, fill_w, bar_h, fill=bar_color, outline="")
        bar_canvas.create_text(bar_w // 2, bar_h // 2,
                                text=f"{usable_pct}%  usable info decoded",
                                fill="#fff", font=("Segoe UI", 10, "bold"))
        usable_var.set(f"{usable_pct}%")
        usable_label.config(fg=bar_color)

        quality_var.set(
            f"Groups: {total_groups}  |  Blocks: {total_blocks}  |  "
            f"Good: {total_blocks - bad_blocks}  |  Errors: {bad_blocks}  |  "
            f"Block quality: {good_pct}%  |  Usable: {usable_pct}%"
        )

        # Service completeness
        ps_recv = len(data.get("ps_segments", {}))
        rt_recv = len(data.get("rt_segments", {}))
        ptyn_recv = len(data.get("ptyn_segments", {}))
        rtp_count = len(data.get("rtp_items", []))
        has_pty = data.get("pty") is not None
        has_dt = data.get("date_time") is not None

        def _pct(n, total):
            return int(n / total * 100) if total > 0 else 0

        def _bar(pct, width=10):
            filled = int(pct / 100 * width)
            return "\u2588" * filled + "\u2591" * (width - filled)

        ps_pct = _pct(ps_recv, 4)
        rt_pct = _pct(rt_recv, 16)
        ptyn_pct = _pct(ptyn_recv, 2)

        services_text = (
            f"PS    [{_bar(ps_pct)}] {ps_recv}/4     "
            f"RT  [{_bar(rt_pct)}] {rt_recv}/16\n"
            f"PTYN  [{_bar(ptyn_pct)}] {ptyn_recv}/2     "
            f"PTY: {'OK' if has_pty else '--'}   "
            f"CT: {'OK' if has_dt else '--'}   "
            f"RT+: {rtp_count}"
        )
        services_var.set(services_text)

        # Color-coded decode quality labels
        # PS: green=4/4, yellow=1-3/4, red=0/4
        if ps_pct >= 100:
            ps_q_label.config(text=" PS ", bg="#2ecc71", fg="#fff")
        elif ps_pct > 0:
            ps_q_label.config(text=" PS ", bg="#f1c40f", fg="#000")
        else:
            ps_q_label.config(text=" PS ", bg="#e74c3c", fg="#fff")
        # RT: green=16/16, yellow=partial, red=0/16
        if rt_pct >= 100:
            rt_q_label.config(text=" RT ", bg="#2ecc71", fg="#fff")
        elif rt_pct > 0:
            rt_q_label.config(text=" RT ", bg="#f1c40f", fg="#000")
        else:
            rt_q_label.config(text=" RT ", bg="#e74c3c", fg="#fff")
        # PTY: green=decoded, red=none
        if has_pty:
            pty_q_label.config(text=" PTY ", bg="#2ecc71", fg="#fff")
        else:
            pty_q_label.config(text=" PTY ", bg="#e74c3c", fg="#fff")
        # PTYN: green=4/4, yellow=1-3/4, red=0/4
        if ptyn_pct >= 100:
            ptyn_q_label.config(text=" PTYN ", bg="#2ecc71", fg="#fff")
        elif ptyn_pct > 0:
            ptyn_q_label.config(text=" PTYN ", bg="#f1c40f", fg="#000")
        else:
            ptyn_q_label.config(text=" PTYN ", bg="#e74c3c", fg="#fff")
        # RT+: green=items present, red=none
        if rtp_count > 0:
            rtp_q_label.config(text=" RT+ ", bg="#2ecc71", fg="#fff")
        else:
            rtp_q_label.config(text=" RT+ ", bg="#e74c3c", fg="#fff")

        # TMC: green=decoded messages, yellow=8A groups but no message, red=none
        tmc_msgs = tmc_messages(data)
        tmc_8a = stats.get("group_counts", {}).get("8A", 0)
        if tmc_msgs:
            tmc_q_label.config(text=f" TMC({len(tmc_msgs)}) ", bg="#2ecc71", fg="#fff")
        elif tmc_8a > 0:
            tmc_q_label.config(text=f" TMC({tmc_8a}) ", bg="#f1c40f", fg="#000")
        else:
            tmc_q_label.config(text=" TMC ", bg="#e74c3c", fg="#fff")

        # AF: green=list complete, yellow=still filling in, red=no AF sent
        if af_complete:
            af_q_label.config(text=f" AF({len(af_freqs)}) ", bg="#2ecc71", fg="#fff")
        elif af_freqs:
            af_q_label.config(text=f" AF({len(af_freqs)}) ", bg="#f1c40f", fg="#000")
        else:
            af_q_label.config(text=" AF ", bg="#e74c3c", fg="#fff")

        # EON: green=other networks with PS decoded, yellow=14A/14B seen
        # but names still filling in, grey=no EON on this station
        eon_nets = data.get("eon") or {}
        if eon_nets and all(net["ps"] for net in eon_nets.values()):
            eon_q_label.config(text=f" EON({len(eon_nets)}) ", bg="#2ecc71", fg="#fff")
        elif eon_nets:
            eon_q_label.config(text=f" EON({len(eon_nets)}) ", bg="#f1c40f", fg="#000")
        else:
            eon_q_label.config(text=" EON ", bg="#333", fg="#888")

        # Group type counts
        gc = stats.get("group_counts", {})
        if gc:
            gc_sorted = sorted(gc.items(), key=lambda x: x[0])
            groups_text = "Groups: " + "  ".join(f"{gt}:{cnt}" for gt, cnt in gc_sorted)
        else:
            groups_text = "Groups: (none)"
        groups_var.set(groups_text)
        seq_var.set("SEQ> " + (group_fingerprint(data) or "(waiting for groups)"))

        # Live window is updated by the fast 200ms poll loop, not here

        # Logo
        if callsign and callsign != current_callsign[0]:
            current_callsign[0] = callsign
            existing = find_existing_logo(callsign, logo_dir)
            if existing:
                logo_path_ref[0] = str(existing)
                _load_logo(str(existing))
            elif callsign not in downloaded_callsigns:
                downloaded_callsigns.add(callsign)
                root.after(100, lambda c=callsign: _download_and_show(c))

        status_var.set(f"{callsign or latest_pi} | {time.strftime('%H:%M:%S')}")

    def _apply_station_file_changes():
        """A DX Mapper log or stations.txt changed: relabel those PIs
        now and reload the logo and city for the new call."""
        if not stations_file_changed():
            return
        overrides = get_pi_overrides()
        changed = False
        for table in (stations_data[0], session_stations[0]):
            for pi, st in table.items():
                new = overrides.get(str(pi).upper())
                if new and st.get("callsign") != new:
                    lookup_station_geo._cache.pop(new, None)
                    downloaded_callsigns.discard(new)
                    st["callsign"] = new
                    changed = True
        if changed:
            location_callsign[0] = None  # redraw city / distance
            current_callsign[0] = None   # reload the logo
            logo_path_ref[0] = None
            logo_img_ref[0] = None
            try:
                logo_label.config(image="", text="")
            except Exception:
                pass

    # Initial display
    refresh_display()

    # Poll every 1 second
    def poll():
        _apply_station_file_changes()
        refresh_display()
        # Network groups are shown as they arrive; spy files change slowly.
        delay = 200 if input_mode.get() in ("tuner", "rftap") else 1000
        root.after(delay, poll)
    root.after(200, poll)

    # Open at a 16:9 size big enough for all three columns
    root.update_idletasks()
    req_w = max(1280, root.winfo_reqwidth() + 20)
    req_h = max(720, root.winfo_reqheight() + 20, req_w * 9 // 16)
    root.geometry(f"{req_w}x{req_h}")

    # --- Live data window (Group Analyzer style) ---
    live_win = None
    live_pending_lines = []  # raw classic group lines from the TCP input awaiting the analyzer
    live_pending_rds2 = []   # (stream, line) RDS2 records from the FM Tuner
    live_win_refs = [None]
    live_current_pi = [""]  # track which station the live window is showing
    oda_tracker = OdaTracker()

    # RBDS USA standard group descriptions (abbreviated)
    type_names = {
        "0A": "PS, PTY, TP/TA, AF", "0B": "PS, PTY, TP/TA",
        "1A": "PIN, ECC", "1B": "PIN",
        "2A": "RadioText (64ch)", "2B": "RadioText (32ch)",
        "3A": "ODA AID", "3B": "ODA",
        "4A": "Clock-Time (CT)", "4B": "ODA",
        "5A": "TDC", "5B": "TDC",
        "6A": "In-House", "6B": "In-House",
        "7A": "Paging", "7B": "Paging",
        "8A": "TMC", "8B": "TMC",
        "9A": "EWS", "9B": "EWS",
        "10A": "PTYN", "10B": "ODA",
        "11A": "ODA", "11B": "ODA",
        "12A": "ODA", "12B": "ODA",
        "13A": "ODA", "13B": "ODA",
        "14A": "EON", "14B": "EON",
        "15A": "Long PS", "15B": "Fast Tuning",
    }
    # RDS2 streams carry the same 4-block frame; the type field is nominal.
    rds2_type_names = {gt: "" for gt in type_names}

    DETECTOR_OFF = ("#2a2a3e", "#fff")
    DETECTOR_ON = ("#2ecc71", "#000")

    def _group_type_of(line):
        """'PPPP GGGG CCCC DDDD' -> ('8A', b2, b4) or None."""
        parts = line.strip().split()
        if len(parts) < 4:
            return None
        try:
            b2_val = int(parts[1], 16)
            b4_val = int(parts[3], 16) if '-' not in parts[3] else None
        except ValueError:
            return None
        gt = f"{(b2_val >> 12) & 0xF}{'A' if not (b2_val >> 11) & 1 else 'B'}"
        return gt, b2_val, b4_val

    def _trim_text_widget(widget, keep_rows, hold_view=False):
        """Drop the oldest lines so the widget keeps rolling indefinitely.
        With hold_view the rows the user is looking at stay put."""
        line_count = int(widget.index('end-1c').split('.')[0])
        drop = line_count - keep_rows
        if drop <= 0:
            return
        top = int(widget.index("@0,0").split('.')[0]) if hold_view else 0
        widget.delete("1.0", f"{drop + 1}.0")
        if hold_view:
            widget.yview(f"{max(1, top - drop)}.0")

    def _following(widget):
        """True unless the user has scrolled up to look at older rows."""
        try:
            if not widget.winfo_ismapped():
                return True
            return widget.yview()[1] >= 0.999
        except Exception:
            return True

    def _append_text(widget, lines):
        follow = _following(widget)
        widget.config(state=tk.NORMAL)
        for item in lines:
            if isinstance(item, tuple):
                widget.insert(tk.END, item[0] + "\n", (item[1],))
            else:
                widget.insert(tk.END, item + "\n")
        _trim_text_widget(widget, LIVE_TEXT_ROWS, hold_view=not follow)
        if follow:
            widget.see(tk.END)
        widget.config(state=tk.DISABLED)

    def _clear_text(widget):
        widget.config(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.config(state=tk.DISABLED)

    def _build_detector(parent, _tk):
        """0A-15B group detector grid; returns {gt: label}."""
        frame = _tk.Frame(parent, bg="#0d1b2a", relief=tk.RIDGE, bd=1)
        _tk.Label(frame, text="Group Detector", fg="#e94560", bg="#0d1b2a",
                 font=("Segoe UI", 8, "bold")).pack(pady=(2, 4))
        grid = _tk.Frame(frame, bg="#0d1b2a")
        grid.pack(padx=4, pady=2)
        labels = {}
        # 4 columns: 0A-7A, 8A-15A, 0B-7B, 8B-15B
        for col, start in enumerate([0, 8, 0, 8]):
            version = "A" if col < 2 else "B"
            for row_idx in range(8):
                gt = f"{start + row_idx}{version}"
                lbl = _tk.Label(grid, text=gt, fg=DETECTOR_OFF[1],
                               bg=DETECTOR_OFF[0], width=5,
                               font=("Consolas", 8))
                lbl.grid(row=row_idx, column=col, padx=1, pady=1)
                labels[gt] = lbl
        return frame, labels

    class DiTracker:
        """Follows the DI bit carried one segment at a time in type 0 groups
        so an incomplete cycle can be told apart from a station that really
        toggles its flag."""

        def __init__(self):
            self.reset()

        def reset(self):
            self.state = {}
            self.last = None
            self.changes = 0
            self.dirty = True

        def add(self, b2_val):
            if (b2_val >> 12) & 0xF != 0:
                return
            seg = b2_val & 0x3
            bit = (b2_val >> 2) & 0x1
            prev = self.state.get(seg)
            if prev is not None and prev != bit:
                self.changes += 1
            self.state[seg] = bit
            self.last = (seg, bit)
            self.dirty = True

        def lines(self):
            if self.last is None:
                return ["No type 0 group yet"]
            seg, bit = self.last
            flag = DI_SEGMENT_NAMES[seg]
            if seg == 3:
                meaning = "stereo" if bit else "mono"
            else:
                meaning = f"{flag} {'on' if bit else 'off'}"
            n = len(self.state)
            assembly = ("complete" if n == 4 else f"partial ({n}/4)")
            if self.changes:
                assembly += f"\nFlag changes: {self.changes}"
            st = self.state
            stereo = ("stereo" if st.get(3) else "mono") if 3 in st else "?"
            return [f"Last 0A/0B: seg {seg}, bit {bit}",
                    f"Interpreted: {meaning}",
                    f"Station DI: {stereo}"
                    f"{', AH' if st.get(2) else ''}"
                    f"{', comp' if st.get(1) else ''}"
                    f"{', dynPTY' if st.get(0) else ''}",
                    f"Assembly: {assembly}"]

    def _build_di_panel(parent, _tk):
        frame = _tk.Frame(parent, bg="#0d1b2a", relief=tk.RIDGE, bd=1)
        _tk.Label(frame, text="DI (0A/0B)", fg="#e94560", bg="#0d1b2a",
                 font=("Segoe UI", 8, "bold")).pack(pady=(2, 2))
        var = _tk.StringVar(value="No type 0 group yet")
        _tk.Label(frame, textvariable=var, fg="#cde", bg="#0d1b2a",
                 justify=tk.LEFT, anchor="w",
                 font=("Consolas", 8)).pack(padx=4, pady=(0, 4), anchor="w")
        return frame, var

    def _scrolled_text(parent, _tk, **kw):
        box = _tk.Frame(parent, bg="#000")
        text = _tk.Text(box, bg="#000", fg="#0f0", font=("Consolas", 9),
                        wrap=tk.NONE, insertbackground="#0f0", **kw)
        sb = _tk.Scrollbar(box, command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill="y")
        text.pack(side=tk.LEFT, fill="both", expand=True)
        text.config(state=tk.DISABLED)
        return box, text

    class GroupTab:
        """One rolling statistics/stream/detector view (classic RDS or one
        RDS2 stream).  Counts are kept here so the tab keeps its numbers even
        while the window is closed; widgets only exist while it is open."""

        def __init__(self, names, di_tracker=None, oda_carriers=None):
            self.names = names
            self.widgets = None
            self.di = di_tracker
            self.oda_carriers = oda_carriers or (lambda: ())
            self.reset()

        def colour_of(self, gt):
            return GROUP_COLOURS[group_class(gt, self.oda_carriers())]

        def reset(self):
            import time as _time
            self.started = _time.time()
            if self.di:
                self.di.reset()
            self.counts = {}
            self.seq = 0
            self.pending = []       # (seq, type, line) not yet drawn in the stream
            self.pending_raw = []   # raw lines not yet drawn in the HEX view
            self._drawn = {}

        def add(self, line):
            info = _group_type_of(line)
            if info is None:
                return None
            gt = info[0]
            self.counts[gt] = self.counts.get(gt, 0) + 1
            self.seq += 1
            self.pending.append((self.seq, gt, line))
            self.pending_raw.append(line)
            if self.di:
                self.di.add(info[1])
            return info

        def build(self, notebook, title, _tk):
            tab = _tk.Frame(notebook, bg="#0d1b2a")
            notebook.add(tab, text=title)

            tables_frame = _tk.Frame(tab, bg="#0d1b2a")
            tables_frame.pack(fill="x", padx=8, pady=(8, 4))
            stat_vars = {}
            type_labels = {}

            def build_stat_table(parent, heading, types):
                frame = _tk.Frame(parent, bg="#0d1b2a", relief=tk.RIDGE, bd=1)
                frame.pack(side=tk.LEFT, expand=True, fill="x", padx=4)
                _tk.Label(frame, text=heading, fg="#e94560", bg="#0d1b2a",
                         font=("Segoe UI", 9, "bold")).pack(pady=(2, 4))
                hdr = _tk.Frame(frame, bg="#0d1b2a")
                hdr.pack(fill="x", padx=4)
                for text, width in (("Type", 28), ("Prop", 20), ("Count", 7),
                                    ("Rate/s", 7)):
                    _tk.Label(hdr, text=text, fg="#888", bg="#0d1b2a",
                             width=width, font=("Consolas", 8),
                             anchor="w").pack(side=tk.LEFT)
                for idx, gt in enumerate(types):
                    row_bg = "#162236" if idx % 2 else "#0d1b2a"
                    row = _tk.Frame(frame, bg=row_bg)
                    row.pack(fill="x", padx=4, pady=0)
                    label = gt
                    if self.names.get(gt):
                        label = f"{gt} ({self.names[gt]})"
                    type_lbl = _tk.Label(row, text=label, fg="#ccc", bg=row_bg,
                                         width=28, font=("Consolas", 8),
                                         anchor="w")
                    type_lbl.pack(side=tk.LEFT)
                    type_labels[gt] = type_lbl
                    prop_var = _tk.StringVar(value="")
                    _tk.Label(row, textvariable=prop_var, fg="#8be9fd",
                             bg=row_bg, width=20, font=("Consolas", 8),
                             anchor="w").pack(side=tk.LEFT)
                    count_var = _tk.StringVar(value="")
                    _tk.Label(row, textvariable=count_var, fg="#50fa7b",
                             bg=row_bg, width=7, font=("Consolas", 8),
                             anchor="w").pack(side=tk.LEFT)
                    rate_var = _tk.StringVar(value="")
                    _tk.Label(row, textvariable=rate_var, fg="#f1fa8c",
                             bg=row_bg, width=7, font=("Consolas", 8),
                             anchor="w").pack(side=tk.LEFT)
                    stat_vars[gt] = (prop_var, count_var, rate_var)

            build_stat_table(tables_frame, "Type A Groups",
                             [f"{i}A" for i in range(16)])
            build_stat_table(tables_frame, "Type B Groups",
                             [f"{i}B" for i in range(16)])

            _tk.Button(tab, text="Clear", bg="#1a5276", fg="#fff",
                       font=("Segoe UI", 9),
                       command=self.clear).pack(pady=4)

            bottom = _tk.Frame(tab, bg="#0d1b2a")
            bottom.pack(fill="both", expand=True, padx=8, pady=(4, 8))
            stream_frame = _tk.Frame(bottom, bg="#000", relief=tk.SUNKEN, bd=1)
            stream_frame.pack(side=tk.LEFT, fill="both", expand=True, padx=(0, 4))
            _tk.Label(stream_frame, text="Group Stream", fg="#888", bg="#0d1b2a",
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
            box, stream_text = _scrolled_text(stream_frame, _tk, height=10)
            box.pack(fill="both", expand=True, padx=2, pady=2)
            for cls, colour in GROUP_COLOURS.items():
                stream_text.tag_configure(cls, foreground=colour)
            legend = _tk.Frame(stream_frame, bg="#0d1b2a")
            legend.pack(anchor="w")
            for cls, text in (("basic", "basic (PS/RT)"), ("oda", "ODA / TMC"),
                              ("other", "CT, EON, PIN...")):
                _tk.Label(legend, text="■ " + text, fg=GROUP_COLOURS[cls],
                         bg="#0d1b2a", font=("Segoe UI", 8)).pack(side=tk.LEFT,
                                                                 padx=(0, 8))
            side = _tk.Frame(bottom, bg="#0d1b2a")
            side.pack(side=tk.RIGHT, fill="y", padx=(4, 0))
            detector_frame, detector = _build_detector(side, _tk)
            detector_frame.pack(fill="x")
            di_var = None
            if self.di:
                di_frame, di_var = _build_di_panel(side, _tk)
                di_frame.pack(fill="x", pady=(4, 0))
                self.di.dirty = True

            self.widgets = {"stat_vars": stat_vars, "stream": stream_text,
                            "detector": detector, "di_var": di_var,
                            "type_labels": type_labels}
            self._drawn = {}
            return tab

        def build_hex(self, notebook, title, _tk):
            tab = _tk.Frame(notebook, bg="#0d1b2a")
            notebook.add(tab, text=title)
            box, hex_text = _scrolled_text(tab, _tk)
            box.pack(fill="both", expand=True, padx=4, pady=4)
            self.widgets["hex"] = hex_text

        def clear(self):
            self.reset()
            w = self.widgets
            if not w:
                return
            for prop_var, count_var, rate_var in w["stat_vars"].values():
                prop_var.set("")
                count_var.set("")
                rate_var.set("")
            for lbl in w["detector"].values():
                lbl.config(bg=DETECTOR_OFF[0], fg=DETECTOR_OFF[1])
            if w.get("di_var"):
                w["di_var"].set("\n".join(self.di.lines()))
            _clear_text(w["stream"])
            if "hex" in w:
                _clear_text(w["hex"])

        def refresh(self):
            import time as _time
            w = self.widgets
            if not w:
                return
            if self.counts != self._drawn:
                self._drawn = dict(self.counts)
                total = sum(self.counts.values())
                elapsed = max(_time.time() - self.started, 1e-3)
                for gt, (prop_var, count_var, rate_var) in w["stat_vars"].items():
                    count = self.counts.get(gt, 0)
                    if total and count:
                        pct = count / total
                        filled = int(pct * 16)
                        prop_var.set("█" * filled + "░" * (16 - filled)
                                     + f" {pct * 100:5.1f}%")
                        count_var.set(f"{count:6d}")
                        rate_var.set(f"{count / elapsed:5.2f}")
                    else:
                        prop_var.set("")
                        count_var.set("")
                        rate_var.set("")
                for gt, lbl in w["detector"].items():
                    on = self.counts.get(gt, 0) > 0
                    if on:
                        lbl.config(bg=self.colour_of(gt), fg="#000")
                    else:
                        lbl.config(bg=DETECTOR_OFF[0], fg=DETECTOR_OFF[1])
                    w["type_labels"][gt].config(
                        fg=self.colour_of(gt) if on else "#ccc")
            if self.di and self.di.dirty and w.get("di_var"):
                w["di_var"].set("\n".join(self.di.lines()))
                self.di.dirty = False
            if self.pending:
                entries = self.pending[-LIVE_TEXT_ROWS:]
                self.pending.clear()
                rows = []
                for seq, gt, line in entries:
                    blocks = " ".join(p.upper() for p in line.split()[:4])
                    rows.append((f"{seq:7d}  {gt:>4}  {blocks}",
                                 group_class(gt, self.oda_carriers())))
                _append_text(w["stream"], rows)
            if self.pending_raw and "hex" in w:
                raw = self.pending_raw[-LIVE_TEXT_ROWS:]
                self.pending_raw.clear()
                rows = []
                for line in raw:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        rows.append("  ".join(f"0x{p.upper():>4}"
                                              for p in parts[:4]))
                _append_text(w["hex"], rows)
            else:
                self.pending_raw.clear()

    classic_tab = GroupTab(
        type_names, DiTracker(),
        oda_carriers=lambda: {a["carrier"] for a in oda_tracker.apps.values()})
    rds2_tabs = {s: GroupTab(rds2_type_names) for s in (1, 2, 3)}
    all_tabs = [classic_tab] + list(rds2_tabs.values())

    def _open_live_window():
        nonlocal live_win
        if live_win is not None and live_win.winfo_exists():
            live_win.lift()
            live_win.focus_force()
            return

        _tk = tk
        from tkinter import ttk

        live_win = _tk.Toplevel(root)
        live_win.title("RDS Group Analyzer")
        live_win.configure(bg="#0f3460")
        live_win.geometry("1100x700")

        notebook = ttk.Notebook(live_win)
        notebook.pack(fill="both", expand=True, padx=4, pady=4)

        # --- Tab 1: Group Statistics (classic RDS, stream 0) ---
        classic_tab.build(notebook, "Group Statistics", _tk)

        # --- Tab 2: ODA AID — what the 3A groups advertise ---
        oda_tab = _tk.Frame(notebook, bg="#0d1b2a")
        notebook.add(oda_tab, text="ODA AID")
        _tk.Label(oda_tab, text="Open Data Applications announced in type 3A groups",
                 fg="#e94560", bg="#0d1b2a",
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(8, 2))
        import tkinter.font as _tkfont
        style = ttk.Style(live_win)
        row_h = _tkfont.Font(live_win, font=("Consolas", 9)).metrics(
            "linespace") + 6  # fits the text at any display scaling
        style.configure("Oda.Treeview", background="#000", fieldbackground="#000",
                        foreground="#0f0", font=("Consolas", 9), rowheight=row_h)
        style.configure("Oda.Treeview.Heading", font=("Segoe UI", 9, "bold"))
        cols = ("aid", "app", "group", "prop", "rate")
        oda_tree = ttk.Treeview(oda_tab, columns=cols, show="headings",
                                style="Oda.Treeview", height=8)
        for cid, text, width, anchor in (
                ("aid", "AID", 70, "center"),
                ("app", "Application", 320, "w"),
                ("group", "Group", 60, "center"),
                ("prop", "Proportion", 220, "w"),
                ("rate", "Rate/s", 70, "e")):
            oda_tree.heading(cid, text=text)
            oda_tree.column(cid, width=width, anchor=anchor, stretch=(cid == "app"))
        oda_tree.pack(fill="x", padx=8, pady=4)
        oda_note = _tk.StringVar(value="No 3A groups received yet")
        _tk.Label(oda_tab, textvariable=oda_note, fg="#888", bg="#0d1b2a",
                 font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=8)
        oda_bottom = _tk.Frame(oda_tab, bg="#0d1b2a")
        oda_bottom.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        oda_detector_frame, oda_detector = _build_detector(oda_bottom, _tk)
        oda_detector_frame.pack(side=tk.RIGHT, fill="y")

        # --- Tab 3: Graph ---
        graph_tab = _tk.Frame(notebook, bg="#0d1b2a")
        notebook.add(graph_tab, text="Graph")
        graph_canvas = _tk.Canvas(graph_tab, bg="#0d1b2a", highlightthickness=0)
        graph_canvas.pack(fill="both", expand=True, padx=4, pady=4)

        # --- HD Data: HD Radio's version of Group Statistics ---
        hd_tab = _tk.Frame(notebook, bg="#0d1b2a")
        notebook.add(hd_tab, text="HD Data")
        hd_head = _tk.Label(hd_tab, text="", fg="#e94560", bg="#0d1b2a",
                            font=("Segoe UI", 10, "bold"), anchor="w")
        hd_head.pack(fill="x", padx=8, pady=(8, 2))
        _tk.Label(hd_tab, text="Counts each kind of HD data the decoder "
                  "receives (SIS station info, PSD song info per HD channel, "
                  "LOT logos/art, alerts, maps, sync). Resets on retune.",
                  fg="#888", bg="#0d1b2a", font=("Segoe UI", 8),
                  anchor="w").pack(fill="x", padx=8)
        hd_tree = ttk.Treeview(hd_tab, columns=("type", "prop", "count"),
                               show="headings", style="Oda.Treeview",
                               height=10)
        for col, text, width in (("type", "HD data", 160),
                                 ("prop", "Proportion", 200),
                                 ("count", "Count", 80)):
            hd_tree.heading(col, text=text)
            hd_tree.column(col, width=width, anchor="w")
        hd_tree.pack(fill="x", padx=8, pady=4)
        _tk.Label(hd_tab, text="Recent HD data", fg="#e94560", bg="#0d1b2a",
                  font=("Segoe UI", 9, "bold"), anchor="w").pack(
            fill="x", padx=8)
        hd_box, hd_log = _scrolled_text(hd_tab, _tk)
        hd_box.pack(fill="both", expand=True, padx=4, pady=4)
        live_win._hd = {"head": hd_head, "tree": hd_tree, "log": hd_log,
                        "drawn": None}

        # --- Tab 4: Group Content HEX ---
        classic_tab.build_hex(notebook, "Group Content HEX", _tk)

        # --- Tabs 5-7: RDS2 streams 1-3 (from the FM Tuner's G1:-G3: records) ---
        for stream, gtab in rds2_tabs.items():
            gtab.build(notebook, f"RDS2 Stream {stream}", _tk)

        live_win._oda_tree = oda_tree
        live_win._oda_note = oda_note
        live_win._oda_detector = oda_detector
        live_win._graph_canvas = graph_canvas
        live_win._graph_drawn = {}

        def _on_live_close():
            nonlocal live_win
            live_win_refs[0] = None
            for gtab in all_tabs:
                gtab.widgets = None
            try:
                live_win.destroy()
            except Exception:
                pass
            live_win = None
        live_win.protocol("WM_DELETE_WINDOW", _on_live_close)
        live_win_refs[0] = live_win
        _refresh_live_window()

    def _reset_live_for_station_change():
        """Clear all live data when the station (PI code) changes."""
        for gtab in all_tabs:
            gtab.clear()
        oda_tracker.reset()
        lw = live_win_refs[0]
        if lw is None or not lw.winfo_exists():
            return
        lw._graph_canvas.delete("all")
        lw._graph_drawn = {}
        _refresh_oda(lw)

    def _clear_live_stats():
        _reset_live_for_station_change()
        live_last_size[0] = 0
        live_partial_line[0] = ""

    def _refresh_oda(lw):
        tree = lw._oda_tree
        rows = oda_tracker.rows()
        existing = tree.get_children()
        for i, (aid, name, carrier, count, prop, rate) in enumerate(rows):
            filled = int(prop * 16)
            values = (f"{aid:04X}", name,
                      carrier if carrier != "0A" else "—",
                      "█" * filled + "░" * (16 - filled) + f" {prop * 100:5.1f}%",
                      f"{rate:5.2f}")
            if i < len(existing):
                tree.item(existing[i], values=values)
            else:
                tree.insert("", tk.END, values=values)
        for iid in existing[len(rows):]:
            tree.delete(iid)
        announced = oda_tracker.group_counts.get("3A", 0)
        if announced:
            lw._oda_note.set(f"{len(rows)} application(s) announced in "
                             f"{announced} type 3A groups")
        else:
            lw._oda_note.set("No 3A groups received yet")
        for gt, lbl in lw._oda_detector.items():
            on = oda_tracker.group_counts.get(gt, 0) > 0
            colours = DETECTOR_ON if on else DETECTOR_OFF
            lbl.config(bg=colours[0], fg=colours[1])

    def _refresh_graph(lw):
        gc = classic_tab.counts
        if gc == lw._graph_drawn:
            return
        lw._graph_drawn = dict(gc)
        graph_canvas = lw._graph_canvas
        graph_canvas.delete("all")
        total = sum(gc.values())
        if gc and total > 0:
            w = graph_canvas.winfo_width() or 800
            h = graph_canvas.winfo_height() or 300
            bar_w = w / 32
            max_count = max(gc.values())
            sorted_types = sorted(gc.keys())
            for i, gt in enumerate(sorted_types):
                bar_h = (gc[gt] / max_count) * (h - 40)
                x = i * (w / len(sorted_types))
                graph_canvas.create_rectangle(x + 2, h - 20 - bar_h,
                                              x + bar_w - 2, h - 20,
                                              fill="#8be9fd", outline="#0d1b2a")
                graph_canvas.create_text(x + bar_w / 2, h - 10, text=gt,
                                         fill="#888", font=("Consolas", 7))

    def _refresh_live_window():
        lw = live_win_refs[0]
        if lw is None or not lw.winfo_exists():
            return
        for gtab in all_tabs:
            gtab.refresh()
        _refresh_oda(lw)
        _refresh_graph(lw)
        _refresh_hd(lw)

    def _refresh_hd(lw):
        w = getattr(lw, "_hd", None)
        if not w:
            return
        view = hd_analyzer_view(hd_latest[0])
        if view == w["drawn"]:
            return
        w["drawn"] = view
        head, rows, log = view
        hd = hd_latest[0] or {}
        mer_colour = hd_mer_grade(hd.get("mer_db"))[1] \
            if hd.get("synced") and isinstance(hd.get("mer_db"), (int, float)) \
            else "#e94560"
        w["head"].config(text=head, fg=mer_colour)
        tree = w["tree"]
        tree.delete(*tree.get_children())
        for row in rows:
            tree.insert("", tk.END, values=row)
        text = w["log"]
        at_end = _following(text)
        text.config(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert(tk.END, "\n".join(log))
        text.config(state=tk.DISABLED)
        if at_end:
            text.see(tk.END)

    # Add menu entry to open the live window
    if not LITE:
        view_menu.add_separator()
        view_menu.add_command(label="Open Group Analyzer Window",
                              command=lambda: _open_live_window())

    # Fast live-window update loop
    live_last_size = [0]
    live_partial_line = [""]  # buffer for incomplete last line

    # Initialize from current file size so we only show NEW groups as they arrive
    try:
        _init_sp = Path(spy_file) if spy_file else None
        if _init_sp and _init_sp.exists():
            live_last_size[0] = _init_sp.stat().st_size
    except Exception:
        pass

    def _live_fast_poll():
        """Feed new groups (TCP or spy file) into the analyzer tabs and redraw."""
        # Detect station change — clear all live data when PI code changes
        cur_pi = current_pi[0] if current_pi else ""
        if cur_pi and live_current_pi[0] and cur_pi != live_current_pi[0]:
            _reset_live_for_station_change()
        live_current_pi[0] = cur_pi

        try:
            new_lines = []
            if live_pending_lines:
                # Groups arriving over TCP (FM Tuner / RDS input)
                new_lines = list(live_pending_lines)
                live_pending_lines.clear()
            sp = Path(spy_file) if (spy_file and input_mode.get() == "spy") else None
            if sp and sp.exists():
                cur_size = sp.stat().st_size
                if cur_size > live_last_size[0]:
                    with open(sp, 'rb') as f:
                        f.seek(live_last_size[0])
                        chunk = f.read()
                    live_last_size[0] = cur_size
                    text = live_partial_line[0] + chunk.decode('utf-8', errors='ignore')
                    lines = text.split('\n')
                    live_partial_line[0] = lines[-1] if lines else ""
                    new_lines.extend(lines[:-1] if lines else [])
                elif cur_size < live_last_size[0]:
                    # File was truncated/replaced — reset
                    live_last_size[0] = 0
                    live_partial_line[0] = ""
                    _reset_live_for_station_change()

            for line in new_lines:
                info = classic_tab.add(line)
                if info:
                    oda_tracker.add_group(*info)

            if live_pending_rds2:
                for stream, line in live_pending_rds2:
                    gtab = rds2_tabs.get(stream)
                    if gtab:
                        gtab.add(line)
                live_pending_rds2.clear()

            _refresh_live_window()
        except Exception:
            pass

        root.after(100, _live_fast_poll)

    root.after(100, _live_fast_poll)

    # Cleanup on close -- stop RFtap listener if running
    def _on_closing():
        if gps_state["reader"]:
            gps_state["reader"].stop()
        if rftap_input:
            rftap_input.stop()
        if tuner_link:
            tuner_link.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_closing)
    if _read_settings().get("gps_follow_away"):
        root.after(1500, _gps_start)
    root.after(2000, _gps_poll)

    if start_input == "tuner":
        # Default build: come up talking to the FM Tuner (RDS on 7376,
        # telemetry on 7373); both links keep retrying until it is running.
        root.after(300, _switch_to_tuner)

    root.after(1200, _first_run_home)
    root.mainloop()


# ===========================================================================
# Main
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Real-time RDS Spy monitor with station logos.")
    parser.add_argument("--spy-file", "-s", default=DEFAULT_SPY_FILE,
                        help=f"Path to the .spy file (default: {DEFAULT_SPY_FILE})")
    parser.add_argument("--watch", "-w", default=DEFAULT_WATCH_DIR,
                        help=f"Folder to watch (default: {DEFAULT_WATCH_DIR})")
    parser.add_argument("--spy-filename", default=DEFAULT_SPY_FILENAME,
                        help="Name of the .spy file (default: recording.spy)")
    parser.add_argument("--logo-dir", "-l", default=DEFAULT_LOGO_DIR,
                        help=f"Logo directory (default: {DEFAULT_LOGO_DIR})")
    parser.add_argument("--pi-file", "-p", default=DEFAULT_PI_OVERRIDES_FILE,
                        help=f"PI code overrides file (default: {DEFAULT_PI_OVERRIDES_FILE})")
    parser.add_argument("--input", choices=("tuner", "spy"), default="tuner",
                        help="Input at launch: 'tuner' connects to the FM Tuner "
                             "on localhost (default); 'spy' watches the .spy file")
    args = parser.parse_args()

    # Determine spy file path
    spy_file = args.spy_file
    if not Path(spy_file).exists():
        # Try watch dir + spy filename
        alt = Path(args.watch) / args.spy_filename
        if alt.exists():
            spy_file = str(alt)

    # Create logo dir if it doesn't exist
    Path(args.logo_dir).mkdir(parents=True, exist_ok=True)

    # PI -> call: DX Mapper's logs, then stations.txt (or --pi-file)
    global _PI_OVERRIDES
    _PI_OVERRIDES = None if args.pi_file == DEFAULT_PI_OVERRIDES_FILE else {
        **load_pi_overrides(args.pi_file), **mapper_log()[0]}

    print(f"RDS Monitor")
    print(f"  Spy file:  {spy_file}")
    print(f"  Logo dir:  {args.logo_dir}")
    print(f"  PI codes:  DX Mapper log + {args.pi_file} ({len(get_pi_overrides())} known)")
    print(f"  Poll:      {POLL_INTERVAL}s")
    print(f"  Input:     {args.input}")
    print()

    run_gui(spy_file, args.logo_dir, args.watch, args.spy_filename,
            start_input=args.input)


if __name__ == "__main__":
    main()
