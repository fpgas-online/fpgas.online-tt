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
  "safe_start": {"state": str}|null,
  "uptime_s": int, "version": str}`. `vid_pid` is the board's USB
  `idVendor:idProduct` and `usb_serial` its USB serial number (the value on
  the board's label), both read from sysfs (`null` if the device is not a USB
  tty). `chip` is what the board told the boot check it carries and `kind`
  follows from it; `kind_reason` says where that came from, or why it is not
  known. `idle_display` is the idle display's file and what it last did
  (below), and `safe_start` what the safe state of a chip board last did
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
- On `kind: fpga` boards, five extra routes list, load and accept designs.
  **Nothing writes to the demo board's filesystem.** Every design is a file on
  the Pi: the packaged demos (`--demos-dir`), the VGA library's entries the
  site pushed (`--library-dir`, below) and visitors' uploads
  (`--uploads-dir`). A name is a demo first, then a library entry, then an
  upload. Loading one sends its bytes over the board's raw
  MicroPython REPL into a buffer in the RP2350's memory and has the SDK's own
  loader clock that buffer into the iCE40, so the board ends in the state the
  SDK's `tt.shuttle.<design>.enable()` leaves, with no file involved. The REPL
  work is a task run through the bridge like any other client, never a second
  owner of the port.

  | Route | Description |
  |-------|-------------|
  | `GET /designs` | `{"enabled": str\|null, "library": {"max_bytes", "used_bytes"}, "designs": [{"name", "title", "author", "description", "docs_url", "repo_url", "clock_hz", "pinout", "source": "demo"\|"library"\|"upload"}, ...]}` — every demo whose `.bin` is on the Pi, with its metadata from `index.json`, every library entry kept here and every upload; a library entry has an upload's placeholders (`""`, `clock_hz` null, `pinout` `{}`) plus `size`, since its metadata is in the site's index; `enabled` is what the board's SDK says is loaded (`null` when the SDK is not running) |
  | `POST /designs/{name}/enable` | body `{"clock_hz": int\|null, "ui_in": int\|null, "ui_in_mask": int}`, every key optional (see "Driving `ui_in`" below) → `{"enabled": name, "clock_hz": int\|null, "ui_in": int\|null, "ui_in_mask": int, "ui_in_driven": [int], "ui_in_held": [int], "ui_in_inputs": [int], "released": [int]}`; if the board's SDK is not running, does what the Commander does in that case (a soft reset from the friendly REPL: Ctrl-C twice, Ctrl-B, Ctrl-D, which runs the board's own `main.py`), then sends the bitstream and loads it; bounded to a 25 s overall REPL deadline (below the site proxy's own 30 s/45 s read timeouts, so a stuck load still gets a clean 502 from this daemon instead of the client seeing a raw connection reset); loading a library entry refreshes its LRU time |
  | `POST /bitstream` | multipart form (`name`, `file`) → `201 {"name", "size", "evicted": [str, ...]}`; kept on the Pi, the board is not involved; rejects names that collide with a demo or a cached library entry (409), non-`[a-z0-9_]{1,40}` names, oversize (>256 KiB) or non-iCE40 files (400); removes the oldest uploads first so at most 16 remain |
  | `GET /library` | `{"max_bytes", "used_bytes", "entries": [{"id", "bin_sha256", "bin_bytes", "used_at"}, ...]}`; never asks the board |
  | `POST /library/{id}` | multipart form (`meta`: `{"id", "bin_sha256", "bin_bytes"}`, at most 4 KiB; `file`, at most 256 KiB) → `201`, or `200` when the same `bin_sha256` is already kept, `{"id", "bin_bytes", "bin_sha256", "evicted": [str, ...], "hides_upload": bool}`; checks the meta's keys and types, that `meta.id` is `{id}`, that `{id}` is not a demo's or `idle_display` (409), and the file's size, SHA-256, size cap and preamble (400); the board is not involved |

  A board that told the check it carries a chip gets `404 {"error": "not an
  fpga board", "detail": why}` on all five, and one the report does not say
  that for yet gets `503 {"error": "board not identified yet", "detail":
  why}`.
  Other error shapes (`{"error": str, "detail": str}`): `503 board
  not present`, `409 another task is running` (or a name taken by a demo,
  the idle display or a library entry), `404 no such design` (including a name that could never be
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
- **The VGA library** (`library.py`) is a cache of prebuilt bitstreams of Tiny
  Tapeout VGA designs, pushed by the
  [site](https://github.com/fpgas-online/fpgas.online-site) with
  `POST /library/{id}` before it asks for a load. The Pi fetches nothing; the
  site gets the index and the bitstreams from the archive and checks them.
  `--library-dir` (default `/var/lib/fpgas-tt/library`, in the same
  `StateDirectory`) holds `<id>.bin` and `<id>.json`
  (`{"id", "bin_sha256", "bin_bytes"}` and nothing else); all other metadata
  is in the site's index. The `.bin` files are capped in total by
  `--library-max-bytes`, whose default is the environment variable
  `FPGAS_TT_LIBRARY_MAX_BYTES`, else 16777216 (16 MiB, about 160 designs);
  infra sets it with a systemd drop-in. A push evicts the least recently used
  entries, by the mtime of their `.json` (a push or a load refreshes it),
  until the total fits. A new build first removes the old `.json`; then
  writes go `.bin` first, then `.json`, each through a
  temporary file and a rename, under one lock; each push first removes what a
  push cut short left (a `.bin` or `.json` alone, `.*.tmp` files), which is
  never listed or counted. The cache never touches the uploads, the demos or
  the idle design, and is lost at a reboot like the uploads; the site pushes
  again.

  Concurrency: two pushes are put in order by the library lock. A push and an
  upload use separate locks and directories, so neither evicts the other's
  files. An upload's check that its name is not a cached library id and a
  push of that same id are not under one lock: if they cross, both files are
  kept and the library entry wins at load time, as for an upload made before
  the site had the index; the push's `hides_upload` says so only when the
  upload was kept before the push answered. A push during a load only writes files; the load has
  already read its bytes, though an enable that arrives during a
  replacement, between the old record's removal and the new record (a
  window of microseconds), gets 404 or loads a hidden upload of the same
  name. An entry evicted between a push and an enable
  makes the enable answer 404, and the site may push again.
- **Driving `ui_in`** (`designs.py`'s `RELEASE_CODE` and `DRIVE_CODE`). The
  daemon never sets `tt.mode`, never stops the project clock and never uses
  the SDK's "MCU controls inputs" mode; it sets the `ui_in` pins directly.
  The enable body's `ui_in_mask` (0 to 255, default 0) names the bits to
  drive and `ui_in` (0 to 255, or null) their values: `ui_in` may have no
  bit outside the mask, a mask other than 0 needs a `ui_in`, and a mask of 0
  needs `ui_in` null. The caller
  always supplies them (the site reads a library entry's from its index); the
  daemon never fills them in. They are only for a VGA library entry: an
  enable of a demo or an upload whose body has either key, even null or 0,
  is refused with `400 ui_in is only for VGA library designs` before the
  board is asked. The `ui_in` pins share nets with `uio` on the Welland HAT,
  so only a mask the harness published may drive them.

  | Pins | Condition | Steps |
  |------|-----------|-------|
  | each bit in `_fo_ui_driven` | `GPIO_OE` shows it driven | latch 0; output for 1 ms; `Pin.IN`; `Pin.PULL_DOWN` |
  | each bit in `_fo_ui_driven` | `GPIO_OE` shows it not driven | `Pin.IN`; `Pin.PULL_DOWN` |
  | each bit outside the mask, not in `_fo_ui_driven` | library load | `Pin.IN`; `Pin.PULL_DOWN` |
  | each mask bit | reads high with the pull-down | `Pin.IN`; `Pin.PULL_DOWN`; listed in `ui_in_held` |
  | each mask bit | reads low with the pull-down | `Pin.IN`; `Pin.PULL_DOWN`; latch to its `ui_in` bit; `Pin.OUT`; listed in `ui_in_driven` |

  The first three rows are the release. It runs on every load, after the
  buffer's hash check and before the FPGA is touched. The 1 ms low discharges
  the pad (RP2350-E9). `GPIO_OE`'s address comes from `rpregs.GPIO_OE_EXPR`.
  A pin released by the other two rows is never made an output. The last two
  rows are the drive. It runs after the load and its clock, only when the load
  succeeded, and only when `ui_in` is given with a mask other than 0. The
  bits it drives become the new `_fo_ui_driven`. On the board, only ints 0 to
  7 of that list are read, so a damaged list cannot stop later loads. A soft
  reset of the board clears the list.

  | Answer key | Lists |
  |------------|-------|
  | `released` | bits a previous load had driven, released before this load |
  | `ui_in_inputs` | on a library load, the bits outside its mask that the daemon had not driven, released straight to inputs (all eight when the mask is 0 and nothing was driven); empty for a demo or upload |
  | `ui_in_driven` | bits now driven |
  | `ui_in_held` | mask bits left as inputs because something held them high |

  A demo or upload load releases only the pins this daemon drove, so pins a
  visitor set through the Commander are left alone; the idle display's own
  load releases them too. A load refused for a damaged buffer leaves the
  running design and its pins as they were; one whose release fails part way
  leaves the running design, and its name, as they were.
- **The display of an FPGA board nobody is using is kept moving**
  (`idle.py`). The boot check ends by streaming a design that animates the
  display from the FPGA's own oscillator, but every start of the board's SDK
  (a Commander that connects to a board without `tt`, or a Run from the page)
  replaces it: SDK 3.1.0 loads `tt_um_factory_test` and, on an FPGA board,
  does not apply the `ui_in = 1` that makes it count and does not drive
  `ui_in`, so the display shows a raw binary count or a still pattern,
  depending on pins nobody sets (the DIP switches, the Pi's Pmod HAT
  lines). The daemon does not look at the display: when no serial client is
  connected and no client, Run or
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
    check leaves it, with the moving design already running. A visitor who
    soft-resets the board at the raw REPL leaves it the same way. The daemon
    says `loaded` for the boot check's state only while nobody has used the
    board through it (no client, Run, upload or library load), the board is
    the one it first opened, the boot check's report
    (`/run/fpgas-online/verify.json`) says it left the idle design's file
    running on this board (`left_running.bitstream`), and that report was
    written no more than 180 s before the daemon started. A daemon that
    `Restart=always` brought back after a visitor's session starts long after
    the report, so it says `left: the SDK is not running`, as it does in
    every other case. A clock stepped forward between the report and the
    daemon's start gives `left` too; one stepped backward can pull a long
    gap into the window. Any start of the daemon within the 180 s that does
    not follow a new report (a crash, `systemctl restart fpgas-tt`,
    `fpgas-verify --test` or `--report` elsewhere) cannot be told from the
    boot's, so a visitor's raw-REPL soft reset in that time reads `loaded`.
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
- **A board with a Tiny Tapeout chip is put into a safe state, in its RAM**
  (`safe.py`; fpgas.online-test-designs issue #181). At every start the
  board's SDK loads the chip's `tt_um_factory_test` with its `config.ini`
  section (`ui_in = 1`, clocked at 10 Hz, the RP2040 driving `ui_in`), and
  with `ui_in[0]` high the factory test drives its counter onto `uio`. The
  Pmod HAT makes one net of each of `ui_in[1:3]` and `uio[1:3]` (HAT JA2-4
  and JB2-4 are the same Pi GPIOs), so the RP2040 and the chip drive against
  each other there. Once each time the board is opened (the daemon's start,
  which follows every boot check, and a board that comes back after it was
  unplugged, reset or power-cycled), after 30 quiet seconds, and only when
  nobody is using it, the daemon stops the project clock (its pin
  released), releases the RP2040's `uio` pins (`uio_oe_pico` 0) and drives
  `ui_in` to 0. It sets `ui_in`'s direction too, not only its value: the
  boot check's wiring test leaves the ui_in pins as inputs with no pull
  (fpgas.online-test-designs issue #196). Like the SDK's own start, it gives
  each ui_in pin the SDK's pull-down and drives it only if it then reads low,
  `ui_in[0]` first; a pin still held high (a DIP switch that is on) is left
  an input and said, and `ui_in[0]` held high is a failure. It does this only
  in the SDK's start state (the factory test enabled, clocked, in
  `ASIC_RP_CONTROL`); any other project or mode, a factory test that is not
  being clocked, an FPGA board and a board without the SDK are left alone,
  and a visitor who came first, or while it waited, has the board as they
  found it. Nothing is written to a file on the board, and the daemon drives
  none of the Pi's GPIOs; the state lasts until the board's SDK starts again
  (a Commander that soft-resets the board). `/health` says what happened, in
  `safe_start.state`: `waiting`, `waiting: <why the board's kind is not
  known>`, `board not present`, `not a chip board`, `left: <why>`, `set:
  <what was changed>`, or `failed: <why>` (tried again after 10 minutes, or
  when the board is opened again).
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
