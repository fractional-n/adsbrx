"""Mode S / ADS-B message decoding (DF0/4/5/11/16/17/18/20/21)."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

from .crc import syndrome

CHARSET = "?ABCDEFGHIJKLMNOPQRSTUVWXYZ????? ???????????????0123456789??????"

EMERGENCY_STATES = ("none", "general", "lifeguard", "minfuel", "nordo", "unlawful", "downed", "reserved")

_SURFACE_MOVEMENT_STEPS = ((2, 0.125), (9, 1.0), (13, 2.0), (39, 15.0), (94, 70.0), (109, 100.0), (124, 175.0))

# Message kinds
IDENT = "ident"
AIRBORNE_POS = "airborne_pos"
SURFACE_POS = "surface_pos"
VELOCITY = "velocity"
STATUS = "status"
SURV_ALT = "surveillance_alt"
SURV_ID = "surveillance_id"
ALL_CALL = "all_call"
AIR_AIR = "air_air"
OTHER = "other"


@dataclass(slots=True)
class CprFrame:
    odd: bool
    lat: int
    lon: int
    surface: bool = False


@dataclass(slots=True)
class ModeSMessage:
    raw: bytes
    df: int
    icao: int
    kind: str = OTHER
    ts: float = 0.0
    sample: int = 0
    rssi: float = -99.0
    corrected: int = 0
    tc: int | None = None
    capability: int | None = None
    tisb: bool = False
    callsign: str | None = None
    category: str | None = None
    altitude: int | None = None
    alt_geom: int | None = None
    on_ground: bool | None = None
    squawk: str | None = None
    emergency: str | None = None
    alert: bool = False
    spi: bool = False
    cpr: CprFrame | None = None
    gs: float | None = None
    track: float | None = None
    vrate: int | None = None
    vrate_source: str | None = None
    heading: float | None = None
    airspeed: int | None = None
    airspeed_type: str | None = None
    geo_minus_baro: int | None = None

    @property
    def hex(self) -> str:
        return self.raw.hex().upper()

    @property
    def icao_hex(self) -> str:
        return f"{self.icao:06X}"

    def to_dict(self) -> dict:
        """Decoded fields that carry information, for JSON output."""
        out: dict = {"hex": self.hex, "df": self.df, "icao": self.icao_hex, "kind": self.kind}
        for f in fields(self):
            if f.name in ("raw", "df", "icao", "kind", "ts", "sample", "rssi", "corrected"):
                continue
            value = getattr(self, f.name)
            if value is None or (f.name in ("alert", "spi", "tisb") and not value):
                continue
            if f.name == "cpr":
                value = {"odd": value.odd, "lat": value.lat, "lon": value.lon, "surface": value.surface}
            out[f.name] = value
        return out


def decode_squawk(id13: int) -> str:
    """Mode A identity (4 octal digits) from a 13-bit ID field."""
    h = _id13_to_hex_gillham(id13)
    return f"{(h >> 12) & 7}{(h >> 8) & 7}{(h >> 4) & 7}{h & 7}"


def _id13_to_hex_gillham(v: int) -> int:
    h = 0
    if v & 0x1000:
        h |= 0x0010  # C1
    if v & 0x0800:
        h |= 0x1000  # A1
    if v & 0x0400:
        h |= 0x0020  # C2
    if v & 0x0200:
        h |= 0x2000  # A2
    if v & 0x0100:
        h |= 0x0040  # C4
    if v & 0x0080:
        h |= 0x4000  # A4
    if v & 0x0020:
        h |= 0x0100  # B1
    if v & 0x0010:
        h |= 0x0001  # D1
    if v & 0x0008:
        h |= 0x0200  # B2
    if v & 0x0004:
        h |= 0x0002  # D2
    if v & 0x0002:
        h |= 0x0400  # B4
    if v & 0x0001:
        h |= 0x0004  # D4
    return h


def _mode_a_to_c(mode_a: int) -> int | None:
    """Gillham (Gray) coded Mode A digits -> altitude in 100 ft units."""
    # D1 set is illegal; D2 set means > 62,700 ft which is far more likely a bit error
    if (mode_a & 0x888B) != 0 or (mode_a & 0x00F0) == 0:
        return None
    one_hundreds = 0
    five_hundreds = 0
    if mode_a & 0x0010:
        one_hundreds ^= 0x007
    if mode_a & 0x0020:
        one_hundreds ^= 0x003
    if mode_a & 0x0040:
        one_hundreds ^= 0x001
    if (one_hundreds & 5) == 5:
        one_hundreds ^= 2
    if one_hundreds > 5:
        return None
    if mode_a & 0x0002:
        five_hundreds ^= 0x0FF
    if mode_a & 0x0004:
        five_hundreds ^= 0x07F
    if mode_a & 0x1000:
        five_hundreds ^= 0x03F
    if mode_a & 0x2000:
        five_hundreds ^= 0x01F
    if mode_a & 0x4000:
        five_hundreds ^= 0x00F
    if mode_a & 0x0100:
        five_hundreds ^= 0x007
    if mode_a & 0x0200:
        five_hundreds ^= 0x003
    if mode_a & 0x0400:
        five_hundreds ^= 0x001
    if five_hundreds & 1:
        one_hundreds = 6 - one_hundreds
    return five_hundreds * 5 + one_hundreds - 13


def _gillham_altitude(ac13: int) -> int | None:
    n = _mode_a_to_c(_id13_to_hex_gillham(ac13))
    if n is None or n < -12:
        return None
    return n * 100


def decode_ac13(ac13: int) -> int | None:
    """Altitude (ft) from the 13-bit AC field of DF0/4/16/20."""
    if ac13 == 0 or ac13 & 0x0040:  # not available, or metric (M bit) which is not supported
        return None
    if ac13 & 0x0010:  # Q bit: 25 ft steps
        n = ((ac13 & 0x1F80) >> 2) | ((ac13 & 0x0020) >> 1) | (ac13 & 0x000F)
        return n * 25 - 1000
    return _gillham_altitude(ac13)


def decode_ac12(ac12: int) -> int | None:
    """Barometric altitude (ft) from the 12-bit ALT field of airborne position messages."""
    if ac12 == 0:
        return None
    if ac12 & 0x0010:
        n = ((ac12 & 0x0FE0) >> 1) | (ac12 & 0x000F)
        return n * 25 - 1000
    return _gillham_altitude(((ac12 & 0x0FC0) << 1) | (ac12 & 0x003F))


def decode_surface_speed(movement: int) -> float | None:
    if movement == 0 or movement > 124:
        return None
    if movement == 1:
        return 0.0
    if movement == 124:
        return 175.0
    for i in range(1, len(_SURFACE_MOVEMENT_STEPS)):
        hi_mov, hi_kt = _SURFACE_MOVEMENT_STEPS[i]
        if movement < hi_mov:
            lo_mov, lo_kt = _SURFACE_MOVEMENT_STEPS[i - 1]
            step = (hi_kt - lo_kt) / (hi_mov - lo_mov)
            return round(lo_kt + (movement - lo_mov) * step, 3)
    return None


def _decode_ident(msg: ModeSMessage, me: int, tc: int) -> None:
    msg.kind = IDENT
    msg.category = f"{'ABCD'[4 - tc]}{(me >> 48) & 7}"
    chars = "".join(CHARSET[(me >> (42 - 6 * i)) & 0x3F] for i in range(8)).rstrip()
    msg.callsign = chars or None


def _decode_airborne_position(msg: ModeSMessage, me: int, tc: int) -> None:
    msg.kind = AIRBORNE_POS
    ss = (me >> 49) & 3
    msg.alert = ss in (1, 2)
    msg.spi = ss == 3
    alt = (me >> 36) & 0xFFF
    if tc <= 18:  # TC 20-22 carry GNSS height whose encoding is ambiguous; position is still used
        msg.altitude = decode_ac12(alt)
    msg.on_ground = False
    msg.cpr = CprFrame(odd=bool((me >> 34) & 1), lat=(me >> 17) & 0x1FFFF, lon=me & 0x1FFFF)


def _decode_surface_position(msg: ModeSMessage, me: int) -> None:
    msg.kind = SURFACE_POS
    msg.on_ground = True
    msg.gs = decode_surface_speed((me >> 44) & 0x7F)
    if (me >> 43) & 1:
        msg.track = ((me >> 36) & 0x7F) * 360.0 / 128.0
    msg.cpr = CprFrame(odd=bool((me >> 34) & 1), lat=(me >> 17) & 0x1FFFF, lon=me & 0x1FFFF, surface=True)


def _decode_velocity(msg: ModeSMessage, me: int) -> None:
    subtype = (me >> 48) & 7
    if subtype not in (1, 2, 3, 4):
        return
    msg.kind = VELOCITY
    supersonic = subtype in (2, 4)
    raw_a = (me >> 32) & 0x3FF
    raw_b = (me >> 21) & 0x3FF

    if subtype in (1, 2):
        if raw_a and raw_b:
            mult = 4 if supersonic else 1
            vew = (raw_a - 1) * mult * (-1 if (me >> 42) & 1 else 1)
            vns = (raw_b - 1) * mult * (-1 if (me >> 31) & 1 else 1)
            msg.gs = math.hypot(vew, vns)
            msg.track = math.degrees(math.atan2(vew, vns)) % 360.0
    else:
        if (me >> 42) & 1:
            msg.heading = raw_a * 360.0 / 1024.0
        if raw_b:
            msg.airspeed = (raw_b - 1) * (4 if supersonic else 1)
            msg.airspeed_type = "TAS" if (me >> 31) & 1 else "IAS"

    vr = (me >> 10) & 0x1FF
    if vr:
        rate = (vr - 1) * 64
        msg.vrate = -rate if (me >> 19) & 1 else rate
        msg.vrate_source = "baro" if (me >> 20) & 1 else "geom"
    dif = me & 0x7F
    if dif:
        diff = (dif - 1) * 25
        msg.geo_minus_baro = -diff if (me >> 7) & 1 else diff


def _decode_status(msg: ModeSMessage, me: int) -> None:
    if (me >> 48) & 7 != 1:
        return
    msg.kind = STATUS
    state = (me >> 45) & 7
    msg.emergency = EMERGENCY_STATES[state] if state else None
    msg.squawk = decode_squawk((me >> 32) & 0x1FFF)


def decode(
    raw: bytes,
    *,
    ts: float = 0.0,
    sample: int = 0,
    rssi: float = -99.0,
    corrected: int = 0,
    icao: int | None = None,
) -> ModeSMessage | None:
    """Decode a frame that already passed CRC / address validation."""
    n = len(raw)
    if n not in (7, 14):
        return None
    df = raw[0] >> 3
    if (n == 14) != (df >= 16):
        return None

    if icao is None:
        if df in (11, 17, 18):
            icao = int.from_bytes(raw[1:4], "big")
        elif df in (0, 4, 5, 16, 20, 21):
            icao = syndrome(raw)
        else:
            return None
    msg = ModeSMessage(raw=raw, df=df, icao=icao, ts=ts, sample=sample, rssi=rssi, corrected=corrected)

    if df in (17, 18):
        msg.capability = raw[0] & 7
        if df == 18:
            msg.tisb = True
            if msg.capability not in (0, 6):
                return msg
        me = int.from_bytes(raw[4:11], "big")
        tc = me >> 51
        msg.tc = tc
        if 1 <= tc <= 4:
            _decode_ident(msg, me, tc)
        elif 5 <= tc <= 8:
            _decode_surface_position(msg, me)
        elif 9 <= tc <= 18 or 20 <= tc <= 22:
            _decode_airborne_position(msg, me, tc)
        elif tc == 19:
            _decode_velocity(msg, me)
        elif tc == 28:
            _decode_status(msg, me)
    elif df == 11:
        msg.kind = ALL_CALL
        msg.capability = raw[0] & 7
        if msg.capability == 4:
            msg.on_ground = True
        elif msg.capability == 5:
            msg.on_ground = False
    elif df in (0, 16):
        msg.kind = AIR_AIR
        msg.on_ground = bool((raw[0] >> 2) & 1)
        msg.altitude = decode_ac13(((raw[2] & 0x1F) << 8) | raw[3])
    elif df in (4, 20):
        msg.kind = SURV_ALT
        _apply_flight_status(msg, raw[0] & 7)
        msg.altitude = decode_ac13(((raw[2] & 0x1F) << 8) | raw[3])
    elif df in (5, 21):
        msg.kind = SURV_ID
        _apply_flight_status(msg, raw[0] & 7)
        msg.squawk = decode_squawk(((raw[2] & 0x1F) << 8) | raw[3])
    return msg


def _apply_flight_status(msg: ModeSMessage, fs: int) -> None:
    msg.alert = fs in (2, 3, 4)
    msg.spi = fs in (4, 5)
    if fs in (0, 2):
        msg.on_ground = False
    elif fs in (1, 3):
        msg.on_ground = True
