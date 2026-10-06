'use strict';
/* Главная: состояние бота, первые шаги, показатели за сегодня, лента событий и склад. */

(() => {
  const FEED_KINDS = {
    sale: { icon: 'bag', tone: 'accent' },
    delivered: { icon: 'box', tone: 'success' },
    confirmed: { icon: 'check', tone: 'success' },
    refund: { icon: 'refund', tone: 'warning' },
    warning: { icon: 'alert', tone: 'warning' },
    error: { icon: 'xCircle', tone: 'danger' },
    message: { icon: 'chat', tone: 'neutral' },
    relist: { icon: 'relist', tone: 'accent' },
    info: { icon: 'info', tone: 'muted' },
  };
  const FEED_FILTERS = {
    all: () => true,
    sales: (item) => ['sale', 'delivered', 'confirmed'].includes(item.kind),
    problems: (item) => ['warning', 'error', 'refund'].includes(item.kind),
    messages: (item) => item.kind === 'message',
  };
  const FEED_SHOWN = 80;
  const GOODS = ['товар', 'товара', 'товаров'];

  /** Плитка события ленты: иконка по типу, заголовок, текст, время, ссылка. */
  function feedItem(item) {
    const kind = FEED_KINDS[item.kind] || FEED_KINDS.info;
    const link = Boolean(item.url);
    return h(
      link ? 'button' : 'div',
      {
        class: ['feed-item', link && 'is-link'],
        type: link ? 'button' : null,
        title: link ? 'Открыть на Playerok' : null,
        onClick: link ? () => openUrl(item.url) : null,
      },
      h('span', { class: ['feed-item__icon', `tone--${kind.tone}`] }, icon(kind.icon, 17)),
      h(
        'span',
        { class: 'feed-item__body' },
        h('span', { class: 'feed-item__title' }, item.title || ''),
        item.text && h('span', { class: 'feed-item__text' }, item.text),
      ),
      h(
        'span',
        { class: 'feed-item__meta' },
        h('time', { datetime: item.ts, title: fmtDateTime(item.ts) }, fmtRelative(item.ts)),
        link && icon('external', 14, 'feed-item__ext'),
      ),
    );
  }

  function metaItem(iconName, text, cls) {
    return h('span', { class: ['hero__meta-item', cls] }, icon(iconName, 15), h('span', {}, text));
  }

  Pages.home = (ctx) => {
    const banner = h('div', { class: 'home__banner' });
    const hero = h('section', { class: 'card hero', 'aria-label': 'Состояние бота' });
    const setup = h('div');
    const kpis = h('section', { class: 'kpis', 'aria-label': 'Показатели за сегодня' });
    const feedList = h('div', { class: 'feed', role: 'list' });
    const live = h('span', { class: 'live' });
    let feedFilter = Prefs.get('feedFilter', 'all');
    if (!FEED_FILTERS[feedFilter]) feedFilter = 'all';
    const filter = segmented(
      [
        { value: 'all', label: 'Все' },
        { value: 'sales', label: 'Продажи' },
        { value: 'problems', label: 'Проблемы' },
        { value: 'messages', label: 'Сообщения' },
      ],
      feedFilter,
      (value) => {
        feedFilter = value;
        Prefs.set('feedFilter', value);
        renderFeed();
      },
      { cls: 'segmented--sm' },
    );
    const stockBody = h('div', { class: 'stock-widget' });

    const feedCard = h(
      'section',
      { class: 'card panel feed-card' },
      h('header', { class: 'panel__head' }, h('div', { class: 'panel__title' }, h('h2', {}, 'Лента событий'), live), filter.el),
      feedList,
    );
    const stockCard = h(
      'section',
      { class: 'card panel' },
      h(
        'header',
        { class: 'panel__head' },
        h('div', { class: 'panel__title' }, h('h2', {}, 'Склад')),
        h('a', { class: 'link', href: '#/delivery' }, 'Правила', icon('chevronRight', 14)),
      ),
      stockBody,
    );

    const body = h('div', { class: 'home' }, banner, hero, setup, kpis, h('div', { class: 'home__grid' }, feedCard, stockCard));

    /* ---- Секции перерисовываются, только когда их данные изменились ---- */
    const memo = {};
    function section(key, data, render) {
      const json = JSON.stringify(data);
      if (memo[key] === json) return;
      memo[key] = json;
      render(data);
    }

    function renderBanner({ configError }) {
      replace(
        banner,
        configError &&
          callout('danger', 'В файле настроек ошибка — бот не запустится', configError, [
            btn('Открыть папку', { kind: 'ghost', size: 'sm', icon: 'folder', onClick: () => openPath('workdir') }),
            btn('Настройки', { kind: 'secondary', size: 'sm', onClick: () => navigate('settings') }),
          ]),
      );
    }

    function renderHero({ bot, account, features, busy, hasToken }) {
      const status = bot.status;
      const st = botState(status);
      const pending = busy || status === 'starting' || status === 'stopping';
      const active = status === 'running' || status === 'starting';

      let lead;
      if (status === 'running') lead = 'Следит за продажами, выдаёт товар и присылает уведомления.';
      else if (status === 'starting') lead = 'Входим в аккаунт Playerok и загружаем последние продажи…';
      else if (status === 'stopping') lead = 'Дожидаемся текущих задач — это займёт пару секунд.';
      else if (status === 'error') lead = 'Исправьте причину и запустите бота снова.';
      else if (!hasToken) lead = 'Укажите токен Playerok в настройках — после этого бота можно запускать.';
      else if (account) lead = 'Нажмите «Запустить», чтобы бот снова следил за продажами.';
      else lead = 'Запустите бота — он войдёт в аккаунт Playerok и начнёт следить за продажами.';

      const meta = [];
      if (status === 'running' && bot.uptime_sec !== null) meta.push(metaItem('clock', `Аптайм ${fmtDuration(bot.uptime_sec)}`));
      if (account) {
        meta.push(metaItem('ring', account.username, 'hero__meta-item--strong'));
        meta.push(metaItem('diamond', `Баланс ${fmtMoney(account.balance)}`));
      }

      let actions;
      if (pending) {
        const wait = btn(status === 'stopping' ? 'Останавливаем…' : 'Запускаем…', { kind: active ? 'secondary' : 'primary', size: 'lg', disabled: true });
        wait.prepend(h('span', { class: 'spinner', 'aria-hidden': 'true' }));
        actions = [wait];
      } else if (active) {
        actions = [
          btn('Остановить', { kind: 'secondary', size: 'lg', icon: 'stop', onClick: () => botAction('stop') }),
          btn('Перезапустить', { kind: 'ghost', size: 'lg', icon: 'restart', onClick: () => botAction('restart') }),
        ];
      } else if (!hasToken) {
        actions = [btn('Указать токен', { kind: 'primary', size: 'lg', icon: 'key', onClick: () => navigate('settings?focus=token'), cls: 'hero__start' })];
      } else {
        actions = [btn('Запустить бота', { kind: 'primary', size: 'lg', icon: 'play', onClick: () => botAction('start'), cls: 'hero__start' })];
        if (status === 'error') actions.push(btn('Журнал', { kind: 'ghost', size: 'lg', icon: 'logs', onClick: () => navigate('logs') }));
      }

      const feats = [
        { on: features.delivery, label: 'Автовыдача', href: '#/delivery' },
        { on: features.telegram, label: 'Telegram', href: '#/settings?focus=telegram' },
        { on: features.relist_after_sale, label: 'Перевыставление после продажи', href: '#/relist' },
        { on: features.relist_expired, label: 'Истёкшие лоты', href: '#/relist' },
      ];

      hero.className = `card hero hero--${status}`;
      replace(
        hero,
        h(
          'div',
          { class: 'hero__main' },
          h('div', { class: ['hero__status', `tone--${st.tone}`] }, h('span', { class: ['dot', `dot--${st.tone}`, status === 'running' && 'dot--pulse'] }), st.short),
          h('h2', { class: 'hero__title' }, st.title),
          h('p', { class: 'hero__lead' }, lead),
          meta.length > 0 && h('div', { class: 'hero__meta' }, meta),
          status === 'error' && bot.error && h('div', { class: 'hero__error', role: 'alert' }, icon('alert', 16), h('span', {}, bot.error)),
          h('div', { class: 'hero__actions' }, actions),
        ),
        h('div', { class: 'hero__art' }, logoArt(status === 'stopping' ? 'stopped' : status, 58)),
        h(
          'div',
          { class: 'hero__features' },
          h('span', { class: 'hero__features-label' }, 'Режимы'),
          feats.map((f) =>
            h(
              'a',
              { class: ['feat', f.on && 'is-on'], href: f.href, title: f.on ? 'Включено' : 'Выключено' },
              h('span', { class: 'feat__dot', 'aria-hidden': 'true' }),
              f.label,
              h('span', { class: 'visually-hidden' }, f.on ? ' — включено' : ' — выключено'),
            ),
          ),
        ),
      );
    }

    function renderSetup({ setup: s, status }) {
      if (s.token && s.rules) {
        replace(setup);
        return;
      }
      const steps = [
        {
          done: s.token,
          title: 'Токен Playerok',
          text: 'Cookie token из браузера — по нему бот входит в ваш аккаунт.',
          action: 'Указать токен',
          href: '#/settings?focus=token',
        },
        {
          done: s.telegram,
          optional: true,
          title: 'Telegram',
          text: 'Уведомления о продажах, выдаче и сообщениях покупателей.',
          action: 'Подключить',
          href: '#/settings?focus=telegram',
        },
        {
          done: s.rules,
          title: 'Правило автовыдачи',
          text: 'Что отправить покупателю после оплаты лота.',
          action: 'Создать правило',
          href: '#/delivery?new=1',
        },
        {
          done: status === 'running',
          title: 'Запуск',
          text: 'Бот следит за продажами и выдаёт товар сам.',
          action: 'Запустить',
          run: true,
        },
      ];
      const next = steps.findIndex((step) => !step.done);
      const doneCount = steps.filter((step) => step.done).length;
      replace(
        setup,
        h(
          'section',
          { class: 'card onboarding', 'aria-label': 'Первые шаги' },
          h(
            'header',
            { class: 'onboarding__head' },
            h('div', {}, h('h2', { class: 'onboarding__title' }, 'Первые шаги'), h('p', { class: 'onboarding__sub' }, 'Четыре шага — и бот начнёт выдавать товар без вашего участия.')),
            h(
              'div',
              { class: 'progress', title: `Готово ${doneCount} из ${steps.length}` },
              h('span', { class: 'progress__text' }, `${doneCount} из ${steps.length}`),
              h('span', { class: 'progress__bar' }, h('span', { class: 'progress__fill', style: { width: `${(doneCount / steps.length) * 100}%` } })),
            ),
          ),
          h(
            'ol',
            { class: 'steps' },
            steps.map((step, index) => {
              const isNext = index === next;
              let action;
              if (step.done) action = h('span', { class: 'step__done' }, icon('check', 15), 'Готово');
              else if (step.run) action = btn(step.action, { kind: isNext ? 'primary' : 'secondary', size: 'sm', icon: 'play', disabled: !s.token || App.botBusy, onClick: () => botAction('start') });
              else action = h('a', { class: ['btn', 'btn--sm', isNext ? 'btn--primary' : 'btn--secondary'], href: step.href }, h('span', { class: 'btn__label' }, step.action));
              return h(
                'li',
                { class: ['step', step.done && 'is-done', isNext && 'is-next'] },
                step.done ? gtile('check', 'success', 32) : ntile(index + 1, isNext ? 'blue' : 'white', 32),
                h('div', { class: 'step__title' }, step.title, step.optional && h('span', { class: 'step__optional' }, 'необязательно')),
                h('p', { class: 'step__text' }, step.text),
                h('div', { class: 'step__action' }, action),
              );
            }),
          ),
        ),
      );
    }

    function kpi({ glyphName, tone = 'white', label, value, caption, href, alert }) {
      return h(
        href ? 'a' : 'div',
        { class: ['card', 'kpi', href && 'kpi--link', alert && 'kpi--alert'], href },
        h('div', { class: 'kpi__head' }, gtile(glyphName, tone, 34), h('span', { class: 'kpi__label' }, label)),
        h('div', { class: 'kpi__value', title: value }, value),
        h('div', { class: 'kpi__caption' }, caption),
      );
    }

    function renderKpis(stats) {
      const attention = stats.attention || 0;
      replace(
        kpis,
        kpi({
          glyphName: 'ring',
          label: 'Продажи сегодня',
          value: fmtInt(stats.sales_today),
          caption: stats.sales_today ? `${plural(stats.sales_today, ['оплата', 'оплаты', 'оплат'])} с начала дня` : 'пока ни одной оплаты',
          href: '#/sales',
        }),
        kpi({ glyphName: 'diamond', label: 'Выручка сегодня', value: fmtMoney(stats.revenue_today), caption: 'по оплаченным сделкам' }),
        kpi({
          glyphName: 'rect',
          label: 'Выдано сегодня',
          value: fmtInt(stats.delivered_today),
          caption: stats.delivered_today ? 'автоматически, без вас' : 'выдач ещё не было',
          href: '#/sales?filter=delivered',
        }),
        kpi({
          glyphName: attention ? 'bang' : 'check',
          tone: attention ? 'warning' : 'white',
          label: 'Требуют внимания',
          value: fmtInt(attention),
          caption: attention ? 'ошибки или нет товара' : 'всё в порядке',
          href: '#/sales?filter=attention',
          alert: attention > 0,
        }),
      );
    }

    function renderStock({ low, total }) {
      if (!low.length) {
        replace(
          stockBody,
          h(
            'div',
            { class: 'stock-ok' },
            gtile(total ? 'check' : 'rect', total ? 'success' : 'muted', 38),
            h(
              'div',
              {},
              h('div', { class: 'stock-ok__title' }, total ? 'Запасов хватает' : 'Склад пуст'),
              h('div', { class: 'stock-ok__text' }, total ? `Всего на складе ${countText(total, GOODS)}.` : 'Добавьте товары в правилах автовыдачи.'),
            ),
          ),
        );
        return;
      }
      replace(
        stockBody,
        h(
          'div',
          { class: 'stock-list' },
          low.map((row) =>
            h(
              'a',
              { class: 'stock-row', href: `#/delivery?stock=${row.rule_id}`, title: 'Открыть склад правила' },
              h('span', { class: ['stock-row__mark', row.count === 0 ? 'tone--danger' : 'tone--warning'] }, icon(row.count === 0 ? 'xCircle' : 'alert', 16)),
              h(
                'span',
                { class: 'stock-row__main' },
                h('span', { class: 'stock-row__name' }, `«${row.match}»`),
                h('span', { class: 'stock-row__hint' }, row.count === 0 ? 'Товар закончился' : `Осталось мало · порог ${row.threshold}`),
              ),
              chip(row.count === 0 ? 'Пусто' : `${fmtInt(row.count)} шт.`, row.count === 0 ? 'danger' : 'warning', { dot: false }),
              icon('chevronRight', 16, 'stock-row__chevron'),
            ),
          ),
        ),
        h('div', { class: 'stock-widget__foot' }, `Всего на складе ${countText(total, GOODS)}`),
      );
    }

    function renderLive(status) {
      const running = status === 'running';
      replace(live, h('span', { class: ['dot', running ? 'dot--success dot--pulse' : 'dot--muted'], 'aria-hidden': 'true' }), running ? 'онлайн' : 'бот не запущен');
    }

    function renderFeed() {
      const items = App.feed.filter(FEED_FILTERS[feedFilter]).slice(-FEED_SHOWN).reverse();
      if (!items.length) {
        const empty = App.feed.length
          ? emptyState({ title: 'Нет таких событий', text: 'Выберите другой фильтр.', compact: true })
          : emptyState({
              title: 'Событий пока нет',
              text: 'Здесь появятся продажи, выдачи и сообщения покупателей, как только бот начнёт работу.',
              compact: true,
            });
        replace(feedList, empty);
        return;
      }
      replace(feedList, items.map(feedItem));
    }

    function update(ov) {
      if (!ov) return;
      section('banner', { configError: ov.config_error }, renderBanner);
      const minute = ov.bot.uptime_sec === null ? null : Math.floor(ov.bot.uptime_sec / 60);
      section('hero', { bot: { ...ov.bot, uptime_sec: minute === null ? null : minute * 60, started_at: null }, account: ov.account, features: ov.features, busy: App.botBusy, hasToken: ov.setup.token }, renderHero);
      section('setup', { setup: ov.setup, status: ov.bot.status, busy: App.botBusy }, renderSetup);
      section('kpis', ov.stats, renderKpis);
      section('stock', { low: ov.low_stock || [], total: ov.stats.stock_total }, renderStock);
      section('live', ov.bot.status, renderLive);
    }

    ctx.on('overview', update);
    ctx.on('bot-busy', () => update(App.overview));
    ctx.on('feed', renderFeed);
    ctx.every(30000, renderFeed);
    update(App.overview);
    renderFeed();

    return { title: 'Главная', subtitle: 'Состояние бота и продажи за сегодня', body };
  };
})();
