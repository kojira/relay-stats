#define _DARWIN_C_SOURCE
#define _GNU_SOURCE
// nostrfy-scan: read-only direct LMDB scan of a nostrfy DB. Same TSV output as relay-scan:
//   E <pubkeyhex> <created_at> <kind> <ptarget-hex|-> <content-json|->
//   P <pubkeyhex> <payload-json-string>   (latest kind 0 of each author seen)
//   B <pubkeyhex>                         (Fediverse bridge account)
//   D <pubkeyhex> <day>                   (full mode: first activity per pubkey-day)
//   S <key> <value>
// usage: nostrfy-scan <dbdir> <since_unix> [until_unix|0] [full]
//        nostrfy-scan <dbdir> <since_unix> <until_unix|0> json <kind,kind,...>   (event JSON lines; search-index.py)
//
// nostrfy (heed/LMDB) tables used (src/db/store.rs, v0.1.15):
//   events      id(32)                         -> event JSON (serde_json, field order id,pubkey,created_at,kind,tags,content,sig)
//   event_meta  id(32)                         -> kind(8 BE) created_at(8 BE) pubkey(32) expiry(8 BE)   [meta_index = true]
//   by_created  created_at(8 BE) id(32)        -> ""
//   by_pubkey   pubkey(32) created_at(8 BE) id(32) -> ""
//   by_kind     kind(8 BE) created_at(8 BE) id(32) -> ""
// All keys are plain byte strings (default memcmp order), so no custom comparators are needed.
// Must be built against the same LMDB as nostrfy (lmdb-master-sys, MDB_IDL_LOGN=16) to share the lock file.
#include "lmdb.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>

static uint64_t be64(const unsigned char *p) {
    uint64_t v = 0;
    for (int i = 0; i < 8; i++) v = (v << 8) | p[i];
    return v;
}

static void hex(const unsigned char *p, size_t n, char *out) {
    static const char *h = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) { out[2*i] = h[p[i] >> 4]; out[2*i+1] = h[p[i] & 15]; }
    out[2*n] = 0;
}

// JSON string literal value (including quotes) following `key` in s[0..n)
static int find_str(const char *s, size_t n, const char *key, const char **o, size_t *ol) {
    const char *p = memmem(s, n, key, strlen(key));
    if (!p) return 0;
    p += strlen(key);
    if (p >= s + n || *p != '"') return 0;
    const char *q = p + 1, *end = s + n;
    while (q < end) { if (*q == '\\') q += 2; else if (*q == '"') break; else q++; }
    if (q >= end) return 0;
    *o = p; *ol = (size_t)(q - p + 1);
    return 1;
}

static int is_text_kind(uint64_t k) { return k == 1 || k == 1111 || k == 42 || k == 30023 || k == 9802; }
static int is_activity(uint64_t k) {
    if (k >= 20000 && k < 30000) return 0;
    return !(k == 1059 || k == 13 || k == 10377);
}
static int is_target_kind(uint64_t k) { return k == 7 || k == 6 || k == 16; }

static void trunc_escape(const char *co, size_t *cl) {
    size_t i = 1, last = 1;
    while (i < *cl) {
        size_t step = co[i] == '\\' ? (co[i+1] == 'u' ? 6 : 2) : 1;
        if (i + step > *cl) break;
        i += step; last = i;
    }
    *cl = last;
}

typedef struct { unsigned char (*k)[32]; char *used; uint32_t *cnt; size_t cap, n; } pkset;
static size_t hsh(const unsigned char *p) { uint64_t x; memcpy(&x, p, 8); return (size_t)x; }
static uint32_t *ps_add(pkset *s, const unsigned char *p) {
    if ((s->n + 1) * 2 > s->cap) {
        pkset t = { 0 }; t.cap = s->cap ? s->cap * 2 : 1 << 16;
        t.k = calloc(t.cap, 32); t.used = calloc(t.cap, 1); t.cnt = calloc(t.cap, 4);
        if (!t.k || !t.used || !t.cnt) { fprintf(stderr, "nostrfy-scan: out of memory\n"); exit(1); }
        for (size_t i = 0; i < s->cap; i++) if (s->used[i]) *ps_add(&t, s->k[i]) = s->cnt[i];
        free(s->k); free(s->used); free(s->cnt); *s = t;
    }
    size_t i = hsh(p) & (s->cap - 1);
    while (s->used[i]) { if (!memcmp(s->k[i], p, 32)) return &s->cnt[i]; i = (i + 1) & (s->cap - 1); }
    memcpy(s->k[i], p, 32); s->used[i] = 1; s->n++;
    return &s->cnt[i];
}
static int ps_has(const pkset *s, const unsigned char *p) {
    if (!s->cap) return 0;
    size_t i = hsh(p) & (s->cap - 1);
    while (s->used[i]) { if (!memcmp(s->k[i], p, 32)) return 1; i = (i + 1) & (s->cap - 1); }
    return 0;
}

// first ["p","<64hex>"] tag in event JSON -> out (hex), 1 if found
static int first_ptag(const char *s, size_t n, char *out) {
    const char *t = memmem(s, n, "\"tags\":[", 8);
    if (!t) return 0;
    const char *end = s + n;
    const char *c = memmem(t, end - t, "\"content\":", 10);   // tags precede content in serde order
    if (c) end = c;
    const char *p = t;
    while ((p = memmem(p, end - p, "[\"p\",\"", 6))) {
        p += 6;
        if (end - p >= 65 && p[64] == '"') {
            int ok = 1;
            for (int i = 0; i < 64; i++) { char ch = p[i]; if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'))) { ok = 0; break; } }
            if (ok) { memcpy(out, p, 64); out[64] = 0; return 1; }
        }
    }
    return 0;
}

#define CHK(x) do { int rc_ = (x); if (rc_) { fprintf(stderr, "%s: %s\n", #x, mdb_strerror(rc_)); return 1; } } while (0)

// json mode (search-index.py): print the stored event JSON, one per line, of the given kinds with
// since <= created_at < until, via the by_kind index. Expired events (NIP-40) are skipped.
static int json_dump(const char *dir, uint64_t since, uint64_t until, const char *kinds) {
    MDB_env *env; MDB_txn *txn; MDB_dbi dEv, dMeta, dKind;
    CHK(mdb_env_create(&env));
    CHK(mdb_env_set_maxdbs(env, 64));
    CHK(mdb_env_set_maxreaders(env, 1025));
    CHK(mdb_env_open(env, dir, MDB_RDONLY | MDB_NOTLS, 0644));
    { int dead = 0; mdb_reader_check(env, &dead); if (dead) fprintf(stderr, "cleared %d stale LMDB reader(s)\n", dead); }
    CHK(mdb_txn_begin(env, NULL, MDB_RDONLY, &txn));
    CHK(mdb_dbi_open(txn, "events", 0, &dEv));
    CHK(mdb_dbi_open(txn, "event_meta", 0, &dMeta));
    CHK(mdb_dbi_open(txn, "by_kind", 0, &dKind));
    uint64_t now = (uint64_t)time(NULL);
    MDB_cursor *c; CHK(mdb_cursor_open(txn, dKind, &c));
    for (const char *p = kinds; *p; ) {
        char *endp; uint64_t kind = strtoull(p, &endp, 10);
        if (endp == p) { fprintf(stderr, "bad kind list\n"); return 2; }
        p = *endp == ',' ? endp + 1 : endp;
        unsigned char sk[16];
        for (int i = 0; i < 8; i++) { sk[i] = (unsigned char)(kind >> (56 - 8*i)); sk[8+i] = (unsigned char)(since >> (56 - 8*i)); }
        MDB_val k = { 16, sk }, v;
        for (int rc = mdb_cursor_get(c, &k, &v, MDB_SET_RANGE); rc == 0; rc = mdb_cursor_get(c, &k, &v, MDB_NEXT)) {
            if (k.mv_size != 48) continue;
            const unsigned char *kb = k.mv_data;
            if (be64(kb) != kind || be64(kb + 8) >= until) break;
            MDB_val idk = { 32, (void*)(kb + 16) }, mv, ev;
            if (!mdb_get(txn, dMeta, &idk, &mv) && mv.mv_size >= 56) {
                uint64_t exp = be64((const unsigned char*)mv.mv_data + 48);
                if (exp && exp <= now) continue;
            }
            if (mdb_get(txn, dEv, &idk, &ev)) continue;
            fwrite(ev.mv_data, 1, ev.mv_size, stdout); putchar('\n');
        }
    }
    mdb_cursor_close(c);
    mdb_txn_abort(txn);
    mdb_env_close(env);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: nostrfy-scan <dbdir> <since_unix> [until_unix|0] [full | json <kinds>]\n"); return 2; }
    uint64_t since = strtoull(argv[2], 0, 10);
    uint64_t until = argc > 3 ? strtoull(argv[3], 0, 10) : UINT64_MAX;
    if (until == 0) until = UINT64_MAX;
    int full = argc > 4 && !strcmp(argv[4], "full");
    if (argc > 5 && !strcmp(argv[4], "json")) return json_dump(argv[1], since, until, argv[5]);
    int64_t tzoff = 32400;
    { const char *e = getenv("RELAY_STATS_TZ_OFFSET"); char *endp;
      if (e) { tzoff = strtoll(e, &endp, 10); if (!*e || *endp || tzoff < -50400 || tzoff > 50400) { fprintf(stderr, "bad RELAY_STATS_TZ_OFFSET\n"); return 2; } } }
    unsigned TEXT_CAP = full ? 40 : 0xFFFF, TGT_CAP = full ? 300 : 0xFFFF;

    MDB_env *env; MDB_txn *txn; MDB_dbi dEv, dMeta, dCa, dPk;
    CHK(mdb_env_create(&env));
    CHK(mdb_env_set_maxdbs(env, 64));
    CHK(mdb_env_set_maxreaders(env, 1025));
    // mapsize 0 = use the size recorded in the environment
    CHK(mdb_env_open(env, argv[1], MDB_RDONLY | MDB_NOTLS, 0644));
    // free reader slots left by processes that died mid-read (killed scan, crash): a stale slot pins
    // its old snapshot, so the relay can never reuse freed pages and the data file keeps growing
    { int dead = 0; mdb_reader_check(env, &dead); if (dead) fprintf(stderr, "cleared %d stale LMDB reader(s)\n", dead); }
    CHK(mdb_txn_begin(env, NULL, MDB_RDONLY, &txn));
    CHK(mdb_dbi_open(txn, "events", 0, &dEv));
    CHK(mdb_dbi_open(txn, "event_meta", 0, &dMeta));
    CHK(mdb_dbi_open(txn, "by_created", 0, &dCa));
    CHK(mdb_dbi_open(txn, "by_pubkey", 0, &dPk));

    MDB_stat st; mdb_stat(txn, dEv, &st);
    printf("S\ttotal_events\t%zu\n", st.ms_entries);
    MDB_envinfo ei; mdb_env_info(env, &ei);
    MDB_stat es; mdb_env_stat(env, &es);
    printf("S\tdb_bytes_used\t%zu\n", (size_t)(ei.me_last_pgno + 1) * es.ms_psize);
    printf("S\tscan_since\t%llu\n", (unsigned long long)since);

    pkset authors = { 0 }, days = { 0 }, bridges = { 0 };
    char pkh[65], th[65];
    size_t nev = 0;

    MDB_cursor *c; CHK(mdb_cursor_open(txn, dCa, &c));
    unsigned char sk[8]; for (int i = 0; i < 8; i++) sk[i] = (unsigned char)(since >> (56 - 8*i));
    MDB_val k = { 8, sk }, v;
    int rc = mdb_cursor_get(c, &k, &v, MDB_SET_RANGE);
    for (; rc == 0; rc = mdb_cursor_get(c, &k, &v, MDB_NEXT)) {
        if (k.mv_size != 40) continue;
        const unsigned char *kb = k.mv_data;
        uint64_t ca = be64(kb);
        if (ca >= until) break;
        MDB_val idk = { 32, (void*)(kb + 8) }, mv, ev;
        if (mdb_get(txn, dMeta, &idk, &mv) || mv.mv_size < 56) continue;
        const unsigned char *m = mv.mv_data;
        uint64_t kind = be64(m);
        const unsigned char *pk = m + 16;
        int need_json = 0;
        uint32_t *cnt = ps_add(&authors, pk);
        unsigned tc = *cnt & 0xFFFF, gc = *cnt >> 16;
        hex(pk, 32, pkh); strcpy(th, "-");
        need_json = (is_target_kind(kind) && gc < TGT_CAP) || (is_text_kind(kind) && tc < TEXT_CAP)
                    || ((kind == 0 || kind == 10002 || is_text_kind(kind)) && !ps_has(&bridges, pk));
        const char *js = NULL; size_t jn = 0;
        if (need_json && !mdb_get(txn, dEv, &idk, &ev)) { js = ev.mv_data; jn = ev.mv_size; }
        if (js && is_target_kind(kind) && gc < TGT_CAP && first_ptag(js, jn, th)) gc++;
        nev++;
        if (js && !ps_has(&bridges, pk)) {
            if (memmem(js, jn, "[\"L\",\"pink.momostr\"]", 20) ||
                ((kind == 0 || kind == 10002) && memmem(js, jn, "[\"proxy\",\"", 10) && memmem(js, jn, ",\"activitypub\"]", 15))) {
                ps_add(&bridges, pk); printf("B\t%s\n", pkh);
            }
        }
        if (full && is_activity(kind)) {
            unsigned char dk[32]; memcpy(dk, pk, 32); uint32_t day = (uint32_t)((ca + tzoff) / 86400), x0;
            memcpy(&x0, dk, 4); x0 ^= day * 2654435761u; memcpy(dk, &x0, 4);
            size_t before = days.n; ps_add(&days, dk);
            if (days.n != before) printf("D\t%s\t%u\n", pkh, day);
        }
        int printed = 0;
        const char *co; size_t cl;
        if (js && is_text_kind(kind) && tc < TEXT_CAP && find_str(js, jn, "\"content\":", &co, &cl)) {
            int trunc = 0;
            if (cl > 1200) { cl = 1199; trunc = 1; trunc_escape(co, &cl); }
            printf("E\t%s\t%llu\t%llu\t%s\t%.*s%s\n", pkh, (unsigned long long)ca, (unsigned long long)kind, th, (int)cl, co, trunc ? "\"" : "");
            printed = 1; tc++;
        }
        if (!printed && (!full || th[0] != '-')) printf("E\t%s\t%llu\t%llu\t%s\t-\n", pkh, (unsigned long long)ca, (unsigned long long)kind, th);
        *cnt = (gc << 16) | tc;
    }
    mdb_cursor_close(c);
    printf("S\twindow_events\t%zu\n", nev);
    printf("S\twindow_authors\t%zu\n", authors.n);
    if (full) printf("S\tactive_pubkey_days\t%zu\n", days.n);

    // latest kind 0 per author: walk by_pubkey backwards from pubkey||max, check meta kind
    MDB_cursor *pc; CHK(mdb_cursor_open(txn, dPk, &pc));
    size_t nprof = 0;
    for (size_t i = 0; i < authors.cap; i++) {
        if (!authors.used[i]) continue;
        unsigned char key[72]; memcpy(key, authors.k[i], 32); memset(key + 32, 0xff, 40);
        MDB_val kk = { 72, key }, vv;
        int r = mdb_cursor_get(pc, &kk, &vv, MDB_SET_RANGE);
        r = r == 0 ? mdb_cursor_get(pc, &kk, &vv, MDB_PREV) : mdb_cursor_get(pc, &kk, &vv, MDB_LAST);
        int scanned = 0;
        for (; r == 0 && scanned < 5000; r = mdb_cursor_get(pc, &kk, &vv, MDB_PREV), scanned++) {
            if (kk.mv_size != 72 || memcmp(kk.mv_data, authors.k[i], 32)) break;
            MDB_val idk = { 32, (char*)kk.mv_data + 40 }, mv, ev;
            if (mdb_get(txn, dMeta, &idk, &mv) || mv.mv_size < 56) continue;
            if (be64(mv.mv_data) != 0 || memcmp((char*)mv.mv_data + 16, authors.k[i], 32)) continue;
            if (mdb_get(txn, dEv, &idk, &ev)) break;
            const char *co; size_t cl;
            if (find_str(ev.mv_data, ev.mv_size, "\"content\":", &co, &cl)) {
                hex(authors.k[i], 32, pkh);
                printf("P\t%s\t%.*s\n", pkh, (int)cl, co);
                nprof++;
            }
            break;
        }
    }
    printf("S\tprofiles\t%zu\n", nprof);
    mdb_txn_abort(txn);
    mdb_env_close(env);
    return 0;
}
