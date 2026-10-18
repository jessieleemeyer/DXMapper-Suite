"""RadioText kept as the station's pattern ("Song by Artist on The Wave").

The song parser is the RDS Monitor's nowplaying.py (same rules), so DX
Mapper can clean RadioText that was imported, typed in or saved before.
"""

import re
from collections import Counter

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
    r"\brt\s*\+|\brt\s*plus|"
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


# Several captures pasted into one cell: lines, semicolons, " || ", " • ",
# or runs of spaces between them.
_SPLIT_RE = re.compile(r"\s*(?:[\r\n;]+|\s{3,}|\s\|\|\s|\s\u2022\s)\s*")
_PATTERN_RE = re.compile(r"\b(?:Song|Artist)\b")


def template(rt, artist=None, title=None):
    """RadioText with the song playing replaced by the words Artist and Song."""
    if not rt:
        return rt
    for val, word in sorted(((artist, "Artist"), (title, "Song")),
                            key=lambda p: -len(p[0] or "")):
        val = (val or "").strip()
        if len(val) >= 2:
            rt = re.sub(re.escape(val), word, rt, flags=re.I)
    return rt


# A website, also when run into the next word ("GoBigKentucky.coArtist").
_URL_RE = re.compile(r"\b[\w-]+\.(?i:com?|net|org|fm|us|info|radio)(?=\b|[A-Z])")

# A log sheet's description of what was caught: RT 'text' (note),
# RT template 'text' (64/64 chars), RT B/1 'text' (note).
_WRAPPED_RE = re.compile(
    "^\\s*RT\\b[^'\"\u2018\u201c]*['\"\u2018\u201c](.+?)['\"\u2019\u201d]"
    "\\s*(?:\\([^)]*\\))?\\s*$", re.IGNORECASE)


def _unwrap(piece):
    m = _WRAPPED_RE.match(piece)
    return m.group(1) if m else piece


def clean_rt(text, names=()):
    """The station's RadioText pattern from what was caught, imported or
    typed in: a song becomes "Song by Artist ..."; promos, sports, church
    programming and contact lines are dropped; with no song, the first
    slogan / station ID line is kept. "" when nothing is worth keeping."""
    if text is None or not str(text).strip():
        return ""
    songs, other = [], []
    for piece in _SPLIT_RE.split(str(text)):
        piece = _clean(_unwrap(piece.strip()))
        if not piece or _NOT_A_TRACK_RE.search(piece) or _URL_RE.search(piece):
            continue
        if _PATTERN_RE.search(piece):
            songs.append(piece)
            continue
        artist, title = parse_now_playing(piece, names)
        if artist or title:
            songs.append(template(piece, artist, title))
        elif not _NOT_A_TRACK_RE.search(piece):
            other.append(piece)
    if songs:
        return Counter(songs).most_common(1)[0][0]
    return other[0] if other else ""
