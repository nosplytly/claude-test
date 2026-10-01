// Languages of the site. Every text is a key in ./locales/{ru,en}.js:
//   - the page marks its texts with data-i18n="key" (an icon inside the element stays), data-i18n-html="key" (a
//     phrase with markup in it) and data-i18n-attr="placeholder:key;aria-label:key";
//   - app.js takes its texts from t('key', {params});
//   - the server answers in the same language (app.js sends X-SH-Lang).
// Language: a link from the bot (?lang=) → the visitor's own choice (the RU/EN switch) → the Telegram app's language
// (Mini App) → the browser's. The page stays hidden (html.i18n-pending, CSS) until its texts are in place.
import ru from './locales/ru.js';
import en from './locales/en.js';

const RU_FAMILY = /^(ru|uk|be|kk|ky|uz|tg|tk|hy|az|ka)\b/i;

function telegramLang() {
  // the launch data Telegram puts in the URL fragment (kept in sessionStorage after the first load)
  let data = '';
  const i = location.hash.indexOf('tgWebAppData=');
  if (i >= 0) data = new URLSearchParams(location.hash.slice(i)).get('tgWebAppData') || '';
  else {
    try { data = JSON.parse(sessionStorage.getItem('__telegram__initParams') || '{}').tgWebAppData || ''; } catch { /* no storage */ }
  }
  try { return JSON.parse(new URLSearchParams(data).get('user') || '{}').language_code || ''; } catch { return ''; }
}

// a link from the bot says which language the customer speaks there (?lang=en): that counts as their choice
function fromLink() {
  const q = new URLSearchParams(location.search || '').get('lang');
  if (q !== 'ru' && q !== 'en') return '';
  try { localStorage.setItem('sh_lang', q); } catch { /* private mode */ }
  try {
    const u = new URL(location.href);
    u.searchParams.delete('lang');
    history.replaceState(history.state, '', u.pathname + u.search + u.hash);  // keep the hash: Telegram's launch data
  } catch { /* old browser: the parameter just stays in the address */ }
  return q;
}

function detect() {
  const link = fromLink();
  if (link) return link;
  try { const own = localStorage.getItem('sh_lang'); if (own === 'ru' || own === 'en') return own; } catch { /* private mode */ }
  const tg = telegramLang();
  if (tg) return RU_FAMILY.test(tg) ? 'ru' : 'en';
  const langs = navigator.languages?.length ? navigator.languages : [navigator.language || 'ru'];
  return langs.some((l) => RU_FAMILY.test(l)) ? 'ru' : 'en';  // a Russian speaker with an English browser reads Russian
}

export const LANG = detect();
export const LOCALE = LANG === 'en' ? 'en-US' : 'ru-RU';
const CATALOG = LANG === 'en' ? en : ru;

/** The text for `key` in the page's language, {name} placeholders filled from `params`. */
export function t(key, params) {
  let text = CATALOG[key] ?? ru[key];
  if (text == null) {
    console.error('i18n: no text for', key);
    return key;
  }
  if (params) text = text.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m));
  return text;
}

function setText(el, text) {
  if (!el.firstElementChild) { el.textContent = text; return; }
  // an icon (svg, a dot) sits next to the words: replace only the words
  const words = [...el.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE && n.data.trim());
  if (words.length) {
    words[0].data = text;
    words.slice(1).forEach((n) => n.remove());
  } else {
    el.append(text);
  }
}

/** Put the page's language into everything marked under `root` (templates included). */
export function apply(root = document) {
  for (const el of root.querySelectorAll('[data-i18n]')) setText(el, t(el.dataset.i18n));
  for (const el of root.querySelectorAll('[data-i18n-html]')) el.innerHTML = t(el.dataset.i18nHtml);
  for (const el of root.querySelectorAll('[data-i18n-attr]')) {
    for (const pair of el.dataset.i18nAttr.split(';')) {
      const [attr, key] = pair.split(':');
      el.setAttribute(attr.trim(), t(key.trim()));
    }
  }
  for (const tpl of root.querySelectorAll('template')) apply(tpl.content);
}

export const hooks = { beforeSwitch: null };  // app.js: tell the server, so the bot speaks the same language

export async function setLang(lang) {
  try { localStorage.setItem('sh_lang', lang); } catch { /* private mode: the switch lasts for this page only */ }
  try { await hooks.beforeSwitch?.(lang); } catch { /* the site switches anyway */ }
  location.reload();
}

function langSwitch() {
  const foot = document.querySelector('.foot');
  if (!foot || foot.querySelector('.lang-switch')) return;
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'lang-switch';
  b.setAttribute('aria-label', t('lang.switch_aria'));
  b.textContent = LANG === 'en' ? 'RU' : 'EN';
  b.addEventListener('click', () => setLang(LANG === 'en' ? 'ru' : 'en'));
  foot.append(b);
}

document.documentElement.lang = LANG;
// pages the server renders (the API docs) open in the language the site is shown in
try { document.cookie = `sh_lang=${LANG}; path=/; max-age=31536000; samesite=lax${location.protocol === 'https:' ? '; secure' : ''}`; } catch { /* cookies off */ }
// a module runs after the document is parsed, so the whole page is already there
apply();
langSwitch();
document.documentElement.classList.remove('i18n-pending');

export const _test = { detect, CATALOG };
