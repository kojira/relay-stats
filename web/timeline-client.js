// relay-stats web: live timeline (kind 1) read straight from the relay over WebSocket.
// Newest first; new posts are inserted at the top as they arrive (the view stays put when scrolled down);
// older posts load when the end comes near (infinite scroll). Only the cards near the viewport exist in the
// DOM (virtual scroll; heights are measured with ResizeObserver), and at most CAP events are kept.
// Hidden as on the other pages: banned pubkeys, NSFW posts, authors whose profile is NSFW, deleted posts.
(() => {
  const CFG = JSON.parse(document.getElementById('tl-cfg').textContent);
  const HIDDEN = new Set(CFG.hidden || []);
  const enc = new TextEncoder();
  const hiddenPk = async pk => HIDDEN.has([...new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(CFG.salt + pk)))].slice(0, 8).map(b => b.toString(16).padStart(2, '0')).join(''));
  const CAP = 20000, OLDER_PAGE = 100, FIRST_PAGE = 60, OVERSCAN = 1500, EST = 170;
  const $list = document.getElementById('tl'), $status = document.getElementById('tl-status');
  const $bridges = document.getElementById('tl-bridges'), $menu = document.getElementById('tl-menu'), $dlg = document.getElementById('tl-json');

  // ---------- helpers ----------
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const safeUrl = u => /^https?:\/\//i.test(u || '') ? String(u) : '';
  const nsfw = e => (e.tags || []).some(t => t[0] === 'content-warning' || ((t[0] === 't' || t[0] === 'l') && String(t[1] || '').toLowerCase() === 'nsfw'))
    || /(^|[\s#＃])#nsfw\b|＃nsfw\b/i.test(e.content || '');
  const CH = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l';
  function bech32(hrp, hex) {
    const G = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
    const poly = v => { let c = 1; for (const x of v) { const b = c >>> 25; c = ((c & 0x1ffffff) << 5) ^ x; for (let i = 0; i < 5; i++) if ((b >>> i) & 1) c ^= G[i]; } return c >>> 0; };
    const data = []; let acc = 0, bits = 0;
    for (const h of hex.match(/../g)) { acc = ((acc << 8) | parseInt(h, 16)) & 0xffff; bits += 8; while (bits >= 5) { bits -= 5; data.push((acc >>> bits) & 31); } }
    if (bits) data.push((acc << (5 - bits)) & 31);
    const exp = [...hrp].map(c => c.charCodeAt(0) >> 5).concat([0], [...hrp].map(c => c.charCodeAt(0) & 31));
    const pm = (poly([...exp, ...data, 0, 0, 0, 0, 0, 0]) ^ 1) >>> 0;
    const cs = [0, 1, 2, 3, 4, 5].map(i => Math.floor(pm / 2 ** (5 * (5 - i))) & 31);
    return hrp + '1' + [...data, ...cs].map(d => CH[d]).join('');
  }
  const npub = pk => bech32('npub', pk), note = id => bech32('note', id);
  const fmt = ts => new Date(ts * 1000).toLocaleString('sv-SE', { timeZone: 'Asia/Tokyo' }).slice(5, 16).replace('-', '/');
  const IMG = /\.(png|jpe?g|gif|webp|avif)(\?\S*)?$/i, VID = /\.(mp4|webm|mov)(\?\S*)?$/i;
  const SPLIT = /(https?:\/\/[^\s<>"'）」】]+|nostr:(?:npub1|note1|nprofile1|nevent1|naddr1)[0-9a-z]+|#[^\s#＃.,!?、。:;()[\]{}"'<>]+)/gu;
  function rich(e) {
    const emo = {};
    for (const t of e.tags || []) if (t[0] === 'emoji' && /^[\w+-]+$/.test(t[1] || '') && safeUrl(t[2])) emo[t[1]] = t[2];
    const text = s => esc(s).replace(/:([\w+-]+):/g, (m, n) => emo[n] ? `<img class="emo" src="${esc(emo[n])}" alt=":${esc(n)}:" title=":${esc(n)}:" loading="lazy">` : m);
    let html = '';
    const media = [];
    for (const s of String(e.content).split(SPLIT)) {
      if (!s) continue;
      if (/^https?:\/\//i.test(s)) {
        if (IMG.test(s)) media.push(`<a href="${esc(s)}" rel="nofollow noopener" target="_blank"><img src="${esc(s)}" loading="lazy" alt=""></a>`);
        else if (VID.test(s)) media.push(`<video src="${esc(s)}" controls preload="none"></video>`);
        html += `<a href="${esc(s)}" rel="nofollow noopener ugc" target="_blank">${esc(s.length > 60 ? s.slice(0, 57) + '…' : s)}</a>`;
      } else if (s.startsWith('nostr:')) {
        const b = s.slice(6);
        const href = /^(npub1|note1)/.test(b) ? '/' + b : 'https://njump.me/' + b;
        html += `<a href="${esc(href)}">${esc(b.slice(0, 18))}…</a>`;
      } else if (s[0] === '#' && s.length > 1) html += `<a href="/search/${encodeURIComponent(s)}">${esc(s)}</a>`;
      else html += text(s);
    }
    return html.replace(/\n/g, '<br>') + (media.length ? `<div class="media">${media.join('')}</div>` : '');
  }

  // ---------- state ----------
  let all = [];                          // newest first
  const byId = new Map();
  let view = [], offsets = [], total = 0, viewIndex = new Map();
  const heights = new Map(), rendered = new Map();
  const profiles = new Map();            // pk -> { p, nsfw } once known
  const wantProfile = new Set();
  let showBridges = localStorage.getItem('tl-bridges') !== '0';
  let olderUntil = 0, olderLoading = false, olderDone = false, frozenTop = false, missed = 0, live = false;
  $bridges.checked = showBridges;

  const bridged = e => e._b ??= (e.tags || []).some(t => t[0] === 'proxy');
  const ready = e => { const p = profiles.get(e.pubkey); return !!p && !p.nsfw && !p.banned; };

  // ---------- relay ----------
  let ws = null, subSeq = 0, backoff = 1000, liveId = '';
  const subs = new Map();
  function send(m) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(m)); }
  function req(filters, onEvent, onDone, keep = false) {
    const id = 's' + (++subSeq);
    subs.set(id, { onEvent, onDone, keep });
    send(['REQ', id, ...filters]);
    return id;
  }
  function connect() {
    setStatus('connecting…');
    ws = new WebSocket(CFG.relay);
    ws.onopen = () => {
      backoff = 1000;
      const newest = all[0];
      liveId = req([newest ? { kinds: [1], since: newest.created_at - 30 } : { kinds: [1], limit: FIRST_PAGE }, { kinds: [5], since: Math.floor(Date.now() / 1000) }],
        addEvent, () => { live = true; setStatus(); schedule(); }, true);
      if (wantProfile.size) flushProfiles();
    };
    ws.onmessage = m => {
      let d; try { d = JSON.parse(m.data); } catch { return; }
      const s = subs.get(d[1]);
      if (!s) return;
      if (d[0] === 'EVENT') s.onEvent(d[2]);
      else if (d[0] === 'EOSE' || d[0] === 'CLOSED') {
        if (!s.keep || d[0] === 'CLOSED') { subs.delete(d[1]); if (d[0] === 'EOSE') send(['CLOSE', d[1]]); }
        s.onDone && s.onDone();
      }
    };
    ws.onclose = () => {
      live = false; olderLoading = false; profLoading = false; subs.clear();
      setStatus('reconnecting…');
      setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 30000);
    };
  }

  // ---------- events ----------
  function addEvent(e) {
    if (!e || typeof e.id !== 'string') return;
    if (e.kind === 5) {
      for (const t of e.tags || []) if (t[0] === 'e' && byId.has(t[1]) && byId.get(t[1]).pubkey === e.pubkey) removeEvent(t[1]);
      return;
    }
    if (e.kind !== 1 || byId.has(e.id) || nsfw(e) || profiles.get(e.pubkey)?.banned) return;
    if (frozenTop && all.length && e.created_at > all[0].created_at) { missed++; setStatus(); return; }
    let lo = 0, hi = all.length;               // keep newest first (ties: id) with a binary search
    while (lo < hi) { const m = (lo + hi) >> 1; const a = all[m]; if (a.created_at > e.created_at || (a.created_at === e.created_at && a.id < e.id)) lo = m + 1; else hi = m; }
    all.splice(lo, 0, e); byId.set(e.id, e);
    if (!profiles.has(e.pubkey)) { wantProfile.add(e.pubkey); queueProfiles(); }
    trim(); schedule();
  }
  function removeEvent(id) {
    const i = all.findIndex(e => e.id === id);
    if (i >= 0) all.splice(i, 1);
    byId.delete(id); heights.delete(id); schedule();
  }
  function trim() {
    if (all.length <= CAP) return;
    const extra = all.length - CAP;
    const nearTop = relTop() < total / 2;
    const gone = nearTop ? all.splice(all.length - extra, extra) : all.splice(0, extra);
    for (const e of gone) { byId.delete(e.id); heights.delete(e.id); }
    if (nearTop) { olderDone = false; olderUntil = all[all.length - 1].created_at; }
    else frozenTop = true;                     // newest were dropped: stop inserting at the top until reload
  }
  function loadOlder() {
    if (olderLoading || olderDone || !live || !all.length || !ws || ws.readyState !== 1) return;
    olderLoading = true;
    const until = olderUntil || all[all.length - 1].created_at;
    let got = 0, fresh = 0, min = until;
    req([{ kinds: [1], until, limit: OLDER_PAGE }], e => { got++; if (e.created_at < min) min = e.created_at; if (!byId.has(e.id)) fresh++; addEvent(e); }, () => {
      olderLoading = false;
      if (!got) { olderDone = true; setStatus(); return; }
      olderUntil = fresh ? min : min - 1;
      schedule();
    });
  }

  // ---------- profiles (batched) ----------
  let profTimer = 0, profLoading = false;
  function queueProfiles() { if (!profTimer) profTimer = setTimeout(() => { profTimer = 0; flushProfiles(); }, 150); }
  function flushProfiles() {
    if (profLoading || !wantProfile.size || !ws || ws.readyState !== 1) return;
    profLoading = true;
    const pks = [...wantProfile].slice(0, 150);
    pks.forEach(pk => wantProfile.delete(pk));
    const best = {};
    req([{ kinds: [0], authors: pks }], e => { if (!best[e.pubkey] || best[e.pubkey].created_at < e.created_at) best[e.pubkey] = e; }, async () => {
      const ban = await Promise.all(pks.map(hiddenPk));
      pks.forEach((pk, i) => {
        const e = best[pk]; let p = {};
        try { p = e ? JSON.parse(e.content) : {}; } catch {}
        profiles.set(pk, { p: p && typeof p === 'object' ? p : {}, nsfw: !!e && nsfw(e), banned: ban[i] });
        if (ban[i]) for (const x of all.filter(x => x.pubkey === pk)) removeEvent(x.id);
      });
      profLoading = false; schedule();
      if (wantProfile.size) flushProfiles();
    });
  }

  // ---------- virtual list ----------
  const listTop = () => $list.getBoundingClientRect().top + window.scrollY;
  const relTop = () => window.scrollY - listTop();
  let scheduled = false;
  function schedule() { if (!scheduled) { scheduled = true; requestAnimationFrame(update); } }
  function update() {
    scheduled = false;
    // keep the first visible card where it is on screen (new posts above it push the page, not the view)
    const top = relTop();
    let anchor = null;
    if (top > 0 && view.length) {
      const i = indexAt(top);
      if (view[i]) anchor = { id: view[i].id, delta: offsets[i] - top };
    }
    view = all.filter(e => ready(e) && (showBridges || !bridged(e)));
    offsets = new Array(view.length); viewIndex = new Map(); total = 0;
    for (let i = 0; i < view.length; i++) { offsets[i] = total; viewIndex.set(view[i].id, i); total += heights.get(view[i].id) || EST; }
    $list.style.height = total + 'px';
    if (anchor && viewIndex.has(anchor.id)) {
      const want = listTop() + offsets[viewIndex.get(anchor.id)] - anchor.delta;
      if (Math.abs(want - window.scrollY) > 0.5) window.scrollTo(0, want);
    }
    render(); setStatus();
  }
  function indexAt(y) {             // first index whose bottom is below y
    let lo = 0, hi = view.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; if (offsets[m] + (heights.get(view[m].id) || EST) <= y) lo = m + 1; else hi = m; }
    return Math.max(0, lo);
  }
  function render() {
    const top = relTop(), vh = window.innerHeight;
    const keep = new Set();
    if (view.length) {
      for (let i = indexAt(top - OVERSCAN); i < view.length && offsets[i] < top + vh + OVERSCAN; i++) {
        const e = view[i];
        keep.add(e.id);
        let el = rendered.get(e.id);
        if (!el) { el = card(e); rendered.set(e.id, el); $list.appendChild(el); ro.observe(el); }
        el.style.transform = `translateY(${offsets[i]}px)`;
      }
    }
    for (const [id, el] of rendered) if (!keep.has(id)) { ro.unobserve(el); el.remove(); rendered.delete(id); }
    if (top + vh + OVERSCAN * 2 > total) loadOlder();
  }
  const ro = new ResizeObserver(entries => {
    let changed = false;
    for (const en of entries) {
      const id = en.target.dataset.id, h = en.target.offsetHeight;
      if (h && heights.get(id) !== h) { heights.set(id, h); changed = true; }
    }
    if (changed) schedule();
  });
  let scrollRaf = 0;
  window.addEventListener('scroll', () => { if (!scrollRaf) scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; render(); }); }, { passive: true });
  window.addEventListener('resize', schedule);

  function card(e) {
    const pr = profiles.get(e.pubkey) || { p: {} }, p = pr.p;
    const name = String(p.display_name || p.displayName || p.name || '').slice(0, 80) || npub(e.pubkey).slice(0, 16) + '…';
    const pic = safeUrl(String(p.picture || ''));
    const reply = (e.tags || []).some(t => t[0] === 'e');
    const el = document.createElement('div');
    el.className = 'tl-item'; el.dataset.id = e.id;
    el.innerHTML = `<article class="card">
<div class="who">${pic ? `<img src="${esc(pic)}" alt="" loading="lazy">` : '<img alt="">'}<div style="min-width:0;flex:1"><a class="n" href="/${npub(e.pubkey)}">${esc(name)}</a>
<div class="s">${p.nip05 ? esc(String(p.nip05).slice(0, 80)) : ''}</div></div><button class="tl-more" aria-label="menu" title="menu">⋯</button></div>
<div class="body">${rich(e)}</div>
<div class="meta"><a href="/${note(e.id)}"><time datetime="${new Date(e.created_at * 1000).toISOString()}">${fmt(e.created_at)}</time></a>${reply ? '<span>reply</span>' : ''}${bridged(e) ? '<span>bridged</span>' : ''}</div></article>`;
    return el;
  }

  // ---------- per-post menu ----------
  let menuFor = null;
  $list.addEventListener('click', ev => {
    const b = ev.target.closest('.tl-more');
    if (!b) return;
    ev.preventDefault(); ev.stopPropagation();
    const id = b.closest('.tl-item').dataset.id;
    if (menuFor === id && !$menu.hidden) { closeMenu(); return; }
    menuFor = id;
    const r = b.getBoundingClientRect();
    $menu.hidden = false;
    $menu.style.top = (r.bottom + window.scrollY + 4) + 'px';
    $menu.style.left = Math.max(8, r.right + window.scrollX - $menu.offsetWidth) + 'px';
  });
  const closeMenu = () => { $menu.hidden = true; menuFor = null; };
  document.addEventListener('click', ev => { if (!$menu.hidden && !$menu.contains(ev.target)) closeMenu(); });
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape') closeMenu(); });
  const copy = (txt, btn) => navigator.clipboard.writeText(txt).then(() => { const o = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => btn.textContent = o, 900); }, () => {});
  $menu.addEventListener('click', ev => {
    const b = ev.target.closest('button[data-act]');
    const e = menuFor && byId.get(menuFor);
    if (!b || !e) return;
    const strip = ({ _b, ...rest }) => rest;
    const act = b.dataset.act;
    if (act === 'json') {
      $dlg.querySelector('pre').textContent = JSON.stringify(strip(e), null, 2);
      closeMenu(); $dlg.showModal();
    } else if (act === 'note') { copy(note(e.id), b); }
    else if (act === 'npub') { copy(npub(e.pubkey), b); }
    else if (act === 'raw') { copy(JSON.stringify(strip(e)), b); }
    else if (act === 'open') { closeMenu(); location.href = '/' + note(e.id); }
  });
  $dlg.querySelector('[data-act=copyjson]').addEventListener('click', ev => copy($dlg.querySelector('pre').textContent, ev.target));
  $dlg.querySelector('[data-act=close]').addEventListener('click', () => $dlg.close());
  $dlg.addEventListener('click', ev => { if (ev.target === $dlg) $dlg.close(); });

  // ---------- controls ----------
  $bridges.addEventListener('change', () => { showBridges = $bridges.checked; localStorage.setItem('tl-bridges', showBridges ? '1' : '0'); schedule(); });
  function setStatus(msg) {
    if (msg) { $status.textContent = msg; return; }
    const parts = [live ? '● live' : '○ offline', view.length + ' posts'];
    if (olderLoading) parts.push('loading older…');
    else if (olderDone) parts.push('no older posts');
    $status.innerHTML = esc(parts.join(' · ')) + (frozenTop ? ` · <a href="/timeline">${missed} newer — reload</a>` : '');
  }
  connect();
})();
