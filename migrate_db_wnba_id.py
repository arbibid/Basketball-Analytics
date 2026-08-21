import sqlite3
import os
import sys

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from config import Config

def migrate():
    db_path = Config.DB_PATH
    print(f"Migrating database at {db_path}...")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    tables_to_migrate = ['odds_history', 'virtual_bets', 'bet_signals']

    for table in tables_to_migrate:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN wnba_id INTEGER")
            print(f"✅ Added wnba_id column to {table}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                print(f"⚠️ Column wnba_id already exists in {table}")
            else:
                print(f"❌ Error adding wnba_id to {table}: {e}")

    conn.commit()
    conn.close()
    print("Migration finished.")

if __name__ == "__main__":
    migrate()