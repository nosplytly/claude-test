// The site in two languages: every text is a key in app/web/assets/locales/{ru,en}.js.
//
// Run:  node tests\test_i18n_site.mjs
// Fails and names the spot when: a key is missing in one language or used but not defined; ru and en disagree on
// {placeholders}, on the markup of a *_html text or on the spaces it is glued with; Russian text is left in app.js
// or in index.html without a key; a page module or a site-facing server module writes Russian past the catalog.
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const WEB = join(ROOT, 'app', 'web');
const I18N = pathToFileURL(join(WEB, 'assets', 'i18n.js')).href;
const CYR = /[А-Яа-яЁё]/;
const BILINGUAL = new Set(['lang.switch_aria']);  // the switch to the other language is labelled in that language
let OK = 0, run = 0;

function check(cond, msg) {
  if (!cond) throw new Error(`FAILED: ${msg}`);
  OK += 1;
  console.log('  ✓', msg);
}
const list = (xs) => (xs.length ? ':\n     ' + xs.join('\n     ') : '');

// ------------------------------------------------------------------ a page just big enough for i18n.js
class Text {
  constructor(data) { this.nodeType = 3; this.data = data; this.parent = null; }
  remove() { this.parent.childNodes = this.parent.childNodes.filter((n) => n !== this); }
}

class El {
  constructor(tag, attrs = {}, kids = []) {
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attrs = { ...attrs };
    this.childNodes = [];
    this.html = null;
    const names = new Set((attrs.class || '').split(/\s+/).filter(Boolean));
    this.classList = { add: (c) => names.add(c), remove: (c) => names.delete(c), contains: (c) => names.has(c) };
    if (tag === 'template') this.content = new El('#fragment', {}, kids);
    else this.append(...kids);
  }

  get dataset() {
    const d = {};
    for (const [k, v] of Object.entries(this.attrs)) {
      if (k.startsWith('data-')) d[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
    }
    return d;
  }

  get firstElementChild() { return this.childNodes.find((n) => n.nodeType === 1) || null; }
  get textContent() { return this.childNodes.map((n) => (n.nodeType === 3 ? n.data : n.textContent)).join(''); }
  set textContent(v) { this.childNodes = []; this.append(String(v)); }
  set innerHTML(v) { this.childNodes = []; this.html = v; }
  get innerHTML() { return this.html; }
  set className(v) { this.attrs.class = v; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(type, fn) { (this.listeners ??= {})[type] = fn; }
  click() { return this.listeners?.click?.(); }

  append(...nodes) {
    for (let n of nodes) {
      if (typeof n === 'string') n = new Text(n);
      n.parent = this;
      this.childNodes.push(n);
    }
  }

  matches(sel) {
    if (sel.startsWith('[')) return sel.slice(1, -1) in this.attrs;
    if (sel.startsWith('.')) return (this.attrs.class || '').split(/\s+/).includes(sel.slice(1));
    return this.tagName === sel.toUpperCase();
  }

  querySelectorAll(sel) {
    const out = [];
    const walk = (el) => {
      for (const n of el.childNodes) {
        if (n.nodeType !== 1) continue;
        if (n.matches(sel)) out.push(n);
        walk(n);  // template contents stay out, as in a browser
      }
    };
    walk(this);
    return out;
  }

  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

const icon = () => new El('svg', { class: 'ic' });

function page() {
  return new El('html', { class: 'i18n-pending' }, [
    new El('body', {}, [
      new El('button', { id: 'loginBtn', 'data-i18n': 'nav.login' }, [icon(), 'Войти']),
      new El('span', { 'data-i18n': 'soon.tag' }, ['Скоро']),
      new El('p', { id: 'loginHint', 'data-i18n-html': 'form.login_hint_html' }, ['Логин для входа в Steam']),
      new El('input', { 'data-i18n-attr': 'placeholder:form.login_ph; aria-label:form.network_aria', placeholder: 'Например, gaben' }),
      new El('template', {}, [new El('h3', { 'data-i18n': 'lm.title' }, ['Вход через Telegram'])]),
      new El('footer', { class: 'foot' }, [new El('a', { 'data-i18n': 'foot.terms' }, ['Условия'])]),
    ]),
  ]);
}

// a browser just big enough for i18n.js to load in Node
function browser({ languages = ['en-US'], hash = '', saved = null, tgParams = null, search = '' } = {}) {
  const store = (init) => { const m = new Map(Object.entries(init)); return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)) }; };
  globalThis.location = { hash, search, href: `https://sh.test/${search}${hash}`, pathname: '/', reload() { globalThis.location.reloaded = true; } };
  globalThis.history = { state: null, replaceState(_, __, url) { globalThis.location.replaced = url; } };
  Object.defineProperty(globalThis, 'navigator', { value: { languages, language: languages[0] }, configurable: true });
  globalThis.localStorage = store(saved ? { sh_lang: saved } : {});
  globalThis.sessionStorage = store(tgParams ? { __telegram__initParams: JSON.stringify(tgParams) } : {});
  globalThis.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };
  const root = page();
  globalThis.document = {
    documentElement: root,
    querySelectorAll: (s) => root.querySelectorAll(s),
    querySelector: (s) => root.querySelector(s),
    createElement: (tag) => new El(tag),
  };
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

console.log('2. страница переводится при загрузке');
const ru = await load({ languages: ['ru-RU'] });
const ruRoot = globalThis.document.documentElement;
check(ru.LOCALE === 'ru-RU' && ruRoot.lang === 'ru' && ru.t('nav.login') === 'Войти', 'русский: <html lang="ru">, числа и даты по-русски');
check(ruRoot.querySelector('.lang-switch')?.textContent === 'EN', 'в подвале кнопка EN');

const en = await load({ languages: ['en-US'] });
const root = globalThis.document.documentElement;
const { t } = en;
check(en.LOCALE === 'en-US' && root.lang === 'en', 'английский: <html lang="en">, числа и даты по-английски');
check(!root.classList.contains('i18n-pending'), 'страница показывается только после перевода (снят i18n-pending)');
const loginBtn = root.querySelector('#loginBtn') || root.querySelectorAll('[data-i18n]')[0];
check(loginBtn.textContent === 'Sign in' && loginBtn.firstElementChild?.tagName === 'SVG', 'кнопка: текст заменён, иконка рядом осталась');
check(root.querySelector('[data-i18n-html]').innerHTML === t('form.login_hint_html') && t('form.login_hint_html').includes('id="whereLogin"'),
  'текст с разметкой (подсказка к логину) вставлен целиком, кнопка «Where do I find it?» на месте');
const input = root.querySelector('input');
check(input.getAttribute('placeholder') === 'e.g. gaben' && input.getAttribute('aria-label') === 'Network', 'атрибуты (placeholder, aria-label)');
const tpl = root.querySelector('template');
check(tpl.content.querySelector('h3').textContent === 'Sign in with Telegram', 'шаблоны окон (<template>) тоже переведены');
const sw = root.querySelector('.lang-switch');
check(sw?.textContent === 'RU' && sw.getAttribute('aria-label') === t('lang.switch_aria'), 'в подвале кнопка RU');
let told = null;
en.hooks.beforeSwitch = async (lang) => { told = lang; };
await sw.click();
check(told === 'ru' && globalThis.localStorage.getItem('sh_lang') === 'ru' && globalThis.location.reloaded,
  'переключение: сервер узнаёт язык (бот заговорит так же), выбор запомнен, страница перезагружена');

console.log('3. каталоги ru / en');
const RU = (await import(pathToFileURL(join(WEB, 'assets', 'locales', 'ru.js')).href)).default;
const EN = (await import(pathToFileURL(join(WEB, 'assets', 'locales', 'en.js')).href)).default;
const keys = Object.keys(RU);
check(keys.length > 200 && keys.length === Object.keys(EN).length && keys.every((k) => k in EN), `одинаковые ключи (${keys.length})`);
const params = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((x) => x[1]).sort().join(',');
const badParams = keys.filter((k) => params(RU[k]) !== params(EN[k])).map((k) => `${k}: {${params(RU[k])}} ≠ {${params(EN[k])}}`);
check(!badParams.length, `подстановки ({min}, {usd}…) совпадают${list(badParams)}`);
const empty = keys.filter((k) => !RU[k].trim() || !EN[k].trim());
check(!empty.length, `нет пустых текстов${list(empty)}`);
const russianInEn = keys.filter((k) => CYR.test(EN[k]) && !BILINGUAL.has(k));
check(!russianInEn.length, `в английском нет русских слов${list(russianInEn)}`);
const latinOnlyRu = keys.filter((k) => !CYR.test(RU[k].replace(/<[^>]+>/g, '')) && RU[k] !== EN[k] && !BILINGUAL.has(k));
check(!latinOnlyRu.length, `в русском каталоге нет забытых английских фраз${list(latinOnlyRu)}`);
// a text glued to another (' Transaction ↗', 'Reason: X. ') keeps its spaces in both languages
const edges = (s) => `${/^\s/.test(s) ? '␣' : ''}|${/\s$/.test(s) ? '␣' : ''}`;
const badEdges = keys.filter((k) => edges(RU[k]) !== edges(EN[k])).map((k) => `${k}: «${RU[k]}» / «${EN[k]}»`);
check(!badEdges.length, `пробелы по краям склеиваемых фраз одинаковые${list(badEdges)}`);
// markup: the same tags with the same ids / classes / links, so app.js finds #whereLogin, #apiIps, [data-open] in both
const skeleton = (s) => [...s.matchAll(/<\/?([a-z0-9]+)([^>]*)>/gi)].map(([, tag, a]) =>
  tag + [...a.matchAll(/\s(id|class|for|href|data-[\w-]+|type|target)="([^"]*)"/g)].map((x) => `[${x[1]}=${x[2]}]`).join('')).join(' ');
const htmlKeys = keys.filter((k) => k.endsWith('_html'));
const badHtml = htmlKeys.filter((k) => skeleton(RU[k]) !== skeleton(EN[k])).map((k) => `${k}:\n       ru ${skeleton(RU[k])}\n       en ${skeleton(EN[k])}`);
check(htmlKeys.length >= 8 && !badHtml.length, `разметка ${htmlKeys.length} текстов с HTML одинаковая в обоих языках${list(badHtml)}`);
const tagsInPlain = keys.filter((k) => !k.endsWith('_html') && /<\/?[a-z][^>]*>/i.test(RU[k] + EN[k]));
check(!tagsInPlain.length, `разметка только в ключах *_html (остальное вставляется как текст)${list(tagsInPlain)}`);

console.log('4. каждый текст страницы — через ключ');
const used = new Set();
const need = (where, key) => {
  used.add(key);
  if (!(key in RU) || !(key in EN)) missingKeys.push(`${where}: ${key}`);
};
const missingKeys = [];

// index.html: a light tokenizer, enough for our own markup
const html = readFileSync(join(WEB, 'index.html'), 'utf8');
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);
const TEXT_ATTRS = ['placeholder', 'aria-label', 'title', 'alt', 'content'];
const stack = [];
const untagged = [], badAttrs = [], ruMismatch = [];
let rawUntil = null, inSvg = 0, htmlChecked = 0;
for (const tok of html.matchAll(/<!--[\s\S]*?-->|<(\/?)([a-zA-Z][\w-]*)((?:[^>"']|"[^"]*"|'[^']*')*?)(\/?)>|([^<]+)/g)) {
  const [whole, closing, tagRaw, attrSrc = '', selfClose, text] = tok;
  if (whole.startsWith('<!--')) continue;
  const tag = tagRaw?.toLowerCase();
  if (rawUntil) {  // inside <script> / <style>
    if (closing && tag === rawUntil) rawUntil = null;
    continue;
  }
  if (text !== undefined) {
    const words = text.replace(/\s+/g, ' ').trim();
    if (!CYR.test(words)) continue;
    htmlChecked += 1;
    const parent = stack[stack.length - 1];
    const covered = parent?.attrs['data-i18n'] || stack.some((el) => el.attrs['data-i18n-html']);
    if (!covered) untagged.push(`<${parent?.tag}> «${words.slice(0, 60)}»`);
    else if (parent.attrs['data-i18n'] && RU[parent.attrs['data-i18n']] && parent.attrs['data-i18n'] in RU) {
      const want = RU[parent.attrs['data-i18n']].replace(/\s+/g, ' ').trim();
      if (want !== words) ruMismatch.push(`${parent.attrs['data-i18n']}: в странице «${words}», в каталоге «${want}»`);
    }
    continue;
  }
  if (closing) {
    while (stack.length && stack.pop().tag !== tag) { /* unclosed <p>, <li>: closed by their parent */ }
    if (tag === 'svg') inSvg -= 1;
    continue;
  }
  const attrs = {};
  for (const a of attrSrc.matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[a[1].toLowerCase()] = a[2] ?? a[3] ?? a[4] ?? '';
  if (attrs['data-i18n']) need(`index.html <${tag}>`, attrs['data-i18n']);
  if (attrs['data-i18n-html']) need(`index.html <${tag}>`, attrs['data-i18n-html']);
  const attrKeys = Object.fromEntries((attrs['data-i18n-attr'] || '').split(';').filter((p) => p.trim()).map((p) => p.split(':').map((x) => x.trim())));
  for (const [a, key] of Object.entries(attrKeys)) {
    need(`index.html <${tag} ${a}>`, key);
    if (key in RU && attrs[a] !== undefined && attrs[a] !== RU[key]) ruMismatch.push(`${key}: в странице «${attrs[a]}», в каталоге «${RU[key]}»`);
  }
  for (const a of TEXT_ATTRS) {
    if (CYR.test(attrs[a] || '')) {
      htmlChecked += 1;
      if (!attrKeys[a]) badAttrs.push(`<${tag} ${a}="${attrs[a]}">`);
    }
  }
  if (tag === 'script' || tag === 'style') { rawUntil = tag; continue; }
  if (tag === 'svg') inSvg += 1;
  if (VOID.has(tag) || selfClose || (inSvg && selfClose)) continue;
  stack.push({ tag, attrs });
}
check(htmlChecked > 100, `index.html: проверено ${htmlChecked} русских текстов и атрибутов`);
check(!untagged.length, `в index.html нет русского текста без data-i18n${list(untagged)}`);
check(!badAttrs.length, `у каждого русского атрибута есть data-i18n-attr${list(badAttrs)}`);
check(!ruMismatch.length, `русский текст в index.html совпадает с каталогом (нет двух версий одной фразы)${list(ruMismatch)}`);

// the page's scripts: no Russian at all (comments aside); every t('key') and every key-looking literal exists
const stripComments = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '').split('\n')
  .map((l) => (/^\s*\/\//.test(l) ? '' : l.replace(/\s\/\/\s.*$/, ''))).join('\n');
const scripts = readdirSync(join(WEB, 'assets')).filter((f) => f.endsWith('.js')).map((f) => join(WEB, 'assets', f));
const russianInJs = [];
const namespaces = new Set(keys.map((k) => k.split('.')[0]));
for (const p of scripts) {
  const name = p.split(/[\\/]/).pop();
  const code = stripComments(readFileSync(p, 'utf8'));
  code.split('\n').forEach((l, i) => { if (CYR.test(l)) russianInJs.push(`${name}:${i + 1}: ${l.trim().slice(0, 100)}`); });
  for (const x of code.matchAll(/\bt\(\s*(['"`])([^'"`]+)\1/g)) need(name, x[2]);
  for (const x of code.matchAll(/'([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)'/g)) {
    if (namespaces.has(x[1].split('.')[0]) && !/\.(js|css|png|svg|html)$/.test(x[1])) need(name, x[1]);
  }
}
check(scripts.length >= 4, `скрипты страницы: ${scripts.map((p) => p.split(/[\\/]/).pop()).join(', ')}`);
check(!russianInJs.length, `в скриптах нет русского текста мимо каталога${list(russianInJs)}`);
check(!missingKeys.length, `каждый ключ из страницы и скриптов есть в обоих языках (${used.size})${list(missingKeys)}`);
const unused = keys.filter((k) => !used.has(k));
check(!unused.length, `в каталоге нет лишних ключей${list(unused)}`);

// what the server writes onto the page (errors in toasts and hints) comes from app/locales — no Russian past it
const SITE_FACING = ['api.py', 'deposits.py', 'promos.py', 'auth.py', 'ratelimit.py', 'main.py', 'webhooks.py', 'schemas.py', 'partner_api.py', 'i18n.py'];
const pyRussian = [];
for (const f of SITE_FACING) {
  const src = readFileSync(join(ROOT, 'app', f), 'utf8').replace(/"""[\s\S]*?"""/g, (m) => m.replace(/[^\n]/g, ' '));
  src.split('\n').forEach((l, i) => {
    const code = l.replace(/\s*#.*$/, '');
    if (/f?"[^"\n]*[А-Яа-яЁё][^"\n]*"|f?'[^'\n]*[А-Яа-яЁё][^'\n]*'/.test(code)) pyRussian.push(`${f}:${i + 1}: ${l.trim().slice(0, 100)}`);
  });
}
check(!pyRussian.length, `серверные модули сайта пишут ошибки только через каталог (${SITE_FACING.length} файлов)${list(pyRussian)}`);

// the partner docs: two hand-written pages that must describe the same API
const docsRu = readFileSync(join(WEB, 'docs.html'), 'utf8');
const docsEn = readFileSync(join(WEB, 'docs.en.html'), 'utf8');
const visible = (s) => s.replace(/<(script|style)[\s\S]*?<\/\1>/g, '').replace(/aria-label="Русская версия"/g, '');
const ruLeft = visible(docsEn).split('\n').filter((l) => CYR.test(l)).map((l) => l.trim().slice(0, 90));
check(!ruLeft.length && /<html lang="en">/.test(docsEn), `документация API на английском — без русских слов${list(ruLeft)}`);
const sections = (s) => [...s.matchAll(/<h2 id="([^"]+)"/g)].map((x) => x[1]).join(' ');
const endpoints = (s) => [...s.matchAll(/<span class="m (\w+)">\w+<\/span><span class="path">([^<]+)<\/span>/g)].map((x) => `${x[1]} ${x[2]}`).join(' | ');
const codes = (s) => [...s.matchAll(/<td>(\d{3})<\/td>/g)].map((x) => x[1]).join(' ');
const fields = (s) => [...s.matchAll(/<tr><td><code>([^<]+)<\/code><\/td>/g)].map((x) => x[1]).join(' ');
check(sections(docsRu) === sections(docsEn) && endpoints(docsRu) === endpoints(docsEn) && endpoints(docsEn).split('|').length >= 11,
  `обе версии документации: те же разделы и методы (${endpoints(docsEn).split('|').length})`);
check(codes(docsRu) === codes(docsEn) && fields(docsRu) === fields(docsEn), 'те же коды ответов, поля, статусы и события');
// examples (JSON, curl, signature code) are the same; only the very first one differs (the placeholder key, the header)
const examples = (s) => [...s.matchAll(/<pre[^>]*><code>([\s\S]*?)<\/code><\/pre>/g)].map((x) => x[1]).slice(1);
const exRu = examples(docsRu), exEn = examples(docsEn);
check(exRu.length >= 9 && exRu.every((x, i) => x === exEn[i]) && exRu.length === exEn.length, `${exRu.length} примеров кода одинаковые в обеих версиях`);
check(/href="\/api-docs\?lang=en"/.test(docsRu) && /href="\/api-docs\?lang=ru"/.test(docsEn), 'в документации есть переключатель RU / EN');
check(/sh_lang=en/.test(globalThis.document.cookie || ''), 'сайт запоминает свой язык в cookie — документация откроется на нём же');

console.log('5. качество английского');
check(t('promo.applied', { code: 'SALE', pct: '4.25%' }) === 'Promo code SALE applied: 4.25% off', 'промокод: код и процент на месте');
check(t('pay.warn', { coin: 'USDT', network: 'Tron (TRC20)' }) + t('pay.warn_balance', { usd: '$1.50' })
  === 'Send only USDT on the Tron (TRC20) network. Any other coin or network means lost funds. $1.50 will be taken from your balance.',
  'предупреждение об оплате склеивается в связный текст');
check(t('pay.confirmations', { n: 3, of: 20 }) + t('pay.network_confirms', { eta: t('eta.2_3') })
  === 'Confirmations: 3 of 20. The network confirms it in 2–3 minutes.', 'подтверждения + срок сети');
check(t('pay.network_confirms', { eta: t('eta.default') }) === 'The network confirms it usually within a couple of minutes.', 'срок по умолчанию');
check(t('res.failed_text', { reason: t('res.reason', { reason: 'declined by the supplier' }), usd: '$5.00' })
  === 'Reason: declined by the supplier. $5.00 is back on your balance — you can place the order again and it\'ll be paid from your balance.',
  'отказ с причиной');
check(t('res.failed_text', { reason: '', usd: '$5.00' }).startsWith('$5.00 is back'), 'отказ без причины не начинается с пробела');
check(t('toast.signed_in', { name: 'Ivan' }) === 'Signed in as Ivan' && t('amount.range', { min: '$1.00', max: '$5,000.00' }) === 'Amount: $1.00 to $5,000.00',
  'вход и диапазон суммы');
check(t('pay.warn', { coin: 'TON' }).includes('{network}'), 'не переданная подстановка остаётся видна ({network}), а не превращается в undefined');
const errors = [];
const realError = console.error;
console.error = (...a) => errors.push(a.join(' '));
const unknown = t('no.such.key');
console.error = realError;
check(unknown === 'no.such.key' && errors.length === 1, 'нет ключа → виден сам ключ и ошибка в консоли, а не падение');
const saved = EN['soon.cta'];
delete EN['soon.cta'];
check(t('soon.cta') === RU['soon.cta'], 'ключа нет в английском → русский текст, а не пустота');
EN['soon.cta'] = saved;
const LISTS_SYMBOLS = new Set(['login.rule']);  // "_ - . only": the symbols themselves, not punctuation
const nbsp = keys.filter((k) => !LISTS_SYMBOLS.has(k)).filter((k) => /\s{2,}/.test(EN[k].replace(/\n\s+/g, ' ')) || /\s[,.!?:;]/.test(EN[k].replace(/<[^>]+>/g, '').replace(/ —/g, '')));
check(!nbsp.length, `в английском нет двойных пробелов и пробелов перед знаками препинания${list(nbsp)}`);

console.log(`\nALL GOOD: ${OK} checks passed`);
