# relay-stats

strfry の LMDB を直接（読み取りのみ）走査して、言語別の推定 DAU/WAU/MAU を出し、リレーのトップページ（`landingPageFile`）に載せる。

## セットアップ
```
cc -O2 -Wall -o relay-scan src/relay-scan.c -I$(brew --prefix)/include -L$(brew --prefix)/lib -llmdb
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
mkdir -p lib && curl -L -o lib/lid.176.ftz https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz
```
strfry 側は `relay.landingPageFile = "<このディレクトリ>/www/index.html"` と `/assets/` の配信が必要（kojira/strfry PR #4）。設定を空にすると strfry 標準のページに戻る。OG 画像には Google Chrome が必要。

## 構成
- `relay-scan`（C, liblmdb）: 直近30日の created_at インデックスを走査 → TSV。`full` 指定で全履歴を順走査（backfill 用）。
- `analyze.py`: 言語判定（fastText lid.176）・bot/BAN 除外・分類 → `www/stats.json`, `www/history/YYYY-MM-DD.json`, `www/index.html`
- `template.html`: 見た目。**自由に編集してよい**。`__RELAY_NAME__`・`__RELAY_STATS_DATA__`・`__OG_VERSION__`（OG 画像 URL の日付）だけが置換される。
  編集後すぐ反映: `./venv/bin/python analyze.py --render www`
  strfry は index.html の mtime 変化を検知して再読込する（再ビルド・再起動不要、最大5秒）。
- `loop.sh`: 1時間ごとに run.sh。`nohup ./loop.sh <relay名> <strfry-db> [banlist] >> loop.log 2>&1 &`

## nostrfy
nostrfy の LMDB は `src/nostrfy-scan.c` で読む（出力は relay-scan と同じ TSV）。nostrfy と同じ LMDB（lmdb-master-sys, `MDB_IDL_LOGN=16`）でビルドすること。
`RELAY_STATS_SCANNER=nostrfy-scan` で run.sh / backfill-run.sh が relay-scan の代わりに使う。
`nostrfy-scan <db> <since> 0 json <kind,...>` は保存されているイベントの JSON を1行ずつ出す（全文検索の索引づくりに使う）。

## 設定（relay-stats.conf）
`relay-stats.conf.example` を `relay-stats.conf` にコピーして書き換える（`relay-stats.conf` は git 管理外）。
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
`relay-stats.conf` に `AUDIT_DIR=<ディレクトリ>`（strfry-ratelimit の audit 記録 `audit-YYYYMMDD.tsv[.gz]` の置き場所）を書くと、ページに「Event traffic」の欄が出る。書かなければ出ない。
- `traffic.py <audit-dir> <www> [state-dir]` が、受信した EVENT の件数（拒否分も含む）・受け入れ件数・接続元（IP）の実数を集計し、`www/traffic.json` を書く。`run.sh` が毎時呼ぶ。
- 種類は3つ: All kinds（全部）/ Non-ephemeral（kind 20000〜29999 以外）/ Ephemeral（kind 20000〜29999）。URL の `#kinds=all|regular|ephemeral` でも選べる。
- 数えるのはこのリレーへ WebSocket で書き込まれたイベントだけ。他のリレーから取り込んだもの（同期・router）は audit に残らないので入らない。
- 表示: Hourly（直近31日）/ Daily / Monthly / By hour of day（時刻ごとの平均）。日の区切りは `TIMEZONE`。集計途中の期間は partial として点線・白抜きで描く。
- 前回から変わった audit ファイルだけ読み直す。
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
  - 3文字以上の語は、どの語でも全文索引で引ける。
  - 1〜2文字の語（「猫」など）は全文索引では引けないので、語ごとに専用の索引を作る。
    - 対象は `short-terms.txt` に書いた語と、よく検索された語（7日間で3回以上）。
    - 毎時の更新で、1回に5語まで作る。上限は300語。
    - 英数字1文字の語と、100万件を超える投稿に出てくる語は作らない。
    - 索引のない短い語で検索されると、`state/search/queries.log` に時刻と語だけを書く（接続元は残さない）。同じ接続元からの同じ語は1日1回だけ数える。
  - 絞り込み（URL のパラメータ。検索ページの「Filters」からも指定できる）:
    - `since=YYYY-MM-DD`、`until=YYYY-MM-DD`：期間（日本時間、両端の日を含む）
    - `author=npub1…`：投稿者（nprofile1… と hex も可）
    - `kind=note`（kind 1）、`kind=article`（kind 30023）
    - 例：`/search/nostr?since=2026-01-01&until=2026-03-31&kind=note`
  - 投稿者や記事だけに絞ると、全文索引をたどっても当たりのほとんどを捨てることになる。その場合は、対象の投稿（投稿者なら6万件まで）を新しい順に直接読んで、本文を照合する。同じ検索の結果は60秒キャッシュする。
  - 索引の rowid は「created_at << 20 | id の一部」で、時刻順に並ぶ。期間指定を rowid の範囲で絞るので、新しい順の並べ替えが要らない。
  - 接続元ごとに回数を制限する（1分あたり、ページ表示120回か検索30回まで）。
- 構成:
  - `web/server.mjs`（Node 22.5 以上、`node:sqlite` を使う）がローカルで動く（`WEB_PORT`、既定 7780）。投稿はリレーに WebSocket で取りにいき、LMDB は直接開かない。
  - リバースプロキシで、上の URL だけをこのサーバーに回す。
  - 環境変数は `web/server.mjs` の先頭を参照。
- 準備:
  ```sh
  (cd web && npm install)
  # relay-stats.conf（nostrfy なら SEARCH_NOSTRFY=/path/to/nostrfy-scan /path/to/nostrfy-data）
  SEARCH_STRFRY=/path/to/strfry --config /path/to/strfry.conf
  # 初回の全件索引。以後は run.sh が毎時、追加分と削除を反映する
  SEARCH_STRFRY="..." ./venv/bin/python search-index.py state/search/build.sqlite --full && mv state/search/build.sqlite state/search/search.sqlite
  # ページサーバー（終了したら再起動する）
  (cd web && WEB_BASE=https://<relay> WEB_BANLIST=<banlist> nohup ./web-loop.sh </dev/null >/dev/null 2>&1 &)
  ```
  索引は大きくなる（イベント数に比例。kind 1 が数百万件なら数GB）。置き場所を変えるときは `state/search` をシンボリックリンクにする。
  cloudflared の設定例（strfry への規則より前に置く）:
  ```yaml
  - hostname: <relay>
    path: ^/(npub1[0-9a-z]+|note1[0-9a-z]+|nprofile1[0-9a-z]*|nevent1[0-9a-z]*|naddr1[0-9a-z]*|npub/.*|event/.*|search(/.*)?|robots\.txt|sitemap(-[0-9a-z]+)?\.xml)$
    service: http://127.0.0.1:7780
  ```

## 過去分の再計算（backfill）
`nohup ./backfill-run.sh <relay> <db> <banlist> > backfill.log 2>&1 &` が全部やる（`.lock` で loop と排他。履歴は `www.backfill/` に作ってから差し替えるので、失敗しても既存の履歴は残る）。
全件走査は1つの読み取りトランザクションで行う（大きな DB では数十分）。その間 LMDB は解放ページを再利用できないので、書き込みの多いリレーではファイルが少し伸びることがある。
手動で行う場合:
```
./relay-scan <db> 1669852800 $(date +%s) full > full.tsv   # Event テーブルを順に全件走査
RELAY_STATS_TZ_OFFSET=$(./venv/bin/python tzoffset.py) は run.sh と同様に指定すること
./venv/bin/python backfill.py <relay> full.tsv www <banlist>  # history/ を日別に生成し、lang-cache.json を作る
```

## OG image
- `og.py www` renders `og-template.html` with headless Chrome into `www/assets/og.png` (1200x630). `run.sh` does it hourly.
- strfry serves `<dir of landingPageFile>/assets/<name>` at `/assets/<name>` (kojira/strfry PR #4).
- `template.html` points `og:image` at `https://<relay>/assets/og.png?v=<YYYYMMDD>` so SNS caches refresh daily.
