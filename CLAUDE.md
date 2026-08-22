## Background

This repo is part of the [fpgas.online](https://fpgas.online) FPGA-as-a-Service
platform. It provides the Pi-side daemon for the Tiny Tapeout front end
(`tinytapeout.fpgas.online`): a single-owner serial bridge to the TT demo
board plus a health endpoint. Design spec:
`fpgas.online-infra/docs/superpowers/specs/2026-08-22-tinytapeout-fpgas-online-design.md`.

## Repository Overview

- `src/fpgas_tt/config.py` — hostname → (switch, port); boards YAML; `discover()`
- `src/fpgas_tt/bridge.py` — `Bridge` (one serial owner) and `Client`
- `src/fpgas_tt/server.py` — aiohttp app (`/health`, `/serial`) and CLI `main()`
- `tests/` — pytest; a pty stands in for the board
- `nfpm.yaml`, `debian/`, `bin/`, `packaging/deb-version.py` — deb packaging (arch all); rolling release, version from `git describe`, published on every green CI run on `main` (see README.md "Releases (rolling)")

Invariants: exactly one owner of the serial port; every consumer (WebSocket
viewers and future tasks) is a bridge client; no arbitration; slow clients are
dropped, the reader never blocks; nothing is fetched or generated after boot.

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
