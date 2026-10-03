#define _DARWIN_C_SOURCE
// relay-scan: read-only direct LMDB scan of a strfry DB.
// Output (TSV, one record per line):
//   E <pubkeyhex> <created_at> <kind> <ptarget-hex|-> <content-json|->   (events in window)
//   P <pubkeyhex> <payload-json>                                         (latest kind 0 of each author seen)
//   S <key> <value>                                                      (summary)
#include <lmdb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>

static int cmp_su64u64(const MDB_val *a, const MDB_val *b) {
    MDB_val a2 = *a, b2 = *b; a2.mv_size -= 16; b2.mv_size -= 16;
    size_t n = a2.mv_size < b2.mv_size ? a2.mv_size : b2.mv_size;
    int r = memcmp(a2.mv_data, b2.mv_data, n);
    if (r) return r;
    if (a2.mv_size != b2.mv_size) return a2.mv_size < b2.mv_size ? -1 : 1;
    for (int off = 16; off > 0; off -= 8) {
        uint64_t ai, bi;
        memcpy(&ai, (char*)a->mv_data + a->mv_size - off, 8);
        memcpy(&bi, (char*)b->mv_data + b->mv_size - off, 8);
        if (ai != bi) return ai < bi ? -1 : 1;
    }
    return 0;
}

static void hex(const unsigned char *p, size_t n, char *out) {
    static const char *h = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) { out[2*i] = h[p[i] >> 4]; out[2*i+1] = h[p[i] & 15]; }
    out[2*n] = 0;
}

// find the JSON string literal value for "content" in a strfry payload; returns pointer+len of literal incl quotes
static int find_content(const char *s, size_t n, const char **o, size_t *ol) {
    const char *key = "\"content\":";
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

// shorten a JSON string literal prefix [co, co+*cl) so it doesn't end inside an escape sequence
static void trunc_escape(const char *co, size_t *cl) {
    size_t i = 1, last = 1;   // skip the opening quote; last = end of the last complete char/escape
    while (i < *cl) {
        size_t step = co[i] == '\\' ? (co[i+1] == 'u' ? 6 : 2) : 1;
        if (i + step > *cl) break;
        i += step; last = i;
    }
    *cl = last;
}

// simple open-addressing set of 32-byte pubkeys
typedef struct { unsigned char (*k)[32]; char *used; uint32_t *cnt; size_t cap, n; } pkset;
static size_t hsh(const unsigned char *p) { uint64_t x; memcpy(&x, p, 8); return (size_t)x; }
// returns pointer to the per-pubkey counter (2 x uint16: text samples, target samples)
static uint32_t *ps_add(pkset *s, const unsigned char *p) {
    if ((s->n + 1) * 2 > s->cap) {
        pkset t = { 0 }; t.cap = s->cap ? s->cap * 2 : 1 << 16;
        t.k = calloc(t.cap, 32); t.used = calloc(t.cap, 1); t.cnt = calloc(t.cap, 4);
        if (!t.k || !t.used || !t.cnt) { fprintf(stderr, "relay-scan: out of memory (set of %zu)\n", t.cap); exit(1); }
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

#define CHK(x) do { int rc_ = (x); if (rc_) { fprintf(stderr, "%s: %s\n", #x, mdb_strerror(rc_)); return 1; } } while (0)

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: relay-scan <dbdir> <since_unix> [until_unix] [full]\n"
        "  default: walk the created_at index (fast for recent windows)\n"
        "  full:    walk the Event table sequentially (fast for whole-history backfill);\n"
        "           text/target samples are capped per pubkey\n"
        "  RELAY_STATS_TZ_OFFSET: seconds east of UTC for day boundaries (default 32400 = JST)\n"); return 2; }
    uint64_t since = strtoull(argv[2], 0, 10);
    uint64_t until = argc > 3 ? strtoull(argv[3], 0, 10) : UINT64_MAX;
    if (until == 0) until = UINT64_MAX;
    int full = argc > 4 && !strcmp(argv[4], "full");
    int64_t tzoff = 32400; // day boundary default: JST
    {
        const char *e = getenv("RELAY_STATS_TZ_OFFSET"); char *endp;
        if (e) {
            tzoff = strtoll(e, &endp, 10);
            if (!*e || *endp || tzoff < -50400 || tzoff > 50400) { fprintf(stderr, "relay-scan: bad RELAY_STATS_TZ_OFFSET '%s'\n", e); return 2; }
        }
    }
    unsigned TEXT_CAP = full ? 40 : 0xFFFF, TGT_CAP = full ? 300 : 0xFFFF;
    MDB_env *env; MDB_txn *txn;
    MDB_dbi dEv, dPl, dCa, dPk;
    CHK(mdb_env_create(&env));
    CHK(mdb_env_set_maxdbs(env, 64));
    CHK(mdb_env_set_mapsize(env, (size_t)10995116277760ULL));
    CHK(mdb_env_set_maxreaders(env, 1025));
    CHK(mdb_env_open(env, argv[1], MDB_RDONLY, 0644));
    CHK(mdb_txn_begin(env, NULL, MDB_RDONLY, &txn));
    CHK(mdb_dbi_open(txn, "rasgueadb_defaultDb__Event", MDB_INTEGERKEY, &dEv));
    CHK(mdb_dbi_open(txn, "rasgueadb_defaultDb__EventPayload", MDB_INTEGERKEY, &dPl));
    CHK(mdb_dbi_open(txn, "rasgueadb_defaultDb__Event__created_at", MDB_DUPSORT|MDB_INTEGERDUP|MDB_DUPFIXED|MDB_INTEGERKEY, &dCa));
    CHK(mdb_dbi_open(txn, "rasgueadb_defaultDb__Event__pubkeyKind", MDB_DUPSORT|MDB_INTEGERDUP|MDB_DUPFIXED, &dPk));
    CHK(mdb_set_compare(txn, dPk, cmp_su64u64));

    MDB_stat st; mdb_stat(txn, dEv, &st);
    printf("S\ttotal_events\t%zu\n", st.ms_entries);
    MDB_envinfo ei; mdb_env_info(env, &ei);
    MDB_stat es; mdb_env_stat(env, &es);   // page size is fixed when the DB is created (OS page size at that time)
    printf("S\tdb_bytes_used\t%zu\n", (size_t)(ei.me_last_pgno + 1) * es.ms_psize);
    printf("S\tscan_since\t%llu\n", (unsigned long long)since);

    pkset authors = { 0 }, days = { 0 }, bridges = { 0 };
    char pkh[65], th[65];
    size_t nev = 0;

#define HANDLE_EVENT(levp, evv) do { \
        const unsigned char *b = (evv).mv_data; \
        if ((evv).mv_size < 88) break; \
        uint64_t ca, kind; memcpy(&ca, b+64, 8); memcpy(&kind, b+72, 8); \
        if (ca < since || ca >= until) break; \
        uint32_t *cnt = ps_add(&authors, b+32); \
        unsigned tc = *cnt & 0xFFFF, gc = *cnt >> 16; \
        hex(b+32, 32, pkh); strcpy(th, "-"); \
        if (is_target_kind(kind) && gc < TGT_CAP) { \
            size_t off = 88; \
            while (off + 2 <= (evv).mv_size) { \
                unsigned char tn = b[off], tl = b[off+1]; \
                if (tn == 'p' && tl == 32 && off + 2 + 32 <= (evv).mv_size) { hex(b+off+2, 32, th); gc++; break; } \
                off += 2 + tl; \
            } \
        } \
        nev++; \
        { /* Fediverse bridge marker (momostr): tag ["L","pink.momostr"] */ \
            size_t off = 88; \
            while (off + 2 <= (evv).mv_size) { \
                unsigned char tn = b[off], tl = b[off+1]; \
                if (tn == 'L' && tl == 12 && off + 2 + 12 <= (evv).mv_size && !memcmp(b+off+2, "pink.momostr", 12)) { \
                    if (!ps_has(&bridges, b+32)) { ps_add(&bridges, b+32); printf("B\t%s\n", pkh); } \
                    break; \
                } \
                off += 2 + tl; \
            } \
            /* Mostr: ["proxy", <url>, "activitypub"] (not indexed; check payload of kind 0 / 10002 once per pubkey) */ \
            if ((kind == 0 || kind == 10002) && !ps_has(&bridges, b+32)) { \
                MDB_val lk = { 8, (levp) }, pv; \
                if (!mdb_get(txn, dPl, &lk, &pv) && pv.mv_size > 1 && ((char*)pv.mv_data)[0] == 0 && \
                    memmem(pv.mv_data, pv.mv_size, "[\"proxy\",\"", 10) && memmem(pv.mv_data, pv.mv_size, ",\"activitypub\"]", 15)) { \
                    ps_add(&bridges, b+32); printf("B\t%s\n", pkh); \
                } \
            } \
        } \
        if (full) { \
            if (is_activity(kind)) { \
                unsigned char dk[32]; memcpy(dk, b+32, 32); uint32_t day = (uint32_t)((ca + tzoff) / 86400), x0; \
                memcpy(&x0, dk, 4); x0 ^= day * 2654435761u; memcpy(dk, &x0, 4); \
                size_t before = days.n; ps_add(&days, dk); \
                if (days.n != before) printf("D\t%s\t%u\n", pkh, day); \
            } \
        } \
        int printed = 0; \
        if (is_text_kind(kind) && tc < TEXT_CAP) { \
            MDB_val lk = { 8, (levp) }, pv; const char *co; size_t cl; \
            if (!mdb_get(txn, dPl, &lk, &pv) && pv.mv_size > 1 && ((char*)pv.mv_data)[0] == 0 && \
                find_content((char*)pv.mv_data + 1, pv.mv_size - 1, &co, &cl)) { \
                int trunc = 0; \
                if (cl > 1200) { cl = 1199; trunc = 1; trunc_escape(co, &cl); } \
                printf("E\t%s\t%llu\t%llu\t%s\t%.*s%s\n", pkh, (unsigned long long)ca, (unsigned long long)kind, th, (int)cl, co, trunc ? "\"" : ""); \
                printed = 1; tc++; \
            } \
        } \
        if (!printed && (!full || th[0] != '-')) printf("E\t%s\t%llu\t%llu\t%s\t-\n", pkh, (unsigned long long)ca, (unsigned long long)kind, th); \
        *cnt = (gc << 16) | tc; \
    } while (0)

    if (full) {
        MDB_cursor *c; CHK(mdb_cursor_open(txn, dEv, &c));
        MDB_val k, v;
        int rc = mdb_cursor_get(c, &k, &v, MDB_FIRST);
        while (rc == 0) {
            uint64_t lev; memcpy(&lev, k.mv_data, 8);
            HANDLE_EVENT(&lev, v);
            rc = mdb_cursor_get(c, &k, &v, MDB_NEXT);
        }
    } else {
        MDB_cursor *c; CHK(mdb_cursor_open(txn, dCa, &c));
        MDB_val k = { 8, &since }, v;
        int rc = mdb_cursor_get(c, &k, &v, MDB_SET_RANGE);
        while (rc == 0) {
            uint64_t kt; memcpy(&kt, k.mv_data, 8);
            if (kt >= until) break;
            rc = mdb_cursor_get(c, &k, &v, MDB_GET_MULTIPLE);
            while (rc == 0) {
                size_t n = v.mv_size / 8;
                for (size_t i = 0; i < n; i++) {
                    uint64_t lev; memcpy(&lev, (char*)v.mv_data + i*8, 8);
                    MDB_val lk = { 8, &lev }, ev;
                    if (mdb_get(txn, dEv, &lk, &ev)) continue;
                    HANDLE_EVENT(&lev, ev);
                }
                rc = mdb_cursor_get(c, &k, &v, MDB_NEXT_MULTIPLE);
            }
            rc = mdb_cursor_get(c, &k, &v, MDB_NEXT_NODUP);
        }
    }
    printf("S\twindow_events\t%zu\n", nev);
    printf("S\twindow_authors\t%zu\n", authors.n);
    if (full) printf("S\tactive_pubkey_days\t%zu\n", days.n);

    // latest kind 0 per author via pubkeyKind index (key = pubkey + kind(8) + created_at(8), value = levId)
    MDB_cursor *pc; CHK(mdb_cursor_open(txn, dPk, &pc));
    size_t nprof = 0;
    for (size_t i = 0; i < authors.cap; i++) {
        if (!authors.used[i]) continue;
        unsigned char key[48]; memcpy(key, authors.k[i], 32);
        uint64_t kz = 0, cmax = UINT64_MAX; memcpy(key+32, &kz, 8); memcpy(key+40, &cmax, 8);
        MDB_val kk = { 48, key }, vv;
        int r = mdb_cursor_get(pc, &kk, &vv, MDB_SET_RANGE);
        if (r == 0) r = mdb_cursor_get(pc, &kk, &vv, MDB_PREV_NODUP); else r = mdb_cursor_get(pc, &kk, &vv, MDB_LAST);
        if (r) continue;
        if (r == 0) r = mdb_cursor_get(pc, &kk, &vv, MDB_LAST_DUP);
        if (r || kk.mv_size != 48 || memcmp(kk.mv_data, authors.k[i], 32)) continue;
        uint64_t kind; memcpy(&kind, (char*)kk.mv_data + 32, 8);
        if (kind != 0) continue;
        uint64_t lev; memcpy(&lev, vv.mv_data, 8);
        MDB_val lk = { 8, &lev }, pv;
        if (mdb_get(txn, dPl, &lk, &pv) || pv.mv_size < 2 || ((char*)pv.mv_data)[0] != 0) continue;
        hex(authors.k[i], 32, pkh);
        const char *co; size_t cl;
        if (find_content((char*)pv.mv_data + 1, pv.mv_size - 1, &co, &cl))
            printf("P\t%s\t%.*s\n", pkh, (int)cl, co);
        nprof++;
    }
    printf("S\tprofiles\t%zu\n", nprof);
    mdb_txn_abort(txn);
    mdb_env_close(env);
    return 0;
}
