"""The command line: argument handling, the helper commands, and complete runs through `main()`."""

import asyncio
import json
import logging
import re
import socket
import subprocess
import sys

import pytest

from adsbrx import __version__, cli
from adsbrx.cli import build_config, build_run_parser, main, setup_logging
from adsbrx.rtl_native import DeviceInfo, RtlSdrError

QUIET_PORTS = ["--web-port", "0", "--sbs-port", "0", "--beast-port", "0", "--raw-port", "0"]
KLM = "8D4840D6202CC371C32CE0576098"  # identification: KLM1023
VELOCITY = "8D485020994409940838175B284F"
AVR_LINE = re.compile(r"\*(?:[0-9A-F]{14}|[0-9A-F]{28});")


@pytest.fixture(autouse=True)
def restore_logging():
    yield
    logger = logging.getLogger("adsbrx")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)


def parse(*argv: str):
    parser = build_run_parser()
    return build_config(parser.parse_args(list(argv)), parser)


def rejected(capsys, *argv: str) -> str:
    """Runs the parser on invalid arguments; returns what it complained about."""
    with pytest.raises(SystemExit) as exc:
        parse(*argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


# ------------------------------------------------------------------- argument handling


def test_defaults_describe_a_local_dongle_with_all_outputs():
    cfg = parse()
    assert cfg.source == "rtlsdr"
    assert (cfg.device_index, cfg.gain, cfg.ppm, cfg.agc, cfg.bias_tee) == (0, "max", 0, False, False)
    assert (cfg.web_port, cfg.sbs_port, cfg.beast_port, cfg.raw_port) == (8080, 30003, 30005, 30002)
    assert cfg.bind == "127.0.0.1"  # nothing is offered to the network unless asked
    assert (cfg.lat, cfg.lon) == (None, None)
    assert cfg.fix_bits == 1 and cfg.all_df is False
    assert cfg.demod == "mlse"  # decodes many more weak messages; --demod classic is the light alternative
    assert (cfg.expire_s, cfg.max_range_nm) == (60.0, 300.0)
    assert (cfg.interactive, cfg.raw_stdout, cfg.stats, cfg.hold, cfg.duration) == (False, False, False, False, None)


def test_the_input_is_implied_by_file_and_rtl_tcp():
    cfg = parse("--file", "capture.cs16", "--format", "cs16", "--loop", "--speed", "2")
    assert (cfg.source, cfg.path, cfg.fmt, cfg.loop, cfg.speed) == ("file", "capture.cs16", "cs16", True, 2.0)
    assert parse("--file", "x", "--source", "file").source == "file"
    assert parse("--source", "sim", "--seed", "7", "--fast").seed == 7
    assert parse("--source", "stdin", "--format", "cs8").source == "stdin"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("192.168.1.20", ("192.168.1.20", 1234)),
        ("radio.local:5555", ("radio.local", 5555)),
        ("[::1]:4000", ("::1", 4000)),
        ("[fe80::1]", ("fe80::1", 1234)),
        ("fe80::1", ("fe80::1", 1234)),  # a bare IPv6 address uses the default port
    ],
)
def test_rtl_tcp_address_forms(text, expected):
    cfg = parse("--rtl-tcp", text)
    assert cfg.source == "rtl_tcp"
    assert (cfg.host, cfg.port) == expected


@pytest.mark.parametrize("text", [":1234", "host:", "host:0", "host:70000", "host:abc", "[::1", "[::1]x", "[::1]:", "[]:80"])
def test_bad_rtl_tcp_addresses_are_rejected(capsys, text):
    assert "--rtl-tcp" in rejected(capsys, "--rtl-tcp", text)


@pytest.mark.parametrize(
    "text, expected",
    [("max", "max"), ("AUTO", "auto"), ("37.2", 37.2), ("0", 0.0), (" 49.6 ", 49.6)],
)
def test_gain_forms(text, expected):
    assert parse("--gain", text).gain == expected


@pytest.mark.parametrize("text", ["loud", "-1", "nan", "inf", ""])
def test_bad_gains_are_rejected(capsys, text):
    assert "--gain" in rejected(capsys, "--gain", text)


def test_port_zero_disables_an_output_and_may_repeat():
    cfg = parse("--web-port", "0", "--sbs-port", "0", "--beast-port", "0")
    assert (cfg.web_port, cfg.sbs_port, cfg.beast_port, cfg.raw_port) == (None, None, None, 30002)
    cfg = parse("--web-port", "9000", "--bind", "0.0.0.0")
    assert (cfg.web_port, cfg.bind) == (9000, "0.0.0.0")


def test_decoder_options():
    assert parse().fix_bits == 1
    assert parse("--no-fix").fix_bits == 0
    assert parse("--aggressive").fix_bits == 2
    cfg = parse("--mode-s", "--expire", "30", "--max-range", "0", "--lat", "35.5", "--lon", "-139.7")
    assert cfg.all_df is True
    assert (cfg.expire_s, cfg.max_range_nm, cfg.lat, cfg.lon) == (30.0, 0.0, 35.5, -139.7)


def test_the_demodulator_can_be_chosen():
    assert parse("--demod", "classic").demod == "classic"
    cfg = parse("--demod", "mlse", "--aggressive")
    assert (cfg.demod, cfg.fix_bits) == ("mlse", 2)
    assert parse("--demod", "mlse", "--no-fix").fix_bits == 0


def test_dongle_options():
    cfg = parse("--device-index", "2", "--ppm", "-4", "--agc", "--bias-tee", "--dll-dir", "C:\\sdr", "--gain", "30")
    assert (cfg.device_index, cfg.ppm, cfg.agc, cfg.bias_tee, cfg.dll_dir, cfg.gain) == (2, -4, True, True, "C:\\sdr", 30.0)


def test_recording_is_opt_in():
    assert parse().record is None
    assert parse("--record", "capture.cu8").record == "capture.cu8"
    cfg = parse("--source", "sim", "--fast", "--record", "x.cu8")
    assert (cfg.source, cfg.record) == ("sim", "x.cu8")


@pytest.mark.parametrize(
    "argv, complaint",
    [
        (["--file", "x", "--source", "sim"], "--file cannot be combined"),
        (["--rtl-tcp", "h", "--source", "file"], "--rtl-tcp cannot be combined"),
        (["--file", "x", "--rtl-tcp", "h"], "--rtl-tcp cannot be combined"),
        (["--source", "file"], "--source file needs --file"),
        (["--lat", "35"], "--lat and --lon must be given together"),
        (["--lon", "139"], "--lat and --lon must be given together"),
        (["-i", "--raw"], "cannot be combined"),
        (["--no-fix", "--aggressive"], "not allowed with"),
        (["--web-port", "9000", "--sbs-port", "9000"], "ports must all be different"),
        (["--lat", "95", "--lon", "0"], "--lat"),
        (["--lon", "181", "--lat", "0"], "--lon"),
        (["--speed", "0"], "--speed"),
        (["--expire", "-5"], "--expire"),
        (["--duration", "0"], "--duration"),
        (["--max-range", "-1"], "--max-range"),
        (["--web-port", "70000"], "--web-port"),
        (["--format", "wav"], "--format"),
        (["--source", "radio"], "--source"),
        (["--demod", "fancy"], "--demod"),
        (["--record", ""], "--record needs a file name"),
        (["--record", "  "], "--record needs a file name"),
    ],
)
def test_conflicting_or_invalid_options_are_rejected(capsys, argv, complaint):
    assert complaint in rejected(capsys, *argv)


# ------------------------------------------------------------------------ small commands


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"adsbrx {__version__}"


def test_help_has_examples_and_lists_the_other_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["-h"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "examples:" in text and "--source sim" in text and "--record" in text
    for command in ("simulate", "devices", "decode"):
        assert command in text
    assert text.isascii()  # old Windows consoles (cp932) must be able to print it


def test_decode_prints_json_and_checks_the_crc(capsys):
    assert main(["decode", KLM, f"*{VELOCITY};"]) == 0
    first, second = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert first["callsign"] == "KLM1023" and first["icao"] == "4840D6" and first["df"] == 17
    assert first["crc_ok"] is True
    assert second["kind"] == "velocity" and second["crc_ok"] is True
    assert second["gs"] == pytest.approx(159.2, abs=0.1)
    assert second["track"] == pytest.approx(182.9, abs=0.1)
    assert second["vrate"] == -832


def test_decode_flags_a_damaged_frame(capsys):
    damaged = KLM[:-1] + "9"
    assert main(["decode", damaged]) == 0
    assert json.loads(capsys.readouterr().out)["crc_ok"] is False


def test_decode_reports_bad_input_but_keeps_going(capsys):
    assert main(["decode", "zz", "8D4840D6", KLM]) == 1
    captured = capsys.readouterr()
    assert "not a hex string: zz" in captured.err
    assert "not a Mode S frame (need 14 or 28 hex digits): 8D4840D6" in captured.err
    assert json.loads(captured.out)["callsign"] == "KLM1023"


def test_decode_without_frames_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["decode"])
    assert exc.value.code == 2


def test_devices_lists_the_dongles(monkeypatch, capsys):
    found = [DeviceInfo(0, "Generic RTL2832U OEM", "Realtek", "RTL2838UHIDIR", "00000001"), DeviceInfo(1, "Other", "", "", "")]
    seen = {}

    def fake_list(lib=None, dll_dir=None):
        seen["dll_dir"] = dll_dir
        return found

    monkeypatch.setattr("adsbrx.rtl_native.list_devices", fake_list)
    assert main(["devices", "--dll-dir", "C:\\sdr"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "2 RTL-SDR device(s):"
    assert lines[1] == "  0: Generic RTL2832U OEM (Realtek, RTL2838UHIDIR, serial 00000001)"
    assert lines[2] == "  1: Other"
    assert seen["dll_dir"] == "C:\\sdr"


def test_devices_without_a_dongle_explains_what_to_do(monkeypatch, capsys):
    monkeypatch.setattr("adsbrx.rtl_native.list_devices", lambda lib=None, dll_dir=None: [])
    assert main(["devices"]) == 1
    assert "WinUSB" in capsys.readouterr().out


def test_devices_reports_a_missing_library(monkeypatch, capsys):
    def missing(lib=None, dll_dir=None):
        raise RtlSdrError("librtlsdr was not found.")

    monkeypatch.setattr("adsbrx.rtl_native.list_devices", missing)
    assert main(["devices"]) == 1
    assert capsys.readouterr().err.strip() == "error: librtlsdr was not found."


# ---------------------------------------------------------------------------- simulate


def test_simulate_writes_a_recording_that_can_be_replayed(tmp_path, capsys):
    recording = tmp_path / "traffic.cu8"
    assert main(["simulate", "-o", str(recording), "--duration", "2"]) == 0
    text = capsys.readouterr().out
    assert recording.stat().st_size == 2 * 2_000_000 * 2  # 2 s of 2 MS/s, 2 bytes per sample
    assert "12 aircraft" in text and "--file" in text

    assert main(["--file", str(recording), "--fast", "--stats", "--lat", "35.5494", "--lon", "139.7798", *QUIET_PORTS]) == 0
    out = capsys.readouterr().out
    assert "Valid frames" in out
    match = re.search(r"Aircraft now\s+: (\d+) \((\d+) with position\)", out)
    assert match and int(match.group(1)) >= 8 and int(match.group(2)) >= 8


def test_simulate_is_reproducible(tmp_path):
    first, second = tmp_path / "a.cu8", tmp_path / "b.cu8"
    for path in (first, second):
        assert main(["simulate", "-o", str(path), "--duration", "0.5", "--seed", "5"]) == 0
    assert first.read_bytes() == second.read_bytes()


def test_simulate_needs_an_output_file_and_a_complete_position(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["simulate"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["simulate", "-o", str(tmp_path / "x"), "--lat", "10"])
    assert exc.value.code == 2
    assert "--lat and --lon must be given together" in capsys.readouterr().err


def test_simulate_reports_an_unwritable_file(tmp_path, capsys):
    assert main(["simulate", "-o", str(tmp_path / "no" / "such" / "dir.cu8"), "--duration", "0.1"]) == 1
    assert capsys.readouterr().err.startswith("error: cannot write")


# ---------------------------------------------------------------------------------- run


def test_a_simulated_run_prints_summaries_and_statistics(capsys):
    assert main(["--source", "sim", "--fast", "--duration", "3", "--stats", *QUIET_PORTS]) == 0
    captured = capsys.readouterr()
    assert "input: built-in signal simulator" in captured.err
    assert re.search(r"aircraft \d+ \(\d+ with position\)", captured.out)
    assert "Valid frames" in captured.out
    assert "soft-decision-corrected" in captured.out and "phase-corrected" not in captured.out  # the sequence detector runs by default


def test_a_simulated_run_with_the_classic_demodulator(capsys):
    assert main(["--source", "sim", "--fast", "--duration", "3", "--stats", "--demod", "classic", *QUIET_PORTS]) == 0
    captured = capsys.readouterr()
    assert re.search(r"aircraft \d+ \(\d+ with position\)", captured.out)
    assert "Valid frames" in captured.out and "phase-corrected" in captured.out
    assert "soft-decision-corrected" not in captured.out


def test_raw_mode_keeps_stdout_clean(tmp_path, capsys):
    recording = tmp_path / "traffic.cu8"
    assert main(["simulate", "-o", str(recording), "--duration", "1"]) == 0
    capsys.readouterr()

    assert main(["--file", str(recording), "--fast", "--raw", *QUIET_PORTS]) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) > 20 and all(AVR_LINE.fullmatch(line) for line in lines)
    assert "input: file" in captured.err


def test_run_can_be_named_explicitly(capsys):
    assert main(["run", "--source", "sim", "--fast", "--duration", "1", *QUIET_PORTS]) == 0
    assert "with position" in capsys.readouterr().out


def test_a_run_saves_its_samples_for_a_later_replay(tmp_path, capsys):
    path = tmp_path / "rec.cu8"
    assert main(["--source", "sim", "--fast", "--duration", "2", "--record", str(path), *QUIET_PORTS]) == 0
    captured = capsys.readouterr()
    assert f"recording the raw I/Q samples to {path}" in captured.err
    assert re.search(r"recorded \d+ MB \(\d+ s of samples\) to ", captured.err)
    size = path.stat().st_size
    assert size >= 2 * 2_000_000 * 2 and size % (2 * 131072) == 0  # at least 2 s, whole blocks

    assert main(["--file", str(path), "--fast", "--stats", *QUIET_PORTS]) == 0
    assert "Valid frames" in capsys.readouterr().out

    assert main(["--source", "sim", "--fast", "--duration", "1", "--record", str(path), *QUIET_PORTS]) == 1
    assert "already exists" in capsys.readouterr().err
    assert path.stat().st_size == size  # the earlier recording is untouched


def test_recording_cannot_overwrite_the_file_being_replayed(tmp_path, capsys):
    path = tmp_path / "traffic.cu8"
    assert main(["simulate", "-o", str(path), "--duration", "0.5"]) == 0
    capsys.readouterr()
    before = path.read_bytes()
    assert main(["--file", str(path), "--fast", "--record", str(path), *QUIET_PORTS]) == 1
    assert "error: cannot record to" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_an_unusable_recording_path_is_a_clean_error(tmp_path, capsys):
    path = tmp_path / "no" / "such" / "dir" / "rec.cu8"
    assert main(["--source", "sim", "--fast", "--duration", "1", "--record", str(path), *QUIET_PORTS]) == 1
    err = capsys.readouterr().err
    assert "error: cannot record to" in err and "Traceback" not in err


def test_a_missing_recording_is_a_clean_error(tmp_path, capsys):
    assert main(["--file", str(tmp_path / "nope.cu8"), *QUIET_PORTS]) == 1
    assert "error: cannot open" in capsys.readouterr().err


def test_a_port_in_use_is_a_clean_error(capsys):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        assert main(["--source", "sim", "--fast", "--duration", "1", "--sbs-port", str(port),
                     "--web-port", "0", "--beast-port", "0", "--raw-port", "0"]) == 1
    err = capsys.readouterr().err
    assert f"error: cannot listen for SBS on 127.0.0.1:{port}" in err
    assert "Traceback" not in err


def test_ctrl_c_ends_the_program_quietly(monkeypatch):
    def interrupt(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", interrupt)
    assert main(["--source", "sim", "--fast", *QUIET_PORTS]) == 0


def test_a_closed_pipe_ends_the_program_quietly(monkeypatch):
    def broken(argv):
        raise BrokenPipeError

    monkeypatch.setitem(cli.COMMANDS, "decode", broken)
    assert main(["decode", KLM]) == 0


def test_verbosity_options_set_the_log_level(capsys):
    levels = {}
    for flag, expected in (("-v", logging.DEBUG), ("-q", logging.WARNING), (None, logging.INFO)):
        argv = ["--source", "sim", "--fast", "--duration", "1", *QUIET_PORTS] + ([flag] if flag else [])
        assert main(argv) == 0
        levels[flag] = logging.getLogger("adsbrx").level
        err = capsys.readouterr().err
        assert ("input: built-in signal simulator" in err) == (expected <= logging.INFO)
    assert levels == {"-v": logging.DEBUG, "-q": logging.WARNING, None: logging.INFO}


def test_verbose_and_quiet_conflict(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["-v", "-q", "--source", "sim"])
    assert exc.value.code == 2
    assert "--verbose and --quiet" in capsys.readouterr().err


def test_log_setup_is_idempotent_and_marks_problems(capsys):
    args = build_run_parser().parse_args([])
    setup_logging(args)
    setup_logging(args)
    logger = logging.getLogger("adsbrx")
    assert len([h for h in logger.handlers if getattr(h, "adsbrx_cli", False)]) == 1
    logger.info("plain note")
    logger.warning("something odd")
    logger.error("something broke")
    assert capsys.readouterr().err.splitlines() == ["plain note", "warning: something odd", "error: something broke"]


def test_python_dash_m_works():
    done = subprocess.run([sys.executable, "-m", "adsbrx", "--version"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0
    assert done.stdout.strip() == f"adsbrx {__version__}"
