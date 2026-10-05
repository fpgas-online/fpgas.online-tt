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
  "vid_pid": str|null, "usb_serial": str|null, "chip": str|null},
  "kind": "fpga"|"other"|"unknown", "kind_reason": str, "clients": int,
  "idle_display": {"design": str, "state": str}|null,
  "uptime_s": int, "version": str}`. `vid_pid` is the board's USB
  `idVendor:idProduct` and `usb_serial` its USB serial number (the value on
  the board's label), both read from sysfs (`null` if the device is not a USB
  tty). `chip` is what the board told the boot check it carries and `kind`
  follows from it; `kind_reason` says where that came from, or why it is not
  known. `idle_display` is the idle display's file and what it last did
  (below).
- On shutdown every open `/serial` socket is closed with code 1001
  (`server shutdown`); on board loss with 1011 (`board disconnected`).
- **What the board is comes from the board, never from where it is plugged
  in.** No file says which Pi or which switch port carries which board, so a
  board moved to another port keeps everything it had. The daemon reads the
  USB serial of the device behind `/dev/ttboard` and looks that board up in the
  boot check's report on the same Pi (`--report`, default
  `/run/fpgas-online/verify.json`, written by fpgas-verify from
  [fpgas.online-test-designs](https://github.com/fpgas-online/fpgas.online-test-designs)).
  The kind is what the board itself told the check it carries (the report's
  `identity.chip`, read from the board's SDK by rpi-hwid): the FPGA breakout
  gives `kind: fpga`, anything else (a Tiny Tapeout chip) gives `other`. Until the report says
  it, the kind is `unknown`: no boot check yet this boot, a board plugged in
  since, or a check that found the board and could not read it (its own reason
  is passed on in `kind_reason`). The report's `variant` is not asked, because
  the check calls every Raspberry Pi USB device `tt-fpga` before reading it
  ([fpgas.online-test-designs issue #124](https://github.com/fpgas-online/fpgas.online-test-designs/issues/124));
  once that is fixed the variant can be believed here. How the check's tests
  went does not change the kind. Both are read at each request, so a boot
  check that finishes after the daemon has started needs no restart (at boot
  `fpgas-verify.service` orders itself `Before=fpgas-tt.service`, in its own
  unit file in fpgas.online-test-designs, so the report is there first). The daemon never asks the
  board what it is: the boot check does that, and a second prober would get in
  a visitor's way. The serial bridge works whatever the kind.
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
  | `POST /designs/{name}/enable` | body `{"clock_hz": int}` (optional) → `{"enabled": name, "clock_hz": int\|null}`; if the board's SDK is not running, does what the Commander does in that case (a soft reset from the friendly REPL: Ctrl-C twice, Ctrl-B, Ctrl-D, which runs the board's own `main.py`), then sends the bitstream and loads it; bounded to a 25 s overall REPL deadline (below the site proxy's own 30 s/45 s read timeouts, so a stuck load still gets a clean 502 from this daemon instead of the client seeing a raw connection reset) |
  | `POST /bitstream` | multipart form (`name`, `file`) → `201 {"name", "size", "evicted": [str, ...]}`; kept on the Pi, the board is not involved; rejects names that collide with a demo, non-`[a-z0-9_]{1,40}` names, oversize (>256 KiB) or non-iCE40 files (400); removes the oldest uploads first so at most 16 remain |

  A board that told the check it carries a chip gets `404 {"error": "not an
  fpga board", "detail": why}` on all three, and one the report does not say
  that for yet gets `503 {"error": "board not identified yet", "detail":
  why}`.
  Other error shapes (`{"error": str, "detail": str}`): `503 board
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
- **The display of an FPGA board nobody is using is kept moving**
  (`idle.py`). The boot check ends by streaming a design that animates the
  display from the FPGA's own oscillator, but every start of the board's SDK
  (a Commander that connects to a board without `tt`, or a Run from the page)
  replaces it: SDK 3.1.0 loads `tt_um_factory_test` and, on an FPGA board,
  does not apply the `ui_in = 1` that makes it count, so the display is a
  still pattern. When no serial client is connected and no client, Run or
  upload has happened for `--idle-after` seconds (default 60), the daemon
  asks the board once what it has loaded (once more when
  `--idle-replace-after` is reached), and if that is the SDK's start
  state it streams the boot check's design again (`--idle-design`, default
  `/usr/share/fpgas-online/tt-fpga/bitstreams/tt-display-tt-fpga/tt_fpga_platform.bin`
  from `fpgas-online-tt-fpga-bitstreams`; empty for no idle display), the way
  a Run loads a design, under the name `idle_display`. Nothing is written to
  the board, and no pin or mode of the SDK is changed: the design ignores its
  clock, reset and inputs.
  - Nothing is typed at the board while a serial client is connected: while a
    client is there the board is the visitor's, a still display included.
  - A serial client that connects while the daemon is asking the board or
    streaming the idle design is accepted and held, not bridged, until the
    daemon has finished or given up. If it was only asking, it finishes the
    question (a moment; five seconds at most) and streams nothing; a load
    takes a few seconds, and the daemon keeps the board twenty seconds at
    most in all. So a visitor never shares the REPL with the daemon's own
    load and never sees its bytes. A Run or a design list that arrives then
    waits the same way, and what a Run waited comes out of its 25 s
    deadline, the start of the SDK included; a Run left with less than 8 s
    of it answers `409 another task is running` at once.
  - A board that was unplugged, reset or power-cycled starts a new quiet
    time; so does a load that failed (ten minutes).
  - A board without the SDK's `tt` object is left alone: that is how the boot
    check leaves it, with the moving design already running.
  - A design a visitor loaded is never replaced, unless
    `--idle-replace-after SECONDS` is given. The daemon cannot see a visitor
    who is watching the camera or working on the Pi, so that option is for
    when the site's own end of a visitor's session can be tied to it.
  - A board the report does not say carries the FPGA breakout is never
    touched.
  - `/health` says what happened, in `idle_display.state`: `waiting`,
    `in use`, `loaded`, `left: <a visitor's design>`, `left: the SDK is not
    running`, `board not present`, `not an fpga board`, `file missing`,
    `file is not an iCE40 bitstream`, or `failed: <why>`. A design name that
    is not one a design here could have is said as `a design`. A missing
    file is also logged once; the file is looked for again at each quiet
    time, so a root that gains it needs no restart.
- Until 2026-10 the daemon copied every demo and every upload to the board's
  `/bitstreams` and loaded from there. Boards from that time still hold those
  files; the daemon neither reads nor removes them.
  The SDK on the board does not know about any of this: its own list
  (`tt.shuttle.projects`) is still the board's `/bitstreams`, so
  `tt.shuttle.<name>.enable()` typed at the board's prompt loads the board's old
  copy of that name, not the Pi's, and uploads are not in that list at all. The
  design the daemon loaded is `tt.shuttle.enabled` (project index -1, in no
  list); calling `.enable()` on it again at the prompt loads nothing, because
  it has no file on the board.
- A load checks the buffer on the board against the Pi's SHA-256 before the
  FPGA is touched, and a load that fails, or in which the SDK's loader did not
  read every byte, leaves `enabled` as `null`, never the name of a design that
  is not running.

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
uv run fpgas-tt --device /dev/ttyACM0 --report my-verify.json
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
