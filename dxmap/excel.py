"""Keeps an Excel (.xlsx) copy of the log, rewritten after every change.

Plain zipfile + XML so the logger needs no extra packages.
"""

import os
import threading
import time
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

from .store import LOGBOOK_COLUMNS

EXCEL_EPOCH = datetime(1899, 12, 30)
LOGBOOK_WIDTHS = {"freq": 9, "callsign": 11, "hd": 6, "hd_channels": 16, "slogan": 26, "pi": 7,
                  "city": 16, "state": 6, "distance": 10, "range_type": 14,
                  "owner": 26, "time_decoding": 14, "rt_plus": 14,
                  "radiotext": 40, "stream": 34, "website": 30, "source": 24, "notes": 40, "group_seq": 50,
                  **{k: 7 for k in ("rds_pi", "rds_ps", "rds_pty", "rds_rt", "rds_rtplus", "rds_ptyn", "rds_longps",
                                    "rds_af", "rds_eon", "rds_tmc", "rds_ct", "rds_tp", "rds_ta", "rds_di")}}

STATION_COLUMNS = (
    ("Callsign", "callsign", 11), ("PI", "pi", 7), ("MHz", "freq_mhz", 7),
    ("PS", "ps", 10), ("PTY", "pty_name", 14), ("Location", "location", 24),
    ("State", "state", 6), ("Country", "country", 8),
    ("Miles", "distance_mi", 8), ("Bearing", "bearing_deg", 8),
    ("Dir", "compass", 5), ("Propagation", "mode", 11),
    ("First logged", "first_heard", 16), ("Last logged", "last_heard", 16),
    ("Times logged", "times_logged", 8),
    ("Best signal dBFS", "best_signal_dbfs", 9),
    ("Best usable info %", "best_usable_info", 9),
    ("Best blocks %", "best_block_pct", 9),
    ("Latitude", "lat", 10), ("Longitude", "lon", 11),
    ("Coords from", "coord_source", 16), ("Licensee", "licensee", 24),
    ("ERP kW", "erp_kw", 7), ("Sources", "sources", 24),
)
LOG_COLUMNS = (
    ("Logged", "start_ts", 16), ("Until", "end_ts", 16),
    ("Callsign", "callsign", 11), ("PI", "pi", 7), ("MHz", "freq_mhz", 7),
    ("PS", "ps", 10), ("RadioText", "rt", 40), ("Signal dBFS", "signal_dbfs", 9),
    ("RDS level dB", "rds_level_db", 9), ("Usable info %", "usable_info", 9),
    ("Blocks %", "block_pct", 9), ("Logged from", "rx_name", 14),
    ("Miles", "rx_distance_mi", 8), ("Dir", "rx_compass", 6),
    ("Source", "source", 18), ("Auto", "auto", 6),
)
SIGNAL_KEYS = ("signal_dbfs", "best_signal_dbfs")
SIGNAL_UNITS = ["s"]  # follows the FM Tuner's View > Signal units


def s_units(dbfs):
    """'S7' or 'S9+12' for dBFS, as the tuner shows it (S1 at -70, 6 dB each)."""
    s = (dbfs + 70.0) / 6.0 + 1
    if s < 1:
        return "S0"
    if s < 9.5:
        return f"S{int(round(s))}"
    return f"S9+{int(round((s - 9) * 6.0))}"


def _header(name):
    return name.replace(" dBFS", "") if SIGNAL_UNITS[0] == "s" else name


DATE_KEYS = {"first_heard", "last_heard", "start_ts", "end_ts"}


def _col(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, value, style=0):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b" s="{style}"><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{value!r}</v></c>'
    if isinstance(value, datetime):
        serial = (value - EXCEL_EPOCH).total_seconds() / 86400
        return f'<c r="{ref}" s="2"><v>{serial:.8f}</v></c>'
    text = "".join(ch for ch in str(value) if ch in "\t\n\r" or ord(ch) >= 32)
    return (f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">'
            f'{escape(text)}</t></is></c>')


WARNING = ("THIS IS A COPY FOR VIEWING. Edits typed here are NOT read back and "
           "get overwritten. Make changes in DX Mapper, or bring an edited copy "
           "in with DX Mapper > Import... (Excel or Google Sheets).")


def _sheet_xml(headers, widths, rows):
    last = _col(len(headers) - 1)
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="2" topLeftCell="A3" '
           'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>']
    for i, w in enumerate(widths):
        out.append(f'<col min="{i + 1}" max="{i + 1}" width="{w + 2}" customWidth="1"/>')
    out.append('</cols><sheetData><row r="1" ht="30" customHeight="1">')
    out.append(_cell("A1", WARNING, 3))
    out.append('</row><row r="2">')
    out.extend(_cell(f"{_col(i)}2", h, 1) for i, h in enumerate(headers))
    out.append("</row>")
    for r, row in enumerate(rows, start=3):
        out.append(f'<row r="{r}">')
        out.extend(_cell(f"{_col(i)}{r}", v) for i, v in enumerate(row))
        out.append("</row>")
    out.append(f'</sheetData><autoFilter ref="A2:{last}{len(rows) + 2}"/>'
               f'<mergeCells count="1"><mergeCell ref="A1:{last if len(headers) > 1 else "B"}1"/>'
               '</mergeCells></worksheet>')
    return "".join(out)


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/></numFmts>'
          '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
          '<font><b/><sz val="11"/><name val="Calibri"/></font>'
          '<font><b/><sz val="13"/><color rgb="FFC00000"/><name val="Calibri"/></font></fonts>'
          '<fills count="4"><fill><patternFill patternType="none"/></fill>'
          '<fill><patternFill patternType="gray125"/></fill>'
          '<fill><patternFill patternType="solid"><fgColor rgb="FFD9E1F2"/></patternFill></fill>'
          '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF2CC"/></patternFill></fill></fills>'
          '<borders count="1"><border/></borders>'
          '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
          '<cellXfs count="4"><xf/>'
          '<xf fontId="1" fillId="2" applyFont="1" applyFill="1"/>'
          '<xf numFmtId="164" applyNumberFormat="1"/>'
          '<xf fontId="2" fillId="3" applyFont="1" applyFill="1" applyAlignment="1">'
          '<alignment vertical="center" wrapText="1"/></xf></cellXfs>'
          '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
          '</styleSheet>')


def write_xlsx(path, sheets):
    """sheets: [(name, headers, widths, rows)] -> .xlsx written atomically."""
    names = "".join(f'<sheet name="{escape(s[0])}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                    for i, s in enumerate(sheets))
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
        for i in range(len(sheets)))
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(len(sheets)))
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   f'{overrides}</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                   '2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<sheets>{names}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'{rels}<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", STYLES)
        for i, (_, headers, widths, rows) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml",
                       _sheet_xml(headers, widths, rows))
    os.replace(tmp, path)


def _value(row, key):
    v = row.get(key)
    if key in DATE_KEYS:
        return datetime.fromtimestamp(v) if v else None
    if key == "sources" and isinstance(v, list):
        return "; ".join(v)
    if key == "auto":
        return "yes" if v else ""
    if key in ("licensee", "erp_kw"):
        return (row.get("fcc") or {}).get(key)
    if key in ("signal_dbfs", "rds_level_db", "usable_info", "block_pct") \
            and isinstance(v, (int, float)) and v < -900:
        return None
    if key in SIGNAL_KEYS and SIGNAL_UNITS[0] == "s" and isinstance(v, (int, float)):
        return s_units(v)
    if isinstance(v, float):
        return round(v, 4)
    return v


def build_sheets(store, home):
    stations = store.stations(home)
    logs = store.all_logs()
    book = store.logbook(home)
    return [
        ("Log Book", [c[1] for c in LOGBOOK_COLUMNS],
         [LOGBOOK_WIDTHS.get(c[0], 12) for c in LOGBOOK_COLUMNS],
         [[r[c[0]] or None for c in LOGBOOK_COLUMNS] for r in book]),
        ("Stations", [_header(c[0]) for c in STATION_COLUMNS],
         [c[2] for c in STATION_COLUMNS],
         [[_value(st, c[1]) for c in STATION_COLUMNS] for st in stations]),
        ("Log History", [_header(c[0]) for c in LOG_COLUMNS],
         [c[2] for c in LOG_COLUMNS],
         [[_value(lg, c[1]) for c in LOG_COLUMNS] for lg in logs]),
    ]


class ExcelMirror:
    """Rewrites the workbook shortly after each change, in the background.

    If Excel has the file open (Windows locks it), the write is retried
    until the file is closed; the database is always the master copy.
    """

    def __init__(self, store, path, home_fn, delay=1.0, retry=15.0):
        self.store, self.path, self.home_fn = store, path, home_fn
        self.delay, self.retry = delay, retry
        self.quiet_max = 30.0
        self.last_saved = None
        self.error = None
        self._dirty = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def schedule(self):
        self._dirty.set()

    def save_now(self):
        try:
            write_xlsx(self.path, build_sheets(self.store, self.home_fn()))
        except PermissionError:
            self.error = "file is open in Excel - will save when it is closed"
            try:
                os.remove(self.path + ".tmp")
            except OSError:
                pass
            return False
        except Exception as e:  # never let the copy break logging
            self.error = str(e)
            return False
        self.last_saved = time.time()
        self.error = None
        after = getattr(self, "after_save", None)
        if after:
            try:
                after()
            except Exception:
                pass
        return True

    def _run(self):
        while not self._stop:
            self._dirty.wait()
            if self._stop:
                return
            time.sleep(self.delay)
            self._dirty.clear()
            # While changes keep coming (imports, location lookups) wait for
            # a quiet moment, but save at least every quiet_max seconds.
            start = time.time()
            while (self._dirty.wait(self.delay * 3) and not self._stop
                   and time.time() - start < self.quiet_max):
                self._dirty.clear()
            if self._stop:
                return
            if not self.save_now():
                time.sleep(self.retry)
                self._dirty.set()

    def stop(self):
        self._stop = True
        self._dirty.set()

    def status(self):
        return {"path": self.path, "last_saved": self.last_saved,
                "error": self.error}
