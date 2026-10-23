"""CI: 試験出力の末尾を GitHub annotation として出す (ログ本体を取得できない環境向け)。"""

from __future__ import annotations

import sys
from pathlib import Path


def main(paths: list[str]) -> None:
    for name in paths:
        p = Path(name)
        if not p.exists():
            print(f"::error title={p.name}::出力ファイルがありません")
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
        text = "\n".join(lines).replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::error title={p.name}::{text}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1:])
