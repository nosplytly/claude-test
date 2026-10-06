'use strict';
/* Продажи: живые сделки и история выдач, фильтры, поиск и карточка сделки. */

(() => {
  const FILTERS = {
    all: () => true,
    attention: (sale) => Boolean(sale.delivery) && ['failed', 'no_stock'].includes(sale.delivery.status),
    delivered: (sale) => Boolean(sale.delivery) && sale.delivery.status === 'delivered',
    no_rule: (sale) => Boolean(sale.delivery) && sale.delivery.status === 'no_rule',
  };
  const COLUMNS = ['Время', 'Лот', 'Покупатель', 'Сумма', 'Сделка', 'Выдача'];

  function shortId(id) {
    const text = String(id || '');
    return text.length > 10 ? `${text.slice(0, 8)}…` : text;
  }

  function dealChip(sale) {
    return sale.status ? chip(sale.status_label || sale.status, DEAL_TONES[sale.status] || 'muted') : h('span', { class: 'dash' }, '—');
  }

  function deliveryChip(delivery) {
    return delivery ? chip(delivery.label || delivery.status, DELIVERY_TONES[delivery.status] || 'muted') : h('span', { class: 'dash' }, '—');
  }

  function kvRow(label, value, extra) {
    return h('div', { class: 'kv__row' }, h('dt', { class: 'kv__key' }, label), h('dd', { class: 'kv__value' }, value, extra));
  }

  /** Боковая панель со всеми подробностями сделки. */
  function openSale(sale) {
    const delivery = sale.delivery;
    const products = (delivery && delivery.products) || [];
    const status = delivery ? delivery.status : null;

    let note = null;
    if (status === 'failed') {
      note = callout('danger', 'Товар не выдан', delivery.error || 'Не удалось отправить сообщение покупателю.');
    } else if (status === 'no_stock') {
      note = callout('warning', 'Не хватило товара на складе', 'Пополните склад правила — бот выдаст товар при следующей проверке.', [
        btn('К автовыдаче', { kind: 'secondary', size: 'sm', onClick: () => navigate('delivery') }),
      ]);
    } else if (status === 'no_rule') {
      note = callout('info', 'Для лота нет правила автовыдачи', 'Выдайте товар вручную в чате или создайте правило для таких лотов.', [
        btn('Создать правило', {
          kind: 'secondary',
          size: 'sm',
          icon: 'plus',
          onClick: () => navigate(`delivery?new=1&match=${encodeURIComponent(sale.item || '')}`),
        }),
      ]);
    } else if (status === 'reserved') {
      note = callout('info', 'Товар отправляется', 'Бот уже взял товар со склада и отправляет его покупателю в чат.');
    } else if (status === 'skipped') {
      note = callout('info', 'Сделка пропущена', 'Её оплатили до первого запуска бота. Если товар ещё не выдан — выдайте вручную.');
    }
    const lastError = delivery && delivery.error && status !== 'failed' ? callout('warning', 'Последняя ошибка выдачи', delivery.error) : null;

    const productsBlock = products.length
      ? h(
          'section',
          { class: 'sheet-section' },
          h(
            'div',
            { class: 'sheet-section__head' },
            h('h3', { class: 'sheet-section__title' }, status === 'failed' ? 'Товар для ручной выдачи' : 'Выданный товар'),
            products.length > 1 && btn('Копировать всё', { kind: 'ghost', size: 'sm', icon: 'copy', onClick: () => copyText(products.join('\n')) }),
          ),
          h(
            'ul',
            { class: 'products' },
            products.map((value, index) =>
              h(
                'li',
                { class: 'product' },
                h('span', { class: 'product__index' }, String(index + 1)),
                h('pre', { class: 'product__text' }, value),
                iconBtn('copy', 'Копировать', () => copyText(value), { cls: 'product__copy' }),
              ),
            ),
          ),
        )
      : null;

    const body = h(
      'div',
      { class: 'deal' },
      h(
        'div',
        { class: 'deal__hero' },
        h('div', { class: 'deal__item' }, sale.item || 'Лот без названия'),
        h('div', { class: 'deal__price' }, fmtMoney(sale.price)),
        h('div', { class: 'deal__chips' }, dealChip(sale), deliveryChip(delivery)),
      ),
      note,
      lastError,
      h(
        'dl',
        { class: 'kv' },
        kvRow('Покупатель', sale.buyer || '—'),
        kvRow('Создана', fmtDateTime(sale.created_at)),
        delivery && delivery.delivered_at && kvRow('Выдано', fmtDateTime(delivery.delivered_at)),
        kvRow('Номер сделки', h('code', { class: 'mono' }, String(sale.deal_id)), iconBtn('copy', 'Копировать номер', () => copyText(String(sale.deal_id)), { cls: 'kv__copy', size: 14 })),
      ),
      productsBlock,
    );

    openDrawer({
      title: 'Сделка',
      subtitle: sale.created_at ? fmtDateTime(sale.created_at) : `#${shortId(sale.deal_id)}`,
      lead: gtile('ring', 'white', 36),
      body,
      footer: [
        btn('Открыть чат', { kind: 'secondary', icon: 'chat', disabled: !sale.chat_url, onClick: () => openUrl(sale.chat_url) }),
        btn('Открыть сделку', { kind: 'primary', icon: 'external', disabled: !sale.url, onClick: () => openUrl(sale.url) }),
      ],
    });
  }

  Pages.sales = (ctx) => {
    let data = null;
    let filter = 'all';
    let query = '';

    const chips = segmented(options([]), filter, (value) => {
      filter = value;
      render();
    }, { cls: 'segmented--chips' });
    const search = searchInput({
      placeholder: 'Лот, покупатель или № сделки',
      onInput: debounce((value) => {
        query = value.trim().toLowerCase();
        render();
      }, 120),
    });
    const rows = h('div', { class: 'table__body', role: 'rowgroup' });
    const table = h(
      'div',
      { class: 'card table sales-table', role: 'table', 'aria-label': 'Продажи' },
      h('div', { class: 'trow thead', role: 'row' }, COLUMNS.map((title, index) => h('span', { class: `c${index}`, role: 'columnheader' }, title))),
      rows,
    );
    const content = h('div', { class: 'sales__content' }, h('div', { class: 'card table-skeleton' }, h('div', { class: 'spinner spinner--lg' })));
    const body = h('div', { class: 'sales' }, h('div', { class: 'toolbar' }, chips.el, h('div', { class: 'toolbar__spacer' }), search), content);

    function options(items) {
      const count = (key) => items.filter(FILTERS[key]).length;
      return [
        { value: 'all', label: 'Все', count: items.length },
        { value: 'attention', label: 'Требуют внимания', count: count('attention'), tone: count('attention') ? 'warning' : null },
        { value: 'delivered', label: 'Выдано', count: count('delivered') },
        { value: 'no_rule', label: 'Без автовыдачи', count: count('no_rule') },
      ];
    }

    function matches(sale) {
      if (!query) return true;
      return [sale.item, sale.buyer, sale.deal_id].some((value) => String(value || '').toLowerCase().includes(query));
    }

    function row(sale) {
      return h(
        'div',
        {
          class: 'trow',
          role: 'row',
          tabindex: '0',
          onClick: () => openSale(sale),
          onKeydown: (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              openSale(sale);
            }
          },
        },
        h('span', { class: 'c0 cell-time', role: 'cell', title: fmtDateTime(sale.created_at) }, fmtShortTime(sale.created_at)),
        h(
          'span',
          { class: 'c1 cell-item', role: 'cell' },
          h('span', { class: 'cell-item__name', title: sale.item || '' }, sale.item || 'Лот без названия'),
          h('span', { class: 'cell-item__sub' }, h('span', { class: 'cell-item__buyer' }, sale.buyer || '—'), h('span', { class: 'mono' }, `#${shortId(sale.deal_id)}`)),
        ),
        h('span', { class: 'c2 cell-buyer', role: 'cell', title: sale.buyer || '' }, sale.buyer || '—'),
        h('span', { class: 'c3 cell-price', role: 'cell' }, fmtMoney(sale.price)),
        h('span', { class: 'c4', role: 'cell' }, dealChip(sale)),
        h('span', { class: 'c5', role: 'cell' }, deliveryChip(sale.delivery)),
      );
    }

    function render() {
      if (!data) return;
      const items = data.items || [];
      chips.setOptions(options(items));
      ctx.setSubtitle(data.live ? 'Сделки в реальном времени и история автовыдачи' : 'История из state.json — запустите бота, чтобы видеть новые сделки');

      if (!items.length) {
        const running = botStatus() === 'running';
        replace(
          content,
          h(
            'div',
            { class: 'card' },
            emptyState({
              title: 'Продаж пока нет',
              text: running ? 'Как только покупатель оплатит лот, сделка появится здесь.' : 'Запустите бота — он покажет последние продажи и будет следить за новыми.',
              action: !running && btn('Запустить бота', { kind: 'primary', icon: 'play', onClick: () => botAction('start') }),
            }),
          ),
        );
        return;
      }
      const shown = items.filter(FILTERS[filter]).filter(matches);
      if (!shown.length) {
        replace(rows, emptyState({
          title: 'Ничего не найдено',
          text: 'Измените фильтр или строку поиска.',
          compact: true,
          action: btn('Показать все', {
            kind: 'secondary',
            size: 'sm',
            onClick: () => {
              filter = 'all';
              chips.set('all');
              query = '';
              search.querySelector('input').value = '';
              render();
            },
          }),
        }));
      } else {
        replace(rows, shown.map(row));
      }
      if (content.firstChild !== table) replace(content, table);
    }

    async function load() {
      try {
        const result = await api('get_sales');
        if (!ctx.alive || sameData(result, data)) return;
        data = result;
        render();
      } catch (exc) {
        if (!data && ctx.alive) {
          replace(content, h('div', { class: 'card card--pad' }, callout('danger', 'Не удалось загрузить продажи', exc.message)));
        }
      }
    }

    ctx.poll('sales', load, 5000);

    return {
      title: 'Продажи',
      subtitle: 'Сделки и автовыдача',
      body,
      actions: [btn('', { kind: 'secondary', icon: 'restart', title: 'Обновить', onClick: () => Poller.kick('page:sales') })],
      onParams(params) {
        const wanted = params.get('filter');
        if (wanted && FILTERS[wanted] && wanted !== filter) {
          filter = wanted;
          chips.set(wanted);
          render();
        }
      },
    };
  };
})();
