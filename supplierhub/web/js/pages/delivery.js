'use strict';
/* Автовыдача: правила (порядок важен), редактор правила и склад товаров. */

(() => {
  const GOODS = ['товар', 'товара', 'товаров'];
  const SAMPLE_PRODUCTS = ['7KQ2-XM4P-99AB', 'P4LW-0QZ8-1HJD', 'Z9RT-55MN-K2Q7'];
  const DEFAULT_MESSAGE = 'Спасибо за покупку, {buyer}!\nВаш товар: {product}\n\nПожалуйста, подтвердите сделку и оставьте отзыв.';
  const PLACEHOLDERS = [
    ['{product}', 'товар со склада'],
    ['{buyer}', 'ник покупателя'],
    ['{item}', 'название лота'],
  ];

  /** Убрать технический префикс «delivery.rules №2:» из ошибки конфига. */
  function cleanError(message) {
    const text = String(message || '').replace(/^delivery\.rules №\d+:\s*/, '');
    return text ? text[0].toUpperCase() + text.slice(1) : text;
  }

  /** Значение товара: литералы \n показываем как перенос строки. */
  function productValue(value) {
    const parts = String(value).split('\\n');
    const out = [];
    parts.forEach((part, index) => {
      if (index) out.push(h('span', { class: 'nl', title: 'Перенос строки (\\n)' }, '↵'));
      out.push(part);
    });
    return out;
  }

  Pages.delivery = (ctx) => {
    let rules = null;
    let lastStockTotal = null;
    const banner = h('div', { class: 'delivery__banner' });
    const restartHint = h('div', { class: 'delivery__banner' });
    let hintStartedAt = null;
    const list = h('div', { class: 'rules' }, h('div', { class: 'card table-skeleton' }, h('div', { class: 'spinner spinner--lg' })));
    const body = h('div', { class: 'delivery' }, banner, restartHint, list);

    /** Бот читает правила при запуске — после правки работающему боту нужен перезапуск. */
    function noteRestart(result) {
      if (!result || !result.restart_required || restartHint.childElementCount) return;
      hintStartedAt = App.overview && App.overview.bot ? App.overview.bot.started_at : null;
      const restart = btn('Перезапустить', { kind: 'primary', size: 'sm', icon: 'restart', onClick: () => botAction('restart') });
      replace(restartHint, callout('info', 'Бот работает со старыми правилами', 'Перезапустите его, чтобы изменения вступили в силу.', [restart]));
    }

    async function load() {
      try {
        const result = await api('list_rules');
        if (!ctx.alive) return;
        if (rules && sameData(result.items, rules)) return;
        rules = result.items || [];
        render();
      } catch (exc) {
        if (ctx.alive && !rules) replace(list, h('div', { class: 'card card--pad' }, callout('danger', 'Не удалось загрузить правила', exc.message)));
      }
    }

    /* ------------------------------------------------------------ Список */

    function render(focus) {
      if (!rules.length) {
        replace(
          list,
          h(
            'div',
            { class: 'card' },
            emptyState({
              title: 'Правил пока нет',
              text: 'Правило связывает лоты с сообщением для покупателя. Например: всем, кто купил «Ключ Steam», отправить ключ со склада.',
              action: btn('Создать первое правило', { kind: 'primary', icon: 'plus', onClick: () => openEditor(null) }),
            }),
          ),
        );
        return;
      }
      replace(list, rules.map((rule, index) => ruleCard(rule, index, rules.length)));
      if (focus) {
        const target = list.querySelector(`[data-rule="${focus.id}"] [data-dir="${focus.dir}"]`);
        const fallback = list.querySelector(`[data-rule="${focus.id}"] [data-dir]:not([disabled])`);
        (target && !target.disabled ? target : fallback)?.focus();
      }
    }

    function stockPanel(rule) {
      const count = rule.stock_count || 0;
      const empty = count === 0;
      const low = count <= rule.low_stock_alert;
      const tone = empty ? 'danger' : low ? 'warning' : 'success';
      const cap = Math.max(rule.low_stock_alert * 4, 20, count);
      const pct = Math.max(empty ? 0 : 4, Math.min(100, (count / cap) * 100));
      return h(
        'button',
        { type: 'button', class: ['stockbox', `stockbox--${tone}`], title: 'Открыть склад', onClick: () => openStock(rule.id) },
        h(
          'span',
          { class: 'stockbox__top' },
          h('span', { class: 'stockbox__label' }, 'На складе'),
          (empty || low) && chip(empty ? 'Пусто' : 'Мало', tone, { dot: false, cls: 'chip--xs' }),
        ),
        h('span', { class: 'stockbox__count' }, fmtInt(count), h('span', { class: 'stockbox__unit' }, 'шт.')),
        h('span', { class: ['bar', `bar--${tone}`] }, h('span', { class: 'bar__fill', style: { width: `${pct}%` } })),
        h('span', { class: 'stockbox__meta' }, `${rule.products_per_sale} за продажу · порог ${rule.low_stock_alert}`),
        h('span', { class: 'stockbox__file', title: rule.stock_file }, rule.stock_file),
      );
    }

    function ruleCard(rule, index, total) {
      const hasStock = Boolean(rule.stock_file);
      const count = rule.stock_count || 0;
      return h(
        'article',
        { class: ['card', 'rule', hasStock && count === 0 && 'rule--empty'], dataset: { rule: rule.id } },
        h(
          'div',
          { class: 'rule__order' },
          ntile(index + 1, 'white', 40),
          h(
            'div',
            { class: 'rule__move' },
            h('button', { type: 'button', class: 'icon-btn icon-btn--ghost', 'aria-label': 'Поднять правило', title: 'Выше', disabled: index === 0, dataset: { dir: -1 }, onClick: () => move(rule, -1) }, icon('up', 15)),
            h('button', { type: 'button', class: 'icon-btn icon-btn--ghost', 'aria-label': 'Опустить правило', title: 'Ниже', disabled: index === total - 1, dataset: { dir: 1 }, onClick: () => move(rule, 1) }, icon('down', 15)),
          ),
        ),
        h(
          'div',
          { class: 'rule__main' },
          h('div', { class: 'rule__eyebrow' }, 'Лоты, где в названии есть'),
          h('h3', { class: 'rule__match', title: rule.match }, `«${rule.match}»`),
          h('div', { class: 'rule__message', title: rule.message }, h('div', { class: 'rule__message-text' }, templateText(rule.message.replace(/\n\s*\n+/g, '\n')))),
          h(
            'div',
            { class: 'rule__actions' },
            btn('Редактировать', { kind: 'secondary', size: 'sm', icon: 'edit', onClick: () => openEditor(rule) }),
            hasStock && btn('Склад', { kind: 'secondary', size: 'sm', icon: 'box', onClick: () => openStock(rule.id) }),
            h('span', { class: 'grow' }),
            btn('Удалить', { kind: 'ghost-danger', size: 'sm', icon: 'trash', onClick: () => remove(rule) }),
          ),
        ),
        h(
          'div',
          { class: 'rule__stock' },
          hasStock
            ? stockPanel(rule)
            : h(
                'div',
                { class: 'stockbox stockbox--none' },
                h('span', { class: 'stockbox__top' }, h('span', { class: 'stockbox__label' }, 'Без склада')),
                gtile('rect', 'muted', 30),
                h('span', { class: 'stockbox__meta' }, 'Всем покупателям уходит один и тот же текст'),
              ),
        ),
      );
    }

    async function move(rule, direction) {
      try {
        const result = await api('move_rule', rule.id, direction);
        rules = result.items || [];
        render({ id: rule.id + direction, dir: direction });
        noteRestart(result);
      } catch (exc) {
        toast.error('Не удалось переместить правило', { text: exc.message });
      }
    }

    async function remove(rule) {
      const ok = await confirmDialog({
        title: `Удалить правило «${rule.match}»?`,
        text: rule.stock_file
          ? `Файл склада ${rule.stock_file} останется на диске — его можно подключить к новому правилу.`
          : 'Бот перестанет отправлять сообщение по этому правилу.',
        confirmLabel: 'Удалить',
        danger: true,
      });
      if (!ok) return;
      try {
        noteRestart(await api('delete_rule', rule.id));
        toast.ok('Правило удалено');
        rules = null;
        await load();
        Poller.kick('state');
      } catch (exc) {
        toast.error('Не удалось удалить правило', { text: exc.message });
      }
    }

    /* ----------------------------------------------------------- Редактор */

    function openEditor(rule, preset = {}) {
      const isNew = !rule;
      const form = {
        match: rule ? rule.match : preset.match || '',
        message: rule ? rule.message : DEFAULT_MESSAGE,
        use_stock: rule ? Boolean(rule.stock_file) : true,
        stock_file: rule ? rule.stock_file || null : null,
        products_per_sale: String(rule ? rule.products_per_sale : 1),
        low_stock_alert: String(rule ? rule.low_stock_alert : 3),
      };
      const initial = JSON.stringify(form);
      let entry = null;

      const matchInput = textInput({
        value: form.match,
        placeholder: 'Например: Ключ Steam',
        id: 'rule-match',
        onInput: (value) => {
          form.match = value;
          matchField.setError('');
          updatePreview();
        },
      });
      matchInput.dataset.autofocus = '';
      const matchField = field({
        label: 'Часть названия лота',
        id: 'rule-match',
        control: matchInput,
        hint: 'Регистр не важен: «ключ steam» подойдёт к лоту «Ключ Steam — Cyberpunk 2077».',
      });

      const textarea = h('textarea', {
        class: 'input textarea',
        id: 'rule-message',
        rows: 6,
        spellcheck: 'false',
        onInput: () => {
          form.message = textarea.value;
          messageField.setError('');
          updatePreview();
        },
      });
      textarea.value = form.message;
      const insert = (token) => {
        textarea.focus();
        textarea.setRangeText(token, textarea.selectionStart, textarea.selectionEnd, 'end');
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
      };
      const messageField = field({
        label: 'Сообщение покупателю',
        id: 'rule-message',
        control: h(
          'div',
          { class: 'composer' },
          h(
            'div',
            { class: 'composer__bar' },
            h('span', { class: 'composer__label' }, 'Вставить'),
            PLACEHOLDERS.map(([token, hint]) =>
              h('button', { type: 'button', class: 'ph-chip', title: `Вставить ${token} — ${hint}`, onClick: () => insert(token) }, h('span', { class: 'ph-chip__token' }, token), h('span', { class: 'ph-chip__hint' }, hint)),
            ),
          ),
          textarea,
        ),
      });

      const preview = h('div', { class: 'bubble' });
      const previewNote = h('div', { class: 'preview__note', hidden: true });

      const perInput = textInput({
        type: 'number',
        min: 1,
        step: 1,
        value: form.products_per_sale,
        id: 'rule-per-sale',
        onInput: (value) => {
          form.products_per_sale = value;
          perField.setError('');
          updatePreview();
        },
      });
      const perField = field({ label: 'Товаров за одну продажу', id: 'rule-per-sale', control: perInput, hint: 'Столько строк склада получит покупатель.' });
      const lowInput = textInput({
        type: 'number',
        min: 0,
        step: 1,
        value: form.low_stock_alert,
        id: 'rule-low',
        onInput: (value) => {
          form.low_stock_alert = value;
          lowField.setError('');
        },
      });
      const lowField = field({ label: 'Предупредить, когда останется', id: 'rule-low', control: lowInput, hint: 'Уведомим, когда товаров станет столько или меньше.' });
      const fileNote = h(
        'div',
        { class: 'file-note' },
        icon('file', 16),
        form.stock_file
          ? h('span', {}, 'Файл склада: ', h('code', { class: 'mono' }, form.stock_file))
          : h('span', {}, 'Файл склада создастся автоматически в папке stock.'),
      );
      const stockFields = h('div', { class: 'editor__stock-fields', hidden: !form.use_stock }, h('div', { class: 'grid-2' }, perField.el, lowField.el), fileNote);
      const stockToggle = toggle({
        checked: form.use_stock,
        id: 'rule-stock',
        label: 'Выдавать товар со склада',
        onChange: (value) => {
          form.use_stock = value;
          stockFields.hidden = !value;
          messageField.setError('');
          updatePreview();
        },
      });
      const formError = h('div', { class: 'form-error', role: 'alert', hidden: true });

      function updatePreview() {
        const amount = Math.min(5, Math.max(1, parseInt(form.products_per_sale, 10) || 1));
        const values = {
          product: form.use_stock ? Array.from({ length: amount }, (_, i) => SAMPLE_PRODUCTS[i % SAMPLE_PRODUCTS.length]).join('\n') : '',
          buyer: 'kirill_play',
          item: form.match.trim() ? `${form.match.trim()} — пример лота` : 'Название лота',
        };
        const parts = form.message
          .split(/(\{(?:product|buyer|item)\})/g)
          .filter(Boolean)
          .map((part) => {
            const found = /^\{(product|buyer|item)\}$/.exec(part);
            return found ? h('span', { class: 'bubble__value' }, values[found[1]]) : part;
          });
        replace(preview, form.message.trim() ? parts : h('span', { class: 'bubble__empty' }, 'Напишите сообщение — здесь появится предпросмотр.'));
        let note = '';
        if (form.use_stock && form.message.trim() && !form.message.includes('{product}')) note = 'Нет {product} — покупатель не получит товар со склада.';
        else if (!form.use_stock && form.message.includes('{product}')) note = 'Склад выключен — на месте {product} будет пусто.';
        previewNote.textContent = note;
        previewNote.hidden = !note;
      }

      function showServerError(message) {
        const text = cleanError(message);
        if (/match/.test(message)) matchField.setError(text);
        else if (/products_per_sale/.test(message)) perField.setError(text);
        else if (/low_stock_alert/.test(message)) lowField.setError(text);
        else if (/\{product\}|message/.test(message)) messageField.setError(text);
        else {
          formError.textContent = text;
          formError.hidden = false;
        }
      }

      async function save() {
        formError.hidden = true;
        let ok = true;
        if (!form.match.trim()) {
          matchField.setError('Укажите часть названия лота.');
          ok = false;
        }
        if (!form.message.trim()) {
          messageField.setError('Напишите сообщение для покупателя.');
          ok = false;
        } else if (form.use_stock && !form.message.includes('{product}')) {
          messageField.setError('Добавьте {product} — иначе покупатель не получит товар со склада.');
          ok = false;
        }
        const perSale = Number(form.products_per_sale);
        const lowAlert = Number(form.low_stock_alert);
        if (form.use_stock && (!Number.isInteger(perSale) || perSale < 1)) {
          perField.setError('Нужно целое число от 1.');
          ok = false;
        }
        if (form.use_stock && (!Number.isInteger(lowAlert) || lowAlert < 0)) {
          lowField.setError('Нужно целое число от 0.');
          ok = false;
        }
        if (!ok) {
          entry.panel.querySelector('.has-error input, .has-error textarea')?.focus();
          return;
        }
        const payload = {
          match: form.match.trim(),
          message: form.message,
          use_stock: form.use_stock,
          stock_file: form.use_stock ? form.stock_file : null,
          products_per_sale: form.use_stock ? perSale : rule ? rule.products_per_sale : 1,
          low_stock_alert: form.use_stock ? lowAlert : rule ? rule.low_stock_alert : 0,
        };
        let result;
        try {
          result = await api('save_rule', payload, isNew ? null : rule.id);
        } catch (exc) {
          showServerError(exc.message);
          return;
        }
        noteRestart(result);
        entry.close(true);
        toast.ok(isNew ? 'Правило создано' : 'Правило сохранено');
        rules = null;
        await load();
        Poller.kick('state');
      }

      const saveButton = btn(isNew ? 'Создать правило' : 'Сохранить', { kind: 'primary', icon: 'check', onClick: () => withBusy(saveButton, save) });
      const body = h(
        'div',
        {
          class: 'editor',
          onKeydown: (event) => {
            if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              withBusy(saveButton, save);
            }
          },
        },
        matchField.el,
        messageField.el,
        h('div', { class: 'preview' }, h('div', { class: 'preview__label' }, icon('chat', 15), 'Так увидит покупатель'), preview, previewNote),
        h(
          'div',
          { class: 'editor__stock' },
          settingRow({
            title: 'Выдавать товар со склада',
            desc: 'Каждой продаже — строки из файла склада, выданные строки удаляются.',
            control: stockToggle.el,
            id: 'rule-stock',
          }),
          stockFields,
        ),
        formError,
      );
      updatePreview();

      entry = openModal({
        title: isNew ? 'Новое правило' : 'Правило автовыдачи',
        subtitle: isNew ? 'Что отправить покупателю после оплаты' : `Правило №${rule.id + 1}`,
        lead: gtile('diamond', 'white', 36),
        body,
        size: 'lg',
        footer: [h('span', { class: 'sheet__hint' }, 'Ctrl+Enter — сохранить'), btn('Отмена', { kind: 'ghost', onClick: () => entry.close() }), saveButton],
        beforeClose: () =>
          JSON.stringify(form) === initial ||
          confirmDialog({ title: 'Закрыть без сохранения?', text: 'Изменения в правиле пропадут.', confirmLabel: 'Закрыть', cancelLabel: 'Вернуться', danger: true }),
      });
    }

    /* -------------------------------------------------------------- Склад */

    function openStock(ruleId) {
      const rule = (rules || []).find((item) => item.id === ruleId);
      let stock = null;
      let reveal = false;

      const summary = h('div', { class: 'stock__summary' });
      const items = h('ol', { class: 'stock-items' });
      const revealBtn = btn('Показать', {
        kind: 'ghost',
        size: 'sm',
        icon: 'eye',
        onClick: () => {
          reveal = !reveal;
          replace(revealBtn, icon(reveal ? 'eyeOff' : 'eye', 15), h('span', { class: 'btn__label' }, reveal ? 'Скрыть' : 'Показать'));
          renderItems();
        },
      });
      const clearBtn = btn('Очистить', { kind: 'ghost-danger', size: 'sm', icon: 'trash', onClick: () => clearAll() });
      const area = h('textarea', {
        class: 'input textarea textarea--mono',
        rows: 3,
        spellcheck: 'false',
        placeholder: 'Один товар — одна строка\nABCD-EFGH-IJKL\nlogin: user42 \\n pass: qwerty',
      });
      const fileInput = h('input', { type: 'file', accept: '.txt,text/plain', class: 'visually-hidden', tabindex: '-1', 'aria-hidden': 'true' });
      const uploadBtn = btn('Загрузить из .txt', { kind: 'secondary', icon: 'upload', onClick: () => fileInput.click() });
      const addBtn = btn('Добавить', { kind: 'primary', icon: 'plus', onClick: () => withBusy(addBtn, () => add(area.value, true)) });

      fileInput.addEventListener('change', () => {
        const file = fileInput.files && fileInput.files[0];
        fileInput.value = '';
        if (!file) return;
        if (file.size > 10 * 1024 * 1024) {
          toast.error('Файл слишком большой', { text: 'Загрузите файл до 10 МБ.' });
          return;
        }
        const reader = new FileReader();
        reader.onload = () => withBusy(uploadBtn, () => add(String(reader.result || '').replace(/^﻿/, ''), false, file.name));
        reader.onerror = () => toast.error('Не удалось прочитать файл', { text: file.name });
        reader.readAsText(file, 'utf-8');
      });

      const body = h(
        'div',
        { class: 'stock' },
        summary,
        h('div', { class: 'stock__list-head' }, h('h3', { class: 'sheet-section__title' }, 'Товары'), h('div', { class: 'stock__tools' }, revealBtn, clearBtn)),
        items,
        h(
          'section',
          { class: 'stock__add' },
          h('h3', { class: 'sheet-section__title' }, 'Добавить товары'),
          area,
          h('p', { class: 'field__hint' }, 'Каждая строка — отдельный товар. Многострочный товар запишите в одну строку через \\n — покупатель получит его с переносами.'),
          h('div', { class: 'stock__add-actions' }, uploadBtn, fileInput, h('span', { class: 'grow' }), addBtn),
        ),
      );

      openDrawer({
        title: 'Склад',
        subtitle: rule ? `«${rule.match}»` : `Правило №${ruleId + 1}`,
        lead: gtile('rect', 'white', 36),
        body,
        wide: true,
      });

      function renderSummary() {
        const count = stock.count;
        const tone = !rule ? 'neutral' : count === 0 ? 'danger' : count <= rule.low_stock_alert ? 'warning' : 'success';
        replace(
          summary,
          h(
            'div',
            { class: ['stock__count', `tone--${tone}`] },
            h('span', { class: 'stock__count-value' }, fmtInt(count)),
            h('span', { class: 'stock__count-label' }, `${plural(count, GOODS)} на складе`),
          ),
          h(
            'div',
            { class: 'stock__file' },
            icon('file', 15),
            h('code', { class: 'mono', title: stock.file }, stock.file),
            rule && h('span', { class: 'stock__per' }, `${rule.products_per_sale} за продажу · порог ${rule.low_stock_alert}`),
          ),
        );
        clearBtn.disabled = !count;
        revealBtn.disabled = !count;
      }

      function renderItems() {
        if (!stock.items.length) {
          replace(items, h('li', { class: 'stock-items__empty' }, emptyState({ title: 'Склад пуст', text: 'Добавьте товары ниже или загрузите .txt-файл.', compact: true })));
          return;
        }
        replace(
          items,
          stock.items.map((value, index) =>
            h(
              'li',
              { class: 'stock-item' },
              h('span', { class: 'stock-item__index' }, String(index + 1)),
              h('span', { class: ['stock-item__value', !reveal && 'is-masked'] }, reveal ? productValue(value) : '••••••••••••'),
              reveal && iconBtn('copy', 'Копировать', () => copyText(value.replace(/\\n/g, '\n')), { cls: 'stock-item__btn', size: 15 }),
              iconBtn('trash', 'Удалить товар', () => removeItem(index, value), { cls: 'stock-item__btn stock-item__btn--danger', size: 15 }),
            ),
          ),
        );
      }

      async function reload() {
        try {
          stock = await api('get_stock', ruleId);
        } catch (exc) {
          replace(items, h('li', {}, callout('danger', 'Не удалось открыть склад', exc.message)));
          return;
        }
        renderSummary();
        renderItems();
      }

      async function add(text, fromArea, fileName) {
        if (!text.trim()) {
          toast.warn(fileName ? 'Файл пустой' : 'Нечего добавлять', { text: 'Нужны товары — по одному в строке.' });
          return;
        }
        try {
          const result = await api('add_stock', ruleId, text);
          if (fromArea) area.value = '';
          toast.ok(result.added ? `Добавлено: ${countText(result.added, GOODS)}` : 'Новых товаров нет', {
            text: `${fileName ? `Из файла ${fileName}. ` : ''}Теперь на складе ${countText(result.count, GOODS)}.`,
          });
        } catch (exc) {
          toast.error('Не удалось добавить товары', { text: exc.message });
          return;
        }
        await reload();
        load();
        Poller.kick('state');
      }

      async function removeItem(index, value) {
        try {
          const result = await api('remove_stock_item', ruleId, index, value);
          if (result.removed) toast.ok('Товар удалён');
          else toast.warn('Склад изменился', { text: 'Похоже, бот только что выдал этот товар — список обновлён.' });
        } catch (exc) {
          toast.error('Не удалось удалить товар', { text: exc.message });
        }
        await reload();
        load();
        Poller.kick('state');
      }

      async function clearAll() {
        const ok = await confirmDialog({
          title: 'Очистить склад?',
          text: `Из файла ${stock.file} будут удалены все ${countText(stock.count, GOODS)}. Отменить это нельзя.`,
          confirmLabel: 'Очистить',
          danger: true,
        });
        if (!ok) return;
        try {
          const result = await api('clear_stock', ruleId);
          toast.ok(`Склад очищен: удалено ${countText(result.removed, GOODS)}`);
        } catch (exc) {
          toast.error('Не удалось очистить склад', { text: exc.message });
        }
        await reload();
        load();
        Poller.kick('state');
      }

      replace(items, h('li', { class: 'stock-items__loading' }, h('span', { class: 'spinner spinner--lg' })));
      reload();
    }

    /* ---------------------------------------------------- Баннер и опрос */

    async function enableDelivery(button) {
      await withBusy(button, async () => {
        try {
          const cfg = await api('get_config');
          cfg.delivery.enabled = true;
          await saveConfig(cfg);
        } catch (exc) {
          toast.error('Не удалось включить автовыдачу', { text: exc.message });
        }
      });
    }

    function renderBanner(ov) {
      if (!ov || ov.features.delivery) {
        replace(banner);
        return;
      }
      if (banner.childElementCount) return;
      const enable = btn('Включить', { kind: 'secondary', size: 'sm', onClick: () => enableDelivery(enable) });
      replace(banner, callout('warning', 'Автовыдача выключена', 'Правила сохранены, но бот не отправит товар, пока автовыдача выключена.', [enable]));
    }

    ctx.on('overview', (ov) => {
      renderBanner(ov);
      if (restartHint.childElementCount && (ov.bot.status !== 'running' || ov.bot.started_at !== hintStartedAt)) {
        replace(restartHint);
      }
      const total = ov.stats.stock_total;
      if (lastStockTotal !== null && total !== lastStockTotal) load();
      lastStockTotal = total;
    });
    renderBanner(App.overview);
    const loaded = load();

    return {
      title: 'Автовыдача',
      subtitle: 'Правила проверяются сверху вниз — срабатывает первое подходящее',
      body,
      actions: [
        btn('Папка склада', { kind: 'ghost', icon: 'folder', onClick: () => openPath('stock') }),
        btn('Новое правило', { kind: 'primary', icon: 'plus', onClick: () => openEditor(null) }),
      ],
      onParams(params) {
        if (params.get('new')) {
          const match = params.get('match') || '';
          history.replaceState(null, '', '#/delivery');
          loaded.then(() => ctx.alive && openEditor(null, { match }));
        } else if (params.has('stock')) {
          const id = Number(params.get('stock'));
          history.replaceState(null, '', '#/delivery');
          loaded.then(() => ctx.alive && Number.isInteger(id) && openStock(id));
        }
      },
    };
  };
})();
