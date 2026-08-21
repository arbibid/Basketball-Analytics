import sqlite3
import os

# Подключаемся к НАСТОЯЩЕЙ базе
db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'wnba_bot.db'))
conn = sqlite3.connect(db_path)
c = conn.cursor()

print("--- Последние 20 записей от BETCITY (ПРОПЫ ИГРОКОВ) ---")
try:
    c.execute("""
        SELECT timestamp, market_type, player_or_team, line 
        FROM odds_history 
        WHERE bookmaker='BETCITY' AND market_type LIKE 'PLAYER_%'
        ORDER BY timestamp DESC 
        LIMIT 20
    """)
    rows = c.fetchall()
    if not rows:
        print("ПУСТО! Похоже, Бетсити не использует префикс PLAYER_")
    for row in rows:
        print(row)

    print("\n--- Последние 20 записей от BETCITY (ВООБЩЕ ВСЕ) ---")
    c.execute("""
        SELECT timestamp, market_type, player_or_team, line 
        FROM odds_history 
        WHERE bookmaker='BETCITY' 
        ORDER BY timestamp DESC 
        LIMIT 20
    """)
    for row in c.fetchall():
        print(row)
except Exception as e:
    print(f"Ошибка БД: {e}")

conn.close()