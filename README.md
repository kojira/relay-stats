# relay-stats

strfry の LMDB を直接（読み取りのみ）走査して、言語別の推定 DAU/WAU/MAU を出し、リレーのトップページ（`landingPageFile`）に載せる。

## セットアップ
```
cc -O2 -Wall -o relay-scan src/relay-scan.c -I$(brew --prefix)/include -L$(brew --prefix)/lib -llmdb
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
mkdir -p lib && curl -L -o lib/lid.176.ftz https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz
```
strfry 側は `relay.landingPageFile = "<このディレクトリ>/www/index.html"` が必要（kojira/strfry PR #4）。OG 画像には Google Chrome が必要。

## 構成
- `relay-scan`（C, liblmdb）: 直近30日の created_at インデックスを走査 → TSV。r で約2秒（キャッシュ済み）。`full` 指定で全履歴を順走査（backfill 用）。
- `analyze.py`: 言語判定（fastText lid.176）・bot/BAN 除外・分類 → `www/stats.json`, `www/history/YYYY-MM-DD.json`, `www/index.html`
- `template.html`: 見た目。**自由に編集してよい**。`__RELAY_NAME__` と `__RELAY_STATS_DATA__` だけが置換される。
  編集後すぐ反映: `~/relay-stats/venv/bin/python ~/relay-stats/analyze.py --render ~/relay-stats/www`
  strfry は index.html の mtime 変化を検知して再読込する（再ビルド・再起動不要、最大5秒）。
- `loop.sh`: 1時間ごとに run.sh。nohup で起動（launchd は TCC で /Volumes を読めないため）。**再起動後は手動起動が必要**:
  - r: `cd ~/relay-stats && nohup ./loop.sh r.kojira.io /Volumes/1TB/strfry/strfry-db /Volumes/1TB/strfry/strfry-db/banned-pubkeys.txt >> loop.log 2>&1 &`
  - x: `cd ~/relay-stats && DYLD_LIBRARY_PATH=/usr/local/lib nohup ./loop.sh x.kojira.io ~/develop/nostr/relays/strfry/strfry-db ~/develop/nostr/relays/strfry/strfry-db/banned-pubkeys.txt >> loop.log 2>&1 &`

strfry 側: `relay.landingPageFile` と `/assets/` 配信（kojira/strfry PR #4。~/strfry-build の feat/landing-page-file[-master]）。
設定を空にすると strfry 標準のページに戻る。

## 設定（relay-stats.conf）
- `TIMEZONE=Asia/Tokyo` … DAU/WAU/MAU の日付の区切り（IANA 名。UTC, America/New_York など）。変更後は過去分も backfill し直すこと。

## 集計の定義
- DAU = その日（TIMEZONE の 0〜24時）に活動した pubkey 数。WAU/MAU = その日までの 7日/30日 の実人数。
- 表の見出しは「確定日」= 昨日。今日の途中経過も選べる。

## 過去分の再計算（backfill）
```
./relay-scan <db> 1669852800 $(date +%s) full > full.tsv   # Event テーブルを順に全件走査（r: 約18分、x: 約20秒）
RELAY_STATS_TZ_OFFSET=$(./venv/bin/python tzoffset.py) は run.sh と同様に指定すること
./venv/bin/python backfill.py <relay> full.tsv www <banlist>  # history/ を日別に生成し、lang-cache.json を作る
```

## OG image
- `og.py www` renders `og-template.html` with headless Chrome into `www/assets/og.png` (1200x630). `run.sh` does it hourly.
- strfry serves `<dir of landingPageFile>/assets/<name>` at `/assets/<name>` (strfry commit 64f6c1a on r / 35e6d7e on x).
- `template.html` points `og:image` at `https://<relay>/assets/og.png?v=<YYYYMMDD>` so SNS caches refresh daily.
