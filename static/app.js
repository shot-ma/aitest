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
    let tab = ['settings', 'summary'].includes(m[2]) ? m[2] : 'records';
    if (tab === 'settings' && app.role !== 'manager') tab = 'records';
    const content = h('div');
    fill(main, appHeader(app, tab), content);
    if (tab === 'settings') await viewSettings(app, content);
    else if (tab === 'summary') viewSummary(app, content);
    else {
      // 表示するビューを決める。URL に指定がなければ先頭のビュー（全員共通の既定）を使う
      let view = null, mode = null;
      if (m[2] === 'view') view = app.views.find(v => v.id === Number(m[3])) || null;
      else if (m[2] === 'calendar') mode = 'calendar';
      else if (m[2] === 'list' || m[2] === 'record') mode = 'list';
      if (!view && !mode) view = app.views[0] || null;
      mode = view ? (view.type === 'calendar' ? 'calendar' : 'list') : mode || 'list';
      const body = h('div');
      fill(content, viewBar(app, view, mode), body);
      const render = () => (mode === 'calendar' ? viewCalendar(app, body, view) : viewRecords(app, body, view));
      await render();
      // #/app/1/record/5 の形式（通知や参照項目のリンク）ならそのレコードを開く
      if (m[2] === 'record' && m[3]) {
        const rec = await api('GET', `/api/apps/${app.id}/records/${m[3]}`);
        const hash = location.hash;
        // 閉じたら通常の URL に戻す（同じリンクをもう一度押しても開けるように）
        recordDialog(app, rec, render).addEventListener('close', () => {
          if (location.hash === hash) history.replaceState(null, '', `#/app/${app.id}/list`);
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
    h('nav', { class: 'tabs' }, link('records', 'レコード'), link('summary', '集計'), app.role === 'manager' && link('settings', '設定')));
}

// ---------- アプリ一覧 ----------

async function viewHome() {
  const [apps, dash] = await Promise.all([api('GET', '/api/apps'), api('GET', '/api/dashboard')]);
  fill(main,
    h('div', { class: 'row' },
      h('h1', {}, 'ダッシュボード'),
      dash.custom && h('span', { class: 'chip', title: '既定の配置から変更しています' }, '自分用の配置'),
      h('span', { class: 'spacer' }),
      ME.role === 'admin' && h('button', { onclick: () => editDashboard(true) }, '全員の既定を編集'),
      dash.custom && h('button', {
        onclick: async () => {
          if (!confirm('自分用の配置を消して、管理者が決めた既定の配置に戻しますか？')) return;
          await api('DELETE', '/api/dashboard'); toast('既定の配置に戻しました'); route();
        },
      }, '既定に戻す'),
      h('button', { class: 'primary', onclick: () => editDashboard(false) }, 'レイアウトを編集')),
    renderDashboard(dash),
    h('div', { class: 'row', style: 'margin-top:32px' },
      h('h2', { style: 'margin:0' }, 'アプリ一覧'), h('span', { class: 'spacer' }),
      h('button', { onclick: newAppDialog }, '＋ アプリを作成')),
    apps.length
      ? h('div', { class: 'cards' }, apps.map(a =>
          h('a', { class: 'card', href: `#/app/${a.id}` },
            h('div', { class: 'icon' }, a.icon),
            h('div', { class: 'name' }, a.name),
            h('div', { class: 'muted' }, a.description),
            h('div', { class: 'muted' }, `${a.record_count} 件`))))
      : h('div', { class: 'panel empty' }, 'アプリがまだありません。「＋ アプリを作成」から始めましょう。'));
}

// ---------- ダッシュボード ----------

const viewLink = wd => `#/app/${wd.app_id}/` + (wd.view_id ? `view/${wd.view_id}` : wd.mode === 'calendar' ? 'calendar' : 'list');
const placeStyle = wd => `grid-column:${wd.x + 1} / span ${wd.w};grid-row:${wd.y + 1} / span ${wd.h};--h:${wd.h}`;

function renderDashboard(dash) {
  if (!dash.widgets.length) {
    return h('div', { class: 'panel empty' }, 'ダッシュボードにビューがありません。「レイアウトを編集」から、よく使う一覧やカレンダーを並べられます。');
  }
  // 小さい画面では1列に並ぶので、上から・左からの順に並べておく
  const widgets = [...dash.widgets].sort((a, b) => a.y - b.y || a.x - b.x);
  const rows = Math.max(...widgets.map(wd => wd.y + wd.h));
  const apps = {}; // 同じアプリの情報は1回だけ読む
  const getApp = id => (apps[id] ||= api('GET', `/api/apps/${id}`));
  return h('div', { class: 'dash', style: `grid-template-rows:repeat(${rows}, var(--cell-h))` }, widgets.map(wd => {
    const body = h('div', { class: 'widget-body' }, h('div', { class: 'muted' }, '読み込み中…'));
    (async () => {
      const app = await getApp(wd.app_id);
      const view = wd.view_id ? app.views.find(v => v.id === wd.view_id) : null;
      if (wd.mode === 'calendar') await viewCalendar(app, body, view, true);
      else await widgetList(app, body, view, wd);
    })().catch(err => fill(body, h('div', { class: 'muted' }, err.message)));
    return h('section', { class: 'widget', style: placeStyle(wd), 'aria-label': `${wd.app_name} ${wd.view_name}` },
      h('div', { class: 'widget-head' },
        h('span', {}, wd.app_icon), h('b', {}, wd.app_name), h('span', { class: 'muted' }, wd.view_name),
        h('span', { class: 'spacer' }), h('a', { href: viewLink(wd) }, '開く ›')),
      body);
  }));
}

// ダッシュボード用の一覧。ビューの表示項目・並び順・絞り込みに従い、高さに合わせた件数を出す
async function widgetList(app, root, view, wd) {
  const cfg = view ? view.config : null;
  const cols = cfg && cfg.columns.length ? app.fields.filter(f => cfg.columns.includes(f.id)) : app.fields;
  const q = new URLSearchParams({ sort: cfg ? cfg.sort : 'id', order: cfg ? cfg.order : 'desc', per_page: wd.h * 4 });
  if (cfg) for (const [id, v] of Object.entries(cfg.filters)) q.set('f_' + id, v);
  const res = await api('GET', `/api/apps/${app.id}/records?${q}`);
  const reload = () => widgetList(app, root, view, wd);
  fill(root,
    res.records.length
      ? h('table', { class: 'widget-table' },
          h('thead', {}, h('tr', {}, h('th', {}, 'No.'), cols.map(f => h('th', {}, f.label)))),
          h('tbody', {}, res.records.map(r => h('tr', {
            tabIndex: 0, onclick: () => recordDialog(app, r, reload),
            onkeydown: e => { if (e.key === 'Enter') recordDialog(app, r, reload); },
          }, h('td', { class: 'num' }, r.id), cols.map(f => h('td', { class: f.type === 'number' ? 'num' : '' }, cellContent(f, r.data[f.id], r.refs)))))))
      : h('div', { class: 'muted' }, 'レコードがありません。'),
    h('div', { class: 'muted widget-foot' }, `全 ${res.total} 件` + (res.total > res.records.length ? `（先頭 ${res.records.length} 件を表示）` : '')));
}

// 配置の編集画面。forDefault が true なら、管理者が全員の既定を編集する
async function editDashboard(forDefault) {
  const [dash, options] = await Promise.all([
    api('GET', forDefault ? '/api/dashboard/default' : '/api/dashboard'), api('GET', '/api/dashboard/options')]);
  const COLS = dash.cols, ROWS = dash.rows;
  let items = dash.widgets.map(wd => ({ ...wd }));
  let dragging = null;

  const fits = (item, x, y, w, hh) => x >= 0 && y >= 0 && w >= 1 && hh >= 1 && x + w <= COLS && y + hh <= ROWS &&
    !items.some(o => o !== item && x < o.x + o.w && o.x < x + w && y < o.y + o.h && o.y < y + hh);
  function change(item, x, y, w, hh) {
    if (!fits(item, x, y, w, hh)) return toast('その位置・大きさにはできません（マス目からはみ出すか、ほかのビューと重なります）');
    Object.assign(item, { x, y, w, h: hh });
    draw();
  }
  const grid = h('div', { class: 'dash editing' });
  function draw() {
    const cells = [];
    for (let y = 0; y < ROWS; y++) for (let x = 0; x < COLS; x++) {
      cells.push(h('button', {
        class: 'dash-cell', style: `grid-column:${x + 1};grid-row:${y + 1}`,
        title: 'ここにビューを追加', 'aria-label': `${y + 1}行目・${x + 1}列目にビューを追加`,
        onclick: () => addWidget(x, y),
        ondragover: e => { if (dragging && fits(dragging, x, y, dragging.w, dragging.h)) e.preventDefault(); },
        ondrop: e => { e.preventDefault(); if (dragging) change(dragging, x, y, dragging.w, dragging.h); dragging = null; },
      }, '＋'));
    }
    const btn = (label, title, fn) => h('button', { class: 'mini', title, 'aria-label': title, onclick: fn }, label);
    fill(grid, cells, items.map(it => h('div', {
      class: 'widget edit', style: placeStyle(it), draggable: true,
      ondragstart: e => { dragging = it; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', ''); },
      ondragend: () => { dragging = null; },
    },
      h('div', { class: 'widget-head' }, h('span', {}, it.app_icon), h('b', {}, it.app_name)),
      h('div', { class: 'muted' }, `${it.mode === 'calendar' ? '📅' : '📋'} ${it.view_name}`),
      h('div', { class: 'muted' }, `幅 ${it.w} × 高さ ${it.h}`),
      h('div', { class: 'widget-tools' },
        btn('←', '左へ移動', () => change(it, it.x - 1, it.y, it.w, it.h)),
        btn('↑', '上へ移動', () => change(it, it.x, it.y - 1, it.w, it.h)),
        btn('↓', '下へ移動', () => change(it, it.x, it.y + 1, it.w, it.h)),
        btn('→', '右へ移動', () => change(it, it.x + 1, it.y, it.w, it.h)),
        btn('幅−', '幅を狭く', () => change(it, it.x, it.y, it.w - 1, it.h)),
        btn('幅＋', '幅を広く', () => change(it, it.x, it.y, it.w + 1, it.h)),
        btn('高−', '高さを低く', () => change(it, it.x, it.y, it.w, it.h - 1)),
        btn('高＋', '高さを高く', () => change(it, it.x, it.y, it.w, it.h + 1)),
        btn('✕', 'ダッシュボードから外す', () => { items = items.filter(o => o !== it); draw(); })))));
  }

  function addWidget(x, y) {
    if (!options.length) return toast('置けるアプリがありません');
    const choices = [];
    const select = h('select', { class: 'wide', 'aria-label': '表示するビュー' }, options.map(o => h('optgroup', { label: `${o.icon} ${o.name}` },
      [{ view_id: null, mode: 'list', name: 'すべての項目（標準）' },
       ...(o.has_date ? [{ view_id: null, mode: 'calendar', name: 'カレンダー（標準）' }] : []),
       ...o.views.map(v => ({ view_id: v.id, mode: v.type === 'calendar' ? 'calendar' : 'list', name: v.name }))]
        .map(v => {
          choices.push({ app_id: o.app_id, app_name: o.name, app_icon: o.icon, view_id: v.view_id, mode: v.mode, view_name: v.name });
          return h('option', { value: String(choices.length - 1) }, `${v.mode === 'calendar' ? '📅' : '📋'} ${v.name}`);
        }))));
    const size = (max, def) => h('select', {}, Array.from({ length: max }, (_, i) => h('option', { value: String(i + 1), selected: i + 1 === def }, String(i + 1))));
    const w = size(COLS - x, Math.min(2, COLS - x)), hh = size(ROWS - y, Math.min(2, ROWS - y));
    openDialog({
      title: `${y + 1}行目・${x + 1}列目にビューを追加`, submitLabel: '追加',
      body: [
        h('div', { class: 'form-field' }, h('label', {}, '表示するビュー'), select),
        h('div', { class: 'form-field row' }, h('label', {}, '幅（マス） ', w), h('label', {}, '高さ（マス） ', hh)),
        h('div', { class: 'muted' }, `ダッシュボードは横 ${COLS} マス × 縦 ${ROWS} マスです。`),
      ],
      onSubmit: async () => {
        const item = { ...choices[Number(select.value)], x, y, w: Number(w.value), h: Number(hh.value) };
        if (!fits(item, x, y, item.w, item.h)) throw new Error('その大きさでは、マス目からはみ出すか、ほかのビューと重なります');
        items.push(item); draw();
      },
    });
  }

  draw();
  fill(main,
    h('div', { class: 'row' },
      h('h1', {}, forDefault ? '全員の既定ダッシュボードを編集' : 'ダッシュボードを編集'),
      h('span', { class: 'spacer' }),
      h('button', { onclick: () => route() }, 'キャンセル'),
      h('button', {
        class: 'primary',
        onclick: async () => {
          try {
            await api('PUT', forDefault ? '/api/dashboard/default' : '/api/dashboard', {
              widgets: items.map(({ app_id, view_id, mode, x, y, w, h: hh }) => ({ app_id, view_id, mode, x, y, w, h: hh })),
            });
            toast('ダッシュボードを保存しました'); route();
          } catch (err) { toast(err.message); }
        },
      }, '保存')),
    h('p', { class: 'muted' }, forDefault
      ? '自分用に配置を変更していない全員に表示されます（各自が見る権限のないビューは、その人には表示されません）。'
      : '空いているマスの「＋」でビューを追加し、ビューはドラッグか矢印ボタンで移動、幅・高さボタンで大きさを変えられます。'),
    h('div', { class: 'dash-edit-wrap' }, grid));
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

// ---------- ビュー（一覧・カレンダーの表示設定に名前を付けたもの） ----------

const go = hash => { if (location.hash === hash) route(); else location.hash = hash; };

// ビューの切り替え欄。アプリの管理者には追加・編集ボタンも出す
function viewBar(app, view, mode) {
  const manager = app.role === 'manager';
  const current = view ? `view/${view.id}` : mode;
  return h('div', { class: 'row viewbar' },
    h('label', {}, 'ビュー ', h('select', {
      value: current, onchange: e => { location.hash = `#/app/${app.id}/${e.target.value}`; },
    },
      app.views.map((v, i) => h('option', { value: `view/${v.id}` }, `${v.type === 'calendar' ? '📅' : '📋'} ${v.name}${i === 0 ? '（既定）' : ''}`)),
      h('option', { value: 'list' }, '📋 すべての項目（標準）'),
      app.fields.some(f => f.type === 'date') && h('option', { value: 'calendar' }, '📅 カレンダー（標準）'))),
    manager && view && h('span', { class: 'chip' }, view.access === 'restricted' ? `🔒 ${view.user_ids.length} 人に公開` : '全員に公開'),
    h('span', { class: 'spacer' }),
    manager && view && h('button', { onclick: () => viewDialog(app, view) }, 'このビューを編集'),
    manager && h('button', { onclick: () => viewDialog(app, null, mode) }, '＋ ビューを追加'));
}

async function viewDialog(app, view, mode) {
  const directory = await api('GET', '/api/users/directory');
  const cfg = view ? view.config : {};
  const dateFields = app.fields.filter(f => f.type === 'date');
  const check = (label, checked) => { const box = h('input', { type: 'checkbox', checked }); return [box, h('label', {}, box, ' ' + label)]; };
  const fieldOptions = list => list.map(f => h('option', { value: String(f.id) }, f.label));

  const name = h('input', { class: 'wide', required: true, maxLength: 50, value: view ? view.name : '', placeholder: '例: 未対応の一覧' });
  const type = h('select', { value: view ? view.type : mode === 'calendar' && dateFields.length ? 'calendar' : 'table', disabled: !!view, onchange: sync },
    h('option', { value: 'table' }, '📋 一覧（表形式）'), dateFields.length > 0 && h('option', { value: 'calendar' }, '📅 カレンダー'));
  // 一覧用: 表示する項目と並び順
  const columns = app.fields.map(f => [f, ...check(f.label, view && view.type === 'table' ? cfg.columns.includes(f.id) : true)]);
  const sort = h('select', { value: cfg.sort || 'id' },
    h('option', { value: 'id' }, 'No.'), h('option', { value: 'updated_at' }, '更新日時'), h('option', { value: 'created_at' }, '作成日時'),
    fieldOptions(app.fields.filter(f => f.type !== 'file')));
  const order = h('select', { value: cfg.order || 'desc' }, h('option', { value: 'asc' }, '昇順'), h('option', { value: 'desc' }, '降順'));
  // カレンダー用: 基準の日付項目と表示項目
  const titleFields = app.fields.filter(f => f.type !== 'file');
  const dateField = h('select', { value: String(cfg.date_field ?? dateFields[0]?.id ?? '') }, fieldOptions(dateFields));
  const titleField = h('select', { value: String(cfg.title_field ?? (app.fields.find(f => f.type === 'text') || titleFields[0])?.id ?? '') }, fieldOptions(titleFields));
  // 共通: 絞り込み条件
  const filters = app.fields.filter(f => [...CHOICE, 'check'].includes(f.type)).map(f => {
    const opts = f.type === 'check' ? [['1', 'オン'], ['0', 'オフ']] : f.options.map(o => [o, o]);
    return [f, h('select', { value: (cfg.filters || {})[f.id] ?? '', 'aria-label': f.label + 'の絞り込み' },
      h('option', { value: '' }, `${f.label}: 指定なし`), opts.map(([v, l]) => h('option', { value: v }, `${f.label}: ${l}`)))];
  });
  // 公開範囲
  const access = h('select', { value: view ? view.access : 'all', onchange: sync },
    h('option', { value: 'all' }, 'このアプリを見られる全員'), h('option', { value: 'restricted' }, '指定したユーザーだけ'));
  const users = directory.map(u => [u, ...check(`${u.display_name}（${u.username}）`, !!view && view.user_ids.includes(u.id))]);
  const [makeDefault, makeDefaultLabel] = check('既定のビューにする（一覧の先頭に移動し、全員が最初に見るビューになります）', !!view && app.views[0].id === view.id);

  const tableBox = h('div', {},
    h('div', { class: 'form-field' }, h('div', { class: 'label' }, '表示する項目'), h('div', { class: 'choices' }, columns.map(c => c[2]))),
    h('div', { class: 'form-field' }, h('div', { class: 'label' }, '並び順'), h('div', { class: 'row' }, sort, order)));
  const calBox = h('div', {},
    h('div', { class: 'form-field' }, h('label', {}, '基準にする日付項目'), dateField),
    h('div', { class: 'form-field' }, h('label', {}, 'カレンダーに表示する項目'), titleField));
  const usersBox = h('div', { class: 'form-field' },
    h('div', { class: 'choices' }, users.map(u => u[2])),
    h('div', { class: 'muted' }, 'アプリの管理権限を持つ人は、指定しなくてもすべてのビューを使えます。'));
  function sync() {
    tableBox.hidden = type.value !== 'table';
    calBox.hidden = type.value !== 'calendar';
    usersBox.hidden = access.value !== 'restricted';
  }
  sync();

  const clearState = () => { for (const s of [listState, calState]) for (const k of Object.keys(s)) delete s[k]; };
  const dlg = openDialog({
    title: view ? `ビュー「${view.name}」を編集` : 'ビューを追加',
    body: [
      h('div', { class: 'form-field' }, h('label', {}, 'ビュー名'), name),
      h('div', { class: 'form-field' }, h('label', {}, '種類'), type),
      tableBox, calBox,
      filters.length > 0 && h('div', { class: 'form-field' }, h('div', { class: 'label' }, '絞り込み条件'), h('div', { class: 'row' }, filters.map(f => f[1]))),
      h('div', { class: 'form-field' }, h('label', {}, 'このビューを使える人'), access),
      usersBox,
      h('div', { class: 'form-field' }, makeDefaultLabel),
    ],
    extra: view ? [h('button', {
      type: 'button', class: 'danger',
      onclick: async () => {
        if (!confirm(`ビュー「${view.name}」を削除しますか？（レコードは削除されません）`)) return;
        await api('DELETE', `/api/apps/${app.id}/views/${view.id}`);
        dlg.close(); clearState(); toast('ビューを削除しました'); go(`#/app/${app.id}`);
      },
    }, '削除')] : [],
    onSubmit: async () => {
      const filterValues = Object.fromEntries(filters.filter(f => f[1].value !== '').map(f => [f[0].id, f[1].value]));
      const body = {
        name: name.value, type: type.value, access: access.value,
        user_ids: users.filter(u => u[1].checked).map(u => u[0].id),
        // 既定のチェックは「先頭へ移動」の指示。すでに先頭なら送らない
        make_default: makeDefault.checked && !(view && app.views[0].id === view.id),
        config: type.value === 'calendar'
          ? { date_field: Number(dateField.value), title_field: Number(titleField.value), filters: filterValues }
          : { columns: columns.filter(c => c[1].checked).map(c => c[0].id), sort: sort.value, order: order.value, filters: filterValues },
      };
      const saved = view
        ? await api('PUT', `/api/apps/${app.id}/views/${view.id}`, body)
        : await api('POST', `/api/apps/${app.id}/views`, body);
      clearState(); toast('ビューを保存しました'); go(`#/app/${app.id}/view/${saved.id}`);
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

async function viewRecords(app, root, view) {
  // ビューの並び順・絞り込みを初期値にする（画面上での変更は一時的なもの）
  const cfg = view ? view.config : null;
  const st = listState[`${app.id}:${view ? view.id : 0}`] ||= {
    q: '', page: 1, sort: cfg ? cfg.sort : 'id', order: cfg ? cfg.order : 'desc', filters: cfg ? { ...cfg.filters } : {},
  };
  const cols = cfg && cfg.columns.length ? app.fields.filter(f => cfg.columns.includes(f.id)) : app.fields;
  const reload = () => viewRecords(app, root, view);
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
          h('thead', {}, h('tr', {}, th('id', 'No.'), cols.map(f => th(f.id, f.label)),
            h('th', { 'aria-label': 'コメント数', style: 'cursor:default' }, '💬'), th('updated_at', '更新日時'))),
          h('tbody', {}, res.records.map(r =>
            h('tr', { tabIndex: 0, onclick: () => recordDialog(app, r, reload),
                      onkeydown: e => { if (e.key === 'Enter') recordDialog(app, r, reload); } },
              h('td', { class: 'num' }, r.id),
              cols.map(f => h('td', { class: f.type === 'number' ? 'num' : '' }, cellContent(f, r.data[f.id], r.refs))),
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
    const locked = canEdit && f.editable === false; // 見えるが変更できない項目
    return h('div', { class: 'form-field' },
      h('div', { class: 'label' }, f.label, f.required && !locked && h('span', { class: 'req' }, '*'),
        locked && h('span', { class: 'muted readonly-note' }, '（閲覧のみ）')),
      locked ? h('fieldset', { class: 'plain', disabled: true }, el) : el, errorEls[f.id]);
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
      c.body && h('div', { class: 'comment-body' }, c.body),
      c.files.length > 0 && h('div', { class: 'comment-files' }, c.files.map(fileLink))))
      : h('div', { class: 'muted' }, 'コメントはまだありません。'));
  }
  load().catch(err => toast(err.message));
  // 添付ファイルは選んだ時点でアップロードし、投稿時に ID の一覧を送る
  let pending = [];
  const pendingEl = h('div', { class: 'comment-files' });
  const drawPending = () => fill(pendingEl, pending.map(x => h('span', { class: 'pending-file' }, fileLink(x),
    h('button', { type: 'button', class: 'icon danger', title: '添付を外す', 'aria-label': `${x.name} を外す`, onclick: () => { pending = pending.filter(y => y !== x); drawPending(); } }, '✕'))));
  const fileInput = h('input', {
    type: 'file', multiple: true, hidden: true, 'aria-label': 'コメントにファイルを添付',
    onchange: async () => {
      for (const file of fileInput.files) {
        if (pending.length >= 10) { toast('添付できるファイルは10件までです'); break; }
        if (file.size > META.max_file_size) { toast(`${file.name}: ファイルが大きすぎます（上限 ${META.max_file_size / 1048576}MB）`); continue; }
        const fd = new FormData();
        fd.append('file', file);
        try { pending.push(await api('POST', `/api/apps/${app.id}/records/${record.id}/comment-files`, fd)); drawPending(); }
        catch (err) { toast(`${file.name}: ${err.message}`); }
      }
      fileInput.value = '';
    },
  });
  return h('div', { class: 'comments' },
    h('h2', {}, 'コメント'), listEl,
    app.role !== 'viewer' && input,
    app.role !== 'viewer' && pendingEl,
    app.role !== 'viewer' && h('div', { class: 'row' }, h('span', { class: 'muted' }, `${userName()} として投稿`), h('span', { class: 'spacer' }),
      fileInput,
      h('button', { type: 'button', onclick: () => fileInput.click() }, '📎 ファイルを添付'),
      h('button', {
        type: 'button',
        onclick: async () => {
          if (!input.value.trim() && !pending.length) return;
          try {
            await api('POST', url, { body: input.value, file_ids: pending.map(x => x.id) });
            input.value = ''; pending = []; drawPending(); await load(); onChange();
          } catch (err) { toast(err.message); }
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

// compact: ダッシュボードの中に表示するとき（日付項目・表示項目の切り替えを出さない）
async function viewCalendar(app, root, view, compact = false) {
  const dateFields = app.fields.filter(f => f.type === 'date');
  if (!dateFields.length) {
    fill(root, h('div', { class: 'panel empty' }, 'カレンダー表示には日付項目が必要です。'));
    return;
  }
  const now = new Date();
  const titleFields = app.fields.filter(f => f.type !== 'file');
  const st = calState[`${app.id}:${view ? view.id : 0}`] ||= {
    // 保存済みビューはその設定、標準カレンダーは前回ブラウザで選んだ項目から始める
    ...(view ? { field: view.config.date_field, title: view.config.title_field } : calPrefs(app.id)),
    year: now.getFullYear(), month: now.getMonth(),
  };
  if (!dateFields.some(f => f.id === st.field)) st.field = dateFields[0].id;
  // 表示項目の既定は最初の1行文字列（無ければ先頭の項目）
  if (!titleFields.some(f => f.id === st.title)) st.title = (app.fields.find(f => f.type === 'text') || titleFields[0]).id;
  const choose = (key, value) => { st[key] = value; if (!view) calPrefs(app.id, { field: st.field, title: st.title }); reload(); };
  const reload = () => viewCalendar(app, root, view, compact);
  const canEdit = app.role !== 'viewer';
  const titleField = titleFields.find(f => f.id === st.title);
  const title = r => plainText(titleField, r) || `No.${r.id}`; // 値が空のレコードは番号で表示

  const lastDay = new Date(st.year, st.month + 1, 0).getDate();
  const q = new URLSearchParams({ per_page: 500, sort: 'id', order: 'asc' });
  if (view) for (const [id, v] of Object.entries(view.config.filters)) q.set('f_' + id, v);
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
  // 祝日と独自の休日（日付 → 一覧）
  const holidays = {};
  for (const x of await loadHolidays(st.year)) (holidays[x.date] ||= []).push(x);

  const move = d => { const t = new Date(st.year, st.month + d, 1); st.year = t.getFullYear(); st.month = t.getMonth(); reload(); };
  const today = ymd(now.getFullYear(), now.getMonth(), now.getDate());
  const cells = [];
  for (let i = 0; i < new Date(st.year, st.month, 1).getDay(); i++) cells.push(h('div', { class: 'cal-cell blank' }));
  for (let d = 1; d <= lastDay; d++) {
    const key = ymd(st.year, st.month, d);
    const items = byDay[key] || [];
    const dow = new Date(st.year, st.month, d).getDay();
    const hols = holidays[key] || [];
    const list = h('div', { class: 'cal-items' }, items.map(r => h('button', {
      class: 'cal-item', title: title(r), onclick: () => recordDialog(app, r, reload),
    }, title(r))));
    cells.push(h('div', { class: 'cal-cell' + (key === today ? ' today' : '') + (hols.length || dow === 0 ? ' sun' : dow === 6 ? ' sat' : '') + (hols.length ? ' holiday' : '') },
      h('div', { class: 'cal-day' }, h('span', {}, String(d)),
        hols.length > 0 && h('span', { class: 'cal-hol', title: hols.map(x => x.name + HOLIDAY_KINDS[x.kind].suffix).join('、') },
          hols.map(x => x.name).join('・')),
        canEdit && dateFields.find(f => f.id === st.field).editable !== false && h('button', {
          class: 'cal-add', title: `${st.month + 1}月${d}日にレコードを追加`, 'aria-label': `${st.month + 1}月${d}日にレコードを追加`,
          onclick: () => recordDialog(app, null, reload, { [st.field]: key }),
        }, '＋')),
      list));
  }

  fill(root,
    h('div', { class: 'row', style: compact ? '' : 'margin-top:14px' },
      h('button', { class: 'icon', 'aria-label': '前の月', onclick: () => move(-1) }, '‹'),
      h('strong', { class: 'cal-title' }, `${st.year}年${st.month + 1}月`),
      h('button', { class: 'icon', 'aria-label': '次の月', onclick: () => move(1) }, '›'),
      h('button', { onclick: () => { st.year = now.getFullYear(); st.month = now.getMonth(); reload(); } }, '今月'),
      h('span', { class: 'spacer' }),
      !compact && dateFields.length > 1 && h('label', {}, '日付項目 ', h('select', {
        value: String(st.field), onchange: e => choose('field', Number(e.target.value)),
      }, dateFields.map(f => h('option', { value: String(f.id) }, f.label)))),
      !compact && h('label', {}, '表示項目 ', h('select', {
        value: String(st.title), onchange: e => choose('title', Number(e.target.value)),
      }, titleFields.map(f => h('option', { value: String(f.id) }, f.label)))),
      !compact && h('button', { onclick: () => holidayDialog(st.year, reload) }, '🎌 休日の設定'),
      h('span', { class: 'muted' }, `${records.length} 件`)),
    h('div', { class: 'cal' },
      ['日', '月', '火', '水', '木', '金', '土'].map((w, i) => h('div', { class: 'cal-head' + (i === 0 ? ' sun' : i === 6 ? ' sat' : '') }, w)),
      cells),
    // 同じ年の各月と、前後の年へ直接移動する
    !compact && h('nav', { class: 'cal-months', 'aria-label': '月の移動' },
      h('button', { class: 'cal-year', onclick: () => { st.year--; reload(); } }, `‹ ${st.year - 1}年`),
      h('div', { class: 'cal-month-list' }, Array.from({ length: 12 }, (_, m) => h('button', {
        class: 'cal-month' + (m === st.month ? ' current' : ''),
        'aria-current': m === st.month ? 'date' : null, 'aria-label': `${st.year}年${m + 1}月`,
        onclick: () => { st.month = m; reload(); },
      }, `${m + 1}月`))),
      h('button', { class: 'cal-year', onclick: () => { st.year++; reload(); } }, `${st.year + 1}年 ›`)));
}

// ---------- 祝日・休日 ----------

const HOLIDAY_KINDS = {
  national: { label: '祝日', suffix: '' },
  shared: { label: '全員共通', suffix: '（全員共通の休日）' },
  personal: { label: '自分だけ', suffix: '（自分だけの休日）' },
};
const holidayCache = {}; // 年 → 読み込み中または読み込み済みの一覧
const loadHolidays = year => (holidayCache[year] ||= api('GET', `/api/holidays?year=${year}`).catch(err => {
  delete holidayCache[year]; toast('休日を読み込めませんでした: ' + err.message); return [];
}));

async function holidayDialog(year, onChange) {
  const listEl = h('div', { class: 'hol-list' });
  const yearSel = h('select', { 'aria-label': '表示する年', onchange: draw },
    [year - 1, year, year + 1].map(y => h('option', { value: String(y), selected: y === year }, `${y}年`)));
  const dateIn = h('input', { type: 'date', required: true, 'aria-label': '休日の日付' });
  const nameIn = h('input', { required: true, maxLength: 50, placeholder: '例: 夏季休暇', 'aria-label': '休日の名前' });
  const scope = h('select', { 'aria-label': '休日の対象' },
    h('option', { value: 'personal' }, '自分だけ'), ME.role === 'admin' && h('option', { value: 'all' }, '全員共通'));
  let changed = false;
  async function draw() {
    const y = Number(yearSel.value);
    delete holidayCache[y];
    const items = await loadHolidays(y);
    fill(listEl, items.length ? items.map(x => h('div', { class: 'row hol-row' },
      h('span', { class: 'hol-date' }, x.date.replace(/-/g, '/') + `（${'日月火水木金土'[new Date(x.date + 'T00:00').getDay()]}）`),
      h('span', { class: 'hol-name' }, x.name),
      h('span', { class: 'chip' + (x.kind === 'national' ? '' : ' chip-alt') }, HOLIDAY_KINDS[x.kind].label),
      h('span', { class: 'spacer' }),
      (x.kind === 'personal' || (x.kind === 'shared' && ME.role === 'admin')) && h('button', {
        type: 'button', class: 'icon danger', title: '削除', 'aria-label': `${x.date} ${x.name} を削除`,
        onclick: async () => {
          if (!confirm(`「${x.name}」（${x.date}）を削除しますか？`)) return;
          try { await api('DELETE', `/api/holidays/${x.id}`); changed = true; draw(); } catch (err) { toast(err.message); }
        },
      }, '✕')))
      : h('div', { class: 'muted' }, 'この年の祝日・休日はありません（祝日は 2020〜2099 年に対応）。'));
  }
  const dlg = openDialog({
    title: '祝日・休日の設定',
    body: [
      h('div', { class: 'form-field' }, h('div', { class: 'label' }, '休日を追加'),
        h('div', { class: 'row' }, dateIn, nameIn, scope,
          h('button', {
            type: 'button', class: 'primary',
            onclick: async () => {
              if (!dateIn.value || !nameIn.value.trim()) return toast('日付と名前を入力してください');
              try {
                await api('POST', '/api/holidays', { date: dateIn.value, name: nameIn.value, scope: scope.value });
                const y = Number(dateIn.value.slice(0, 4));
                delete holidayCache[y]; changed = true;
                nameIn.value = ''; toast('休日を追加しました');
                if (![...yearSel.options].some(o => Number(o.value) === y)) yearSel.append(h('option', { value: String(y) }, `${y}年`));
                yearSel.value = String(y); draw();
              } catch (err) { toast(err.message); }
            },
          }, '追加')),
        h('div', { class: 'muted' }, ME.role === 'admin'
          ? '「全員共通」は全員のカレンダーに、「自分だけ」はあなたのカレンダーにだけ表示されます。'
          : '追加した休日は、あなたのカレンダーにだけ表示されます。全員共通の休日はシステム管理者が登録します。')),
      h('div', { class: 'row' }, h('div', { class: 'label', style: 'font-weight:600' }, '登録済みの祝日・休日'), h('span', { class: 'spacer' }), yearSel),
      listEl,
    ],
  });
  dlg.addEventListener('close', () => { if (changed) onChange(); });
  draw();
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
  const directory = await api('GET', '/api/users/directory');
  const draft = app.fields.map(f => ({ ...f, config: { ...f.config }, user_ids: [...f.user_ids], editor_ids: [...f.editor_ids], optionsText: f.options.join('\n') }));
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
      // 項目ごとの閲覧権限。制限すると、指定した人とアプリ管理者以外にはこの項目が存在しないように見える
      h('div', { class: 'opts row field-access' },
        h('label', {}, '閲覧できる人 ', h('select', {
          value: f.access, 'aria-label': `${f.label || '項目'} を閲覧できる人`,
          onchange: e => { f.access = e.target.value; renderFields(); },
        }, h('option', { value: 'all' }, 'アプリを見られる全員'), h('option', { value: 'restricted' }, '🔒 指定したユーザーだけ'))),
        f.access === 'restricted' && directory.map(u => h('label', { class: 'pick' },
          h('input', {
            type: 'checkbox', checked: f.user_ids.includes(u.id),
            onchange: e => { f.user_ids = e.target.checked ? [...f.user_ids, u.id] : f.user_ids.filter(id => id !== u.id); },
          }), ` ${u.display_name}`))),
      // 項目ごとの編集権限。制限すると、指定した人とアプリ管理者以外は値を見られるが変更できない
      h('div', { class: 'opts row field-access' },
        h('label', {}, '編集できる人 ', h('select', {
          value: f.edit_access, 'aria-label': `${f.label || '項目'} を編集できる人`,
          onchange: e => { f.edit_access = e.target.value; renderFields(); },
        }, h('option', { value: 'all' }, '閲覧できる編集者全員'), h('option', { value: 'restricted' }, '✏️ 指定したユーザーだけ'))),
        f.edit_access === 'restricted' && directory.map(u => h('label', { class: 'pick' },
          h('input', {
            type: 'checkbox', checked: f.editor_ids.includes(u.id),
            onchange: e => { f.editor_ids = e.target.checked ? [...f.editor_ids, u.id] : f.editor_ids.filter(id => id !== u.id); },
          }), ` ${u.display_name}`))),
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
  const access = await api('GET', `/api/apps/${app.id}/access`);
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
      h('div', { class: 'muted' }, 'レコードに入力する項目を定義します。項目を削除すると、その項目に入力済みの値も削除されます。「閲覧できる人」を制限した項目は、指定した人とこのアプリの管理者だけが閲覧・編集できます。「編集できる人」だけを制限すると、ほかの人は値を見られますが変更できません。'),
      list,
      h('div', { class: 'row', style: 'margin-top:12px' },
        h('button', { onclick: () => { draft.push({ label: '', type: 'text', required: false, optionsText: '', config: {}, access: 'all', user_ids: [], edit_access: 'all', editor_ids: [] }); renderFields(); list.lastChild.querySelector('input').focus(); } }, '＋ 項目を追加'),
        h('span', { class: 'spacer' }),
        h('button', {
          class: 'primary',
          onclick: async () => {
            const removed = app.fields.filter(f => !draft.some(d => d.id === f.id));
            if (removed.length && !confirm(`項目「${removed.map(f => f.label).join('、')}」と入力済みの値を削除します。よろしいですか？`)) return;
            try {
              await api('PUT', `/api/apps/${app.id}/fields`, {
                fields: draft.map(f => ({ id: f.id ?? null, label: f.label, type: f.type, required: f.required, options: f.optionsText.split('\n'), config: f.config, access: f.access, user_ids: f.user_ids, edit_access: f.edit_access, editor_ids: f.editor_ids })),
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
