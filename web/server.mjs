// relay-stats web: njump-style pages and full-text search for one relay, rendered on the server
// so search engines can read them.
//
//   /npub1…            profile + latest notes          /npub/<hex>  -> 301 /npub1…
//   /note1…            one note (kind 1) or article    /event/<hex> -> 301 /note1…
//   /search/<words>    full-text search (noindex,follow)  /search?q= -> 301 /search/<q>
//   /robots.txt, /sitemap.xml, /sitemap-<n>.xml
// Everything else is 404 (nprofile / nevent / naddr included).
//
// Events are read from the relay over its own WebSocket (never the LMDB directly); search uses the
// SQLite FTS5 index built by search-index.py. Hidden (404): banned pubkeys (re-read from the banlist
// file when it changes), deleted events, and events / profiles flagged NSFW (content-warning tag,
// "nsfw" t / l tag, #nsfw in the text).
//
// env: WEB_PORT (7780), WEB_RELAY_WS (ws://127.0.0.1:7777), WEB_BASE (https://r.kojira.io),
//      WEB_SEARCH_DB (../state/search/search.sqlite), WEB_BANLIST, WEB_RELAY_NAME (r.kojira.io)

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { fileURLToPath } from 'node:url';
import { nip19, verifyEvent } from 'nostr-tools';
import { marked } from 'marked';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PORT = +(process.env.WEB_PORT || 7780);
const RELAY_WS = process.env.WEB_RELAY_WS || 'ws://127.0.0.1:7777';
const BASE = (process.env.WEB_BASE || 'https://r.kojira.io').replace(/\/$/, '');
const RELAY_NAME = process.env.WEB_RELAY_NAME || new URL(BASE).host;
const SEARCH_DB = process.env.WEB_SEARCH_DB || path.join(HERE, '..', 'state', 'search', 'search.sqlite');
const BANLIST = process.env.WEB_BANLIST || '';
const PAGE = 30, SITEMAP_PAGE = 45000;
const HEX64 = /^[0-9a-f]{64}$/;

// ---------- banlist (hot reload) ----------
let banned = new Set(), banMtime = 0;
function bans() {
  if (!BANLIST) return banned;
  try {
    const st = fs.statSync(BANLIST);
    if (st.mtimeMs !== banMtime) {
      banMtime = st.mtimeMs;
      banned = new Set(fs.readFileSync(BANLIST, 'utf8').split('\n').map(l => l.trim()).filter(l => HEX64.test(l)));
    }
  } catch { /* keep the last list */ }
  return banned;
}

// ---------- search index ----------
let db = null, dbIno = 0;
function sdb() {
  try {
    const st = fs.statSync(SEARCH_DB);
    if (!db || st.ino !== dbIno) {           // rebuilt (renamed into place) -> reopen
      try { db && db.close(); } catch {}
      db = new DatabaseSync(SEARCH_DB, { readOnly: true });
      dbIno = st.ino;
    }
  } catch { db = null; }
  return db;
}

// ---------- relay client (one shared connection, REQ -> events until EOSE) ----------
let ws = null, wsReady = null, subSeq = 0;
const pending = new Map();
function relay() {
  if (ws && ws.readyState <= 1) return wsReady;
  ws = new WebSocket(RELAY_WS);
  wsReady = new Promise((res, rej) => {
    ws.addEventListener('open', () => res(), { once: true });
    ws.addEventListener('error', () => rej(new Error('relay connect failed')), { once: true });
  });
  ws.addEventListener('message', m => {
    let d; try { d = JSON.parse(m.data); } catch { return; }
    const p = pending.get(d[1]);
    if (!p) return;
    if (d[0] === 'EVENT') p.events.push(d[2]);
    else if (d[0] === 'EOSE' || d[0] === 'CLOSED') { ws.send(JSON.stringify(['CLOSE', d[1]])); pending.delete(d[1]); clearTimeout(p.t); p.res(p.events); }
  });
  ws.addEventListener('close', () => { for (const [k, p] of pending) { clearTimeout(p.t); p.res(p.events); pending.delete(k); } ws = null; });
  return wsReady;
}
async function req(...filters) {
  await relay();
  const id = 'w' + (++subSeq);
  return new Promise(res => {
    const p = { events: [], res, t: setTimeout(() => { pending.delete(id); res(p.events); }, 8000) };
    pending.set(id, p);
    ws.send(JSON.stringify(['REQ', id, ...filters]));
  });
}

// ---------- small caches ----------
const cache = new Map();
function cached(key, ttl, fn) {
  const c = cache.get(key);
  if (c && c.exp > Date.now()) return c.v;
  const v = fn();
  cache.set(key, { v, exp: Date.now() + ttl });
  if (cache.size > 5000) for (const k of [...cache.keys()].slice(0, 1000)) cache.delete(k);
  return v;
}

const nsfw = e => (e.tags || []).some(t => t[0] === 'content-warning' || ((t[0] === 't' || t[0] === 'l') && String(t[1] || '').toLowerCase() === 'nsfw'))
  || /(^|[\s#＃])#nsfw\b|＃nsfw\b/i.test(e.content || '');
const visible = e => e && verifyEvent(e) && !bans().has(e.pubkey) && !nsfw(e);

async function profileOf(pk) {
  return cached('p:' + pk, 300_000, async () => {
    const [e] = (await req({ kinds: [0], authors: [pk], limit: 1 })).sort((a, b) => b.created_at - a.created_at);
    let p = {};
    try { p = e ? JSON.parse(e.content) : {}; } catch {}
    return { ev: e || null, p: p && typeof p === 'object' ? p : {} };
  });
}
async function profilesOf(pks) {
  const need = [...new Set(pks)].filter(pk => !cache.has('p:' + pk));
  if (need.length) {
    const evs = await req({ kinds: [0], authors: need });
    const best = {};
    for (const e of evs) if (!best[e.pubkey] || best[e.pubkey].created_at < e.created_at) best[e.pubkey] = e;
    for (const pk of need) {
      let p = {}; try { p = best[pk] ? JSON.parse(best[pk].content) : {}; } catch {}
      cache.set('p:' + pk, { v: Promise.resolve({ ev: best[pk] || null, p: p && typeof p === 'object' ? p : {} }), exp: Date.now() + 300_000 });
    }
  }
  const out = {};
  for (const pk of new Set(pks)) out[pk] = await profileOf(pk);
  return out;
}
async function isDeleted(e) {
  const filters = [{ kinds: [5], authors: [e.pubkey], '#e': [e.id], limit: 1 }];
  if (e.kind === 30023) {
    const d = (e.tags.find(t => t[0] === 'd') || [])[1] || '';
    filters.push({ kinds: [5], authors: [e.pubkey], '#a': [`30023:${e.pubkey}:${d}`], limit: 1 });
  }
  return (await req(...filters)).length > 0;
}

// ---------- rendering ----------
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const npubOf = pk => nip19.npubEncode(pk);
const noteOf = id => nip19.noteEncode(id);
const nameOf = (pk, p) => (p.display_name || p.displayName || p.name || '').toString().slice(0, 80) || npubOf(pk).slice(0, 16) + '…';
const fmtTime = ts => new Date(ts * 1000).toLocaleString('sv-SE', { timeZone: 'Asia/Tokyo' }).slice(0, 16) + ' JST';
const isoTime = ts => new Date(ts * 1000).toISOString();
const IMG = /\.(png|jpe?g|gif|webp|avif)(\?\S*)?$/i, VID = /\.(mp4|webm|mov)(\?\S*)?$/i;
const safeUrl = u => /^https?:\/\//i.test(u) ? u : '';

function richText(content) {
  // URLs -> links / media, nostr: refs -> local links, hashtags -> search links; everything else escaped
  const parts = String(content).split(/(https?:\/\/[^\s<>"'）」】]+|nostr:(?:npub1|note1|nprofile1|nevent1|naddr1)[0-9a-z]+|#[^\s#＃.,!?、。:;()[\]{}"'<>]+)/gu);
  let html = '', media = [];
  for (const s of parts) {
    if (!s) continue;
    if (/^https?:\/\//i.test(s)) {
      if (IMG.test(s)) media.push(`<a href="${esc(s)}" rel="nofollow noopener" target="_blank"><img src="${esc(s)}" loading="lazy" alt=""></a>`);
      else if (VID.test(s)) media.push(`<video src="${esc(s)}" controls preload="none"></video>`);
      html += `<a href="${esc(s)}" rel="nofollow noopener ugc" target="_blank">${esc(s.length > 60 ? s.slice(0, 57) + '…' : s)}</a>`;
    } else if (s.startsWith('nostr:')) {
      const b = s.slice(6);
      try {
        const d = nip19.decode(b);
        const href = d.type === 'npub' ? '/' + b : d.type === 'note' ? '/' + b
          : d.type === 'nprofile' ? '/' + npubOf(d.data.pubkey) : d.type === 'nevent' ? '/' + noteOf(d.data.id) : '';
        html += href ? `<a href="${href}">${esc(b.slice(0, 18))}…</a>` : esc(s);
      } catch { html += esc(s); }
    } else if (s[0] === '#' && s.length > 1) {
      html += `<a href="/search/${encodeURIComponent(s)}">${esc(s)}</a>`;
    } else html += esc(s);
  }
  return html.replace(/\n/g, '<br>') + (media.length ? `<div class="media">${media.join('')}</div>` : '');
}
function articleHtml(content) {
  const raw = marked.parse(String(content), { gfm: true, breaks: false, async: false });
  // strip scripts / handlers / iframes; keep the rest as rendered markdown
  return raw.replace(/<(script|style|iframe|object|embed)[\s\S]*?<\/\1>/gi, '')
            .replace(/<(script|iframe|object|embed)[^>]*>/gi, '')
            .replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, '')
            .replace(/(href|src)\s*=\s*"(?:javascript|data):[^"]*"/gi, '$1="#"');
}
const plain = (s, n) => { s = String(s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };

const CSS = `
:root{--bg:#05070d;--panel:#0b1220;--line:#1b2a3e;--fg:#d7e3f0;--dim:#7d93ab;--acc:#00e5ff;--mag:#ff4fd8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.7 ui-sans-serif,system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{border-bottom:1px solid var(--line);padding:12px 18px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
header .brand{font:700 18px ui-monospace,Menlo,monospace;letter-spacing:.06em;background:linear-gradient(90deg,var(--acc),var(--mag));-webkit-background-clip:text;color:transparent}
header form{flex:1;display:flex;gap:6px;min-width:220px;max-width:520px}
header input{flex:1;background:var(--panel);border:1px solid var(--line);color:var(--fg);padding:7px 10px;border-radius:6px;font-size:14px}
header button{background:var(--acc);color:#001;border:0;border-radius:6px;padding:0 14px;font-weight:700;cursor:pointer}
main{max-width:760px;margin:0 auto;padding:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 12px}
.who{display:flex;gap:10px;align-items:center;margin-bottom:6px}.who img{width:40px;height:40px;border-radius:50%;object-fit:cover;background:#123}
.who .n{font-weight:700}.who .s{color:var(--dim);font-size:12px;word-break:break-all}
.body{word-break:break-word;overflow-wrap:anywhere}.media img,.media video{max-width:100%;max-height:520px;border-radius:8px;margin-top:8px;display:block}
.meta{color:var(--dim);font-size:12px;margin-top:8px;display:flex;gap:12px;flex-wrap:wrap}
.prof{display:flex;gap:16px;align-items:flex-start}.prof img.av{width:96px;height:96px;border-radius:50%;object-fit:cover;background:#123}
.prof h1{margin:0;font-size:22px}.banner{width:100%;max-height:200px;object-fit:cover;border-radius:10px;margin-bottom:12px}
h1.t{font-size:24px;line-height:1.4;margin:.2em 0 .4em}.article img{max-width:100%}.article pre{overflow:auto;background:#0004;padding:10px;border-radius:6px}
.pager{display:flex;justify-content:space-between;margin:16px 0}.hint{color:var(--dim);font-size:13px}
.filters summary{cursor:pointer;color:var(--dim)}.filters form{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:flex-end;margin-top:10px}
.filters label{display:flex;flex-direction:column;font-size:12px;color:var(--dim);gap:3px}.filters label.wide{flex:1;min-width:240px}
.filters input,.filters select{background:var(--bg);border:1px solid var(--line);color:var(--fg);padding:6px 8px;border-radius:6px;font:inherit;font-size:14px;color-scheme:dark}
.filters button{background:var(--acc);color:#001;border:0;border-radius:6px;padding:7px 16px;font-weight:700;cursor:pointer}
.presets{display:flex;gap:10px;font-size:13px;width:100%}
mark{background:#ff4fd833;color:inherit;border-radius:3px}footer{color:var(--dim);font-size:12px;text-align:center;padding:24px}
`;
function page({ title, desc = '', canonical = '', robots = 'index,follow', og = {}, body, q = '', jsonld = null, status = 200 }) {
  const t = esc(title), d = esc(plain(desc, 200));
  return [status, `<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${t}</title><meta name="description" content="${d}"><meta name="robots" content="${robots}">
${canonical ? `<link rel="canonical" href="${esc(canonical)}">` : ''}
<meta property="og:site_name" content="${esc(RELAY_NAME)}"><meta property="og:title" content="${t}"><meta property="og:description" content="${d}">
<meta property="og:type" content="${og.type || 'website'}">${canonical ? `<meta property="og:url" content="${esc(canonical)}">` : ''}
${og.image ? `<meta property="og:image" content="${esc(og.image)}">` : `<meta property="og:image" content="${BASE}/assets/og.png">`}
<meta name="twitter:card" content="${og.image ? 'summary' : 'summary_large_image'}">
${jsonld ? `<script type="application/ld+json">${JSON.stringify(jsonld).replace(/</g, '\\u003c')}</script>` : ''}
<style>${CSS}</style></head><body>
<header><a class="brand" href="/">${esc(RELAY_NAME.toUpperCase())}</a>
<form action="/search" method="get" role="search"><input name="q" value="${esc(q)}" placeholder="Search notes and articles (3+ characters)" aria-label="search"><button>Search</button></form></header>
<main>${body}</main><footer>Nostr events stored on wss://${esc(RELAY_NAME)} · <a href="/">relay stats</a></footer></body></html>`];
}
const notFound = (msg = 'Not found') => page({ title: `404 · ${RELAY_NAME}`, robots: 'noindex', status: 404,
  body: `<div class="card"><h1 class="t">404</h1><p class="hint">${esc(msg)}</p></div>` });

function whoHtml(pk, prof, small = false) {
  const p = prof.p || {}, pic = safeUrl(String(p.picture || ''));
  return `<div class="who">${pic ? `<img src="${esc(pic)}" alt="" loading="lazy">` : '<img alt="">'}<div><a class="n" href="/${npubOf(pk)}">${esc(nameOf(pk, p))}</a>
<div class="s">${p.nip05 ? esc(String(p.nip05).slice(0, 80)) + ' · ' : ''}${small ? '' : esc(npubOf(pk))}</div></div></div>`;
}
function noteCard(e, prof, hl = '') {
  const title = e.kind === 30023 ? ((e.tags.find(t => t[0] === 'title') || [])[1] || '') : '';
  let text = e.kind === 30023 ? plain(((e.tags.find(t => t[0] === 'summary') || [])[1]) || e.content, 280) : e.content;
  let body = richText(text);
  if (hl) for (const w of hl.split(/\s+/).filter(w => w.length >= 2)) {
    const re = new RegExp('(' + esc(w).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')(?![^<]*>)', 'gi');
    body = body.replace(re, '<mark>$1</mark>');
  }
  return `<article class="card">${whoHtml(e.pubkey, prof, true)}
${title ? `<h2 style="margin:.2em 0"><a href="/${noteOf(e.id)}">${esc(title)}</a></h2>` : ''}
<div class="body">${body}</div>
<div class="meta"><a href="/${noteOf(e.id)}"><time datetime="${isoTime(e.created_at)}">${fmtTime(e.created_at)}</time></a>${e.kind === 30023 ? '<span>article</span>' : ''}</div></article>`;
}

// ---------- handlers ----------
async function notePage(id) {
  const [e] = await req({ ids: [id] });
  if (!e || (e.kind !== 1 && e.kind !== 30023) || !visible(e) || await isDeleted(e)) return notFound();
  const prof = await profileOf(e.pubkey);
  if (prof.ev && nsfw(prof.ev)) return notFound();
  const name = nameOf(e.pubkey, prof.p), canonical = `${BASE}/${noteOf(e.id)}`;
  let body, title, desc;
  if (e.kind === 30023) {
    const tag = k => (e.tags.find(t => t[0] === k) || [])[1] || '';
    title = tag('title') || plain(e.content, 60); desc = tag('summary') || plain(e.content, 200);
    const img = safeUrl(tag('image'));
    body = `<article class="card">${whoHtml(e.pubkey, prof)}${img ? `<img class="banner" src="${esc(img)}" alt="">` : ''}
<h1 class="t">${esc(title)}</h1><div class="body article">${articleHtml(e.content)}</div>
<div class="meta"><time datetime="${isoTime(+tag('published_at') || e.created_at)}">${fmtTime(+tag('published_at') || e.created_at)}</time><span>updated ${fmtTime(e.created_at)}</span></div></article>`;
    title = `${title} — ${name}`;
  } else {
    title = `${name}: "${plain(e.content, 60)}"`; desc = plain(e.content, 200);
    body = noteCard(e, prof).replace('class="card"', 'class="card" style="font-size:17px"');
  }
  const img = (String(e.content).match(/https?:\/\/\S+\.(?:png|jpe?g|gif|webp)/i) || [])[0] || safeUrl(String(prof.p.picture || ''));
  return page({ title, desc, canonical, og: { type: 'article', image: img }, body,
    jsonld: { '@context': 'https://schema.org', '@type': e.kind === 30023 ? 'Article' : 'SocialMediaPosting', headline: plain(title, 110),
      datePublished: isoTime(e.created_at), author: { '@type': 'Person', name, url: `${BASE}/${npubOf(e.pubkey)}` }, url: canonical } });
}

async function profilePage(pk, until) {
  if (bans().has(pk)) return notFound();
  const [prof, evs] = await Promise.all([profileOf(pk), req({ kinds: [1, 30023], authors: [pk], limit: PAGE * 2, ...(until ? { until } : {}) })]);
  if (prof.ev && (nsfw(prof.ev) || !verifyEvent(prof.ev))) return notFound();
  const deleted = new Set((await req({ kinds: [5], authors: [pk], limit: 500 })).flatMap(d => d.tags.filter(t => t[0] === 'e').map(t => t[1])));
  const notes = evs.filter(e => visible(e) && !deleted.has(e.id)).sort((a, b) => b.created_at - a.created_at).slice(0, PAGE);
  if (!prof.ev && !notes.length) return notFound();
  const p = prof.p, name = nameOf(pk, p), npub = npubOf(pk), pic = safeUrl(String(p.picture || '')), banner = safeUrl(String(p.banner || ''));
  const more = notes.length === PAGE ? `<div class="pager"><span></span><a href="/${npub}?until=${notes[notes.length - 1].created_at - 1}" rel="nofollow">older →</a></div>` : '';
  const body = `${banner ? `<img class="banner" src="${esc(banner)}" alt="">` : ''}<div class="card prof">${pic ? `<img class="av" src="${esc(pic)}" alt="">` : ''}
<div><h1>${esc(name)}</h1><div class="hint">${p.nip05 ? esc(String(p.nip05).slice(0, 80)) + '<br>' : ''}<span style="word-break:break-all">${npub}</span></div>
${p.about ? `<div class="body" style="margin-top:8px">${richText(String(p.about).slice(0, 2000))}</div>` : ''}
${p.website && safeUrl(String(p.website)) ? `<div class="meta"><a href="${esc(p.website)}" rel="nofollow noopener ugc">${esc(String(p.website).slice(0, 80))}</a></div>` : ''}</div></div>
${notes.map(e => noteCard(e, prof)).join('') || '<p class="hint">No notes stored on this relay.</p>'}${more}`;
  return page({ title: `${name} (${npub.slice(0, 14)}…) · nostr`, desc: p.about ? plain(p.about, 200) : `${name} on nostr`,
    canonical: `${BASE}/${npub}`, robots: until ? 'noindex,follow' : 'index,follow', og: { type: 'profile', image: pic }, body,
    jsonld: { '@context': 'https://schema.org', '@type': 'ProfilePage', mainEntity: { '@type': 'Person', name, identifier: npub, url: `${BASE}/${npub}` } } });
}

function ftsQuery(q) {
  // each word quoted (FTS5 phrase); trigram needs >= 3 characters per word
  const words = q.split(/\s+/).filter(Boolean).slice(0, 8);
  return { long: words.filter(w => [...w].length >= 3), short: words.filter(w => [...w].length < 3) };
}
// ---- search filters: ?since=YYYY-MM-DD&until=YYYY-MM-DD (JST, inclusive) &author=npub1…|hex &kind=note|article
const JST = 9 * 3600;
const dayStart = v => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v || ''); if (!m) return 0;
  const t = Date.UTC(+m[1], +m[2] - 1, +m[3]) / 1000 - JST; return Number.isFinite(t) && t > 0 ? t : 0; };
function parseFilters(sp) {
  const f = { since: dayStart(sp.get('since')), until: dayStart(sp.get('until')), author: '', kind: '' };
  if (f.until) f.until += 86400 - 1;
  const a = (sp.get('author') || '').trim();
  if (HEX64.test(a)) f.author = a;
  else if (a.startsWith('npub1') || a.startsWith('nprofile1')) { try { const d = nip19.decode(a); f.author = d.type === 'npub' ? d.data : d.data.pubkey; } catch {} }
  else if (a.startsWith('nostr:')) { try { const d = nip19.decode(a.slice(6)); f.author = d.type === 'npub' ? d.data : d.data.pubkey; } catch {} }
  if (sp.get('kind') === 'note' || sp.get('kind') === 'article') f.kind = sp.get('kind');
  f.badAuthor = !!a && !f.author;
  return f;
}
const ymd = t => new Date((t + JST) * 1000).toISOString().slice(0, 10);
function filterQuery(f, extra = {}) {
  const o = new URLSearchParams();
  if (f.since) o.set('since', ymd(f.since));
  if (f.until) o.set('until', ymd(f.until));
  if (f.author) o.set('author', npubOf(f.author));
  if (f.kind) o.set('kind', f.kind);
  for (const [k, v] of Object.entries(extra)) o.set(k, v);
  const s = o.toString(); return s ? '?' + s : '';
}
function filterForm(q, f) {
  const sel = (v, l) => `<option value="${v}"${f.kind === v ? ' selected' : ''}>${l}</option>`;
  const open = f.since || f.until || f.author || f.kind;
  return `<details class="card filters"${open ? ' open' : ''}><summary>Filters${open ? ' (active)' : ''}</summary>
<form action="/search" method="get"><input type="hidden" name="q" value="${esc(q)}">
<label>From <input type="date" name="since" value="${f.since ? ymd(f.since) : ''}"></label>
<label>To <input type="date" name="until" value="${f.until ? ymd(f.until) : ''}"></label>
<label>Type <select name="kind">${sel('', 'All')}${sel('note', 'Notes (kind 1)')}${sel('article', 'Articles (kind 30023)')}</select></label>
<label class="wide">Author <input name="author" value="${f.author ? npubOf(f.author) : ''}" placeholder="npub1…"></label>
<div class="presets">${[['24h', 1], ['7 days', 7], ['30 days', 30], ['1 year', 365]].map(([l, d]) =>
  `<a href="/search/${encodeURIComponent(q)}${filterQuery({ ...f, since: Math.floor(Date.now() / 1000) - d * 86400, until: 0 })}" rel="nofollow">${l}</a>`).join('')}
${open ? `<a href="/search/${encodeURIComponent(q)}" rel="nofollow">clear</a>` : ''}</div>
<button>Apply</button></form></details>`;
}
const ROWID_SHIFT = 2 ** 20;
const likeArg = w => '%' + w.replace(/[\\%_]/g, c => '\\' + c) + '%';
// Query planning. The FTS walk (newest first) is fast when matches are dense, but with a narrow filter
// (an author, articles only) most matches are thrown away. A narrow filter is scanned directly instead:
// that author's / all articles, newest first, with LIKE on the text (same case-insensitivity as trigram
// for ASCII). Cut-over: the filter set is small (<= DIRECT_MAX rows).
const DIRECT_MAX = 60000;
function searchRows(d, match, long, short, lo, hi, f) {
  const cond = ['n.nsfw = 0'], args = [];
  if (f.author) { cond.push('n.pubkey = unhex(?)'); args.push(f.author); }
  if (f.kind) cond.push(`n.kind = ${f.kind === 'note' ? 1 : 30023}`);
  for (const w of short) { cond.push("n.content LIKE ? ESCAPE '\\'"); args.push(likeArg(w)); }
  let direct = '';
  if (f.author) {
    const c = d.prepare('SELECT count(*) c FROM notes WHERE pubkey = unhex(?)').get(f.author).c;
    if (c <= DIRECT_MAX) direct = 'notes_author';
  }
  if (!direct && f.kind === 'article' && hasIndex(d, 'notes_kind')) direct = 'notes_kind';
  if (direct) {
    const likes = long.map(() => "n.content LIKE ? ESCAPE '\\'");
    return d.prepare(`SELECT n.rowid r, lower(hex(n.id)) id, lower(hex(n.pubkey)) pk FROM notes n INDEXED BY ${direct}
      WHERE n.rowid BETWEEN ? AND ? AND ${[...cond, ...likes].join(' AND ')}
      ORDER BY ${direct === 'notes_author' ? 'n.created_at DESC, n.rowid DESC' : 'n.kind DESC, n.rowid DESC'} LIMIT ${PAGE * 2}`)
      .all(lo, hi, ...args, ...long.map(likeArg));
  }
  return d.prepare(`SELECT f.rowid r, lower(hex(n.id)) id, lower(hex(n.pubkey)) pk FROM notes_fts f JOIN notes n ON n.rowid = f.rowid
      WHERE notes_fts MATCH ? AND f.rowid BETWEEN ? AND ? AND ${cond.join(' AND ')}
      ORDER BY f.rowid DESC LIMIT ${PAGE * 2}`).all(match, lo, hi, ...args);
}
const hasIndex = (d, name) => cached('ix:' + name + dbIno, 600_000, () => !!d.prepare("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?").get(name));   // search-index.py: rowid = created_at << 20 | id bits

async function searchPage(q, sp) {
  q = q.trim().slice(0, 100);
  if (!q) return notFound('Empty query');
  const d = sdb();
  if (!d) return notFound('Search index is not ready yet');
  const f = parseFilters(sp), cursor = +(sp.get('cursor') || 0) || 0;
  const head = `<p class="hint">Results for <b>${esc(q)}</b>`;
  const title = `"${q}" · search · ${RELAY_NAME}`;
  const fail = msg => page({ title, robots: 'noindex,follow', q, status: 404, body: filterForm(q, f) + `<div class="card"><p class="hint">${msg}</p></div>` });
  if (f.badAuthor) return fail('Author must be an npub1… / nprofile1… or a 64-character hex pubkey.');
  if (f.since && f.until && f.since > f.until) return fail('"From" is after "To".');
  const { long, short } = ftsQuery(q);
  if (!long.length) return fail('Use at least one word of 3 or more characters.');
  const match = long.map(w => '"' + w.replace(/"/g, '""') + '"').join(' ');
  // FTS rowids are time-ordered, so the date range is a rowid range and "newest first" needs no sort
  const lo = f.since ? f.since * ROWID_SHIFT : 0;
  let hi = f.until ? (f.until + 1) * ROWID_SHIFT - 1 : Number.MAX_SAFE_INTEGER;
  if (cursor) hi = Math.min(hi, cursor - 1);
  const t0 = Date.now();
  let rows;
  try {
    rows = cached('sq:' + JSON.stringify([match, short, lo, hi, f.author, f.kind]), 60_000, () => searchRows(d, match, long, short, lo, hi, f));
  } catch (err) { console.error(new Date().toISOString(), 'search', err.message); return fail('Bad query.'); }
  const ban = bans();
  rows = rows.filter(r => !ban.has(r.pk));
  const ids = rows.slice(0, PAGE + 10).map(r => r.id);
  const evs = ids.length ? await req({ ids }) : [];
  const byId = Object.fromEntries(evs.map(e => [e.id, e]));
  const shown = rows.slice(0, PAGE + 10).filter(r => byId[r.id] && visible(byId[r.id])).slice(0, PAGE);
  const notes = shown.map(r => byId[r.id]);
  // people whose names match (only on the first, unfiltered-by-author page)
  const profRows = cursor || f.author ? [] : d.prepare(`SELECT lower(hex(p.pubkey)) pk FROM profiles_fts f JOIN profiles p ON p.rowid = f.rowid
      WHERE profiles_fts MATCH ? LIMIT 12`).all(match).filter(r => !ban.has(r.pk));
  const profs = await profilesOf([...notes.map(e => e.pubkey), ...profRows.map(r => r.pk), ...(f.author ? [f.author] : [])]);
  const people = profRows.map(r => r.pk).filter(pk => profs[pk].ev && !nsfw(profs[pk].ev)).slice(0, 8);
  const ms = Date.now() - t0;
  const active = [f.since && 'from ' + ymd(f.since), f.until && 'to ' + ymd(f.until),
    f.author && 'by ' + esc(nameOf(f.author, profs[f.author].p)), f.kind && (f.kind === 'note' ? 'notes only' : 'articles only')].filter(Boolean).join(' · ');
  if (!notes.length && !people.length) return fail(`No results for <b>${esc(q)}</b>${active ? ' (' + active + ')' : ''}.`);
  const more = rows.length >= PAGE && shown.length ? `<div class="pager"><span></span><a href="/search/${encodeURIComponent(q)}${filterQuery(f, { cursor: shown[shown.length - 1].r })}" rel="nofollow">older →</a></div>` : '';
  const body = `${filterForm(q, f)}${head}${active ? ' · ' + active : ''} · newest first · ${ms} ms</p>
${people.length ? `<div class="card"><div class="hint" style="margin-bottom:6px">People</div>${people.map(pk => whoHtml(pk, profs[pk])).join('')}</div>` : ''}
${notes.map(e => noteCard(e, profs[e.pubkey], q)).join('')}${more}`;
  return page({ title, desc: `Nostr notes matching "${q}" on ${RELAY_NAME}`, robots: 'noindex,follow', q, body });
}

function robots() {
  return [200, `User-agent: *\nAllow: /\nDisallow: /search\nDisallow: /*?until=\nSitemap: ${BASE}/sitemap.xml\n`, 'text/plain; charset=utf-8'];
}
function sitemap(n) {
  const d = sdb();
  if (!d) return [404, 'not ready', 'text/plain'];
  const ban = [...bans()];
  // notes and articles of the last 365 days, newest first, then authors
  const total = d.prepare(`SELECT count(*) c FROM notes WHERE nsfw = 0 AND created_at > ?`).get(Math.floor(Date.now() / 1000) - 365 * 86400).c;
  const pages = Math.max(1, Math.ceil(total / SITEMAP_PAGE));
  if (n === null) {
    const xs = Array.from({ length: pages }, (_, i) => `<sitemap><loc>${BASE}/sitemap-${i}.xml</loc></sitemap>`).join('')
      + `<sitemap><loc>${BASE}/sitemap-profiles.xml</loc></sitemap>`;
    return [200, `<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${xs}</sitemapindex>`, 'application/xml'];
  }
  let urls;
  if (n === 'profiles') {
    urls = d.prepare(`SELECT lower(hex(pubkey)) pk, max(created_at) ca FROM notes WHERE nsfw = 0 AND created_at > ? GROUP BY pubkey ORDER BY ca DESC LIMIT ${SITEMAP_PAGE}`)
      .all(Math.floor(Date.now() / 1000) - 365 * 86400).filter(r => !ban.includes(r.pk))
      .map(r => `<url><loc>${BASE}/${npubOf(r.pk)}</loc><lastmod>${isoTime(r.ca).slice(0, 10)}</lastmod></url>`);
  } else {
    if (!(n >= 0 && n < pages)) return [404, 'not found', 'text/plain'];
    const banSet = new Set(ban);
    urls = d.prepare(`SELECT lower(hex(id)) id, lower(hex(pubkey)) pk, created_at ca FROM notes WHERE nsfw = 0 AND created_at > ? ORDER BY created_at DESC LIMIT ${SITEMAP_PAGE} OFFSET ${n * SITEMAP_PAGE}`)
      .all(Math.floor(Date.now() / 1000) - 365 * 86400).filter(r => !banSet.has(r.pk))
      .map(r => `<url><loc>${BASE}/${noteOf(r.id)}</loc><lastmod>${isoTime(r.ca).slice(0, 10)}</lastmod></url>`);
  }
  return [200, `<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls.join('')}</urlset>`, 'application/xml'];
}

// ---------- rate limit (per client IP from Cloudflare) ----------
const hits = new Map();
function limited(ip, cost) {
  const now = Date.now(), b = hits.get(ip) || { t: now, n: 0 };
  if (now - b.t > 60_000) { b.t = now; b.n = 0; }
  b.n += cost; hits.set(ip, b);
  if (hits.size > 20000) for (const [k, v] of hits) if (now - v.t > 60_000) hits.delete(k);
  return b.n > 120;            // 120 cost units per minute: a page costs 1, a search 4 (so 30 searches)
}

async function route(u) {
  const p = decodeURIComponent(u.pathname);
  if (p === '/robots.txt') return robots();
  if (p === '/sitemap.xml') return cached('sm', 3600_000, () => sitemap(null));
  let m;
  if ((m = p.match(/^\/sitemap-(\d+|profiles)\.xml$/))) return cached('sm' + m[1], 3600_000, () => sitemap(m[1] === 'profiles' ? 'profiles' : +m[1]));
  if (p === '/search' || p === '/search/') {
    const q = (u.searchParams.get('q') || '').trim();
    const rest = new URLSearchParams([...u.searchParams].filter(([k, v]) => k !== 'q' && v !== ''));
    return q ? [301, '', '', `/search/${encodeURIComponent(q)}${rest.size ? '?' + rest : ''}`] : notFound('Empty query');
  }
  if ((m = p.match(/^\/search\/(.+)$/))) return searchPage(m[1], u.searchParams);
  if ((m = p.match(/^\/npub\/([0-9a-f]{64})$/))) return [301, '', '', '/' + npubOf(m[1])];
  if ((m = p.match(/^\/event\/([0-9a-f]{64})$/))) return [301, '', '', '/' + noteOf(m[1])];
  if ((m = p.match(/^\/(npub1[02-9ac-hj-np-z]{58})$/))) {
    let pk; try { pk = nip19.decode(m[1]).data; } catch { return notFound(); }
    return profilePage(pk, +(u.searchParams.get('until') || 0) || 0);
  }
  if ((m = p.match(/^\/(note1[02-9ac-hj-np-z]{58})$/))) {
    let id; try { id = nip19.decode(m[1]).data; } catch { return notFound(); }
    return notePage(id);
  }
  return notFound();
}

http.createServer(async (rq, rs) => {
  const ip = rq.headers['cf-connecting-ip'] || rq.socket.remoteAddress;
  let u;
  try { u = new URL(rq.url, BASE); } catch { rs.writeHead(400).end(); return; }
  if (rq.method !== 'GET' && rq.method !== 'HEAD') { rs.writeHead(405).end(); return; }
  if (limited(ip, u.pathname.startsWith('/search') ? 4 : 1)) {
    rs.writeHead(429, { 'content-type': 'text/plain', 'retry-after': '60' }).end('Too many requests'); return;
  }
  let out;
  try { out = await route(u); } catch (err) { console.error(new Date().toISOString(), u.pathname, err); out = [503, 'temporarily unavailable', 'text/plain']; }
  const [status, body, type = 'text/html; charset=utf-8', loc] = out;
  const h = { 'content-type': type, 'x-content-type-options': 'nosniff' };
  if (loc) h.location = loc;
  if (status === 200) h['cache-control'] = u.pathname.startsWith('/search') ? 'public, max-age=60' : 'public, max-age=300';
  rs.writeHead(status, h).end(rq.method === 'HEAD' ? undefined : body);
}).listen(PORT, '127.0.0.1', () => console.log(`relay-stats web on 127.0.0.1:${PORT} -> ${RELAY_WS}, index ${SEARCH_DB}`));
