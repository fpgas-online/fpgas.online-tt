"""The VGA library's cache on the Pi (library.py): no board involved."""

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path

import pytest

from fpgas_tt import designs, library

LIB = Path(__file__).parent / "data" / "library"  # a copy of tinytapeout-vga-videos' fixture


def fixture_entries() -> list[tuple[str, str, bytes]]:
    """(id, bin_sha256, bytes) of each entry of the copied fixture, read from its own index."""
    doc = json.loads((LIB / "fpga-library.json").read_text())
    return [(d["id"], d["bin_sha256"], (LIB / d["bin"]).read_bytes()) for d in doc["designs"]]


ENTRIES = fixture_entries()


@pytest.fixture
def lib(tmp_path) -> Path:
    return tmp_path / "library"


def files(path: Path) -> list[str]:
    return sorted(f.name for f in path.iterdir())


def age(lib: Path, id: str, when: float) -> None:
    os.utime(lib / f"{id}.json", (when, when))


def test_the_fixture_copy_is_what_its_index_says():
    assert len(ENTRIES) == 5
    for id, sha, data in ENTRIES:
        assert len(data) == 4096 and hashlib.sha256(data).hexdigest() == sha, id


def test_store_writes_bin_then_json_and_lists_it(lib, monkeypatch):
    id, sha, data = ENTRIES[0]
    order = []
    real = library.os.replace

    def seen(src, dst):
        order.append(Path(dst).name)
        real(src, dst)

    monkeypatch.setattr(library.os, "replace", seen)
    assert library.store(lib, id, sha, data, 1 << 20) == (True, [])
    assert order == [f"{id}.bin", f"{id}.json"]
    assert files(lib) == [f"{id}.bin", f"{id}.json"]  # no temporary file left
    assert library.bin_path(lib, id).read_bytes() == data
    assert json.loads((lib / f"{id}.json").read_text()) == {"id": id, "bin_sha256": sha, "bin_bytes": 4096}
    [entry] = library.entries(lib)
    assert {k: entry[k] for k in ("id", "bin_sha256", "bin_bytes")} == {"id": id, "bin_sha256": sha, "bin_bytes": 4096}
    assert entry["used_at"].endswith("Z") and len(entry["used_at"]) == len("2026-11-26T04:10:00Z")
    assert library.names(lib) == {id}
    assert library.used_bytes(lib) == 4096


def test_an_empty_or_missing_library_has_nothing(lib):
    assert library.entries(lib) == [] and library.names(lib) == set() and library.used_bytes(lib) == 0


def test_same_sha_push_is_idempotent_and_refreshes_lru(lib):
    id, sha, data = ENTRIES[0]
    library.store(lib, id, sha, data, 1 << 20)
    age(lib, id, 1000)
    before = library.bin_path(lib, id).stat().st_mtime_ns
    assert library.store(lib, id, sha, data, 1 << 20) == (False, [])
    assert (lib / f"{id}.json").stat().st_mtime > 1000  # used now
    assert library.bin_path(lib, id).stat().st_mtime_ns == before  # not written again


def test_new_build_replaces_old_bytes(lib):
    id, sha, data = ENTRIES[0]
    library.store(lib, id, sha, data, 1 << 20)
    new = data[:8] + b"\x42" + data[9:] + b"\x00" * 100
    new_sha = hashlib.sha256(new).hexdigest()
    assert library.store(lib, id, new_sha, new, 1 << 20) == (True, [])
    assert library.bin_path(lib, id).read_bytes() == new
    assert [(e["bin_sha256"], e["bin_bytes"]) for e in library.entries(lib)] == [(new_sha, len(new))]
    assert files(lib) == [f"{id}.bin", f"{id}.json"]


def test_a_replacement_cut_short_never_leaves_the_old_record_on_the_new_bytes(lib, monkeypatch):
    id, sha, data = ENTRIES[0]
    library.store(lib, id, sha, data, 1 << 20)
    new = data[:8] + b"\x42" + data[9:]
    real = library._write

    def cut_short_at_the_record(path, payload):
        if path.suffix == ".json":
            raise OSError(28, "No space left on device")
        real(path, payload)

    monkeypatch.setattr(library, "_write", cut_short_at_the_record)
    with pytest.raises(OSError, match="No space"):
        library.store(lib, id, hashlib.sha256(new).hexdigest(), new, 1 << 20)
    assert library.bin_path(lib, id).read_bytes() == new  # the new bytes landed ...
    assert library.entries(lib) == []  # ... and no record describes them as the old build
    monkeypatch.setattr(library, "_write", real)
    library.store(lib, *ENTRIES[1], 1 << 20)  # the next store removes the lone .bin
    assert files(lib) == [f"{ENTRIES[1][0]}.bin", f"{ENTRIES[1][0]}.json"]


def test_same_sha_with_a_bin_of_the_wrong_size_is_written_again(lib):
    id, sha, data = ENTRIES[0]
    library.store(lib, id, sha, data, 1 << 20)
    library.bin_path(lib, id).write_bytes(data[:100])  # not what its record says
    assert library.store(lib, id, sha, data, 1 << 20) == (True, [])
    assert library.bin_path(lib, id).read_bytes() == data
    assert [(e["bin_sha256"], e["bin_bytes"]) for e in library.entries(lib)] == [(sha, 4096)]


def test_lru_evicts_oldest_until_it_fits(lib):
    for i, (id, sha, data) in enumerate(ENTRIES[:3]):
        assert library.store(lib, id, sha, data, 3 * 4096) == (True, [])
        age(lib, id, 1000 + i)
    # the oldest by use, not by store: entry 0 was used last
    age(lib, ENTRIES[0][0], 2000)
    id, sha, data = ENTRIES[3]
    assert library.store(lib, id, sha, data, 3 * 4096) == (True, [ENTRIES[1][0]])
    assert library.names(lib) == {ENTRIES[0][0], ENTRIES[2][0], id}
    assert library.used_bytes(lib) == 3 * 4096
    assert not library.bin_path(lib, ENTRIES[1][0]).exists()


def test_the_entry_just_stored_is_kept_even_alone_over_the_cap(lib):
    for id, sha, data in ENTRIES[:2]:
        library.store(lib, id, sha, data, 1 << 20)
    id, sha, data = ENTRIES[2]
    created, evicted = library.store(lib, id, sha, data, 100)
    assert created and sorted(evicted) == sorted(e[0] for e in ENTRIES[:2])
    assert library.names(lib) == {id}


def test_orphans_are_removed_and_never_counted(lib):
    id, sha, data = ENTRIES[0]
    lib.mkdir()
    (lib / "lone_bin.bin").write_bytes(data)  # a store cut short between its two writes
    (lib / "lone_json.json").write_text(json.dumps({"id": "lone_json", "bin_sha256": sha, "bin_bytes": 4096}))
    (lib / ".x.tmp").write_bytes(b"half a write")
    assert library.entries(lib) == [] and library.used_bytes(lib) == 0  # never listed, never counted
    library.store(lib, id, sha, data, 1 << 20)
    assert files(lib) == [f"{id}.bin", f"{id}.json"]


def test_a_record_that_is_not_its_entrys_is_an_orphan(lib):
    id, sha, data = ENTRIES[0]
    lib.mkdir()
    (lib / "bad_json.bin").write_bytes(data)
    (lib / "bad_json.json").write_text("{not json")
    (lib / "wrong_id.bin").write_bytes(data)
    (lib / "wrong_id.json").write_text(json.dumps({"id": "other", "bin_sha256": sha, "bin_bytes": 4096}))
    assert library.entries(lib) == []
    library.store(lib, id, sha, data, 1 << 20)
    assert files(lib) == [f"{id}.bin", f"{id}.json"]


def test_touch_after_eviction_is_harmless(lib):
    id, sha, data = ENTRIES[0]
    library.store(lib, id, sha, data, 4096)
    library.store(lib, *ENTRIES[1], 4096)  # evicts id
    assert id not in library.names(lib)
    library.touch(lib, id)  # no error
    library.touch(lib / "never-made", id)
    assert id not in library.names(lib)


def test_touch_refreshes_the_use_time(lib):
    id, sha, data = ENTRIES[0]
    library.store(lib, id, sha, data, 1 << 20)
    age(lib, id, 1000)
    library.touch(lib, id)
    assert (lib / f"{id}.json").stat().st_mtime > 1000


def test_eviction_never_touches_uploads(tmp_path, lib):
    uploads = tmp_path / "pi-uploads"
    for i in range(3):
        designs.store_upload(uploads, f"up_{i}", b"\x7e\xaa\x99\x7e" + bytes([i]) * 8000)
    before = {f: (uploads / f).read_bytes() for f in files(uploads)}
    for id, sha, data in ENTRIES:
        library.store(lib, id, sha, data, 2 * 4096)  # evicts on every store after the second
    assert {f: (uploads / f).read_bytes() for f in files(uploads)} == before
    # and the other way: uploads making room never remove a library entry
    kept = files(lib)
    for i in range(designs.MAX_UPLOADS + 2):
        designs.store_upload(uploads, f"more_{i}", b"\x7e\xaa\x99\x7e")
    assert files(lib) == kept


@pytest.mark.parametrize(
    "meta,error",
    [
        ({"id": "a", "bin_sha256": "0" * 64, "bin_bytes": 1, "title": "x"}, "unknown keys: title"),
        ({"id": "a", "bin_sha256": "0" * 64}, "missing keys: bin_bytes"),
        ({"id": "a", "bin_sha256": "0" * 64, "bin_bytes": True}, "bin_bytes must be an integer"),
        ({"id": "a", "bin_sha256": "0" * 64, "bin_bytes": 1.0}, "bin_bytes must be an integer"),
        ({"id": "a", "bin_sha256": "0" * 64, "bin_bytes": 0}, "bin_bytes must be an integer"),
        ({"id": "a", "bin_sha256": "0" * 64, "bin_bytes": 256 * 1024 + 1}, "bin_bytes must be an integer"),
        ({"id": "a", "bin_sha256": "A" * 64, "bin_bytes": 1}, "bin_sha256 must be 64 lowercase hex"),
        ({"id": "a", "bin_sha256": "0" * 63, "bin_bytes": 1}, "bin_sha256 must be 64 lowercase hex"),
        ({"id": "Bad Id", "bin_sha256": "0" * 64, "bin_bytes": 1}, "id must match"),
        ({"id": 7, "bin_sha256": "0" * 64, "bin_bytes": 1}, "id must match"),
        (["id", "bin_sha256", "bin_bytes"], "must be a JSON object"),
        ({"id": "a", "bin_sha256": "0" * 64, "bin_bytes": 1}, None),
    ],
)
def test_check_meta_rules(meta, error):
    got = library.check_meta(meta)
    if error is None:
        assert got is None
    else:
        assert got is not None and error in got


def test_concurrent_stores_lose_nothing(lib):
    """Eight pushes at once: each is kept or evicted whole, and the total fits."""
    pushes = []
    for i in range(8):
        data = ENTRIES[0][2][:8] + bytes([i + 10]) + ENTRIES[0][2][9:]
        pushes.append((f"push_{i}", hashlib.sha256(data).hexdigest(), data))
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        done = [pool.submit(library.store, lib, id, sha, data, 5 * 4096) for id, sha, data in pushes]
        results = [f.result() for f in done]  # .result() raises what the store raised
    assert all(created for created, _ in results)
    evicted = [n for _, ev in results for n in ev]
    assert len(evicted) == len(set(evicted)) == 3
    kept = library.names(lib)
    assert len(kept) == 5 and kept.isdisjoint(evicted) and kept | set(evicted) == {p[0] for p in pushes}
    assert library.used_bytes(lib) == 5 * 4096
    for id, sha, data in pushes:
        if id in kept:
            assert library.bin_path(lib, id).read_bytes() == data
    assert sorted(files(lib)) == sorted([f"{n}.bin" for n in kept] + [f"{n}.json" for n in kept])


def test_max_bytes_default_reads_env(monkeypatch):
    monkeypatch.delenv(library.MAX_BYTES_ENV, raising=False)
    assert library.max_bytes_default() == 16777216
    monkeypatch.setenv("FPGAS_TT_LIBRARY_MAX_BYTES", "1048576")
    assert library.max_bytes_default() == 1048576
    for bad in ("lots", "0", "-5", ""):
        monkeypatch.setenv("FPGAS_TT_LIBRARY_MAX_BYTES", bad)
        with pytest.raises(ValueError, match="library cap"):
            library.max_bytes_default()


def test_library_imports_nothing_from_designs():
    source = Path(library.__file__).read_text()
    assert "import designs" not in source and "from fpgas_tt.designs" not in source
    assert "from fpgas_tt import designs" not in source
