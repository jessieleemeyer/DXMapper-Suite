"""SQLite log: one row per station (call/PI + frequency), one row per log."""

import functools
import json
from contextlib import contextmanager
import re
import sqlite3
import threading
import time

from . import rtclean
from .geo import compass_point, distance_bearing, pi_to_callsign
from .importers import (LOGBOOK_HEADERS, keep_header, logbook_column, hd_checklist, parse_hd_report, parse_report_badges,
                        parse_report_features)

STATION_FIELDS = ("pi", "callsign", "freq_mhz", "ps", "pty", "pty_name",
                  "location", "city", "state", "country", "lat", "lon",
                  "notes", "mode")
NUMERIC_BEST = ("signal_dbfs", "usable_info", "block_pct")
AUTO_MERGE_SECONDS = 600
# HD2 etc. share their analog station's PI; a PI alone means the analog.
NOT_HD = " AND coalesce(callsign, '') NOT LIKE '%-HD_'"


def _as_listed(row, st=None):
    """A station row with what the Log Book shows: typed-in call letters,
    PI and frequency win over the logged ones, and a sheet's HD column
    (HD2-HD8) makes it that subchannel."""
    r = dict(row)
    if st is not None:
        m = re.match(r"HD([2-8])\b", log_fields(st).get("hd") or "", re.I)
        if m and r["callsign"] and not hd_sub(r["callsign"]):
            r["callsign"] = f"{r['callsign']}-HD{m.group(1)}"
    try:
        book = (json.loads(r["extra"] or "{}") or {}).get(BOOK) or {}
    except (ValueError, TypeError):
        book = {}
    for col, key in (("callsign", "callsign"), ("pi", "pi")):
        if str(book.get(key) or "").strip():
            r[col] = str(book[key]).strip().upper()
    try:
        if str(book.get("freq") or "").strip():
            r["freq_mhz"] = round(float(book["freq"]), 1)
    except ValueError:
        pass
    return r


def _relays(row):
    try:
        return bool((json.loads(row["fcc"] or "{}") or {}).get("relays"))
    except (ValueError, TypeError):
        return False


def _fcc_freq(row):
    try:
        return (json.loads(row["fcc"] or "{}") or {}).get("freq_mhz")
    except (ValueError, TypeError):
        return None

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pi TEXT, callsign TEXT, freq_mhz REAL, ps TEXT, pty INTEGER,
    pty_name TEXT, location TEXT, city TEXT, state TEXT, country TEXT,
    lat REAL, lon REAL, coord_source TEXT, notes TEXT, mode TEXT,
    first_heard REAL, last_heard REAL, times_logged INTEGER DEFAULT 0,
    best_signal_dbfs REAL, best_usable_info REAL, best_block_pct REAL,
    sources TEXT, extra TEXT, fcc TEXT, rev INTEGER
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id INTEGER, start_ts REAL, end_ts REAL, source TEXT,
    auto INTEGER DEFAULT 0, freq_mhz REAL, ps TEXT, rt TEXT,
    signal_dbfs REAL, rds_level_db REAL, usable_info REAL,
    block_pct REAL, details TEXT,
    rx_lat REAL, rx_lon REAL, rx_name TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS logs_station ON logs(station_id);
"""


def _clean(value):
    if isinstance(value, str):
        value = "".join(c if c.isprintable() else " " for c in value).strip()
        return value or None
    return value


S_UNIT = re.compile(r"^\s*S([0-9])(?:\s*\+\s*(\d+))?\s*$", re.I)


def _signal(value):
    """dBFS from a number or S-units text like 'S7' / 'S9+12' (S1 = -70)."""
    m = S_UNIT.match(str(value)) if isinstance(value, str) else None
    if m:
        return -70.0 + (int(m.group(1)) - 1) * 6.0 + float(m.group(2) or 0)
    return _float(value)


def _float(value):
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace("%", "").split()[0])
    except (TypeError, ValueError, IndexError):
        return None


HD_TAG = re.compile(r"(?<![A-Za-z])HD\s*-?\s*([2-8])\b", re.I)
HD_CALL = re.compile(r"[\s-]+HD\s*-?\s*([2-8])\b", re.I)


def hd_sub(callsign):
    """ "HD2" for an HD subchannel's call (e.g. WOVO-HD2), else None."""
    m = re.search(r"-HD([2-8])$", callsign or "", re.I)
    return f"HD{m.group(1)}" if m else None


def rt_template(rt, artist=None, title=None):
    """RadioText as the station's pattern: the song playing replaced by the
    words Artist and Song, e.g. "Artist - Song - Now on The Wave"."""
    return rtclean.template(rt, artist, title)


def _rt_keys(extra):
    """The log's RadioText columns ("RadioText", "RT", "Radio Text
    Decoded"...), the one the Log Book shows first."""
    return [k for k in extra or {}
            if not k.startswith("_") and logbook_column(k) == "radiotext"]


def _rt_key(extra):
    return next(iter(_rt_keys(extra)), None)


def now_playing(rec):
    """(artist, title) the RDS Monitor saw (RT+ or parsed RadioText)."""
    artist, title = rec.get("artist"), rec.get("title")
    if not (artist or title) and rec.get("now_playing"):
        a, _, t = str(rec["now_playing"]).partition(" - ")
        artist, title = (a, t) if t else (None, a)
    return artist, title


def normalize(rec):
    """Clean an incoming record (feed message or imported row)."""
    out = {k: _clean(v) for k, v in rec.items() if k != "report"}
    if isinstance(rec.get("report"), str) and rec["report"].strip():
        out["report"] = rec["report"].strip("\n")  # keep its lines
    # HD subchannels (WOVO-HD2, "98.1 HD2", an HD column of "HD2") are
    # stations of their own, kept apart from the analog station.
    sub = None
    if isinstance(out.get("callsign"), str):
        m = HD_CALL.search(out["callsign"])
        if m:
            sub = m.group(1)
            out["callsign"] = out["callsign"][:m.start()].strip() or None
    if isinstance(out.get("freq_mhz"), str):
        m = HD_TAG.search(out["freq_mhz"])
        if m:
            sub = sub or m.group(1)
            out["freq_mhz"] = HD_TAG.sub(" ", out["freq_mhz"]).strip(" -")
    m = re.fullmatch(r"\s*HD\s*-?\s*([2-8])\s*", str(out.get("hd") or ""), re.I)
    if m:
        sub = sub or m.group(1)
    if out.get("pi"):
        out["pi"] = str(out["pi"]).upper().replace("0X", "")[-4:].zfill(4)
    if out.get("callsign"):
        out["callsign"] = str(out["callsign"]).upper().split()[0]
    elif out.get("pi"):
        out["callsign"] = pi_to_callsign(out["pi"])
    if sub:
        base = re.sub(r"-HD\d$", "", out.get("callsign") or "") or out.get("pi")
        out["callsign"] = f"{base}-HD{sub}"
    for key in ("freq_mhz", "lat", "lon", "rx_lat", "rx_lon") + NUMERIC_BEST + ("rds_level_db",):
        if key in out:
            out[key] = (_signal if key == "signal_dbfs" else _float)(out[key])
    f = out.get("freq_mhz")
    if f is not None:
        if 875 <= f <= 1080:
            f = f / 10.0
        elif 87500 <= f <= 108000:
            f = f / 1000.0
        out["freq_mhz"] = round(f, 1) if 64.0 <= f <= 110.0 else None
    if out.get("pty") is not None:
        try:
            out["pty"] = int(str(out["pty"]).split()[0])
        except ValueError:
            out["pty"] = None
    if out.get("lat") is not None and not -90 <= out["lat"] <= 90:
        out["lat"] = out["lon"] = None
    if out.get("lon") is not None and not -180 <= out["lon"] <= 180:
        out["lat"] = out["lon"] = None
    if out.get("rx_lat") is not None and not (
            -90 <= out["rx_lat"] <= 90 and out.get("rx_lon") is not None
            and -180 <= out["rx_lon"] <= 180):
        out["rx_lat"] = out["rx_lon"] = None
    if not out.get("location") and out.get("city"):
        out["location"] = ", ".join(
            x for x in (out.get("city"), out.get("state")) if x)
    return out


def logged_distance(extra):
    """Miles from a distance column of an imported log (e.g. "Calibrated
    Distance"), or None. Values marked km are converted."""
    for key, val in (extra or {}).items():
        if "dist" not in key.lower():
            continue
        m = re.search(r"\d+(?:\.\d+)?", str(val).replace(",", ""))
        if m:
            mi = float(m.group())
            return round(mi * 0.621371, 1) if "km" in str(val).lower() else mi
    return None


def _rx_distance(log, lat, lon):
    """Distance/direction from where the log was made (Home or Away)."""
    log["rx_distance_mi"] = log["rx_compass"] = None
    if None not in (log.get("rx_lat"), log.get("rx_lon"), lat, lon):
        mi, brg = distance_bearing(log["rx_lat"], log["rx_lon"], lat, lon)
        log["rx_distance_mi"] = round(mi, 1)
        log["rx_compass"] = compass_point(brg)


WIKI_SLOGAN = "Slogan (Wikipedia)"
RT_WTFDA = "_rt_wtfda"  # RadioText came from WTFDA: a decode replaces it
WTFDA_SLOGAN = "Slogan (WTFDA)"
DONGLES = "_dongles"  # the SDRs that made the catches, newest first
BOOK = "_book"  # Log Book cells typed in: {column: value}, kept over updates


def slogan_key(extra):
    """The log column holding the station's slogan/branding, if any."""
    return next((k for k in extra or {}
                 if re.search(r"slogan|branding", k, re.I)), None)


# Station Details' "Decoded RDS" checklist, one ✓ column each.
RDS_CHECKS = (
    ("rds_pi", "PI"), ("rds_ps", "PS"), ("rds_pty", "PTY"), ("rds_rt", "RT"),
    ("rds_rtplus", "RT+"), ("rds_ptyn", "PTYN"), ("rds_longps", "Long PS"),
    ("rds_af", "AF"), ("rds_eon", "EON"), ("rds_tmc", "TMC"), ("rds_ct", "CT"),
    ("rds_tp", "TP"), ("rds_ta", "TA"), ("rds_di", "DI"),
)
LOGBOOK_COLUMNS = (
    ("freq", "Frequency"), ("callsign", "Call Letters"), ("hd", "HD"),
    ("hd_channels", "HD Channels"), ("slogan", "Slogan / Format"), ("pi", "PI Code"), ("city", "City"),
    ("state", "State"), ("distance", "Calibrated Distance"),
    ("range_type", "Range Type"), ("owner", "Owner / Licensee"),
    ("time_decoding", "Time Decoding"), ("rt_plus", "RT+ Capabilities"),
    ("radiotext", "RadioText"),
) + RDS_CHECKS + (
    ("stream", "Live Stream"),
    ("website", "Website"),
    ("source", "Source"), ("notes", "Notes"),
    ("group_seq", "Group Sequence"),
)
_LOGBOOK_HEADERS = LOGBOOK_HEADERS
_NO = re.compile(r"\s*(no|none|n|false|0|-+|\u2014|n/?a|unknown|not\b.*|\?)?\s*", re.I)


def _yes(value):
    return value not in (None, "") and not _NO.fullmatch(str(value))


@functools.lru_cache(maxsize=4096)
def _parsed_report(report):
    return parse_report_features(report), parse_report_badges(report)


def range_type(dist, local_mi=40, ext_mi=80):
    """Local / Extended local / DX by distance from home (miles)."""
    if dist is None:
        return ""
    return ("Local" if dist <= local_mi else
            "Extended local" if dist <= ext_mi else "DX")


FED_BY = "Fed by"


def _base_call(callsign):
    return re.sub(r"-HD[2-8]$", "", (callsign or "").strip(), flags=re.I).upper()


def _plain_call(callsign):
    """The call without -FM / -LP / -HDn, for matching, e.g. WGRK-FM -> WGRK."""
    return re.sub(r"-(FM|LP|HD[1-8])$", "", _base_call(callsign))


def _owner_key(name):
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(r"\b(inc|llc|l l c|corp|corporation|co|company|ltd|lp|the|of)\b", " ", s)
    return " ".join(s.split())


def _logbook_value(extra, col):
    book = (extra.get(BOOK) or {}).get(col)
    if book not in (None, ""):
        return str(book).strip()
    pat = dict(_LOGBOOK_HEADERS).get(col)
    return next((str(v).strip() for k, v in extra.items() if not k.startswith("_")
                 and pat and re.search(pat, k.strip(), re.I)), "")


def _owner_of(d):
    """A _relatives() station's owner: typed in, imported, else the FCC's."""
    return _logbook_value(d["extra"] or {}, "owner") or (d["fcc"] or {}).get("licensee") or ""


def _place_of(d):
    """(city, state) of a _relatives() station."""
    extra, fcc = d["extra"] or {}, d["fcc"] or {}
    return (_logbook_value(extra, "city") or fcc.get("city") or "",
            _logbook_value(extra, "state") or fcc.get("state") or "")


def _split_relay(text):
    m = re.fullmatch(r"\s*([A-Z0-9]{3,8}(?:-FM|-LP)?)(?:\s*-?\s*(HD[1-8]))?\s*",
                     (text or "").upper())
    if not m:
        return None
    base = re.sub(r"-(FM|LP)$", "", m.group(1))
    return base, (None if m.group(2) in (None, "HD1") else m.group(2))


def _relay_parent(d, wiki):
    """(analog call, HD channel or None) a translator entry relays: what
    you typed in Fed by, else the FCC's translator record, with the HD
    channel from the parent's Wikipedia translator list; None when it is
    not a translator."""
    typed = next((v for k, v in (d["extra"] or {}).items()
                  if k.strip().lower() == FED_BY.lower()), None)
    if typed:
        return _split_relay(str(typed))
    fcc = d["fcc"] or {}
    parent = _split_relay(fcc.get("relays")) if fcc.get("relays") else None
    if not parent:  # WTFDA's "Relay of" (e.g. an AM's FM translator)
        m = re.search(r"\b([A-Z][A-Z0-9]{2,7})(?:-(?:AM|FM|LP))?\b(?:\s*-?\s*(HD[1-8]))?",
                      str((d.get("wtfda") or {}).get("relay_of") or "").upper())
        wt = m and _split_relay(m.group(1) + (" " + m.group(2) if m.group(2) else ""))
        if wt and wt[0] != d["base"]:
            return wt
    base = parent[0] if parent else d["base"]
    page = (wiki.get(base) or {}).get("wiki") or {}
    for r in page.get("relays") or []:
        if d["freq_mhz"] and abs(r["freq_mhz"] - d["freq_mhz"]) < 0.05 and (
                parent or not wiki.get(base) is d):
            return base, r.get("hd") or (parent[1] if parent else None)
    return parent


def hd_columns(row, call, subs):
    """An analog station with HD gets 1 in the HD column and its channels
    (HD1 is the analog station itself) in "HD Channels"."""
    if hd_sub(call):
        return row
    if subs or str(row.get("hd") or "").upper().startswith("HD1") \
            or row.get("hd") == "1":
        row["hd"] = "1"
        if subs or not row.get("hd_channels"):
            row["hd_channels"] = ", ".join(["HD1"] + sorted(subs or []))
    return row


def hd_subs_by_base(callsigns):
    """{analog call: ["HD2", ...]} from the stations' callsigns."""
    out = {}
    for c in callsigns:
        sub = hd_sub(c)
        if sub:
            out.setdefault(re.sub(r"-HD[2-8]$", "", c, flags=re.I).upper(),
                           []).append(sub)
    return out


def log_fields(st, features=None, badges=None, local_mi=40, ext_mi=80):
    """The station as a row of the log book (the columns of a DX log sheet):
    imported columns first, then what the map, FCC and RDS reports know.
    "used" lists the imported columns shown; "log_badges" are decode badges
    the sheet says were decoded but no logged report has shown yet."""
    extra = st.get("extra") or {}
    found, used = {}, []
    for key, val in extra.items():
        if key.startswith("_"):
            continue
        for col, pat in _LOGBOOK_HEADERS:
            if re.search(pat, key.strip(), re.I):
                used.append(key)
                found.setdefault(col, str(val).strip())
                break
    feats = {f["name"]: f for f in (features or {}).get("features", [])}
    ok = {b["name"] for b in badges or [] if b.get("state") == "ok"}
    call = st.get("callsign") or ""
    city, _, state = (st.get("location") or "").partition(",")
    dist = logged_distance(extra)
    if dist is None:
        dist = st.get("distance_mi")
    hd_feat = feats.get("HD Radio")

    def decoded(her, feature, badge):
        f = feats.get(feature)
        if _yes(her):
            return her
        if (f and f.get("state") == "decoded") or badge in ok:
            detail = (f or {}).get("detail")
            return "Yes" + (f" ({detail})" if detail else "")
        return her or ""

    row = {
        "freq": (f"{st['freq_mhz']:.1f}" if st.get("freq_mhz")
                 else found.get("freq", "")),
        "callsign": call or found.get("callsign", ""),
        "hd": hd_sub(call) or found.get("hd") or (
            "Yes (seen)" if hd_feat else ""),
        "hd_channels": found.get("hd_channels", ""),
        "slogan": st.get("slogan") or found.get("slogan", ""),
        "pi": st.get("pi") or found.get("pi", ""),
        "city": st.get("city") or found.get("city") or city.strip(),
        "state": st.get("state") or found.get("state") or state.strip(),
        "distance": found.get("distance") or (
            f"{round(dist)} mi" if dist is not None else ""),
        "range_type": found.get("range_type") or range_type(
            dist, local_mi, ext_mi),
        "owner": found.get("owner") or (st.get("fcc") or {}).get("licensee") or "",
        "time_decoding": decoded(found.get("time_decoding"), "CT (clock time)", "CT"),
        "rt_plus": decoded(found.get("rt_plus"), "RT+", "RT+"),
        "radiotext": found.get("radiotext", ""),
        "stream": found.get("stream") or (st.get("stream") or {}).get("url", ""),
        "website": found.get("website") or (st.get("stream") or {}).get("website") or "",
        "source": ", ".join(extra.get(DONGLES) or []),
        "notes": "",
        "group_seq": found.get("group_seq", ""),
    }
    row.update({k: str(v) for k, v in (extra.get(BOOK) or {}).items() if k in row})
    log_badges = [{"name": b, "state": "ok", "from_log": True}
                  for b, col in (("RT+", "rt_plus"), ("CT", "time_decoding"))
                  if b not in ok and _yes(found.get(col))]
    seen = {b["name"] for b in badges or []} | {b["name"] for b in log_badges}
    done = {n for n, f in feats.items() if f.get("state") == "decoded"}

    def has(badge, feature=None):
        return badge in seen or (feature or badge) in done
    checks = {
        "rds_pi": bool(st.get("pi")), "rds_ps": "PS" in seen or bool(st.get("ps")),
        "rds_pty": "PTY" in seen or st.get("pty") is not None,
        "rds_rt": "RT" in seen, "rds_rtplus": has("RT+"), "rds_ptyn": has("PTYN"),
        "rds_longps": "Long PS" in done, "rds_af": has("AF"), "rds_eon": has("EON"),
        "rds_tmc": has("TMC"), "rds_ct": has("CT", "CT (clock time)"),
        "rds_tp": "TP" in done, "rds_ta": "TA" in done, "rds_di": "DI" in done,
        "rds_itunes": any(str(a.get("aid") or "").upper().endswith("C3B0")
                          or "itunes" in str(a.get("app") or "").lower()
                          for a in (features or {}).get("advertised", []))
        or any("itunes" in n.lower() for n in [*(features or {}).get("decoded", {}), *feats]),
    }
    row.update({k: "\u2713" if v else "" for k, v in checks.items()})
    return dict(row, used=used, log_badges=log_badges)


def report_decodes(report):
    """Everything a report shows as decoded or announced, as a set."""
    f = parse_report_features(report)
    return ({("badge", b["name"]) for b in parse_report_badges(report)}
            | {("oda", k) for k in f.get("decoded", {})}
            | {("3a", a.get("aid")) for a in f.get("advertised", [])}
            | {("feature", x["name"]) for x in f.get("features", [])
               if x.get("state") == "decoded"})


def merge_features(parsed):
    """parse_report_features() of a log's reports, newest first, combined:
    nothing decoded or announced in any of them is lost."""
    out = {"decoded": {}, "advertised": [], "features": []}
    aids, names = set(), {}
    for f in parsed:
        for k, v in f.get("decoded", {}).items():
            out["decoded"].setdefault(k, v)
        for a in f.get("advertised", []):
            if a.get("aid") not in aids:
                aids.add(a.get("aid"))
                out["advertised"].append(a)
        for x in f.get("features", []):
            i = names.get(x["name"])
            if i is None:
                names[x["name"]] = len(out["features"])
                out["features"].append(x)
            elif (out["features"][i].get("state") != "decoded"
                  and x.get("state") == "decoded"):
                out["features"][i] = x
    for k in parsed[0] if parsed else {}:
        out.setdefault(k, parsed[0][k])
    return out


def merge_badges(parsed):
    """Decode badges of a log's reports combined: green beats yellow and the
    bigger count wins."""
    best = {}
    for badges in parsed:
        for b in badges:
            had = best.get(b["name"])
            if had is None:
                best[b["name"]] = dict(b)
                continue
            had["state"] = "ok" if "ok" in (had["state"], b["state"]) else "part"
            had["count"] = max(had.get("count") or 0, b.get("count") or 0) or None
    return list(best.values())


class Store:
    def __init__(self, path):
        # Where distances in imported logs are measured from (Home).
        self.check_origin = None
        self.path = str(path)
        self.on_change = None
        self._batch_threads = set()
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self._migrate()

    def _migrate(self):
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(logs)")}
        for col, typ in (("rx_lat", "REAL"), ("rx_lon", "REAL"),
                         ("rx_name", "TEXT")):
            if col not in have:
                self.db.execute(f"ALTER TABLE logs ADD COLUMN {col} {typ}")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(stations)")}
        if "stream" not in have:  # live stream lookup (streams.find_stream)
            self.db.execute("ALTER TABLE stations ADD COLUMN stream TEXT")
        if "wiki" not in have:  # HD channels/translators from Wikipedia
            self.db.execute("ALTER TABLE stations ADD COLUMN wiki TEXT")
        if "wtfda" not in have:  # db.wtfda.org entry (wtfda.lookup)
            self.db.execute("ALTER TABLE stations ADD COLUMN wtfda TEXT")
        if not self.db.execute("SELECT 1 FROM meta WHERE key='rt_cleaned'").fetchone():
            self._clean_radiotext()
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('rt_cleaned', '1')")
        self.db.commit()

    def cleanup(self, progress=None):
        """Log Book > Clean up: deletes the old imported notes and imported
        columns that aren't log book columns, and tidies RadioText. The
        Log Book's own Notes column (typed in) is kept. progress(done, total)."""
        rows = self.db.execute("SELECT id, extra FROM stations").fetchall()
        changed = 0
        for i, r in enumerate(rows, 1):
            try:
                extra = json.loads(r["extra"] or "{}")
            except ValueError:
                extra = None
            if extra is not None:
                kept = {k: v for k, v in extra.items()
                        if k.startswith("_") or k in (WIKI_SLOGAN, WTFDA_SLOGAN)
                        or keep_header(k)}
                if kept != extra:
                    self._update(r["id"], {"extra": json.dumps(kept),
                                           "rev": self._bump()})
                    changed += 1
            if progress and (i % 25 == 0 or i == len(rows)):
                progress(i, len(rows))
        self.db.execute("UPDATE stations SET notes=NULL WHERE notes IS NOT NULL")
        self._clean_radiotext()
        self.db.commit()
        return {"stations": len(rows), "changed": changed}

    def _clean_radiotext(self):
        """Once: RadioText saved before (songs, promos, several captures in
        one cell) becomes the station's pattern, as new catches are kept."""
        rows = self.db.execute(
            "SELECT id, callsign, ps, extra FROM stations").fetchall()
        for r in rows:
            try:
                extra = json.loads(r["extra"] or "{}")
            except ValueError:
                continue
            names = (r["ps"], r["callsign"], extra.get(slogan_key(extra) or ""))
            changed = False
            for key in _rt_keys(extra):
                new = rtclean.clean_rt(extra[key], names)
                if new != extra[key]:
                    changed = True
                    if new:
                        extra[key] = new
                    else:
                        extra.pop(key)
            book = extra.get(BOOK) or {}
            if book.get("radiotext"):
                new = rtclean.clean_rt(book["radiotext"], names)
                if new != book["radiotext"]:
                    book["radiotext"], changed = new, True
            if changed:
                self.db.execute("UPDATE stations SET extra=?, rev=? WHERE id=?",
                                (json.dumps(extra), self._bump(), r["id"]))

    def restore(self, path):
        """Replace the whole log with a backup copy (e.g. after a reinstall).
        Raises sqlite3.DatabaseError / ValueError for a file that is not a
        DX Mapper log."""
        src = sqlite3.connect(str(path))
        try:
            names = {r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"stations", "logs"} <= names:
                raise ValueError(f"{path} is not a DX Mapper log")
            with self._lock:
                old = self.rev
                src.backup(self.db)
                self._migrate()
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('rev', ?)",
                                (str(old),))
                self._bump()
                self.db.commit()
        finally:
            src.close()

    # -- revision counter (lets the map poll only for changes) ---------
    def station_count(self):
        """Unique stations with at least one log."""
        return self.db.execute(
            "SELECT COUNT(DISTINCT station_id) FROM logs "
            "WHERE station_id IN (SELECT id FROM stations)").fetchone()[0]

    @property
    def rev(self):
        row = self.db.execute(
            "SELECT value FROM meta WHERE key='rev'").fetchone()
        return int(row[0]) if row else 0

    def _bump(self):
        rev = self.rev + 1
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('rev', ?)",
                        (str(rev),))
        if self.on_change and not self._in_batch():
            self.on_change()
        return rev

    def _in_batch(self):
        return threading.get_ident() in self._batch_threads

    @contextmanager
    def batch(self):
        """Many logs in one transaction (bulk import): one commit and one
        Excel/backup refresh at the end instead of one per row."""
        me = threading.get_ident()
        self._batch_threads.add(me)
        try:
            yield
        finally:
            self._batch_threads.discard(me)
            self.commit()
            if self.on_change:
                self.on_change()

    def commit(self):
        with self._lock:
            self.db.commit()

    # -- matching -------------------------------------------------------
    def find_station(self, rec):
        """The logged station matching a record (callsign/PI + MHz), or None."""
        try:
            rec = normalize(rec)
        except ValueError:
            return None
        with self._lock:
            row = self._find(rec)
        return dict(row) if row else None

    def _find(self, rec):
        call, pi, freq = rec.get("callsign"), rec.get("pi"), rec.get("freq_mhz")
        if hd_sub(call):
            pi = None  # the analog station's PI, not this subchannel's
        q = self.db.execute
        if freq is not None:
            for col, val in (("callsign", call), ("pi", pi)):
                if not val:
                    continue
                row = q(f"SELECT * FROM stations WHERE {col}=?{NOT_HD if col == 'pi' else ''} AND "
                        "abs(freq_mhz-?)<0.05", (val, freq)).fetchone()
                if row:
                    return row
            for col, val in (("callsign", call), ("pi", pi)):
                if not val:
                    continue
                row = q(f"SELECT * FROM stations WHERE {col}=?{NOT_HD if col == 'pi' else ''} AND "
                        "freq_mhz IS NULL", (val,)).fetchone()
                if row:
                    return row
            return None
        for col, val in (("callsign", call), ("pi", pi)):
            if not val:
                continue
            row = q(f"SELECT * FROM stations WHERE {col}=?{NOT_HD if col == 'pi' else ''} "
                    "ORDER BY last_heard DESC", (val,)).fetchone()
            if row:
                return row
        return None

    # -- writes ---------------------------------------------------------
    def elsewhere(self, rec):
        """The logged station with this PI (or call letters) on another
        frequency - a strong station splattering across the band - or None.
        Translators already logged as translators don't count."""
        try:
            rec = normalize(rec)
        except ValueError:
            return None
        call, pi, freq = rec.get("callsign"), rec.get("pi"), rec.get("freq_mhz")
        if freq is None or hd_sub(call):
            return None
        with self._lock:
            for col, val in (("pi", pi), ("callsign", call)):
                if not val:
                    continue
                for row in self.db.execute(
                        f"SELECT * FROM stations WHERE {col}=?{NOT_HD} AND freq_mhz IS NOT NULL "
                        "AND abs(freq_mhz-?)>=0.05 ORDER BY times_logged DESC", (val, freq)).fetchall():
                    if not _relays(row):
                        return dict(row)
        return None

    def log(self, rec, source="monitor", auto=False, ts=None,
            merge_window=AUTO_MERGE_SECONDS, into=None):
        """Add a reception. Returns (station_id, created). into: the station
        id to add it to (a splatter catch of a logged station)."""
        dongle = " ".join(str(rec.get("dongle") or "").split())[:80]
        rec = normalize(rec)
        if not (rec.get("callsign") or rec.get("pi")):
            raise ValueError("record needs a callsign or PI")
        ts = _float(rec.get("heard_at")) or ts or time.time()
        with self._lock:
            row = (self.db.execute("SELECT * FROM stations WHERE id=?", (into,)).fetchone()
                   if into else None) or self._find(rec)
            created = row is None
            rev = self._bump()
            if created:
                cur = self.db.execute(
                    "INSERT INTO stations (first_heard, last_heard, "
                    "times_logged, sources, extra, rev) VALUES (?,?,0,'[]','{}',?)",
                    (ts, ts, rev))
                sid = cur.lastrowid
                row = self.db.execute("SELECT * FROM stations WHERE id=?",
                                      (sid,)).fetchone()
            sid = row["id"]
            freq, fccf = rec.get("freq_mhz"), _fcc_freq(row)
            if (not created and freq and row["freq_mhz"] and fccf
                    and abs(freq - row["freq_mhz"]) >= 0.05 and abs(freq - fccf) < 0.05):
                # logged one channel off first; the FCC says this one is right
                self._update(sid, {"freq_mhz": freq})
                row = self.db.execute("SELECT * FROM stations WHERE id=?", (sid,)).fetchone()
            artist, title = now_playing(rec)
            names = (rec.get("ps"), rec.get("callsign"), rec.get("slogan"))
            if artist or title:
                rt = rt_template(rec.get("rt"), artist, title)
            elif rec.get("rt_kind") == "other":  # the Monitor knows it's no song
                rt = " ".join((rec.get("rt") or "").split()) or None
            else:
                rt = rtclean.clean_rt(rec.get("rt"), names) or None
                if rec.get("rt") and not rec.get("rt_kind"):
                    rec = dict(rec, rt_kind="promo" if not rt else
                               "song" if rtclean._PATTERN_RE.search(rt) else "other")
            playing = " ".join(x for x in (artist, title) if x).lower()
            ps = (rec.get("ps") or "").strip()
            if ps and len(ps) >= 2 and ps.lower() in playing:
                rec = dict(rec, ps=None)  # a piece of a scrolling song title
            # An import only fills blanks: what the log has is your own
            # decodes and edits.
            imported = str(source).startswith("import")
            upd = {}
            for k in STATION_FIELDS:
                v = rec.get(k)
                if v is None or k == "notes":
                    continue
                if row[k] is None or (not imported and k in (
                        "ps", "pty", "pty_name", "callsign", "pi")
                        and row[k] != v and ts >= (row["last_heard"] or 0)):
                    upd[k] = v
            if "lat" in upd:
                upd["coord_source"] = rec.get("coord_source") or source
            upd["first_heard"] = min(row["first_heard"] or ts, ts)
            upd["last_heard"] = max(row["last_heard"] or ts, ts)
            for k in NUMERIC_BEST:
                v = rec.get(k)
                best = row["best_" + k]
                if v is not None and (best is None or v > best):
                    upd["best_" + k] = v
            sources = set(json.loads(row["sources"] or "[]"))
            sources.add(source)
            upd["sources"] = json.dumps(sorted(sources))
            # The first live catch of an HD subchannel known only from an
            # import replaces the sheet's name and RadioText, like call
            # letters (HD lineups change; an analog keeps the sheet's).
            caught = bool(hd_sub(row["callsign"])) and not str(
                source).startswith("import") and all(
                str(s).startswith("import")
                for s in json.loads(row["sources"] or "[]"))
            extra = json.loads(row["extra"] or "{}")
            incoming = dict(rec.get("extra") or {})
            for k in [k for k in incoming if k == _rt_key({k: 1})]:
                incoming[k] = rtclean.clean_rt(incoming[k], names)
            extra.update({k: v for k, v in incoming.items()
                          if v not in (None, "") and not (
                              imported and extra.get(k) not in (None, ""))})
            if dongle:
                extra[DONGLES] = ([dongle] + [x for x in extra.get(DONGLES) or []
                                              if x != dongle])[:5]
            rt_key = _rt_key(extra)
            # RadioText keeps the station's song pattern ("Song by Artist on
            # The Wave"), which helps confirm the station. A newer song
            # pattern replaces an older live one; a sheet's or one typed in
            # stays; promos (ads, sports, church, contact lines) never count.
            kind = rec.get("rt_kind") or ("song" if artist or title else None)
            live = not any(str(s).startswith("import")
                           for s in json.loads(row["sources"] or "[]"))
            if rt and kind != "promo" and extra.pop(RT_WTFDA, None):
                extra[rt_key] = rt  # the station's own decode beats WTFDA's
            elif rt and kind != "promo" and (
                    rt_key is None or caught
                    or (kind == "song" and live and extra.get(rt_key) != rt
                        and "radiotext" not in (extra.get(BOOK) or {}))):
                extra[rt_key or "RadioText"] = rt
            slogan = str(rec.get("slogan") or "").strip()
            key = slogan_key(extra)
            if slogan and (key is None or key == WIKI_SLOGAN or caught):
                extra.pop(WIKI_SLOGAN, None)
                extra[key if key and key != WIKI_SLOGAN else "Slogan / Format"] = slogan
            upd["extra"] = json.dumps(extra)
            merged = False
            if auto:
                last = self.db.execute(
                    "SELECT * FROM logs WHERE station_id=? AND auto=1 "
                    "ORDER BY end_ts DESC LIMIT 1", (sid,)).fetchone()
                if last and ts - (last["end_ts"] or 0) <= merge_window:
                    # Still on the station: the newest report replaces the
                    # older one, so features that turn up later get added.
                    details = json.loads(last["details"] or "{}")
                    old_rep, new_rep = details.get("report"), rec.get("report")
                    if (isinstance(old_rep, str) and isinstance(new_rep, str)
                            and report_decodes(old_rep) - report_decodes(new_rep)):
                        # Something the old report decoded has gone quiet:
                        # keep it so a ✓ is never lost.
                        details["earlier_reports"] = (
                            [old_rep] + details.get("earlier_reports", []))[:5]
                    details.update({k: v for k, v in rec.items()
                                    if k not in STATION_FIELDS and k not in (
                                        "extra", "heard_at", "type",
                                        "rx_lat", "rx_lon", "rx_name")
                                    and v not in (None, "", [], {})})
                    self.db.execute("UPDATE logs SET details=? WHERE id=?",
                                    (json.dumps(details), last["id"]))
                    self.db.execute(
                        "UPDATE logs SET end_ts=?, signal_dbfs=max(coalesce("
                        "signal_dbfs,-999),?), usable_info=max(coalesce("
                        "usable_info,-1),?), ps=coalesce(?,ps), rt=coalesce(?,rt)"
                        " WHERE id=?",
                        (ts, rec.get("signal_dbfs") or -999,
                         rec.get("usable_info") or -1, rec.get("ps"),
                         rt, last["id"]))
                    merged = True
            if not merged:
                details = {k: v for k, v in rec.items()
                           if k not in STATION_FIELDS and k not in (
                               "extra", "heard_at", "type",
                               "rx_lat", "rx_lon", "rx_name")
                           and v not in (None, "", [], {})}
                self.db.execute(
                    "INSERT INTO logs (station_id, start_ts, end_ts, source, "
                    "auto, freq_mhz, ps, rt, signal_dbfs, rds_level_db, "
                    "usable_info, block_pct, details, rx_lat, rx_lon, "
                    "rx_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, ts, ts, source, 1 if auto else 0,
                     rec.get("freq_mhz"), rec.get("ps"), rt,
                     rec.get("signal_dbfs"), rec.get("rds_level_db"),
                     rec.get("usable_info"), rec.get("block_pct"),
                     json.dumps(details), rec.get("rx_lat"),
                     rec.get("rx_lon"), rec.get("rx_name")))
                upd["times_logged"] = (row["times_logged"] or 0) + 1
            self.last_merged = merged
            upd["rev"] = rev
            self._update(sid, upd)
            if not self._in_batch():  # live logs are saved at once, even mid-import
                self.db.commit()
            return self._settle(sid), created

    def _settle(self, sid):
        """One entry per station: when this station now has the call letters
        and frequency of another entry (moved there by an FCC fix, a catch or
        an edit), the two are merged. Returns the id that's left."""
        with self._lock:
            row = self.db.execute("SELECT * FROM stations WHERE id=?", (sid,)).fetchone()
            if not row or not row["callsign"] or row["freq_mhz"] is None:
                return sid
            listed = _as_listed(row, self._station_dict(row, None))["callsign"]
            other = next((o for o in self.db.execute(
                "SELECT * FROM stations WHERE callsign=? AND id<>? AND freq_mhz IS NOT NULL "
                "AND abs(freq_mhz-?)<0.05 ORDER BY first_heard", (row["callsign"], sid,
                                                                  row["freq_mhz"])).fetchall()
                if _as_listed(o, self._station_dict(o, None))["callsign"] == listed), None)
            if not other:
                return sid
            older = (other["first_heard"] or 0, other["id"]) <= (row["first_heard"] or 0, row["id"])
            keep, drop = (other["id"], sid) if older else (sid, other["id"])
            self.merge(keep, drop)
            return keep

    def _update(self, sid, fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE stations SET {cols} WHERE id=?",
                        list(fields.values()) + [sid])

    def edit(self, sid, fields):
        allowed = {k: v for k, v in normalize(fields).items()
                   if k in STATION_FIELDS}
        if "lat" in allowed or "lon" in allowed:
            allowed["coord_source"] = "manual"
        with self._lock:
            if isinstance(fields.get("slogan"), str):
                row = self.db.execute("SELECT extra FROM stations WHERE id=?",
                                      (sid,)).fetchone()
                extra = json.loads((row and row["extra"]) or "{}")
                key = slogan_key(extra)
                auto = fields.get("slogan_source") == "Wikipedia"
                if auto and key:
                    pass  # never replace a slogan from the log or typed in
                elif fields["slogan"].strip():
                    if key in (WIKI_SLOGAN, WTFDA_SLOGAN):
                        extra.pop(key)  # typed in: now it's the user's own
                        key = None
                    key = key or (WIKI_SLOGAN if auto else "Slogan")
                    extra[key] = fields["slogan"].strip()
                elif key:
                    extra.pop(key)
                allowed["extra"] = json.dumps(extra)
            if isinstance(fields.get("fed_by"), str):
                row = self.db.execute("SELECT extra FROM stations WHERE id=?",
                                      (sid,)).fetchone()
                extra = json.loads(allowed.get("extra")
                                   or (row and row["extra"]) or "{}")
                fed = fields["fed_by"].strip().upper()
                if fed:
                    extra[FED_BY] = fed
                else:
                    extra.pop(FED_BY, None)
                allowed["extra"] = json.dumps(extra)
            allowed["rev"] = self._bump()
            self._update(sid, allowed)
            self.db.commit()
            return self._settle(sid)

    def set_book_cell(self, sid, col, value):
        """Keep a Log Book cell as typed in ("" = blank); None puts back
        what DX Mapper fills in."""
        with self._lock:
            row = self.db.execute("SELECT extra FROM stations WHERE id=?",
                                  (sid,)).fetchone()
            if not row:
                raise ValueError("That station isn't in the log book any more")
            extra = json.loads(row["extra"] or "{}")
            book = dict(extra.get(BOOK) or {})
            if value is None:
                if col not in book:
                    return
                book.pop(col)
            else:
                book[col] = value
            if book:
                extra[BOOK] = book
            else:
                extra.pop(BOOK, None)
            self._update(sid, {"extra": json.dumps(extra), "rev": self._bump()})
            self.db.commit()

    def set_wtfda(self, sid, info):
        """Save a db.wtfda.org entry ({} = none) and fill what the station
        is missing: PI, PS, slogan, RadioText (a decode replaces it later),
        city / state and, with no better site, coordinates."""
        info = dict(info or {}, checked=time.time())
        with self._lock:
            row = self.db.execute("SELECT * FROM stations WHERE id=?",
                                  (sid,)).fetchone()
            if not row:
                return
            upd = {"wtfda": json.dumps(info)}
            if not hd_sub(row["callsign"]):
                extra = json.loads(row["extra"] or "{}")
                if info.get("callsign") and not row["callsign"]:
                    upd["callsign"] = re.sub(r"-FM$", "", info["callsign"].upper())
                if info.get("pi") and not row["pi"] and re.fullmatch(
                        r"[0-9A-Fa-f]{4}", info["pi"]):
                    upd["pi"] = info["pi"].upper()
                if info.get("ps") and not row["ps"]:
                    upd["ps"] = info["ps"]
                if info.get("slogan") and slogan_key(extra) in (None, WIKI_SLOGAN):
                    extra.pop(WIKI_SLOGAN, None)  # WTFDA is the DXers' own list
                    extra[WTFDA_SLOGAN] = info["slogan"]
                rt = rtclean.clean_rt(info.get("radiotext"), (
                    info.get("ps"), row["callsign"], info.get("slogan")))
                if rt and not _rt_key(extra):
                    extra["RadioText"], extra[RT_WTFDA] = rt, True
                for k in ("city", "state"):
                    if info.get(k) and not row[k]:
                        upd[k] = info[k].title() if k == "city" else info[k]
                if info.get("lat") is not None and (row["lat"] is None or (
                        row["coord_source"] or "").startswith(("City", "Wikipedia"))):
                    upd.update(lat=info["lat"], lon=info["lon"], coord_source="WTFDA")
                upd["extra"] = json.dumps(extra)
            upd["rev"] = self._bump()
            self._update(sid, upd)
            self.db.commit()

    def needs_wtfda(self):
        """Ids of stations with call letters never looked up on WTFDA."""
        with self._lock:
            return [r[0] for r in self.db.execute(
                "SELECT id FROM stations WHERE wtfda IS NULL AND "
                "(coalesce(callsign, '') <> '' OR coalesce(pi, '') <> '') "
                "AND coalesce(callsign, '') NOT LIKE '%-HD_' "
                "ORDER BY last_heard DESC")]

    def set_wiki(self, sid, info):
        """Save what the station's Wikipedia page says about its HD
        channels and translators (logos.wiki_hd_info)."""
        with self._lock:
            self._update(sid, {"wiki": json.dumps(dict(info, checked=time.time())),
                               "rev": self._bump()})
            self.db.commit()

    def set_fcc(self, sid, fcc, replace=False):
        """Save an FCC lookup. replace=True (new call letters): the FCC site
        and city win over the old ones, except coordinates set by hand."""
        with self._lock:
            row = self.db.execute("SELECT * FROM stations WHERE id=?",
                                  (sid,)).fetchone()
            if not row:
                return
            upd = {"fcc": json.dumps(fcc or {"not_found": True})}
            if fcc:
                rough = row["lat"] is None or (row["coord_source"] or "").startswith(("City", "WTFDA", "Wikipedia"))
                if replace or fcc.get("relays"):
                    # a translator moves a catch off the parent's FCC site
                    rough = rough or row["coord_source"] == "FCC" or (
                        replace and row["coord_source"] != "manual")
                if rough and fcc.get("lat") is not None:
                    upd.update(lat=fcc["lat"], lon=fcc["lon"],
                               coord_source="FCC")
                for k in ("location", "city", "state", "country"):
                    if (replace or not row[k]) and fcc.get(k):
                        upd[k] = fcc[k]
                if row["freq_mhz"] is None and fcc.get("freq_mhz"):
                    upd["freq_mhz"] = fcc["freq_mhz"]
            upd["rev"] = self._bump()
            self._update(sid, upd)
            self.db.commit()
            return self._settle(sid)

    def set_stream(self, sid, found):
        """Save a live stream lookup: {"url", "source", ...} or None (none
        found; looked up again later)."""
        found = {k: v for k, v in (found or {}).items() if v}
        with self._lock:
            row = self.db.execute("SELECT stream FROM stations WHERE id=?",
                                  (sid,)).fetchone()
            try:
                old = json.loads(row["stream"]) if row and row["stream"] else {}
            except ValueError:
                old = {}
            if old.get("url") and not found.get("url"):
                found = dict(found, url=old["url"], source=old.get("source"),
                             name=old.get("name"))  # keep a stream found before
            found.setdefault("website", old.get("website"))
            info = dict(found, checked=time.time())
            if not info.get("url"):
                info["none"] = True
            self._update(sid, {"stream": json.dumps(info), "rev": self._bump()})
            self.db.commit()

    def needs_stream_lookup(self, sid):
        """True for a station with call letters never looked up for a stream."""
        with self._lock:
            row = self.db.execute("SELECT callsign, stream FROM stations "
                                  "WHERE id=?", (sid,)).fetchone()
        return bool(row and row["callsign"] and not row["stream"])

    def needs_stream(self, retry_after=7 * 86400):
        """[(id, callsign, freq_mhz)] with no stream yet: never looked up,
        or none found more than retry_after seconds ago."""
        with self._lock:
            rows = self.db.execute(
                "SELECT id, callsign, freq_mhz, stream FROM stations "
                "WHERE coalesce(callsign, '') <> ''").fetchall()
        out = []
        for r in rows:
            try:
                s = json.loads(r["stream"]) if r["stream"] else None
            except ValueError:
                s = None
            if s and s.get("url") and s.get("website"):
                continue
            if s and time.time() - (s.get("checked") or 0) < retry_after:
                continue
            out.append((r["id"], r["callsign"], r["freq_mhz"]))
        return out

    def set_coords(self, sid, lat, lon, source):
        """Place a station that has no coordinates yet."""
        with self._lock:
            row = self.db.execute("SELECT lat FROM stations WHERE id=?",
                                  (sid,)).fetchone()
            if not row or row["lat"] is not None:
                return
            self._update(sid, {"lat": lat, "lon": lon, "coord_source": source,
                               "rev": self._bump()})
            self.db.commit()

    def dupes(self):
        """Stations logged more than once - the same call letters or PI on
        the same frequency, or on another one (splatter): [{"keep": ..., "drop": ...}].
        The FCC frequency is kept, else the one logged most. Translators
        logged as translators are left alone."""
        with self._lock:
            rows = [r for r in self.db.execute(
                "SELECT * FROM stations WHERE freq_mhz IS NOT NULL" + NOT_HD
                + " ORDER BY freq_mhz").fetchall() if not _relays(r)]
        rows = [r for r in (_as_listed(r, self._station_dict(r, None)) for r in rows)
                if r["freq_mhz"] is not None and not hd_sub(r["callsign"])]
        def rank(r):
            f = _fcc_freq(r)
            return (f is not None and abs(f - r["freq_mhz"]) < 0.05,
                    r["times_logged"] or 0, -(r["first_heard"] or 0))
        def brief(r):
            f = _fcc_freq(r)
            return {"id": r["id"], "callsign": r["callsign"], "pi": r["pi"],
                    "freq_mhz": r["freq_mhz"], "times_logged": r["times_logged"] or 0,
                    "location": r["location"],
                    "fcc": f is not None and abs(f - r["freq_mhz"]) < 0.05}
        out, used = [], set()
        for col in ("callsign", "pi"):
            groups = {}
            for r in rows:
                if r[col] and r["id"] not in used:
                    groups.setdefault(r[col], []).append(r)
            for group in groups.values():
                if len(group) < 2:
                    continue
                keep = max(group, key=rank)
                for r in group:
                    if r is keep:
                        continue
                    if (col == "pi" and abs(r["freq_mhz"] - keep["freq_mhz"]) < 0.05
                            and r["callsign"] and keep["callsign"]
                            and r["callsign"] != keep["callsign"]):
                        continue  # two stations on one channel sharing a PI
                    out.append({"keep": brief(keep), "drop": brief(r)})
                    used.update((keep["id"], r["id"]))
        return sorted(out, key=lambda x: x["keep"]["freq_mhz"])

    def merge(self, keep, drop):
        """Move drop's logs into keep and delete drop. keep's own values win;
        blanks are filled from drop."""
        with self._lock:
            k = self.db.execute("SELECT * FROM stations WHERE id=?", (keep,)).fetchone()
            d = self.db.execute("SELECT * FROM stations WHERE id=?", (drop,)).fetchone()
            if not k or not d or keep == drop:
                raise ValueError("Those stations aren't both in the log")
            upd = {c: d[c] for c in k.keys()
                   if c not in ("id", "rev", "extra", "sources") and k[c] in (None, "") and d[c] not in (None, "")}
            for c in ("first_heard",):
                upd[c] = min(x for x in (k[c], d[c], float("inf")) if x is not None)
            for c in ("last_heard", "best_signal_dbfs", "best_usable_info", "best_block_pct"):
                vals = [x for x in (k[c], d[c]) if x is not None]
                if vals:
                    upd[c] = max(vals)
            ke, de = json.loads(k["extra"] or "{}"), json.loads(d["extra"] or "{}")
            book = {**(de.get(BOOK) or {}), **(ke.get(BOOK) or {})}
            extra = {**de, **ke}
            if book:
                extra[BOOK] = book
            upd["extra"] = json.dumps(extra)
            upd["sources"] = json.dumps(sorted(set(json.loads(k["sources"] or "[]"))
                                               | set(json.loads(d["sources"] or "[]"))))
            self.db.execute("UPDATE logs SET station_id=? WHERE station_id=?", (keep, drop))
            self.db.execute("DELETE FROM stations WHERE id=?", (drop,))
            upd["times_logged"] = self.db.execute(
                "SELECT count(*) FROM logs WHERE station_id=?", (keep,)).fetchone()[0]
            upd["rev"] = self._bump()
            self._update(keep, upd)
            self.db.commit()

    def delete(self, sid):
        with self._lock:
            self.db.execute("DELETE FROM logs WHERE station_id=?", (sid,))
            self.db.execute("DELETE FROM stations WHERE id=?", (sid,))
            self._bump()
            self.db.commit()

    def delete_log(self, log_id):
        with self._lock:
            row = self.db.execute("SELECT station_id FROM logs WHERE id=?",
                                  (log_id,)).fetchone()
            if not row:
                return
            self.db.execute("DELETE FROM logs WHERE id=?", (log_id,))
            n = self.db.execute("SELECT count(*) FROM logs WHERE station_id=?",
                                (row[0],)).fetchone()[0]
            self._update(row[0], {"times_logged": n, "rev": self._bump()})
            self.db.commit()

    # -- reads ----------------------------------------------------------
    def _station_dict(self, row, home):
        d = dict(row)
        for k in ("sources", "extra", "fcc", "stream", "wiki", "wtfda"):
            try:
                d[k] = json.loads(d[k]) if d[k] else None
            except ValueError:
                d[k] = None
        d["distance_mi"] = d["bearing_deg"] = d["compass"] = None
        if home and d["lat"] is not None and d["lon"] is not None:
            mi, brg = distance_bearing(home[0], home[1], d["lat"], d["lon"])
            d["distance_mi"] = round(mi, 1)
            d["bearing_deg"] = round(brg)
            d["compass"] = compass_point(brg)
        key = slogan_key(d.get("extra"))
        d["slogan"] = str(d["extra"][key]).strip() if key else None
        d["slogan_source"] = "Wikipedia" if key == WIKI_SLOGAN else None
        logged = logged_distance(d.get("extra"))
        origin = self.check_origin or home
        if logged is not None and origin and d["lat"] is not None:
            mi = round(distance_bearing(origin[0], origin[1], d["lat"], d["lon"])[0], 1)
            d["location_check"] = {"logged_mi": logged, "map_mi": mi,
                                   "ok": abs(mi - logged) <= max(30, 0.15 * logged)}
        return d

    def _log_filter(self, since=None, until=None, live=False):
        cond, args = [], []
        if since is not None:
            cond.append("COALESCE(l.end_ts, l.start_ts) >= ?")
            args.append(float(since))
        if until is not None:
            cond.append("l.start_ts < ?")
            args.append(float(until))
        if live:
            cond.append("COALESCE(l.source, '') NOT LIKE 'import%'")
        return (" WHERE " + " AND ".join(cond)) if cond else "", args

    def stations(self, home=None, since=None, until=None, live=False):
        """All stations, or only those with a log in [since, until).

        live=True leaves out imported logs (the Current view).
        """
        with self._lock:
            if since is None and until is None and not live:
                rows = self.db.execute(
                    "SELECT * FROM stations ORDER BY last_heard DESC").fetchall()
            else:
                where, args = self._log_filter(since, until, live)
                rows = self.db.execute(
                    "SELECT s.*, count(l.id) AS period_logs, "
                    "min(l.start_ts) AS period_first, "
                    "max(COALESCE(l.end_ts, l.start_ts)) AS period_last "
                    "FROM stations s JOIN logs l ON l.station_id = s.id"
                    f"{where} GROUP BY s.id ORDER BY period_last DESC",
                    args).fetchall()
        return [self._station_dict(r, home) for r in rows]

    def days(self, limit=400):
        """Days with logs (local time), newest first, for the History view."""
        with self._lock:
            rows = self.db.execute(
                "SELECT date(start_ts, 'unixepoch', 'localtime') AS day, "
                "count(DISTINCT station_id) AS stations, count(*) AS logs "
                "FROM logs GROUP BY day ORDER BY day DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    local_mi = 40  # the Local / Extended local radii, kept up to date by the App
    ext_mi = 80

    def logbook(self, home=None):
        """One row per station in the log book's columns, by frequency."""
        with self._lock:
            rows = self.db.execute("SELECT * FROM stations").fetchall()
            reps = self.db.execute(
                "SELECT station_id, details FROM logs "
                "WHERE details LIKE '%\"report\"%'").fetchall()
        reports = {}
        for sid, details in reps:
            try:
                det = json.loads(details or "{}")
            except ValueError:
                continue
            if isinstance(det.get("report"), str):
                reports.setdefault(sid, []).extend(
                    [det["report"]] + det.get("earlier_reports", []))
        subs = hd_subs_by_base(r["callsign"] or "" for r in rows)
        relatives = {d["id"]: d for d in self._relatives()[0]}
        out = []
        for r in rows:
            st = self._station_dict(r, home)
            parsed = [_parsed_report(x) for x in reports.get(st["id"], [])]
            lf = log_fields(st, merge_features([p[0] for p in parsed]),
                            merge_badges([p[1] for p in parsed]), self.local_mi,
                            self.ext_mi)
            lf.pop("used")
            rel = relatives.get(st["id"]) or {}
            wiki = rel.get("wiki") or {}
            own = [] if rel.get("parent") else sorted(
                set(subs.get((st.get("callsign") or "").upper()) or [])
                | set([] if wiki.get("translator") else wiki.get("names") or {}))
            hd_columns(lf, st.get("callsign") or "", own)
            out.append(dict(lf, id=st["id"], freq_mhz=st.get("freq_mhz"),
                            distance_mi=st.get("distance_mi"),
                            last_heard=st.get("last_heard"),
                            stream_source=(st.get("stream") or {}).get("source"),
                            has_coords=st.get("lat") is not None))
        out.sort(key=lambda x: (x["freq_mhz"] or 999, x["callsign"]))
        return out

    def on_frequency(self, freq, home=None):
        """Logged stations on a frequency, closest first."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM stations WHERE abs(freq_mhz - ?) < 0.05",
                (float(freq),)).fetchall()
        out = [self._station_dict(r, home) for r in rows]
        out.sort(key=lambda d: (d["distance_mi"] is None, d["distance_mi"] or 0))
        return out

    def station(self, sid, home=None):
        with self._lock:
            row = self.db.execute("SELECT * FROM stations WHERE id=?",
                                  (sid,)).fetchone()
            if not row:
                return None
            logs = self.db.execute(
                "SELECT * FROM logs WHERE station_id=? ORDER BY start_ts DESC",
                (sid,)).fetchall()
        d = self._station_dict(row, home)
        everything = self._relatives()
        d["hd_family"] = self.hd_family(d, everything)
        d.update(self.relay_links(d, everything))
        d["logs"] = []
        for lg in logs:
            item = dict(lg)
            try:
                item["details"] = json.loads(item["details"] or "{}")
            except ValueError:
                item["details"] = {}
            if isinstance(item["details"].get("report"), str):
                reports = [item["details"]["report"]] + item["details"].pop(
                    "earlier_reports", [])
                item["details"]["report_features"] = merge_features(
                    [parse_report_features(r) for r in reports])
                item["details"]["report_badges"] = merge_badges(
                    [parse_report_badges(r) for r in reports])
                item["details"]["hd_report"] = parse_hd_report(reports[0])
            _rx_distance(item, d["lat"], d["lon"])
            d["logs"].append(item)
        d["hd_checklist"] = self._hd_checklist(d)
        dets = [lg["details"] for lg in d["logs"] if "report_features" in lg["details"]]
        d["logbook"] = log_fields(
            d, merge_features([x["report_features"] for x in dets]),
            merge_badges([x["report_badges"] for x in dets]), self.local_mi,
            self.ext_mi)
        hd_columns(d["logbook"], d.get("callsign") or "",
                   [f["hd"] for f in d["hd_family"] if f["hd"]])
        return d

    def _hd_checklist(self, d):
        """HD1 is read from the station's own reports; HD2-HD8 from the
        reports logged on the analog station they ride on."""
        m = re.search(r"-HD([2-8])$", d.get("callsign") or "", re.I)
        reports = [(lg.get("end_ts") or lg.get("start_ts"),
                    lg["details"].get("hd_report")) for lg in d["logs"]]
        latest = None
        parent = m and next((f for f in d.get("hd_family") or []
                             if f.get("id") is not None
                             and (f.get("hd") or "HD1") == "HD1"), None)
        if parent:
            with self._lock:
                rows = self.db.execute(
                    "SELECT start_ts, end_ts, details FROM logs WHERE "
                    "station_id=? AND details LIKE '%\"report\"%'",
                    (parent["id"],)).fetchall()
            for r in rows:
                try:
                    text = json.loads(r["details"] or "{}").get("report")
                except ValueError:
                    text = None
                if isinstance(text, str):
                    reports.append((r["end_ts"] or r["start_ts"],
                                    parse_hd_report(text)))
                    if latest is None or r["start_ts"] > latest["start_ts"]:
                        latest = {"start_ts": r["start_ts"],
                                  "details": {"report": text}}
        d["parent_report"] = latest
        return hd_checklist(reports, f"HD{m.group(1)}" if m else "HD1")

    def _relatives(self):
        """Every station's id, call, freq, HD channel and, for a translator,
        the (analog call, HD channel or None) it relays."""
        with self._lock:
            rows = self.db.execute(
                "SELECT id, callsign, freq_mhz, extra, fcc, wiki, wtfda FROM stations"
            ).fetchall()
        out, wiki = [], {}
        for r in rows:
            d = {"id": r["id"], "callsign": r["callsign"] or "",
                 "freq_mhz": r["freq_mhz"], "hd": hd_sub(r["callsign"])}
            for k in ("extra", "fcc", "wiki", "wtfda"):
                try:
                    d[k] = json.loads(r[k]) if r[k] else {}
                except ValueError:
                    d[k] = {}
            key = slogan_key(d["extra"])
            d["slogan"] = str(d["extra"][key]).strip() if key else None
            d["base"] = _base_call(d["callsign"])
            if d["wiki"] and not d["hd"] and not d["wiki"].get("translator"):
                wiki.setdefault(d["base"], d)
            out.append(d)
        for d in out:
            d["parent"] = _relay_parent(d, wiki)
        return out, wiki

    def hd_family(self, st, everything=None):
        """The analog station and its HD2-HD8 entries, linked both ways
        (HD1 is the analog station's own entry), without st itself; HD
        channels Wikipedia lists that aren't logged yet have no id."""
        rows, wiki = everything or self._relatives()
        me = next((d for d in rows if d["id"] == st.get("id")), None)
        base = _base_call(st.get("callsign"))
        if not base or (me and me["parent"]):
            return []
        freq = st.get("freq_mhz")
        family = [{k: d[k] for k in ("id", "callsign", "freq_mhz", "slogan", "hd")}
                  for d in rows if d["base"] == base and d["id"] != st.get("id")
                  and not d["parent"] and (d["hd"] or not freq or not d["freq_mhz"]
                                           or abs(d["freq_mhz"] - freq) < 0.05)]
        have = {f["hd"] for f in family} | {hd_sub(st.get("callsign"))}
        for hd, name in ((wiki.get(base) or {}).get("wiki", {}).get("names") or {}).items():
            if hd not in have:
                family.append({"id": None, "callsign": f"{base}-{hd}",
                               "freq_mhz": freq, "slogan": name, "hd": hd})
        return sorted(family, key=lambda x: (x["hd"] is not None, x["hd"] or ""))

    def neighbors(self, st, sister_calls=(), market=(), everything=None):
        """{"sisters", "market"} for Station Details: sister stations
        (sister_calls from Wikipedia, plus logged stations with the same
        owner) and stations in the same market (market: the FCC's FM
        stations licensed to its city, plus logged stations from the same
        city and state). Each is {"callsign", "freq_mhz", "city", "id"};
        id is the logged station's, or None when it isn't logged."""
        rows, _wiki = everything or self._relatives()
        mains = [d for d in rows if not d["hd"] and not d["parent"]]
        by_call = {}
        for d in mains:
            for c in ((d["fcc"] or {}).get("callsign"), d["callsign"]):
                if c:
                    by_call.setdefault(_plain_call(c), d)
        me = _plain_call(st.get("callsign"))
        owner = _owner_key(st.get("owner"))
        place = ((st.get("city") or "").strip().lower(),
                 (st.get("state") or "").strip().lower())

        def item(call, freq=None, city=None, d=None):
            d = d or by_call.get(_plain_call(call))
            return {"callsign": ((d["fcc"] or {}).get("callsign") or d["callsign"]) if d else call,
                    "freq_mhz": (d and d["freq_mhz"]) or freq,
                    "city": city or (d and _place_of(d)[0]) or None,
                    "id": d and d["id"]}

        def add(out, it):
            key = _plain_call(it["callsign"])
            if key and key != me and key not in {_plain_call(x["callsign"]) for x in out}:
                out.append(it)
        sisters, mkt = [], []
        for c in sister_calls:
            add(sisters, item(c))
        for d in mains:
            if owner and _owner_key(_owner_of(d)) == owner:
                add(sisters, item(None, d=d))
        for r in market:
            add(mkt, item(r["callsign"], r.get("freq_mhz"), r.get("city")))
        if all(place):
            for d in mains:
                if tuple(x.lower() for x in _place_of(d)) == place:
                    add(mkt, item(None, d=d))
        key = lambda x: (x["freq_mhz"] is None, x["freq_mhz"] or 0, x["callsign"])
        return {"sisters": sorted(sisters, key=key), "market": sorted(mkt, key=key)}

    def relay_links(self, st, everything=None):
        """{"fed_by": what this translator relays, "translators": the
        translators relaying this station or HD channel}."""
        rows, wiki = everything or self._relatives()
        me = next((d for d in rows if d["id"] == st.get("id")), None)
        out = {"fed_by": None, "translators": []}
        if not me:
            return out
        if me["parent"]:
            base, hd = me["parent"]
            target = next((d for d in rows if d["base"] == base and d["hd"] == hd
                           and not d["parent"]), None)
            if not target and hd:  # the HD channel isn't logged: its station
                target = next((d for d in rows if d["base"] == base
                               and not d["hd"] and not d["parent"]), None)
            out["fed_by"] = {"callsign": f"{base}-{hd}" if hd else base,
                             "id": target and target["id"],
                             "freq_mhz": target and target["freq_mhz"]}
            return out
        base, hd = me["base"], me["hd"]
        for d in rows:
            if d["parent"] == (base, hd):
                out["translators"].append({
                    "id": d["id"], "freq_mhz": d["freq_mhz"],
                    "callsign": (d["fcc"] or {}).get("callsign") or d["callsign"]})
        logged = [t["freq_mhz"] for t in out["translators"] if t["freq_mhz"]]
        for r in ((wiki.get(base) or {}).get("wiki", {}).get("relays") or []):
            if r.get("hd") == hd and not any(abs(f - r["freq_mhz"]) < 0.05 for f in logged):
                out["translators"].append({"id": None, "freq_mhz": r["freq_mhz"],
                                           "callsign": r.get("call"),
                                           "city": r.get("city")})
        out["translators"].sort(key=lambda x: x["freq_mhz"] or 0)
        return out

    def all_logs(self, since=None, until=None, live=False):
        """Every reception, newest first, with its station's identity."""
        where, args = self._log_filter(since, until, live)
        with self._lock:
            rows = self.db.execute(
                "SELECT l.*, s.callsign AS callsign, s.pi AS pi, "
                "s.freq_mhz AS station_freq, s.location AS location, "
                "s.lat AS lat, s.lon AS lon "
                f"FROM logs l JOIN stations s ON s.id = l.station_id{where} "
                "ORDER BY l.start_ts DESC, l.id DESC", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("freq_mhz") is None:
                d["freq_mhz"] = d.get("station_freq")
            d.pop("details", None)
            _rx_distance(d, d.get("lat"), d.get("lon"))
            out.append(d)
        return out

    def needs_geo(self):
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id, callsign, freq_mhz FROM stations WHERE "
                "(fcc IS NULL AND callsign IS NOT NULL) OR (lat IS NULL AND "
                "(city IS NOT NULL OR location IS NOT NULL))").fetchall()]

    def close(self):
        with self._lock:
            self.db.close()
