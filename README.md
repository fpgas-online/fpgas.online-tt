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
- On `kind: fpga` boards, three extra routes list, load and accept designs.
  **Nothing writes to the demo board's filesystem.** Every design is a file on
  the Pi: the packaged demos (`--demos-dir`) and visitors' uploads
  (`--uploads-dir`). Loading one sends its bytes over the board's raw
  MicroPython REPL into a buffer in the RP2350's memory and has the SDK's own
  loader clock that buffer into the iCE40, so the board ends in the state the
  SDK's `tt.shuttle.<design>.enable()` leaves, with no file involved. The REPL
  work is a task run through the bridge like any other client, never a second
  owner of the port.

  | Route | Description |
  |-------|-------------|
  | `GET /designs` | `{"enabled": str\|null, "designs": [{"name", "title", "author", "description", "docs_url", "repo_url", "clock_hz", "pinout", "source": "demo"\|"upload"}, ...]}` — every demo whose `.bin` is on the Pi, with its metadata from `index.json`, and every upload; `enabled` is what the board's SDK says is loaded (`null` when the SDK is not running) |
  | `POST /designs/{name}/enable` | body `{"clock_hz": int}` (optional) → `{"enabled": name, "clock_hz": int\|null}`; starts the SDK with the board's own `main.py` if it is not running, sends the bitstream and loads it; bounded to a 25 s overall REPL deadline (below the site proxy's own 30 s/45 s read timeouts, so a stuck load still gets a clean 502 from this daemon instead of the client seeing a raw connection reset) |
  | `POST /bitstream` | multipart form (`name`, `file`) → `201 {"name", "size", "evicted": [str, ...]}`; kept on the Pi, the board is not involved; rejects names that collide with a demo, non-`[a-z0-9_]{1,40}` names, oversize (>256 KiB) or non-iCE40 files (400); removes the oldest uploads first so at most 16 remain |

  Non-fpga boards get `404 {"error": "not an fpga board", "detail": ""}` on
  all three. Other error shapes (`{"error": str, "detail": str}`): `503 board
  not present`, `409 another task is running` (or a demo-name collision on
  upload), `404 no such design` (including a name that could never be
  valid — rejected before the board is asked), `502 REPL task failed`
  (detail is the board's traceback, ANSI/non-printable bytes stripped and
  truncated), `400` for validation failures, `500 internal error` for
  anything unexpected (logged with a traceback; always JSON, never a bare
  crash page).
- `--demos-dir` (default `/usr/share/fpgas-tt/demos`) points at the demo
  bitstream set (`index.json` + `<name>.bin` files). `--uploads-dir` (default
  `/var/lib/fpgas-tt/uploads`, the service's `StateDirectory`) keeps uploads;
  on the fleet's Pis that is lost at a reboot, like everything else a visitor
  leaves on a Pi.
- Until 2026-10 the daemon copied every demo and every upload to the board's
  `/bitstreams` and loaded from there. Boards from that time still hold those
  files; the daemon neither reads nor removes them.

The KianV boot macro is a later phase; it too will be a task that goes
*through* the bridge as a client — there is only ever one owner of the
serial port.

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
(`nfpm.yaml`, `arch: all`) and uploads it as an asset on the current
**series** GitHub Release.

**Series tags** (`vX.Y`, e.g. `v0.1`) are two-component, enforced by the
repo's tag ruleset, and must be pushed by a maintainer over SSH — the
`GITHUB_TOKEN` cannot create tags via the Releases API. The first series
`v0.0` sits on the root commit. The workflow only creates the release object
(if it does not yet exist) and uploads assets to it; it does not create tags.
The [fpgas-online/apt](https://github.com/fpgas-online/apt) repo enumerates
all releases (every 15 min, or on demand) and pulls any new
`fpgas-online-tt_*.deb` asset into its pool, publishing it to
<https://fpgas-online.github.io/apt>. No tokens are involved on this side.

- `.github/workflows/ci.yml` — lint/test/test-bookworm/deb gates on every
  push and PR. A green run on `main` is what triggers the release.
- `.github/workflows/build-deb.yml` — triggered by `workflow_run` when CI
  completes successfully on `main` (checked out at the SHA CI validated,
  with full history); builds the deb and, on success, uploads it to the
  current series release (`--clobber` makes re-runs of the same version
  idempotent). Also runs on `v*` tag pushes and `workflow_dispatch`.

The version is derived from `git describe` by `packaging/deb-version.py`: a
`vX.Y` tag on `main` gives `X.Y`; each commit after it gives `X.Y.postN`;
with no tag yet, `0.0.post<commit count>`. To start a new series, push an
annotated `vX.Y` tag on `main` over SSH — that becomes the version base; the
workflow then uploads to the corresponding release.

`pyproject.toml`'s static `version = "0.1.0"` is unrelated to the deb
version above — it is only the wheel/series base and is not read by the
release workflow.

`workflow_dispatch` re-runs of `build-deb.yml` are allowed at any time.

## License

Apache-2.0
