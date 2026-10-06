'use strict';
/* Перевыставление: вернуть лоты в продажу после продажи и по истечении срока. */

(() => {
  function snapshot(relist) {
    return {
      after_sale: Boolean(relist.after_sale),
      expired: Boolean(relist.expired),
      interval_minutes: String(relist.interval_minutes ?? 30),
      match: [...(relist.match || [])],
    };
  }

  Pages.relist = (ctx) => {
    let config = null;
    let original = null;
    let form = null;
    const content = h('div', { class: 'relist__content' }, h('div', { class: 'card table-skeleton' }, h('div', { class: 'spinner spinner--lg' })));
    const bar = saveBar({ onSave: save, onReset: reset });
    const body = h('div', { class: 'relist' }, content, bar.el);

    function isDirty() {
      return Boolean(form) && !sameData(form, original);
    }

    function changed() {
      bar.show(isDirty());
    }

    function build() {
      const intervalInput = textInput({
        type: 'number',
        min: 1,
        step: 1,
        value: form.interval_minutes,
        id: 'relist-interval',
        cls: 'input--narrow',
        disabled: !form.expired,
        onInput: (value) => {
          form.interval_minutes = value;
          intervalField.setError('');
          changed();
        },
      });
      const intervalField = field({
        label: 'Проверять каждые, минут',
        id: 'relist-interval',
        control: intervalInput,
        cls: 'field--inline',
      });
      const afterSale = toggle({
        checked: form.after_sale,
        id: 'relist-after-sale',
        label: 'Сразу после продажи',
        onChange: (value) => {
          form.after_sale = value;
          changed();
        },
      });
      const expired = toggle({
        checked: form.expired,
        id: 'relist-expired',
        label: 'Истёкшие лоты',
        onChange: (value) => {
          form.expired = value;
          intervalInput.disabled = !value;
          changed();
        },
      });
      const match = chipsInput({
        values: form.match,
        placeholder: 'Например: Ключ Steam — и Enter',
        id: 'relist-match',
        onChange: (values) => {
          form.match = values;
          changed();
        },
      });

      const modes = h(
        'section',
        { class: 'card panel' },
        h('header', { class: 'panel__head' }, h('div', { class: 'panel__title' }, gtile('ring', 'white', 32), h('h2', {}, 'Когда перевыставлять'))),
        h(
          'div',
          { class: 'settings-list' },
          settingRow({
            title: 'Сразу после продажи',
            desc: 'На бесплатном тарифе Playerok лот снимается с витрины после продажи. Бот выставит его снова — если на складе правила хватает товара.',
            control: afterSale.el,
            id: 'relist-after-sale',
          }),
          settingRow({
            title: 'Истёкшие лоты',
            desc: 'Периодически ищет лоты с истёкшим сроком размещения и выставляет их заново.',
            control: expired.el,
            id: 'relist-expired',
            extra: h('div', { class: 'setting__extra' }, intervalField.el),
          }),
        ),
      );

      const filter = h(
        'section',
        { class: 'card panel' },
        h('header', { class: 'panel__head' }, h('div', { class: 'panel__title' }, gtile('diamond', 'white', 32), h('h2', {}, 'Какие лоты'))),
        h(
          'div',
          { class: 'panel__body' },
          field({
            label: 'Только лоты, в названии которых есть',
            id: 'relist-match',
            control: match.el,
            hint: 'Добавьте части названий — Enter или запятая. Если список пуст, перевыставляются все лоты.',
          }).el,
          h(
            'div',
            { class: 'scope' },
            icon('info', 16),
            h('span', {}, form.match.length ? 'Бот трогает только лоты из списка.' : 'Сейчас список пуст — бот перевыставляет все ваши лоты.'),
          ),
        ),
      );

      const steps = [
        ['Лот продан', 'Бесплатный тариф снимает его с витрины, а сделка уходит в «Продажи».'],
        ['Проверка склада', 'Если у правила автовыдачи хватает товара — лот выставляется заново.'],
        ['Товара нет', 'Лот остаётся снятым, а вам приходит уведомление «не перевыставлен».'],
      ];
      const help = h(
        'aside',
        { class: 'card panel help' },
        h('header', { class: 'panel__head' }, h('div', { class: 'panel__title' }, h('h2', {}, 'Как это работает'))),
        h(
          'ol',
          { class: 'help__steps' },
          steps.map(([title, text], index) => h('li', { class: 'help__step' }, ntile(index + 1, index === 1 ? 'blue' : 'white', 28), h('div', {}, h('div', { class: 'help__title' }, title), h('p', { class: 'help__text' }, text)))),
        ),
        h('p', { class: 'help__foot' }, 'Изменения вступают в силу после перезапуска бота.'),
      );

      replace(content, h('div', { class: 'split' }, h('div', { class: 'split__main' }, modes, filter), help));
      return { intervalField, match };
    }

    let controls = null;

    async function load() {
      try {
        config = await api('get_config');
      } catch (exc) {
        if (ctx.alive) replace(content, h('div', { class: 'card card--pad' }, callout('danger', 'Не удалось загрузить настройки', exc.message)));
        return;
      }
      if (!ctx.alive) return;
      original = snapshot(config.relist || {});
      form = snapshot(config.relist || {});
      controls = build();
      changed();
    }

    function reset() {
      form = snapshot(original);
      controls = build();
      changed();
    }

    async function save() {
      if (!controls.match.commit()) return;
      const minutes = Number(form.interval_minutes);
      if (!Number.isFinite(minutes) || minutes < 1) {
        controls.intervalField.setError('Не меньше 1 минуты.');
        bar.setError('Проверьте интервал проверки.');
        return;
      }
      const cfg = {
        ...config,
        relist: { after_sale: form.after_sale, expired: form.expired, interval_minutes: minutes, match: form.match },
      };
      try {
        await saveConfig(cfg);
      } catch (exc) {
        bar.setError(exc.message);
        return;
      }
      config = cfg;
      original = snapshot(cfg.relist);
      form = snapshot(cfg.relist);
      controls = build();
      changed();
    }

    load();

    return {
      title: 'Перевыставление',
      subtitle: 'Бот возвращает проданные и истёкшие лоты на витрину',
      body,
      isDirty,
    };
  };
})();
