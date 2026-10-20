"""作業ディレクトリ (収集器が書いている) の CSV/ログを repo の data/odds_ts/ に合流させて commit + push する。

    python3 scripts/sync_odds.py WORK_DIR YYYYMMDD "odds_ts 20261024 morning"

- 収集器のファイルには一切触らない (読むだけ)。書きかけの最終行 (改行で終わっていない部分) は今回は取らない。
- 毎回 origin/main に reset --hard してから「origin の行 + まだ無い作業側の行」を書き、commit して push する。
  push が拒否されたら (他のジョブが先に push した) 取り直してやり直す (最大 5 回)。
  追記専用の CSV なので行の和集合で合流でき、rebase の衝突は起きない。
- その日の races.csv が無い (= レースの無い日) なら何もしない。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DST = REPO / "data" / "odds_ts"


def git(*args, check=True):
    return subprocess.run(["git", "-C", str(REPO), *args], check=check, text=True, capture_output=True)


def complete_lines(p: Path) -> list[str]:
    b = p.read_bytes()
    cut = b.rfind(b"\n")
    if cut < 0:
        return []
    return b[: cut + 1].decode("utf-8", errors="replace").splitlines(keepends=True)


def merge_into(src: Path, dst: Path) -> None:
    new = complete_lines(src)
    old = complete_lines(dst) if dst.exists() else []
    seen = set(old)
    add = []
    for ln in new:
        if ln not in seen:
            seen.add(ln)
            add.append(ln)
    if add:
        with open(dst, "a", encoding="utf-8", newline="") as f:
            f.writelines(add)


def main():
    work, ymd, msg = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    if not (work / f"{ymd}_races.csv").exists():
        print(f"sync: {ymd}_races.csv が無い (レースの無い日)。commit しません")
        return 0
    files = sorted(work.glob(f"{ymd}_*.csv")) + [p for p in [work / "collector.log"] if p.exists()]
    for attempt in range(1, 6):
        git("fetch", "--quiet", "origin", "main")
        git("reset", "--quiet", "--hard", "origin/main")
        DST.mkdir(parents=True, exist_ok=True)
        for p in files:
            merge_into(p, DST / p.name)
        git("add", "-A", str(DST))
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("sync: 変更なし")
            return 0
        git("commit", "--quiet", "-m", msg)
        r = git("push", "--quiet", "origin", "HEAD:main", check=False)
        if r.returncode == 0:
            print(f"sync: push 済み ({attempt} 回目) {git('rev-parse', '--short', 'HEAD').stdout.strip()}")
            return 0
        print(f"sync: push 失敗 ({attempt} 回目): {r.stderr.strip()[:300]}")
        time.sleep(5 * attempt)
    print("sync: 5 回失敗。artifact を見てください", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
