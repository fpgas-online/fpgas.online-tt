"""Write the REPL-text snapshots the tests compare against: `uv run tests/data/make_snapshots.py`.

safe_code_retry_false.txt / safe_code_retry_true.txt pin SAFE_CODE byte for byte (test_safe).
load_code_null_ui_in.txt is the last step of `_load_steps("x", DATA, 1000)` before the ui_in release and drive
were added (`--load-code` rewrites it); no test reads it, it only shows what that change did to LOAD_CODE.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
DATA = b"\x7e\xaa\x99\x7e" + bytes(60)


def main(argv: list[str]) -> None:
    from fpgas_tt import designs, safe  # noqa: PLC0415 - run as a script

    (HERE / "safe_code_retry_false.txt").write_text(safe.safe_code(False))
    (HERE / "safe_code_retry_true.txt").write_text(safe.safe_code(True))
    if "--load-code" in argv:
        (HERE / "load_code_null_ui_in.txt").write_text(designs._load_steps("x", DATA, 1000)[-1])


if __name__ == "__main__":
    main(sys.argv[1:])
