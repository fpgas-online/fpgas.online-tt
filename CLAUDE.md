## Background

This repo is part of the [fpgas.online](https://fpgas.online) FPGA-as-a-Service
platform. It provides the Pi-side daemon for the Tiny Tapeout front end
(`tinytapeout.fpgas.online`): a single-owner serial bridge to the TT demo
board plus a health endpoint. Design spec:
`fpgas.online-infra/docs/superpowers/specs/2026-08-22-tinytapeout-fpgas-online-design.md`.

## Repository Overview

- `src/fpgas_tt/identity.py` — what the board is, from its USB serial and the boot check's report; `identify()`
- `src/fpgas_tt/usbinfo.py` — USB ids and serial number of the device behind a tty (sysfs)
- `src/fpgas_tt/bridge.py` — `Bridge` (one serial owner) and `Client`
- `src/fpgas_tt/server.py` — aiohttp app (`/health`, `/serial`, and on FPGA boards `/designs`, `/designs/{name}/enable`, `/bitstream`, `/library`, `/library/{id}`) and CLI `main()`
- `src/fpgas_tt/designs.py` — list and load the Pi's designs (a demo first, then a library entry, then an upload) and keep uploads
- `src/fpgas_tt/library.py` — the VGA library's cache in `--library-dir`: `<id>.bin` + `<id>.json` (`id`, `bin_sha256`, `bin_bytes` only), LRU by the `.json`'s mtime under `--library-max-bytes` (env `FPGAS_TT_LIBRARY_MAX_BYTES`, else 16 MiB), orphans removed under `_LIBRARY_LOCK`; imports nothing from `designs`, never touches uploads
- `src/fpgas_tt/idle.py` — the idle display: a moving design streamed into an FPGA board nobody is using
- `src/fpgas_tt/safe.py` — the safe state of a chip board, set in its RAM at the daemon's start (issue #181)
- `tests/` — pytest; a pty stands in for the board
- `nfpm.yaml`, `debian/`, `bin/`, `packaging/deb-version.py` — deb packaging (arch all); rolling release, version from `git describe`, published on every green CI run on `main` (see README.md "Releases (rolling)")

Invariants: exactly one owner of the serial port; every consumer (WebSocket
viewers and future tasks) is a bridge client; no arbitration; slow clients are
dropped, the reader never blocks; nothing is fetched or generated after boot;
nothing says which switch port a board is on (a board is known by its USB
serial, the value on its label); nothing writes to the demo board's files.
The VGA library keeps "nothing is fetched after boot": the Pi fetches nothing,
and library entries arrive only by push from the gateway (`POST /library/{id}`
from the site, over the per-port VLAN that already carries `/bitstream`).

## Conventions

- **Python**: Use `uv` for all Python commands (`uv run`, `uv pip`). Never use bare `python` or `pip`.
- **Dates**: ISO 8601 (YYYY-MM-DD) or day-first. Never month-first.
- **Commits**: small, discrete commits; every change via PR; CI green before merge.
- **License**: Apache 2.0.
- **Linting**: ruff (blocking). Tests: pytest (blocking).
- **No force push**.

## Related Repos

| Repo | Purpose |
|------|---------|
| [fpgas.online-infra](https://github.com/fpgas-online/fpgas.online-infra) | Ansible; bakes this deb into the Pi NFS root |
| [fpgas.online-site](https://github.com/fpgas-online/fpgas.online-site) | Django site (the `ttsite` app proxies to this daemon) |
| [tt-commander-app (fork)](https://github.com/fpgas-online/tt-commander-app) | Web Commander that connects to `/serial` |
| [apt](https://github.com/fpgas-online/apt) | APT repo this deb is published to |
