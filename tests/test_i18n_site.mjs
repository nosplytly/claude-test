// English version of the site: every Russian phrase a visitor can see has an English line.
//
// Run:  node tests\test_i18n_site.mjs
// The phrases are pulled straight from index.html, app.js and the server's error messages, so a new Russian
// string added anywhere without a translation makes this test fail and name it.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const I18N = pathToFileURL(join(ROOT, 'app', 'web', 'assets', 'i18n.js')).href;
const CYR = /[А-Яа-яЁё]/;
let OK = 0, run = 0;

function check(cond, msg) {
  if (!cond) throw new Error(`FAILED: ${msg}`);
  OK += 1;
  console.log('  ✓', msg);
}

// a browser just big enough for i18n.js to load in Node
function browser({ languages = ['en-US'], hash = '', saved = null, tgParams = null, search = '' } = {}) {
  const store = (init) => { const m = new Map(Object.entries(init)); return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)) }; };
  globalThis.location = { hash, search, href: `https://sh.test/${search}${hash}`, pathname: '/', reload() {} };
  globalThis.history = { state: null, replaceState(_, __, url) { globalThis.location.replaced = url; } };
  Object.defineProperty(globalThis, 'navigator', { value: { languages, language: languages[0] }, configurable: true });
  globalThis.localStorage = store(saved ? { sh_lang: saved } : {});
  globalThis.sessionStorage = store(tgParams ? { __telegram__initParams: JSON.stringify(tgParams) } : {});
  globalThis.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1, DOCUMENT_FRAGMENT_NODE: 11 };
  globalThis.NodeFilter = { SHOW_TEXT: 4, SHOW_ELEMENT: 1 };
  globalThis.MutationObserver = class { observe() {} };
  globalThis.window = { confirm: () => true };
  const root = { nodeType: 1, tagName: 'HTML', lang: '', getAttribute: () => null, closest: () => null };
  globalThis.document = { documentElement: root, querySelector: () => null, createTreeWalker: () => ({ nextNode: () => null }) };
}

async function load(opts) {
  browser(opts);
  run += 1;
  return import(`${I18N}?run=${run}`);  // a fresh copy: the language is picked when the module loads
}

// Telegram's launch data as it sits in the URL fragment
const tgHash = (lang) => '#tgWebAppData=' + encodeURIComponent(
  `query_id=AA&user=${encodeURIComponent(JSON.stringify({ id: 1, first_name: 'X', language_code: lang }))}&auth_date=1&hash=abc`)
  + '&tgWebAppVersion=8.0&tgWebAppPlatform=tdesktop';

console.log('1. какой язык показать');
check((await load({ languages: ['ru-RU', 'en'] })).LANG === 'ru', 'русский браузер → русский');
check((await load({ languages: ['en-US', 'ru'] })).LANG === 'ru', 'английский браузер, но русский в списке → русский');
check((await load({ languages: ['uk-UA'] })).LANG === 'ru', 'украинский браузер → русский (ближе, чем английский)');
check((await load({ languages: ['en-US'] })).LANG === 'en', 'только английский → английский');
check((await load({ languages: ['de-DE', 'fr'] })).LANG === 'en', 'немецкий / французский → английский');
check((await load({ languages: ['ru-RU'], hash: tgHash('en') })).LANG === 'en', 'мини-апп: язык Telegram (en) важнее браузера');
check((await load({ languages: ['en-US'], hash: '#/deposit?' + tgHash('ru').slice(1) })).LANG === 'ru',
  'мини-апп с нужной страницей (#/deposit?tgWebAppData=…) → язык Telegram (ru)');
const initData = decodeURIComponent(tgHash('en').split('&tgWebAppVersion')[0].slice('#tgWebAppData='.length));
check((await load({ languages: ['ru-RU'], tgParams: { tgWebAppData: initData } })).LANG === 'en',
  'после перезагрузки мини-аппа язык берётся из сохранённых данных Telegram');
check((await load({ languages: ['en-US'], saved: 'ru' })).LANG === 'ru', 'выбор переключателем RU/EN важнее всего');
check((await load({ languages: ['ru-RU'], hash: tgHash('ru'), saved: 'en' })).LANG === 'en', '…и важнее языка Telegram');
const viaBot = await load({ languages: ['ru-RU'], search: '?lang=en', hash: '#/deposit' });
check(viaBot.LANG === 'en' && globalThis.localStorage.getItem('sh_lang') === 'en' && globalThis.location.replaced === '/#/deposit',
  'ссылка из бота (?lang=en) → английский, запомнен, из адреса убран, страница (#/deposit) сохранена');
check((await load({ languages: ['en-US'], search: '?lang=xx' })).LANG === 'en', 'мусор в ?lang= игнорируется');
const ru = await load({ languages: ['ru-RU'] });
check(ru.tr('Войти') === 'Войти' && ru.LOCALE === 'ru-RU', 'в русском режиме тексты не трогаются, числа и даты по-русски');

const m = await load({ languages: ['en-US'] });
const { tr } = m;
check(m.LOCALE === 'en-US' && globalThis.document.documentElement.lang === 'en', 'английский режим: <html lang="en">, числа и даты по-английски');

console.log('2. всё, что есть на странице, переведено');
const missing = [];
const expect = (src, text) => {
  const out = tr(text);
  if (CYR.test(out)) missing.push(`${src}: «${text.trim()}» → «${out.trim()}»`);
};

// index.html: text between tags and the visible attributes (templates included)
const html = readFileSync(join(ROOT, 'app', 'web', 'index.html'), 'utf8').replace(/<(script|style)[\s\S]*?<\/\1>/g, '');
const htmlTexts = html.split(/<[^>]+>/).map((t) => t.replace(/\s+/g, ' ').trim()).filter((t) => CYR.test(t));
const attrs = [...html.matchAll(/\s(?:placeholder|aria-label|title|alt|content)="([^"]*)"/g)].map((x) => x[1]).filter((t) => CYR.test(t));
htmlTexts.forEach((t) => expect('index.html', t));
attrs.forEach((t) => expect('index.html [attr]', t));
check(htmlTexts.length > 80 && attrs.length > 5, `index.html: ${htmlTexts.length} фраз и ${attrs.length} атрибутов проверено`);

// app.js: every Russian string literal; ${…} stands for a value, markup inside a string is split into its text parts
const js = readFileSync(join(ROOT, 'app', 'web', 'assets', 'app.js'), 'utf8');
const literals = [...js.matchAll(/'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`/g)]
  .map((x) => x[1] ?? x[2] ?? x[3]).filter((t) => CYR.test(t));
let jsCount = 0;
for (const lit of literals) {
  if (lit.includes(" : ''}")) continue;  // the regex split a nested template here; its real text is checked below
  const text = lit.replace(/\$\{[^}]*\}/g, 'X');
  for (const part of text.split(/<[^>]+>/)) {
    if (CYR.test(part)) { expect('app.js', part); jsCount += 1; }
  }
}
check(jsCount > 110, `app.js: ${jsCount} строк проверено`);

// the server's messages that end up on the page (errors in toasts and hints, ledger lines)
const serverFiles = ['api.py', 'orders.py', 'deposits.py', 'promos.py', 'auth.py', 'ratelimit.py', 'main.py', 'webhooks.py', 'fail2ban.py'];
const ADMIN_ONLY = /\/promo|Код: 3–32|Размер|Не понял|Слишком большой|Заказ уже не в ожидании/;
let pyCount = 0;
for (const f of serverFiles) {
  const src = readFileSync(join(ROOT, 'app', f), 'utf8');
  const re = /(?:OrderError|PromoError|BadWebhookUrl|HTTPException\(\d+,|"message":|comment=|super\(\).__init__\(|detail":?)\s*\(?\s*f?"([^"]*[А-Яа-яЁё][^"]*)"(\s*\n?\s*f?"[^"]*")*/g;
  for (const x of src.matchAll(re)) {
    // Python joins adjacent literals: "a " "b" → "a b"
    const lits = x[0].slice(x[0].search(/f?"[^"]*[А-Яа-яЁё]/));
    const full = lits.match(/f?"[^"]*"/g).map((q) => q.replace(/^f?"|"$/g, '')).join('');
    if (ADMIN_ONLY.test(full)) continue;
    expect(`${f}`, full.replace(/\{[^}]*\}/g, 'X'));
    pyCount += 1;
  }
}
const f2b = readFileSync(join(ROOT, 'app', 'fail2ban.py'), 'utf8').match(/"(Доступ с вашего IP[^"]*)"/)[1];
expect('fail2ban.py', f2b);
check(pyCount > 45, `сервер: ${pyCount} сообщений об ошибках проверено`);

// texts that app.js glues together from several strings at run time, and values the server fills in
const samples = [
  'Вы вошли: Ivan',
  'Сумма от 100 ₽ до 30 000 ₽', 'от 100 ₽ до 30 000 ₽', 'Сумма от $1.00 до $5,000.00', 'от $1.00 до $5,000.00 · зачисляется в USD',
  'минимальный перевод в BTC — $15.00, лишнее останется на балансе сайта',
  'минимальный перевод в BTC — $15.00, на баланс зачислится вся сумма', '$12.34 · точная сумма будет в счёте',
  'Промокод SALE применён: скидка 4.25%', 'Промокод SALE: −0.5% к скидке', 'Промокод GIFT: +$2 на баланс', 'Промокод GIFT',
  'Заказ SH-7K2M9', '$25.00 на баланс · D-4F2A',
  'Отправляйте только USDT в сети Tron (TRC20). Другая монета или сеть — потеря средств. С баланса сайта спишется $1.50.',
  'Отправляйте только TON в сети TON. Другая монета или сеть — потеря средств.',
  'Подтверждений: 3 из 20. Сеть подтверждает за 2–3 минуты.', 'Сеть подтверждает обычно до пары минут.',
  'Причина: отклонено поставщиком. $5.00 вернулись на баланс сайта — можно оформить заказ заново, он оплатится с баланса.',
  '$5.00 вернулись на баланс сайта — можно оформить заказ заново, он оплатится с баланса.',
  'Недостаточно средств на балансе: 1.50$, нужно 5.00$', 'Сумма должна быть от 50 ₽ до 30 000 ₽',
  'Сейчас можем принять заказ максимум на 12 345.5 ₽. Уменьшите сумму или попробуйте позже',
  'Оплата в Bitcoin временно недоступна, выберите другую монету', 'Сумма пополнения от $1.00 до $5000.00',
  'Некорректный IP: 1.2.3', 'Webhook: Домен не найден', 'Некорректные или отсутствующие поля: amount',
  '29.09, 14:30 · Возврат по заказу SH-7K2M9', '12.5 USDT (TRC20), заявка #12', 'Steam gaben: 500 ₽',
  ' пополнен на ', '. Приятных покупок!', 'Зачислено ', 'Вход отменён в боте. ', 'Код устарел. ',
];
samples.forEach((t) => expect('собранная строка', t));
check(!missing.length, `ни одной фразы без перевода${missing.length ? ':\n     ' + missing.join('\n     ') : ''}`);

console.log('3. качество перевода');
check(tr('Промокод SALE применён: скидка 4.25%') === 'Promo code SALE applied: 4.25% off', 'числа и коды внутри фраз сохраняются');
check(tr('  Войти ') === '  Sign in ', 'пробелы по краям (между <b> и текстом) сохраняются');
check(tr('Отправляйте только USDT в сети Tron (TRC20). Другая монета или сеть — потеря средств. С баланса сайта спишется $1.50.')
  === 'Send only USDT on the Tron (TRC20) network. A different coin or network means lost funds. $1.50 will be taken from your site balance.',
  'склеенное предупреждение переводится целиком, сумма $1.50 не обрезается');
check(tr('Недостаточно средств на балансе: 1.50$, нужно 5.00$') === 'Not enough balance: $1.50, need $5.00', 'знак $ ставится по-английски — перед суммой');
check(tr('Сеть подтверждает за 2–3 минуты.') === 'The network confirms in 2–3 minutes.', 'вложенная фраза (срок) тоже переводится');
check(tr('Something English') === 'Something English' && tr('gaben') === 'gaben', 'нерусский текст не трогается');
const same = Object.entries(m._test.EN).filter(([k, v]) => CYR.test(v));
check(!same.length, `в самом словаре нет русских слов в переводах${same.length ? ': ' + same.map((x) => x[0]).join(', ') : ''}`);

console.log(`\nALL GOOD: ${OK} checks passed`);
