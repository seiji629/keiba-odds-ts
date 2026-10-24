# keiba-odds-ts

JRA レース当日の単勝・複勝オッズを netkeiba のオッズ API から時系列で記録する (学習・研究用)。
収集は GitHub Actions のホストランナーで動かし、結果の CSV は**非公開リポジトリ `seiji629/keiba-odds-data`** の
`data/odds_ts/` に commit する。この公開リポジトリにはコードだけを置き、データは置かない。

## データの置き場所と理由

- netkeiba 利用規約 (第 14 条) により、取得したデータを不特定多数が閲覧できる場所に置けない。そのためコード (この repo) は公開、
  データは非公開 repo に分けている。**`keiba-odds-data` を公開にしない・この repo に CSV を commit しない** (`.gitignore` で `data/` を除外)。
- 書き込みの認証は deploy key: 非公開 repo に書き込み可の deploy key ("keiba-odds-ts actions") を登録し、その秘密鍵を
  この repo の Actions secret `DATA_DEPLOY_KEY` に入れてある。ワークフローは `actions/checkout` の `ssh-key` で非公開 repo を
  `./datarepo` に checkout し、そこへ SSH で push する。この repo 自身への権限は `contents: read` だけ。
  鍵を作り直すときは deploy key と secret を両方入れ替える。fork からの pull request の実行には secret は渡らない。
- workflow artifact は作らない (公開 repo の artifact はログインした GitHub ユーザーなら誰でも落とせるため)。
  その分、ジョブが落ちたときに最後の push 以後が失われ得る。
- 読むとき: `git clone git@github.com:seiji629/keiba-odds-data.git` (または `gh repo clone seiji629/keiba-odds-data`) して
  `git pull` で更新する。手元で `odds_ts_summary.py` を使うなら、その `data/odds_ts/` をこの repo の `data/odds_ts/` に
  コピー (またはシンボリックリンク) する。

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
| afternoon | `40 4 * * *` / 予備 `55 4 * * *` | 13:40 / 13:55 | `odds_collector.py --place --until 17:45` |

- 毎日動く (レースの無い日は約 1 分で終わり、commit もしない)。`env: TZ: Asia/Tokyo` なので「今日」は JST。
- 収集ジョブは 1 つの concurrency group (`collect`, `cancel-in-progress: false`) に並ぶので、同時に 2 つは動かない。
  GitHub は 1 group に「実行中 1 本 + 待ち (pending) 1 本」までしか持たず、新しい pending は古い pending をキャンセルする。
- **午前→午後の引き継ぎ**: GitHub の cron は数分〜数十分遅れることがあり (2026-10-24 は 14:10 の cron が 14:34 に起動し、
  14:20〜14:34 に取得の空白ができた)、混雑時は飛ばされることもある。そこで午後の cron は午前の `--until 14:20` より前の
  13:40 / 13:55 にしてある。午後の実行は午前の実行中に pending になり (残っていた午前の予備 pending はこれでキャンセルされる。
  13:55 の予備が来れば 13:40 の pending と入れ替わるだけで、どちらも afternoon)、午前が 14:20 に終わって push し終えた直後に始まる。
  その時点のデータ repo を checkout して同じ日のファイルに追記する (続きから取る)。午前がすでに終わっていれば午後はすぐ始まり 17:45 まで動く。
  `--until` は「今日の HH:MM」なので、14:2x に始まった午後の実行は止まらない。`--until` を過ぎてから始まった実行
  (午後の後ろに並んだ予備など) は一覧を 1 回取ってすぐ終わる。
- mode は cron 文字列で決める (`30 23`/`45 23` → morning、`40 4`/`55 4` → afternoon)。知らない文字列なら JST の起動時刻が
  13:30 以後で afternoon、それより前で morning。
- 収集中は 30 分ごとに `scripts/sync_odds.py` で commit + push し、終了時 (失敗・中断でも `if: always()`) にもう一度 push する。
  push は毎回 origin/main を取り直して「origin の行 + 新しい行」を書くので衝突しない (最大 5 回やり直し)。
  push 先は `datarepo` (非公開 repo の checkout) の origin。
- **ジョブが落ちた・キャンセルされた場合、最後の push 以後 (最大 30 分) は失われ得る** (artifact は作らない)。
- 手動実行: Actions → collect → Run workflow (`mode`: morning / afternoon / once / synctest、`date`、`race_id`)。
  `once` は 1 レースの現在値を表示するだけで何も書かない (疎通確認用、データ repo も checkout しない)。
  `synctest` は netkeiba に触らず、印のファイル `synctest/00000000_races.csv` を `sync_odds.py` で非公開 repo に push し、
  すぐ消す commit を push する (deploy key と push 経路の確認用)。
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

Actions と手元で同じ日に収集器を動かさないこと (並列になる)。手元で書いた `data/odds_ts/` は git に入らない
(`.gitignore`)。本番データは非公開 repo を clone して読む。

## CSV (非公開 repo の `data/odds_ts/`、追記のみ)

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
