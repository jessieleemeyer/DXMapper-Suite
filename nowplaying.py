"""Now-playing extraction and temporary cover-art lookup for the RDS Monitor.

`parse_now_playing(rt)` turns free-form RadioText into (artist, title) when
the text looks like a track announcement; RT+ Title/Artist tags always win
over this and are handled by the caller.

`ArtCache` fetches cover art from the iTunes Search API into memory only.
Nothing is ever written to disk: the cache is a dict of image bytes that
disappears when the program exits.
"""

import io
import json
import re
import threading
import urllib.parse
import urllib.request

ITUNES_SEARCH = "https://itunes.apple.com/search"
ART_SIZE = 300          # pixels requested from the CDN (artworkUrl100 -> 300x300)
FETCH_TIMEOUT = 8       # seconds per HTTP request
CACHE_MAX_TRACKS = 40   # distinct tracks remembered in RAM per run
USER_AGENT = "RDS-Monitor/1.0 (cover art lookup)"

# Prefixes stations put in front of the track announcement.
_PREFIX_RE = re.compile(
    r"^\s*(?:now\s*playing|now\s*on\s*air|playing\s*now|playing|np|on\s*air|"
    r"you'?re\s*listening\s*to|currently\s*playing)\s*[:\-|]?\s*",
    re.IGNORECASE)

# Trailing station slogans / call-to-actions after the track.
_FREQ_RE = re.compile(r"\b\d{2,3}\.\d\s*(?:fm)?\b", re.IGNORECASE)
_CTA_RE = re.compile(r"^(?:text|call|visit|www\.|http)", re.IGNORECASE)
_ON_STATION_RE = re.compile(r"\s+(?:(?:only|here|now)\s+)?on\s+(?:the\s+)?[^|~]*\b\d{2,3}\.\d.*$",
                            re.IGNORECASE)


def _strip_suffix(text):
    """Drop trailing station slogans / call-to-actions after the track."""
    text = _ON_STATION_RE.sub("", text)
    for sep in (" | ", " ~ ", " - ", " \u2013 ", " \u2014 "):
        while sep in text:
            head, _, tail = text.rpartition(sep)
            tail = tail.strip()
            if head and (_CTA_RE.match(tail) or _FREQ_RE.search(tail)):
                text = head
            else:
                break
    return text

_SEPARATORS = (" - ", " – ", " — ", " / ", " | ", " ~ ", " :: ")

# Text that is a station message, not a song.
_NOT_A_TRACK_RE = re.compile(
    r"\b(?:traffic|weather|news|call\s+now|text\s+\w+\s+to|contest|"
    r"win\b|tickets|advertis|www\.|\.com\b|http|"
    # sports
    r"vs\.?\s|play[\s-]by[\s-]play|kick-?off|tip-?off|halftime|pregame|"
    r"post-?game|game\s+(?:day|time|night)|scores?\b|football|basketball|"
    r"baseball|softball|volleyball|playoffs?\b|"
    # church / religious programming
    r"sermon|pastor|ministr|bible|scripture|devotion|worship\s+service|"
    r"church\s+service|sunday\s+service|prayer\s+(?:line|request|time)|"
    # promos / contact
    r"download|our\s+app|facebook|instagram|twitter|tiktok|follow\s+us|"
    r"like\s+us|sponsored|brought\s+to\s+you|presented\s+by|"
    r"\d{3}[-.\s]\d{3}[-.\s]\d{4}|sale\b|% off|"
    r"listen\s+(?:live|online|on\s+the)|stream\s+us)",
    re.IGNORECASE)

# "... by Artist on The Wave": the station's own tag after the artist.
_ON_TAG_RE = re.compile(r"\s+(?:only\s+|here\s+|now\s+)?(?:on|at)\s+\S.*$",
                        re.IGNORECASE)


def _strip_names(text, names):
    """Drop the station's own name (PS, call letters, slogan) at the end,
    e.g. "Fast Car - Luke Combs - The Wave"."""
    for name in sorted({(n or "").strip() for n in names if n
                        and len((n or "").strip()) >= 3}, key=len, reverse=True):
        esc = re.escape(name)
        text = re.sub(r"^(?:the\s+)?" + esc + r"\s*[-|~:\u2013\u2014]\s*", "",
                      text, flags=re.IGNORECASE)
        text = re.sub(r"\s*(?:[-|~:\u2013\u2014]\s*)?(?:(?:only\s+|now\s+)?"
                      r"(?:on|at)\s+)?(?:the\s+)?" + esc + r"[!.\s]*$", "",
                      text, flags=re.IGNORECASE)
    return text


def _clean(s):
    s = s.strip().strip('"\u201c\u201d\'').strip()
    s = re.sub(r"\s{2,}", " ", s)
    return s


def rt_kind(rt, names=()):
    """"song", "promo" (ads, sports, church programming, contact lines) or
    "other" (slogans and station IDs)."""
    if not rt or not rt.strip():
        return None
    if any(parse_now_playing(rt, names)):
        return "song"
    return "promo" if _NOT_A_TRACK_RE.search(rt) else "other"


def parse_now_playing(rt, names=()):
    """Return (artist, title) parsed from RadioText, or ("", "") if the
    text does not look like a track announcement.

    Recognised shapes (case-insensitive prefixes are removed first):
      "Artist - Title"          "Now Playing: Artist - Title"
      "Title by Artist"         "\"Title\" by Artist"
      "Artist / Title"          "Artist | Title"
    Plain slogans, promos and contact lines return ("", "").
    """
    if not rt:
        return "", ""
    text = _clean(rt)
    had_prefix = bool(_PREFIX_RE.match(text))
    text = _PREFIX_RE.sub("", text, count=1)
    text = _strip_suffix(text)
    text = _clean(_strip_names(text, names))
    if not text:
        return "", ""
    if not had_prefix and re.search(
            r"\b(?:vs\.?\s|sermon|pastor|ministr|play[\s-]by[\s-]play|"
            r"download|sponsored|presented\s+by|brought\s+to\s+you)",
            text, re.IGNORECASE):
        return "", ""

    # "Title by Artist" (quoted title or a single " by ")
    m = re.match(r'^["\u201c](.+?)["\u201d]\s+by\s+(.+)$', text, re.IGNORECASE)
    if m:
        return _clean(m.group(2)), _clean(m.group(1))
    if text.lower().count(" by ") == 1 and not any(sep in text for sep in _SEPARATORS):
        idx = text.lower().index(" by ")
        title = _clean(text[:idx])
        artist = _clean(_ON_TAG_RE.sub("", text[idx + 4:]))
        if title and artist and not _NOT_A_TRACK_RE.search(text):
            return artist, title

    for sep in _SEPARATORS:
        if sep in text:
            parts = [_clean(p) for p in text.split(sep) if _clean(p)]
            if len(parts) >= 2:
                artist, title = parts[0], parts[1]
                if (not had_prefix and _NOT_A_TRACK_RE.search(artist + " " + title)):
                    return "", ""
                if len(artist) > 1 and len(title) > 1:
                    return artist, title
            break
    return "", ""


def track_key(artist, title):
    return (artist.strip().lower(), title.strip().lower())


# Search pages for the song on each music service, in button order.
MUSIC_SEARCH = (
    ("YTM", "YouTube Music", "https://music.youtube.com/search?q={plus}"),
    ("SPOT", "Spotify", "https://open.spotify.com/search/{path}"),
    ("AMZ", "Amazon Music", "https://music.amazon.com/search/{path}"),
    ("APL", "Apple Music", "https://music.apple.com/us/search?term={plus}"),
)


def music_search_urls(artist, title):
    """{short label: search URL} for the song on each music service, or {}
    when there is no song (promos, ads and station IDs never get a title)."""
    from urllib.parse import quote, quote_plus
    query = " ".join(x.strip() for x in (artist or "", title or "") if x.strip())
    if not query:
        return {}
    return {short: url.format(plus=quote_plus(query), path=quote(query, safe=""))
            for short, _name, url in MUSIC_SEARCH}


class ArtCache:
    """Memory-only cover-art cache with background fetching.

    get(artist, title) returns image bytes if already fetched, None while a
    fetch is pending or if the lookup failed. `on_update()` is called from
    the worker thread when a fetch completes; the GUI should re-poll.
    """

    def __init__(self, on_update=None, fetcher=None):
        self._lock = threading.Lock()
        self._images = {}      # key -> bytes
        self._failed = set()   # keys with no art available
        self._pending = set()
        self._order = []
        self._on_update = on_update
        self._fetch = fetcher or fetch_itunes_art
        self.enabled = True

    def get(self, artist, title):
        if not (artist or title) or not self.enabled:
            return None
        key = track_key(artist, title)
        with self._lock:
            if key in self._images:
                return self._images[key]
            if key in self._failed or key in self._pending:
                return None
            self._pending.add(key)
        threading.Thread(target=self._worker, args=(key, artist, title),
                         daemon=True).start()
        return None

    def status(self, artist, title):
        key = track_key(artist, title)
        with self._lock:
            if key in self._images:
                return "ready"
            if key in self._pending:
                return "fetching"
            if key in self._failed:
                return "none"
        return "idle"

    def clear(self):
        with self._lock:
            self._images.clear()
            self._failed.clear()
            self._order.clear()

    def _worker(self, key, artist, title):
        data = None
        try:
            data = self._fetch(artist, title)
        except Exception:
            data = None
        with self._lock:
            self._pending.discard(key)
            if data:
                self._images[key] = data
                self._order.append(key)
                while len(self._order) > CACHE_MAX_TRACKS:
                    self._images.pop(self._order.pop(0), None)
            else:
                self._failed.add(key)
        if self._on_update:
            try:
                self._on_update()
            except Exception:
                pass


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def fetch_itunes_art(artist, title):
    """Cover art bytes (JPEG/PNG) for a track via the iTunes Search API,
    or None if nothing matched.  Kept entirely in memory."""
    term = " ".join(p for p in (artist, title) if p).strip()
    if not term:
        return None
    query = urllib.parse.urlencode({
        "term": term, "media": "music", "entity": "song", "limit": 5,
    })
    body = _http_get(f"{ITUNES_SEARCH}?{query}")
    results = json.loads(body.decode("utf-8", errors="replace")).get("results", [])
    if not results:
        return None
    want_artist = artist.strip().lower()
    want_title = title.strip().lower()
    best = None
    for r in results:
        a = (r.get("artistName") or "").lower()
        t = (r.get("trackName") or "").lower()
        score = (want_artist and want_artist in a) + (want_title and want_title in t)
        if best is None or score > best[0]:
            best = (score, r)
    url = best[1].get("artworkUrl100") or best[1].get("artworkUrl60")
    if not url:
        return None
    url = re.sub(r"/\d+x\d+bb\.", f"/{ART_SIZE}x{ART_SIZE}bb.", url)
    data = _http_get(url)
    return data if data and len(data) > 100 else None


def image_from_bytes(data):
    """PIL Image from raw bytes (caller keeps the Tk PhotoImage alive)."""
    from PIL import Image
    return Image.open(io.BytesIO(data))
