"""Optional USB GPS support: read NMEA from a GPS dongle's COM port.

Most USB GPS dongles show up in Windows as a COM port sending NMEA 0183
text ($GPRMC, $GPGGA ...). This needs pyserial; without it, or without a
dongle, everything else keeps working and locations are typed by hand.
"""

import math
import threading
import time

BAUDS = (9600, 4800, 38400, 115200)
GPS_WORDS = ("gps", "gnss", "u-blox", "ublox", "nmea", "prolific", "sirf",
             "location", "navigation")
FOLLOW_MIN_MILES = 0.5
FOLLOW_MIN_SECONDS = 60


def have_pyserial():
    try:
        import serial  # noqa: F401
        return True
    except ImportError:
        return False


def _checksum_ok(line):
    if "*" not in line:
        return True
    body, _, cs = line[1:].partition("*")
    try:
        want = int(cs[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def _coord(value, hemi, deg_digits):
    if not value or hemi not in "NSEW" or not hemi:
        return None
    try:
        deg = int(value[:deg_digits])
        minutes = float(value[deg_digits:])
    except ValueError:
        return None
    out = deg + minutes / 60.0
    return -out if hemi in "SW" else out


def parse_nmea(line):
    """One NMEA sentence -> {"lat", "lon", ...} for a valid fix, else None.

    Reads RMC (position, speed) and GGA (position, satellites, altitude)
    from any talker (GP, GN, GL, GA, BD).
    """
    line = (line or "").strip()
    if not line.startswith("$") or not _checksum_ok(line):
        return None
    f = line.split("*")[0].split(",")
    kind = f[0][3:] if len(f[0]) == 6 else ""
    if kind == "RMC" and len(f) >= 7:
        if f[2] != "A":
            return None
        lat, lon = _coord(f[3], f[4], 2), _coord(f[5], f[6], 3)
        fix = {"kind": "RMC"}
        try:
            fix["speed_mph"] = round(float(f[7]) * 1.15078, 1)
        except (IndexError, ValueError):
            pass
    elif kind == "GGA" and len(f) >= 7:
        if f[6] in ("", "0"):
            return None
        lat, lon = _coord(f[2], f[3], 2), _coord(f[4], f[5], 3)
        fix = {"kind": "GGA"}
        try:
            fix["sats"] = int(f[7])
        except (IndexError, ValueError):
            pass
        try:
            fix["alt_ft"] = round(float(f[9]) * 3.28084)
        except (IndexError, ValueError):
            pass
    else:
        return None
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    fix["lat"], fix["lon"] = round(lat, 5), round(lon, 5)
    return fix


def distance_mi(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(min(1.0, math.sqrt(h)))


def should_follow(last_saved, last_time, fix, now=None):
    """True when "Keep Away updated from GPS" should save this fix:
    first fix, or moved at least FOLLOW_MIN_MILES and FOLLOW_MIN_SECONDS
    since the last save (so driving doesn't rewrite the file every second)."""
    if not fix:
        return False
    if last_saved is None:
        return True
    now = time.time() if now is None else now
    return (now - (last_time or 0) >= FOLLOW_MIN_SECONDS
            and distance_mi(last_saved, (fix["lat"], fix["lon"])) >= FOLLOW_MIN_MILES)


def list_ports():
    """[(port, description)] with likely GPS ports first."""
    ports = []
    try:
        from serial.tools import list_ports as lp
        ports = [(p.device, p.description or p.device) for p in lp.comports()]
    except ImportError:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                 r"HARDWARE\DEVICEMAP\SERIALCOMM")
            i = 0
            while True:
                try:
                    _, dev, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                ports.append((dev, dev))
                i += 1
        except (ImportError, OSError):
            pass
    gpsish = lambda p: any(w in p[1].lower() for w in GPS_WORDS)
    return sorted(ports, key=lambda p: (not gpsish(p), p[0]))


class GpsReader:
    """Reads a GPS dongle on a background thread.

    port "Auto" tries every COM port (likely GPS ones first) at the common
    baud rates until NMEA shows up. on_fix(fix) is called from the thread
    for each valid fix; .status is a short line for the window.
    """

    def __init__(self, port="Auto", on_fix=None, opener=None, ports=None):
        self.port = port or "Auto"
        self.on_fix = on_fix
        self.fix = None
        self.fix_time = 0.0
        self.found_port = None
        self.status = "Starting..."
        self._opener = opener
        self._ports = ports
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="gps")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def _open(self, port, baud):
        if self._opener:
            return self._opener(port, baud)
        import serial
        return serial.Serial(port, baud, timeout=1)

    def _candidates(self):
        if self.port != "Auto":
            return [self.port]
        ports = self._ports() if self._ports else list_ports()
        return [p for p, _ in ports]

    def _probe(self, port, baud, seconds=2.5):
        """Open port at baud; return it if NMEA arrives, else None."""
        try:
            ser = self._open(port, baud)
        except Exception:
            return None
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set():
            try:
                raw = ser.readline()
            except Exception:
                break
            text = raw.decode("ascii", "ignore").strip() if raw else ""
            if text.startswith("$") and len(text) > 6 and _checksum_ok(text):
                return ser
        try:
            ser.close()
        except Exception:
            pass
        return None

    def _find(self):
        if self._opener is None and not have_pyserial():
            self.status = "pyserial is not installed (run Install All)"
            return None, None
        for port in self._candidates():
            for baud in BAUDS:
                if self._stop.is_set():
                    return None, None
                self.status = f"Looking for GPS on {port} ({baud})..."
                ser = self._probe(port, baud)
                if ser:
                    return ser, port
        self.status = ("No GPS found - plug it in, then press Retry"
                       if self.port == "Auto" else f"No GPS data on {self.port}")
        return None, None

    def _run(self):
        ser, port = self._find()
        if not ser:
            return
        self.found_port = port
        self.status = f"{port}: waiting for a fix (go near a window or outside)"
        while not self._stop.is_set():
            try:
                raw = ser.readline()
            except Exception:
                self.status = f"{port}: GPS unplugged"
                break
            if not raw:
                if self.fix and time.time() - self.fix_time > 10:
                    self.status = f"{port}: lost the fix"
                continue
            fix = parse_nmea(raw.decode("ascii", "ignore"))
            if not fix:
                continue
            if self.fix and fix["kind"] == "GGA":
                fix = {**self.fix, **fix}
            elif self.fix and fix["kind"] == "RMC" and "sats" in self.fix:
                fix = {**fix, "sats": self.fix["sats"]}
            self.fix, self.fix_time = fix, time.time()
            sats = f", {fix['sats']} satellites" if fix.get("sats") else ""
            self.status = f"{port}: fix {fix['lat']:.5f}, {fix['lon']:.5f}{sats}"
            if self.on_fix:
                try:
                    self.on_fix(fix)
                except Exception:
                    pass
        try:
            ser.close()
        except Exception:
            pass
