"use strict";
/* まんがビューワー クライアント */
const $app = document.getElementById("app");
const enc = p => p.split("/").map(encodeURIComponent).join("/");
const dec = h => h.split("/").map(decodeURIComponent).join("/");
const q = encodeURIComponent;
const baseName = p => p.split("/").pop();
const stripExt = n => n.replace(/\.[^./]+$/, "");

/* ---------- 端末ごとの表示設定 (localStorage) ---------- */
const PREF_DEF = {
  mode: "auto",        // auto | double | single
  binding: "rtl",      // rtl(右開き) | ltr(左開き)
  coverAlone: true,    // 表紙は単独表示
  wideAlone: true,     // 横長ページは単独表示
  clickSwap: false,    // クリック/タップ方向を反転
  fit: "contain",      // contain | width
  quality: "auto",     // auto(画面に合わせる) | light | orig
  preload: 4,
  bg: "#111111",
  tile: 150,
  wheel: true,
  remember: true,
  view: "list",        // list(サムネ+タイトル) | grid
  sort: "name_asc",
  resume: true,        // 起動時に前回の本の続きから開く
  autoFull: true,      // タッチ端末では最初のタップで自動的に全画面にする
};
let prefs = Object.assign({}, PREF_DEF);
try { Object.assign(prefs, JSON.parse(localStorage.getItem("mv_prefs") || "{}")); } catch (e) {}
const savePrefs = () => { try { localStorage.setItem("mv_prefs", JSON.stringify(prefs)); } catch (e) {} };

/* ---------- 共通 ---------- */
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) for (const k in props) {
    const v = props[k];
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}

async function API(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  if (r.status === 401) { showLogin(); throw new Error("login"); }
  let j = null;
  try { j = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
  return j;
}

function showLogin() {
  leaveReader();
  const pw = h("input", { type: "password", placeholder: "パスワード", autofocus: true });
  const msg = h("div", { class: "err" });
  const go = async () => {
    try {
      await API("/api/login", { password: pw.value });
      route();
    } catch (e) { if (e.message !== "login") msg.textContent = e.message; }
  };
  pw.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  $app.replaceChildren(h("div", { class: "login" }, h("h2", { style: "margin:0;color:#fff;font-size:18px" }, "📚 まんがビューワー"), pw,
    h("button", { class: "btn pri", onclick: go }, "ログイン"), msg));
}

/* ---------- ルーター ---------- */
function route() {
  const hash = location.hash || "#/b/";
  const m = hash.match(/^#\/(\w)(?:\/(.*))?$/) || [];
  const kind = m[1] || "b", arg = m[2] || "";
  if (kind !== "s") { try { sessionStorage.setItem("mv_last", hash); } catch (e) {} }
  if (kind !== "r") leaveReader();
  document.documentElement.style.setProperty("--tile", prefs.tile + "px");
  if (kind === "r") openReader(dec(arg));
  else if (kind === "s") showSettings();
  else if (kind === "w") showWizard();
  else if (kind === "q") showSearch(decodeURIComponent(arg));
  else showBrowse(dec(arg));
}
window.addEventListener("hashchange", route);

/* ---------- ライブラリ ---------- */
function topbar(title, extra) {
  const si = h("input", { type: "text", placeholder: "検索…", enterkeyhint: "search" });
  si.addEventListener("keydown", e => { if (e.key === "Enter" && si.value.trim()) location.hash = "#/q/" + q(si.value.trim()); });
  return h("div", { class: "topbar" },
    h("a", { href: "#/b/", style: "font-weight:700;color:#fff;white-space:nowrap" }, "📚 まんが"),
    h("div", { class: "grow" }, si), extra,
    h("button", { class: "btn sm", title: "設定", onclick: () => { location.hash = "#/s"; } }, "⚙ 設定"));
}

function crumbs(path) {
  const parts = path ? path.split("/") : [];
  const out = [h("a", { href: "#/b/" }, "ホーム")];
  let acc = "";
  parts.forEach((p, i) => {
    acc += (acc ? "/" : "") + p;
    out.push(" / ", i === parts.length - 1 ? h("span", null, p) : h("a", { href: "#/b/" + enc(acc) }, p));
  });
  return h("div", { class: "crumbs" }, out);
}

const collator = new Intl.Collator("ja", { numeric: true, sensitivity: "base" });
const SORTS = [["name_asc", "名前 昇順"], ["name_desc", "名前 降順"], ["mtime_desc", "更新日 新しい順"], ["mtime_asc", "更新日 古い順"],
  ["read_desc", "最近読んだ順"], ["size_desc", "サイズ 大きい順"], ["size_asc", "サイズ 小さい順"]];
function cmpFor(key, isDir) {
  const nm = (a, b) => collator.compare(a.name, b.name);
  switch (key) {
    case "name_desc": return (a, b) => collator.compare(b.name, a.name);
    case "mtime_desc": return (a, b) => (b.mtime || 0) - (a.mtime || 0) || nm(a, b);
    case "mtime_asc": return (a, b) => (a.mtime || 0) - (b.mtime || 0) || nm(a, b);
    case "read_desc": return isDir ? nm : (a, b) => (b.ts || 0) - (a.ts || 0) || nm(a, b);
    case "size_desc": return isDir ? nm : (a, b) => (b.size || 0) - (a.size || 0) || nm(a, b);
    case "size_asc": return isDir ? nm : (a, b) => (a.size || 0) - (b.size || 0) || nm(a, b);
    default: return nm;
  }
}
const fmtSize = n => n >= 1073741824 ? (n / 1073741824).toFixed(1) + " GB" : (n / 1048576).toFixed(n >= 104857600 ? 0 : 1) + " MB";
const fmtDate = t => { const d = new Date(t * 1000); return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`; };

function coverBox(path, o) {
  const cover = h("div", { class: "cover" }, h("div", { class: "ph" }, o.dir ? "📁" : "📖"));
  const img = h("img", { loading: "lazy", decoding: "async", src: "/api/cover?b=" + q(path), alt: "" });
  img.addEventListener("load", () => cover.querySelector(".ph") && cover.querySelector(".ph").remove());
  img.addEventListener("error", () => img.remove());
  cover.prepend(img);
  if (o.dir) cover.append(h("div", { class: "badge" }, "📁"));
  if (!o.dir && o.t && o.p != null) {
    const last = o.l != null ? o.l : o.p;
    cover.append(h("div", { class: "bar" }, h("i", { style: `width:${Math.min(100, Math.round((last + 1) / o.t * 100))}%` })));
    if (last >= o.t - 1) cover.append(h("div", { class: "badge" }, "読了"));
  }
  return cover;
}

/* e = {dir, path, name, size, mtime, p, l, t, ts, bm, sub} */
function entryEl(e) {
  const hash = (e.dir ? "#/b/" : "#/r/") + enc(e.path);
  const go = () => { location.hash = hash; };
  const title = e.dir ? e.name : stripExt(e.name);
  if (prefs.view === "grid") return h("div", { class: "tile", onclick: go }, coverBox(e.path, e), h("div", { class: "nm" }, title));
  const info = [];
  if (e.sub) info.push(e.sub);
  if (!e.dir && e.size) info.push(fmtSize(e.size));
  if (e.mtime) info.push(fmtDate(e.mtime));
  if (!e.dir && e.t && e.p != null) { const last = e.l != null ? e.l : e.p; info.push(last >= e.t - 1 ? "読了" : `${last + 1} / ${e.t} ページ`); }
  if (e.bm) info.push("🔖" + e.bm);
  return h("div", { class: "item", onclick: go }, h("div", { class: "thumb" }, coverBox(e.path, e)),
    h("div", { class: "meta" }, h("div", { class: "ttl" }, (e.dir ? "📁 " : "") + title), h("div", { class: "sub" }, info.join("  ・  "))));
}
const listWrap = items => h("div", { class: prefs.view === "grid" ? "grid" : "list" }, items.map(entryEl));

function controlsBar(onChange, showSort) {
  const sel = h("select", { class: "sortsel" }, SORTS.map(([v, t]) => h("option", { value: v, selected: prefs.sort === v }, t)));
  sel.addEventListener("change", () => { prefs.sort = sel.value; savePrefs(); onChange(); });
  const vb = (v, label) => h("button", { class: "btn sm" + (prefs.view === v ? " pri" : ""), onclick: () => { prefs.view = v; savePrefs(); onChange(); } }, label);
  return h("div", { class: "ctrl" }, showSort ? sel : null, h("div", { class: "grow" }), vb("list", "☰ リスト"), vb("grid", "▦ 並べる"));
}

async function showBrowse(path) {
  $app.replaceChildren(topbar(), h("div", { class: "msg" }, "読み込み中…"));
  let data, rec = null;
  try {
    const st = await (await fetch("/api/status")).json();
    if (!st.configured) { location.replace("#/w"); return; }
    data = await API("/api/list?path=" + q(path));
    if (!path && prefs.remember) { try { rec = await API("/api/recent"); } catch (e) {} }
  } catch (e) {
    if (e.message === "login") return;
    $app.replaceChildren(topbar(), h("div", { class: "msg err" }, e.message), h("div", { class: "msg" }, h("button", { class: "btn pri", onclick: () => { location.hash = "#/w"; } }, "🔍 ネットワークからマンガの場所を選び直す"), " ", h("a", { href: "#/s" }, "⚙ 設定")));
    return;
  }
  if (path && data.images > 0 && !data.dirs.length && !data.books.length) { location.replace("#/r/" + enc(path)); return; }   // 画像だけのフォルダ=1冊として開く
  const pre = path ? path + "/" : "";
  const dirs = data.dirs.map(d => ({ dir: true, path: pre + d, name: d, mtime: (data.dmt || {})[d] }));
  const books = data.books.map(b => ({ path: pre + b.name, name: b.name, size: b.size, mtime: b.mtime, p: b.p, l: b.l, t: b.t, ts: b.ts, bm: b.bm }));
  const body = h("div");
  const render = () => {
    const wrap = h("div", { class: "wrap" });
    if (rec && rec.items.length) {
      wrap.append(h("h2", null, "続きから読む"), h("div", { class: "hscroll" }, rec.items.map(r => {
        const t = h("div", { class: "tile", onclick: () => { location.hash = "#/r/" + enc(r.path); } }, coverBox(r.path, r), h("div", { class: "nm" }, r.name));
        return t;
      })));
    }
    if (data.images > 0) wrap.append(h("h2", null, "このフォルダは画像フォルダです"), h("div", { class: "folders" },
      h("div", { class: "folder", onclick: () => { location.hash = "#/r/" + enc(path); } }, h("span", null, "📖"), h("span", null, `このフォルダを読む(画像 ${data.images} 枚)`))));
    if (dirs.length) wrap.append(h("h2", null, `フォルダ (${dirs.length})`), listWrap([...dirs].sort(cmpFor(prefs.sort, true))));
    if (books.length) wrap.append(h("h2", null, `マンガ (${books.length})`), listWrap([...books].sort(cmpFor(prefs.sort, false))));
    if (!dirs.length && !books.length && !data.images) wrap.append(h("div", { class: "msg" }, "ここには何もありません"));
    body.replaceChildren(controlsBar(render, true), wrap);
  };
  render();
  $app.replaceChildren(topbar(), crumbs(path), body);
  window.scrollTo(0, 0);
}

let searchTimer = null;
async function showSearch(text) {
  clearTimeout(searchTimer);
  const body = h("div", null, h("div", { class: "wrap" }, h("div", { class: "msg" }, "検索中…")));
  $app.replaceChildren(topbar(), h("div", { class: "crumbs" }, h("a", { href: "#/b/" }, "ホーム"), " / 検索: " + text), body);
  try {
    const r = await API("/api/search?q=" + q(text));
    const mk = i => ({ dir: i.type === "d", path: i.path, name: i.path.split("/").pop(), sub: i.path.includes("/") ? i.path.slice(0, i.path.lastIndexOf("/")) : "" });
    const dirs = r.items.filter(i => i.type === "d").map(mk), books = r.items.filter(i => i.type === "b").map(mk);
    const render = () => {
      const wrap = h("div", { class: "wrap" });
      if (r.scanning) wrap.append(h("div", { class: "msg" }, `インデックス作成中… (${r.count} 件) 少し待って再検索してください`));
      if (dirs.length) wrap.append(h("h2", null, `フォルダ (${dirs.length})`), listWrap([...dirs].sort(cmpFor(prefs.sort, true))));
      if (books.length) wrap.append(h("h2", null, `マンガ (${books.length})`), listWrap([...books].sort(cmpFor(prefs.sort, true))));
      if (!r.items.length && !r.scanning) wrap.append(h("div", { class: "msg" }, "見つかりませんでした(新しく追加した場合は設定から再スキャン)"));
      body.replaceChildren(controlsBar(render, false), wrap);
    };
    render();
    if (r.scanning) searchTimer = setTimeout(() => { if (location.hash.startsWith("#/q/")) showSearch(text); }, 2500);
  } catch (e) { if (e.message !== "login") body.replaceChildren(h("div", { class: "msg err" }, e.message)); }
}

/* ---------- 初回セットアップ ウィザード ---------- */
const joinPath = (p, n) => { const sep = p.includes("\\") || /^[A-Za-z]:/.test(p) ? "\\" : "/"; return p.replace(/[\\/]+$/, "") + sep + n; };

function qrCard(urls) {
  if (!urls || !urls.length) return null;
  const u = urls[0];
  return h("div", { class: "card", style: "text-align:center" }, h("h3", null, "📱 スマホ・タブレットはこのQRを読み取るだけ"),
    h("img", { src: "/api/qr?u=" + q(u), style: "width:220px;height:220px;background:#fff;border-radius:10px;padding:6px", onerror: e => e.target.replaceWith(h("div", { class: "hint" }, "(QRはPyMuPDF等と一緒にqrcodeを入れると表示されます)")) }),
    h("div", { class: "stat" }, urls.map(x => h("div", null, x))),
    h("div", { class: "hint", style: "padding-bottom:10px" }, "読み取ったあとの接続先はブラウザが記憶します(ホーム画面に追加すると次回からワンタップ)"));
}

async function showWizard() {
  const head = (t) => h("div", { class: "topbar" }, h("a", { href: "#/b/", style: "font-weight:700;color:#fff" }, "📚 まんが"), h("b", { class: "grow" }, t),
    h("button", { class: "btn sm", onclick: () => { location.hash = "#/s"; } }, "⚙ 手動設定"));
  const view = (t, ...kids) => { $app.replaceChildren(head(t), h("div", { class: "set" }, ...kids)); window.scrollTo(0, 0); };
  const tile = (icon, title, sub, fn) => h("div", { class: "folder", style: "padding:14px", onclick: fn }, h("span", { style: "font-size:22px" }, icon),
    h("div", { style: "min-width:0" }, h("div", { style: "white-space:nowrap;overflow:hidden;text-overflow:ellipsis" }, title), sub ? h("div", { class: "hint" }, sub) : null));

  // STEP1: マシン一覧
  async function step1() {
    const hostsBox = h("div", { class: "folders" }, h("div", { class: "msg" }, "🔍 ネットワークを探しています…(数秒)"));
    const drivesBox = h("div", { class: "folders" });
    const manual = h("input", { type: "text", placeholder: "\\\\nas\\nas\\manga  または  D:\\manga" });
    view("① マンガがあるPCを選ぶ",
      h("div", { class: "card" }, h("h3", null, "🖥 ネットワーク上のPC"), hostsBox,
        h("div", { class: "rowcol" }, h("button", { class: "btn sm", onclick: step1 }, "🔄 再検索"))),
      h("div", { class: "card" }, h("h3", null, "💾 このサーバーPC内のドライブ"), drivesBox),
      h("div", { class: "card" }, h("h3", null, "⌨ パスを直接入力"), h("div", { class: "rowcol" }, h("div", { class: "c" }, manual,
        h("button", { class: "btn pri", onclick: () => manual.value.trim() && browse(manual.value.trim()) }, "次へ")))));
    try {
      const r = await API("/api/fs/list?path=");
      drivesBox.replaceChildren(...r.dirs.map(d => tile("💾", d, "", () => browse(d))));
    } catch (e) {}
    try {
      const r = await API("/api/net/scan");
      hostsBox.replaceChildren();
      if (!r.hosts.length) hostsBox.append(h("div", { class: "msg" }, "見つかりませんでした。下の直接入力か、PCの共有設定(ネットワーク探索)を確認してください。"));
      r.hosts.sort((a, b) => (a.name || a.ip).localeCompare(b.name || b.ip)).forEach(x =>
        hostsBox.append(tile("🖥", (x.name || x.ip) + (x.self ? "(このPC)" : ""), x.ip, () => x.self ? browse(r.drives_hint || "") : shares(x))));
    } catch (e) { hostsBox.replaceChildren(h("div", { class: "msg err" }, e.message)); }
  }

  // STEP2: 共有フォルダ
  async function shares(x, creds) {
    const host = x.name || x.ip;
    view("② 共有フォルダを選ぶ", h("div", { class: "msg" }, `${host} の共有を取得中…`));
    let r;
    try { r = await API("/api/net/shares?host=" + q(host)); } catch (e) { r = { shares: [], error: e.message }; }
    if (!r.shares.length && x.name && x.ip) { try { r = await API("/api/net/shares?host=" + q(x.ip)); } catch (e) {} }
    const body = [h("button", { class: "btn sm", onclick: step1 }, "← PC選択に戻る")];
    if (r.shares.length) body.push(h("div", { class: "card" }, h("h3", null, "📂 " + host + " の共有"), h("div", { class: "folders" }, r.shares.map(n => tile("📂", n, "", () => browse("\\\\" + host + "\\" + n))))));
    else {
      body.push(h("div", { class: "msg err" }, r.error || "共有を取得できませんでした"));
      const u = h("input", { type: "text", placeholder: "ユーザー名" }), p = h("input", { type: "password", placeholder: "パスワード", autocomplete: "off" });
      const out = h("div", { class: "stat" });
      body.push(h("div", { class: "card" }, h("h3", null, "🔑 そのPCのユーザー名とパスワードでログイン"),
        h("div", { class: "rowcol" }, u), h("div", { class: "rowcol" }, p),
        h("div", { class: "rowcol" }, h("button", { class: "btn pri", onclick: async () => {
          out.textContent = "接続中…";
          try { const l = await API("/api/net/login", { host, user: u.value, password: p.value }); if (l.ok) shares(x); else { out.className = "stat ng"; out.textContent = l.error || "ログイン失敗"; } }
          catch (e) { out.className = "stat ng"; out.textContent = e.message; }
        } }, "ログインして再取得"), out),
        h("div", { class: "hint", style: "padding-bottom:10px" }, "Windowsの資格情報に保存され、次回から入力不要です。共有名がわかる場合は ① の直接入力でも進めます。")));
    }
    view("② 共有フォルダを選ぶ", ...body);
  }

  // STEP3: フォルダ階層
  async function browse(path) {
    view("③ マンガのフォルダを選ぶ", h("div", { class: "msg" }, "読み込み中…"));
    let r;
    try { r = await API("/api/fs/list?path=" + q(path)); }
    catch (e) { view("③ マンガのフォルダを選ぶ", h("div", { class: "msg err" }, e.message), h("button", { class: "btn", onclick: step1 }, "← 戻る")); return; }
    const info = `フォルダ ${r.folders} ・ zip/rar/pdf ${r.archives} ・ 画像 ${r.images}`;
    view("③ マンガのフォルダを選ぶ",
      h("div", { class: "card" }, h("div", { class: "stat", style: "word-break:break-all" }, "📍 " + (r.path || "(ドライブ一覧)")), h("div", { class: "hint" }, info),
        r.path ? h("div", { class: "rowcol" }, h("button", { class: "btn pri", style: "width:100%;min-height:48px", onclick: () => verify(r.path) }, "✔ このフォルダに決める")) : null,
        h("div", { class: "rowcol" }, h("button", { class: "btn sm", onclick: () => (r.parent ? browse(r.parent) : step1()) }, "↑ 上へ"))),
      h("div", { class: "card" }, h("div", { class: "folders" }, r.dirs.length ? r.dirs.map(n => tile("📁", n, "", () => browse(r.full ? n : joinPath(r.path, n)))) : [h("div", { class: "msg" }, "サブフォルダはありません")])));
  }

  // STEP4: 画像取得テスト → 記憶
  async function verify(path) {
    view("④ 確認中", h("div", { class: "msg" }, "マンガを探して画像を取得しています…"));
    try {
      const v = await API("/api/net/verify", { path });
      if (!v.ok) { view("④ 見つかりません", h("div", { class: "msg err" }, v.error), h("button", { class: "btn", onclick: () => browse(path) }, "← フォルダ選択に戻る")); return; }
      const bi = await API("/api/book?b=" + q(v.sample));
      if (!bi.total) throw new Error("画像が含まれていません");
      const thumb = h("img", { src: `/api/page?b=${q(v.sample)}&i=0&w=400`, style: "max-width:200px;max-height:280px;border-radius:8px;border:1px solid var(--line);background:#fff" });
      await new Promise((res, rej) => { thumb.onload = res; thumb.onerror = () => rej(new Error("画像を取得できませんでした(RARの場合は7-Zipが必要です)")); });
      await API("/api/settings", { library_path: path, setup_done: true });   // ← 取得できたので記憶
      let st = null; try { st = await API("/api/settings"); } catch (e) {}
      view("✔ 設定完了", h("div", { class: "card", style: "text-align:center;padding:14px" }, h("div", { class: "ok", style: "font-size:17px;margin-bottom:10px" }, "画像を取得できました。この場所を記憶しました。"),
        thumb, h("div", { class: "hint", style: "margin-top:8px;word-break:break-all" }, path + "\n" + v.sample),
        h("div", { class: "rowcol" }, h("button", { class: "btn pri", style: "width:100%;min-height:48px", onclick: () => { location.hash = "#/b/"; } }, "📚 マンガを読む"))),
        qrCard(st && st.urls));
    } catch (e) {
      if (e.message === "login") return;
      view("④ 取得できませんでした", h("div", { class: "msg err" }, e.message), h("button", { class: "btn", onclick: () => browse(path) }, "← フォルダ選択に戻る"));
    }
  }
  step1();
}

/* ---------- 設定画面 ---------- */
/* ---- アップデート ---- */
function updateCard() {
  const st = h("div", { class: "stat" }, "確認中…");
  const btns = h("div", { class: "rowcol", style: "display:flex;gap:10px;flex-wrap:wrap" });
  const autoChk = h("input", { type: "checkbox" });
  const render = u => {
    autoChk.checked = !!u.auto;
    btns.replaceChildren();
    if (u.error) { st.className = "stat err"; st.textContent = `サーバーアプリ v${u.version} (build ${u.build}) ／ 更新を確認できません: ${u.error}`; }
    else if (u.available) { st.className = "stat ok"; st.textContent = `サーバーアプリ v${u.version} (build ${u.build}) → 新しい build ${u.latest} があります`; }
    else { st.className = "stat"; st.textContent = `サーバーアプリ v${u.version} (build ${u.build}) は最新です`; }
    if (u.available && u.can_apply) btns.append(h("button", { class: "btn pri", onclick: async () => {
      if (!confirm("サーバーを更新して再起動します(数十秒かかります)。よろしいですか?")) return;
      st.textContent = "更新をダウンロード中… 完了するとサーバーが自動で再起動します";
      try { await API("/api/update/apply", {}); } catch (e) { st.className = "stat err"; st.textContent = e.message; return; }
      st.textContent = "更新中… 少し待ってから画面を再読み込みしてください";
      setTimeout(() => location.reload(), 15000);
    } }, "⬆ 今すぐサーバーを更新"));
    if (u.available && !u.can_apply) btns.append(h("div", { class: "hint" }, "(Python直接起動のため手動更新です。exe版は自動更新できます)"));
  };
  const check = async () => { st.className = "stat"; st.textContent = "確認中…"; try { render(await API("/api/update/check", {})); } catch (e) { st.className = "stat err"; st.textContent = e.message; } };
  autoChk.addEventListener("change", async () => { try { await API("/api/update/auto", { on: autoChk.checked }); } catch (e) {} });
  check();
  return h("div", { class: "card" }, h("h3", null, "⬆ アップデート"), h("div", { class: "rowcol" }, st),
    h("div", { class: "rowcol", style: "display:flex;gap:10px;flex-wrap:wrap" }, h("button", { class: "btn sm", onclick: check }, "サーバーの更新を確認"),
      window.MangaApp && window.MangaApp.checkUpdate ? h("button", { class: "btn sm", onclick: () => window.MangaApp.checkUpdate() }, `📱 スマホアプリの更新を確認${window.MangaApp.appBuild ? " (build " + window.MangaApp.appBuild() + ")" : ""}`) : null),
    btns,
    h("div", { class: "rowcol" }, h("label", { style: "display:flex;gap:8px;align-items:center" }, autoChk, "サーバー起動時に自動で更新する(exe版)")));
}

async function showSettings() {
  $app.replaceChildren(h("div", { class: "msg" }, "読み込み中…"));
  let s;
  try { s = await API("/api/settings"); } catch (e) { if (e.message !== "login") $app.replaceChildren(h("div", { class: "msg err" }, e.message)); return; }

  const F = {};   // サーバー設定の入力欄
  const row = (label, input, hint) => h("div", { class: "row" }, h("label", { class: "t" }, label, hint ? h("div", { class: "hint" }, hint) : null), h("div", { class: "c" }, input));
  const txt = (k, ph, type) => (F[k] = h("input", { type: type || "text", value: s[k] ?? "", placeholder: ph || "" }));
  const num = (k, min, max, step) => (F[k] = h("input", { type: "number", value: s[k], min, max, step: step || 1 }));
  const chk = (k) => (F[k] = h("input", { type: "checkbox", checked: !!s[k] }));

  const testOut = h("div", { class: "stat" });
  const testBtn = h("button", { class: "btn sm", onclick: async () => {
    testOut.textContent = "確認中…"; testOut.className = "stat";
    try {
      const r = await API("/api/test_path", { path: F.library_path.value });
      if (r.is_dir) { testOut.className = "stat ok"; testOut.textContent = `✔ 接続OK: フォルダ ${r.folders} / アーカイブ ${r.archives} 件 (${r.ms} ms)`; }
      else { testOut.className = "stat ng"; testOut.textContent = "✖ " + (r.error || (r.exists ? "フォルダではありません" : "パスが見つかりません")) + ` (${r.ms} ms)`; }
    } catch (e) { testOut.className = "stat ng"; testOut.textContent = e.message; }
  } }, "接続テスト");

  const pwNote = s.has_password ? "設定済み(空欄のままなら変更しません)" : "未設定(LAN内の誰でもアクセス可)";
  const msg = h("div", { class: "stat" });
  const gather = () => {
    const b = {
      library_path: F.library_path.value, host: F.host.value, port: +F.port.value, sevenzip_path: F.sevenzip_path.value,
      cache_dir: F.cache_dir.value, cache_max_gb: +F.cache_max_gb.value, resize_enabled: F.resize_enabled.checked,
      resize_quality: +F.resize_quality.value, resize_max_width: +F.resize_max_width.value, extract_parallel: +F.extract_parallel.value,
    };
    if (F.password.value) b.password = F.password.value;
    return b;
  };
  const save = async (restart) => {
    msg.className = "stat"; msg.textContent = "保存中…";
    try {
      const r = await API("/api/settings", gather());
      msg.className = "stat ok"; msg.textContent = "✔ 保存しました";
      if (restart || r.restart_required) {
        if (restart) {
          msg.textContent = "再起動中…(数秒後に自動で再接続します)";
          await API("/api/restart", {});
          const url = location.protocol + "//" + (r.host && r.host !== "0.0.0.0" && false ? r.host : location.hostname) + ":" + r.port + "/#/s";
          const t0 = Date.now();
          const tick = async () => {
            try { const x = await fetch(url.replace("#/s", "api/status"), { mode: "no-cors" }); location.href = url; }
            catch (e) { if (Date.now() - t0 < 30000) setTimeout(tick, 1000); else msg.textContent = "再接続できません。サーバーが起動しているか確認してください。"; }
          };
          setTimeout(tick, 2500);
        } else { msg.append(h("div", { class: "ng" }, "ポート/待受アドレスの変更は「保存して再起動」で反映されます")); }
      }
    } catch (e) { msg.className = "stat ng"; if (e.message !== "login") msg.textContent = "✖ " + e.message; }
  };

  const T = s.tools;
  const toolsStat = h("div", { class: "stat" },
    `サーバー v${T.version} / Python ${T.python} (${T.platform})`, h("br"),
    "7-Zip: ", T.sevenzip ? h("span", { class: "ok" }, T.sevenzip) : h("span", { class: "ng" }, "見つかりません"), h("br"),
    "tar(bsdtar): ", T.tar ? h("span", { class: "ok" }, T.tar) : h("span", { class: "ng" }, "なし"), h("br"),
    "Pillow(画像縮小): ", T.pillow ? h("span", { class: "ok" }, "あり") : h("span", { class: "ng" }, "なし (pip install pillow)"), h("br"),
    `検索インデックス: ${s.scan.indexed} 件${s.scan.running ? "(作成中…" + s.scan.count + ")" : ""}`);

  const sel = (k, opts) => {
    const e = h("select", null, opts.map(([v, t]) => h("option", { value: v, selected: String(prefs[k]) === String(v) }, t)));
    e.addEventListener("change", () => { prefs[k] = typeof PREF_DEF[k] === "number" ? +e.value : e.value; savePrefs(); });
    return e;
  };
  const pchk = (k) => { const e = h("input", { type: "checkbox", checked: !!prefs[k] }); e.addEventListener("change", () => { prefs[k] = e.checked; savePrefs(); }); return e; };
  const pcol = (k) => { const e = h("input", { type: "color", value: prefs[k], style: "width:60px;height:38px;padding:2px;background:none;border:1px solid var(--line);border-radius:8px" }); e.addEventListener("input", () => { prefs[k] = e.value; savePrefs(); }); return e; };

  $app.replaceChildren(
    h("div", { class: "topbar" }, h("button", { class: "btn sm", onclick: () => { location.hash = sessionStorage.getItem("mv_last") || "#/b/"; } }, "← 戻る"), h("b", null, "設定")),
    h("div", { class: "set" },
      h("div", { class: "card" }, h("h3", null, "📡 ネットワーク / サーバー"),
        h("div", { class: "rowcol" }, h("div", { style: "font-size:14px;margin-bottom:6px" }, "マンガ置き場のパス(ネットワークドライブ)",
          h("div", { class: "hint" }, "例: \\\\NAS\\manga\\ または Z:\\manga  ※サーバーを動かすPCから見えるパス。常駐運用ならUNC(\\\\…)推奨")),
          h("div", { class: "c" }, txt("library_path", "\\\\192.168.1.10\\share\\manga"), testBtn), testOut,
          h("div", { class: "rowcol" }, h("button", { class: "btn", onclick: () => { location.hash = "#/w"; } }, "🔍 ネットワークから探して選ぶ"))),
        row("待受アドレス", txt("host", "0.0.0.0"), "0.0.0.0=LAN内の全端末から接続可 / 127.0.0.1=このPCのみ"),
        row("ポート番号", num("port", 1, 65535)),
        row("パスワード", (F.password = h("input", { type: "password", placeholder: pwNote, autocomplete: "new-password" })), "設定すると全端末でログインが必要になります"),
        s.has_password ? row("パスワード解除", h("button", { class: "btn sm", onclick: async () => { await API("/api/settings", { clear_password: true }); showSettings(); } }, "パスワードを外す")) : null,
        row("7-Zip のパス", txt("sevenzip_path", "空欄=自動検出 (C:\\Program Files\\7-Zip\\7z.exe)"), "RAR/CBR/7Z の展開に使用"),
      ),
      h("div", { class: "card" }, h("h3", null, "⚡ 速度 / キャッシュ"),
        row("画像を画面サイズに縮小して配信", chk("resize_enabled"), "スマホ・タブレットで表示と通信が大幅に軽くなります"),
        row("縮小画質 (40-98)", num("resize_quality", 40, 98)),
        row("縮小の最大幅 (px)", num("resize_max_width", 600, 6000, 100)),
        row("RAR同時展開数", num("extract_parallel", 1, 6)),
        row("キャッシュフォルダ", txt("cache_dir"), "RARの一時展開先・縮小画像・表紙。できればSSD上に"),
        row("キャッシュ上限 (GB)", num("cache_max_gb", 0.2, 2000, 0.5), "超えると古いものから自動削除"),
        h("div", { class: "rowcol" }, h("button", { class: "btn sm", onclick: async () => { await API("/api/clear_cache", {}); msg.className = "stat ok"; msg.textContent = "✔ キャッシュを削除しました"; } }, "キャッシュを全削除"), " ",
          h("button", { class: "btn sm", onclick: async () => { await API("/api/rescan", {}); msg.className = "stat ok"; msg.textContent = "✔ 検索インデックスの再作成を開始しました"; } }, "検索インデックス再スキャン")),
      ),
      window.MangaApp ? h("div", { class: "card" }, h("h3", null, "📱 このアプリの接続先"), h("div", { class: "rowcol" },
        h("button", { class: "btn", onclick: () => window.MangaApp.changeServer() }, "🔍 ネットワークからサーバーを探し直す"),
        h("div", { class: "hint", style: "padding-top:6px" }, "PCの入れ替えやIP変更があっても、同じサーバーは自動で見つけ直します"))) : null,
      updateCard(),
      window.MangaApp ? null : qrCard(s.urls),
      h("div", { class: "card" }, h("h3", null, "このサーバーの状態"), toolsStat),
      h("div", { class: "rowcol", style: "display:flex;gap:10px;flex-wrap:wrap" },
        h("button", { class: "btn pri", onclick: () => save(false) }, "保存"), h("button", { class: "btn", onclick: () => save(true) }, "保存して再起動"),
        h("button", { class: "btn", onclick: async () => { if (confirm("サーバーを停止します。スマホ等からも使えなくなります。よろしいですか?")) { await API("/api/quit", {}); msg.textContent = "サーバーを停止しました(デスクトップのアイコンで再開できます)"; } } }, "サーバー停止"), msg),

      h("div", { class: "card" }, h("h3", null, "📖 表示(この端末のみ)"),
        row("見開き/単ページ", sel("mode", [["auto", "自動(PC=見開き / タブレット・スマホ=単ページ)"], ["double", "常に見開き"], ["single", "常に単ページ"]])),
        row("綴じ方向", sel("binding", [["rtl", "右開き(日本のマンガ)"], ["ltr", "左開き"]])),
        row("表紙を単独で表示", pchk("coverAlone"), "見開き時、1ページ目を単独にして以降のペアをずらさない"),
        row("横長ページは単独表示", pchk("wideAlone"), "見開き画像などを1枚で大きく表示"),
        row("画面への合わせ方", sel("fit", [["contain", "画面に収める"], ["width", "幅に合わせる(縦スクロール・単ページ時)"]])),
        row("画質", sel("quality", [["auto", "自動(画面サイズに合わせる)"], ["light", "軽量(さらに小さく)"], ["orig", "原寸(縮小しない)"]])),
        row("先読みページ数", sel("preload", [[2, "2"], [4, "4"], [6, "6"], [10, "10"]])),
        row("クリック/スワイプ方向を反転", pchk("clickSwap"), "PC: 左クリック=戻る・右クリック=進む / スマホ: 上スワイプ=前ページ に入れ替え"),
        row("マウスホイールでページ送り", pchk("wheel")),
        row("背景色", pcol("bg")),
        row("一覧の表示", sel("view", [["list", "リスト(サムネ+タイトル全文)"], ["grid", "サムネを並べる"]])),
        row("一覧の並び順", sel("sort", SORTS)),
        row("サムネの大きさ(並べる表示)", sel("tile", [[110, "小"], [150, "中"], [200, "大"]])),
        row("起動時に前回の本の続きから開く", pchk("resume")),
        row("続きから読む履歴を表示", pchk("remember")),
        row("タッチ端末で自動的に全画面", pchk("autoFull"), "Android(Chrome)向け。iPhoneは「ホーム画面に追加」で全画面になります"),
        h("div", { class: "hint", style: "padding:8px 0" }, "操作: PC=左クリック進む/右クリック戻る・←→・Space・ホイール・Fで全画面・上端/下端にマウスでメニュー ｜ スマホ/タブレット=上スワイプ次ページ/下スワイプ前ページ・画面の左右端タップでもめくれる・中央ダブルタップでメニュー"),
      ),
    ));
}

/* ---------- リーダー ---------- */
let R = null;

function isDouble() {
  if (prefs.mode === "double") return true;
  if (prefs.mode === "single") return false;
  return matchMedia("(pointer:fine)").matches && innerWidth >= 800 && innerWidth >= innerHeight * 1.1;
}

function calcW() {
  if (prefs.quality === "orig") return 0;
  const dbl = isDouble() && R && !R.fitw();
  let w = innerWidth * (window.devicePixelRatio || 1) / (dbl ? 2 : 1);
  if (prefs.quality === "light") w *= 0.75;
  return Math.min(3000, Math.max(400, Math.ceil(w / 200) * 200));
}

async function openReader(path) {
  leaveReader();
  const stage = h("div", { id: "stage" });
  const loading = h("div", { id: "loading" }, "読み込み中…");
  const toastEl = h("div", { id: "toast" });
  const title = h("div", { class: "ttl" }, stripExt(baseName(path)));
  const pg = h("div", { class: "pg" }, "");
  const slider = h("input", { type: "range", min: 1, max: 1, value: 1 });
  const modeBtn = h("button", { class: "btn", title: "見開き/単ページ切替(D)" }, "");
  const top = h("div", { class: "rbar top hide" },
    h("button", { class: "btn", onclick: () => closeReader() }, "📚 本棚"), title, modeBtn,
    h("button", { class: "btn", title: "全画面(F)", onclick: () => toggleFull() }, "⛶"),
    h("button", { class: "btn", title: "設定", onclick: () => { location.hash = "#/s"; } }, "⚙"));
  const bmBtn = h("button", { class: "btn bmb", title: "しおりを追加/削除 (B)" }, "🔖");
  const bmList = h("button", { class: "btn", title: "しおり一覧" }, "📑");
  const bot = h("div", { class: "rbar bot hide" }, bmBtn, pg, slider, bmList);
  const homeBtn = h("button", { class: "homebtn", title: "本棚へ戻る", onclick: e => { e.stopPropagation(); closeReader(); } }, "📚");
  const root = h("div", { id: "reader", style: `--rbg:${prefs.bg}` }, stage, loading, toastEl, top, bot, homeBtn);
  $app.replaceChildren(root);
  document.title = stripExt(baseName(path));

  R = { path, root, stage, loading, toastEl, pg, slider, top, bot, modeBtn, bmBtn, bms: new Set(), total: 0, spread: [], imgs: new Map(), tk: 0, w: 0,
        ac: new AbortController(), pinned: false, nb: null, fitw: () => prefs.fit === "width" && !isDouble() };
  const me = R;

  let info;
  try { info = await API("/api/book?b=" + q(path)); }
  catch (e) { if (R !== me) return; if (e.message !== "login") { loading.textContent = ""; showEnd(e.message, true); } return; }
  if (R !== me) return;
  if (!info.total) { loading.textContent = ""; showEnd("画像が見つかりませんでした", true); return; }
  me.total = info.total;
  slider.max = info.total;
  const bind = () => { slider.style.direction = prefs.binding === "rtl" ? "rtl" : "ltr"; };
  bind();
  let start = prefs.remember ? (info.p || 0) : 0;
  if (start >= me.total - 1) start = 0;
  updateModeBtn();
  modeBtn.addEventListener("click", cycleMode);
  slider.addEventListener("input", () => { pg.textContent = `${slider.value} / ${me.total}`; });
  slider.addEventListener("change", () => go(+slider.value - 1));
  attachEvents();
  bmBtn.addEventListener("click", e => { e.stopPropagation(); toggleBookmark(); });
  bmList.addEventListener("click", e => { e.stopPropagation(); showBmPanel(); });
  API("/api/bookmarks?b=" + q(path)).then(r => { if (R === me) { me.bms = new Set(r.items.map(i => i.p)); updateBm(); } }).catch(() => {});
  API("/api/neighbors?b=" + q(path)).then(n => { if (R === me) me.nb = n; }).catch(() => {});
  go(start);
}

function leaveReader() {
  if (!R) return;
  flushProgress();
  R.ac.abort();
  R.tk++;
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  document.title = "まんがビューワー";
  R = null;
}

function closeReader() {
  if (!R) return;
  const p = R.path.includes("/") ? R.path.slice(0, R.path.lastIndexOf("/")) : "";
  location.hash = "#/b/" + enc(p);
}

let noAutoFull = false;
function toggleFull() {
  if (document.fullscreenElement) { noAutoFull = true; document.exitFullscreen(); }
  else { noAutoFull = false; enterFull(); }
}
function enterFull() {
  const d = document.documentElement;
  try { const r = d.requestFullscreen ? d.requestFullscreen({ navigationUI: "hide" }) : (d.webkitRequestFullscreen && d.webkitRequestFullscreen()); if (r && r.catch) r.catch(() => {}); } catch (e) {}
}
// Android等: 最初のタッチ操作で自動的に全画面へ(ユーザー操作が必要なため)
document.addEventListener("touchend", () => {
  if (prefs.autoFull && !noAutoFull && !document.fullscreenElement && document.fullscreenEnabled && matchMedia("(pointer:coarse)").matches) enterFull();
}, { passive: true });

function toast(t) {
  if (!R) return;
  R.toastEl.textContent = t; R.toastEl.classList.add("on");
  clearTimeout(R.toastT); R.toastT = setTimeout(() => R && R.toastEl.classList.remove("on"), 1400);
}

function updateModeBtn() {
  if (!R) return;
  const lab = { auto: "自動", double: "見開き", single: "単ページ" }[prefs.mode];
  R.modeBtn.textContent = (isDouble() ? "▯▯ " : "▯ ") + lab;
}

function cycleMode() {
  prefs.mode = { auto: "double", double: "single", single: "auto" }[prefs.mode];
  savePrefs(); updateModeBtn();
  toast({ auto: "自動", double: "見開き", single: "単ページ" }[prefs.mode]);
  go(R.spread[0] || 0);
}

function srcOf(i, w) { return `/api/page?b=${q(R.path)}&i=${i}` + (w ? `&w=${w}` : ""); }

function getImg(i) {
  const me = R, w = me.w, k = i + "@" + w;
  let p = me.imgs.get(k);
  if (!p) {
    p = new Promise((res, rej) => {
      const im = new Image();
      im.decoding = "async";
      im.onload = () => res(im);
      im.onerror = () => rej(new Error("画像の読み込みに失敗 (p." + (i + 1) + ")"));
      im.src = srcOf(i, w);
    });
    me.imgs.set(k, p);
    p.catch(() => me.imgs.delete(k));
  }
  return p;
}
const isWide = im => im.naturalWidth > im.naturalHeight * 1.05;

async function spreadAt(p) {
  if (!isDouble() || R.fitw()) return [p];
  if (p === 0 && prefs.coverAlone) return [0];
  if (p + 1 >= R.total) return [p];
  const a = await getImg(p);
  if (prefs.wideAlone && isWide(a)) return [p];
  const b = await getImg(p + 1);
  if (prefs.wideAlone && isWide(b)) return [p];
  return [p, p + 1];
}

async function prevStart(f) {
  if (!isDouble() || R.fitw()) return f - 1;
  const a = f - 1;
  if (a <= 0) return 0;
  const ia = await getImg(a);
  if (prefs.wideAlone && isWide(ia)) return a;
  const b = a - 1;
  if (b < 0 || (b === 0 && prefs.coverAlone)) return a;
  const ib = await getImg(b);
  if (prefs.wideAlone && isWide(ib)) return a;
  return b;
}

let loadingT = null;
async function go(p) {
  const me = R;
  if (!me) return;
  p = Math.max(0, Math.min(me.total - 1, p));
  const tk = ++me.tk;
  const newW = calcW();
  if (newW !== me.w) { me.w = newW; me.imgs.clear(); }
  clearTimeout(loadingT);
  loadingT = setTimeout(() => { if (R === me && me.tk === tk) me.loading.style.display = ""; }, 250);
  try {
    const sp = await spreadAt(p);
    if (R !== me || me.tk !== tk) return;
    me.spread = sp;
    const ims = await Promise.all(sp.map(getImg));
    if (R !== me || me.tk !== tk) return;
    draw(sp, ims);
  } catch (e) {
    if (R !== me || me.tk !== tk) return;
    clearTimeout(loadingT);
    me.loading.style.display = "none";
    toast(e.message || "エラー");
  }
}

function draw(sp, ims) {
  const me = R;
  clearTimeout(loadingT);
  me.loading.style.display = "none";
  const dbl = isDouble() && !me.fitw();
  me.stage.className = (dbl ? "dbl " : "") + (prefs.binding === "rtl" ? "rtl " : "") + (me.fitw() ? "fitw" : "");
  ims.forEach((im, k) => { im.className = sp.length === 2 ? (k === 0 ? "first" : "second") : "single"; im.draggable = false; });
  me.stage.replaceChildren(...ims);
  me.stage.scrollTop = 0;
  const last = sp[sp.length - 1];
  me.slider.value = sp[0] + 1;
  updateBm();
  me.pg.textContent = sp.length === 2 ? `${sp[0] + 1}-${last + 1} / ${me.total}` : `${sp[0] + 1} / ${me.total}`;
  updateModeBtn();
  saveProgressSoon();
  // 先読み・メモリ整理
  for (let k = 1; k <= prefs.preload; k++) if (last + k < me.total) getImg(last + k).catch(() => {});
  for (let k = 1; k <= 2; k++) if (sp[0] - k >= 0) getImg(sp[0] - k).catch(() => {});
  const lo = sp[0] - 4, hi = last + prefs.preload + 3;
  for (const key of [...me.imgs.keys()]) { const i = +key.split("@")[0]; if (i < lo || i > hi) me.imgs.delete(key); }
}

/* ---- しおり(1冊に複数) ---- */
function updateBm() {
  if (!R) return;
  const on = R.spread.some(p => R.bms.has(p));
  R.bmBtn.classList.toggle("on", on);
}
async function bmPost(page, on) {
  const me = R;
  const r = await API("/api/bookmark", { b: me.path, page, on });
  if (R === me) { me.bms = new Set(r.items.map(i => i.p)); updateBm(); }
  return r.items;
}
async function toggleBookmark() {
  if (!R || !R.spread.length) return;
  const has = R.spread.filter(p => R.bms.has(p));
  try {
    if (has.length) { for (const p of has) await bmPost(p, false); toast("しおりを外しました"); }
    else { await bmPost(R.spread[0], true); toast("🔖 しおりを追加 (p." + (R.spread[0] + 1) + ")"); }
  } catch (e) { toast("しおりを保存できません"); }
}
async function showBmPanel() {
  if (!R) return;
  const me = R;
  if (me.bmPanel) { me.bmPanel.remove(); me.bmPanel = null; return; }
  const panel = h("div", { id: "bmpanel" });
  me.bmPanel = panel;
  me.root.append(panel);
  const close = () => { panel.remove(); if (R === me) me.bmPanel = null; };
  const draw = async () => {
    let items = [];
    try { items = (await API("/api/bookmarks?b=" + q(me.path))).items; } catch (e) {}
    panel.replaceChildren(
      h("div", { class: "bmhead" }, h("b", null, `🔖 しおり (${items.length})`), h("div", { class: "grow" }),
        h("button", { class: "btn sm", onclick: async () => { const p = me.spread[0]; if (p != null) { await bmPost(p, true); draw(); } } }, "＋ 今のページ"),
        h("button", { class: "btn sm", onclick: close }, "✕")),
      items.length ? h("div", { class: "bmlist" }, items.map(it => h("div", { class: "bmitem", onclick: () => { close(); go(it.p); } },
        h("img", { src: `/api/page?b=${q(me.path)}&i=${it.p}&w=200`, loading: "lazy" }),
        h("div", { class: "bmmeta" }, h("div", null, `ページ ${it.p + 1} / ${me.total}`), h("div", { class: "hint" }, fmtDate(it.ts))),
        h("button", { class: "btn sm", onclick: async e => { e.stopPropagation(); await bmPost(it.p, false); draw(); } }, "削除"))))
      : h("div", { class: "msg" }, "しおりはまだありません。🔖で追加できます"));
  };
  draw();
}

function saveProgressSoon() {
  clearTimeout(R.saveT);
  R.saveT = setTimeout(flushProgress, 700);
}
function flushProgress() {
  if (!R || !R.spread.length || !R.total) return;
  clearTimeout(R.saveT);
  const body = JSON.stringify({ b: R.path, page: R.spread[0], last: R.spread[R.spread.length - 1], total: R.total });
  try { fetch("/api/progress", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true }); } catch (e) {}
}

async function next() {
  if (!R || !R.spread.length) return;
  if (R.endOv) { if (R.nb && R.nb.next) location.hash = "#/r/" + enc(R.nb.next); return; }
  const last = R.spread[R.spread.length - 1];
  if (last + 1 >= R.total) { flushProgress(); showEnd(); return; }
  go(last + 1);
}
async function prev() {
  if (!R || !R.spread.length) return;
  if (R.endOv) { hideEnd(); return; }
  const f = R.spread[0];
  if (f <= 0) { toast("最初のページです"); return; }
  const me = R;
  try { const s = await prevStart(f); if (R === me) go(s); } catch (e) { go(f - 1); }
}

function showEnd(text, isErr) {
  if (!R || R.endOv) return;
  const me = R;
  const box = h("div", { id: "endov" },
    h("div", { style: "font-size:18px;padding:0 20px;text-align:center" }, text || "最後まで読みました"),
    !isErr && me.nb && me.nb.next ? h("button", { class: "btn pri", onclick: () => { location.hash = "#/r/" + enc(me.nb.next); } }, "次の巻へ ▶ " + stripExt(baseName(me.nb.next))) : null,
    !isErr ? h("button", { class: "btn", onclick: () => { hideEnd(); go(0); } }, "最初から読み直す") : null,
    h("button", { class: "btn", onclick: () => closeReader() }, "一覧に戻る"),
    !isErr ? h("button", { class: "btn", onclick: () => hideEnd() }, "閉じる") : null);
  me.endOv = box;
  me.root.append(box);
}
function hideEnd() { if (R && R.endOv) { R.endOv.remove(); R.endOv = null; } }

/* ---- UIバー ---- */
function setBars(show) {
  if (!R) return;
  R.top.classList.toggle("hide", !show);
  R.bot.classList.toggle("hide", !show);
  R.barsOn = show;
}

function attachEvents() {
  const me = R, sig = { signal: me.ac.signal };
  const inBars = t => t.closest && t.closest(".rbar,#endov,#bmpanel,.homebtn");
  const fwd = () => (prefs.clickSwap ? prev() : next());
  const back = () => (prefs.clickSwap ? next() : prev());
  const rtl = () => prefs.binding === "rtl";

  // --- マウス ---
  me.root.addEventListener("click", e => {
    if (inBars(e.target) || e.button !== 0) return;
    if (Date.now() - (me.touchAt || 0) < 700) return;
    fwd();
  }, sig);
  me.root.addEventListener("contextmenu", e => {
    e.preventDefault();
    if (inBars(e.target)) return;
    if (Date.now() - (me.touchAt || 0) < 700) return;
    back();
  }, sig);
  me.root.addEventListener("auxclick", e => { if (e.button === 1) { e.preventDefault(); setBars(!me.barsOn); me.pinned = me.barsOn; } }, sig);
  me.root.addEventListener("mousemove", e => {
    if (Date.now() - (me.touchAt || 0) < 700) return;
    const near = e.clientY < 64 || e.clientY > innerHeight - 90;
    if (near) setBars(true); else if (!me.pinned && me.barsOn) setBars(false);
  }, sig);
  let wheelT = 0;
  me.root.addEventListener("wheel", e => {
    if (!prefs.wheel || me.fitw() || inBars(e.target)) return;
    e.preventDefault();
    const now = Date.now();
    if (now - wheelT < 160 || Math.abs(e.deltaY) < 4) return;
    wheelT = now;
    if (e.deltaY > 0) next(); else prev();
  }, { passive: false, signal: me.ac.signal });

  // --- タッチ ---
  let t0 = null;
  me.root.addEventListener("touchstart", e => {
    if (inBars(e.target)) { t0 = null; return; }
    if (e.touches.length > 1) { t0 = null; me.multi = true; return; }
    me.multi = false;
    const t = e.touches[0];
    t0 = { x: t.clientX, y: t.clientY, t: Date.now() };
  }, { passive: true, signal: me.ac.signal });
  me.root.addEventListener("touchend", e => {
    if (!t0 || me.multi) { if (e.touches.length === 0) me.multi = false; t0 = null; return; }
    const t = e.changedTouches[0], s = t0; t0 = null;
    if (window.visualViewport && visualViewport.scale > 1.02) return;   // ピンチ拡大中は操作しない
    const dx = t.clientX - s.x, dy = t.clientY - s.y, dt = Date.now() - s.t;
    me.touchAt = Date.now();
    const swapped = prefs.clickSwap;
    // 上スワイプ=次ページ / 下スワイプ=前ページ
    if (Math.abs(dy) > 45 && Math.abs(dy) > Math.abs(dx) * 1.4 && dt < 1000) {
      if (me.fitw()) return;   // 幅合わせ(縦スクロール)時は通常スクロール
      e.preventDefault();
      let forward = dy < 0;
      if (swapped) forward = !forward;
      forward ? next() : prev();
      return;
    }
    if (Math.abs(dx) < 12 && Math.abs(dy) < 12 && dt < 600) {
      e.preventDefault();
      const f = t.clientX / innerWidth, now = Date.now();
      if (f > 0.3 && f < 0.7) {   // 中央ダブルタップ=メニュー
        const lt = me.lastTap;
        if (lt && now - lt.t < 400 && Math.abs(lt.x - t.clientX) < 60 && Math.abs(lt.y - t.clientY) < 60) { me.lastTap = null; setBars(!me.barsOn); me.pinned = me.barsOn; }
        else me.lastTap = { t: now, x: t.clientX, y: t.clientY };
        return;
      }
      me.lastTap = null;
      if (me.barsOn) { setBars(false); me.pinned = false; return; }
      let forward = (f <= 0.3) === rtl();   // 左右端タップ(右開き: 左=進む)
      if (swapped) forward = !forward;
      forward ? next() : prev();
    }
  }, { passive: false, signal: me.ac.signal });

  // --- キーボード ---
  document.addEventListener("keydown", e => {
    if (e.target.tagName === "INPUT" && e.target.type !== "range") return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    let used = true;
    if (k === "ArrowLeft") (rtl() ? next : prev)();
    else if (k === "ArrowRight") (rtl() ? prev : next)();
    else if (k === " " || k === "PageDown" || (k === "ArrowDown" && !me.fitw())) (e.shiftKey ? prev : next)();
    else if (k === "PageUp" || (k === "ArrowUp" && !me.fitw())) prev();
    else if (k === "Home") go(0);
    else if (k === "End") go(me.total - 1);
    else if (k === "f" || k === "F") toggleFull();
    else if (k === "d" || k === "D") cycleMode();
    else if (k === "b" || k === "B") toggleBookmark();
    else if (k === "m" || k === "M") { setBars(!me.barsOn); me.pinned = me.barsOn; }
    else if (k === "Escape") { if (me.bmPanel) { me.bmPanel.remove(); me.bmPanel = null; } else if (me.endOv) hideEnd(); else if (document.fullscreenElement) document.exitFullscreen(); else closeReader(); }
    else used = false;
    if (used) e.preventDefault();
  }, sig);

  // --- リサイズ/回転 ---
  let rt = null;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => { if (R === me && me.spread.length) go(me.spread[0]); }, 250);
  }, sig);
  window.addEventListener("pagehide", flushProgress, sig);
  document.addEventListener("visibilitychange", () => { if (document.hidden) flushProgress(); }, sig);
}

/* ---------- 起動 ---------- */
(async () => {
  if (!location.hash && prefs.resume) {          // アプリ起動時: 前回の本のページから開く
    try {
      const st = await (await fetch("/api/status")).json();
      if (st.configured) {
        const r = await API("/api/recent");
        if (r.items.length) {          // 本棚(その本のフォルダ)を履歴に積んでから本を開く → 戻るで本棚へ
          const pth = r.items[0].path, par = pth.includes("/") ? pth.slice(0, pth.lastIndexOf("/")) : "";
          history.replaceState(null, "", "#/b/" + enc(par));
          history.pushState(null, "", "#/r/" + enc(pth));
          route(); return;
        }
      }
    } catch (e) {}
  }
  route();
})();
