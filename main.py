import requests
import os
import re, json, hmac, hashlib, urllib.parse, asyncio, threading, time, random, secrets, urllib.request, urllib.parse as urlparse, html
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from dotenv import load_dotenv
from aiogram import Bot, types
from aiogram.dispatcher import Dispatcher
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
import database
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
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
WEBAPP_SHORT_NAME = os.getenv('WEBAPP_SHORT_NAME', '').strip().strip('/')
# Безопасность админки: доступ только по точному Telegram ID.
# Username, пароль и локальные флаги аккаунта не дают права администратора.
ADMIN_TELEGRAM_ID = 7491528121
ADMIN_SESSION_TTL = 5 * 60
ADMIN_MAX_REQUESTS_PER_MINUTE = 20
IP_BAN_DEFAULT_MINUTES = 60
RUSH_MAX_BET = 1_000_000
RUSH_MAX_MULTIPLIER = 2.5

KEY_DROP_CATALOG = [
    {'id':'rust-key','name':'Ключ латунного кейса','rarity':'common','value':120,'icon':'🗝️'},
    {'id':'brass-key','name':'Ключ латунного кейса','rarity':'rare','value':650,'icon':'🔑'},
    {'id':'obsidian-key','name':'Ключ чёрного кейса','rarity':'epic','value':2800,'icon':'🗝️'},
    {'id':'vault-key','name':'Ключ хранилища кейса','rarity':'legendary','value':12000,'icon':'🔐'},
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
        raise ValueError('Для этого кейса нет ключа')
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

# Extra server-side binding for admin sessions. The token itself is never enough:
# a session is also tied to the browser User-Agent that created it and is kept
# in server memory. This is deliberately separate from the normal player session.
_admin_session_meta = {}
_admin_session_meta_lock = threading.RLock()
ADMIN_SESSION_META_TTL = ADMIN_SESSION_TTL

def _admin_client_fingerprint():
    ua = str(request.headers.get('User-Agent') or '')[:512]
    return hashlib.sha256(ua.encode('utf-8')).hexdigest()

def _remember_admin_session(token_hash):
    now = time.monotonic()
    with _admin_session_meta_lock:
        # Keep the in-memory map small and discard stale entries.
        stale = [k for k, v in _admin_session_meta.items()
                 if now - float(v.get('created', now)) > ADMIN_SESSION_META_TTL + 60]
        for k in stale:
            _admin_session_meta.pop(k, None)
        _admin_session_meta[token_hash] = {
            'fingerprint': _admin_client_fingerprint(),
            'created': now,
            'last_seen': now,
        }

def _check_admin_session_binding(token_hash):
    now = time.monotonic()
    fp = _admin_client_fingerprint()
    with _admin_session_meta_lock:
        meta = _admin_session_meta.get(token_hash)
        if not meta:
            raise PermissionError('Админ-сессия недействительна. Откройте админ-панель заново.')
        if now - float(meta.get('created', now)) > ADMIN_SESSION_META_TTL:
            _admin_session_meta.pop(token_hash, None)
            raise PermissionError('Админ-сессия истекла. Откройте админ-панель заново.')
        if not hmac.compare_digest(str(meta.get('fingerprint') or ''), fp):
            _admin_session_meta.pop(token_hash, None)
            raise PermissionError('Сессия привязана к другому браузеру.')
        meta['last_seen'] = now

PORT = int(os.getenv('PORT', '8080') or 8080)

# 12-hour bonus ledger. Kept separately so the bonus cadence is independent
# from the legacy database.claim_daily() 24-hour/date-based cooldown.
BONUS_12H_LOCK = threading.RLock()
BONUS_12H_COOLDOWN = 12 * 60 * 60

# The bonus ledger is stored in SQLite, not a JSON file, so it survives
# application reloads/restarts and is backed by the same persistent database.
def _load_bonus_12h(user_id=None):
    if user_id is None:
        return {}
    return database.get_bonus_12h(int(user_id))

def _save_bonus_12h(user_id, row):
    database.save_bonus_12h(int(user_id), row)

def _bonus_reward_catalog():
    return [1000,1500,2000,2500,3000,4000,5000,6000,7000,8000,9000,10000,12000,15000]

def _ensure_bonus_12h(user_id, now=None):
    """Return an authoritative bonus state. A missing/legacy row gets a fresh
    12-hour claim window immediately; the browser never invents the timer."""
    now = float(now or time.time())
    with BONUS_12H_LOCK:
        row = _load_bonus_12h(int(user_id)) or {}
        if not row:
            available = now
            expires = available + BONUS_12H_COOLDOWN
            row = {'last_claim': 0, 'next_claim_at': available, 'streak_day': 0,
                   'claims_in_day': 0, 'total_claims': 0, 'available_at': available,
                   'expires_at': expires, 'status': 'available'}
            _save_bonus_12h(int(user_id), row)
            return row
        # Normalize legacy rows. In the previous version expires_at was incorrectly
        # tied to the last claim, so the 12-hour cooldown could be mistaken for an
        # expired claim window. A real claim window starts only when next_claim_at arrives.
        available = float(row.get('available_at', 0) or 0)
        expires = float(row.get('expires_at', 0) or 0)
        next_claim = float(row.get('next_claim_at', 0) or 0)
        last_claim = float(row.get('last_claim', 0) or 0)
        total_claims = int(row.get('total_claims', 0) or 0)
        # A never-claimed bonus is always a fresh day-1 bonus. Repair all
        # stale DB fields (including an old "completed" status) so the first
        # claim cannot get stuck at 0/14 with a zero countdown.
        if total_claims <= 0 and last_claim <= 0:
            available = now
            expires = now + BONUS_12H_COOLDOWN
            next_claim = now
            status = 'available'
        else:
            if total_claims > 0 and last_claim and next_claim and available <= last_claim + 1 and abs(expires - next_claim) <= 1:
                available = next_claim
                expires = next_claim + BONUS_12H_COOLDOWN
            elif not available:
                available = next_claim or ((last_claim + BONUS_12H_COOLDOWN) if last_claim else now)
            if not expires:
                expires = available + BONUS_12H_COOLDOWN
            status = str(row.get('status') or 'available')
        if status not in ('available','completed'):
            status = 'available'
        row.update({'available_at': available, 'expires_at': expires, 'status': status})
        if float(row.get('next_claim_at', 0) or 0) != available:
            row['next_claim_at'] = available
        _save_bonus_12h(int(user_id), row)
        return row

def _reset_expired_bonus_12h(user_id, now=None, notify=True):
    now = float(now or time.time())
    reset = database.expire_bonus_12h(int(user_id), now)
    if not reset:
        return False
    text = 'Бонус сброшен\nТы не забрал доступную награду в течение 12 часов. Бонусная серия начата заново.'
    telegram_html = ('<tg-emoji emoji-id=\"5447644880824181073\">🎁</tg-emoji> <b>Бонус сброшен</b>\n'
                     'Ты не забрал доступную награду в течение 12 часов. Бонусная серия начата заново.')
    try:
        notify_user(int(user_id), 'Бонус сброшен', text, telegram_html=telegram_html)
    except Exception as exc:
        print(f'⚠️ Не удалось создать уведомление о сбросе бонуса {user_id}: {exc}')
    return True

def _bonus_12h_sweeper():
    while True:
        try:
            expired = database.list_expired_bonus_12h(time.time(), 200)
            for row in expired:
                _reset_expired_bonus_12h(int(row['user_id']), time.time(), notify=True)
        except Exception as exc:
            print(f'⚠️ Bonus sweeper error: {exc}')
        time.sleep(20)

def claim_bonus_12h(user_id, premium=False):
    now=time.time()
    rewards=_bonus_reward_catalog()
    MAX_CLAIMS=14
    with BONUS_12H_LOCK:
        row=_ensure_bonus_12h(int(user_id), now)
        available_at=float(row.get('available_at',0) or 0)
        expires=float(row.get('expires_at',0) or 0)
        if str(row.get('status') or 'available') == 'completed':
            raise ValueError('Серия бонуса 14/14 завершена')
        if available_at > now:
            raise ValueError('Бонус ещё недоступен')
        if expires and now >= expires:
            _reset_expired_bonus_12h(int(user_id), now, notify=True)
            row=_ensure_bonus_12h(int(user_id), now)
        total=max(0,int(row.get('total_claims',0) or 0))
        if total >= MAX_CLAIMS:
            _save_bonus_12h(int(user_id), {**row,'status':'completed','expires_at':0,'next_claim_at':0})
            raise ValueError('Серия бонуса 14/14 завершена')
        reward=rewards[total]*(2 if premium else 1)
        balance=database.update_balance(int(user_id),reward)
        new_total=total+1
        if new_total >= MAX_CLAIMS:
            _save_bonus_12h(int(user_id), {'last_claim':now,'next_claim_at':0,'streak_day':new_total,
                'claims_in_day':0,'total_claims':new_total,'available_at':0,'expires_at':0,'status':'completed'})
            next_claim=0
        else:
            # The next reward becomes available 12 hours after this claim.
            # Its 12-hour expiry window starts only after it becomes available.
            next_claim=now+BONUS_12H_COOLDOWN
            expires_at=next_claim+BONUS_12H_COOLDOWN
            _save_bonus_12h(int(user_id), {'last_claim':now,'next_claim_at':next_claim,
                'streak_day':new_total,'claims_in_day':0,'total_claims':new_total,
                'available_at':next_claim,'expires_at':expires_at,'status':'available'})
        return {'balance':balance,'reward':reward,'streak':new_total,'claims_in_day':0,
                'total_claims':new_total,'last_daily':int(now*1000),'next_daily':int(next_claim*1000),
                'available_at':int((next_claim if new_total < MAX_CLAIMS else 0)*1000),
                'expires_at':0,
                'available':False}


if not BOT_TOKEN:
    print('⚠️ BOT_TOKEN не задан! Бот не будет работать, но API запустится.')

database.init_db()

def _load_custom_case_items():
    """Merge admin-created case cubes into the server catalog without replacing the built-in catalog."""
    try:
        for row in database.get_case_custom_items():
            c = CASES_BY_ID.get(str(row.get('case_id')))
            if not c:
                continue
            item = {
                'name': str(row.get('name') or 'Куб')[:80],
                'rarity': str(row.get('rarity') or 'common').lower(),
                'value': max(1, min(50_000_000, int(row.get('value') or 1))),
                'chance': max(0.0001, float(row.get('chance') or 1.0)),
                'custom_id': int(row.get('id') or 0)
            }
            c.setdefault('items', []).append(item)
    except Exception:
        traceback.print_exc()

_load_custom_case_items()


app = Flask(__name__)

@app.after_request
def _admin_security_headers(response):
    if request.path.startswith('/api/admin/'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
    return response


# Persistent profile name styles (separate from the game database so older database.py
# versions remain compatible). Values are style keys: default/gold/emerald/ruby/amethyst/ice/neon.
NAME_STYLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'profile_name_styles.json')
_NAME_STYLE_LOCK = threading.RLock()
_NAME_STYLE_KEYS = {'default','gold','emerald','ruby','amethyst','ice','neon','rainbow','plasma','arctic','diamond','sunset','ocean','toxic','aurora'}

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
    key=str(u.get('profile_name_style') or '').lower()
    if key in _NAME_STYLE_KEYS and key != 'default':
        return key
    # Compatibility fallback for accounts created by older builds.
    try:
        legacy=str(_load_name_styles().get(str(user_id)) or '').lower()
        if legacy in _NAME_STYLE_KEYS:
            return legacy
    except Exception:
        pass
    return 'default'

def _premium_active_value(value):
    if value is None or value == '':
        return False
    try:
        # Accept both ISO timestamps and Unix seconds/milliseconds.
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            n=float(value)
            if n > 10_000_000_000:
                n /= 1000.0
            return n > time.time()
        raw=str(value).strip().replace('Z','+00:00')
        dt=datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            return dt.timestamp() > time.time()
        return dt > datetime.now()
    except Exception:
        return False

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
    # Static HTML fragments are versioned by the app and safe to cache; this
    # avoids repeatedly transferring the ~2 MB embedded case-image catalog.
    if request.path.startswith('/assets/') or request.path in ('/promo/2026samergermancrut.jpg', '/case_images.html'):
        response.headers.setdefault('Cache-Control', 'public, max-age=86400, stale-while-revalidate=604800')
    # Compress the large WebApp HTML/JSON responses. This is especially useful
    # for the single-page UI, while already-compressed images are left alone.
    accept = request.headers.get('Accept-Encoding', '')
    content_type = response.headers.get('Content-Type', '')
    if ('gzip' in accept.lower() and 'gzip' not in response.headers.get('Content-Encoding','').lower()
            and ('text/' in content_type or 'application/json' in content_type)
            and response.status_code not in (204, 304) and response.direct_passthrough is False):
        body = response.get_data()
        if len(body) >= 1024:
            import gzip
            compressed = gzip.compress(body, compresslevel=6, mtime=0)
            if len(compressed) < len(body):
                response.set_data(compressed)
                response.headers['Content-Encoding'] = 'gzip'
                response.headers['Vary'] = 'Accept-Encoding'
                response.headers['Content-Length'] = str(len(compressed))
    return response

_INDEX_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
_INDEX_INCLUDE_REPLACEMENTS = {
    '<!-- @include auth.html -->': 'auth.html',
    '<!-- @include app.html -->': 'app.html',
    '<!-- @include modals.html -->': 'modals.html',
}
_INDEX_CACHE = None
_INDEX_CACHE_LOCK = threading.Lock()

def _render_index_html():
    global _INDEX_CACHE
    with _INDEX_CACHE_LOCK:
        if _INDEX_CACHE is not None:
            return _INDEX_CACHE
        html = open(_INDEX_TEMPLATE, 'r', encoding='utf-8').read()
        base = os.path.dirname(_INDEX_TEMPLATE)
        for marker, filename in _INDEX_INCLUDE_REPLACEMENTS.items():
            fragment_path = os.path.join(base, filename)
            fragment = open(fragment_path, 'r', encoding='utf-8').read()
            html = html.replace(marker, fragment)
        _INDEX_CACHE = html
        return html

@app.get('/')
def index_page():
    response = app.make_response(_render_index_html())
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response

@app.get('/assets/<path:filename>')
def app_asset(filename):
    # Static assets kept outside index.html so the browser does not parse megabytes
    # of base64 before the first screen can render.
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)

@app.get('/promo/2026samergermancrut.jpg')
def promo_secret_image():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'IMG_20260817_134411.jpg')

@app.get('/promo/referral_share.jpg')
def referral_share_image():
    # Public image used by Telegram's prepared referral message.
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'referral_share.jpg', mimetype='image/jpeg')

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
    
    await message.reply(f'✅ Тестово начислено 20 Stars!\n<tg-emoji emoji-id="{MONEY_TELEGRAM_EMOJI_ID}">💰</tg-emoji> Проверьте баланс в профиле', parse_mode='HTML')

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

    if payload.startswith('premium_gift_'):
        parts=payload.split('_')
        if len(parts) < 5:
            return
        try:
            sender_id=int(parts[2]); recipient_id=int(parts[3]); days=int(parts[4])
        except (TypeError,ValueError):
            return
        gift_prices={30:20,60:35,90:45}
        expected_stars=gift_prices.get(days)
        if expected_stars is None:
            return
        if sender_id != int(user_id) or str(payment.currency or '') != 'XTR' or int(payment.total_amount or 0) != expected_stars:
            print(f'⚠️ Отклонён gift payment: payer={user_id} payload={payload} amount={payment.total_amount}')
            return
        charge_id=str(payment.telegram_payment_charge_id or '')
        if database.premium_gift_payment_exists(charge_id):
            return
        recipient=database.get_user(recipient_id)
        if not recipient or int(recipient.get('banned') or 0):
            print(f'⚠️ Gift recipient unavailable: {recipient_id}')
            return
        database.grant_premium(recipient_id, days)
        sender_row=database.get_user(sender_id) or {}
        sender_name=sender_row.get('display_name') or sender_row.get('username') or str(sender_id)
        gift_text = (
            f'Вам игрок {sender_name} подарил(а) Премиум на {days} дней стоимостью {expected_stars} звёзд\n\n'
            'Доступно:\n'
            '• x2 ежедневный бонус\n'
            '• статус Premium в профиле\n'
            '• специальные Premium-бонусы\n\n'
            'Спасибо за поддержку GDPLAY!'
        )
        gift_html=(
            f'<tg-emoji emoji-id="5199749070830197566">🎁</tg-emoji> Вам игрок <b>{html.escape(str(sender_name))}</b> подарил(а) Премиум на <b>{days} дней</b> стоимостью <b>{expected_stars}</b> звёзд <tg-emoji emoji-id="5920281855378068765">⭐</tg-emoji>\n\n'
            'Доступно:\n'
            '• x2 ежедневный бонус\n'
            '• статус Premium в профиле\n'
            '• специальные Premium-бонусы\n\n'
            '<tg-emoji emoji-id="5256165287329309225">💖</tg-emoji> Спасибо за поддержку GDPLAY!'
        )
        notify_user(recipient_id, 'Подарен Premium', gift_text, '5199749070830197566', gift_html)
        database.record_premium_gift_payment(charge_id, sender_id, recipient_id, expected_stars)
        try:
            await message.reply(f'🎁 Подарок отправлен! Premium выдан игроку на {days} дней.')
        except Exception:
            pass
        return

    if payload.startswith('premium_'):
        parts=payload.split('_')
        # Current invoice payload: premium_<days>_<user_id>_<nonce>.
        # Accept the old ordering too for compatibility.
        try:
            if len(parts) >= 3 and str(parts[1]).isdigit():
                days=int(parts[1])
                payload_user_id=int(parts[2])
            else:
                days=int(parts[2]) if len(parts) >= 3 else 30
                payload_user_id=None
        except (TypeError,ValueError):
            days=30
            payload_user_id=None
        premium_prices={30:20,60:35,90:45}
        expected_stars=premium_prices.get(days)
        if payload_user_id is not None and payload_user_id != int(user_id):
            print(f'⚠️ Отклонён Premium payment: payer={user_id} payload_user={payload_user_id}')
            return
        if expected_stars is None or str(payment.currency or '') != 'XTR' or int(payment.total_amount or 0) != expected_stars:
            print(f'⚠️ Отклонён Premium payment: user={user_id} payload={payload} amount={payment.total_amount}')
            return
        database.create_user(user_id, message.from_user.username or message.from_user.first_name or str(user_id))
        database.grant_premium(user_id, days)
        premium_text = (
            f'Premium успешно активирован на {days} дней стоимостью {expected_stars} звёзд!\n\n'
            'Доступно:\n'
            '• x2 ежедневный бонус\n'
            '• статус Premium в профиле\n'
            '• специальные Premium-бонусы\n\n'
            'Спасибо за поддержку GDPLAY!'
        )
        premium_html = (
            f'<tg-emoji emoji-id="5330255294251410630">⭐</tg-emoji> Premium успешно активирован на <b>{days} дней</b> стоимостью <b>{expected_stars}</b> звёзд!\n\n'
            'Доступно:\n'
            '• x2 ежедневный бонус\n'
            '• статус Premium в профиле\n'
            '• специальные Premium-бонусы\n\n'
            '<tg-emoji emoji-id="5256165287329309225">💖</tg-emoji> Спасибо за поддержку GDPLAY!'
        )
        notify_user(user_id, 'Premium активирован', premium_text, '5330255294251410630', premium_html)
        # Второй Premium emoji нужен перед благодарностью; для этого Telegram-ответ отправляется отдельно.
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
        coin_text = f'Покупка успешна! Начислено {coins:,}'.replace(',', ' ')
        coin_html = (
            f'<tg-emoji emoji-id="5375296873982604963">💰</tg-emoji> Покупка успешна! Начислено {coins:,}'.replace(',', ' ') +
            ' <tg-emoji emoji-id="5375296873982604963">💰</tg-emoji>'
        )
        notify_user(user_id, 'Покупка успешна', coin_text, '5375296873982604963', coin_html)
        # Второй кастомный emoji передан отдельным сообщением в Telegram не нужен в HTML;
        # в основном уведомлении он будет добавлен после суммы через кастомный Telegram markup ниже.
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

        if payload.startswith('premium_gift_'):
            parts=payload.split('_')
            gift_prices={30:20,60:35,90:45}
            try:
                sender_id=int(parts[2]); days=int(parts[4])
                expected=gift_prices.get(days)
            except (TypeError,ValueError,IndexError):
                sender_id=0; expected=None
            valid = expected is not None and currency == 'XTR' and amount == expected and sender_id == int(query.from_user.id)
        elif payload.startswith('premium_'):
            parts=payload.split('_')
            prices={30:20,60:35,90:45}
            try: expected=prices.get(int(parts[1]))
            except (TypeError,ValueError,IndexError): expected=None
            valid = expected is not None and currency == 'XTR' and amount == expected
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
    # Telegram подписывает start_param вместе с остальными полями initData,
    # когда Mini App открыт по прямой ссылке (t.me/bot?startapp=... или
    # старому t.me/bot?start=...). Раньше это поле нигде не читалось на
    # сервере, из-за чего реферальные переходы напрямую в веб-приложение
    # (минуя команду /start в чате бота) никогда не засчитывались.
    user['_start_param'] = str(pairs.get('start_param') or '').strip()
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
                # A stored login session must not hide a fresh Telegram referral
                # deep-link. When signed initData is present, process its start_param
                # for the already-registered account before returning the session.
                signed_init = str(payload.get('initData') or '').strip()
                if signed_init:
                    try:
                        tg = verify_init_data(signed_init)
                        if int(tg.get('id') or 0) == int(uid):
                            register_web_referral(tg, payload, uid, False)
                    except Exception as referral_auth_error:
                        print(f'[REFERRAL] existing-session check skipped: {referral_auth_error}')
                database.record_user_ip(uid, client_ip)
                if u.get('banned'):
                    raise PermissionError(u.get('ban_reason') or 'Аккаунт заблокирован')
                return {'id': uid, 'username': u.get('username') or ''}, u
    user = verify_init_data(payload.get('initData', ''))
    is_new_user = database.create_user(user['id'], user.get('username') or user.get('first_name') or str(user['id']))
    register_web_referral(user, payload, user['id'], is_new_user)
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
    token_hash = _hash_admin_session(token)
    database.create_admin_session(token_hash, ADMIN_TELEGRAM_ID, expires.strftime('%Y-%m-%d %H:%M:%S'))
    _remember_admin_session(token_hash)
    return token, int(expires.timestamp())

def admin(payload):
    payload = payload or {}
    _admin_rate_check(request.remote_addr)
    if not ADMIN_TELEGRAM_ID: raise PermissionError('Админка не настроена')
    tg=verify_init_data(payload.get('initData',''))
    if int(tg.get('id') or 0) != ADMIN_TELEGRAM_ID: raise PermissionError('Нет доступа')
    token=str(payload.get('adminSession') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{40,100}', token): raise PermissionError('Админ-сессия отсутствует или истекла')
    token_hash = _hash_admin_session(token)
    if not database.get_admin_session(token_hash, ADMIN_TELEGRAM_ID): raise PermissionError('Админ-сессия истекла. Откройте админ-панель заново.')
    _check_admin_session_binding(token_hash)
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
    return {k: u.get(k) for k in ['user_id','username','balance','vault_balance','stars','level','trades','daily_claimed','daily_streak','banned','ban_reason','moderation_notice','premium_until','cases_opened','battle_winnings','avatar','display_name','display_name_changed_at','creator_badge','tester_badge','two_factor_enabled','tech_break_enabled','tech_break_reason','total_spins','total_wins','total_losses','battles_played','biggest_win']} | {'global_tech_break': database.get_global_tech_break()} | {'custom_status': custom_status, 'profile_name_style': (get_profile_name_style(u.get('user_id')) if _premium_active_value(u.get('premium_until')) else 'default'), 'profile_frame': str(u.get('profile_frame') or 'default'), 'notification_settings': database.get_notification_settings(u.get('user_id')), 'case_pity': database.get_case_pity(u.get('user_id')), 'cubes': cubes, 'keys': [], 'buffs': buffs, 'best_drops': best_drops, 'cube_slots': database.cube_inventory_limit(u.get('user_id')), 'cube_count': len(cubes)}

def _extract_ref_id(*candidates):
    """Достаёт числовой id реферера из значений вида 'ref_123' / '123'."""
    for raw in candidates:
        raw = str(raw or '').strip()
        if not raw:
            continue
        if raw.startswith('ref_'):
            raw = raw[4:]
        if raw.lstrip('-').isdigit():
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    return None

def _referral_invite_word(n):
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        n = 0
    if n % 10 == 1 and n % 100 != 11:
        return 'друга'
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return 'друга'
    return 'друзей'

def _notify_referral_success(referrer_id, referred_id, result):
    """Send the referrer a Telegram notification after a referral is actually credited."""
    try:
        referred = database.get_user(int(referred_id)) or {}
        name = effective_name(referred)
        remaining = int(result.get('referrer_daily_remaining', 0))
        reward = int(result.get('reward', database.REFERRAL_REWARD))
        text = (
            f'{name} перешёл по твоей реферальной ссылке!\n'
            f'+{reward}\n'
            f'Ты можешь сегодня пригласить ещё {remaining} {_referral_invite_word(remaining)}'
        )
        # Referral notification layout: custom emoji 5415594207068822547 at the
        # very beginning, and money emoji 5375296873982604963 immediately after
        # the reward amount. Keep the plain-text version unchanged for the app DB.
        start_emoji = '<tg-emoji emoji-id="5415594207068822547">🔔</tg-emoji>'
        money_emoji = '<tg-emoji emoji-id="5375296873982604963">💰</tg-emoji>'
        safe_name = html.escape(str(name), quote=False)
        safe_remaining = html.escape(str(remaining), quote=False)
        telegram_html = (
            f'{start_emoji} {safe_name} перешёл по твоей реферальной ссылке!\n'
            f'+{reward} {money_emoji}\n'
            f'Ты можешь сегодня пригласить ещё {safe_remaining} {_referral_invite_word(remaining)}'
        )
        notify_user(int(referrer_id), 'Новый реферал', text, '5415594207068822547', telegram_html)
    except Exception as exc:
        print(f'⚠️ Не удалось отправить уведомление о реферале {referrer_id}: {exc}')

def register_web_referral(tg, payload, referred_id, is_new_user=None):
    """Register a referral on every valid Mini App entry.

    This deliberately does not require a new account: existing Telegram users
    can enter a referral link too. The database enforces once-per-link and the
    10-different-links-per-day limit, and pays 3000 immediately.
    """
    ref_id = _extract_ref_id((tg or {}).get('_start_param'), (payload or {}).get('ref'))
    if ref_id is None or ref_id == referred_id:
        return
    try:
        result = database.register_referral(ref_id, referred_id, database.REFERRAL_REWARD)
        if result.get('created'):
            print(f'[REFERRAL] entry: referrer={ref_id} referred={referred_id} reward={result.get("reward", database.REFERRAL_REWARD)}')
            _notify_referral_success(ref_id, referred_id, result)
        elif result.get('reason') == 'daily_limit':
            print(f'[REFERRAL] daily limit reached for referred user={referred_id}')
    except Exception as referral_error:
        print(f'⚠️ Referral processing error (web): {referral_error}')

def effective_name(u):
    dn = (u.get('display_name') or '').strip() if u else ''
    if dn: return dn
    return (u.get('username') if u else None) or 'Игрок'

def format_orbs(value):
    try:
        return f'{int(value):,}'
    except (TypeError, ValueError):
        return str(value)

def format_orbs_word(value):
    """Return a correctly declined Russian orb amount."""
    try:
        n = abs(int(value))
    except (TypeError, ValueError):
        return f'{value} орбов'
    if 11 <= n % 100 <= 14:
        word = 'орбов'
    elif n % 10 == 1:
        word = 'орб'
    elif n % 10 in (2, 3, 4):
        word = 'орба'
    else:
        word = 'орбов'
    return f'{format_orbs(value)} {word}'

MONEY_TELEGRAM_EMOJI_ID = '5375296873982604963'

def error(e, status=400):
    return jsonify({'ok': False, 'error': str(e)}), status

# --- API Роуты ---
@app.get('/health')
def health():
    return jsonify({'ok': True, 'service': 'GDPLAY', 'webapp': WEBAPP_URL})

def _telegram_html_with_emoji(text, emoji_id=None, placeholder='💰'):
    safe = html.escape(str(text), quote=False)
    if emoji_id:
        return f'<tg-emoji emoji-id="{html.escape(str(emoji_id), quote=True)}">{placeholder}</tg-emoji> {safe}'
    return safe

def send_telegram_message(chat_id, text, emoji_id=None, raw_html=None, reply_markup=None):
    if not BOT_TOKEN:
        raise ValueError('BOT_TOKEN не задан')
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    primary = raw_html if raw_html is not None else _telegram_html_with_emoji(text, emoji_id)

    def _request(message_text, parse_mode='HTML'):
        payload = {'chat_id': int(chat_id), 'text': message_text}
        if parse_mode:
            payload['parse_mode'] = parse_mode
        # Never reassign the outer function argument here.  Assigning to
        # reply_markup inside this nested function makes it a local variable
        # and causes UnboundLocalError before it can be read.  That was the
        # reason persisted orb/sale notifications could not be delivered.
        markup = reply_markup
        if markup is not None:
            if hasattr(markup, 'to_python'):
                markup = markup.to_python()
            elif hasattr(markup, 'to_dict'):
                markup = markup.to_dict()
            payload['reply_markup'] = json.dumps(markup, ensure_ascii=False, separators=(',', ':'))
        body = urlparse.urlencode(payload).encode()
        req = urllib.request.Request(url, data=body, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode('utf-8', errors='replace')
            except Exception:
                detail = str(exc)
            raise RuntimeError(f'Telegram HTTP {exc.code}: {detail}') from exc

    try:
        data = _request(primary, 'HTML')
    except RuntimeError as exc:
        # A malformed/unsupported HTML tag must not make a notification retry forever.
        # Fall back to plain text once; this also protects against custom-emoji HTML
        # being rejected by Telegram for a particular account/message.
        if 'Telegram HTTP 400' not in str(exc):
            raise
        data = _request(str(text), None)
    if not data.get('ok'):
        raise ValueError(str(data.get('description') or 'Не удалось отправить сообщение в Telegram'))
    return data

def _deliver_notification(notification_id, user_id, text, telegram_emoji_id=None, telegram_html=None, attempts=3):
    """Deliver one persisted notification; an atomic DB claim prevents duplicates."""
    try:
        pending = next((n for n in database.get_unsent_telegram_notifications(100) if int(n.get('id') or 0) == int(notification_id)), None)
        if pending and not database.notification_enabled(int(user_id), notification_category(pending.get('title'))):
            database.mark_notification_telegram_sent(notification_id)
            return False
    except Exception as exc:
        print(f'⚠️ Notification preference check failed: id={notification_id}: {exc}')
    if not database.claim_notification_telegram(notification_id):
        return False
    last_exc = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            send_telegram_message(user_id, text, telegram_emoji_id, telegram_html)
            database.mark_notification_telegram_sent(notification_id)
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < attempts:
                time.sleep(0.7 * attempt)
    database.release_notification_telegram(notification_id)
    print(f'⚠️ Telegram notification queued for retry: user={user_id} id={notification_id}: {last_exc}')
    return False

NOTIFICATION_CATEGORY_LABELS = {
    'orbs_transfer': 'Передача орбов',
    'shop_sale': 'Продажа куба в магазине',
    'premium': 'Premium и подарки',
    'purchases': '🛍 Покупки',
    'moderation': 'Баны и ограничения',
    'tech_break': 'Технические уведомления',
    'subscription_bonus': 'Бонус за подписку',
    'admin': 'Уведомления от администрации',
    'bonus': 'Бонусы',
    'other': 'Другие уведомления',
}

def notification_category(title):
    t = str(title or '').strip().lower()
    if 'получены орбы' in t or 'передал' in t:
        return 'orbs_transfer'
    if 'куб купили' in t or 'магазин' in t and 'уведом' not in t:
        return 'shop_sale'
    if 'premium' in t or 'подарен premium' in t:
        return 'premium'
    if 'покуп' in t:
        return 'purchases'
    if t in ('бан', 'теневой бан') or 'заблокирован' in t or 'теневой' in t:
        return 'moderation'
    if 'технический перерыв' in t or 'тех. перерыва' in t:
        return 'tech_break'
    if 'бонус за подписку' in t:
        return 'subscription_bonus'
    if 'администратор' in t or 'от администрации' in t:
        return 'admin'
    if 'бонус' in t:
        return 'bonus'
    return 'other'

def notify_user(user_id, title, text, telegram_emoji_id=None, telegram_html=None):
    """Persist first, then deliver unless this notification category is disabled by the player."""
    category = notification_category(title)
    if not database.notification_enabled(int(user_id), category):
        print(f'🔕 Notification disabled: user={user_id} category={category} title={title}')
        return None
    notification_id = database.add_notification(user_id, title, text, 'important', telegram_emoji_id, telegram_html)
    if BOT_TOKEN:
        _deliver_notification(notification_id, int(user_id), text, telegram_emoji_id, telegram_html, attempts=3)
    return notification_id


TECH_BREAK_TELEGRAM_EMOJI_ID = '5366531532926231383'
TECH_BREAK_THANKS_EMOJI_ID = '5217449524410199951'
ADMIN_BROADCAST_EMOJI_ID = '5197304993920616826'

def _tech_break_html(title, reason=None, ended=False):
    """Build Telegram HTML so both custom emojis are rendered in the message itself."""
    start_emoji = f'<tg-emoji emoji-id="{TECH_BREAK_TELEGRAM_EMOJI_ID}">🔧</tg-emoji>'
    thanks_emoji = f'<tg-emoji emoji-id="{TECH_BREAK_THANKS_EMOJI_ID}">🙏</tg-emoji>'
    safe_title = html.escape(str(title), quote=False)
    parts = [f'{start_emoji} {safe_title}']
    if reason:
        parts.append(f'Причина: {html.escape(str(reason), quote=False)}')
    if ended:
        parts.append(f'{thanks_emoji} Спасибо вам за ожидание!')
    else:
        parts.append(f'{thanks_emoji} Спасибо вам за ожидание!')
    return '\n'.join(parts)

TECH_BREAK_EXEMPT_UID = 8750046129

def _notify_all_tech_break(text, html_text, title, reply_markup=None):
    # Global technical-break messages must reach EVERY registered player,
    # including the administrator. Send them concurrently so one slow Telegram
    # request cannot block delivery to everyone else.
    user_ids = set()
    try:
        rows = database.list_users('') if hasattr(database, 'list_users') else []
        for u in rows:
            if isinstance(u, dict):
                uid = u.get('user_id', u.get('id'))
                if uid:
                    try:
                        user_ids.add(int(uid))
                    except (TypeError, ValueError):
                        pass
    except Exception as exc:
        print(f'⚠️ Tech-break users lookup failed: {exc}')

    try:
        if ADMIN_TELEGRAM_ID:
            user_ids.add(int(ADMIN_TELEGRAM_ID))
    except (TypeError, ValueError):
        pass

    def deliver(uid):
        try:
            if int(uid) == TECH_BREAK_EXEMPT_UID:
                return False
            if not database.notification_enabled(uid, 'tech_break'):
                return False
            # Telegram delivery is attempted immediately. If it fails, persist
            # the notification so the normal retry worker can deliver it later.
            if BOT_TOKEN:
                send_telegram_message(uid, text, None, html_text, reply_markup=reply_markup)
                try:
                    notification_id = database.add_notification(uid, title, text, 'important', None, html_text)
                    database.mark_notification_telegram_sent(notification_id)
                except Exception as persist_exc:
                    print(f'⚠️ Tech-break delivered but not persisted: user={uid}: {persist_exc}')
            else:
                notify_user(uid, title, text, None, html_text)
            return True
        except Exception as exc:
            print(f'⚠️ Tech-break Telegram notification failed: user={uid}: {exc}')
            try:
                notify_user(uid, title, text, None, html_text)
            except Exception as queue_exc:
                print(f'⚠️ Tech-break fallback queue failed: user={uid}: {queue_exc}')
            return False

    if not user_ids:
        print(f'🔔 Tech-break "{title}" notification dispatch: 0/0 users')
        return 0

    # A bounded pool prevents a large player list from creating hundreds of
    # simultaneous connections while still making delivery much faster than
    # the previous one-user-at-a-time loop.
    workers = min(32, max(1, len(user_ids)))
    sent = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='tech-notify') as pool:
        futures = [pool.submit(deliver, uid) for uid in sorted(user_ids)]
        for future in as_completed(futures):
            try:
                sent += 1 if future.result() else 0
            except Exception as exc:
                print(f'⚠️ Tech-break worker error: {exc}')

    print(f'🔔 Tech-break "{title}" notification dispatch: {sent}/{len(user_ids)} users')
    return sent

def notify_global_tech_break(reason):
    """Notify every registered player in Telegram when the global tech break starts."""
    reason = str(reason or '').strip()
    text = 'Технический перерыв\n' + (f'Причина: {reason}' if reason else 'Игра временно недоступна.') + '\nСпасибо вам за ожидание!'
    return _notify_all_tech_break(text, _tech_break_html('Технический перерыв', reason), 'Технический перерыв')

def notify_global_tech_break_reason_changed(reason):
    """Notify players that the active global maintenance reason was changed."""
    reason = str(reason or '').strip()
    text = 'Причина тех. перерыва изменена\n' + (f'Новая причина: {reason}' if reason else 'Игра временно недоступна.') + '\nСпасибо вам за ожидание!'
    html_text = _tech_break_html('Причина тех. перерыва изменена', reason)
    # Keep the same custom emojis used by the existing maintenance messages.
    return _notify_all_tech_break(text, html_text, 'Причина тех. перерыва изменена')

def notify_global_tech_break_end(reason=''):
    """Notify every registered player when the global technical break ends.

    The end-of-maintenance message gets the same "🎮 Играть" button as /start.
    """
    reason = str(reason or '').strip()
    text = 'Технический перерыв завершён\nИгра снова доступна.\nСпасибо вам за ожидание!'
    html_text = _tech_break_html('Технический перерыв завершён', reason, ended=True)

    # Keep the exact same game-opening button behavior as /start.
    reply_markup = None
    try:
        if dp:
            reply_markup = build_main_keyboard(True)
    except Exception as exc:
        print(f'⚠️ Could not build game button for tech-break end: {exc}')

    return _notify_all_tech_break(text, html_text, 'Технический перерыв завершён', reply_markup=reply_markup)

def _telegram_notification_sweeper():
    while True:
        try:
            if BOT_TOKEN:
                pending = database.get_unsent_telegram_notifications(100)
                for n in pending:
                    try:
                        if not database.notification_enabled(int(n.get('user_id') or 0), notification_category(n.get('title'))):
                            database.mark_notification_telegram_sent(n['id'])
                            continue
                        if not database.claim_notification_telegram(n['id']):
                            continue
                        retry_markup = None
                        try:
                            if n.get('title') == 'Технический перерыв завершён' and dp:
                                retry_markup = build_main_keyboard(True)
                        except Exception:
                            retry_markup = None
                        send_telegram_message(n['user_id'], n['text'], n.get('telegram_emoji_id'), n.get('telegram_text'), reply_markup=retry_markup)
                        database.mark_notification_telegram_sent(n['id'])
                    except Exception as exc:
                        msg = str(exc)
                        print(f"⚠️ Notification delivery retry failed: id={n.get('id')} user={n.get('user_id')}: {msg}")
                        # 403 usually means the user blocked the bot. Keep 400 queued because
                        # custom emoji/HTML may be rejected and the sender already has a plain-text fallback.
                        if 'Telegram HTTP 403' in msg or 'HTTP Error 403' in msg:
                            try:
                                database.mark_notification_telegram_sent(n['id'])
                                print(f"ℹ️ Notification id={n.get('id')} closed after Telegram 403")
                            except Exception as mark_exc:
                                print(f"⚠️ Could not close failed notification id={n.get('id')}: {mark_exc}")
                        else:
                            try:
                                database.release_notification_telegram(n['id'])
                            except Exception:
                                pass
        except Exception as exc:
            print(f'⚠️ Notification sweeper error: {exc}')
        time.sleep(3)

_bonus_sweeper_thread = threading.Thread(target=_bonus_12h_sweeper, name='bonus-12h-sweeper', daemon=True)
_bonus_sweeper_thread.start()
_notification_sweeper_thread = threading.Thread(target=_telegram_notification_sweeper, name='telegram-notification-sweeper', daemon=True)
_notification_sweeper_thread.start()

def cleanup_old_battles():
    while True:
        try:
            with database.db() as c:
                # Возвращаем ставки игрокам перед удалением зависшего батла
                expired = c.execute("SELECT id, stake FROM battles WHERE status='open' AND datetime(created_at) <= datetime('now','-7 hours')").fetchall()
                for b in expired:
                    players = c.execute("SELECT user_id FROM battle_players WHERE battle_id=?", (b['id'],)).fetchall()
                    for p in players:
                        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (int(b['stake'] or 0), int(p['user_id'])))
                    c.execute("UPDATE battles SET status='expired_refunded' WHERE id=?", (b['id'],))
                c.execute("DELETE FROM battles WHERE status='expired_refunded' AND datetime(created_at) <= datetime('now','-7 hours')")
                c.commit()
        except Exception:
            pass
        time.sleep(600)

threading.Thread(target=cleanup_old_battles, daemon=True).start()

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

@app.post('/api/presence')
def api_presence():
    try:
        payload=request.get_json(silent=True) or {}
        token=str(payload.get('authToken') or '').strip()
        uid=database.get_auth_session(token) if token else None
        if not uid:
            tg=verify_init_data(payload.get('initData',''))
            uid=tg.get('id') if tg else None
        if not uid:
            return error(ValueError('Сессия не найдена'),401)
        u=database.get_user(uid)
        if not u or u.get('banned'):
            return error(ValueError('Доступ запрещён'),403)
        if bool(payload.get('online', True)):
            database.touch_app_presence(uid)
        else:
            database.clear_app_presence(uid)
        return jsonify({'ok':True})
    except Exception as e:
        return error(e,400)

@app.post('/api/me')
def api_me():
    try:
        payload=request.get_json(silent=True) or {}
        ip=database.get_client_ip_from_request(request)
        if database.is_ip_banned(ip):
            return error(ValueError('Доступ заблокирован для этого IP-адреса'),403)

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
                    signed_init = str(payload.get('initData') or '').strip()
                    if signed_init:
                        try:
                            tg_session = verify_init_data(signed_init)
                            if int(tg_session.get('id') or 0) == int(uid):
                                register_web_referral(tg_session, payload, uid, False)
                        except Exception as referral_auth_error:
                            print(f'[REFERRAL] /api/me existing-session check skipped: {referral_auth_error}')
                    database.record_user_ip(uid, ip)
                    if u.get('banned'):
                        return jsonify({'ok':False,'error':u.get('ban_reason') or 'Аккаунт заблокирован','user':public(u),'is_admin':int(uid)==ADMIN_TELEGRAM_ID,'global_tech_break':database.get_global_tech_break(),'case_pity':database.get_case_pity(uid)}),403
                    notice=database.get_moderation_notice(uid)
                    return jsonify({'ok':True,'user':public(u),'moderation_notice':notice,'is_admin':int(uid)==ADMIN_TELEGRAM_ID,'server_time':datetime.now(timezone.utc).isoformat(),'session_restored':True,'global_tech_break':database.get_global_tech_break(),'case_pity':database.get_case_pity(uid)})

        tg=verify_init_data(payload.get('initData',''))
        is_new_user = database.create_user(tg['id'], tg.get('username') or tg.get('first_name') or str(tg['id']))
        register_web_referral(tg, payload, tg['id'], is_new_user)
        database.set_telegram_display_name(tg['id'], telegram_display_name(tg))
        database.enforce_ban_state(tg['id'])
        database.record_user_ip(tg['id'], ip)
        u=database.get_user(tg['id'])
        notice=database.get_moderation_notice(tg['id'])
        if u and u.get('banned'):
            # Не скрываем причину: клиент получает её отдельным полем даже при 403.
            return jsonify({'ok':False,'error':u.get('ban_reason') or 'Аккаунт заблокирован','user':public(u),'moderation_notice':notice,'is_admin':False,'global_tech_break':database.get_global_tech_break(),'case_pity':database.get_case_pity(int(tg['id']))}),403
        photo=str(tg.get('photo_url') or '').strip()
        old_avatar=str(u.get('avatar') or '') if u else ''
        if photo and (not old_avatar or old_avatar.startswith('https://t.me/i/userpic/')) and photo != old_avatar:
            database.set_avatar(tg['id'],photo); u=database.get_user(tg['id'])
        auth_token = database.create_auth_session(tg['id'])
        return jsonify({'ok':True,'token':auth_token,'authToken':auth_token,'user':public(u),'moderation_notice':notice,'is_admin':is_admin_user(tg,u),'server_time':datetime.now(timezone.utc).isoformat(),'global_tech_break':database.get_global_tech_break(),'case_pity':database.get_case_pity(int(tg['id']))})
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
        return jsonify({'ok': True, 'user': public(fresh), 'farm_action': None})
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
        amount_text = format_orbs(amount)
        transfer_text = f'{sender_name} передал(а) вам {format_orbs_word(amount)}'
        transfer_html = f'<tg-emoji emoji-id="{MONEY_TELEGRAM_EMOJI_ID}">💰</tg-emoji> {html.escape(transfer_text, quote=False)}'
        # Persist the notification first; Telegram delivery is retried by the
        # background sweeper if Telegram is temporarily unavailable.
        notify_user(target, 'Получены орбы', transfer_text, MONEY_TELEGRAM_EMOJI_ID, transfer_html)
        return jsonify({'ok':True,**result,'balance':result['sender_balance']})
    except Exception as e: return error(e,400)

@app.post('/api/vault/deposit')
def api_vault_deposit():
    try:
        user, u = current(request.get_json(silent=True) or {})
        if u.get('banned'):
            return error(ValueError('Аккаунт заблокирован'), 403)
        p = request.get_json(silent=True) or {}
        amount = int(p.get('amount', 0) or 0)
        result = database.vault_deposit(user['id'], amount)
        return jsonify({'ok': True, **result})
    except Exception as e:
        return error(e, 400)

@app.post('/api/vault/withdraw')
def api_vault_withdraw():
    try:
        user, u = current(request.get_json(silent=True) or {})
        if u.get('banned'):
            return error(ValueError('Аккаунт заблокирован'), 403)
        p = request.get_json(silent=True) or {}
        amount = int(p.get('amount', 0) or 0)
        result = database.vault_withdraw(user['id'], amount)
        return jsonify({'ok': True, **result})
    except Exception as e:
        return error(e, 400)

@app.post('/api/notifications/settings')
def api_notification_settings():
    try:
        user, u = current(request.get_json(silent=True) or {})
        payload = request.get_json(silent=True) or {}
        settings = database.set_notification_settings(user['id'], payload.get('settings') or {})
        return jsonify({'ok': True, 'settings': settings, 'labels': NOTIFICATION_CATEGORY_LABELS})
    except Exception as e:
        return error(e, 400)

@app.get('/api/notifications/settings')
def api_notification_settings_get():
    try:
        payload = request.args.to_dict()
        user, u = current(payload)
        return jsonify({'ok': True, 'settings': database.get_notification_settings(user['id']), 'labels': NOTIFICATION_CATEGORY_LABELS})
    except Exception as e:
        return error(e, 400)

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
        deleted = database.clear_notifications(user['id'])
        return jsonify({'ok':True,'deleted':deleted,'unread':0})
    except Exception as e: return error(e,400)

# Short-lived cache for prepared referral messages. Creating a Telegram
# PreparedInlineMessage requires Telegram to fetch the promo image, so doing it
# on every click makes the Share button feel slow. Reuse the same prepared
# message for a while; the referral URL is user-specific and stays unchanged.
REFERRAL_PREPARED_CACHE = {}
REFERRAL_PREPARED_CACHE_LOCK = threading.RLock()
REFERRAL_PREPARED_CACHE_TTL = 20 * 60

@app.post('/api/referrals/share')
def api_referrals_share():
    """Prepare a native Telegram share message with a real inline "Играть" button.

    Telegram Mini Apps expose WebApp.shareMessage() for PreparedInlineMessage
    objects created by savePreparedInlineMessage. This lets the shared message
    contain an actual inline keyboard instead of only a plain URL.
    """
    try:
        user, _ = current(request.get_json(silent=True) or {})
        if not BOT_TOKEN:
            raise ValueError('Telegram bot token не настроен.')
        global BOT_USERNAME
        if not BOT_USERNAME:
            r = requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getMe', timeout=5)
            payload = r.json() if r.ok else {}
            BOT_USERNAME = str((payload.get('result') or {}).get('username') or '').strip().lstrip('@')
        if not BOT_USERNAME:
            raise ValueError('Не удалось определить бота.')

        ref_id = int(user['id'])

        # Reuse a recently prepared message so Telegram is not asked to fetch
        # the promo image again every time the user taps Share.
        now_ts = time.time()
        with REFERRAL_PREPARED_CACHE_LOCK:
            cached = REFERRAL_PREPARED_CACHE.get(ref_id)
            if cached and cached.get('expires_at', 0) > now_ts:
                return jsonify({
                    'ok': True,
                    'id': cached['id'],
                    'expires_at': cached.get('telegram_expiration')
                })

        if WEBAPP_SHORT_NAME:
            play_url = f'https://t.me/{BOT_USERNAME}/{WEBAPP_SHORT_NAME}?startapp=ref_{ref_id}'
        else:
            play_url = f'https://t.me/{BOT_USERNAME}?startapp=ref_{ref_id}'

        text = 'Хочешь поиграть без настоящих денег и ты фанат Geometry Dash?\n\nТебе сюда!\n\nЗдесь ты можешь собирать кубы своих кумиров, сражаться в батлах и бороться за первое место в рейтинге!'
        # Telegram custom emoji is embedded in the caption using its emoji-id.
        caption = '<tg-emoji emoji-id=\"5456309534962234647\">🎮</tg-emoji> ' + text
        # Prefer the configured public URL. Otherwise use this request's public
        # Railway host instead of a stale hard-coded domain.
        public_base = (os.getenv('PUBLIC_API_URL') or request.host_url).strip().rstrip('/')
        photo_url = public_base + '/promo/referral_share.jpg'
        result = {
            'type': 'photo',
            'id': f'gdplay_ref_{ref_id}',
            'photo_url': photo_url,
            'thumbnail_url': photo_url,
            'photo_width': 1536,
            'photo_height': 635,
            'title': 'GDPLAY',
            'description': text,
            'caption': caption,
            'parse_mode': 'HTML',
            'reply_markup': {
                'inline_keyboard': [[
                    {'text': 'Играть', 'url': play_url}
                ]]
            }
        }
        payload = {
            'user_id': ref_id,
            'result': json.dumps(result, ensure_ascii=False, separators=(',', ':')),
            'allow_user_chats': True,
            'allow_group_chats': True,
            'allow_channel_chats': True
        }
        r = requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/savePreparedInlineMessage', json=payload, timeout=8)
        data_json = r.json() if r.content else {}
        if not r.ok or not data_json.get('ok'):
            desc = str((data_json.get('description') if isinstance(data_json, dict) else '') or 'Telegram не смог подготовить сообщение.')
            raise ValueError(desc)
        prepared = data_json.get('result') or {}
        msg_id = str(prepared.get('id') or '').strip()
        if not msg_id:
            raise ValueError('Telegram не вернул идентификатор сообщения.')
        telegram_expiration = prepared.get('expiration_date')
        with REFERRAL_PREPARED_CACHE_LOCK:
            REFERRAL_PREPARED_CACHE[ref_id] = {
                'id': msg_id,
                'expires_at': time.time() + REFERRAL_PREPARED_CACHE_TTL,
                'telegram_expiration': telegram_expiration,
            }
        return jsonify({'ok': True, 'id': msg_id, 'expires_at': telegram_expiration})
    except Exception as e:
        return error(e, 400)

@app.post('/api/referrals')
def api_referrals():
    try:
        user, u = current(request.get_json(silent=True) or {})
        global BOT_USERNAME
        stats = database.get_referral_stats(user['id'])
        milestones = database.ensure_referral_milestone_rewards(user['id'])
        # Веб-приложение может запросить ссылку раньше, чем поток aiogram успеет
        # выполнить on_startup. В этом случае один раз получаем username через Bot API.
        if not BOT_USERNAME and BOT_TOKEN:
            try:
                r = requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getMe', timeout=5)
                payload = r.json() if r.ok else {}
                BOT_USERNAME = str((payload.get('result') or {}).get('username') or '').strip().lstrip('@')
            except Exception:
                BOT_USERNAME = ''
        ref_id=int(user["id"])
        if not BOT_USERNAME:
            # ВАЖНО: раньше здесь при отсутствии BOT_USERNAME отдавалась ссылка
            # вида `{WEBAPP_URL}/?ref={id}`. Она выглядела рабочей, но реферал
            # по ней никогда не засчитывался: Telegram Mini App не прокидывает
            # произвольные query-параметры сайта внутрь initData, а наш сервер
            # нигде их и не читал. Единственный формат, который Telegram
            # подписывает и доставляет на сервер как start_param — это
            # ?start=/?startapp= в ссылке на самого бота, поэтому без
            # известного username бота рабочую реферальную ссылку выдать
            # невозможно и нужно попросить повторить попытку.
            raise ValueError('Не удалось получить ссылку. Повтори через несколько секунд.')
        link = f'https://t.me/{BOT_USERNAME}?start=ref_{ref_id}'
        return jsonify({'ok':True,'count':stats['count'],'earned':stats['earned'],'daily_count':stats.get('daily_count',0),'daily_limit':stats.get('daily_limit',5),'people':stats.get('people',[]),'reward':database.REFERRAL_REWARD,'link':link,'milestones':[{'count':10,'frame':'cyan','name':'Циановая','unlocked':stats['count']>=10},{'count':50,'frame':'sapphire','name':'Сапфировая','unlocked':stats['count']>=50},{'count':100,'frame':'aurora','name':'Аврора','unlocked':stats['count']>=100}],'next_milestone':milestones.get('next')})
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
            premium_until = r.get('premium_until')
            premium_active = _premium_active_value(premium_until)
            r['profile_name_style'] = (db_style or styles.get(str(r.get('user_id')), 'default')) if premium_active else 'default'
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
        now=time.time()
        row=_ensure_bonus_12h(int(user['id']), now)
        reset=False
        available_at=float(row.get('available_at',0) or 0)
        expires_at=float(row.get('expires_at',0) or 0)
        if str(row.get('status') or 'available') == 'available' and available_at <= now and expires_at > 0 and expires_at <= now:
            reset=_reset_expired_bonus_12h(int(user['id']), now, notify=True)
            row=_ensure_bonus_12h(int(user['id']), now)
            available_at=float(row.get('available_at',0) or 0)
            expires_at=float(row.get('expires_at',0) or 0)
        streak=min(14,max(0,int(row.get('total_claims',0) or 0)))
        status=str(row.get('status') or 'available')
        available=bool(status == 'available' and streak < 14 and available_at <= now < expires_at)
        waiting=bool(status == 'available' and streak < 14 and available_at > now)
        fresh=database.get_user(user['id'])
        premium_until=fresh.get('premium_until')
        is_premium=_premium_active_value(premium_until)
        rewards=_bonus_reward_catalog()
        reward=rewards[streak] * (2 if is_premium else 1) if streak < 14 else 0
        return jsonify({'ok':True,'last_daily':int(float(row.get('last_claim',0) or 0)*1000),
            'next_daily':int((available_at if waiting else expires_at)*1000) if (available or waiting) else 0,
            'available_at':int(available_at*1000) if (available or waiting) else 0,
            'expires_at':int(expires_at*1000) if available else 0,
            'streak':streak,'claims_in_day':0,'reward':reward,'premium':is_premium,
            'available':available,'waiting':waiting,'completed':streak>=14,'reset':bool(reset)})
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
        return jsonify({'ok': True, 'balance': result['balance'], 'reward': result['reward'], 'streak': result['streak'], 'claims_in_day': result['claims_in_day'], 'premium': is_premium, 'last_daily': result['last_daily'], 'next_daily': result['next_daily'], 'available_at': result.get('available_at', result['next_daily']), 'expires_at': result.get('expires_at', 0), 'available': False, 'waiting': bool(result.get('next_daily', 0)), 'farm_action': None})
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
        # Shared account-wide guard: a second phone cannot run an unlimited
        # open/sell loop against the same Telegram account.
        database.guard_cube_economy_action(user['id'], 'open')
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

        if case_id == 'hall':
            guarantee_items = [x for x in case['items'] if str(x.get('name')) == 'Kuro' and str(x.get('rarity')).lower() == 'emerald']
            pity_limit = 50
        else:
            guarantee_items = [x for x in case['items'] if str(x.get('rarity')).lower() == 'divine']
            pity_limit = 25
        result = database.case_open_batch(
            user['id'], 0 if use_key else price, drops,
            request_id=request_id, case_id=case_id,
            pity_config={'limit': pity_limit, 'items': guarantee_items}
        )
        drops = result.get('drops') or drops
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
                'case_pity': result.get('pity', database.get_case_pity(user['id'])),
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
        return jsonify({
            'ok': True,
            'balance': new_balance,
            'cubes': database.get_cubes(user['id']),
            'keys': [],
            'keys_added': keys_added,
            'cases_opened': fresh.get('cases_opened', 0),
            'best_drops': json.loads(fresh.get('best_drops') or '[]'),
            'drops': drops,
            'farm_action': None
        })
    except Exception as e:
        return error(e, 400)

@app.get('/api/cases')
def api_cases_catalog():
    try:
        # Return the authoritative server catalog, including admin-added cubes.
        return jsonify({'ok': True, 'cases': list(CASES_BY_ID.values())})
    except Exception as e:
        return error(e, 500)

@app.post('/api/admin/case-items')
def admin_case_items():
    try:
        admin(request.get_json(silent=True) or {})
        rows = database.get_case_custom_items()
        cases = [{'id': str(c.get('id')), 'name': str(c.get('name') or c.get('id'))} for c in CASES_BY_ID.values()]
        return jsonify({'ok': True, 'cases': cases, 'items': rows})
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/case-item/delete')
def admin_case_item_delete():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        item_id = int(p.get('id') or 0)
        if item_id <= 0:
            raise ValueError('Не указан куб')
        row = database.delete_case_custom_item(item_id)
        if not row:
            raise ValueError('Добавленный админом куб не найден')
        case = CASES_BY_ID.get(str(row.get('case_id')))
        if case:
            case['items'] = [x for x in case.get('items', []) if int(x.get('custom_id') or 0) != item_id]
        database.add_admin_log(a[0]['id'], None, 'Удаление куба из кейса', f"{row.get('case_id')}/{row.get('name')}/{item_id}")
        return jsonify({'ok': True, 'deleted': row, 'cases': list(CASES_BY_ID.values())})
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/case-item')
def admin_case_item():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        case_id = str(p.get('case_id') or '').strip()
        case = CASES_BY_ID.get(case_id)
        if not case:
            raise ValueError('Кейс не найден')
        name = str(p.get('name') or '').strip()[:80]
        if not name:
            raise ValueError('Название куба не может быть пустым')
        rarity = str(p.get('rarity') or 'common').strip().lower()
        allowed = {'common','rare','epic','legendary','mythic','divine','emerald'}
        if rarity not in allowed:
            raise ValueError('Недопустимая редкость куба')
        value = max(1, min(50_000_000, int(p.get('value') or 0)))
        chance = max(0.0001, min(1000000.0, float(p.get('chance') or 1.0)))
        row_id = database.add_case_custom_item(case_id, name, rarity, value, chance)
        item = {'name':name,'rarity':rarity,'value':value,'chance':chance,'custom_id':row_id}
        case.setdefault('items', []).append(item)
        database.add_admin_log(a[0]['id'], None, 'Добавление куба в кейс', f'{case_id}/{name}/{rarity}/{value}/{chance}')
        return jsonify({'ok':True,'case_id':case_id,'item':item,'cases':list(CASES_BY_ID.values())})
    except Exception as e:
        return error(e,400)

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
        database.guard_cube_economy_action(user['id'], 'open')
        if use_key:
            consume_case_key(user['id'], case_id, 1)
        items_for_pick = database.adjust_case_items_for_bad_luck(case['items'], user['id'])
        item = weighted_pick({**case, 'items': items_for_pick})
        name = str(item['name'])
        rarity = str(item['rarity'])
        value = int(item['value'])
        if case_id == 'hall':
            guarantee_items = [x for x in case['items'] if str(x.get('name')) == 'Kuro' and str(x.get('rarity')).lower() == 'emerald']
            pity_limit = 50
        else:
            guarantee_items = [x for x in case['items'] if str(x.get('rarity')).lower() == 'divine']
            pity_limit = 25
        result = database.case_open_batch(
            user['id'], 0 if use_key else price,
            [{'name': name, 'rarity': rarity, 'value': value, 'case_id': case_id}],
            request_id=request_id, case_id=case_id,
            pity_config={'limit': pity_limit, 'items': guarantee_items}
        )
        saved_drop = (result.get('drops') or [{}])[0]
        name = str(saved_drop.get('name') or name)
        rarity = str(saved_drop.get('rarity') or rarity)
        value = int(saved_drop.get('value') or value)
        if result.get('duplicate'):
            fresh = database.get_user(user['id']) or {}
            saved_drop = (result.get('drops') or [{}])[0]
            return jsonify({'ok': True, 'balance': result['balance'], 'drop': saved_drop, 'cubes': database.get_cubes(user['id']), 'keys': [], 'key': None, 'cases_opened': fresh.get('cases_opened',0), 'best_drops': json.loads(fresh.get('best_drops') or '[]'), 'case_pity': database.get_case_pity(user['id']), 'duplicate': True})
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
        return jsonify({'ok': True, 'balance': new_balance, 'cubes': cubes,
                        'keys': [],
                        'key': key,
                        'cases_opened': fresh.get('cases_opened', 0),
                        'best_drops': json.loads(fresh.get('best_drops') or '[]'),
                        'drop': {'name':name,'rarity':rarity,'value':value,'case_id':case_id},
                        'case_pity': result.get('pity', database.get_case_pity(user['id'])),
                        'farm_action': None})
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
        # Use the immutable cube id, never a stale inventory index from another device.
        cube_id_raw = p.get('cube_id')
        if cube_id_raw is None or str(cube_id_raw).strip() == '':
            idx = int(p.get('index', -1))
            cubes = database.get_cubes(user['id'])
            if idx < 0 or idx >= len(cubes):
                raise ValueError('Куб не найден в инвентаре')
            cube_id = int(cubes[idx]['id'])
        else:
            cube_id = int(cube_id_raw)
        database.guard_cube_economy_action(user['id'], 'sell')
        balance, gain = database.sell_cube(user['id'], cube_id)
        fresh_cubes = database.get_cubes(user['id'])
        return jsonify({'ok': True, 'balance': balance, 'gain': gain, 'cubes': fresh_cubes})
    except Exception as e:
        return error(e, 400)

@app.post('/api/cube/sell-all')
def api_cube_sell_all():
    try:
        user, u = current(request.get_json(silent=True) or {})
        database.guard_cube_economy_action(user['id'], 'sell-all')
        balance, gain, count = database.sell_all_cubes(user['id'])
        return jsonify({'ok': True, 'balance': balance, 'gain': gain, 'sold': count, 'cubes': database.get_cubes(user['id'])})
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
        user, db_user = current(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        key = str(p.get('style') or 'default').strip().lower()
        if key not in _NAME_STYLE_KEYS:
            raise ValueError('Неизвестный цвет имени')
        # current() returns a compact Telegram identity as `user`; the actual
        # premium_until value lives in the database row returned as db_user.
        # Using user.get('premium_until') here made every non-default style
        # look like Premium had expired after a fresh login.
        premium_until = (db_user or {}).get('premium_until')
        premium_active = _premium_active_value(premium_until)
        # Любой декоративный стиль имени доступен только при активном Premium.
        # После окончания Premium стиль автоматически сбрасывается на обычный.
        if key != 'default' and not premium_active:
            database.set_profile_name_style(user['id'], 'default')
            return jsonify({'ok': True, 'profile_name_style': 'default', 'premium_required': True})
        database.set_profile_name_style(user['id'], key)
        # Дублируем сохранение в отдельное хранилище стилей для старых баз/сборок.
        # Основным источником остаётся users.profile_name_style, JSON — только fallback.
        try:
            styles = _load_name_styles()
            styles[str(user['id'])] = key
            _save_name_styles(styles)
        except Exception as style_store_error:
            print(f'[PROFILE STYLE] fallback store error: {style_store_error}')
        return jsonify({'ok': True, 'profile_name_style': key})
    except Exception as e:
        return error(e, 400)

@app.get('/api/profile/rewards')
def api_profile_rewards():
    try:
        user,db_user=current(request.args.to_dict())
        # Repair/grant referral milestone frames before returning the catalog so
        # old accounts that already reached 10/50/100 see the new frames too.
        database.ensure_referral_milestone_rewards(user['id'])
        frames=[]
        owned=set(database.get_available_profile_frames(user['id']))
        fresh=db_user or {}
        premium_until=fresh.get('premium_until')
        premium_active=_premium_active_value(premium_until)
        if premium_active:
            owned.add('premium')
        for k,v in database.PROFILE_FRAMES.items():
            if k=='default': continue
            frames.append({'key':k, **v, 'owned': k in owned})
        return jsonify({'ok':True,'frames':frames,'styles':list(database.PROFILE_STYLES)})
    except Exception as e:
        return error(e,400)

@app.post('/api/admin/profile-reward')
def admin_profile_reward():
    """Grant/revoke a profile frame or name style without affecting other admin rewards."""
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        uid = int(p.get('user_id') or 0)
        if uid <= 0:
            raise ValueError('Некорректный user_id')

        # Accept both payload names used by the existing admin UI and the newer UI.
        typ = str(p.get('reward_type') or p.get('type') or 'frame').strip().lower()
        key = str(p.get('key') or '').strip().lower()
        action = str(p.get('action') or 'grant').strip().lower()
        if typ not in ('frame', 'style'):
            raise ValueError('Неверный тип награды')
        if typ == 'frame':
            if key not in database.PROFILE_FRAMES:
                raise ValueError('Рамка не найдена')
        else:
            if key not in database.PROFILE_STYLES:
                raise ValueError('Стиль не найден')

        if action in ('remove', 'revoke', 'delete'):
            database.revoke_profile_reward(uid, typ, key)
            if typ == 'frame' and database.get_profile_frame(uid) == key:
                database.set_profile_frame(uid, 'default')
            if typ == 'style' and database.get_profile_name_style(uid) == key:
                database.set_profile_name_style(uid, 'default')
            result_action = 'remove'
        else:
            database.grant_profile_reward(uid, typ, key)
            if typ == 'frame':
                database.set_profile_frame(uid, key)
            else:
                database.set_profile_name_style(uid, key)
            result_action = 'grant'

        database.add_admin_log(a[0]['id'], uid, 'Профильная награда', f'{result_action}: {typ}:{key}')
        return jsonify({'ok': True, 'action': result_action, 'reward_type': typ, 'key': key})
    except Exception as e:
        return error(e, 400)

@app.post('/api/admin/profile-reward/remove')
def admin_profile_reward_remove():
    try:
        a=admin(request.get_json(silent=True) or {})
        p=request.get_json(silent=True) or {}
        target=int(p.get('user_id') or 0)
        typ=str(p.get('reward_type') or 'frame')
        key=str(p.get('key') or '').strip().lower()
        if typ not in ('frame','style'):
            raise ValueError('Неверный тип награды')
        database.revoke_profile_reward(target, typ, key)
        if typ=='frame' and database.get_profile_frame(target)==key:
            database.set_profile_frame(target,'default')
        if typ=='style':
            database.set_profile_name_style(target,'default')
        return jsonify({'ok':True})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)}),400

@app.post('/api/profile/frame')
def api_profile_frame():
    try:
        user, db_user = current(request.get_json(silent=True) or {})
        p=request.get_json(silent=True) or {}
        key=str(p.get('frame') or 'default').strip().lower()
        allowed=set(database.PROFILE_FRAMES.keys())
        if key not in allowed:
            raise ValueError('Неизвестная рамка')
        uid=int(user['id'])
        if key == 'default':
            database.set_profile_frame(uid, key)
            return jsonify({'ok':True,'profile_frame':key})
        # Referral milestone frames are real unlocks, not merely cosmetic buttons.
        database.ensure_referral_milestone_rewards(uid)
        if key in ('cyan','sapphire','aurora'):
            thresholds={'cyan':10,'sapphire':50,'aurora':100}
            if database.get_successful_referral_count(uid) < thresholds[key]:
                raise ValueError(f'Нужно {thresholds[key]} успешных приглашений')
        elif key == 'premium':
            premium_until=db_user.get('premium_until')
            premium_active=_premium_active_value(premium_until)
            if not premium_active and not database.user_has_profile_reward(uid, 'frame', 'premium'):
                raise ValueError('Premium-рамка доступна только при активном Premium или админской выдаче')
            if not database.user_has_profile_reward(uid,'frame',key):
                raise PermissionError('Рамка ещё не получена')
        elif not database.user_has_profile_reward(uid,'frame',key):
            # Legacy frames still require their existing achievement/reward unlock.
            cases=int(db_user.get('cases_opened') or 0)
            balance=int(db_user.get('balance') or 0)
            streak=int(db_user.get('daily_streak') or 0)
            conditions={'gold':cases>=25,'emerald':cases>=100,'ruby':cases>=2000,'amethyst':balance>=2500000,'ice':streak>=28,'obsidian':cases>=1000,'neon':balance>=100000000}
            if not conditions.get(key, False):
                raise ValueError('Рамка ещё не разблокирована')
        database.set_profile_frame(uid, key)
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
        return jsonify({'ok': True, 'players': database.count_users(), 'online': database.count_online_users()})
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
        # All admin actions must pass the same server-side gate: exact Telegram
        # admin ID + short-lived admin session. Nothing in the browser can bypass it.
        p=request.get_json(silent=True) or {}
        admin(p)
        action=str(p.get('action') or '').strip().lower()
        if action=='enable':
            reason=str(p.get('reason') or '').strip()[:500]
            if len(reason)<3: raise ValueError('Укажи причину тех. перерыва')
            previous_break = database.get_global_tech_break()
            database.set_global_tech_break(True,reason)
            if previous_break and previous_break.get('enabled'):
                threading.Thread(target=notify_global_tech_break_reason_changed, args=(reason,), name='tech-break-reason-notify', daemon=True).start()
            else:
                threading.Thread(target=notify_global_tech_break, args=(reason,), name='tech-break-notify', daemon=True).start()
            return jsonify({'ok':True,'global_tech_break':database.get_global_tech_break()})
        if action=='clear':
            previous_break = database.get_global_tech_break()
            database.set_global_tech_break(False,'')
            if previous_break and previous_break.get('enabled'):
                threading.Thread(target=notify_global_tech_break_end, args=(previous_break.get('reason',''),), name='tech-break-end-notify', daemon=True).start()
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
            value = int(p.get('value', 0))
            database.set_balance(target, value)
            details = f'Баланс установлен: {value}'
        elif action == 'vault':
            value = int(p.get('value', 0))
            value = database.admin_set_vault(target, value)
            details = f'Сейф установлен: {value}'
        elif action == 'cases':
            value = max(0, int(p.get('value', 0)))
            value = database.admin_set_cases(target, value)
            details = f'Кейсы в топе установлены: {value}'
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
            previous_break = database.get_global_tech_break()
            database.set_global_tech_break(True, reason)
            if previous_break and previous_break.get('enabled'):
                threading.Thread(target=notify_global_tech_break_reason_changed, args=(reason,), name='tech-break-reason-notify', daemon=True).start()
                details = f'Причина глобального тех. перерыва изменена: {reason}'
            else:
                threading.Thread(target=notify_global_tech_break, args=(reason,), name='tech-break-notify', daemon=True).start()
                details = f'Глобальный тех. перерыв включён: {reason}'
        elif action == 'clear_tech_break':
            previous_break = database.get_global_tech_break()
            database.set_global_tech_break(False, '')
            if previous_break and previous_break.get('enabled'):
                threading.Thread(target=notify_global_tech_break_end, args=(previous_break.get('reason',''),), name='tech-break-end-notify', daemon=True).start()
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
        if val:
            try:
                safe_reason = html.escape(reason, quote=False)
                notify_user(target, 'Бан', f'Аккаунт заблокирован. Причина: {reason}', '5467726558762391390',
                            f'<tg-emoji emoji-id="5467726558762391390">🚫</tg-emoji> <b>Аккаунт заблокирован</b>\nПричина: {safe_reason}')
            except Exception as notify_error:
                print(f'⚠️ Ban notification error: {notify_error}')
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
            try:
                notify_user(target, 'Теневой бан', 'На аккаунт наложен теневой бан.', '5467726558762391390',
                            '<tg-emoji emoji-id="5467726558762391390">🚫</tg-emoji> <b>Теневой бан</b>\nНа аккаунт наложен теневой бан.')
            except Exception as notify_error:
                print(f'⚠️ Shadow-ban notification error: {notify_error}')
        database.add_admin_log(a[0]['id'], target, 'Теневой бан' if val else 'Снятие теневого бана')
        return jsonify({'ok': True})
    except Exception as e:
        return error(e, 400)


@app.post('/api/admin/remove-cube')
def admin_remove_cube():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        target=int(p['user_id'])
        removed=database.remove_one_cube(target)
        if not removed: raise ValueError('У игрока нет кубов')
        database.add_admin_log(a[0]['id'],target,'Удаление куба','-1')
        return jsonify({'ok':True,'user':public(database.get_user(target))})
    except Exception as e: return error(e,400)

@app.post('/api/admin/cube')
def admin_cube():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        name = str(p.get('name', 'Куб')).strip()[:80]
        rarity = str(p.get('rarity', 'rare')).strip().lower()
        value = int(p.get('value', 0))
        # Keep the rarity selected in the admin panel.  Custom cubes may use
        # the full rarity scale, including emerald; never silently downgrade
        # an unsupported value to "rare".
        allowed_rarities = {'common','rare','epic','legendary','mythic','divine','emerald'}
        if rarity not in allowed_rarities:
            raise ValueError('Недопустимая редкость куба')
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

@app.post('/api/admin/delete-recent-users')
def admin_delete_recent_users():
    try:
        a=admin(request.get_json(silent=True) or {}); p=request.get_json(silent=True) or {}
        total=database.count_users()
        try:
            count=int(p.get('count'))
        except (TypeError, ValueError):
            raise ValueError('Укажи количество аккаунтов для удаления')
        if count<1:
            raise ValueError('Количество должно быть больше нуля')
        count=min(count, total)
        # 'kuro' — зарезервированный неприкосновенный аккаунт; собственный
        # аккаунт админа тоже всегда исключаем из выборки на случай, если
        # регистрация админа попадёт в ту же секунду, что и последние игроки.
        deleted_ids=database.delete_recent_users(count, exclude_usernames=['kuro'], exclude_user_ids=[a[0]['id']])
        database.add_admin_log(a[0]['id'], a[0]['id'], 'Массовое удаление аккаунтов', f'Удалено последних зарегистрированных: {len(deleted_ids)} (запрошено {count})')
        return jsonify({'ok':True, 'deleted': len(deleted_ids), 'total_players': database.count_users()})
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
        result=database.redeem_promo(p.get('code'),user['id']); return jsonify({'ok':True,'result':result,'fullscreen_slides':result.get('fullscreen_slides',[]),'slide_rewards':result.get('slide_rewards',[]),'user':public(database.get_user(user['id'])),'farm_action':None})
    except Exception as e: return error(e,400)

@app.post('/api/admin/promos')
def admin_promos():
    try:
        admin(request.get_json(silent=True) or {})
        promos=database.list_promos()
        for promo in promos:
            code=urllib.parse.quote(str(promo.get('code') or ''))
            if BOT_USERNAME:
                if WEBAPP_SHORT_NAME:
                    promo['promo_link']=f'https://t.me/{BOT_USERNAME}/{WEBAPP_SHORT_NAME}?startapp=promo_{code}'
                else:
                    promo['promo_link']=f'https://t.me/{BOT_USERNAME}?startapp=promo_{code}'
            else:
                promo['promo_link']=''
        return jsonify({'ok':True,'promos':promos})
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
        reward_type = 'both' if str(p.get('reward_type','coins')).lower() in ('coins_vip','both') else str(p.get('reward_type','coins')).lower()
        promo=database.create_promo(
            p.get('code'), reward_type, int(p.get('coins') or 0),
            int(p.get('vip_days') or 0), int(p.get('max_uses') or 1),
            a[0]['id'], fullscreen_slides=slides
        )
        database.add_admin_log(a[0]['id'],None,'Промокод',promo['code'])
        code=urllib.parse.quote(str(promo['code']))
        if BOT_USERNAME:
            if WEBAPP_SHORT_NAME:
                promo_link=f'https://t.me/{BOT_USERNAME}/{WEBAPP_SHORT_NAME}?startapp=promo_{code}'
            else:
                promo_link=f'https://t.me/{BOT_USERNAME}?startapp=promo_{code}'
        else:
            promo_link=''
        return jsonify({'ok':True,'promo':promo,'promo_link':promo_link})
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
        seller_id = int(result.get('seller_id') or 0)
        cube_name = str(result.get('cube_name') or 'Куб')
        price = int(result.get('seller_price') or result.get('purchase_price') or 0)
        if seller_id:
            sale_text = f'В магазине купили твой куб «{cube_name}» за {format_orbs_word(price)}'
            sale_html = f'<tg-emoji emoji-id="{MONEY_TELEGRAM_EMOJI_ID}">💰</tg-emoji> {html.escape(sale_text, quote=False)}'
            notify_user(
                seller_id,
                'Куб купили в магазине',
                sale_text,
                MONEY_TELEGRAM_EMOJI_ID,
                sale_html
            )
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
        if not u:
            raise ValueError('Пользователь не найден')
        # Check the authoritative server balance before attempting to join.
        # database.join_battle_record repeats this check inside its transaction
        # to protect against races, while this early check gives a clear error.
        stake = int(battle.get('stake') or 0)
        if int(u.get('balance') or 0) < stake:
            return error(ValueError('Недостаточно монет'), 409)
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
        battle_id = str(p['battle_id'])
        result = database.play_battle_record(battle_id, user['id'])
        u = database.get_user(user['id'])
        return jsonify({
            'ok': True, 
            'balance': u['balance'] if u else 0, 
            'cubes': database.get_cubes(user['id']),
            'message': f"🎲 Победил {result['winner_name']} и получил {result.get('payout', result['pot']):,} 💰".replace(',', ' '),
            'result': result,
            'farm_action': None
        })
    except Exception as e:
        return error(e, 400)

# --- Бот команды ---
if dp:
    def miniapp_direct_url():
        # Direct Mini App links can be opened from group chats too. When a
        # Main/named Mini App is configured in BotFather, Telegram supplies
        # signed initData to the WebApp just like the private-chat button.
        if BOT_USERNAME:
            if WEBAPP_SHORT_NAME:
                return f'https://t.me/{BOT_USERNAME}/{WEBAPP_SHORT_NAME}?startapp=play'
            return f'https://t.me/{BOT_USERNAME}?startapp=play'
        return WEBAPP_URL

    def build_main_keyboard(private_chat=True):
        kb = InlineKeyboardMarkup(row_width=1)
        if private_chat and WEBAPP_URL.startswith(('https://', 'http://')) and 'your-app.netlify.app' not in WEBAPP_URL:
            kb.add(InlineKeyboardButton('🎮 Играть', web_app=WebAppInfo(url=WEBAPP_URL)))
        else:
            # URL button is supported in groups; with a configured Main Mini App
            # it opens the Telegram Mini App instead of an external browser page.
            kb.add(InlineKeyboardButton('🎮 Играть', url=miniapp_direct_url()))
        kb.add(InlineKeyboardButton('📢 Подписаться', url='https://t.me/GDplay_off'))
        return kb

    async def send_main_menu(message: types.Message, referral_id=None):
        uid = message.from_user.id
        created = database.create_user(uid, message.from_user.username or message.from_user.first_name or str(uid))
        if created and referral_id is not None:
            try:
                ref_id=int(referral_id)
                result=database.register_referral(ref_id, uid, database.REFERRAL_REWARD)
                if result.get('created') and ref_id != uid:
                    _notify_referral_success(ref_id, uid, result)
                elif result.get('reason') == 'daily_limit':
                    print(f'⚠️ Реферальный лимит 5/день достигнут для {ref_id}')
            except Exception as referral_error:
                print(f'⚠️ Referral processing error: {referral_error}')
        await message.answer(
            '<tg-emoji emoji-id=\"5256165287329309225\">💖</tg-emoji> <b>GDPLAY</b>\n\n'
            '<tg-emoji emoji-id=\"5472055112702629499\">👋</tg-emoji> Добро пожаловать!\n\n'
            '<tg-emoji emoji-id=\"5226758131584888561\">🎮</tg-emoji> Нажми <b>Играть</b>, чтобы открыть игру',
            reply_markup=build_main_keyboard(message.chat.type == 'private'),
            parse_mode='HTML'
        )

    def _ensure_bot_user(message):
        uid=int(message.from_user.id)
        u=database.get_user(uid)
        if not u:
            database.create_user(uid, message.from_user.username or message.from_user.first_name or str(uid))
            u=database.get_user(uid)
        return uid,u

    def _shop_page(items, page=0, query=''):
        q=str(query or '').strip().lower()
        filtered=[x for x in items if not q or q in str(x.get('cube_name') or '').lower()]
        per=6; pages=max(1,(len(filtered)+per-1)//per); page=max(0,min(int(page),pages-1))
        chunk=filtered[page*per:(page+1)*per]
        return filtered,pages,chunk,page

    def _shop_keyboard(items, page=0, query=''):
        filtered,pages,chunk,page=_shop_page(items,page,query)
        kb=InlineKeyboardMarkup(row_width=2)
        for x in chunk:
            name=html.escape(str(x.get('cube_name') or 'Куб'))[:28]
            price=_fmt(x.get('price',0))
            kb.add(InlineKeyboardButton(f'🧊 {name} · {price} 💰', callback_data=f'shopbuy:{int(x["id"])}'))
        nav=[]
        if page>0: nav.append(InlineKeyboardButton('‹ Назад',callback_data=f'shop:{page-1}:{query[:30]}'))
        if page<pages-1: nav.append(InlineKeyboardButton('Вперёд ›',callback_data=f'shop:{page+1}:{query[:30]}'))
        if nav: kb.row(*nav)
        kb.row(InlineKeyboardButton('🔎 Поиск по названию',callback_data='shopsearch'))
        return kb,pages,page,len(filtered)

    def _shop_text(items,page=0,query=''):
        filtered,pages,chunk,page=_shop_page(items,page,query)
        title='🛒 <b>GDPLAY · МАГАЗИН</b>'
        sub=f'Страница <b>{page+1}/{pages}</b>' + (f' · поиск: <b>{html.escape(query)}</b>' if query else '')
        if not chunk: body='\n\n😔 Подходящих кубов пока нет.'
        else:
            lines=['']
            for x in chunk:
                lines.append(f'🧊 <b>{html.escape(str(x.get("cube_name") or "Куб"))}</b> · {html.escape(str(x.get("cube_rarity") or "common"))}\n💰 <b>{_fmt(x.get("price",0))}</b> орбов')
            body='\n'.join(lines)
        return title+'\n\n'+sub+body

    # /send flow: /send @username -> ask for the amount -> transfer the
    # authoritative balance and send the same Telegram notification as the
    # WebApp transfer endpoint. State is short-lived and scoped per sender.
    _pending_bot_transfers = {}
    _pending_bot_transfers_lock = threading.Lock()
    BOT_TRANSFER_STATE_TTL = 5 * 60

    @dp.message_handler(commands=['send'])
    async def send_command(message: types.Message):
        uid, _ = _ensure_bot_user(message)
        raw_target = str(message.get_args() or '').strip()
        if not raw_target:
            await message.answer('❌ Укажи получателя. Пример: <code>/send @user</code>', parse_mode='HTML')
            return
        username = raw_target.split()[0].strip()
        if not username.startswith('@'):
            await message.answer('❌ Получатель должен быть указан через @username. Пример: <code>/send @user</code>', parse_mode='HTML')
            return
        username = username[1:].strip()
        if not username:
            await message.answer('❌ Укажи username получателя.', parse_mode='HTML')
            return
        target = database.find_user_by_username(username)
        if not target:
            await message.answer('❌ Игрок с таким username не найден.')
            return
        target_id = int(target['user_id'])
        if target_id == int(uid):
            await message.answer('❌ Нельзя отправить орбы самому себе.')
            return
        with _pending_bot_transfers_lock:
            _pending_bot_transfers[int(uid)] = {'target_id': target_id, 'target_name': effective_name(target), 'expires': time.time() + BOT_TRANSFER_STATE_TTL}
        await message.answer(
            f'💰 <b>Перевод орбов</b>\n\nКому: <b>{html.escape(effective_name(target))}</b>\n\n'
            'Сколько хотите отправить?\n\n'
            'Введите целое число орбов.',
            parse_mode='HTML'
        )

    @dp.message_handler(lambda message: int(message.from_user.id) in _pending_bot_transfers)
    async def send_amount_message(message: types.Message):
        uid = int(message.from_user.id)
        if message.text and message.text.startswith('/'):
            with _pending_bot_transfers_lock:
                _pending_bot_transfers.pop(uid, None)
            await message.answer('❌ Перевод отменён. Сначала снова используй <code>/send @user</code>.', parse_mode='HTML')
            return
        with _pending_bot_transfers_lock:
            state = _pending_bot_transfers.get(uid)
            if state and float(state.get('expires', 0)) < time.time():
                _pending_bot_transfers.pop(uid, None)
                state = None
        if not state:
            await message.answer('❌ Сессия перевода истекла. Используй <code>/send @user</code> ещё раз.', parse_mode='HTML')
            return
        raw_amount = str(message.text or '').strip().replace(' ', '').replace(',', '')
        try:
            amount = int(raw_amount)
        except Exception:
            await message.answer('❌ Введи целое число орбов, например: <code>1000</code>', parse_mode='HTML')
            return
        if amount < 1:
            await message.answer('❌ Сумма должна быть не меньше 1 орба.')
            return
        try:
            result = database.transfer_balance(uid, int(state['target_id']), amount)
            sender_row = database.get_user(uid) or {}
            sender_name = effective_name(sender_row)
            amount_text = format_orbs(amount)
            transfer_text = f'{sender_name} передал(а) вам {format_orbs_word(amount)}'
            transfer_html = f'<tg-emoji emoji-id="{MONEY_TELEGRAM_EMOJI_ID}">💰</tg-emoji> {html.escape(transfer_text, quote=False)}'
            notify_user(int(state['target_id']), 'Получены орбы', transfer_text, MONEY_TELEGRAM_EMOJI_ID, transfer_html)
            with _pending_bot_transfers_lock:
                _pending_bot_transfers.pop(uid, None)
            await message.answer(
                f'✅ <b>Перевод выполнен</b>\n\nОтправлено: <b>{html.escape(amount_text)} орбов</b>\n'
                f'Получатель: <b>{html.escape(str(state["target_name"]))}</b>\n\n'
                f'Ваш баланс: <b>{format_orbs(result["sender_balance"])}</b> орбов',
                parse_mode='HTML'
            )
        except Exception as e:
            await message.answer('❌ ' + html.escape(str(e)[:250]), parse_mode='HTML')

    # -------------------- /premium --------------------
    @dp.message_handler(commands=['premium'])
    async def premium_command(message: types.Message):
        _ensure_bot_user(message)
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton('⭐ Premium на 30 дней — 20 Stars', callback_data='premiumbuy:30'),
            InlineKeyboardButton('⭐ Premium на 60 дней — 35 Stars', callback_data='premiumbuy:60'),
            InlineKeyboardButton('⭐ Premium на 90 дней — 45 Stars', callback_data='premiumbuy:90'),
        )
        await message.answer(
            '⭐ <b>GDPLAY PREMIUM</b>\n\n'
            'Выбери срок подписки. Оплата проходит через Telegram Stars.\n\n'
            '• x2 ежедневный бонус\n'
            '• Premium-статус в профиле\n'
            '• Premium-бонусы и дополнительные возможности',
            parse_mode='HTML',
            reply_markup=kb
        )

    @dp.callback_query_handler(lambda c: str(c.data or '').startswith('premiumbuy:'))
    async def premium_buy_callback(call: types.CallbackQuery):
        try:
            days = int(str(call.data).split(':', 1)[1])
            prices = {30: 20, 60: 35, 90: 45}
            stars = prices.get(days)
            if stars is None:
                raise ValueError('Недопустимый срок Premium')
            if not BOT_TOKEN:
                raise ValueError('BOT_TOKEN не настроен')
            payload = f'premium_{days}_{int(call.from_user.id)}_{secrets.token_urlsafe(8)}'
            response = requests.post(
                f'https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink',
                json={
                    'title': f'Premium {days} дней',
                    'description': f'Premium на {days} дней · {stars} Telegram Stars',
                    'payload': payload,
                    'currency': 'XTR',
                    'prices': [{'label': f'Premium {days} дней', 'amount': stars}],
                },
                timeout=10,
            ).json()
            if not response.get('ok'):
                raise ValueError('Telegram не смог создать счёт на оплату')
            invoice = response.get('result')
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(InlineKeyboardButton(f'💳 Оплатить {stars} Stars', url=invoice))
            kb.add(InlineKeyboardButton('◀️ Назад', callback_data='premium:back'))
            await call.message.edit_text(
                f'⭐ <b>Premium на {days} дней</b>\n\nСтоимость: <b>{stars} Telegram Stars</b>\n\n'
                'Нажми кнопку ниже для оплаты.',
                parse_mode='HTML',
                reply_markup=kb
            )
            await call.answer()
        except Exception as exc:
            await call.answer('❌ ' + str(exc)[:180], show_alert=True)

    @dp.callback_query_handler(lambda c: str(c.data or '') == 'premium:back')
    async def premium_back_callback(call: types.CallbackQuery):
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton('⭐ Premium на 30 дней — 20 Stars', callback_data='premiumbuy:30'),
            InlineKeyboardButton('⭐ Premium на 60 дней — 35 Stars', callback_data='premiumbuy:60'),
            InlineKeyboardButton('⭐ Premium на 90 дней — 45 Stars', callback_data='premiumbuy:90'),
        )
        await call.message.edit_text(
            '⭐ <b>GDPLAY PREMIUM</b>\n\nВыбери срок подписки.',
            parse_mode='HTML',
            reply_markup=kb
        )
        await call.answer()

    @dp.message_handler(commands=['balance'])
    async def balance_command(message: types.Message):
        uid,u=_ensure_bot_user(message)
        name=html.escape(str(u.get('display_name') or u.get('username') or message.from_user.first_name or 'Игрок'))
        kb=InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton('🎮 Открыть GDPLAY',url=miniapp_direct_url()))
        kb.row(InlineKeyboardButton('🛒 Магазин',callback_data='shop:0:'),InlineKeyboardButton('🎁 Кейсы',callback_data='case:menu'))
        text_msg=(f'<tg-emoji emoji-id="5256165287329309225">💖</tg-emoji> <b>GDPLAY · БАЛАНС</b>\n\n'
                  f'👤 <b>{name}</b>\n\n💰 Баланс\n<b>{_fmt(u.get("balance"))}</b> орбов\n\n'
                  f'📦 Открыто кейсов: <b>{_fmt(u.get("cases_opened"))}</b>')
        await message.answer(text_msg,parse_mode='HTML',reply_markup=kb)

    @dp.message_handler(commands=['shop'])
    async def shop_command(message: types.Message):
        uid,u=_ensure_bot_user(message)
        q=str(message.get_args() or '').strip()[:40]
        items=database.get_shop_items_public(uid)
        kb,_,_,_= _shop_keyboard(items,0,q)
        await message.answer(_shop_text(items,0,q),parse_mode='HTML',reply_markup=kb)

    @dp.message_handler(commands=['case'])
    async def case_command(message: types.Message):
        _ensure_bot_user(message)
        kb=InlineKeyboardMarkup(row_width=1)
        for cid,c in CASES_BY_ID.items():
            kb.add(InlineKeyboardButton(f'🎁 {c["name"]} · {_fmt(c["price"])} 💰',callback_data=f'case:{cid}'))
        await message.answer('🎁 <b>GDPLAY · КЕЙСЫ</b>\n\nВыбери кейс, который хочешь открыть:',parse_mode='HTML',reply_markup=kb)

    @dp.callback_query_handler(lambda c: str(c.data or '').startswith('shop:') or str(c.data or '')=='shopsearch')
    async def shop_callback(call: types.CallbackQuery):
        data_cb=str(call.data or '')
        if data_cb=='shopsearch':
            await call.answer('Напиши название куба после команды: /shop название',show_alert=True); return
        _,pg,*rest=data_cb.split(':',2); query=rest[0] if rest else ''
        items=database.get_shop_items_public(call.from_user.id)
        kb,_,_,_= _shop_keyboard(items,int(pg or 0),query)
        await call.message.edit_text(_shop_text(items,int(pg or 0),query),parse_mode='HTML',reply_markup=kb)
        await call.answer()

    @dp.callback_query_handler(lambda c: str(c.data or '').startswith('shopbuy:'))
    async def shop_buy_callback(call: types.CallbackQuery):
        try:
            result=database.buy_shop_item_atomic(int(str(call.data).split(':',1)[1]),int(call.from_user.id))
            # The WebApp purchase path already sends this notification. The bot
            # purchase path must do the same, otherwise the seller only sees the
            # balance change in the app and gets no Telegram notification.
            seller_id = int(result.get('seller_id') or 0)
            cube_name = str(result.get('cube_name') or 'Куб')
            price = int(result.get('seller_price') or result.get('purchase_price') or 0)
            if seller_id:
                sale_text = f'В магазине купили твой куб «{cube_name}» за {format_orbs_word(price)}'
                sale_html = f'<tg-emoji emoji-id="{MONEY_TELEGRAM_EMOJI_ID}">💰</tg-emoji> {html.escape(sale_text, quote=False)}'
                notify_user(seller_id, 'Куб купили в магазине', sale_text, MONEY_TELEGRAM_EMOJI_ID, sale_html)
            await call.answer('✅ Куб куплен!',show_alert=False)
            items=database.get_shop_items_public(call.from_user.id)
            kb,_,_,_= _shop_keyboard(items,0,'')
            await call.message.edit_text(_shop_text(items,0,''),parse_mode='HTML',reply_markup=kb)
        except Exception as e:
            await call.answer('❌ '+str(e)[:180],show_alert=True)

    @dp.callback_query_handler(lambda c: str(c.data or '').startswith('case:'))
    async def case_callback(call: types.CallbackQuery):
        cid=str(call.data).split(':',1)[1]
        if cid=='menu':
            kb=InlineKeyboardMarkup(row_width=1)
            for case_id,c in CASES_BY_ID.items(): kb.add(InlineKeyboardButton(f'🎁 {c["name"]} · {_fmt(c["price"])} 💰',callback_data=f'case:{case_id}'))
            await call.message.edit_text('🎁 <b>GDPLAY · КЕЙСЫ</b>\n\nВыбери кейс:',parse_mode='HTML',reply_markup=kb); await call.answer(); return
        c=CASES_BY_ID.get(cid)
        if not c: await call.answer('Кейс не найден',show_alert=True); return
        kb=InlineKeyboardMarkup(row_width=1)
        _,u=_ensure_bot_user(call)
        balance=int(u.get('balance') or 0) if u else 0
        price=int(c.get('price') or 0)
        if balance >= price:
            case_web_url = f'{WEBAPP_URL.rstrip("/")}/?case={urllib.parse.quote(cid)}' if WEBAPP_URL else ''
            if case_web_url and str(call.message.chat.type) == 'private':
                kb.add(InlineKeyboardButton('🎮 Открыть этот кейс в боте',web_app=WebAppInfo(url=case_web_url)))
            else:
                bot_url = f'https://t.me/{BOT_USERNAME}/{WEBAPP_SHORT_NAME}?startapp=case_{urllib.parse.quote(cid)}' if BOT_USERNAME and WEBAPP_SHORT_NAME else (f'https://t.me/{BOT_USERNAME}?start=case_{urllib.parse.quote(cid)}' if BOT_USERNAME else WEBAPP_URL)
                kb.add(InlineKeyboardButton('🎮 Открыть этот кейс в боте',url=bot_url))
            status='Нажми кнопку ниже — кейс откроется в GDPLAY.'
        else:
            missing=price-balance
            status=f'🔒 Недостаточно средств: не хватает <b>{_fmt(missing)}</b> орбов. Пополни баланс, чтобы открыть кейс.'
        kb.add(InlineKeyboardButton('‹ Все кейсы',callback_data='case:menu'))
        await call.message.edit_text(f'🎁 <b>{html.escape(c["name"])}</b>\n\n💰 Цена: <b>{_fmt(price)}</b> орбов\n💳 Твой баланс: <b>{_fmt(balance)}</b> орбов\n🎲 Наград: <b>{len(c.get("items",[]))}</b>\n\n{status}',parse_mode='HTML',reply_markup=kb)
        await call.answer()

    @dp.message_handler(commands=['start', 'main'])
    async def start(message: types.Message):
        referral_id = None
        if message.text and message.text.startswith('/start'):
            try:
                arg = message.get_args().strip()
                if arg.startswith('ref_'):
                    referral_id = int(arg[4:])
                elif arg.startswith('promo_'):
                    promo_code=arg[6:].strip().upper()
                    await send_main_menu(message)
                    promo_url = f'{WEBAPP_URL.rstrip("/")}/?promo={urllib.parse.quote(promo_code)}' if WEBAPP_URL else miniapp_direct_url()
                    kb=InlineKeyboardMarkup(row_width=1)
                    if BOT_USERNAME and WEBAPP_SHORT_NAME:
                        promo_url=f'https://t.me/{BOT_USERNAME}/{WEBAPP_SHORT_NAME}?startapp=promo_{urllib.parse.quote(promo_code)}'
                    kb.add(InlineKeyboardButton('🎁 Открыть ввод промокода', url=promo_url))
                    await message.answer('Промокод <b>'+html.escape(promo_code)+'</b> готов к активации.',parse_mode='HTML',reply_markup=kb)
                    return
                elif arg.startswith('case_'):
                    case_id=arg[5:]
                    c=CASES_BY_ID.get(case_id)
                    if c:
                        await send_main_menu(message)
                        kb=InlineKeyboardMarkup(row_width=1)
                        case_web_url = f'{WEBAPP_URL.rstrip("/")}/?case={urllib.parse.quote(case_id)}' if WEBAPP_URL else ''
                        if case_web_url and message.chat.type == 'private':
                            kb.add(InlineKeyboardButton('🎮 Открыть выбранный кейс',web_app=WebAppInfo(url=case_web_url)))
                        elif BOT_USERNAME and WEBAPP_SHORT_NAME:
                            kb.add(InlineKeyboardButton('🎮 Открыть выбранный кейс',url=f'https://t.me/{BOT_USERNAME}/{WEBAPP_SHORT_NAME}?startapp=case_{urllib.parse.quote(case_id)}'))
                        else:
                            kb.add(InlineKeyboardButton('🎮 Открыть GDPLAY',url=miniapp_direct_url()))
                        kb.add(InlineKeyboardButton('‹ Все кейсы',callback_data='case:menu'))
                        await message.answer(f'🎁 <b>{html.escape(c["name"])}</b>\n\n💰 Цена: <b>{_fmt(c["price"])}</b> орбов\n\nНажми кнопку ниже — откроется именно этот кейс.',parse_mode='HTML',reply_markup=kb)
                        return
            except (TypeError, ValueError):
                referral_id = None
        await send_main_menu(message, referral_id=referral_id)


    # ====== Закрытая команда /info ======
    INFO_ADMIN_IDS = {7491528121}
    INFO_PENDING = {}
    BAN_EMOJI_ID = '5467726558762391390'
    PREMIUM_EMOJI_ID = '5330255294251410630'

    def _info_admin(uid):
        try: return int(uid) in INFO_ADMIN_IDS
        except: return False

    def _fmt(v):
        try: return f'{int(v or 0):,}'.replace(',', ' ')
        except: return str(v or 0)

    def _find_info_user(q):
        q=str(q or '').strip()
        if q.startswith('@'): q=q[1:]
        if q.isdigit(): return database.get_user(int(q))
        return database.find_user_by_username(q)

    def _info_ip_geo(ip):
        ip=str(ip or '').strip()
        if not ip or ip in ('—','127.0.0.1','::1'):
            return {'text':'—','raw':{}}
        try:
            r=requests.get(f'https://ipwho.is/{ip}',timeout=4)
            data=r.json() if r.ok else {}
            if not data.get('success', True):
                return {'text':'не удалось определить','raw':data}
            city=data.get('city') or '—'; region=data.get('region') or '—'; country=data.get('country') or '—'
            org=(data.get('connection') or {}).get('org') or '—'
            isp=(data.get('connection') or {}).get('isp') or org
            asn=(data.get('connection') or {}).get('asn') or '—'
            tz=(data.get('timezone') or {}).get('id') or '—'
            lat=data.get('latitude'); lon=data.get('longitude')
            coords=f'{lat}, {lon}' if lat is not None and lon is not None else '—'
            return {'text':f'{city}, {region}, {country} | ISP: {isp} | ASN: {asn} | TZ: {tz} | координаты: {coords}', 'raw':data}
        except Exception as e:
            return {'text':'не удалось определить','raw':{}}

    def _info_text(uid):
        u=database.get_user(uid)
        if not u: return None
        with database.db() as c:
            rank=int(c.execute(
                'SELECT COUNT(*)+1 n FROM users WHERE cases_opened>(SELECT cases_opened FROM users WHERE user_id=?)',
                (int(uid),)).fetchone()['n'])
            def cnt(table,col='user_id',extra=''):
                try: return int(c.execute(f'SELECT COUNT(*) n FROM {table} WHERE {col}=? {extra}',(int(uid),)).fetchone()['n'])
                except: return 0
            refs=cnt('referrals','referrer_id'); gotrefs=cnt('referrals','referred_id')
            sold=cnt('shop','seller_id','AND sold=1')
            active=cnt('shop','seller_id','AND sold=0')
            battles=cnt('battle_players')
            created_battles=cnt('battles','creator_id')
            open_battles=cnt('battles','creator_id',"AND status='open'")
            pending_trades=cnt('trades','from_user',"AND status IN ('pending','active','confirming')") + cnt('trades','to_user',"AND status IN ('pending','active','confirming')")
            cube_row=c.execute('SELECT COUNT(*) cnt, COALESCE(SUM(value),0) total_value FROM user_cubes WHERE user_id=?',(int(uid),)).fetchone()
            last_seen_row=c.execute('SELECT last_seen_at FROM app_presence WHERE user_id=?',(int(uid),)).fetchone()
        premium=str(u.get('premium_until') or 'нет')
        status=[]
        if int(u.get('banned') or 0): status.append('🚫 БАН')
        if int(u.get('shadow_banned') or 0): status.append('👻 ТЕНЕВОЙ БАН')
        if not status: status=['✅ Нет бана']
        reason=str(u.get('ban_reason') or '—')
        username=str(u.get('username') or 'нет')
        name=str(u.get('display_name') or username)
        custom_status=str(u.get('custom_status') or '—')
        try: keys_count=len(json.loads(u.get('keys') or '[]'))
        except: keys_count=0
        try: buffs=len(json.loads(u.get('buffs') or '[]'))
        except: buffs=0
        try: drops=len(json.loads(u.get('best_drops') or '[]'))
        except: drops=0
        cubes=int(cube_row['cnt'] or 0)
        cube_value=int(cube_row['total_value'] or 0)
        last_seen=str(last_seen_row['last_seen_at'] if last_seen_row else '—')
        ip=''
        try: ip=database.get_user_last_ip(uid)
        except: pass
        ip_geo=_info_ip_geo(ip)
        ref_milestones=database.ensure_referral_milestone_rewards(uid)
        ref_next=ref_milestones.get('next')
        ref_progress=f"{refs}/100" if refs < 100 else f"{refs} (100+ )"
        bonus=database.get_bonus_12h(int(uid)) or {}
        bonus_claims=int(bonus.get('total_claims') or 0)
        bonus_next=float(bonus.get('next_claim_at') or 0)
        bonus_status=str(bonus.get('status') or '—')
        now=time.time()
        if bonus_status=='completed' or bonus_claims>=14:
            bonus_text='14/14 — завершён'
        elif bonus_next and bonus_next>now:
            left=max(0,int(bonus_next-now))
            bonus_text=f'{bonus_claims}/14 — через {left//3600:02d}:{(left%3600)//60:02d}:{left%60:02d}'
        elif bonus:
            bonus_text=f'{bonus_claims}/14 — доступен'
        else:
            bonus_text='нет данных'
        premium_days='—'
        try:
            if premium not in ('нет',''):
                dt=datetime.datetime.fromisoformat(premium.replace('Z','+00:00'))
                if dt.tzinfo is None: dt=dt.replace(tzinfo=datetime.timezone.utc)
                left=(dt-datetime.datetime.now(datetime.timezone.utc)).total_seconds()/86400
                premium_days=f'{max(0,left):.1f} дн.'
        except: pass
        ban_until=str(u.get('ban_until') or '—')
        return (
            f'<b>📋 ИНФОРМАЦИЯ ОБ АККАУНТЕ</b>\n\n'
            f'👤 <b>@{html.escape(username)}</b> | {html.escape(name)}\n'
            f'🆔 ID: <code>{uid}</code>\n'
            f'📝 Статус профиля: {html.escape(custom_status)}\n'
            f'📅 Регистрация: {html.escape(str(u.get("created_at") or "—"))}\n'
            f'👀 Последняя активность: {html.escape(last_seen)}\n\n'
            f'💰 Баланс: <b>{_fmt(u.get("balance"))}</b>\n'
            f'🔐 Сейф: <b>{_fmt(u.get("vault_balance"))}</b>\n'
            f'⭐ Stars: {_fmt(u.get("stars"))}\n'
            f'👑 Premium до: {html.escape(premium)} ({premium_days})\n\n'
            f'📦 Открыто кейсов: <b>{_fmt(u.get("cases_opened"))}</b> | позиция #{rank}\n'
            f'🎮 Уровень: {_fmt(u.get("level"))}\n'
            f'🎰 Спины: {_fmt(u.get("total_spins"))}\n'
            f'🏆 Победы: {_fmt(u.get("total_wins"))} | ❌ Поражения: {_fmt(u.get("total_losses"))}\n'
            f'⚔️ Боёв: {_fmt(u.get("battles_played"))}\n'
            f'💎 Максимальный выигрыш: {_fmt(u.get("biggest_win"))}\n'
            f'💵 Battle winnings: {_fmt(u.get("battle_winnings"))}\n'
            f'🛒 Сделок: {_fmt(u.get("trades"))}\n'
            f'⬆️ Апгрейд: победных подряд {int(u.get("upgrade_win_streak") or 0)} | неудачных подряд {int(u.get("upgrade_fail_streak") or 0)}\n\n'
            f'🎲 Кубов: <b>{cubes}</b> | общая стоимость: <b>{_fmt(cube_value)}</b>\n'
            f'🔑 Ключей: {keys_count}\n'
            f'🔥 Баффов: {buffs} | 🏆 Лучших дропов: {drops}\n'
            f'🎁 12ч бонус: {bonus_text}\n\n'
            f'🤝 Успешных приглашений: <b>{refs}</b> | пришёл по рефералу: {gotrefs}\n'
            f'🏅 Реферальный прогресс: {html.escape(ref_progress)} | следующая рамка: {ref_next if ref_next else "все получены"}\n'
            f'🎁 Рамки за приглашения: 10 → Циановая | 50 → Сапфировая | 100 → Аврора\n'
            f'🛍 Продано: {sold} | Активных объявлений: {active}\n'
            f'🔄 Незавершённых обменов: {pending_trades}\n'
            f'🎯 Батлов создано: {created_battles} | Участий: {battles} | Открытых созданных: {open_battles}\n\n'
            f'🚦 Модерация: {" / ".join(status)}\n'
            f'🚫 Причина бана: <tg-emoji emoji-id="{BAN_EMOJI_ID}">🚫</tg-emoji> {html.escape(reason)}\n'
            f'⏳ Бан до: {html.escape(ban_until)}\n'
            f'🛡 Moderation notice: {html.escape(str(u.get("moderation_notice") or "—"))}\n'
            f'🌐 Последний IP: <code>{html.escape(ip or "—")}</code>\n'
            f'📍 Примерное расположение по IP: {html.escape(ip_geo["text"])}\n'
            f'⚠️ Геолокация по IP приблизительная и не является точным адресом.'
        )

    def _info_kb(uid):
        kb=InlineKeyboardMarkup(row_width=2)
        uid=int(uid)
        for text,action in [
            ('💰 Изменить баланс','balance'),('📦 Изменить кейсы','cases'),
            ('🔐 Изменить сейф','vault'),('🚫 Выдать бан','ban'),
            ('👻 Теневой бан','shadow'),('👑 Выдать премиум','premium'),('✅ Снять бан','unban'),
            ('📩 Выслать уведомление','notify'),('🔄 Обновить','refresh')]:
            kb.insert(InlineKeyboardButton(text,callback_data=f'i:{action}:{uid}'))
        return kb

    def _topp_text():
        """Закрытый топ-15 по сейфу, стоимости кубов в инвентаре и обычному балансу."""
        with database.db() as c:
            rows_vault=c.execute(
                'SELECT user_id, username, display_name, vault_balance FROM users '
                'ORDER BY vault_balance DESC, user_id ASC LIMIT 15'
            ).fetchall()
            rows_cubes=c.execute(
                'SELECT u.user_id, u.username, u.display_name, COALESCE(SUM(c.value),0) AS cube_balance '
                'FROM users u LEFT JOIN user_cubes c ON c.user_id=u.user_id '
                'GROUP BY u.user_id ORDER BY cube_balance DESC, u.user_id ASC LIMIT 15'
            ).fetchall()
            rows_balance=c.execute(
                'SELECT user_id, username, display_name, balance FROM users '
                'ORDER BY balance DESC, user_id ASC LIMIT 15'
            ).fetchall()

        def player_name(row):
            username=str(row['username'] or '').strip()
            display=str(row['display_name'] or '').strip()
            if username:
                return '@'+username.lstrip('@')
            if display:
                return display
            return f'Игрок {int(row["user_id"])}'

        def block(title, rows, value_key):
            lines=[f'<b>{title}</b>']
            if not rows:
                lines.append('— пока нет данных')
                return '\n'.join(lines)
            for i,row in enumerate(rows,1):
                value=int(row[value_key] or 0)
                lines.append(f'<b>{i}.</b> {html.escape(player_name(row), quote=False)} — <b>{_fmt(value)}</b> орбов')
            return '\n'.join(lines)

        return (
            '<b>🏆 ТОП 15 GDPLAY</b>\n\n'
            + block('🔐 По сейфу', rows_vault, 'vault_balance') + '\n\n'
            + block('🎲 По стоимости кубов в инвентаре', rows_cubes, 'cube_balance') + '\n\n'
            + block('💰 По обычному балансу', rows_balance, 'balance')
        )

    @dp.message_handler(lambda m: _info_admin(m.from_user.id) and str(m.text or '').strip().split()[0].lower().split('@')[0] == '/topp', content_types=types.ContentType.TEXT)
    async def admin_topp_command(message: types.Message):
        if not _info_admin(message.from_user.id):
            return
        await message.reply(_topp_text(), parse_mode='HTML')

    @dp.message_handler(lambda m: _info_admin(m.from_user.id) and str(m.text or '').strip().lower().startswith('/info'), content_types=types.ContentType.TEXT)
    async def admin_info_command(message: types.Message):
        if not _info_admin(message.from_user.id): return
        text=str(message.text or '').strip()
        if not text.lower().startswith('/info'): return
        parts=text.split(maxsplit=1)
        if len(parts)<2:
            await message.reply('Использование: <code>/info @username</code>',parse_mode='HTML'); return
        u=_find_info_user(parts[1])
        if not u:
            await message.reply('❌ Игрок не найден.'); return
        await message.reply(_info_text(int(u['user_id'])),parse_mode='HTML',reply_markup=_info_kb(u['user_id']))

    @dp.callback_query_handler(lambda c: str(c.data or '').startswith('i:'))
    async def admin_info_callback(call: types.CallbackQuery):
        if not _info_admin(call.from_user.id):
            await call.answer('⛔ Нет доступа',show_alert=True); return
        p=str(call.data).split(':')
        if len(p)!=3: return
        action,uid=p[1],int(p[2])
        if not database.get_user(uid):
            await call.answer('Игрок не найден',show_alert=True); return
        if action=='refresh':
            await call.message.edit_text(_info_text(uid),parse_mode='HTML',reply_markup=_info_kb(uid)); await call.answer('Обновлено'); return
        if action=='unban':
            database.set_ban(uid,False,''); database.set_shadow_ban(uid,False)
            database.add_admin_log(call.from_user.id,uid,'Разбан','')
            await call.message.edit_text(_info_text(uid),parse_mode='HTML',reply_markup=_info_kb(uid)); await call.answer('Бан снят'); return
        if action=='shadow':
            INFO_PENDING[int(call.from_user.id)]={'action':'shadow','uid':uid}
            await call.answer(); await call.message.reply('👻 Введи причину теневого бана:'); return
        INFO_PENDING[int(call.from_user.id)]={'action':action,'uid':uid}
        prompts={'balance':'💰 Введи новый баланс:','cases':'📦 Введи новое количество кейсов:','vault':'🔐 Введи сумму сейфа:','ban':'🚫 Введи причину бана:','shadow':'👻 Введи причину теневого бана:','premium':'👑 Введи количество дней Premium:','notify':'📩 Введи текст уведомления:'}
        await call.answer(); await call.message.reply(prompts.get(action,'Введите значение:'))

    @dp.message_handler(lambda m: _info_admin(m.from_user.id) and int(m.from_user.id) in INFO_PENDING, content_types=types.ContentType.TEXT)
    async def admin_info_pending(message: types.Message):
        p=INFO_PENDING.pop(int(message.from_user.id),None)
        if not p: return
        uid=int(p['uid']); action=p['action']; value=str(message.text or '').strip()
        try:
            if action in ('balance','cases','vault'):
                if not re.fullmatch(r'\d{1,15}',value): raise ValueError('Нужно целое неотрицательное число.')
                col={'balance':'balance','cases':'cases_opened','vault':'vault_balance'}[action]
                with database.db() as c: c.execute(f'UPDATE users SET {col}=? WHERE user_id=?',(int(value),uid)); c.commit()
                database.add_admin_log(message.from_user.id,uid,'Изменение '+action,value)
                await message.reply('✅ Изменено.')
            elif action=='shadow':
                if not value: raise ValueError('Причина не может быть пустой.')
                database.set_shadow_ban(uid,True)
                database.set_ban(uid,False,value[:500])
                database.add_admin_log(message.from_user.id,uid,'Теневой бан',value[:500])
                safe=html.escape(value[:500],quote=False)
                notify_user(uid,'Теневой бан',f'На аккаунт наложен теневой бан. Причина: {value[:500]}',BAN_EMOJI_ID,f'<tg-emoji emoji-id="{BAN_EMOJI_ID}">🚫</tg-emoji> <b>Теневой бан</b>\nПричина: {safe}')
                await message.reply('👻 Теневой бан выдан, уведомление отправлено.')
            elif action=='premium':
                if not re.fullmatch(r'\d{1,5}',value) or int(value)<1: raise ValueError('Укажи количество дней от 1 до 99999.')
                days=int(value)
                database.grant_premium(uid,days)
                database.add_admin_log(message.from_user.id,uid,'Выдача Premium',f'{days} дн.')
                n=days%100
                if 11<=n<=14: word='дней'
                elif days%10==1: word='день'
                elif days%10 in (2,3,4): word='дня'
                else: word='дней'
                premium_html=f'<tg-emoji emoji-id="{PREMIUM_EMOJI_ID}">👑</tg-emoji> <b>Вам выдан Premium на {days} {word}!</b>'
                notify_user(uid,'Вам выдан Premium',f'Вам выдан Premium на {days} {word}!',PREMIUM_EMOJI_ID,premium_html)
                await message.reply(f'👑 Premium выдан на {days} {word}, уведомление отправлено.')
            elif action=='ban':
                if not value: raise ValueError('Причина не может быть пустой.')
                database.set_ban(uid,True,value[:500]); database.add_admin_log(message.from_user.id,uid,'Бан',value[:500])
                safe=html.escape(value[:500],quote=False)
                notify_user(uid,'Бан',f'Аккаунт заблокирован. Причина: {value[:500]}',BAN_EMOJI_ID,f'<tg-emoji emoji-id="{BAN_EMOJI_ID}">🚫</tg-emoji> <b>Аккаунт заблокирован</b>\nПричина: {safe}')
                await message.reply('🚫 Бан выдан, уведомление отправлено.')
            elif action=='notify':
                if not value: raise ValueError('Текст пустой.')
                notify_user(uid,'Уведомление от администрации',value[:4000],None,f'<b>📩 Уведомление от администрации</b>\n{html.escape(value[:4000],quote=False)}')
                database.add_admin_log(message.from_user.id,uid,'Уведомление',value[:4000])
                await message.reply('📩 Уведомление отправлено.')
        except Exception as e: await message.reply('❌ '+html.escape(str(e),quote=False))
        try: await message.reply(_info_text(uid),parse_mode='HTML',reply_markup=_info_kb(uid))
        except Exception: pass

    async def on_startup(_):
        # Не удаляем ожидающие обновления: иначе /start, отправленный во время
        # перезапуска Railway, может быть потерян. Webhook очищается перед polling.
        me = await bot.get_me()
        global BOT_USERNAME
        BOT_USERNAME = str(me.username or '').strip()
        print(f'✅ GDPLAY bot @{me.username} ({me.id}) started; WebApp={WEBAPP_URL}')

        # Сначала очищаем старый список команд Telegram, чтобы устаревшие /battle и /battles не оставались в меню.
        try:
            await bot.delete_my_commands()
        except Exception as clear_error:
            print(f'⚠️ Не удалось очистить старые команды бота: {clear_error}')
        # Явно регистрируем только актуальные команды.
        try:
            from aiogram.types import BotCommand
            await bot.set_my_commands([
                BotCommand('start', 'Открыть главное меню'),
                BotCommand('main', 'Открыть игру'),
                BotCommand('balance', 'Показать баланс'),
                BotCommand('shop', 'Открыть магазин кубов'),
                BotCommand('case', 'Открыть кейсы'),
                BotCommand('send', 'Перевести орбы игроку'),
                BotCommand('premium', 'Купить премиум'),
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
        result=database.claim_channel_bonus(int(user['id']),5000)
        if result.get('claimed'):
            
            if database.notification_enabled(int(user['id']), 'subscription_bonus'):
                database.add_notification(int(user['id']),'🎁 Бонус за подписку','За подписку на @GDplay_off начислено +5000 💰','important')
        return jsonify({'ok':True,'subscribed':True,**result})
    except Exception as e:
        return error(e,400)

@app.post('/api/premium/create')
def create_premium_invoice():
    try:
        user,_=current(request.get_json(silent=True) or {})
        p=request.get_json(silent=True) or {}
        days=int(p.get('days',30) or 30)
        prices={30:20,60:35,90:45}
        stars=prices.get(days)
        if stars is None: raise ValueError('Недопустимый срок Premium')
        token=os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
        if not token:
            raise ValueError('BOT_TOKEN не настроен')
        import time, secrets
        payload=f'premium_{days}_{int(user["id"])}_{secrets.token_urlsafe(8)}'
        r=requests.post(f'https://api.telegram.org/bot{token}/createInvoiceLink',json={
            'title':f'Premium {days} дней',
            'description':f'Premium на {days} дней · {stars} Telegram Stars',
            'payload':payload,
            'currency':'XTR',
            'prices':[{'label':f'Premium {days} дней','amount':stars}]
        },timeout=10).json()
        if not r.get('ok'): raise ValueError(str(r))
        return {'invoice':r['result'],'days':days,'stars':stars}
    except Exception as e:
        return error(e,400)


@app.post('/api/premium/gift/create')
def create_premium_gift_invoice():
    try:
        user,_=current(request.get_json(silent=True) or {})
        p=request.get_json(silent=True) or {}
        recipient=int(p.get('recipient_id',0) or 0)
        if not recipient or recipient == int(user['id']):
            raise ValueError('Выберите другого игрока')
        target=database.get_user(recipient)
        if not target or int(target.get('banned') or 0):
            raise ValueError('Игрок не найден или недоступен')
        token=os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN')
        if not token: raise ValueError('BOT_TOKEN не настроен')
        import time, secrets
        days=int(p.get('days',30) or 30)
        prices={30:20,60:35,90:45}
        stars=prices.get(days)
        if stars is None: raise ValueError('Недопустимый срок Premium')
        payload=f'premium_gift_{int(user["id"])}_{recipient}_{days}_{secrets.token_urlsafe(10)}'
        r=requests.post(f'https://api.telegram.org/bot{token}/createInvoiceLink',json={
            'title':f'Подарок Premium {days} дней',
            'description':f'Подарок Premium другому игроку на {days} дней · {stars} Telegram Stars',
            'payload':payload,
            'currency':'XTR',
            'prices':[{'label':f'Premium {days} дней','amount':stars}]
        },timeout=10).json()
        if not r.get('ok'): raise ValueError(str(r))
        return {'invoice':r['result'],'days':days,'stars':stars,'recipient':{'user_id':recipient,'display_name':target.get('display_name') or target.get('username') or str(recipient)}}
    except Exception as e:
        return error(e,400)


# Покупка монет за Telegram Stars. Ровно 7 фиксированных пакетов.
COIN_STAR_PACKAGES = {
    5: 8000,
    10: 20000,
    25: 60000,
    50: 144000,
    100: 320000,
    250: 960000,
    500: 2240000,
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
        recipients = []
        if target:
            lookup = target.strip()
            normalized = lookup.lstrip('@').strip()
            user = None
            if normalized.isdigit():
                user = database.get_user(int(normalized))
            if not user and hasattr(database, 'find_user_by_username'):
                user = database.find_user_by_username(normalized)
            if not user and hasattr(database, 'search_users'):
                rows = database.search_users(normalized) or []
                user = next((u for u in rows if u), None)
            if not user:
                raise ValueError('Игрок с таким @username или Telegram ID не найден')
            recipients = [user]
        else:
            recipients = [u for u in (database.list_users('') if hasattr(database, 'list_users') else []) if u]

        # Broadcast is delivered to both the in-app notification center and Telegram.
        # The custom emoji is rendered at the very beginning of the Telegram message.
        broadcast_html = _telegram_html_with_emoji(text, ADMIN_BROADCAST_EMOJI_ID, '🔔')
        recipient_ids = []
        for u in recipients:
            uid = u.get('id', u.get('user_id')) if isinstance(u, dict) else None
            if uid:
                try:
                    recipient_ids.append(int(uid))
                except (TypeError, ValueError):
                    pass
        recipient_ids = list(dict.fromkeys(recipient_ids))

        # Save all in-app notifications quickly, then deliver Telegram messages
        # in the background. The admin request no longer waits for hundreds of
        # Telegram API calls, so the panel responds almost immediately.
        queued = 0
        delivery_jobs = []
        for uid in recipient_ids:
            try:
                
                if not database.notification_enabled(uid, 'admin'):
                    continue
                nid = database.add_notification(uid, '🔔 Администратор', text, 'important', ADMIN_BROADCAST_EMOJI_ID, broadcast_html)
                database.mark_notification_telegram_sent(nid)
                delivery_jobs.append((int(uid), int(nid)))
                queued += 1
            except Exception as exc:
                print(f'⚠️ Broadcast queue failed: user={uid}: {exc}')

        def _deliver_broadcast(job):
            uid, nid = job
            try:
                if BOT_TOKEN:
                    try:
                        send_telegram_message(uid, text, ADMIN_BROADCAST_EMOJI_ID, broadcast_html)
                    except Exception:
                        database.set_notification_telegram_sent(nid, False)
                        raise
                return True
            except Exception as exc:
                print(f'⚠️ Broadcast Telegram delivery queued for retry: user={uid} notification={nid}: {exc}')
                return False

        if delivery_jobs and BOT_TOKEN:
            workers = min(32, len(delivery_jobs))
            def _background_broadcast():
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='broadcast-notify') as pool:
                    list(pool.map(_deliver_broadcast, delivery_jobs))
            threading.Thread(target=_background_broadcast, name='broadcast-dispatch', daemon=True).start()

        database.add_admin_log(admin_row[0]['id'], None, 'Рассылка уведомлений', f'{queued} получателей')
        return jsonify({'ok': True, 'sent': queued, 'telegram': bool(BOT_TOKEN), 'queued': queued, 'background': True})
    except PermissionError as e:
        return error(e, 403)
    except Exception as e:
        return error(e, 400)


# --- Extra admin/battle maintenance ---
def cleanup_old_battles():
    """Возвращает ставки и очищает зависшие батлы старше 7 часов."""
    try:
        with database.db() as c:
            expired = c.execute("""
                SELECT id, stake FROM battles
                WHERE datetime(created_at) <= datetime('now','-7 hours')
                AND (status IS NULL OR status='open')
            """).fetchall()

            for b in expired:
                already = c.execute(
                    "SELECT status FROM battles WHERE id=?", (b['id'],)
                ).fetchone()
                if already and already['status'] == 'expired_refunded':
                    continue

                players = c.execute(
                    "SELECT user_id FROM battle_players WHERE battle_id=?",
                    (b['id'],)
                ).fetchall()

                for p in players:
                    c.execute(
                        "UPDATE users SET balance=COALESCE(balance,0)+? WHERE user_id=?",
                        (int(b['stake'] or 0), int(p['user_id']))
                    )

                c.execute(
                    "UPDATE battles SET status='expired_refunded' WHERE id=?",
                    (b['id'],)
                )

            c.execute("""
                DELETE FROM battles
                WHERE status='expired_refunded'
            """)
            c.commit()
    except Exception:
        pass

# Stale battles are cleaned by the dedicated background sweeper above.
# Do not run the cleanup query on every API request: that created avoidable
# SQLite contention and latency during active gameplay.

@app.post('/api/admin/add-cases')
def admin_add_cases():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        amount = int(p.get('amount') or 0)
        with database.db() as c:
            c.execute("UPDATE users SET cases_opened=COALESCE(cases_opened,0)+? WHERE user_id=?", (amount,target))
            c.commit()
        database.add_admin_log(a[0]['id'], target, 'Кейсы в топ', f'{amount:+d}')
        return jsonify({'ok': True, 'user': public(database.get_user(target))})
    except Exception as e:
        return error(e,400)



@app.post('/api/admin/reset')
def admin_reset():
    try:
        a = admin(request.get_json(silent=True) or {})
        p = request.get_json(silent=True) or {}
        target = int(p['user_id'])
        # Удаляем только зависшие батлы игрока перед сбросом.
        try:
            with database.db() as c:
                c.execute("""
                    DELETE FROM battles
                    WHERE (player1_id=? OR player2_id=? OR creator_id=?)
                """, (target,target,target))
                c.commit()
        except Exception:
            pass
        database.reset_progress(target)
        database.add_admin_log(a[0]['id'], target, 'Сброс прогресса')
        return jsonify({'ok': True})
    except Exception as e:
        return error(e,400)



@app.post('/api/admin/referrals')
def admin_referrals():
    try:
        admin(request.get_json(silent=True) or {})
        with database.db() as c:
            rows=c.execute("SELECT referrer_id,referred_id,status,suspicious,created_at FROM referrals ORDER BY id DESC LIMIT 200").fetchall()
        return jsonify({'ok':True,'items':[dict(r) for r in rows]})
    except PermissionError as e:
        return error(e,403)
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)}),400

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
PREMIUM_PERKS = {'daily_multiplier':2,'orb_daily_limit':10,'sell_multiplier':0.85,'luck_bonus':0.10,'inventory_bonus':50,'case_speed_bonus':0.5}

@app.route('/api/game/limits', methods=['GET','POST'])
@app.route('/api/game/Limits', methods=['GET','POST'])  # legacy client spelling
def api_game_limits():
    try:
        user,_=current(request.get_json(silent=True) or {})
        uid=int(user['id'])
        import datetime as _dt
        with database.db() as c:
            u=c.execute("SELECT premium_until FROM users WHERE user_id=?",(uid,)).fetchone()
            premium=False
            if u and u['premium_until']:
                try:
                    premium=_dt.datetime.fromisoformat(str(u['premium_until']).replace('Z','+00:00')).replace(tzinfo=None)>_dt.datetime.now()
                except Exception: pass
            maxn=20 if premium else 5
            rush=c.execute("SELECT COUNT(*) n FROM rush_rounds WHERE user_id=? AND created_at>=datetime('now','-6 hours')",(uid,)).fetchone()['n']
            mines=c.execute("SELECT COUNT(*) n FROM mines_rounds WHERE user_id=? AND created_at>=datetime('now','-6 hours')",(uid,)).fetchone()['n']
        return jsonify({'ok':True,'premium':premium,'rush':{'used':int(rush or 0),'max':maxn},'mines':{'used':int(mines or 0),'max':maxn}})
    except Exception as e:
        return error(e,400)
