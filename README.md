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

## 言語判定
アカウントごとに 自分の投稿 → プロフィール（name / display_name / about）→ 反応・返信した相手の多数決 → 全期間の再計算結果 の順で決める。
- 漢字・かなを含む文は文字の種類で決める（fastText lid.176 は漢字だけの中国語を ja と判定しがちなため）。ひらがな1文字以上かカタカナ2文字以上 → ja。漢字だけの文は简体字・繁体字専用字・中国語の助詞（zh）と日本の新字体・々（ja）で決め、どちらもなければ判定に使わない。
- それ以外の文は fastText（確からしさ 0.5 以上）。

## 言語別アカウント一覧
`https://<relay>/#accounts=<lang>`（日別は `&day=YYYY-MM-DD`）。MAU / その日の DAU に数えたアカウントを、kind 0・最新の kind 1・言語の決め方つきで並べる。言語判定の確認用。
データは analyze.py が毎時 `www/assets/accounts.json`（言語別の人数・日別人数）と `www/assets/accounts-<lang>.json` に書く。

## Event traffic（任意）
`relay-stats.conf` に `AUDIT_DIR=~/strfry-audit`（strfry-ratelimit の audit 記録 `audit-YYYYMMDD.tsv[.gz]` の置き場所）を書くと、ページに「Event traffic」の欄が出る。書かなければ出ない。
- `traffic.py <audit-dir> <www> [state-dir]` が、受信した EVENT の件数（拒否分も含む）・受け入れ件数・接続元（IP）の実数を集計し、`www/traffic.json` を書く。`run.sh` が毎時呼ぶ。
- 種類は3つ: All kinds（全部）/ Non-ephemeral（kind 20000〜29999 以外）/ Ephemeral（kind 20000〜29999）。URL の `#kinds=all|regular|ephemeral` でも選べる。
- 数えるのはこのリレーへ WebSocket で書き込まれたイベントだけ。他のリレーから取り込んだもの（同期・router）は audit に残らないので入らない。
- 表示: Hourly（直近31日）/ Daily / Monthly / By hour of day（時刻ごとの平均）。日の区切りは `TIMEZONE`。集計途中の期間は partial として点線・白抜きで描く。
- 前回から変わった audit ファイルだけ読み直す（x で約1秒、初回は7日分で約26秒）。
- 状態は `state/traffic/`（git 管理外）。接続元はハッシュ化した値だけを前月・今月分保持し、それより古い日・月は確定値として残す（audit の90日保持を過ぎてもグラフは残る）。ページと JSON には件数だけを出す。作り直すときは `state/traffic/` を消す。

## 投稿ページと全文検索（任意、strfry のみ）
njump.me と同じ形の URL で、投稿・記事・プロフィールのページと全文検索を出す。HTML はサーバーで作るので、検索エンジンにも読ませられる。
- URL:
  - `/npub1…`：プロフィールと最新の投稿
  - `/note1…`：kind 1 の投稿、または kind 30023 の記事
  - `/search/<検索語>`：全文検索
  - `/npub/<hex>` と `/event/<hex>`：`/npub1…`・`/note1…` へ301で転送
  - `/robots.txt`、`/sitemap.xml`
  - それ以外は 404（nprofile / nevent / naddr も 404）
- 出さないもの（404）:
  - ban 済みの pubkey（banlist は変更を検知して読み直す）
  - 削除された投稿（kind 5）
  - NSFW 扱いの投稿（`content-warning` タグ、`t` / `l` タグの `nsfw`、本文の `#nsfw`）
- 検索ページは `noindex,follow`。Google に登録するのは投稿・記事・プロフィールのページで、sitemap には直近365日分を載せる。
- 検索:
  - `search-index.py` が kind 1・30023 の本文と、kind 0 の名前を SQLite FTS5（trigram）に入れる。
  - 3文字以上の語で検索し、2文字以下の語は絞り込みにだけ使う。
  - 接続元ごとに回数を制限する（1分あたり、ページ表示120回か検索30回まで）。
- 構成:
  - `web/server.mjs`（Node 22.5 以上、`node:sqlite` を使う）が 127.0.0.1:7780 で動く。投稿はリレーに WebSocket で取りにいき、LMDB は直接開かない。
  - cloudflared の ingress で、上の URL だけを 7780 に回す。
- 準備:
  ```sh
  (cd web && npm install)
  # relay-stats.conf
  SEARCH_STRFRY=/path/to/strfry --config /path/to/strfry.conf
  # 初回の全件索引（r で約15分）。以後は run.sh が毎時、追加分と削除を反映する
  SEARCH_STRFRY="..." ./venv/bin/python search-index.py state/search/build.sqlite --full && mv state/search/build.sqlite state/search/search.sqlite
  cp web/launchd/io.kojira.relay-stats-web.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.kojira.relay-stats-web.plist
  ```
  cloudflared の設定例（strfry への規則より前に置く）:
  ```yaml
  - hostname: r.kojira.io
    path: ^/(npub1[0-9a-z]+|note1[0-9a-z]+|nprofile1[0-9a-z]*|nevent1[0-9a-z]*|naddr1[0-9a-z]*|npub/.*|event/.*|search(/.*)?|robots\.txt|sitemap(-[0-9a-z]+)?\.xml)$
    service: http://127.0.0.1:7780
  ```

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
