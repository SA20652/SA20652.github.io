import sqlite3, json, datetime, secrets, os, hashlib, re, random
from upgrade_config import upgrade_chance, is_valid_target
from contextlib import contextmanager

DB_PATH = os.getenv('DB_PATH','database.db')
# This release performs a one-time destructive reset so the project starts completely empty.
RESET_MARKER = os.path.abspath(DB_PATH) + '.gdplay_reset_v4_done'

def _reset_database_once():
    if os.path.exists(RESET_MARKER):
        return
    # Remove the SQLite database and its WAL/SHM companions if they exist.
    for path in (DB_PATH, DB_PATH + '-wal', DB_PATH + '-shm'):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


@contextmanager
def db():
    conn=sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        # SQLite's built-in lower()/upper() only fold ASCII characters, so
        # searching for players by name in Russian, Ukrainian, or any other
        # non-Latin alphabet with different casing would silently fail to
        # match (e.g. "иван" would not find "Иван"). Overriding lower() with
        # Python's Unicode-aware str.lower() makes every `lower(...) LIKE ?`
        # search in this file work correctly for any language/script.
        conn.create_function('lower', 1, lambda s: s.lower() if isinstance(s, str) else s)
        yield conn
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.executescript('''
        
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'important',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            read INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS channel_bonus_claims(
            user_id INTEGER PRIMARY KEY,
            claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, 
            username TEXT, 
            balance INTEGER NOT NULL DEFAULT 1000,
            stars INTEGER NOT NULL DEFAULT 0, 
            level INTEGER NOT NULL DEFAULT 0, 
            trades INTEGER NOT NULL DEFAULT 0,
            daily_claimed TEXT, 
            daily_streak INTEGER NOT NULL DEFAULT 0,
            total_spins INTEGER NOT NULL DEFAULT 0, 
            total_wins INTEGER NOT NULL DEFAULT 0,
            total_losses INTEGER NOT NULL DEFAULT 0, 
            biggest_win INTEGER NOT NULL DEFAULT 0,
            banned INTEGER NOT NULL DEFAULT 0, 
            shadow_banned INTEGER NOT NULL DEFAULT 0,
            password_hash TEXT, 
            premium_until TEXT, 
            creator_badge INTEGER NOT NULL DEFAULT 0,
            tester_badge INTEGER NOT NULL DEFAULT 0,
            custom_status TEXT NOT NULL DEFAULT '',
            buffs TEXT NOT NULL DEFAULT '[]',
            best_drops TEXT NOT NULL DEFAULT '[]',
            keys TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            cases_opened INTEGER DEFAULT 0,
            battles_played INTEGER DEFAULT 0,
            battle_winnings INTEGER NOT NULL DEFAULT 0,
            avatar TEXT,
            two_factor_enabled INTEGER NOT NULL DEFAULT 0,
            two_factor_code_hash TEXT,
            two_factor_expires TEXT,
            server_initialized INTEGER NOT NULL DEFAULT 0,
            clicker_quarters INTEGER NOT NULL DEFAULT 0
        );
        
        CREATE TABLE IF NOT EXISTS user_cubes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            user_id INTEGER NOT NULL, 
            name TEXT NOT NULL,
            rarity TEXT NOT NULL DEFAULT 'common', 
            value INTEGER NOT NULL DEFAULT 0, 
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_user_cubes_user ON user_cubes(user_id);
        
        CREATE TABLE IF NOT EXISTS shop(
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            seller_id INTEGER NOT NULL, 
            cube_name TEXT NOT NULL,
            cube_rarity TEXT NOT NULL, 
            cube_value INTEGER NOT NULL DEFAULT 0, 
            price INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'coins', 
            sold INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(seller_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_shop_open ON shop(sold,created_at);
        
        CREATE TABLE IF NOT EXISTS battles(
            id TEXT PRIMARY KEY, 
            creator_id INTEGER NOT NULL, 
            creator_name TEXT, 
            stake INTEGER NOT NULL,
            max_players INTEGER NOT NULL, 
            mode TEXT NOT NULL, 
            status TEXT NOT NULL DEFAULT 'open',
            winner_id INTEGER, 
            pot INTEGER DEFAULT 0, 
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            FOREIGN KEY(creator_id) REFERENCES users(user_id)
        );
        
        CREATE TABLE IF NOT EXISTS battle_players(
            battle_id TEXT NOT NULL, 
            user_id INTEGER NOT NULL, 
            username TEXT, 
            roll INTEGER,
            PRIMARY KEY(battle_id,user_id), 
            FOREIGN KEY(battle_id) REFERENCES battles(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        
        CREATE TABLE IF NOT EXISTS admin_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            admin_id INTEGER, 
            target_id INTEGER, 
            action TEXT, 
            details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(admin_id) REFERENCES users(user_id)
        );
        
        CREATE TABLE IF NOT EXISTS admin_sessions(
            token_hash TEXT PRIMARY KEY,
            admin_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(admin_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin ON admin_sessions(admin_id);
        CREATE INDEX IF NOT EXISTS idx_admin_sessions_expiry ON admin_sessions(expires_at);

        CREATE TABLE IF NOT EXISTS ip_bans(
            ip TEXT PRIMARY KEY,
            reason TEXT NOT NULL DEFAULT '',
            expires_at TEXT,
            permanent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS user_ips(
            user_id INTEGER NOT NULL,
            ip TEXT NOT NULL,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, ip),
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_user_ips_ip ON user_ips(ip);
        CREATE TABLE IF NOT EXISTS economy_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            net_profit INTEGER NOT NULL DEFAULT 0,
            wager INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_economy_events_user_time ON economy_events(user_id, created_at);

        CREATE TABLE IF NOT EXISTS recent_wins(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            cube_name TEXT,
            rarity TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user INTEGER NOT NULL,
            to_user INTEGER NOT NULL,
            offer_cube_id INTEGER NOT NULL,
            offer_cube_name TEXT,
            offer_cube_rarity TEXT,
            offer_cube_value INTEGER,
            request_cube_name TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        
        CREATE TABLE IF NOT EXISTS cases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            price INTEGER,
            cubes TEXT,
            rarity TEXT,
            icon TEXT,
            color TEXT,
            buff TEXT
        );

        CREATE TABLE IF NOT EXISTS auth_sessions(
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS case_open_requests(
            user_id INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            case_id TEXT NOT NULL,
            qty INTEGER NOT NULL,
            balance REAL NOT NULL,
            drops TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id, request_id),
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_case_open_requests_user_time ON case_open_requests(user_id, created_at);

        CREATE TABLE IF NOT EXISTS case_open_guard(
            user_id INTEGER PRIMARY KEY,
            last_open_ms INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS clicker_claims(
            user_id INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            claimed_at_ms INTEGER NOT NULL,
            click_x REAL NOT NULL DEFAULT 63,
            click_y REAL NOT NULL DEFAULT 63,
            PRIMARY KEY(user_id, request_id),
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_clicker_claims_user_time ON clicker_claims(user_id, claimed_at_ms);
        CREATE TABLE IF NOT EXISTS clicker_guard(
            user_id INTEGER PRIMARY KEY,
            last_click_ms INTEGER NOT NULL DEFAULT 0,
            session_start_ms INTEGER NOT NULL DEFAULT 0,
            banned_until_ms INTEGER NOT NULL DEFAULT 0,
            day_key TEXT NOT NULL DEFAULT '',
            day_earned INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS plinko_rounds(
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            bet INTEGER NOT NULL,
            multiplier REAL NOT NULL,
            payout INTEGER NOT NULL,
            path TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_plinko_rounds_user ON plinko_rounds(user_id, created_at);

        CREATE TABLE IF NOT EXISTS rush_rounds(
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            bet INTEGER NOT NULL,
            crash_milli INTEGER NOT NULL,
            started_at_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            multiplier REAL,
            payout INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_rush_rounds_user ON rush_rounds(user_id, created_at);

        CREATE TABLE IF NOT EXISTS promo_codes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
            reward_type TEXT NOT NULL DEFAULT 'coins', coins INTEGER NOT NULL DEFAULT 0,
            vip_days INTEGER NOT NULL DEFAULT 0, max_uses INTEGER NOT NULL DEFAULT 1,
            uses_count INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
            fullscreen_slides TEXT NOT NULL DEFAULT '[]',
            created_by INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS promo_uses(
            id INTEGER PRIMARY KEY AUTOINCREMENT, promo_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(promo_id,user_id),
            FOREIGN KEY(promo_id) REFERENCES promo_codes(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code);
        CREATE INDEX IF NOT EXISTS idx_promo_uses_user ON promo_uses(user_id);
        ''')
        
        promo_cols = {r['name'] for r in c.execute("PRAGMA table_info(promo_codes)").fetchall()}
        if 'fullscreen_slides' not in promo_cols:
            c.execute("ALTER TABLE promo_codes ADD COLUMN fullscreen_slides TEXT NOT NULL DEFAULT '[]'")

        # Встроенный промокод: каждый аккаунт может активировать его только один раз,
        # но сам код не имеет общего лимита использований.
        c.execute(
            "INSERT OR IGNORE INTO promo_codes(code,reward_type,coins,vip_days,max_uses,uses_count,active,created_by) VALUES(?,?,?,?,?,?,?,NULL)",
            ('2026SAMERGERMANCRUT','photo',0,0,2147483647,0,1)
        )

        # Встроенный промокод ATTASHA: 1 488 монет каждому аккаунту один раз.
        c.execute(
            "INSERT OR IGNORE INTO promo_codes(code,reward_type,coins,vip_days,max_uses,uses_count,active,created_by) VALUES(?,?,?,?,?,?,?,NULL)",
            ('ATTASHA','coins',1488,0,2147483647,0,1)
        )

        # Миграция: добавляем новые колонки в уже существующие базы (Railway volume и т.п.)
        existing_cols = {r['name'] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if 'battle_winnings' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN battle_winnings INTEGER NOT NULL DEFAULT 0')
        if 'cases_opened' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN cases_opened INTEGER DEFAULT 0')
        if 'avatar' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN avatar TEXT')
        if 'display_name' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN display_name TEXT')
        if 'display_name_changed_at' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN display_name_changed_at TEXT')
        if 'creator_badge' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN creator_badge INTEGER NOT NULL DEFAULT 0')
        if 'tester_badge' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN tester_badge INTEGER NOT NULL DEFAULT 0')
        if 'custom_status' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN custom_status TEXT NOT NULL DEFAULT ''")
        if 'buffs' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN buffs TEXT NOT NULL DEFAULT '[]'")
        if 'best_drops' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN best_drops TEXT NOT NULL DEFAULT '[]'")
        if 'keys' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN keys TEXT NOT NULL DEFAULT '[]'")
        if 'two_factor_enabled' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN two_factor_enabled INTEGER NOT NULL DEFAULT 0')
        if 'two_factor_code_hash' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN two_factor_code_hash TEXT')
        if 'two_factor_expires' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN two_factor_expires TEXT')
        if 'server_initialized' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN server_initialized INTEGER NOT NULL DEFAULT 0')
        if 'daily_streak' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN daily_streak INTEGER NOT NULL DEFAULT 0')
        if 'ban_reason' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT NOT NULL DEFAULT ''")
        if 'moderation_notice' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN moderation_notice TEXT NOT NULL DEFAULT ''")
        if 'ban_until' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN ban_until TEXT')
        if 'farm_strikes' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN farm_strikes INTEGER NOT NULL DEFAULT 0')
        if 'farm_last_reason' not in existing_cols:
            c.execute("ALTER TABLE users ADD COLUMN farm_last_reason TEXT NOT NULL DEFAULT ''")
        if 'upgrade_fail_streak' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN upgrade_fail_streak INTEGER NOT NULL DEFAULT 0')
        if 'upgrade_win_streak' not in existing_cols:
            c.execute('ALTER TABLE users ADD COLUMN upgrade_win_streak INTEGER NOT NULL DEFAULT 0')
        c.execute('''
            CREATE TABLE IF NOT EXISTS moderation_notices(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                notice_type TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                show_once INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_moderation_notices_user ON moderation_notices(user_id, id DESC)')

        battles_cols = {r['name'] for r in c.execute("PRAGMA table_info(battles)").fetchall()}
        if 'finished_at' not in battles_cols:
            c.execute('ALTER TABLE battles ADD COLUMN finished_at TEXT')

        # Восстанавливаем топ по выигрышам из фактически завершённых батлов.
        # Ранее battle_winnings мог не увеличиваться при завершении батла, из-за
        # чего у всех в рейтинге отображался ноль. Источник истины — таблица battles.
        c.execute('''
            UPDATE users
            SET battle_winnings = COALESCE((
                SELECT SUM(COALESCE(b.pot, 0))
                FROM battles b
                WHERE b.winner_id = users.user_id AND b.status = 'done'
            ), 0)
        ''')
        c.execute('''
            UPDATE users
            SET battles_played = COALESCE((
                SELECT COUNT(DISTINCT bp.battle_id)
                FROM battle_players bp
                JOIN battles b ON b.id = bp.battle_id
                WHERE bp.user_id = users.user_id AND b.status = 'done'
            ), 0)
        ''')

        # Lightweight migration for databases created before the clicker feature.
        try:
            c.execute('ALTER TABLE users ADD COLUMN clicker_quarters INTEGER NOT NULL DEFAULT 0')
        except sqlite3.OperationalError:
            pass

        # Lightweight migration for the spatial clicker anti-spam.
        try:
            c.execute('ALTER TABLE clicker_claims ADD COLUMN click_x REAL NOT NULL DEFAULT 63')
        except sqlite3.OperationalError:
            pass
        try:
            c.execute('ALTER TABLE clicker_claims ADD COLUMN click_y REAL NOT NULL DEFAULT 63')
        except sqlite3.OperationalError:
            pass
        for col, ddl in [
            ('banned_until_ms', 'INTEGER NOT NULL DEFAULT 0'),
            ('day_key', "TEXT NOT NULL DEFAULT ''"),
            ('day_earned', 'INTEGER NOT NULL DEFAULT 0'),
        ]:
            try:
                c.execute(f'ALTER TABLE clicker_guard ADD COLUMN {col} {ddl}')
            except sqlite3.OperationalError:
                pass

        if c.execute('SELECT COUNT(*) n FROM cases').fetchone()['n'] == 0:
            cases = [
                ('Ржавый сейф', 150, '["Riot","Zoink","Doggie"]', 'common', '🔩', '#6b7280', None),
                ('Латунный сейф', 500, '["BrianTheBurger","Surv","Knobbelboy"]', 'rare', '🟫', '#d97706', None),
                ('Чёрный сейф', 2500, '["Dorami","KrmaL","Cursed"]', 'epic', '⬛', '#1f2937', None),
                ('Позолоченный сейф', 6000, '["Michigun","Zobros","EVW"]', 'legendary', '👑', '#fbbf24', '+50% к шансу легендарного'),
                ('Demon-сейф', 12000, '["Luqualizer","Hinds","Partition"]', 'mythic', '😈', '#ef4444', '+100% к шансу мифического'),
                ('Хранилище OG', 130000, '["Nautilus","Cersia","Rafer"]', 'divine', '🗝️', '#ec4899', '+200% к шансу божественного')
            ]
            c.executemany('INSERT INTO cases(name,price,cubes,rarity,icon,color,buff) VALUES(?,?,?,?,?,?,?)', cases)

def row_user(r): 
    return dict(r) if r else None

def create_user(user_id, username):
    with db() as c:
        uid=int(user_id); incoming=str(username or '').strip()
        row=c.execute('SELECT username FROM users WHERE user_id=?',(uid,)).fetchone()
        if not row:
            c.execute('INSERT INTO users(user_id, username) VALUES(?,?)',(uid,incoming))
        else:
            existing=str(row['username'] or '').strip()
            # Не затираем имена встроенных администраторов Telegram-профилем.
            if existing.lower() not in {'kuro','kitzkuro'} and incoming:
                c.execute('UPDATE users SET username=? WHERE user_id=?',(incoming,uid))

def set_username_admin(user_id, username):
    name = str(username or '').strip()
    if not name or len(name) < 3 or len(name) > 16:
        raise ValueError('Ник должен быть от 3 до 16 символов')
    import re
    if not re.fullmatch(r'[a-zA-Zа-яА-Я0-9_]+', name):
        raise ValueError('Недопустимые символы в нике')
    with db() as c:
        row = c.execute('SELECT user_id FROM users WHERE lower(username)=? AND user_id<>?', (name.lower(), int(user_id))).fetchone()
        if row:
            raise ValueError('Этот ник уже занят')
        row = c.execute('SELECT user_id FROM users WHERE lower(display_name)=? AND user_id<>?', (name.lower(), int(user_id))).fetchone()
        if row:
            raise ValueError('Этот ник уже занят')
        c.execute('UPDATE users SET username=?, display_name=? WHERE user_id=?', (name, name, int(user_id)))

def clear_user_buffs(user_id):
    with db() as c:
        c.execute("UPDATE users SET buffs='[]' WHERE user_id=?", (int(user_id),))

def clear_best_drops(user_id):
    with db() as c:
        c.execute("UPDATE users SET best_drops='[]' WHERE user_id=?", (int(user_id),))

def set_avatar_admin(user_id, avatar):
    with db() as c:
        c.execute('UPDATE users SET avatar=? WHERE user_id=?', (str(avatar or '')[:50000] or None, int(user_id)))

def set_stars(user_id, value):
    with db() as c:
        c.execute('UPDATE users SET stars=? WHERE user_id=?', (max(0,int(value)), int(user_id)))

def set_sessions_expired(user_id):
    with db() as c:
        c.execute('DELETE FROM auth_sessions WHERE user_id=?', (int(user_id),))

def set_telegram_display_name(user_id, name):
    # Telegram-имя синхронизируется автоматически: Имя + Фамилия,
    # а если фамилии нет — только Имя. Это не считается ручной сменой ника.
    name = str(name or '').strip()[:80]
    with db() as c:
        c.execute('UPDATE users SET display_name=? WHERE user_id=?', (name or None, int(user_id)))

def set_display_name(user_id, name):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT display_name_changed_at FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not row:
            c.rollback()
            raise ValueError('Пользователь не найден')
        last = row['display_name_changed_at']
        if last:
            last_dt = datetime.datetime.fromisoformat(last)
            if datetime.datetime.now() - last_dt < datetime.timedelta(days=7):
                c.rollback()
                next_dt = last_dt + datetime.timedelta(days=7)
                raise ValueError(f'Менять ник можно раз в неделю — следующая смена доступна {next_dt.strftime("%d.%m.%Y")}')
        now = datetime.datetime.now().isoformat()
        c.execute('UPDATE users SET display_name=?, display_name_changed_at=? WHERE user_id=?', (name, now, int(user_id)))
        c.commit()

def get_client_ip_from_request(request_obj):
    # Railway/proxy: берём первый адрес из X-Forwarded-For, затем fallback на remote_addr.
    raw = str(request_obj.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    ip = raw or str(request_obj.remote_addr or '').strip()
    return ip[:64]

def is_ip_banned(ip):
    ip=str(ip or '').strip()[:64]
    if not ip:
        return False
    with db() as c:
        r=c.execute('SELECT expires_at,permanent FROM ip_bans WHERE ip=?',(ip,)).fetchone()
        if not r:
            return False
        if int(r['permanent'] or 0):
            return True
        exp=r['expires_at']
        if exp:
            try:
                if datetime.datetime.fromisoformat(str(exp)) > datetime.datetime.now():
                    return True
            except Exception:
                return True
        c.execute('DELETE FROM ip_bans WHERE ip=?',(ip,))
        return False

def set_ip_ban(ip, reason='', minutes=0, permanent=False, created_by=None):
    ip=str(ip or '').strip()[:64]
    if not ip: raise ValueError('IP не указан')
    expires=None if permanent else (datetime.datetime.now()+datetime.timedelta(minutes=max(1,int(minutes or 60)))).isoformat()
    with db() as c:
        c.execute('INSERT OR REPLACE INTO ip_bans(ip,reason,expires_at,permanent,created_by) VALUES(?,?,?,?,?)',
                  (ip,str(reason or '').strip()[:500],expires,1 if permanent else 0,int(created_by) if created_by else None))

def clear_ip_ban(ip):
    with db() as c:
        c.execute('DELETE FROM ip_bans WHERE ip=?',(str(ip or '').strip()[:64],))

def get_user_last_ip(user_id):
    with db() as c:
        r=c.execute('SELECT ip FROM user_ips WHERE user_id=? ORDER BY last_seen_at DESC LIMIT 1',(int(user_id),)).fetchone()
        return str(r['ip']) if r and r['ip'] else ''

def record_user_ip(user_id, ip):
    ip=str(ip or '').strip()[:64]
    if not ip: return
    with db() as c:
        c.execute('INSERT INTO user_ips(user_id,ip,last_seen_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(user_id,ip) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP',
                  (int(user_id),ip))

def enforce_ban_state(user_id):
    with db() as c:
        r=c.execute('SELECT banned,ban_until FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        if not r: return False
        if int(r['banned'] or 0) and r['ban_until']:
            try:
                if datetime.datetime.fromisoformat(str(r['ban_until'])) <= datetime.datetime.now():
                    c.execute("UPDATE users SET banned=0,ban_until=NULL,ban_reason='',moderation_notice='' WHERE user_id=?",(int(user_id),))
                    return False
            except Exception:
                pass
        return bool(int(r['banned'] or 0))

def record_economy_event(user_id, source, net_profit=0, wager=0):
    """Записывает серверный денежный результат и при подозрительном фарме выдаёт эскалацию.
    Крупный выигрыш после сопоставимой ставки не считается фармом: при wager >= profit
    событие считается игровой прибылью, а не бесплатной эмиссией."""
    profit=max(0,int(net_profit or 0)); wager=max(0,int(wager or 0))
    if profit <= 0: return None
    uid=int(user_id)
    with db() as c:
        c.execute('INSERT INTO economy_events(user_id,source,net_profit,wager) VALUES(?,?,?,?)',(uid,str(source)[:40],profit,wager))
        cutoff=(datetime.datetime.now()-datetime.timedelta(hours=4)).strftime('%Y-%m-%d %H:%M:%S')
        row=c.execute('SELECT COALESCE(SUM(net_profit),0) profit,COALESCE(SUM(wager),0) wager,COUNT(*) n FROM economy_events WHERE user_id=? AND created_at>=?',(uid,cutoff)).fetchone()
        total_profit=int(row['profit'] or 0); total_wager=int(row['wager'] or 0)
        # 1 млрд за 4 часа при сопоставимом игровом обороте — не фарм.
        suspicious = total_profit >= 1_000_000_000 and total_wager < total_profit
        if not suspicious:
            return None
        u=c.execute('SELECT farm_strikes,banned,username FROM users WHERE user_id=?',(uid,)).fetchone()
        strikes=int(u['farm_strikes'] or 0)+1
        if strikes >= 3:
            reason='Античит: подозрительно быстрое получение валюты (3-е нарушение)'
            c.execute("UPDATE users SET farm_strikes=?,banned=1,ban_until=NULL,ban_reason=?,farm_last_reason=?,moderation_notice=? WHERE user_id=?",
                      (strikes,reason,reason,reason,uid))
            return {'strike':strikes,'permanent':True,'reason':reason}
        minutes=60 if strikes==1 else 24*60
        until=(datetime.datetime.now()+datetime.timedelta(minutes=minutes)).isoformat()
        reason=f'Античит: слишком быстрое получение валюты. Временный бан на {"1 час" if strikes==1 else "24 часа"}.'
        c.execute("UPDATE users SET farm_strikes=?,banned=1,ban_until=?,ban_reason=?,farm_last_reason=?,moderation_notice=? WHERE user_id=?",
                  (strikes,until,reason,reason,reason,uid))
        return {'strike':strikes,'permanent':False,'until':until,'reason':reason}

def evaluate_economy_farm(user_id):
    uid=int(user_id)
    with db() as c:
        cutoff=(datetime.datetime.now()-datetime.timedelta(hours=4)).strftime('%Y-%m-%d %H:%M:%S')
        row=c.execute('SELECT COALESCE(SUM(net_profit),0) profit,COALESCE(SUM(wager),0) wager FROM economy_events WHERE user_id=? AND created_at>=?',(uid,cutoff)).fetchone()
        total_profit=int(row['profit'] or 0); total_wager=int(row['wager'] or 0)
        # Крупный выигрыш после сопоставимой ставки не является фармом.
        if total_profit < 1_000_000_000 or total_wager >= total_profit:
            return None
        u=c.execute('SELECT farm_strikes,banned FROM users WHERE user_id=?',(uid,)).fetchone()
        if not u or int(u['banned'] or 0):
            return None
        strikes=int(u['farm_strikes'] or 0)+1
        if strikes >= 3:
            reason='Античит: подозрительно быстрое получение валюты (3-е нарушение)'
            c.execute("UPDATE users SET farm_strikes=?,banned=1,ban_until=NULL,ban_reason=?,farm_last_reason=?,moderation_notice=? WHERE user_id=?",(strikes,reason,reason,reason,uid))
            return {'strike':strikes,'permanent':True,'reason':reason}
        minutes=60 if strikes==1 else 24*60
        until=(datetime.datetime.now()+datetime.timedelta(minutes=minutes)).isoformat()
        reason=f'Античит: слишком быстрое получение валюты. Временный бан на {"1 час" if strikes==1 else "24 часа"}.'
        c.execute("UPDATE users SET farm_strikes=?,banned=1,ban_until=?,ban_reason=?,farm_last_reason=?,moderation_notice=? WHERE user_id=?",(strikes,until,reason,reason,reason,uid))
        return {'strike':strikes,'permanent':False,'until':until,'reason':reason}

def get_user(user_id):
    with db() as c:
        return row_user(c.execute('SELECT * FROM users WHERE user_id=?', (int(user_id),)).fetchone())

def count_users(search=''):
    """Return the real number of registered players, without a 100-player cap."""
    with db() as c:
        search = str(search or '').strip()
        if search:
            q = f'%{search.lower()}%'
            row = c.execute(
                'SELECT COUNT(*) AS n FROM users '
                "WHERE lower(COALESCE(username, '')) LIKE ? "
                "OR lower(COALESCE(display_name, '')) LIKE ? "
                "OR CAST(user_id AS TEXT) LIKE ?"
                , (q, q, q)
            ).fetchone()
        else:
            row = c.execute('SELECT COUNT(*) AS n FROM users').fetchone()
        return int(row['n'] or 0)


def list_users(search='', limit=None, offset=0):
    """Return ALL matching players by default. Optional limit/offset remain for compatibility."""
    offset = max(0, int(offset or 0))
    if limit is not None:
        limit = max(1, int(limit))
    with db() as c:
        search = str(search or '').strip()
        where = ''
        params = []
        if search:
            q = f'%{search.lower()}%'
            where = (
                " WHERE lower(COALESCE(username, '')) LIKE ? "
                "OR lower(COALESCE(display_name, '')) LIKE ? "
                "OR CAST(user_id AS TEXT) LIKE ?"
            )
            params.extend([q, q, q])
        sql = 'SELECT * FROM users' + where + ' ORDER BY created_at DESC'
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            params.extend([limit, offset])
        elif offset:
            sql += ' LIMIT -1 OFFSET ?'
            params.append(offset)
        rows = c.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def list_users_page(search='', page=1, page_size=100):
    """Compatibility helper for callers that still request pages."""
    page_size = max(1, min(500, int(page_size or 100)))
    page = max(1, int(page or 1))
    offset = (page - 1) * page_size
    total = count_users(search)
    users = list_users(search, limit=page_size, offset=offset)
    return {
        'users': users,
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': (total + page_size - 1) // page_size if total else 0
    }


def list_online_users(exclude_user_id=None, minutes=5, search=''):
    """Users who touched the app recently; returns public transfer fields."""
    minutes=max(1,min(30,int(minutes or 5)))
    search=str(search or '').strip().lower()
    with db() as c:
        params=[minutes]
        sql="""SELECT u.user_id,u.username,u.display_name,u.avatar,u.balance,MAX(ui.last_seen_at) AS last_seen_at
               FROM users u JOIN user_ips ui ON ui.user_id=u.user_id
               WHERE datetime(ui.last_seen_at) >= datetime('now', ? || ' minutes')
                 AND COALESCE(u.banned,0)=0"""
        if exclude_user_id is not None:
            sql += ' AND u.user_id<>?'; params.append(int(exclude_user_id))
        if search:
            sql += " AND (lower(COALESCE(u.username,'')) LIKE ? OR lower(COALESCE(u.display_name,'')) LIKE ? OR CAST(u.user_id AS TEXT) LIKE ?)"
            q=f'%{search}%'; params.extend([q,q,q])
        sql += ' GROUP BY u.user_id ORDER BY datetime(last_seen_at) DESC, lower(COALESCE(u.display_name,u.username)) LIMIT 100'
        rows=c.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def list_all_transfer_users(exclude_user_id=None, search=''):
    search=str(search or '').strip().lower()
    with db() as c:
        params=[]
        sql="""SELECT user_id,username,display_name,avatar
                 FROM users
                 WHERE COALESCE(banned,0)=0"""
        if exclude_user_id is not None:
            sql += ' AND user_id<>?'
            params.append(int(exclude_user_id))
        if search:
            sql += " AND (lower(COALESCE(username,'')) LIKE ? OR lower(COALESCE(display_name,'')) LIKE ? OR CAST(user_id AS TEXT) LIKE ?)"
            q=f'%{search}%'
            params.extend([q,q,q])
        sql += ' ORDER BY lower(COALESCE(display_name,username))'
        return [dict(r) for r in c.execute(sql, tuple(params)).fetchall()]

def transfer_balance(sender_id, receiver_id, amount):
    sender_id=int(sender_id); receiver_id=int(receiver_id); amount=int(amount)
    if sender_id == receiver_id: raise ValueError('Нельзя отправить орбы самому себе')
    if amount < 1: raise ValueError('Сумма должна быть не меньше 1 орба')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        sender=c.execute('SELECT balance,banned FROM users WHERE user_id=?',(sender_id,)).fetchone()
        receiver=c.execute('SELECT user_id,balance,banned FROM users WHERE user_id=?',(receiver_id,)).fetchone()
        if not sender or not receiver: c.rollback(); raise ValueError('Игрок не найден')
        if int(sender['banned'] or 0) or int(receiver['banned'] or 0): c.rollback(); raise ValueError('Перевод недоступен для заблокированного аккаунта')
        debit=c.execute('UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?',(amount,sender_id,amount))
        if debit.rowcount != 1:
            c.rollback(); raise ValueError('Недостаточно орбов')
        c.execute('UPDATE users SET balance=balance+? WHERE user_id=?',(amount,receiver_id))
        sb=c.execute('SELECT balance FROM users WHERE user_id=?',(sender_id,)).fetchone()['balance']
        rb=c.execute('SELECT balance FROM users WHERE user_id=?',(receiver_id,)).fetchone()['balance']
        c.commit()
        return {'sender_balance':int(sb),'receiver_balance':int(rb),'amount':amount}

def update_balance(user_id, amount):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r = c.execute('SELECT balance FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not r: 
            c.rollback()
            raise ValueError('Пользователь не найден')
        new = max(0, int(r['balance']) + int(amount))
        c.execute('UPDATE users SET balance=? WHERE user_id=?', (new, int(user_id)))
        c.commit()
        return new

def set_balance(user_id, balance):
    with db() as c:
        c.execute('UPDATE users SET balance=? WHERE user_id=?', (max(0,int(balance)), int(user_id)))

def update_stars(user_id, amount):
    with db() as c:
        c.execute('UPDATE users SET stars=MAX(0,stars+?) WHERE user_id=?', (int(amount), int(user_id)))

def update_level(user_id, new_level):
    with db() as c:
        c.execute('UPDATE users SET level=? WHERE user_id=?', (max(0,int(new_level)), int(user_id)))

def increment_trades(user_id):
    with db() as c:
        c.execute('UPDATE users SET trades=trades+1 WHERE user_id=?', (int(user_id),))


KEY_CATALOG_SERVER = [
    {'id':'rust-key','name':'Ключ латунного сейфа','rarity':'common','value':120,'icon':'🗝️'},
    {'id':'brass-key','name':'Ключ латунного сейфа','rarity':'rare','value':650,'icon':'🔑'},
    {'id':'obsidian-key','name':'Ключ чёрного сейфа','rarity':'epic','value':2800,'icon':'🗝️'},
    {'id':'vault-key','name':'Ключ хранилища сейфа','rarity':'legendary','value':12000,'icon':'🔐'},
    {'id':'master-key','name':'Мастер-ключ','rarity':'mythic','value':65000,'icon':'🔑'},
]

def get_keys(user_id):
    u = get_user(user_id)
    if not u:
        return []
    try:
        keys = json.loads(u.get('keys') or '[]')
    except Exception:
        keys = []
    if not isinstance(keys, list):
        return []
    # Серверное стакование: одинаковые ключи остаются одной логической группой
    # с полем stack_count, при этом внутренние id сохраняются для совместимости.
    stacks = {}
    for k in keys:
        if not isinstance(k, dict):
            continue
        sid = str(k.get('key_type_id') or k.get('type_id') or k.get('catalog_id') or k.get('name') or '')
        if sid not in stacks:
            item = dict(k)
            item['key_type_id'] = sid
            item['stack_count'] = 0
            stacks[sid] = item
        stacks[sid]['stack_count'] += 1
    return list(stacks.values())

def set_keys(user_id, keys):
    clean=[]
    for k in (keys or []):
        if not isinstance(k, dict):
            continue
        try:
            kid=str(k.get('id') or '')
            catalog=next((x for x in KEY_CATALOG_SERVER if x['id']==kid), None)
            if not catalog:
                continue
            clean.append({
                'id': kid if kid else secrets.token_hex(8),
                'key_type_id': catalog['id'],
                'name': catalog['name'],
                'rarity': catalog['rarity'],
                'value': int(catalog['value']),
                'icon': catalog['icon'],
                'time': int(k.get('time') or 0),
            })
        except Exception:
            continue
    with db() as c:
        c.execute('UPDATE users SET keys=? WHERE user_id=?', (json.dumps(clean, ensure_ascii=False), int(user_id)))

def add_key(user_id, key_id):
    catalog=next((x for x in KEY_CATALOG_SERVER if x['id']==str(key_id)), None)
    if not catalog:
        raise ValueError('Неизвестный ключ')
    key={**catalog, 'key_type_id': catalog['id'], 'id': secrets.token_hex(12), 'time': int(datetime.datetime.now().timestamp()*1000)}
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT keys FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        if not row:
            c.rollback(); raise ValueError('Пользователь не найден')
        try: keys=json.loads(row['keys'] or '[]')
        except Exception: keys=[]
        keys=[k for k in keys if isinstance(k,dict)]
        keys.append(key)
        c.execute('UPDATE users SET keys=? WHERE user_id=?',(json.dumps(keys,ensure_ascii=False),int(user_id)))
        c.commit()
    return key


def consume_key_type_atomic(user_id, key_catalog_id):
    """Consume one key of a given catalog type atomically.

    Supports both current catalog ids and legacy key records that only kept
    name/rarity/value, so an existing key is not falsely reported as missing.
    """
    catalog = next((k for k in KEY_CATALOG_SERVER if k['id'] == str(key_catalog_id)), None)
    if not catalog:
        raise ValueError('Неизвестный ключ')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT keys FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not row:
            c.rollback(); raise ValueError('Пользователь не найден')
        try:
            keys = json.loads(row['keys'] or '[]')
        except Exception:
            keys = []
        keys = [k for k in keys if isinstance(k, dict)]

        def matches(k):
            explicit = str(k.get('key_type_id') or k.get('type_id') or k.get('catalog_id') or '').strip()
            if explicit:
                return explicit == str(key_catalog_id)
            return (
                str(k.get('name') or k.get('key_name') or '').strip() == str(catalog.get('name') or '').strip()
                and str(k.get('rarity') or '').strip() == str(catalog.get('rarity') or '').strip()
                and int(k.get('value') or 0) == int(catalog.get('value') or 0)
            )

        pos = next((i for i,k in enumerate(keys) if matches(k)), None)
        if pos is None:
            c.rollback()
            raise ValueError(f'Нужен {catalog["name"]}')
        consumed = keys.pop(pos)
        c.execute('UPDATE users SET keys=? WHERE user_id=?',
                  (json.dumps(keys, ensure_ascii=False), int(user_id)))
        c.commit()
    return consumed

def consume_key_types_atomic(user_id, key_catalog_ids, count=1):
    """Consume count keys matching any of the supplied catalog ids atomically."""
    catalogs=[k for k in KEY_CATALOG_SERVER if k['id'] in {str(x) for x in (key_catalog_ids or [])}]
    if not catalogs or int(count) < 1:
        raise ValueError('Неизвестный ключ')
    count=int(count)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT keys FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        if not row:
            c.rollback(); raise ValueError('Пользователь не найден')
        try: keys=json.loads(row['keys'] or '[]')
        except Exception: keys=[]
        keys=[k for k in keys if isinstance(k,dict)]

        def matches(k, catalog):
            explicit=str(k.get('key_type_id') or k.get('type_id') or k.get('catalog_id') or '').strip()
            if explicit:
                return explicit == str(catalog['id'])
            return (
                str(k.get('name') or k.get('key_name') or '').strip()==str(catalog.get('name') or '').strip()
                and str(k.get('rarity') or '').strip()==str(catalog.get('rarity') or '').strip()
                and int(k.get('value') or 0)==int(catalog.get('value') or 0)
            )

        positions=[]
        for i,k in enumerate(keys):
            if any(matches(k,c) for c in catalogs):
                positions.append(i)
                if len(positions)>=count: break
        if len(positions)<count:
            c.rollback()
            raise ValueError(f'Нужно ключей: {count}')
        consumed=[keys[i] for i in positions]
        kept=[k for i,k in enumerate(keys) if i not in set(positions)]
        c.execute('UPDATE users SET keys=? WHERE user_id=?',(json.dumps(kept,ensure_ascii=False),int(user_id)))
        c.commit()
    return {'consumed':consumed,'keys':kept}


def clear_keys(user_id):
    with db() as c:
        c.execute("UPDATE users SET keys='[]' WHERE user_id=?", (int(user_id),))

def combine_keys_atomic(user_id, key_catalog_id):
    """Combine 3 identical keys into 1 key of the next tier."""
    catalog = next((k for k in KEY_CATALOG_SERVER if k['id'] == str(key_catalog_id)), None)
    if not catalog:
        raise ValueError('Неизвестный тип ключа')
    idx = KEY_CATALOG_SERVER.index(catalog)
    if idx >= len(KEY_CATALOG_SERVER) - 1:
        raise ValueError('Этот ключ уже максимального уровня')
    target = KEY_CATALOG_SERVER[idx + 1]
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT keys FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not row:
            c.rollback(); raise ValueError('Пользователь не найден')
        try: keys = json.loads(row['keys'] or '[]')
        except Exception: keys = []
        keys = [k for k in keys if isinstance(k, dict)]
        matching = [k for k in keys if (str(k.get('key_type_id') or k.get('type_id') or k.get('catalog_id') or k.get('id')) == str(catalog['id']))]
        if len(matching) < 3:
            c.rollback(); raise ValueError('Нужно 3 одинаковых ключа')
        removed = 0; kept = []
        for k in keys:
            if (str(k.get('key_type_id') or k.get('type_id') or k.get('catalog_id') or k.get('id')) == str(catalog['id'])) and removed < 3:
                removed += 1
                continue
            kept.append(k)
        reward = {**target, 'id': secrets.token_hex(12), 'time': int(datetime.datetime.now().timestamp()*1000)}
        kept.append(reward)
        c.execute('UPDATE users SET keys=? WHERE user_id=?', (json.dumps(kept, ensure_ascii=False), int(user_id)))
        c.commit()
        return {'keys': kept, 'consumed': 3, 'reward': reward}

def _key_catalog_item(name, rarity, value):
    for k in KEY_CATALOG_SERVER:
        if k['name']==str(name) and k['rarity']==str(rarity) and int(k['value'])==int(value):
            return k
    return None

def upgrade_key_atomic(user_id, key_id, target_name, target_rarity, target_value, stake=0):
    from upgrade_config import upgrade_chance, is_valid_target
    stake=max(0,int(stake or 0))
    target_name=str(target_name or '')[:80]
    target_rarity=str(target_rarity or 'common')[:20]
    target_value=max(1,int(target_value or 1))
    target_key=_key_catalog_item(target_name,target_rarity,target_value)
    if not target_key and not is_valid_target(target_name,target_rarity,target_value):
        raise ValueError('Недопустимая цель апгрейда')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        u=c.execute('SELECT balance,keys FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        if not u:
            c.rollback(); raise ValueError('Пользователь не найден')
        if int(u['balance'] or 0)<stake:
            c.rollback(); raise ValueError('Недостаточно монет для дополнительной ставки')
        try: keys=json.loads(u['keys'] or '[]')
        except Exception: keys=[]
        src=next((k for k in keys if isinstance(k,dict) and str(k.get('id'))==str(key_id)),None)
        if not src:
            c.rollback(); raise ValueError('Ключ не найден на сервере. Обнови инвентарь')
        source_value=max(1,int(src.get('value') or 0))
        # Upgrade chance is exactly the value shown by the client: source value +
        # optional stake divided by target value. No hidden rarity multiplier is
        # allowed to make the wheel visually differ from the displayed percent.
        chance = max(0.0, min(100.0, ((source_value + stake) / max(1, target_value)) * 100.0))
        streak_row=c.execute('SELECT upgrade_fail_streak, upgrade_win_streak FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        fail_streak=int((streak_row['upgrade_fail_streak'] if streak_row else 0) or 0)
        win_streak=int((streak_row['upgrade_win_streak'] if streak_row else 0) or 0)
        streak_penalty = False
        roll=secrets.randbelow(1000000)/10000.0
        effective_chance = chance
        protected = False
        success=roll<effective_chance
        keys=[k for k in keys if not (isinstance(k,dict) and str(k.get('id'))==str(key_id))]
        c.execute('UPDATE users SET balance=balance-? WHERE user_id=?',(stake,int(user_id)))
        if success:
            if target_key:
                reward={**target_key,'id':secrets.token_hex(12),'time':int(datetime.datetime.now().timestamp()*1000)}
                keys.append(reward)
            else:
                c.execute('INSERT INTO user_cubes(user_id,name,rarity,value,metadata) VALUES(?,?,?,?,?)',
                          (int(user_id),target_name,target_rarity,target_value,'{}'))
        c.execute('UPDATE users SET keys=? WHERE user_id=?',(json.dumps(keys,ensure_ascii=False),int(user_id)))
        new_fail_streak = 0 if success else fail_streak + 1
        new_win_streak = (win_streak + 1) if success else 0
        c.execute('UPDATE users SET upgrade_fail_streak=?, upgrade_win_streak=? WHERE user_id=?',(new_fail_streak,new_win_streak,int(user_id)))
        fresh=c.execute('SELECT balance FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        c.commit()
        return {'balance':int(fresh['balance']),'success':bool(success),'chance':round(effective_chance,2),'base_chance':round(chance,2),'roll':round(roll,2),
                'protected':bool(protected),'fail_streak':new_fail_streak,'win_streak':new_win_streak,'streak_penalty':bool(streak_penalty),
                'keys':keys,'cubes':get_cubes(user_id)}

def get_cubes(user_id):
    with db() as c:
        rows = c.execute('SELECT id,name,rarity,value,metadata,created_at FROM user_cubes WHERE user_id=? ORDER BY id', (int(user_id),)).fetchall()
        return [dict(r) | {'metadata': json.loads(r['metadata'] or '{}')} for r in rows]

def set_cubes(user_id, cubes):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM user_cubes WHERE user_id=?', (int(user_id),))
        for x in cubes or []:
            if not isinstance(x, dict): 
                continue
            meta = dict(x.get('metadata') or {}) if isinstance(x.get('metadata'), dict) else {}
            # Сохраняем дополнительные поля куба, но не дублируем системные поля.
            for k,v in x.items():
                if k not in {'id','name','rarity','value','metadata','created_at'}:
                    meta[k]=v
            c.execute('INSERT INTO user_cubes(user_id,name,rarity,value,metadata) VALUES(?,?,?,?,?)', 
                     (int(user_id), str(x.get('name','Куб')), str(x.get('rarity','common')), 
                      int(x.get('value',0)), json.dumps(meta, ensure_ascii=False)))
        c.commit()

def set_cases_opened(user_id, value):
    with db() as c:
        c.execute('UPDATE users SET cases_opened=? WHERE user_id=?', (max(0, int(value)), int(user_id)))

def set_battle_winnings(user_id, value):
    with db() as c:
        c.execute('UPDATE users SET battle_winnings=? WHERE user_id=?', (max(0, int(value)), int(user_id)))

def set_avatar(user_id, avatar):
    with db() as c:
        c.execute('UPDATE users SET avatar=? WHERE user_id=?', (avatar or None, int(user_id)))

def add_recent_win(user_id, username, cube_name, rarity):
    with db() as c:
        c.execute('INSERT INTO recent_wins (user_id, username, cube_name, rarity) VALUES (?,?,?,?)',
                   (int(user_id), (username or '')[:32], (cube_name or '')[:80], (rarity or 'common')[:20]))
        # держим таблицу компактной — оставляем последние 200 записей
        c.execute('DELETE FROM recent_wins WHERE id NOT IN (SELECT id FROM recent_wins ORDER BY id DESC LIMIT 200)')

def get_recent_wins(limit=3):
    with db() as c:
        rows = c.execute('''
            SELECT user_id, username, cube_name, rarity, created_at
            FROM recent_wins ORDER BY id DESC LIMIT ?
        ''', (int(limit),)).fetchall()
        return [dict(r) for r in rows]

def create_trade(from_user, to_user, offer_cube_id, request_cube_name):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        cube = c.execute('SELECT * FROM user_cubes WHERE id=? AND user_id=?', (int(offer_cube_id), int(from_user))).fetchone()
        if not cube:
            c.rollback()
            raise ValueError('Куб не найден в твоём инвентаре')
        to_exists = c.execute('SELECT 1 FROM users WHERE user_id=?', (int(to_user),)).fetchone()
        if not to_exists:
            c.rollback()
            raise ValueError('Игрок с таким ID не найден')
        c.execute('''INSERT INTO trades(from_user,to_user,offer_cube_id,offer_cube_name,offer_cube_rarity,offer_cube_value,request_cube_name)
                      VALUES(?,?,?,?,?,?,?)''',
                   (int(from_user), int(to_user), cube['id'], cube['name'], cube['rarity'], cube['value'],
                    (request_cube_name or '').strip() or None))
        c.commit()

def get_trades_for(user_id):
    with db() as c:
        incoming = c.execute('''
            SELECT t.*, COALESCE(NULLIF(u.display_name,''), u.username) from_name
            FROM trades t LEFT JOIN users u ON u.user_id=t.from_user
            WHERE t.to_user=? AND t.status='pending' ORDER BY t.id DESC
        ''', (int(user_id),)).fetchall()
        outgoing = c.execute('''
            SELECT t.*, COALESCE(NULLIF(u.display_name,''), u.username) to_name
            FROM trades t LEFT JOIN users u ON u.user_id=t.to_user
            WHERE t.from_user=? AND t.status='pending' ORDER BY t.id DESC
        ''', (int(user_id),)).fetchall()
        return [dict(r) for r in incoming], [dict(r) for r in outgoing]

def respond_trade(trade_id, user_id, accept):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        t = c.execute('SELECT * FROM trades WHERE id=?', (int(trade_id),)).fetchone()
        if not t or t['status'] != 'pending':
            c.rollback()
            raise ValueError('Обмен не найден или уже закрыт')
        if int(t['to_user']) != int(user_id):
            c.rollback()
            raise ValueError('Это не твой обмен')
        if not accept:
            c.execute("UPDATE trades SET status='declined' WHERE id=?", (int(trade_id),))
            c.commit()
            return
        offered = c.execute('SELECT * FROM user_cubes WHERE id=? AND user_id=?', (t['offer_cube_id'], t['from_user'])).fetchone()
        if not offered:
            c.execute("UPDATE trades SET status='cancelled' WHERE id=?", (int(trade_id),))
            c.commit()
            raise ValueError('Этого предмета у отправителя больше нет')
        requested_id = None
        if t['request_cube_name']:
            req_row = c.execute('SELECT id FROM user_cubes WHERE user_id=? AND name=? LIMIT 1', (int(user_id), t['request_cube_name'])).fetchone()
            if not req_row:
                c.rollback()
                raise ValueError('У тебя нет запрошенного предмета — обмен невозможен')
            requested_id = req_row['id']
        c.execute('UPDATE user_cubes SET user_id=? WHERE id=?', (int(user_id), offered['id']))
        if requested_id:
            c.execute('UPDATE user_cubes SET user_id=? WHERE id=?', (int(t['from_user']), requested_id))
        c.execute("UPDATE trades SET status='accepted' WHERE id=?", (int(trade_id),))
        c.commit()

def cancel_trade(trade_id, user_id):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        t = c.execute('SELECT * FROM trades WHERE id=?', (int(trade_id),)).fetchone()
        if not t or t['status'] != 'pending':
            c.rollback()
            raise ValueError('Обмен не найден')
        if int(t['from_user']) != int(user_id):
            c.rollback()
            raise ValueError('Это не твой обмен')
        c.execute("UPDATE trades SET status='cancelled' WHERE id=?", (int(trade_id),))
        c.commit()

def get_leaderboard(sort_by='cases', limit=15):
    # Топ всегда строится по серверной таблице users. Локальная сессия/логин
    # не влияет на наличие аккаунта в рейтинге: вышел из аккаунта — строка
    # в users всё равно остаётся и продолжает участвовать в топе.
    col = {'cases': 'cases_opened', 'balance': 'balance', 'winnings': 'battle_winnings'}.get(sort_by, 'cases_opened')
    with db() as c:
        rows = c.execute(f'''
            SELECT user_id, COALESCE(NULLIF(display_name,''), username) AS username,
                   balance, cases_opened, battle_winnings, avatar,
                   premium_until, creator_badge, tester_badge, custom_status,
                   buffs, best_drops, battles_played, total_wins
            FROM users
            WHERE banned=0 AND shadow_banned=0
            ORDER BY {col} DESC, created_at ASC, user_id ASC
            LIMIT ?
        ''', (int(limit),)).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            try: item['buffs']=json.loads(item.get('buffs') or '[]')
            except Exception: item['buffs']=[]
            try: item['best_drops']=json.loads(item.get('best_drops') or '[]')
            except Exception: item['best_drops']=[]
            try: item['custom_status']=json.loads(item.get('custom_status') or '{}')
            except Exception: item['custom_status']={}
            item['cube_count']=len(get_cubes(item['user_id']))
            item['wins']=int(item.get('total_wins') or 0)
            item['battles']=int(item.get('battles_played') or 0)
            result.append(item)
        return result

def _is_premium_row(user_row):
    pu = user_row['premium_until'] if user_row and 'premium_until' in user_row.keys() else None
    return bool(pu) and str(pu) > datetime.datetime.now().isoformat()

def set_cube_locked(user_id,cube_id,locked=True):
    with db() as c:
        r=c.execute('SELECT metadata FROM user_cubes WHERE user_id=? AND id=?',(int(user_id),int(cube_id))).fetchone()
        if not r: raise ValueError('Куб не найден')
        try: meta=json.loads(r['metadata'] or '{}')
        except Exception: meta={}
        meta['locked']=bool(locked)
        c.execute('UPDATE user_cubes SET metadata=? WHERE user_id=? AND id=?',(json.dumps(meta,ensure_ascii=False),int(user_id),int(cube_id)))
        c.commit()

def sell_cube(user_id, cube_id):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        u = c.execute('SELECT premium_until FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        ratio = 85 if _is_premium_row(u) else 70
        row = c.execute('SELECT value,metadata FROM user_cubes WHERE user_id=? AND id=?', (int(user_id), int(cube_id))).fetchone()
        if not row:
            c.rollback()
            raise ValueError('Куб не найден')
        try: meta=json.loads(row['metadata'] or '{}')
        except Exception: meta={}
        if meta.get('locked'): c.rollback(); raise ValueError('Куб заморожен — сначала разблокируй его')
        gain = max(0, int(row['value']) * ratio // 100)
        c.execute('DELETE FROM user_cubes WHERE id=?', (int(cube_id),))
        c.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (gain, int(user_id)))
        r = c.execute('SELECT balance FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        c.commit()
        return r['balance'], gain

def sell_all_cubes(user_id):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        u = c.execute('SELECT premium_until FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        ratio = 85 if _is_premium_row(u) else 70
        rows=c.execute('SELECT id,value,metadata FROM user_cubes WHERE user_id=?',(int(user_id),)).fetchall()
        unlocked=[]
        for r in rows:
            try: meta=json.loads(r['metadata'] or '{}')
            except Exception: meta={}
            if not meta.get('locked'): unlocked.append(r)
        gain=sum(max(0,int(r['value'])*ratio//100) for r in unlocked)
        if unlocked:
            ids=','.join('?'*len(unlocked))
            c.execute(f'DELETE FROM user_cubes WHERE user_id=? AND id IN ({ids})',(int(user_id),*[int(r['id']) for r in unlocked]))
        c.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (gain, int(user_id)))
        r = c.execute('SELECT balance FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        c.commit()
        return r['balance'], gain, len(rows)

def case_open(user_id, price, name, rarity, value):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r = c.execute('SELECT balance FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not r:
            c.rollback()
            raise ValueError('Пользователь не найден')
        bal = int(r['balance'])
        price = max(0, int(price))
        if bal < price:
            c.rollback()
            raise ValueError('Недостаточно монет')
        new_bal = bal - price
        c.execute('UPDATE users SET balance=?, cases_opened=COALESCE(cases_opened,0)+1 WHERE user_id=?', (new_bal, int(user_id)))
        c.execute('INSERT INTO user_cubes(user_id,name,rarity,value,metadata) VALUES(?,?,?,?,?)',
                   (int(user_id), name, rarity, int(value), '{}'))
        c.commit()
        return new_bal


def case_open_batch(user_id, price, drops, request_id=None, case_id='', cooldown_ms=900):
    """Atomically open a batch, prevent duplicate submissions and return the saved result on retry."""
    price = max(0, int(price))
    drops = list(drops or [])
    if not drops:
        raise ValueError('Нет наград для открытия')
    request_id = str(request_id or '').strip()[:120]
    if not request_id:
        raise ValueError('Не указан request_id')
    case_id = str(case_id or '')[:80]
    total = price * len(drops)
    uid = int(user_id)
    now_ms = int(datetime.datetime.now().timestamp() * 1000)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')

        existing = c.execute(
            'SELECT balance,drops,qty,case_id FROM case_open_requests WHERE user_id=? AND request_id=?',
            (uid, request_id)
        ).fetchone()
        if existing:
            c.rollback()
            try:
                saved_drops = json.loads(existing['drops'] or '[]')
            except Exception:
                saved_drops = []
            return {
                'balance': float(existing['balance']),
                'drops': saved_drops,
                'qty': int(existing['qty'] or len(saved_drops)),
                'case_id': str(existing['case_id'] or case_id),
                'duplicate': True
            }

        guard = c.execute('SELECT last_open_ms FROM case_open_guard WHERE user_id=?', (uid,)).fetchone()
        last_ms = int(guard['last_open_ms'] or 0) if guard else 0
        if last_ms and now_ms - last_ms < int(cooldown_ms):
            c.rollback()
            raise ValueError('Открытие уже обрабатывается. Подожди немного и попробуй снова.')

        r = c.execute('SELECT balance FROM users WHERE user_id=?', (uid,)).fetchone()
        if not r:
            c.rollback()
            raise ValueError('Пользователь не найден')
        bal = float(r['balance'] or 0)
        if bal < total:
            c.rollback()
            raise ValueError('Недостаточно монет')
        new_bal = round(bal - total, 2)

        c.execute(
            'INSERT INTO case_open_guard(user_id,last_open_ms) VALUES(?,?) '
            'ON CONFLICT(user_id) DO UPDATE SET last_open_ms=excluded.last_open_ms',
            (uid, now_ms)
        )
        c.execute(
            'UPDATE users SET balance=?, cases_opened=COALESCE(cases_opened,0)+? WHERE user_id=?',
            (new_bal, len(drops), uid)
        )
        for item in drops:
            name = str(item['name'])[:120]
            rarity = str(item['rarity'])[:20]
            value = int(item['value'])
            c.execute(
                'INSERT INTO user_cubes(user_id,name,rarity,value,metadata) VALUES(?,?,?,?,?)',
                (uid, name, rarity, value, '{}')
            )

        saved_json = json.dumps(drops, ensure_ascii=False, separators=(',', ':'))
        c.execute(
            'INSERT INTO case_open_requests(user_id,request_id,case_id,qty,balance,drops) VALUES(?,?,?,?,?,?)',
            (uid, request_id, case_id, len(drops), new_bal, saved_json)
        )
        c.execute("DELETE FROM case_open_requests WHERE user_id=? AND created_at < datetime('now','-2 days')", (uid,))
        c.commit()
        return {'balance': new_bal, 'drops': drops, 'qty': len(drops), 'case_id': case_id, 'duplicate': False}


def clicker_claim(user_id, request_id, reward=1, **kwargs):
    """Server-authoritative clicker: 120s activity with <=10s pauses triggers a 1h clicker ban; 4000/day cap."""
    uid=int(user_id); request_id=str(request_id or '').strip()[:120]
    if not request_id: raise ValueError('Не указан request_id')
    reward=1; now_ms=int(datetime.datetime.now().timestamp()*1000); day_key=datetime.datetime.now().strftime('%Y-%m-%d')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT balance FROM users WHERE user_id=?',(uid,)).fetchone()
        if not row: c.rollback(); raise ValueError('Пользователь не найден')
        replay=c.execute('SELECT 1 FROM clicker_claims WHERE user_id=? AND request_id=?',(uid,request_id)).fetchone()
        if replay: c.rollback(); return {'balance':int(row['balance'] or 0),'credited':0,'duplicate':True,'reward':0}
        g=c.execute('SELECT last_click_ms,session_start_ms,banned_until_ms,day_key,day_earned FROM clicker_guard WHERE user_id=?',(uid,)).fetchone()
        if not g:
            c.execute('INSERT INTO clicker_guard(user_id,last_click_ms,session_start_ms,banned_until_ms,day_key,day_earned) VALUES(?,?,?,?,?,?)',(uid,0,0,0,day_key,0))
            g={'last_click_ms':0,'session_start_ms':0,'banned_until_ms':0,'day_key':day_key,'day_earned':0}
        banned_until=int(g['banned_until_ms'] or 0)
        if banned_until>now_ms:
            c.rollback(); raise PermissionError(f'Кликер заблокирован ещё {max(1,(banned_until-now_ms+59999)//60000)} мин.')
        last=int(g['last_click_ms'] or 0); session=int(g['session_start_ms'] or 0)
        earned=int(g['day_earned'] or 0) if str(g['day_key'] or '')==day_key else 0
        if last and now_ms-last<=10000:
            if not session: session=last
            if now_ms-session>=120000:
                ban_until=now_ms+3600000
                c.execute('UPDATE clicker_guard SET last_click_ms=?,session_start_ms=?,banned_until_ms=?,day_key=?,day_earned=? WHERE user_id=?',(now_ms,session,ban_until,day_key,earned,uid))
                c.commit(); raise PermissionError('Антиавтокликер: кликер заблокирован на 1 час за 2 минуты активности')
        else:
            session=now_ms
        if earned>=4000:
            c.rollback(); raise ValueError('Дневной лимит кликера — 4 000 монет — уже достигнут')
        credit=min(reward,4000-earned); new_balance=int(row['balance'] or 0)+credit
        c.execute('UPDATE users SET balance=? WHERE user_id=?',(new_balance,uid))
        c.execute('INSERT INTO clicker_claims(user_id,request_id,claimed_at_ms,click_x,click_y) VALUES(?,?,?,?,?)',(uid,request_id,now_ms,0,0))
        c.execute('UPDATE clicker_guard SET last_click_ms=?,session_start_ms=?,banned_until_ms=0,day_key=?,day_earned=? WHERE user_id=?',(now_ms,session,day_key,earned+credit,uid))
        c.execute('DELETE FROM clicker_claims WHERE user_id=? AND claimed_at_ms < ?',(uid,now_ms-86400000))
        c.commit()
        return {'balance':new_balance,'credited':credit,'duplicate':False,'reward':credit,'daily_earned':earned+credit,'daily_limit':4000}

def remove_cube_by_id(user_id, cube_id):
    with db() as c:
        c.execute('DELETE FROM user_cubes WHERE user_id=? AND id=?', (int(user_id), int(cube_id)))

def add_cube(user_id, name, rarity, value=0, metadata=None):
    with db() as c:
        c.execute('INSERT INTO user_cubes(user_id,name,rarity,value,metadata) VALUES(?,?,?,?,?)',
                 (int(user_id), name, rarity, int(value), json.dumps(metadata or {}, ensure_ascii=False)))


def upgrade_cube_atomic(user_id, cube_id, target_name, target_rarity, target_value, stake=0, target_kind='cube', target_key_id=''):
    """Atomically deduct the extra stake and perform a server-authoritative upgrade."""
    stake = max(0, int(stake or 0))
    target_value = max(1, min(50000000, int(target_value or 1)))
    target_name = str(target_name or 'Куб')[:80]
    target_rarity = str(target_rarity or 'common')[:20]
    target_kind = 'key' if str(target_kind) == 'key' else 'cube'
    if target_rarity not in {'common','rare','epic','legendary','mythic','divine','emerald'}:
        target_rarity = 'common'
    target_key = _key_catalog_item(target_name, target_rarity, target_value) if target_kind == 'key' else None
    if target_kind == 'key':
        if not target_key or (target_key_id and str(target_key_id) != str(target_key['id'])):
            raise ValueError('Недопустимая цель-ключ')
    elif not is_valid_target(target_name, target_rarity, target_value):
        raise ValueError('Недопустимая цель апгрейда')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        u = c.execute('SELECT balance,keys FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not u:
            c.rollback()
            raise ValueError('Пользователь не найден')
        balance = int(u['balance'] or 0)
        if balance < stake:
            c.rollback()
            raise ValueError('Недостаточно монет для дополнительной ставки')
        row = c.execute(
            'SELECT id,value,metadata FROM user_cubes WHERE user_id=? AND id=?',
            (int(user_id), int(cube_id))
        ).fetchone()
        if not row:
            c.rollback()
            raise ValueError('Куб не найден в инвентаре')
        try:
            meta = json.loads(row['metadata'] or '{}')
        except Exception:
            meta = {}
        if meta.get('locked'):
            c.rollback()
            raise ValueError('Куб заморожен — сначала разблокируй его')
        source_value = max(1, int(row['value'] or 0))
        # Exact same formula as the UI: (source value + stake) / target value.
        # No hidden rarity modifier may change the server chance after it is shown.
        chance = max(0.0, min(100.0, ((source_value + stake) / max(1, target_value)) * 100.0))
        streak_row = c.execute('SELECT upgrade_fail_streak, upgrade_win_streak FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        fail_streak = int((streak_row['upgrade_fail_streak'] if streak_row else 0) or 0)
        win_streak = int((streak_row['upgrade_win_streak'] if streak_row else 0) or 0)
        streak_penalty = False
        roll = secrets.randbelow(1000000) / 10000.0
        effective_chance = chance
        protected = False
        success = roll < effective_chance

        keys = []
        try: keys = json.loads(u['keys'] or '[]')
        except Exception: keys = []
        keys = [k for k in keys if isinstance(k, dict)]
        c.execute('UPDATE users SET balance=balance-? WHERE user_id=?', (stake, int(user_id)))
        c.execute('DELETE FROM user_cubes WHERE id=? AND user_id=?', (int(row['id']), int(user_id)))
        if success:
            if target_kind == 'key':
                reward = {**target_key, 'id': secrets.token_hex(12), 'time': int(datetime.datetime.now().timestamp()*1000)}
                keys.append(reward)
            else:
                c.execute(
                    'INSERT INTO user_cubes(user_id,name,rarity,value,metadata) VALUES(?,?,?,?,?)',
                    (int(user_id), target_name, target_rarity, target_value, '{}')
                )
        c.execute('UPDATE users SET keys=? WHERE user_id=?', (json.dumps(keys, ensure_ascii=False), int(user_id)))
        new_balance = c.execute(
            'SELECT balance FROM users WHERE user_id=?', (int(user_id),)
        ).fetchone()['balance']
        fresh = c.execute(
            'SELECT id,name,rarity,value,metadata FROM user_cubes WHERE user_id=? ORDER BY id',
            (int(user_id),)
        ).fetchall()
        cubes = []
        for r in fresh:
            try: m = json.loads(r['metadata'] or '{}')
            except Exception: m = {}
            cubes.append({'id': r['id'], 'name': r['name'], 'rarity': r['rarity'],
                          'value': r['value'], 'metadata': m})
        c.execute('UPDATE users SET keys=? WHERE user_id=?', (json.dumps(keys, ensure_ascii=False), int(user_id)))
        new_streak = 0 if success else fail_streak + 1
        c.execute('UPDATE users SET upgrade_fail_streak=? WHERE user_id=?', (new_streak, int(user_id)))
        c.commit()
        return {
            'balance': int(new_balance), 'success': bool(success),
            'chance': round(chance, 2), 'roll': round(roll, 2),
            'protected': bool(protected), 'fail_streak': new_streak,
            'cubes': cubes, 'keys': keys
        }


def can_claim_daily(user_id):
    u = get_user(user_id)
    if not u or not u.get('daily_claimed'): 
        return True
    return str(u['daily_claimed']) < datetime.datetime.now().strftime('%Y-%m-%d')

def claim_daily(user_id, amount=500):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        u = c.execute('SELECT daily_claimed,daily_streak,balance FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not u:
            c.rollback()
            raise ValueError('Пользователь не найден')
        today = datetime.datetime.now().date()
        claimed = u['daily_claimed']
        if claimed == today.strftime('%Y-%m-%d'):
            c.rollback()
            raise ValueError('Ежедневный бонус уже получен')

        # Серия сохраняется только при входе каждый день. После пропуска дня
        # серия начинается заново с первого дня.
        streak = int(u['daily_streak'] or 0)
        if claimed:
            try:
                previous = datetime.datetime.strptime(str(claimed), '%Y-%m-%d').date()
                if (today - previous).days == 1:
                    streak += 1
                else:
                    streak = 1
            except ValueError:
                streak = 1
        else:
            streak = 1
        streak = min(max(streak, 1), 7)

        # Базовая награда растёт по серии: 500 → 1000. На 7-й день — максимум.
        reward = int(amount) + (streak - 1) * 100
        bal = int(u['balance']) + reward
        c.execute('UPDATE users SET balance=?,daily_claimed=?,daily_streak=? WHERE user_id=?',
                  (bal,today.strftime('%Y-%m-%d'),streak,int(user_id)))
        c.execute('INSERT INTO economy_events(user_id,source,net_profit,wager) VALUES(?,?,?,0)',(int(user_id),'daily',int(reward)))
        c.commit()
        return {'balance': bal, 'reward': reward, 'streak': streak}

def set_ban(user_id, value, reason=''):
    uid=int(user_id); val=1 if value else 0; reason=str(reason or '').strip()[:500]
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE users SET banned=?, ban_reason=?, moderation_notice=?, ban_until=NULL WHERE user_id=?',
                  (val, reason if val else '', (reason if val else ''), uid))
        if val:
            c.execute('INSERT INTO moderation_notices(user_id,notice_type,reason,show_once) VALUES(?,?,?,0)',
                      (uid,'ban',reason))
        else:
            c.execute('DELETE FROM moderation_notices WHERE user_id=? AND notice_type IN ("ban","shadow_ban")',(uid,))
        c.commit()

def create_moderation_notice(user_id, notice_type, reason, show_once=0):
    with db() as c:
        c.execute('INSERT INTO moderation_notices(user_id,notice_type,reason,show_once) VALUES(?,?,?,?)',
                  (int(user_id), str(notice_type)[:30], str(reason or '').strip()[:500], int(show_once)))

def get_moderation_notice(user_id):
    with db() as c:
        r=c.execute('SELECT id,notice_type,reason,show_once,created_at FROM moderation_notices WHERE user_id=? ORDER BY id DESC LIMIT 1',
                    (int(user_id),)).fetchone()
        if not r: return None
        out=dict(r)
        if int(out.get('show_once') or 0):
            c.execute('DELETE FROM moderation_notices WHERE id=?',(int(out['id']),))
        return out

def set_shadow_ban(user_id, value):
    with db() as c:
        c.execute('UPDATE users SET shadow_banned=? WHERE user_id=?', (1 if value else 0, int(user_id)))

def find_user_by_username(username):
    name = str(username or '').strip().lower()
    if not name: return None
    with db() as c:
        r = c.execute("SELECT * FROM users WHERE lower(username)=? OR lower(display_name)=? LIMIT 1", (name, name)).fetchone()
        return row_user(r)

def set_two_factor(user_id, enabled):
    with db() as c:
        c.execute('UPDATE users SET two_factor_enabled=?, two_factor_code_hash=NULL, two_factor_expires=NULL WHERE user_id=?', (1 if enabled else 0, int(user_id)))

def set_two_factor_code(user_id, code_hash, expires_at):
    with db() as c:
        c.execute('UPDATE users SET two_factor_code_hash=?, two_factor_expires=? WHERE user_id=?', (code_hash, expires_at, int(user_id)))

def verify_two_factor_code(user_id, code_hash):
    with db() as c:
        r=c.execute('SELECT two_factor_code_hash,two_factor_expires FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        if not r or r['two_factor_code_hash'] != code_hash: return False
        try: valid=datetime.datetime.fromisoformat(str(r['two_factor_expires'])) >= datetime.datetime.now()
        except Exception: valid=False
        if not valid: return False
        c.execute('UPDATE users SET two_factor_code_hash=NULL, two_factor_expires=NULL WHERE user_id=?',(int(user_id),))
        return True

def create_auth_session(user_id, ttl_hours=24*365):
    token=secrets.token_urlsafe(48); token_hash=hashlib.sha256(token.encode()).hexdigest()
    expires=(datetime.datetime.now()+datetime.timedelta(hours=int(ttl_hours))).isoformat()
    with db() as c: c.execute('INSERT OR REPLACE INTO auth_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(token_hash,int(user_id),expires))
    return token

def get_auth_session(token):
    if not token: return None
    token_hash=hashlib.sha256(str(token).encode()).hexdigest()
    with db() as c:
        r=c.execute('SELECT user_id,expires_at FROM auth_sessions WHERE token_hash=?',(token_hash,)).fetchone()
        if not r: return None
        try:
            if datetime.datetime.fromisoformat(str(r['expires_at'])) < datetime.datetime.now():
                c.execute('DELETE FROM auth_sessions WHERE token_hash=?',(token_hash,)); return None
        except Exception: return None
        return int(r['user_id'])

def delete_auth_session(token):
    if not token: return
    token_hash=hashlib.sha256(str(token).encode()).hexdigest()
    with db() as c: c.execute('DELETE FROM auth_sessions WHERE token_hash=?',(token_hash,))

def set_password_hash(user_id, h):
    with db() as c:
        c.execute('UPDATE users SET password_hash=? WHERE user_id=?', (h, int(user_id)))

def grant_premium(user_id, days):
    until = (datetime.datetime.now()+datetime.timedelta(days=int(days))).isoformat()
    with db() as c:
        c.execute('UPDATE users SET premium_until=? WHERE user_id=?', (until, int(user_id)))

def set_creator_badge(user_id, enabled=True):
    with db() as c:
        c.execute(
            'UPDATE users SET creator_badge=? WHERE user_id=?',
            (1 if enabled else 0, int(user_id))
        )

def set_tester_badge(user_id, enabled=True):
    with db() as c:
        c.execute('UPDATE users SET tester_badge=? WHERE user_id=?',(1 if enabled else 0,int(user_id)))

def set_custom_status(user_id, emoji='', text='', gradient=False, color1='#f0c674', color2='#ffffff'):
    emoji = str(emoji or '').strip()[:8]
    text = str(text or '').strip()[:32]
    color_re = re.compile(r'^#[0-9a-fA-F]{6}$')
    color1 = str(color1 or '#f0c674').strip()[:7]
    color2 = str(color2 or '#ffffff').strip()[:7]
    if not color_re.fullmatch(color1):
        color1 = '#f0c674'
    if not color_re.fullmatch(color2):
        color2 = '#ffffff'
    payload = {'emoji': emoji, 'text': text, 'gradient': bool(gradient), 'color1': color1, 'color2': color2} if (emoji or text) else {}
    with db() as c:
        c.execute('UPDATE users SET custom_status=? WHERE user_id=?',
                  (json.dumps(payload, ensure_ascii=False, separators=(',', ':')) if payload else '', int(user_id)))

def set_buffs(user_id, buffs):
    with db() as c:
        c.execute('UPDATE users SET buffs=? WHERE user_id=?',(json.dumps(buffs or [], ensure_ascii=False),int(user_id)))

def get_buffs(user_id):
    u=get_user(user_id)
    try: return json.loads(u.get('buffs') or '[]') if u else []
    except Exception: return []

def has_active_buff(user_id, buff_type):
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    return any(str(b.get('type') or b.get('name') or '') == str(buff_type) and (not b.get('expires_at') or str(b.get('expires_at')) > now) for b in get_buffs(user_id))

def adjust_case_items_for_bad_luck(items, user_id):
    if not has_active_buff(user_id, 'bad_luck'):
        return items
    # Reduce the weight of epic+ rewards by 80%, then normalize via weighted_pick.
    out=[]
    for item in items:
        x=dict(item)
        if str(x.get('rarity','')).lower() in {'epic','legendary','mythic','divine','emerald','изумрудная'}:
            x['chance']=float(x.get('chance') or 0)*0.20
        out.append(x)
    return out

def set_best_drops(user_id, drops):
    with db() as c:
        c.execute('UPDATE users SET best_drops=? WHERE user_id=?',(json.dumps(drops or [], ensure_ascii=False),int(user_id)))

def delete_user(user_id):
    """Permanently remove an account and all rows that reference it.

    Several legacy tables intentionally do not use ON DELETE CASCADE, so a plain
    DELETE FROM users could fail with SQLite FOREIGN KEY constraint errors.
    Clean those dependent rows explicitly in one transaction.
    """
    uid = int(user_id)
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        exists = c.execute('SELECT 1 FROM users WHERE user_id=?', (uid,)).fetchone()
        if not exists:
            c.rollback()
            raise ValueError('Пользователь не найден')

        # Active battles created by the user must be removed before users.
        battle_ids = [r['id'] for r in c.execute('SELECT id FROM battles WHERE creator_id=?', (uid,)).fetchall()]
        if battle_ids:
            marks = ','.join('?' for _ in battle_ids)
            c.execute(f'DELETE FROM battle_players WHERE battle_id IN ({marks})', battle_ids)
            c.execute(f'DELETE FROM battles WHERE id IN ({marks})', battle_ids)

        # The user can also be a participant in somebody else's battle.
        c.execute('DELETE FROM battle_players WHERE user_id=?', (uid,))

        # Tables without ON DELETE CASCADE.
        c.execute('DELETE FROM recent_wins WHERE user_id=?', (uid,))
        c.execute('UPDATE admin_logs SET admin_id=NULL WHERE admin_id=?', (uid,))
        c.execute('DELETE FROM admin_sessions WHERE admin_id=?', (uid,))
        c.execute('UPDATE promo_codes SET created_by=NULL WHERE created_by=?', (uid,))
        c.execute('DELETE FROM trades WHERE from_user=? OR to_user=?', (uid, uid))

        # auth/user-owned tables with CASCADE are safe to leave to SQLite.
        c.execute('DELETE FROM users WHERE user_id=?', (uid,))
        c.commit()


def reset_progress(user_id):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE users SET balance=1000,stars=0,level=0,trades=0,total_spins=0,total_wins=0,total_losses=0,biggest_win=0,daily_claimed=NULL,daily_streak=0,cases_opened=0,battle_winnings=0 WHERE user_id=?', (int(user_id),))
        c.execute('DELETE FROM user_cubes WHERE user_id=?', (int(user_id),))
        c.execute("UPDATE users SET keys='[]' WHERE user_id=?", (int(user_id),))
        c.commit()

def add_admin_log(admin_id, target_id, action, details=''):
    with db() as c:
        c.execute('INSERT INTO admin_logs(admin_id,target_id,action,details) VALUES(?,?,?,?)', 
                 (int(admin_id), int(target_id), action, details))

def create_admin_session(token_hash, admin_id, expires_at):
    with db() as c:
        c.execute('DELETE FROM admin_sessions WHERE admin_id=? OR expires_at <= CURRENT_TIMESTAMP', (int(admin_id),))
        c.execute('INSERT INTO admin_sessions(token_hash,admin_id,expires_at) VALUES(?,?,?)', (str(token_hash), int(admin_id), str(expires_at)))
        c.commit()

def get_admin_session(token_hash, admin_id):
    with db() as c:
        row=c.execute('SELECT admin_id FROM admin_sessions WHERE token_hash=? AND admin_id=? AND expires_at > CURRENT_TIMESTAMP', (str(token_hash), int(admin_id))).fetchone()
        if not row:
            return False
        c.execute('UPDATE admin_sessions SET last_used_at=CURRENT_TIMESTAMP WHERE token_hash=?', (str(token_hash),))
        c.commit()
        return True

def revoke_admin_sessions(admin_id):
    with db() as c:
        c.execute('DELETE FROM admin_sessions WHERE admin_id=?', (int(admin_id),))
        c.commit()

def get_admin_logs():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM admin_logs ORDER BY id DESC LIMIT 100').fetchall()]

def get_shop_items_public(viewer_id=None):
    with db() as c:
        rows = c.execute('''
            SELECT s.id,s.seller_id,s.cube_name,s.cube_rarity,s.cube_value,s.price,s.currency,s.created_at,
                   COALESCE(NULLIF(u.display_name,''), u.username) seller_name
            FROM shop s 
            LEFT JOIN users u ON u.user_id=s.seller_id 
            WHERE s.sold=0 
            ORDER BY s.id DESC
        ''').fetchall()
        return [dict(r) for r in rows]

def list_shop_item_atomic(seller_id, name, rarity, value, price, currency='coins', cube_id=None, index=None):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        
        if cube_id is not None:
            r = c.execute('SELECT id,metadata FROM user_cubes WHERE id=? AND user_id=?', (int(cube_id), int(seller_id))).fetchone()
            if not r:
                c.rollback()
                raise ValueError('Куб не найден')
            try: meta=json.loads(r['metadata'] or '{}')
            except Exception: meta={}
            if meta.get('locked'): c.rollback(); raise ValueError('Куб заморожен — сначала разблокируй его')
            c.execute('DELETE FROM user_cubes WHERE id=?', (int(cube_id),))
        elif index is not None:
            rows = c.execute('SELECT id,metadata FROM user_cubes WHERE user_id=? ORDER BY id', (int(seller_id),)).fetchall()
            idx = int(index)
            if idx < 0 or idx >= len(rows):
                c.rollback()
                raise ValueError('Куб не найден')
            try: meta = json.loads(rows[idx]['metadata'] or '{}')
            except Exception: meta = {}
            if meta.get('locked'):
                c.rollback()
                raise ValueError('Куб заморожен — сначала разблокируй его')
            cube_id = rows[idx]['id']
            c.execute('DELETE FROM user_cubes WHERE id=?', (cube_id,))
        else:
            c.rollback()
            raise ValueError('Не указан куб для продажи')
        
        c.execute('''
            INSERT INTO shop(seller_id, cube_name, cube_rarity, cube_value, price, currency, sold) 
            VALUES(?,?,?,?,?,?,0)
        ''', (int(seller_id), name, rarity, int(value), int(price), currency))
        
        item_id = c.execute('SELECT last_insert_rowid() x').fetchone()['x']
        c.commit()
        return item_id

def cancel_shop_item_atomic(item_id, seller_id):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r = c.execute('SELECT seller_id,cube_name,cube_rarity,cube_value FROM shop WHERE id=? AND sold=0', (int(item_id),)).fetchone()
        if not r:
            c.rollback()
            raise ValueError('Объявление не найдено')
        if int(r['seller_id']) != int(seller_id):
            c.rollback()
            raise ValueError('Это не твоё объявление')
        c.execute('UPDATE shop SET sold=1 WHERE id=?', (int(item_id),))
        c.execute('INSERT INTO user_cubes(user_id,name,rarity,value,metadata) VALUES(?,?,?,?,?)',
                 (int(seller_id), r['cube_name'], r['cube_rarity'], int(r['cube_value']), json.dumps({'returned_from_market':True})))
        c.commit()
        return {'name':r['cube_name'], 'rarity':r['cube_rarity'], 'value':r['cube_value']}

def buy_shop_item_atomic(item_id, buyer_id):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r = c.execute('SELECT seller_id,cube_name,cube_rarity,cube_value,price FROM shop WHERE id=? AND sold=0', (int(item_id),)).fetchone()
        if not r:
            c.rollback()
            raise ValueError('Объявление уже продано или не найдено')
        if int(r['seller_id']) == int(buyer_id):
            c.rollback()
            raise ValueError('Нельзя купить свой куб')
        
        b = c.execute('SELECT balance FROM users WHERE user_id=?', (int(buyer_id),)).fetchone()
        if not b or int(b['balance']) < int(r['price']):
            c.rollback()
            raise ValueError('Недостаточно монет')
        
        c.execute('UPDATE users SET balance=balance-? WHERE user_id=?', (int(r['price']), int(buyer_id)))
        c.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (int(r['price']), int(r['seller_id'])))
        c.execute('UPDATE shop SET sold=1 WHERE id=?', (int(item_id),))
        c.execute('INSERT INTO user_cubes(user_id,name,rarity,value,metadata) VALUES(?,?,?,?,?)',
                 (int(buyer_id), r['cube_name'], r['cube_rarity'], int(r['cube_value']), json.dumps({'bought':True,'price':r['price']})))
        seller_id=int(r['seller_id']); cube_name=str(r['cube_name']); price=int(r['price'])
        c.commit()
        add_notification(seller_id,'Продан куб',f'Ваш куб «{cube_name}» купили в магазине за {price} 💰 орбов')
        return {'balance': int(b['balance'])-price, 'cubes': get_cubes(int(buyer_id))}

def create_battle_record(bid, creator_id, creator_name, stake, max_players, mode):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        uid=int(creator_id); stake=int(stake)
        bal=c.execute('SELECT balance FROM users WHERE user_id=?',(uid,)).fetchone()
        if not bal:
            c.rollback(); raise ValueError('Пользователь не найден')
        if int(bal['balance']) < stake:
            c.rollback(); raise ValueError('Недостаточно монет')
        c.execute('UPDATE users SET balance=balance-? WHERE user_id=?',(stake,uid))
        c.execute('INSERT INTO battles(id,creator_id,creator_name,stake,max_players,mode,status) VALUES(?,?,?,?,?,?,?)',(bid,uid,creator_name,stake,int(max_players),mode,'open'))
        c.execute('INSERT INTO battle_players(battle_id,user_id,username) VALUES(?,?,?)',(bid,uid,creator_name))
        c.commit(); return int(bal['balance'])-stake

PLINKO_MAX_BET = 999_999_999_999

def play_plinko_round(round_id, user_id, bet):
    # Сервер сам выбирает путь и множитель. Клиент не может подменить выигрыш.
    # round_id делает запрос идемпотентным: повтор сетевого запроса не списывает
    # ставку повторно и не начисляет выигрыш повторно.
    import secrets
    multipliers = [5.0, 2.0, 0.7, 0.4, 0.1, 0.4, 2.0, 5.0]
    bet = int(bet)
    if bet < 1 or bet > PLINKO_MAX_BET:
        raise ValueError(f'Ставка должна быть от 1 до {PLINKO_MAX_BET:,}'.replace(',', ' '))
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute(
            'SELECT user_id,bet,multiplier,payout,path FROM plinko_rounds WHERE id=?',
            (round_id,)
        ).fetchone()
        if existing:
            if int(existing['user_id']) != int(user_id) or int(existing['bet']) != int(bet):
                c.rollback()
                raise ValueError('ID игры уже использован')
            fresh = c.execute('SELECT balance FROM users WHERE user_id=?', (int(user_id),)).fetchone()
            c.commit()
            return {
                'round_id': round_id,
                'balance': int(fresh['balance']) if fresh else 0,
                'bet': int(existing['bet']),
                'multiplier': float(existing['multiplier']),
                'payout': int(existing['payout']),
                'path': json.loads(existing['path'])
            }

        u = c.execute('SELECT balance,banned FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not u:
            c.rollback()
            raise ValueError('Пользователь не найден')
        if int(u['banned'] or 0):
            c.rollback()
            raise ValueError('Вы заблокированы')
        if int(u['balance']) < int(bet):
            c.rollback()
            raise ValueError('Недостаточно орбов')

        # 8 шагов дают 9 конечных ячеек. Сохраняем число правых шагов после
        # каждого ряда, чтобы фронтенд мог красиво воспроизвести именно этот путь.
        rights = 0
        path = []
        for _ in range(7):
            if secrets.randbelow(2):
                rights += 1
            path.append(rights)
        multiplier = float(multipliers[rights])
        payout = max(0, int(round(int(bet) * multiplier)))

        debit = c.execute('UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?',
                  (int(bet), int(user_id), int(bet)))
        if debit.rowcount != 1:
            c.rollback()
            raise ValueError('Недостаточно орбов')
        c.execute('UPDATE users SET balance=balance+?, total_wins=total_wins+CASE WHEN ? > 0 THEN 1 ELSE 0 END, biggest_win=MAX(biggest_win,?) WHERE user_id=?',
                  (payout, payout, payout, int(user_id)))
        if payout > bet:
            c.execute('INSERT INTO economy_events(user_id,source,net_profit,wager) VALUES(?,?,?,?)',(int(user_id),'plinko',int(payout-bet),int(bet)))
        c.execute(
            'INSERT INTO plinko_rounds(id,user_id,bet,multiplier,payout,path) VALUES(?,?,?,?,?,?)',
            (round_id, int(user_id), int(bet), multiplier, payout, json.dumps(path, separators=(',',':')))
        )
        fresh = c.execute('SELECT balance FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        c.commit()
        return {
            'round_id': round_id,
            'balance': int(fresh['balance']),
            'bet': int(bet),
            'multiplier': multiplier,
            'payout': payout,
            'path': path
        }

RUSH_MAX_BET = 1_000_000
RUSH_MAX_MULTIPLIER = 2.5


def _rush_multiplier(started_at_ms, now_ms=None):
    now_ms = int(now_ms or (time.time() * 1000))
    elapsed = max(0, now_ms - int(started_at_ms))
    # Очень плавный старт: в первые секунды множитель растёт заметно мягче,
    # без резкого выхода к 1.15x. Кривая остаётся единой с клиентом.
    seconds = elapsed / 1000.0
    return 1.0 + seconds / 11.0 + (seconds ** 1.4) / 65.0


def start_rush_round(round_id, user_id, bet):
    import secrets, time
    bet = int(bet)
    if bet < 1 or bet > RUSH_MAX_BET:
        raise ValueError(f'Ставка должна быть от 1 до {RUSH_MAX_BET:,}'.replace(',', ' '))
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        existing = c.execute('SELECT * FROM rush_rounds WHERE id=?', (round_id,)).fetchone()
        if existing:
            if int(existing['user_id']) != int(user_id) or int(existing['bet']) != bet:
                c.rollback(); raise ValueError('ID игры уже использован')
            c.commit()
            return {'round_id': round_id, 'bet': bet, 'started_at_ms': int(existing['started_at_ms']), 'status': existing['status']}
        recent_count = c.execute(
            "SELECT COUNT(*) AS n FROM rush_rounds WHERE user_id=? AND created_at >= datetime('now','-1 hour')",
            (int(user_id),)
        ).fetchone()
        if int(recent_count['n'] or 0) >= 5:
            c.rollback(); raise ValueError('Лимит игр достигнут: максимум 5 игр в час. Попробуй позже.')
        active = c.execute("SELECT id FROM rush_rounds WHERE user_id=? AND status='active' ORDER BY created_at DESC LIMIT 1", (int(user_id),)).fetchone()
        if active:
            c.rollback(); raise ValueError('У тебя уже идёт раунд — забери выигрыш или дождись конца')
        u = c.execute('SELECT balance,banned FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not u: c.rollback(); raise ValueError('Пользователь не найден')
        if int(u['banned'] or 0): c.rollback(); raise ValueError('Вы заблокированы')
        if int(u['balance']) < bet: c.rollback(); raise ValueError('Недостаточно орбов')
        # Fair random crash point. Stored on server and never sent to the client.
        # Most rounds end early, but rare rounds can run much longer.
        r = secrets.randbelow(10000) / 10000.0
        # Крэш никогда не выше 2.5x. Небольшая часть раундов позволяет
        # забрать ровно 2.5x, остальные срываются раньше.
        bad_luck = has_active_buff(user_id, 'bad_luck')
        if bad_luck:
            # In 80% of bad-luck rounds, crash before the normal profitable range.
            if secrets.randbelow(100) < 80:
                crash = 1000 + secrets.randbelow(151)
            else:
                crash = 1150 + int((r ** 2.7) * 1350)
        else:
            crash = 1150 + int((r ** 2.7) * 1350)
        crash = min(2500, max(1000, crash))
        started = int(time.time() * 1000)
        debit = c.execute('UPDATE users SET balance=balance-? WHERE user_id=? AND balance>=?', (bet,int(user_id),bet))
        if debit.rowcount != 1: c.rollback(); raise ValueError('Недостаточно орбов')
        c.execute('INSERT INTO rush_rounds(id,user_id,bet,crash_milli,started_at_ms,status) VALUES(?,?,?,?,?,\'active\')', (round_id,int(user_id),bet,crash,started))
        fresh = c.execute('SELECT balance FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        c.commit()
        return {'round_id':round_id,'bet':bet,'started_at_ms':started,'status':'active','balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}


def get_rush_status(round_id, user_id):
    import time
    with db() as c:
        r=c.execute('SELECT * FROM rush_rounds WHERE id=? AND user_id=?',(round_id,int(user_id))).fetchone()
        if not r:
            raise ValueError('Раунд не найден')
        fresh=c.execute('SELECT balance FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        if r['status']=='cashed':
            return {'round_id':round_id,'status':'cashed','multiplier':min(RUSH_MAX_MULTIPLIER,float(r['multiplier'] or 1.0)),'payout':int(r['payout'] or 0),'balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}
        if r['status']=='crashed':
            return {'round_id':round_id,'status':'crashed','multiplier':min(RUSH_MAX_MULTIPLIER,float(r['crash_milli'])/1000.0),'payout':0,'balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}
        now=int(time.time()*1000)
        mult=min(RUSH_MAX_MULTIPLIER,_rush_multiplier(r['started_at_ms'],now))
        crash=min(RUSH_MAX_MULTIPLIER,float(r['crash_milli'])/1000.0)
        if mult >= crash:
            # Conditional update makes the terminal transition atomic without
            # holding a write lock during every 100–150 ms status request.
            changed=c.execute("UPDATE rush_rounds SET status='crashed', multiplier=?, finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'",(crash,round_id)).rowcount
            if changed:
                return {'round_id':round_id,'status':'crashed','multiplier':crash,'payout':0,'balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}
            # Another request finished the same round between SELECT and UPDATE.
            rr=c.execute('SELECT status,multiplier,payout FROM rush_rounds WHERE id=? AND user_id=?',(round_id,int(user_id))).fetchone()
            status=rr['status'] if rr else 'crashed'
            return {'round_id':round_id,'status':status,'multiplier':min(RUSH_MAX_MULTIPLIER,float(rr['multiplier'] or crash)),'payout':int(rr['payout'] or 0),'balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}
        return {'round_id':round_id,'status':'active','multiplier':mult,'payout':0,'balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}


def cashout_rush_round(round_id, user_id):
    import time
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r = c.execute('SELECT * FROM rush_rounds WHERE id=? AND user_id=?', (round_id,int(user_id))).fetchone()
        if not r: c.rollback(); raise ValueError('Раунд не найден')
        if r['status'] == 'cashed':
            fresh=c.execute('SELECT balance FROM users WHERE user_id=?',(int(user_id),)).fetchone(); c.commit()
            return {'round_id':round_id,'status':'cashed','multiplier':float(r['multiplier'] or 1.0),'payout':int(r['payout']),'balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}
        if r['status'] == 'crashed':
            fresh=c.execute('SELECT balance FROM users WHERE user_id=?',(int(user_id),)).fetchone(); c.commit()
            return {'round_id':round_id,'status':'crashed','multiplier':float(r['crash_milli'])/1000.0,'payout':0,'balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}
        now=int(time.time()*1000)
        mult=min(RUSH_MAX_MULTIPLIER, _rush_multiplier(r['started_at_ms'], now))
        crash=min(RUSH_MAX_MULTIPLIER, float(r['crash_milli'])/1000.0)
        if mult >= crash:
            c.execute("UPDATE rush_rounds SET status='crashed', multiplier=?, finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'", (crash,round_id))
            fresh=c.execute('SELECT balance FROM users WHERE user_id=?',(int(user_id),)).fetchone(); c.commit()
            return {'round_id':round_id,'status':'crashed','multiplier':crash,'payout':0,'balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}
        payout=max(0,int(int(r['bet']) * mult))
        if payout > int(r['bet']):
            c.execute('INSERT INTO economy_events(user_id,source,net_profit,wager) VALUES(?,?,?,?)',(int(user_id),'rush',int(payout-int(r['bet'])),int(r['bet'])))
        c.execute('UPDATE users SET balance=balance+?, total_wins=total_wins+CASE WHEN ? > 0 THEN 1 ELSE 0 END, biggest_win=MAX(biggest_win,?) WHERE user_id=?',(payout,payout,payout,int(user_id)))
        c.execute("UPDATE rush_rounds SET status='cashed', multiplier=?, payout=?, finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'",(mult,payout,round_id))
        fresh=c.execute('SELECT balance FROM users WHERE user_id=?',(int(user_id),)).fetchone(); c.commit()
        return {'round_id':round_id,'status':'cashed','multiplier':mult,'payout':payout,'balance':int(fresh['balance']),'server_now_ms':int(time.time()*1000)}


def get_battles():
    with db() as c:
        # 'done' включаем ненадолго после завершения — чтобы все участники
        # (не только тот, кто запустил розыгрыш) успели увидеть анимацию броска
        rows = c.execute('''
            SELECT * FROM battles
            WHERE status IN ('open','playing')
               OR (status='done' AND finished_at IS NOT NULL AND finished_at > datetime('now','-25 seconds'))
            ORDER BY created_at DESC
        ''').fetchall()
        out = []
        for r in rows:
            ps = c.execute('SELECT bp.user_id,bp.username,bp.roll,u.avatar FROM battle_players bp LEFT JOIN users u ON u.user_id=bp.user_id WHERE bp.battle_id=? ORDER BY bp.rowid', (r['id'],)).fetchall()
            out.append({
                'id': r['id'],
                'creator': r['creator_id'],
                'creator_name': r['creator_name'],
                'stake': r['stake'],
                'max': r['max_players'],
                'mode': r['mode'],
                'status': r['status'],
                'winner': r['winner_id'],
                'pot': r['pot'],
                'created': r['created_at'],
                'players': [{'user': p['user_id'], 'name': p['username'] or 'Игрок', 'roll': p['roll'], 'avatar': p['avatar']} for p in ps]
            })
        return out

def join_battle_record(bid, user_id, username):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        b = c.execute('SELECT stake,max_players,status FROM battles WHERE id=?', (bid,)).fetchone()
        if not b or b['status'] != 'open':
            c.rollback()
            raise ValueError('Батл не найден или уже начался')
        
        n = c.execute('SELECT COUNT(*) n FROM battle_players WHERE battle_id=?', (bid,)).fetchone()['n']
        if n >= b['max_players']:
            c.rollback()
            raise ValueError('Батл заполнен')
        
        if c.execute('SELECT 1 FROM battle_players WHERE battle_id=? AND user_id=?', (bid, int(user_id))).fetchone():
            c.rollback()
            raise ValueError('Ты уже в этом батле')
        
        u = c.execute('SELECT balance FROM users WHERE user_id=?', (int(user_id),)).fetchone()
        if not u or int(u['balance']) < int(b['stake']):
            c.rollback()
            raise ValueError('Недостаточно монет')
        
        c.execute('UPDATE users SET balance=balance-? WHERE user_id=?', (int(b['stake']), int(user_id)))
        c.execute('INSERT INTO battle_players(battle_id,user_id,username) VALUES(?,?,?)', (bid, int(user_id), username))
        n += 1
        became_full = n >= b['max_players']
        if became_full:
            c.execute("UPDATE battles SET status='playing' WHERE id=?", (bid,))
        c.commit()
        return became_full

def cancel_battle_record(bid, user_id):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        b = c.execute('SELECT creator_id,stake,status FROM battles WHERE id=?', (bid,)).fetchone()
        if not b:
            c.rollback()
            raise ValueError('Батл не найден')
        if int(b['creator_id']) != int(user_id):
            c.rollback()
            raise ValueError('Только создатель может отменить батл')
        if b['status'] != 'open':
            c.rollback()
            raise ValueError('Нельзя отменить уже начавшийся батл')
        
        players = c.execute('SELECT user_id FROM battle_players WHERE battle_id=?', (bid,)).fetchall()
        for p in players:
            c.execute('UPDATE users SET balance=balance+? WHERE user_id=?', (int(b['stake']), int(p['user_id'])))
        c.execute("UPDATE battles SET status='cancelled' WHERE id=?", (bid,))
        c.commit()


def _normalize_promo_slides(slides):
    out=[]
    for item in list(slides or []):
        if not isinstance(item, dict):
            continue
        image=str(item.get('image') or item.get('src') or '').strip()
        if not image or not image.startswith(('data:image/','https://','http://')):
            continue
        reward_type=str(item.get('reward_type') or item.get('rewardType') or 'none').lower()
        if reward_type not in ('none','coins','vip','cube','buff'):
            reward_type='none'
        out.append({
            'image': image[:8_000_000],
            'duration': 3,
            'reward_type': reward_type,
            'reward_value': max(0, int(item.get('reward_value', item.get('rewardValue', 0)) or 0))
        })
    return out


def create_promo(code, reward_type, coins, vip_days, max_uses, created_by, fullscreen_slides=None):
    code=str(code or '').strip().upper(); reward_type=str(reward_type or 'coins').lower()
    coins=max(0,int(coins or 0)); vip_days=max(0,int(vip_days or 0)); max_uses=max(1,int(max_uses or 1))
    slides=_normalize_promo_slides(fullscreen_slides)
    if not re.fullmatch(r'[A-Z0-9_-]{3,32}',code): raise ValueError('Промокод: 3–32 символа, только A-Z, 0-9, _ и -')
    if reward_type not in ('coins','vip','both'): raise ValueError('Неверный тип награды')
    if reward_type in ('coins','both') and coins<=0: raise ValueError('Укажи количество монет')
    if reward_type in ('vip','both') and vip_days<=0: raise ValueError('Укажи количество дней VIP')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            c.execute(
                'INSERT INTO promo_codes(code,reward_type,coins,vip_days,max_uses,fullscreen_slides,created_by) VALUES(?,?,?,?,?,?,?)',
                (code,reward_type,coins,vip_days,max_uses,json.dumps(slides,ensure_ascii=False),int(created_by))
            )
        except sqlite3.IntegrityError:
            c.rollback(); raise ValueError('Такой промокод уже существует')
        c.commit(); return get_promo(code)

def get_promo(code):
    with db() as c:
        r=c.execute('SELECT * FROM promo_codes WHERE code=?',(str(code or '').strip().upper(),)).fetchone()
        if not r: return None
        item=dict(r)
        try: item['fullscreen_slides']=json.loads(item.get('fullscreen_slides') or '[]')
        except Exception: item['fullscreen_slides']=[]
        return item

def list_promos():
    with db() as c:
        rows=c.execute('SELECT p.*,u.username creator_username FROM promo_codes p LEFT JOIN users u ON u.user_id=p.created_by ORDER BY p.id DESC').fetchall()
        out=[]
        for r in rows:
            item=dict(r)
            try: item['fullscreen_slides']=json.loads(item.get('fullscreen_slides') or '[]')
            except Exception: item['fullscreen_slides']=[]
            out.append(item)
        return out

def disable_promo(promo_id):
    with db() as c:
        c.execute('UPDATE promo_codes SET active=0 WHERE id=?',(int(promo_id),)); c.commit(); return c.rowcount>0

def redeem_promo(code,user_id):
    code=str(code or '').strip().upper()
    if not code: raise ValueError('Введите промокод')
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        p=c.execute('SELECT * FROM promo_codes WHERE code=?',(code,)).fetchone()
        if not p or not int(p['active']): c.rollback(); raise ValueError('Промокод не найден или отключён')
        u=c.execute('SELECT username,balance,premium_until FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        if not u: c.rollback(); raise ValueError('Пользователь не найден')
        creator_unlimited = code == '2026SAMERGERMANCRUT' and str(u['username'] or '').strip().lower() == 'kitzkuro'
        if not creator_unlimited:
            if int(p['uses_count'])>=int(p['max_uses']): c.rollback(); raise ValueError('Лимит использований промокода исчерпан')
            if c.execute('SELECT 1 FROM promo_uses WHERE promo_id=? AND user_id=?',(int(p['id']),int(user_id))).fetchone(): c.rollback(); raise ValueError('Ты уже использовал этот промокод')
        coins=int(p['coins']); vip_days=int(p['vip_days'])
        if coins:
            c.execute('UPDATE users SET balance=balance+? WHERE user_id=?',(coins,int(user_id)))
            c.execute('INSERT INTO economy_events(user_id,source,net_profit,wager) VALUES(?,?,?,0)',(int(user_id),'promo',coins))
        premium_until=u['premium_until']
        if vip_days:
            now=datetime.datetime.now(datetime.timezone.utc); base=now
            if premium_until:
                try:
                    old=datetime.datetime.fromisoformat(str(premium_until).replace('Z','+00:00'))
                    if old.tzinfo is None: old=old.replace(tzinfo=datetime.timezone.utc)
                    if old>now: base=old
                except Exception: pass
            premium_until=(base+datetime.timedelta(days=vip_days)).isoformat()
            c.execute('UPDATE users SET premium_until=? WHERE user_id=?',(premium_until,int(user_id)))
        if not creator_unlimited:
            c.execute('INSERT INTO promo_uses(promo_id,user_id) VALUES(?,?)',(int(p['id']),int(user_id)))
            c.execute('UPDATE promo_codes SET uses_count=uses_count+1 WHERE id=?',(int(p['id']),))
        try: slides=json.loads(p['fullscreen_slides'] or '[]')
        except Exception: slides=[]

        # Награда, привязанная к каждой картинке, выдаётся один раз вместе
        # с активацией промокода. Сама картинка содержит только данные для UI.
        slide_rewards=[]
        now=datetime.datetime.now(datetime.timezone.utc)
        current_premium=premium_until
        for slide in slides if isinstance(slides,list) else []:
            if not isinstance(slide,dict):
                continue
            rtype=str(slide.get('reward_type') or 'none').lower()
            value=max(0,int(slide.get('reward_value') or 0))
            if value<=0 or rtype=='none':
                continue
            if rtype=='coins':
                c.execute('UPDATE users SET balance=balance+? WHERE user_id=?',(value,int(user_id)))
                c.execute('INSERT INTO economy_events(user_id,source,net_profit,wager) VALUES(?,?,?,0)',(int(user_id),'promo_slide',value))
                slide_rewards.append({'type':'coins','value':value})
            elif rtype=='vip':
                base=now
                if current_premium:
                    try:
                        old=datetime.datetime.fromisoformat(str(current_premium).replace('Z','+00:00'))
                        if old.tzinfo is None: old=old.replace(tzinfo=datetime.timezone.utc)
                        if old>now: base=old
                    except Exception: pass
                current_premium=(base+datetime.timedelta(days=value)).isoformat()
                c.execute('UPDATE users SET premium_until=? WHERE user_id=?',(current_premium,int(user_id)))
                slide_rewards.append({'type':'vip','value':value})
            elif rtype=='cube':
                c.execute('INSERT INTO user_cubes(user_id,name,rarity,value,metadata) VALUES(?,?,?,?,?)',
                          (int(user_id),'Награда промокода','rare',value,'{}'))
                slide_rewards.append({'type':'cube','value':value})
            elif rtype=='buff':
                try:
                    current_buffs=json.loads((c.execute('SELECT buffs FROM users WHERE user_id=?',(int(user_id),)).fetchone()['buffs'] or '[]'))
                except Exception:
                    current_buffs=[]
                hours=max(1,value)
                expires=(now+datetime.timedelta(hours=hours)).isoformat()
                current_buffs=[b for b in current_buffs if isinstance(b,dict) and (not b.get('expires_at') or str(b.get('expires_at'))>now.isoformat())]
                current_buffs.append({'type':'promo','name':'Подарок промокода','description':'Выдан за картинку промокода','expires_at':expires})
                c.execute('UPDATE users SET buffs=? WHERE user_id=?',(json.dumps(current_buffs,ensure_ascii=False),int(user_id)))
                slide_rewards.append({'type':'buff','value':hours})

        if current_premium != premium_until:
            c.execute('UPDATE users SET premium_until=? WHERE user_id=?',(current_premium,int(user_id)))
        c.commit()
        fresh=c.execute('SELECT balance,premium_until FROM users WHERE user_id=?',(int(user_id),)).fetchone()
        return {'code':code,'reward_type':str(p['reward_type'] or ''),'coins':coins,'vip_days':vip_days,'balance':int(fresh['balance']),'premium_until':fresh['premium_until'],'fullscreen_slides':slides,'slide_rewards':slide_rewards}

def play_battle_record(bid, user_id):
    import random
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        b = c.execute('SELECT * FROM battles WHERE id=?', (bid,)).fetchone()
        if not b or b['status'] != 'playing':
            c.rollback()
            raise ValueError('Батл ещё не заполнен')
        
        ps = c.execute('SELECT bp.user_id,bp.username,bp.roll,u.avatar FROM battle_players bp LEFT JOIN users u ON u.user_id=bp.user_id WHERE bp.battle_id=?', (bid,)).fetchall()
        rolls = []
        for p in ps:
            roll = p['roll'] if p['roll'] is not None else random.randint(1,6)
            c.execute('UPDATE battle_players SET roll=? WHERE battle_id=? AND user_id=?', (roll, bid, p['user_id']))
            rolls.append((p['user_id'], p['username'] or 'Игрок', roll, p['avatar']))
        
        while len(rolls) > 1 and len({x[2] for x in rolls}) == 1:
            rolls = [(x[0], x[1], random.randint(1,6), x[3]) for x in rolls]
        if b['mode'] == 'random':
            winner = random.choice(rolls)
        else:
            max_roll = max(x[2] for x in rolls)
            winners = [x for x in rolls if x[2] == max_roll]
            winner = random.choice(winners)
        
        pot = int(b['stake']) * len(rolls)
        winner_stake=int(b['stake'])
        if pot > winner_stake:
            c.execute('INSERT INTO economy_events(user_id,source,net_profit,wager) VALUES(?,?,?,?)',(int(winner[0]),'battle',int(pot-winner_stake),winner_stake))
        c.execute('UPDATE users SET balance=balance+?,total_wins=total_wins+1,battle_winnings=battle_winnings+?,battles_played=battles_played+1,biggest_win=MAX(biggest_win,?) WHERE user_id=?',
                 (pot, pot, pot, winner[0]))
        for participant in rolls:
            if int(participant[0]) != int(winner[0]):
                c.execute('UPDATE users SET battles_played=battles_played+1 WHERE user_id=?', (int(participant[0]),))
        c.execute("UPDATE battles SET status='done',winner_id=?,pot=?,finished_at=CURRENT_TIMESTAMP WHERE id=?", (winner[0], pot, bid))
        c.commit()
        return {
            'winner_id': winner[0],
            'winner_name': winner[1],
            'rolls': [{'user': x[0], 'name': x[1], 'roll': x[2], 'avatar': x[3]} for x in rolls],
            'pot': pot
        }


def claim_channel_bonus(user_id, amount=1000):
    user_id=int(user_id); amount=max(0,int(amount))
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        already=c.execute('SELECT 1 FROM channel_bonus_claims WHERE user_id=?',(user_id,)).fetchone()
        if already:
            c.rollback()
            return {'claimed':False,'already':True,'balance':int(c.execute('SELECT balance FROM users WHERE user_id=?',(user_id,)).fetchone()['balance'])}
        user=c.execute('SELECT balance,banned FROM users WHERE user_id=?',(user_id,)).fetchone()
        if not user:
            c.rollback(); raise ValueError('Пользователь не найден')
        if int(user['banned'] or 0):
            c.rollback(); raise PermissionError('Аккаунт заблокирован')
        c.execute('UPDATE users SET balance=balance+? WHERE user_id=?',(amount,user_id))
        c.execute('INSERT INTO channel_bonus_claims(user_id) VALUES(?)',(user_id,))
        new_balance=int(c.execute('SELECT balance FROM users WHERE user_id=?',(user_id,)).fetchone()['balance'])
        c.commit()
        return {'claimed':True,'already':False,'balance':new_balance}

def add_notification(user_id,title,text,ntype='important'):
    with db() as c:
        c.execute('INSERT INTO notifications(user_id,title,text,type) VALUES(?,?,?,?)',(int(user_id),str(title),str(text),str(ntype)))

def get_notifications(user_id,limit=30):
    with db() as c:
        return [dict(x) for x in c.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ?', (int(user_id),int(limit))).fetchall()]

def mark_notifications_read(user_id):
    with db() as c:
        c.execute('UPDATE notifications SET read=1 WHERE user_id=?',(int(user_id),))

def get_unread_notification_count(user_id):
    with db() as c:
        r=c.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id=? AND read=0 AND type='important'",(int(user_id),)).fetchone()
        return int(r['n'] or 0)
