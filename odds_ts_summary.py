"""odds_collector.py の出力 (data/odds_ts/YYYYMMDD_*.csv) をレースごとに要約する。

    python3 odds_ts_summary.py 20261024
    python3 odds_ts_summary.py 20261019 --dir data/odds_ts/dryrun

レースごとに:
- n_snap: 異なる official_datetime の数 (保存されたスナップショット)
- first_rel / last_rel: 最初・最後の official_datetime の発走からの分 (負 = 発走前)
- med_int_s: T-30..T の間の、連続する異なるスナップショットの間隔 (秒) の中央値
- 賭け手が見られた最後の値 (取得時刻 <= T-2 分の最後のスナップショット) と最終スナップショットの比較:
  馬ごとの d = log(最終オッズ) - log(T-2 時点オッズ) (負 = 売れた = オッズが下がった)
  max_abs_d: |d| の最大、drop_uma / drop_d: 最も下がった馬とその d
出力: 表を表示し、YYYYMMDD_summary.csv (レース単位) と YYYYMMDD_late_change.csv (馬単位) を書く。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
CLOSE_MIN = 2  # 発売締切 = 発走 2 分前


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="YYYYMMDD (ファイル名の先頭)")
    ap.add_argument("--dir", default=str(ROOT / "data" / "odds_ts"))
    a = ap.parse_args()
    d = Path(a.dir)
    races = pd.read_csv(d / f"{a.date}_races.csv", dtype={"race_id": str, "venue": str})
    races["post"] = pd.to_datetime(races["post_time"])
    w = pd.read_csv(d / f"{a.date}_win.csv", dtype={"race_id": str})
    # 2 つのジョブ (午前・午後) の書き込みが重なっても同じ行を二重に数えない
    w = w.drop_duplicates(["fetched_at", "race_id", "umaban"], keep="last")
    w["fetched"] = pd.to_datetime(w["fetched_at"])
    w["off"] = pd.to_datetime(w["official_datetime"])
    polls = pd.read_csv(d / f"{a.date}_polls.csv", dtype={"race_id": str}).drop_duplicates()

    out, horses = [], []
    for r in races.itertuples():
        g = w[w["race_id"] == r.race_id]
        pr = polls[polls["race_id"] == r.race_id]
        rec = dict(race_id=r.race_id, venue=r.venue, race_no=r.race_no, post=r.post.strftime("%H:%M"),
                   n_polls=len(pr), n_snap=0)
        if g.empty:
            out.append(rec)
            continue
        snaps = g.groupby("off").agg(fetched=("fetched", "min")).reset_index().sort_values("off")
        rel = (snaps["off"] - r.post).dt.total_seconds() / 60
        rec.update(n_snap=len(snaps), first_rel=round(rel.iloc[0], 1), last_rel=round(rel.iloc[-1], 1))
        win30 = snaps[(snaps["off"] >= r.post - pd.Timedelta(minutes=30)) & (snaps["off"] <= r.post)]["off"]
        iv = win30.diff().dt.total_seconds().dropna()
        rec["med_int_s"] = round(iv.median(), 1) if len(iv) else np.nan
        # T-2 分までに取得できた最後のスナップショット vs 最終スナップショット
        cut = r.post - pd.Timedelta(minutes=CLOSE_MIN)
        seen = g[g["fetched"] <= cut]
        if not seen.empty:
            pre_off = seen["off"].max()
            fin_off = g["off"].max()
            pre = seen[seen["off"] == pre_off].drop_duplicates("umaban", keep="last").set_index("umaban")
            fin = g[g["off"] == fin_off].drop_duplicates("umaban", keep="last").set_index("umaban")
            m = pre[["odds", "popularity"]].join(fin[["odds", "popularity"]], lsuffix="_pre", rsuffix="_fin",
                                                 how="inner").dropna(subset=["odds_pre", "odds_fin"])
            m["d"] = np.log(m["odds_fin"]) - np.log(m["odds_pre"])
            rec.update(pre_off_rel=round((pre_off - r.post).total_seconds() / 60, 1),
                       fin_off_rel=round((fin_off - r.post).total_seconds() / 60, 1))
            if len(m):
                i = m["d"].idxmin()
                rec["max_abs_d"] = round(m["d"].abs().max(), 3)
                if m.loc[i, "d"] < 0:  # 下がった馬がいるときだけ
                    rec.update(drop_uma=int(i), drop_d=round(m.loc[i, "d"], 3),
                               drop_odds=f"{m.loc[i, 'odds_pre']}->{m.loc[i, 'odds_fin']}")
                for u, x in m.iterrows():
                    horses.append(dict(race_id=r.race_id, umaban=int(u), odds_pre=x.odds_pre, odds_fin=x.odds_fin,
                                       pop_pre=x.popularity_pre, pop_fin=x.popularity_fin, d=round(x.d, 4),
                                       pre_off=pre_off, fin_off=fin_off))
        out.append(rec)

    s = pd.DataFrame(out)
    pd.set_option("display.width", 200, "display.max_columns", 30)
    print(s.to_string(index=False))
    s.to_csv(d / f"{a.date}_summary.csv", index=False)
    pd.DataFrame(horses).to_csv(d / f"{a.date}_late_change.csv", index=False)
    print(f"\n→ {d / f'{a.date}_summary.csv'} ({len(s)} レース)、{d / f'{a.date}_late_change.csv'} ({len(horses)} 頭)")
    if "ts" in polls:
        gap = polls["ts"].sort_values().diff().dropna()
        if len(gap):
            span = polls["ts"].max() - polls["ts"].min()
            print(f"リクエスト {len(polls)} 回、最小間隔 {gap.min():.3f} 秒、"
                  f"平均 {len(polls) / span if span > 0 else float('nan'):.3f} req/s")


if __name__ == "__main__":
    main()
