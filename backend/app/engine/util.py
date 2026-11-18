"""Small shared helpers (dates, INR formatting)."""
from __future__ import annotations
import re
from datetime import date, datetime, timedelta

_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
            "%Y-%m-%d", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y", "%d/%m/%Y")


def parse_dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return None
    for f in _FORMATS:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def parse_date(v) -> date | None:
    d = parse_dt(v)
    return d.date() if d else None


def num(v, default=None):
    if v is None:
        return default
    try:
        s = str(v).replace(",", "").strip()
        if s == "" or s.lower() == "nan":
            return default
        return float(s)
    except ValueError:
        return default


def inr(x) -> str:
    """Indian digit grouping, e.g. ₹1,37,531"""
    if x is None:
        return "-"
    neg = x < 0
    x = abs(x)
    if x >= 1000 or abs(x - round(x)) < 0.005:
        s, dec = str(int(round(x))), ""
    else:
        s, dec = f"{x:.2f}".split(".")
        dec = "." + dec
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = head + "," + tail
    return ("-" if neg else "") + "₹" + s + dec
