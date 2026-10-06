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
  /** started_at запуска, которому нужен перезапуск после правок из этого окна (см. noteRestart). */
  restartFor: null,
  els: {},
};

function botState(status) {
  return BOT_STATES[status] || BOT_STATES.stopped;
}

function botStatus() {
  return (App.overview && App.overview.bot && App.overview.bot.status) || 'stopped';
}

/**
 * Бот работает, но не может делать своё дело: Playerok не принимает токен,
 * нет связи или не сохраняется state.json. Статус при этом «running» —
 * без этой проверки интерфейс показывал бы зелёное «Бот работает».
 */
function botProblem(ov = App.overview) {
  const bot = ov && ov.bot;
  return bot && bot.status === 'running' && bot.problem ? bot.problem : null;
}

const PROBLEMS = {
  auth: { title: 'Playerok не принимает токен', short: 'Токен не принят' },
  network: { title: 'Нет связи с Playerok', short: 'Нет связи с Playerok' },
  state: { title: 'Бот не может сохранить данные', short: 'Ошибка файла state.json' },
};

function problemInfo(problem) {
  return PROBLEMS[problem.kind] || { title: 'Бот работает с ошибками', short: 'Есть проблема' };
}

/*
 * Перезапуск нужен, если работающему боту поменяли настройки, которые он
 * читает только при запуске. Об этом говорит бэкенд (bot.restart_required),
 * а результат сохранения из этого окна запоминаем сами — на случай старого
 * бэкенда. Подсказка общая для всех страниц и живёт до перезапуска.
 */
const RESTART_PENDING = 'starting';

function noteRestart(result) {
  if (!result || !result.restart_required) return;
  const bot = App.overview && App.overview.bot;
  App.restartFor = (bot && bot.status === 'running' && bot.started_at) || RESTART_PENDING;
  Bus.emit('restart', true);
}

function trackRestart(ov) {
  if (!App.restartFor) return;
  const { status, started_at: startedAt } = ov.bot;
  if (status === 'starting') return;
  if (status !== 'running') App.restartFor = null;
  else if (App.restartFor === RESTART_PENDING) App.restartFor = startedAt;
  else if (startedAt !== App.restartFor) App.restartFor = null;
}

function restartNeeded(ov = App.overview) {
  if (!ov || !ov.bot || ov.bot.status !== 'running') return false;
  return Boolean(ov.bot.restart_required || App.restartFor);
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
      App.overview = { ...App.overview, bot: { ...App.overview.bot, status: result.status, error: null, problem: null } };
      trackRestart(App.overview);
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
  noteRestart(result);
  if (result && result.restart_required) {
    toast.ok('Настройки сохранены', {
      text: 'Бот работает со старыми настройками — перезапустите его, чтобы применить.',
      action: {
        label: 'Перезапустить',
        // Тост мог пережить остановку бота — тогда не запускаем его заново.
        onClick: () => (botStatus() === 'running' ? botAction('restart') : toast.info('Бот уже остановлен', { text: 'Новые настройки применятся при следующем запуске.' })),
      },
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
  trackRestart(overview);
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
    h(
      'div',
      { class: 'sidebar__foot' },
      h('span', {}, 'SupplierHub'),
      // В браузере нет окна, которое можно закрыть: без этой кнопки приложение и бот остались бы висеть в фоне.
      isBrowserMode()
        ? h('button', { type: 'button', class: 'sidebar__quit', title: 'Остановить бота и закрыть SupplierHub', onClick: () => quitApp() }, icon('close', 13), 'Выйти')
        : h('span', {}, version),
    ),
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
  const hasToken = !(ov && ov.setup && !ov.setup.token);
  const problem = botProblem(ov);
  const restart = restartNeeded(ov);
  const key = JSON.stringify([status, minutes, attention, low, username, App.botBusy, hasToken, problem && problem.kind, restart]);
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
  // Пока бот входит в аккаунт, его можно остановить: вход может долго висеть на сети.
  const busy = App.botBusy || status === 'stopping';
  const active = status === 'running' || status === 'starting';
  let title = st.short;
  let tone = st.tone;
  let sub = hasToken ? 'Не запущен' : 'Нужен токен Playerok';
  if (problem) {
    title = 'Не работает';
    tone = 'warning';
    sub = problemInfo(problem).short;
  } else if (status === 'running') sub = [username, fmtDuration(ov.bot.uptime_sec, true)].filter(Boolean).join(' · ') || 'В работе';
  else if (status === 'starting') sub = 'Вход в Playerok…';
  else if (status === 'stopping') sub = 'Завершаем задачи…';
  else if (status === 'error') sub = 'Причина — на главной';
  else if (username && hasToken) sub = username;

  // Без токена «Запустить» закончился бы ошибкой — сразу ведём туда, где его вписать.
  let action;
  if (active) {
    action = btn('Остановить', { kind: 'secondary', icon: busy ? null : 'stop', size: 'sm', disabled: busy, onClick: () => botAction('stop'), cls: 'botbox__btn' });
  } else if (!hasToken) {
    action = btn('Указать токен', { kind: 'primary', icon: busy ? null : 'key', size: 'sm', disabled: busy, onClick: () => navigate('settings?focus=token'), cls: 'botbox__btn' });
  } else {
    action = btn('Запустить', { kind: 'primary', icon: busy ? null : 'play', size: 'sm', disabled: busy, onClick: () => botAction('start'), cls: 'botbox__btn' });
  }
  if (busy) action.prepend(h('span', { class: 'spinner', 'aria-hidden': 'true' }));

  // Подсказка о перезапуске видна на любой странице, пока бот не перезапущен.
  const restartBox =
    restart &&
    !busy &&
    h(
      'div',
      { class: 'botbox__restart', role: 'status' },
      h('span', { class: 'botbox__restart-text' }, icon('alert', 14), 'Бот работает со старыми настройками'),
      btn('Перезапустить', { kind: 'primary', size: 'sm', icon: 'restart', onClick: () => botAction('restart'), cls: 'botbox__btn' }),
    );

  replace(
    App.els.botBox,
    h(
      'div',
      { class: 'botbox__status' },
      h('span', { class: ['dot', `dot--${tone}`, status === 'running' && !problem && 'dot--pulse'], 'aria-hidden': 'true' }),
      h('div', { class: 'botbox__text' }, h('div', { class: 'botbox__title' }, title), h('div', { class: 'botbox__sub', title: sub }, sub)),
    ),
    restartBox,
    action,
  );
}

/* ------------------------------------------------------------- Выход */

function isBrowserMode() {
  return Boolean(App.info && App.info.mode === 'browser');
}

/** Остановить бота и закрыть приложение (в режиме браузера окна, которое можно закрыть, нет). */
async function quitApp() {
  const running = ['running', 'starting'].includes(botStatus());
  const ok = await confirmDialog({
    title: 'Выйти из SupplierHub?',
    text: running
      ? 'Бот остановится и перестанет выдавать товар, пока вы снова не запустите SupplierHub.'
      : 'SupplierHub закроется. Чтобы вернуться, запустите его ярлыком.',
    confirmLabel: running ? 'Остановить и выйти' : 'Выйти',
    danger: running,
  });
  if (!ok) return;
  try {
    await api('quit_app');
  } catch (exc) {
    toast.error('Не удалось закрыть SupplierHub', { text: exc.message });
    return;
  }
  Poller.stop('state');
  Layers.closeAll();
  if (App.page) App.page.teardown();
  App.page = null;
  showFatal('Бот остановлен. Эту вкладку можно закрыть, а чтобы вернуться, запустите SupplierHub ярлыком.', {
    title: 'SupplierHub закрыт',
    retry: false,
    art: 'stopped',
  });
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

/** Экран вместо интерфейса: нет связи, устаревшая ссылка или приложение закрыто. */
function showFatal(message, { title = 'Не удалось подключиться', retry = true, art = 'error' } = {}) {
  const host = document.getElementById('app');
  replace(
    host,
    h(
      'div',
      { class: ['splash', 'splash--message', art === 'error' && 'splash--error'] },
      logoArt(art, 34),
      h('h1', { class: 'splash__title' }, title),
      h('p', { class: 'splash__text' }, message),
      retry && btn('Повторить', { kind: 'primary', icon: 'restart', onClick: () => location.reload() }),
    ),
  );
}

async function boot() {
  try {
    App.info = await api('get_app_info');
  } catch (exc) {
    showFatal(exc.message, exc.final ? { title: exc.title, retry: false } : {});
    return;
  }
  buildShell();
  document.body.classList.add('is-ready');
  Bus.on('overview', renderChrome);
  Bus.on('bot-busy', renderChrome);
  Bus.on('restart', renderChrome);
  await refreshState();
  renderChrome();
  Poller.start('state', refreshState, 2000, 2000);
  window.addEventListener('hashchange', route);
  route();
}

document.addEventListener('DOMContentLoaded', boot);
