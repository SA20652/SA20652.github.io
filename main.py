import requests
import os
import re, json, hmac, hashlib, urllib.parse, asyncio, threading, time, random, secrets, urllib.request, urllib.parse as urlparse
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from aiogram import Bot, types
from aiogram.dispatcher import Dispatcher
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
import database
import traceback
from case_catalog import CASES_BY_ID, weighted_pick
from upgrade_config import is_valid_target

# Серверный каталог остаётся источником истины: Kuro теперь стоит 7 000 000.
for _case in CASES_BY_ID.values():
    for _item in _case.get('items', []):
        if str(_item.get('name') or '').strip().lower() == 'kuro':
            _item['value'] = 7_000_000

load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
BOT_USERNAME = os.getenv('BOT_USERNAME', '').strip().lstrip('@')
WEBAPP_URL = os.getenv('WEBAPP_URL', 'https://your-app.netlify.app').strip()
# Безопасность админки: доступ только по точному Telegram ID.
# Username, пароль и локальные флаги аккаунта не дают права администратора.
ADMIN_TELEGRAM_ID = 7491528121
ADMIN_SESSION_TTL = 10 * 60
ADMIN_MAX_REQUESTS_PER_MINUTE = 30
IP_BAN_DEFAULT_MINUTES = 60
RUSH_MAX_BET = 1_000_000
RUSH_MAX_MULTIPLIER = 2.5

KEY_DROP_CATALOG = [
    {'id':'rust-key','name':'Ключ латунного сейфа','rarity':'common','value':120,'icon':'🗝️'},
    {'id':'brass-key','name':'Ключ латунного сейфа','rarity':'rare','value':650,'icon':'🔑'},
    {'id':'obsidian-key','name':'Ключ чёрного сейфа','rarity':'epic','value':2800,'icon':'🗝️'},
    {'id':'vault-key','name':'Ключ хранилища сейфа','rarity':'legendary','value':12000,'icon':'🔐'},
    {'id':'master-key','name':'Мастер-ключ','rarity':'mythic','value':65000,'icon':'🔑'},
]
CASE_KEY_REQUIREMENTS = {
    # The client/catalog historically used rust-key for the brass safe.
    # Accept both old and current brass-key records.
    'brass': ['brass-key', 'rust-key'],
    'obsidian': ['obsidian-key'],
    'vault': ['vault-key'],
    'master': ['master-key'],
}
def required_keys_for_case(case_id):
    return CASE_KEY_REQUIREMENTS.get(str(case_id), [])

def required_key_for_case(case_id):
    keys = required_keys_for_case(case_id)
    return keys[0] if keys else None

def consume_case_key(user_id, case_id, count=1):
    accepted = required_keys_for_case(case_id)
    if not accepted:
        raise ValueError('Для этого сейфа нет ключа')
    return database.consume_key_types_atomic(user_id, accepted, int(count))

def server_maybe_drop_key(user_id, case_price):
    if secrets.randbelow(10000) >= 800:
        return None
    max_value=max(120,int(case_price or 0)*55//100)
    pool=[k for k in KEY_DROP_CATALOG if int(k['value'])<=max_value] or KEY_DROP_CATALOG[:1]
    chosen=secrets.choice(pool)
    return None
_admin_rate = defaultdict(deque)
_admin_rate_lock = threading.Lock()
PORT = int(os.getenv('PORT', '8080') or 8080)

# 12-hour bonus ledger. Kept separately so the bonus cadence is independent
# from the legacy database.claim_daily() 24-hour/date-based cooldown.
BONUS_12H_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bonus_12h.json')
_BONUS_12H_LOCK = threading.RLock()
BONUS_12H_COOLDOWN = 12 * 60 * 60

def _load_bonus_12h():
    with _BONUS_12H_LOCK:
        try:
            with open(BONUS_12H_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

def _save_bonus_12h(data):
    tmp = BONUS_12H_FILE + '.tmp'
    with _BONUS_12H_LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BONUS_12H_FILE)

def claim_bonus_12h(user_id, premium=False):
    now = time.time()
    key = str(user_id)
    rewards = [1000, 1500, 2000, 2500, 3000, 4000, 5000]
    with _BONUS_12H_LOCK:
        ledger = _load_bonus_12h()
        row = ledger.get(key) if isinstance(ledger.get(key), dict) else {}
        last = float(row.get('last_claim', 0) or 0)
        next_at = float(row.get('next_claim_at', 0) or 0)
        streak_day = int(row.get('streak_day', 0) or 0)
        claims_in_day = int(row.get('claims_in_day', 0) or 0)
        # A gap longer than 24h breaks the current daily streak.
        if last and now - last > 24 * 60 * 60:
            streak_day = 0
            claims_in_day = 0
        if last and now - last < BONUS_12H_COOLDOWN:
            remaining = max(1, int(BONUS_12H_COOLDOWN - (now - last)))
            raise ValueError(f'Бонус уже получен. Следующий бонус через {remaining // 3600:02d}:{(remaining % 3600) // 60:02d}:{remaining % 60:02d}')
        # Each pair of 12-hour claims completes one streak day. Day 7 is the cap.
        if streak_day <= 0:
            streak_day = 1
            claims_in_day = 0
        elif claims_in_day >= 2:
            streak_day = min(7, streak_day + 1)
            claims_in_day = 0
        reward = int(rewards[streak_day - 1])
        if premium:
            reward *= 2
        balance = database.update_balance(int(user_id), reward)
        claims_in_day += 1
        next_claim = now + BONUS_12H_COOLDOWN
        ledger[key] = {
            'last_claim': now,
            'next_claim_at': next_claim,
            'streak_day': streak_day,
            'claims_in_day': claims_in_day,
        }
        _save_bonus_12h(ledger)
        return {
            'balance': balance, 'reward': reward, 'streak': streak_day,
            'claims_in_day': claims_in_day, 'last_daily': int(now * 1000),
            'next_daily': int(next_claim * 1000),
        }

if not BOT_TOKEN:
    print('⚠️ BOT_TOKEN не задан! Бот не будет работать, но API запустится.')

database.init_db()

app = Flask(__name__)

# Persistent profile name styles (separate from the game database so older database.py
# versions remain compatible). Values are style keys: default/gold/emerald/ruby/amethyst/ice/neon.
NAME_STYLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'profile_name_styles.json')
_NAME_STYLE_LOCK = threading.RLock()
_NAME_STYLE_KEYS = {'default','gold','emerald','ruby','amethyst','ice','neon'}

def _load_name_styles():
    with _NAME_STYLE_LOCK:
        try:
            with open(NAME_STYLE_FILE, 'r', encoding='utf-8') as f:
                raw=json.load(f)
            return {str(k): str(v) for k,v in (raw or {}).items() if str(v) in _NAME_STYLE_KEYS}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

def _save_name_styles(styles):
    tmp=NAME_STYLE_FILE+'.tmp'
    with _NAME_STYLE_LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(styles, f, ensure_ascii=False, indent=2)
        os.replace(tmp, NAME_STYLE_FILE)

def get_profile_name_style(user_id):
    u=database.get_user(user_id) or {}
    key=str(u.get('profile_name_style') or 'default').lower()
    return key if key in _NAME_STYLE_KEYS else 'default'

# Ограничиваем CORS адресом WebApp вместо wildcard.
_allowed_origins = [WEBAPP_URL.rstrip('/')] if WEBAPP_URL and not WEBAPP_URL.startswith('https://your-app.netlify.app') else []
# Telegram WebView/Netlify can use a different Origin from the value stored in
# WEBAPP_URL.  API calls are authenticated by Telegram initData/admin sessions,
# so do not let a stale WEBAPP_URL silently break the whole game through CORS.
CORS(app, resources={r'/api/*': {'origins': '*'}})

@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if request.path == '/' or request.path.endswith('.html'):
        response.headers['Content-Security-Policy'] = "default-src 'self' https://telegram.org https://fonts.googleapis.com https://fonts.gstatic.com data: blob:; connect-src 'self' https://gdplay.up.railway.app https://telegram.org; img-src 'self' data: blob: https://t.me https://telegram.org; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; script-src 'self' 'unsafe-inline' https://telegram.org; connect-src 'self' https://api.telegram.org; frame-ancestors https://web.telegram.org https://telegram.org; base-uri 'self'; object-src 'none'"
    return response

@app.get('/')
def index_page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'index.html')

@app.get('/assets/<path:filename>')
def app_asset(filename):
    # Static assets kept outside index.html so the browser does not parse megabytes
    # of base64 before the first screen can render.
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)

@app.get('/promo/2026samergermancrut.jpg')
def promo_secret_image():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'IMG_20260817_134411.jpg')

@app.get('/promo/attasha.jpg')
def promo_attasha_image():
    # ImgBB share pages are HTML pages, not direct image URLs. Resolve the
    # fixed share link server-side to its og:image and return the actual image
    # to the WebApp. If ImgBB is temporarily unavailable, fall back to the
    # bundled promo image instead of breaking the promo animation.
    share_url = 'https://ibb.co/7dQy9z8f'
    try:
        req = urllib.request.Request(share_url, headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read(2_000_000).decode('utf-8', 'ignore')
        m = re.search(r'<meta[^>]+property=[\"\']og:image[\"\'][^>]+content=[\"\']([^\"\']+)', html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=[\"\']([^\"\']+)[\"\'][^>]+property=[\"\']og:image[\"\']', html, re.I)
        if m:
            image_url = html.unescape(m.group(1))
            ireq = urllib.request.Request(image_url, headers={'User-Agent':'Mozilla/5.0'})
            with urllib.request.urlopen(ireq, timeout=8) as ir:
                blob = ir.read(8_000_000)
                ctype = ir.headers.get_content_type() or 'image/jpeg'
            from flask import Response
            return Response(blob, mimetype=ctype, headers={'Cache-Control':'public, max-age=3600'})
    except Exception as e:
        print(f'attasha image proxy error: {e}')
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'IMG_20260817_134411.jpg')


# --- Бот ---
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher(bot) if bot else None

# ============================================
# ТЕСТОВАЯ ПОКУПКА STARS (без денег)
# ============================================

@dp.message_handler(commands=['test_payment'])
async def test_payment(message: types.Message):
    """Тестовая команда для начисления 20 Stars без оплаты"""
    user_id = message.from_user.id
    
    # Только админ может использовать
    if user_id != ADMIN_TELEGRAM_ID:
        await message.reply("⛔ Только для админа!")
        return
    
    # Имитация платежа на 20 Stars
    fake_payment = {
        "currency": "XTR",
        "total_amount": 20,
        "invoice_payload": "test_payload",
        "telegram_payment_charge_id": f"test_charge_{user_id}_{int(datetime.now().timestamp())}",
        "provider_payment_charge_id": "test_provider"
    }
    
    # Начисляем Stars
    await process_stars_payment(user_id, fake_payment)
    
    await message.reply(f"✅ Тестово начислено 20 Stars!\n💰 Проверьте баланс в профиле")

async def process_stars_payment(user_id, payment_data):
    """
    ОБЩАЯ ФУНКЦИЯ для начисления Stars.
    Используется и для тестов, и для реальных платежей.
    """
    stars_amount = payment_data["total_amount"]
    
    # Начисляем через database
    try:
        database.add_stars(user_id, stars_amount)
    except AttributeError:
        # Если нет функции add_stars, пробуем другие варианты
        try:
            database.update_user_balance(user_id, stars_amount)
        except:
            # Если ничего не работает, просто логируем
            print(f"⚠️ Не удалось начислить Stars через database, но сумма: {stars_amount}")
    
    print(f"✅ Начислено {stars_amount} Stars пользователю {user_id}")
    return True

# Реальный обработчик платежа от Telegram
@dp.message_handler(content_types=types.ContentType.SUCCESSFUL_PAYMENT)
async def handle_successful_payment(message: types.Message):
    """Единый обработчик успешных Stars-платежей.

    Важно: этот handler зарегистрирован раньше остальных SUCCESSFUL_PAYMENT,
    поэтому Premium нужно обрабатывать здесь, иначе общий обработчик перехватывает
    платёж и Premium не активируется.
    """
    user_id = message.from_user.id
    payment = message.successful_payment
    payload = str(payment.invoice_payload or '')

    if payload.startswith('premium_'):
        database.create_user(user_id, message.from_user.username or message.from_user.first_name or str(user_id))
        database.grant_premium(user_id, 30)
        await message.reply(
            '⭐ Premium успешно активирован на 30 дней!\n\n'
            'Доступно:\n'
            '• x2 ежедневный бонус\n'
            '• статус ⭐ Premium в профиле\n'
            '• специальные Premium-бонусы\n'
            'Спасибо за поддержку GDPLAY!'
        )
        print(f'✅ Premium выдан пользователю {user_id}; payload={payload}')
        return

    coin_package = _coin_package_from_payload(payload)
    if coin_package:
        stars, coins = coin_package
        if str(payment.currency or '') != 'XTR' or int(payment.total_amount or 0) != stars:
            print(f'⚠️ Отклонён неверный coin payment: user={user_id} payload={payload} amount={payment.total_amount}')
            return
        charge_id = str(payment.telegram_payment_charge_id or '')
        with _coin_payment_lock:
            if charge_id and charge_id in _coin_payment_charges:
                print(f'ℹ️ Повторный coin payment пропущен: {charge_id}')
                return
            if charge_id:
                _coin_payment_charges.add(charge_id)
        database.create_user(user_id, message.from_user.username or message.from_user.first_name or str(user_id))
        new_balance = database.update_balance(user_id, coins)
        await message.reply(f'✅ Покупка успешна! Начислено {coins:,}'.replace(',', ' ') + ' 💰')
        print(f'✅ Монеты выданы: user={user_id} coins={coins} stars={stars} payload={payload}')
        return

    await process_stars_payment(user_id, {
        "currency": payment.currency,
        "total_amount": payment.total_amount,
        "invoice_payload": payload,
        "telegram_payment_charge_id": payment.telegram_payment_charge_id,
        "provider_payment_charge_id": payment.provider_payment_charge_id
    })
    await message.reply(f"✅ Оплата прошла! Начислено {payment.total_amount} Stars!")

# Обработчик предварительного запроса (обязательно для реальной оплаты)
@dp.pre_checkout_query_handler()
async def process_pre_checkout(query: types.PreCheckoutQuery):
    # Критично: Telegram отменяет оплату, если answerPreCheckoutQuery
    # не получен в течение 10 секунд. Используем прямой вызов Bot API,
    # а не query.answer(), чтобы исключить несовместимость версии aiogram.
    try:
        payload = str(query.invoice_payload or '')
        currency = str(query.currency or '')
        amount = int(query.total_amount or 0)

        print(f'💳 pre_checkout: id={query.id} user={query.from_user.id} '
              f'currency={currency} amount={amount} payload={payload[:80]}')

        if payload.startswith('premium_'):
            valid = currency == 'XTR' and amount == 20
        else:
            coin_package = _coin_package_from_payload(payload)
            valid = bool(coin_package) and currency == 'XTR' and amount == coin_package[0]

        if not valid:
            await bot.answer_pre_checkout_query(
                pre_checkout_query_id=query.id,
                ok=False,
                error_message='Неверная сумма или счёт'
            )
            return

        await bot.answer_pre_checkout_query(
            pre_checkout_query_id=query.id,
            ok=True
        )
        print(f'✅ pre_checkout подтверждён: {query.id}')
    except Exception as e:
        print(f'❌ pre_checkout error: {e}')
        try:
            await bot.answer_pre_checkout_query(
                pre_checkout_query_id=query.id,
                ok=False,
                error_message='Не удалось проверить счёт. Попробуйте ещё раз.'
            )
        except Exception as answer_error:
            print(f'❌ не удалось ответить на pre_checkout_query: {answer_error}')

# --- Функции ---
def verify_init_data(init_data):
    print('[AUTH] initData received:', bool(init_data), 'length:', len(init_data or ''))
    if not init_data:
        raise ValueError('Telegram WebApp не передал initData')
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop('hash', None)
    if not received:
        raise ValueError('Нет hash в initData')
    check = '\n'.join(f'{k}={pairs[k]}' for k in sorted(pairs))
    secret = hmac.new(b'WebAppData', BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        print('[AUTH] hash check FAILED')
        raise ValueError('Недействительный Telegram initData')
    print('[AUTH] hash check OK')
    auth = int(pairs.get('auth_date', '0') or 0)
    if auth and abs(datetime.now(timezone.utc).timestamp() - auth) > 86400:
        raise ValueError('initData устарел')
    user = json.loads(pairs.get('user', '{}'))
    print('[AUTH] telegram id:', user.get('id'))
    if not user.get('id'):
        raise ValueError('Telegram user отсутствует')
    return user

def telegram_display_name(user):
    if not user:
        return 'Игрок'
    first = str(user.get('first_name') or '').strip()
    last = str(user.get('last_name') or '').strip()
    return ' '.join(x for x in (first, last) if x) or 'Игрок'

def current(payload):
    payload = payload or {}
    client_ip = database.get_client_ip_from_request(request)
    if database.is_ip_banned(client_ip):
        raise PermissionError('Доступ заблокирован для этого IP-адреса')
    token = str(payload.get('authToken') or '').strip()
    if token:
        uid = database.get_auth_session(token)
        if uid:
            database.enforce_ban_state(uid)
            u = database.get_user(uid)
            if u:
                database.record_user_ip(uid, client_ip)
                if u.get('banned'):
                    raise PermissionError(u.get('ban_reason') or 'Аккаунт заблокирован')
                return {'id': uid, 'username': u.get('username') or ''}, u
    user = verify_init_data(payload.get('initData', ''))
    database.create_user(user['id'], user.get('username') or user.get('first_name') or str(user['id']))
    database.set_telegram_display_name(user['id'], telegram_display_name(user))
    database.enforce_ban_state(user['id'])
    u = database.get_user(user['id'])
    database.record_user_ip(user['id'], client_ip)
    if u and u.get('banned'):
        raise PermissionError(u.get('ban_reason') or 'Аккаунт заблокирован')
    # Telegram photo_url обновляем только для Telegram-аватаров. Кастомный
    # data:image/blob аватар игрока не перезаписываем.
    photo = str(user.get('photo_url') or '').strip()
    old_avatar = str(u.get('avatar') or '') if u else ''
    if photo and (not old_avatar or old_avatar.startswith('https://t.me/i/userpic/')):
        if photo != old_avatar:
            database.set_avatar(user['id'], photo)
            u = database.get_user(user['id'])
    return user, u

def is_admin_user(user, db_user=None):
    # Единственный источник права администратора — точный Telegram ID.
    # Username намеренно НЕ учитывается: его можно изменить, а совпадение
    # username не должно давать доступ к админке.
    return bool(ADMIN_TELEGRAM_ID and user and int(user.get('id') or 0) == ADMIN_TELEGRAM_ID)

def _admin_rate_check(ip):
    now=time.time(); key=str(ip or 'unknown')[:80]
    with _admin_rate_lock:
        q=_admin_rate[key]
        while q and q[0] <= now-60: q.popleft()
        if len(q) >= ADMIN_MAX_REQUESTS_PER_MINUTE:
            raise PermissionError('Слишком много запросов к админ-панели. Попробуйте позже.')
        q.append(now)

def _hash_admin_session(token):
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()

def create_admin_session(payload):
    if not ADMIN_TELEGRAM_ID: raise PermissionError('Админка не настроена')
    _admin_rate_check(request.remote_addr)
    tg=verify_init_data((payload or {}).get('initData',''))
    if int(tg.get('id') or 0) != ADMIN_TELEGRAM_ID: raise PermissionError('Нет доступа')
    database.create_user(tg['id'], tg.get('username') or tg.get('first_name') or str(tg['id']))
    token=secrets.token_urlsafe(32)
    expires=datetime.now(timezone.utc)+timedelta(seconds=ADMIN_SESSION_TTL)
    database.create_admin_session(_hash_admin_session(token), ADMIN_TELEGRAM_ID, expires.strftime('%Y-%m-%d %H:%M:%S'))
    return token, int(expires.timestamp())

def admin(payload):
    payload = payload or {}
    _admin_rate_check(request.remote_addr)
    if not ADMIN_TELEGRAM_ID: raise PermissionError('Админка не настроена')
    tg=verify_init_data(payload.get('initData',''))
    if int(tg.get('id') or 0) != ADMIN_TELEGRAM_ID: raise PermissionError('Нет доступа')
    token=str(payload.get('adminSession') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', token): raise PermissionError('Админ-сессия отсутствует или истекла')
    if not database.get_admin_session(_hash_admin_session(token), ADMIN_TELEGRAM_ID): raise PermissionError('Админ-сессия истекла. Откройте админ-панель заново.')
    user,u=current({'initData':payload.get('initData','')})
    if not is_admin_user(user,u): raise PermissionError('Нет доступа')
    return user,u

def public(u):
    if not u:
        return None
    cubes = database.get_cubes(u['user_id'])
    try: buffs=json.loads(u.get('buffs') or '[]')
    except Exception: buffs=[]
    try: best_drops=json.loads(u.get('best_drops') or '[]')
    except Exception: best_drops=[]
    try: custom_status=json.loads(u.get('custom_status') or '{}')
    except Exception: custom_status={}
    return {k: u.get(k) for k in ['user_id','username','balance','stars','level','trades','daily_claimed','daily_streak','banned','ban_reason','moderation_notice','premium_until','cases_opened','battle_winnings','avatar','display_name','display_name_changed_at','creator_badge','tester_badge','two_factor_enabled','tech_break_enabled','tech_break_reason']} | {'global_tech_break': database.get_global_tech_break()} | {'custom_status': custom_status, 'profile_name_style': get_profile_name_style(u.get('user_id')), 'profile_frame': str(u.get('profile_frame') or 'default'), 'cubes': cubes, 'keys': [], 'buffs': buffs, 'best_drops': best_drops}

def effective_name(u):
    dn = (u.get('display_name') or '').strip() if u else ''
    if dn: return dn
    return (u.get('username') if u else None) or 'Игрок'

def error(e, status=400):
    return jsonify({'ok': False, 'error': str(e)}), status

# --- API Роуты ---
@app.get('/health')
def health():
    return jsonify({'ok': True, 'service': 'GDPLAY', 'webapp': WEBAPP_URL})

def send_telegram_message(chat_id, text):
    if not BOT_TOKEN:
        raise ValueError('BOT_TOKEN не задан')
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    body = urlparse.urlencode({'chat_id': int(chat_id), 'text': text}).encode()
    req = urllib.request.Request(url, data=body, method='POST')
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    if not data.get('ok'):
        raise ValueError('Не удалось отправить код в Telegram')

def sha256_password(password):
    return hashlib.sha256(str(password or '').encode()).hexdigest()

def require_telegram(payload):
    # Для auth-операций только проверяем Telegram initData.
    # Не создаём игрового пользователя заранее: иначе Telegram username
    # мог совпасть с игровым username и регистрация ошибочно считала
    # аккаунт уже существующим, а вход получал пустой пароль.
    return verify_init_data((payload or {}).get('initData', ''))

@app.post('/api/auth/register')
def auth_register():
    try:
        p=request.get_json(silent=True) or {}
        tg= require_telegram(p)
        username=str(p.get('username','')).strip()
        password=str(p.get('password',''))
        if not re.fullmatch(r'[a-zA-Zа-яА-Я0-9_]{3,16}', username):
            raise ValueError('Имя: 3–16 символов, буквы/цифры/_')
        if username.lower() == 'kuro':
            raise ValueError('Ник «kuro» зарезервирован и недоступен для регистрации.')
        if len(password)<4: raise ValueError('Пароль должен быть не короче 4 символов')

        existing=database.find_user_by_username(username)
        if existing:
            # Восстановление/задание пароля разрешено только владельцу
            # серверного аккаунта (его Telegram ID хранится в user_id).
            if int(existing.get('user_id') or 0) == int(tg['id']) and not existing.get('password_hash'):
                database.set_password_hash(tg['id'], sha256_password(password))
                fresh=database.get_user(tg['id'])
                token=database.create_auth_session(tg['id'])
                return jsonify({'ok':True,'token':token,'user':public(fresh),'password_initialized':True})
            raise ValueError('Это имя уже занято. Используй «Вход», если аккаунт уже существует.')

        database.create_user(tg['id'], username)
        database.set_password_hash(tg['id'], sha256_password(password))
        fresh=database.get_user(tg['id'])
        token=database.create_auth_session(tg['id'])
        return jsonify({'ok':True,'token':token,'user':public(fresh)})
    except Exception as e: return error(e,400)

@app.post('/api/auth/login')
def auth_login():
    try:
        p=request.get_json(silent=True) or {}
        require_telegram(p)
        username=str(p.get('username','')).strip()
        password=str(p.get('password',''))
        u=database.find_user_by_username(username)
        # Никакой миграции по одному локальному username больше нет.
        # Иначе любой человек мог создать локальное состояние с именем
        # kitzkuro и получить его админ-права.
        if not u:
            raise ValueError('Аккаунт не найден. Если аккаунт уже существует, проверь имя пользователя.')
        if not u.get('password_hash'):
            raise ValueError('У аккаунта ещё не задан пароль. Открой «Регистрация» с того Telegram-профиля, которому принадлежит этот аккаунт, чтобы задать пароль один раз.')
        if not hmac.compare_digest(str(u['password_hash']),sha256_password(password)):
            raise ValueError('Неверное имя пользователя или пароль')
        if u.get('banned'): raise PermissionError('Аккаунт заблокирован')
        if int(u.get('two_factor_enabled') or 0):
            code=f'{secrets.randbelow(1000000):06d}'
            expires=(datetime.now()+timedelta(minutes=10)).isoformat()
            database.set_two_factor_code(u['user_id'],hashlib.sha256(code.encode()).hexdigest(),expires)
            send_telegram_message(u['user_id'],f'🔐 GDPLAY\nКод входа: {code}\nКод действует 10 минут. Если это были не вы — ничего не делайте.')
            return jsonify({'ok':True,'two_factor_required':True,'user_id':int(u['user_id'])})
        token=database.create_auth_session(u['user_id'])
        return jsonify({'ok':True,'token':token,'user':public(u)})
    except PermissionError as e: return error(e,403)
    except Exception as e: return error(e,400)

@app.post('/api/auth/verify-2fa')
def auth_verify_2fa():
    try:
        p=request.get_json(silent=True) or {}
        require_telegram(p)
        uid=int(p.get('user_id',0)); code=str(p.get('code','')).strip()
        if not uid or not database.verify_two_factor_code(uid,hashlib.sha256(code.encode()).hexdigest()):
            raise ValueError('Неверный или просроченный код')
        u=database.get_user(uid)
        token=database.create_auth_session(uid)
        return jsonify({'ok':True,'token':token,'user':public(u)})
    except Exception as e: return error(e,400)

@app.post('/api/security/password')
def security_password():
    try:
        user,u=current(request.get_json(silent=True) or {})
        password=str((request.get_json(silent=True) or {}).get('password',''))
        if len(password)<4: raise ValueError('Пароль должен быть не короче 4 символов')
        database.set_password_hash(user['id'],sha256_password(password))
        return jsonify({'ok':True})
    except Exception as e: return error(e,400)

@app.post('/api/security/2fa')
def security_2fa():
    try:
        user,u=current(request.get_json(silent=True) or {})
        enabled=bool(int((request.get_json(silent=True) or {}).get('enabled',0)))
        if not u: raise ValueError('Пользователь не найден')
        if not u.get('password_hash'):
            raise ValueError('Сначала задай пароль для аккаунта')
        database.set_two_factor(user['id'],enabled)
        return jsonify({'ok':True,'enabled':enabled,'user':public(database.get_user(user['id']))})
    except Exception as e: return error(e,400)

@app.post('/api/me')
def api_me():
    try:
        payload=request.get_json(silent=True) or {}
        ip=database.get_client_ip_from_request(request)
        if database.is_ip_banned(ip):
            return error(ValueError('Доступ заблокирован для этого IP-адреса'),403)
        print('[AUTH] /api/me called')

        # Надёжное восстановление уже авторизованной сессии. Telegram WebView
        # иногда на мгновение не отдаёт initData после перезапуска/возврата в
        # приложение. В этом случае валидная серверная auth-сессия остаётся
        # достаточным доказательством личности и не требует повторного Telegram
        # handshake. Первичный вход/регистрация по-прежнему требуют initData.
        auth_token=str(payload.get('authToken') or '').strip()
        if auth_token:
            uid=database.get_auth_session(auth_token)
            if uid:
                database.enforce_ban_state(uid)
                u=database.get_user(uid)
                if u:
                    database.record_user_ip(uid, ip)
                    if u.get('banned'):
                        return jsonify({'ok':False,'error':u.get('ban_reason') or 'Аккаунт заблокирован','user':public(u),'is_admin':int(uid)==ADMIN_TELEGRAM_ID,'global_tech_break':database.get_global_tech_break()}),403
                    notice=database.get_moderation_notice(uid)
                    return jsonify({'ok':True,'user':public(u),'moderation_notice':notice,'is_admin':int(uid)==ADMIN_TELEGRAM_ID,'server_time':datetime.now(timezone.utc).isoformat(),'session_restored':True,'global_tech_break':database.get_global_tech_break()})

        tg=verify_init_data(payload.get('initData',''))
        print('[AUTH] creating/finding user:', tg.get('id'))
        database.create_user(tg['id'], tg.get('username') or tg.get('first_name') or str(tg['id']))
        database.set_telegram_display_name(tg['id'], telegram_display_name(tg))
        database.enforce_ban_state(tg['id'])
        database.record_user_ip(tg['id'], ip)
        u=database.get_user(tg['id'])
        notice=database.get_moderation_notice(tg['id'])
        if u and u.get('banned'):
            # Не скрываем причину: клиент получает её отдельным полем даже при 403.
            return jsonify({'ok':False,'error':u.get('ban_reason') or 'Аккаунт заблокирован','user':public(u),'moderation_notice':notice,'is_admin':False,'global_tech_break':database.get_global_tech_break()}),403
        photo=str(tg.get('photo_url') or '').strip()
        old_avatar=str(u.get('avatar') or '') if u else ''
        if photo and (not old_avatar or old_avatar.startswith('https://t.me/i/userpic/')) and photo != old_avatar:
            database.set_avatar(tg['id'],photo); u=database.get_user(tg['id'])
        auth_token = database.create_auth_session(tg['id'])
        return jsonify({'ok':True,'token':auth_token,'authToken':auth_token,'user':public(u),'moderation_notice':notice,'is_admin':is_admin_user(tg,u),'server_time':datetime.now(timezone.utc).isoformat(),'global_tech_break':database.get_global_tech_break()})
    except Exception as e:
        print(f"[AUTH] api_me error: {e}")
        print(traceback.format_exc())
        return error(e,401)

@app.post('/api/sync')
def api_sync():
    try:
        user, u = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        if u.get('banned'):
            return error(ValueError('Аккаунт заблокирован'), 403)
        
        state = p.get('state') or {}
        # ВАЖНО: клиентский WebView больше не может записывать экономику/инвентарь.
        # DevTools может подменить JavaScript или fetch, но сервер не принимает эти
        # значения как источник истины. Разрешаем только пользовательский аватар.
        if 'avatar' in state:
            av = state.get('avatar') or ''
            if isinstance(av, str) and len(av) <= 300000:
                database.set_avatar(user['id'], av)
        fresh = database.get_user(user['id'])
        farm_action = database.evaluate_economy_farm(user['id'])
        if farm_action:
            fresh = database.get_user(user['id'])
        return jsonify({'ok': True, 'user': public(fresh), 'farm_action': farm_action})
    except Exception as e:
        print(f"api_sync error: {e}")
        print(traceback.format_exc())
        return error(e, 400)

@app.post('/api/all-users')
def api_all_users():
    try:
        user,_=current(request.get_json(silent=True) or {})
        p=request.get_json(silent=True) or {}
        rows=database.list_all_transfer_users(user['id'], str(p.get('search',''))[:50])
        return jsonify({'ok':True,'users':[{'user_id':r['user_id'],'username':r.get('username') or '', 'display_name':r.get('display_name') or r.get('username') or 'Игрок','avatar':r.get('avatar') or ''} for r in rows]})
    except Exception as e: return error(e,400)

@app.post('/api/orbs/transfer')
def api_orbs_transfer():
    try:
        user,sender_row=current(request.get_json(silent=True) or {})
        p=request.get_json(silent=True) or {}
        target=int(p.get('user_id',0) or 0)
        amount=int(p.get('amount',0) or 0)
        result=database.transfer_balance(user['id'],target,amount)
        sender_name=effective_name(sender_row)
        database.add_notification(target,'Получены орбы',f'{sender_name} передал(а) вам {amount} 💰 орбов')
        return jsonify({'ok':True,**result,'balance':result['sender_balance']})
    except Exception as e: return error(e,400)

@app.post('/api/notifications')
def api_notifications():
    try:
        user,_=current(request.get_json(silent=True) or {})
        return jsonify({'ok':True,'notifications':database.get_notifications(user['id']),'unread':database.get_unread_notification_count(user['id'])})
    except Exception as e: return error(e,400)

@app.post('/api/notifications/read')
def api_notifications_read():
    try:
        user,_=current(request.get_json(silent=True) or {})
        database.mark_notifications_read(user['id'])
        return jsonify({'ok':True,'unread':0})
    except Exception as e: return error(e,400)


@app.post('/api/notifications/clear')
def api_notifications_clear():
    try:
        user,_=current(request.get_json(silent=True) or {})
        # Support both the new database method and older database.py versions.
        fn=getattr(database,'clear_notifications',None)
        if callable(fn): fn(user['id'])
        return jsonify({'ok':True})
    except Exception as e: return error(e,400)

@app.post('/api/referrals')
def api_referrals():
    try:
        user, u = current(request.get_json(silent=True) or {})
        global BOT_USERNAME
        stats = database.get_referral_stats(user['id'])
        # Веб-приложение может запросить ссылку раньше, чем поток aiogram успеет
        # выполнить on_startup. В этом случае один раз получаем username через Bot API.
        if not BOT_USERNAME and BOT_TOKEN:
            try:
                r = requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getMe', timeout=5)
                payload = r.json() if r.ok else {}
                BOT_USERNAME = str((payload.get('result') or {}).get('username') or '').strip().lstrip('@')
            except Exception:
                BOT_USERNAME = ''
        if not BOT_USERNAME:
            raise ValueError('Не удалось получить ссылку. Повтори через несколько секунд.')
        link = f'https://t.me/{BOT_USERNAME}?start=ref_{int(user["id"])}'
        return jsonify({'ok':True,'count':stats['count'],'earned':stats['earned'],'reward':7500,'link':link})
    except Exception as e:
        return error(e,400)

@app.post('/api/leaderboard')
def api_leaderboard():
    try:
        # Важно: рейтинг не привязан к текущей игровой сессии.
        # Даже после logout аккаунт остаётся в users и виден в топе.
        current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        sort_by = p.get('sort_by', 'cases')
        if sort_by not in ('cases', 'balance', 'winnings'):
            sort_by = 'cases'
        rows = database.get_leaderboard(sort_by, 15)
        # Цвет имени хранится в users.profile_name_style. Старый JSON оставляем
        # только как fallback для совместимости со старыми данными.
        styles = _load_name_styles()
        for r in rows:
            db_style = str(r.get('profile_name_style') or '').strip().lower()
            r['profile_name_style'] = db_style or styles.get(str(r.get('user_id')), 'default')
        return jsonify({'ok': True, 'leaderboard': rows})
    except Exception as e:
        return error(e, 400)

@app.post('/api/profile/view')
def api_profile_view():
    try:
        # Профиль публичный: авторизация Telegram нужна только для доступа
        # к самому приложению, но профиль не зависит от того, залогинен ли
        # владелец просматриваемого аккаунта.
        current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        uid = int(p.get('user_id', 0) or 0)
        if not uid:
            raise ValueError('Пользователь не указан')
        u = database.get_user(uid)
        if not u or int(u.get('banned') or 0) or int(u.get('shadow_banned') or 0):
            raise ValueError('Профиль не найден')
        return jsonify({'ok': True, 'user': public(u) | {
            'cube_count': len(database.get_cubes(uid)),
            'battles': int(u.get('battles_played') or 0),
            'wins': int(u.get('total_wins') or 0)
        }})
    except Exception as e:
        return error(e, 400)

@app.post('/api/daily/status')
def api_daily_status():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        ledger = _load_bonus_12h()
        row = ledger.get(str(user['id'])) if isinstance(ledger.get(str(user['id'])), dict) else {}
        last = float(row.get('last_claim', 0) or 0)
        next_at = float(row.get('next_claim_at', 0) or 0)
        streak_day = min(7, max(0, int(row.get('streak_day', 0) or 0)))
        claims = min(2, max(0, int(row.get('claims_in_day', 0) or 0)))
        if last and time.time() - last > 24 * 60 * 60:
            streak_day, claims = 0, 0
        fresh = database.get_user(user['id'])
        premium_until = fresh.get('premium_until')
        is_premium = bool(premium_until) and str(premium_until) > datetime.now().isoformat()
        rewards = [1000,1500,2000,2500,3000,4000,5000]
        day = max(1, streak_day or 1)
        reward = rewards[day-1] * (2 if is_premium else 1)
        return jsonify({'ok':True,'last_daily':int(last*1000) if last else 0,'next_daily':int(next_at*1000) if next_at else 0,'streak':streak_day,'claims_in_day':claims,'reward':reward,'premium':is_premium})
    except Exception as e:
        return error(e,400)

@app.post('/api/daily')
def api_daily():
    try:
        user, u = current(request.get_json(silent=True) or {})
        fresh = database.get_user(user['id'])
        is_premium = bool(fresh.get('premium_until')) and str(fresh['premium_until']) > datetime.now().isoformat()
        # Base reward is determined by the server-side streak ledger. Premium doubles it.
        result = claim_bonus_12h(user['id'], is_premium)
        farm_action=database.evaluate_economy_farm(user['id'])
        return jsonify({'ok': True, 'balance': result['balance'], 'reward': result['reward'], 'streak': result['streak'], 'claims_in_day': result['claims_in_day'], 'premium': is_premium, 'last_daily': result['last_daily'], 'next_daily': result['next_daily'], 'farm_action': farm_action})
    except Exception as e:
        return error(e, 400)

VALID_RARITIES = {'common', 'rare', 'epic', 'legendary', 'mythic', 'divine', 'emerald'}

@app.post('/api/cube/open-batch')
def api_cube_open_batch():
    """Open 2-3 cases atomically; the response is the sole authoritative result for the UI."""
    try:
        user, u = current(request.get_json(silent=True) or {})
        if u.get('banned'):
            return error(ValueError('Аккаунт заблокирован'), 403)
        p = request.get_json(silent=True) or {}
        request_id = str(p.get('request_id') or '').strip()
        if not request_id:
            raise ValueError('Не указан request_id')
        case_id = str(p.get('case_id') or '').strip()
        case = CASES_BY_ID.get(case_id)
        if not case:
            legacy_name = str(p.get('case_name') or p.get('name') or '').strip()
            if legacy_name:
                for candidate in CASES_BY_ID.values():
                    if str(candidate.get('name') or '').strip() == legacy_name:
                        case = candidate
                        case_id = str(candidate['id'])
                        break
        if not case:
            raise ValueError('Неизвестный кейс')
        qty = max(1, min(3, int(p.get('qty') or 1)))
        price = int(case['price'])
        use_key = bool(p.get('use_key'))
        if use_key:
            consume_case_key(user['id'], case_id, qty)

        drops = []
        for _ in range(qty):
            items_for_pick = database.adjust_case_items_for_bad_luck(case['items'], user['id'])
            item = weighted_pick({**case, 'items': items_for_pick})
            drops.append({
                'name': str(item['name']),
                'rarity': str(item['rarity']),
                'value': int(item['value']),
                'case_id': case_id
            })

        result = database.case_open_batch(user['id'], 0 if use_key else price, drops, request_id=request_id, case_id=case_id)
        if result.get('duplicate'):
            fresh = database.get_user(user['id']) or {}
            return jsonify({
                'ok': True,
                'balance': result['balance'],
                'cubes': database.get_cubes(user['id']),
                'keys': [],
                'keys_added': [],
                'cases_opened': fresh.get('cases_opened', 0),
                'best_drops': json.loads(fresh.get('best_drops') or '[]'),
                'drops': result['drops'],
                'farm_action': None,
                'duplicate': True
            })
        new_balance = result['balance']

        # Keys are disabled. They are no longer generated or exposed.
        keys_added = []

        try:
            existing = json.loads((database.get_user(user['id']) or {}).get('best_drops') or '[]')
        except Exception:
            existing = []
        existing = [d for d in existing if isinstance(d, dict)]
        now_ms = datetime.now().timestamp() * 1000
        for drop in drops:
            existing.append({
                'name': drop['name'], 'rarity': drop['rarity'], 'value': drop['value'],
                'time': now_ms, 'source_case_id': case_id
            })
            try:
                database.add_recent_win(user['id'], effective_name(u), drop['name'], drop['rarity'])
            except Exception:
                pass
        existing.sort(key=lambda d: int(d.get('value') or 0), reverse=True)
        database.set_best_drops(user['id'], existing[:10])

        fresh = database.get_user(user['id'])
        farm_action = database.evaluate_economy_farm(user['id'])
        return jsonify({
            'ok': True,
            'balance': new_balance,
            'cubes': database.get_cubes(user['id']),
            'keys': [],
            'keys_added': keys_added,
            'cases_opened': fresh.get('cases_opened', 0),
            'best_drops': json.loads(fresh.get('best_drops') or '[]'),
            'drops': drops,
            'farm_action': farm_action
        })
    except Exception as e:
        return error(e, 400)

@app.post('/api/cube/open')
def api_cube_open():
    try:
        user, u = current(request.get_json(silent=True) or {})
        if u.get('banned'):
            return error(ValueError('Аккаунт заблокирован'), 403)
        p = request.get_json(silent=True) or {}
        # SECURITY: цена и награда кейса НИКОГДА не берутся из браузера.
        # Браузер передаёт только идентификатор кейса; сервер сам выбирает
        # цену и предмет из своего каталога криптографическим RNG.
        case_id = str(p.get('case_id') or p.get('source_case_id') or '').strip()
        case = CASES_BY_ID.get(case_id)
        # Backward compatibility for an already-cached Telegram WebView:
        # older clients sent the case name instead of case_id. We only use
        # the name to resolve a server-side catalog entry; price/reward from
        # the client are never trusted.
        if not case:
            legacy_name = str(p.get('case_name') or p.get('name') or '').strip()
            if legacy_name:
                for candidate in CASES_BY_ID.values():
                    if str(candidate.get('name') or '').strip() == legacy_name:
                        case = candidate
                        case_id = str(candidate['id'])
                        break
        if not case:
            raise ValueError('Неизвестный кейс')
        price = int(case['price'])
        request_id = str(p.get('request_id') or '').strip()
        if not request_id:
            raise ValueError('Не указан request_id')
        use_key = bool(p.get('use_key'))
        if use_key:
            consume_case_key(user['id'], case_id, 1)
        items_for_pick = database.adjust_case_items_for_bad_luck(case['items'], user['id'])
        item = weighted_pick({**case, 'items': items_for_pick})
        name = str(item['name'])
        rarity = str(item['rarity'])
        value = int(item['value'])
        result = database.case_open_batch(user['id'], 0 if use_key else price, [{'name': name, 'rarity': rarity, 'value': value, 'case_id': case_id}], request_id=request_id, case_id=case_id)
        if result.get('duplicate'):
            fresh = database.get_user(user['id']) or {}
            saved_drop = (result.get('drops') or [{}])[0]
            return jsonify({'ok': True, 'balance': result['balance'], 'drop': saved_drop, 'cubes': database.get_cubes(user['id']), 'keys': [], 'key': None, 'cases_opened': fresh.get('cases_opened',0), 'best_drops': json.loads(fresh.get('best_drops') or '[]'), 'duplicate': True})
        new_balance = result['balance']
        try:
            existing = json.loads((database.get_user(user['id']) or {}).get('best_drops') or '[]')
        except Exception:
            existing = []
        existing = [d for d in existing if isinstance(d, dict)]
        existing.append({'name': name, 'rarity': rarity, 'value': value,
                         'time': datetime.now().timestamp() * 1000,
                         'source_case_id': case_id})
        existing.sort(key=lambda d: int(d.get('value') or 0), reverse=True)
        database.set_best_drops(user['id'], existing[:10])
        # Recent drops are also written by the server; client cannot forge them.
        try:
            database.add_recent_win(user['id'], effective_name(u), name, rarity)
        except Exception:
            pass
        key = server_maybe_drop_key(user['id'], price)
        cubes = database.get_cubes(user['id'])
        fresh = database.get_user(user['id'])
        farm_action = database.evaluate_economy_farm(user['id'])
        return jsonify({'ok': True, 'balance': new_balance, 'cubes': cubes,
                        'keys': [],
                        'key': key,
                        'cases_opened': fresh.get('cases_opened', 0),
                        'best_drops': json.loads(fresh.get('best_drops') or '[]'),
                        'drop': {'name':name,'rarity':rarity,'value':value,'case_id':case_id},
                        'farm_action': farm_action})
    except Exception as e:
        return error(e, 400)

@app.post('/api/cube/lock')
def api_cube_lock():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        idx=int(p.get('index',-1)); locked=bool(p.get('locked',True)); cubes=database.get_cubes(user['id'])
        if idx<0 or idx>=len(cubes): raise ValueError('Куб не найден в инвентаре')
        database.set_cube_locked(user['id'],int(cubes[idx]['id']),locked)
        return jsonify({'ok':True,'locked':locked,'cubes':database.get_cubes(user['id'])})
    except Exception as e: return error(e,400)

@app.post('/api/cube/sell')
def api_cube_sell():
    try:
        user, u = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        idx = int(p.get('index', -1))
        cubes = database.get_cubes(user['id'])
        if idx < 0 or idx >= len(cubes):
            raise ValueError('Куб не найден в инвентаре')
        cube_id = cubes[idx]['id']
        balance, gain = database.sell_cube(user['id'], cube_id)
        fresh_cubes = database.get_cubes(user['id'])
        return jsonify({'ok': True, 'balance': balance, 'gain': gain, 'cubes': fresh_cubes})
    except Exception as e:
        return error(e, 400)

@app.post('/api/cube/sell-all')
def api_cube_sell_all():
    try:
        user, u = current(request.get_json(silent=True) or {})
        balance, gain, count = database.sell_all_cubes(user['id'])
        return jsonify({'ok': True, 'balance': balance, 'gain': gain, 'sold': count, 'cubes': database.get_cubes(user['id'])})
    except Exception as e:
        return error(e, 400)

@app.post('/api/trade/create')
def api_trade_create():
    try:
        user, u = current(request.get_json(silent=True) or {})
        if u and u.get('banned'):
            raise ValueError('Аккаунт заблокирован')
        p = request.get_json(silent=True) or {}
        cube_index = int(p.get('cube_index', -1))
        to_user = int(p.get('to_user', 0))
        request_name = str(p.get('request_name', ''))[:80]
        if to_user == user['id']:
            raise ValueError('Нельзя предложить обмен самому себе')
        cubes = database.get_cubes(user['id'])
        if cube_index < 0 or cube_index >= len(cubes):
            raise ValueError('Куб не найден')
        database.create_trade(user['id'], to_user, cubes[cube_index]['id'], request_name)
        return jsonify({'ok': True})
    except Exception as e:
        return error(e, 400)

@app.post('/api/trade/list')
def api_trade_list():
    try:
        user, u = current(request.get_json(silent=True) or {})
        incoming, outgoing = database.get_trades_for(user['id'])
        return jsonify({'ok': True, 'incoming': incoming, 'outgoing': outgoing})
    except Exception as e:
        return error(e, 400)

@app.post('/api/trade/respond')
def api_trade_respond():
    try:
        user, u = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        trade_id = int(p.get('trade_id'))
        accept = bool(int(p.get('accept', 0)))
        database.respond_trade(trade_id, user['id'], accept)
        return jsonify({'ok': True, 'cubes': database.get_cubes(user['id'])})
    except Exception as e:
        return error(e, 400)

@app.post('/api/trade/cancel')
def api_trade_cancel():
    try:
        user, u = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        database.cancel_trade(int(p.get('trade_id')), user['id'])
        return jsonify({'ok': True})
    except Exception as e:
        return error(e, 400)

@app.post('/api/keys/combine')
def api_keys_combine():
    try:
        user, u = current(request.get_json(silent=True) or {})
        if u.get('banned'):
            return error(ValueError('Аккаунт заблокирован'), 403)
        p = request.get_json(silent=True) or {}
        key_id = str(p.get('key_id') or '')
        result = database.combine_keys_atomic(user['id'], key_id)
        return jsonify({'ok': True, **result})
    except Exception as e:
        return error(e, 400)


@app.post('/api/upgrade')
def api_upgrade():
    try:
        user, u = current(request.get_json(silent=True) or {})
        if u.get('banned'):
            return error(ValueError('Аккаунт заблокирован'), 403)
        p = request.get_json(silent=True) or {}
        source_kind = 'key' if str(p.get('source_kind','cube')) == 'key' else 'cube'
        target_kind = 'key' if str(p.get('target_kind','cube')) == 'key' else 'cube'
        target_name = str(p.get('target_name', ''))[:80]
        target_rarity = str(p.get('target_rarity', ''))
        target_value = max(1, min(50000000, int(p.get('target_value', 1))))
        target_key_id = str(p.get('target_key_id') or '')
        stake = max(0, int(p.get('stake', 0)))

        if source_kind == 'key':
            keys = database.get_keys(user['id'])
            key_id = str(p.get('source_key_id') or '')
            if not key_id:
                raise ValueError('Ключ не указан')
            result = database.upgrade_key_atomic(
                user['id'], key_id, target_name, target_rarity, target_value, stake
            )
        else:
            cube_id_raw = p.get('cube_id')
            if cube_id_raw is not None and str(cube_id_raw).strip():
                cube_id = int(cube_id_raw)
            else:
                cubes = database.get_cubes(user['id'])
                idx = int(p.get('index', -1))
                if idx < 0 or idx >= len(cubes):
                    raise ValueError('Куб не найден в инвентаре — обнови страницу и попробуй снова')
                cube_id = int(cubes[idx]['id'])
            result = database.upgrade_cube_atomic(
                user['id'], cube_id, target_name, target_rarity, target_value,
                stake, target_kind=target_kind, target_key_id=target_key_id
            )
        return jsonify({'ok': True, **result})
    except Exception as e:
        return error(e, 400)


@app.post('/api/profile/name-style')
def api_profile_name_style():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        key = str(p.get('style') or 'default').strip().lower()
        if key not in _NAME_STYLE_KEYS:
            raise ValueError('Неизвестный цвет имени')
        database.set_profile_name_style(user['id'], key)
        return jsonify({'ok': True, 'profile_name_style': key})
    except Exception as e:
        return error(e, 400)

@app.post('/api/profile/frame')
def api_profile_frame():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p=request.get_json(silent=True) or {}
        key=str(p.get('frame') or 'default').strip().lower()
        allowed={'default','gold','emerald','ruby','amethyst','ice','obsidian','neon','ghost','premium'}
        if key not in allowed:
            raise ValueError('Неизвестная рамка')
        database.set_profile_frame(user['id'], key)
        return jsonify({'ok':True,'profile_frame':key})
    except Exception as e:
        return error(e,400)

@app.post('/api/profile/nickname')
def api_profile_nickname():
    # Смена ника отключена. Ник задаётся только при регистрации.
    return error(ValueError('Смена ника отключена.'), 403)

@app.post('/api/clicker/claim')
def api_clicker_claim():
    try:
        user, u = current(request.get_json(silent=True) or {})
        if u.get('banned'):
            return error(ValueError('Аккаунт заблокирован'), 403)
        p = request.get_json(silent=True) or {}
        request_id = str(p.get('request_id') or '').strip()
        result = database.clicker_claim(user['id'], request_id, reward=1, x=p.get('x',63), y=p.get('y',63), area_size=48)
        return jsonify({'ok': True, **result, 'reward': 1})
    except Exception as e:
        return error(e, 400)

@app.post('/api/drops/add')
def api_drops_add():
    # SECURITY: recent drops are generated only by authoritative server actions
    # (case opening, battles, etc.). The browser cannot inject fake wins.
    try:
        user, _ = current(request.get_json(silent=True) or {})
        return jsonify({'ok': True, 'serverOnly': True,
                        'drops': database.get_recent_wins(3)})
    except Exception as e:
        return error(e, 400)

@app.post('/api/drops/recent')
def api_drops_recent():
    try:
        current(request.get_json(silent=True) or {})
        rows = database.get_recent_wins(3)
        return jsonify({'ok': True, 'drops': rows})
    except Exception as e:
        return error(e, 400)

@app.post('/api/game/tower/active')
def api_tower_active():
    try:
        user,_=current(request.get_json(silent=True) or {})
        return jsonify({'ok':True,**database.get_active_tower_round(int(user['id']))})
    except Exception as e: return error(e,400)

@app.post('/api/game/tower/start')
def api_tower_start():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r't_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        raw_bet=p.get('bet')
        if raw_bet is None or str(raw_bet).strip()=='': raise ValueError('Укажи ставку в орбах')
        try: bet=int(raw_bet)
        except (TypeError, ValueError): raise ValueError('Некорректная ставка')
        return jsonify({'ok':True,**database.start_tower_round(rid,int(user['id']),bet)})
    except Exception as e: return error(e,400)

@app.post('/api/game/tower/continue')
def api_tower_continue():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r't_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True,**database.continue_tower_round(rid,int(user['id']))})
    except Exception as e: return error(e,400)

@app.post('/api/game/tower/pick')
def api_tower_pick():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r't_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True,**database.pick_tower_door(rid,int(user['id']),int(p.get('door')))})
    except Exception as e: return error(e,400)

@app.post('/api/game/tower/cashout')
def api_tower_cashout():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r't_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True,**database.cashout_tower_round(rid,int(user['id']))})
    except Exception as e: return error(e,400)

@app.post('/api/game/mines/active')
def api_mines_active():
    try:
        user,_=current(request.get_json(silent=True) or {})
        return jsonify({'ok':True,**database.get_active_mines_round(int(user['id']))})
    except Exception as e:
        return error(e,400)

@app.post('/api/game/mines/start')
def api_mines_start():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip();
        if not re.fullmatch(r'm_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        raw_bet=p.get('bet')
        if raw_bet is None or str(raw_bet).strip()=='':
            raise ValueError('Укажи ставку в орбах')
        try:
            bet=int(raw_bet)
        except (TypeError, ValueError):
            raise ValueError('Некорректная ставка')
        return jsonify({'ok':True,**database.start_mines_round(rid,int(user['id']),bet)})
    except Exception as e: return error(e,400)

@app.post('/api/game/mines/continue')
def api_mines_continue():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r'm_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True,**database.continue_mines_round(rid,int(user['id']))})
    except Exception as e: return error(e,400)

@app.post('/api/game/mines/pick')
def api_mines_pick():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip();
        if not re.fullmatch(r'm_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True,**database.pick_mines_cell(rid,int(user['id']),int(p.get('cell')))})
    except Exception as e: return error(e,400)

@app.post('/api/game/mines/cashout')
def api_mines_cashout():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip();
        if not re.fullmatch(r'm_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True,**database.cashout_mines_round(rid,int(user['id']))})
    except Exception as e: return error(e,400)

@app.post('/api/game/bomber/start')
def api_bomber_start():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r'b_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True,**database.start_bomber_round(rid,int(user['id']),int(p.get('bet')))})
    except Exception as e: return error(e,400)

@app.post('/api/game/bomber/pick')
def api_bomber_pick():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r'b_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True,**database.pick_bomber_cell(rid,int(user['id']),int(p.get('cell')))})
    except Exception as e: return error(e,400)

@app.post('/api/game/bomber/cashout')
def api_bomber_cashout():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        rid=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r'b_[A-Za-z0-9_-]{16,80}',rid): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True,**database.cashout_bomber_round(rid,int(user['id']))})
    except Exception as e: return error(e,400)

@app.post('/api/game/rush/start')
def api_rush_start():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        round_id = str(p.get('round_id') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', round_id):
            raise ValueError('Некорректный ID игры')
        raw_bet=p.get('bet')
        if isinstance(raw_bet,bool): raise ValueError('Ставка должна быть целым числом')
        bet=int(raw_bet)
        if bet<1 or bet>RUSH_MAX_BET: raise ValueError(f'Ставка должна быть от 1 до {RUSH_MAX_BET:,}'.replace(',',' '))
        result=database.start_rush_round(round_id,int(user['id']),bet)
        return jsonify({'ok':True, **result})
    except Exception as e: return error(e,400)

@app.post('/api/game/rush/status')
def api_rush_status():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p=request.get_json(silent=True) or {}
        round_id=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', round_id): raise ValueError('Некорректный ID игры')
        return jsonify({'ok':True, **database.get_rush_status(round_id,int(user['id']))})
    except Exception as e: return error(e,400)

@app.post('/api/game/rush/cashout')
def api_rush_cashout():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p=request.get_json(silent=True) or {}
        round_id=str(p.get('round_id') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', round_id): raise ValueError('Некорректный ID игры')
        result=database.cashout_rush_round(round_id,int(user['id']))
        farm_action=database.evaluate_economy_farm(user['id'])
        if farm_action: result['farm_action']=farm_action
        return jsonify({'ok':True, **result})
    except Exception as e: return error(e,400)

# --- ADMIN ---
@app.post('/api/admin/session')
def admin_session():
    try:
        token,expires_at=create_admin_session(request.get_json(silent=True) or {})
        return jsonify({'ok':True,'adminSession':token,'expiresAt':expires_at})
    except PermissionError as e: return error(e,403)
    except Exception as e: return error(e,400)

@app.post('/api/admin/ui')
def admin_ui():
    try:
        # Полный HTML админки выдаётся только после серверной проверки
        # Telegram ID и короткой админ-сессии. Обычный игрок этот фрагмент
        # вообще не получает.
        admin(request.get_json(silent=True) or {})
        path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin_fragment.html')
        with open(path, 'r', encoding='utf-8') as f:
            html=f.read()
        return jsonify({'ok':True,'html':html})
    except PermissionError as e: return error(e,403)
    except Exception as e: return error(e,400)

@app.post('/api/admin/logout')
def admin_logout():
    try:
        admin(request.get_json(silent=True) or {})
        database.revoke_admin_sessions(ADMIN_TELEGRAM_ID)
        return jsonify({'ok':True})
    except PermissionError as e: return error(e,403)
    except Exception as e: return error(e,400)

@app.post('/api/admin/users')
def admin_users():
    try:
        admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        search = str(p.get('search', ''))[:80]
        # No hard 100-user cap. The admin UI receives the real total separately.
        total = database.count_users(search)
        rows = []
        for u in database.list_users(search):
            x = public(u)
            if x:
                x['shadow_banned'] = bool(int(u.get('shadow_banned') or 0))
                x['cube_count'] = len(x['cubes'])
                rows.append(x)
        return jsonify({'ok': True, 'users': rows, 'total': total, 'total_players': database.count_users()})
    except PermissionError as e:
        return error(e, 403)
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/stats')
def admin_stats():
    try:
        admin(request.get_json(silent=True) or {})
        return jsonify({'ok': True, 'players': database.count_users()})
    except PermissionError as e:
        return error(e, 403)
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/money')
def admin_money():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        amount = int(p['amount'])
        new = database.update_balance(target, amount)
        database.add_admin_log(a[0]['id'], target, 'Валюта', f'{amount:+d}')
        return jsonify({'ok': True, 'balance': new, 'user': public(database.get_user(target))})
    except Exception as e:
        return error(e, 400)

def remove_premium_and_buffs(target):
    """Снимает Premium и удаляет только баффы, относящиеся к Premium.

    Новые database.py могут предоставить remove_premium(). Для старых версий
    оставляем совместимый fallback через grant_premium() с датой в прошлом.
    """
    remover = getattr(database, 'remove_premium', None)
    if callable(remover):
        remover(target)
    else:
        grant = getattr(database, 'grant_premium', None)
        if not callable(grant):
            raise RuntimeError('В database.py нет функции снятия Premium')
        # Совместимость со старой БД: grant_premium(user, days) выставляет
        # дату окончания. Большой отрицательный срок гарантированно истекает.
        grant(target, -36500)

    buffs = database.get_buffs(target) or []
    premium_buffs = []
    removed = 0
    for buff in buffs:
        btype = str((buff or {}).get('type') or '').strip().lower()
        bname = str((buff or {}).get('name') or '').strip().lower()
        bdesc = str((buff or {}).get('description') or '').strip().lower()
        is_premium = (
            btype == 'premium_drop' or
            'premium' in btype or
            'premium' in bname or
            'premium' in bdesc
        )
        if is_premium:
            removed += 1
        else:
            premium_buffs.append(buff)
    database.set_buffs(target, premium_buffs)
    return removed


@app.post('/api/admin/tech-break')
def admin_tech_break_global():
    try:
        # Do not require a short-lived adminSession here. Prefer the normal
        # auth token (the same token already used successfully by /api/me),
        # with Telegram initData as a fallback. This prevents a false
        # "check your internet" message when Telegram temporarily omits initData.
        p=request.get_json(silent=True) or {}
        _admin_rate_check(request.remote_addr)
        admin_uid=None
        if str(p.get('authToken') or '').strip():
            admin_user,_ = current(p)
            admin_uid=int(admin_user['id'])
        else:
            tg=verify_init_data(p.get('initData',''))
            admin_uid=int(tg.get('id') or 0)
        if admin_uid != ADMIN_TELEGRAM_ID:
            raise PermissionError('Нет доступа')
        action=str(p.get('action') or '').strip().lower()
        if action=='enable':
            reason=str(p.get('reason') or '').strip()[:500]
            if len(reason)<3: raise ValueError('Укажи причину тех. перерыва')
            database.set_global_tech_break(True,reason)
            return jsonify({'ok':True,'global_tech_break':database.get_global_tech_break()})
        if action=='clear':
            database.set_global_tech_break(False,'')
            return jsonify({'ok':True,'global_tech_break':database.get_global_tech_break()})
        raise ValueError('Неизвестное действие')
    except PermissionError as e:
        return error(e,403)
    except Exception as e:
        return error(e,400)

@app.post('/api/admin/account')
def admin_account_action():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p.get('user_id', 0))
        action = str(p.get('action', '')).strip()
        target_user = database.get_user(target)
        if not target_user:
            raise ValueError('Пользователь не найден')
        actor = (a[0].get('username') or '').strip().lower()
        target_name = (target_user.get('username') or '').strip().lower()
        if target_name == 'kuro' and actor != 'kuro' and action in {'delete','ban','shadow','username','password','2fa','sessions'}:
            raise PermissionError('Этот аккаунт защищён')

        if action == 'username':
            name = str(p.get('username','')).strip()
            database.set_username_admin(target, name)
            details = f'Новый ник: {name}'
        elif action == 'password':
            pw = str(p.get('password',''))
            if len(pw) < 4: raise ValueError('Пароль минимум 4 символа')
            database.set_password_hash(target, hashlib.sha256(pw.encode()).hexdigest())
            details = 'Пароль изменён'
        elif action == '2fa':
            enabled = bool(p.get('enabled', False))
            database.set_two_factor(target, enabled)
            details = '2FA включена' if enabled else '2FA отключена и коды сброшены'
        elif action == 'sessions':
            database.set_sessions_expired(target)
            details = 'Все активные сессии завершены'
        elif action == 'balance':
            value = max(0, int(p.get('value', 0)))
            database.set_balance(target, value)
            details = f'Баланс установлен: {value}'
        elif action == 'stars':
            value = max(0, int(p.get('value', 0)))
            database.set_stars(target, value)
            details = f'Звёзды установлены: {value}'
        elif action == 'level':
            value = max(0, int(p.get('value', 0)))
            database.update_level(target, value)
            details = f'Уровень установлен: {value}'
        elif action == 'remove_premium':
            removed_buffs = remove_premium_and_buffs(target)
            details = f'Premium снят; Premium-баффов удалено: {removed_buffs}'
        elif action == 'clear_buffs':
            database.clear_user_buffs(target)
            details = 'Активные баффы очищены'
        elif action == 'clear_drops':
            database.clear_best_drops(target)
            details = 'Лучшие дропы очищены'
        elif action == 'tech_break':
            reason = str(p.get('reason','')).strip()[:500]
            if not reason: raise ValueError('Укажи причину тех. перерыва')
            database.set_global_tech_break(True, reason)
            details = f'Глобальный тех. перерыв включён: {reason}'
        elif action == 'clear_tech_break':
            database.set_global_tech_break(False, '')
            details = 'Глобальный тех. перерыв снят'
        elif action == 'avatar':
            database.set_avatar_admin(target, p.get('avatar',''))
            details = 'Аватар обновлён' if p.get('avatar') else 'Аватар очищен'
        elif action == 'verify_password':
            details = 'Проверка аккаунта'
        else:
            raise ValueError('Неизвестное действие')
        database.add_admin_log(a[0]['id'], target, 'Аккаунт', details)
        fresh = database.get_user(target)
        result = {'ok': True, 'user': public(fresh)}
        if action == 'verify_password':
            result['password_set'] = bool(fresh.get('password_hash'))
            result['two_factor_enabled'] = bool(fresh.get('two_factor_enabled'))
        return jsonify(result)
    except PermissionError as e:
        return error(e, 403)
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/password')
def admin_password():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        pw = str(p.get('password', ''))
        if len(pw) < 4:
            raise ValueError('Пароль минимум 4 символа')
        h = hashlib.sha256(pw.encode()).hexdigest()
        database.set_password_hash(target, h)
        database.add_admin_log(a[0]['id'], target, 'Смена пароля', 'Пароль изменён')
        return jsonify({'ok': True})
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/ban')
def admin_ban():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        val = bool(int(p.get('banned', 1)))
        reason = str(p.get('reason') or '').strip()[:500]
        if val and not reason:
            raise ValueError('Укажи причину бана')
        acting_username = (a[0].get('username') or '').strip().lower()
        target_user = database.get_user(target)
        target_username = (target_user.get('username') or '').strip().lower() if target_user else ''
        # kuro неприкосновеннен для всех, кроме самого себя
        if target_username == 'kuro' and acting_username != 'kuro':
            raise PermissionError('Этого игрока нельзя заблокировать')
        database.set_ban(target, val, reason if val else '')
        database.add_admin_log(a[0]['id'], target, 'Бан' if val else 'Разбан', reason if val else '')
        return jsonify({'ok': True})
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/shadow')
def admin_shadow():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        val = bool(int(p.get('shadow_banned', 1)))
        database.set_shadow_ban(target, val)
        if val:
            database.add_notification(target,'Теневой бан','На аккаунт наложен теневой бан')
        database.add_admin_log(a[0]['id'], target, 'Теневой бан' if val else 'Снятие теневого бана')
        return jsonify({'ok': True})
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/cube')
def admin_cube():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        name = str(p.get('name', 'Куб')).strip()[:80]
        rarity = str(p.get('rarity', 'rare'))
        value = max(0, int(p.get('value', 0)))
        if rarity not in {'common','rare','epic','legendary','mythic','divine'}:
            rarity = 'rare'
        database.add_cube(target, name, rarity, value)
        database.add_admin_log(a[0]['id'], target, 'Выдача куба', f'{name}/{rarity}/{value}')
        return jsonify({'ok': True, 'user': public(database.get_user(target))})
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/premium')
def admin_premium():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        days = max(1, int(p.get('days', 7)))
        database.grant_premium(target, days)
        database.add_admin_log(a[0]['id'], target, 'Премиум', f'{days} дн.')
        return jsonify({'ok': True, 'user': public(database.get_user(target))})
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/reset')
def admin_reset():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        database.reset_progress(target)
        database.add_admin_log(a[0]['id'], target, 'Сброс прогресса')
        return jsonify({'ok': True})
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/creator-badge')
def admin_creator_badge():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        target=int(p['user_id']); enabled=bool(p.get('enabled',True))
        database.set_creator_badge(target,enabled)
        database.add_admin_log(a[0]['id'],target,'Создатель','выдан' if enabled else 'снят')
        return jsonify({'ok':True,'user':public(database.get_user(target))})
    except Exception as e: return error(e,403 if isinstance(e,PermissionError) else 400)

@app.post('/api/admin/tester-badge')
def admin_tester_badge():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        target=int(p['user_id']); enabled=bool(p.get('enabled',True))
        database.set_tester_badge(target,enabled)
        database.add_admin_log(a[0]['id'],target,'Тестер','выдан' if enabled else 'снят')
        return jsonify({'ok':True,'user':public(database.get_user(target))})
    except Exception as e: return error(e,403 if isinstance(e,PermissionError) else 400)

@app.post('/api/admin/custom-status')
def admin_custom_status():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p.get('user_id', 0))
        target_user = database.get_user(target)
        if not target_user:
            raise ValueError('Пользователь не найден')

        emoji = str(p.get('emoji') or '').strip()[:8]
        text = str(p.get('text') or '').strip()[:32]
        gradient = bool(p.get('gradient', False))
        color1 = str(p.get('color1') or '#f0c674').strip()[:7]
        color2 = str(p.get('color2') or '#ffffff').strip()[:7]
        database.set_custom_status(target, emoji, text, gradient, color1, color2)
        details = f'{emoji} {text}'.strip() if (emoji or text) else 'статус снят'
        database.add_admin_log(a[0]['id'], target, 'Свой статус', details)
        return jsonify({'ok': True, 'user': public(database.get_user(target))})
    except PermissionError as e:
        return error(e, 403)
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/buff')
def admin_buff():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        target=int(p['user_id']); hours=max(1,min(720,int(p.get('duration_hours',24))))
        btype=str(p.get('buff_type') or p.get('type') or 'luck')[:40]
        now=datetime.now(timezone.utc); expires=(now+__import__('datetime').timedelta(hours=hours)).isoformat()
        buffs=database.get_buffs(target); buffs=[b for b in buffs if not b.get('expires_at') or str(b.get('expires_at'))>now.isoformat()]
        desc = 'Шанс редких наград, побед и удачных выпадений уменьшен на 80%' if btype == 'bad_luck' else 'Выдан администратором'
        buffs.append({'type':btype,'name':btype,'description':desc,'expires_at':expires})
        database.set_buffs(target,buffs)
        database.add_admin_log(a[0]['id'],target,'Бафф',f'{btype}/{hours}ч')
        return jsonify({'ok':True,'user':public(database.get_user(target))})
    except Exception as e: return error(e,400)

@app.post('/api/admin/delete-user')
def admin_delete_user():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        target=int(p['user_id']); target_user=database.get_user(target)
        if not target_user: raise ValueError('Пользователь не найден')
        target_name=(target_user.get('username') or '').strip().lower(); actor=(a[0].get('username') or '').strip().lower()
        if target_name=='kuro' and actor!='kuro': raise PermissionError('Этого игрока нельзя удалить')
        reason=str(p.get('reason') or '').strip()[:500]
        if not reason:
            raise ValueError('Укажи причину удаления аккаунта')
        database.create_moderation_notice(target, 'delete', reason, 1)
        database.delete_user(target)
        database.add_admin_log(a[0]['id'],target,'Удаление аккаунта',reason)
        return jsonify({'ok':True})
    except Exception as e: return error(e,403 if isinstance(e,PermissionError) else 400)

@app.post('/api/admin/ip-ban')
def admin_ip_ban():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        ip=str(p.get('ip') or '').strip()
        reason=str(p.get('reason') or '').strip()[:500]
        permanent=bool(p.get('permanent'))
        minutes=max(1,min(30*24*60,int(p.get('minutes') or IP_BAN_DEFAULT_MINUTES)))
        if not reason: raise ValueError('Укажи причину IP-бана')
        database.set_ip_ban(ip,reason,minutes,permanent,a[0]['id'])
        database.add_admin_log(a[0]['id'],None,'IP-бан',f'{ip}: {reason}')
        return jsonify({'ok':True})
    except Exception as e: return error(e,403 if isinstance(e,PermissionError) else 400)

@app.post('/api/admin/ip-ban-user')
def admin_ip_ban_user():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        target=int(p['user_id'])
        target_user=database.get_user(target)
        if not target_user: raise ValueError('Пользователь не найден')
        ip=database.get_user_last_ip(target)
        if not ip: raise ValueError('У этого аккаунта ещё не зафиксирован IP-адрес')
        reason=str(p.get('reason') or '').strip()[:500]
        if not reason: raise ValueError('Укажи причину IP-бана')
        permanent=bool(p.get('permanent'))
        minutes=max(1,min(30*24*60,int(p.get('minutes') or IP_BAN_DEFAULT_MINUTES)))
        database.set_ip_ban(ip,reason,minutes,permanent,a[0]['id'])
        database.add_admin_log(a[0]['id'],target,'IP-бан аккаунта',f'{ip}: {reason}')
        return jsonify({'ok':True,'ip':ip,'user_id':target})
    except Exception as e: return error(e,403 if isinstance(e,PermissionError) else 400)

@app.post('/api/admin/ip-unban')
def admin_ip_unban():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        ip=str(p.get('ip') or '').strip()
        database.clear_ip_ban(ip)
        database.add_admin_log(a[0]['id'],None,'Снятие IP-бана',ip)
        return jsonify({'ok':True})
    except Exception as e: return error(e,403 if isinstance(e,PermissionError) else 400)

@app.post('/api/admin/logs')
def admin_logs():
    try:
        admin(request.get_json(silent=True) or {})
        return jsonify({'ok': True, 'logs': database.get_admin_logs()})
    except Exception as e:
        return error(e, 400)


# --- PROMO CODES ---
@app.post('/api/promo/redeem')
def promo_redeem():
    try:
        user,_=current(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        result=database.redeem_promo(p.get('code'),user['id']); farm_action=database.evaluate_economy_farm(user['id']); return jsonify({'ok':True,'result':result,'fullscreen_slides':result.get('fullscreen_slides',[]),'slide_rewards':result.get('slide_rewards',[]),'user':public(database.get_user(user['id'])),'farm_action':farm_action})
    except Exception as e: return error(e,400)

@app.post('/api/admin/promos')
def admin_promos():
    try: admin(request.get_json(silent=True) or {}); return jsonify({'ok':True,'promos':database.list_promos()})
    except Exception as e: return error(e,403 if isinstance(e,PermissionError) else 400)

@app.post('/api/admin/promo/create')
def admin_promo_create():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        slides = p.get('fullscreen_slides') or p.get('slides') or []
        if not isinstance(slides, list):
            raise ValueError('Картинки промокода должны быть списком')
        if len(slides) > 20:
            raise ValueError('Можно добавить не более 20 картинок')
        promo=database.create_promo(
            p.get('code'), p.get('reward_type','coins'), int(p.get('coins') or 0),
            int(p.get('vip_days') or 0), int(p.get('max_uses') or 1),
            a[0]['id'], fullscreen_slides=slides
        )
        database.add_admin_log(a[0]['id'],None,'Промокод',promo['code'])
        return jsonify({'ok':True,'promo':promo})
    except Exception as e: return error(e,403 if isinstance(e,PermissionError) else 400)

@app.post('/api/admin/promo/disable')
def admin_promo_disable():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        if not database.disable_promo(int(p.get('promo_id') or 0)): raise ValueError('Промокод не найден')
        database.add_admin_log(a[0]['id'],None,'Промокод отключён',str(p.get('promo_id'))); return jsonify({'ok':True})
    except Exception as e: return error(e,403 if isinstance(e,PermissionError) else 400)

# --- MARKET ---
@app.post('/api/mp/market')
def mp_market():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        items = database.get_shop_items_public(user['id'])
        return jsonify({'ok': True, 'items': items})
    except Exception as e:
        return error(e, 400)

@app.post('/api/mp/market/list')
def mp_market_list():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        if str(p.get('kind','')).lower() == 'key':
            raise ValueError('Продажа ключей в магазин отключена — ключи можно только улучшать')
        idx = int(p.get('index', -1))
        price = max(1, int(p.get('price', 0)))
        cubes = database.get_cubes(user['id'])
        if idx < 0 or idx >= len(cubes):
            raise ValueError('Куб не найден')
        cube = cubes[idx]
        if bool((cube.get('metadata') or {}).get('locked')):
            raise ValueError('Куб заморожен — сначала разблокируй его')
        item_id = database.list_shop_item_atomic(
            user['id'], 
            cube['name'], 
            cube.get('rarity', 'common'), 
            int(cube.get('value', 0)), 
            price, 
            'coins', 
            cube.get('id'), 
            idx
        )
        return jsonify({'ok': True, 'id': item_id, 'cubes': database.get_cubes(user['id'])})
    except Exception as e:
        return error(e, 400)

@app.post('/api/mp/market/cancel')
def mp_market_cancel():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        database.cancel_shop_item_atomic(int(p['item_id']), user['id'])
        return jsonify({'ok': True, 'cubes': database.get_cubes(user['id'])})
    except Exception as e:
        return error(e, 400)

@app.post('/api/mp/market/buy')
def mp_market_buy():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        result = database.buy_shop_item_atomic(int(p['item_id']), user['id'])
        return jsonify({'ok': True, **result})
    except Exception as e:
        return error(e, 400)

# --- BATTLES ---
@app.post('/api/mp/battles')
def mp_battles():
    try:
        user, u = current(request.get_json(silent=True) or {})
        bs = database.get_battles()
        for b in bs:
            b['viewer_id'] = user['id']
            b['is_joined'] = any(str(p['user']) == str(user['id']) for p in b.get('players', []))
            b['is_creator'] = str(b.get('creator')) == str(user['id'])
            for p in b.get('players', []):
                if not p.get('name') or p['name'] == 'Игрок':
                    u_info = database.get_user(p['user'])
                    if u_info:
                        p['name'] = u_info.get('username', f'Игрок_{p["user"]}')
        fresh = database.get_user(user['id'])
        return jsonify({'ok': True, 'battles': bs, 'balance': int(fresh.get('balance', 0)) if fresh else 0})
    except Exception as e:
        return error(e, 400)

@app.post('/api/mp/battles/create')
def battle_create():
    try:
        user, u = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        stake = max(1, int(p.get('stake', 1)))
        maxp = min(5, max(2, int(p.get('max', 2))))
        mode = p.get('mode', 'high') if p.get('mode') in ('high','random') else 'high'
        row = database.get_user(user['id'])
        if not row: raise ValueError('Пользователь не найден')
        bid = f"b{int(datetime.now(timezone.utc).timestamp()*1000)}_{user['id']}"
        username = effective_name(row)
        balance = database.create_battle_record(bid,user['id'],username,stake,maxp,mode)
        return jsonify({'ok':True,'balance':balance,'id':bid})
    except Exception as e:
        return error(e, 400)

@app.post('/api/mp/battles/join')
def battle_join():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        battle_id = str(p.get('battle_id', ''))
        if not battle_id:
            raise ValueError('ID батла не указан')
        battles = database.get_battles()
        battle = next((b for b in battles if b['id'] == battle_id), None)
        if not battle:
            raise ValueError('Батл не найден')
        if len(battle.get('players', [])) >= battle.get('max', 2):
            raise ValueError('Батл уже заполнен')
        u = database.get_user(user['id'])
        if u and u.get('banned'):
            raise ValueError('Вы заблокированы')
        username = effective_name(u)
        became_full = database.join_battle_record(battle_id, user['id'], username)
        fresh_user = database.get_user(user['id'])
        result = None
        if became_full:
            # Батл заполнился — розыгрыш запускается сразу же, без ручного нажатия «Играть»
            result = database.play_battle_record(battle_id, user['id'])
            fresh_user = database.get_user(user['id'])
        return jsonify({'ok': True, 'balance': fresh_user['balance'] if fresh_user else 0, 'result': result,
                         'cubes': database.get_cubes(user['id']) if result else None})
    except Exception as e:
        return error(e, 400)

@app.post('/api/mp/battles/cancel')
def battle_cancel():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        database.cancel_battle_record(str(p['battle_id']), user['id'])
        fresh_user = database.get_user(user['id'])
        return jsonify({'ok': True, 'balance': fresh_user['balance'] if fresh_user else 0})
    except Exception as e:
        return error(e, 400)

@app.post('/api/mp/battles/play')
def battle_play():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        result = database.play_battle_record(str(p['battle_id']), user['id'])
        farm_action=database.evaluate_economy_farm(user['id'])
        u = database.get_user(user['id'])
        return jsonify({
            'ok': True, 
            'balance': u['balance'] if u else 0, 
            'cubes': database.get_cubes(user['id']),
            'message': f"🎲 Победил {result['winner_name']} и получил {result.get('payout', result['pot']):,} 💰".replace(',', ' '),
            'result': result,
            'farm_action': farm_action
        })
    except Exception as e:
        return error(e, 400)

# --- Бот команды ---
if dp:
    def build_main_keyboard(private_chat=True):
        # Telegram accepts WebApp buttons only in private chats. Keep the
        # WebApp button for private chats (so initData is available), and use
        # a normal URL button only as a safe fallback for group chats.
        kb = InlineKeyboardMarkup(row_width=1)
        if private_chat and WEBAPP_URL.startswith(('https://', 'http://')) and 'your-app.netlify.app' not in WEBAPP_URL:
            kb.add(InlineKeyboardButton('🎮 Играть', web_app=WebAppInfo(url=WEBAPP_URL)))
        else:
            kb.add(InlineKeyboardButton('🎮 Играть', url=WEBAPP_URL))
        return kb

    async def send_main_menu(message: types.Message, referral_id=None):
        uid = message.from_user.id
        created = database.create_user(uid, message.from_user.username or message.from_user.first_name or str(uid))
        if created and referral_id is not None:
            try:
                ref_id=int(referral_id)
                result=database.register_referral(ref_id, uid, 7500)
                if result.get('created') and ref_id != uid:
                    try:
                        await bot.send_message(ref_id, f'🎉 По твоей реферальной ссылке присоединился новый игрок! +{result["reward"]:,}'.replace(',', ' ') + ' 💰')
                    except Exception as notify_error:
                        print(f'⚠️ Не удалось уведомить реферера {ref_id}: {notify_error}')
            except Exception as referral_error:
                print(f'⚠️ Referral processing error: {referral_error}')
        await message.answer(
            '🎮 **GDPLAY**\n\nДобро пожаловать! 🚀\n\nНажми **Играть**, чтобы открыть игру.',
            reply_markup=build_main_keyboard(message.chat.type == 'private'),
            parse_mode='Markdown'
        )

    @dp.message_handler(commands=['start', 'main'])
    async def start(message: types.Message):
        referral_id = None
        if message.text and message.text.startswith('/start'):
            try:
                arg = message.get_args().strip()
                if arg.startswith('ref_'):
                    referral_id = int(arg[4:])
            except (TypeError, ValueError):
                referral_id = None
        await send_main_menu(message, referral_id=referral_id)

    # Fallback: если Telegram прислал обычное сообщение вместо команды,
    # всё равно показываем главное меню. Это не меняет игровую логику и
    # гарантирует, что кнопка «Играть» не пропадёт из чата бота.
    @dp.message_handler(content_types=types.ContentTypes.ANY)
    async def fallback_menu(message: types.Message):
        try:
            await send_main_menu(message)
        except Exception as e:
            print(f'⚠️ Не удалось отправить меню бота: {e}')

    async def on_startup(_):
        # Не удаляем ожидающие обновления: иначе /start, отправленный во время
        # перезапуска Railway, может быть потерян. Webhook очищается перед polling.
        me = await bot.get_me()
        global BOT_USERNAME
        BOT_USERNAME = str(me.username or '').strip()
        print(f'✅ GDPLAY bot @{me.username} ({me.id}) started; WebApp={WEBAPP_URL}')

        # Явно регистрируем команды, чтобы /start и /main были доступны в меню Telegram.
        try:
            from aiogram.types import BotCommand
            await bot.set_my_commands([
                BotCommand('start', 'Открыть главное меню'),
                BotCommand('main', 'Открыть игру'),
            ])
        except Exception as cmd_error:
            print(f'⚠️ Не удалось установить команды бота: {cmd_error}')

# --- ЗАПУСК ---
# Telegram-бот запускается в отдельном потоке и при обычном `python main.py`,
# и при запуске Flask через gunicorn/другой WSGI-сервер. Это важно для Railway:
# если Railway импортирует `app`, блок `if __name__ == '__main__'` не выполняется.
_bot_thread_started = False
_bot_thread_lock = threading.Lock()

def _run_telegram_bot():
    if not dp or not bot:
        print('⚠️ Бот не запущен: BOT_TOKEN отсутствует.')
        return

    # Важно для оплаты Stars: pre_checkout_query должен обрабатываться
    # тем же живым polling-процессом, который получает updates от Telegram.
    # Если Telegram временно оборвал соединение, запускаем polling заново.
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            print('🤖 Подготовка Telegram polling...')
            loop.run_until_complete(bot.delete_webhook(drop_pending_updates=False))
            loop.run_until_complete(on_startup(None))
            print('🤖 Telegram polling запущен; /start, /main и платежи активны.')
            executor.start_polling(dp, skip_updates=False)
            print('⚠️ Telegram polling завершился — перезапуск через 2 сек.')
        except Exception as e:
            print(f'❌ Telegram bot crashed: {e}')
            traceback.print_exc()
        finally:
            try:
                loop.close()
            except Exception:
                pass
        time.sleep(2)

def start_telegram_bot_once():
    global _bot_thread_started
    if not dp or not bot:
        return
    with _bot_thread_lock:
        if _bot_thread_started:
            return
        _bot_thread_started = True
        t = threading.Thread(target=_run_telegram_bot, name='telegram-bot', daemon=True)
        t.start()

# Запускаем при импорте модуля — совместимо с Railway/gunicorn.
start_telegram_bot_once()


@app.post('/api/channel/claim')
def claim_channel_bonus():
    try:
        user, _ = current(request.get_json(silent=True) or {})
        token=os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
        if not token:
            raise ValueError('BOT_TOKEN не настроен')
        tg=requests.get(f'https://api.telegram.org/bot{token}/getChatMember', params={'chat_id':'@GDplay_off','user_id':int(user['id'])}, timeout=8).json()
        if not tg.get('ok'):
            raise ValueError('Не удалось проверить подписку. Убедись, что бот добавлен администратором канала.')
        member=tg.get('result') or {}
        status=str(member.get('status') or '')
        if status in ('left','kicked','restricted') and not (status=='restricted' and member.get('is_member')):
            return jsonify({'ok':False,'subscribed':False,'error':'Сначала подпишись на канал GDplay, затем нажми «Проверить подписку».'}), 400
        result=database.claim_channel_bonus(int(user['id']),1000)
        if result.get('claimed'):
            database.add_notification(int(user['id']),'🎁 Бонус за подписку','За подписку на @GDplay_off начислено +1000 💰','important')
        return jsonify({'ok':True,'subscribed':True,**result})
    except Exception as e:
        return error(e,400)

@app.post('/api/premium/create')
def create_premium_invoice():
    try:
        token=os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
        if not token:
            raise ValueError('BOT_TOKEN не настроен')
        import time
        payload='premium_'+str(time.time())
        r=requests.post(f'https://api.telegram.org/bot{token}/createInvoiceLink',json={
            'title':'Premium 30 дней',
            'description':'x2 ежедневный бонус, 10 Орб-рывков, x0.85 продажа, +10% удачи, +25% Battle, Premium-кейс, 100 слотов и ускоренные кейсы',
            'payload':payload,
            # Для Telegram Stars provider_token ОБЯЗАТЕЛЬНО не передаём.
            # Telegram Bot API использует XTR без платёжного провайдера.
            'currency':'XTR',
            'prices':[{'label':'Premium 30 дней','amount':20}]
        },timeout=10).json()
        if not r.get('ok'): raise ValueError(str(r))
        return {'invoice':r['result']}
    except Exception as e:
        return error(e,400)


# Покупка монет за Telegram Stars. Ровно 7 фиксированных пакетов.
COIN_STAR_PACKAGES = {
    5: 7000,
    10: 16000,
    25: 45000,
    50: 100000,
    100: 210000,
    250: 500000,
    500: 750000,
}
_coin_payment_charges = set()
_coin_payment_lock = threading.Lock()

def _coin_package_from_payload(payload):
    m = re.fullmatch(r'coins_(5|10|25|50|100|250|500)_([0-9]+)', str(payload or ''))
    if not m:
        return None
    stars = int(m.group(1))
    coins = int(m.group(2))
    if COIN_STAR_PACKAGES.get(stars) != coins:
        return None
    return stars, coins

@app.post('/api/coins/create')
def create_coins_invoice():
    try:
        token = os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
        if not token:
            raise ValueError('BOT_TOKEN не настроен')
        p = request.get_json(silent=True) or {}
        stars = int(p.get('stars', 0) or 0)
        coins = COIN_STAR_PACKAGES.get(stars)
        if not coins:
            raise ValueError('Недоступный пакет монет')
        payload = f'coins_{stars}_{coins}'
        r = requests.post(f'https://api.telegram.org/bot{token}/createInvoiceLink', json={
            'title': f'{coins:,}'.replace(',', ' ') + ' монет',
            'description': f'Покупка {coins:,}'.replace(',', ' ') + f' монет за {stars} ⭐',
            'payload': payload,
            'currency': 'XTR',
            'prices': [{'label': f'{coins:,}'.replace(',', ' ') + ' монет', 'amount': stars}]
        }, timeout=10).json()
        if not r.get('ok'):
            raise ValueError(str(r))
        return {'invoice': r['result'], 'stars': stars, 'coins': coins}
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/notifications/send')
def admin_send_notifications():
    try:
        # Рассылка должна проходить через ту же серверную проверку, что и остальные
        # действия админки; раньше этот endpoint принимал запрос без adminSession.
        admin_row = admin(request.get_json(silent=True) or {})
        data = request.get_json(silent=True) or {}
        text = str(data.get('text') or '').strip()
        if not text:
            raise ValueError('Пустой текст')
        target = str(data.get('player') or '').strip()
        sent = 0
        if target:
            rows = database.search_users(target) if hasattr(database, 'search_users') else []
            if not rows and target.isdigit():
                try:
                    rows = [database.get_user_by_id(int(target))]
                except Exception:
                    rows = []
            user = next((u for u in rows if u), None)
            if not user:
                raise ValueError('Игрок не найден')
            database.add_notification(user['id'], '🔔 Уведомление', text, 'important')
            sent = 1
        else:
            users = database.list_users('') if hasattr(database, 'list_users') else []
            for u in users:
                database.add_notification(u['id'], 'Админ', text, 'important')
                sent += 1
        database.add_admin_log(admin_row[0]['id'], None, 'Рассылка уведомлений', f'{sent} получателей')
        return jsonify({'ok': True, 'sent': sent})
    except PermissionError as e:
        return error(e, 403)
    except Exception as e:
        return error(e, 400)

if __name__ == '__main__':
    print('🚀 Запуск GDPLAY...')

    def run_flask():
        app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f'✅ Flask API запущен на порту {PORT}')

    # Бот уже запущен в отдельном потоке. Держим процесс живым.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print('🛑 GDPLAY остановлен')


# Premium perks configuration
PREMIUM_PERKS = {'daily_multiplier':2,'orb_daily_limit':10,'sell_multiplier':0.85,'luck_bonus':0.10,'battle_reward_multiplier':1.25,'inventory_bonus':50,'case_speed_bonus':0.5}
