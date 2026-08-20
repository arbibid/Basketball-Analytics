# Version: 6.3 (Unified Database Fix + Proxy/Timeout Update)
import requests
from bs4 import BeautifulSoup
import sqlite3
import logging
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config import Config
from database.db_manager import DBManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('InjuryScraper')


def fetch_and_update_injuries():
    """
    Fetches WNBA injuries from RotoWire using requests, parses with BeautifulSoup,
    and updates the player_injuries table using the central DBManager.
    """
    url = getattr(Config, 'ROTOWIRE_INJURY_URL',
                  "https://www.rotowire.com/wnba/tables/injury-report.php?team=ALL&pos=ALL")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    proxy_url = getattr(Config, 'PROXY_URL', None)
    proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None

    logger.info(f"Fetching injury data from {url} (Proxy: {proxy_url})")
    try:
        response = requests.get(url, headers=headers, proxies=proxies, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"Failed to fetch injury data: {e}")
        return

    injuries_to_insert = []

    for item in data:
        player_name = item.get("player", "").strip()
        team = item.get("team", "").strip()
        status = item.get("status", "").strip()

        # Some fields might contain HTML fragments
        injury_raw = item.get("injury", "")
        soup = BeautifulSoup(injury_raw, "html.parser")
        description = soup.get_text(strip=True)

        if player_name:
            injuries_to_insert.append((player_name, team, status, description))

    if not injuries_to_insert:
        logger.warning("No injury data parsed.")
        return

    logger.info(f"Parsed {len(injuries_to_insert)} injury records. Updating database...")

    # Используем центральный DBManager для подключения к единой базе
    db = DBManager()

    try:
        conn = db.get_connection()
        cursor = conn.cursor()

        # ЖЕЛЕЗОБЕТОННО СОЗДАЕМ ТАБЛИЦУ В ПРАВИЛЬНОЙ БАЗЕ
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS player_injuries (
                player_name TEXT PRIMARY KEY,
                team TEXT,
                status TEXT,
                description TEXT
            )
        ''')

        # Clear old data
        cursor.execute('DELETE FROM player_injuries')

        # Insert fresh data
        cursor.executemany('''
            INSERT INTO player_injuries (player_name, team, status, description)
            VALUES (?, ?, ?, ?)
        ''', injuries_to_insert)

        conn.commit()
        conn.close()
        logger.info("Successfully updated 'player_injuries' table.")
    except Exception as e:
        logger.error(f"Database error during update: {e}")


if __name__ == "__main__":
    fetch_and_update_injuries()