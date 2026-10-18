"""Station details from the WTFDA FM database (https://db.wtfda.org/).

One search per station by call letters. The site's robots.txt asks for
30 seconds between requests, so lookups go one at a time through a queue.
"""

import html
import http.cookiejar
import re
import urllib.parse
import urllib.request

SITE = "https://db.wtfda.org/"
USER_AGENT = "DX-Mapper/1.0 (station details lookup)"
GAP_S = 30
COLUMNS = ("callsign", "relay_of", "freq", "city", "state", "country",
           "format", "slogan", "facility_id", "erp_h", "erp_v", "haat_h",
           "haat_v", "lat", "lon", "language", "mode", "pi", "ps", "radiotext",
           "pty", "remarks")


def _fetch(callsign, pi=""):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [("User-Agent", USER_AGENT), ("Referer", SITE)]
    page = op.open(SITE, timeout=20).read().decode("utf-8", "replace")
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', page)
    form = {k: "" for k in ("frequency", "city", "state", "fac_country", "prl",
                            "format", "slogan", "language", "mode", "picode", "id")}
    form.update(csrfmiddlewaretoken=m.group(1) if m else "", callsign=callsign,
                picode=pi, numperpage="25")
    body = urllib.parse.urlencode(form).encode()
    return op.open(SITE + "fac_frequency/up/1", body, timeout=20).read().decode(
        "utf-8", "replace")


def parse_rows(page):
    """Result rows as dicts (COLUMNS), skipping the search form and header."""
    out = []
    table = page[page.find("</form>"):] if "</form>" in page else page
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)
        if len(cells) != len(COLUMNS) or "<a" in cells[0]:
            continue
        vals = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
        out.append({k: v for k, v in zip(COLUMNS, vals) if v and v != "-"})
    return out


def _dms(text, west=False):
    m = re.fullmatch(r"(\d+)-(\d+)-(\d+(?:\.\d+)?)", text or "")
    if not m:
        return None
    deg = int(m.group(1)) + int(m.group(2)) / 60 + float(m.group(3)) / 3600
    return round(-deg if west else deg, 5)


def lookup(callsign, freq_mhz=None, fetch=_fetch, pi=None):
    """The WTFDA entry for a station ({} when it has none), matched on call
    letters (WXYZ = WXYZ-FM) and, when given, the frequency. With no call
    letters, by PI code: only when exactly one station on the frequency
    has it."""
    call = re.sub(r"-(FM|LP|HD\d)$", "", (callsign or "").upper().strip())
    pi = (pi or "").upper().strip()
    if not call:
        if not re.fullmatch(r"[0-9A-F]{4}", pi) or freq_mhz is None:
            return {}
        hits = []
        for r in parse_rows(fetch("", pi=pi)):
            try:
                if (r.get("pi") or "").upper() == pi and abs(
                        float(r.get("freq")) - freq_mhz) < 0.05:
                    hits.append(r)
            except (TypeError, ValueError):
                pass
        return _coords(hits[0]) if len(hits) == 1 else {}
    rows = parse_rows(fetch(call))
    best = None
    for r in rows:
        if re.sub(r"-(FM|LP)$", "", r.get("callsign", "").upper()) != call:
            continue
        try:
            same = freq_mhz is None or abs(float(r.get("freq")) - freq_mhz) < 0.05
        except (TypeError, ValueError):
            same = False
        if same:
            best = r
            break
        best = best or r
    return _coords(best) if best else {}


def _coords(row):
    out = dict(row)
    best = row
    lat, lon = _dms(best.get("lat")), _dms(best.get("lon"), west=True)
    if lat is not None and lon is not None:
        out["lat"], out["lon"] = lat, lon
    else:
        out.pop("lat", None)
        out.pop("lon", None)
    return out
