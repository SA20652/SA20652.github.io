import os, re, json, hmac, hashlib, urllib.parse, asyncio, threading, time, random, secrets, urllib.request, urllib.parse as urlparse
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
from dotenv import load_dotenv
from aiogram import Bot, types
from aiogram.dispatcher import Dispatcher
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
import database
import traceback
from case_catalog import CASES_BY_ID, weighted_pick
from upgrade_config import is_valid_target

load_dotenv()
PORT = int(os.environ.get('PORT', 5000))
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
WEBAPP_URL = os.getenv('WEBAPP_URL', 'https://your-app.netlify.app').strip()
# Безопасность админки: доступ только по точному Telegram ID.
# Username, пароль и локальные флаги аккаунта не дают права администратора.
ADMIN_TELEGRAM_ID = 7491528121
ADMIN_SESSION_TTL = 10 * 60
ADMIN_MAX_REQUESTS_PER_MINUTE = 30
IP_BAN_DEFAULT_MINUTES = 60
RUSH_MAX_BET = 1_000_000
RUSH_MAX_MULTIPLIER = 2.5

KEY_DROP_CATALOG = []  # keys removed
CASE_KEY_REQUIREMENTS = {}  # keys removed completely
def required_keys_for_case(case_id):
    return CASE_KEY_REQUIREMENTS.get(str(case_id), [])

def disabled_required_key_for_case(case_id):
    keys = required_keys_for_case(case_id)
    return keys[0] if keys else None

def consume_case_key(user_id, case_id, count=1):
    accepted = required_keys_for_case(case_id)
    if not accepted:
        raise ValueError('Для этого сейфа нет ключа')
    return database.consume_key_types_atomic(user_id, accepted, int(count))

def server_maybe_drop_key(user_id, case_price):
    return None


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if request.path == '/' or request.path.endswith('.html'):
        response.headers['Content-Security-Policy'] = "default-src 'self' https://telegram.org https://fonts.googleapis.com https://fonts.gstatic.com data: blob:; img-src 'self' data: blob: https://t.me https://telegram.org; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; script-src 'self' 'unsafe-inline' https://telegram.org; connect-src 'self' https://api.telegram.org; frame-ancestors https://web.telegram.org https://telegram.org; base-uri 'self'; object-src 'none'"
    return response

@app.get('/')
def index_page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'index.html')

@app.get('/promo/2026samergermancrut.jpg')
def promo_secret_image():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'IMG_20260817_134411.jpg')


# --- Бот ---
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher(bot) if bot else None

# --- Функции ---
def verify_init_data(init_data):
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
        raise ValueError('Недействительный Telegram initData')
    auth = int(pairs.get('auth_date', '0') or 0)
    if auth and abs(datetime.now(timezone.utc).timestamp() - auth) > 86400:
        raise ValueError('initData устарел')
    user = json.loads(pairs.get('user', '{}'))
    if not user.get('id'):
        raise ValueError('Telegram user отсутствует')
    return user

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
    return {k: u.get(k) for k in ['user_id','username','balance','stars','level','trades','daily_claimed','daily_streak','banned','ban_reason','moderation_notice','premium_until','cases_opened','battle_winnings','avatar','display_name','display_name_changed_at','creator_badge','tester_badge','two_factor_enabled']} | {'cubes': cubes, 'keys': [], 'buffs': buffs, 'best_drops': best_drops}

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
        tg=verify_init_data(payload.get('initData',''))
        database.create_user(tg['id'], tg.get('username') or tg.get('first_name') or str(tg['id']))
        database.enforce_ban_state(tg['id'])
        database.record_user_ip(tg['id'], ip)
        u=database.get_user(tg['id'])
        notice=database.get_moderation_notice(tg['id'])
        if u and u.get('banned'):
            # Не скрываем причину: клиент получает её отдельным полем даже при 403.
            return jsonify({'ok':False,'error':u.get('ban_reason') or 'Аккаунт заблокирован','user':public(u),'moderation_notice':notice,'is_admin':False}),403
        photo=str(tg.get('photo_url') or '').strip()
        old_avatar=str(u.get('avatar') or '') if u else ''
        if photo and (not old_avatar or old_avatar.startswith('https://t.me/i/userpic/')) and photo != old_avatar:
            database.set_avatar(tg['id'],photo); u=database.get_user(tg['id'])
        return jsonify({'ok':True,'user':public(u),'moderation_notice':notice,'is_admin':is_admin_user(tg,u),'server_time':datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        print(f"api_me error: {e}")
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
        database.add_notification(target,'Получены орбы',f'🔔 {sender_name} отправил(а) вам {amount} 💰 орбов!','important')
        return jsonify({'ok':True,**result,'balance':result['sender_balance']})
    except Exception as e: return error(e,400)

def _notifications_payload():
    # Уведомления могут открываться через GET/POST.
    # Раньше GET-запрос терял initData, из-за чего current() считал
    # пользователя неавторизованным. Собираем данные из всех источников.
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}
    if request.args:
        for k, v in request.args.items():
            payload.setdefault(k, v)
    if request.headers.get('X-Init-Data'):
        payload.setdefault('initData', request.headers.get('X-Init-Data'))
    if request.headers.get('X-Auth-Token'):
        payload.setdefault('authToken', request.headers.get('X-Auth-Token'))
    return payload

@app.route('/api/notifications', methods=['POST','GET'])
def api_notifications():
    try:
        payload = _notifications_payload()
        user,_ = current(payload)
        rows = database.get_notifications(user['id'])
        return jsonify({'ok':True,'notifications':rows,'unread':database.get_unread_notification_count(user['id'])})
    except Exception as e:
        # Возвращаем понятную ошибку для диагностики вместо скрытого падения.
        # Это позволяет увидеть реальную причину в интерфейсе/консоли.
        print(f'notifications api error: {e}')
        print(traceback.format_exc())
        return jsonify({'ok':True,'notifications':[],'unread':0}), 200

@app.route('/api/notifications/read', methods=['POST','GET'])
def api_notifications_read():
    try:
        user,_=current(_notifications_payload())
        database.mark_notifications_read(user['id'])
        return jsonify({'ok':True,'unread':0})
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

@app.post('/api/daily')
def api_daily():
    try:
        user, u = current(request.get_json(silent=True) or {})
        fresh = database.get_user(user['id'])
        is_premium = bool(fresh.get('premium_until')) and str(fresh['premium_until']) > datetime.now().isoformat()
        amount = 2000 if is_premium else 1000
        result = database.claim_daily(user['id'], amount)
        farm_action=database.evaluate_economy_farm(user['id'])
        return jsonify({'ok': True, 'balance': result['balance'], 'reward': result['reward'], 'streak': result['streak'], 'premium': is_premium, 'farm_action': farm_action})
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
        use_key = False
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
        use_key = False
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
        cube = cubes[idx]
        cube_id = cube['id']
        cube_name = cube.get('name') or cube.get('title') or f"Куб #{cube_id}"
        balance, gain = database.sell_cube(user['id'], cube_id)
        database.add_notification(user['id'], 'Продажа куба', f'🛒 Ваш куб «{cube_name}» был продан за {gain} 💰 орбов!', 'important')
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
        # Ключи полностью отключены в апгрейдере.
        if str(p.get('source_kind','cube')) == 'key' or str(p.get('target_kind','cube')) == 'key':
            raise ValueError('Ключи нельзя улучшать')
        source_kind = 'cube'
        target_kind = 'cube'
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
        elif action == 'clear_buffs':
            database.clear_user_buffs(target)
            details = 'Активные баффы очищены'
        elif action == 'clear_drops':
            database.clear_best_drops(target)
            details = 'Лучшие дропы очищены'
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
        # kuro неприкосновенен для всех, кроме самого себя
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
        return jsonify({'ok': True, 'battles': bs})
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
            'message': f"🎲 Победил {result['winner_name']} и получил {result['pot']:,} 💰".replace(',', ' '),
            'result': result,
            'farm_action': farm_action
        })
    except Exception as e:
        return error(e, 400)

# --- Бот команды ---
if dp:
    def build_main_keyboard():
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton('🎮 Играть', web_app=WebAppInfo(url=WEBAPP_URL)))
        return kb

    async def send_main_menu(message: types.Message):
        uid = message.from_user.id
        database.create_user(uid, message.from_user.username or message.from_user.first_name or str(uid))
        await message.answer(
            '🎮 **GDPLAY**\n\nДобро пожаловать! 🚀\n\nНажми **Играть**, чтобы открыть игру.',
            reply_markup=build_main_keyboard(),
            parse_mode='Markdown'
        )

    @dp.message_handler(commands=['start', 'main'])
    async def start(message: types.Message):
        await send_main_menu(message)

    async def on_startup(_):
        # Не удаляем ожидающие обновления: иначе /start, отправленный во время
        # перезапуска Railway, может быть потерян. Webhook очищается перед polling.
        me = await bot.get_me()
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
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        print('🤖 Подготовка Telegram polling...')
        # Удаляем webhook, но НЕ удаляем накопившиеся updates.
        loop.run_until_complete(bot.delete_webhook(drop_pending_updates=False))
        loop.run_until_complete(on_startup(None))
        print('🤖 Telegram polling запущен; /start и /main активны.')
        executor.start_polling(dp, skip_updates=False)
    except Exception as e:
        print(f'❌ Telegram bot crashed: {e}')
        traceback.print_exc()

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
