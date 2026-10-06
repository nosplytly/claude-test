'use strict';
/*
 * SupplierHub — компоненты интерфейса: кнопки, поля, переключатели,
 * уведомления (тосты), модальные окна, боковые панели и диалог подтверждения.
 */

/* ---------------------------------------------------------------- Кнопки */

/** Кнопка. kind: primary | secondary | ghost | danger | danger-solid. */
function btn(label, { kind = 'secondary', icon: iconName, size, onClick, type = 'button', disabled, title, cls } = {}) {
  return h(
    'button',
    {
      type,
      class: ['btn', `btn--${kind}`, size && `btn--${size}`, !label && 'btn--icon-only', cls],
      onClick,
      disabled,
      title,
      'aria-label': label ? null : title,
    },
    iconName && icon(iconName, size === 'sm' ? 15 : size === 'lg' ? 19 : 17),
    label && h('span', { class: 'btn__label' }, label),
  );
}

function iconBtn(name, label, onClick, { kind = 'ghost', size = 16, cls, disabled } = {}) {
  return h(
    'button',
    { type: 'button', class: ['icon-btn', `icon-btn--${kind}`, cls], 'aria-label': label, title: label, onClick, disabled },
    icon(name, size),
  );
}

/** Выполнить действие с индикатором загрузки на кнопке. */
async function withBusy(button, fn) {
  if (!button || button.classList.contains('is-busy')) return undefined;
  const spinner = h('span', { class: 'spinner', 'aria-hidden': 'true' });
  const wasDisabled = button.disabled;
  button.classList.add('is-busy');
  button.disabled = true;
  button.prepend(spinner);
  try {
    return await fn();
  } finally {
    spinner.remove();
    button.classList.remove('is-busy');
    button.disabled = wasDisabled;
  }
}

/* ----------------------------------------------------------------- Поля */

function textInput({ value = '', placeholder, type = 'text', onInput, mono, id, inputmode, min, max, step, cls, disabled } = {}) {
  return h('input', {
    class: ['input', mono && 'input--mono', cls],
    type,
    value: value === null || value === undefined ? '' : String(value),
    placeholder,
    id,
    inputmode,
    min,
    max,
    step,
    disabled,
    autocomplete: 'off',
    spellcheck: 'false',
    onInput: (event) => onInput && onInput(event.target.value),
  });
}

/** Поле для секрета: скрыто точками, с кнопкой «показать». */
function secretInput({ value, placeholder, onInput, id } = {}) {
  const input = textInput({ value, placeholder, type: 'password', onInput, mono: true, id, cls: 'input--secret' });
  const toggleBtn = iconBtn('eye', 'Показать', () => {
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    const label = show ? 'Скрыть' : 'Показать';
    toggleBtn.title = label;
    toggleBtn.setAttribute('aria-label', label);
    replace(toggleBtn, icon(show ? 'eyeOff' : 'eye', 16));
  });
  return { el: h('div', { class: 'input-wrap' }, input, h('div', { class: 'input-wrap__addon' }, toggleBtn)), input };
}

/** Поле поиска с иконкой. */
function searchInput({ placeholder = 'Поиск', value = '', onInput } = {}) {
  const input = textInput({ value, placeholder, type: 'search', onInput, cls: 'input--search' });
  return h('label', { class: 'search' }, icon('search', 16, 'search__icon'), input);
}

/** Подпись + контрол + подсказка + ошибка. */
function field({ label, hint, control, id, cls } = {}) {
  const error = h('div', { class: 'field__error', role: 'alert', hidden: true });
  const el = h(
    'div',
    { class: ['field', cls] },
    label && h('label', { class: 'field__label', for: id }, label),
    control,
    hint && h('div', { class: 'field__hint' }, hint),
    error,
  );
  return {
    el,
    setError(message) {
      error.textContent = message || '';
      error.hidden = !message;
      el.classList.toggle('has-error', Boolean(message));
    },
  };
}

/** Переключатель (checkbox role=switch). */
function toggle({ checked = false, onChange, label, disabled, id } = {}) {
  const input = h('input', {
    type: 'checkbox',
    role: 'switch',
    class: 'switch__input',
    checked,
    disabled,
    id,
    'aria-label': label,
    onChange: () => onChange && onChange(input.checked),
  });
  const el = h('span', { class: 'switch' }, input, h('span', { class: 'switch__track', 'aria-hidden': 'true' }, h('span', { class: 'switch__thumb' })));
  return { el, input };
}

/** Строка настройки: заголовок и пояснение слева, контрол справа. */
function settingRow({ title, desc, control, id, extra, cls } = {}) {
  return h(
    'div',
    { class: ['setting', cls] },
    h('div', { class: 'setting__text' }, h('label', { class: 'setting__title', for: id }, title), desc && h('div', { class: 'setting__desc' }, desc), extra),
    h('div', { class: 'setting__control' }, control),
  );
}

/** Сегментный переключатель / фильтр-чипы. options: [{value, label, count}]. */
function segmented(options, value, onChange, { cls } = {}) {
  const el = h('div', { class: ['segmented', cls], role: 'radiogroup' });
  let current = value;
  let opts = options;
  function render() {
    replace(
      el,
      opts.map((opt) =>
        h(
          'button',
          {
            type: 'button',
            role: 'radio',
            class: ['segmented__item', opt.value === current && 'is-active'],
            'aria-checked': String(opt.value === current),
            onClick: () => {
              if (current === opt.value) return;
              current = opt.value;
              render();
              onChange(current);
            },
          },
          h('span', {}, opt.label),
          opt.count !== undefined && opt.count !== null && h('span', { class: ['segmented__count', opt.tone && `segmented__count--${opt.tone}`] }, fmtInt(opt.count)),
        ),
      ),
    );
  }
  render();
  return {
    el,
    set(next) {
      current = next;
      render();
    },
    setOptions(next) {
      if (sameData(next, opts)) return;
      opts = next;
      render();
    },
  };
}

/**
 * Ввод списка значений чипами: Enter / запятая добавляют, Backspace удаляет.
 * validate(value) возвращает текст ошибки или ''.
 */
function chipsInput({ values = [], placeholder = '', validate, onChange, inputmode, id } = {}) {
  let items = [...values];
  const list = h('span', { class: 'chips__list' });
  const input = h('input', { class: 'chips__input', placeholder, inputmode, id, autocomplete: 'off', spellcheck: 'false' });
  const error = h('div', { class: 'field__error', role: 'alert', hidden: true });
  const box = h('div', { class: 'chips', onClick: (event) => event.target === box && input.focus() }, list, input);

  function showError(message) {
    error.textContent = message || '';
    error.hidden = !message;
    box.classList.toggle('has-error', Boolean(message));
  }

  function render() {
    replace(
      list,
      items.map((value, index) =>
        h(
          'span',
          { class: 'chip-tag' },
          h('span', { class: 'chip-tag__text' }, String(value)),
          h(
            'button',
            {
              type: 'button',
              class: 'chip-tag__x',
              'aria-label': `Убрать ${value}`,
              title: 'Убрать',
              onClick: () => {
                items.splice(index, 1);
                render();
                onChange && onChange(items.slice());
                input.focus();
              },
            },
            icon('close', 12),
          ),
        ),
      ),
    );
    input.placeholder = items.length ? '' : placeholder;
  }

  /** Добавить то, что набрано в поле. Возвращает false при ошибке. */
  function commit() {
    const parts = input.value.split(/[,;\n]+/).map((part) => part.trim()).filter(Boolean);
    if (!parts.length) {
      input.value = '';
      showError('');
      return true;
    }
    for (const part of parts) {
      const problem = validate ? validate(part) : '';
      if (problem) {
        showError(problem);
        return false;
      }
    }
    let changed = false;
    for (const part of parts) {
      if (!items.includes(part)) {
        items.push(part);
        changed = true;
      }
    }
    input.value = '';
    showError('');
    render();
    if (changed && onChange) onChange(items.slice());
    return true;
  }

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      commit();
    } else if (event.key === 'Backspace' && !input.value && items.length) {
      items.pop();
      render();
      onChange && onChange(items.slice());
    }
  });
  input.addEventListener('blur', commit);
  input.addEventListener('input', () => showError(''));
  render();
  return {
    el: h('div', { class: 'chips-field' }, box, error),
    input,
    commit,
    get: () => items.slice(),
  };
}

/* ---------------------------------------------------------------- Мелочи */

const DEAL_TONES = {
  PENDING: 'muted',
  PAID: 'accent',
  SENT: 'neutral',
  CONFIRMED: 'success',
  ROLLED_BACK: 'warning',
  FAILED: 'danger',
};
const DELIVERY_TONES = {
  delivered: 'success',
  reserved: 'accent',
  no_stock: 'warning',
  failed: 'danger',
  no_rule: 'muted',
  skipped: 'muted',
};

function chip(text, tone = 'muted', { dot = true, cls } = {}) {
  return h(
    'span',
    { class: ['chip', `chip--${tone}`, cls], title: text },
    dot && h('span', { class: 'chip__dot', 'aria-hidden': 'true' }),
    h('span', { class: 'chip__text' }, text),
  );
}

/** Плашка-подсказка: tone = info | warning | danger | success. */
function callout(tone, title, text, actions) {
  const iconName = { warning: 'alert', danger: 'xCircle', success: 'check', info: 'info' }[tone] || 'info';
  return h(
    'div',
    { class: ['callout', `callout--${tone}`] },
    h('span', { class: 'callout__icon' }, icon(iconName, 18)),
    h('div', { class: 'callout__body' }, title && h('div', { class: 'callout__title' }, title), text && h('div', { class: 'callout__text' }, text)),
    actions && h('div', { class: 'callout__actions' }, actions),
  );
}

/** Композиция 2×2 из плиток логотипа. state: running | stopped | error | starting | idle. */
function logoArt(state = 'idle', size = 52) {
  const last = state === 'error' ? gtile('bang', 'danger', size) : gtile('plus', 'blue', size);
  return h(
    'div',
    { class: ['logo-art', `logo-art--${state}`], style: { '--art': `${size}px` }, 'aria-hidden': 'true' },
    gtile('ring', 'white', size),
    gtile('diamond', 'white', size),
    gtile('rect', 'white', size),
    last,
  );
}

function emptyState({ title, text, action, art = 'idle', compact = false } = {}) {
  return h(
    'div',
    { class: ['empty', compact && 'empty--compact'] },
    logoArt(art, compact ? 22 : 30),
    h('div', { class: 'empty__title' }, title),
    text && h('div', { class: 'empty__text' }, text),
    action && h('div', { class: 'empty__action' }, action),
  );
}

async function copyText(text, message = 'Скопировано') {
  let copied = false;
  try {
    await navigator.clipboard.writeText(text);
    copied = true;
  } catch (_) {
    const area = h('textarea', { class: 'visually-hidden', readOnly: true });
    area.value = text;
    document.body.append(area);
    area.select();
    try {
      copied = document.execCommand('copy');
    } catch (__) {
      copied = false;
    }
    area.remove();
  }
  if (copied) toast.ok(message);
  else toast.error('Не удалось скопировать', { text: 'Выделите текст и нажмите Ctrl+C.' });
}

/** Открыть ссылку Playerok во внешнем браузере (только через приложение). */
async function openUrl(url) {
  if (!url) return;
  try {
    await api('open_url', url);
  } catch (exc) {
    toast.error('Не удалось открыть ссылку', { text: exc.message });
  }
}

async function openPath(which) {
  try {
    await api('open_path', which);
  } catch (exc) {
    toast.error('Не удалось открыть', { text: exc.message });
  }
}

/* ---------------------------------------------------------------- Тосты */

const Toasts = {
  show(title, { kind = 'success', text, action, timeout = 4200 } = {}) {
    const root = document.getElementById('toasts');
    if (!root) return () => {};
    const iconName = { success: 'check', error: 'alert', info: 'info', warning: 'alert' }[kind] || 'info';
    let timer;
    const close = () => {
      clearTimeout(timer);
      if (el.classList.contains('is-leaving')) return;
      el.classList.add('is-leaving');
      setTimeout(() => el.remove(), 200);
    };
    const el = h(
      'div',
      { class: ['toast', `toast--${kind}`], role: kind === 'error' ? 'alert' : 'status' },
      h('span', { class: 'toast__icon' }, icon(iconName, 16)),
      h('div', { class: 'toast__body' }, h('div', { class: 'toast__title' }, title), text && h('div', { class: 'toast__text' }, text)),
      action &&
        h(
          'button',
          {
            type: 'button',
            class: 'toast__action',
            onClick: () => {
              close();
              action.onClick();
            },
          },
          action.label,
        ),
      h('button', { type: 'button', class: 'toast__close', 'aria-label': 'Закрыть', title: 'Закрыть', onClick: close }, icon('close', 14)),
    );
    root.append(el);
    while (root.children.length > 4) root.firstElementChild.remove();
    const arm = () => {
      clearTimeout(timer);
      timer = setTimeout(close, timeout);
    };
    el.addEventListener('mouseenter', () => clearTimeout(timer));
    el.addEventListener('mouseleave', arm);
    arm();
    return close;
  },
};

const toast = {
  ok: (title, options = {}) => Toasts.show(title, { ...options, kind: 'success' }),
  info: (title, options = {}) => Toasts.show(title, { ...options, kind: 'info' }),
  warn: (title, options = {}) => Toasts.show(title, { timeout: 6500, ...options, kind: 'warning' }),
  error: (title, options = {}) => Toasts.show(title, { timeout: 7500, ...options, kind: 'error' }),
};

/* ------------------------------------------- Модальные окна и панели */

const Layers = (() => {
  const stack = [];
  const FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]):not([type="file"]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function focusables(panel) {
    return [...panel.querySelectorAll(FOCUSABLE)].filter((el) => el.getClientRects().length > 0);
  }

  function open({ type, panel, label, beforeClose, onClose, initialFocus }) {
    const root = document.getElementById('layers');
    const previous = document.activeElement;
    panel.setAttribute('role', type === 'confirm' ? 'alertdialog' : 'dialog');
    panel.setAttribute('aria-modal', 'true');
    if (label) panel.setAttribute('aria-label', label);
    panel.tabIndex = -1;
    const backdrop = h('div', { class: ['layer', `layer--${type}`] }, panel);
    let pressedOnBackdrop = false;
    backdrop.addEventListener('mousedown', (event) => {
      pressedOnBackdrop = event.target === backdrop;
    });
    backdrop.addEventListener('click', (event) => {
      if (pressedOnBackdrop && event.target === backdrop) entry.close();
    });

    const entry = {
      panel,
      closed: false,
      async close(force = false) {
        if (entry.closed) return;
        if (!force && beforeClose && !(await beforeClose())) return;
        if (entry.closed) return;
        entry.closed = true;
        stack.splice(stack.indexOf(entry), 1);
        backdrop.classList.remove('is-open');
        setTimeout(() => backdrop.remove(), 220);
        if (!stack.length) document.body.classList.remove('has-layer');
        if (previous && typeof previous.focus === 'function' && document.contains(previous)) {
          previous.focus({ preventScroll: true });
        }
        if (onClose) onClose();
      },
    };
    stack.push(entry);
    root.append(backdrop);
    document.body.classList.add('has-layer');
    backdrop.getBoundingClientRect(); // запускаем анимацию появления
    backdrop.classList.add('is-open');
    const target = initialFocus || panel.querySelector('[data-autofocus]') || panel;
    setTimeout(() => target.focus({ preventScroll: true }), 40);
    return entry;
  }

  document.addEventListener(
    'keydown',
    (event) => {
      const top = stack[stack.length - 1];
      if (!top) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        top.close();
      } else if (event.key === 'Tab') {
        const items = focusables(top.panel);
        if (!items.length) {
          event.preventDefault();
          return;
        }
        const first = items[0];
        const last = items[items.length - 1];
        if (!top.panel.contains(document.activeElement)) {
          event.preventDefault();
          first.focus();
        } else if (event.shiftKey && (document.activeElement === first || document.activeElement === top.panel)) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    },
    true,
  );

  return {
    open,
    closeAll() {
      for (const entry of [...stack].reverse()) entry.close(true);
    },
    get count() {
      return stack.length;
    },
  };
})();

function layerHead(title, subtitle, onClose, lead) {
  return h(
    'header',
    { class: 'sheet__head' },
    lead,
    h('div', { class: 'sheet__titles' }, h('h2', { class: 'sheet__title' }, title), subtitle && h('p', { class: 'sheet__sub' }, subtitle)),
    iconBtn('close', 'Закрыть (Esc)', onClose, { cls: 'sheet__close' }),
  );
}

/** Модальное окно по центру. Возвращает объект с close(). */
function openModal({ title, subtitle, lead, body, footer, size = 'md', beforeClose, onClose, initialFocus }) {
  let entry = null;
  const panel = h(
    'div',
    { class: ['sheet', 'modal', `modal--${size}`] },
    layerHead(title, subtitle, () => entry && entry.close(), lead),
    h('div', { class: 'sheet__body' }, body),
    footer && h('footer', { class: 'sheet__foot' }, footer),
  );
  entry = Layers.open({ type: 'modal', panel, label: title, beforeClose, onClose, initialFocus });
  return entry;
}

/** Боковая панель справа. */
function openDrawer({ title, subtitle, lead, body, footer, beforeClose, onClose, wide = false }) {
  let entry = null;
  const panel = h(
    'div',
    { class: ['sheet', 'drawer', wide && 'drawer--wide'] },
    layerHead(title, subtitle, () => entry && entry.close(), lead),
    h('div', { class: 'sheet__body' }, body),
    footer && h('footer', { class: 'sheet__foot' }, footer),
  );
  entry = Layers.open({ type: 'drawer', panel, label: title, beforeClose, onClose });
  return entry;
}

/** Диалог подтверждения вместо window.confirm. Возвращает Promise<boolean>. */
function confirmDialog({ title, text, confirmLabel = 'Подтвердить', cancelLabel = 'Отмена', danger = false } = {}) {
  return new Promise((resolve) => {
    let confirmed = false;
    let entry = null;
    const cancel = btn(cancelLabel, { kind: 'secondary', onClick: () => entry.close() });
    const ok = btn(confirmLabel, {
      kind: danger ? 'danger-solid' : 'primary',
      onClick: () => {
        confirmed = true;
        entry.close();
      },
    });
    const panel = h(
      'div',
      { class: ['sheet', 'modal', 'modal--confirm'] },
      h(
        'div',
        { class: 'confirm' },
        gtile(danger ? 'bang' : 'plus', danger ? 'danger' : 'blue', 44),
        h('div', { class: 'confirm__body' }, h('h2', { class: 'sheet__title' }, title), text && h('p', { class: 'confirm__text' }, text)),
      ),
      h('footer', { class: 'sheet__foot' }, cancel, ok),
    );
    entry = Layers.open({
      type: 'confirm',
      panel,
      label: title,
      initialFocus: danger ? cancel : ok,
      onClose: () => resolve(confirmed),
    });
  });
}

/* ---------------------------------------------- Панель «Сохранить» */

function saveBar({ onSave, onReset, text = 'Есть несохранённые изменения' }) {
  const error = h('span', { class: 'savebar__error', hidden: true });
  const save = btn('Сохранить', { kind: 'primary', icon: 'check', onClick: () => withBusy(save, onSave) });
  const el = h(
    'div',
    { class: 'savebar', hidden: true },
    h(
      'div',
      { class: 'savebar__inner', role: 'region', 'aria-label': 'Несохранённые изменения' },
      h('span', { class: 'savebar__dot', 'aria-hidden': 'true' }),
      h('div', { class: 'savebar__texts' }, h('span', { class: 'savebar__text' }, text), error),
      h('div', { class: 'savebar__actions' }, btn('Отменить', { kind: 'ghost', onClick: onReset }), save),
    ),
  );
  function setError(message) {
    error.textContent = message || '';
    error.hidden = !message;
    el.classList.toggle('has-error', Boolean(message));
  }
  return {
    el,
    show(visible) {
      el.hidden = !visible;
      if (!visible) setError('');
    },
    setError,
  };
}

/** Текст сообщения с подсветкой подстановок {product} {buyer} {item}. */
function templateText(text) {
  return String(text || '')
    .split(/(\{(?:product|buyer|item)\})/g)
    .filter(Boolean)
    .map((part) => (/^\{(product|buyer|item)\}$/.test(part) ? h('span', { class: 'ph' }, part) : part));
}
