"""Station logo download, the same lookup the RDS Monitor's own download
uses: Wikipedia page image, then the Wikipedia infobox, then the station's
Facebook picture. Saved into the RDS Monitor's logos folder so both apps
show it."""
import json
import os
import re
import time
import urllib.parse
from pathlib import Path

from .geo import http_get_bytes

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_REST = "https://en.wikipedia.org/api/rest_v1"
FACEBOOK_PICTURE = "https://graph.facebook.com/{page}/picture?type=large"
_SKIP_IMG = ("icon", "commons-logo", "ambox", "portal", "stub", "wikimedia",
             "search")
_SKIP_FB = {"sharer", "sharer.php", "share", "plugins", "dialog", "tr",
            "login", "policies", "help", "pages", "profile.php", "groups",
            "events", "hashtag", "watch", "marketplace", "privacy"}
_SKIP_SITE = ("fcc.gov", "fccdata", "radio-locator", "wikipedia", "wikimedia",
              "archive.org", "nielsen", "radiodiscussions")


def image_ext(data):
    """File extension for image bytes, or None when it isn't an image."""
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    return None


def _json(get, url, params):
    return json.loads(get(url + "?" + urllib.parse.urlencode(params))[0])


def facebook_page(text):
    for m in re.finditer(r"(?:https?://)?(?:www\.|m\.)?(?:facebook|fb)\.com/"
                         r"([A-Za-z0-9._-]{2,})", text):
        slug = m.group(1).rstrip(".")
        # facebook.com/2008/fbml is page code, not a page
        if slug.lower() not in _SKIP_FB and not re.fullmatch(r"\d{1,5}", slug):
            return slug
    return None


# Words in a picture's file name that mean a photo, not a logo.
_PHOTO_WORDS = ("tower", "studio", "building", "office", "transmitter",
                "antenna", "facility", "facilities", "headquarters", "mast",
                "site", "photo", "sign")


def _file_name(url_or_field):
    """'File:WKQQ 100.1 logo.png' / a Wikimedia (thumb) URL -> its file name,
    lower case with spaces: 'wkqq 100.1 logo.png'."""
    v = urllib.parse.unquote(str(url_or_field or "")).split("?")[0]
    v = re.sub(r"^\[\[|\]\]$", "", v.strip()).split("|")[0]
    v = re.sub(r"^(?:file|image):", "", v.strip(), flags=re.I)
    v = v.rstrip("/").rsplit("/", 1)[-1]
    v = re.sub(r"^\d+px-", "", v)
    return v.replace("_", " ").strip().lower()


def _photo(name, avoid):
    """A facility photo (the infobox's "image", or a tower/studio file)?"""
    return bool(name) and (name in avoid or (
        "logo" not in name and any(w in name for w in _PHOTO_WORDS)))


def facebook_picture(slug, get=http_get_bytes):
    """The page's profile picture URL, or None when Facebook only has its
    grey person placeholder ("silhouette") or no such page."""
    try:
        data = _json(get, FACEBOOK_PICTURE.format(page=slug).split("?")[0],
                     {"type": "large", "redirect": "false"}).get("data") or {}
    except Exception:
        return None
    return None if data.get("is_silhouette") else data.get("url")


def candidates(callsign, get=http_get_bytes):
    """Image URLs to try for a call, best first (lazily, tier by tier).
    "WHAS" is often a disambiguation page, so WHAS-FM and WHAS (FM) next."""
    call = callsign.upper()
    titles = [call] if call.endswith("-FM") else [call, call + "-FM",
                                                  call + " (FM)"]
    for title in titles:
        yield from _title_candidates(title, get)


def _title_candidates(callsign, get):
    avoid = set()
    try:  # the infobox's own "logo" picture first; its "image" is a photo
        text = _json(get, WIKI_API, {
            "action": "parse", "page": callsign, "prop": "wikitext",
            "section": 0, "redirects": 1, "format": "json",
        }).get("parse", {}).get("wikitext", {}).get("*", "")
        fields = dict(re.findall(r"^[ \t]*\|[ \t]*(\w+)[ \t]*=[ \t]*(.*?)[ \t]*$",
                                 text, re.M))
        avoid = {_file_name(fields[k]) for k in ("image", "photo")
                 if fields.get(k)} - {""}
        logo = _file_name(fields.get("logo"))
        if logo and re.search(r"\.(png|jpe?g|gif|svg|webp)$", logo):
            pages = _json(get, WIKI_API, {
                "action": "query", "titles": "File:" + logo,
                "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 500,
                "format": "json"}).get("query", {}).get("pages", {})
            for page in pages.values():
                for info in page.get("imageinfo") or []:
                    src = info.get("thumburl") or info.get("url")
                    if src:
                        yield src.split("?")[0]
    except Exception:
        pass
    try:
        pages = _json(get, WIKI_API, {
            "action": "query", "titles": callsign, "prop": "pageimages",
            "pithumbsize": 500, "pilicense": "any", "format": "json",
            "redirects": 1,
        }).get("query", {}).get("pages", {})
        for pid, page in pages.items():
            src = pid != "-1" and (page.get("thumbnail") or {}).get("source")
            if src and not _photo(_file_name(src), avoid):
                yield src.split("?")[0]
    except Exception:
        pass
    try:
        html = get(f"{WIKI_REST}/page/html/"
                   f"{urllib.parse.quote(callsign)}")[0].decode("utf-8",
                                                               "ignore")
        if 'id="disambigbox"' in html or "mw:PageProp/disambiguation" in html:
            return
        for tag in re.findall(r"<img[^>]+>", html)[:10]:
            m = re.search(r'src="([^"]+)"', tag)
            sizes = [int(n) for n in
                     re.findall(r'(?:width|height)="(\d+)"', tag)]
            if not m or (sizes and min(sizes) < 60):
                continue
            src = m.group(1)
            src = "https:" + src if src.startswith("//") else src
            name = src.rsplit("/", 1)[-1].lower()
            if (not name.endswith(".svg")
                    and not any(s in name for s in _SKIP_IMG)
                    and not _photo(_file_name(src), avoid)):
                yield src
    except Exception:
        pass
    try:
        sites = []
        pages = _json(get, WIKI_API, {
            "action": "query", "titles": callsign, "prop": "extlinks",
            "ellimit": 100, "format": "json", "redirects": 1,
        }).get("query", {}).get("pages", {})
        for page in pages.values():
            for link in page.get("extlinks", []):
                url = link.get("*", "")
                if (url.lower().startswith("http")
                        and not any(s in url.lower() for s in _SKIP_SITE)):
                    sites.append(url)
        slug = next(filter(None, map(facebook_page, sites)), None)
        for url in sites[:3]:
            if slug:
                break
            try:
                slug = facebook_page(get(url)[0].decode("utf-8", "ignore"))
            except Exception:
                continue
        pic = slug and facebook_picture(slug, get)
        if pic:
            yield pic
    except Exception:
        pass


def download(callsign, folder, get=http_get_bytes):
    """Find and save CALL.<ext> in folder. The saved Path, or None.
    A station with no logo of its own gets its network's (e.g. a King of
    Kings Radio station): the network's Wikipedia page, then the website
    on the station's page."""
    def first(urls):
        for url in urls:
            try:
                path = _save(get(url)[0], folder, callsign)
            except Exception:
                continue
            if path:
                return path
        return None
    path = first(candidates(callsign, get))
    if path:
        return path
    try:
        network, site = wiki_parent(wiki_page(callsign, get=get))
    except Exception:
        return None
    if network and network.upper() != callsign.upper():
        path = first(_title_candidates(network, get))
    if not path and site:
        path = download_site_logo(callsign, site, folder, get)
    return path


def all_candidates(callsign, get=http_get_bytes, limit=12):
    """Every logo URL the lookup can find for a call, for the logo picker:
    [{"url", "source"}], the station's own first, then its network's and
    its website's."""
    out, seen = [], set()

    def add(urls, source):
        for url in urls:
            key = _file_name(url) if "wikimedia" in url else url.split("?")[0]
            if (len(out) >= limit or key in seen or key.lower().endswith(".ico")
                    or re.search(r"default-favicon|favicon-\d+x\d+", key, re.I)):
                continue
            seen.add(key)
            out.append({"url": url.replace("&amp;", "&"), "source": source})
    add(candidates(callsign, get), "Wikipedia / Facebook")
    try:
        network, site = wiki_parent(wiki_page(callsign, get=get))
    except Exception:
        network = site = None
    if network and network.upper() != callsign.upper():
        add(_title_candidates(network, get), "Network: " + network)
    if site:
        try:
            html = get(site)[0].decode("utf-8", "ignore")
            add(site_logo_candidates(html, site), "Website")
        except Exception:
            pass
    return out


def fetch_image(url, get=http_get_bytes):
    """(bytes, ".ext") of an image URL, or None when it isn't an image."""
    data = get(url)[0]
    ext = image_ext(data or b"")
    return (data, ext) if ext and len(data) >= 200 else None


def wiki_parent(text):
    """(network page title, website URL) from a station's infobox, each
    or None. The network is the "network" line, or "affiliations" when it
    links just one page (several are program suppliers, not the parent)."""
    fields = dict(re.findall(r"^[ \t]*\|[ \t]*(\w+)[ \t]*=[ \t]*(.*?)[ \t]*$",
                             text or "", re.M))
    network = None
    links = re.findall(r"\[\[([^\]|#]+)", fields.get("network", ""))
    if links:
        network = links[0].strip()
    else:
        links = re.findall(r"\[\[([^\]|#]+)", fields.get("affiliations", ""))
        if len(links) == 1:
            network = links[0].strip()
    site = None
    m = re.search(r"https?://[^\s\]|}<]+", fields.get("website", ""))
    if m:
        site = m.group(0)
    else:
        m = re.search(r"\{\{\s*url\s*\|\s*([\w.-]+\.[a-z]{2,}[^\s|}]*)",
                      fields.get("website", ""), re.I)
        site = m and "http://" + m.group(1)
    if site and any(s in site.lower() for s in _SKIP_SITE):
        site = None
    return network, site


def _wiki_clean(v):
    v = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>|<!--.*?-->", "", v, flags=re.S)
    v = re.sub(r"<br\s*/?>", " / ", v)
    v = re.sub(r"\{\{[^{}|]*\|([^{}]*)\}\}", r"\1", v)  # {{nowrap|x}} -> x
    v = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"<[^>]+>|'''?|\{\{|\}\}", "", v)
    return re.sub(r"\s+", " ", v).strip(" ;,/\"“”")


def wiki_subchannel(text, n):
    """An HD subchannel's name from the infobox's Subchannels line, e.g.
    "HD2: The Loop ([[classic rock]])" -> "The Loop (classic rock)"."""
    m = re.search(r"^[ \t]*\|[ \t]*subchannels?[ \t]*=(.*?)(?=^[ \t]*\||^\}\})",
                  text + "\n}}", re.M | re.S | re.I)
    if not m:
        return None
    m = re.search(r"HD\s*%s\s*[:=\-\u2013\u2014]\s*(.+?)(?=HD\s*\d\s*[:=\-\u2013\u2014]|<br|\n|$)"
                  % n, m.group(1), re.I | re.S)
    return _wiki_clean(m.group(1)) or None if m else None


def wiki_sisters(text):
    """Call letters on the infobox's Sister stations line, in order, e.g.
    "[[WTCO]], [[WAIN (AM)|WAIN]]" -> ["WTCO", "WAIN"]."""
    m = re.search(r"^[ \t]*\|[ \t]*sister[_ ]stations?[ \t]*=(.*?)(?=^[ \t]*\||^\}\})",
                  text + "\n}}", re.M | re.S | re.I)
    if not m:
        return []
    body = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r" \1 ", m.group(1))
    body = re.sub(r"\((?:AM|FM)\)", " ", body, flags=re.I)
    out = []
    for c in re.findall(r"\b[KWC][A-Z]{2,3}(?:-(?:FM|LP|AM|TV|CD|LD))?\b", body):
        if c not in out:
            out.append(c)
    return out


def wiki_hd_info(text):
    """What a station's infobox says about its HD channels: names
    {"HD2": "98.5 Icons (Classic country)"}, the website of each HD
    channel {"HD2": url} and its translators [{"freq_mhz", "call",
    "city", "hd"}] ("hd" is the HD channel it carries, or None)."""
    names = {}
    for n in range(2, 9):
        name = wiki_subchannel(text, n)
        if name:
            names[f"HD{n}"] = name
    relays = []
    for m in re.finditer(r"\{\{\s*Radio Relay\s*\|((?:[^{}]|\{\{[^{}]*\}\})*)\}\}",
                         text, re.I):
        body = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", m.group(1))
        parts = [x.strip() for x in body.split("|")]
        try:
            freq = float(parts[0])
        except (ValueError, IndexError):
            continue
        hd = next((f"HD{h.group(1)}" for x in parts[3:]
                   for h in [re.fullmatch(r"HD\s*([1-8])", x, re.I)] if h), None)
        relays.append({"freq_mhz": freq,
                       "call": (parts[1] if len(parts) > 1 else "").upper() or None,
                       "city": _wiki_clean(parts[2]) if len(parts) > 2 else None,
                       "hd": None if hd == "HD1" else hd})
    sites = {}
    m = re.search(r"^[ \t]*\|[ \t]*website[ \t]*=(.*)$", text, re.M | re.I)
    for part in re.split(r"<br\s*/?>", m.group(1) if m else "", flags=re.I):
        hd = re.search(r"\(\s*HD\s*([2-8])\s*\)", part, re.I)
        url = re.search(r"https?://[^\s|\]}<>]+", part)
        if hd and url:
            sites[f"HD{hd.group(1)}"] = url.group(0)
    return {"names": names, "sites": sites, "relays": relays}


def wiki_page(callsign, freq_mhz=None, get=http_get_bytes):
    """The station's own Wikipedia infobox text (the first page whose
    frequency matches; an FM translator listed for an AM counts), or ""."""
    return wiki_find(callsign, freq_mhz, get)[1]


def wiki_summary(callsign, freq_mhz=None, get=http_get_bytes):
    """{"text", "url"} - the opening paragraph of the station's own
    Wikipedia article (same page match as wiki_page), or None."""
    title = wiki_find(callsign, freq_mhz, get)[0]
    if not title:
        return None
    page = _json(get, WIKI_REST + "/page/summary/"
                 + urllib.parse.quote(title.replace(" ", "_"), safe=""), {})
    text = (page.get("extract") or "").strip()
    if not text or page.get("type") == "disambiguation":
        return None
    url = ((page.get("content_urls") or {}).get("desktop") or {}).get("page")
    return {"text": text, "url": url or "https://en.wikipedia.org/wiki/"
            + urllib.parse.quote(title.replace(" ", "_"))}


def wiki_find(callsign, freq_mhz=None, get=http_get_bytes):
    """(title, infobox text) of the station's own Wikipedia page, or
    (None, "")."""
    call = re.sub(r"-HD[1-8]$", "", callsign.upper())
    titles = ([call] if call.endswith(("-FM", "-LP")) else
              [call, call + "-FM", call + " (FM)", call + "-LP"])
    for title in titles:
        params = {"action": "parse", "page": title, "prop": "wikitext",
                  "section": 0, "redirects": 1, "format": "json"}
        try:
            parsed = _json(get, WIKI_API, params).get("parse", {})
        except Exception:
            continue
        text = parsed.get("wikitext", {}).get("*", "")
        if not text or re.search(r"\{\{\s*Infobox (?:television|TV)", text, re.I):
            continue
        fields = dict(re.findall(r"^[ \t]*\|[ \t]*(\w+)[ \t]*=[ \t]*(.*?)[ \t]*$",
                                 text, re.M))
        freqs = " ".join([fields.get(k, "") for k in
                          ("frequency", "translators", "translator", "repeater")]
                         + re.findall(r"\{\{\s*Radio Relay\s*\|\s*([\d.]+)",
                                      text, re.I))
        if freq_mhz:
            nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", freqs)]
            if not any(abs(n - float(freq_mhz)) < 0.05 for n in nums):
                continue
        return parsed.get("title") or title, text
    return None, ""


_CALL_TITLE = re.compile(r"^([KW][A-Z]{2,3}|[KW]\d{3}[A-Z]{2})"
                         r"(?:-(?:FM|LP|LPFM))?(?: \((?:FM|AM)\))?$")


def wiki_search_calls(text, get=http_get_bytes, limit=10):
    """Calls of the radio station pages Wikipedia's search finds for a
    slogan or name ("Q102 Cincinnati" -> ["WKRQ"])."""
    res = _json(get, WIKI_API, {"action": "query", "list": "search",
                                "srsearch": text, "srlimit": limit,
                                "format": "json"})
    out = []
    for hit in (res.get("query") or {}).get("search") or []:
        m = _CALL_TITLE.match(hit.get("title") or "")
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def site_logo_candidates(html, base):
    """Logo URLs on a station website, best first: the page's share image,
    then its app icon, then any other icon."""
    found = {}
    for tag in re.findall(r"<(?:meta|link)\b[^>]*>", html, re.I):
        attr = dict((k.lower(), v) for k, v in
                    re.findall(r'([\w:-]+)\s*=\s*["\']([^"\']*)["\']', tag))
        rel = attr.get("rel", "").lower()
        prop = (attr.get("property") or attr.get("name") or "").lower()
        if prop in ("og:image", "og:image:url", "twitter:image") and attr.get("content"):
            found.setdefault(0, []).append(attr["content"])
        elif "apple-touch-icon" in rel and attr.get("href"):
            found.setdefault(1, []).append(attr["href"])
        elif "icon" in rel.split() and attr.get("href"):
            found.setdefault(2, []).append(attr["href"])
    out = []
    for rank in sorted(found):
        for url in found[rank]:
            url = urllib.parse.urljoin(base, url.strip())
            if url.startswith("http") and url not in out:
                out.append(url)
    return out


def _save(data, folder, name):
    ext = image_ext(data or b"")
    if not ext or len(data) < 200:
        return None
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ext)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return Path(path)


def download_site_logo(name, site, folder, get=http_get_bytes):
    """Save NAME.<ext> (e.g. WBUL-HD2.png) from the channel's own website,
    a stand-in until the station's logo decodes over HD. Path or None."""
    try:
        html = get(site)[0].decode("utf-8", "ignore")
    except Exception:
        return None
    for url in site_logo_candidates(html, site):
        try:
            path = _save(get(url)[0], folder, name)
        except Exception:
            continue
        if path:
            return path
    return None


def wiki_branding(callsign, freq_mhz=None, get=http_get_bytes):
    """The "Branding" line of the station's Wikipedia infobox, or None.
    Skips pages whose infobox frequency is a different one (same call on
    another station), but an FM translator listed for an AM counts.
    For an HD subchannel (WKQX-HD2) it is the analog page's Subchannels
    entry instead."""
    call = callsign.upper()
    hd = re.search(r"-HD([2-8])$", call)
    if hd:
        call = call[:hd.start()]
    titles = ([call] if call.endswith(("-FM", "-LP")) else
              [call, call + "-FM", call + " (FM)", call + "-LP"])
    for title in titles:
        params = {"action": "parse", "page": title, "prop": "wikitext",
                  "section": 0, "redirects": 1, "format": "json"}
        for wait in (2, 0):
            try:
                text = _json(get, WIKI_API, params).get(
                    "parse", {}).get("wikitext", {}).get("*", "")
                break
            except Exception as e:
                text = ""
                if getattr(e, "code", None) != 429 or not wait:
                    break
                time.sleep(wait)  # Wikipedia busy: one more try
        fields = dict(re.findall(r"^[ \t]*\|[ \t]*(\w+)[ \t]*=[ \t]*(.*?)[ \t]*$", text,
                                 re.M))
        brand = _wiki_clean(fields.get("branding", ""))
        if hd:
            brand = wiki_subchannel(text, hd.group(1))
        if not brand or re.search(r"\{\{\s*Infobox (?:television|TV)", text,
                                  re.I):
            continue  # no branding, or the call's TV station
        # An FM translator of an AM counts: {{Radio Relay|106.7|W294CO|..}}
        freqs = " ".join([fields.get(k, "") for k in
                          ("frequency", "translators", "translator",
                           "repeater")]
                         + re.findall(r"\{\{\s*Radio Relay\s*\|\s*([\d.]+)",
                                      text, re.I))
        if freq_mhz:  # no matching FM frequency on the page: not this one
            nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", freqs)]
            if not any(abs(n - float(freq_mhz)) < 0.05 for n in nums):
                continue
        return brand[:80]
    return None
