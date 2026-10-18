"""Find a station's live internet stream: Radio Browser (community
directory), then iHeartRadio, then TuneIn. Every match must carry the
station's own call letters, and a frequency it names must be the station's."""

import html
import json
import re
import urllib.parse
import urllib.request

from .geo import USER_AGENT

RADIO_BROWSER = "https://de1.api.radio-browser.info/json/stations/search?"
IHEART = "https://api.iheart.com/api/v2/content/liveStations?"
TUNEIN = "https://opml.radiotime.com/Search.ashx?"
WIKI_API = "https://en.wikipedia.org/w/api.php?"
STREAM_HOST = re.compile(
    r"streamtheworld|streamguys|ihrhls|revma|streamon\.fm|securenetsystems|"
    r"radio\.co\b|zeno\.fm|listen2myradio|surfernetwork|triton|"
    r"\.m3u8?\b|\.pls\b|/stream\b|/live\b|/listen\b", re.I)
LISTEN_TEXT = re.compile(r"listen\s*(?:live|now)|live\s*stream|stream\s*live|"
                         r"listen\b|on\s*air\s*now", re.I)


def _get_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _get_text(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(600000).decode("utf-8", "replace"), r.geturl()


def _site(url):
    url = (url or "").strip()
    if not url or url.lower() in ("0", "none", "null"):
        return None
    return url if re.match(r"https?://", url, re.I) else "https://" + url


def split_call(callsign):
    """('WOVO', 2) for WOVO-HD2, ('WXYZ', None) for WXYZ / WXYZ-FM."""
    m = re.fullmatch(r"([A-Z0-9]{3,8}?)(?:-FM)?(?:-HD([1-8]))?",
                     (callsign or "").strip().upper())
    if not m:
        return None, None
    hd = int(m.group(2)) if m.group(2) else None
    return m.group(1), (None if hd == 1 else hd)


def name_matches(name, call, freq_mhz=None, hd=None):
    """True when a directory entry's name is this station: its call
    letters as a word, the right HD channel, and no other FM/AM dial."""
    text = (name or "").upper()
    if not re.search(rf"(?<![A-Z0-9]){re.escape(call)}(?:-FM)?(?![A-Z0-9])", text):
        return False
    hds = {int(n) for n in re.findall(r"HD\s*([2-8])", text)}
    if hd != (min(hds) if hds else None):
        return False
    if freq_mhz:
        fm = {float(x) for x in re.findall(r"(?<![\d.])(\d{2,3}\.\d)(?![\d.])", text)
              if 87.5 <= float(x) <= 108.0}
        if fm and all(abs(f - freq_mhz) > 0.05 for f in fm):
            return False
        am = [int(x) for x in re.findall(r"(?<![\d.])(\d{3,4})(?![\d.])", text)
              if 530 <= int(x) <= 1710]
        if am and not fm:
            return False  # the AM station with the same call letters
    return True


def radio_browser(call, freq_mhz=None, hd=None, get=_get_json):
    rows = get(RADIO_BROWSER + urllib.parse.urlencode(
        {"name": call, "limit": 30, "hidebroken": "true",
         "order": "clickcount", "reverse": "true"}))
    for r in rows or []:
        url = r.get("url_resolved") or r.get("url")
        if url and name_matches(r.get("name"), call, freq_mhz, hd):
            return {"url": url, "source": "Radio Browser", "name": r.get("name"),
                    "website": _site(r.get("homepage"))}
    return None


def iheart(call, freq_mhz=None, hd=None, get=_get_json):
    letters = f"{call}-FM" + (f"-HD{hd}" if hd else "")
    data = get(IHEART + urllib.parse.urlencode(
        {"callLetters": letters, "limit": 5}))
    for h in (data or {}).get("hits") or []:
        if (h.get("band") or "FM").upper() != "FM" or not h.get("isActive", True):
            continue
        try:
            if freq_mhz and abs(float(h.get("freq")) - freq_mhz) > 0.05:
                continue
        except (TypeError, ValueError):
            pass
        if h.get("id"):
            return {"url": f"https://www.iheart.com/live/{h['id']}/",
                    "source": "iHeartRadio", "name": h.get("name"),
                    "website": _site(h.get("website"))}
    return None


def tunein(call, freq_mhz=None, hd=None, get=_get_json):
    data = get(TUNEIN + urllib.parse.urlencode(
        {"query": call, "types": "station", "render": "json"}))
    for x in (data or {}).get("body") or []:
        gid = x.get("guide_id") or ""
        if (x.get("item") == "station" and gid.startswith("s")
                and name_matches(x.get("text"), call, freq_mhz, hd)):
            return {"url": f"https://tunein.com/radio/{gid}/",
                    "source": "TuneIn", "name": x.get("text")}
    return None


def _first_url(value):
    m = re.search(r"https?://[^\s|\]}<>]+", value or "")
    if m:
        return m.group(0)
    m = re.search(r"\{\{\s*URL\s*\|\s*([^|}]+)", value or "", re.I)
    return _site(m.group(1)) if m else None


def wikipedia(call, freq_mhz=None, hd=None, get=_get_json):
    """The infobox's webcast (Listen Live) and website links."""
    for title in (call + "-FM", call, call + " (FM)", call + "-LP"):
        data = get(WIKI_API + urllib.parse.urlencode(
            {"action": "parse", "page": title, "prop": "wikitext", "section": 0,
             "redirects": 1, "format": "json"}))
        text = (((data or {}).get("parse") or {}).get("wikitext") or {}).get("*", "")
        if not text or re.search(r"\{\{\s*Infobox (?:television|TV)", text, re.I):
            continue
        fields = dict(re.findall(r"^[ \t]*\|[ \t]*(\w+)[ \t]*=[ \t]*(.*?)[ \t]*$",
                                 text, re.M))
        if freq_mhz:  # the same call letters on another station's page
            freqs = " ".join(fields.get(k, "") for k in
                             ("frequency", "translators", "translator", "repeater"))
            freqs += " " + " ".join(re.findall(
                r"\{\{\s*Radio Relay\s*\|\s*([\d.]+)", text, re.I))
            nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", freqs)]
            if not any(abs(n - float(freq_mhz)) < 0.05 for n in nums):
                continue
        url = None if hd else _first_url(fields.get("webcast"))
        site = _first_url(fields.get("website"))
        if url or site:
            return {"url": url, "source": "Wikipedia", "website": site}
    return None


def website_listen(site, get_text=_get_text):
    """A Listen Live link on the station's own home page, or None."""
    page, final = get_text(site)
    links = []
    for m in re.finditer(r"<a\b[^>]*?href=[\"']([^\"'#][^\"']*)[\"'][^>]*>(.*?)</a>",
                         page, re.I | re.S):
        href = html.unescape(m.group(1)).strip()
        if href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        label = re.sub(r"<[^>]+>", " ", m.group(2)) + " " + m.group(0)[:300]
        links.append((urllib.parse.urljoin(final or site, href), label))
    store = re.compile(r"apps\.apple|play\.google|itunes|facebook|twitter|"
                       r"instagram|youtube|tiktok|alexa|amazon\.", re.I)
    for url, label in links:  # "Listen Live" first, then a known stream host
        if LISTEN_TEXT.search(label) and not store.search(url):
            return url
    for url, label in links:
        if STREAM_HOST.search(url) and not store.search(url):
            return url
    return None


SOURCES = (radio_browser, iheart, tunein, wikipedia)


def find_stream(callsign, freq_mhz=None, get=_get_json, get_text=_get_text):
    """{"url", "source", "name", "website"} for the station: the first
    directory with its stream, the station's website from any of them,
    and failing a stream, a Listen Live link on that website. None when
    nothing was found; LookupError when no directory could be reached
    (so "not found" is never saved by mistake)."""
    call, hd = split_call(callsign)
    if not call:
        return None
    errors, found, website = [], None, None
    for source in SOURCES:
        try:
            hit = source(call, freq_mhz, hd, get=get)
        except Exception as e:
            errors.append(e)
            continue
        if hit:
            website = website or hit.get("website")
            if hit.get("url") and not found:
                found = hit
        if found and website:
            break
    if not found and website:
        try:
            url = website_listen(website, get_text)
        except Exception:
            url = None
        if url:
            found = {"url": url, "source": "Station website"}
    if found or website:
        return dict(found or {}, website=website)
    if SOURCES and len(errors) == len(SOURCES):
        raise LookupError(f"no stream directory could be reached ({errors[0]})")
    return None
