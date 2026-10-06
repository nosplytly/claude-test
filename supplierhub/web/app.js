'use strict';
/*
 * SupplierHub — оболочка приложения: боковое меню, маршруты (#/home …),
 * общий опрос состояния бота и ленты, запуск и остановка бота.
 */

const ROUTES = [
  { id: 'home', label: 'Главная', icon: 'home' },
  { id: 'sales', label: 'Продажи', icon: 'ring' },
  { id: 'delivery', label: 'Автовыдача', icon: 'diamond' },
  { id: 'relist', label: 'Перевыставление', icon: 'relist' },
  { id: 'settings', label: 'Настройки', icon: 'sliders' },
  { id: 'logs', label: 'Журнал', icon: 'logs' },
];

const BOT_STATES = {
  running: { title: 'Бот работает', short: 'Работает', tone: 'success' },
  starting: { title: 'Бот запускается…', short: 'Запуск…', tone: 'accent' },
  stopping: { title: 'Бот останавливается…', short: 'Остановка…', tone: 'muted' },
  stopped: { title: 'Бот остановлен', short: 'Остановлен', tone: 'muted' },
  error: { title: 'Бот остановлен из-за ошибки', short: 'Ошибка', tone: 'danger' },
};

const FEED_LIMIT = 300;

const App = {
  info: null,
  overview: null,
  feed: [],
  feedLastId: 0,
  botBusy: false,
  route: null,
  page: null,
  failures: 0,
  els: {},
};

function botState(status) {
  return BOT_STATES[status] || BOT_STATES.stopped;
}

function botStatus() {
  return (App.overview && App.overview.bot && App.overview.bot.status) || 'stopped';
}

function navigate(path) {
  const next = `#/${path}`;
  if (location.hash === next) route();
  else location.hash = next;
}

/* ------------------------------------------------------ Управление ботом */

async function botAction(action) {
  if (App.botBusy) return;
  App.botBusy = true;
  Bus.emit('bot-busy', true);
  try {
    const result = await api(`${action}_bot`);
    if (App.overview && result && result.status) {
      App.overview = { ...App.overview, bot: { ...App.overview.bot, status: result.status, error: null } };
      Bus.emit('overview', App.overview);
    }
    if (action === 'stop') toast.ok('Бот остановлен');
    else if (action === 'restart') toast.info('Бот перезапускается');
  } catch (exc) {
    const setup = /токен|config\.toml|настро|telegram/i.test(exc.message);
    const title = action === 'stop' ? 'Не удалось остановить бота' : 'Не удалось запустить бота';
    toast.error(title, {
      text: exc.message,
      action: setup ? { label: 'Настройки', onClick: () => navigate('settings') } : null,
    });
  } finally {
    App.botBusy = false;
    Bus.emit('bot-busy', false);
    Poller.kick('state');
  }
}

/** Сохранить config.toml и подсказать про перезапуск, если бот работает. */
async function saveConfig(cfg) {
  const result = await api('save_config', cfg);
  if (result && result.restart_required) {
    toast.ok('Настройки сохранены', {
      text: 'Бот работает со старыми настройками — перезапустите его, чтобы применить.',
      action: { label: 'Перезапустить', onClick: () => botAction('restart') },
      timeout: 12000,
    });
  } else {
    toast.ok('Настройки сохранены');
  }
  Poller.kick('state');
  return result;
}

/* --------------------------------------------------------- Общий опрос */

async function refreshState() {
  let overview;
  let feed;
  try {
    [overview, feed] = await Promise.all([api('get_overview'), api('get_feed', App.feedLastId)]);
  } catch (exc) {
    App.failures += 1;
    if (App.failures >= 2) setConnection(false, exc.message);
    return;
  }
  App.failures = 0;
  setConnection(true);
  if (feed && typeof feed.last_id === 'number') {
    if (feed.last_id < App.feedLastId) {
      // Приложение перезапустили — лента началась заново.
      App.feed = [];
      App.feedLastId = 0;
      Bus.emit('feed', []);
    } else {
      const fresh = (feed.items || []).filter((item) => item.id > App.feedLastId);
      if (fresh.length) {
        App.feed.push(...fresh);
        if (App.feed.length > FEED_LIMIT) App.feed.splice(0, App.feed.length - FEED_LIMIT);
        Bus.emit('feed', fresh);
      }
      App.feedLastId = feed.last_id;
    }
  }
  App.overview = overview;
  Bus.emit('overview', overview);
}

function setConnection(ok, message) {
  const banner = App.els.offline;
  if (!banner) return;
  if (ok) {
    banner.hidden = true;
    return;
  }
  replace(
    banner,
    h('span', { class: 'offline__dot', 'aria-hidden': 'true' }),
    h('span', {}, 'Нет связи с приложением — переподключаемся…'),
    message && h('span', { class: 'offline__detail' }, message),
  );
  banner.hidden = false;
}

/* --------------------------------------------------------- Боковое меню */

function buildShell() {
  const nav = h(
    'nav',
    { class: 'nav', 'aria-label': 'Разделы' },
    ROUTES.map((r) =>
      h(
        'a',
        { class: 'nav__item', href: `#/${r.id}`, dataset: { route: r.id } },
        h('span', { class: 'nav__icon' }, icon(r.icon, 18)),
        h('span', { class: 'nav__label' }, r.label),
        h('span', { class: 'nav__badge', hidden: true }),
      ),
    ),
  );
  const botBox = h('div', { class: 'botbox' });
  const version = App.info && App.info.version ? `v${App.info.version}` : '';
  const sidebar = h(
    'aside',
    { class: 'sidebar' },
    h(
      'a',
      { class: 'brand', href: '#/home', 'aria-label': 'SupplierHub — на главную' },
      h('img', { class: 'brand__logo', src: 'assets/logo.svg', alt: '', width: 36, height: 36, draggable: 'false' }),
      h('span', { class: 'brand__text' }, h('span', { class: 'brand__name' }, 'SupplierHub'), h('span', { class: 'brand__caption' }, 'для Playerok')),
    ),
    h('div', { class: 'nav__caption' }, 'Меню'),
    nav,
    h('div', { class: 'sidebar__spacer' }),
    botBox,
    h('div', { class: 'sidebar__foot' }, h('span', {}, 'SupplierHub'), h('span', {}, version)),
  );
  const offline = h('div', { class: 'offline', role: 'status', hidden: true });
  const main = h('main', { class: 'main', id: 'main', tabindex: '-1' }, offline, h('div', { class: 'main__page' }));
  replace(document.getElementById('app'), sidebar, main);
  App.els = { nav, botBox, main, offline, pageHost: main.lastElementChild };
}

let chromeKey = '';

function renderChrome() {
  const ov = App.overview;
  const status = botStatus();
  const minutes = ov && ov.bot && typeof ov.bot.uptime_sec === 'number' ? Math.floor(ov.bot.uptime_sec / 60) : null;
  const attention = ov && ov.stats ? ov.stats.attention : 0;
  const low = ov && ov.low_stock ? ov.low_stock.length : 0;
  const username = ov && ov.account ? ov.account.username : '';
  const key = JSON.stringify([status, minutes, attention, low, username, App.botBusy]);
  if (key === chromeKey) return;
  chromeKey = key;

  for (const link of App.els.nav.querySelectorAll('.nav__item')) {
    const badge = link.querySelector('.nav__badge');
    const value = link.dataset.route === 'sales' ? attention : link.dataset.route === 'delivery' ? low : 0;
    badge.hidden = !value;
    badge.textContent = value ? String(value) : '';
    badge.title = link.dataset.route === 'sales' ? 'Требуют внимания' : 'Мало товара на складе';
  }

  const st = botState(status);
  const busy = App.botBusy || status === 'starting' || status === 'stopping';
  const active = status === 'running' || status === 'starting';
  let sub = 'Не запущен';
  if (status === 'running') sub = [username, fmtDuration(ov.bot.uptime_sec)].filter(Boolean).join(' · ') || 'В работе';
  else if (status === 'starting') sub = 'Вход в Playerok…';
  else if (status === 'stopping') sub = 'Завершаем задачи…';
  else if (status === 'error') sub = 'Причина — на главной';
  else if (username) sub = username;

  const action = active
    ? btn('Остановить', { kind: 'secondary', icon: busy ? null : 'stop', size: 'sm', disabled: busy, onClick: () => botAction('stop'), cls: 'botbox__btn' })
    : btn('Запустить', { kind: 'primary', icon: busy ? null : 'play', size: 'sm', disabled: busy, onClick: () => botAction('start'), cls: 'botbox__btn' });
  if (busy) action.prepend(h('span', { class: 'spinner', 'aria-hidden': 'true' }));

  replace(
    App.els.botBox,
    h(
      'div',
      { class: 'botbox__status' },
      h('span', { class: ['dot', `dot--${st.tone}`, status === 'running' && 'dot--pulse'], 'aria-hidden': 'true' }),
      h('div', { class: 'botbox__text' }, h('div', { class: 'botbox__title' }, st.short), h('div', { class: 'botbox__sub', title: sub }, sub)),
    ),
    action,
  );
}

function markActiveNav() {
  for (const link of App.els.nav.querySelectorAll('.nav__item')) {
    const active = link.dataset.route === App.route;
    link.classList.toggle('is-active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
}

/* ------------------------------------------------------------ Маршруты */

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, query = ''] = raw.split('?');
  return { id: path, params: new URLSearchParams(query) };
}

let routing = false;

async function route() {
  if (routing) return;
  const { id, params } = parseHash();
  const target = ROUTES.find((r) => r.id === id);
  if (!target) {
    const saved = Prefs.get('page');
    const fallback = ROUTES.some((r) => r.id === saved) ? saved : 'home';
    history.replaceState(null, '', `#/${fallback}`);
    route();
    return;
  }
  if (App.page && App.route === id) {
    if (App.page.onParams) App.page.onParams(params);
    return;
  }
  if (App.page && App.page.isDirty && App.page.isDirty()) {
    routing = true;
    const leave = await confirmDialog({
      title: 'Уйти без сохранения?',
      text: 'На странице есть несохранённые изменения. Если уйти, они пропадут.',
      confirmLabel: 'Уйти',
      cancelLabel: 'Остаться',
      danger: true,
    });
    routing = false;
    if (!leave) {
      history.replaceState(null, '', `#/${App.route}`);
      return;
    }
  }
  mountPage(target, params);
}

function mountPage(target, params) {
  if (App.page) App.page.teardown();
  Layers.closeAll();

  const cleanups = [];
  const subtitle = h('p', { class: 'page__subtitle' });
  const ctx = {
    params,
    on(event, fn) {
      cleanups.push(Bus.on(event, fn));
    },
    poll(name, fn, interval, delay = 0) {
      cleanups.push(Poller.start(`page:${name}`, fn, interval, delay));
    },
    every(interval, fn) {
      const timer = setInterval(fn, interval);
      cleanups.push(() => clearInterval(timer));
    },
    onCleanup(fn) {
      cleanups.push(fn);
    },
    setSubtitle(text) {
      subtitle.textContent = text || '';
    },
    get alive() {
      return App.page && App.page.ctx === ctx;
    },
  };

  const factory = Pages[target.id];
  const page = factory(ctx);
  subtitle.textContent = page.subtitle || '';
  const header = h(
    'header',
    { class: 'page__head' },
    h('div', { class: 'page__titles' }, h('h1', { class: 'page__title' }, page.title || target.label), subtitle),
    page.actions && h('div', { class: 'page__actions' }, page.actions),
  );
  const el = h('div', { class: ['page', page.fill && 'page--fill'], dataset: { page: target.id } }, header, page.body);

  App.page = {
    ...page,
    ctx,
    teardown() {
      for (const fn of cleanups) fn();
      if (page.destroy) page.destroy();
    },
  };
  App.route = target.id;
  replace(App.els.pageHost, el);
  App.els.main.scrollTop = 0;
  App.els.main.classList.toggle('main--fill', Boolean(page.fill));
  markActiveNav();
  Prefs.set('page', target.id);
  document.title = `${page.title || target.label} · SupplierHub`;
  if (page.onParams) page.onParams(params);
}

/* ---------------------------------------------------------------- Старт */

function showFatal(message) {
  const host = document.getElementById('app');
  replace(
    host,
    h(
      'div',
      { class: 'splash splash--error' },
      logoArt('error', 34),
      h('h1', { class: 'splash__title' }, 'Не удалось подключиться'),
      h('p', { class: 'splash__text' }, message),
      btn('Повторить', { kind: 'primary', icon: 'restart', onClick: () => location.reload() }),
    ),
  );
}

async function boot() {
  try {
    App.info = await api('get_app_info');
  } catch (exc) {
    showFatal(exc.message);
    return;
  }
  buildShell();
  document.body.classList.add('is-ready');
  Bus.on('overview', renderChrome);
  Bus.on('bot-busy', renderChrome);
  await refreshState();
  renderChrome();
  Poller.start('state', refreshState, 2000, 2000);
  window.addEventListener('hashchange', route);
  route();
}

document.addEventListener('DOMContentLoaded', boot);
