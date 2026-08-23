import asyncio
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


def board_file(fake_repl, name: str) -> Path:
    return fake_repl.root / "bitstreams" / f"{name}.bin"


def test_demo_index_loads_and_missing_dir_is_empty(tmp_path):
    idx = designs.load_demo_index(DEMOS)
    assert set(idx) == {"tt_um_demo_a", "tt_um_demo_b"}
    assert idx["tt_um_demo_a"]["clock_hz"] == 1000
    assert designs.load_demo_index(tmp_path / "nope") == {}


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


async def test_list_designs_merges_board_files_with_demo_index(runner, fake_repl):
    board_file(fake_repl, "tt_um_demo_a").write_bytes(PRE)
    board_file(fake_repl, "my_upload").write_bytes(PRE)
    body = await designs.list_designs(runner, DEMOS)
    assert body["enabled"] is None
    names = [d["name"] for d in body["designs"]]
    assert names == ["my_upload", "tt_um_demo_a"]
    a, u = body["designs"][1], body["designs"][0]
    assert a["source"] == "demo" and a["title"] == "Demo A" and a["clock_hz"] == 1000
    assert u["source"] == "upload" and u["title"] == "" and u["clock_hz"] is None and u["pinout"] == {}


async def test_enable_design_and_report_enabled(runner, fake_repl):
    board_file(fake_repl, "tt_um_demo_a").write_bytes(PRE)
    out = await designs.enable_design(runner, "tt_um_demo_a", clock_hz=1000)
    assert out == {"enabled": "tt_um_demo_a", "clock_hz": 1000}
    assert fake_repl.tt.shuttle.enable_log == ["tt_um_demo_a"]
    assert fake_repl.tt.clock_log == [1000]
    body = await designs.list_designs(runner, DEMOS)
    assert body["enabled"] == "tt_um_demo_a"


async def test_enable_unknown_design_raises_not_found(runner, fake_repl):
    with pytest.raises(designs.DesignNotFound):
        await designs.enable_design(runner, "nope", clock_hz=None)


async def test_enable_design_applies_explicit_zero_clock_hz(runner, fake_repl):
    # clock_hz=0 is a legitimate explicit value, distinct from "no clock_hz
    # given" (clock_hz=None) -- it must still reach tt.clock_project_PWM.
    board_file(fake_repl, "tt_um_demo_a").write_bytes(PRE)
    out = await designs.enable_design(runner, "tt_um_demo_a", clock_hz=0)
    assert out == {"enabled": "tt_um_demo_a", "clock_hz": 0}
    assert fake_repl.tt.clock_log == [0]


async def test_write_bitstream_round_trips_and_refreshes_index(runner, fake_repl):
    data = PRE + bytes(range(256)) * 3  # 772 bytes: several chunks, last one partial
    await designs.write_bitstream(runner, "my_upload", data)
    assert board_file(fake_repl, "my_upload").read_bytes() == data
    # the shuttle index is rebuilt: the new design is enable-able
    await designs.enable_design(runner, "my_upload", clock_hz=None)


async def test_evict_uploads_keeps_newest_and_never_touches_demos(runner, fake_repl):
    import os
    import time

    for i in range(designs.MAX_UPLOADS):
        p = board_file(fake_repl, f"u{i:02d}")
        p.write_bytes(PRE)
        os.utime(p, (time.time() - 1000 + i, time.time() - 1000 + i))
    board_file(fake_repl, "tt_um_demo_a").write_bytes(PRE)  # demo, oldest mtime possible
    evicted = await designs.evict_uploads(runner, {"tt_um_demo_a"}, keep=designs.MAX_UPLOADS - 1)
    assert evicted == ["u00"]
    assert not board_file(fake_repl, "u00").exists()
    assert board_file(fake_repl, "tt_um_demo_a").exists()


async def test_sync_demos_copies_missing_and_skips_same_size(runner, fake_repl):
    board_file(fake_repl, "tt_um_demo_b").write_bytes((DEMOS / "tt_um_demo_b.bin").read_bytes())
    out = await designs.sync_demos(runner, DEMOS)
    assert out == {"synced": ["tt_um_demo_a"], "skipped": ["tt_um_demo_b"]}
    assert board_file(fake_repl, "tt_um_demo_a").read_bytes() == (DEMOS / "tt_um_demo_a.bin").read_bytes()


async def test_sync_demos_with_empty_index_touches_nothing(runner, fake_repl, tmp_path):
    # No index.json at all -- load_demo_index returns {} -- must skip the
    # board round trip entirely: not one byte should cross the wire.
    out = await designs.sync_demos(runner, tmp_path / "no-such-demos-dir")
    assert out == {"synced": [], "skipped": []}
    assert fake_repl.transcript == b""


async def test_sync_demos_removes_stale_tmp_files(runner, fake_repl):
    # A prior crash mid-write can leave a straggling *.tmp; sync_demos must
    # sweep it before anything else, even though it's not a demo itself.
    stale = fake_repl.root / "bitstreams" / "x.bin.tmp"
    stale.write_bytes(b"partial")
    board_file(fake_repl, "tt_um_demo_a").write_bytes((DEMOS / "tt_um_demo_a.bin").read_bytes())
    board_file(fake_repl, "tt_um_demo_b").write_bytes((DEMOS / "tt_um_demo_b.bin").read_bytes())
    out = await designs.sync_demos(runner, DEMOS)
    assert out == {"synced": [], "skipped": ["tt_um_demo_a", "tt_um_demo_b"]}
    assert not stale.exists()


async def test_write_bitstream_cleans_up_tmp_on_failure(runner, fake_repl, monkeypatch):
    # Simulate a write that gets partway (the temp file really is created on
    # the board) and then fails -- e.g. the board raised mid-transfer, or
    # someone else's keystrokes interfered. write_bitstream must not leave
    # the .tmp behind for a later sync/upload to trip over.
    real_exec_steps = ReplRunner.exec_steps

    async def flaky_exec_steps(self, steps, **kw):
        if len(steps) > 1:
            await real_exec_steps(self, steps[:1], **kw)  # really create the .tmp
            raise ReplError("simulated failure mid-write", "boom")
        return await real_exec_steps(self, steps, **kw)  # cleanup's own single-step exec

    monkeypatch.setattr(ReplRunner, "exec_steps", flaky_exec_steps)
    data = PRE + bytes(range(256)) * 3
    with pytest.raises(ReplError, match="simulated failure"):
        await designs.write_bitstream(runner, "my_upload", data)
    # The aborted session never ran the board-side `f.close()` -- unlike a
    # real board's file object, the host-side fake's leaks an open fd that
    # would otherwise trip a ResourceWarning at interpreter shutdown; the
    # cleanup snippet already unlinked the path itself (see the assertions
    # below), so this is just tidying up the fake's own simulated state.
    leaked = fake_repl._globals.pop("f", None)
    if leaked is not None:
        leaked.close()
    assert not (fake_repl.root / "bitstreams" / "my_upload.bin.tmp").exists()
    assert not board_file(fake_repl, "my_upload").exists()
