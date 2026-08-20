import sqlite3
import datetime
import os
import logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "wnba_bot.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def archive_old_records():
    logging.info(f"Начинаем архивацию старых записей БД: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()

        # Создаем архивные таблицы, если их нет
        c.execute('''CREATE TABLE IF NOT EXISTS virtual_bets_archive (
            id INTEGER PRIMARY KEY,
            date TEXT,
            match_name TEXT,
            market TEXT,
            player_name TEXT,
            line REAL,
            selection TEXT,
            kf REAL,
            target_kf REAL,
            bet_amount REAL,
            published_at TEXT,
            status TEXT,
            prelim_flag INTEGER,
            coupon_id TEXT,
            actual_result REAL,
            profit REAL,
            archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS odds_history_archive (
            id INTEGER PRIMARY KEY,
            event_id TEXT,
            market_type TEXT,
            player_or_team TEXT,
            line REAL,
            over_kf REAL,
            under_kf REAL,
            factor_id INTEGER,
            bookmaker TEXT,
            timestamp DATETIME,
            archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''')

        # Рассчитываем дату (например, 2 недели назад)
        cutoff_date = (datetime.datetime.now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")

        logging.info(f"Удаляем и переносим данные старше {cutoff_date}...")

        # 1. Перенос virtual_bets
        # Мы переносим только ставки, которые уже рассчитаны (SETTLED, WON, LOST, REFUND)
        # или просто все старые (если матч так и не был завершен).
        c.execute('''
            INSERT INTO virtual_bets_archive
            (id, date, match_name, market, player_name, line, selection, kf, target_kf, bet_amount, published_at, status, prelim_flag, coupon_id, actual_result, profit)
            SELECT id, date, match_name, market, player_name, line, selection, kf, target_kf, bet_amount, published_at, status, prelim_flag, coupon_id, actual_result, profit
            FROM virtual_bets
            WHERE date < ? AND status IN ('WON', 'LOST', 'REFUND')
        ''', (cutoff_date,))
        archived_bets = c.rowcount

        c.execute("DELETE FROM virtual_bets WHERE date < ? AND status IN ('WON', 'LOST', 'REFUND')", (cutoff_date,))
        deleted_bets = c.rowcount

        # 2. Перенос odds_history (Привязываемся к match_tracking, чтобы узнать дату)
        # Поскольку в odds_history нет прямо даты, мы можем удалять те, у которых event_id относится к старым матчам.
        c.execute('''
            INSERT INTO odds_history_archive (id, event_id, market_type, player_or_team, line, over_kf, under_kf, factor_id, bookmaker, timestamp)
            SELECT o.id, o.event_id, o.market_type, o.player_or_team, o.line, o.over_kf, o.under_kf, o.factor_id, o.bookmaker, o.timestamp
            FROM odds_history o
            JOIN match_tracking m ON o.event_id = m.match_id
            WHERE m.match_date < ?
        ''', (cutoff_date,))
        archived_odds = c.rowcount

        c.execute('''
            DELETE FROM odds_history
            WHERE event_id IN (SELECT match_id FROM match_tracking WHERE match_date < ?)
        ''', (cutoff_date,))
        deleted_odds = c.rowcount

        conn.commit()

        logging.info(f"✅ Архивация завершена. Перенесено/удалено ставок: {archived_bets}/{deleted_bets}. Котировок: {archived_odds}/{deleted_odds}.")

        # Vacuum database to reclaim space
        logging.info("Очистка неиспользуемого места (VACUUM)...")
        c.execute("VACUUM")
        logging.info("VACUUM завершен.")

    except Exception as e:
        logging.error(f"Ошибка при архивации: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    archive_old_records()
