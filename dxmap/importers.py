"""Station imports: Google Sheets / CSV / TSV logs, RDS Spy .spy files and
.txt files (RDS Spy group text, RDS Monitor reports, or tabular text)."""

import csv
import io
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .geo import pi_to_callsign

# Map field -> header words that mean it (lower-case, punctuation stripped).
FIELD_SYNONYMS = {
    "freq_mhz": ("freq", "frequency", "mhz", "freq mhz", "frequency mhz",
                 "fm", "dial", "khz"),
    "callsign": ("call", "callsign", "call sign", "calls", "call letters",
                 "station", "station call"),
    "pi": ("pi", "pi code", "rds pi", "picode"),
    "ps": ("ps", "rds ps", "ps name", "program service"),
    "rt": ("rt", "radiotext", "radio text", "rds rt", "rds radiotext"),
    "city": ("city", "town", "community", "city of license", "col"),
    "state": ("state", "st", "prov", "province", "state prov"),
    "country": ("country", "itu", "cty"),
    "location": ("location", "city state", "qth", "market", "city st"),
    "lat": ("lat", "latitude", "tx lat"),
    "lon": ("lon", "long", "lng", "longitude", "tx lon"),
    "date": ("date", "log date", "date heard", "day", "first heard"),
    "time": ("time", "utc", "time utc", "local time", "time heard"),
    "datetime": ("datetime", "date time", "timestamp", "heard"),
    "mode": ("mode", "propagation", "prop", "skip", "type"),
    "pty": ("pty", "format", "genre"),
    "signal_dbfs": ("signal", "dbfs", "signal dbfs", "strength"),
    "hd": ("hd", "hd radio", "hd channel", "hd sub", "hd subchannel",
           "subchannel"),
}
IGNORE = "-"
# Header words that are never a log field even when a synonym is inside
# (e.g. "Time Decoding" is not the time heard).
NOT_A_FIELD = ("decod", "capab", "range")
FIELDS = ("freq_mhz", "callsign", "pi", "ps", "rt", "city", "state", "country",
          "location", "lat", "lon", "date", "time", "datetime", "mode",
          "pty", "signal_dbfs", "hd")
FIELD_LABELS = {
    "freq_mhz": "Frequency (MHz)", "callsign": "Callsign", "pi": "PI code",
    "ps": "PS name", "rt": "RadioText", "city": "City", "state": "State/Prov",
    "country": "Country", "location": "Location (City, ST)",
    "lat": "Latitude", "lon": "Longitude", "date": "Date", "time": "Time",
    "datetime": "Date + time", "mode": "Propagation / mode",
    "pty": "Format / PTY", "signal_dbfs": "Signal",
    "hd": "HD / subchannel (HD2…)",
}


def _norm_header(h):
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def google_sheet_csv_url(url):
    """Google Sheets share/edit link -> CSV export link (same tab)."""
    m = re.search(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    if "/pub" in url and "output=csv" in url:
        return url
    gid = re.search(r"[#&?]gid=(\d+)", url)
    out = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
    return out + (f"&gid={gid.group(1)}" if gid else "")


def parse_table(text):
    """(headers, rows) from CSV/TSV/semicolon text; skips leading blanks."""
    text = text.lstrip("\ufeff")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], []
    sample = "\n".join(lines[:20])
    counts = {d: sample.count(d) for d in (",", "\t", ";", "|")}
    delim = max(counts, key=counts.get)
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delim))
    # The header is the first row with at least two recognised words.
    header_idx = 0
    for i, row in enumerate(rows[:10]):
        if sum(1 for c in row if detect_field(c)) >= 2:
            header_idx = i
            break
    headers = [h.strip() for h in rows[header_idx]]
    body = [r for r in rows[header_idx + 1:] if any(c.strip() for c in r)]
    return headers, body


def detect_field(header):
    h = _norm_header(header)
    if not h:
        return None
    for field in FIELDS:
        if h in FIELD_SYNONYMS[field]:
            return field
    if any(w in h for w in NOT_A_FIELD):
        return None
    for field in FIELDS:
        for syn in FIELD_SYNONYMS[field]:
            if len(syn) > 3 and syn in h:
                return field
    return None


# Which imported column fills which log book column (first match wins).
LOGBOOK_HEADERS = (
    ("freq", r"^(freq|frequency|mhz|dial)\b"), ("callsign", r"^call"),
    ("hd_channels", r"^hd\s*(sub)?channels?\b"), ("hd", r"^hd\b|\bhd radio"), ("slogan", r"slogan|branding"),
    ("pi", r"^pi\b|\bpi code"),
    ("city", r"^(city|town|community)\b|city of license"),
    ("state", r"^(state|st|prov|province)\b"), ("distance", r"dist"),
    ("range_type", r"range"), ("owner", r"owner|licensee"),
    ("time_decoding", r"time decod|clock|^ct\b"),
    ("rt_plus", r"rt\s*\+|rt\s*plus"), ("radiotext", r"radio\s*text|^rt$"),
    ("stream", r"stream|webcast|^listen"),
    ("website", r"web\s*site|home\s*page|^url$"),
    ("group_seq", r"group\s*seq|fingerprint"),
)


def logbook_column(header):
    """The log book column an imported header fills, or None."""
    h = (header or "").strip()
    return next((col for col, pat in LOGBOOK_HEADERS
                 if re.search(pat, h, re.I)), None)


def keep_header(header):
    """Only the log book's own columns (and Fed by) are kept from a log;
    notes and other columns are not."""
    return bool(logbook_column(header)) or (
        header or "").strip().lower() == "fed by"


def detect_mapping(headers):
    """{column index: field} using header names; first match wins. A column
    that is neither a field nor a log book column is ignored."""
    mapping, used = {}, set()
    for i, h in enumerate(headers):
        f = detect_field(h)
        if f and f not in used:
            mapping[i] = f
            used.add(f)
        elif not keep_header(h):
            mapping[i] = IGNORE
    return mapping


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d.%m.%Y",
                 "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%m-%d-%Y")
_TIME_FORMATS = ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M:%S %p", "%H%M")


def parse_when(date_text=None, time_text=None):
    """Epoch seconds (local time) from date/time cells, or None."""
    date_text = (date_text or "").strip().replace(",", "")
    time_text = (time_text or "").strip().upper().replace("UTC", "").strip()
    if not date_text:
        return None
    if " " in date_text and not time_text:
        for fmt in _DATE_FORMATS:
            for tfmt in _TIME_FORMATS:
                try:
                    return datetime.strptime(date_text, f"{fmt} {tfmt}").timestamp()
                except ValueError:
                    continue
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(date_text, fmt)
            break
        except ValueError:
            continue
    else:
        try:
            d = datetime.fromisoformat(date_text)
        except ValueError:
            return None
    for tfmt in _TIME_FORMATS:
        try:
            t = datetime.strptime(time_text, tfmt)
            d = d.replace(hour=t.hour, minute=t.minute, second=t.second)
            break
        except ValueError:
            continue
    return d.timestamp()


def rows_to_records(headers, rows, mapping):
    """Records for Store.log() from table rows and a {col: field} map."""
    mapping = {int(k): v for k, v in mapping.items() if v}
    ignored = {i for i, v in mapping.items() if v == IGNORE}
    records = []
    for row in rows:
        rec, extra = {}, {}
        for i, cell in enumerate(row):
            cell = cell.strip()
            if not cell or i in ignored:
                continue
            field = mapping.get(i)
            if field:
                rec[field] = cell
            # The log book columns, under their own headers.
            if i < len(headers) and keep_header(headers[i]):
                extra[headers[i].strip()] = cell
        if rec.get("location") and not rec.get("city") and "," in rec["location"]:
            city, _, state = rec["location"].rpartition(",")
            rec.setdefault("city", city.strip())
            rec.setdefault("state", state.strip())
        when = parse_when(rec.pop("datetime", None) or rec.pop("date", None),
                          rec.pop("time", None))
        rec.pop("date", None)
        rec.pop("time", None)
        if when:
            rec["heard_at"] = when
        if rec.get("pty") and not str(rec["pty"]).strip().isdigit():
            rec.pop("pty")  # text format: already kept under its own header
        call = (rec.get("callsign") or "").upper()
        m = re.match(r"^([KWCX][A-Z]{2,3}(?:-FM)?|[KW]\d{3}[A-Z]{2})"
                     r"(?:[\s-]*HD\s*-?\s*([2-8]))?\b", call)
        if call and not m and not rec.get("pi"):
            rec.pop("callsign")  # not a call: kept under its own header
        elif m:  # an HD subchannel keeps its -HD2
            rec["callsign"] = m.group(1) + (f"-HD{m.group(2)}" if m.group(2) else "")
        rec["extra"] = extra
        if rec.get("callsign") or rec.get("pi"):
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# RDS Spy groups (.spy, or .txt with the same hex lines)
# ---------------------------------------------------------------------------

_HEX_LINE = re.compile(
    r"^\s*(?:#S[1-3]\s*)?([0-9A-Fa-f]{4}|----)\s+([0-9A-Fa-f]{4}|----)\s+"
    r"([0-9A-Fa-f]{4}|----)\s+([0-9A-Fa-f]{4}|----)(?:\s*@\s*(.*))?$")
_FREQ_RE = re.compile(r"(?<![\d.])((?:8[7-9]|9\d|10[0-8])\.\d{1,2})\s*(?:MHz)?",
                      re.I)
_SPY_TIME = re.compile(r"(\d{4})[/-](\d{2})[/-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def looks_like_spy(text):
    hits = sum(1 for ln in text.splitlines()[:200] if _HEX_LINE.match(ln))
    return hits >= 5


def _spy_time(text):
    m = _SPY_TIME.search(text or "")
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).timestamp()
    except ValueError:
        return None


def parse_spy(text, filename="", min_groups=3):
    """One record per PI seen in an RDS Spy group log.

    A PI needs `min_groups` groups (block A) to count, so single bit-error
    PIs don't become stations.  PS is the most-voted character per slot.
    """
    pi_count = Counter()
    ps_votes = defaultdict(lambda: [Counter() for _ in range(8)])
    pty_votes = defaultdict(Counter)
    first_seen, last_seen = {}, {}
    freq = None
    current_time = None
    m = _FREQ_RE.search(filename or "")
    if m:
        freq = float(m.group(1))
    for line in text.splitlines():
        mm = _HEX_LINE.match(line)
        if not mm:
            if freq is None and ("<" in line or "#" in line or "freq" in line.lower()):
                fm = _FREQ_RE.search(line)
                if fm:
                    freq = float(fm.group(1))
            t = _spy_time(line)
            if t:
                current_time = t
            continue
        a, b, _c, d, stamp = mm.groups()
        if a == "----":
            continue
        pi = a.upper()
        t = _spy_time(stamp) or current_time
        pi_count[pi] += 1
        if t:
            first_seen.setdefault(pi, t)
            last_seen[pi] = t
        if b == "----":
            continue
        bv = int(b, 16)
        pty_votes[pi][(bv >> 5) & 0x1F] += 1
        if bv >> 12 == 0 and d != "----":
            seg = bv & 3
            dv = int(d, 16)
            for k, ch in enumerate((dv >> 8, dv & 0xFF)):
                if 0x20 <= ch < 0x7F:
                    ps_votes[pi][seg * 2 + k][chr(ch)] += 1
    records = []
    for pi, n in pi_count.most_common():
        if n < min_groups:
            continue
        ps = "".join(v.most_common(1)[0][0] if v else " "
                     for v in ps_votes[pi]).strip()
        pty = pty_votes[pi].most_common(1)[0][0] if pty_votes[pi] else None
        rec = {"pi": pi, "callsign": pi_to_callsign(pi), "ps": ps or None,
               "pty": pty, "freq_mhz": freq,
               "extra": {"Groups in file": str(n)}}
        if filename:
            rec["extra"]["Imported from"] = filename
        if pi in first_seen:
            rec["heard_at"] = first_seen[pi]
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# RDS Monitor report (.txt)
# ---------------------------------------------------------------------------

_REPORT_KEYS = {
    "frequency": "freq_mhz", "location": "location", "ps name": "ps",
    "pty": "pty", "signal": "signal_dbfs", "rds level": "rds_level_db",
    "usable info": "usable_info", "callsign": "callsign",
}


_REPORT_LINE = re.compile(r"^ {2}(?! )([^=\n]*?\S)\s*=\s*(.*\S)\s*$")


def parse_report_info(text):
    """{label: value} of every "  Label = value" line of an RDS Monitor
    report, in report order (empty/"(none)" values left out)."""
    info = {}
    for line in text.splitlines():
        m = _REPORT_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if re.fullmatch(r"\((none|unknown)\)|None|---|0/64 received", val):
            continue
        info.setdefault(key, val)
    return info


def _fraction(text, label):
    m = re.search(r"^ {2}%s\s*= (\d+)/(\d+)" % re.escape(label), text, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _badge(name, done, count=None):
    return {"name": name, "state": "ok" if done else "part", "count": count}


def parse_report_badges(text):
    """The monitor's Decode badges for one report, only for what was decoded:
    [{"name": "AF", "state": "ok"|"part", "count": 5}, ...] in badge order.
    "ok" = complete (green), "part" = partly received (yellow)."""
    badges = []
    info = parse_report_info(text)
    for name, label in (("PS", "PS segs"), ("RT", "RT chars")):
        got = _fraction(text, label)
        if got and got[0]:
            badges.append(_badge(name, got[0] >= got[1]))
        elif not got and info.get("PS Name" if name == "PS" else "RT"):
            badges.append(_badge(name, True))
    if re.match(r"\d+", info.get("PTY", "")):
        badges.append(_badge("PTY", True))
    got = _fraction(text, "PTYN segs")
    if got and got[0]:
        badges.append(_badge("PTYN", got[0] >= got[1]))
    elif not got and info.get("PTYN"):
        badges.append(_badge("PTYN", True))
    features = parse_report_features(text)
    if info.get("RT+ Active") == "Yes" or "RT+" in features["decoded"]:
        badges.append(_badge("RT+", True))
    msgs = re.search(r"^ {2}Messages\s*= (\d+)", text, re.M)
    groups = re.search(r"^ {2}8A groups\s*= (\d+)", text, re.M)
    if msgs and int(msgs.group(1)):
        badges.append(_badge("TMC", True, int(msgs.group(1))))
    elif "TMC" in features["decoded"]:
        badges.append(_badge("TMC", True))
    elif groups and int(groups.group(1)):
        badges.append(_badge("TMC", False, int(groups.group(1))))
    af = re.match(r"(\d+)(?:/\d+)? (complete|partial)", info.get("AF status", ""))
    if af and int(af.group(1)):
        badges.append(_badge("AF", af.group(2) == "complete", int(af.group(1))))
    eon = re.match(r"(\d+) other", info.get("EON", ""))
    if eon and int(eon.group(1)):
        nets = re.findall(r"^ {4}PI [0-9A-Fa-f]{4}\s+PS=(\S)", text, re.M)
        badges.append(_badge("EON", bool(nets) and "-" not in nets,
                             int(eon.group(1))))
    if _field(text, "Date/Time"):
        badges.append(_badge("CT", True))
    for name in features["decoded"]:
        if name not in ("RT+", "TMC"):
            badges.append(_badge(name, True))
    return badges


_ODA_SHORT = {"RT+ (RadioText Plus)": "RT+", "RDS-TMC (ALERT-C)": "TMC",
              "RadioText Plus (RT+)": "RT+", "RDS-TMC: ALERT-C": "TMC",
              "Enhanced RadioText (eRT)": "eRT",
              "RT+ for enhanced RadioText (eRT+)": "eRT+",
              "Cross referencing DAB within RDS": "DAB cross-ref"}


def parse_report_features(text):
    """What a report shows the station sends: applications actually decoded
    ({"RT+": "13A", ...}) and every AID announced in 3A groups, including
    unregistered/abnormal ones (flagged known=False)."""
    decoded, advertised, section = {}, [], ""
    for line in text.splitlines():
        h = re.match(r"^\s*--- (.*?) ---\s*$", line)
        if h:
            section = h.group(1)
            continue
        if section == "Decoded ODA applications":
            m = re.match(r"^ {2}(\S.*?) -- decoded from (\S+)", line)
            if m:
                decoded[_ODA_SHORT.get(m.group(1), m.group(1))] = m.group(2)
        elif section.startswith("Announced ODA registrations"):
            m = re.match(r"^ {2}0x([0-9A-Fa-f]{4}) -> (\d+[AB]) -- (.*\S)",
                         line)
            if not m:
                continue
            aid, group, name = m.group(1).upper(), m.group(2), m.group(3)
            if not any((a["aid"], a["group"]) == (aid, group)
                       for a in advertised):
                advertised.append({"aid": aid, "group": group, "app": name,
                                   "known": not name.lower().startswith(
                                       "unknown")})
        elif re.match(r"^ {2}RT\+ Active\s*=\s*Yes", line):
            decoded.setdefault("RT+", None)
        elif section.startswith("TMC"):
            m = re.match(r"^ {2}Messages\s*=\s*(\d+)", line)
            if m and int(m.group(1)):
                decoded.setdefault("TMC", "8A")
    advertised.sort(key=lambda a: not a["known"])
    return {"decoded": decoded, "advertised": advertised,
            "features": parse_report_enhancements(text, decoded, advertised)}


def _field(text, key):
    m = re.search(r"^ {2}%s\s*= *(.*?)\s*$" % re.escape(key), text, re.M)
    v = m.group(1) if m else ""
    return "" if v.startswith("(none") else v


def parse_report_enhancements(text, decoded, advertised):
    """Everything in a report that enhances the station, best first:
    [{"name", "state": "decoded"|"seen", "detail"}]. "decoded" = the monitor
    decoded it; "seen" = its data was received but there is no decoder yet
    (an announced app's group carried traffic, HD sidebands, 15A groups).
    Anything neither decoded nor seen is left out."""
    out = []

    def add(name, state, detail=""):
        if not any(f["name"] == name for f in out):
            out.append({"name": name, "state": state, "detail": detail})
    for name in sorted(decoded, key=lambda n: (n not in ("RT+", "TMC"),
                                               n != "RT+", n)):
        add(name, "decoded", f"decoded from {decoded[name]}"
            if decoded[name] else "decoded")
    af = re.match(r"(\d+)(/\d+)? (complete|partial)", _field(text, "AF status"))
    if af and int(af.group(1)):
        add("AF", "decoded", f"{af.group(1)}{af.group(2) or ''} frequencies, "
            f"{af.group(3)}")
    eon = re.match(r"(\d+) other", _field(text, "EON"))
    if eon and int(eon.group(1)):
        add("EON", "decoded", f"{eon.group(1)} other network(s)")
    if _field(text, "PTYN"):
        add("PTYN", "decoded", _field(text, "PTYN"))
    if _field(text, "Date/Time"):
        add("CT (clock time)", "decoded", _field(text, "Date/Time"))
    if _field(text, "TP") == "1":
        add("TP", "decoded", "traffic programme")
    if _field(text, "TA") == "1":
        add("TA", "decoded", "traffic announcement was on")
    di = _field(text, "DI")
    if re.match(r"\d", di):
        flags = [t for k, t in (("d0 stereo", "stereo"),
                                ("d1 artificial head", "artificial head"),
                                ("d2 compression", "compressed"),
                                ("d3 dynamic PTY", "dynamic PTY"))
                 if re.search(r"^ +%s\s*= 1" % k, text, re.M)]
        add("DI", "decoded", ", ".join(flags) or di)
    hd = _field(text, "HD Radio (IBOC)")
    if hd and not hd.startswith("no "):
        add("HD Radio", "seen" if "not decoded" in hd or "indicator" in hd
            else "decoded", hd)
    raw = {m.group(1): int(m.group(2)) for m in re.finditer(
        r"^ {2}(\d+[AB]): (\d+) groups", text, re.M)}
    long_ps = re.search(r"^ +15A = .*\((\d+)\)", text, re.M)
    if _field(text, "Long PS"):
        add("Long PS", "decoded", _field(text, "Long PS"))
    elif long_ps and int(long_ps.group(1)):
        add("Long PS", "seen",
            f"{long_ps.group(1)} 15A groups received, nothing decoded")
    for a in advertised:
        short = _ODA_SHORT.get(a["app"], a["app"])
        if (a["known"] and short not in decoded
                and raw.get(a["group"], 0) > 0):
            add(short, "seen", f"0x{a['aid']} on {a['group']}: "
                f"{raw[a['group']]} groups received, no decoder yet")
    return out


HD_CHANNEL_CHECKS = ("HD audio", "Channel name", "Program type", "Song / artist",
                     "Album", "Album art", "Station logo")
HD_STATION_CHECKS = ("Station name", "Slogan", "Station message",
                     "Facility ID", "Station location", "Emergency alert",
                     "Traffic map", "Weather map")
_HD_STATION_KEYS = {"station name": "Station name", "slogan": "Slogan",
                    "message": "Station message", "facility ID": "Facility ID",
                    "location": "Station location"}


def parse_hd_report(text):
    """What the HD Radio part of an RDS Monitor report shows was observed:
    {"status", "mer_db", "ber", "station": [item], "programs": {"HD1": [item]}}
    or None when the decoder never locked."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^ {2}HD Radio\s*= (.*\S)", line)
        if m:
            break
    else:
        return None
    head = m.group(1)
    if not (head.startswith("locked") or "lost lock" in head):
        return None
    mer = re.search(r"MER (-?[\d.]+) dB", head)
    ber = re.search(r"BER ([\d.]+)", head)
    station, progs = set(), {}
    playing, current = "HD1", None

    def prog(hd):
        return progs.setdefault(hd, set())
    for line in lines[i + 1:]:
        if not line.startswith("    "):
            break
        m = re.match(r"^ +(\S.*?)\s*= (.*\S)\s*$", line)
        if not m:
            continue
        key, val = m.groups()
        if key == "playing":
            p = re.match(r"HD[1-8]", val)
            playing = p.group(0) if p else playing
        elif key in _HD_STATION_KEYS:
            station.add(_HD_STATION_KEYS[key])
        elif re.fullmatch(r"HD[1-8]", key):
            current = key
            if val != "(no name yet)":
                if not val.startswith("("):
                    prog(key).add("Channel name")
                if val.endswith(")"):
                    prog(key).add("Program type")
            else:
                prog(key)
        elif key == "now playing" and current:
            prog(current).add("Song / artist")
            if re.search(r"\[.+\]$", val):
                prog(current).add("Album")
        elif key == "ALERT":
            station.add("Emergency alert")
        elif key == "station logos":
            for hd in re.findall(r"HD[1-8]", val.split("(kept")[0]):
                prog(hd).add("Station logo")
        elif key == "showing now":
            if "album art" in val:
                prog(playing).add("Album art")
            if "traffic map" in val:
                station.add("Traffic map")
            if "weather map" in val:
                station.add("Weather map")
        elif key == "HD data":
            for hd in re.findall(r"\bAudio (HD[1-8]):", val):
                prog(hd).add("HD audio")
            for hd in re.findall(r"\bPSD (HD[1-8]):", val):
                prog(hd).add("Song / artist")
            if "HERE Traffic" in val:
                station.add("Traffic map")
            if "HERE Weather" in val:
                station.add("Weather map")
            if re.search(r"\bAlert:", val):
                station.add("Emergency alert")
    return {"status": "locked" if head.startswith("locked") else "lost",
            "mer_db": float(mer.group(1)) if mer else None,
            "ber": float(ber.group(1)) if ber else None,
            "station": sorted(station),
            "programs": {k: sorted(v) for k, v in progs.items()}}


def hd_checklist(reports, program="HD1"):
    """Every decodable HD item for one channel, ticked once any report saw
    it. reports = [(time, parse_hd_report(...) or None)]."""
    seen, best, last, locked = set(), None, None, False
    for ts, r in reports:
        if not r:
            continue
        locked = True
        seen |= set(r["station"]) | set(r["programs"].get(program, []))
        if r.get("mer_db") is not None and (best is None or r["mer_db"] > best):
            best = r["mer_db"]
        if ts and (last is None or ts > last):
            last = ts
    return {"locked": locked, "best_mer": best, "last_lock": last,
            "items": [{"name": n, "seen": n in seen}
                      for n in HD_CHANNEL_CHECKS + HD_STATION_CHECKS]}

def looks_like_report(text):
    return "RDS Monitor Report" in text[:2000] or bool(
        re.search(r"^Station: \S+\s+\(PI: [0-9A-F]{4}\)", text, re.M))


def parse_report(text, filename=""):
    records, rec = [], None
    created = None
    m = re.search(r"Created: (\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})", text)
    if m:
        created = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S").timestamp()
    for line in text.splitlines():
        sm = re.match(r"^Station: (\S+)\s+\(PI: ([0-9A-Fa-f]{4})\)", line)
        if sm:
            rec = {"callsign": None if sm.group(1).startswith("(")
                   else sm.group(1), "pi": sm.group(2).upper(), "extra": {}}
            if created:
                rec["heard_at"] = created
            if filename:
                rec["extra"]["Imported from"] = filename
            records.append(rec)
            continue
        if rec is None:
            continue
        km = re.match(r"^\s{2}([A-Za-z][A-Za-z +/]*?)\s*=\s*(.*)$", line)
        if not km:
            continue
        key, val = km.group(1).strip().lower(), km.group(2).strip()
        if val.startswith("(") and val.endswith(")"):
            continue
        if key == "transmitter":
            nums = re.findall(r"-?\d+\.\d+", val)
            if len(nums) >= 2:
                rec["lat"], rec["lon"] = float(nums[0]), float(nums[1])
                rec["coord_source"] = "report"
        elif key == "blocks":
            pm = re.search(r"\(([\d.]+)%\)", val)
            if pm:
                rec["block_pct"] = float(pm.group(1))
        elif key in _REPORT_KEYS:
            rec[_REPORT_KEYS[key]] = val
        elif key in ("rt", "radiotext", "radio text"):
            rec["rt"] = val
    return [r for r in records if r.get("callsign") or r.get("pi")]


# ---------------------------------------------------------------------------
# Excel .xlsx (e.g. Google Sheets: File > Download > Microsoft Excel)
# ---------------------------------------------------------------------------

_XL = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XL_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_DATE_FMT_IDS = {14: "d", 15: "d", 16: "d", 17: "d", 18: "t", 19: "t", 20: "t",
                 21: "t", 22: "dt", 45: "t", 46: "t", 47: "t"}


def _xl_col(ref):
    n = 0
    for c in ref:
        if not c.isalpha():
            break
        n = n * 26 + ord(c.upper()) - 64
    return n - 1


def _xl_number(text, kind):
    v = float(text)
    if kind:
        when = datetime(1899, 12, 30) + timedelta(days=v)
        if kind == "d" or (kind == "dt" and abs(v % 1) < 1e-9):
            return when.strftime("%Y-%m-%d")
        if kind == "t":
            return when.strftime("%H:%M:%S")
        return when.strftime("%Y-%m-%d %H:%M:%S")
    if v.is_integer():
        return str(int(v))
    return ("%.10f" % v).rstrip("0").rstrip(".")


def _xl_date_kinds(z):
    """cell style index -> "d"/"t"/"dt" for date/time number formats."""
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except KeyError:
        return {}
    custom = {}
    for nf in root.iter(_XL + "numFmt"):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", nf.get("formatCode", "")).lower()
        has_d, has_t = bool(re.search(r"[yd]", code)), bool(re.search(r"[hs]", code))
        if has_d or has_t:
            custom[int(nf.get("numFmtId"))] = ("dt" if has_d and has_t
                                                else "d" if has_d else "t")
    kinds = {}
    xfs = root.find(_XL + "cellXfs")
    for i, xf in enumerate(xfs if xfs is not None else []):
        fid = int(xf.get("numFmtId", 0))
        kind = custom.get(fid) or _DATE_FMT_IDS.get(fid)
        if kind:
            kinds[i] = kind
    return kinds


def _xl_sheet_rows(xml, shared, kinds):
    rows = []
    for row in ET.fromstring(xml).iter(_XL + "row"):
        cells = {}
        for c in row.iter(_XL + "c"):
            t, v = c.get("t"), c.find(_XL + "v")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(_XL + "t"))
            elif v is None or v.text is None or t == "e":
                continue
            elif t == "s":
                val = shared[int(v.text)]
            elif t == "b":
                val = "TRUE" if v.text == "1" else "FALSE"
            elif t in ("str", "d"):
                val = v.text
            else:
                try:
                    val = _xl_number(v.text, kinds.get(int(c.get("s", 0))))
                except (ValueError, OverflowError):
                    val = v.text
            col = _xl_col(c.get("r", "")) if c.get("r") else len(cells)
            cells[col] = val.strip()
        if any(cells.values()):
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])
    return rows


def parse_xlsx(data):
    """(headers, rows) from an .xlsx workbook. Every visible sheet with log
    headers is read (a "Sheet" column is added when there are several)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise ValueError("this is not an Excel .xlsx file - in Google Sheets "
                         "use File > Download > Microsoft Excel (.xlsx)") from e
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_XL + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_XL + "t")))
    rels = {}
    try:
        for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(_PKG_REL + "Relationship"):
            target = r.get("Target", "")
            rels[r.get("Id")] = target.lstrip("/") if target.startswith("/") else "xl/" + target
    except KeyError:
        pass
    kinds = _xl_date_kinds(z)
    sheets = []
    for sh in wb.iter(_XL + "sheet"):
        path = rels.get(sh.get(_XL_REL))
        if sh.get("state") in ("hidden", "veryHidden") or path not in z.namelist():
            continue
        rows = _xl_sheet_rows(z.read(path), shared, kinds)
        if rows:
            sheets.append((sh.get("name") or "", rows))
    if not sheets:
        raise ValueError("the workbook has no rows")
    found = []
    for name, rows in sheets:
        for i, row in enumerate(rows[:15]):
            if len(detect_mapping(row)) >= 2:
                found.append((name, [h.strip() for h in row], rows[i + 1:]))
                break
    if not found:
        name, rows = sheets[0]
        found = [(name, [h.strip() for h in rows[0]], rows[1:])]
    headers = []
    for _name, hdr, _rows in found:
        for h in hdr:
            if h and h not in headers:
                headers.append(h)
    multi = len(found) > 1
    if multi:
        headers.append("Sheet")
    out = []
    for name, hdr, rows in found:
        for row in rows:
            cells = {h: row[i] for i, h in enumerate(hdr) if h and i < len(row)}
            if multi:
                cells["Sheet"] = name
            out.append([cells.get(h, "") for h in headers])
    return headers, out


def load_file(filename, text, data=None):
    """Classify an uploaded file.

    Returns {"kind": "records", "records": [...]} for .spy/report files, or
    {"kind": "table", "headers": [...], "rows": [...], "mapping": {...}}
    for spreadsheets (the user confirms the column mapping first).
    """
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")) or (data and data[:2] == b"PK"):
        headers, rows = parse_xlsx(data or b"")
        return {"kind": "table", "format": "Excel workbook", "headers": headers,
                "rows": rows, "mapping": detect_mapping(headers)}
    if name.endswith(".spy") or looks_like_spy(text):
        return {"kind": "records", "format": "RDS Spy groups",
                "records": parse_spy(text, filename)}
    if looks_like_report(text):
        return {"kind": "records", "format": "RDS Monitor report",
                "records": parse_report(text, filename)}
    headers, rows = parse_table(text)
    return {"kind": "table", "format": "spreadsheet", "headers": headers,
            "rows": rows, "mapping": detect_mapping(headers)}


def now():
    return time.time()
