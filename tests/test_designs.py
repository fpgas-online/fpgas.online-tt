import ast
import asyncio
import os
import re
from pathlib import Path

import pytest

from fpgas_tt import designs, library, rpregs
from fpgas_tt.bridge import Bridge
from fpgas_tt.designs import ValidationError, validate_bitstream
from fpgas_tt.repl import ReplError, ReplRunner
from tests.test_library import ENTRIES

DEMOS = Path(__file__).parent / "data" / "demos"
PRE = b"\x7e\xaa\x99\x7e"
# What an enable answers besides its name and clock when it was given no ui_in and released nothing.
NO_UI_IN = {"ui_in": None, "ui_in_mask": 0, "ui_in_driven": [], "ui_in_held": [], "ui_in_inputs": [], "released": []}


async def wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest.fixture
async def runner(fake_board, fake_repl):
    b = Bridge(str(fake_board.path), reopen_interval=0.05)
    await b.start()
    await wait_for(lambda: b.present)
    yield ReplRunner(b)
    await b.stop()


def board_tree(fake_repl) -> dict[str, bytes | None]:
    """Everything on the fake board's filesystem: {path: contents, or None for a directory}."""
    root = fake_repl.root
    return {str(f.relative_to(root)): (f.read_bytes() if f.is_file() else None) for f in sorted(root.rglob("*"))}


@pytest.fixture
def uploads(tmp_path) -> Path:
    return tmp_path / "pi-uploads"


def test_demo_index_loads_and_missing_dir_is_empty(tmp_path):
    idx = designs.load_demo_index(DEMOS)
    assert set(idx) == {"tt_um_demo_a", "tt_um_demo_b"}
    assert idx["tt_um_demo_a"]["clock_hz"] == 1000
    assert designs.load_demo_index(tmp_path / "nope") == {}


def test_load_demo_index_handles_corrupt_json(tmp_path):
    (tmp_path / "index.json").write_text("{not valid json")
    assert designs.load_demo_index(tmp_path) == {}


def test_load_demo_index_drops_entries_with_invalid_names(tmp_path):
    (tmp_path / "index.json").write_text(
        '{"demos": [{"name": "ok_name"}, {"name": "Bad Name"}, '
        '{"name": "../etc/passwd"}, {"nope": "no name field"}, "not even a dict"]}'
    )
    assert set(designs.load_demo_index(tmp_path)) == {"ok_name"}


def test_parse_json_raises_replerror_on_garbage():
    with pytest.raises(ReplError):
        designs._parse_json("this is not json")


def test_a_name_with_a_trailing_newline_is_not_a_name():
    assert designs.NAME_RE.match("abc") and not designs.NAME_RE.match("abc\n")


@pytest.mark.parametrize(
    "name,data,status,msg",
    [
        ("Bad Name", PRE + b"x" * 10, 400, "name"),
        ("a" * 41, PRE + b"x" * 10, 400, "name"),
        ("ok_name", b"\x00" * 70 + PRE, 400, "preamble"),
        ("ok_name", PRE + b"x" * (256 * 1024), 400, "large"),
        ("tt_um_demo_a", PRE + b"x" * 10, 409, "demo"),
    ],
)
def test_validate_bitstream_rejects(name, data, status, msg):
    with pytest.raises(ValidationError) as ei:
        validate_bitstream(name, data, {"tt_um_demo_a"})
    assert ei.value.status == status
    assert msg in str(ei.value).lower()


def test_validate_bitstream_accepts_preamble_anywhere_in_first_64_bytes():
    validate_bitstream("my_design", b"\xff" * 60 + PRE + b"\x00" * 100, set())


# -- the Pi's own store: no board involved --


def test_design_names_are_the_demos_on_the_pi_and_the_uploads(tmp_path, uploads, caplog):
    assert designs.design_names(DEMOS, uploads) == ["tt_um_demo_a", "tt_um_demo_b"]  # no uploads directory yet
    designs.store_upload(uploads, "my_upload", PRE + b"u")
    (uploads / "Not A Name.bin").write_bytes(PRE)  # put there by hand: never offered
    assert designs.design_names(DEMOS, uploads) == ["my_upload", "tt_um_demo_a", "tt_um_demo_b"]
    # a demo listed in index.json without its bitstream is not offered, and that is said
    (tmp_path / "index.json").write_text('{"demos": [{"name": "listed_only"}]}')
    assert designs.design_names(tmp_path, uploads) == ["my_upload"]
    assert "listed_only" in caplog.text


def test_design_file_finds_demos_and_uploads_and_refuses_everything_else(uploads):
    designs.store_upload(uploads, "my_upload", PRE + b"u")
    assert designs.design_file(DEMOS, uploads, "tt_um_demo_a") == DEMOS / "tt_um_demo_a.bin"
    assert designs.design_file(DEMOS, uploads, "my_upload") == uploads / "my_upload.bin"
    for name in ("nope", "Not A Valid Name!", "../etc/passwd", ""):
        with pytest.raises(designs.DesignNotFound):
            designs.design_file(DEMOS, uploads, name)


def test_store_upload_keeps_the_file_whole_and_replaces_one_of_the_same_name(uploads):
    assert designs.store_upload(uploads, "my_upload", PRE + b"one") == []
    assert designs.store_upload(uploads, "my_upload", PRE + b"two") == []
    assert (uploads / "my_upload.bin").read_bytes() == PRE + b"two"
    assert sorted(f.name for f in uploads.iterdir()) == ["my_upload.bin"]  # no temporary file left


def test_store_upload_leaves_nothing_behind_when_the_write_fails(uploads, monkeypatch):
    designs.store_upload(uploads, "my_upload", PRE + b"one")

    def no_room(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(designs.os, "replace", no_room)
    with pytest.raises(OSError, match="No space"):
        designs.store_upload(uploads, "my_upload", PRE + b"two")
    assert (uploads / "my_upload.bin").read_bytes() == PRE + b"one"
    assert sorted(f.name for f in uploads.iterdir()) == ["my_upload.bin"]


def test_store_upload_removes_the_oldest_uploads_beyond_the_limit(uploads):
    for i, name in enumerate(["up_old", "up_mid", "up_new"]):
        designs.store_upload(uploads, name, PRE + bytes([i]))
        os.utime(uploads / f"{name}.bin", (1000 + i, 1000 + i))
    assert designs.store_upload(uploads, "up_newest", PRE, keep=3) == ["up_old"]
    assert designs.design_names(DEMOS / "nope", uploads) == ["up_mid", "up_new", "up_newest"]
    # the upload just stored is never the one removed, however old the clock says it is
    os.utime(uploads / "up_newest.bin", (1, 1))
    assert designs.store_upload(uploads, "up_newest", PRE, keep=1) == ["up_mid", "up_new"]
    assert designs.design_names(DEMOS / "nope", uploads) == ["up_newest"]


def test_what_is_listed_is_what_can_be_loaded(tmp_path, uploads):
    """An upload whose name a later index.json lists without a bitstream is still the upload."""
    designs.store_upload(uploads, "later_a_demo", PRE)
    (tmp_path / "index.json").write_text('{"demos": [{"name": "later_a_demo"}]}')
    assert designs.design_names(tmp_path, uploads) == ["later_a_demo"]
    assert designs.design_file(tmp_path, uploads, "later_a_demo") == uploads / "later_a_demo.bin"


def test_uploads_at_the_same_time_do_not_trip_over_each_others_evictions(uploads):
    import concurrent.futures

    for i in range(4):
        designs.store_upload(uploads, f"old_{i}", PRE, keep=4)
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        done = [pool.submit(designs.store_upload, uploads, f"new_{i}", PRE + bytes([i]), 4) for i in range(8)]
        evicted = [n for f in done for n in f.result()]  # .result() raises what the upload raised
    assert len(designs.design_names(DEMOS / "nope", uploads)) == 4 and len(evicted) == len(set(evicted)) == 8


async def test_an_upload_removed_before_it_is_read_is_not_found(runner, fake_repl, uploads, monkeypatch):
    monkeypatch.setattr(designs, "design_file", lambda demos_dir, uploads_dir, name, **kw: uploads / "evicted.bin")
    with pytest.raises(designs.DesignNotFound):
        await designs.enable_design(runner, "evicted", None, DEMOS, uploads)
    assert fake_repl.transcript == b""


# -- the board: read what is loaded, load from memory, write nothing --


async def test_list_designs_is_the_pis_designs_with_what_the_board_has_loaded(runner, fake_repl, uploads):
    designs.store_upload(uploads, "my_upload", PRE)
    (fake_repl.root / "bitstreams" / "left_by_an_older_daemon.bin").write_bytes(PRE)  # on the board: not offered
    body = await designs.list_designs(runner, DEMOS, uploads)
    assert body["enabled"] is None
    assert [d["name"] for d in body["designs"]] == ["my_upload", "tt_um_demo_a", "tt_um_demo_b"]
    u, a = body["designs"][0], body["designs"][1]
    assert a["source"] == "demo" and a["title"] == "Demo A" and a["clock_hz"] == 1000
    assert u["source"] == "upload" and u["title"] == "" and u["clock_hz"] is None and u["pinout"] == {}


async def test_list_designs_on_a_board_whose_sdk_is_not_running_says_nothing_is_loaded(runner, fake_repl, uploads):
    del fake_repl._globals["tt"]  # as after a raw-REPL soft reset
    assert (await designs.list_designs(runner, DEMOS, uploads))["enabled"] is None


async def test_enable_design_loads_the_pis_file_through_the_sdk_and_writes_nothing_to_the_board(
    runner, fake_repl, uploads
):
    (fake_repl.root / "main.py").write_text("print('the SDK')\n")
    (fake_repl.root / "bitstreams" / "custom.bin").write_bytes(b"left by the old loader")
    before = board_tree(fake_repl)
    data = (DEMOS / "tt_um_demo_a.bin").read_bytes()
    out = await designs.enable_design(runner, "tt_um_demo_a", 1000, DEMOS, uploads)
    assert out == {"enabled": "tt_um_demo_a", "clock_hz": 1000, **NO_UI_IN}
    assert fake_repl.loaded == [("pi:tt_um_demo_a.bin", data)]  # the SDK's loader read exactly the Pi's bytes
    assert fake_repl.tt.shuttle.enable_log == ["tt_um_demo_a"] and fake_repl.tt.clock_log == [1000]
    assert (await designs.list_designs(runner, DEMOS, uploads))["enabled"] == "tt_um_demo_a"
    assert board_tree(fake_repl) == before  # nothing made, changed or removed on the board
    assert not hasattr(fake_repl.loader, "open")  # the loader has its own `open` back
    assert fake_repl._globals["_fo_buf"] is None  # and the buffer is given back


async def test_enable_design_loads_a_bitstream_longer_than_one_step(runner, fake_repl, uploads):
    data = PRE + bytes(range(256)) * 20  # several CHUNKs and a short last one
    designs.store_upload(uploads, "my_upload", data)
    await designs.enable_design(runner, "my_upload", None, DEMOS, uploads)
    assert fake_repl.loaded == [("pi:my_upload.bin", data)]
    assert fake_repl.tt.clock_log == []  # no clock_hz given: the clock is left alone


async def test_enable_unknown_design_raises_not_found_before_touching_the_board(runner, fake_repl, uploads):
    for name in ("nope", "Not A Valid Name!"):
        with pytest.raises(designs.DesignNotFound):
            await designs.enable_design(runner, name, None, DEMOS, uploads)
    assert fake_repl.transcript == b""  # never touched the board


async def test_a_design_only_on_the_board_is_not_loadable(runner, fake_repl, uploads):
    (fake_repl.root / "bitstreams" / "left_by_an_older_daemon.bin").write_bytes(PRE)
    with pytest.raises(designs.DesignNotFound):
        await designs.enable_design(runner, "left_by_an_older_daemon", None, DEMOS, uploads)


async def test_enable_design_applies_explicit_zero_clock_hz(runner, fake_repl, uploads):
    # clock_hz=0 is a legitimate explicit value, distinct from "no clock_hz
    # given" (clock_hz=None) -- it must still reach tt.clock_project_PWM.
    out = await designs.enable_design(runner, "tt_um_demo_a", 0, DEMOS, uploads)
    assert out == {"enabled": "tt_um_demo_a", "clock_hz": 0, **NO_UI_IN}
    assert fake_repl.tt.clock_log == [0]


SDK_MAIN = "print('BOOT: Tiny Tapeout SDK')\ntt = _the_sdk\nprint('tt.sdk_version=3.1.0')\n"


async def test_enable_design_starts_a_missing_sdk_the_way_the_commander_does(runner, fake_repl, uploads):
    """After a raw-REPL soft reset (mpremote, the boot check) there is no `tt`. The Commander then soft-resets
    the board from the friendly REPL, which runs the board's own main.py (Tim, tt-07: do the same)."""
    fake_repl._globals["_the_sdk"] = fake_repl._globals.pop("tt")
    (fake_repl.root / "main.py").write_text(SDK_MAIN)
    before = board_tree(fake_repl)
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert fake_repl.soft_resets == 1
    assert b"\x03\x03\x02\x04" in bytes(fake_repl.transcript)  # the Commander's own bytes, in its order
    assert fake_repl._globals["tt"] is fake_repl.tt and fake_repl.tt.shuttle.enable_log == ["tt_um_demo_a"]
    assert board_tree(fake_repl) == before


async def test_a_running_sdk_is_not_reset(runner, fake_repl, uploads):
    (fake_repl.root / "main.py").write_text(SDK_MAIN)
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert fake_repl.soft_resets == 0


async def test_enable_design_fails_loudly_when_the_boards_main_py_does_not_start_the_sdk(runner, fake_repl, uploads):
    del fake_repl._globals["tt"]
    (fake_repl.root / "main.py").write_text("print('TT FPGA board ready')\n")  # the old test wrapper's no-op
    with pytest.raises(ReplError, match="main.py did not start the Tiny Tapeout SDK") as ei:
        await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert "TT FPGA board ready" in str(ei.value.detail)
    assert fake_repl.soft_resets == 1 and fake_repl.loaded == []


async def test_a_main_py_that_prints_the_sdks_line_without_building_it_is_not_a_started_sdk(
    runner, fake_repl, uploads
):
    del fake_repl._globals["tt"]
    (fake_repl.root / "main.py").write_text("print('tt.sdk_version=3.1.0')\n")
    with pytest.raises(ReplError, match="did not start the Tiny Tapeout SDK"):
        await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)


async def test_output_right_after_the_prompt_does_not_hide_that_the_board_is_back(
    runner, fake_repl, uploads, monkeypatch
):
    """Another client of the bridge (a viewer typing) can make the board print straight after its prompt."""
    import tests.fakerepl as fakerepl

    monkeypatch.setattr(fakerepl, "FRIENDLY_BANNER", fakerepl.FRIENDLY_BANNER + b"1+1\r\n2\r\n>>> x")
    fake_repl._globals["_the_sdk"] = fake_repl._globals.pop("tt")
    (fake_repl.root / "main.py").write_text(SDK_MAIN)
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert fake_repl.soft_resets == 1 and fake_repl.tt.shuttle.enable_log == ["tt_um_demo_a"]


async def test_a_board_that_does_not_come_back_from_the_reset_fails_in_time(runner, fake_repl, uploads, monkeypatch):
    del fake_repl._globals["tt"]
    (fake_repl.root / "main.py").write_text("import time\ntime.sleep(2)\n")
    monkeypatch.setattr(designs, "SDK_START_TIMEOUT", 0.3)
    with pytest.raises(ReplError, match="did not come back to its prompt in time|did not soft-reset in time"):
        await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)


async def test_enable_design_fails_when_the_sdks_loader_did_not_read_the_whole_bitstream(
    runner, fake_repl, uploads, monkeypatch
):
    """The SDK's loader prints an OSError instead of raising it: a load that read nothing must not pass."""
    monkeypatch.setattr(fake_repl.loader, "spi_transferPIO", lambda filepath, freq=1_000_000: None)
    with pytest.raises(ReplError) as ei:
        await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert "the loader read 0 of" in str(ei.value.detail)
    assert not hasattr(fake_repl.loader, "open")


async def test_a_load_that_failed_is_not_reported_as_loaded(runner, fake_repl, uploads, monkeypatch):
    """The SDK names the design as enabled before it has transferred it."""
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    monkeypatch.setattr(fake_repl.loader, "spi_transferPIO", lambda filepath, freq=1_000_000: None)
    with pytest.raises(ReplError):
        await designs.enable_design(runner, "tt_um_demo_b", None, DEMOS, uploads)
    assert (await designs.list_designs(runner, DEMOS, uploads))["enabled"] is None  # not demo_b, and not demo_a


async def test_a_bitstream_that_did_not_arrive_as_sent_is_not_loaded(runner, fake_repl, uploads, monkeypatch):
    real = designs._load_steps

    def corrupted(name, data, clock_hz, **kwargs):
        steps = real(name, data, clock_hz, **kwargs)
        return [*steps[:-1], "_fo_buf[5] ^= 0xff\n", steps[-1]]

    monkeypatch.setattr(designs, "_load_steps", corrupted)
    with pytest.raises(ReplError) as ei:
        await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert "did not arrive as it was sent" in str(ei.value.detail)
    assert fake_repl.loaded == [] and fake_repl.tt.shuttle.enable_log == []


async def test_a_load_refused_before_the_fpga_is_touched_leaves_the_running_design_named(
    runner, fake_repl, uploads, monkeypatch
):
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    real = designs._load_steps

    def corrupted(name, data, clock_hz, **kwargs):
        steps = real(name, data, clock_hz, **kwargs)
        return [*steps[:-1], "_fo_buf[5] ^= 0xff\n", steps[-1]]

    monkeypatch.setattr(designs, "_load_steps", corrupted)
    with pytest.raises(ReplError):
        await designs.enable_design(runner, "tt_um_demo_b", None, DEMOS, uploads)
    assert (await designs.list_designs(runner, DEMOS, uploads))["enabled"] == "tt_um_demo_a"  # still running


async def test_a_load_cut_short_leaves_nothing_that_stops_the_next_one(runner, fake_repl, uploads):
    """Whatever cut it short (a deadline, a Commander connecting): the next load lets the old buffer go before it
    makes its own, and takes a loader `open` left behind away."""
    data = PRE + bytes(range(256)) * 20
    designs.store_upload(uploads, "my_upload", data)
    await runner.exec_steps(designs._load_steps("my_upload", data, None)[:3])  # buffer and two chunks, no load
    assert len(fake_repl._globals["_fo_buf"]) == len(data)
    fake_repl.loader.open = lambda path, mode="rb": None  # as if cut short between the load and its clean-up
    seen = []
    real_bytearray = bytearray

    def bytearray_seeing_the_old_buffer(n):
        seen.append(fake_repl._globals["_fo_buf"])
        return real_bytearray(n)

    fake_repl._globals["bytearray"] = bytearray_seeing_the_old_buffer
    await designs.enable_design(runner, "my_upload", None, DEMOS, uploads)
    assert seen == [None]  # the old buffer was let go before the new one was made
    assert fake_repl.loaded == [("pi:my_upload.bin", data)] and not hasattr(fake_repl.loader, "open")


async def test_a_board_whose_sdk_start_was_cut_short_is_started_again(runner, fake_repl, uploads):
    """The SDK's main.py sets `tt = None` before it builds the board: interrupted there, `tt` exists and is None."""
    fake_repl._globals["_the_sdk"] = fake_repl._globals["tt"]
    fake_repl._globals["tt"] = None
    (fake_repl.root / "main.py").write_text(SDK_MAIN)
    assert (await designs.list_designs(runner, DEMOS, uploads))["enabled"] is None
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert fake_repl.soft_resets == 1
    assert fake_repl._globals["tt"] is fake_repl.tt and fake_repl.tt.shuttle.enable_log == ["tt_um_demo_a"]


async def test_a_failed_load_gives_the_loader_its_own_open_back(runner, fake_repl, uploads, monkeypatch):
    def breaks(design, force=False):
        raise RuntimeError("pins")

    monkeypatch.setattr(fake_repl.tt.shuttle, "enable", breaks)
    with pytest.raises(ReplError):
        await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert not hasattr(fake_repl.loader, "open") and fake_repl._globals["_fo_buf"] is None


# -- the guard: nothing this daemon sends to the board may change a file on it (Tim, 2026-10-05) --

BOARD_WRITE = re.compile(
    r"""open\([^)]*,\s*(?:mode\s*=\s*)?["'][^"']*[wax+{]"""  # open(path, "w"), open(path, mode="ab")
    r"""|open\([^),]*,\s*(?:mode\s*=\s*)?[A-Za-z_]"""  # open(path, mode): a mode that is not written out
    r"""|\bu?os\.(?:mkdir|remove|rename|rmdir|unlink|sync|mount|umount|VfsLfs2|VfsFat)\b"""
    r"""|\bfrom\s+u?os\s+import\b|\bimport\s+u?os\s+as\b|__import__|\b(?:mip|vfs)\."""
    r"""|\.write_(?:text|bytes)\(|\bFlash\(|\bwriteblocks\b|\bmkfs\b|\bioctl\(|/bitstreams"""
)


def sent_to_the_board(source: str) -> list[str]:
    """Every string and bytes constant of Python `source` (an f-string as one text), docstrings left out:
    whatever the daemon sends to the board is one of these."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)
    }  # fmt: skip
    fstrings = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]
    parts = {id(v) for f in fstrings for v in f.values}
    joined = ["".join(v.value if isinstance(v, ast.Constant) else "{}" for v in f.values) for f in fstrings]
    return joined + [
        n.value if isinstance(n.value, str) else n.value.decode("latin-1")
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, (str, bytes))
        and id(n) not in docstrings and id(n) not in parts
    ]  # fmt: skip


@pytest.mark.parametrize("path", sorted((Path(designs.__file__).parent).rglob("*.py")), ids=lambda p: p.name)
def test_no_text_in_the_daemon_reads_like_a_board_write(path):
    """A tripwire, not a proof: it reads the daemon's literal text, so it catches the old snippets coming back
    and their obvious relatives, not a write built at run time. What holds every test to the rule is the fake
    board, which refuses any change to its filesystem (fakerepl.BoardWrite)."""
    found = [(text[:80], BOARD_WRITE.search(text).group()) for text in sent_to_the_board(path.read_text())
             if BOARD_WRITE.search(text)]  # fmt: skip
    assert not found, found


def test_the_steps_of_a_load_as_sent_do_not_read_like_a_board_write():
    for step in [designs.SDK_CODE, designs.ENABLED_CODE, *designs._load_steps("my_upload", PRE * 600, 1000)]:
        assert not BOARD_WRITE.search(step), BOARD_WRITE.search(step)


async def test_the_fake_board_refuses_what_the_daemon_used_to_send(runner, fake_repl):
    from tests.fakerepl import BoardWrite  # noqa: F401 - named for the reader; it reaches us as a REPL error

    for code in ("f = open('/bitstreams/x.bin.tmp', 'wb')\n", "import os\nos.remove('/bitstreams/x.bin')\n",
                 "import os\nos.rename('/a', '/b')\n", "import os\nos.mkdir('/bitstreams')\n"):  # fmt: skip
        with pytest.raises(ReplError) as ei:
            await runner.exec(code)
        assert "BoardWrite" in str(ei.value.detail)
    assert fake_repl.board_writes == [
        "open('/bitstreams/x.bin.tmp', 'wb')", "os.remove('/bitstreams/x.bin')", "os.rename('/a', '/b')",
        "os.mkdir('/bitstreams')",
    ]  # fmt: skip
    # a snippet that swallows the refusal, as the old sweep did, is still recorded
    await runner.exec("import os\ntry:\n    os.remove('/bitstreams/y.tmp')\nexcept Exception:\n    pass\nprint('ok')\n")
    assert fake_repl.board_writes[-1] == "os.remove('/bitstreams/y.tmp')"
    fake_repl.board_writes.clear()  # these were this test's own


@pytest.mark.parametrize(
    "source",
    [
        "STEP = \"f = open('/bitstreams/x.bin.tmp', 'wb')\\n\"",  # how uploads and demos used to be stored
        "STEP = \"import os\\nos.remove('/bitstreams/x.bin')\\n\"",
        "STEP = \"os.rename('a', 'b')\"",
        "STEP = \"os.mkdir('/bitstreams')\"",
        "client.write(b\"open('main.py', 'w')\\x04\")",
        "path = f'/bitstreams/{name}.bin'",
    ],
)
def test_the_guard_sees_how_the_daemon_used_to_write_to_the_board(source):
    assert [text for text in sent_to_the_board(source) if BOARD_WRITE.search(text)], source


# -- the VGA library: a third source, between the demos and the uploads --


@pytest.fixture
def lib(tmp_path) -> Path:
    return tmp_path / "pi-library"


def test_precedence_demo_then_library_then_upload(uploads, lib):
    id, sha, data = ENTRIES[1]
    designs.store_upload(uploads, id, PRE + b"an upload made before the index existed")
    assert designs.design_file(DEMOS, uploads, id, library_dir=lib) == uploads / f"{id}.bin"
    library.store(lib, id, sha, data, 1 << 20)
    assert designs.design_file(DEMOS, uploads, id, library_dir=lib) == library.bin_path(lib, id)  # over the upload
    # a demo wins over a library entry of its name (a push refuses one: this one is put there by hand)
    library.store(lib, "tt_um_demo_a", sha, data, 1 << 20)
    assert designs.design_file(DEMOS, uploads, "tt_um_demo_a", library_dir=lib) == DEMOS / "tt_um_demo_a.bin"
    assert designs.design_names(DEMOS, uploads, library_dir=lib) == sorted([id, "tt_um_demo_a", "tt_um_demo_b"])
    # without a library, as before
    assert designs.design_file(DEMOS, uploads, id) == uploads / f"{id}.bin"
    assert designs.design_names(DEMOS, uploads) == sorted([id, "tt_um_demo_a", "tt_um_demo_b"])
    for name in ("nope", "Not A Valid Name!", "../etc/passwd", ""):
        with pytest.raises(designs.DesignNotFound):
            designs.design_file(DEMOS, uploads, name, library_dir=lib)


def test_upload_refused_for_cached_library_id():
    with pytest.raises(ValidationError) as ei:
        validate_bitstream("ttsky25a_vga_example", PRE, {"tt_um_demo_a"}, library_names={"ttsky25a_vga_example"})
    assert ei.value.status == 409
    assert str(ei.value) == "ttsky25a_vga_example is a VGA library design; pick another"
    validate_bitstream("my_design", PRE, {"tt_um_demo_a"}, library_names={"ttsky25a_vga_example"})


@pytest.mark.parametrize("name,msg", [("tt_um_demo_a", "is a demo name"), ("idle_display", "idle display")])
def test_check_name_free_is_validate_bitstreams_own_rule(name, msg):
    for check in (lambda: designs.check_name_free(name, {"tt_um_demo_a"}),
                  lambda: validate_bitstream(name, PRE, {"tt_um_demo_a"})):  # fmt: skip
        with pytest.raises(ValidationError, match=msg) as ei:
            check()
        assert ei.value.status == 409
    designs.check_name_free("my_design", {"tt_um_demo_a"})


async def test_listing_reports_library_entries_in_upload_shape_with_size(runner, fake_repl, uploads, lib):
    designs.store_upload(uploads, "my_upload", PRE)
    id, sha, data = ENTRIES[1]
    library.store(lib, id, sha, data, 1 << 20)
    body = await designs.list_designs(runner, DEMOS, uploads, library_dir=lib)
    assert [d["name"] for d in body["designs"]] == ["my_upload", "tt_um_demo_a", "tt_um_demo_b", id]
    assert body["designs"][-1] == {
        "name": id, "title": "", "author": "", "description": "", "docs_url": "", "repo_url": "",
        "clock_hz": None, "pinout": {}, "source": "library", "size": 4096,
    }  # fmt: skip
    assert body["designs"][0]["source"] == "upload" and "size" not in body["designs"][0]
    assert body["designs"][1]["source"] == "demo" and "size" not in body["designs"][1]
    # without a library, as before
    assert [d["name"] for d in (await designs.list_designs(runner, DEMOS, uploads))["designs"]] == [
        "my_upload", "tt_um_demo_a", "tt_um_demo_b"]  # fmt: skip


async def test_enabled_library_design_not_in_cache_still_lists_enabled(runner, fake_repl, uploads, lib):
    id, sha, data = ENTRIES[1]
    library.store(lib, id, sha, data, 4096)
    await designs.enable_design(runner, id, None, DEMOS, uploads, library_dir=lib)
    library.store(lib, *ENTRIES[2], 4096)  # evicts it
    body = await designs.list_designs(runner, DEMOS, uploads, library_dir=lib)
    assert body["enabled"] == id  # what the board says, whatever the Pi still keeps
    assert id not in [d["name"] for d in body["designs"]]


async def test_enable_reads_library_bin_and_touches_lru(runner, fake_repl, uploads, lib):
    id, sha, data = ENTRIES[0]
    library.store(lib, id, sha, data, 1 << 20)
    os.utime(lib / f"{id}.json", (1000, 1000))
    out = await designs.enable_design(runner, id, 25_175_000, DEMOS, uploads, library_dir=lib)
    assert out == {"enabled": id, "clock_hz": 25_175_000, "ui_in": None, "ui_in_mask": 0, "ui_in_driven": [],
                   "ui_in_held": [], "ui_in_inputs": list(range(8)), "released": []}  # a library load: all eight inputs
    assert fake_repl.loaded == [(f"pi:{id}.bin", data)]
    assert (lib / f"{id}.json").stat().st_mtime > 1000


async def test_a_library_entry_evicted_before_the_enable_is_not_found(runner, fake_repl, uploads, lib):
    library.store(lib, *ENTRIES[0], 4096)
    library.store(lib, *ENTRIES[1], 4096)  # evicts ENTRIES[0]
    with pytest.raises(designs.DesignNotFound):
        await designs.enable_design(runner, ENTRIES[0][0], None, DEMOS, uploads, library_dir=lib)
    assert fake_repl.transcript == b""


# -- ui_in: what the daemon drove is released on every load; only ui_in_mask bits are driven (spec Ruling AD) --

D = PRE + bytes(60)
GUARD_LINES = [
    "_fo_p.mode = Pin.IN",
    "_fo_p.pull = Pin.PULL_DOWN",
    "time.sleep_ms(5)",
    "if _fo_p():",
    "    _fo_held.append(_fo_i)",
    "    continue",
]


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i : text.index(end, i)]


def test_ui_in_guard_text():
    assert designs.ui_in_guard(4, "_fo_held") == "".join(f"    {line}\n" for line in GUARD_LINES)
    assert "        _fo_held.append(_fo_i)\n" in designs.ui_in_guard(4, "_fo_held")


def test_release_block_on_every_load():
    masked = {"ui_in": 1, "ui_in_mask": 1}
    for kwargs in ({}, masked, {"library": True}, {**masked, "library": True}):
        step = designs._load_steps("x", D, None, **kwargs)[-1]
        hash_check = step.index("raise ValueError('the bitstream did not arrive as it was sent')")
        before = min(step.index("_fo_reader = _fo_Reader("), step.index("_fo_touched = True"))
        for piece in ("globals().get('_fo_ui_driven')", rpregs.GPIO_OE_EXPR, "time.sleep_ms(1)", "Pin.PULL_DOWN",
                      "print('released=' + json.dumps("):  # fmt: skip
            assert hash_check < step.index(piece) < before, piece
        for module in ("import machine, time, json\n", "from machine import Pin\n"):
            assert hash_check < step.index(module) < before
        assert "import sys" not in step


def test_release_discharges_only_daemon_driven_pins():
    step = designs._load_steps("x", D, None, ui_in=1, ui_in_mask=1, library=True)[-1]
    driven = _between(step, "    for _fo_i in _fo_prev:\n", "    _fo_inputs = [")
    # the latch-0 write and the 1 ms sit under the GPIO_OE test, and come before Pin.IN, then Pin.PULL_DOWN
    test = driven.index("        if _fo_oe >> _fo_p.gpio_num & 1:\n")
    latch = driven.index("            _fo_p.raw_pin.value(0)\n")
    wait = driven.index("            time.sleep_ms(1)\n")
    made_input = driven.index("        _fo_p.mode = Pin.IN\n")
    pull = driven.index("        _fo_p.pull = Pin.PULL_DOWN\n")
    assert test < latch < wait < made_input < pull
    assert f"    _fo_oe = machine.mem32[{rpregs.GPIO_OE_EXPR}]\n" in step
    # the other released pins: the unmasked bits that the daemon did not drive, straight to inputs
    assert "    _fo_inputs = [_fo_i for _fo_i in [1, 2, 3, 4, 5, 6, 7] if _fo_i not in _fo_prev]\n" in step
    others = _between(step, "    for _fo_i in _fo_inputs:\n", "    _fo_ui_driven = []\n")
    assert others.index("Pin.IN") < others.index("Pin.PULL_DOWN")
    for never in ("Pin.OUT", "raw_pin.value(", "sleep_ms"):
        assert never not in others


def test_library_release_includes_unmasked_bits():
    def extra(**kwargs) -> str:
        step = designs._load_steps("x", D, None, **kwargs)[-1]
        assert "_fo_prev = list(globals().get('_fo_ui_driven') or [])" in step  # _fo_ui_driven, always
        return _between(step, "    _fo_inputs = [_fo_i for _fo_i in ", " if _fo_i not in _fo_prev]")

    assert extra(ui_in=1, ui_in_mask=1, library=True).endswith("[1, 2, 3, 4, 5, 6, 7]")
    assert extra(ui_in=None, ui_in_mask=0, library=True).endswith(repr(list(range(8))))
    assert extra(ui_in=0x81, ui_in_mask=0x81, library=True).endswith("[1, 2, 3, 4, 5, 6]")
    assert extra(ui_in=1, ui_in_mask=1, library=False).endswith("[]")
    assert extra().endswith("[]")


def test_no_drive_block_when_mask_zero():
    for kwargs in ({"ui_in": None, "ui_in_mask": 0}, {"ui_in": None, "ui_in_mask": 0, "library": True}):
        step = designs._load_steps("x", D, 1000, **kwargs)[-1]
        assert "raw_pin.value((" not in step and "print('ui_in='" not in step
        assert step.endswith("tt.clock_project_PWM(1000)\nprint('enabled')\n")


def test_drive_block_never_touches_mode_or_clock():
    steps = designs._load_steps("x", D, 1000, ui_in=1, ui_in_mask=1)
    step = steps[-1]
    for text in ("".join(steps), designs.LOAD_CODE, designs.RELEASE_CODE, designs.DRIVE_CODE):
        for never in ("tt.mode", "clock_project_stop", "RPMode"):
            assert never not in text
    drive = "".join(
        [
            "from machine import Pin\n",
            "import time, json\n",
            "_fo_tt = tt\n",
            "_fo_held = []\n",
            "_fo_ui_driven = []\n",
            "for _fo_i in range(8):\n",
            "    if not (1 >> _fo_i) & 1:\n",
            "        continue\n",
            "    _fo_p = getattr(_fo_tt.pins, 'ui_in%d' % _fo_i)\n",
            *(f"    {line}\n" for line in GUARD_LINES),
            "    _fo_p.raw_pin.value((1 >> _fo_i) & 1)\n",
            "    _fo_p.mode = Pin.OUT\n",
            "    _fo_ui_driven.append(_fo_i)\n",
            "print('ui_in=' + json.dumps({'driven': _fo_ui_driven, 'held': _fo_held}))\n",
        ]
    )
    assert drive in step  # the spec's block, as written there
    loop = _between(step, "for _fo_i in range(8):\n    if not (1", "print('ui_in='")
    assert loop.index("raw_pin.value((1 >> _fo_i) & 1)") < loop.index("_fo_p.mode = Pin.OUT")
    at = step.index(drive)
    assert step.index("tt.clock_project_PWM(1000)\n") < at and step.index("finally:\n") < at
    assert step.splitlines()[-1] == "print('enabled')"


class _Says:
    """A REPL runner whose board answers the load's last step with `last`."""

    def __init__(self, last: str) -> None:
        self.last = last

    async def exec_steps(self, steps, **kwargs):
        return [*(["ok"] * (len(steps) - 1)), self.last]


async def test_load_design_parses_released_driven_held(monkeypatch):
    parsed = []
    real = designs._parse_json
    monkeypatch.setattr(designs, "_parse_json", lambda out: parsed.append(out) or real(out))
    said = (
        'released={"released": [0], "inputs": [1, 2, 3, 4, 5, 6, 7]}\r\n'
        'ui_in={"driven": [0], "held": []}\r\nenabled\r\n'
    )
    out = await designs.load_design(_Says(said), "x", D, None, ui_in=1, ui_in_mask=1, library=True)
    assert out == {"released": [0], "ui_in_driven": [0], "ui_in_held": [], "ui_in_inputs": [1, 2, 3, 4, 5, 6, 7]}
    assert parsed == ['{"released": [0], "inputs": [1, 2, 3, 4, 5, 6, 7]}', '{"driven": [0], "held": []}']
    # no drive block: nothing driven, nothing held
    out = await designs.load_design(_Says('released={"released": [], "inputs": []}\r\nenabled\r\n'), "x", D, None)
    assert out == {"released": [], "ui_in_driven": [], "ui_in_held": [], "ui_in_inputs": []}
    for bad in ("enabled\r\n", 'released={"released": [0], "inputs": [1]\r\nenabled\r\n',
                'released={"released": [9], "inputs": []}\r\nenabled\r\n',
                'released={"released": [true], "inputs": []}\r\nenabled\r\n',
                'released={"released": [], "inputs": []}\r\nui_in=[0]\r\nenabled\r\n'):  # fmt: skip
        with pytest.raises(ReplError):
            await designs.load_design(_Says(bad), "x", D, None)


def _pins(fake_repl) -> list[tuple[int, object]]:
    return [(fake_repl.tt.ui_pin(k).hw_mode, fake_repl.tt.ui_pin(k).hw_pull) for k in range(8)]


async def test_release_reports_two_lists(runner, fake_repl, uploads, lib):
    assert fake_repl.chip == "rp2350"
    id, sha, data = ENTRIES[1]
    library.store(lib, id, sha, data, 1 << 20)
    first = await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads, library_dir=lib,
                                        ui_in=1, ui_in_mask=1)  # fmt: skip
    assert (first["released"], first["ui_in_inputs"], first["ui_in_driven"]) == ([], [], [0])
    assert fake_repl._globals["_fo_ui_driven"] == [0]
    outputs, latches = len(fake_repl.tt.made_outputs), len(fake_repl.tt.latch_writes)
    out = await designs.enable_design(runner, id, None, DEMOS, uploads, library_dir=lib, ui_in=1, ui_in_mask=1)
    assert out == {"enabled": id, "clock_hz": None, "ui_in": 1, "ui_in_mask": 1, "ui_in_driven": [0],
                   "ui_in_held": [], "ui_in_inputs": [1, 2, 3, 4, 5, 6, 7], "released": [0]}  # fmt: skip
    # bit 0 was discharged (latch 0) and released, then driven again; no other pin was made an output
    assert fake_repl.tt.latch_writes[latches:] == [(0, 0), (0, 1)]
    assert fake_repl.tt.made_outputs[outputs:] == [0]
    assert _pins(fake_repl)[1:] == [(0, 2)] * 7  # Pin.IN, Pin.PULL_DOWN
    # a bit both driven before and outside the new mask is listed only under `released`
    out = await designs.enable_design(runner, id, None, DEMOS, uploads, library_dir=lib, ui_in=2, ui_in_mask=2)
    assert (out["released"], out["ui_in_inputs"], out["ui_in_driven"]) == ([0], [2, 3, 4, 5, 6, 7], [1])
    assert _pins(fake_repl)[0] == (0, 2)
    # a mask of 0 makes all eight bits inputs
    out = await designs.enable_design(runner, id, None, DEMOS, uploads, library_dir=lib)
    assert (out["released"], out["ui_in_inputs"], out["ui_in_driven"]) == ([1], [0, 2, 3, 4, 5, 6, 7], [])
    assert _pins(fake_repl) == [(0, 2)] * 8 and fake_repl._globals["_fo_ui_driven"] == []


async def test_release_frees_only_daemon_driven_pins(runner, fake_repl, uploads):
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads, ui_in=1, ui_in_mask=1)
    # the visitor's own write_ui_in, through the Commander, sets bit 1: the chip drives it high
    tt = fake_repl.tt
    tt.ui_pin(1).mode = 1
    tt.ui_in.value = tt.ui_out | 0b10
    latches = len(tt.latch_writes)
    out = await designs.enable_design(runner, "tt_um_demo_b", None, DEMOS, uploads)
    assert (out["released"], out["ui_in_inputs"], out["ui_in_driven"], out["ui_in_held"]) == ([0], [], [], [])
    assert tt.latch_writes[latches:] == [(0, 0)]  # only bit 0 discharged
    assert tt.ui_pin(1).hw_mode == 1 and tt.ui_out >> 1 & 1 and tt.ui_level(1) == 1  # bit 1 untouched
    assert _pins(fake_repl)[0] == (0, 2)


async def test_a_released_pin_the_chip_no_longer_drives_is_not_made_an_output(runner, fake_repl, uploads):
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads, ui_in=1, ui_in_mask=1)
    fake_repl.tt.ui_pin(0).hw_mode = 0  # something let go of it behind the daemon's back
    outputs, latches = len(fake_repl.tt.made_outputs), len(fake_repl.tt.latch_writes)
    out = await designs.enable_design(runner, "tt_um_demo_b", None, DEMOS, uploads)
    assert out["released"] == [0]
    assert fake_repl.tt.latch_writes[latches:] == [] and fake_repl.tt.made_outputs[outputs:] == []


async def test_release_exception_keeps_enabled_name(runner, fake_repl, uploads):
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    fake_repl._globals["_fo_ui_driven"] = [9]  # no ui_in9: the release raises
    with pytest.raises(ReplError):
        await designs.enable_design(runner, "tt_um_demo_b", None, DEMOS, uploads)
    assert (await designs.list_designs(runner, DEMOS, uploads))["enabled"] == "tt_um_demo_a"
    assert [name for name, _ in fake_repl.loaded] == ["pi:tt_um_demo_a.bin"]  # the FPGA was never touched


async def test_a_held_mask_bit_is_left_an_input(runner, fake_repl, uploads):
    fake_repl.tt.dip_on, fake_repl.tt.counter = {2}, 0  # an FPGA board: no factory test on uio
    out = await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads, ui_in=0b101, ui_in_mask=0b111)
    assert (out["ui_in_driven"], out["ui_in_held"]) == ([0, 1], [2])
    assert fake_repl._globals["_fo_ui_driven"] == [0, 1]


async def test_a_failed_load_drives_nothing(runner, fake_repl, uploads, monkeypatch):
    monkeypatch.setattr(fake_repl.loader, "spi_transferPIO", lambda filepath, freq=1_000_000: None)
    outputs = len(fake_repl.tt.made_outputs)
    with pytest.raises(ReplError):
        await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads, ui_in=1, ui_in_mask=1)
    assert fake_repl.tt.made_outputs[outputs:] == [] and fake_repl._globals["_fo_ui_driven"] == []
