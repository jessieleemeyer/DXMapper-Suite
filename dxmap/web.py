"""Local web UI + JSON API (default http://127.0.0.1:7380/)."""

import base64
import csv
import io
import itertools
import json
import mimetypes
import queue
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ThreadingHTTPServer.request_queue_size = 64  # many map tiles arrive at once
from pathlib import Path

from . import logos
from . import excel, geo, importers, streams
from .store import LOGBOOK_COLUMNS, Store, hd_sub as store_hd_sub
from . import rtclean, wtfda

VIEW_ID = 10_000_000  # ids from the k-th view-only log are shifted by k*VIEW_ID
_LOG_NAME = re.compile(r"^\w[\w .'&()-]{0,39}$")
_NO_FILL = {"id", "logs", "distance_mi", "compass", "hd_family", "fed_by",
            "translators", "log", "view_only", "also_in", "times_logged",
            "first_heard", "last_heard"}
_NO_SHIFT = {"fcc", "wiki", "wtfda", "extra", "stream", "sources", "details",
             "logbook"}


def _match_key(d):
    """Same station in two logs: same call letters (or PI) on the same MHz."""
    call = re.sub(r"-FM$", "", (d.get("callsign") or "").strip().upper())
    f = d.get("freq_mhz")
    if f is None:
        f = d.get("station_freq")
    try:
        f = round(float(f), 1)
    except (TypeError, ValueError):
        f = None
    return (call, f) if call else ("PI:" + (d.get("pi") or "").upper(), f)


def _blank(v):
    return v is None or v == "" or v == [] or v == {} or v in ("{}", "[]", "null")


def _fill(mine, other):
    """Fill mine's blanks from the same station in a view-only log."""
    for k, v in other.items():
        if k not in _NO_FILL and not _blank(v) and _blank(mine.get(k)):
            mine[k] = v
    a, b = mine.get("times_logged"), other.get("times_logged")
    if isinstance(a, int) and isinstance(b, int):
        mine["times_logged"] = a + b
    for k, pick in (("first_heard", min), ("last_heard", max)):
        a, b = mine.get(k), other.get(k)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            mine[k] = pick(a, b)
        elif b is not None and a is None:
            mine[k] = b


def _offset_ids(d, off):
    if isinstance(d, list):
        return [_offset_ids(x, off) for x in d]
    if not isinstance(d, dict):
        return d
    return {k: (v + off if k in ("id", "station_id") and isinstance(v, int)
                else v if k in _NO_SHIFT else _offset_ids(v, off))
            for k, v in d.items()}

STATIC_DIR = Path(__file__).resolve().parent / "static"
WEB_HOST = "127.0.0.1"
WEB_PORT = 7380
TUNER_COMMAND = ("127.0.0.1", 7374)  # FM Tuner command channel
GEO_WORKERS = 4  # FCC lookups at once (city lookups stay 1/s)
TILE_SOURCES = {
    "normal": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "terrain": "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
    "hillshade": "https://server.arcgisonline.com/ArcGIS/rest/services/"
                 "Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}",
    "satellite": "https://server.arcgisonline.com/ArcGIS/rest/services/"
                 "World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "labels": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/"
              "World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
}
# Downloads at once per map layer, so busy servers don't refuse.
_tile_slots = {k: threading.BoundedSemaphore(8) for k in TILE_SOURCES}
# Cache folder per layer; Terrain moved from OpenTopoMap (which often left
# holes) to Esri, so its old tiles in TileCache/terrain are deleted.
TILE_FOLDERS = {"terrain": "terrain-esri"}
OLD_TILE_FOLDERS = ("terrain",)
DEFAULT_HOME = (39.8283, -98.5795)  # middle of the US until a home is set
# The free Lite build sets LITE = True: no import, Excel, stream/website
# lookups, Update all or Log Book column chooser.
LITE = False
CONDITIONS_SITES = {
    "tropo": "https://www.dxinfocentre.com/tropo.html",
    "dxmaps": "https://www.dxmaps.com/spots/mapg.php",
    "propquest": "https://www.propquest.co.uk/",
}
FULL_ONLY = ("/api/export.xlsx", "/api/listen", "/api/streams/missing",
             "/api/monitor/sync", "/api/import/preview", "/api/import/commit")
LITE_HIDDEN_COLUMNS = ("stream", "website")
LOGBOOK_VIEWS = ("classic", "modern")
WIKI_SUMMARY_DAYS = 30  # Wikipedia summaries are rechecked monthly
NEIGHBOR_DAYS = 30  # sister / same market lookups are rechecked monthly
DEFAULT_LOCAL_MI = 40      # Local: in regularly
DEFAULT_EXTENDED_MI = 80   # Extended local: sometimes; farther is DX
OLD_DEFAULT_LOCAL_MI = 100
MAP_LAYERS = ("normal", "terrain", "satellite")


MASTER_BACKUP = "DX Master Backup"
DAILY_KEEP = 30

NOW_TIMEOUT = 45  # s without an update before Now hearing disappears

def station_line(pi, call, loc, lat, lon, slogan):
    """One RDS Monitor stations.txt line: PI CALL | City, ST | lat lon | Slogan."""
    coords = "" if lat is None or lon is None else f"{lat:.5f} {lon:.5f}"
    if not (loc or coords or slogan):
        return f"{pi} {call}"
    return (f"{pi:<4} {call:<6} | {loc or ''} | {coords}"
            + (f" | {slogan}" if slogan else ""))


LOGO_CALL = r"[A-Z0-9]{3,8}(?:-HD[2-8])?"


def _hd_simulcast(name, base):
    """The AM station an HD channel's Wikipedia name says it carries,
    e.g. "Simulcast of WTCO 1450 AM" -> WTCO; None otherwise."""
    m = re.search(r"\b([KW][A-Z]{2,3})(?:-AM\b|\s*\(AM\)|\s+AM\b|\s+1?\d{3}\b)", name)
    if not m and re.search(r"simulcast", name, re.I):
        m = re.search(r"\b([KW][A-Z]{2,3})\b", name)
    call = m and m.group(1)
    return call if call and call != base else None

class App:
    """Everything the HTTP handlers and the feed share."""

    def __init__(self, store, settings_path, monitor_dir=None, fetch=None):
        self.store = store
        self.settings_path = Path(settings_path)
        self.monitor_dir = Path(monitor_dir) if monitor_dir else None
        self.fetch = fetch
        self.backup = {"last": None, "error": None}
        self._backup_rev = None
        self._backup_lock = threading.Lock()
        self.feed = None
        self.excel = None
        self.tuner_addr = TUNER_COMMAND
        self.started_at = time.time()
        self.now = None
        self.tuned_pick = None  # (MHz, station id) picked in Now hearing
        self._tuned_checked = (0.0, None)
        self._last_home = None
        self.settings = self._load_settings()
        self.main_db = Path(store.path)
        self.main_excel = None
        self.log_name = self.main_name
        self.views = []      # [(name, Store)] shown with self.store, view only
        self._spare = {}
        self._logs_lock = threading.RLock()
        excel.SIGNAL_UNITS[0] = self.settings.get("signal_units", "s")
        store.local_mi = float(self.settings["local_radius_mi"])
        store.ext_mi = float(self.settings["extended_radius_mi"])
        self._imports = {}
        self._geo_q = queue.PriorityQueue()
        self._wtfda_q = queue.Queue()
        self._wtfda_queued = set()
        self._wtfda_thread = None
        self._wtfda_lock = threading.Lock()
        self._geo_seq = itertools.count()
        self.last_live = None
        self._summary_lock = threading.Lock()
        self.focus = None
        self.map_pick = None  # station last picked on the map
        self.card_request = None  # Open station card -> the Log Book
        self.logbook_seen = 0.0
        self._geo_pending = set()
        self._geo_lock = threading.Lock()
        self._geo_threads = []
        self.tile_ports = []
        self._geocache_lock = threading.Lock()
        self._geocache = None
        self.import_job = None
        self.cleanup_job = None
        self._cleanup_lock = threading.Lock()
        self._import_lock = threading.Lock()
        self.tile_fetch = None
        self.find_stream = streams.find_stream
        self._stream_q = queue.Queue()
        self._stream_pending = set()
        self._stream_thread = None

    # -- settings ---------------------------------------------------------
    def _monitor_location(self):
        """(lat, lon, label) of the RDS Monitor's active location: Home, or
        Away when its Tools menu is switched to Away. Its default home when
        never set; None when no RDS Monitor folder was found."""
        if not self.monitor_dir:
            return None
        for path in (self.monitor_dir / "settings" / "monitor_settings.json",
                     self.monitor_dir / "monitor_settings.json"):
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            try:
                if (data.get("active_location") == "away"
                        and data.get("away_lat") is not None):
                    name = data.get("away_name") or "Away"
                    label = name if name.lower().startswith("away") else f"Away: {name}"
                    return float(data["away_lat"]), float(data["away_lon"]), label
                return float(data["home_lat"]), float(data["home_lon"]), "Home"
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return DEFAULT_HOME + ("Home",)

    def receive_locations(self):
        """{"home": (lat, lon, "Home"), "away": (lat, lon, label) or None}
        for the import dialog's "Logged from" choice."""
        home = (self.settings["home_lat"], self.settings["home_lon"], "Home")
        away = None
        for path in ((self.monitor_dir / "settings" / "monitor_settings.json",
                      self.monitor_dir / "monitor_settings.json")
                     if self.monitor_dir else ()):
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            try:
                home = (float(data["home_lat"]), float(data["home_lon"]), "Home")
            except (KeyError, TypeError, ValueError):
                home = DEFAULT_HOME + ("Home",)
            try:
                name = str(data.get("away_name") or "Away")
                away = (float(data["away_lat"]), float(data["away_lon"]),
                        name if name.lower().startswith("away") else f"Away: {name}")
            except (KeyError, TypeError, ValueError):
                pass
            break
        else:
            if self.monitor_dir:
                home = DEFAULT_HOME + ("Home",)
        return {"home": home, "away": away}

    def _monitor_home(self):
        loc = self._monitor_location()
        return loc[:2] if loc else None

    @property
    def location_label(self):
        loc = self._monitor_location()
        return loc[2] if loc else "Home"

    def _load_settings(self):
        try:
            data = json.loads(self.settings_path.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        if "home_lat" not in data:
            home = self._monitor_home() or DEFAULT_HOME
            data["home_lat"], data["home_lon"] = home
        data.setdefault("map_layer", "normal")
        if ("extended_radius_mi" not in data
                and data.get("local_radius_mi") == OLD_DEFAULT_LOCAL_MI):
            data.pop("local_radius_mi")  # the old default, not a choice
        data.setdefault("local_radius_mi", DEFAULT_LOCAL_MI)
        data.setdefault("extended_radius_mi", max(
            DEFAULT_EXTENDED_MI, float(data["local_radius_mi"])))
        data.setdefault("logbook_hidden", [])  # Log Book columns turned off
        data.setdefault("logbook_view", "classic")  # or "modern" (cards)
        data.setdefault("backup_dir", "")
        data.setdefault("daily_backups", False)
        return data

    def save_settings(self, changes):
        if self.monitor_dir and ("home_lat" in changes or "home_lon" in changes):
            raise ValueError("Home follows the RDS Monitor's home location - "
                             "change it there (Tools > Set Home Location)")
        if "local_radius_mi" in changes or "extended_radius_mi" in changes:
            mi = float(changes.get("local_radius_mi",
                                   self.settings["local_radius_mi"]))
            ext = float(changes.get("extended_radius_mi",
                                    self.settings["extended_radius_mi"]))
            if not 1 <= mi <= 5000:
                raise ValueError("Local radius must be 1-5000 miles")
            if not mi <= ext <= 5000:
                raise ValueError("Extended local must be from the Local "
                                 "radius up to 5000 miles")
            self.settings["local_radius_mi"] = mi
            self.settings["extended_radius_mi"] = ext
            self.store.local_mi, self.store.ext_mi = mi, ext
        if "logbook_view" in changes:
            if changes["logbook_view"] not in LOGBOOK_VIEWS:
                raise ValueError("Log Book view must be Classic or Modern")
            self.settings["logbook_view"] = changes["logbook_view"]
        if "logbook_hidden" in changes:
            keys = {c[0] for c in LOGBOOK_COLUMNS}
            hidden = [k for k in changes["logbook_hidden"] or [] if k in keys]
            if len(hidden) == len(keys):
                raise ValueError("Keep at least one Log Book column")
            self.settings["logbook_hidden"] = hidden
        for key in ("home_lat", "home_lon"):
            if key in changes:
                value = float(changes[key])
                limit = 90 if key == "home_lat" else 180
                if not -limit <= value <= limit:
                    raise ValueError(f"{key} out of range")
                self.settings[key] = value
        backup_changed = False
        if "backup_dir" in changes:
            self.settings["backup_dir"] = str(changes["backup_dir"] or "").strip()
            backup_changed = True
        if "daily_backups" in changes:
            self.settings["daily_backups"] = bool(changes["daily_backups"])
            backup_changed = True
        if changes.get("map_layer") in MAP_LAYERS:
            self.settings["map_layer"] = changes["map_layer"]
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings, indent=2), "utf-8")
        tmp.replace(self.settings_path)
        if self.excel and ("home_lat" in changes or "home_lon" in changes):
            self.excel.schedule()
        if backup_changed:
            self.backup_now(force=True)

    def backup_dirs(self):
        own = self._log_folder()
        base = self.backup_root()
        dirs = [base / own if own else base]
        extra = self.settings.get("backup_dir")
        if extra and (Path(extra) / own if own else Path(extra)) != dirs[0]:
            dirs.append(Path(extra) / own if own else Path(extra))
        return dirs

    def _copy_db(self, dest):
        tmp = dest.with_name(dest.name + ".tmp")
        out = sqlite3.connect(tmp)
        try:
            with self.store._lock:
                self.store.db.backup(out)
        finally:
            out.close()
        os.replace(tmp, dest)

    def restore_backup(self, body):
        """Bring back the whole log from a master backup (a folder holding
        DX Master Backup.db, or the .db file). The log being replaced is
        saved first as Backups/Before restore <date>.db."""
        where = str(body.get("path") or "").strip().strip('"')
        if not where:
            raise ValueError("choose the folder that holds the backup")
        src = Path(where)
        if src.is_dir():
            src = src / (MASTER_BACKUP + ".db")
        if not src.is_file():
            raise ValueError(f"no {MASTER_BACKUP}.db found in {where}")
        keep = self.backup_dirs()[0]
        keep.mkdir(parents=True, exist_ok=True)
        self._copy_db(keep / time.strftime("Before restore %Y-%m-%d %H%M%S.db"))
        try:
            self.store.restore(src)
        except sqlite3.DatabaseError as e:
            raise ValueError(f"{src} is not a DX Mapper log ({e})") from e
        self.backup_now(force=True)
        if self.excel:
            self.excel.schedule()
        stations = self.store.stations(self.home)
        return {"stations": len(stations), "logs": len(self.store.all_logs())}

    def backup_now(self, force=False):
        """Master backup of the whole log - every station and every log,
        never trimmed - in settings/Backups and the backup folder (e.g.
        Google Drive) when set. Optional dated daily copies (last 30)."""
        with self._backup_lock:
            rev = self.store.rev
            if not force and rev == self._backup_rev:
                return True
            errors = []
            sheets = excel.build_sheets(self.store, self.home)
            for folder in self.backup_dirs():
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                    self._copy_db(folder / (MASTER_BACKUP + ".db"))
                    excel.write_xlsx(str(folder / (MASTER_BACKUP + ".xlsx")), sheets)
                except PermissionError:
                    errors.append(f"{folder}: file is open - will retry")
                except Exception as e:
                    errors.append(f"{folder}: {e}")
            if self.settings.get("daily_backups"):
                daily = self.backup_dirs()[0] / "Daily"
                try:
                    daily.mkdir(parents=True, exist_ok=True)
                    self._copy_db(daily / time.strftime("DX Backup %Y-%m-%d.db"))
                    for old in sorted(daily.glob("DX Backup *.db"))[:-DAILY_KEEP]:
                        old.unlink()
                except Exception as e:
                    errors.append(f"daily copy: {e}")
            self.backup["error"] = "; ".join(errors) or None
            if not errors:
                self._backup_rev = rev
                self.backup["last"] = time.time()
            return not errors

    @property
    def home(self):
        home = self._monitor_home() or (self.settings["home_lat"],
                                        self.settings["home_lon"])
        self.store.check_origin = self.receive_locations()["home"][:2]
        if home != self._last_home:
            if self._last_home is not None and self.excel:
                self.excel.schedule()
            self._last_home = home
        return home

    # -- logging ----------------------------------------------------------
    LOGO_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
                       ".bmp")

    def logo_path(self, callsign):
        """The RDS Monitor's logo file for a call (settings/logos/CALL.png
        etc., the files the monitor downloads and shows), or None. A
        translator shows the logo of the station it relays."""
        call = (callsign or "").strip().upper()
        parent = self._logo_parent(call)
        for c in ((parent, re.sub(r"-HD[2-8]$", "", parent)) if parent else ()):
            f = self._logo_file(c)
            if f:
                return f
        return self._logo_file(call)

    def _logo_parent(self, call):
        """For a translator's call letters: the station (or HD channel) it
        relays, e.g. W293BK -> WTCO; for an HD channel that simulcasts an
        AM station (Wikipedia's subchannel name): that AM; else None."""
        cache = getattr(self, "_logo_parents", None)
        if not cache or time.time() - cache[0] > 5:
            parents = {}
            rows, wiki = self.store._relatives()
            for d in rows:
                if not d["parent"]:
                    if d["hd"]:  # an HD channel simulcasting an AM station
                        names = (((wiki.get(d["base"]) or {}).get("wiki") or {})
                                 .get("names") or (d["wiki"] or {}).get("names") or {})
                        am = _hd_simulcast(names.get(d["hd"]) or "", d["base"])
                        if am:
                            parents.setdefault(d["callsign"].strip().upper(), am)
                    continue
                base, hd = d["parent"]
                for c in (d["callsign"], (d["fcc"] or {}).get("callsign")):
                    c = re.sub(r"-FM$", "", (c or "").strip().upper())
                    if c and re.sub(r"-HD[2-8]$", "", c) != base:
                        parents.setdefault(c, f"{base}-{hd}" if hd else base)
            cache = self._logo_parents = (time.time(), parents)
        return cache[1].get((call or "").strip().upper())

    def _logo_file(self, call):
        if not self.monitor_dir or not re.fullmatch(LOGO_CALL, call):
            return None
        folder = self.monitor_dir / "settings" / "logos"
        try:
            files = list(folder.iterdir())
        except OSError:
            return None
        found = [f for f in files if f.stem.upper() == call
                 and f.suffix.lower() in self.LOGO_EXTENSIONS and f.is_file()]
        found.sort(key=lambda f: self.LOGO_EXTENSIONS.index(f.suffix.lower()))
        for f in found:
            try:
                if f.stat().st_size > 0:
                    return f
            except OSError:
                continue
        return None

    def download_logo(self, callsign):
        """Download a logo for a call into the RDS Monitor's logos folder
        (kept when one is already there). The file Path, or None."""
        call = (callsign or "").strip().upper()
        parent = self._logo_parent(call)
        if parent:  # a translator: its parent station's logo
            call = parent if "-HD" not in parent or self._hd_site(parent) \
                else re.sub(r"-HD[2-8]$", "", parent)
        if not self.monitor_dir or not re.fullmatch(LOGO_CALL, call):
            return None
        found = self.logo_path(call)
        if found:  # only a logo decoded over HD replaces one (RDS Monitor)
            return found
        folder = self.monitor_dir / "settings" / "logos"
        if "-HD" in call:
            site = self._hd_site(call)
            return site and logos.download_site_logo(call, site, folder)
        return logos.download(call, folder)

    def _logo_call(self, callsign):
        call = (callsign or "").strip().upper()
        parent = self._logo_parent(call)
        if parent:
            call = parent if "-HD" not in parent or self._hd_site(parent) \
                else re.sub(r"-HD[2-8]$", "", parent)
        return call if re.fullmatch(LOGO_CALL, call) else None

    def logo_choices(self, callsign):
        """{"call", "choices": [{"url", "source"}]} for the logo picker."""
        call = self._logo_call(callsign)
        if not call:
            return {"call": callsign, "choices": []}
        return {"call": call, "choices": logos.all_candidates(call)}

    def picked_logos(self):
        """Calls whose logo was chosen in the logo picker (settings/logos/
        picked.txt); nothing automatic replaces those."""
        try:
            return set((self.monitor_dir / "settings" / "logos" / "picked.txt")
                       .read_text().split())
        except (OSError, TypeError):
            return set()

    def pick_logo(self, callsign, url=None, data=None):
        """Save the logo picked (an image URL) or uploaded (bytes) as the
        station's logo, replacing the old one. The Path."""
        call = self._logo_call(callsign)
        if not self.monitor_dir or not call:
            raise ValueError("RDS Monitor folder not found")
        if url:
            got = logos.fetch_image(url)
        else:
            ext = logos.image_ext(data or b"")
            got = (data, ext) if ext else None
        if not got:
            raise ValueError("That isn't a picture DX Mapper can use "
                             "(PNG, JPG, GIF, WEBP or BMP)")
        folder = self.monitor_dir / "settings" / "logos"
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.iterdir():
            if old.stem.upper() == call and old.suffix.lower() in self.LOGO_EXTENSIONS:
                old.unlink()
        path = folder / (call + got[1])
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(got[0])
        os.replace(tmp, path)
        picked = self.picked_logos() | {call}
        (folder / "picked.txt").write_text("\n".join(sorted(picked)) + "\n")
        return path

    def _hd_site(self, call):
        """The HD channel's own website from its station's Wikipedia page."""
        base, _, hd = call.upper().partition("-")
        with self.store._lock:
            rows = self.store.db.execute(
                "SELECT wiki FROM stations WHERE upper(callsign)=? "
                "AND wiki IS NOT NULL", (base,)).fetchall()
        for r in rows:
            try:
                site = (json.loads(r[0]).get("sites") or {}).get(hd)
            except (ValueError, AttributeError):
                continue
            if site:
                return site
        return None

    WIKI_RECHECK_S = 30 * 86400
    wtfda_fetch = None  # tests: callsign -> results page

    def queue_wtfda(self, sid):
        """Look a station up on db.wtfda.org once, in the background, one
        request at a time 30 s apart (the site's robots.txt)."""
        if self.fetch is not None and self.wtfda_fetch is None:
            return  # tests with made-up lookups: never the real site
        with self._wtfda_lock:
            if sid in self._wtfda_queued:
                return
            self._wtfda_queued.add(sid)
            self._wtfda_q.put(sid)
            if not (self._wtfda_thread and self._wtfda_thread.is_alive()):
                self._wtfda_thread = threading.Thread(
                    target=self._wtfda_worker, daemon=True, name="wtfda")
                self._wtfda_thread.start()

    def _wtfda_worker(self):
        last = 0
        while True:
            try:
                sid = self._wtfda_q.get(timeout=60)
            except queue.Empty:
                with self._wtfda_lock:
                    if self._wtfda_q.empty():
                        self._wtfda_thread = None
                        return
                continue
            store = self.store
            st = store.station(sid)
            if not st or st.get("wtfda") is not None or not (
                    st.get("callsign") or st.get("pi")):
                continue
            time.sleep(max(0, last + wtfda.GAP_S - time.time())
                       if self.wtfda_fetch is None else 0)
            last = time.time()
            try:
                info = wtfda.lookup(st.get("callsign"), st.get("freq_mhz"),
                                    pi=st.get("pi"),
                                    **({"fetch": self.wtfda_fetch}
                                       if self.wtfda_fetch else {}))
            except Exception:
                with self._wtfda_lock:
                    self._wtfda_queued.discard(sid)  # offline: next start
                continue
            if store is not self.store:
                continue  # switched logs while looking it up
            store.set_wtfda(sid, info)
            if not st.get("callsign") and info.get("callsign"):
                self.queue_geo(sid)  # now it has call letters: FCC site
            call = st.get("callsign") or info.get("callsign")
            if call and info.get("slogan") and not st.get("slogan"):
                self.monitor_set_call(st.get("pi") or info.get("pi"),
                                      re.sub(r"-FM$", "", call.upper()),
                                      fill={"slogan": info["slogan"]})

    def queue_missing_wtfda(self):
        for sid in self.store.needs_wtfda():
            self.queue_wtfda(sid)

    def find_hd_info(self, sid, st, force=False):
        """HD channel names and websites and the translators from the
        station's Wikipedia page, rechecked monthly."""
        call = st.get("callsign") or ""
        wiki = st.get("wiki") or {}
        if not call or re.search(r"-HD[2-8]$", call, re.I) or (
                not force and time.time() - (wiki.get("checked") or 0)
                < self.WIKI_RECHECK_S):
            return
        get = ((lambda u: (self.fetch(u).encode("utf-8"), ""))
               if self.fetch else geo.http_get_bytes)
        text = logos.wiki_page(call, st.get("freq_mhz"), get)
        info = logos.wiki_hd_info(text) if text else {}
        freq = st.get("freq_mhz")
        if freq and any(abs(r["freq_mhz"] - freq) < 0.05
                        for r in info.get("relays") or []):
            info["translator"] = True  # this entry is one of its translators
        self.store.set_wiki(sid, info)

    def wiki_summary(self, sid):
        """The opening paragraph of the station's Wikipedia article for the
        Log Book side panel, {"text", "url"} or None; cached a month in
        settings/wiki_summaries.json."""
        st = self.view_station(sid)
        call = (st or {}).get("callsign") or ""
        if not call:
            return None
        key = f"{re.sub(r'-HD[1-8]$', '', call.upper())} {st.get('freq_mhz') or ''}"
        path = self.settings_path.parent / "wiki_summaries.json"
        with self._summary_lock:
            try:
                cache = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                cache = {}
            hit = cache.get(key)
            if hit and time.time() - hit.get("checked", 0) < WIKI_SUMMARY_DAYS * 86400:
                return hit.get("summary")
        get = ((lambda u: (self.fetch(u).encode("utf-8"), ""))
               if self.fetch else geo.http_get_bytes)
        summary = logos.wiki_summary(call, st.get("freq_mhz"), get)
        with self._summary_lock:
            try:
                cache = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                cache = {}
            cache[key] = {"summary": summary, "checked": time.time()}
            try:
                path.write_text(json.dumps(cache, indent=1), "utf-8")
            except OSError:
                pass
        return summary

    def neighbors(self, sid):
        """{"sisters", "market"} for Station Details (see Store.neighbors);
        Wikipedia's Sister stations and the FCC's list for the city are
        cached a month in settings/neighbors.json. "error" says what
        couldn't be reached; your logged stations are still listed."""
        st = self.view_station(sid)
        if not st:
            return {"sisters": [], "market": []}
        call = re.sub(r"-HD[1-8]$", "", (st.get("callsign") or "").upper())
        fcc = st.get("fcc") or {}
        book = next((r for r in self.store.logbook(self.home) if r["id"] == st["id"]), {}) \
            if not st.get("view_only") else {}
        city = book.get("city") or fcc.get("city") or (st.get("location") or "").partition(",")[0].strip()
        state = book.get("state") or fcc.get("state") or (st.get("location") or "").partition(",")[2].strip()
        key = f"{call} {st.get('freq_mhz') or ''} {city}, {state}"
        path = self.settings_path.parent / "neighbors.json"
        with self._summary_lock:
            try:
                cache = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                cache = {}
        hit = cache.get(key) or {}
        errors = []
        if time.time() - hit.get("checked", 0) >= NEIGHBOR_DAYS * 86400:
            got = {"checked": time.time()}
            get = ((lambda u: (self.fetch(u).encode("utf-8"), ""))
                   if self.fetch else geo.http_get_bytes)
            try:
                got["sisters"] = logos.wiki_sisters(logos.wiki_page(call, st.get("freq_mhz"), get)) if call else []
            except Exception:
                errors.append("Wikipedia")
            try:
                got["market"] = geo.fcc_city(city, state, fetch=self.fetch)
            except Exception:
                errors.append("the FCC")
            if not errors:
                hit = got
                with self._summary_lock:
                    try:
                        cache = json.loads(path.read_text("utf-8"))
                    except (OSError, ValueError):
                        cache = {}
                    cache[key] = hit
                    try:
                        path.write_text(json.dumps(cache, indent=1), "utf-8")
                    except OSError:
                        pass
            else:
                hit = {**hit, **got}
        out = self.store.neighbors(
            {"callsign": call, "owner": book.get("owner") or fcc.get("licensee"),
             "city": city, "state": state},
            hit.get("sisters") or [], hit.get("market") or [])
        if errors:
            out["error"] = "Couldn't reach " + " or ".join(errors)
        return out

    def monitor_set_call(self, pi, callsign, slogan=None, fill=None):
        """Put PI -> callsign in the RDS Monitor's stations.txt (the line its
        own Set Callsign writes), keeping that line's city/coordinates and
        every other line. The running monitor sees the file change and
        relabels the station. slogan (str) replaces the line's slogan ("" to
        remove it). fill={location, lat, lon, slogan} only fills empty parts
        and leaves a line whose call was set differently alone. True when
        the file was changed."""
        if store_hd_sub(callsign):
            return False  # the PI belongs to the analog station
        pi = (pi or "").strip().upper()
        callsign = (callsign or "").strip().upper()
        if (not self.monitor_dir or len(pi) != 4 or len(callsign) < 3
                or len(callsign.split()) != 1):
            return False
        path = self.monitor_dir / "settings" / "stations.txt"
        try:
            lines = path.read_text(encoding="utf-8-sig",
                                   errors="ignore").splitlines()
        except FileNotFoundError:
            lines = ["# PI CALLSIGN | City, ST | lat lon",
                     "# Edit freely - the program only appends new stations."]
        except OSError:
            return False
        fill = fill or {}
        if isinstance(slogan, str):
            slogan = slogan.replace("|", "/").strip()
        fill_slogan = (fill.get("slogan") or "").replace("|", "/").strip() or None
        out, found = [], False
        for line in lines:
            e = geo.parse_station_line(line)
            if not e or e["pi"] != pi:
                out.append(line)
                continue
            if found:
                continue  # a second line for this PI would fight the first
            found = True
            if fill and e["callsign"] != callsign:
                out.append(line)  # filling only: a call set in the monitor wins
                continue
            loc = e.get("location") or fill.get("location")
            lat, lon = e.get("lat"), e.get("lon")
            if lat is None or lon is None:
                lat, lon = fill.get("lat"), fill.get("lon")
            sl = (e.get("slogan") if slogan is None else slogan or None) or fill_slogan
            same = (e["callsign"], e.get("location"), e.get("lat"), e.get("lon"),
                    e.get("slogan")) == (callsign, loc, lat, lon, sl)
            out.append(line if same else
                       station_line(pi, callsign, loc, lat, lon, sl))
        if not found:
            out.append(station_line(pi, callsign, fill.get("location"),
                                    fill.get("lat"), fill.get("lon"),
                                    slogan or fill_slogan))
        if out == lines:
            return False
        text = "\n".join(out) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            try:
                os.replace(tmp, path)
            except OSError:  # Windows: the monitor has it open right now
                path.write_text(text, encoding="utf-8")
                tmp.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def monitor_slogan(self, pi, callsign):
        """The slogan on the RDS Monitor's stations.txt line for this station."""
        if not self.monitor_dir:
            return None
        by_pi, by_call = geo.load_monitor_stations(
            self.monitor_dir / "settings" / "stations.txt")
        call = (callsign or "").upper()
        e = by_pi.get((pi or "").upper())
        if not e or e["callsign"] != call:
            e = by_call.get(call)
        return (e or {}).get("slogan")

    sync_pause = 1.0  # seconds between Wikipedia lookups while filling

    def sync_monitor_file(self):
        """Update all, in the background: fill the RDS Monitor's stations.txt
        from the whole log (details), download missing logos, then look up
        live streams for every station without one. Without an RDS Monitor
        folder only the streams are looked up."""
        job = getattr(self, "monitor_sync", None)
        if job and job.get("running"):
            return True
        self.monitor_sync = {"running": True, "phase": "details", "done": 0,
                             "total": 0, "written": 0, "slogans": 0,
                             "monitor": bool(self.monitor_dir),
                             "started": time.time(), "errors": [],
                             "logos": {"done": 0, "total": 0, "found": 0},
                             "streams": {"done": 0, "total": 0, "found": 0,
                                         "failed": 0}}
        threading.Thread(target=self._sync_monitor_file, daemon=True,
                         name="monitor-sync").start()
        return True

    def _sync_monitor_file(self):
        """Streams run alongside details then logos (different servers);
        a step that breaks is reported and the others still run."""
        job = self.monitor_sync

        def guard(name, step, *args):
            try:
                step(*args)
            except Exception as e:
                job["errors"].append(f"{name}: {e}")

        try:
            st = threading.Thread(target=guard, daemon=True, name="update-streams",
                                  args=("Streams", self._find_missing_streams,
                                        job["streams"]))
            st.start()
            if self.monitor_dir:
                guard("Details", self._sync_details, job)
                job["details_finished"] = time.time()
                job["phase"] = "logos"
                guard("Logos", self._fetch_missing_logos, job["logos"])
            job["phase"] = "streams"
            st.join()
        finally:
            job["phase"] = "done"
            job["running"] = False
            job["finished"] = time.time()

    def _sync_details(self, job):
        with self.store._lock:
            ids = [r[0] for r in self.store.db.execute(
                "SELECT id FROM stations WHERE coalesce(pi,'') <> ''"
                " AND coalesce(callsign,'') <> '' ORDER BY id")]
        job["total"] = len(ids)
        for sid in ids:
            st = self.store.station(sid)
            if st and not st.get("slogan"):
                try:
                    if self.find_branding(sid, st):
                        time.sleep(self.sync_pause)
                except Exception:
                    pass
                st = self.store.station(sid)
            if st and self.monitor_set_call(
                    st.get("pi"), st["callsign"], fill={
                        "location": st.get("location"), "lat": st.get("lat"),
                        "lon": st.get("lon"), "slogan": st.get("slogan")}):
                job["written"] += 1
            if st and st.get("slogan"):
                job["slogans"] += 1
            job["done"] += 1

    def fetch_missing_logos(self):
        """Download a logo for every station that has none yet, in the
        background (Download missing logos). Logos already in the RDS
        Monitor's logos folder are kept. False without a monitor folder."""
        if not self.monitor_dir:
            return False
        job = getattr(self, "logo_job", None)
        if job and job.get("running"):
            return True
        self.logo_job = {"running": True, "done": 0, "total": 0, "found": 0}
        threading.Thread(target=self._fetch_missing_logos, daemon=True,
                         name="missing-logos").start()
        return True

    def _fetch_missing_logos(self, job=None):
        job = job or self.logo_job
        try:
            with self.store._lock:
                calls = [r[0] for r in self.store.db.execute(
                    "SELECT callsign FROM stations WHERE coalesce(callsign,'') <> ''")]
            # An HD channel tries its own website's logo (from Wikipedia);
            # without one it shows its analog station's logo.
            calls = {c.strip().upper() for c in calls}
            # A translator uses the logo of the station it relays.
            calls = {self._logo_parent(c) or c for c in calls}
            calls = sorted(calls
                           | {re.sub(r"-HD[2-8]$", "", c.strip().upper())
                              for c in calls})
            calls = [c for c in calls if re.fullmatch(LOGO_CALL, c)
                     and not self.logo_path(c)
                     and ("-HD" not in c or self._hd_site(c))]
            job["total"] = len(calls)
            for call in calls:
                try:
                    if self.download_logo(call):
                        job["found"] += 1
                except Exception:
                    pass
                job["done"] += 1
                time.sleep(self.sync_pause)
        finally:
            job["running"] = False
            job["finished"] = time.time()

    # -- live streams ---------------------------------------------------------
    def queue_stream(self, sid):
        """Look up a live-logged station's internet stream in the background,
        once (Find live streams retries the ones not found)."""
        if LITE:
            return
        with self._geo_lock:
            if sid in self._stream_pending:
                return
            if not self.store.needs_stream_lookup(sid):
                return
            self._stream_pending.add(sid)
            self._stream_q.put(sid)
            if not (self._stream_thread and self._stream_thread.is_alive()):
                self._stream_thread = threading.Thread(
                    target=self._stream_worker, daemon=True, name="streams")
                self._stream_thread.start()

    def _stream_worker(self):
        while True:
            try:
                sid = self._stream_q.get(timeout=30)
            except queue.Empty:
                return
            try:
                st = self.store.station(sid)
                if st and st.get("callsign"):
                    self.store.set_stream(sid, self.find_stream(
                        st["callsign"], st.get("freq_mhz")))
            except Exception:
                pass
            finally:
                with self._geo_lock:
                    self._stream_pending.discard(sid)

    def find_missing_streams(self):
        """Look up a stream for every station without one (Find live streams)."""
        job = getattr(self, "stream_job", None)
        if job and job.get("running"):
            return
        self.stream_job = {"running": True, "done": 0, "total": 0, "found": 0}
        threading.Thread(target=self._find_missing_streams, daemon=True,
                         name="missing-streams").start()

    def _find_missing_streams(self, job=None):
        job = job or self.stream_job
        try:
            todo = self.store.needs_stream(retry_after=0)
            job["total"] = len(todo)
            for sid, call, freq in todo:
                try:
                    found = self.find_stream(call, freq) or {}
                    self.store.set_stream(sid, found)
                    job["found"] += bool(found.get("url"))
                    job["websites"] = job.get("websites", 0) + bool(found.get("website"))
                except Exception as e:  # not saved, so it is tried again
                    job["failed"] = job.get("failed", 0) + 1
                    job["error"] = f"{call}: {e}"
                job["done"] += 1
                time.sleep(self.sync_pause)
        finally:
            job["running"] = False
            job["finished"] = time.time()

    def monitor_station(self, rec):
        """Fill coords from the monitor's stations.txt when the record has none."""
        if not self.monitor_dir or rec.get("lat") is not None:
            return rec
        by_pi, by_call = geo.load_monitor_stations(
            self.monitor_dir / "settings" / "stations.txt")
        call = (rec.get("callsign") or "").upper()
        e = by_call.get(call) or by_pi.get((rec.get("pi") or "").upper())
        if e and e.get("lat") is not None and (not call or e["callsign"] == call):
            rec = dict(rec, lat=e["lat"], lon=e["lon"],
                       coord_source="RDS Monitor stations.txt")
            rec.setdefault("location", e.get("location"))
        return rec

    def note_signal_units(self, msg):
        """Follow the FM Tuner's Signal units (sent along by the RDS Monitor)."""
        units = msg.get("signal_units")
        if units in ("s", "db") and units != self.settings.get("signal_units", "s"):
            self.settings["signal_units"] = excel.SIGNAL_UNITS[0] = units
            self.save_settings({})
            if self.excel:
                self.excel.schedule()

    def now_hearing(self, msg):
        """The station the RDS Monitor is decoding right now (not logged)."""
        if msg.get("type") == "tuned":
            return self.tuned_from_monitor(msg)
        self.note_signal_units(msg)
        if msg.get("clear"):
            self.now = None
            return
        rec = self.monitor_station(dict(msg))
        known = self.store.find_station(rec)
        if known:
            rec["station_id"] = known["id"]
            if rec.get("lat") is None and known.get("lat") is not None:
                rec.update(lat=known["lat"], lon=known["lon"])
                rec.setdefault("location", known.get("location"))
        rec["received"] = time.time()
        self.now = rec

    def log_now(self):
        """Log the station the RDS Monitor is hearing (FM Tuner remote's
        Log this station button)."""
        n = self.now
        if not n or time.time() - n["received"] > NOW_TIMEOUT:
            return {"ok": False, "error": "The RDS Monitor isn't hearing a station"}
        rec = {k: v for k, v in n.items()
               if k not in ("received", "station_id", "type")}
        rec["type"] = "log"
        rec["ts"] = time.time()
        sid, created = self.record(rec, source="remote")
        return {"ok": True, "station_id": sid, "new": created,
                "callsign": rec.get("callsign")}

    def _now_state(self):
        n = self.now
        if not n or time.time() - n["received"] > NOW_TIMEOUT:
            return None
        out = {k: n.get(k) for k in ("pi", "callsign", "freq_mhz", "ps", "rt",
                                     "location", "signal_dbfs", "rx_name",
                                     "station_id", "slogan", "now_playing")}
        out["age_s"] = round(time.time() - n["received"], 1)
        try:
            out["rx_lat"], out["rx_lon"] = float(n["rx_lat"]), float(n["rx_lon"])
        except (KeyError, TypeError, ValueError):
            out["rx_lat"], out["rx_lon"] = self.home
            out["rx_name"] = self.location_label
        try:
            out["lat"], out["lon"] = float(n["lat"]), float(n["lon"])
            mi, brg = geo.distance_bearing(out["rx_lat"], out["rx_lon"],
                                           out["lat"], out["lon"])
            out["distance_mi"], out["compass"] = round(mi, 1), geo.compass_point(brg)
        except (KeyError, TypeError, ValueError):
            out["lat"] = out["lon"] = None
        return out

    def record(self, rec, auto=False, source=None):
        rec = dict(rec)
        self.note_signal_units(rec)
        rec.pop("signal_units", None)
        if isinstance(rec.get("report"), str) and rec["report"].strip():
            # The RDS Monitor's report for the logged station: fill what the
            # record lacks and keep every report line with this log entry.
            parsed = importers.parse_report(rec["report"])
            for k, v in (parsed[0] if parsed else {}).items():
                if k not in ("extra", "heard_at") and rec.get(k) in (None, ""):
                    rec[k] = v
            rec["report_info"] = importers.parse_report_info(rec["report"])
        rec = self.monitor_station(rec)
        if rec.get("rx_lat") is None:
            rec["rx_lat"], rec["rx_lon"] = self.home
            rec["rx_name"] = self.location_label
        if rec.get("group_seq"):  # the RDS Monitor's group order fingerprint
            extra = dict(rec.get("extra") or {})
            extra["Group Sequence"] = str(rec["group_seq"])[:200]
            rec["extra"] = extra
        source = source or ("monitor auto" if auto else "monitor")
        hd = rec.pop("hd", None)
        programs = [p for p in (hd or {}).get("programs") or []
                    if isinstance(p, dict) and isinstance(p.get("program"), int)]
        if programs:
            extra = dict(rec.get("extra") or {})
            extra["HD"] = "1"  # HD1 is this analog station; HD2+ get their own
            rec["extra"] = extra
        into = None
        if auto and not self.store.find_station(rec):  # pressing Log always logs it
            other = self.store.elsewhere(rec)
            if other and not self._translator_here(rec, other):
                into = other["id"]  # its splatter, not a new station
        sid, created = self.store.log(rec, source=source, auto=auto, into=into)
        self._record_hd_programs(rec, hd, programs, source, auto)
        if self.settings.get("log_to_all"):  # off unless ticked in Logs
            for name, other in list(self.views):
                try:
                    other.log(rec, source=source, auto=auto)
                    self._record_hd_programs(rec, hd, programs, source, auto, other)
                except Exception as e:
                    print(f"Couldn't also log into {name}: {e}", flush=True)
        if not (auto and getattr(self.store, "last_merged", False)):
            self.last_live = {"id": sid, "at": time.time()}  # updates don't jump the map
        self.queue_geo(sid, urgent=True)
        self.queue_stream(sid)
        return sid, created

    def _translator_here(self, rec, station):
        """True when the FCC lists a translator on the caught frequency for
        the logged station; offline or unknown counts as splatter."""
        call = station.get("callsign")
        if not call:
            return False
        try:
            fcc = geo.fcc_heard(call, rec.get("freq_mhz"), self.home, fetch=self.fetch)
        except Exception:
            return False
        return bool(fcc and fcc.get("relays"))

    def _record_hd_programs(self, rec, hd, programs, source, auto, store=None):
        """A real HD decoder lock (sent by the RDS Monitor from the FM
        Tuner) logs HD2-HD8 as their own stations, like an imported
        WOVO-HD2; what is caught replaces the sheet's name."""
        base = re.sub(r"-HD\d$", "", rec.get("callsign") or "") or (
            (hd or {}).get("call") or "").split("-")[0].upper()
        if not base:
            return
        for p in programs:
            n = p["program"] + 1
            if n < 2 or n > 8:
                continue
            sub = {"callsign": f"{base}-HD{n}", "freq_mhz": rec.get("freq_mhz"),
                   "slogan": p.get("name") or None,
                   "pty_name": p.get("type") or None,
                   "artist": p.get("artist"), "title": p.get("title"),
                   "extra": {"HD": f"HD{n}"},
                   "heard_at": rec.get("heard_at")}
            for k in ("lat", "lon", "location", "rx_lat", "rx_lon", "rx_name"):
                if rec.get(k) is not None:
                    sub[k] = rec[k]
            sub_id, _ = (store or self.store).log(
                {k: v for k, v in sub.items() if v is not None},
                source=source, auto=auto)
            self.queue_geo(sub_id)
            self.queue_stream(sub_id)

    # -- background FCC lookups --------------------------------------------
    def queue_geo(self, sid, urgent=False):
        """Look up a station's location in the background; live logs
        (urgent) go ahead of imported stations."""
        with self._geo_lock:
            if sid in self._geo_pending and not urgent:
                return
            self._geo_pending.add(sid)
        self._geo_q.put((0 if urgent else 1, next(self._geo_seq), sid))
        self._geo_threads = [t for t in self._geo_threads if t.is_alive()]
        if len(self._geo_threads) < min(GEO_WORKERS, self._geo_q.qsize()):
            t = threading.Thread(target=self._geo_worker, daemon=True)
            self._geo_threads.append(t)
            t.start()

    def tune(self, freq):
        """Ask the FM Tuner (the only program that owns the radio) to tune
        to freq, using the same one-line command the RDS Monitor sends."""
        try:
            freq = round(float(freq), 2)
        except (TypeError, ValueError):
            raise ValueError("this station has no frequency to tune") from None
        reply = self._tuner_cmd(f"TUNE {freq:.2f}")
        if not reply.get("ok"):
            raise ValueError("FM Tuner: %s" % (reply.get("error")
                                               or "no answer to TUNE"))
        return {"ok": True,
                "frequency_mhz": reply.get("frequency_mhz", freq)}

    def _tuner_cmd(self, line, timeout=3):
        """One command line to the FM Tuner; its JSON reply ({} if unreadable)."""
        buf = b""
        try:
            with socket.create_connection(self.tuner_addr, timeout=timeout) as s:
                s.settimeout(timeout)
                s.sendall((line + "\n").encode())
                while b"\n" not in buf:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
        except OSError:
            raise ValueError("The FM Tuner isn't answering - start the "
                             "FM Tuner and try again") from None
        try:
            return json.loads(buf.split(b"\n", 1)[0] or b"{}")
        except ValueError:
            return {}

    def tuner_freq(self):
        """The FM Tuner's frequency (MHz) right now, RDS or not; else the
        RDS Monitor's station's; None when neither is running."""
        try:
            f = (self._tuner_cmd("STATUS").get("status") or {}).get("frequency_mhz")
            if f:
                return round(float(f), 1)
        except (ValueError, TypeError):
            pass
        n = self._now_state()
        return n and n.get("freq_mhz")

    def _tuner_now_freq(self):
        """The FM Tuner's frequency for Now hearing, asked at most every 3 s
        (None when the Tuner isn't running)."""
        now = time.time()
        if now - self._tuned_checked[0] < 3:
            return self._tuned_checked[1]
        try:
            f = (self._tuner_cmd("STATUS", timeout=0.5).get("status")
                 or {}).get("frequency_mhz")
            f = round(float(f), 1) if f else None
        except (ValueError, TypeError):
            f = None
        mon = getattr(self, "_monitor_freq", None)
        if f is None and mon and now - mon[0] < 60:
            f = mon[1]  # the Tuner on another PC: the RDS Monitor's word
        self._tuned_checked = (now, f)
        return f

    def tuned_from_monitor(self, msg):
        """The RDS Monitor's frequency (sent on each retune): switch at once
        and drop the last frequency's RDS station."""
        try:
            f = round(float(msg.get("freq_mhz")), 1)
        except (TypeError, ValueError):
            return
        now = time.time()
        self._monitor_freq = (now, f)
        self._tuned_checked = (now, f)
        n = self.now
        if n and n.get("freq_mhz") and abs(float(n["freq_mhz"]) - f) >= 0.05:
            self.now = None

    def _live_now(self):
        """Now hearing: the RDS Monitor's station, or - when the Tuner is on
        a frequency with no RDS - the logged station(s) on that frequency."""
        n = self._now_state()
        f = self._tuner_now_freq()
        if not f or (n and (not n.get("freq_mhz")
                            or abs(float(n["freq_mhz"]) - f) < 0.05)):
            return n
        return self._tuned_state(f)

    def _tuned_state(self, freq):
        """Now hearing with no RDS: the one logged station on the Tuner's
        frequency, or the one picked; several -> choices, never a guess."""
        on = self.store.on_frequency(freq, self.home)
        pick = self.tuned_pick
        st = None
        if pick and abs(pick[0] - freq) < 0.05:
            st = next((s for s in on if s["id"] == pick[1]), None)
        if st is None and len(on) == 1:
            st = on[0]
        out = dict.fromkeys(("pi", "callsign", "ps", "rt", "location",
                             "signal_dbfs", "station_id", "slogan",
                             "now_playing", "lat", "lon", "distance_mi",
                             "compass"))
        out.update(no_rds=True, freq_mhz=freq, age_s=0,
                   rx_lat=self.home[0], rx_lon=self.home[1],
                   rx_name=self.location_label,
                   choices=[{k: s.get(k) for k in ("id", "callsign", "location",
                                                   "distance_mi", "compass")}
                            for s in on])
        if st:
            out.update({k: st.get(k) for k in ("pi", "callsign", "location",
                                               "slogan", "lat", "lon",
                                               "distance_mi", "compass")},
                       station_id=st["id"])
        return out

    def pick_tuned(self, sid):
        """Which logged station you're hearing on the Tuner's frequency (no
        RDS); None goes back to asking."""
        f = self._tuner_now_freq()
        if sid in (None, ""):
            self.tuned_pick = None
            return {"ok": True}
        if not f:
            raise ValueError("The FM Tuner isn't answering")
        self.tuned_pick = (f, int(sid))
        return {"ok": True}

    def find_stations(self, query="", freq=None, fetch=None):
        """Add station search: FCC stations on a frequency (closest first)
        and/or matching a call, slogan, city, state or region ("central
        ky"). [{callsign, freq_mhz, location, service, licensee, lat, lon,
        distance_mi, compass, logged}] - stations with no RDS can be logged."""
        fetch = fetch or self.fetch
        q = " ".join((query or "").split())
        try:
            freq = round(float(freq), 1) if freq not in (None, "") else None
        except (TypeError, ValueError):
            freq = None
        city, state, _region = geo.parse_place(q)  # "central ky" -> KY
        call = q.upper() if re.fullmatch(
            r"[KWCX][A-Z0-9]{2,6}(?:-(?:FM|LP))?", q.upper()) else None
        recs = []
        if call:
            recs = geo.fcc_search(freq, call=call.split("-")[0], fetch=fetch)
        elif state:
            recs = geo.fcc_search(freq, city=city, state=state, fetch=fetch)
            if not recs and city:  # e.g. a suburb: the state's stations
                recs = geo.fcc_search(freq, state=state, fetch=fetch)
        elif freq:
            recs = geo.fcc_search(freq, fetch=fetch)
        words = [w for w in re.sub(r"[,.]", " ", q).lower().split()]
        if q and not (call or state):
            # a slogan or name: the log's slogans, Wikipedia, then the text
            hits = self._slogan_calls(q, freq, fetch)
            matched = [r for r in recs if r["callsign"].split("-")[0] in hits
                       or all(w in " ".join(str(r.get(k) or "") for k in (
                           "callsign", "location", "licensee")).lower()
                              for w in words)]
            if matched or not freq:
                recs = matched
            if not freq:
                for c in list(hits)[:6]:
                    try:
                        recs += geo.fcc_search(call=c, fetch=fetch)
                    except Exception:
                        pass
        out, seen = [], set()
        for r in recs:
            key = (r["callsign"], r["freq_mhz"])
            if key in seen:
                continue
            seen.add(key)
            mi, brg = geo.distance_bearing(self.home[0], self.home[1],
                                           r["lat"], r["lon"])
            known = self.store.find_station({"callsign": r["callsign"],
                                             "freq_mhz": r["freq_mhz"]})
            out.append({k: r.get(k) for k in (
                "callsign", "freq_mhz", "location", "service", "licensee",
                "lat", "lon", "status", "erp_kw", "station_class")} | {
                "distance_mi": round(mi), "compass": geo.compass_point(brg),
                "logged": bool(known), "station_id": known and known["id"]})
        out.sort(key=lambda r: r["distance_mi"])  # locals first
        return out[:60]

    def _slogan_calls(self, text, freq=None, fetch=None):
        """Calls whose slogan matches: the log's, then Wikipedia's search."""
        want = text.lower()
        calls = []
        for st in self.store.stations():
            if want in (st.get("slogan") or "").lower() and st.get("callsign"):
                calls.append(st["callsign"].split("-")[0].upper())
        try:
            found = logos.wiki_search_calls(
                text + (f" {freq:.1f}" if freq else "") + " radio station")
        except Exception:
            found = []
        return list(dict.fromkeys(calls + found))

    def add_station(self, rec):
        """Log a station picked in Add station (no RDS needed)."""
        call = (rec.get("callsign") or "").strip().upper()
        if not call:
            raise ValueError("Pick a station first")
        try:
            freq = float(rec.get("freq_mhz"))
        except (TypeError, ValueError):
            raise ValueError("That station has no frequency") from None
        out = {"type": "log", "ts": time.time(), "callsign": call,
               "freq_mhz": freq, "location": rec.get("location")}
        if rec.get("lat") is not None:
            out.update(lat=rec["lat"], lon=rec["lon"])
        sid, created = self.record(out, source="added by hand")
        return {"ok": True, "station_id": sid, "new": created}

    def fcc_for(self, sid):
        st = self.store.station(sid)
        if not st or not st.get("callsign"):
            return st
        try:
            fcc = geo.fcc_heard(st["callsign"], st.get("freq_mhz"),
                                self.home, fetch=self.fetch)
        except Exception as e:
            raise ValueError(f"FCC lookup failed ({e}) - check the "
                             "internet connection and try again") from e
        self.store.set_fcc(sid, fcc)
        return self.store.station(sid, self.home)

    COVERAGE_DAYS = 30

    def coverage(self, callsign, freq_mhz):
        """FCC service contour for the tuned station, saved for 30 days."""
        call = re.sub(r"[^A-Z0-9-]", "", (callsign or "").upper())
        if len(call) < 3:
            return None
        freq = round(float(freq_mhz), 1) if freq_mhz else None
        f = self.settings_path.parent / "Coverage" / f"{call}_{freq}.json"
        try:
            if time.time() - f.stat().st_mtime < self.COVERAGE_DAYS * 86400:
                return json.loads(f.read_text("utf-8")) or None
        except (OSError, ValueError):
            pass
        cov = geo.fcc_coverage(call, freq, self.home, fetch=self.fetch)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cov or {}), "utf-8")
        return cov

    def edit_station(self, sid, body):
        """Save an edit from Station Details or the Log Book."""
        old = self.store.station(sid) or {}
        try:  # unchanged coordinates are not "set by hand"
            if (float(body["lat"]), float(body["lon"])) == (
                    old.get("lat"), old.get("lon")):
                body = {k: v for k, v in body.items() if k not in ("lat", "lon")}
        except (KeyError, TypeError, ValueError):
            pass
        sid = self.store.edit(sid, body) or sid
        relocated = None
        new_call = (body.get("callsign") or "").strip().upper()
        if new_call and new_call != (old.get("callsign") or "").upper():
            relocated = self.relocate(sid)
        st = self.store.station(sid, self.home)
        if st and relocated is not None:
            st["relocated"] = relocated
        if st and ("callsign" in body or "slogan" in body):
            st["monitor_updated"] = self.monitor_set_call(
                st.get("pi"), st.get("callsign"),
                slogan=((st.get("slogan") or "") if "slogan" in body else None))
        return st

    # Log Book columns that change the station itself when typed in.
    BOOK_FIELDS = {"callsign": "callsign", "pi": "pi", "freq": "freq_mhz",
                   "city": "city", "state": "state", "slogan": "slogan"}

    def merge_dupes(self, pairs):
        """Merge [keep, drop] station pairs; a copy of the log is saved first."""
        folder = self.backup_dirs()[0]
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self._copy_db(folder / time.strftime("Before Duplicates %Y-%m-%d %H%M%S.db"))
        except Exception as e:
            raise ValueError(f"Couldn't save a copy first, so nothing was merged ({e})")
        for keep, drop in pairs:
            self.store.merge(int(keep), int(drop))
        return {"ok": True, "merged": len(pairs)}

    def start_cleanup(self):
        """Log Book > Clean up, in the background with progress. A copy of
        the log is saved first; if that fails nothing is deleted."""
        with self._cleanup_lock:
            if self.cleanup_job and self.cleanup_job["state"] == "running":
                raise ValueError("Clean up is already running")
            folder = self.backup_dirs()[0]
            dest = folder / time.strftime("Before Clean up %Y-%m-%d %H%M%S.db")
            try:
                folder.mkdir(parents=True, exist_ok=True)
                self._copy_db(dest)
            except Exception as e:
                raise ValueError(f"Couldn't save a copy first, so nothing was deleted ({e})")
            job = self.cleanup_job = {"state": "running", "done": 0, "total": 0,
                                      "backup": str(dest)}

        def run():
            try:
                job.update(self.store.cleanup(
                    progress=lambda i, n: job.update(done=i, total=n)))
                job["state"] = "done"
            except Exception as e:
                job.update(state="error", error=str(e))
        self._cleanup_thread = threading.Thread(target=run, daemon=True)
        self._cleanup_thread.start()
        return dict(job)

    def book_edit(self, cells, value):
        """The same value typed into each [station id, column] cell; ""
        blanks them, None puts back what DX Mapper fills in. Other columns
        keep what you typed over later catches and lookups."""
        cols = dict(LOGBOOK_COLUMNS)
        if value is not None:
            value = str(value).strip()
        todo = []
        for sid, col in cells:
            if col not in cols:
                raise ValueError(f"Unknown column {col}")
            if col.startswith("rds_"):
                raise ValueError(f"{cols[col]} is ticked from your RDS reports and can't be typed in")
            field = self.BOOK_FIELDS.get(col)
            if value == "" and field in ("callsign", "pi", "freq_mhz"):
                raise ValueError(f"{cols[col]} can't be blank")
            if value and col == "freq":
                try:
                    float(value)
                except ValueError:
                    raise ValueError("Frequency must be a number like 101.1")
            todo.append((int(sid), col, field))
        for sid, col, field in todo:
            if col == "radiotext" and value:
                st = self.store.station(sid) or {}
                value = rtclean.clean_rt(value, (st.get("ps"), st.get("callsign"),
                                                 st.get("slogan"))) or ""
            if field and value:
                self.store.set_book_cell(sid, col, None)
                self.edit_station(sid, {field: value})
            else:
                self.store.set_book_cell(sid, col, value)
        return {"ok": True, "saved": len(todo)}

    def relocate(self, sid):
        """New call letters: look the station up again (FCC site, city).
        Returns "fcc", "not found" or "offline"."""
        st = self.store.station(sid)
        try:
            fcc = geo.fcc_heard(st["callsign"], st.get("freq_mhz"),
                                self.home, fetch=self.fetch)
        except Exception:
            return "offline"
        self.store.set_fcc(sid, fcc, replace=True)
        if not fcc:
            self.queue_geo(sid, urgent=True)  # city fallback if it has none
        return "fcc" if fcc else "not found"

    # -- map tile cache -----------------------------------------------------
    def tile(self, layer, z, x, y):
        """(image bytes, content type) of a map tile, from settings/TileCache
        when already downloaded. Raises LookupError for unknown tiles."""
        src = TILE_SOURCES.get(layer)
        if not src or not 0 <= z <= 20 or not (0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise LookupError("no such tile")
        cache = self.settings_path.parent / "TileCache"
        if not self.__dict__.get("_old_tiles_removed"):
            self._old_tiles_removed = True
            for old in OLD_TILE_FOLDERS:
                if (cache / old).is_dir():
                    threading.Thread(target=shutil.rmtree, args=(cache / old, True),
                                     daemon=True).start()
        folder = cache / TILE_FOLDERS.get(layer, layer) / str(z) / str(x)
        cached = next((p for p in (folder / f"{y}.png", folder / f"{y}.jpg")
                       if p.is_file()), None)
        ctype_of = {".png": "image/png", ".jpg": "image/jpeg"}
        if cached:  # saved tiles are kept for good (delete TileCache to refresh)
            return cached.read_bytes(), ctype_of[cached.suffix]
        url = src.format(s="abc"[(x + y) % 3], z=z, x=x, y=y)
        with _tile_slots[layer]:  # a few at a time, so busy servers don't refuse
            for attempt in range(3):
                try:
                    fetch = self.tile_fetch or (
                        geo.http_get_pooled if attempt < 2 else geo.http_get_bytes)
                    data, ctype = fetch(url)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    if not self.tile_fetch:
                        time.sleep(1 + 2 * attempt)
        if ctype.startswith("image/") and data:
            ext = ".jpg" if "jpeg" in ctype else ".png"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                tmp = folder / f"{y}{ext}.tmp"
                tmp.write_bytes(data)
                os.replace(tmp, folder / f"{y}{ext}")
                if cached and cached.suffix != ext:
                    cached.unlink()
            except OSError:
                pass
        return data, ctype

    def _geocode_cache_path(self):
        return self.settings_path.parent / "geocode_cache.json"

    def find_branding(self, sid, st):
        """Slogan for a station that has none: Wikipedia's "Branding"
        (once per call and frequency each run; may be out of date)."""
        tried = self.__dict__.setdefault("_branding_tried", set())
        key = (st["callsign"].upper(), st.get("freq_mhz"))
        if key in tried:
            return False
        tried.add(key)
        if store_hd_sub(st["callsign"]) and all(
                str(s).startswith("import") for s in st.get("sources") or []):
            return False  # a past HD catch: its name may have changed since
        shared = self.monitor_slogan(st.get("pi"), st["callsign"])
        if shared:  # already in the RDS Monitor's stations.txt
            self.store.edit(sid, {"slogan": shared})
            return False
        get = ((lambda u: (self.fetch(u).encode("utf-8"), ""))
               if self.fetch else geo.http_get_bytes)
        brand = logos.wiki_branding(st["callsign"], st.get("freq_mhz"), get)
        if brand:
            self.store.edit(sid, {"slogan": brand, "slogan_source": "Wikipedia"})
            self.monitor_set_call(st.get("pi"), st["callsign"],
                                  fill={"slogan": brand})
        return True

    def locate(self, sid):
        """Find a station's coordinates: the FCC transmitter site from its
        call letters first, otherwise its City + State (OpenStreetMap)."""
        try:
            self._locate(sid)
        finally:
            st = self.store.station(sid)
            if st and st.get("callsign") and not st.get("slogan"):
                try:
                    self.find_branding(sid, st)
                except Exception:
                    pass  # offline: tried again next start
            if st and st.get("callsign"):
                try:
                    self.find_hd_info(sid, st)
                except Exception:
                    pass
            if st and st.get("wtfda") is None and (st.get("callsign") or st.get(
                    "pi")) and not store_hd_sub(st.get("callsign") or ""):
                self.queue_wtfda(sid)

    def _locate(self, sid):
        store = self.store
        st = store.station(sid)
        if not st:
            return
        city, state = st.get("city"), st.get("state")
        if not city and st.get("location"):
            city, _, rest = st["location"].partition(",")
            state = state or rest.strip() or None
        with self._geocache_lock:
            if self._geocache is None:
                try:
                    self._geocache = json.loads(
                        self._geocode_cache_path().read_text("utf-8"))
                except (OSError, ValueError):
                    self._geocache = {}
        if st.get("lat") is None and (city or "").strip():
            # A city looked up before: on the map at once, FCC refines it.
            hit = geo.cached_place(city, state, st.get("country"), self._geocache)
            if hit:
                if store is self.store:
                    store.set_coords(sid, hit[0], hit[1], "City (OpenStreetMap)")
        if st.get("fcc") is None and st.get("callsign"):
            try:
                self.fcc_for(sid)
            except ValueError:
                pass  # offline: retried on the next start
            st = self.store.station(sid)
        if st.get("lat") is not None or not (city or "").strip():
            return
        before = len(self._geocache)
        try:
            hit = geo.geocode_place(city, state, st.get("country"),
                                    cache=self._geocache, fetch=self.fetch)
        except Exception:
            return  # offline: retried on the next start
        if len(self._geocache) != before:
            try:
                self._geocache_lock.acquire()
                path = self._geocode_cache_path()
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._geocache, indent=0), "utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
            finally:
                self._geocache_lock.release()
        if hit:
            self.store.set_coords(sid, hit[0], hit[1], "City (OpenStreetMap)")

    def _geo_worker(self):
        while True:
            try:
                sid = self._geo_q.get(timeout=30)[2]
            except queue.Empty:
                return
            try:
                self.locate(sid)
            except Exception:
                pass
            finally:
                with self._geo_lock:
                    self._geo_pending.discard(sid)

    def queue_missing_geo(self):
        for st in self.store.needs_geo():
            self.queue_geo(st["id"])

    # -- imports ------------------------------------------------------------
    def import_preview(self, body):
        url = (body.get("url") or "").strip()
        if url:
            csv_url = importers.google_sheet_csv_url(url) or url
            try:
                text = (self.fetch or geo.http_get_text)(csv_url)
            except Exception as e:
                raise ValueError(
                    f"could not download the sheet ({e}). In Google Sheets use "
                    "Share > 'Anyone with the link' (Viewer), or File > "
                    "Download > CSV and import the file") from e
            if text.lstrip().lower().startswith(("<!doctype", "<html")):
                raise ValueError(
                    "Google returned a sign-in page, not the sheet. Set Share > "
                    "General access to 'Anyone with the link', or download it "
                    "as CSV and import the file")
            name = "Google Sheet"
        else:
            text = body.get("text") or ""
            name = body.get("filename") or "upload"
            data = None
            if body.get("data_b64"):
                data = base64.b64decode(body["data_b64"].split(",")[-1])
        result = importers.load_file(name, text, data if not url else None)
        token = uuid.uuid4().hex
        self._imports = {token: dict(result, name=name)}
        out = {"token": token, "kind": result["kind"],
               "format": result["format"], "name": name}
        if result["kind"] == "table":
            out.update(headers=result["headers"],
                       sample=result["rows"][:8], total=len(result["rows"]),
                       mapping={str(k): v for k, v in result["mapping"].items()},
                       fields=[{"key": k, "label": importers.FIELD_LABELS[k]}
                               for k in importers.FIELDS])
        else:
            out.update(total=len(result["records"]),
                       sample=result["records"][:8])
        return out

    def import_commit(self, body, background=False):
        job = self._imports.pop(body.get("token"), None)
        if not job:
            raise ValueError("import expired - choose the file again")
        if job["kind"] == "table":
            mapping = body.get("mapping") or job["mapping"]
            records = importers.rows_to_records(job["headers"], job["rows"],
                                                mapping)
            source = "import: " + ("Google Sheets" if job["name"] == "Google Sheet"
                                   else job["name"])
        else:
            records = job["records"]
            source = f"import: {job['name']}"
        rx = self.receive_locations().get(body.get("rx") or "")
        job = {"name": job["name"], "rows": len(records), "done": 0,
               "added": 0, "updated": 0, "skipped": 0, "errors": [],
               "started": time.time(), "finished": None}
        if not background:
            self._run_import(job, records, source, rx)
            return {k: job[k] for k in ("added", "updated", "skipped", "rows", "errors")}
        with self._import_lock:
            if self.import_job and not self.import_job["finished"]:
                raise ValueError("an import is already running - wait for it to finish")
            self.import_job = job
        threading.Thread(target=self._run_import, args=(job, records, source, rx),
                         daemon=True).start()
        return {"started": True, "rows": len(records), "name": job["name"]}

    def _run_import(self, job, records, source, rx):
        found = []
        try:
            with self.store.batch():
                for i, rec in enumerate(records, 1):
                    try:
                        rec = self.monitor_station(rec)
                        if rx and rec.get("rx_lat") is None:
                            rec["rx_lat"], rec["rx_lon"], rec["rx_name"] = rx
                        sid, created = self.store.log(rec, source=source)
                    except ValueError as e:
                        job["skipped"] += 1
                        if len(job["errors"]) < 5:
                            job["errors"].append(str(e))
                    else:
                        job["added" if created else "updated"] += 1
                        found.append(sid)
                    job["done"] = i
                    if i % 25 == 0:
                        # Low priority: save progress and let live logs and
                        # the map go first.
                        self.store.commit()
                        live = (self.last_live or {}).get("at") or 0
                        time.sleep(0.25 if time.time() - live < 5 else 0.01)
        except Exception as e:
            job["errors"].append(f"{type(e).__name__}: {e}")
        finally:
            job["finished"] = time.time()
        for sid in found:
            self.queue_geo(sid)

    def export_csv(self):
        cols = ["callsign", "pi", "freq_mhz", "ps", "pty", "location", "state",
                "country", "lat", "lon", "distance_mi", "compass",
                "first_heard", "last_heard", "times_logged",
                "best_signal_dbfs", "best_usable_info", "best_block_pct",
                "sources"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for st in self.store.stations(self.home):
            row = []
            for c in cols:
                v = st.get(c)
                if c in ("first_heard", "last_heard") and v:
                    v = time.strftime("%Y-%m-%d %H:%M", time.localtime(v))
                elif c == "sources" and v:
                    v = "; ".join(v)
                row.append("" if v is None else v)
            w.writerow(row)
        return buf.getvalue()

    def export_xlsx(self):
        fd, path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        try:
            excel.write_xlsx(path, excel.build_sheets(self.store, self.home))
            with open(path, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    # ---- several logs: Main is settings/dx_log.db, others settings/Logs/<name>.db.
    # The first ticked log (self.store) gets catches and edits; the rest
    # (self.views) are shown with it, view only.
    @property
    def main_name(self):
        return self.settings.get("main_log_name") or "Main"

    def log_dir(self):
        return self.settings_path.parent / "Logs"

    def backup_root(self):
        return self.settings_path.parent / "Backups"

    def log_names(self):
        names = [self.main_name]
        if self.log_dir().is_dir():
            names += sorted((p.stem for p in self.log_dir().glob("*.db")
                             if p.stem.lower() != self.main_name.lower()),
                            key=str.lower)
        return names

    def log_db(self, name):
        return self.main_db if name == self.main_name else self.log_dir() / f"{name}.db"

    def log_excel(self, name):
        if name == self.main_name:
            return str(self.main_excel or self.settings_path.parent / "DX Log.xlsx")
        return str(self.log_dir() / f"{name}.xlsx")

    def _log_folder(self):
        return "" if Path(self.store.path) == self.main_db else self.log_name

    def _name_ok(self, name, exists=False):
        name = " ".join(str(name or "").split())
        if exists:
            if name not in self.log_names():
                raise ValueError(f"There's no log named {name}")
            return name
        if not _LOG_NAME.match(name):
            raise ValueError("A log name can have letters, numbers, spaces "
                             "and - ' & ( ) . (up to 40)")
        if name.lower() in (n.lower() for n in self.log_names()):
            raise ValueError(f"There's already a log named {name}")
        return name

    def logs_info(self):
        return {"log": self.log_name, "logs": self.log_names(),
                "logs_shown": [self.log_name] + [n for n, _ in self.views],
                "log_to_all": bool(self.settings.get("log_to_all"))}

    def _save_logs_setting(self):
        info = self.logs_info()
        self.settings["logs_shown"] = info["logs_shown"]
        self.settings["log_to_all"] = info["log_to_all"]
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings, indent=2), "utf-8")
        tmp.replace(self.settings_path)

    def show_logs(self, names):
        """Show the ticked logs together; names[0] gets new catches."""
        have, order = self.log_names(), []
        for n in names or []:
            n = " ".join(str(n).split())
            if n in have and n not in order:
                order.append(n)
        if not order:
            raise ValueError("Tick at least one log")
        with self._logs_lock:
            if order[0] != self.log_name:
                self._switch_write(order[0])
            old = dict(self.views)
            views = [(n, old.get(n) or self._spare.pop(n, None)
                      or Store(self.log_db(n))) for n in order[1:]]
            used = {id(s) for _, s in views} | {id(self.store)}
            for s in [s for _, s in self.views] + list(self._spare.values()):
                if id(s) not in used:
                    try:
                        s.close()
                    except Exception:
                        pass
            self.views, self._spare = views, {}
            self._save_logs_setting()
        return self.logs_info()

    def _switch_write(self, name):
        job = self.import_job
        if (job and not job.get("finished")) or (
                self.cleanup_job and self.cleanup_job.get("state") == "running"):
            raise ValueError("Wait for the import or clean-up to finish, then switch logs")
        new = dict(self.views).get(name) or Store(self.log_db(name))
        old = self.store
        if self.excel:
            try:
                self.excel.save_now()
            except Exception:
                pass
        try:
            self.backup_now()
        except Exception:
            pass
        for q in (self._geo_q, self._wtfda_q, self._stream_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        with self._geo_lock:
            self._geo_pending.clear()
            self._stream_pending.clear()
        with self._wtfda_lock:
            self._wtfda_queued.clear()
        new.local_mi, new.ext_mi = old.local_mi, old.ext_mi
        new.check_origin = getattr(old, "check_origin", None)
        new.on_change, old.on_change = old.on_change, None
        self._spare[self.log_name] = old
        self.views = [(n, s) for n, s in self.views if s is not new]
        self.store, self.log_name = new, name
        self._backup_rev = None
        if self.excel:
            self.excel.store = new
            self.excel.path = self.log_excel(name)
            self.excel.schedule()

    def new_log_book(self, name):
        name = self._name_ok(name)
        self.log_dir().mkdir(parents=True, exist_ok=True)
        Store(self.log_dir() / f"{name}.db").close()
        return self.logs_info()

    def rename_log_book(self, old, new):
        old, new = self._name_ok(old, exists=True), self._name_ok(new)
        with self._logs_lock:
            if old != self.main_name:
                if old == self.log_name:
                    raise ValueError(f"New catches go into {old}. Tick another log "
                                     f"first so it's first, then rename {old}.")
                was = dict(self.views).get(old)
                if was:
                    was.close()
                src, dst = Path(self.log_db(old)), self.log_dir() / f"{new}.db"
                src.rename(dst)
                for a, b in ((Path(self.log_excel(old)), self.log_dir() / f"{new}.xlsx"),
                             (self.backup_root() / old, self.backup_root() / new)):
                    if a.exists():
                        a.rename(b)
                self.views = [(new, Store(dst)) if n == old else (n, s)
                              for n, s in self.views]
            else:
                self.settings["main_log_name"] = new
                self.views = [(new if n == old else n, s) for n, s in self.views]
            if self.log_name == old:
                self.log_name = new
            self._save_logs_setting()
        return self.logs_info()

    def delete_log_book(self, name):
        """Delete one log: its database, Excel copy and its backups here."""
        name = self._name_ok(name, exists=True)
        if name == self.log_name:
            raise ValueError(f"New catches go into {name}. Tick another log first "
                             f"so it's first, then delete {name}.")
        with self._logs_lock:
            for n, s in self.views:
                if n == name:
                    s.close()
            self.views = [(n, s) for n, s in self.views if n != name]
            db = Path(self.log_db(name))
            for f in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"),
                      Path(self.log_excel(name))):
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass
            root = self.backup_root()
            if name != self.main_name:
                shutil.rmtree(root / name, ignore_errors=True)
            else:
                for f in ("DX Master Backup.db", "DX Master Backup.xlsx"):
                    try:
                        (root / f).unlink()
                    except FileNotFoundError:
                        pass
                shutil.rmtree(root / "Daily", ignore_errors=True)
            self._save_logs_setting()
        return self.logs_info()

    def write_id(self, vid):
        """A station/log id in the log that takes edits, or a polite error."""
        vid = int(vid)
        k = vid // VIEW_ID
        if k:
            name = self.views[k - 1][0] if k <= len(self.views) else "other"
            raise ValueError(f"That's from your {name} log, which is view only. "
                             f"Use Copy to {self.log_name} first.")
        return vid

    def view_rev(self):
        if not self.views and Path(self.store.path) == self.main_db:
            return self.store.rev
        return "|".join(f"{n}:{s.rev}" for n, s in
                        [(self.log_name, self.store)] + self.views)

    def view_stations(self, **period):
        mine = self.store.stations(self.home, **period)
        if not self.views:
            return mine
        for s in mine:
            s["log"] = self.log_name
        idx = {_match_key(s): s for s in mine}
        every = ({_match_key(s): s for s in self.store.stations(self.home)}
                 if any(period.values()) else idx)
        out, shown = list(mine), {id(s) for s in mine}
        for k, (name, store) in enumerate(self.views, 1):
            for o in store.stations(self.home, **period):
                key = _match_key(o)
                hit = idx.get(key) or every.get(key)
                if hit is not None:
                    if id(hit) not in shown:
                        hit["log"] = self.log_name
                        out.append(hit)
                        shown.add(id(hit))
                        idx[key] = hit
                    _fill(hit, o)
                    hit.setdefault("also_in", []).append(name)
                    continue
                o = dict(_offset_ids(o, k * VIEW_ID), log=name, view_only=True)
                idx[key] = o
                out.append(o)
                shown.add(id(o))
        return out

    def view_count(self):
        return len(self.view_stations()) if self.views else self.store.station_count()

    def view_logs(self, **period):
        logs = self.store.all_logs(**period)
        if not self.views:
            return logs
        ids = {_match_key(s): s["id"] for s in self.view_stations()}
        for lg in logs:
            lg["log"] = self.log_name
        for k, (name, store) in enumerate(self.views, 1):
            for lg in store.all_logs(**period):
                key = _match_key({"callsign": lg.get("callsign"), "pi": lg.get("pi"),
                                  "freq_mhz": lg.get("station_freq")})
                sid = ids.get(key, lg["station_id"] + k * VIEW_ID)
                logs.append(dict(lg, id=lg["id"] + k * VIEW_ID, station_id=sid,
                                 log=name, view_only=True))
        logs.sort(key=lambda lg: lg.get("start_ts") or 0, reverse=True)
        return logs

    def view_days(self):
        days = self.store.days()
        if not self.views:
            return days
        by = {}
        for d in days + [d for _, s in self.views for d in s.days()]:
            key = d.get("day")
            if key in by:
                for k, v in d.items():
                    if isinstance(v, (int, float)) and isinstance(by[key].get(k), (int, float)):
                        by[key][k] += v
            else:
                by[key] = dict(d)
        return sorted(by.values(), key=lambda d: str(d.get("day")), reverse=True)

    def view_logbook(self):
        rows = self.store.logbook(self.home)
        if not self.views:
            return rows
        for r in rows:
            r["log"] = self.log_name
        idx = {_match_key(r): r for r in rows}
        for k, (name, store) in enumerate(self.views, 1):
            for r in store.logbook(self.home):
                key = _match_key(r)
                hit = idx.get(key)
                if hit is not None:
                    _fill(hit, r)
                    hit.setdefault("also_in", []).append(name)
                    continue
                r = dict(r, id=r["id"] + k * VIEW_ID, log=name, view_only=True)
                idx[key] = r
                rows.append(r)
        return rows

    def view_station(self, vid):
        vid = int(vid)
        k = vid // VIEW_ID
        if k:
            if k > len(self.views):
                return None
            name, store = self.views[k - 1]
            st = store.station(vid - k * VIEW_ID, self.home)
            if not st:
                return None
            st = _offset_ids(st, k * VIEW_ID)
            for lg in st.get("logs") or []:
                lg.update(log=name, view_only=True)
            return dict(st, log=name, view_only=True, write_log=self.log_name)
        st = self.store.station(vid, self.home)
        if not st or not self.views:
            return st
        st.update(log=self.log_name, write_log=self.log_name, also_in=[])
        for lg in st.get("logs") or []:
            lg["log"] = self.log_name
        key = _match_key(st)
        for k, (name, store) in enumerate(self.views, 1):
            for o in store.stations(self.home):
                if _match_key(o) != key:
                    continue
                full = _offset_ids(store.station(o["id"], self.home) or {}, k * VIEW_ID)
                _fill(st, full)
                old = full.get("logs") or []
                for lg in old:
                    lg.update(log=name, view_only=True)
                st["logs"] = sorted((st.get("logs") or []) + old,
                                    key=lambda lg: lg.get("start_ts") or 0, reverse=True)
                st["also_in"].append({"log": name, "logs": len(old),
                                      "first": min((lg.get("start_ts") or 0 for lg in old),
                                                   default=None)})
        return st

    def copy_to_current(self, sid=None, log_id=None):
        """Copy a station from a view-only log into the log that takes
        catches, with its whole HD family; with log_id also that catch (and
        the family's entries from the same catch). Blanks fill only."""
        vid = int(log_id if log_id is not None else sid)
        k = vid // VIEW_ID
        if not k or k > len(self.views):
            raise ValueError("Only stations from a view-only log can be copied")
        name, src = self.views[k - 1]
        span = None
        if log_id is not None:
            row = src.db.execute("SELECT * FROM logs WHERE id=?",
                                 (vid - k * VIEW_ID,)).fetchone()
            if not row:
                raise ValueError("That log entry is gone")
            local = row["station_id"]
            span = (row["start_ts"] or 0, row["end_ts"] or row["start_ts"] or 0)
        else:
            local = vid - k * VIEW_ID
        st = src.station(local)
        if not st:
            raise ValueError("That station is gone")
        family = [local] + [f["id"] for f in st.get("hd_family") or [] if f.get("id")]
        dst, stations, logs = self.store, 0, 0
        with dst._lock, dst.db:
            for fid in family:
                srow = src.db.execute("SELECT * FROM stations WHERE id=?", (fid,)).fetchone()
                if not srow:
                    continue
                tid = self._copy_station_row(dst, srow, fresh=span is not None)
                stations += 1
                if span is None:
                    continue
                for lrow in src.db.execute(
                        "SELECT * FROM logs WHERE station_id=? AND start_ts<=? "
                        "AND coalesce(end_ts, start_ts)>=?",
                        (fid, span[1] + 600, span[0] - 600)).fetchall():
                    logs += self._copy_log_row(dst, lrow, tid)
            dst._bump()
        return {"ok": True, "stations": stations, "logs": logs, "into": self.log_name,
                "from": name}

    @staticmethod
    def _cols(db, table):
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}

    def _copy_station_row(self, dst, srow, fresh):
        key = _match_key(dict(srow))
        hit = next((s for s in dst.stations() if _match_key(s) == key), None)
        cols = [c for c in srow.keys() if c not in ("id", "rev")
                and c in self._cols(dst.db, "stations")]
        if hit:
            cur = dst.db.execute("SELECT * FROM stations WHERE id=?", (hit["id"],)).fetchone()
            sets = [c for c in cols if c not in ("first_heard", "last_heard", "times_logged")
                    and _blank(cur[c]) and not _blank(srow[c])]
            if sets:
                dst.db.execute(f"UPDATE stations SET {', '.join(c + '=?' for c in sets)} "
                               "WHERE id=?", [srow[c] for c in sets] + [hit["id"]])
            return hit["id"]
        vals = {c: srow[c] for c in cols}
        if fresh:
            vals.update(times_logged=0, first_heard=None, last_heard=None)
        cur = dst.db.execute(f"INSERT INTO stations ({', '.join(vals)}) VALUES "
                             f"({', '.join('?' * len(vals))})", list(vals.values()))
        return cur.lastrowid

    def _copy_log_row(self, dst, lrow, tid):
        if dst.db.execute("SELECT 1 FROM logs WHERE station_id=? AND abs(start_ts-?)<1",
                          (tid, lrow["start_ts"] or 0)).fetchone():
            return 0
        cols = [c for c in lrow.keys() if c not in ("id", "rev")
                and c in self._cols(dst.db, "logs")]
        vals = {c: lrow[c] for c in cols}
        vals["station_id"] = tid
        dst.db.execute(f"INSERT INTO logs ({', '.join(vals)}) VALUES "
                       f"({', '.join('?' * len(vals))})", list(vals.values()))
        ts = lrow["start_ts"]
        dst.db.execute("UPDATE stations SET times_logged=coalesce(times_logged,0)+1, "
                       "first_heard=min(coalesce(first_heard,?),?), "
                       "last_heard=max(coalesce(last_heard,?),?) WHERE id=?",
                       (ts, ts, ts, ts, tid))
        return 1

    def close_logs(self):
        for s in [self.store] + [s for _, s in self.views] + list(self._spare.values()):
            try:
                s.close()
            except Exception:
                pass

    def state(self):
        feed = self.feed
        return {
            "rev": self.view_rev(),
            "station_count": self.view_count(),
            **self.logs_info(),
            "started_at": self.started_at,
            "home": {"lat": self.home[0], "lon": self.home[1],
                     "source": "RDS Monitor" if self.monitor_dir else "DX Mapper",
                     "label": self.location_label},
            "locations": self.receive_locations(),
            "now": self._live_now(),
            "import": self.import_job,
            "monitor_sync": getattr(self, "monitor_sync", None),
            "logo_job": getattr(self, "logo_job", None),
            "stream_job": getattr(self, "stream_job", None),
            "last_live": self.last_live,
            "focus": self.focus,
            "map_pick": self.map_pick,
            "geo_pending": len(self._geo_pending),
            "settings": self.settings,
            "lite": LITE,
            "excel": self.excel.status() if self.excel else None,
            "backup": dict(self.backup, dirs=[str(p) for p in self.backup_dirs()]),
            "feed": {
                "port": feed.port if feed else None,
                "listening": bool(feed and feed._server),
                "error": feed.error if feed else None,
                "clients": feed.clients if feed else 0,
                "last_message": feed.last_message if feed else None,
                "last_station": feed.last_station if feed else None,
            },
        }


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DXMapper/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8",
                  extra=None):
            if not isinstance(body, (bytes, bytearray)):
                body = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (extra or {}):
                self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 50 * 1024 * 1024:
                raise ValueError("file too large")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")

        def _route(self, method):
            path, _, qs = self.path.partition("?")
            if LITE and path in FULL_ONLY:
                return self._send(404, {"error": "Not in RDS Monitor Lite"})
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(qs).items()}

            def period():
                num = (lambda k: float(query[k]) if query.get(k) else None)
                return {"since": num("since"), "until": num("until"),
                        "live": query.get("live") in ("1", "true")}
            try:
                if method == "GET":
                    if path in ("/logbook", "/logbook.html"):
                        return self._static("logbook.html")
                    if path == "/api/logbook/cleanup":
                        return self._send(200, dict(app.cleanup_job or {}))
                    if path == "/api/logbook/dupes":
                        return self._send(200, {"pairs": app.store.dupes()})
                    if path == "/api/logbook":
                        app.logbook_seen = time.time()
                        return self._send(200, {
                            "card": app.card_request,
                            "rev": app.view_rev(),
                            "local_mi": app.store.local_mi,
                            "ext_mi": app.store.ext_mi,
                            "hidden": [] if LITE else app.settings["logbook_hidden"],
                            "view": app.settings.get("logbook_view", "classic"),
                            "lite": LITE,
                            "columns": [list(c) for c in LOGBOOK_COLUMNS
                                        if not (LITE and c[0] in LITE_HIDDEN_COLUMNS)],
                            "rows": app.view_logbook()})
                    if path in ("/", "/index.html"):
                        return self._static("index.html")
                    if path.startswith("/static/"):
                        return self._static(path[len("/static/"):])
                    if path == "/tileports.js":
                        return self._send(200, ("window.DX_TILE_PORTS = %s;" % json.dumps(
                            app.tile_ports)).encode(), "text/javascript; charset=utf-8")
                    m = re.fullmatch(r"/tiles/([a-z]+)/(\d+)/(\d+)/(\d+)", path)
                    if m:
                        try:
                            data, ctype = app.tile(m.group(1), *map(int, m.groups()[1:]))
                        except LookupError:
                            return self._send(404, {"error": "no such tile"})
                        except Exception as e:
                            return self._send(502, {"error": f"tile download failed: {e}"})
                        return self._send(200, data, ctype,
                                          {"Cache-Control": "max-age=86400",
                                           "Access-Control-Allow-Origin": "*"})
                    if path == "/api/tuner/freq":
                        return self._send(200, {"freq_mhz": app.tuner_freq()})
                    if path == "/api/stations/find":
                        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                        try:
                            found = app.find_stations((qs.get("q") or [""])[0],
                                                      (qs.get("freq") or [None])[0])
                        except OSError as e:
                            return self._send(502, {"error": "Couldn't reach the FCC: %s" % e})
                        return self._send(200, {"stations": found})
                    m = re.fullmatch(r"/api/logo/([A-Za-z0-9]+(?:-HD[2-8])?)/choices", path)
                    if m:
                        return self._send(200, app.logo_choices(m.group(1)))
                    m = re.fullmatch(r"/api/logo/([A-Za-z0-9]+(?:-HD[2-8])?)", path)
                    if m:
                        f = app.logo_path(m.group(1))
                        if not f:
                            return self._send(404, {"error": "no logo"})
                        ctype = (mimetypes.guess_type(str(f))[0]
                                 or "application/octet-stream")
                        return self._send(200, f.read_bytes(), ctype,
                                          {"Cache-Control": "no-cache"})
                    if path == "/api/state":
                        return self._send(200, app.state())
                    if path == "/api/coverage":
                        try:
                            cov = app.coverage(query.get("call"), query.get("freq"))
                        except Exception as e:
                            return self._send(502, {"error": f"FCC coverage lookup failed: {e}"})
                        if not cov:
                            return self._send(404, {"error": "no FCC coverage outline"})
                        return self._send(200, cov)
                    if path == "/api/stations":
                        return self._send(200, {
                            "rev": app.view_rev(),
                            "stations": app.view_stations(
                                                           **period())})
                    if path == "/api/logs":
                        return self._send(200, {
                            "rev": app.view_rev(),
                            "logs": app.view_logs(**period())})
                    if path == "/api/days":
                        return self._send(200, {"days": app.view_days()})
                    m = re.fullmatch(r"/api/station/(\d+)/neighbors", path)
                    if m:
                        return self._send(200, app.neighbors(int(m.group(1))))
                    m = re.fullmatch(r"/api/station/(\d+)/summary", path)
                    if m:
                        try:
                            return self._send(200, {"summary": app.wiki_summary(int(m.group(1)))})
                        except Exception as e:
                            return self._send(502, {"error": f"Wikipedia lookup failed: {e}"})
                    m = re.fullmatch(r"/api/station/(\d+)", path)
                    if m:
                        st = app.view_station(int(m.group(1)))
                        if not st:
                            return self._send(404, {"error": "no such station"})
                        if st.get("fcc") is None and st.get("callsign") and not st.get("view_only"):
                            app.queue_geo(st["id"])
                        return self._send(200, st)
                    if path == "/api/export.xlsx":
                        name = time.strftime("DX Log %Y-%m-%d.xlsx")
                        return self._send(
                            200, app.export_xlsx(),
                            "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet",
                            {"Content-Disposition":
                             f'attachment; filename="{name}"'})
                    if path == "/api/export.csv":
                        name = time.strftime("dx_log_%Y%m%d.csv")
                        return self._send(
                            200, app.export_csv().encode("utf-8"),
                            "text/csv; charset=utf-8",
                            {"Content-Disposition":
                             f'attachment; filename="{name}"'})
                elif method == "POST":
                    body = self._json_body()
                    if path == "/api/log":
                        sid, created = app.record(body, source="map")
                        return self._send(200, {"ok": True, "station_id": sid,
                                                "new": created})
                    if path == "/api/stations/add":
                        try:
                            return self._send(200, app.add_station(body))
                        except ValueError as e:
                            return self._send(400, {"error": str(e)})
                    if path == "/api/now/pick":
                        try:
                            return self._send(200, app.pick_tuned(body.get("station_id")))
                        except (ValueError, TypeError) as e:
                            return self._send(400, {"error": str(e)})
                    if path == "/api/now/log":
                        return self._send(200, app.log_now())
                    m = re.fullmatch(r"/api/logo/([A-Za-z0-9]+(?:-HD[2-8])?)/pick", path)
                    if m:
                        raw = body.get("data") or ""
                        try:
                            app.pick_logo(m.group(1), url=body.get("url"),
                                          data=base64.b64decode(raw.split(",")[-1])
                                          if raw else None)
                        except Exception as e:
                            return self._send(400, {"error": str(e)})
                        return self._send(200, {"ok": True})
                    m = re.fullmatch(r"/api/logo/([A-Za-z0-9]+(?:-HD[2-8])?)", path)
                    if m:
                        if not app.monitor_dir:
                            return self._send(400, {
                                "error": "RDS Monitor folder not found"})
                        if not app.download_logo(m.group(1)):
                            return self._send(404, {
                                "error": "No logo found online for "
                                         + m.group(1).upper()})
                        return self._send(200, {"ok": True})
                    if path == "/api/open-card":
                        sid = int(body["id"])
                        app.card_request = {"id": sid, "at": time.time()}
                        seen = time.time() - app.logbook_seen < 12  # a Log Book is open
                        if not seen and body.get("browser"):
                            webbrowser.open(f"http://127.0.0.1:{self.server.server_port}/logbook?card={sid}")
                        return self._send(200, {"ok": True, "seen": seen})
                    if path == "/api/map/pick":
                        app.map_pick = {"id": int(body["id"]), "at": time.time()}
                        return self._send(200, {"ok": True})
                    if path == "/api/focus":
                        app.focus = {"id": int(body["id"]), "at": time.time()}
                        return self._send(200, {"ok": True})
                    if path == "/api/open-logbook":
                        webbrowser.open(f"http://127.0.0.1:{self.server.server_port}/logbook")
                        return self._send(200, {"ok": True})
                    if path == "/api/screenshot":
                        m = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=]+)",
                                         str(body.get("png") or ""))
                        if not m:
                            return self._send(400, {"error": "no picture"})
                        folder = app.settings_path.parent / "Screenshots"
                        folder.mkdir(parents=True, exist_ok=True)
                        f = folder / time.strftime("DX Mapper %Y-%m-%d %H%M%S.png")
                        f.write_bytes(base64.b64decode(m.group(1)))
                        if hasattr(os, "startfile"):
                            try:
                                os.startfile(str(folder))
                            except OSError:
                                pass
                        return self._send(200, {"ok": True, "file": f.name})
                    if path == "/api/open-site":
                        url = CONDITIONS_SITES.get(body.get("site"))
                        if not url:
                            return self._send(400, {"error": "Unknown website"})
                        webbrowser.open(url)
                        return self._send(200, {"ok": True})
                    if path == "/api/tune":
                        return self._send(200, app.tune(body.get("freq_mhz")))
                    if path == "/api/listen":
                        st = app.view_station(int(body.get("id") or 0))
                        what = "website" if body.get("what") == "website" else "stream"
                        url = ((st or {}).get("logbook") or {}).get(what) or ""
                        if not re.match(r"https?://", url or ""):
                            return self._send(400, {"error": "No website found for this station"
                                                    if what == "website" else
                                                    "No live stream found for this station"})
                        webbrowser.open(url)
                        return self._send(200, {"ok": True, "url": url})
                    if path == "/api/streams/missing":
                        app.find_missing_streams()
                        return self._send(200, app.state())
                    if path == "/api/logos/missing":
                        if not app.fetch_missing_logos():
                            return self._send(400, {
                                "error": "RDS Monitor folder not found"})
                        return self._send(200, app.state())
                    if path == "/api/monitor/sync":
                        if not app.sync_monitor_file():
                            return self._send(400, {
                                "error": "RDS Monitor folder not found"})
                        return self._send(200, app.state())
                    if path == "/api/restore":
                        done = app.restore_backup(body)
                        return self._send(200, dict(app.state(), restored=done))
                    if path == "/api/backup":
                        ok = app.backup_now(force=True)
                        return self._send(200, dict(app.state(), ok=ok))
                    if path == "/api/settings":
                        app.save_settings(body)
                        return self._send(200, app.state())
                    if path == "/api/import/preview":
                        return self._send(200, app.import_preview(body))
                    if path == "/api/logbook/cleanup":
                        return self._send(200, app.start_cleanup())
                    if path == "/api/logbook/dupes":
                        return self._send(200, app.merge_dupes(body.get("pairs") or []))
                    if path == "/api/logbooks/show":
                        if "log_to_all" in body:
                            app.settings["log_to_all"] = bool(body["log_to_all"])
                        return self._send(200, app.show_logs(body.get("names") or []))
                    if path == "/api/logbooks/new":
                        return self._send(200, app.new_log_book(body.get("name")))
                    if path == "/api/logbooks/rename":
                        return self._send(200, app.rename_log_book(body.get("old"), body.get("new")))
                    if path == "/api/logbooks/delete":
                        return self._send(200, app.delete_log_book(body.get("name")))
                    if path == "/api/logbooks/copy":
                        return self._send(200, app.copy_to_current(body.get("id"), body.get("log_id")))
                    if path == "/api/logbook/edit":
                        for c in body.get("cells") or []:
                            app.write_id(c[0] if isinstance(c, (list, tuple)) else c.get("id", 0))
                        return self._send(200, app.book_edit(
                            body.get("cells") or [], body.get("value")))
                    if path == "/api/import/commit":
                        return self._send(200, app.import_commit(body, background=True))
                    m = re.fullmatch(r"/api/station/(\d+)/fcc", path)
                    if m:
                        sid = app.write_id(m.group(1))
                        geo._fcc_cache.clear()
                        app.store.set_fcc(sid, None)
                        return self._send(200, app.fcc_for(sid))
                    m = re.fullmatch(r"/api/station/(\d+)", path)
                    if m:
                        return self._send(200, app.edit_station(app.write_id(m.group(1)), body))
                elif method == "DELETE":
                    m = re.fullmatch(r"/api/station/(\d+)", path)
                    if m:
                        app.store.delete(app.write_id(m.group(1)))
                        return self._send(200, {"ok": True})
                    m = re.fullmatch(r"/api/log/(\d+)", path)
                    if m:
                        app.store.delete_log(app.write_id(m.group(1)))
                        return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:  # keep the server alive
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def _static(self, rel):
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR not in target.parents or not target.is_file():
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "text/plain"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, target.read_bytes(), ctype)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


def serve(app, host=WEB_HOST, port=WEB_PORT):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # Browsers only fetch 6 things at once from one address, so map tiles are
    # also served on the next two ports to load three times as many at once.
    app.tile_ports = [port]
    for extra in (port + 1, port + 2) if port else ():
        try:
            more = ThreadingHTTPServer((host, extra), make_handler(app))
        except OSError:
            continue
        more.daemon_threads = True
        threading.Thread(target=more.serve_forever, daemon=True).start()
        app.tile_ports.append(extra)
    return httpd
