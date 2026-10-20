# keiba-odds-ts

JRA レース当日の単勝・複勝オッズを netkeiba のオッズ API から時系列で記録する (学習・研究用)。
収集は GitHub Actions のホストランナーで動かし、結果の CSV を `data/odds_ts/` に commit する。

## 取り方 (`odds_collector.py`)

- 出走表は `race.netkeiba.com/top/race_list_sub.html?kaisai_date=YYYYMMDD` から作る。レースの無い日は一覧が空で、1 リクエストで終わる。
- 発走 T の 30 分前から単勝 30 秒おき・複勝 60 秒おき。T 以後は official_datetime が 3 回続けて同じ (確定オッズ) か T+20 分で終了。
  それより前のレースは単勝 10 分おき。
- **リクエストはすべて直列で、どの 2 本の間も 1.1 秒以上空ける (1 req/s 以下、並列なし)。** コード内で ≥ 1.0 秒を assert している。
  403/429 か 5 回連続失敗で 5 分休む。この間隔を短くしない・収集器を同時に 2 つ動かさないこと。

## ワークフロー (`.github/workflows/collect.yml`)

| ジョブ | cron (UTC) | JST | コマンド |
|---|---|---|---|
| morning | `30 23 * * *` / 予備 `45 23 * * *` | 08:30 / 08:45 | `odds_collector.py --place --until 14:20` |
| afternoon | `10 5 * * *` / 予備 `25 5 * * *` | 14:10 / 14:25 | `odds_collector.py --place --until 17:45` |

- 毎日動く (レースの無い日は約 1 分で終わり、commit もしない)。`env: TZ: Asia/Tokyo` なので「今日」は JST。
- 収集ジョブは 1 つの concurrency group (`collect`) に並ぶので、同時に 2 つは動かない。午後のジョブは午前のジョブが 14:20 に終わって
  push し終えてから始まり、その時点の data を checkout して同じ日のファイルに追記する (続きから取る)。予備 cron は本体の後に起動し、
  `--until` を過ぎていればすぐ終わる。GitHub の cron は数分〜数十分遅れることがあり、混雑時は飛ばされることもある。
- 収集中は 30 分ごとに `scripts/sync_odds.py` で commit + push し、終了時 (失敗・中断でも `if: always()`) にもう一度 push する。
  push は毎回 origin/main を取り直して「origin の行 + 新しい行」を書くので衝突しない (最大 5 回やり直し)。
  `data/odds_ts` は workflow artifact (14 日保存) にも上げる。
- **ジョブが落ちた・キャンセルされた場合、最後の push 以後 (最大 30 分) は失われ得る。** artifact も最後のステップに到達したときだけ作られる。
- 手動実行: Actions → collect → Run workflow (`mode`: morning / afternoon / once、`date`、`race_id`)。
  `once` は 1 レースの現在値を表示するだけで何も書かない (疎通確認用)。
- 止めるとき: Actions → collect → `…` → Disable workflow。
- 公開リポジトリで 60 日間活動が無いと GitHub が schedule を自動で止める。

## 手元で動かす

```sh
pip install -r requirements.txt
python3 odds_collector.py --once 202605040611          # 1 レースの現在値を表示
python3 odds_collector.py --dry-run --dry-run-minutes 4 --place   # 過去レースに偽の発走時刻を付けて 4 分回す (data/odds_ts/dryrun/)
python3 odds_collector.py --place --until 17:45        # 今日の本番
python3 odds_ts_summary.py 20261024                    # レースごとの要約
```

Actions と手元で同じ日に同時に動かさないこと (並列になる)。

## CSV (`data/odds_ts/`、追記のみ)

- `YYYYMMDD_races.csv`: race_id, venue (場コード 01 札幌 … 10 小倉), race_no, post_time, source
- `YYYYMMDD_win.csv`: fetched_at (取得時刻), official_datetime (JRA のオッズ時刻), race_id, umaban, odds, popularity。
  official_datetime が前回保存と違うときだけ書く (細かい段階の最初と最後は同じでも書く)。
- `YYYYMMDD_place.csv`: 同じく odds_lo, odds_hi (複勝の下限・上限), popularity
- `YYYYMMDD_polls.csv`: 1 リクエスト 1 行 (fetched_at, race_id, type, http_status, official_datetime, changed, elapsed_ms, phase, ts=epoch 秒)。欠測・間隔の監査用。
- `collector.log`: 進行ログ
- 同じ official_datetime の行は複数あり得る (同じ値を別時刻に取ったもの)。スナップショットは official_datetime ごとに数える。
  JRA の発売締切は発走 2 分前。締切後〜確定までは official_datetime が締切前の値で止まり、確定オッズは発走の数分後に出る。
- `odds_ts_summary.py YYYYMMDD` は (fetched_at, race_id, umaban) の重複を除いてから、レースごとのスナップショット数・間隔と
  締切 (T−2 分) までに見えた最後の値 → 最終値の log オッズ変化を出す (`_summary.csv`, `_late_change.csv`、git には入れない)。
