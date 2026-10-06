'use strict';
/*
 * SupplierHub — общие помощники интерфейса.
 *
 * Здесь: построение DOM без innerHTML, иконки в стиле логотипа, связь с
 * приложением (pywebview или локальный HTTP-мост), форматирование и опрос.
 * Скрипты подключаются обычными <script> (без модулей), чтобы страница
 * открывалась и по file://, и с локального сервера.
 */

/** Реестр страниц: каждая страница из js/pages регистрирует себя здесь. */
const Pages = {};

/* ------------------------------------------------------------------ DOM */

const SVG_NS = 'http://www.w3.org/2000/svg';
/** Атрибуты, которые надёжнее ставить свойством элемента. */
const DOM_PROPS = new Set(['value', 'checked', 'disabled', 'readOnly', 'hidden', 'selected']);

function setAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') {
      const cls = Array.isArray(value) ? value.filter(Boolean).join(' ') : value;
      if (cls) el.setAttribute('class', cls);
    } else if (key === 'style') {
      for (const [prop, val] of Object.entries(value)) {
        if (val !== null && val !== undefined) el.style.setProperty(prop, String(val));
      }
    } else if (key === 'dataset') {
      for (const [name, val] of Object.entries(value)) el.dataset[name] = String(val);
    } else if (key === 'ref') {
      value(el);
    } else if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (DOM_PROPS.has(key)) {
      el[key] = value;
    } else {
      el.setAttribute(key, value === true ? '' : String(value));
    }
  }
}

function addChildren(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) addChildren(el, child);
    else if (child instanceof Node) el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  }
}

/**
 * Создать элемент. Строки-дети всегда вставляются как текст, поэтому данные
 * из Playerok (названия, сообщения, товары) не могут стать разметкой.
 */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) setAttrs(el, attrs);
  addChildren(el, children);
  return el;
}

function svgEl(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  if (attrs) setAttrs(el, attrs);
  addChildren(el, children);
  return el;
}

/** Заменить содержимое элемента. */
function replace(el, ...children) {
  el.replaceChildren();
  addChildren(el, children);
  return el;
}

/* --------------------------------------------------------------- Иконки */

// Линейные иконки 24×24. Геометрия перекликается с плитками логотипа.
const ICONS = {
  home: [
    ['rect', { x: 4, y: 4, width: 7, height: 7, rx: 2 }],
    ['rect', { x: 13, y: 4, width: 7, height: 7, rx: 2 }],
    ['rect', { x: 4, y: 13, width: 7, height: 7, rx: 2 }],
    ['rect', { x: 13, y: 13, width: 7, height: 7, rx: 2 }],
  ],
  ring: [['circle', { cx: 12, cy: 12, r: 7 }]],
  diamond: [['path', { d: 'M12 3.8 20.2 12 12 20.2 3.8 12Z' }]],
  rect: [['rect', { x: 3.5, y: 6.5, width: 17, height: 11, rx: 3 }]],
  plus: [['path', { d: 'M12 5v14M5 12h14' }]],
  relist: [
    ['path', { d: 'M19.4 13.2A7.5 7.5 0 0 1 6.3 17' }],
    ['path', { d: 'M4.6 10.8A7.5 7.5 0 0 1 17.7 7' }],
    ['path', { d: 'M18.2 3.6v3.8h-3.8' }],
    ['path', { d: 'M5.8 20.4v-3.8h3.8' }],
  ],
  sliders: [
    ['path', { d: 'M4 7.5h8.5M17.5 7.5H20M4 16.5h2.5M11.5 16.5H20' }],
    ['circle', { cx: 15, cy: 7.5, r: 2.5 }],
    ['circle', { cx: 9, cy: 16.5, r: 2.5 }],
  ],
  logs: [['path', { d: 'M5 6.5h14M5 10.5h14M5 14.5h9M5 18.5h11' }]],
  search: [
    ['circle', { cx: 11, cy: 11, r: 6.2 }],
    ['path', { d: 'm19.5 19.5-4-4' }],
  ],
  close: [['path', { d: 'M6.5 6.5l11 11M17.5 6.5l-11 11' }]],
  check: [['path', { d: 'm5 12.5 4.5 4.5L19 7.5' }]],
  copy: [
    ['rect', { x: 8.5, y: 8.5, width: 11.5, height: 11.5, rx: 3 }],
    ['path', { d: 'M15.5 8.5V7A3 3 0 0 0 12.5 4H7A3 3 0 0 0 4 7v5.5a3 3 0 0 0 3 3h1.5' }],
  ],
  eye: [
    ['path', { d: 'M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z' }],
    ['circle', { cx: 12, cy: 12, r: 3 }],
  ],
  eyeOff: [
    ['path', { d: 'M4 4l16 16' }],
    ['path', { d: 'M10.4 5.6c.5-.1 1-.1 1.6-.1 6 0 9.5 6.5 9.5 6.5a16 16 0 0 1-2.7 3.5' }],
    ['path', { d: 'M6.7 6.9A15.6 15.6 0 0 0 2.5 12S6 18.5 12 18.5c1.7 0 3.2-.5 4.5-1.2' }],
    ['path', { d: 'M9.9 9.9a3 3 0 0 0 4.2 4.2' }],
  ],
  trash: [
    ['path', { d: 'M4.5 7h15M9.5 4h5' }],
    ['path', { d: 'M6.5 7l.8 11.1A2 2 0 0 0 9.3 20h5.4a2 2 0 0 0 2-1.9L17.5 7' }],
  ],
  edit: [
    ['path', { d: 'M14.5 5.5l4 4L9 19H5v-4z' }],
    ['path', { d: 'M12.5 7.5l4 4' }],
  ],
  up: [['path', { d: 'M12 18.5v-13M6.5 11 12 5.5l5.5 5.5' }]],
  down: [['path', { d: 'M12 5.5v13M6.5 13l5.5 5.5 5.5-5.5' }]],
  external: [
    ['path', { d: 'M13.5 4.5h6v6M19.5 4.5l-8.5 8.5' }],
    ['path', { d: 'M18 14v3.5a2.5 2.5 0 0 1-2.5 2.5h-9A2.5 2.5 0 0 1 4 17.5v-9A2.5 2.5 0 0 1 6.5 6H10' }],
  ],
  folder: [
    [
      'path',
      { d: 'M3.5 7.5A2.5 2.5 0 0 1 6 5h3.4l2.1 2.4H18a2.5 2.5 0 0 1 2.5 2.5v6.6A2.5 2.5 0 0 1 18 19H6a2.5 2.5 0 0 1-2.5-2.5Z' },
    ],
  ],
  file: [
    ['path', { d: 'M6.5 3.5h7l4.5 4.5v10a2.5 2.5 0 0 1-2.5 2.5h-9A2.5 2.5 0 0 1 4 18V6a2.5 2.5 0 0 1 2.5-2.5Z' }],
    ['path', { d: 'M13.5 3.5V8H18M8 12.5h8M8 16h5' }],
  ],
  upload: [
    ['path', { d: 'M12 15V4.5M7.5 9 12 4.5 16.5 9' }],
    ['path', { d: 'M4.5 15v2.5A2.5 2.5 0 0 0 7 20h10a2.5 2.5 0 0 0 2.5-2.5V15' }],
  ],
  play: [['path', { d: 'M8 5.8v12.4a.8.8 0 0 0 1.2.7l9.6-6.2a.8.8 0 0 0 0-1.4L9.2 5.1A.8.8 0 0 0 8 5.8Z' }]],
  stop: [['rect', { x: 6.5, y: 6.5, width: 11, height: 11, rx: 2.5 }]],
  restart: [
    ['path', { d: 'M19.5 12a7.5 7.5 0 1 1-2.2-5.3' }],
    ['path', { d: 'M19.5 4.5v4h-4' }],
  ],
  alert: [
    ['path', { d: 'M10.3 4.6a2 2 0 0 1 3.4 0l7 12.1A2 2 0 0 1 19 19.7H5a2 2 0 0 1-1.7-3Z' }],
    ['path', { d: 'M12 9.5v4M12 16.6v.1' }],
  ],
  info: [
    ['circle', { cx: 12, cy: 12, r: 8.5 }],
    ['path', { d: 'M12 11v5.2M12 7.9v.1' }],
  ],
  xCircle: [
    ['circle', { cx: 12, cy: 12, r: 8.5 }],
    ['path', { d: 'M9.2 9.2l5.6 5.6M14.8 9.2l-5.6 5.6' }],
  ],
  chat: [
    [
      'path',
      { d: 'M5.5 5h13A2 2 0 0 1 20.5 7v8a2 2 0 0 1-2 2H11l-4.5 3.5V17h-1a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z' },
    ],
  ],
  refund: [
    ['path', { d: 'M9 5.5 4.5 10 9 14.5' }],
    ['path', { d: 'M5 10h9.5a5 5 0 0 1 0 10H11' }],
  ],
  box: [
    ['path', { d: 'M4 8 12 4l8 4v8l-8 4-8-4Z' }],
    ['path', { d: 'M4 8l8 4 8-4M12 12v8' }],
  ],
  bag: [
    ['path', { d: 'M5.5 8.5h13l-.9 10.2a1.5 1.5 0 0 1-1.5 1.3H7.9a1.5 1.5 0 0 1-1.5-1.3Z' }],
    ['path', { d: 'M9 11V7.5a3 3 0 0 1 6 0V11' }],
  ],
  key: [
    ['circle', { cx: 8, cy: 15.5, r: 4 }],
    ['path', { d: 'M11 12.5 19.5 4M16 7.5l2.5 2.5M14 9.5l1.8 1.8' }],
  ],
  send: [
    ['path', { d: 'M20.5 3.5 10 14' }],
    ['path', { d: 'M20.5 3.5 14 20.5l-4-6.5-6.5-4Z' }],
  ],
  arrowRight: [['path', { d: 'M5 12h14M13 6l6 6-6 6' }]],
  chevronRight: [['path', { d: 'm9.5 6 6 6-6 6' }]],
  wave: [['path', { d: 'M3 12h3.5l2.5-6 4 12 2.5-6H21' }]],
  globe: [
    ['circle', { cx: 12, cy: 12, r: 8.5 }],
    ['path', { d: 'M3.5 12h17M12 3.5c2.4 2.4 3.5 5.2 3.5 8.5s-1.1 6.1-3.5 8.5c-2.4-2.4-3.5-5.2-3.5-8.5s1.1-6.1 3.5-8.5Z' }],
  ],
  clock: [
    ['circle', { cx: 12, cy: 12, r: 8.5 }],
    ['path', { d: 'M12 7.5V12l3 2' }],
  ],
};

/** Линейная иконка (stroke = currentColor). */
function icon(name, size = 18, cls = '') {
  const parts = ICONS[name] || ICONS.info;
  return svgEl(
    'svg',
    {
      class: ['icon', cls],
      width: size,
      height: size,
      viewBox: '0 0 24 24',
      fill: 'none',
      stroke: 'currentColor',
      'stroke-width': 1.8,
      'stroke-linecap': 'round',
      'stroke-linejoin': 'round',
      'aria-hidden': 'true',
      focusable: 'false',
    },
    parts.map(([tag, attrs]) => svgEl(tag, attrs)),
  );
}

// Залитые глифы плиток логотипа (координаты взяты из logo.svg, плитка 44×44).
const GLYPHS = {
  ring: [['circle', { cx: 22, cy: 22, r: 8.6, fill: 'none', stroke: 'currentColor', 'stroke-width': 5.6 }]],
  diamond: [['path', { d: 'M22 9.4 34.6 22 22 34.6 9.4 22Z', fill: 'currentColor' }]],
  rect: [['rect', { x: 10.2, y: 14.5, width: 23.6, height: 15, rx: 3.2, fill: 'currentColor' }]],
  plus: [
    ['rect', { x: 19, y: 11.4, width: 6, height: 21.2, rx: 1.6, fill: 'currentColor' }],
    ['rect', { x: 11.4, y: 19, width: 21.2, height: 6, rx: 1.6, fill: 'currentColor' }],
  ],
  check: [
    [
      'path',
      {
        d: 'M13 22.8l5.6 5.6L31 16',
        fill: 'none',
        stroke: 'currentColor',
        'stroke-width': 5,
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
      },
    ],
  ],
  bang: [
    ['rect', { x: 19, y: 10, width: 6, height: 15.5, rx: 2.4, fill: 'currentColor' }],
    ['circle', { cx: 22, cy: 31.6, r: 3.3, fill: 'currentColor' }],
  ],
  play: [['path', { d: 'M17 13.2v17.6a1.3 1.3 0 0 0 2 1.1l13.4-8.8a1.3 1.3 0 0 0 0-2.2L19 12.1a1.3 1.3 0 0 0-2 1.1Z', fill: 'currentColor' }]],
  stop: [['rect', { x: 14.5, y: 14.5, width: 15, height: 15, rx: 3.4, fill: 'currentColor' }]],
};

function glyph(name) {
  return svgEl(
    'svg',
    { class: 'glyph', viewBox: '0 0 44 44', 'aria-hidden': 'true', focusable: 'false' },
    (GLYPHS[name] || GLYPHS.ring).map(([tag, attrs]) => svgEl(tag, attrs)),
  );
}

/** Плитка как на логотипе: tone = white | blue | muted | success | warning | danger. */
function gtile(name, tone = 'white', size = 40) {
  return h('span', { class: ['gtile', `gtile--${tone}`], style: { '--size': `${size}px` } }, glyph(name));
}

/** Плитка с цифрой (порядковый номер правила, шаг онбординга). */
function ntile(text, tone = 'white', size = 40) {
  return h(
    'span',
    { class: ['gtile', 'gtile--num', `gtile--${tone}`], style: { '--size': `${size}px` } },
    String(text),
  );
}

/* ------------------------------------------------------ Связь с приложением */

const Bridge = (() => {
  const TOKEN_KEY = 'supplierhub.token';
  let token = null;
  let waiter = null;

  // Токен HTTP-моста приходит в адресе (#token=...): запоминаем и убираем из адреса.
  const match = /(?:^#|[#&])token=([^&]*)/.exec(location.hash);
  if (match) {
    token = decodeURIComponent(match[1]);
    try {
      sessionStorage.setItem(TOKEN_KEY, token);
    } catch (_) {
      /* хранилище недоступно — токен живёт только в памяти */
    }
    history.replaceState(null, '', location.pathname + location.search);
  } else {
    try {
      token = sessionStorage.getItem(TOKEN_KEY);
    } catch (_) {
      token = null;
    }
  }

  function nativeApi() {
    const pw = window.pywebview;
    return pw && pw.api && typeof pw.api.get_overview === 'function' ? pw.api : null;
  }

  /** Дождаться pywebview (окно) или убедиться, что есть токен моста (браузер). */
  function ready() {
    if (nativeApi() || token) return Promise.resolve();
    if (!waiter) {
      waiter = new Promise((resolve, reject) => {
        const started = Date.now();
        const check = () => {
          if (nativeApi()) {
            stop();
            resolve();
          } else if (Date.now() - started > 15000) {
            stop();
            waiter = null;
            reject(new Error('Нет связи с приложением. Закройте окно и запустите SupplierHub заново.'));
          }
        };
        const timer = setInterval(check, 50);
        const stop = () => {
          clearInterval(timer);
          window.removeEventListener('pywebviewready', check);
        };
        window.addEventListener('pywebviewready', check);
      });
    }
    return waiter;
  }

  async function overHttp(method, args) {
    let response;
    try {
      response = await fetch(`/api/${encodeURIComponent(method)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-SH-Token': token || '' },
        body: JSON.stringify({ args }),
        cache: 'no-store',
      });
    } catch (_) {
      throw new Error('SupplierHub не отвечает — возможно, приложение закрыто.');
    }
    if (response.status === 403) {
      throw new Error('Нет доступа: откройте интерфейс заново из приложения SupplierHub.');
    }
    if (response.status === 404) throw new Error(`Приложение не знает команду ${method}.`);
    try {
      return await response.json();
    } catch (_) {
      throw new Error(`Ошибка связи с приложением (HTTP ${response.status}).`);
    }
  }

  async function call(method, args) {
    await ready();
    const native = nativeApi();
    let envelope;
    if (native) {
      if (typeof native[method] !== 'function') throw new Error(`Приложение не знает команду ${method}.`);
      try {
        envelope = await native[method](...args);
      } catch (exc) {
        throw new Error((exc && exc.message) || 'Ошибка вызова приложения.');
      }
    } else {
      envelope = await overHttp(method, args);
    }
    if (!envelope || typeof envelope !== 'object') throw new Error('Приложение вернуло пустой ответ.');
    if (envelope.ok) return envelope.data;
    throw new Error(envelope.error || 'Неизвестная ошибка.');
  }

  return { call, ready };
})();

/** Вызвать метод приложения: вернёт data или бросит Error с текстом по-русски. */
function api(method, ...args) {
  return Bridge.call(method, args);
}

/* ------------------------------------------------------- Форматирование */

const NUM = new Intl.NumberFormat('ru-RU');
const MONEY = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const MONEY_CENTS = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];

function fmtInt(value) {
  return NUM.format(Number(value) || 0);
}

function fmtMoney(value) {
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) return '—';
  const number = Number(value);
  return `${(Number.isInteger(number) ? MONEY : MONEY_CENTS).format(number)} ₽`;
}

/** plural(5, ['товар', 'товара', 'товаров']) → «товаров». */
function plural(n, forms) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return forms[2];
  if (b > 1 && b < 5) return forms[1];
  if (b === 1) return forms[0];
  return forms[2];
}

function countText(n, forms) {
  return `${fmtInt(n)} ${plural(Number(n) || 0, forms)}`;
}

function parseTime(iso) {
  if (!iso) return null;
  let date = new Date(iso);
  if (Number.isNaN(date.getTime())) date = new Date(String(iso).replace(/(\.\d{3})\d+/, '$1'));
  return Number.isNaN(date.getTime()) ? null : date;
}

const pad2 = (n) => String(n).padStart(2, '0');

function fmtClock(date, seconds = false) {
  const base = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  return seconds ? `${base}:${pad2(date.getSeconds())}` : base;
}

function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function dayLabel(date) {
  const now = new Date();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameDay(date, now)) return 'сегодня';
  if (sameDay(date, yesterday)) return 'вчера';
  const year = date.getFullYear() !== now.getFullYear() ? ` ${date.getFullYear()}` : '';
  return `${date.getDate()} ${MONTHS[date.getMonth()]}${year}`;
}

/** «сегодня, 14:05» / «вчера, 09:12» / «3 окт, 18:40». */
function fmtDateTime(iso) {
  const date = parseTime(iso);
  return date ? `${dayLabel(date)}, ${fmtClock(date)}` : '—';
}

/** Короткое время для таблиц: сегодня — только часы. */
function fmtShortTime(iso) {
  const date = parseTime(iso);
  if (!date) return '—';
  return sameDay(date, new Date()) ? fmtClock(date) : `${dayLabel(date)}, ${fmtClock(date)}`;
}

function fmtRelative(iso) {
  const date = parseTime(iso);
  if (!date) return '';
  const sec = (Date.now() - date.getTime()) / 1000;
  if (sec < 45) return 'только что';
  if (sec < 3600) return `${Math.max(1, Math.round(sec / 60))} мин назад`;
  if (sec < 6 * 3600 && sameDay(date, new Date())) return `${Math.floor(sec / 3600)} ч назад`;
  return fmtDateTime(iso);
}

/** Длительность: «2 ч 14 мин», «3 д 5 ч». */
function fmtDuration(totalSec) {
  if (totalSec === null || totalSec === undefined) return '';
  const sec = Math.max(0, Math.floor(totalSec));
  const days = Math.floor(sec / 86400);
  const hours = Math.floor((sec % 86400) / 3600);
  const minutes = Math.floor((sec % 3600) / 60);
  if (days) return `${days} д ${hours} ч`;
  if (hours) return `${hours} ч ${minutes} мин`;
  if (minutes) return `${minutes} мин`;
  return 'меньше минуты';
}

/* ------------------------------------------------------ События и опрос */

const Bus = (() => {
  const handlers = new Map();
  return {
    on(event, fn) {
      if (!handlers.has(event)) handlers.set(event, new Set());
      handlers.get(event).add(fn);
      return () => handlers.get(event).delete(fn);
    },
    emit(event, data) {
      for (const fn of [...(handlers.get(event) || [])]) {
        try {
          fn(data);
        } catch (exc) {
          console.error(exc);
        }
      }
    },
  };
})();

/** Периодические задачи. Пока окно скрыто, опрос стоит на паузе. */
const Poller = (() => {
  const tasks = new Map();

  function schedule(task, delay) {
    clearTimeout(task.timer);
    task.timer = setTimeout(() => run(task), delay);
  }

  async function run(task) {
    if (task.stopped) return;
    if (document.hidden) {
      task.paused = true;
      return;
    }
    task.busy = true;
    try {
      await task.fn();
    } catch (exc) {
      console.warn(exc);
    }
    task.busy = false;
    if (task.stopped) return;
    schedule(task, task.again ? 0 : task.interval);
    task.again = false;
  }

  function stop(name) {
    const task = tasks.get(name);
    if (!task) return;
    task.stopped = true;
    clearTimeout(task.timer);
    tasks.delete(name);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    for (const task of tasks.values()) {
      if (task.paused) {
        task.paused = false;
        schedule(task, 0);
      }
    }
  });

  return {
    /** Запустить задачу; возвращает функцию остановки. */
    start(name, fn, interval, delay = 0) {
      stop(name);
      const task = { fn, interval, timer: null, busy: false, again: false, paused: false, stopped: false };
      tasks.set(name, task);
      schedule(task, delay);
      return () => {
        if (tasks.get(name) === task) stop(name);
      };
    },
    stop,
    /** Выполнить задачу прямо сейчас (или сразу после текущего прогона). */
    kick(name) {
      const task = tasks.get(name);
      if (!task) return;
      if (task.busy) task.again = true;
      else schedule(task, 0);
    },
  };
})();

/** Хранилище мелких настроек интерфейса (может быть недоступно). */
const Prefs = {
  get(key, fallback = null) {
    try {
      const value = localStorage.getItem(`supplierhub.${key}`);
      return value === null ? fallback : value;
    } catch (_) {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`supplierhub.${key}`, String(value));
    } catch (_) {
      /* без хранилища просто не запоминаем */
    }
  },
};

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/** Стабильный ключ для сравнения данных между опросами. */
function sameData(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
