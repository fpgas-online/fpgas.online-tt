# fpgas.online-tt

Pi-side daemon that owns a Tiny Tapeout demo board's USB serial port and
exposes it to the [tinytapeout.fpgas.online](https://tinytapeout.fpgas.online)
web front end as a fan-out WebSocket bridge.

Part of the [fpgas.online](https://fpgas.online) platform. Design:
`fpgas.online-infra/docs/superpowers/specs/2026-08-22-tinytapeout-fpgas-online-design.md`.

## What it does

- Opens `/dev/ttboard` (udev symlink for the demo board's RP2040/RP2350 USB-CDC
  port) at 115200 baud and keeps retrying every second until a board appears.
- `WS /serial` — every connected client receives the same bytes from the board
  and may write bytes to it. No locking. A client that falls more than 256 KiB
  behind is dropped; the board reader is never blocked.
- `GET /health` — `{"board": {"present": bool, "device": str,
  "vid_pid": str|null}, "kind": str, "slug": str, "switch": int|null,
  "port": int|null, "hostname": str, "clients": int, "uptime_s": int,
  "version": str, "config_error": str|null}`. `vid_pid` is the board's USB
  `idVendor:idProduct` read from sysfs (`null` if the device is not a USB
  tty); `config_error` is non-null when the board map was unreadable and the
  daemon fell back to a plain `asic` bridge instead of restart-looping.
- On shutdown every open `/serial` socket is closed with code 1001
  (`server shutdown`); on board loss with 1011 (`board disconnected`).
- Discovers which board it is from its hostname (`pi-sw<switch>-p<port>`) and
  `/etc/fpgas-online/tt-boards.yaml` (baked into the Pi NFS root by
  fpgas.online-infra). Unknown hostname ⇒ plain `asic` bridge.

Later phases add bitstream upload / design listing (FPGA boards) and the
KianV boot macro; they are tasks that go *through* the bridge as clients —
there is only ever one owner of the serial port.

## Install (on the Pi NFS root — done by fpgas.online-infra)

```bash
apt install fpgas-online-tt
systemctl enable fpgas-tt.service
```

Listens on `0.0.0.0:8765`; only the gateway can reach it (per-port VLANs).

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run fpgas-tt --device /dev/ttyACM0 --boards tests/data/tt-boards.yaml --hostname pi-sw1-p6
```

Tests use a pseudo-terminal as a fake board; no hardware needed.

## Releases (rolling)

This project is a **rolling release**. There are no manual version bumps:
every green CI run on `main` builds `fpgas-online-tt_<version>_all.deb`
(`nfpm.yaml`, `arch: all`) and uploads it as an asset on the rolling
`debs` GitHub Release. The [fpgas-online/apt](https://github.com/fpgas-online/apt)
repo polls that release (every 15 min, or on demand) and pulls any new
asset into its pool, publishing it to <https://fpgas-online.github.io/apt>.
No tokens are involved on this side — the upload uses the workflow's own
`GITHUB_TOKEN`.

- `.github/workflows/ci.yml` — lint/test/test-bookworm/deb gates on every
  push and PR. A green run on `main` is what triggers the release.
- `.github/workflows/build-deb.yml` — triggered by `workflow_run` when CI
  completes successfully on `main` (checked out at the SHA CI validated,
  with full history); builds the deb and, on success, uploads it to the
  `debs` release (created on first use, pinned to the repo's root commit,
  never moved; `--clobber` makes re-runs of the same version idempotent).
  Also runs on `v*` tag pushes and `workflow_dispatch`.

The version is derived from `git describe` by `packaging/deb-version.py`: a
`vX.Y.Z` tag on `main` gives `X.Y.Z`; each commit after it gives
`X.Y.Z.postN`; with no tag yet, `0.0.post<commit count>`. To start a new
series, push an annotated `vX.Y.Z` tag on `main`. (The `debs` tag itself is
a release marker, not a version tag, and `deb-version.py` only matches
`v[0-9]*`, so it never affects the computed version.)

`pyproject.toml`'s static `version = "0.1.0"` is unrelated to the deb
version above — it is only the wheel/series base and is not read by the
release workflow.

`workflow_dispatch` re-runs of `build-deb.yml` are allowed at any time.

## License

Apache-2.0
