import ast
import asyncio
import os
import re
from pathlib import Path

import pytest

from fpgas_tt import designs
from fpgas_tt.bridge import Bridge
from fpgas_tt.designs import ValidationError, validate_bitstream
from fpgas_tt.repl import ReplError, ReplRunner

DEMOS = Path(__file__).parent / "data" / "demos"
PRE = b"\x7e\xaa\x99\x7e"


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


def test_parse_json_and_parse_int_raise_replerror_on_garbage():
    with pytest.raises(ReplError):
        designs._parse_json("this is not json")
    with pytest.raises(ReplError):
        designs._parse_int("this is not an int")


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
    assert out == {"enabled": "tt_um_demo_a", "clock_hz": 1000}
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
    assert out == {"enabled": "tt_um_demo_a", "clock_hz": 0}
    assert fake_repl.tt.clock_log == [0]


async def test_enable_design_starts_the_sdk_with_the_boards_own_main_py_when_it_is_not_running(
    runner, fake_repl, uploads
):
    """After a raw-REPL soft reset (mpremote, the boot check) there is no `tt` until main.py has run."""
    fake_repl._globals["_the_sdk"] = fake_repl._globals.pop("tt")
    (fake_repl.root / "main.py").write_text("print('BOOT: Tiny Tapeout SDK')\ntt = _the_sdk\n")
    before = board_tree(fake_repl)
    await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert fake_repl._globals["tt"] is fake_repl.tt and fake_repl.tt.shuttle.enable_log == ["tt_um_demo_a"]
    assert board_tree(fake_repl) == before


async def test_enable_design_fails_loudly_when_the_boards_main_py_does_not_start_the_sdk(runner, fake_repl, uploads):
    del fake_repl._globals["tt"]
    (fake_repl.root / "main.py").write_text("print('TT FPGA board ready')\n")  # the old test wrapper's no-op
    with pytest.raises(ReplError) as ei:
        await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert "NameError" in str(ei.value.detail)
    assert fake_repl.loaded == []


async def test_enable_design_fails_when_the_sdks_loader_did_not_read_the_whole_bitstream(
    runner, fake_repl, uploads, monkeypatch
):
    """The SDK's loader prints an OSError instead of raising it: a load that read nothing must not pass."""
    monkeypatch.setattr(fake_repl.loader, "spi_transferPIO", lambda filepath, freq=1_000_000: None)
    with pytest.raises(ReplError) as ei:
        await designs.enable_design(runner, "tt_um_demo_a", None, DEMOS, uploads)
    assert "the loader read 0 of" in str(ei.value.detail)
    assert not hasattr(fake_repl.loader, "open")


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


@pytest.mark.parametrize("path", sorted((Path(designs.__file__).parent).glob("*.py")), ids=lambda p: p.name)
def test_nothing_the_daemon_sends_to_the_board_can_change_a_file_on_it(path):
    found = [(text[:80], BOARD_WRITE.search(text).group()) for text in sent_to_the_board(path.read_text())
             if BOARD_WRITE.search(text)]  # fmt: skip
    assert not found, found


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
