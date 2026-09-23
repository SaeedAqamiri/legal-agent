"""Persian (Jalali) calendar conversion — stdlib-only port of jalaali-js.

Needed to map official document dates (e.g. ``1396/09/05`` from the Tamin
corpus manifest) onto ``date`` values used by temporal identity checks.
"""

from __future__ import annotations

from datetime import date

from .errors import DomainError

_BREAKS = (
    -61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
    1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178,
)


def _div(a: int, b: int) -> int:
    quotient = abs(a) // abs(b)
    if (a >= 0) != (b >= 0):
        return -quotient
    return quotient


def _mod(a: int, b: int) -> int:
    return ((a % b) + b) % b


def _jal_cal(jy: int) -> tuple[int, int, int]:
    """Return (leap, gy, march) for the Jalali year."""
    breaks_length = len(_BREAKS)
    gy = jy + 621
    leap_j = -14
    jp = _BREAKS[0]
    if jy < jp or jy >= _BREAKS[breaks_length - 1]:
        raise DomainError(f"jalali year {jy} is out of supported range")
    jump = 0
    for index in range(1, breaks_length):
        jm = _BREAKS[index]
        jump = jm - jp
        if jy < jm:
            break
        leap_j += _div(jump, 33) * 8 + _div(_mod(jump, 33), 4)
        jp = jm
    n = jy - jp
    leap_j += _div(n, 33) * 8 + _div(_mod(n, 33) + 3, 4)
    if _mod(jump, 33) == 4 and jump - n == 4:
        leap_j += 1
    leap_g = _div(gy, 4) - _div((_div(gy, 100) + 1) * 3, 4) - 150
    march = 20 + leap_j - leap_g
    if jump - n < 6:
        n = n - jump + _div(jump + 4, 33) * 33
    leap = _mod(_mod(n + 1, 33) - 1, 4)
    if leap == -1:
        leap = 4
    return leap, gy, march


def _g2d(gy: int, gm: int, gd: int) -> int:
    d = (
        _div((gy + _div(gm - 8, 6) + 100100) * 1461, 4)
        + _div(153 * ((gm + 9) % 12) + 2, 5)
        + gd
        - 34840408
    )
    d = d - _div(_div(gy + 100100 + _div(gm - 8, 6), 100) * 3, 4) + 752
    return d


def _d2g(jdn: int) -> tuple[int, int, int]:
    j = 4 * jdn + 139361631
    j = j + _div(_div(4 * jdn + 183187720, 146097) * 3, 4) * 4 - 3908
    i = _div(j % 1461, 4) * 5 + 308
    gd = _div(i % 153, 5) + 1
    gm = (_div(i, 153) % 12) + 1
    gy = _div(j, 1461) - 100100 + _div(8 - gm, 6)
    return gy, gm, gd


def _j2d(jy: int, jm: int, jd: int) -> int:
    _, gy, march = _jal_cal(jy)
    return _g2d(gy, 3, march) + (jm - 1) * 31 - _div(jm, 7) * (jm - 7) + jd - 1


def _d2j(jdn: int) -> tuple[int, int, int]:
    gy = _d2g(jdn)[0]
    jy = gy - 621
    leap, _, march = _jal_cal(jy)
    jdn1f = _g2d(gy, 3, march)
    k = jdn - jdn1f
    if 0 <= k <= 185:
        return jy, 1 + _div(k, 31), 1 + (k % 31)
    if k >= 0:
        k -= 186
    else:
        jy -= 1
        k += 179
        if leap == 1:
            k += 1
    return jy, 7 + _div(k, 30), 1 + (k % 30)


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    if not 1 <= jm <= 12:
        raise DomainError("jalali month must be between 1 and 12")
    if not 1 <= jd <= 31:
        raise DomainError("jalali day must be between 1 and 31")
    gy, gm, gd = _d2g(_j2d(jy, jm, jd))
    return date(gy, gm, gd)


def gregorian_to_jalali(value: date) -> tuple[int, int, int]:
    return _d2j(_g2d(value.year, value.month, value.day))


def parse_jalali_date(text: str) -> date:
    """Parse ``YYYY/MM/DD`` (Persian or Latin digits, any separator)."""
    normalized = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    for separator in ("/", "-", "."):
        if separator in normalized:
            parts = normalized.split(separator)
            if len(parts) == 3 and all(part.strip().isdigit() for part in parts):
                return jalali_to_gregorian(
                    int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())
                )
    raise DomainError(f"unrecognized jalali date: {text!r}")
