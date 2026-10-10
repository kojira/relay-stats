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
- `template.html`: 見た目。**自由に編集してよい**。`__RELAY_NAME__`・`__RELAY_STATS_DATA__`・`__OG_VERSION__`（OG 画像 URL の日付）だけが置換される。
  編集後すぐ反映: `~/relay-stats/venv/bin/python ~/relay-stats/analyze.py --render ~/relay-stats/www`
  strfry は index.html の mtime 変化を検知して再読込する（再ビルド・再起動不要、最大5秒）。
- `loop.sh`: 1時間ごとに run.sh。nohup で起動（launchd は TCC で /Volumes を読めないため）。**再起動後は手動起動が必要**:
  - r: `cd ~/relay-stats && nohup ./loop.sh r.kojira.io /Volumes/1TB/strfry/strfry-db /Volumes/1TB/strfry/strfry-db/banned-pubkeys.txt >> loop.log 2>&1 &`
  - x: `cd ~/relay-stats && DYLD_LIBRARY_PATH=/usr/local/lib nohup ./loop.sh x.kojira.io ~/develop/nostr/relays/strfry/strfry-db ~/develop/nostr/relays/strfry/strfry-db/banned-pubkeys.txt >> loop.log 2>&1 &`

strfry 側: `relay.landingPageFile` と `/assets/` 配信（kojira/strfry PR #4。~/strfry-build の feat/landing-page-file[-master]）。
設定を空にすると strfry 標準のページに戻る。

## nostrfy（n.kojira.io）
nostrfy の LMDB は `src/nostrfy-scan.c` で読む（出力は relay-scan と同じ TSV）。nostrfy と同じ LMDB（lmdb-master-sys, `MDB_IDL_LOGN=16`）でビルドすること。
`RELAY_STATS_SCANNER=nostrfy-scan` で run.sh / backfill-run.sh が relay-scan の代わりに使う。

## 設定（relay-stats.conf）
- `TIMEZONE=Asia/Tokyo` … DAU/WAU/MAU の日付の区切り（IANA 名。UTC, America/New_York など）。変更後は過去分も backfill し直すこと。

## 集計の定義
- DAU = その日（TIMEZONE の 0〜24時）に活動した pubkey 数。WAU/MAU = その日までの 7日/30日 の実人数。
- 表の見出しは「確定日」= 昨日。今日の途中経過も選べる。
- `events_30d` / `kinds_30d` は実際には走査窓（32日）の件数。`inferred_from_reactions` には lang-cache（過去の言語判定）由来も含む。
- TIMEZONE の現在の UTC オフセットで日を区切る（夏時間の切り替えは無視）。不正な TIMEZONE はエラーで止まる。

## Ephemeral events（任意）
`relay-stats.conf` に `AUDIT_DIR=~/strfry-audit`（strfry-ratelimit の audit 記録 `audit-YYYYMMDD.tsv[.gz]` の置き場所）を書くと、ページに「Ephemeral events」の欄が出る。書かなければ出ない。
- `ephemeral.py <audit-dir> <www> [state-dir]` が kind 20000〜29999 の受信件数（拒否分も含む）・受け入れ件数・接続元（IP）の実数を集計し、`www/ephemeral.json` を書く。`run.sh` が毎時呼ぶ。
- 表示: Hourly（直近31日）/ Daily / Monthly / By hour of day（時刻ごとの平均）。日の区切りは `TIMEZONE`。集計途中の期間は partial として点線・白抜きで描く。
- 前回から変わった audit ファイルだけ読み直す（x で約1秒、初回は7日分で約26秒）。
- 状態は `state/ephemeral/`（git 管理外）。接続元はハッシュ化した値だけを前月・今月分保持し、それより古い日・月は確定値として残す（audit の90日保持を過ぎてもグラフは残る）。ページと JSON には件数だけを出す。作り直すときは `state/ephemeral/` を消す。

## 過去分の再計算（backfill）
`nohup ./backfill-run.sh <relay> <db> <banlist> > backfill.log 2>&1 &` が全部やる（`.lock` で loop と排他。履歴は `www.backfill/` に作ってから差し替えるので、失敗しても既存の履歴は残る）。
全件走査は1つの読み取りトランザクションで行う（r で約18分）。その間 LMDB は解放ページを再利用できないので、書き込みの多いリレーではファイルが少し伸びることがある。
手動で行う場合:
```
./relay-scan <db> 1669852800 $(date +%s) full > full.tsv   # Event テーブルを順に全件走査（r: 約18分、x: 約20秒）
RELAY_STATS_TZ_OFFSET=$(./venv/bin/python tzoffset.py) は run.sh と同様に指定すること
./venv/bin/python backfill.py <relay> full.tsv www <banlist>  # history/ を日別に生成し、lang-cache.json を作る
```

## OG image
- `og.py www` renders `og-template.html` with headless Chrome into `www/assets/og.png` (1200x630). `run.sh` does it hourly.
- strfry serves `<dir of landingPageFile>/assets/<name>` at `/assets/<name>` (strfry commit 64f6c1a on r / 35e6d7e on x).
- `template.html` points `og:image` at `https://<relay>/assets/og.png?v=<YYYYMMDD>` so SNS caches refresh daily.
