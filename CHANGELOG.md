# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-17

First public release.

### Added

- Receiver for 1090 MHz ADS-B (Mode S extended squitter) with an RTL-SDR dongle, a `rtl_tcp` server, an IQ file (cu8 / cs8 / cs16), standard input, or the built-in signal simulator. `librtlsdr` is loaded at run time with `ctypes`.
- Two demodulators: `mlse` (default: Viterbi sequence detection with soft-decision CRC repair, decodes many more weak messages) and the lighter `classic` (`--demod classic`).
- Mode S / ADS-B decoding: identification, airborne and surface position (CPR, global and local), altitude, speed, heading, vertical rate, squawk and emergency state; DF0/4/5/16/20/21 replies with `--mode-s`.
- Web UI with a map, aircraft table, detail panel, trails, range rings, received signal level and noise level, unit switch, and a Japanese / English language switch. Leaflet is bundled.
- Outputs: dump1090-compatible JSON, SBS-1 (30003), Beast (30005) and raw AVR (30002).
- Terminal aircraft table (`-i`), `--stats`, and `--record FILE` to save the raw IQ while receiving.
- Tools for testing without hardware: `adsbrx simulate`, `adsbrx decode`, and a weak-signal benchmark (`python -m adsbrx.sim.bench`).
- Automated tests (about 485), run by GitHub Actions on Linux, Windows and macOS with Python 3.10–3.14.

[0.1.0]: https://github.com/fractional-n/adsbrx/releases/tag/v0.1.0
