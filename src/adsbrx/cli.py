"""Command line interface: `adsbrx [run]`, `adsbrx simulate`, `adsbrx devices`, `adsbrx decode`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from collections.abc import Sequence

from . import __version__
from .app import DEMODULATORS, SOURCES, App, AppError, Config
from .crc import syndrome
from .demod import SAMPLE_RATE
from .sources import BLOCK_SAMPLES, FORMATS, SourceError

log = logging.getLogger("adsbrx")

EXAMPLES = """\
examples:
  adsbrx                                      receive with the first RTL-SDR dongle
  adsbrx --lat 35.55 --lon 139.78 -i          live aircraft table, with your position
  adsbrx --source sim                         try everything without any hardware
  adsbrx --file capture.cu8 --fast --stats    decode a recording as fast as possible
  adsbrx --record rec.cu8 --duration 240      also save 4 minutes of raw I/Q (about 1 GB)
  adsbrx --rtl-tcp 192.168.1.20               use a dongle served by rtl_tcp
  adsbrx --bind 0.0.0.0                       let other computers connect (LAN)

other commands (see 'adsbrx COMMAND -h'):
  simulate    write a synthetic I/Q recording for testing
  devices     list the RTL-SDR dongles that are plugged in
  decode      decode Mode S frames given as hex strings
"""


# ------------------------------------------------------------------------ argument types


def _gain(text: str) -> str | float:
    word = text.strip().lower()
    if word in ("max", "auto"):
        return word
    try:
        value = float(word)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"invalid gain {text!r} (use max, auto, or a number of dB)")
    return value


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {text!r} (0-65535)")
    return value


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"invalid value {text!r} (must be a number greater than 0)")
    return value


def _non_negative(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"invalid value {text!r} (must be a number, 0 or greater)")
    return value


def _latitude(text: str) -> float:
    return _float_in(text, -90.0, 90.0)


def _longitude(text: str) -> float:
    return _float_in(text, -180.0, 180.0)


def _float_in(text: str, low: float, high: float) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not low <= value <= high:
        raise argparse.ArgumentTypeError(f"invalid value {text!r} (must be between {low:g} and {high:g})")
    return value


def _host_port(text: str) -> tuple[str, int]:
    """HOST, HOST:PORT or [IPv6]:PORT (the rtl_tcp default port is 1234)."""
    text = text.strip()
    port: str | None
    if text.startswith("["):
        host, closed, rest = text[1:].partition("]")
        if not closed or (rest and not rest.startswith(":")):
            raise argparse.ArgumentTypeError(f"invalid host {text!r} (write IPv6 addresses as [address]:port)")
        port = rest[1:] if rest else None
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
    else:
        host, port = text, None  # no port, or a bare IPv6 address
    if not host:
        raise argparse.ArgumentTypeError(f"invalid host {text!r}")
    number = 1234 if port is None else _port(port)
    if number == 0:
        raise argparse.ArgumentTypeError(f"invalid port in {text!r}")
    return host, number


# ------------------------------------------------------------------------------ parsers


def build_run_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adsbrx",
        description=(
            "Receive and decode ADS-B (1090 MHz Mode S) from an RTL-SDR dongle. "
            "Shows a live aircraft list, serves a web map, and re-publishes the messages "
            "in SBS-1 (BaseStation), Beast and raw formats."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"adsbrx {__version__}")

    g = p.add_argument_group("input")
    g.add_argument("--source", choices=SOURCES, help="where the samples come from (default: rtlsdr, or implied by --file / --rtl-tcp)")
    g.add_argument("--device-index", type=int, default=0, metavar="N", help="RTL-SDR dongle number, see 'adsbrx devices' (default: 0)")
    g.add_argument("--gain", type=_gain, default="max", metavar="DB|max|auto", help="tuner gain: a number of dB, 'max' (default) or 'auto'")
    g.add_argument("--ppm", type=int, default=0, help="frequency correction of the dongle in ppm (default: 0)")
    g.add_argument("--agc", action="store_true", help="enable the RTL2832 digital AGC (normally not wanted)")
    g.add_argument("--bias-tee", action="store_true", help="power an LNA through the antenna cable (dongles with a bias tee only)")
    g.add_argument("--dll-dir", metavar="DIR", help="folder containing rtlsdr.dll and libusb-1.0.dll (or set ADSBRX_RTLSDR_DLL_DIR)")
    g.add_argument("--rtl-tcp", type=_host_port, metavar="HOST[:PORT]", help="read samples from an rtl_tcp server (default port: 1234)")
    g.add_argument("--file", metavar="PATH", help="decode an I/Q recording made at 2 MS/s")
    g.add_argument("--format", choices=sorted(FORMATS), default="cu8", help="sample format of --file and stdin (default: cu8)")
    g.add_argument("--loop", action="store_true", help="play --file over and over")
    g.add_argument("--fast", action="store_true", help="play --file and the simulator as fast as possible instead of in real time")
    g.add_argument("--speed", type=_positive, default=1.0, metavar="X", help="real-time playback speed factor (default: 1)")
    g.add_argument("--seed", type=int, default=1, help="random seed of the simulator (default: 1)")
    g.add_argument(
        "--record",
        metavar="FILE",
        help="also save the raw I/Q samples (2 MS/s, unsigned 8-bit: 4 MB per second) to the new file FILE, "
        "to replay with --file; an existing file is never overwritten",
    )

    g = p.add_argument_group("receiver and decoder")
    g.add_argument("--lat", type=_latitude, help="your antenna latitude in degrees (enables distances and single-message position decoding)")
    g.add_argument("--lon", type=_longitude, help="your antenna longitude in degrees")
    g.add_argument(
        "--demod",
        choices=DEMODULATORS,
        default="mlse",
        help="demodulator: mlse (sequence detection and soft-decision repair: decodes many more weak "
        "messages, needs several times the CPU) or classic (light, for slow computers) (default: mlse)",
    )
    fix = g.add_mutually_exclusive_group()
    fix.add_argument("--no-fix", action="store_true", help="do not repair damaged messages")
    fix.add_argument("--aggressive", action="store_true", help="also repair 2-bit errors (more messages, more risk of wrong data)")
    g.add_argument("--mode-s", action="store_true", help="also accept Mode S altitude/identity/ACAS replies (DF0/4/5/16/20/21) from aircraft already heard; default: ADS-B only")
    g.add_argument("--expire", type=_positive, default=60.0, metavar="S", help="forget aircraft not heard for S seconds (default: 60)")
    g.add_argument("--max-range", type=_non_negative, default=300.0, metavar="NM", help="reject positions further than NM from you; 0 = no limit (default: 300)")

    g = p.add_argument_group("outputs (port 0 disables an output)")
    g.add_argument("--bind", default="127.0.0.1", metavar="ADDR", help="address to listen on; use 0.0.0.0 to allow other computers (default: 127.0.0.1)")
    g.add_argument("--web-port", type=_port, default=8080, metavar="PORT", help="web map and JSON (default: 8080)")
    g.add_argument("--sbs-port", type=_port, default=30003, metavar="PORT", help="SBS-1 / BaseStation text (default: 30003)")
    g.add_argument("--beast-port", type=_port, default=30005, metavar="PORT", help="Beast binary (default: 30005)")
    g.add_argument("--raw-port", type=_port, default=30002, metavar="PORT", help="raw AVR text *HEX; (default: 30002)")
    g.add_argument("-i", "--interactive", action="store_true", help="show a live aircraft table in the terminal")
    g.add_argument("--raw", action="store_true", help="print every message as *HEX; on stdout (no table or summaries)")
    g.add_argument("--stats", action="store_true", help="print decoder statistics when finished")

    g = p.add_argument_group("running")
    g.add_argument("--hold", action="store_true", help="keep the outputs running after the input ends (until Ctrl-C)")
    g.add_argument("--duration", type=_positive, metavar="S", help="stop after S seconds of input")
    g.add_argument("-v", "--verbose", action="store_true", help="show debug messages")
    g.add_argument("-q", "--quiet", action="store_true", help="show warnings and errors only")
    return p


def build_simulate_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adsbrx simulate",
        description="Write a synthetic 1090 MHz I/Q recording (2 MS/s, unsigned 8-bit) that can be replayed with 'adsbrx --file'.",
    )
    p.add_argument("-o", "--output", required=True, metavar="FILE", help="file to write")
    p.add_argument("--duration", type=_positive, default=10.0, metavar="S", help="seconds of signal, 4 MB per second (default: 10)")
    p.add_argument("--seed", type=int, default=1, help="random seed (default: 1)")
    p.add_argument("--lat", type=_latitude, help="receiver latitude (default: Tokyo Haneda)")
    p.add_argument("--lon", type=_longitude, help="receiver longitude")
    p.add_argument("--noise", type=_non_negative, default=2.0, metavar="SIGMA", help="receiver noise, standard deviation in 8-bit counts (default: 2)")
    return p


def build_devices_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="adsbrx devices", description="List the RTL-SDR dongles that are plugged in.")
    p.add_argument("--dll-dir", metavar="DIR", help="folder containing rtlsdr.dll and libusb-1.0.dll (or set ADSBRX_RTLSDR_DLL_DIR)")
    return p


def build_decode_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adsbrx decode",
        description="Decode Mode S frames given as hex strings (14 or 28 hex digits, optionally written *8D...;). Prints one JSON object per frame.",
    )
    p.add_argument("frames", nargs="+", metavar="HEX", help="the frames to decode")
    return p


# ---------------------------------------------------------------------------- run command


def build_config(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Config:
    source = args.source
    if args.file is not None:
        if source not in (None, "file"):
            parser.error(f"--file cannot be combined with --source {source}")
        source = "file"
    if args.rtl_tcp is not None:
        if source not in (None, "rtl_tcp"):
            parser.error(f"--rtl-tcp cannot be combined with --source {source}")
        source = "rtl_tcp"
    source = source or "rtlsdr"
    if source == "file" and not args.file:
        parser.error("--source file needs --file PATH")
    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon must be given together")
    if args.interactive and args.raw:
        parser.error("--interactive and --raw cannot be combined")
    if args.record is not None and not args.record.strip():
        parser.error("--record needs a file name")

    ports = {"web": args.web_port, "sbs": args.sbs_port, "beast": args.beast_port, "raw": args.raw_port}
    used = [port for port in ports.values() if port]
    if len(used) != len(set(used)):
        parser.error("the output ports must all be different")

    def port_or_none(port: int) -> int | None:
        return port if port > 0 else None

    host, port = args.rtl_tcp if args.rtl_tcp is not None else ("127.0.0.1", 1234)
    return Config(
        source=source,
        device_index=args.device_index,
        gain=args.gain,
        ppm=args.ppm,
        agc=args.agc,
        bias_tee=args.bias_tee,
        host=host,
        port=port,
        path=args.file,
        fmt=args.format,
        loop=args.loop,
        fast=args.fast,
        speed=args.speed,
        dll_dir=args.dll_dir,
        seed=args.seed,
        record=args.record,
        lat=args.lat,
        lon=args.lon,
        demod=args.demod,
        fix_bits=0 if args.no_fix else 2 if args.aggressive else 1,
        all_df=args.mode_s,
        expire_s=args.expire,
        max_range_nm=args.max_range,
        bind=args.bind,
        web_port=port_or_none(args.web_port),
        sbs_port=port_or_none(args.sbs_port),
        beast_port=port_or_none(args.beast_port),
        raw_port=port_or_none(args.raw_port),
        interactive=args.interactive,
        raw_stdout=args.raw,
        stats=args.stats,
        hold=args.hold,
        duration=args.duration,
    )


class _LogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text if record.levelno < logging.WARNING else f"{record.levelname.lower()}: {text}"


def setup_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logger = logging.getLogger("adsbrx")
    for handler in list(logger.handlers):
        if getattr(handler, "adsbrx_cli", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.adsbrx_cli = True
    handler.setFormatter(_LogFormatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)


def cmd_run(argv: Sequence[str]) -> int:
    parser = build_run_parser()
    args = parser.parse_args(argv)
    if args.verbose and args.quiet:
        parser.error("--verbose and --quiet cannot be combined")
    config = build_config(args, parser)
    setup_logging(args)
    try:
        app = App(config)
        return asyncio.run(app.run())
    except KeyboardInterrupt:
        return 0
    except (AppError, SourceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


# ----------------------------------------------------------------------- other commands


def cmd_simulate(argv: Sequence[str]) -> int:
    from .sim.scenario import HANEDA, SimStream, default_scenario

    parser = build_simulate_parser()
    args = parser.parse_args(argv)
    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon must be given together")
    receiver = (args.lat, args.lon) if args.lat is not None else HANEDA
    scenario = default_scenario(receiver=receiver, seed=args.seed, respawn=True)
    stream = SimStream(scenario, noise_sigma=args.noise, seed=args.seed)
    total = int(args.duration * SAMPLE_RATE)
    done = 0
    try:
        with open(args.output, "wb") as out:
            while done < total:
                count = min(BLOCK_SAMPLES, total - done)
                out.write(stream.read(count))
                done += count
    except OSError as exc:
        print(f"error: cannot write {args.output}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.output}: {done / SAMPLE_RATE:.1f} s, {2 * done / 1e6:.0f} MB, {len(scenario.aircraft)} aircraft")
    print(f"replay it with: adsbrx --file {args.output} --fast --stats --lat {receiver[0]} --lon {receiver[1]}")
    return 0


def cmd_devices(argv: Sequence[str]) -> int:
    from .rtl_native import RtlSdrError, list_devices

    args = build_devices_parser().parse_args(argv)
    try:
        devices = list_devices(dll_dir=args.dll_dir)
    except RtlSdrError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not devices:
        print("No RTL-SDR device found. Plug in the dongle (on Windows, install the WinUSB driver with Zadig).")
        return 1
    print(f"{len(devices)} RTL-SDR device(s):")
    for dev in devices:
        details = ", ".join(x for x in (dev.manufacturer, dev.product, f"serial {dev.serial}" if dev.serial else "") if x)
        print(f"  {dev.index}: {dev.name}" + (f" ({details})" if details else ""))
    return 0


def cmd_decode(argv: Sequence[str]) -> int:
    from . import modes

    args = build_decode_parser().parse_args(argv)
    status = 0
    for token in args.frames:
        text = token.strip().lstrip("*@<").rstrip(";>").strip()
        try:
            raw = bytes.fromhex(text)
        except ValueError:
            print(f"error: not a hex string: {token}", file=sys.stderr)
            status = 1
            continue
        msg = modes.decode(raw)
        if msg is None:
            print(f"error: not a Mode S frame (need 14 or 28 hex digits): {token}", file=sys.stderr)
            status = 1
            continue
        info = msg.to_dict()
        if msg.df in (11, 17, 18):
            info["crc_ok"] = syndrome(raw) == 0
        print(json.dumps(info))
    return status


COMMANDS = {
    "run": cmd_run,
    "simulate": cmd_simulate,
    "devices": cmd_devices,
    "decode": cmd_decode,
}


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in COMMANDS:
        command, rest = argv[0], argv[1:]
    else:
        command, rest = "run", argv
    try:
        return COMMANDS[command](rest)
    except BrokenPipeError:
        return 0
