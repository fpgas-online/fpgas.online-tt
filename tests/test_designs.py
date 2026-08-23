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


async def test_enable_design_rejects_invalid_name_before_touching_repl(runner, fake_repl):
    with pytest.raises(designs.DesignNotFound):
        await designs.enable_design(runner, "Not A Valid Name!", clock_hz=None)
    assert fake_repl.transcript == b""  # never touched the board


async def test_enable_design_applies_explicit_zero_clock_hz(runner, fake_repl):
    # clock_hz=0 is a legitimate explicit value, distinct from "no clock_hz
    # given" (clock_hz=None) -- it must still reach tt.clock_project_PWM.
    board_file(fake_repl, "tt_um_demo_a").write_bytes(PRE)
    out = await designs.enable_design(runner, "tt_um_demo_a", clock_hz=0)
    assert out == {"enabled": "tt_um_demo_a", "clock_hz": 0}
    assert fake_repl.tt.clock_log == [0]


async def test_write_bitstream_round_trips_and_refreshes_index(runner, fake_repl):
    data = PRE + bytes(range(256)) * 5  # 1284 bytes: several CHUNK=1024 steps, last one partial
    await designs.write_bitstream(runner, "my_upload", data)
    assert board_file(fake_repl, "my_upload").read_bytes() == data
    # the shuttle index is rebuilt: the new design is enable-able
    await designs.enable_design(runner, "my_upload", clock_hz=None)


async def test_write_bitstream_twice_overwrites_via_rename(runner, fake_repl):
    # The second write's os.rename lands on top of the first write's final
    # name -- must not error, and must leave no stray .tmp behind.
    data1 = PRE + bytes(range(200))
    data2 = PRE + bytes(range(200))[::-1]
    await designs.write_bitstream(runner, "my_upload", data1)
    await designs.write_bitstream(runner, "my_upload", data2)
    assert board_file(fake_repl, "my_upload").read_bytes() == data2
    assert not (fake_repl.root / "bitstreams" / "my_upload.bin.tmp").exists()


async def test_write_bitstream_removes_existing_final_name_before_rename(runner, fake_repl):
    # MicroPython's VfsFat os.rename() raises EEXIST when the destination
    # already exists (LFS2 doesn't); the fake's rename stays plain POSIX
    # (which allows overwriting unconditionally either way), so this only
    # checks the *generated board code* removes the old final name before
    # renaming onto it -- not that the fake's rename behaves like VfsFat.
    await designs.write_bitstream(runner, "my_upload", PRE + b"x" * 50)
    transcript = bytes(fake_repl.transcript)
    remove_idx = transcript.index(b"os.remove('/bitstreams/my_upload.bin')")
    rename_idx = transcript.index(b"os.rename(")
    assert remove_idx < rename_idx


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


async def test_evict_uploads_skips_names_that_dont_match_name_re(runner, fake_repl):
    # A .bin file with an unexpected name (not something write_bitstream
    # would ever produce) must never be interpolated into generated board
    # code, nor evicted -- just left alone.
    weird = fake_repl.root / "bitstreams" / "weird name!.bin"
    weird.write_bytes(PRE)
    board_file(fake_repl, "valid_upload").write_bytes(PRE)
    evicted = await designs.evict_uploads(runner, set(), keep=0)
    assert evicted == ["valid_upload"]
    assert weird.exists()


async def test_sync_demos_syncs_everything_on_a_truly_fresh_board(runner, fake_repl):
    # No manifest yet: even a demo that happens to already be on the board
    # with byte-identical content gets (re)written -- there's no record that
    # it's actually what it claims to be, so nothing is trusted unverified.
    board_file(fake_repl, "tt_um_demo_b").write_bytes((DEMOS / "tt_um_demo_b.bin").read_bytes())
    out = await designs.sync_demos(runner, DEMOS)
    assert out == {"synced": ["tt_um_demo_a", "tt_um_demo_b"], "skipped": []}
    assert board_file(fake_repl, "tt_um_demo_a").read_bytes() == (DEMOS / "tt_um_demo_a.bin").read_bytes()
    assert board_file(fake_repl, "tt_um_demo_b").read_bytes() == (DEMOS / "tt_um_demo_b.bin").read_bytes()


async def test_sync_demos_skips_unchanged_on_a_repeat_run(runner, fake_repl):
    first = await designs.sync_demos(runner, DEMOS)
    assert first == {"synced": ["tt_um_demo_a", "tt_um_demo_b"], "skipped": []}
    second = await designs.sync_demos(runner, DEMOS)
    assert second == {"synced": [], "skipped": ["tt_um_demo_a", "tt_um_demo_b"]}


async def test_sync_demos_resyncs_when_content_changes_even_if_size_matches(runner, fake_repl, tmp_path):
    # The old size-only comparison would never notice this: iCE40 bitstreams
    # for one part are typically all the same length, so a released update
    # to a demo (new content, same size) would have been skipped forever.
    demos_dir = tmp_path / "demos"
    demos_dir.mkdir()
    (demos_dir / "index.json").write_text('{"demos": [{"name": "tt_um_demo_a"}]}')
    original = (DEMOS / "tt_um_demo_a.bin").read_bytes()
    (demos_dir / "tt_um_demo_a.bin").write_bytes(original)
    out1 = await designs.sync_demos(runner, demos_dir)
    assert out1 == {"synced": ["tt_um_demo_a"], "skipped": []}

    tampered = bytes(b ^ 0xFF for b in original)  # same size, different bytes
    assert len(tampered) == len(original)
    (demos_dir / "tt_um_demo_a.bin").write_bytes(tampered)  # simulates an updated demo release
    out2 = await designs.sync_demos(runner, demos_dir)
    assert out2 == {"synced": ["tt_um_demo_a"], "skipped": []}
    assert board_file(fake_repl, "tt_um_demo_a").read_bytes() == tampered


async def test_sync_demos_removes_stale_demo_dropped_from_index(runner, fake_repl, tmp_path):
    demos_dir = tmp_path / "demos"
    demos_dir.mkdir()
    (demos_dir / "index.json").write_text('{"demos": [{"name": "tt_um_demo_a"}, {"name": "tt_um_demo_b"}]}')
    (demos_dir / "tt_um_demo_a.bin").write_bytes((DEMOS / "tt_um_demo_a.bin").read_bytes())
    (demos_dir / "tt_um_demo_b.bin").write_bytes((DEMOS / "tt_um_demo_b.bin").read_bytes())
    board_file(fake_repl, "my_upload").write_bytes(PRE)  # unrelated upload, never in any manifest

    out1 = await designs.sync_demos(runner, demos_dir)
    assert out1 == {"synced": ["tt_um_demo_a", "tt_um_demo_b"], "skipped": []}
    assert board_file(fake_repl, "tt_um_demo_b").exists()

    # demo_b dropped from the index (e.g. removed in a later release)
    (demos_dir / "index.json").write_text('{"demos": [{"name": "tt_um_demo_a"}]}')
    out2 = await designs.sync_demos(runner, demos_dir)
    assert out2 == {"synced": [], "skipped": ["tt_um_demo_a"]}
    assert not board_file(fake_repl, "tt_um_demo_b").exists()  # stale demo removed
    assert board_file(fake_repl, "my_upload").exists()  # never touched -- not in any manifest


async def test_sync_demos_skips_manifest_write_when_nothing_changed(runner, fake_repl):
    await designs.sync_demos(runner, DEMOS)  # first run: writes files + manifest
    write_pattern = b"open('/bitstreams/.demos.json', 'w')"
    count_after_first = bytes(fake_repl.transcript).count(write_pattern)
    assert count_after_first == 1

    out2 = await designs.sync_demos(runner, DEMOS)  # nothing changed
    assert out2 == {"synced": [], "skipped": ["tt_um_demo_a", "tt_um_demo_b"]}
    count_after_second = bytes(fake_repl.transcript).count(write_pattern)
    assert count_after_second == count_after_first  # no additional manifest write


async def test_sync_demos_with_empty_index_touches_nothing(runner, fake_repl, tmp_path):
    # No index.json at all -- load_demo_index returns {} -- must skip the
    # board round trip entirely: not one byte should cross the wire.
    out = await designs.sync_demos(runner, tmp_path / "no-such-demos-dir")
    assert out == {"synced": [], "skipped": []}
    assert fake_repl.transcript == b""


async def test_sync_demos_removes_stale_tmp_files(runner, fake_repl):
    # A prior crash mid-write can leave a straggling *.tmp; sync_demos must
    # sweep it before anything else, even though it's not a demo itself.
    await designs.sync_demos(runner, DEMOS)  # first run: populates the manifest
    stale = fake_repl.root / "bitstreams" / "x.bin.tmp"
    stale.write_bytes(b"partial")
    out = await designs.sync_demos(runner, DEMOS)
    assert out == {"synced": [], "skipped": ["tt_um_demo_a", "tt_um_demo_b"]}
    assert not stale.exists()


async def test_sync_demos_creates_bitstreams_dir_when_missing(runner, fake_repl):
    import shutil

    shutil.rmtree(fake_repl.root / "bitstreams")
    out = await designs.sync_demos(runner, DEMOS)
    assert out == {"synced": ["tt_um_demo_a", "tt_um_demo_b"], "skipped": []}
    assert (fake_repl.root / "bitstreams").is_dir()


async def test_list_designs_handles_missing_bitstreams_dir(runner, fake_repl):
    import shutil

    shutil.rmtree(fake_repl.root / "bitstreams")
    body = await designs.list_designs(runner, DEMOS)
    assert body == {"enabled": None, "designs": []}


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
