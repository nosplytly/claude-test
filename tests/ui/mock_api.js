/*
 * Фейковый API SupplierHub для разработки интерфейса и UI-тестов.
 *
 * Подключается раньше app.js (Playwright: page.add_init_script) и изображает
 * window.pywebview.api: каждый метод возвращает Promise с конвертом
 * {ok: true, data} или {ok: false, error}, как настоящий бэкенд.
 *
 * Сценарий выбирается параметром адреса ?state=...:
 *   running (по умолчанию) — бот работает, есть продажи, правила, лента и журнал;
 *   stopped — бот остановлен, данные те же;
 *   empty   — первый запуск: нет config.toml, правил, продаж и событий;
 *   error   — бот упал с ошибкой входа;
 *   config  — в config.toml ошибка проверки;
 *   xss     — названия, ники и сообщения содержат HTML (проверка экранирования).
 * Состояние доступно тестам как window.__mock, вызовы — window.__mockCalls.
 */
(() => {
  'use strict';

  if (window.pywebview && window.pywebview.api) return;

  const scenario = new URLSearchParams(location.search).get('state') || 'running';
  const NOW = Date.now();
  const MIN = 60 * 1000;
  const iso = (ms) => new Date(ms).toISOString();
  const ago = (minutes) => iso(NOW - minutes * MIN);
  const XSS = '<img src=x onerror="window.__xss=1"><script>window.__xss=1</script>';
  const tag = (text) => (scenario === 'xss' ? `${text} ${XSS}` : text);
  const clone = (value) => (value === undefined ? null : JSON.parse(JSON.stringify(value)));

  /* ----------------------------------------------------------- Данные */

  function keys(prefix, count) {
    const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    let seed = prefix.length * 7919;
    const next = () => {
      seed = (seed * 16807) % 2147483647;
      return alphabet[seed % alphabet.length];
    };
    return Array.from({ length: count }, () =>
      [0, 1, 2].map(() => Array.from({ length: 4 }, next).join('')).join('-'),
    );
  }

  const defaultConfig = () => ({
    playerok: { token: '', proxy: '', deals_interval: 20, use_websocket: true },
    telegram: { enabled: false, bot_token: '', chat_ids: [], notify_messages: true, messages_interval: 10, proxy: '' },
    delivery: { enabled: true, mark_sent: true },
    relist: { after_sale: true, expired: false, interval_minutes: 30, match: [] },
  });

  const filledConfig = () => ({
    playerok: {
      token: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJOZW9uS2V5cyJ9.mock-signature',
      proxy: '',
      deals_interval: 20,
      use_websocket: true,
    },
    telegram: {
      enabled: true,
      bot_token: '7412345678:AAH4mockTokenForSupplierHubUi_x9',
      chat_ids: [123456789, -1002233445566],
      notify_messages: true,
      messages_interval: 10,
      proxy: '',
    },
    delivery: { enabled: true, mark_sent: true },
    relist: { after_sale: true, expired: false, interval_minutes: 30, match: ['Ключ Steam', 'Genshin'] },
  });

  const filledRules = () => [
    {
      match: tag('Ключ Steam'),
      message: tag('Спасибо за покупку, {buyer}!\nВаш ключ: {product}\nАктивация: Steam → Игры → Активировать в Steam.\nПожалуйста, подтвердите сделку и оставьте отзыв 🙏'),
      stock_file: 'stock/klyuch-steam.txt',
      products_per_sale: 1,
      low_stock_alert: 3,
      stock: scenario === 'xss' ? [...keys('steam', 3), XSS] : keys('steam', 14),
    },
    {
      match: 'Genshin Impact',
      message: 'Привет, {buyer}! Данные для входа в аккаунт:\n{product}\n\nСразу смените пароль и почту.',
      stock_file: 'stock/genshin-impact.txt',
      products_per_sale: 1,
      low_stock_alert: 3,
      stock: ['login: gi_acc_118\\npass: Qm7#tr92\\nпочта: gi118@mail.ru', 'login: gi_acc_121\\npass: Lp0!zx41\\nпочта: gi121@mail.ru'],
    },
    {
      match: 'Аккаунт Minecraft',
      message: 'Ваш аккаунт Minecraft Java:\n{product}\nПочту можно сменить в профиле.',
      stock_file: 'stock/akkaunt-minecraft.txt',
      products_per_sale: 1,
      low_stock_alert: 2,
      stock: [],
    },
    {
      match: 'Гайд',
      message: 'Спасибо за покупку! Ссылка на гайд: https://example.com/guide\nЕсли будут вопросы — пишите в этот чат.',
      stock_file: null,
      products_per_sale: 1,
      low_stock_alert: 0,
      stock: [],
    },
  ];

  const delivery = (status, extra = {}) => ({ status, products: [], error: null, delivered_at: null, ...extra });

  const filledSales = () => [
    { deal_id: '9f21c7a0-5b1e-4c8e-a7d2-1b6f0c9e7d11', item: tag('Ключ Steam — Cyberpunk 2077'), buyer: tag('kirill_play'), price: 1290, status: 'PAID', created_at: ago(1), chat_id: 'c-1001', delivery: delivery('reserved', { products: [keys('cp', 1)[0]] }) },
    { deal_id: '3b8e11d2-7a40-4d55-9b1c-58e2f0a4c372', item: 'Подписка Spotify Premium — 1 месяц', buyer: 'orbit_fm', price: 249, status: 'PENDING', created_at: ago(4), chat_id: 'c-1002', delivery: null },
    { deal_id: '5d0a6c19-2f3b-4c7a-8e61-90b4d2e1f845', item: 'Аккаунт Minecraft Java + смена почты', buyer: 'dendy_kid', price: 790, status: 'PAID', created_at: ago(14), chat_id: 'c-1003', delivery: delivery('no_stock') },
    { deal_id: 'a71f3e58-6c2d-4b90-b3a4-7e5d1c8f2a06', item: 'Ключ Steam — Elden Ring', buyer: 'ShadowFox', price: 2190, status: 'SENT', created_at: ago(32), chat_id: 'c-1004', delivery: delivery('delivered', { products: ['ELDN-7QK2-XM4P-99AB'], delivered_at: ago(31) }) },
    { deal_id: 'c4e92b07-1d6f-4a38-8c5e-2f7b9a0d6e13', item: 'Genshin Impact — аккаунт AR 58, 6 персонажей 5★', buyer: 'Mira_Sun', price: 4290, status: 'SENT', created_at: ago(51), chat_id: 'c-1005', delivery: delivery('delivered', { products: ['login: gi_acc_117\npass: Hn2$kd77\nпочта: gi117@mail.ru'], delivered_at: ago(50) }) },
    { deal_id: 'e8b3d4f1-9a27-4e6c-b5d0-3c1a8f7e2b94', item: 'Гайд: прокачка до 60 уровня за 3 дня', buyer: 'vasya_pro', price: 299, status: 'CONFIRMED', created_at: ago(70), chat_id: 'c-1006', delivery: delivery('delivered', { delivered_at: ago(70) }) },
    { deal_id: '17c5a9e3-4b8d-4f21-a6e0-9d2c7b5f1a38', item: 'Ключ Steam — Hades II', buyer: 'anna.k', price: 1150, status: 'PAID', created_at: ago(118), chat_id: 'c-1007', delivery: delivery('failed', { products: ['HDS2-LL0P-ZZ81-3KQW'], error: 'Playerok вернул 502 Bad Gateway при отправке сообщения в чат' }) },
    { deal_id: '2e9f6b14-8c3a-4d57-9e02-6a1b4c8d3f75', item: 'Буст рейтинга Dota 2 (+500 MMR)', buyer: 'Teemo_main', price: 1800, status: 'PAID', created_at: ago(185), chat_id: 'c-1008', delivery: delivery('no_rule') },
    { deal_id: '6a3d8c21-5e9b-4f04-8a17-b2c6e1d9f048', item: 'Ключ Steam — Stardew Valley', buyer: 'Gleb', price: 459, status: 'CONFIRMED', created_at: ago(300), chat_id: 'c-1009', delivery: delivery('delivered', { products: ['STDW-9KQ1-PL2M-0XZV'], delivered_at: ago(300) }) },
    { deal_id: 'b05e7f92-3c1d-4a68-9b4e-8d2f6a1c7e39', item: 'Ключ Steam — Baldur’s Gate 3', buyer: 'player_one', price: 2690, status: 'ROLLED_BACK', created_at: ago(26 * 60), chat_id: 'c-1010', delivery: delivery('delivered', { products: ['BG3K-77AA-QW12-ZZ90'], delivered_at: ago(26 * 60 - 1) }) },
    { deal_id: 'd9c1a4e6-7f28-4b35-8e90-1a5c3d7b2f64', item: 'Ключ Steam — Hollow Knight', buyer: 'kate_w', price: 399, status: 'FAILED', created_at: ago(30 * 60), chat_id: 'c-1011', delivery: null },
    { deal_id: '48f2b6d0-1e7c-4a93-b5d8-6c0e9f3a2b17', item: 'Аккаунт Minecraft Java + смена почты', buyer: 'noname_42', price: 790, status: 'CONFIRMED', created_at: ago(52 * 60), chat_id: 'c-1012', delivery: delivery('skipped') },
  ];

  const dealUrl = (id) => `https://playerok.com/deal/${id}`;
  const chatUrl = (id) => `https://playerok.com/chats/${id}`;

  function filledFeed() {
    const s = filledSales();
    const items = [
      ['info', 160, 'Бот запущен: NeonKeys', 'Баланс: 18 450,50 ₽\nАвтовыдача: вкл, правил: 4\nПеревыставление: после продажи', null],
      ['info', 150, 'Для лота «Буст рейтинга Dota 2 (+500 MMR)» нет правила автовыдачи — выдайте товар вручную.', '', dealUrl(s[7].deal_id)],
      ['warning', 118, 'Ошибка выдачи по сделке «Ключ Steam — Hades II»: Playerok вернул 502 Bad Gateway. Повторю при следующей проверке.', '', dealUrl(s[6].deal_id)],
      ['error', 96, 'Не удалось выдать товар по сделке «Ключ Steam — Hades II» за 5 попыток: Playerok вернул 502 Bad Gateway. Выдайте вручную.', 'Товар для ручной выдачи:\nHDS2-LL0P-ZZ81-3KQW', dealUrl(s[6].deal_id)],
      ['confirmed', 70, 'Покупатель подтвердил сделку «Гайд: прокачка до 60 уровня за 3 дня» — +299 ₽', '', dealUrl(s[5].deal_id)],
      ['sale', 51, 'Новая продажа: «Genshin Impact — аккаунт AR 58, 6 персонажей 5★» за 4290 ₽', 'Покупатель: Mira_Sun', dealUrl(s[4].deal_id)],
      ['delivered', 50, 'Товар выдан: «Genshin Impact — аккаунт AR 58, 6 персонажей 5★» → Mira_Sun', 'Осталось в genshin-impact.txt: 2 — 📉 пора пополнить', dealUrl(s[4].deal_id)],
      ['message', 44, 'Mira_Sun: Здравствуйте! Всё пришло, спасибо 🙂', 'Открыть чат', chatUrl('c-1005')],
      ['sale', 32, 'Новая продажа: «Ключ Steam — Elden Ring» за 2190 ₽', 'Покупатель: ShadowFox', dealUrl(s[3].deal_id)],
      ['delivered', 31, 'Товар выдан: «Ключ Steam — Elden Ring» → ShadowFox', 'Осталось в klyuch-steam.txt: 14', dealUrl(s[3].deal_id)],
      ['relist', 30, 'Лот «Ключ Steam — Elden Ring» снова в продаже (статус: APPROVED)', '', null],
      ['refund', 22, 'Сделка «Ключ Steam — Baldur’s Gate 3» отменена, деньги возвращены покупателю', '', dealUrl(s[9].deal_id)],
      ['sale', 14, 'Новая продажа: «Аккаунт Minecraft Java + смена почты» за 790 ₽', 'Покупатель: dendy_kid', dealUrl(s[2].deal_id)],
      ['error', 14, 'Закончился товар в akkaunt-minecraft.txt: сделка «Аккаунт Minecraft Java + смена почты» ждёт выдачи. Пополните файл — бот выдаст товар при следующей проверке.', '', dealUrl(s[2].deal_id)],
      ['warning', 13, 'Лот «Аккаунт Minecraft Java + смена почты» не перевыставлен: на складе не хватает товара. Пополните файл и выставьте лот вручную.', '', null],
      ['message', 6, tag('dendy_kid: Добрый день, когда будет аккаунт? Оплатил 10 минут назад'), 'Открыть чат', chatUrl('c-1003')],
      ['sale', 1, tag('Новая продажа: «Ключ Steam — Cyberpunk 2077» за 1290 ₽'), tag('Покупатель: kirill_play'), dealUrl(s[0].deal_id)],
    ];
    return items.map(([kind, minutes, title, text, url], index) => ({ id: index + 1, ts: ago(minutes), kind, title, text, url }));
  }

  function filledLogs() {
    const templates = [
      ['INFO', 'playerok_bot.watchers', 'Проверка продаж: новых сделок нет'],
      ['INFO', 'playerok_bot.watchers', 'Проверка сообщений: 0 новых'],
      ['DEBUG', 'PlayerokAPI.websocket', 'ping → pong 84 мс'],
      ['INFO', 'playerok_bot.relist', 'Перевыставление: проверено лотов 12, истёкших нет'],
      ['INFO', 'playerok_bot.watchers', 'Проверка продаж: новых сделок нет'],
      ['INFO', 'playerok_bot.telegram', 'Уведомление: 🛒 Новая продажа: «Ключ Steam — Elden Ring» за 2190 ₽ | Покупатель: ShadowFox'],
      ['INFO', 'playerok_bot.delivery', 'Сделка a71f3e58-6c2d-4b90-b3a4-7e5d1c8f2a06: товар выдан'],
      ['WARNING', 'playerok_bot.delivery', 'Сделка 17c5a9e3-4b8d-4f21-a6e0-9d2c7b5f1a38: попытка выдачи 3 не удалась: Playerok вернул 502 Bad Gateway'],
      ['INFO', 'playerok_bot.watchers', 'Проверка продаж: новых сделок нет'],
      ['WARNING', 'PlayerokAPI.websocket', 'WebSocket закрыт (1006), переподключение через 5 с'],
      ['INFO', 'PlayerokAPI.websocket', 'WebSocket подключён'],
      ['ERROR', 'playerok_bot.delivery', 'Сделка 17c5a9e3-4b8d-4f21-a6e0-9d2c7b5f1a38: не удалось выдать товар за 5 попыток — нужна ручная выдача'],
    ];
    const lines = [
      ['INFO', 'supplierhub', 'SupplierHub 1.0.0 запущен, рабочая папка C:\\Users\\Артур\\Desktop\\SupplierHub'],
      ['INFO', 'supplierhub.runtime', 'Запуск бота…'],
      ['INFO', 'playerok_bot.bot', 'Вход выполнен: NeonKeys, баланс 18450.50 ₽'],
    ];
    for (let i = 0; i < 64; i += 1) lines.push(templates[i % templates.length]);
    lines.push(['INFO', 'playerok_bot.watchers', tag('Новая продажа: Ключ Steam — Cyberpunk 2077 (kirill_play)')]);
    return lines.map(([level, name, message], index) => ({ id: index + 1, ts: iso(NOW - (lines.length - index) * 41 * 1000), level, name, message }));
  }

  /* -------------------------------------------------------- Состояние */

  const state = {
    configExists: true,
    configError: null,
    config: filledConfig(),
    rules: filledRules(),
    sales: filledSales(),
    feed: filledFeed(),
    logs: filledLogs(),
    bot: { status: 'running', error: null, startedAt: NOW - (2 * 3600 + 14 * 60) * 1000 },
    account: { id: 'u_5f3a9c', username: tag('NeonKeys'), balance: 18450.5 },
    opened: [],
  };

  if (scenario === 'stopped') {
    state.bot = { status: 'stopped', error: null, startedAt: null };
  } else if (scenario === 'empty') {
    Object.assign(state, {
      configExists: false,
      config: defaultConfig(),
      rules: [],
      sales: [],
      feed: [],
      account: null,
      bot: { status: 'stopped', error: null, startedAt: null },
      logs: [
        { id: 1, ts: ago(1), level: 'INFO', name: 'supplierhub', message: 'SupplierHub 1.0.0 запущен, рабочая папка C:\\Users\\Артур\\Desktop\\SupplierHub' },
        { id: 2, ts: ago(1), level: 'INFO', name: 'supplierhub', message: 'Файл config.toml не найден — заполните настройки в приложении.' },
      ],
    });
  } else if (scenario === 'error') {
    state.bot = {
      status: 'error',
      error: 'Не удалось подключиться к Playerok: токен отклонён (401 Unauthorized). Возьмите свежий cookie token в браузере и сохраните его в настройках.',
      startedAt: null,
    };
    state.account = null;
    state.logs.push(
      { id: state.logs.length + 1, ts: ago(0.5), level: 'INFO', name: 'supplierhub.runtime', message: 'Запуск бота…' },
      { id: state.logs.length + 2, ts: ago(0.4), level: 'ERROR', name: 'supplierhub.runtime', message: 'Бот остановлен: Не удалось подключиться к Playerok: токен отклонён (401 Unauthorized).' },
    );
  } else if (scenario === 'config') {
    state.bot = { status: 'stopped', error: null, startedAt: null };
    state.configError = 'delivery.rules №2: указан stock_file, но в message нет {product} — покупатель не получил бы товар.';
  }

  let feedSeq = state.feed.length;
  let logSeq = state.logs.length;

  function pushFeed(kind, title, text = '', url = null) {
    feedSeq += 1;
    state.feed.push({ id: feedSeq, ts: iso(Date.now()), kind, title, text, url });
    if (state.feed.length > 300) state.feed.shift();
  }

  function log(level, name, message) {
    logSeq += 1;
    state.logs.push({ id: logSeq, ts: iso(Date.now()), level, name, message });
    if (state.logs.length > 1000) state.logs.shift();
  }

  /* ----------------------------------------------------- Вычисления */

  const DEAL_LABELS = { PAID: 'Оплачена', SENT: 'Отправлена', CONFIRMED: 'Завершена', ROLLED_BACK: 'Возврат', PENDING: 'Ожидает оплаты', FAILED: 'Не состоялась' };
  const DELIVERY_LABELS = { delivered: 'Выдано', reserved: 'Выдаётся', no_stock: 'Нет товара', failed: 'Ошибка выдачи', no_rule: 'Без автовыдачи', skipped: 'Пропущена' };

  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  const isToday = (value) => Boolean(value) && sameDay(new Date(value), new Date());
  const running = () => state.bot.status === 'running';

  function ruleView(rule, index) {
    return {
      id: index,
      match: rule.match,
      message: rule.message,
      stock_file: rule.stock_file,
      products_per_sale: rule.products_per_sale,
      low_stock_alert: rule.low_stock_alert,
      stock_count: rule.stock_file ? rule.stock.length : null,
    };
  }

  function saleView(sale) {
    return {
      deal_id: sale.deal_id,
      url: dealUrl(sale.deal_id),
      item: sale.item,
      buyer: sale.buyer,
      price: sale.price,
      status: sale.status,
      status_label: DEAL_LABELS[sale.status] || '—',
      created_at: sale.created_at,
      chat_url: sale.chat_id ? chatUrl(sale.chat_id) : null,
      delivery: sale.delivery ? { ...sale.delivery, label: DELIVERY_LABELS[sale.delivery.status] || sale.delivery.status } : null,
    };
  }

  function overview() {
    const paid = state.sales.filter((s) => ['PAID', 'SENT', 'CONFIRMED'].includes(s.status) && isToday(s.created_at));
    const cfg = state.config;
    return {
      bot: {
        status: state.bot.status,
        error: state.bot.error,
        started_at: state.bot.startedAt ? iso(state.bot.startedAt) : null,
        uptime_sec: state.bot.startedAt && running() ? Math.floor((Date.now() - state.bot.startedAt) / 1000) : null,
      },
      account: state.account,
      stats: {
        sales_today: paid.length,
        revenue_today: paid.reduce((sum, s) => sum + (s.price || 0), 0),
        delivered_today: state.sales.filter((s) => s.delivery && s.delivery.status === 'delivered' && isToday(s.delivery.delivered_at)).length,
        attention: state.sales.filter((s) => s.delivery && ['failed', 'no_stock'].includes(s.delivery.status)).length,
        stock_total: state.rules.reduce((sum, r) => sum + (r.stock_file ? r.stock.length : 0), 0),
      },
      low_stock: state.rules
        .map((rule, index) => ({ rule, index }))
        .filter(({ rule }) => rule.stock_file && rule.stock.length <= rule.low_stock_alert)
        .map(({ rule, index }) => ({ rule_id: index, match: rule.match, count: rule.stock.length, threshold: rule.low_stock_alert })),
      features: {
        delivery: cfg.delivery.enabled,
        telegram: cfg.telegram.enabled,
        relist_after_sale: cfg.relist.after_sale,
        relist_expired: cfg.relist.expired,
      },
      setup: {
        token: Boolean(cfg.playerok.token),
        telegram: Boolean(cfg.telegram.enabled && cfg.telegram.bot_token && cfg.telegram.chat_ids.length),
        rules: state.rules.length > 0,
      },
      config_error: state.configError,
    };
  }

  function slug(text) {
    const base = text.toLowerCase().replace(/[^a-z0-9а-яё]+/g, '-').replace(/^-+|-+$/g, '') || 'stock';
    let name = `stock/${base}.txt`;
    let n = 2;
    while (state.rules.some((r) => r.stock_file === name)) name = `stock/${base}-${n++}.txt`;
    return name;
  }

  function validateRule(rule, number) {
    const where = `delivery.rules №${number}`;
    if (!String(rule.match || '').trim()) return `${where}: не задано match — часть названия лота`;
    if (!String(rule.message || '').trim()) return `${where}: не задан текст message`;
    if (rule.use_stock && !rule.message.includes('{product}')) {
      return `${where}: указан stock_file, но в message нет {product} — покупатель не получил бы товар.`;
    }
    if (!Number.isInteger(rule.products_per_sale) || rule.products_per_sale < 1) return `${where}: products_per_sale должен быть целым числом ≥ 1`;
    if (!Number.isInteger(rule.low_stock_alert) || rule.low_stock_alert < 0) return `${where}: low_stock_alert должен быть целым числом ≥ 0`;
    if (rule.stock_file && (rule.stock_file.includes('..') || !rule.stock_file.startsWith('stock/'))) {
      return 'Файл склада должен лежать в папке stock рабочей папки.';
    }
    return null;
  }

  function validateConfig(cfg) {
    if (!(Number(cfg.playerok.deals_interval) >= 5)) return 'playerok.deals_interval должен быть числом не меньше 5';
    if (!(Number(cfg.telegram.messages_interval) >= 3)) return 'telegram.messages_interval должен быть числом не меньше 3';
    if (!(Number(cfg.relist.interval_minutes) >= 1)) return 'relist.interval_minutes должен быть числом не меньше 1';
    if (!Array.isArray(cfg.telegram.chat_ids) || cfg.telegram.chat_ids.some((id) => !Number.isInteger(id))) {
      return 'telegram.chat_ids должен быть списком чисел, например [123456789]';
    }
    if (cfg.telegram.enabled && !cfg.telegram.bot_token) {
      return 'Telegram включён, но не задан telegram.bot_token (или переменная окружения TELEGRAM_BOT_TOKEN).';
    }
    if (cfg.telegram.enabled && !cfg.telegram.chat_ids.length) return 'Telegram включён, но список telegram.chat_ids пуст.';
    return null;
  }

  function ruleAt(ruleId) {
    const rule = state.rules[ruleId];
    if (!rule) throw new Error('Правило не найдено — обновите страницу.');
    return rule;
  }

  function stockRule(ruleId) {
    const rule = ruleAt(ruleId);
    if (!rule.stock_file) throw new Error('У этого правила нет склада.');
    return rule;
  }

  /* ------------------------------------------------------------ Бот */

  const timers = [];

  function startBot() {
    state.bot = { status: 'starting', error: null, startedAt: null };
    log('INFO', 'supplierhub.runtime', 'Запуск бота…');
    timers.push(
      setTimeout(() => {
        if (state.bot.status !== 'starting') return;
        state.bot = { status: 'running', error: null, startedAt: Date.now() };
        state.account = state.account || { id: 'u_5f3a9c', username: 'NeonKeys', balance: 18450.5 };
        log('INFO', 'playerok_bot.bot', `Вход выполнен: ${state.account.username}, баланс ${state.account.balance.toFixed(2)} ₽`);
        pushFeed('info', `Бот запущен: ${state.account.username}`, `Баланс: 18 450,50 ₽\nАвтовыдача: ${state.config.delivery.enabled ? `вкл, правил: ${state.rules.length}` : 'выкл'}`);
      }, 1400),
    );
  }

  function stopBot() {
    state.bot = { ...state.bot, status: 'stopping' };
    log('INFO', 'supplierhub.runtime', 'Остановка бота…');
    timers.push(
      setTimeout(() => {
        state.bot = { status: 'stopped', error: null, startedAt: null };
        log('INFO', 'supplierhub.runtime', 'Бот остановлен');
      }, 700),
    );
  }

  // Пока бот работает, журнал понемногу пополняется.
  let tick = 0;
  setInterval(() => {
    if (!running()) return;
    tick += 1;
    log(tick % 4 === 0 ? 'DEBUG' : 'INFO', tick % 4 === 0 ? 'PlayerokAPI.websocket' : 'playerok_bot.watchers', tick % 4 === 0 ? 'ping → pong 91 мс' : 'Проверка продаж: новых сделок нет');
  }, 2500);

  /* ----------------------------------------------------------- Методы */

  const methods = {
    get_app_info: () => ({ version: '1.0.0', workdir: 'C:\\Users\\Артур\\Desktop\\SupplierHub', config_exists: state.configExists, mode: 'window' }),

    get_overview: () => overview(),

    get_feed: (afterId = 0) => ({ items: state.feed.filter((item) => item.id > afterId), last_id: feedSeq }),

    get_logs: (afterId = 0) => ({ items: state.logs.filter((item) => item.id > afterId), last_id: logSeq }),

    start_bot() {
      if (['running', 'starting'].includes(state.bot.status)) throw new Error('Бот уже запущен.');
      if (state.bot.status === 'stopping') throw new Error('Бот ещё останавливается — подождите пару секунд.');
      if (!state.configExists) throw new Error('Сначала заполните настройки: файл config.toml ещё не создан.');
      if (state.configError) throw new Error(state.configError);
      if (!state.config.playerok.token) throw new Error('Не задан токен Playerok: заполните его в настройках.');
      startBot();
      return { status: state.bot.status };
    },

    stop_bot() {
      if (['running', 'starting'].includes(state.bot.status)) stopBot();
      return { status: state.bot.status };
    },

    restart_bot() {
      if (!state.config.playerok.token) throw new Error('Не задан токен Playerok: заполните его в настройках.');
      state.bot = { status: 'stopping', error: null, startedAt: state.bot.startedAt };
      timers.push(setTimeout(startBot, 600));
      return { status: 'stopping' };
    },

    get_sales: () => ({
      items: [...state.sales].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).map(saleView),
      live: running(),
    }),

    get_config: () => state.config,

    save_config(cfg) {
      const merged = {
        playerok: { ...state.config.playerok, ...(cfg.playerok || {}) },
        telegram: { ...state.config.telegram, ...(cfg.telegram || {}) },
        delivery: { ...state.config.delivery, ...(cfg.delivery || {}) },
        relist: { ...state.config.relist, ...(cfg.relist || {}) },
      };
      const problem = validateConfig(merged);
      if (problem) throw new Error(problem);
      state.config = clone(merged);
      state.configExists = true;
      log('INFO', 'supplierhub', 'Настройки сохранены в config.toml');
      return { saved: true, restart_required: running() };
    },

    list_rules: () => ({ items: state.rules.map(ruleView) }),

    save_rule(rule, ruleId = null) {
      const isNew = ruleId === null || ruleId === undefined;
      if (!isNew) ruleAt(ruleId);
      const number = isNew ? state.rules.length + 1 : ruleId + 1;
      const problem = validateRule(rule, number);
      if (problem) throw new Error(problem);
      const previous = isNew ? null : state.rules[ruleId];
      let stockFile = null;
      if (rule.use_stock) stockFile = rule.stock_file || (previous && previous.stock_file) || slug(rule.match);
      const shared = stockFile && state.rules.find((r) => r.stock_file === stockFile);
      const next = {
        match: rule.match.trim(),
        message: rule.message,
        stock_file: stockFile,
        products_per_sale: rule.products_per_sale,
        low_stock_alert: rule.low_stock_alert,
        stock: shared ? shared.stock : [],
      };
      if (isNew) state.rules.push(next);
      else state.rules[ruleId] = next;
      const index = isNew ? state.rules.length - 1 : ruleId;
      log('INFO', 'supplierhub', `Правило №${index + 1} сохранено`);
      return { item: ruleView(next, index), restart_required: running() };
    },

    delete_rule(ruleId) {
      ruleAt(ruleId);
      state.rules.splice(ruleId, 1);
      return { deleted: true, restart_required: running() };
    },

    move_rule(ruleId, direction) {
      ruleAt(ruleId);
      const target = ruleId + direction;
      if (target >= 0 && target < state.rules.length) {
        const [rule] = state.rules.splice(ruleId, 1);
        state.rules.splice(target, 0, rule);
      }
      return { items: state.rules.map(ruleView), restart_required: running() };
    },

    get_stock(ruleId) {
      const rule = stockRule(ruleId);
      return { rule_id: ruleId, file: rule.stock_file, items: rule.stock, count: rule.stock.length };
    },

    add_stock(ruleId, text) {
      const rule = stockRule(ruleId);
      const lines = String(text).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
      rule.stock.push(...lines);
      return { added: lines.length, count: rule.stock.length };
    },

    remove_stock_item(ruleId, index, value) {
      const rule = stockRule(ruleId);
      if (rule.stock[index] !== value) return { removed: false, count: rule.stock.length };
      rule.stock.splice(index, 1);
      return { removed: true, count: rule.stock.length };
    },

    clear_stock(ruleId) {
      const rule = stockRule(ruleId);
      const removed = rule.stock.length;
      rule.stock.splice(0);
      return { removed };
    },

    check_token(token = null) {
      const value = token || state.config.playerok.token;
      if (!value) throw new Error('Токен не задан — вставьте cookie token из браузера.');
      if (value.length < 20 || /bad|wrong/i.test(value)) throw new Error('Playerok не принял токен (401 Unauthorized). Возьмите свежий cookie token из браузера.');
      return { username: 'NeonKeys', balance: 18450.5 };
    },

    test_telegram(settings = null) {
      const tg = settings || state.config.telegram;
      if (!tg.bot_token) throw new Error('Не задан токен бота.');
      if (!/^\d{6,}:[\w-]{20,}$/.test(tg.bot_token)) throw new Error('Telegram отклонил токен бота (401 Unauthorized) — проверьте его у @BotFather.');
      if (!tg.chat_ids || !tg.chat_ids.length) throw new Error('Список chat id пуст.');
      return { sent: true };
    },

    open_path(which) {
      if (!['workdir', 'stock', 'log'].includes(which)) throw new Error('Неизвестная папка.');
      state.opened.push(which);
      return {};
    },

    open_url(url) {
      if (!/^https:\/\//.test(String(url))) throw new Error('Открывать можно только https-ссылки.');
      state.opened.push(url);
      return {};
    },
  };

  const SLOW = { check_token: 900, test_telegram: 800, start_bot: 250, stop_bot: 200, restart_bot: 200, save_config: 250, save_rule: 200 };
  window.__mockCalls = [];

  const api = {};
  for (const [name, fn] of Object.entries(methods)) {
    api[name] = (...args) => {
      window.__mockCalls.push({ method: name, args: clone(args) });
      return new Promise((resolve) => {
        setTimeout(() => {
          try {
            resolve({ ok: true, data: clone(fn(...clone(args))) });
          } catch (exc) {
            resolve({ ok: false, error: exc.message });
          }
        }, SLOW[name] || 25);
      });
    };
  }

  window.__mock = { state, scenario, pushFeed, log };
  window.pywebview = { api, platform: 'mock', token: 'mock' };
  document.addEventListener('DOMContentLoaded', () => window.dispatchEvent(new Event('pywebviewready')));
})();
