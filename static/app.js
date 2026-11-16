'use strict';

// ---------- 小道具 ----------

// DOM 要素を組み立てる。文字列は必ずテキストノードになるので HTML 注入は起きない。
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  el.append(...kids.flat(Infinity).filter(k => k != null && k !== false));
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k in el) el[k] = v;
    else el.setAttribute(k, v);
  }
  return el;
}

// 要素の中身を入れ替える（配列・null を含んでいてもよい）。
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false));
}

let ME = null; // ログイン中のユーザー（未ログインなら null）
const userName = () => (ME ? ME.display_name : '');
const ROLE_LABELS = { viewer: '閲覧のみ', editor: '編集可', manager: '管理' };

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(url, opt);
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // セッション切れ・無効化などでログインが外れたらログイン画面へ戻す
    if (res.status === 401 && !url.startsWith('/api/auth/') && ME) { ME = null; showAuth(false); }
    const d = data.detail;
    const err = new Error(typeof d === 'string' ? d : '入力内容を確認してください');
    if (d && d.errors) err.fieldErrors = d.errors;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3000);
}

// モーダルダイアログ。onSubmit が例外を投げた場合は閉じずにメッセージを表示する。
function openDialog({ title, body, submitLabel = '保存', onSubmit, extra = [] }) {
  const dlg = h('dialog', {},
    h('form', {
      method: 'dialog',
      onsubmit: async e => {
        e.preventDefault();
        if (!onSubmit) return;
        try { await onSubmit(); dlg.close(); }
        catch (err) { if (!err.fieldErrors) toast(err.message); }
      },
    },
      h('div', { class: 'dlg-head' }, title),
      h('div', { class: 'dlg-body' }, body),
      h('div', { class: 'dlg-foot' },
        extra, h('span', { class: 'spacer' }),
        h('button', { type: 'button', onclick: () => dlg.close() }, onSubmit ? 'キャンセル' : '閉じる'),
        onSubmit && h('button', { class: 'primary', type: 'submit' }, submitLabel))));
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

const main = document.getElementById('main');
let META = null;
const CHOICE = ['select', 'radio', 'multi'];
// アプリごとの一覧の表示状態（検索語・絞り込み・並び順・ページ）
const listState = {};

// ---------- ルーティング ----------

async function route() {
  if (!ME) return;
  if (!META) META = await api('GET', '/api/meta');
  // 画面を切り替えるときは開いているダイアログを閉じる
  document.querySelectorAll('dialog').forEach(d => d.close());
  const m = location.hash.match(/^#\/app\/(\d+)(?:\/(\w+))?(?:\/(\d+))?/);
  try {
    if (location.hash === '#/users' && ME.role === 'admin') return await viewUsers();
    if (!m) return await viewHome();
    const app = await api('GET', `/api/apps/${m[1]}`);
    let tab = m[2] === 'record' ? 'records' : m[2] || 'records';
    if (tab === 'settings' && app.role !== 'manager') tab = 'records';
    const content = h('div');
    fill(main, appHeader(app, tab), content);
    if (tab === 'settings') await viewSettings(app, content);
    else if (tab === 'summary') viewSummary(app, content);
    else if (tab === 'calendar') await viewCalendar(app, content);
    else {
      await viewRecords(app, content);
      // #/app/1/record/5 の形式（通知や参照項目のリンク）ならそのレコードを開く
      if (m[2] === 'record' && m[3]) {
        const rec = await api('GET', `/api/apps/${app.id}/records/${m[3]}`);
        const hash = location.hash;
        // 閉じたら通常の一覧 URL に戻す（同じリンクをもう一度押しても開けるように）
        recordDialog(app, rec, () => viewRecords(app, content)).addEventListener('close', () => {
          if (location.hash === hash) history.replaceState(null, '', `#/app/${app.id}`);
        });
      }
    }
  } catch (err) {
    if (!ME) return; // ログインが外れた場合はログイン画面を表示済み
    fill(main, h('div', { class: 'empty' }, err.message, h('p', {}, h('a', { href: '#/' }, 'アプリ一覧へ戻る'))));
  }
}
window.addEventListener('hashchange', route);
boot();

// ---------- ログインとアカウント ----------

const userBtn = document.getElementById('userBtn');
const badge = document.getElementById('badge');
function showUser() { userBtn.textContent = '👤 ' + userName(); }
userBtn.addEventListener('click', accountDialog);

async function boot() {
  const st = await api('GET', '/api/auth/state');
  if (st.user) signedIn(st.user); else showAuth(st.setup_required);
}

function signedIn(user) {
  ME = user;
  document.body.classList.add('signed-in');
  showUser(); pollNotifications(); route();
}

// ログイン画面。ユーザーが1人もいないときは最初の管理者を登録する画面になる。
function showAuth(setup) {
  document.body.classList.remove('signed-in');
  document.querySelectorAll('dialog').forEach(d => d.close());
  const username = h('input', { class: 'wide', required: true, autocomplete: 'username', autocapitalize: 'none', maxLength: 50 });
  const display = h('input', { class: 'wide', required: true, maxLength: 50, placeholder: '例: 山田 太郎' });
  const password = h('input', { class: 'wide', type: 'password', required: true, autocomplete: setup ? 'new-password' : 'current-password' });
  const error = h('div', { class: 'error', role: 'alert' });
  fill(main, h('form', {
    class: 'panel auth',
    onsubmit: async e => {
      e.preventDefault();
      error.textContent = '';
      try {
        signedIn(setup
          ? await api('POST', '/api/auth/setup', { username: username.value, display_name: display.value, password: password.value })
          : await api('POST', '/api/auth/login', { username: username.value, password: password.value }));
      } catch (err) { error.textContent = err.message; }
    },
  },
    h('h1', {}, setup ? '初期設定' : 'ログイン'),
    setup && h('p', { class: 'muted' }, '最初のユーザーを登録します。このユーザーがシステム管理者になり、ほかのユーザーを追加できます。'),
    h('div', { class: 'form-field' }, h('label', {}, 'ユーザーID'), username,
      setup && h('div', { class: 'muted' }, '半角英小文字・数字・._- の3〜50文字')),
    setup && h('div', { class: 'form-field' }, h('label', {}, '表示名'), display),
    h('div', { class: 'form-field' }, h('label', {}, 'パスワード'), password,
      setup && h('div', { class: 'muted' }, '8文字以上')),
    error,
    h('button', { class: 'primary wide', type: 'submit' }, setup ? '登録して始める' : 'ログイン')));
  username.focus();
}

function accountDialog() {
  const display = h('input', { class: 'wide', value: ME.display_name, maxLength: 50 });
  const current = h('input', { class: 'wide', type: 'password', autocomplete: 'current-password' });
  const next = h('input', { class: 'wide', type: 'password', autocomplete: 'new-password' });
  const dlg = openDialog({
    title: `アカウント（${ME.username}）`,
    body: [
      h('div', { class: 'form-field' }, h('label', {}, '表示名'), display),
      h('h2', { style: 'margin-top:18px' }, 'パスワードの変更'),
      h('div', { class: 'muted', style: 'margin-bottom:8px' }, '変更しない場合は空のままにしてください。'),
      h('div', { class: 'form-field' }, h('label', {}, '現在のパスワード'), current),
      h('div', { class: 'form-field' }, h('label', {}, '新しいパスワード（8文字以上）'), next),
    ],
    extra: [
      h('button', {
        type: 'button',
        onclick: async () => {
          await api('POST', '/api/auth/logout');
          ME = null; META = null; showAuth(false);
        },
      }, 'ログアウト'),
      ME.role === 'admin' && h('a', { class: 'btn', href: '#/users', onclick: () => dlg.close() }, 'ユーザー管理'),
    ],
    onSubmit: async () => {
      if (next.value || current.value) {
        await api('PUT', '/api/auth/password', { current: current.value, new: next.value });
      }
      if (display.value.trim() !== ME.display_name) {
        ME = await api('PUT', '/api/auth/profile', { display_name: display.value });
        showUser();
      }
      toast('保存しました');
    },
  });
}

// ---------- ユーザー管理（システム管理者のみ） ----------

async function viewUsers() {
  const users = await api('GET', '/api/users');
  fill(main,
    h('div', { class: 'row' },
      h('h1', {}, 'ユーザー管理'), h('span', { class: 'spacer' }),
      h('button', { class: 'primary', onclick: () => userDialog(null) }, '＋ ユーザーを追加')),
    h('div', { class: 'table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, ['ユーザーID', '表示名', '種別', '状態', '登録日時'].map(t => h('th', { style: 'cursor:default' }, t)))),
      h('tbody', {}, users.map(u => h('tr', { tabIndex: 0, onclick: () => userDialog(u), onkeydown: e => { if (e.key === 'Enter') userDialog(u); } },
        h('td', {}, u.username), h('td', {}, u.display_name),
        h('td', {}, u.role === 'admin' ? 'システム管理者' : '一般'),
        h('td', {}, u.active ? '有効' : h('span', { class: 'muted' }, '無効')),
        h('td', { class: 'muted' }, u.created_at)))))),
    h('p', { class: 'muted' }, 'システム管理者は、ユーザーの管理と全アプリの管理ができます。無効にしたユーザーはログインできなくなります（データは残ります）。'));
}

function userDialog(u) {
  const username = h('input', { class: 'wide', required: true, maxLength: 50, autocapitalize: 'none', value: u ? u.username : '', disabled: !!u });
  const display = h('input', { class: 'wide', required: true, maxLength: 50, value: u ? u.display_name : '' });
  const password = h('input', { class: 'wide', type: 'password', required: !u, autocomplete: 'new-password' });
  const role = h('select', { value: u ? u.role : 'user' }, h('option', { value: 'user' }, '一般'), h('option', { value: 'admin' }, 'システム管理者'));
  const active = h('input', { type: 'checkbox', checked: u ? u.active : true });
  openDialog({
    title: u ? `ユーザー「${u.username}」を編集` : 'ユーザーを追加',
    submitLabel: u ? '保存' : '追加',
    body: [
      h('div', { class: 'form-field' }, h('label', {}, 'ユーザーID'), username,
        !u && h('div', { class: 'muted' }, '半角英小文字・数字・._- の3〜50文字。後から変更できません。')),
      h('div', { class: 'form-field' }, h('label', {}, '表示名'), display),
      h('div', { class: 'form-field' }, h('label', {}, u ? 'パスワードの再設定（変更しない場合は空）' : '初期パスワード（8文字以上）'), password),
      h('div', { class: 'form-field' }, h('label', {}, '種別'), role),
      u && h('div', { class: 'form-field' }, h('label', { style: 'font-weight:400' }, active, ' 有効（ログインを許可する）')),
    ],
    onSubmit: async () => {
      if (u) {
        await api('PUT', `/api/users/${u.id}`, { display_name: display.value, role: role.value, active: active.checked, password: password.value });
        if (u.id === ME.id) { ME = { ...ME, display_name: display.value.trim(), role: role.value }; showUser(); }
      } else {
        await api('POST', '/api/users', { username: username.value, display_name: display.value, password: password.value, role: role.value });
      }
      toast('保存しました'); route();
    },
  });
}

// ---------- 通知 ----------

async function pollNotifications() {
  if (!ME) return null;
  try {
    const r = await api('GET', '/api/notifications');
    badge.textContent = r.unread > 99 ? '99+' : r.unread;
    badge.hidden = !r.unread;
    return r;
  } catch { return null; }
}
pollNotifications();
setInterval(pollNotifications, 30000);

document.getElementById('bell').addEventListener('click', async () => {
  const r = await pollNotifications();
  if (!r) return toast('通知を取得できませんでした');
  const dlg = openDialog({
    title: `通知（${userName()} さん宛て）`,
    body: r.items.length
      ? r.items.map(n => h('a', {
          class: 'notif' + (n.read ? '' : ' unread'),
          href: `#/app/${n.app_id}` + (n.record_id ? `/record/${n.record_id}` : ''),
          onclick: () => dlg.close(),
        },
          h('div', {}, n.message),
          h('div', { class: 'muted' }, `${n.app_icon} ${n.app_name}　${n.created_at}`)))
      : h('div', { class: 'muted' }, '通知はありません。アプリ画面の「通知を受け取る」を押すと、そのアプリの更新が届きます。'),
  });
  if (r.unread) { await api('POST', '/api/notifications/read'); pollNotifications(); }
});

function appHeader(app, tab) {
  const link = (key, label) => h('a', {
    href: `#/app/${app.id}` + (key === 'records' ? '' : '/' + key),
    class: tab === key ? 'active' : '',
  }, label);
  return h('div', {},
    h('div', { class: 'row' },
      h('span', { style: 'font-size:1.8rem' }, app.icon),
      h('div', {}, h('h1', {}, app.name), h('div', { class: 'muted' }, app.description)),
      h('span', { class: 'spacer' }),
      h('span', { class: 'chip', title: 'このアプリでのあなたの権限' }, ROLE_LABELS[app.role]),
      h('button', {
        title: 'このアプリのレコード登録・更新・コメントを通知で受け取ります',
        onclick: async () => {
          await api('PUT', `/api/apps/${app.id}/follow`, { follow: !app.following });
          toast(app.following ? '通知を停止しました' : '通知を受け取ります'); route();
        },
      }, app.following ? '🔔 通知を受け取り中' : '🔕 通知を受け取る')),
    h('nav', { class: 'tabs' }, link('records', 'レコード一覧'),
      app.fields.some(f => f.type === 'date') && link('calendar', 'カレンダー'), link('summary', '集計'), app.role === 'manager' && link('settings', '設定')));
}

// ---------- アプリ一覧 ----------

async function viewHome() {
  const apps = await api('GET', '/api/apps');
  fill(main, 
    h('div', { class: 'row' },
      h('h1', {}, 'アプリ一覧'), h('span', { class: 'spacer' }),
      h('button', { class: 'primary', onclick: newAppDialog }, '＋ アプリを作成')),
    apps.length
      ? h('div', { class: 'cards' }, apps.map(a =>
          h('a', { class: 'card', href: `#/app/${a.id}` },
            h('div', { class: 'icon' }, a.icon),
            h('div', { class: 'name' }, a.name),
            h('div', { class: 'muted' }, a.description),
            h('div', { class: 'muted' }, `${a.record_count} 件`))))
      : h('div', { class: 'panel empty' }, 'アプリがまだありません。「＋ アプリを作成」から始めましょう。'));
}

function newAppDialog() {
  const name = h('input', { class: 'wide', required: true, maxLength: 100 });
  const desc = h('input', { class: 'wide', maxLength: 500 });
  const icon = h('input', { value: '📋', maxLength: 8, size: 4 });
  const tpl = h('select', {
    onchange: () => {
      const t = META.templates[tpl.value];
      if (t) { name.value = name.value || t.name; desc.value = desc.value || t.description; icon.value = t.icon; }
    },
  },
    h('option', { value: '' }, '空のアプリ（項目を自分で定義）'),
    Object.entries(META.templates).map(([k, t]) => h('option', { value: k }, `${t.icon} ${t.name}`)));
  openDialog({
    title: 'アプリを作成',
    submitLabel: '作成',
    body: [
      h('div', { class: 'form-field' }, h('label', {}, 'テンプレート'), tpl),
      h('div', { class: 'form-field' }, h('label', {}, 'アプリ名', h('span', { class: 'req' }, '*')), name),
      h('div', { class: 'form-field' }, h('label', {}, '説明'), desc),
      h('div', { class: 'form-field' }, h('label', {}, 'アイコン（絵文字）'), icon),
    ],
    onSubmit: async () => {
      const app = await api('POST', '/api/apps', {
        name: name.value, description: desc.value, icon: icon.value, template: tpl.value || null,
      });
      location.hash = app.fields.length ? `#/app/${app.id}` : `#/app/${app.id}/settings`;
    },
  });
}

// ---------- レコード一覧 ----------

function buildQuery(app, st) {
  const p = new URLSearchParams({ sort: st.sort, order: st.order, page: st.page });
  if (st.q) p.set('q', st.q);
  for (const [id, v] of Object.entries(st.filters)) if (v !== '') p.set('f_' + id, v);
  return p;
}

const stop = e => e.stopPropagation();
function fileLink(x) {
  const kb = Math.max(1, Math.round(x.size / 1024)).toLocaleString('ja-JP');
  return h('a', { class: 'file', href: `/api/files/${x.id}`, title: `${x.name}（${kb} KB）`, onclick: stop }, '📎 ' + x.name);
}

function cellContent(f, v, refs) {
  if (v == null || v === '') return '';
  if (f.type === 'file') return v.map(fileLink);
  if (f.type === 'lookup') {
    const ref = refs[f.id];
    return ref && ref.label != null
      ? h('a', { href: `#/app/${ref.app_id}/record/${ref.id}`, onclick: stop }, ref.label)
      : h('span', { class: 'muted' }, ref && ref.restricted ? '（権限なし）' : '（削除済み）');
  }
  if (f.type === 'multi') return v.map(x => h('span', { class: 'chip' }, x));
  if (f.type === 'select' || f.type === 'radio') return h('span', { class: 'chip' }, v);
  if (f.type === 'check') return v ? '✓' : '';
  if (f.type === 'number') return Number(v).toLocaleString('ja-JP');
  if (f.type === 'url' && /^https?:\/\//.test(v)) {
    return h('a', { href: v, target: '_blank', rel: 'noopener noreferrer', onclick: stop }, v);
  }
  return String(v);
}

async function viewRecords(app, root) {
  const st = listState[app.id] ||= { q: '', sort: 'id', order: 'desc', page: 1, filters: {} };
  const reload = () => viewRecords(app, root);
  const canEdit = app.role !== 'viewer';

  if (!app.fields.length) {
    fill(root, h('div', { class: 'panel empty' },
      'まだ項目がありません。', h('a', { href: `#/app/${app.id}/settings` }, '設定'), 'で項目を追加してください。'));
    return;
  }
  const res = await api('GET', `/api/apps/${app.id}/records?${buildQuery(app, st)}`);
  const pages = Math.max(1, Math.ceil(res.total / res.per_page));
  if (st.page > pages) { st.page = pages; return reload(); }

  const search = h('input', {
    type: 'search', placeholder: 'キーワード検索', value: st.q,
    onkeydown: e => { if (e.key === 'Enter') { st.q = search.value.trim(); st.page = 1; reload(); } },
    onsearch: () => { st.q = search.value.trim(); st.page = 1; reload(); },
  });
  const filters = app.fields.filter(f => [...CHOICE, 'check'].includes(f.type)).map(f => {
    const opts = f.type === 'check' ? [['1', 'オン'], ['0', 'オフ']] : f.options.map(o => [o, o]);
    return h('select', {
      'aria-label': f.label + 'で絞り込み',
      value: st.filters[f.id] ?? '',
      onchange: e => { st.filters[f.id] = e.target.value; st.page = 1; reload(); },
    }, h('option', { value: '' }, `${f.label}: すべて`), opts.map(([v, l]) => h('option', { value: v }, `${f.label}: ${l}`)));
  });
  const fileInput = h('input', {
    type: 'file', accept: '.csv,text/csv', hidden: true,
    onchange: async () => {
      if (!fileInput.files[0]) return;
      const fd = new FormData();
      fd.append('file', fileInput.files[0]);
      try {
        const r = await api('POST', `/api/apps/${app.id}/import`, fd);
        if (r.errors.length) {
          openDialog({
            title: `CSV 取り込み結果（${r.imported} 件登録、${r.errors.length} 件エラー）`,
            body: h('ul', {}, r.errors.map(e => h('li', {}, `${e.line} 行目: ${e.message}`))),
          });
        } else toast(`${r.imported} 件を取り込みました`);
        reload();
      } catch (err) { toast(err.message); }
    },
  });

  const th = (key, label) => h('th', {
    onclick: () => {
      if (st.sort === String(key)) st.order = st.order === 'asc' ? 'desc' : 'asc';
      else { st.sort = String(key); st.order = 'asc'; }
      reload();
    },
  }, label, st.sort === String(key) ? (st.order === 'asc' ? ' ▲' : ' ▼') : '');

  fill(root, 
    h('div', { class: 'row', style: 'margin-top:14px' },
      search, filters, h('span', { class: 'spacer' }),
      fileInput,
      canEdit && h('button', { onclick: () => fileInput.click() }, 'CSV 取り込み'),
      h('a', { class: 'btn', href: `/api/apps/${app.id}/export.csv?${buildQuery(app, st)}` }, 'CSV 書き出し'),
      canEdit && h('button', { class: 'primary', onclick: () => recordDialog(app, null, reload) }, '＋ レコード追加')),
    res.records.length
      ? h('div', { class: 'table-wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, th('id', 'No.'), app.fields.map(f => th(f.id, f.label)),
            h('th', { 'aria-label': 'コメント数', style: 'cursor:default' }, '💬'), th('updated_at', '更新日時'))),
          h('tbody', {}, res.records.map(r =>
            h('tr', { tabIndex: 0, onclick: () => recordDialog(app, r, reload),
                      onkeydown: e => { if (e.key === 'Enter') recordDialog(app, r, reload); } },
              h('td', { class: 'num' }, r.id),
              app.fields.map(f => h('td', { class: f.type === 'number' ? 'num' : '' }, cellContent(f, r.data[f.id], r.refs))),
              h('td', { class: 'muted' }, r.comment_count || ''),
              h('td', { class: 'muted' }, r.updated_at))))))
      : h('div', { class: 'panel empty' }, 'レコードがありません。'),
    h('div', { class: 'row', style: 'margin-top:12px' },
      h('span', { class: 'muted' }, `全 ${res.total} 件`), h('span', { class: 'spacer' }),
      h('button', { disabled: st.page <= 1, onclick: () => { st.page--; reload(); } }, '‹ 前へ'),
      h('span', { class: 'muted' }, `${st.page} / ${pages}`),
      h('button', { disabled: st.page >= pages, onclick: () => { st.page++; reload(); } }, '次へ ›')));
}

// 項目の型に応じた入力欄を作り、値を取り出す関数と一緒に返す。
function fieldInput(f, value, ref) {
  const name = 'f' + f.id;
  switch (f.type) {
    case 'file': {
      // 選んだ時点でアップロードし、レコード保存時に ID の一覧を送る
      let files = [...(value || [])];
      const listEl = h('div');
      const draw = () => fill(listEl, files.map(x => h('div', { class: 'row' }, fileLink(x),
        h('button', { type: 'button', class: 'icon danger', title: '添付を外す', onclick: () => { files = files.filter(y => y !== x); draw(); } }, '✕'))));
      const input = h('input', {
        type: 'file', multiple: true, 'aria-label': f.label,
        onchange: async () => {
          for (const file of input.files) {
            if (file.size > META.max_file_size) { toast(`${file.name}: ファイルが大きすぎます（上限 ${META.max_file_size / 1048576}MB）`); continue; }
            const fd = new FormData();
            fd.append('file', file);
            try { files.push(await api('POST', `/api/apps/${f.app_id}/fields/${f.id}/files`, fd)); draw(); }
            catch (err) { toast(`${file.name}: ${err.message}`); }
          }
          input.value = '';
        },
      });
      draw();
      return [h('div', {}, listEl, input), () => files.map(x => x.id)];
    }
    case 'lookup': {
      // 参照先アプリのレコードを検索して1件選ぶ
      let cur = ref ? { id: ref.id, label: ref.label ?? (ref.restricted ? '（権限なし）' : '（削除済み）') } : null;
      let timer;
      const box = h('div'), results = h('div', { class: 'lookup-results' });
      const search = h('input', {
        type: 'search', class: 'wide', placeholder: '参照先のレコードを検索', 'aria-label': f.label + 'を検索',
        oninput: () => { clearTimeout(timer); timer = setTimeout(find, 250); },
        onkeydown: e => { if (e.key === 'Enter') e.preventDefault(); },
      });
      async function find() {
        try {
          const rows = await api('GET', `/api/apps/${f.app_id}/fields/${f.id}/candidates?q=${encodeURIComponent(search.value)}`);
          fill(results, rows.length
            ? rows.map(r => h('button', { type: 'button', onclick: () => { cur = r; draw(); } }, r.label))
            : h('span', { class: 'muted' }, '該当するレコードがありません'));
        } catch (err) { fill(results, h('span', { class: 'muted' }, err.message)); }
      }
      function draw() {
        if (cur) {
          fill(box, h('div', { class: 'row' }, h('span', { class: 'chip' }, cur.label),
            h('button', { type: 'button', class: 'icon', title: '選択を解除', onclick: () => { cur = null; draw(); } }, '✕')));
        } else { fill(box, search, results); find(); }
      }
      draw();
      return [box, () => (cur ? cur.id : null)];
    }
    case 'textarea': { const el = h('textarea', { value: value ?? '' }); return [el, () => el.value]; }
    case 'select': {
      const el = h('select', { value: value ?? '' }, h('option', { value: '' }, '（未選択）'), f.options.map(o => h('option', { value: o }, o)));
      return [el, () => el.value];
    }
    case 'radio': {
      const el = h('div', { class: 'choices' }, f.options.map(o =>
        h('label', {}, h('input', { type: 'radio', name, value: o, checked: value === o }), ' ' + o)));
      return [el, () => el.querySelector('input:checked')?.value ?? ''];
    }
    case 'multi': {
      const el = h('div', { class: 'choices' }, f.options.map(o =>
        h('label', {}, h('input', { type: 'checkbox', value: o, checked: (value || []).includes(o) }), ' ' + o)));
      return [el, () => [...el.querySelectorAll('input:checked')].map(i => i.value)];
    }
    case 'check': {
      const box = h('input', { type: 'checkbox', checked: !!value });
      return [h('label', { style: 'font-weight:400' }, box, ' オン'), () => box.checked];
    }
    default: {
      const type = { number: 'number', date: 'date', time: 'time', url: 'url' }[f.type] || 'text';
      const el = h('input', { type, value: value ?? '', class: type === 'text' || type === 'url' ? 'wide' : '', step: type === 'number' ? 'any' : null });
      return [el, () => el.value];
    }
  }
}

// preset: 新規追加時にあらかじめ入れておく値（項目ID → 値）
function recordDialog(app, record, onDone, preset = {}) {
  const canEdit = app.role !== 'viewer'; // 閲覧のみの人には入力欄を無効にして表示だけする
  const getters = {}, errorEls = {};
  const body = app.fields.map(f => {
    const [el, get] = fieldInput(f, record ? record.data[f.id] : preset[f.id], record?.refs[f.id]);
    getters[f.id] = get;
    errorEls[f.id] = h('div', { class: 'error' });
    return h('div', { class: 'form-field' },
      h('div', { class: 'label' }, f.label, f.required && h('span', { class: 'req' }, '*')), el, errorEls[f.id]);
  });
  const dlg = openDialog({
    title: !record ? 'レコードを追加' : canEdit ? `レコード No.${record.id} を編集` : `レコード No.${record.id}`,
    body: [h('fieldset', { class: 'plain', disabled: !canEdit }, body),
      record && h('div', { class: 'muted' }, `作成: ${record.created_at} ${record.created_by}　更新: ${record.updated_at} ${record.updated_by}`),
      record && commentsSection(app, record, onDone)],
    extra: record && canEdit ? [h('button', {
      type: 'button', class: 'danger',
      onclick: async () => {
        if (!confirm('このレコードを削除しますか？')) return;
        await api('DELETE', `/api/apps/${app.id}/records/${record.id}`);
        dlg.close(); toast('削除しました'); onDone();
      },
    }, '削除')] : [],
    onSubmit: canEdit && (async () => {
      const data = {};
      for (const f of app.fields) { data[f.id] = getters[f.id](); errorEls[f.id].textContent = ''; }
      try {
        if (record) await api('PUT', `/api/apps/${app.id}/records/${record.id}`, { data });
        else await api('POST', `/api/apps/${app.id}/records`, { data });
      } catch (err) {
        for (const [id, msg] of Object.entries(err.fieldErrors || {})) if (errorEls[id]) errorEls[id].textContent = msg;
        throw err;
      }
      toast('保存しました'); onDone();
    }),
  });
  return dlg;
}

function commentsSection(app, record, onChange) {
  const url = `/api/apps/${app.id}/records/${record.id}/comments`;
  const listEl = h('div');
  const input = h('textarea', { placeholder: 'コメントを入力', 'aria-label': 'コメント', maxLength: 2000, style: 'min-height:60px' });
  async function load() {
    const items = await api('GET', url);
    fill(listEl, items.length ? items.map(c => h('div', { class: 'comment' },
      h('div', { class: 'row muted' }, h('b', {}, c.author), c.created_at, h('span', { class: 'spacer' }),
        (c.author_id === ME.id || app.role === 'manager') && h('button', {
          type: 'button', class: 'icon danger', title: 'コメントを削除',
          onclick: async () => {
            if (!confirm('このコメントを削除しますか？')) return;
            await api('DELETE', `${url}/${c.id}`); await load(); onChange();
          },
        }, '✕')),
      h('div', { class: 'comment-body' }, c.body)))
      : h('div', { class: 'muted' }, 'コメントはまだありません。'));
  }
  load().catch(err => toast(err.message));
  return h('div', { class: 'comments' },
    h('h2', {}, 'コメント'), listEl,
    app.role !== 'viewer' && input,
    app.role !== 'viewer' && h('div', { class: 'row' }, h('span', { class: 'muted' }, `${userName()} として投稿`), h('span', { class: 'spacer' }),
      h('button', {
        type: 'button',
        onclick: async () => {
          if (!input.value.trim()) return;
          try { await api('POST', url, { body: input.value }); input.value = ''; await load(); onChange(); }
          catch (err) { toast(err.message); }
        },
      }, 'コメントする')));
}

// ---------- カレンダー ----------

const calState = {}; // アプリごとの表示状態（基準にする日付項目・表示項目・表示中の年月）
// 選んだ日付項目と表示項目は、次に開いたときのためにブラウザへ保存する
function calPrefs(appId, value) {
  try {
    if (value) localStorage.setItem('cal-' + appId, JSON.stringify(value));
    else return JSON.parse(localStorage.getItem('cal-' + appId)) || {};
  } catch { /* 保存できない環境では毎回既定値になる */ }
  return {};
}
// カレンダーに出す文字列。項目の型に合わせて整形する
function plainText(f, r) {
  const v = r.data[f.id];
  if (v == null || v === '') return '';
  if (f.type === 'multi') return v.join('、');
  if (f.type === 'check') return v ? `✓ ${f.label}` : '';
  if (f.type === 'number') return Number(v).toLocaleString('ja-JP');
  if (f.type === 'lookup') { const ref = r.refs[f.id]; return ref.label ?? (ref.restricted ? '（権限なし）' : '（削除済み）'); }
  return String(v);
}
const pad = n => String(n).padStart(2, '0');
const ymd = (y, m, d) => `${y}-${pad(m + 1)}-${pad(d)}`;

async function viewCalendar(app, root) {
  const dateFields = app.fields.filter(f => f.type === 'date');
  if (!dateFields.length) {
    fill(root, h('div', { class: 'panel empty' }, 'カレンダー表示には日付項目が必要です。'));
    return;
  }
  const now = new Date();
  const titleFields = app.fields.filter(f => f.type !== 'file');
  const st = calState[app.id] ||= { ...calPrefs(app.id), year: now.getFullYear(), month: now.getMonth() };
  if (!dateFields.some(f => f.id === st.field)) st.field = dateFields[0].id;
  // 表示項目の既定は最初の1行文字列（無ければ先頭の項目）
  if (!titleFields.some(f => f.id === st.title)) st.title = (app.fields.find(f => f.type === 'text') || titleFields[0]).id;
  const choose = (key, value) => { st[key] = value; calPrefs(app.id, { field: st.field, title: st.title }); reload(); };
  const reload = () => viewCalendar(app, root);
  const canEdit = app.role !== 'viewer';
  const titleField = titleFields.find(f => f.id === st.title);
  const title = r => plainText(titleField, r) || `No.${r.id}`; // 値が空のレコードは番号で表示

  const lastDay = new Date(st.year, st.month + 1, 0).getDate();
  const q = new URLSearchParams({ per_page: 500, sort: 'id', order: 'asc' });
  q.set(`f_${st.field}_from`, ymd(st.year, st.month, 1));
  q.set(`f_${st.field}_to`, ymd(st.year, st.month, lastDay));
  // 件数が多くても全件出すため、ページを最後まで読む
  const records = [];
  for (let page = 1; ; page++) {
    q.set('page', page);
    const res = await api('GET', `/api/apps/${app.id}/records?${q}`);
    records.push(...res.records);
    if (records.length >= res.total || !res.records.length) break;
  }
  const byDay = {};
  for (const r of records) (byDay[r.data[st.field]] ||= []).push(r);

  const move = d => { const t = new Date(st.year, st.month + d, 1); st.year = t.getFullYear(); st.month = t.getMonth(); reload(); };
  const today = ymd(now.getFullYear(), now.getMonth(), now.getDate());
  const cells = [];
  for (let i = 0; i < new Date(st.year, st.month, 1).getDay(); i++) cells.push(h('div', { class: 'cal-cell blank' }));
  for (let d = 1; d <= lastDay; d++) {
    const key = ymd(st.year, st.month, d);
    const items = byDay[key] || [];
    const dow = new Date(st.year, st.month, d).getDay();
    const list = h('div', { class: 'cal-items' }, items.map(r => h('button', {
      class: 'cal-item', title: title(r), onclick: () => recordDialog(app, r, reload),
    }, title(r))));
    cells.push(h('div', { class: 'cal-cell' + (key === today ? ' today' : '') + (dow === 0 ? ' sun' : dow === 6 ? ' sat' : '') },
      h('div', { class: 'cal-day' }, h('span', {}, String(d)),
        canEdit && h('button', {
          class: 'cal-add', title: `${st.month + 1}月${d}日にレコードを追加`, 'aria-label': `${st.month + 1}月${d}日にレコードを追加`,
          onclick: () => recordDialog(app, null, reload, { [st.field]: key }),
        }, '＋')),
      list));
  }

  fill(root,
    h('div', { class: 'row', style: 'margin-top:14px' },
      h('button', { class: 'icon', 'aria-label': '前の月', onclick: () => move(-1) }, '‹'),
      h('strong', { class: 'cal-title' }, `${st.year}年${st.month + 1}月`),
      h('button', { class: 'icon', 'aria-label': '次の月', onclick: () => move(1) }, '›'),
      h('button', { onclick: () => { st.year = now.getFullYear(); st.month = now.getMonth(); reload(); } }, '今月'),
      h('span', { class: 'spacer' }),
      dateFields.length > 1 && h('label', {}, '日付項目 ', h('select', {
        value: String(st.field), onchange: e => choose('field', Number(e.target.value)),
      }, dateFields.map(f => h('option', { value: String(f.id) }, f.label)))),
      h('label', {}, '表示項目 ', h('select', {
        value: String(st.title), onchange: e => choose('title', Number(e.target.value)),
      }, titleFields.map(f => h('option', { value: String(f.id) }, f.label)))),
      h('span', { class: 'muted' }, `${records.length} 件`)),
    h('div', { class: 'cal' },
      ['日', '月', '火', '水', '木', '金', '土'].map((w, i) => h('div', { class: 'cal-head' + (i === 0 ? ' sun' : i === 6 ? ' sat' : '') }, w)),
      cells));
}

// ---------- 集計 ----------

function viewSummary(app, root) {
  const groupable = app.fields.filter(f => !['textarea', 'url', 'number', 'file'].includes(f.type));
  const numeric = app.fields.filter(f => f.type === 'number');
  if (!groupable.length) {
    fill(root, h('div', { class: 'panel empty' }, '集計に使える項目（メニュー・日付など）がありません。'));
    return;
  }
  const preferred = groupable.find(f => CHOICE.includes(f.type)) || groupable[0];
  const group = h('select', { value: String(preferred.id), onchange: draw }, groupable.map(f => h('option', { value: String(f.id) }, f.label)));
  const sum = h('select', { onchange: draw }, h('option', { value: '' }, '件数'), numeric.map(f => h('option', { value: String(f.id) }, `${f.label} の合計`)));
  const out = h('div', { style: 'margin-top:14px' });
  fill(root, h('div', { class: 'panel' },
    h('div', { class: 'row' }, h('label', {}, '分類する項目 ', group), h('label', {}, '集計する値 ', sum)), out));
  draw();

  async function draw() {
    const q = new URLSearchParams({ group: group.value });
    if (sum.value) q.set('sum', sum.value);
    const res = await api('GET', `/api/apps/${app.id}/aggregate?${q}`);
    const val = g => (sum.value ? g.sum : g.count);
    const max = Math.max(1, ...res.groups.map(val));
    fill(out, 
      res.groups.length ? res.groups.map(g => h('div', { class: 'bar-row' },
        h('div', {}, g.key),
        h('div', { class: 'bar-track' }, h('div', { class: 'bar', style: `width:${Math.max(0, val(g)) / max * 100}%` })),
        h('div', { class: 'bar-val' }, val(g).toLocaleString('ja-JP'))))
        : h('div', { class: 'empty' }, 'レコードがありません。'),
      h('div', { class: 'muted', style: 'margin-top:8px' }, `対象レコード: ${res.total} 件`));
  }
}

// ---------- 設定 ----------

async function viewSettings(app, root) {
  // 参照項目の設定用に、アプリ一覧と参照先アプリの項目を用意する
  const apps = await api('GET', '/api/apps');
  const targetFields = {};
  async function loadTarget(id) {
    if (id && !targetFields[id]) {
      const t = await api('GET', `/api/apps/${id}`).catch(() => ({ fields: [] }));
      targetFields[id] = t.fields.filter(x => META.lookup_display_types.includes(x.type));
    }
  }
  await Promise.all(app.fields.filter(f => f.type === 'lookup').map(f => loadTarget(f.config.app_id)));

  const name = h('input', { class: 'wide', value: app.name, required: true, maxLength: 100 });
  const desc = h('input', { class: 'wide', value: app.description, maxLength: 500 });
  const icon = h('input', { value: app.icon, maxLength: 8, size: 4 });
  const draft = app.fields.map(f => ({ ...f, config: { ...f.config }, optionsText: f.options.join('\n') }));
  const list = h('div');

  function renderFields() {
    fill(list, draft.length ? draft.map((f, i) => h('div', { class: 'field-row' },
      h('input', { value: f.label, placeholder: '項目名', 'aria-label': '項目名', maxLength: 100, oninput: e => { f.label = e.target.value; } }),
      h('select', {
        value: f.type, disabled: !!f.id, 'aria-label': '項目タイプ',
        title: f.id ? '保存済みの項目はタイプを変更できません' : '',
        onchange: e => { f.type = e.target.value; renderFields(); },
      }, Object.entries(META.field_types).map(([k, l]) => h('option', { value: k }, l))),
      h('label', {}, h('input', { type: 'checkbox', checked: f.required, onchange: e => { f.required = e.target.checked; } }), ' 必須'),
      h('div', { class: 'row', style: 'gap:4px' },
        h('button', { class: 'icon', title: '上へ', disabled: i === 0, onclick: () => move(i, -1) }, '↑'),
        h('button', { class: 'icon', title: '下へ', disabled: i === draft.length - 1, onclick: () => move(i, 1) }, '↓'),
        h('button', { class: 'icon danger', title: '項目を削除', onclick: () => { draft.splice(i, 1); renderFields(); } }, '✕')),
      CHOICE.includes(f.type) && h('div', { class: 'opts' },
        h('textarea', { value: f.optionsText, placeholder: '選択肢を1行に1つずつ入力', 'aria-label': '選択肢', oninput: e => { f.optionsText = e.target.value; } })),
      f.type === 'lookup' && h('div', { class: 'opts row' },
        h('label', {}, '参照先アプリ ', h('select', {
          value: String(f.config.app_id ?? ''), disabled: !!f.id,
          title: f.id ? '保存済みの項目は参照先アプリを変更できません' : '',
          onchange: async e => { f.config = { app_id: Number(e.target.value) || null }; await loadTarget(f.config.app_id); renderFields(); },
        }, h('option', { value: '' }, '（選択してください）'),
          apps.map(a => h('option', { value: String(a.id) }, `${a.icon} ${a.name}`)))),
        h('label', {}, '表示する項目 ', h('select', {
          value: String(f.config.field_id ?? ''),
          onchange: e => { f.config.field_id = Number(e.target.value) || null; },
        }, h('option', { value: '' }, '（選択してください）'),
          (targetFields[f.config.app_id] || []).map(x => h('option', { value: String(x.id) }, x.label)))))))
      : h('div', { class: 'muted', style: 'padding:12px 0' }, '項目がありません。「＋ 項目を追加」で追加してください。'));
  }
  function move(i, d) { [draft[i], draft[i + d]] = [draft[i + d], draft[i]]; renderFields(); }
  renderFields();

  // アクセス権: 全ユーザー向けの既定と、ユーザーごとの個別指定
  const [access, directory] = await Promise.all([api('GET', `/api/apps/${app.id}/access`), api('GET', '/api/users/directory')]);
  const members = access.members.map(m => ({ ...m }));
  const defaultRole = h('select', { value: access.default_role },
    h('option', { value: 'none' }, 'アクセス不可'), h('option', { value: 'viewer' }, '閲覧のみ'), h('option', { value: 'editor' }, '編集可'));
  const memberList = h('div');
  const addSelect = h('select', {
    'aria-label': 'ユーザーを追加',
    onchange: () => {
      const u = directory.find(x => x.id === Number(addSelect.value));
      if (u) members.push({ user_id: u.id, username: u.username, display_name: u.display_name, role: 'editor' });
      renderMembers();
    },
  });
  function renderMembers() {
    fill(memberList, members.length ? members.map((m, i) => h('div', { class: 'row member' },
      h('span', { class: 'member-name' }, `${m.display_name}（${m.username}）`),
      h('select', { value: m.role, 'aria-label': `${m.display_name} の権限`, onchange: e => { m.role = e.target.value; } },
        Object.entries(ROLE_LABELS).map(([k, l]) => h('option', { value: k }, l))),
      h('button', { class: 'icon danger', title: '個別指定を外す', onclick: () => { members.splice(i, 1); renderMembers(); } }, '✕')))
      : h('div', { class: 'muted', style: 'padding:8px 0' }, '個別に権限を指定したユーザーはいません。'));
    fill(addSelect, h('option', { value: '' }, '＋ ユーザーを追加…'),
      directory.filter(u => !members.some(m => m.user_id === u.id)).map(u => h('option', { value: String(u.id) }, `${u.display_name}（${u.username}）`)));
    addSelect.value = '';
  }
  renderMembers();

  fill(root, 
    h('div', { class: 'panel' },
      h('h2', {}, '項目の設定'),
      h('div', { class: 'muted' }, 'レコードに入力する項目を定義します。項目を削除すると、その項目に入力済みの値も削除されます。'),
      list,
      h('div', { class: 'row', style: 'margin-top:12px' },
        h('button', { onclick: () => { draft.push({ label: '', type: 'text', required: false, optionsText: '', config: {} }); renderFields(); list.lastChild.querySelector('input').focus(); } }, '＋ 項目を追加'),
        h('span', { class: 'spacer' }),
        h('button', {
          class: 'primary',
          onclick: async () => {
            const removed = app.fields.filter(f => !draft.some(d => d.id === f.id));
            if (removed.length && !confirm(`項目「${removed.map(f => f.label).join('、')}」と入力済みの値を削除します。よろしいですか？`)) return;
            try {
              await api('PUT', `/api/apps/${app.id}/fields`, {
                fields: draft.map(f => ({ id: f.id ?? null, label: f.label, type: f.type, required: f.required, options: f.optionsText.split('\n'), config: f.config })),
              });
              toast('項目を保存しました'); route();
            } catch (err) { toast(err.message); }
          },
        }, '項目を保存'))),
    h('div', { class: 'panel' },
      h('h2', {}, 'アクセス権'),
      h('div', { class: 'muted' }, '閲覧のみ: レコードを見られる　／　編集可: レコードの登録・編集・削除とコメント　／　管理: 項目・アクセス権の設定とアプリの削除。システム管理者は常に管理できます。'),
      h('div', { class: 'form-field', style: 'margin-top:12px' }, h('label', {}, '全ユーザー（個別指定のない人）'), defaultRole),
      h('div', { class: 'label', style: 'font-weight:600;font-size:.9rem' }, 'ユーザーごとの指定'),
      memberList,
      h('div', { class: 'row', style: 'margin-top:8px' }, addSelect, h('span', { class: 'spacer' }),
        h('button', {
          class: 'primary',
          onclick: async () => {
            try {
              await api('PUT', `/api/apps/${app.id}/access`, {
                default_role: defaultRole.value, members: members.map(m => ({ user_id: m.user_id, role: m.role })),
              });
              toast('アクセス権を保存しました'); route();
            } catch (err) { toast(err.message); }
          },
        }, 'アクセス権を保存'))),
    h('div', { class: 'panel' },
      h('h2', {}, 'アプリの基本情報'),
      h('div', { class: 'form-field' }, h('label', {}, 'アプリ名'), name),
      h('div', { class: 'form-field' }, h('label', {}, '説明'), desc),
      h('div', { class: 'form-field' }, h('label', {}, 'アイコン（絵文字）'), icon),
      h('div', { class: 'row' },
        h('button', {
          class: 'danger',
          onclick: async () => {
            if (!confirm(`アプリ「${app.name}」とすべてのレコードを削除します。元に戻せません。よろしいですか？`)) return;
            await api('DELETE', `/api/apps/${app.id}`);
            toast('アプリを削除しました'); location.hash = '#/';
          },
        }, 'アプリを削除'),
        h('span', { class: 'spacer' }),
        h('button', {
          class: 'primary',
          onclick: async () => {
            try {
              await api('PUT', `/api/apps/${app.id}`, { name: name.value, description: desc.value, icon: icon.value });
              toast('保存しました'); route();
            } catch (err) { toast(err.message); }
          },
        }, '基本情報を保存'))));
}
