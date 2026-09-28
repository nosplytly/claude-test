// English version of the site. The page and app.js are written in Russian; in English mode every Russian phrase
// that reaches the page (markup, text set by app.js, server errors) is swapped for its English line as it appears.
// One dictionary here instead of keys all over the code: the Russian text stays readable where it's written.
//
// Language: the visitor's own choice (the RU/EN switch) → the Telegram app's language (Mini App) → the browser's.

const RU_FAMILY = /^(ru|uk|be|kk|ky|uz|tg|tk|hy|az|ka)\b/i;
const CYR = /[А-Яа-яЁё]/;

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

export const hooks = { beforeSwitch: null };  // app.js: tell the server, so the bot speaks the same language

export async function setLang(lang) {
  try { localStorage.setItem('sh_lang', lang); } catch { /* private mode: the switch lasts for this page only */ }
  try { await hooks.beforeSwitch?.(lang); } catch { /* the site switches anyway */ }
  location.reload();
}

// ------------------------------------------------------------------ dictionary (whitespace-collapsed Russian → English)
const EN = {
  // head, header, menu
  'SupplierHub — пополнение Steam криптовалютой': 'SupplierHub — top up Steam with crypto',
  'Пополнение баланса Steam криптовалютой со скидкой. USDT, BTC, ETH, TON, SOL, LTC, TRX. Автоматически за пару минут, вход через Telegram.':
    'Top up your Steam wallet with crypto at a discount. USDT, BTC, ETH, TON, SOL, LTC, TRX. Automatic, in a couple of minutes, sign-in with Telegram.',
  'SupplierHub — пополнение Steam криптой': 'SupplierHub — top up Steam with crypto',
  'Пополняйте Steam криптовалютой со скидкой. Автоматически, за пару минут.': 'Top up Steam with crypto at a discount. Automatic, in a couple of minutes.',
  'К форме пополнения': 'Skip to the top-up form',
  'автовыдача цифровых товаров': 'instant digital top-ups',
  'SupplierHub — на главную': 'SupplierHub — home',
  'Поддержка в Telegram': 'Support on Telegram',
  'Поддержка': 'Support',
  'Войти': 'Sign in',
  'Мои заказы': 'My orders',
  'Пополнить баланс': 'Add funds',
  'История баланса': 'Balance history',
  'API для партнёров': 'API for partners',
  'Выйти': 'Sign out',
  // order form
  'Пополнение Steam': 'Steam top-up',
  'Скидка от номинала': 'Off face value',
  'Логин Steam': 'Steam login',
  'Например, gaben': 'e.g. gaben',
  'Логин для входа в Steam,': 'The name you sign in to Steam with,',
  'не никнейм': 'not your nickname',
  'Где найти?': 'Where is it?',
  'Валюта аккаунта Steam': 'Steam account currency',
  'Сумма пополнения': 'Top-up amount',
  'Чем оплатите': 'Pay with',
  'Сеть перевода': 'Network',
  'Сеть': 'Network',
  'Зачислим в Steam': 'Credited to Steam',
  'Цена со скидкой': 'Discounted price',
  'С баланса сайта': 'From site balance',
  'К оплате': 'To pay',
  'Ваша выгода': 'You save',
  'Есть промокод?': 'Have a promo code?',
  'ПРОМОКОД': 'PROMO CODE',
  'Промокод': 'Promo code',
  'Применить': 'Apply',
  'Убрать промокод': 'Remove promo code',
  'Перейти к оплате': 'Proceed to payment',
  'Продолжая, вы принимаете': 'By continuing, you accept the',
  'условия сервиса': 'terms of service',
  'Назад': 'Back',
  'Пополнение временно недоступно': 'Top-ups are temporarily unavailable',
  'Введите сумму': 'Enter an amount',
  'вся сумма спишется с баланса сайта': 'the full amount comes from your site balance',
  'выберите, чем оплатите': 'choose how to pay',
  'Войти и оплатить': 'Sign in and pay',
  'Оплатить с баланса': 'Pay from balance',
  'Только латиница, цифры и символы _ - . (3–64 символа)': 'Latin letters, digits and _ - . only (3–64 characters)',
  'Аккаунт найден — можно пополнять': 'Account found — ready to top up',
  'Укажите логин Steam': 'Enter your Steam login',
  'Не удалось рассчитать сумму. Обновите страницу и попробуйте ещё раз.': "Couldn't calculate the amount. Reload the page and try again.",
  'Выберите монету для оплаты': 'Choose a coin to pay with',
  'Проверяем логин…': 'Checking login…',
  'Создаём счёт…': 'Creating invoice…',
  'Войдите, чтобы зачислить бонус на баланс': 'Sign in to add the bonus to your balance',
  'Сервис временно недоступен': 'Service temporarily unavailable',
  'точная сумма будет в счёте': 'the exact amount will be in the invoice',
  'Приём оплаты скоро откроется': 'Payments open soon',
  'Загружаем способы оплаты…': 'Loading payment methods…',
  'Не удалось загрузить данные сервиса. Проверьте подключение и': "Couldn't load the service data. Check your connection and",
  'обновите страницу': 'reload the page',
  // balance top-up
  'Пополнение баланса': 'Balance top-up',
  'Баланс оплачивает заказы — на сайте и через API': 'Your balance pays for orders — on the site and via API',
  'Сейчас на балансе': 'Current balance',
  'Станет после оплаты': 'After payment',
  'Создать счёт': 'Create invoice',
  'Войти и пополнить': 'Sign in and top up',
  'Назад к форме': 'Back to the form',
  // payment screen
  'Заказ': 'Order',
  'Время на оплату': 'Time to pay',
  'Счёт': 'Invoice',
  'Оплата': 'Payment',
  'Пополнение': 'Top-up',
  'Зачисление': 'Crediting',
  'Готово': 'Done',
  'QR-код для оплаты': 'Payment QR code',
  'Отправьте ровно': 'Send exactly',
  'На адрес': 'To address',
  'Комментарий (memo)': 'Comment (memo)',
  'Укажите, если кошелёк позволяет — так платёж найдётся даже при неточной сумме.':
    'Add it if your wallet allows — then the payment is found even if the amount is off.',
  'Ожидаем платёж': 'Waiting for payment',
  'Обычно он появляется в сети через 10–60 секунд после отправки.': 'It usually shows up on-chain 10–60 seconds after sending.',
  'Платёж найден, ждём подтверждения сети': 'Payment found, waiting for network confirmation',
  'Пополняем Steam…': 'Topping up Steam…',
  'Заказ в очереди — выполним в ближайшие минуты.': "Order queued — we'll complete it within minutes.",
  'Можно закрыть страницу — пришлём уведомление в Telegram.': "You can close this page — we'll notify you on Telegram.",
  'Транзакция ↗': 'Transaction ↗',
  'Время на оплату вышло': 'Payment time is up',
  'Не отправляйте оплату по этому счёту. Если уже отправили — деньги зачислятся на баланс сайта.':
    "Don't pay this invoice anymore. If you already did, the money will be credited to your site balance.",
  'Отправили, а платёж не находится?': "Sent it but the payment isn't found?",
  'Отменить заказ': 'Cancel order',
  'Точно отменить? Нажмите ещё раз': 'Really cancel? Tap again',
  '⚙ Симулировать оплату (dev)': '⚙ Simulate payment (dev)',
  'Симулирован входящий платёж': 'Incoming payment simulated',
  'около минуты': 'in about a minute',
  'за несколько секунд': 'in a few seconds',
  'меньше минуты': 'in under a minute',
  'за 2–3 минуты': 'in 2–3 minutes',
  'за 10–20 минут': 'in 10–20 minutes',
  'за 5–10 минут': 'in 5–10 minutes',
  'обычно до пары минут': 'usually within a couple of minutes',
  // result screen
  'Готово!': 'Done!',
  'Баланс пополнен': 'Balance topped up',
  'пополнен на': 'topped up with',
  '. Приятных покупок!': '. Enjoy!',
  'Пополнить ещё': 'Top up again',
  'Зачислено': 'Credited',
  '. Баланс оплатит следующие заказы — на сайте и через API.': '. Your balance will pay for your next orders — on the site and via API.',
  'Пополнить Steam': 'Top up Steam',
  'Не удалось пополнить': 'Top-up failed',
  'Оформить заново': 'Order again',
  'Заказ отменён': 'Order cancelled',
  'Если оплата всё же придёт на этот счёт, она зачислится на баланс сайта.':
    'If a payment still arrives for this invoice, it will be credited to your site balance.',
  'Создать новый заказ': 'New order',
  'Создать новый счёт': 'New invoice',
  // footer, modals
  'Условия': 'Terms',
  'Как это работает': 'How it works',
  'Закрыть': 'Close',
  'Вход через Telegram': 'Sign in with Telegram',
  'Откройте бота и нажмите в нём этот код': 'Open the bot and tap this code there',
  'Открыть Telegram': 'Open Telegram',
  'Ждём подтверждения в боте…': 'Waiting for confirmation in the bot…',
  'Получить новый код': 'Get a new code',
  'Вход отменён в боте.': 'Sign-in was cancelled in the bot.',
  'Код устарел.': 'The code has expired.',
  'Вход через Telegram временно недоступен': 'Telegram sign-in is temporarily unavailable',
  'Вы вышли': 'Signed out',
  'Скопировано': 'Copied',
  'Ошибка сети, попробуйте ещё раз': 'Network error, please try again',
  'Dev-вход': 'Dev sign-in',
  'Бот не настроен (нет BOT_TOKEN), поэтому локально можно войти тестовым пользователем.':
    "The bot isn't configured (no BOT_TOKEN), so locally you can sign in as a test user.",
  'Войти как тестовый пользователь': 'Sign in as a test user',
  'Где найти логин Steam': 'Where to find your Steam login',
  'Нужен именно логин — то, что вы вводите при входе в Steam. Никнейм профиля не подойдёт.':
    "We need the login — what you type when signing in to Steam. Your profile nickname won't work.",
  'Откройте Steam или': 'Open Steam or',
  'Нажмите на своё имя в правом верхнем углу →': 'Click your name in the top-right corner →',
  'Об аккаунте': 'Account details',
  'Логин указан в заголовке:': 'The login is in the heading:',
  '«Аккаунт ВАШ_ЛОГИН»': '“YOUR_LOGIN’s account”',
  'Платёж не находится?': 'Payment not found?',
  'Так бывает, если отправлена не та сумма (например, биржа удержала комиссию). Вставьте хэш транзакции — мы найдём её в сети и зачислим деньги после проверки.':
    "This happens when a different amount was sent (e.g. the exchange kept a fee). Paste the transaction hash — we'll find it on-chain and credit the money after a check.",
  'Хэш транзакции (TxID)': 'Transaction hash (TxID)',
  '0x… / хэш из кошелька или биржи': '0x… / hash from your wallet or exchange',
  'Отправить на проверку': 'Submit for review',
  'Вставьте хэш транзакции': 'Paste the transaction hash',
  // cabinet
  'Кабинет': 'Account',
  'Заказы': 'Orders',
  'Баланс': 'Balance',
  'Баланс сайта': 'Site balance',
  'Автоматически оплачивает следующие заказы — на сайте и через API': 'Automatically pays for your next orders — on the site and via API',
  'Заказов пока нет': 'No orders yet',
  'Движений по балансу пока нет': 'No balance activity yet',
  'ждёт оплату': 'awaiting payment',
  'оплачен': 'paid',
  'пополняется': 'in progress',
  'в очереди': 'queued',
  'проверяется': 'checking',
  'готово': 'done',
  'возврат на баланс': 'refunded to balance',
  'истёк': 'expired',
  'отменён': 'cancelled',
  'Возврат': 'Refund',
  'Корректировка': 'Adjustment',
  'корректировка админом': 'adjusted by admin',
  'Подключите свой сервис или бота: пополняйте баланс криптой и создавайте пополнения Steam автоматически. Заказы через API оплачиваются с баланса.':
    'Connect your service or bot: fund your balance with crypto and create Steam top-ups automatically. API orders are paid from the balance.',
  'Документация →': 'Documentation →',
  'Ваша скидка': 'Your discount',
  'API-ключ': 'API key',
  'Ключ': 'Key',
  'Создан': 'Created',
  'Последний запрос': 'Last request',
  'ещё не было': 'none yet',
  'Ключа ещё нет. Он показывается один раз — сохраните его в надёжном месте.': "No key yet. It's shown only once — keep it somewhere safe.",
  'Перевыпустить': 'Reissue',
  'Создать ключ': 'Create key',
  'Старый ключ перестанет работать. Перевыпустить?': 'The old key will stop working. Reissue?',
  'Ваш API-ключ — сохраните его, больше мы его не покажем:': "Your API key — save it, we won't show it again:",
  'Отозвать': 'Revoke',
  'Отозвать ключ? Запросы с ним перестанут работать.': 'Revoke the key? Requests with it will stop working.',
  'Ключ отозван': 'Key revoked',
  'Безопасность и уведомления': 'Security and notifications',
  'IP-адреса, с которых разрешён ключ': 'IP addresses allowed to use the key',
  'пусто = любые; через запятую': 'empty = any; comma-separated',
  'Пришлём POST при событиях': 'We send a POST on',
  '. Подпись — заголовок': '. Signature: the',
  '(HMAC-SHA256 тела вашим секретом).': 'header (HMAC-SHA256 of the body with your secret).',
  'сначала создайте ключ': 'create a key first',
  'Сохранить': 'Save',
  'Сохранено': 'Saved',
  'Секрет для проверки подписи вебхуков — сохраните, больше не покажем:': "Webhook signing secret — save it, we won't show it again:",
  'Тестовый вебхук': 'Test webhook',
  'Отправляем webhook.test': 'Sending webhook.test',
  'Новый секрет': 'New secret',
  'Старый секрет перестанет подходить. Сгенерировать новый?': 'The old secret will stop working. Generate a new one?',
  'Новый секрет вебхуков — сохраните:': 'New webhook secret — save it:',
  // terms & FAQ
  'Условия сервиса': 'Terms of service',
  'SupplierHub оказывает услугу пополнения баланса Steam-аккаунта по указанному пользователем логину. Оплата принимается в криптовалюте.':
    'SupplierHub tops up the Steam wallet of the account whose login the user provides. Payment is accepted in cryptocurrency.',
  'Заказ и оплата': 'Orders and payment',
  'Сумма к оплате фиксируется на время действия счёта. Отправляйте ровно указанную сумму в указанной сети — средства, отправленные в другой сети или другой монетой, могут быть утеряны.':
    'The amount due is fixed while the invoice is valid. Send exactly the stated amount on the stated network — funds sent on another network or in another coin may be lost.',
  'Исполнение': 'Fulfilment',
  'После подтверждения платежа в сети пополнение выполняется автоматически. Пользователь отвечает за правильность указанного логина.':
    'Once the payment is confirmed on-chain, the top-up runs automatically. The user is responsible for entering the correct login.',
  'Возвраты': 'Refunds',
  'Если пополнение не удалось, стоимость заказа возвращается на баланс сайта и может быть использована для нового заказа. Платёж, пришедший после истечения счёта, также зачисляется на баланс.':
    'If a top-up fails, the order cost returns to the site balance and can be used for a new order. A payment that arrives after the invoice expires is also credited to the balance.',
  'По любым вопросам пишите в поддержку — кнопка «Поддержка» вверху страницы.': 'For any questions, contact support — the “Support” button at the top of the page.',
  '1. Логин и сумма': '1. Login and amount',
  'Укажите логин Steam — мы сразу проверим, что аккаунт можно пополнить. Выберите валюту аккаунта и сумму.':
    "Enter your Steam login — we'll check right away that the account can be topped up. Pick the account currency and the amount.",
  '2. Оплата криптой': '2. Pay with crypto',
  'Выберите монету и сеть. Мы покажем точную сумму, адрес и QR-код. Сумма уникальна — по ней мы автоматически узнаём ваш платёж.':
    "Choose a coin and network. We'll show the exact amount, address and QR code. The amount is unique — that's how we recognise your payment automatically.",
  '3. Пополнение': '3. Top-up',
  'Как только платёж подтвердится в сети, Steam пополнится автоматически. Статус виден на сайте, уведомление придёт в Telegram.':
    'As soon as the payment is confirmed on-chain, Steam is topped up automatically. The status is on the site, and a notification comes to Telegram.',
  'Если что-то пошло не так': 'If something goes wrong',
  'Деньги не пропадают: при любой ошибке они возвращаются на баланс сайта. Им можно оплатить следующий заказ.':
    'Your money is never lost: on any error it returns to your site balance, which pays for your next order.',
  // coming soon: the marketplace plugin
  'Скоро': 'Soon',
  'Плагин для FunPay и Playerok': 'Plugin for FunPay & Playerok',
  'Автовыдача пополнений Steam на FunPay и Playerok 24/7.': 'Automatic Steam top-up delivery on FunPay and Playerok, 24/7.',
  'Узнать о запуске': 'Notify me',
  'Вы в списке': "You're on the list",
  'Готово! Напишем в Telegram, как только плагин выйдет': "Done! We'll message you on Telegram as soon as the plugin is out",
  'Вы уже в списке — напишем о запуске': "You're already on the list — we'll tell you when it launches",
  // server messages (errors come as `detail`, shown in toasts and hints)
  'Приняли! Найдём транзакцию в сети и зачислим после проверки — обычно это занимает несколько минут. Уведомление придёт в Telegram.':
    "Got it! We'll find the transaction on-chain and credit it after a check — usually within a few minutes. You'll get a Telegram notification.",
  'Приняли! Найдём транзакцию в сети и зачислим после проверки. Уведомление придёт в Telegram.':
    "Got it! We'll find the transaction on-chain and credit it after a check. You'll get a Telegram notification.",
  'Логин: 3–64 символа — латиница, цифры, _ - .': 'Login: 3–64 characters — Latin letters, digits, _ - .',
  'Логин Steam: 3–64 символа — латиница, цифры, _ - .': 'Steam login: 3–64 characters — Latin letters, digits, _ - .',
  'Аккаунт не найден или его нельзя пополнить': "Account not found or it can't be topped up",
  'Этот Steam-аккаунт нельзя пополнить: проверьте логин (именно логин для входа, не никнейм)':
    "This Steam account can't be topped up: check the login (the sign-in name, not the nickname)",
  'Не удалось подтвердить вход через Telegram — откройте приложение из бота заново': "Couldn't confirm the Telegram sign-in — reopen the app from the bot",
  'Заказ не найден': 'Order not found',
  'Пополнение не найдено': 'Top-up not found',
  'Это не похоже на хэш транзакции': "That doesn't look like a transaction hash",
  'Проверка временно недоступна': 'Checking is temporarily unavailable',
  'Аккаунт заблокирован': 'Account blocked',
  'Этот заказ уже нельзя отменить': 'This order can no longer be cancelled',
  'Платёж уже найден — дождитесь подтверждения': 'The payment is already found — wait for confirmation',
  'Эта транзакция уже на проверке': 'This transaction is already under review',
  'Сначала укажите адрес вебхука': 'Set a webhook URL first',
  'нет открытого счёта': 'no open invoice',
  'Некорректный логин': 'Invalid login',
  'Некорректный логин Steam': 'Invalid Steam login',
  'Некорректная сумма': 'Invalid amount',
  'Неизвестная валюта': 'Unknown currency',
  'Выберите способ оплаты': 'Choose a payment method',
  'Пополнение временно недоступно — идёт пополнение резерва. Попробуйте позже': 'Top-ups are temporarily unavailable — the reserve is being refilled. Try again later',
  'Сервис пополнения временно недоступен, попробуйте через пару минут': 'The top-up service is temporarily unavailable, try again in a couple of minutes',
  'Не удалось проверить логин Steam, попробуйте ещё раз': "Couldn't check the Steam login, please try again",
  'Слишком много неоплаченных заказов — оплатите или отмените предыдущие': 'Too many unpaid orders — pay or cancel the previous ones',
  'Не удалось списать баланс, попробуйте ещё раз': "Couldn't charge the balance, please try again",
  'У вас уже есть 3 неоплаченных счёта на пополнение — оплатите или дождитесь их истечения': 'You already have 3 unpaid top-up invoices — pay them or wait until they expire',
  'Вы уже использовали этот промокод': "You've already used this promo code",
  'Такого промокода нет': 'No such promo code',
  'Срок действия промокода истёк': 'This promo code has expired',
  'Промокод закончился': 'This promo code has run out',
  'Это промокод на скидку — введите его при оформлении заказа': 'This is a discount code — enter it when placing an order',
  'Это промокод на баланс — активируйте его отдельно, кнопкой «Применить»': 'This is a balance code — redeem it separately with the “Apply” button',
  'Войдите через Telegram': 'Sign in with Telegram',
  'Слишком много запросов, подождите немного': 'Too many requests, please wait a moment',
  'Доступ с вашего IP временно ограничен из-за подозрительной активности. Попробуйте позже.':
    'Access from your IP is temporarily restricted due to suspicious activity. Please try again later.',
  'Проверьте введённые данные': 'Check the data you entered',
  'Нужен адрес вида https://…': 'The address must look like https://…',
  'Слишком длинный адрес': 'The address is too long',
  'Домен не найден': 'Domain not found',
  'Адрес ведёт во внутреннюю сеть': 'The address points to an internal network',
  'возврат администратором': 'refunded by admin',
  'отклонено поставщиком': 'rejected by the supplier',
};

const DICT = new Map(Object.entries(EN));

// phrases with numbers, names and amounts inside; applied in order to whatever the dictionary didn't cover
const PATTERNS = [
  [/^Вы вошли: (.+)$/, (_, n) => `Signed in: ${n}`],
  [/^Сумма пополнения от (.+) до (.+)$/, (_, a, b) => `Top-up amount: ${a} to ${b}`],
  [/^Сумма должна быть от (.+) до (.+)$/, (_, a, b) => `The amount must be from ${a} to ${b}`],
  [/^Сумма от (.+) до (.+)$/, (_, a, b) => `Amount: ${a} to ${b}`],
  [/^от (.+?) до (.+?) · зачисляется в USD$/, (_, a, b) => `${a} to ${b} · credited in USD`],
  [/^от (.+) до (.+)$/, (_, a, b) => `${a} to ${b}`],
  [/^минимальный перевод в (\S+) — (.+?), лишнее останется на балансе сайта$/,
    (_, c, u) => `the minimum ${c} transfer is ${u}; the rest stays on your site balance`],
  [/^минимальный перевод в (\S+) — (.+?), на баланс зачислится вся сумма$/,
    (_, c, u) => `the minimum ${c} transfer is ${u}; all of it goes to your balance`],
  [/^(.+) · точная сумма будет в счёте$/, (_, u) => `${u} · the exact amount will be in the invoice`],
  [/^Промокод (\S+) применён: скидка (.+)$/, (_, c, p) => `Promo code ${c} applied: ${p} off`],
  [/^Промокод (\S+): (.+)$/, (_, c, l) => `Promo code ${c}: ${tr(l)}`],
  [/^Промокод (\S+)$/, (_, c) => `Promo code ${c}`],
  [/^−(.+?)% к скидке$/, (_, x) => `extra −${x}% off`],
  [/^\+(.+) на баланс$/, (_, x) => `+${x} to your balance`],
  [/^Заказ (\S+)$/, (_, id) => `Order ${id}`],
  [/^(.+) на баланс · (\S+)$/, (_, u, id) => `${u} to balance · ${id}`],
  [/Отправляйте только (\S+) в сети (.+?)\. Другая монета или сеть — потеря средств\./,
    (_, c, n) => `Send only ${c} on the ${n} network. A different coin or network means lost funds.`],
  [/С баланса сайта спишется (.+)\.$/, (_, u) => `${u} will be taken from your site balance.`],
  [/Подтверждений: (\S+) из (\S+)\./, (_, a, b) => `Confirmations: ${a} of ${b}.`],
  [/Сеть подтверждает (.+?)\.$/, (_, eta) => `The network confirms ${tr(eta)}.`],
  [/^Причина: (.+?)\. /, (_, r) => `Reason: ${tr(r)}. `],
  [/(\S+) вернулись на баланс сайта — можно оформить заказ заново, он оплатится с баланса\./,
    (_, u) => `${u} went back to your site balance — you can order again and it will be paid from the balance.`],
  [/Недостаточно средств на балансе: (.+?)\$, нужно (.+?)\$/, (_, a, b) => `Not enough balance: $${a}, need $${b}`],
  [/Сейчас можем принять заказ максимум на (.+?)\. Уменьшите сумму или попробуйте позже/,
    (_, a) => `Right now we can take orders of up to ${a}. Lower the amount or try again later`],
  [/Оплата в (\S+) временно недоступна, выберите другую монету/, (_, c) => `Payments in ${c} are temporarily unavailable, choose another coin`],
  [/^Некорректный IP: (.+)$/, (_, x) => `Invalid IP: ${x}`],
  [/^Webhook: (.+)$/, (_, x) => `Webhook: ${tr(x)}`],
  [/^Некорректные или отсутствующие поля: (.+)$/, (_, x) => `Invalid or missing fields: ${x}`],
  [/Возврат по заказу (\S+)/, (_, id) => `Refund for order ${id}`],
  [/, заявка #(\d+)/, (_, n) => `, claim #${n}`],
];

/** English for one Russian phrase (leading/trailing spaces kept); unknown phrases come back unchanged. */
export function tr(s) {
  if (LANG !== 'en' || !s || !CYR.test(s)) return s;
  const lead = s.match(/^\s*/)[0], trail = s.slice(lead.length).match(/\s*$/)[0];
  const core = s.trim().replace(/\s+/g, ' ');
  if (DICT.has(core)) return lead + DICT.get(core) + trail;
  let out = core;
  for (const [re, fn] of PATTERNS) {
    if (!CYR.test(out)) break;
    out = out.replace(re, fn);
  }
  return out === core ? s : lead + out + trail;
}

// ------------------------------------------------------------------ the page
const ATTRS = ['placeholder', 'aria-label', 'title', 'alt', 'content'];
const SKIP = new Set(['SCRIPT', 'STYLE', 'CODE', 'TEXTAREA']);

function trText(node) {
  const p = node.parentElement;
  if (!p || SKIP.has(p.tagName) || p.closest('[translate="no"]')) return;
  const v = node.data;
  if (!CYR.test(v)) return;
  const t = tr(v);
  if (t !== v) node.data = t;
}

function trAttrs(elm) {
  if (elm.closest('[translate="no"]')) return;
  for (const a of ATTRS) {
    const v = elm.getAttribute(a);
    if (v && CYR.test(v) && (a !== 'content' || elm.tagName === 'META')) {
      const t = tr(v);
      if (t !== v) elm.setAttribute(a, t);
    }
  }
}

function walk(root) {
  if (root.nodeType === Node.TEXT_NODE) { trText(root); return; }
  if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return;
  if (root.nodeType === Node.ELEMENT_NODE) trAttrs(root);
  const it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  for (let n = it.nextNode(); n; n = it.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) trText(n);
    else { trAttrs(n); if (n.tagName === 'TEMPLATE') walk(n.content); }
  }
}

function langSwitch() {
  const foot = document.querySelector('.foot');
  if (!foot || foot.querySelector('.lang-switch')) return;
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'lang-switch';
  b.setAttribute('translate', 'no');
  b.setAttribute('aria-label', LANG === 'en' ? 'Русская версия' : 'English version');
  b.textContent = LANG === 'en' ? 'RU' : 'EN';
  b.addEventListener('click', () => setLang(LANG === 'en' ? 'ru' : 'en'));
  foot.append(b);
}

function start() {
  langSwitch();
  if (LANG !== 'en') return;
  walk(document.documentElement);
  new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === 'childList') m.addedNodes.forEach(walk);
      else if (m.type === 'characterData') trText(m.target);
      else if (m.type === 'attributes') trAttrs(m.target);
    }
  }).observe(document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS });
}

document.documentElement.lang = LANG;
if (LANG === 'en') {
  const confirm0 = window.confirm.bind(window);
  window.confirm = (msg) => confirm0(tr(String(msg)));
}
// a module runs after the document is parsed, so the whole page is already there
start();

export const _test = { EN, PATTERNS, detect };
