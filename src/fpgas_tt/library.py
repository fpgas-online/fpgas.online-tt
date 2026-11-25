"""The VGA library's cache on the Pi: prebuilt bitstreams the site pushes, kept by id, evicted by age.

The site (fpgas.online-site, on the gateway) fetches the library's index and bitstreams from the archive,
checks them, and pushes each one here with ``POST /library/{id}`` before it asks for a load. The Pi fetches
nothing. Each entry is two files in ``--library-dir``: ``<id>.bin``, the bytes, and ``<id>.json``,
``{"id", "bin_sha256", "bin_bytes"}`` and nothing else. All other metadata (title, clock, pinout, inputs)
lives only in the site's copy of the index.

The total size of the ``.bin`` files is capped (``--library-max-bytes``); a store evicts the entries least
recently used, by the mtime of their ``.json``, which a push or a load refreshes. The cache never touches the
uploads, the demos or the idle design. On the fleet's Pis the state directory is in RAM, so the cache is lost
at a reboot and the site pushes again.

This module imports nothing from ``designs`` (``designs`` imports it); ``designs`` checks a name with its
``NAME_RE`` before it asks here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# In the service's StateDirectory (debian/fpgas-tt.service), beside the uploads.
LIBRARY_DIR_DEFAULT = Path("/var/lib/fpgas-tt/library")
MAX_BYTES_ENV = "FPGAS_TT_LIBRARY_MAX_BYTES"
MAX_BYTES_DEFAULT = 16 * 1024 * 1024  # about 160 designs of the iCE40UP5K's 104090 bytes
BIN_MAX_BYTES = 256 * 1024  # designs.MAX_BITSTREAM_BYTES: the same cap, kept here so nothing is imported
META_KEYS = ("id", "bin_sha256", "bin_bytes")
_ID_RE = re.compile(r"^[a-z0-9_]{1,40}\Z")  # designs.NAME_RE
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")

# Held for every write and eviction, so two pushes never remove each other's files. Uploads have their own
# lock (designs._STORE_LOCK) and their own directory.
_LIBRARY_LOCK = threading.Lock()


def parse_max_bytes(text: str) -> int:
    """A cap given as text (the environment, the command line): a positive whole number of bytes."""
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"library cap must be a whole number of bytes, not {text!r}") from None
    if value < 1:
        raise ValueError(f"library cap must be at least 1 byte, not {value}")
    return value


def max_bytes_default() -> int:
    """The cap when ``--library-max-bytes`` is not given: ``$FPGAS_TT_LIBRARY_MAX_BYTES``, else 16 MiB."""
    raw = os.environ.get(MAX_BYTES_ENV)
    return MAX_BYTES_DEFAULT if raw is None else parse_max_bytes(raw)


def check_meta(meta: object) -> str | None:
    """The first thing wrong with a pushed entry's ``meta``, or None when it is right."""
    if not isinstance(meta, dict):
        return "must be a JSON object"
    if set(meta) != set(META_KEYS):
        extra = sorted(set(meta) - set(META_KEYS))
        missing = [k for k in META_KEYS if k not in meta]
        what = "; ".join(
            [*(["unknown keys: " + ", ".join(extra)] if extra else []),
             *(["missing keys: " + ", ".join(missing)] if missing else [])]
        )  # fmt: skip
        return f"keys must be exactly id, bin_sha256 and bin_bytes ({what})"
    if not isinstance(meta["id"], str) or not _ID_RE.match(meta["id"]):
        return "id must match ^[a-z0-9_]{1,40}$"
    if not isinstance(meta["bin_sha256"], str) or not _SHA256_RE.match(meta["bin_sha256"]):
        return "bin_sha256 must be 64 lowercase hex digits"
    n = meta["bin_bytes"]
    # bool is a subclass of int: {"bin_bytes": true} must not become 1
    if isinstance(n, bool) or not isinstance(n, int) or not (1 <= n <= BIN_MAX_BYTES):
        return f"bin_bytes must be an integer between 1 and {BIN_MAX_BYTES}"
    return None


def bin_path(library_dir: Path, id: str) -> Path:
    return Path(library_dir) / f"{id}.bin"


def _json_path(library_dir: Path, id: str) -> Path:
    return Path(library_dir) / f"{id}.json"


def _read_meta(library_dir: Path, id: str) -> dict | None:
    """The entry's own record, or None when it is missing, unreadable, or not for this id."""
    try:
        with open(_json_path(library_dir, id), encoding="utf-8") as f:
            meta = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("library: %s.json is unreadable (%s)", id, exc)
        return None
    if check_meta(meta) is not None or meta["id"] != id:
        log.warning("library: %s.json is not a record of %s", id, id)
        return None
    return meta


def _files(library_dir: Path) -> list[str]:
    try:
        return os.listdir(library_dir)
    except FileNotFoundError:
        return []


def entries(library_dir: Path) -> list[dict]:
    """Every entry kept here, by id: ``{"id", "bin_sha256", "bin_bytes", "used_at"}``. An entry needs both
    of its files and a good record; a ``.bin`` alone is never listed. Nothing is hashed."""
    library_dir = Path(library_dir)
    files = set(_files(library_dir))
    out = []
    for f in sorted(files):
        id = f[:-5]
        if not f.endswith(".json") or not _ID_RE.match(id) or f"{id}.bin" not in files:
            continue
        meta = _read_meta(library_dir, id)
        if meta is None:
            continue
        try:  # an entry can be evicted between the listing and here
            mtime = _json_path(library_dir, id).stat().st_mtime
        except FileNotFoundError:
            continue
        used_at = datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({**meta, "used_at": used_at})
    return out


def names(library_dir: Path) -> set[str]:
    return {e["id"] for e in entries(library_dir)}


def used_bytes(library_dir: Path) -> int:
    """The total size of the entries' ``.bin`` files, the number the cap is checked against."""
    return sum(e["bin_bytes"] for e in entries(library_dir))


def touch(library_dir: Path, id: str) -> None:
    """Mark entry `id` as used now. An entry evicted since it was read is not an error."""
    with _LIBRARY_LOCK:
        try:
            os.utime(_json_path(library_dir, id))
        except FileNotFoundError:
            pass


def _write(path: Path, data: bytes) -> None:
    """Put `data` at `path` whole or not at all: a temporary file beside it, then a rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _remove(library_dir: Path, id: str) -> None:
    # the record first: an entry without its record is never listed, and the next store removes the .bin
    _json_path(library_dir, id).unlink(missing_ok=True)
    bin_path(library_dir, id).unlink(missing_ok=True)


def _remove_orphans(library_dir: Path) -> None:
    """Under the lock: whatever a store cut short left behind. A ``.bin`` without its ``.json``, a ``.json``
    without its ``.bin`` (or one that is not a good record), and every temporary file. Every write takes the
    lock, so no temporary file is in use while this runs."""
    files = set(_files(library_dir))
    for f in files:
        if f.startswith(".") and f.endswith(".tmp"):
            log.info("library: removing the leftover %s", f)
            (library_dir / f).unlink(missing_ok=True)
    ids = {f[:-4] for f in files if f.endswith(".bin")} | {f[:-5] for f in files if f.endswith(".json")}
    for id in ids:
        if not _ID_RE.match(id):
            continue  # not a name an entry could have: not this module's file
        whole = f"{id}.bin" in files and f"{id}.json" in files and _read_meta(library_dir, id) is not None
        if not whole:
            log.info("library: removing the incomplete entry %s", id)
            _remove(library_dir, id)


def store(library_dir: Path, id: str, sha256: str, data: bytes, max_bytes: int) -> tuple[bool, list[str]]:
    """Keep entry `id` (the bytes `data`, whose SHA-256 the caller has checked is `sha256`) and evict the
    least recently used other entries until the total fits `max_bytes`. Returns ``(created, evicted)``:
    `created` is False when the same build was already kept (only its use time is refreshed). An older build
    of `id` is replaced. The entry just stored is never evicted, even when it alone is over the cap."""
    library_dir = Path(library_dir)
    with _LIBRARY_LOCK:
        library_dir.mkdir(parents=True, exist_ok=True)
        _remove_orphans(library_dir)
        old = _read_meta(library_dir, id)
        if old is not None and old["bin_sha256"] == sha256:
            os.utime(_json_path(library_dir, id))
            return False, []
        meta = {"id": id, "bin_sha256": sha256, "bin_bytes": len(data)}
        # the .bin first: a store cut short between the two leaves a .bin alone, which is never listed
        _write(bin_path(library_dir, id), data)
        _write(_json_path(library_dir, id), json.dumps(meta).encode("utf-8"))
        kept = entries(library_dir)
        total = sum(e["bin_bytes"] for e in kept)
        others = [e for e in kept if e["id"] != id]
        others.sort(key=lambda e: (_json_path(library_dir, e["id"]).stat().st_mtime, e["id"]))
        evicted = []
        for e in others:
            if total <= max_bytes:
                break
            _remove(library_dir, e["id"])
            total -= e["bin_bytes"]
            evicted.append(e["id"])
        if evicted:
            log.info("library: evicted %s to keep %s", ", ".join(evicted), id)
        return True, evicted
