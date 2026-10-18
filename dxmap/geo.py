"""Distances, PI -> callsign (NRSC-4-B) and FCC FM Query lookups.

Standard library only (urllib), so the logger needs no pip packages.
"""

import re
import http.client
import json
import math
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

FCC_FMQ_URL = "https://transition.fcc.gov/fcc-bin/fmq"
USER_AGENT = ("DXMapper/1.0 "
              "(https://github.com/fm-dx; contact: j.rds.monitor@gmail.com)")

COMPASS_POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def compass_point(bearing_deg):
    return COMPASS_POINTS[int((bearing_deg % 360.0) / 22.5 + 0.5) % 16]


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
    return miles, math.degrees(math.atan2(y, x)) % 360.0


def pi_to_callsign(pi_code):
    """US callsign for a PI code (NRSC-4-B), or None."""
    try:
        pi = int(str(pi_code).strip().upper().replace("0X", ""), 16)
    except (TypeError, ValueError):
        return None
    if 0x1000 <= pi < 0x54A8:
        decimal, prefix = pi - 0x1000, "K"
    elif 0x54A8 <= pi <= 0x994F:
        decimal, prefix = pi - 0x54A8, "W"
    else:
        return None
    l3, rem = divmod(decimal, 676)
    l2, l1 = divmod(rem, 26)
    if l3 > 25:
        return None
    return prefix + chr(65 + l3) + chr(65 + l2) + chr(65 + l1)


def _dms(deg, minutes, seconds, hemisphere):
    value = float(deg) + float(minutes) / 60.0 + float(seconds) / 3600.0
    return -value if hemisphere.strip().upper() in ("S", "W") else value


def _num(text):
    try:
        return float(text.split()[0])
    except (AttributeError, IndexError, ValueError):
        return None


def parse_fcc_fmq(text, callsign, freq_mhz=None):
    """Best record for `callsign` from an FCC FM Query (list=4) listing.

    Prefers a licensed (LIC) full-service FM record on `freq_mhz`; skips
    auxiliary (FA) and records without coordinates.  Returns a dict with
    location, lat, lon and the technical details, or None.
    """
    callsign = (callsign or "").upper()
    best = None
    for rec, f in _fmq_records(text):
        if rec["callsign"].split("-")[0] != callsign.split("-")[0]:
            continue
        freq = rec["freq_mhz"]
        rank = (0 if f[9] == "LIC" else 1,
                0 if f[3] in ("FM", "FL", "FX", "FB") else 1,
                0 if freq_mhz is None or freq is None
                or abs(freq - freq_mhz) < 0.05 else 1)
        if best is None or rank < best[0]:
            best = (rank, rec)
    return best[1] if best else None


def _fmq_records(text):
    """(record, raw fields) for each FCC FM Query (list=4) line with a site."""
    for line in text.splitlines():
        f = [x.strip() for x in line.split("|")]
        if len(f) < 28 or not f[1]:
            continue
        call = f[1].upper()
        try:
            lat = _dms(f[20], f[21], f[22], f[19])
            lon = _dms(f[24], f[25], f[26], f[23])
        except ValueError:
            continue
        freq = _num(f[2])
        city = f[10].title() if f[10] else ""
        rec = {
            "callsign": call,
            "freq_mhz": freq,
            "service": f[3],
            "channel": f[4],
            "antenna": "Directional" if f[5] == "DA" else "Non-directional",
            "station_class": f[7],
            "status": f[9],
            "location": f"{city}, {f[11]}" if city and f[11] else None,
            "city": city,
            "state": f[11],
            "country": f[12],
            "erp_kw": _num(f[14]) or _num(f[15]),
            "haat_m": _num(f[16]) or _num(f[17]),
            "facility_id": f[18],
            "lat": lat,
            "lon": lon,
            "licensee": f[27].strip().title() if f[27] else "",
        }
        if len(f) > 32:
            rec["rcamsl_m"] = _num(f[32])
        if len(f) > 38 and f[38] not in ("", "-"):
            rec["appid"], rec["lmsid"], rec["fileno"] = f[37], f[38], f[13]
        yield rec, f


_fcc_cache = {}
_fcc_lock = threading.Lock()


def fcc_lookup(callsign, freq_mhz=None, fetch=None):
    """FCC FM Query record for a callsign; cached for the process life.

    `fetch(url) -> text` is injectable for tests.
    """
    if not callsign or len(callsign) < 3:
        return None
    callsign = re.sub(r"-HD[2-8]$", "", callsign, flags=re.I)
    key = (callsign.upper(), round(freq_mhz, 1) if freq_mhz else None)
    with _fcc_lock:
        if key in _fcc_cache:
            return _fcc_cache[key]
    url = FCC_FMQ_URL + "?" + urllib.parse.urlencode(
        {"call": callsign.split("-")[0].upper(), "list": "4"})
    # Network errors propagate so the caller can retry later instead of
    # recording "no FCC record".
    text = fetch(url) if fetch else http_get_text(url)
    result = parse_fcc_fmq(text, callsign, freq_mhz)
    with _fcc_lock:
        _fcc_cache[key] = result
    return result


def fcc_city(city, state, fetch=None):
    """The full-power and LPFM stations the FCC lists for a city of license
    (licensed or construction permits; no translators), by frequency."""
    if not city or not state:
        return []
    url = FCC_FMQ_URL + "?" + urllib.parse.urlencode(
        {"city": city.upper(), "state": state.upper(), "list": "4"})
    text = fetch(url) if fetch else http_get_text(url)
    out, seen = [], set()
    for rec, f in _fmq_records(text):
        if f[3] not in ("FM", "FL") or f[9] not in ("LIC", "CP") \
                or rec["city"].lower() != city.strip().lower() \
                or rec["callsign"] in seen:
            continue
        seen.add(rec["callsign"])
        out.append({k: rec[k] for k in ("callsign", "freq_mhz", "city", "state", "status")})
    return sorted(out, key=lambda r: r["freq_mhz"] or 0)


STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT",
    "delaware": "DE", "district of columbia": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "puerto rico": "PR", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
REGION_WORDS = {"north", "south", "east", "west", "central", "northern",
                "southern", "eastern", "western", "northeast", "northwest",
                "southeast", "southwest", "northeastern", "northwestern",
                "southeastern", "southwestern", "upstate", "downstate",
                "metro", "area", "upper", "lower", "middle", "mid", "the"}
SERVICE_NAMES = {"FM": "FM", "FL": "LPFM", "FX": "Translator", "FB": "Booster"}


def parse_place(text):
    """'Springfield, IL' / 'central kentucky' / 'ky' -> (city, ST, region
    words) with None for what isn't there; (None, None, ()) without a state."""
    words = re.sub(r"[,.]", " ", text or "").lower().split()
    for n in (3, 2, 1):
        if len(words) < n:
            continue
        tail = " ".join(words[-n:])
        st = STATES.get(tail) or (tail.upper() if n == 1 and tail.upper()
                                  in STATES.values() else None)
        if st:
            rest = words[:-n]
            region = tuple(w for w in rest if w in REGION_WORDS)
            city = " ".join(w for w in rest if w not in REGION_WORDS)
            return (city.title() or None, st, region)
    return None, None, ()


def fcc_search(freq_mhz=None, call=None, city=None, state=None,
               fetch=None):
    """FCC FM Query listing for a frequency, a call, a city
    and/or a state: [record] with "service" as FM / LPFM / Translator /
    Booster, licensed and construction permits only, one per station."""
    q = {"list": "4"}
    if freq_mhz:  # the whole country: locals sort first, DX still listed
        q.update(freq=f"{freq_mhz:.1f}", fre2=f"{freq_mhz:.1f}")
    if call:
        q["call"] = call.upper()
    if city:
        q["city"] = city.upper()
    if state:
        q["state"] = state.upper()
    url = FCC_FMQ_URL + "?" + urllib.parse.urlencode(q)
    text = fetch(url) if fetch else http_get_text(url)
    out, seen = [], set()
    for rec, f in _fmq_records(text):
        if f[3] not in SERVICE_NAMES or f[9] not in ("LIC", "CP", "MOD") \
                or rec["freq_mhz"] is None or (freq_mhz and abs(
                    rec["freq_mhz"] - freq_mhz) >= 0.05):
            continue
        key = (rec["callsign"], rec["freq_mhz"])
        if key in seen:
            continue
        seen.add(key)
        rec["service"] = SERVICE_NAMES[f[3]]
        out.append(rec)
    return out


def _dms_query(value, pos, neg):
    hemi = pos if value >= 0 else neg
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    s = round((value - d - m / 60.0) * 3600)
    return d, m, min(s, 59), hemi


TRANSLATOR_RADIUS_KM = 400
_translator_cache = {}


def fcc_translator(freq_mhz, home, fetch=None):
    """Nearest licensed FM translator (FX) on `freq_mhz` to `home`, or None.
    Uses the FCC FM Query radius search (the nationwide list is capped)."""
    if not freq_mhz or not home:
        return None
    key = (round(freq_mhz, 1), round(home[0], 2), round(home[1], 2))
    with _fcc_lock:
        if key in _translator_cache:
            return _translator_cache[key]
    dlat, mlat, slat, ns = _dms_query(home[0], "N", "S")
    dlon, mlon, slon, ew = _dms_query(home[1], "E", "W")
    url = FCC_FMQ_URL + "?" + urllib.parse.urlencode({
        "freq": f"{freq_mhz:.1f}", "fre2": f"{freq_mhz:.1f}", "serv": "FX",
        "list": "4", "dist": str(TRANSLATOR_RADIUS_KM),
        "dlat2": dlat, "mlat2": mlat, "slat2": slat, "NS": ns,
        "dlon2": dlon, "mlon2": mlon, "slon2": slon, "EW": ew})
    text = fetch(url) if fetch else http_get_text(url)
    best = None
    for rec, f in _fmq_records(text):
        if f[3] != "FX" or rec["freq_mhz"] is None \
                or abs(rec["freq_mhz"] - freq_mhz) >= 0.05:
            continue
        miles = distance_bearing(home[0], home[1], rec["lat"], rec["lon"])[0]
        rank = (0 if f[9] == "LIC" else 1, miles)
        if best is None or rank < best[0]:
            best = (rank, rec)
    result = best[1] if best else None
    with _fcc_lock:
        _translator_cache[key] = result
    return result


def fcc_heard(callsign, freq_mhz=None, home=None, fetch=None):
    """FCC record for what was actually heard. Translators usually send the
    parent's PI, so when the caught frequency isn't the parent's, the
    nearest translator on that frequency is used (the signal you heard,
    so the map and distance are right), marked with what it relays."""
    parent = fcc_lookup(callsign, freq_mhz, fetch=fetch)
    if not (parent and freq_mhz and home and parent.get("freq_mhz")) \
            or abs(parent["freq_mhz"] - freq_mhz) < 0.05:
        return parent
    translator = fcc_translator(freq_mhz, home, fetch=fetch)
    if not translator:
        return parent
    hd = re.search(r"-HD[2-8]$", callsign or "", flags=re.I)
    out = dict(translator)
    out["relays"] = parent["callsign"] + (hd.group(0).upper() if hd else "")
    out["parent"] = {k: parent.get(k) for k in
                     ("callsign", "freq_mhz", "location", "lat", "lon")}
    return out


FCC_CONTOUR_URL = "https://transition.fcc.gov/fcc-bin/contourplot.kml"


def contour_dbu(station_class):
    """Protected service contour (dBu) for an FM station class."""
    return {"B": 54, "B1": 57}.get((station_class or "").upper(), 60)


def parse_contour(text):
    """[[lat, lon], ...] from the FCC contour map page, or []."""
    m = re.search(r"var coordinates = (\[\s*\[\s*\[.*?\]\s*\]\s*\])\s*;",
                  text or "", re.S)
    if not m:
        return []
    try:
        ring = json.loads(m.group(1))[0]
        return [[round(lat, 5), round(lon, 5)] for lon, lat in ring]
    except (ValueError, TypeError, IndexError):
        return []


def fcc_coverage(callsign, freq_mhz=None, home=None, fetch=None):
    """FCC service contour of the station heard (translator-aware), or None."""
    rec = fcc_heard(callsign, freq_mhz, home, fetch=fetch)
    if not rec or not rec.get("lmsid"):
        return None
    dbu = contour_dbu(rec.get("station_class"))
    url = FCC_CONTOUR_URL + "?" + urllib.parse.urlencode({
        "appid": rec.get("appid") or "", "lmsid": rec["lmsid"],
        "call": rec["callsign"], "freq": rec.get("freq_mhz") or "",
        "contour": dbu}) + "&.map"
    points = parse_contour(fetch(url) if fetch else http_get_text(url, 20))
    if len(points) < 3:
        return None
    return {"callsign": rec["callsign"], "freq_mhz": rec.get("freq_mhz"),
            "dbu": dbu, "points": points, "relays": rec.get("relays")}


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_geocode_lock = threading.Lock()
_geocode_last = [0.0]


def _place_parts(city, state, country):
    return [city.strip()] + [p.strip() for p in (state, country) if p and p.strip()]


def cached_place(city, state, country, cache):
    """(lat, lon) of a city already in `cache`, without going online."""
    if not (city or "").strip() or not cache:
        return None
    hit = cache.get(", ".join(_place_parts(city, state, country)).lower())
    return tuple(hit) if hit else None


def geocode_place(city, state=None, country=None, cache=None, fetch=None):
    """(lat, lon) of a city from OpenStreetMap's Nominatim, or None.

    `cache` (a dict, persisted by the caller) remembers answers including
    misses. Live requests are spaced 1.1 s apart per Nominatim's policy.
    """
    city = (city or "").strip()
    if not city:
        return None
    parts = _place_parts(city, state, country)
    key = ", ".join(parts).lower()
    if cache is not None and key in cache:
        hit = cache[key]
        return tuple(hit) if hit else None
    params = {"q": ", ".join(parts), "format": "jsonv2", "limit": "1"}
    if not country:
        params["countrycodes"] = "us,ca,mx"
    url = NOMINATIM_URL + "?" + urllib.parse.urlencode(params)
    if fetch:
        text = fetch(url)
    else:
        with _geocode_lock:
            wait = _geocode_last[0] + 1.1 - time.time()
            if wait > 0:
                time.sleep(wait)
            try:
                text = http_get_text(url)
            finally:
                _geocode_last[0] = time.time()
    try:
        hits = json.loads(text)
        result = (round(float(hits[0]["lat"]), 5), round(float(hits[0]["lon"]), 5))
    except (ValueError, LookupError, TypeError):
        result = None
    if cache is not None:
        cache[key] = list(result) if result else None
    return result


def http_get_bytes(url, timeout=12):
    """(body, content type). Some servers (FCC) refuse unfamiliar
    User-Agents with 403, so fall back through a few before giving up."""
    for ua in (USER_AGENT, None, "curl/8.0"):
        headers = {"User-Agent": ua} if ua else {}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if e.code != 403 or ua == "curl/8.0":
                raise


_pool = {}  # host -> idle open connections, reused for map tiles
_pool_lock = threading.Lock()


def http_get_pooled(url, timeout=20):
    """(body, content type) like http_get_bytes, but over a kept-open
    connection: no new TLS handshake per map tile."""
    u = urllib.parse.urlsplit(url)
    path = u.path + ("?" + u.query if u.query else "")
    for _ in range(2):
        with _pool_lock:
            idle = _pool.setdefault(u.netloc, [])
            conn = idle.pop() if idle else None
        fresh = conn is None
        if fresh:
            conn = http.client.HTTPSConnection(
                u.netloc, timeout=timeout, context=ssl.create_default_context())
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT})
            r = conn.getresponse()
            body = r.read()
        except (http.client.HTTPException, OSError):
            conn.close()
            if fresh:
                raise
            continue  # the server closed an idle connection: open a new one
        if r.will_close:
            conn.close()
        else:
            with _pool_lock:
                if len(_pool[u.netloc]) < 16:
                    _pool[u.netloc].append(conn)
                else:
                    conn.close()
        if r.status != 200:
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, None)
        return body, r.headers.get("Content-Type", "")
    raise OSError("connection kept failing")


def http_get_text(url, timeout=12):
    raw = http_get_bytes(url, timeout)[0]
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_station_line(line):
    """RDS Monitor stations.txt line 'PI CALL | City, ST | lat lon | Slogan'."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    fields = [x.strip() for x in line.split("|")]
    head = fields[0].split()
    if len(head) < 2:
        return None
    lat = lon = None
    if len(fields) > 2:
        nums = fields[2].replace(",", " ").split()
        try:
            lat, lon = float(nums[0]), float(nums[1])
        except (IndexError, ValueError):
            pass
    return {"pi": head[0].upper(), "callsign": head[1].upper(),
            "location": fields[1] if len(fields) > 1 and fields[1] else None,
            "lat": lat, "lon": lon,
            "slogan": fields[3] if len(fields) > 3 and fields[3] else None}


def load_monitor_stations(path):
    """{PI: entry} and {CALL: entry} from the monitor's stations.txt."""
    by_pi, by_call = {}, {}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                e = parse_station_line(line)
                if e:
                    by_pi.setdefault(e["pi"], e)
                    by_call.setdefault(e["callsign"], e)
    except OSError:
        pass
    return by_pi, by_call
