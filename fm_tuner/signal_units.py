"""Signal strength as S-units or dB, picked in the tuner's View menu.

The RTL-SDR only measures dBFS (relative to its full scale), so S-units
here are relative too, not calibrated microvolts: S1 at -70 dBFS (the
meter's empty end), 6 dB per S-unit, S9 at -22 dBFS, then S9+10 etc.
"""

UNITS = ("s", "db")
S1_DBFS = -70.0
DB_PER_S = 6.0


def s_units(dbfs):
    """'S7', 'S9+12' or 'S0' for a signal in dBFS."""
    s = (dbfs - S1_DBFS) / DB_PER_S + 1
    if s < 1:
        return "S0"
    if s < 9.5:
        return f"S{int(round(s))}"
    return f"S9+{int(round((s - 9) * DB_PER_S))}"


def format_signal(dbfs, units="s"):
    """The signal as the user wants it (S-units unless dB), or None."""
    if not isinstance(dbfs, (int, float)):
        return None
    if units == "db":
        return f"{dbfs:.1f} dBFS"
    return s_units(dbfs)

