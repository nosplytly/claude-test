import { LANG, LOCALE, hooks as i18nHooks, t } from './i18n.js';  // first: the page's language is set before anything else
import { playIntro } from './intro.js';
import { startBackground } from './bg.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const LOGIN_RE = /^[A-Za-z0-9_.\-]{3,64}$/;
const COIN_ICON = (c) => `/assets/coins/${c.toLowerCase()}.svg`;
const COIN_NAME = { USDT: 'Tether USD', BTC: 'Bitcoin', ETH: 'Ethereum', TON: 'Toncoin', LTC: 'Litecoin', TRX: 'TRON', SOL: 'Solana', BNB: 'BNB' };
const NET_ICON = { TRC20: 'trx', BEP20: 'bnb', ERC20: 'eth', SOL: 'sol', TON: 'ton' };
const DEP_CHIPS = [10, 25, 50, 100];
const CONFIRM_ETA = {  // how long the network takes to confirm a payment (keys of the texts)
  usdt_trc20: 'eta.minute', trx: 'eta.minute', usdt_ton: 'eta.seconds', ton: 'eta.seconds', usdt_sol: 'eta.seconds',
  sol: 'eta.seconds', usdt_bep20: 'eta.under_minute', usdt_erc20: 'eta.2_3', eth: 'eta.2_3', btc: 'eta.10_20', ltc: 'eta.5_10',
};
const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

// ------------------------------------------------------------------ Telegram Mini App
// Opened from the bot, Telegram adds its signed launch data to the URL fragment (#tgWebAppData=… or #/path?tgWebAppData=…).
// Then Telegram's SDK is loaded and the visitor is logged in with that data — no code to tap. Inside Telegram Web the
// site runs in a cross-site iframe where cookies don't work, so the session token also rides in a header.
const TG = { app: null, session: '' };
const inTelegram = () => {
  if (/tgWebAppData=/.test(location.hash)) return true;
  try { return !!sessionStorage.getItem('__telegram__initParams'); } catch { return false; }
};

function loadTelegramSdk() {
  return new Promise((resolve) => {
    const s = document.createElement('script');
    s.src = 'https://telegram.org/js/telegram-web-app.js';
    s.onload = () => resolve(window.Telegram?.WebApp || null);
    s.onerror = () => resolve(null);
    document.head.append(s);
  });
}

async function initMiniApp() {
  if (!inTelegram()) return false;
  const wa = await loadTelegramSdk();
  if (!wa || !wa.initData) return false;
  TG.app = wa;
  document.body.classList.add('in-tg');
  try {
    wa.ready();
    wa.expand();
    wa.setHeaderColor?.('#0B0B0C');
    wa.setBackgroundColor?.('#0B0B0C');
    wa.disableVerticalSwipes?.();  // a scroll inside the form must not close the app
  } catch { /* older clients */ }
  // drop the launch data from the address, keep the page the button pointed to (#/deposit …)
  const h = location.hash;
  const path = h.startsWith('#/') ? h.split('?')[0].split('&')[0] : '';
  history.replaceState(null, '', location.pathname + (path || ''));
  try {
    const r = await api('/api/auth/webapp', { method: 'POST', body: { init_data: wa.initData } });
    TG.session = r.token;
    S.user = r.user;
  } catch (e) { toast(e.message, true); }
  wa.BackButton?.onClick(() => { location.hash = '#/'; });
  return true;
}

const haptic = (kind) => { try { TG.app?.HapticFeedback?.notificationOccurred(kind); } catch { /* not in Telegram */ } };

const S = {
  cfg: null,
  user: null,
  currency: 'RUB',
  login: { value: '', valid: null },
  pay: null, // { kind: 'order' | 'deposit', id, data }
  poll: 0,
  timer: 0,
  pendingSubmit: false,
  touched: false,
  unavailable: '',
};

// ------------------------------------------------------------------ utils
async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { 'X-SH': '1', 'X-SH-Lang': LANG, ...(body ? { 'Content-Type': 'application/json' } : {}),
      ...(TG.session ? { 'X-SH-Session': TG.session } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || t('err.network'));
    err.status = res.status;
    throw err;
  }
  return data;
}

const nf = (v, max = 2, min = 0) =>
  new Intl.NumberFormat(LOCALE, { maximumFractionDigits: max, minimumFractionDigits: min }).format(v);
const sign = (cur) => (S.cfg?.currencies.find((c) => c.code === cur) || {}).sign || cur;
const fmtFiat = (v, cur) => `${nf(v, 2, Math.abs(v - Math.round(v)) > 0.004 ? 2 : 0)} ${sign(cur)}`;
const fmtUsd = (v) => `${v < 0 ? '−' : ''}$${nf(Math.abs(v), 2, 2)}`;
const decimals = (step) => (String(step).split('.')[1] || '').length;
const ceilTo = (x, step) => { const f = 10 ** decimals(step); return Math.ceil(x * f - 1e-7) / f; };
const fmtCoin = (x, step) => x.toFixed(decimals(step)).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
const fmtDate = (iso) => new Date(iso).toLocaleString(LOCALE, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
// a discount promo code replaces it with the total the server computed (capped the same way it will cap the order)
const discount = () => +(S.promo?.total ?? S.user?.discount ?? S.cfg.discount);
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

function toast(msg, bad = false) {
  if (bad) haptic('error');
  const t = el('div', 'toast' + (bad ? ' bad' : ''));
  t.innerHTML = `<svg class="i"><use href="#${bad ? 'i-warn' : 'i-check'}"/></svg><span></span>`;
  t.querySelector('span').textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 260); }, bad ? 5200 : 2600);
}

let activeModal = null;
function openModal(id) {
  activeModal?.close();
  const node = $('#' + id).content.firstElementChild.cloneNode(true);
  const dialog = $('[role="dialog"]', node);
  const prev = document.activeElement;
  const oldOverflow = document.body.style.overflow;
  const background = $$('header, main, footer, .skip-link').map((element) => [element, element.inert]);
  background.forEach(([element]) => { element.inert = true; });
  document.body.style.overflow = 'hidden';
  dialog.tabIndex = -1;
  if (!dialog.hasAttribute('aria-labelledby') && !dialog.hasAttribute('aria-label')) {
    const heading = $('h2, h3', dialog);
    if (heading) { heading.id ||= `${id}Title`; dialog.setAttribute('aria-labelledby', heading.id); }
  }
  let onClose = null;
  let closed = false;
  let focusTimer = 0;
  const focusable = () => $$('a[href], button, input, select, textarea, [tabindex]', node)
    .filter((element) => !element.disabled && element.tabIndex >= 0 && element.getClientRects().length);
  const close = () => {
    if (closed) return;
    closed = true;
    clearTimeout(focusTimer);
    node.remove();
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('focusin', onFocus);
    background.forEach(([element, inert]) => { element.inert = inert; });
    document.body.style.overflow = oldOverflow;
    activeModal = null;
    onClose && onClose();
    if (prev?.isConnected) prev.focus({ preventScroll: true });
  };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (e.key !== 'Tab') return;
    const items = focusable();
    const first = items[0], last = items[items.length - 1];
    if (!items.length) { e.preventDefault(); dialog.focus(); }
    else if (e.shiftKey && (document.activeElement === first || !items.includes(document.activeElement))) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && (document.activeElement === last || !items.includes(document.activeElement))) {
      e.preventDefault(); first.focus();
    }
  };
  const onFocus = (e) => {
    if (!closed && !node.contains(e.target)) (focusable()[0] || dialog).focus({ preventScroll: true });
  };
  node.addEventListener('click', (e) => {
    if (e.target === node || e.target.closest('button[data-close]')) close();
  });
  document.body.appendChild(node);
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('focusin', onFocus);
  focusTimer = setTimeout(() => {
    const items = focusable();
    (items.find((element) => element.matches('input, a.cta, button.cta')) || items[0] || dialog).focus({ preventScroll: true });
  }, 0);
  activeModal = { node, close, set onClose(fn) { onClose = fn; } };
  return activeModal;
}

async function copy(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    (activeModal?.node || document.body).appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove();
    btn?.focus({ preventScroll: true });
  }
  if (btn) {
    btn.classList.add('copied');
    const ic = btn.querySelector('.ic');
    if (ic) {
      ic.innerHTML = '<svg class="i"><use href="#i-check"/></svg>';
      setTimeout(() => { btn.classList.remove('copied'); ic.innerHTML = '<svg class="i"><use href="#i-copy"/></svg>'; }, 1400);
    }
  }
  toast(t('toast.copied'));
}

function copyBox(text, mono = true) {
  const b = el('button', 'copy');
  b.type = 'button';
  b.innerHTML = `<span class="v ${mono ? 'mono' : ''}"></span><span class="ic"><svg class="i"><use href="#i-copy"/></svg></span>`;
  b.querySelector('.v').textContent = text;
  b.addEventListener('click', () => copy(text, b));
  return b;
}

function setText(node, text) {
  if (node.textContent === text) return;
  node.textContent = text;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  node.animate?.([{ opacity: 0.35, transform: 'translateY(3px)' }, { opacity: 1, transform: 'none' }],
    { duration: 260, easing: 'cubic-bezier(.2,.8,.2,1)' });
}

// segmented control with a sliding thumb (currency switch, cabinet tabs)
function moveThumb(seg) {
  const on = seg?.querySelector('[aria-checked="true"], [aria-selected="true"]');
  const t = seg?.querySelector('.seg-thumb');
  if (!on || !t) return;
  t.style.width = `${on.offsetWidth}px`;
  t.style.transform = `translateX(${on.offsetLeft}px)`;
}
addEventListener('resize', () => $$('.seg').forEach(moveThumb));

function syncRadioTabStops(group) {
  const radios = $$('[role="radio"]', group);
  const selected = radios.find((radio) => radio.getAttribute('aria-checked') === 'true') || radios[0];
  radios.forEach((radio) => { radio.tabIndex = radio === selected ? 0 : -1; });
}

document.addEventListener('keydown', (e) => {
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(e.key)) return;
  const radio = e.target.closest('[role="radio"]');
  const group = radio?.closest('[role="radiogroup"]');
  if (!group) return;
  const radios = $$('[role="radio"]', group).filter((button) => !button.disabled);
  if (!radios.length) return;
  const current = radios.indexOf(radio);
  const index = e.key === 'Home' ? 0 : e.key === 'End' ? radios.length - 1
    : (current + (['ArrowRight', 'ArrowDown'].includes(e.key) ? 1 : -1) + radios.length) % radios.length;
  e.preventDefault();
  radios[index].focus();
  radios[index].click();
  if (!radios[index].isConnected) $('[aria-checked="true"]', group)?.focus();
});

// ------------------------------------------------------------------ coin + network picker (used twice)
function makePicker({ coins, nets, wrap, storeKey, onChange }) {
  const st = { coin: null, method: null };
  const groups = () => {
    const by = new Map();
    for (const m of S.cfg?.methods || []) {
      if (!by.has(m.coin)) by.set(m.coin, []);
      by.get(m.coin).push(m);
    }
    return by;
  };
  function render() {
    coins.innerHTML = '';
    for (const [coin] of groups()) {
      const b = el('button', 'coin');
      b.type = 'button';
      b.setAttribute('role', 'radio');
      b.dataset.coin = coin;
      b.title = COIN_NAME[coin] || coin;
      b.innerHTML = `<img src="${COIN_ICON(coin)}" alt="" width="32" height="32"><span>${coin}</span>`;
      b.addEventListener('click', () => select(coin));
      coins.appendChild(b);
    }
    const saved = S.cfg.methods.find((m) => m.code === store.get(storeKey)) || S.cfg.methods[0];
    if (saved) select(saved.coin, saved.code, true);
  }
  function select(coin, code, silent = false) {
    const list = groups().get(coin);
    if (!list) return;
    const m = list.find((x) => x.code === code) || list.find((x) => x.code === st.method) || list[0];
    st.coin = coin;
    st.method = m.code;
    store.set(storeKey, m.code);
    $$('.coin', coins).forEach((b) => b.setAttribute('aria-checked', String(b.dataset.coin === coin)));
    syncRadioTabStops(coins);
    const restoreNetworkFocus = nets.contains(document.activeElement);
    nets.innerHTML = '';
    if (list.length > 1) {
      for (const x of list) {
        const b = el('button', 'net');
        const networkIcon = el('img');
        networkIcon.src = COIN_ICON(NET_ICON[x.network] || x.coin);
        networkIcon.alt = '';
        networkIcon.width = 14;
        networkIcon.height = 14;
        b.append(networkIcon, el('span', null, x.network));
        b.type = 'button';
        b.title = x.network_title;
        b.setAttribute('role', 'radio');
        b.setAttribute('aria-checked', String(x.code === st.method));
        b.addEventListener('click', () => select(coin, x.code));
        nets.appendChild(b);
      }
    }
    syncRadioTabStops(nets);
    wrap.classList.toggle('open', list.length > 1);
    if (restoreNetworkFocus) ($('[aria-checked="true"]', nets) || $('[aria-checked="true"]', coins))?.focus({ preventScroll: true });
    if (!silent) onChange?.();
  }
  return { st, render, select, method: () => S.cfg?.methods.find((m) => m.code === st.method) };
}

function coinAmount(m, usd) {
  if (!m || !(usd > 0)) return null;
  const p = +S.cfg.prices[m.coin];
  if (!p) return null;
  usd = Math.max(usd, +m.min_usd || 0); // e.g. BTC can't carry less than ~$0.5; the extra lands on the balance
  return ceilTo(usd / (m.stable ? p : p / (1 + +S.cfg.markup / 100)), m.step);
}

// ------------------------------------------------------------------ user
function renderUser() {
  const u = S.user;
  $('#loginBtn').hidden = !!u;
  $('#userChip').hidden = !u;
  if (u) {
    renderAvatar(u);
    $('#userName').textContent = u.name;
    $('#userBal').textContent = fmtUsd(+u.balance);
  }
  if (S.cfg) $('#discountBadge').textContent = `−${discount()}%`;
  compute();
  computeDeposit();
}

// the Telegram photo (served by our backend; 204 = no photo) over the first letter of the name
function renderAvatar(u) {
  const box = $('#avatar');
  const src = u.avatar || '';
  if (box.dataset.src === src) return; // renderUser runs on every balance refresh: don't reload or flicker
  box.dataset.src = src;
  box.textContent = (u.first_name || u.username || u.name || '?').replace('@', '').slice(0, 1);
  if (!src) return;
  const img = new Image();
  img.alt = '';
  img.onload = () => { if (box.dataset.src === src && img.naturalWidth) box.replaceChildren(img); };
  img.src = src;
}

async function refreshUser() {
  try { S.user = (await api('/api/me')).user; renderUser(); } catch { /* offline */ }
}

$('#userBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  const m = $('#userMenu');
  m.hidden = !m.hidden;
  $('#userBtn').setAttribute('aria-expanded', String(!m.hidden));
});
addEventListener('click', () => { $('#userMenu').hidden = true; $('#userBtn').setAttribute('aria-expanded', 'false'); });
$('#userMenu').addEventListener('click', async (e) => {
  const act = e.target.closest('[data-act]')?.dataset.act;
  if (act === 'orders' || act === 'balance' || act === 'api') openCabinet(act);
  if (act === 'deposit') location.hash = '#/deposit';
  if (act === 'logout') {
    await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
    S.user = null; renderUser(); markSoon(false); location.hash = '#/'; toast(t('toast.signed_out'));
  }
});
$('#loginBtn').addEventListener('click', () => openLogin());
document.addEventListener('click', (e) => {
  const go = e.target.closest('[data-go]');
  if (go) location.hash = go.dataset.go;
  const scroll = e.target.closest('a[href="#card"]');
  if (!scroll) return;
  const target = $('#card');
  if (!target) return;
  e.preventDefault();
  if (location.hash !== '#/') location.hash = '#/';
  showView('viewForm');
  requestAnimationFrame(() => {
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!target.hasAttribute('tabindex')) target.tabIndex = -1;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
  });
});

// ------------------------------------------------------------------ login (Telegram bot deep link)
function openLogin() {
  if (S.cfg?.dev_login) {
    const m = openModal('tplDevLogin');
    $('.dev-go', m.node).addEventListener('click', async () => {
      try { const r = await api('/api/dev/login', { method: 'POST', body: {} }); S.user = r.user; m.close(); onLoggedIn(r.user); }
      catch (e) { toast(e.message, true); }
    });
    m.onClose = () => { if (!S.user) S.pendingSubmit = false; };
    return;
  }
  if (!S.cfg?.bot) { toast(t('lm.unavailable'), true); return; }
  const m = openModal('tplLogin');
  let timer = 0, alive = true;
  const stop = () => { alive = false; clearInterval(timer); document.removeEventListener('visibilitychange', tick); };
  m.onClose = () => { stop(); if (!S.user) S.pendingSubmit = false; };
  const codeEl = $('.lm-code', m.node), link = $('.lm-open', m.node), wait = $('.lm-wait', m.node);

  // one tile per digit, like the logo: the last one in brand blue
  const showCode = (code) => codeEl.replaceChildren(...[...code].map((d, i, all) => el('span', i === all.length - 1 ? 'blue' : '', d)));

  async function start() {
    showCode('··');
    wait.textContent = t('lm.wait');
    try {
      const r = await api('/api/auth/start', { method: 'POST' });
      if (!alive) return;
      showCode(String(r.code));
      codeEl.animate([{ transform: 'scale(.85)' }, { transform: 'scale(1)' }], { duration: 380, easing: 'cubic-bezier(.34,1.56,.64,1)' });
      link.href = r.url;
      clearInterval(timer);
      timer = setInterval(tick, 2000);
    } catch (e) { toast(e.message, true); m.close(); }
  }
  async function tick() {
    if (!alive || document.hidden) return;
    try {
      const r = await api('/api/auth/poll');
      if (!alive) return;
      if (r.status === 'ok') { stop(); S.user = r.user; m.close(); onLoggedIn(r.user); }
      else if (r.status === 'expired' || r.status === 'cancelled' || r.status === 'none') {
        clearInterval(timer);
        wait.innerHTML = '';
        const b = el('button', 'link-btn', t('lm.new_code'));
        b.type = 'button';
        b.onclick = start;
        wait.append(r.status === 'cancelled' ? t('lm.cancelled') : t('lm.expired'), b);
      }
    } catch { /* retry on next tick */ }
  }
  document.addEventListener('visibilitychange', tick);
  start();
}

function onLoggedIn(user) {
  S.user = user;
  renderUser();
  api('/api/waitlist').then((r) => markSoon(r.joined)).catch(() => {});  // maybe joined through the bot earlier
  toast(t('toast.signed_in', { name: user.name }));
  if (S.pendingSubmit) { S.pendingSubmit = false; submitOrder(); }
  else route();
}

// ------------------------------------------------------------------ Steam order form
const pick = makePicker({ coins: $('#coins'), nets: $('#nets'), wrap: $('#netsWrap'), storeKey: 'sh_method',
  onChange: () => { $('#methodHint').hidden = true; $('#fMethod').classList.remove('bad'); $('#coins').removeAttribute('aria-invalid'); compute(); } });

function renderCurrencies() {
  const seg = $('#curSeg');
  $$('button', seg).forEach((b) => b.remove());
  for (const c of S.cfg.currencies) {
    const b = el('button');
    b.type = 'button';
    b.setAttribute('role', 'radio');
    b.dataset.cur = c.code;
    b.innerHTML = `${c.code} <span>${c.sign}</span>`;
    b.addEventListener('click', () => setCurrency(c.code));
    seg.appendChild(b);
  }
  markCurrency();
}

function markCurrency() {
  $$('#curSeg button').forEach((b) => b.setAttribute('aria-checked', String(b.dataset.cur === S.currency)));
  syncRadioTabStops($('#curSeg'));
  requestAnimationFrame(() => moveThumb($('#curSeg')));
}

const curCfg = () => S.cfg?.currencies.find((c) => c.code === S.currency);

function setCurrency(code) {
  if (code === S.currency) return;
  const old = S.currency, amt = parseAmount();
  S.currency = code;
  store.set('sh_cur', code);
  const r = S.cfg.rates;
  if (amt && r[old] && r[code]) {
    let v = (amt * r[code]) / r[old];
    v = code === 'USD' ? Math.max(1, Math.round(v)) : Math.round(v / 10) * 10;
    $('#amount').value = String(v);
  }
  $('#amountSign').textContent = sign(code);
  markCurrency();
  renderChips();
  onAmount();
}

function renderChips() {
  const box = $('#chips');
  box.innerHTML = '';
  for (const v of curCfg().chips) {
    const b = el('button', 'chip num', `${nf(v)} ${sign(S.currency)}`);
    b.type = 'button';
    b.dataset.v = v;
    b.addEventListener('click', () => { $('#amount').value = String(v); onAmount(); });
    box.appendChild(b);
  }
  markChips();
}

function markChips() {
  const a = parseAmount();
  $$('#chips .chip').forEach((c) => c.classList.toggle('on', +c.dataset.v === a));
}

const num = (input) => { const v = parseFloat((input.value || '').replace(',', '.').replace(/\s/g, '')); return Number.isFinite(v) ? v : 0; };
const parseAmount = () => num($('#amount'));

function cleanMoneyInput(input) {
  let v = input.value.replace(',', '.').replace(/[^\d.]/g, '');
  const [i, ...rest] = v.split('.');
  v = rest.length ? `${i}.${rest.join('').slice(0, 2)}` : i;
  if (v !== input.value) input.value = v;
}

function amountError() {
  const c = curCfg();
  if (!c) return t('amount.unavailable');
  const a = parseAmount();
  if (!a) return t('amount.enter');
  if (a < +c.min || a > +c.max) return t('amount.range', { min: fmtFiat(+c.min, c.code), max: fmtFiat(+c.max, c.code) });
  return null;
}

function onAmount() {
  cleanMoneyInput($('#amount'));
  if (!curCfg()) return;
  markChips();
  showAmountHint();
  compute();
}

function showAmountHint(force = false) {
  const c = curCfg();
  if (!c) return;
  const err = amountError();
  const showErr = err && (force || (parseAmount() > 0 && S.touched));
  $('#fAmount').classList.toggle('bad', !!showErr);
  $('#amount').setAttribute('aria-invalid', String(!!showErr));
  $('#amountHint').className = 'hint' + (showErr ? ' err' : '');
  $('#amountHint').textContent = showErr ? err : t('amount.hint', { min: fmtFiat(+c.min, c.code), max: fmtFiat(+c.max, c.code) });
}

function quote() {
  const cfg = S.cfg, cur = S.currency, amt = parseAmount();
  const rate = +cfg.rates[cur];
  if (!amt || !rate || amountError()) return null;
  const d = discount() / 100;
  const priceUsd = (amt / rate) * (1 - d);
  const bal = S.user ? +S.user.balance : 0;
  const covers = bal > 0 && bal >= priceUsd - 0.02;
  const fromBal = covers ? priceUsd : Math.max(0, Math.min(bal, priceUsd));
  const due = priceUsd - fromBal;
  const m = pick.method();
  return { amt, cur, priceUsd, payFiat: amt * (1 - d), save: amt * d, covers, fromBal, due, m,
    coinAmt: covers ? null : coinAmount(m, due) };
}

function compute() {
  if (!S.cfg || !S.cfg.currencies.length) return;
  const q = quote();
  if (!q) {
    ['#sGet', '#sPrice', '#sPay', '#sSave'].forEach((id) => setText($(id), '—'));
    $('#sBalRow').hidden = true;
    $('#sNote').textContent = '';
  } else {
    setText($('#sGet'), fmtFiat(q.amt, q.cur));
    setText($('#sPrice'), fmtFiat(q.payFiat, q.cur));
    setText($('#sSave'), fmtFiat(q.save, q.cur));
    $('#sBalRow').hidden = !(q.fromBal > 0);
    if (q.fromBal > 0) setText($('#sBal'), `−${fmtUsd(q.fromBal)}`);
    if (q.covers) {
      setText($('#sPay'), fmtUsd(0));
      $('#sNote').textContent = t('note.full_balance');
    } else if (q.m && q.coinAmt) {
      setText($('#sPay'), `≈ ${fmtCoin(q.coinAmt, q.m.step)} ${q.m.coin}`);
      const minUsd = +q.m.min_usd || 0;
      $('#sNote').textContent = q.due < minUsd
        ? t('note.min_transfer', { coin: q.m.coin, usd: fmtUsd(minUsd) })
        : t('note.exact', { usd: fmtUsd(q.due) });
    } else {
      setText($('#sPay'), fmtUsd(q.due));
      $('#sNote').textContent = q.m ? '' : t('note.choose_coin');
    }
  }
  const blocked = S.unavailable && !(q?.covers && S.cfg.currencies.length && !S.cfg.methods.length);
  const txt = blocked ? S.unavailable : !S.user ? t('cta.signin_pay') : q && q.covers ? t('cta.pay_balance') : t('cta.pay');
  if (!$('#cta').classList.contains('loading')) {
    $('#ctaText').textContent = txt;
    $('#cta').disabled = !!blocked;
  }
}

// Steam login check
let loginTimer = 0;
function setLoginState(kind, msg) {
  const f = $('#fLogin'), st = $('#loginState'), hint = $('#loginHint');
  f.classList.toggle('ok', kind === 'ok');
  f.classList.toggle('bad', kind === 'bad');
  $('#login').setAttribute('aria-invalid', String(kind === 'bad'));
  st.innerHTML = kind === 'loading' ? '<span class="spin"></span>'
    : kind === 'ok' ? '<svg><use href="#i-check"/></svg>'
      : kind === 'bad' ? '<svg><use href="#i-x"/></svg>' : '';
  if (kind === 'bad' || kind === 'ok') {
    hint.className = 'hint' + (kind === 'bad' ? ' err' : '');
    hint.textContent = msg;
  } else {
    hint.className = 'hint';
    hint.innerHTML = t('form.login_hint_html');
    $('#whereLogin').addEventListener('click', () => openModal('tplWhere'));
  }
}

async function checkLogin(v) {
  if (!LOGIN_RE.test(v)) {
    S.login = { value: v, valid: false };
    setLoginState('bad', t('login.rule'));
    return false;
  }
  if (S.login.value === v && S.login.valid !== null) return S.login.valid;
  S.login = { value: v, valid: null };
  setLoginState('loading');
  try {
    const r = await api('/api/steam/check', { method: 'POST', body: { login: v } });
    if ($('#login').value.trim() !== v) return null;
    S.login = { value: v, valid: r.valid };
    setLoginState(r.valid ? 'ok' : 'bad', r.valid ? t('login.ok') : r.message);
    return r.valid;
  } catch (e) {
    S.login = { value: v, valid: null };
    setLoginState('idle');
    if (e.status !== 429) toast(e.message, true);
    return null;
  }
}

$('#login').addEventListener('input', () => {
  const v = $('#login').value.trim();
  clearTimeout(loginTimer);
  if (v !== S.login.value) { S.login = { value: '', valid: null }; setLoginState('idle'); }
  store.set('sh_login', v);
  // each distinct login costs one request to the supplier, whose rate limit is tight: wait for a real pause
  if (v.length >= 3) loginTimer = setTimeout(() => checkLogin(v), 1300);
});
$('#login').addEventListener('blur', () => {
  const v = $('#login').value.trim();
  clearTimeout(loginTimer);
  if (v.length >= 1 && v !== S.login.value) checkLogin(v);
});
$('#whereLogin').addEventListener('click', () => openModal('tplWhere'));
$('#amount').addEventListener('input', onAmount);
$('#amount').addEventListener('blur', () => { S.touched = true; showAmountHint(); });
$('#viewForm').addEventListener('submit', (e) => { e.preventDefault(); submitOrder(); });

async function submitOrder() {
  const cta = $('#cta');
  if (cta.classList.contains('loading') || !curCfg()) return;
  S.touched = true;
  const login = $('#login').value.trim();
  if (!login) { setLoginState('bad', t('login.enter')); $('#login').focus(); return; }
  if (amountError()) { showAmountHint(true); $('#amount').focus(); return; }
  const q = quote();
  if (!q) { toast(t('err.calc'), true); return; }
  if (S.unavailable && !q.covers) return;
  if (!q.covers && !pick.st.method) {
    $('#fMethod').classList.add('bad');
    $('#coins').setAttribute('aria-invalid', 'true');
    $('#coins').setAttribute('aria-describedby', 'methodHint');
    $('#methodHint').hidden = false;
    $('#methodHint').textContent = t('err.choose_coin');
    return;
  }
  if (!S.user) { S.pendingSubmit = true; openLogin(); return; }

  cta.classList.add('loading');
  cta.disabled = true;
  $('#ctaText').textContent = t('cta.checking');
  try {
    const ok = await checkLogin(login);
    if (ok === false) { $('#login').focus(); return; }
    $('#ctaText').textContent = t('cta.creating');
    const o = await api('/api/orders', {
      method: 'POST',
      body: { steam_login: login, amount: String(parseAmount()), currency: S.currency, method: pick.st.method,
        promo: S.promo?.code || null },
    });
    if (S.promo) { S.promo = null; renderPromo(); }  // one order per code use
    refreshUser();
    location.hash = `#/order/${o.id}`;
  } catch (e) {
    if (e.status === 401) { S.user = null; renderUser(); S.pendingSubmit = true; openLogin(); }
    else toast(e.message, true);
  } finally {
    cta.classList.remove('loading');
    cta.disabled = false;
    compute();
  }
}

// ------------------------------------------------------------------ promo code
function renderPromo() {
  const on = !!S.promo;
  $('#promoChip').hidden = !on;
  $('#promoOpen').hidden = on || !$('#promoRow').hidden;
  if (on) {
    $('#promoRow').hidden = true;
    $('#promoCode').textContent = S.promo.code;
    $('#promoLabel').textContent = S.promo.label;
  }
  renderUser();
}

async function applyPromo() {
  const code = $('#promoInput').value.trim().toUpperCase();
  if (!code) { $('#promoInput').focus(); return; }
  const btn = $('#promoApply');
  btn.disabled = true;
  try {
    const r = await api('/api/promo/check', { method: 'POST', body: { code } });
    if (r.kind === 'bonus') {  // balance bonus: credit it right away
      if (!S.user) { toast(t('promo.signin_bonus')); openLogin(); return; }
      const x = await api('/api/promo/redeem', { method: 'POST', body: { code } });
      closePromo();
      toast(t('promo.redeemed', { code: r.code, label: x.label }));
      refreshUser();
      return;
    }
    S.promo = { code: r.code, total: +r.total, label: r.label };
    renderPromo();
    toast(t('promo.applied', { code: r.code, pct: fmtPct(S.promo.total) }));
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

const fmtPct = (x) => `${+x.toFixed(2)}%`;
$('#promoOpen').addEventListener('click', () => {
  $('#promoRow').hidden = false;
  $('#promoOpen').hidden = true;
  $('#promoInput').focus();
});
$('#promoApply').addEventListener('click', applyPromo);
// no code after all: the cross, Esc, or leaving the empty field folds it back into the "Есть промокод?" row
function closePromo() {
  $('#promoInput').value = '';
  $('#promoRow').hidden = true;
  $('#promoOpen').hidden = !!S.promo;
}
$('#promoClose').addEventListener('click', () => { closePromo(); $('#promoOpen').focus(); });
$('#promoRow').addEventListener('focusout', (ev) => {
  if (!$('#promoInput').value.trim() && !$('#promoRow').contains(ev.relatedTarget)) closePromo();
});
$('#promoInput').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') { ev.preventDefault(); applyPromo(); }
  if (ev.key === 'Escape') { ev.preventDefault(); closePromo(); $('#promoOpen').focus(); }
});
$('#promoInput').addEventListener('input', (ev) => { ev.target.value = ev.target.value.toUpperCase(); });
$('#promoRemove').addEventListener('click', () => { S.promo = null; $('#promoInput').value = ''; renderPromo(); });

// ------------------------------------------------------------------ balance top-up form
const dpick = makePicker({ coins: $('#depCoins'), nets: $('#depNets'), wrap: $('#depNetsWrap'), storeKey: 'sh_dep_method',
  onChange: () => computeDeposit() });

function renderDepositForm() {
  const box = $('#depChips');
  box.innerHTML = '';
  for (const v of DEP_CHIPS) {
    const b = el('button', 'chip num', `$${v}`);
    b.type = 'button';
    b.dataset.v = v;
    b.addEventListener('click', () => { $('#depAmount').value = String(v); computeDeposit(); });
    box.appendChild(b);
  }
  if (!$('#depAmount').value) $('#depAmount').value = '25';
  computeDeposit();
}

function depositError() {
  if (!S.cfg) return t('service.unavailable');
  const a = num($('#depAmount'));
  if (!a) return t('amount.enter');
  if (a < +S.cfg.deposit_min || a > +S.cfg.deposit_max) return t('amount.range', { min: fmtUsd(+S.cfg.deposit_min), max: fmtUsd(+S.cfg.deposit_max) });
  return null;
}

function computeDeposit() {
  if (!S.cfg) return;
  const a = num($('#depAmount'));
  $$('#depChips .chip').forEach((c) => c.classList.toggle('on', +c.dataset.v === a));
  const err = depositError();
  $('#fDep').classList.toggle('bad', !!err && a > 0);
  $('#depAmount').setAttribute('aria-invalid', String(!!err && a > 0));
  $('#depHint').className = 'hint' + (err && a > 0 ? ' err' : '');
  $('#depHint').textContent = err && a > 0 ? err : t('dep.hint', { min: fmtUsd(+S.cfg.deposit_min), max: fmtUsd(+S.cfg.deposit_max) });
  const bal = S.user ? +S.user.balance : 0;
  const m = dpick.method();
  const minUsd = +(m?.min_usd || 0);
  const credited = Math.max(a, minUsd); // BTC can't carry tiny amounts: the invoice (and the credit) is at least min_usd
  setText($('#depNow'), S.user ? fmtUsd(bal) : '—');
  setText($('#depAfter'), a && !err ? fmtUsd(bal + credited) : '—');
  const c = !err ? coinAmount(m, a) : null;
  setText($('#depPay'), c ? `≈ ${fmtCoin(c, m.step)} ${m.coin}` : '—');
  $('#depNote').textContent = !c ? '' : a < minUsd
    ? t('note.min_transfer_dep', { coin: m.coin, usd: fmtUsd(minUsd) })
    : t('note.exact_short');
  if (!$('#depCta').classList.contains('loading')) {
    $('#depCta').disabled = !S.cfg.methods.length;
    $('#depCtaText').textContent = !S.cfg.methods.length ? t('payments.soon') : S.user ? t('dep.cta') : t('dep.signin');
  }
}

$('#depAmount').addEventListener('input', () => { cleanMoneyInput($('#depAmount')); computeDeposit(); });
$('#viewDeposit').addEventListener('submit', async (e) => {
  e.preventDefault();
  const cta = $('#depCta');
  if (cta.classList.contains('loading') || !S.cfg || !S.cfg.methods.length) return;
  const error = depositError();
  if (error) {
    $('#fDep').classList.add('bad');
    $('#depHint').className = 'hint err';
    $('#depHint').textContent = error;
    $('#depAmount').setAttribute('aria-invalid', 'true');
    $('#depAmount').focus();
    return;
  }
  if (!S.user) { openLogin(); return; }
  cta.classList.add('loading');
  cta.disabled = true;
  try {
    const d = await api('/api/deposits', { method: 'POST', body: { amount_usd: String(num($('#depAmount'))), method: dpick.st.method } });
    location.hash = `#/deposit/${d.id}`;
  } catch (err) {
    if (err.status === 401) { S.user = null; renderUser(); openLogin(); } else toast(err.message, true);
  } finally {
    cta.classList.remove('loading');
    cta.disabled = false;
  }
});

// ------------------------------------------------------------------ views & routing
function showView(id) {
  document.body.dataset.view = id;  // narrow screens show the plugin strip only next to the order form
  for (const v of ['viewForm', 'viewDeposit', 'viewPay', 'viewResult']) {
    const node = $('#' + v);
    const on = v === id;
    if (on && node.hidden) {
      node.hidden = false;
      node.classList.remove('enter');
      void node.offsetWidth;
      node.classList.add('enter');
    } else if (!on) node.hidden = true;
  }
}

function stopPolling() { clearTimeout(S.poll); clearInterval(S.timer); S.poll = 0; S.timer = 0; }

function route() {
  stopPolling();
  if (TG.app?.BackButton) {  // Telegram's own «back» arrow on every page except the form
    if (location.hash && location.hash !== '#/') TG.app.BackButton.show(); else TG.app.BackButton.hide();
  }
  if (!S.cfg) { showView('viewForm'); return; }
  const h = location.hash;
  let m;
  if ((m = h.match(/^#\/order\/([A-Z0-9]{6,16})$/))) {
    if (!S.user) { showView('viewForm'); openLogin(); return; }
    loadPay('order', m[1], true);
  } else if ((m = h.match(/^#\/deposit\/([A-Z0-9]{6,16})$/))) {
    if (!S.user) { showView('viewForm'); openLogin(); return; }
    loadPay('deposit', m[1], true);
  } else if (h === '#/deposit') {
    showView('viewDeposit');
    computeDeposit();
  } else {
    showView('viewForm');
    requestAnimationFrame(() => moveThumb($('#curSeg')));
  }
}
addEventListener('hashchange', route);

async function loadPay(kind, id, first = false) {
  const path = kind === 'order' ? `/api/orders/${id}` : `/api/deposits/${id}`;
  const hash = `#/${kind}/${id}`;
  try {
    const d = await api(path);
    if (location.hash !== hash) return; // the user already left this order: never paint a late answer over the new page
    S.pay = { kind, id, data: d };
    renderPay(kind, d, first);
    if (S.user && d.balance !== undefined && d.balance !== S.user.balance) { S.user.balance = d.balance; renderUser(); }
  } catch (e) {
    if (location.hash !== hash) return;
    if (first) { toast(e.message, true); location.hash = '#/'; return; }
  }
  if (location.hash === hash && S.pay && ['pay', 'confirming', 'topup'].includes(S.pay.data.stage)) {
    S.poll = setTimeout(() => (document.hidden ? waitVisible(() => loadPay(kind, id)) : loadPay(kind, id)), 3000);
  }
}

function waitVisible(fn) {
  const h = () => { if (!document.hidden) { document.removeEventListener('visibilitychange', h); fn(); } };
  document.addEventListener('visibilitychange', h);
}

const STEP_OF = { pay: 1, confirming: 1, topup: 2, done: 3, failed: 2 };

function renderSteps(stage) {
  const cur = STEP_OF[stage] ?? 1;
  $$('#steps .step').forEach((s) => {
    const i = +s.dataset.s;
    s.classList.toggle('done', i < cur || stage === 'done');
    s.classList.toggle('now', i === cur && stage !== 'done' && stage !== 'failed');
    s.classList.toggle('fail', i === cur && stage === 'failed');
  });
  $('#stepsProg').style.width = `${25 * Math.min(cur, 3)}%`;
}

function renderPay(kind, d, first) {
  if (['done', 'failed', 'expired', 'cancelled'].includes(d.stage)) { renderResult(kind, d); return; }
  const wasHidden = $('#viewPay').hidden;
  showView('viewPay');
  const isOrder = kind === 'order';
  $('#payTitle').textContent = isOrder ? t('pay.order_title', { id: d.id }) : t('pay.deposit_title');
  $('#paySub').textContent = isOrder ? `${d.steam_login} · ${fmtFiat(+d.amount, d.currency)}` : t('pay.deposit_sub', { usd: fmtUsd(+d.usd), id: d.id });
  $('#step2Label').textContent = isOrder ? t('step.topup') : t('step.credit');
  renderSteps(d.stage);

  // orders carry the invoice inside; a deposit IS the invoice
  const inv = isOrder ? d.invoice : { ...d, id: d.invoice_id, from_balance: '0' };
  const paying = (d.stage === 'pay' || d.stage === 'confirming') && inv;
  $('#payInvoice').hidden = !paying;
  $('#timer').hidden = !(d.stage === 'pay' && inv);
  $('#claimBtn').hidden = !paying;
  $('#cancelBtn').hidden = !(isOrder && d.stage === 'pay');
  $('#devPayBtn').hidden = !(S.cfg.dev_pay && d.stage === 'pay');
  if (paying && (first || wasHidden || $('#payAmount').dataset.inv !== String(inv.id))) {
    $('#payAmount').dataset.inv = String(inv.id);
    $('#payAmount').textContent = inv.amount;
    $('#payCoin').textContent = inv.coin;
    $('#payQr').src = inv.qr;
    $('#payQrCoin').src = COIN_ICON(inv.coin);
    const netIcon = $('#payNetIcon');
    netIcon.onerror = () => { netIcon.onerror = null; netIcon.src = COIN_ICON(inv.coin); };
    netIcon.src = COIN_ICON(inv.network === inv.coin ? inv.coin : (NET_ICON[inv.network] || inv.coin));
    const net = $('#payNet');
    net.innerHTML = '';
    net.append(inv.network_title + ' ');
    if (inv.network !== inv.coin && inv.network !== inv.network_title) net.append(el('span', 'tag', inv.network));
    $('#payAddr').textContent = inv.address;
    $('#payMemoBox').hidden = !inv.comment;
    $('#payMemo').textContent = inv.comment || '';
    const netName = inv.network === inv.coin ? inv.network_title : `${inv.network_title} (${inv.network})`;
    let warn = t('pay.warn', { coin: inv.coin, network: netName });
    if (+inv.from_balance > 0) warn += t('pay.warn_balance', { usd: fmtUsd(+inv.from_balance) });
    $('#payWarn').textContent = warn;
  }
  if (paying && d.stage === 'pay') startTimer(inv.expires_at); else clearInterval(S.timer);

  const p = d.payment;
  if (d.stage === 'pay') {
    setStatus(t('pay.waiting'), t('pay.waiting_text'));
  } else if (d.stage === 'confirming') {
    const conf = p && p.required > 1 ? t('pay.confirmations', { n: Math.min(p.confirmations, p.required), of: p.required }) : '';
    const eta = t(CONFIRM_ETA[inv?.method] || 'eta.default');
    setStatus(t('pay.found'), conf + t('pay.network_confirms', { eta }), p?.tx_url);
  } else if (d.stage === 'topup') {
    setStatus(t('pay.topping'), d.status === 'queued' ? t('pay.queued') : t('pay.close_page'));
  }
}

function setStatus(title, text, txUrl) {
  $('#stTitle').textContent = title;
  const box = $('#stText');
  box.textContent = text || '';
  if (txUrl) box.append(Object.assign(el('a', null, t('pay.tx')), { href: txUrl, target: '_blank', rel: 'noopener' }));
}

function startTimer(expiresAt) {
  clearInterval(S.timer);
  const end = Date.parse(expiresAt);
  const total = (S.cfg.invoice_ttl_min || 30) * 60 * 1000;
  const C = 2 * Math.PI * 21;
  const bar = $('#timerBar');
  bar.style.strokeDasharray = `${C}`;
  const tick = () => {
    const left = Math.max(0, end - Date.now());
    const mm = Math.floor(left / 60000), ss = Math.floor((left % 60000) / 1000);
    $('#timerText').textContent = `${String(mm).padStart(2, '0')}:${String(ss).padStart(2, '0')}`;
    bar.style.strokeDashoffset = `${C * (1 - left / total)}`;
    $('#timer').classList.toggle('low', left < 5 * 60000);
    if (left <= 0) {
      clearInterval(S.timer);
      setStatus(t('pay.expired'), t('pay.expired_text'));
    }
  };
  tick();
  S.timer = setInterval(tick, 1000);
}

function renderResult(kind, d) {
  stopPolling();
  showView('viewResult');
  const mark = $('#resMark');
  mark.classList.remove('fail', 'idle');
  const icon = $('#resIcon path');
  const title = $('#resTitle'), text = $('#resText');
  const main = $('#resMain'), second = $('#resSecond');
  second.hidden = false;
  second.textContent = kind === 'order' ? t('res.my_orders') : t('res.history');
  second.onclick = () => openCabinet(kind === 'order' ? 'orders' : 'balance');
  main.onclick = () => { location.hash = '#/'; };
  if (d.stage === 'done') {
    icon.setAttribute('d', 'M5 12.5l4.5 4.5L19 7.5');
    title.textContent = kind === 'order' ? t('res.done') : t('res.funds_added');
    if (kind === 'order') {
      text.innerHTML = t('res.steam_done_html', { login: esc(d.steam_login), amount: esc(fmtFiat(+d.amount, d.currency)) });
      $('#resMainText').textContent = t('res.again');
    } else {
      text.innerHTML = t('res.deposit_done_html', { usd: esc(fmtUsd(+d.received_usd)) });
      $('#resMainText').textContent = t('res.topup_steam');
    }
    confetti();
    haptic('success');
  } else if (d.stage === 'failed') {
    mark.classList.add('fail');
    icon.setAttribute('d', 'M7 7l10 10M17 7 7 17');
    title.textContent = t('res.failed');
    text.textContent = t('res.failed_text', { reason: d.error ? t('res.reason', { reason: d.error }) : '', usd: fmtUsd(+d.price_usd) });
    $('#resMainText').textContent = t('res.order_again');
    main.onclick = () => { prefill(d); location.hash = '#/'; };
  } else {
    mark.classList.add('idle');
    icon.setAttribute('d', d.stage === 'expired' ? 'M12 7v5l3 2' : 'M7 7l10 10M17 7 7 17');
    title.textContent = d.stage === 'expired' ? t('pay.expired') : t('res.cancelled');
    text.textContent = t('res.cancelled_text');
    $('#resMainText').textContent = kind === 'order' ? t('res.new_order') : t('res.new_invoice');
    main.onclick = () => { if (kind === 'order') { prefill(d); location.hash = '#/'; } else location.hash = '#/deposit'; };
  }
  refreshUser();
}

function prefill(o) {
  $('#login').value = o.steam_login;
  if (o.currency !== S.currency) { S.currency = o.currency; markCurrency(); renderChips(); $('#amountSign').textContent = sign(o.currency); }
  $('#amount').value = String(+o.amount);
  onAmount();
  $('#login').dispatchEvent(new Event('input'));
}

function confetti() {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const r = $('#resMark').getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const colors = ['#2B59FF', '#F7F7F5', '#4B74FF', '#F7F7F5'];
  for (let i = 0; i < 28; i++) {
    const c = el('span', 'confetti');
    c.style.left = `${cx}px`; c.style.top = `${cy}px`;
    c.style.background = colors[i % colors.length];
    document.body.appendChild(c);
    const a = Math.random() * Math.PI * 2, dist = 90 + Math.random() * 160;
    const x = Math.cos(a) * dist, y = Math.sin(a) * dist - 60;
    c.animate([
      { transform: 'translate(-50%,-50%) scale(.4) rotate(0)', opacity: 1 },
      { transform: `translate(calc(-50% + ${x}px), calc(-50% + ${y}px)) scale(1) rotate(${Math.random() * 360}deg)`, opacity: 1, offset: 0.55 },
      { transform: `translate(calc(-50% + ${x * 1.15}px), calc(-50% + ${y + 140}px)) scale(.8) rotate(${Math.random() * 720}deg)`, opacity: 0 },
    ], { duration: 1300 + Math.random() * 500, delay: 900, easing: 'cubic-bezier(.2,.8,.2,1)', fill: 'both' })
      .finished.then(() => c.remove());
  }
}

// pay view buttons
$('#payBack').addEventListener('click', () => { location.hash = S.pay?.kind === 'deposit' ? '#/deposit' : '#/'; });
$('#copyAmount').addEventListener('click', (e) => copy($('#payAmount').textContent, e.currentTarget));
$('#copyAddr').addEventListener('click', (e) => copy($('#payAddr').textContent, e.currentTarget));
$('#copyMemo').addEventListener('click', (e) => copy($('#payMemo').textContent, e.currentTarget));
$('#cancelBtn').addEventListener('click', async (e) => {
  const b = e.currentTarget;
  if (!b.dataset.sure) {
    b.dataset.sure = '1';
    b.textContent = t('pay.cancel_confirm');
    setTimeout(() => { delete b.dataset.sure; b.textContent = t('pay.cancel'); }, 4000);
    return;
  }
  try {
    const o = await api(`/api/orders/${S.pay.id}/cancel`, { method: 'POST' });
    renderPay('order', o);
  } catch (err) { toast(err.message, true); }
});
$('#devPayBtn').addEventListener('click', async () => {
  try { await api(`/api/dev/pay/${S.pay.id}`, { method: 'POST' }); toast(t('pay.dev_done')); }
  catch (e) { toast(e.message, true); }
});
$('#claimBtn').addEventListener('click', () => {
  const m = openModal('tplClaim');
  $('#txid', m.node).addEventListener('input', (e) => e.target.removeAttribute('aria-invalid'));
  $('.cl-go', m.node).addEventListener('click', async () => {
    const input = $('#txid', m.node);
    const txid = input.value.trim();
    if (txid.length < 20) { input.setAttribute('aria-invalid', 'true'); input.focus(); toast(t('claim.paste'), true); return; }
    const base = S.pay.kind === 'order' ? '/api/orders/' : '/api/deposits/';
    try {
      const r = await api(`${base}${S.pay.id}/claim`, { method: 'POST', body: { txid } });
      m.close();
      toast(r.message);
    } catch (e) { toast(e.message, true); }
  });
});

// ------------------------------------------------------------------ cabinet (orders / balance / API)
const PILL = {  // order status → [text key, colour]
  awaiting_payment: ['pill.awaiting_payment', 'wait'], paid: ['pill.paid', 'wait'], submitting: ['pill.in_progress', 'wait'],
  queued: ['pill.queued', 'wait'], uncertain: ['pill.in_progress', 'wait'], processing: ['pill.in_progress', 'wait'],
  delivered: ['pill.delivered', 'ok'], rejected: ['pill.rejected', 'bad'], expired: ['pill.expired', ''], cancelled: ['pill.cancelled', ''],
};

function openCabinet(tab = 'orders') {
  if (!S.user) { openLogin(); return; }
  const m = openModal('tplCabinet');
  const seg = $('.cb-tabs', m.node);
  const loaded = {};
  const show = (name) => {
    $$('button[data-tab]', seg).forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    $$('section[data-pane]', m.node).forEach((p) => { p.hidden = p.dataset.pane !== name; });
    requestAnimationFrame(() => moveThumb(seg));
    if (!loaded[name]) { loaded[name] = true; LOADERS[name](m); }
  };
  $$('button[data-tab]', seg).forEach((b) => b.addEventListener('click', () => show(b.dataset.tab)));
  $('.cb-deposit', m.node).addEventListener('click', () => { m.close(); location.hash = '#/deposit'; });
  show(tab);
}

const LOADERS = {
  async orders(m) {
    const list = $('.cb-orders', m.node);
    try {
      const { orders } = await api('/api/orders');
      list.innerHTML = '';
      if (!orders.length) { list.replaceChildren(el('div', 'empty', t('cb.no_orders'))); return; }
      for (const o of orders) {
        const [label, cls] = PILL[o.status] || [o.status, ''];
        const b = el('button', 'o-item');
        b.type = 'button';
        b.innerHTML = '<span class="o-ic"><svg><use href="#i-steam"/></svg></span><span class="o-main"><b></b><span></span></span><span class="pill"></span>';
        $('.o-main b', b).textContent = `${fmtFiat(+o.amount, o.currency)} → ${o.steam_login}`;
        $('.o-main span', b).textContent = `${o.id} · ${fmtDate(o.created_at)}`;
        const pill = $('.pill', b);
        pill.textContent = PILL[o.status] ? t(label) : label;
        if (cls) pill.classList.add(cls);
        b.addEventListener('click', () => { m.close(); location.hash = `#/order/${o.id}`; });
        list.appendChild(b);
      }
    } catch (e) { list.innerHTML = ''; list.append(el('div', 'empty', e.message)); }
  },

  async balance(m) {
    const box = $('.cb-history', m.node);
    try {
      const r = await api('/api/balance/history');
      $('.cb-bal', m.node).textContent = fmtUsd(+r.balance);
      if (S.user) { S.user.balance = r.balance; renderUser(); }
      box.innerHTML = '';
      if (!r.items.length) { box.replaceChildren(el('div', 'empty', t('cb.no_history'))); return; }
      for (const it of r.items) {
        const row = el('div', 'h-item');
        const main = el('div', 'h-main');
        main.append(el('b', null, it.title), el('span', null, `${fmtDate(it.created_at)}${it.comment ? ' · ' + it.comment : ''}`));
        const v = +it.amount;
        row.append(main, el('span', 'h-amt num' + (v > 0 ? ' plus' : ''), `${v > 0 ? '+' : ''}${fmtUsd(v)}`));
        box.appendChild(row);
      }
    } catch (e) { box.innerHTML = ''; box.append(el('div', 'empty', e.message)); }
  },

  async api(m) { await renderApiPane(m); },
};

async function renderApiPane(m) {
  const pane = $('.cb-api', m.node);
  let acc;
  try { acc = await api('/api/account'); } catch (e) { pane.innerHTML = ''; pane.append(el('div', 'empty', e.message)); return; }
  pane.innerHTML = '';

  // intro
  const intro = el('div', 'api-block');
  intro.innerHTML = t('api.intro_html');
  intro.append(kv(t('api.discount'), `${acc.user.discount}%`), kv(t('api.balance'), fmtUsd(+acc.user.balance)));
  pane.append(intro);

  // key
  const keyBlock = el('div', 'api-block');
  keyBlock.append(el('h4', null, t('api.key_title')));
  const k = acc.api_key;
  if (k) {
    keyBlock.append(kv(t('api.key'), `${k.prefix}…`), kv(t('api.created'), fmtDate(k.created_at)),
      kv(t('api.last'), k.last_used_at ? `${fmtDate(k.last_used_at)} · ${k.last_ip || ''}` : t('api.never')));
  } else {
    keyBlock.append(el('p', null, t('api.no_key')));
  }
  const keyBtns = el('div', 'btn-row');
  keyBtns.style.marginTop = '12px';
  const gen = el('button', 'btn-sm primary', k ? t('api.reissue') : t('api.create'));
  gen.type = 'button';
  gen.onclick = async () => {
    if (k && !confirm(t('api.reissue_confirm'))) return;
    try {
      const r = await api('/api/account/api-key', { method: 'POST' });
      await renderApiPane(m);
      showSecret($('.cb-api', m.node), t('api.your_key'), r.key);
    } catch (e) { toast(e.message, true); }
  };
  keyBtns.append(gen);
  if (k) {
    const rev = el('button', 'btn-sm danger', t('api.revoke'));
    rev.type = 'button';
    rev.onclick = async () => {
      if (!confirm(t('api.revoke_confirm'))) return;
      try { await api('/api/account/api-key', { method: 'DELETE' }); await renderApiPane(m); toast(t('api.revoked')); }
      catch (e) { toast(e.message, true); }
    };
    keyBtns.append(rev);
  }
  keyBlock.append(keyBtns);
  pane.append(keyBlock);

  // settings
  const set = el('div', 'api-block');
  set.innerHTML = t('api.settings_html');
  $('#apiIps', set).value = k?.allowed_ips || '';
  $('#apiIps', set).disabled = !k;
  if (!k) $('#apiIps', set).placeholder = t('api.create_first');
  $('#apiHook', set).value = acc.webhook_url || '';
  const btns = el('div', 'btn-row');
  const save = el('button', 'btn-sm primary', t('api.save'));
  save.type = 'button';
  save.onclick = async () => {
    try {
      const r = await api('/api/account/settings', { method: 'POST',
        body: { allowed_ips: $('#apiIps', set).value, webhook_url: $('#apiHook', set).value } });
      toast(t('api.saved'));
      if (r.webhook_secret) showSecret(set, t('api.secret'), r.webhook_secret);
    } catch (e) { toast(e.message, true); }
  };
  btns.append(save);
  if (acc.webhook_url) {
    const test = el('button', 'btn-sm', t('api.test'));
    test.type = 'button';
    test.onclick = async () => {
      try { await api('/api/account/webhook-test', { method: 'POST' }); toast(t('api.test_sent')); }
      catch (e) { toast(e.message, true); }
    };
    const sec = el('button', 'btn-sm', t('api.new_secret'));
    sec.type = 'button';
    sec.onclick = async () => {
      if (!confirm(t('api.new_secret_confirm'))) return;
      try {
        const r = await api('/api/account/webhook-secret', { method: 'POST' });
        showSecret(set, t('api.new_secret_saved'), r.webhook_secret);
      } catch (e) { toast(e.message, true); }
    };
    btns.append(test, sec);
  }
  set.append(btns);
  pane.append(set);
}

function kv(k, v) {
  const row = el('div', 'kv');
  row.append(el('span', null, k), el('b', null, v));
  return row;
}

function showSecret(container, label, value) {
  container.querySelector('.secret')?.remove();
  const box = el('div', 'secret');
  box.append(el('div', null, label), copyBox(value));
  container.prepend(box);
  box.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

// ------------------------------------------------------------------ footer modals
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-open]');
  if (!t) return;
  openModal(t.dataset.open === 'faq' ? 'tplFaq' : 'tplTerms');
});

// ------------------------------------------------------------------ coming soon: the FunPay / Playerok plugin
// "Notify me": signed in — straight onto the waiting list; otherwise through the bot (/start plugin).
function markSoon(joined) {
  const cta = $('#soonCta');
  cta.classList.toggle('done', joined);
  $('span', cta).textContent = joined ? t('soon.joined') : t('soon.cta');
}

function initSoon() {
  const cta = $('#soonCta');
  const link = S.cfg.bot ? `https://t.me/${S.cfg.bot}?start=plugin` : '';
  cta.hidden = !(link || S.cfg.dev_login);
  if (link) cta.href = link;
  markSoon(!!S.cfg.waitlist);  // the server remembers, so the card says so after a reload too
  cta.addEventListener('click', async (ev) => {
    if (S.user) {
      ev.preventDefault();
      try {
        const r = await api('/api/waitlist', { method: 'POST' });
        haptic('success');
        toast(r.new ? t('soon.toast_new') : t('soon.toast_again'));
        markSoon(true);
      } catch (e) { toast(e.message, true); }
    } else if (!link) { ev.preventDefault(); openLogin(); }
    else if (TG.app) { ev.preventDefault(); TG.app.openTelegramLink(link); }
  });
}

// ------------------------------------------------------------------ boot
function unavailable(msg) {
  S.unavailable = msg;
  $('#cta').disabled = true;
  $('#ctaText').textContent = msg;
}

function showOfflineNotice() {
  if ($('.form-notice', $('#viewForm'))) return;
  const notice = el('div', 'form-notice');
  notice.setAttribute('role', 'status');
  notice.append(t('offline.notice'));
  const retry = el('a', 'link-btn', t('offline.reload'));
  retry.href = location.href;
  retry.addEventListener('click', (e) => { e.preventDefault(); location.reload(); });
  notice.append(retry, '.');
  $('#viewForm').insertBefore(notice, $('.card-head', $('#viewForm')));
}

async function refreshRates() {
  try {
    const r = await api('/api/rates');
    if (Object.keys(r.rates).length) S.cfg.rates = r.rates;
    S.cfg.prices = r.prices;
    compute();
    computeDeposit();
  } catch { /* keep old */ }
}

async function boot() {
  $('#year').textContent = new Date().getFullYear();
  startBackground();
  const miniApp = inTelegram();
  let full = true;
  try { full = !sessionStorage.getItem('sh_intro'); sessionStorage.setItem('sh_intro', '1'); } catch { /* ignore */ }
  const intro = miniApp
    ? Promise.resolve().then(() => { document.body.classList.remove('intro-on'); $('#intro')?.remove(); })  // app: no intro
    : Promise.resolve().then(() => playIntro({ full })).catch(() => {
      document.body.classList.remove('intro-on');
      $('#intro')?.remove();
    });
  $('#cta').disabled = true;
  $('#ctaText').textContent = t('boot.loading');

  try {
    if (miniApp) await initMiniApp();  // logs in first, so the config below already knows the user
    S.cfg = await api('/api/config');
  } catch {
    await intro;
    $('#card').classList.add('card-in');
    unavailable(t('service.unavailable'));
    showOfflineNotice();
    return;
  }
  const cfg = S.cfg;
  S.user = cfg.user;
  if (cfg.support_url) {
    $('#supportLink').href = cfg.support_url;
    $('#supportLink').hidden = false;
    $('#supportLink').addEventListener('click', (ev) => {  // inside Telegram open the chat in Telegram itself
      if (TG.app && /^https:\/\/t\.me\//.test(cfg.support_url)) { ev.preventDefault(); TG.app.openTelegramLink(cfg.support_url); }
    });
  }
  if (!cfg.currencies.length) {
    await intro;
    $('#card').classList.add('card-in');
    unavailable(t('amount.unavailable'));
    return;
  }
  const savedCur = store.get('sh_cur');
  if (savedCur && cfg.currencies.some((c) => c.code === savedCur)) S.currency = savedCur;
  else if (!cfg.currencies.some((c) => c.code === S.currency)) S.currency = cfg.currencies[0].code;
  $('#amountSign').textContent = sign(S.currency);
  renderCurrencies();
  renderChips();
  $('#amount').value = String(curCfg().chips[1]);
  const savedLogin = store.get('sh_login');
  if (savedLogin) $('#login').value = savedLogin;
  pick.render();
  dpick.render();
  renderDepositForm();
  if (!cfg.methods.length) unavailable(t('payments.soon'));
  onAmount();
  renderUser();
  initSoon();
  i18nHooks.beforeSwitch = (lang) => (S.user ? api('/api/me/lang', { method: 'POST', body: { lang } }) : null);
  route();
  setInterval(refreshRates, 60000);

  await intro;
  $('#card').classList.add('card-in');
  requestAnimationFrame(() => moveThumb($('#curSeg')));
  if (savedLogin && savedLogin.length >= 3) checkLogin(savedLogin);
}

boot();
