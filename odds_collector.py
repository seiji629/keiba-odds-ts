"""レース当日の単勝 (と複勝) オッズを時系列で記録する収集器。

    python3 odds_collector.py                      # 今日 (ローカル日付) の全レース
    python3 odds_collector.py --date 20261024 --place
    python3 odds_collector.py --until 16:30        # 16:30 で打ち切り
    python3 odds_collector.py --once 202605040611  # 1 レースの現在値を表示するだけ
    python3 odds_collector.py --dry-run --dry-run-minutes 4 --place

方針 (README「直前オッズの 1 分刻み収集」):
- 細かい段階: 発走 T の 30 分前から単勝を 30 秒おき (--place なら複勝を 60 秒おき)。
  T 以後も 30 秒おきに取り、official_datetime が T 以後の値のまま 3 回続けて変わらない (= 確定オッズ) か
  T+20 分で終了。各場で T-30 に入ったレースはすべて細かい段階になる (前のレースの確定待ちと次のレースの T-30 は重なり得る)。
- 粗い段階: まだ細かい段階に入っていないレースは単勝を 10 分おき (起動直後は 10 分の中に散らして始める)。
- 通信はすべて直列、どの 2 つのリクエストの間も MIN_GAP 秒 (>= 1.0) 以上空ける。細かい段階が優先。
- 403/429 または 5 回連続失敗で 5 分休む (速めることはしない)。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "odds_ts"

ODDS_URL = "https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={race_id}&type={t}&action=init"
LIST_URL = "https://race.netkeiba.com/top/race_list_sub.html?kaisai_date={ymd}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120 Safari/537.36"}
TIMEOUT = 20
MIN_GAP = 1.1             # 秒。どの 2 リクエストの間も最低これだけ空ける (規約は >= 1.0)
FINE_START = 30 * 60      # T-30 分から細かい段階
FINE_WIN = 30             # 単勝 30 秒おき
FINE_PLACE = 60           # 複勝 60 秒おき
COARSE = 10 * 60          # 粗い段階 10 分おき
POST_MAX = 20 * 60        # T+20 分で打ち切り (確定は通常 T+5〜10 分、審議があると遅れる)
STABLE_N = 3              # T 以後 official_datetime が 3 回続けて同じなら確定とみなす
BACKOFF = 5 * 60
MAX_FAIL = 5
VENUES = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
          "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}

WIN_COLS = ["fetched_at", "official_datetime", "race_id", "umaban", "odds", "popularity"]
PLACE_COLS = ["fetched_at", "official_datetime", "race_id", "umaban", "odds_lo", "odds_hi", "popularity"]
POLL_COLS = ["fetched_at", "race_id", "type", "http_status", "official_datetime", "changed",
             "elapsed_ms", "phase", "ts"]

STOP = False
LOGF = None


def now() -> datetime:
    return datetime.now()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def log(msg: str, loud: bool = False) -> None:
    line = f"{iso(now())} {'!!! ' if loud else ''}{msg}"
    print(line, flush=True)
    if LOGF is not None:
        LOGF.write(line + "\n")
        LOGF.flush()


def _on_signal(signum, _frame):
    global STOP
    STOP = True
    log(f"signal {signum} を受信、終了します")


def nap(sec: float) -> None:
    """STOP を見ながら眠る。"""
    end = time.monotonic() + sec
    while not STOP:
        rest = end - time.monotonic()
        if rest <= 0:
            return
        time.sleep(min(rest, 0.5))


# ---------------------------------------------------------------------------
# 通信 (直列、全体で 1 本の間隔制御)
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, polls_writer=None):
        self.s = requests.Session()
        self.last = None          # 直前リクエスト開始時刻 (monotonic)
        self.n = 0
        self.t0 = time.monotonic()
        self.fail = 0
        self.polls = polls_writer

    def _throttle(self):
        if self.last is not None:
            wait = MIN_GAP - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            gap = time.monotonic() - self.last
            assert gap >= 1.0, f"throttle violated: {gap:.3f}s"
        self.last = time.monotonic()
        self.n += 1

    def get(self, url: str):
        """(http_status, text or None, elapsed_ms, epoch_ts)。例外は status=0。"""
        self._throttle()
        ts = time.time()
        t = time.monotonic()
        try:
            r = self.s.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.encoding = "UTF-8"
            st, body = r.status_code, r.text
        except requests.RequestException as e:
            st, body = 0, None
            log(f"request error {type(e).__name__}: {url}")
        ms = int((time.monotonic() - t) * 1000)
        if st == 200:
            self.fail = 0
        else:
            self.fail += 1
        if st in (403, 429):
            log(f"HTTP {st} — ブロックの恐れ。{BACKOFF // 60} 分休みます ({url})", loud=True)
            self.backoff()
        elif self.fail >= MAX_FAIL:
            log(f"{self.fail} 回連続失敗。{BACKOFF // 60} 分休みます", loud=True)
            self.backoff()
        return st, (body if st == 200 else None), ms, ts

    def backoff(self):
        nap(BACKOFF)
        self.fail = 0

    def rate(self) -> float:
        el = time.monotonic() - self.t0
        return self.n / el if el > 0 else 0.0


def parse_odds(text: str | None, t: int):
    """-> (official_datetime str or '', {umaban: [v1, v2, v3]})"""
    if not text:
        return "", {}
    try:
        j = json.loads(text)
    except ValueError:
        return "", {}
    d = j.get("data") or {}
    if not isinstance(d, dict):
        return "", {}
    off = d.get("official_datetime") or ""
    odds = d.get("odds") or {}
    rows = odds.get(str(t)) or {} if isinstance(odds, dict) else {}
    return off, rows if isinstance(rows, dict) else {}


def _f(x):
    try:
        v = float(x)
        return v if v > 0 else ""
    except (TypeError, ValueError):
        return ""


def _i(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# 出走表 (レース一覧)
# ---------------------------------------------------------------------------
def schedule_from_list(client: Client, ymd: str):
    st, text, ms, ts = client.get(LIST_URL.format(ymd=ymd))
    if client.polls is not None:
        client.polls.row(["list", "list", st, "", "", ms, "schedule", ts], ts)
    if not text:
        log(f"レース一覧の取得に失敗 (HTTP {st})")
        return []
    ids, times, seen = [], [], set()
    # race_id とその後の最初の時刻を対応させる (同じ race_id のリンクは複数ある)
    for m in re.finditer(r"race_id=(\d{12})|RaceList_Itemtime\">\s*(\d{1,2}:\d{2})", text):
        if m.group(1):
            rid = m.group(1)
            if rid not in seen:
                seen.add(rid)
                ids.append(rid)
                times.append(None)
        elif m.group(2) and ids and times[-1] is None:
            times[-1] = m.group(2)
    n_time = len(re.findall(r"RaceList_Itemtime", text))
    log(f"レース一覧: race_id {len(ids)} 件、時刻 {n_time} 件")
    out = []
    for rid, hm in zip(ids, times):
        if hm is None:
            continue
        out.append(dict(race_id=rid, venue=rid[4:6], race_no=int(rid[10:12]),
                        post_time=f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]} {int(hm[:-3]):02d}:{hm[-2:]}",
                        source="netkeiba"))
    return out


def schedule_from_jrdb(ymd: str):
    try:
        sys.path.insert(0, str(ROOT))
        import jrdb_parse
        t = jrdb_parse.build_today(f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}")
    except Exception as e:  # noqa: BLE001
        log(f"JRDB からの出走表作成に失敗: {e}")
        return []
    if t is None or len(t) == 0 or "post_time" not in t:
        return []
    t = t.drop_duplicates("race_id").dropna(subset=["post_time"])
    return [dict(race_id=str(r.race_id), venue=str(r.race_id)[4:6], race_no=int(str(r.race_id)[10:12]),
                 post_time=f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]} {r.post_time}", source="jrdb_bac")
            for r in t.itertuples()]


# ---------------------------------------------------------------------------
# 書き込み (追記・毎回 flush)
# ---------------------------------------------------------------------------
class Appender:
    def __init__(self, path: Path, cols):
        self.path = path
        new = not path.exists() or path.stat().st_size == 0
        self.f = open(path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if new:
            self.w.writerow(cols)
            self.f.flush()

    def rows(self, rows):
        self.w.writerows(rows)
        self.f.flush()

    def row(self, r, ts=None):
        if ts is not None:  # polls 用: 先頭に fetched_at、末尾に ts
            r = [iso(datetime.fromtimestamp(ts))] + r[:-1] + [f"{r[-1]:.3f}"]
        self.w.writerow(r)
        self.f.flush()

    def close(self):
        self.f.close()


def last_official(path: Path) -> dict:
    """再起動時: レースごとに最後に保存した official_datetime。"""
    out = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out[r["race_id"]] = r["official_datetime"]
    return out


# ---------------------------------------------------------------------------
# 本体
# ---------------------------------------------------------------------------
@dataclass
class Race:
    race_id: str
    venue: str
    race_no: int
    post: datetime
    due_coarse: float = 0.0      # epoch
    due_win: float = 0.0
    due_place: float = 0.0
    fine: bool = False
    done: bool = False
    fine_first: dict = field(default_factory=lambda: {1: True, 2: True})
    prev_off: dict = field(default_factory=dict)   # 直前の poll の official (型ごと)
    stable: int = 0
    n_polls: int = 0


def phase_of(r: Race, t: datetime) -> str:
    if r.done:
        return "done"
    if t >= r.post - timedelta(seconds=FINE_START):
        return "fine"
    return "coarse"


def run(races_rows, tag: str, outdir: Path, place: bool, until: datetime | None,
        client: Client, polls: Appender, stop_at: datetime | None = None, relax_final: bool = False):
    t0 = now()
    races = []
    for d in races_rows:
        r = Race(d["race_id"], d["venue"], int(d["race_no"]), datetime.strptime(d["post_time"], "%Y-%m-%d %H:%M"))
        if t0 > r.post + timedelta(seconds=POST_MAX):
            r.done = True
        races.append(r)
    live = [r for r in races if not r.done]
    log(f"対象 {len(live)} / {len(races)} レース (終了済み {len(races) - len(live)})"
        + (f"、--place あり" if place else ""))
    if not live:
        log("すべてのレースが終了済みです。何もしません。")
        return 0
    win_w = Appender(outdir / f"{tag}_win.csv", WIN_COLS)
    place_w = Appender(outdir / f"{tag}_place.csv", PLACE_COLS) if place else None
    stored = {1: last_official(outdir / f"{tag}_win.csv"),
              2: last_official(outdir / f"{tag}_place.csv") if place else {}}

    # 粗い段階の初回は 10 分の中に散らす (起動直後に一斉に取らない)
    e0 = time.time()
    for i, r in enumerate(sorted(live, key=lambda x: x.post)):
        r.due_coarse = e0 + i * min(COARSE / max(len(live), 1), 10.0)

    def poll(r: Race, t: int, phase: str):
        st, text, ms, ts = client.get(ODDS_URL.format(race_id=r.race_id, t=t))
        off, rows = parse_odds(text, t)
        fetched = iso(datetime.fromtimestamp(ts))
        changed = bool(off) and off != stored[t].get(r.race_id)
        first_fine = phase == "fine" and r.fine_first[t]
        write = bool(rows) and (changed or first_fine)
        if write:
            if t == 1:
                win_w.rows([[fetched, off, r.race_id, int(u), _f(v[0]), _i(v[2] if len(v) > 2 else "")]
                            for u, v in sorted(rows.items())])
            else:
                place_w.rows([[fetched, off, r.race_id, int(u), _f(v[0]), _f(v[1] if len(v) > 1 else ""),
                               _i(v[2] if len(v) > 2 else "")] for u, v in sorted(rows.items())])
            stored[t][r.race_id] = off
            if phase == "fine":
                r.fine_first[t] = False
        polls.row([r.race_id, "win" if t == 1 else "place", st, off, int(changed), ms, phase, ts], ts)
        r.n_polls += 1
        return off, rows, text is not None

    def final_poll(r: Race):
        """細かい段階の最後: 必ず保存する。"""
        st, text, ms, ts = client.get(ODDS_URL.format(race_id=r.race_id, t=1))
        off, rows = parse_odds(text, 1)
        fetched = iso(datetime.fromtimestamp(ts))
        changed = bool(off) and off != stored[1].get(r.race_id)
        if rows:
            win_w.rows([[fetched, off, r.race_id, int(u), _f(v[0]), _i(v[2] if len(v) > 2 else "")]
                        for u, v in sorted(rows.items())])
            stored[1][r.race_id] = off
        polls.row([r.race_id, "win", st, off, int(changed), ms, "final", ts], ts)
        r.n_polls += 1

    last_status = 0.0
    try:
        while not STOP:
            tn = now()
            ep = time.time()
            if until and tn >= until:
                log(f"--until {until:%H:%M} に到達")
                break
            if stop_at and tn >= stop_at:
                log("dry-run の時間切れ")
                break
            live = [r for r in races if not r.done]
            if not live:
                log("全レースの細かい段階が終わりました")
                break
            # 細かい段階に入ったレース
            for r in live:
                if not r.fine and phase_of(r, tn) == "fine":
                    r.fine = True
                    r.due_win = ep
                    r.due_place = ep
                    log(f"{r.race_id} ({VENUES.get(r.venue, r.venue + '場')} {r.race_no}R 発走 {r.post:%H:%M}) 細かい段階へ")
            # 期限切れ (T+20)
            for r in live:
                if r.fine and tn >= r.post + timedelta(seconds=POST_MAX):
                    log(f"{r.race_id} T+20 分に到達 (確定を確認できず)。最終取得して終了")
                    final_poll(r)
                    r.done = True
            live = [r for r in races if not r.done]
            # 次に取るものを選ぶ: 細かい段階を優先、その中で期限の早い順
            tasks = []
            for r in live:
                if r.fine:
                    tasks.append((0, r.due_win, r, 1))
                    if place:
                        tasks.append((0, r.due_place, r, 2))
                else:
                    tasks.append((1, r.due_coarse, r, 1))
            due = [x for x in tasks if x[1] <= ep]
            if not due:
                nxt = min(x[1] for x in tasks) if tasks else ep + 1
                nap(min(max(nxt - ep, 0.05), 1.0))
                if time.monotonic() - last_status > 600:
                    last_status = time.monotonic()
                    log(f"状態: 残り {len(live)} レース、細かい段階 {sum(r.fine for r in live)}、"
                        f"リクエスト {client.n} 回、{client.rate():.3f} req/s")
                continue
            due.sort(key=lambda x: (x[0], x[1]))
            _, _, r, t = due[0]
            if r.fine:
                phase = "fine"
                off, rows, ok = poll(r, t, phase)
                base = time.time()
                if t == 1:
                    r.due_win = base + FINE_WIN
                    if now() >= r.post:
                        prev = r.prev_off.get(1)
                        after_post = False
                        if off:
                            try:
                                after_post = datetime.strptime(off, "%Y-%m-%d %H:%M:%S") >= r.post
                            except ValueError:
                                pass
                        if off and off == prev and (after_post or relax_final):
                            r.stable += 1
                        else:
                            r.stable = 0
                        if r.stable >= STABLE_N - 1:  # 3 回続けて同じ値 = 変化なしが 2 回
                            log(f"{r.race_id} official {off} が {STABLE_N} 回続けて同じ。確定とみなして終了")
                            final_poll(r)
                            r.done = True
                    if off:
                        r.prev_off[1] = off
                else:
                    r.due_place = base + FINE_PLACE
            else:
                poll(r, 1, "coarse")
                r.due_coarse = time.time() + COARSE
    finally:
        win_w.close()
        if place_w:
            place_w.close()
    log(f"終了: リクエスト {client.n} 回、平均 {client.rate():.3f} req/s")
    return 0


def fake_schedule():
    """dry-run 用: 3 場 x 2 レース、発走は数分後。race_id は実在の過去レース (API が答える)。"""
    t = now().replace(second=0, microsecond=0)
    ids = [("202605040611", "202605040612"), ("202605040609", "202605040610"), ("202605040607", "202605040608")]
    out = []
    for k, (a, b) in enumerate(ids):
        for j, rid in enumerate((a, b)):
            post = t + timedelta(minutes=1 + k + 2 * j)
            if (k, j) == (2, 1):  # 粗い段階も通すため 1 レースだけ 40 分後
                post = t + timedelta(minutes=40)
            out.append(dict(race_id=rid, venue=f"9{k}", race_no=int(rid[10:]),
                            post_time=post.strftime("%Y-%m-%d %H:%M"), source="dry-run"))
    return out


def write_schedule(rows, path: Path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["race_id", "venue", "race_no", "post_time", "source"])
        w.writeheader()
        w.writerows(rows)


def main():
    global LOGF
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--place", action="store_true", help="複勝も 60 秒おきに取る")
    ap.add_argument("--until", help="HH:MM で打ち切り")
    ap.add_argument("--once", metavar="RACE_ID", help="1 レースの単勝 (と --place なら複勝) を表示して終了")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dry-run-minutes", type=float, default=4.0)
    a = ap.parse_args()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    if a.once:
        c = Client()
        for t in ([1, 2] if a.place else [1]):
            st, text, ms, _ = c.get(ODDS_URL.format(race_id=a.once, t=t))
            off, rows = parse_odds(text, t)
            print(f"race_id={a.once} type={'単勝' if t == 1 else '複勝'} HTTP {st} {ms}ms "
                  f"official_datetime={off or '(なし)'} 頭数={len(rows)}")
            for u, v in sorted(rows.items()):
                print(f"  {u}: {' / '.join(v)}")
        return 0

    outdir = OUT / "dryrun" if a.dry_run else OUT
    outdir.mkdir(parents=True, exist_ok=True)
    LOGF = open(outdir / "collector.log", "a", encoding="utf-8")
    ymd = now().strftime("%Y%m%d") if a.dry_run else a.date
    log(f"=== odds_collector start date={ymd} place={a.place} dry_run={a.dry_run} until={a.until} ===")

    polls = Appender(outdir / f"{ymd}_polls.csv", POLL_COLS)
    client = Client(polls)
    try:
        if a.dry_run:
            rows = fake_schedule()
        else:
            rows = schedule_from_list(client, ymd)
            if not rows:
                log("netkeiba に一覧が無いので JRDB BAC から作ります")
                rows = schedule_from_jrdb(ymd)
        if not rows:
            log("この日のレースがありません。終了します。")
            return 0
        rows.sort(key=lambda d: (d["post_time"], d["venue"]))
        write_schedule(rows, outdir / f"{ymd}_races.csv")
        log(f"出走表 {len(rows)} レース → {outdir / f'{ymd}_races.csv'}")
        by_v = {}
        for d in rows:
            by_v.setdefault(d["venue"], []).append(d)
        for v, ds in sorted(by_v.items()):
            log(f"  {VENUES.get(v, v)} ({v}): " + " ".join(f"{d['race_no']}R {d['post_time'][-5:]}" for d in
                                                         sorted(ds, key=lambda d: d['race_no'])))
        until = None
        if a.until:
            hh, mm = map(int, a.until.split(":"))
            until = now().replace(hour=hh, minute=mm, second=0, microsecond=0)
        stop_at = now() + timedelta(minutes=a.dry_run_minutes) if a.dry_run else None
        return run(rows, ymd, outdir, a.place, until, client, polls, stop_at=stop_at, relax_final=a.dry_run)
    finally:
        polls.close()
        log("=== odds_collector stop ===")
        LOGF.close()


if __name__ == "__main__":
    sys.exit(main())
