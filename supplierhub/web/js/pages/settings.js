'use strict';
/* Настройки: Playerok, Telegram, автовыдача, дополнительные параметры и папка с данными. */

(() => {
  function snapshot(cfg) {
    const p = cfg.playerok || {};
    const t = cfg.telegram || {};
    const d = cfg.delivery || {};
    return {
      playerok: {
        token: p.token || '',
        proxy: p.proxy || '',
        deals_interval: String(p.deals_interval ?? 20),
        use_websocket: p.use_websocket !== false,
      },
      telegram: {
        enabled: Boolean(t.enabled),
        bot_token: t.bot_token || '',
        chat_ids: (t.chat_ids || []).map(String),
        notify_messages: t.notify_messages !== false,
        messages_interval: String(t.messages_interval ?? 10),
        proxy: t.proxy || '',
      },
      delivery: { enabled: d.enabled !== false, mark_sent: d.mark_sent !== false },
    };
  }

  function telegramSettings(t) {
    return {
      enabled: t.enabled,
      bot_token: t.bot_token.trim(),
      chat_ids: t.chat_ids.map(Number),
      notify_messages: t.notify_messages,
      messages_interval: Number(t.messages_interval),
      proxy: t.proxy.trim(),
    };
  }

  function toConfig(form, base) {
    return {
      playerok: {
        token: form.playerok.token.trim(),
        proxy: form.playerok.proxy.trim(),
        deals_interval: Number(form.playerok.deals_interval),
        use_websocket: form.playerok.use_websocket,
      },
      telegram: telegramSettings(form.telegram),
      delivery: { ...form.delivery },
      relist: base.relist,
    };
  }

  /** Строка статуса под кнопкой «Проверить» / «Отправить тест». */
  function setStatus(el, tone, text) {
    el.hidden = !text;
    el.className = `inline-status tone--${tone}`;
    replace(el, icon(tone === 'success' ? 'check' : tone === 'danger' ? 'xCircle' : 'info', 16), h('span', {}, text));
  }

  function section({ id, glyphName, tone = 'white', title, desc, aside, children }) {
    return h(
      'section',
      { class: 'card settings-section', id: `settings-${id}`, 'aria-labelledby': `settings-${id}-title` },
      h(
        'header',
        { class: 'settings-section__head' },
        gtile(glyphName, tone, 36),
        h('div', { class: 'settings-section__titles' }, h('h2', { id: `settings-${id}-title` }, title), desc && h('p', {}, desc)),
        aside && h('div', { class: 'settings-section__aside' }, aside),
      ),
      h('div', { class: 'settings-section__body' }, children),
    );
  }

  Pages.settings = (ctx) => {
    let base = null;
    let original = null;
    let form = null;
    let ui = null;
    let pendingFocus = null;
    const content = h('div', { class: 'settings__content' }, h('div', { class: 'card table-skeleton' }, h('div', { class: 'spinner spinner--lg' })));
    const bar = saveBar({ onSave: save, onReset: reset });
    const body = h('div', { class: 'settings' }, content, bar.el);

    const isDirty = () => Boolean(form) && !sameData(form, original);
    const changed = () => bar.show(isDirty());

    function build() {
      const f = form;
      const set = (group, key) => (value) => {
        f[group][key] = value;
        changed();
      };

      /* ---- Playerok ---- */
      const token = secretInput({
        value: f.playerok.token,
        placeholder: 'Вставьте значение cookie token',
        id: 'set-token',
        onInput: (value) => {
          f.playerok.token = value;
          tokenField.setError('');
          changed();
        },
      });
      const tokenStatus = h('div', { class: 'inline-status', hidden: true });
      const checkBtn = btn('Проверить', {
        kind: 'secondary',
        icon: 'key',
        onClick: () =>
          withBusy(checkBtn, async () => {
            setStatus(tokenStatus, 'muted', 'Входим в Playerok…');
            try {
              const result = await api('check_token', f.playerok.token.trim() || null);
              setStatus(tokenStatus, 'success', `Вход выполнен: ${result.username} · баланс ${fmtMoney(result.balance)}`);
            } catch (exc) {
              setStatus(tokenStatus, 'danger', exc.message);
            }
          }),
      });
      const tokenField = field({
        label: 'Токен аккаунта',
        id: 'set-token',
        control: h('div', { class: 'input-row' }, token.el, checkBtn),
      });
      const tokenHelp = h(
        'details',
        { class: 'howto' },
        h('summary', {}, icon('info', 15), 'Где взять токен?'),
        h(
          'ol',
          { class: 'howto__steps' },
          h('li', {}, 'Откройте playerok.com в браузере и войдите в аккаунт продавца.'),
          h('li', {}, 'Нажмите F12 → вкладка «Application» («Приложение») → Cookies → https://playerok.com.'),
          h('li', {}, 'Скопируйте значение cookie ', h('code', {}, 'token'), ' и вставьте сюда.'),
        ),
        h('p', { class: 'howto__note' }, 'Токен даёт полный доступ к аккаунту — никому его не показывайте. Если выйти из аккаунта в браузере, токен перестанет работать.'),
      );

      /* ---- Telegram ---- */
      const tgEnabled = toggle({
        checked: f.telegram.enabled,
        id: 'set-tg-enabled',
        label: 'Уведомления в Telegram',
        onChange: (value) => {
          f.telegram.enabled = value;
          tgOff.hidden = value;
          changed();
        },
      });
      const botToken = secretInput({
        value: f.telegram.bot_token,
        placeholder: '1234567890:AA…',
        id: 'set-tg-token',
        onInput: (value) => {
          f.telegram.bot_token = value;
          botTokenField.setError('');
          changed();
        },
      });
      const botTokenField = field({ label: 'Токен бота', id: 'set-tg-token', control: botToken.el, hint: 'Создайте бота у @BotFather и скопируйте токен.' });
      const chats = chipsInput({
        values: f.telegram.chat_ids,
        placeholder: 'Например: 123456789',
        inputmode: 'numeric',
        id: 'set-tg-chats',
        validate: (value) => (/^-?\d{4,20}$/.test(value) ? '' : 'Chat id — это число, например 123456789.'),
        onChange: (values) => {
          f.telegram.chat_ids = values;
          chatsField.setError('');
          changed();
        },
      });
      const chatsField = field({ label: 'Кому писать (chat id)', id: 'set-tg-chats', control: chats.el, hint: 'Свой id подскажет @userinfobot. Можно несколько.' });
      const notify = toggle({ checked: f.telegram.notify_messages, id: 'set-tg-notify', label: 'Пересылать сообщения покупателей', onChange: set('telegram', 'notify_messages') });
      const tgStatus = h('div', { class: 'inline-status', hidden: true });
      const testBtn = btn('Отправить тест', {
        kind: 'secondary',
        icon: 'send',
        onClick: () =>
          withBusy(testBtn, async () => {
            if (!chats.commit()) return;
            const settings = telegramSettings(f.telegram);
            if (!settings.bot_token || !settings.chat_ids.length) {
              setStatus(tgStatus, 'danger', 'Укажите токен бота и хотя бы один chat id.');
              return;
            }
            setStatus(tgStatus, 'muted', 'Отправляем…');
            try {
              await api('test_telegram', { ...settings, enabled: true, messages_interval: settings.messages_interval || 10 });
              setStatus(tgStatus, 'success', 'Сообщение отправлено — проверьте Telegram.');
            } catch (exc) {
              setStatus(tgStatus, 'danger', exc.message);
            }
          }),
      });
      const tgOff = h('div', { class: 'section-note', hidden: f.telegram.enabled }, icon('info', 15), 'Уведомления выключены — события видны только в приложении и журнале.');

      /* ---- Автовыдача ---- */
      const deliveryOn = toggle({ checked: f.delivery.enabled, id: 'set-delivery', label: 'Автовыдача', onChange: set('delivery', 'enabled') });
      const markSent = toggle({ checked: f.delivery.mark_sent, id: 'set-mark-sent', label: 'Отмечать сделку выполненной', onChange: set('delivery', 'mark_sent') });

      /* ---- Дополнительно ---- */
      const proxyInput = (group, id) =>
        textInput({ value: f[group].proxy, placeholder: 'http://user:pass@1.2.3.4:8080', mono: true, id, onInput: set(group, 'proxy') });
      const dealsField = field({
        label: 'Проверка продаж, сек',
        id: 'set-deals-interval',
        control: textInput({
          type: 'number',
          min: 5,
          step: 1,
          value: f.playerok.deals_interval,
          id: 'set-deals-interval',
          onInput: (value) => {
            f.playerok.deals_interval = value;
            dealsField.setError('');
            changed();
          },
        }),
        hint: 'Не меньше 5. Страховка на случай, если WebSocket пропустит событие.',
      });
      const messagesField = field({
        label: 'Проверка сообщений, сек',
        id: 'set-messages-interval',
        control: textInput({
          type: 'number',
          min: 3,
          step: 1,
          value: f.telegram.messages_interval,
          id: 'set-messages-interval',
          onInput: (value) => {
            f.telegram.messages_interval = value;
            messagesField.setError('');
            changed();
          },
        }),
        hint: 'Не меньше 3. Как часто искать новые сообщения покупателей.',
      });
      const websocket = toggle({ checked: f.playerok.use_websocket, id: 'set-websocket', label: 'WebSocket', onChange: set('playerok', 'use_websocket') });

      /* ---- Данные ---- */
      const workdir = (App.info && App.info.workdir) || '';
      const modeLabel = App.info && App.info.mode === 'browser' ? 'в браузере' : 'в окне';

      const sections = [
        section({
          id: 'playerok',
          glyphName: 'ring',
          title: 'Playerok',
          desc: 'Вход в ваш аккаунт продавца',
          children: [tokenField.el, tokenStatus, tokenHelp],
        }),
        section({
          id: 'telegram',
          glyphName: 'diamond',
          title: 'Telegram',
          desc: 'Уведомления о продажах, выдаче и сообщениях покупателей',
          aside: tgEnabled.el,
          children: [
            tgOff,
            h('div', { class: 'grid-2' }, botTokenField.el, chatsField.el),
            h('div', { class: 'settings-list settings-list--flush' }, settingRow({ title: 'Пересылать сообщения покупателей', desc: 'Новые сообщения из чатов Playerok придут в Telegram.', control: notify.el, id: 'set-tg-notify' })),
            h(
              'div',
              { class: 'test-row' },
              testBtn,
              h('p', { class: 'field__hint test-row__hint' }, 'Перед проверкой напишите своему боту /start — иначе Telegram не даст ему писать вам.'),
            ),
            tgStatus,
          ],
        }),
        section({
          id: 'delivery',
          glyphName: 'rect',
          title: 'Автовыдача',
          desc: 'Отправка товара сразу после оплаты',
          aside: h('a', { class: 'link', href: '#/delivery' }, 'Правила', icon('chevronRight', 14)),
          children: h(
            'div',
            { class: 'settings-list settings-list--flush' },
            settingRow({ title: 'Выдавать товар автоматически', desc: 'По правилам из раздела «Автовыдача».', control: deliveryOn.el, id: 'set-delivery' }),
            settingRow({ title: 'Отмечать сделку выполненной', desc: 'После выдачи нажимать «Я отправил» за вас — сделка получит статус «Отправлена».', control: markSent.el, id: 'set-mark-sent' }),
          ),
        }),
        section({
          id: 'advanced',
          glyphName: 'plus',
          title: 'Дополнительно',
          desc: 'Прокси и частота проверок — обычно менять не нужно',
          children: [
            h(
              'div',
              { class: 'grid-2' },
              field({ label: 'Прокси для Playerok', id: 'set-pl-proxy', control: proxyInput('playerok', 'set-pl-proxy'), hint: 'Пусто — без прокси.' }).el,
              field({ label: 'Прокси для Telegram', id: 'set-tg-proxy', control: proxyInput('telegram', 'set-tg-proxy'), hint: 'Пригодится, если Telegram недоступен.' }).el,
              dealsField.el,
              messagesField.el,
            ),
            h(
              'div',
              { class: 'settings-list settings-list--flush' },
              settingRow({ title: 'Мгновенные события через WebSocket', desc: 'Продажи приходят сразу, без ожидания следующей проверки.', control: websocket.el, id: 'set-websocket' }),
            ),
          ],
        }),
        section({
          id: 'data',
          glyphName: 'rect',
          tone: 'muted',
          title: 'Данные',
          desc: 'Настройки, склад, история продаж и журнал хранятся в рабочей папке',
          children: [
            h(
              'div',
              { class: 'path-box' },
              icon('folder', 16),
              h('code', { class: 'path-box__path', title: workdir }, workdir || '—'),
              workdir && iconBtn('copy', 'Копировать путь', () => copyText(workdir), { size: 15 }),
            ),
            h(
              'div',
              { class: 'button-row' },
              btn('Открыть папку', { kind: 'secondary', icon: 'folder', onClick: () => openPath('workdir') }),
              btn('Папка склада', { kind: 'ghost', icon: 'box', onClick: () => openPath('stock') }),
              btn('Файл журнала', { kind: 'ghost', icon: 'file', onClick: () => openPath('log') }),
            ),
            h('p', { class: 'about' }, `SupplierHub ${App.info ? App.info.version : ''} · работает ${modeLabel}`),
            // В браузере закрыть вкладку мало: приложение и бот продолжат работать в фоне.
            isBrowserMode() &&
              h(
                'div',
                { class: 'quit-row' },
                h('p', { class: 'field__hint' }, 'Вкладку браузера можно закрыть — бот продолжит работу. Чтобы выключить и бота, и SupplierHub, нажмите «Выйти».'),
                btn('Выйти из SupplierHub', { kind: 'ghost-danger', icon: 'close', onClick: () => quitApp() }),
              ),
          ],
        }),
      ];
      replace(content, h('div', { class: 'settings-stack' }, sections));
      return { token, tokenField, botToken, botTokenField, chats, chatsField, dealsField, messagesField };
    }

    function applyFocus() {
      if (!pendingFocus || !ui) return;
      const target = pendingFocus;
      pendingFocus = null;
      const sectionId = target === 'token' ? 'playerok' : target;
      const sectionEl = content.querySelector(`#settings-${sectionId}`);
      if (!sectionEl) return;
      sectionEl.scrollIntoView({ block: 'start', behavior: 'smooth' });
      sectionEl.classList.remove('is-flash');
      sectionEl.getBoundingClientRect();
      sectionEl.classList.add('is-flash');
      const input = target === 'token' ? ui.token.input : target === 'telegram' ? ui.botToken.input : null;
      if (input) setTimeout(() => input.focus({ preventScroll: true }), 350);
      history.replaceState(null, '', '#/settings');
    }

    async function load() {
      try {
        base = await api('get_config');
      } catch (exc) {
        if (ctx.alive) replace(content, h('div', { class: 'card card--pad' }, callout('danger', 'Не удалось загрузить настройки', exc.message)));
        return;
      }
      if (!ctx.alive) return;
      original = snapshot(base);
      form = snapshot(base);
      ui = build();
      changed();
      applyFocus();
    }

    function reset() {
      form = snapshot(base);
      ui = build();
      changed();
    }

    function validate() {
      let ok = ui.chats.commit();
      const deals = Number(form.playerok.deals_interval);
      if (!Number.isFinite(deals) || deals < 5) {
        ui.dealsField.setError('Не меньше 5 секунд.');
        ok = false;
      }
      const messages = Number(form.telegram.messages_interval);
      if (!Number.isFinite(messages) || messages < 3) {
        ui.messagesField.setError('Не меньше 3 секунд.');
        ok = false;
      }
      if (form.telegram.enabled && !form.telegram.bot_token.trim()) {
        ui.botTokenField.setError('Telegram включён — нужен токен бота.');
        ok = false;
      }
      if (form.telegram.enabled && !form.telegram.chat_ids.length) {
        ui.chatsField.setError('Добавьте хотя бы один chat id.');
        ok = false;
      }
      return ok;
    }

    function showServerError(message) {
      bar.setError(message);
      if (/deals_interval/.test(message)) ui.dealsField.setError('Проверьте значение.');
      else if (/messages_interval/.test(message)) ui.messagesField.setError('Проверьте значение.');
      else if (/bot_token/.test(message)) ui.botTokenField.setError('Проверьте токен бота.');
      else if (/chat_ids/.test(message)) ui.chatsField.setError('Проверьте список chat id.');
    }

    async function save() {
      if (!validate()) {
        bar.setError('Исправьте отмеченные поля.');
        content.querySelector('.has-error')?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        return;
      }
      const cfg = toConfig(form, base);
      try {
        await saveConfig(cfg);
      } catch (exc) {
        showServerError(exc.message);
        return;
      }
      base = { ...base, ...cfg };
      original = snapshot(base);
      form = snapshot(base);
      changed();
    }

    load();

    return {
      title: 'Настройки',
      subtitle: 'Подключение к Playerok, уведомления и параметры работы',
      body,
      isDirty,
      onParams(params) {
        const focus = params.get('focus');
        if (focus) {
          pendingFocus = focus;
          applyFocus();
        }
      },
    };
  };
})();
