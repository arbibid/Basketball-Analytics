# Version: 6.7
import sqlite3
import os
import datetime
import time
import logging
from config import Config

logger = logging.getLogger(__name__)


class DBManager:
    def __init__(self, db_path=Config.DB_PATH):
        self.db_path = db_path

    def get_connection(self):
        max_retries = 5
        for i in range(max_retries):
            try:
                conn = sqlite3.connect(self.db_path, timeout=60.0)
                return conn
            except sqlite3.OperationalError as e:
                if 'database is locked' in str(e) and i < max_retries - 1:
                    time.sleep(1)
                    continue
                raise e
        return sqlite3.connect(self.db_path, timeout=60.0)

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()

        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA synchronous=NORMAL;')

        # Существующие таблицы
        cursor.execute('''CREATE TABLE IF NOT EXISTS matches (
            game_id TEXT PRIMARY KEY, date TEXT, away_team TEXT, home_team TEXT,
            away_score INTEGER, home_score INTEGER, referee_1 TEXT, referee_2 TEXT,
            referee_3 TEXT, attendance TEXT, duration TEXT
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS player_stats (
            game_id TEXT, team_abbr TEXT, player_name TEXT, position TEXT, minutes TEXT,
            fgm INTEGER, fga INTEGER, fg_pct REAL, fg3m INTEGER, fg3a INTEGER, fg3_pct REAL,
            ftm INTEGER, fta INTEGER, ft_pct REAL, oreb INTEGER, dreb INTEGER, reb INTEGER,
            ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER, pts INTEGER, plus_minus REAL,
            PRIMARY KEY (game_id, player_name)
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS odds_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            event_id TEXT, market_type TEXT, player_or_team TEXT, line REAL,
            over_kf REAL, under_kf REAL, factor_id INTEGER, bookmaker TEXT DEFAULT 'FONBET',
            parent_event_id TEXT, wnba_id INTEGER
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TIMESTAMP,
            subscription_end TIMESTAMP, is_vip BOOLEAN DEFAULT 0, tier TEXT DEFAULT 'free',
            has_used_trial BOOLEAN DEFAULT 0
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS invoices (
            invoice_id TEXT PRIMARY KEY, user_id INTEGER, amount REAL, currency TEXT,
            status TEXT, created_at TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS match_tracking (
            match_id TEXT PRIMARY KEY, match_date TEXT, team1 TEXT, team2 TEXT,
            status TEXT DEFAULT 'NEW', proj_score1 REAL, proj_score2 REAL, math_log TEXT,
            bookmaker TEXT DEFAULT 'FONBET', market_type TEXT
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS team_elo (
            team_abbr TEXT PRIMARY KEY, elo_rating REAL, last_updated TEXT
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY, value TEXT
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS virtual_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, match_name TEXT, market TEXT,
            player_name TEXT, line REAL, prediction REAL, selection TEXT, kf REAL,
            bet_amount REAL, status TEXT DEFAULT 'PENDING', category TEXT, vip_kf TEXT,
            published_at TEXT, is_preliminary TEXT DEFAULT '0', coupon_id TEXT,
            actual_result REAL, profit REAL, bookmaker TEXT DEFAULT 'FONBET', market_type TEXT,
            wnba_id INTEGER
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS actual_lineups (
            match_id TEXT, team TEXT, player_name TEXT, status TEXT, source TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (match_id, team, player_name)
        )''')

        # ====================================================================
        # 🔥 НОВЫЕ ТАБЛИЦЫ ДЛЯ РОУТЕРА И РЕАЛЬНЫХ СТАВОК (От Джулис)
        # ====================================================================

        # Очередь сигналов от Предиктора для Роутера
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bet_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_name TEXT,
                market_type TEXT,
                target TEXT,
                line REAL,
                expected_kf REAL,
                edge REAL,
                status TEXT DEFAULT 'READY', -- READY, PROCESSED, REJECTED
                bookmaker TEXT DEFAULT 'FONBET',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                wnba_id INTEGER
            )
        ''')
        
        try:
            cursor.execute("ALTER TABLE bet_signals ADD COLUMN bookmaker TEXT DEFAULT 'FONBET'")
        except sqlite3.OperationalError:
            pass

        # Хранилище реальных купонов из БК
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT UNIQUE,
                bookmaker TEXT,
                item_name TEXT,
                price REAL,
                amount REAL,
                status TEXT DEFAULT 'OPEN',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Заполняем дефолтные настройки, если их нет
        defaults = [('standard_price', '100'), ('pro_price', '200'), ('base_bet_amount', '50'),
                    ('game_edge_threshold', '2.0'), ('player_edge_threshold', '0.05'), ('win_edge_threshold', '0.015')]
        for k, v in defaults:
            cursor.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES (?, ?)", (k, v))

        conn.commit()
        conn.close()

    # ====================================================================
    # 🔥 НОВЫЕ МЕТОДЫ ДЛЯ РОУТЕРА (Интеграция логики Джулис)
    # ====================================================================

    def add_bet_signal(self, match_name, market_type, target, line, expected_kf, edge, bookmaker='FONBET', wnba_id=None):
        conn = self.get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge, bookmaker, wnba_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (match_name, market_type, target, line, expected_kf, edge, bookmaker, wnba_id))
            conn.commit()
            return c.lastrowid
        finally:
            conn.close()

    def save_odds(self, event_id, factor_id, market_type, player_or_team, line, over_kf, under_kf, parent_event_id, wnba_id=None, bookmaker='FONBET'):
        conn = self.get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                INSERT INTO odds_history (event_id, factor_id, market_type, player_or_team, line, over_kf, under_kf, parent_event_id, wnba_id, bookmaker)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (event_id, factor_id, market_type, player_or_team, line, over_kf, under_kf, parent_event_id, wnba_id, bookmaker))
            conn.commit()
        finally:
            conn.close()

    def get_ready_signals(self):
        conn = self.get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT * FROM bet_signals WHERE status = 'READY'")
            return c.fetchall()
        finally:
            conn.close()

    def update_signal_status(self, signal_id, new_status):
        conn = self.get_connection()
        try:
            c = conn.cursor()
            c.execute("UPDATE bet_signals SET status = ? WHERE id = ?", (new_status, signal_id))
            conn.commit()
        finally:
            conn.close()

    def save_real_order(self, order_id, bookmaker, item_name, price, amount):
        conn = self.get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                INSERT INTO active_orders (order_id, bookmaker, item_name, price, amount)
                VALUES (?, ?, ?, ?, ?)
            ''', (str(order_id), bookmaker, item_name, float(price), float(amount)))
            conn.commit()
            logger.info(f"✅ Ордер {order_id} ({bookmaker}) успешно сохранен в БД.")
        except sqlite3.IntegrityError:
            logger.error(f"⚠️ Ордер {order_id} уже существует в базе!")
        finally:
            conn.close()

    def update_real_order_status(self, order_id, new_status):
        conn = self.get_connection()
        try:
            c = conn.cursor()
            c.execute("UPDATE active_orders SET status = ? WHERE order_id = ?", (new_status, str(order_id)))
            conn.commit()
        finally:
            conn.close()

    # ====================================================================
    # СТАРЫЕ МЕТОДЫ (Без изменений)
    # ====================================================================

    def get_system_setting(self, key, default_value=None):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM system_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else default_value
        finally:
            conn.close()

    def set_system_setting(self, key, value):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
        finally:
            conn.close()

    def get_all_system_settings(self):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM system_settings")
            return cursor.fetchall()
        finally:
            conn.close()

    def update_invoice_status(self, invoice_id, status):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('UPDATE invoices SET status = ? WHERE invoice_id = ?', (status, invoice_id))
            conn.commit()
        finally:
            conn.close()

    def get_vip_settings(self):
        price = float(self.get_system_setting("pro_price", 200.0))
        days = int(self.get_system_setting("pro_days", 7))
        return price, days

    def get_standard_settings(self):
        price = float(self.get_system_setting("standard_price", 100.0))
        days = int(self.get_system_setting("standard_days", 7))
        return price, days

    def update_vip_settings(self, new_price, new_days, tier='pro'):
        prefix = tier.lower()
        self.set_system_setting(f"{prefix}_price", str(new_price))
        self.set_system_setting(f"{prefix}_days", str(new_days))

    def add_user(self, user_id, username, first_name):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = datetime.datetime.now()
            trial_end = now + datetime.timedelta(minutes=Config.TRIAL_PERIOD_MINUTES)
            cursor.execute('''INSERT OR IGNORE INTO users
                              (user_id, username, first_name, joined_at, subscription_end, is_vip, tier)
                              VALUES (?, ?, ?, ?, ?, 0, 'free')''',
                           (user_id, username, first_name, now, trial_end))
            conn.commit()
        finally:
            conn.close()

    def check_subscription(self, user_id):
        if user_id == Config.ADMIN_ID: return True
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT subscription_end FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            if result and result[0]:
                try:
                    sub_end = datetime.datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S.%f')
                except ValueError:
                    sub_end = datetime.datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
                return datetime.datetime.now() <= sub_end
            return False
        finally:
            conn.close()

    def get_all_users(self):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, username, first_name, joined_at, subscription_end, is_vip, tier FROM users')
            return cursor.fetchall()
        finally:
            conn.close()

    def revoke_vip(self, user_id: int):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = datetime.datetime.now()
            cursor.execute('''UPDATE users SET is_vip = 0, subscription_end = ? WHERE user_id = ?''', (now, user_id))
            updated = cursor.rowcount > 0
            conn.commit()
            return updated
        finally:
            conn.close()

    def grant_vip_days(self, user_id: int, days: int):
        return self.grant_subscription(user_id, days, is_vip=1)

    def grant_subscription(self, user_id: int, days: int, is_vip: int = 1):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = datetime.datetime.now()
            cursor.execute('SELECT subscription_end FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            if result and result[0]:
                try:
                    sub_end = datetime.datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S.%f')
                except ValueError:
                    sub_end = datetime.datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
                new_sub_end = sub_end + datetime.timedelta(days=days) if sub_end > now else now + datetime.timedelta(
                    days=days)
            else:
                new_sub_end = now + datetime.timedelta(days=days)
            cursor.execute('''UPDATE users SET is_vip = ?, subscription_end = ? WHERE user_id = ?''',
                           (is_vip, new_sub_end, user_id))
            updated = cursor.rowcount > 0
            conn.commit()
            return updated
        finally:
            conn.close()

    def create_invoice(self, invoice_id, user_id, amount, currency='RUB'):
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            now = datetime.datetime.now()
            cursor.execute('''INSERT INTO invoices (invoice_id, user_id, amount, currency, status, created_at)
                              VALUES (?, ?, ?, ?, 'PENDING', ?)''', (invoice_id, user_id, amount, currency, now))
            conn.commit()
        finally:
            conn.close()