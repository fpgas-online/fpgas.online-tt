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
- `GET /health` — `{"board": {"present": bool, "device": str}, "kind": str,
  "slug": str, "switch": int|null, "port": int|null, "hostname": str,
  "clients": int, "uptime_s": int, "version": str}`.
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

## Packaging

`nfpm.yaml` builds an `arch: all` deb in CI (`.github/workflows/build-deb.yml`)
on `v*` tags and publishes it to <https://fpgas-online.github.io/apt>.

## License

Apache-2.0
