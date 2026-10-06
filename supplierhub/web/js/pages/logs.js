'use strict';
/* Журнал: технические записи бота и приложения с фильтром, поиском и автопрокруткой. */

(() => {
  const MAX_LINES = 2000;
  const THRESHOLDS = { all: 0, info: 20, warning: 30, error: 40 };
  const LEVEL_NUM = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
  const LEVEL_SHORT = { DEBUG: 'DBG', INFO: 'INFO', WARNING: 'WARN', ERROR: 'ERR', CRITICAL: 'CRIT' };

  Pages.logs = (ctx) => {
    let entries = [];
    let lastId = 0;
    let loaded = false;
    let level = Prefs.get('logLevel', 'all');
    if (!(level in THRESHOLDS)) level = 'all';
    let query = '';
    let follow = true;

    const view = h('div', { class: 'logview', role: 'log', 'aria-label': 'Записи журнала', tabindex: '0' });
    const counter = h('span', { class: 'logbar__count' });
    const levels = segmented(
      [
        { value: 'all', label: 'Все' },
        { value: 'info', label: 'Инфо' },
        { value: 'warning', label: 'Предупреждения' },
        { value: 'error', label: 'Ошибки' },
      ],
      level,
      (value) => {
        level = value;
        Prefs.set('logLevel', value);
        renderAll();
      },
    );
    const search = searchInput({
      placeholder: 'Поиск по журналу',
      onInput: debounce((value) => {
        query = value.trim().toLowerCase();
        renderAll();
      }, 150),
    });
    const followToggle = toggle({
      checked: true,
      id: 'log-follow',
      label: 'Автопрокрутка',
      onChange: (value) => {
        follow = value;
        if (value) toBottom();
      },
    });

    function visible(entry) {
      if ((LEVEL_NUM[entry.level] ?? 20) < THRESHOLDS[level]) return false;
      return !query || `${entry.name} ${entry.message}`.toLowerCase().includes(query);
    }

    /** Подсветить совпадения поиска (через <mark>, без innerHTML). */
    function highlight(text) {
      const value = String(text || '');
      if (!query) return value;
      const lower = value.toLowerCase();
      const parts = [];
      let from = 0;
      let at = lower.indexOf(query);
      while (at !== -1) {
        if (at > from) parts.push(value.slice(from, at));
        parts.push(h('mark', {}, value.slice(at, at + query.length)));
        from = at + query.length;
        at = lower.indexOf(query, from);
      }
      parts.push(value.slice(from));
      return parts;
    }

    function line(entry) {
      const date = parseTime(entry.ts);
      const lvl = String(entry.level || 'INFO').toUpperCase();
      return h(
        'div',
        { class: ['logline', `logline--${lvl.toLowerCase()}`] },
        h('span', { class: 'logline__time', title: date ? fmtDateTime(entry.ts) : '' }, date ? fmtClock(date, true) : ''),
        h('span', { class: 'logline__level' }, LEVEL_SHORT[lvl] || lvl),
        h('span', { class: 'logline__name', title: entry.name }, entry.name),
        h('span', { class: 'logline__msg' }, highlight(entry.message)),
      );
    }

    function toBottom() {
      view.scrollTop = view.scrollHeight;
    }

    function updateCounter(shown) {
      counter.textContent = shown === entries.length ? countText(entries.length, ['запись', 'записи', 'записей']) : `${fmtInt(shown)} из ${fmtInt(entries.length)}`;
    }

    function renderAll() {
      const shown = entries.filter(visible);
      if (!shown.length) {
        replace(
          view,
          emptyState({
            title: entries.length ? 'Ничего не найдено' : loaded ? 'Записей пока нет' : 'Загрузка…',
            text: entries.length ? 'Измените уровень или строку поиска.' : 'Здесь появятся технические сообщения бота.',
            compact: true,
          }),
        );
      } else {
        replace(view, shown.map(line));
      }
      updateCounter(shown.length);
      if (follow) toBottom();
    }

    function append(fresh) {
      entries.push(...fresh);
      if (entries.length > MAX_LINES) {
        entries = entries.slice(-MAX_LINES);
        renderAll();
        return;
      }
      const shown = fresh.filter(visible);
      if (shown.length) {
        if (!view.querySelector('.logline')) view.replaceChildren();
        view.append(...shown.map(line));
      }
      updateCounter(entries.filter(visible).length);
      if (follow) toBottom();
    }

    view.addEventListener('scroll', () => {
      const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 32;
      if (follow !== atBottom) {
        follow = atBottom;
        followToggle.input.checked = atBottom;
      }
    });

    async function load() {
      const result = await api('get_logs', lastId);
      if (!ctx.alive) return;
      if (result.last_id < lastId) {
        entries = [];
        lastId = 0;
        renderAll();
        return;
      }
      const fresh = (result.items || []).filter((item) => item.id > lastId);
      lastId = result.last_id;
      if (!loaded) {
        loaded = true;
        entries = fresh.slice(-MAX_LINES);
        renderAll();
      } else if (fresh.length) {
        append(fresh);
      }
    }

    function copyVisible() {
      const text = entries
        .filter(visible)
        .map((entry) => {
          const date = parseTime(entry.ts);
          return `${date ? fmtClock(date, true) : ''} ${entry.level} ${entry.name}: ${entry.message}`;
        })
        .join('\n');
      if (text) copyText(text, 'Записи скопированы');
    }

    renderAll();
    ctx.poll('logs', load, 1500);

    const body = h(
      'div',
      { class: 'logs' },
      h(
        'div',
        { class: 'toolbar logbar' },
        levels.el,
        search,
        h('div', { class: 'toolbar__spacer' }),
        counter,
        h('label', { class: 'inline-toggle', for: 'log-follow' }, 'Автопрокрутка'),
        followToggle.el,
      ),
      h('div', { class: 'card logcard' }, view),
    );

    return {
      title: 'Журнал',
      subtitle: 'Технические события бота и приложения',
      body,
      fill: true,
      actions: [
        btn('Копировать', { kind: 'ghost', icon: 'copy', onClick: copyVisible }),
        btn('Открыть файл журнала', { kind: 'secondary', icon: 'file', onClick: () => openPath('log') }),
      ],
    };
  };
})();
