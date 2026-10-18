#!/usr/bin/env python3
"""Turn relay-scan TSV into language-split DAU/WAU/MAU stats (JSON) and render the landing page.

usage: analyze.py <relay-name> <scan.tsv> <out-dir> [banlist]
       analyze.py --render <out-dir>      # re-render index.html from stats.json after editing template.html
"""
import collections, datetime, json, os, re, sys, time

import fasttext

HERE = os.path.dirname(os.path.abspath(__file__))
fasttext.FastText.eprint = lambda *a, **k: None
MODEL = fasttext.load_model(os.path.join(HERE, "lib", "lid.176.ftz"))

DAY = 86400
WINDOWS = {"dau": 1, "wau": 7, "mau": 30}   # calendar days (JST) ending at the target date
def _load_config():
    """relay-stats.conf (KEY=VALUE). Keys: TIMEZONE (IANA name, e.g. Asia/Tokyo, UTC, America/New_York)."""
    cfg = {"TIMEZONE": "Asia/Tokyo"}
    path = os.environ.get("RELAY_STATS_CONF", os.path.join(HERE, "relay-stats.conf"))
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
    return cfg


CONFIG = _load_config()
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(CONFIG["TIMEZONE"])
except Exception as e:  # python < 3.9 or unknown name: fail loudly instead of silently using another zone
    raise SystemExit(f"relay-stats: invalid TIMEZONE={CONFIG['TIMEZONE']!r} in relay-stats.conf ({e!r})")
# Day boundaries use the zone's current UTC offset (DST shifts are ignored for simplicity).
# Day number d = (ts + TZ_OFFSET) // DAY, i.e. the date is 1970-01-01 + d days (same as relay-scan).
TZ_OFFSET = int(datetime.datetime.now(TZ).utcoffset().total_seconds())
TZ_LABEL = CONFIG["TIMEZONE"]
# full-text search box (only where web/server.mjs serves /search: SEARCH_STRFRY / SEARCH_NOSTRFY set in relay-stats.conf)
SEARCH_FORM = ('<form class="sform" action="/search" method="get" role="search">'
               '<input name="q" placeholder="Search notes and articles" aria-label="search"><button>Search</button></form>')


def day_of(ts):
    return (ts + TZ_OFFSET) // DAY


EPOCH_DATE = datetime.date(1970, 1, 1)


def day_name(d):
    return (EPOCH_DATE + datetime.timedelta(days=d)).isoformat()


def day_from_name(name):
    return (datetime.date.fromisoformat(name) - EPOCH_DATE).days
# Kinds that do not indicate a human being active on this relay.
NOT_ACTIVITY = {1059, 13, 10377}
BOT_NIP05_SUFFIXES = ("nostrverse.eth.link",)
# Fediverse bridges (Mostr / momostr): counted separately as "_bridge", not in languages/_all
BRIDGE_NIP05_SUFFIXES = ("mostr.pub", "momostr.pink")
BRIDGE_KEY = "_bridge"
NOISE = re.compile(r"https?://\S+|nostr:\S+|(?:npub|note|nevent|naddr|nprofile)1\w+|#\[\d+\]|:[A-Za-z0-9_+-]+:|#\S+")
TOP_LANGS = 12


# lid.176 leans to "ja" for Han-only text (no kana), so Chinese is often called Japanese. CJK text is decided by
# script instead: kana present -> ja; Han-only -> zh / ja by characters that exist in only one of the two
# (simplified / traditional-only forms and Chinese particles vs Japanese shinjitai / kokuji / 々);
# Han-only text with no such character gives no vote.
KANA = re.compile(r"[\u3041-\u309f\u30a1-\u30fa\u30fc\uff66-\uff9f]")
HIRA = re.compile(r"[\u3041-\u309f]")
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LETTER = re.compile(r"[^\W\d_]")
ZH_ONLY = set(
    "这们个说时对过还发么样问书车东长门见头买卖让认识语读请谢电话开关现实习产镜轻记码无员钱红绿边进连运远该谁较级经结给统专业务动劳华单卫历厅压变听吗呢啊吧亲报场块坚"
    "处备复够奋妈妇娱孙宁审宽导层岁岛币帅师带帮库应庆张录彻态总恶惊惯战户执扩扫护拥择换摄敌显晓暂术杀权极构枪标树桥梦检欢气汇汉沟泽洁测济浓涛润涨渐满滚灭灵炼烟热爱牵"
    "犹狮猎环玛畅疗盖监盘离种积称稳穷竞笔简粮紧纠纪约纯纸线练组细织终绍绑绕绘络绝继绩续维综缓编缘缩罗网罚职联肃胜脑脸腾舰艺节苏药莱获萨营虑虽补观规视览觉计订讨训讯讲"
    "讶许论设访证评诉词译试诗诚诞询详误诸调谈谊谋谓谨谱贝负贡财责贤败货质贩贪贫购贯贴费贵贸贺资赏赔赖赚赛赞赠赢赵赶趋跃践踪轨轮软轰载辆辈辉输辑达迁迈违迟适递逊遗邓邮"
    "郑酱释针钉钓钟钢钥铁铃铜铝银铺链销锁锅错锦键镇闪闭闯闲间闹闻阀阅阔队阳阴阵阶际陆陈险随隐难雾页顶项顺须顾顿预领颇频题颜额风飞饭饮饰饱饿馆驱驶驻驾验骑骗鱼鸟鸡鸣鸭"
    "鹅齐龄龟們這說來對會學國實發經體與為麼裡還讓點選關臺灣號雖應歡嗎聽覺變圖歲縣廣傳鐵轉檢單戰數藥續總濟兩價壓龍龜醫氣區歐黨獨寫舊證譯議讀權觀驗賣樣當從很"
)
JA_ONLY = set(
    "々帰関発気駅売読楽変図円歳県沢様広伝鉄転実対検単戦薬経続総済覚両価圧払渋絵浜桜込畑峠枠働辻栃匂凪営険闘歩毎悪悩亜仏団囲観権歓勧駆騒験鶏竜亀譲醸弁舗辺隣黒満焼恵徳"
    "応拡択栄乗剰寝掲渓隠稲穏髪殻脳嬢壌穂瀬頼曽巣桟帯逓戻涙拝斎剤斉渇謁廃"
)


def script_lang(t):
    """"ja"/"zh" decided from the script; None = no Han / kana (fastText decides); "" = Han without kana and no
    telling character (fastText may still decide a non-CJK language from the rest of the text)."""
    if HIRA.search(t) or len(KANA.findall(t)) >= 2:   # one katakana alone is often a kaomoji (ツ)
        return "ja"
    han = HAN.findall(t)
    if not han:
        return None
    if len(han) * 2 < len(LETTER.findall(t)):   # mostly another script (e.g. a Han name in English text)
        return ""
    zh = sum(c in ZH_ONLY for c in t)
    ja = sum(c in JA_ONLY for c in t)
    return "zh" if zh > ja else "ja" if ja > zh else ""


def detect(text):
    t = NOISE.sub(" ", text).replace("\n", " ").strip()
    t = t.encode("utf-8", "replace").decode("utf-8")   # drop lone surrogates
    s = script_lang(t)
    if s:
        return s
    if len(t) < 4:
        return None
    try:
        (label,), (p,) = MODEL.predict(t)
    except (TypeError, ValueError):
        return None
    label = label[9:]
    if s == "" and label in ("ja", "zh", "yue", "wuu", "ko"):   # Han without kana: fastText cannot tell these apart
        return None
    return label if p >= 0.5 else None


def is_ephemeral(kind):
    return 20000 <= kind < 30000


def main():
    relay, scan, outdir = sys.argv[1:4]
    banned = set()
    if len(sys.argv) > 4 and os.path.exists(sys.argv[4]):
        banned = {l.strip() for l in open(sys.argv[4]) if len(l.strip()) == 64}
    now = int(time.time())

    summary, actdays, votes, target = {}, collections.defaultdict(set), collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    profiles = {}
    bridge_marked = set()
    kinds30 = collections.Counter()
    latest_note = {}   # pk -> (created_at, content JSON literal) of the newest kind 1 in the scan window
    with open(scan, encoding="utf-8", errors="replace") as f:
        for line in f:
            rec = line.rstrip("\n").split("\t")
            tag = rec[0]
            if tag == "E":
                _, pk, ca, kind, tgt, content = rec
                ca, kind = int(ca), int(kind)
                if pk in banned or ca > now + 600:
                    continue
                kinds30[kind] += 1
                if not is_ephemeral(kind) and kind not in NOT_ACTIVITY:
                    actdays[pk].add(day_of(ca))
                if kind == 1 and content != "-" and ca > latest_note.get(pk, (0,))[0]:
                    latest_note[pk] = (ca, content)
                if content != "-":
                    try:
                        lang = detect(json.loads(content))
                    except ValueError:
                        lang = None
                    if lang:
                        votes[pk][lang] += 1
                if tgt != "-":
                    target[pk][tgt] += 1
            elif tag == "P":
                try:
                    profiles[rec[1]] = json.loads(json.loads(rec[2]))
                except (ValueError, IndexError):
                    profiles[rec[1]] = {}
            elif tag == "B":
                bridge_marked.add(rec[1])
            elif tag == "S":
                summary[rec[1]] = int(rec[2])

    def nip05(pk):
        p = profiles.get(pk)
        v = p.get("nip05") if isinstance(p, dict) else None
        return v if isinstance(v, str) and v.strip() else None

    bots = {pk for pk in profiles if (nip05(pk) or "").lower().endswith(BOT_NIP05_SUFFIXES)}
    for pk in bots:
        actdays.pop(pk, None)
    bridges = bridge_marked | {pk for pk in profiles if (nip05(pk) or "").lower().endswith(BRIDGE_NIP05_SUFFIXES)}
    last = actdays
    cache_path = os.path.join(outdir, "lang-cache.json")   # written by backfill.py (whole-history evidence)
    lang_cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}

    # 1) language from own text; 2) profile name/about; 3) majority language of people reacted to
    lang = {pk: v.most_common(1)[0][0] for pk, v in votes.items() if v}
    for pk in last:
        if pk in lang:
            continue
        p = profiles.get(pk)
        if isinstance(p, dict):
            text = " ".join(str(p.get(x) or "") for x in ("about", "display_name", "name"))
            l = detect(text)
            if l:
                lang[pk] = l
    inferred, from_cache = {}, set()
    for pk in last:
        if pk in lang or pk not in target:
            continue
        c = collections.Counter()
        for t, n in target[pk].items():
            if t in lang:
                c[lang[t]] += n
        if c:
            inferred[pk] = c.most_common(1)[0][0]
    for pk in last:
        if pk not in lang and pk not in inferred and pk in lang_cache:
            inferred[pk] = lang_cache[pk]
            from_cache.add(pk)

    def klass(pk):
        if pk not in profiles:
            return "no_profile"
        return "nip05" if nip05(pk) else "profile_only"

    today = day_of(now)

    def count(target_day):
        rows = collections.defaultdict(lambda: {w: {"total": 0, "nip05": 0, "profile_only": 0, "no_profile": 0} for w in WINDOWS})
        for pk, ds in actdays.items():
            l = lang.get(pk) or inferred.get(pk) or "unknown"
            c = klass(pk)
            keys = (BRIDGE_KEY,) if pk in bridges else (l, "_all")
            for w, n in WINDOWS.items():
                if any(target_day - n < d <= target_day for d in ds):
                    for key in keys:
                        rows[key][w]["total"] += 1
                        rows[key][w][c] += 1
        return {l: r for l, r in sorted(rows.items(), key=lambda x: -x[1]["mau"]["total"])}

    base = {
        "relay": relay,
        "generated_at": now,
        "generated_at_iso": datetime.datetime.fromtimestamp(now, TZ).isoformat(),
        "timezone": TZ_LABEL,
        "db": {
            "total_events": summary.get("total_events"),
            "bytes_used": summary.get("db_bytes_used"),
            "events_30d": summary.get("window_events"),
        },
        "excluded": {"bot_nip05_accounts": len(bots), "banned_pubkeys": len(banned),
                     "bridge_accounts": len(bridges & set(actdays))},
        "language_method": {
            "from_own_text": sum(1 for pk in last if pk in votes and votes[pk]),
            "from_profile": sum(1 for pk in last if pk in lang and not votes.get(pk)),
            "inferred_from_reactions": len(inferred),
        },
        "kinds_30d": dict(kinds30.most_common(15)),
    }
    os.makedirs(os.path.join(outdir, "history"), exist_ok=True)

    def write(path, obj):
        with open(path + ".tmp", "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)

    # yesterday = latest complete day (headline); today = in progress
    for d, partial in ((today - 1, False), (today, True)):
        snap = dict(base, date=day_name(d), partial=partial, active=count(d))
        write(os.path.join(outdir, "history", f"{day_name(d)}.json"), snap)
        if not partial:
            out = snap
    out["today"] = {"date": day_name(today), "active": json.load(open(os.path.join(outdir, "history", f"{day_name(today)}.json")))["active"]}
    write(os.path.join(outdir, "stats.json"), out)
    try:
        write_accounts(outdir, today, actdays, lang, inferred, from_cache, votes, profiles, latest_note, bridges, klass, nip05)
    except Exception as e:   # the account list is a debugging aid; never block the stats update
        print(f"accounts list failed: {e!r}", file=sys.stderr)
    render(out, outdir)


def write_accounts(outdir, today, actdays, lang, inferred, from_cache, votes, profiles, latest_note, bridges, klass, nip05):
    """assets/accounts-<lang>.json: every account active in the last 30 days with its profile, newest note and
    the evidence behind its language, so the language split can be checked by eye (landing page #accounts)."""
    adir = os.path.join(outdir, "assets")
    os.makedirs(adir, exist_ok=True)

    def cut(v, n):
        v = v if isinstance(v, str) else ""
        return v if len(v) <= n else v[:n] + "…"

    groups = collections.defaultdict(list)
    for pk, ds in actdays.items():
        recent = [d for d in ds if today - 30 < d <= today]
        if not recent:
            continue
        if pk in votes and votes[pk]:
            how = "note"
        elif pk in lang:
            how = "profile"
        elif pk in inferred:
            how = "history" if pk in from_cache else "reactions"
        else:
            how = "none"
        l = BRIDGE_KEY if pk in bridges else (lang.get(pk) or inferred.get(pk) or "unknown")
        p = profiles.get(pk) if isinstance(profiles.get(pk), dict) else {}
        note = latest_note.get(pk)
        text = None
        if note:
            try:
                text = json.loads(note[1])
            except ValueError:
                text = None
        groups[l].append({
            "pk": pk, "how": how, "votes": dict(votes[pk].most_common(4)) if pk in votes else {},
            "last": day_name(max(recent)), "days": len(recent), "cls": klass(pk),
            "ago": sorted(today - d for d in recent),   # active days, as days before the list date (0 = today)
            "name": cut(p.get("display_name") or p.get("name"), 60), "nip05": cut(nip05(pk), 80),
            "about": cut(p.get("about"), 240), "pic": cut(p.get("picture"), 300) if str(p.get("picture") or "").startswith("https://") else "",
            "note": cut(text, 300) if text else "", "note_at": note[0] if note else 0,
        })
    index, daily = {}, {}
    for l, rows in groups.items():
        rows.sort(key=lambda r: (r["last"], r["note_at"]), reverse=True)
        tmp = os.path.join(adir, f"accounts-{l}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"lang": l, "date": day_name(today), "rows": rows}, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, os.path.join(adir, f"accounts-{l}.json"))
        index[l] = len(rows)
        per_day = [0] * 30
        for r in rows:
            for a in r["ago"]:
                per_day[a] += 1
        daily[l] = per_day
    for n in os.listdir(adir):   # languages that dropped out
        if n.startswith("accounts-") and n.endswith(".json") and n[9:-5] not in index:
            os.remove(os.path.join(adir, n))
    tmp = os.path.join(adir, "accounts.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"date": day_name(today), "langs": index, "daily": daily}, f, separators=(",", ":"))
    os.replace(tmp, os.path.join(adir, "accounts.json"))


def relay_nips(relay):
    """supported_nips from the relay's NIP-11 document (fetched at render time; [] if unreachable)."""
    import urllib.request
    url = os.environ.get("RELAY_NIP11_URL") or "https://" + relay + "/"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/nostr+json", "User-Agent": "relay-stats"})
        with urllib.request.urlopen(req, timeout=10) as r:
            nips = json.load(r).get("supported_nips") or []
        return sorted({int(n) for n in nips if isinstance(n, int) or str(n).isdigit()})
    except Exception:
        return []


def nips_html(nips):
    if not nips:
        return ""
    links = " ".join('<a href="https://github.com/nostr-protocol/nips/blob/master/%02d.md" title="NIP-%02d">%02d</a>' % (n, n, n) for n in nips)
    return '<div class="nips">NIPs ' + links + '</div>'


def render(s, outdir):
    """Inject stats + history into template.html (editable without touching code)."""
    hist = []
    hdir = os.path.join(outdir, "history")
    for name in sorted(n for n in os.listdir(hdir) if n.endswith(".json")):
        try:
            h = json.load(open(os.path.join(hdir, name)))
            if h.get("partial"):
                continue
            # compact: [dau, wau, mau] for the languages that matter on the chart
            hist.append({"date": name[:10], "active": {l: [r[w]["total"] for w in WINDOWS] for l, r in h["active"].items()
                                                       if l in ("_all", "ja", "en", "unknown") or r["mau"]["total"] >= 50}})
        except Exception:
            pass
    tpl = open(os.environ.get("RELAY_STATS_TEMPLATE", os.path.join(HERE, "template.html")), encoding="utf-8").read()
    traffic = None
    try:
        traffic = json.load(open(os.path.join(outdir, "traffic.json")))   # written by traffic.py (relays with an audit trail)
    except (OSError, ValueError):
        pass
    data = json.dumps({"stats": s, "history": hist, "traffic": traffic}, ensure_ascii=False).replace("<", "\\u003c")
    html = (tpl.replace("__RELAY_STATS_DATA__", data).replace("__RELAY_NAME__", s["relay"])
               .replace("<!--SUPPORTED_NIPS-->", nips_html(relay_nips(s["relay"])))
               .replace("<!--SEARCH_FORM-->", SEARCH_FORM if (CONFIG.get("SEARCH_STRFRY") or CONFIG.get("SEARCH_NOSTRFY")) else "")
               .replace("__OG_VERSION__", str(s.get("date", "")).replace("-", "")))   # changes daily -> SNS caches refresh
    tmp = os.path.join(outdir, "index.html.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, os.path.join(outdir, "index.html"))


def rerender(outdir):
    render(json.load(open(os.path.join(outdir, "stats.json"))), outdir)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--render":
        rerender(sys.argv[2])
    else:
        main()
