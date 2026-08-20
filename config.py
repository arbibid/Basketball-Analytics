# Version: 6.3 (Stable + Gemini Integration)
import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    PROXY_URL = os.getenv("PROXY_URL", None)
    TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "123456789:AA_fake_token_for_ci_tests")
    TG_YOOKASSA_TOKEN = os.getenv("TG_YOOKASSA_TOKEN")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

    # Игнорируем ошибку конвертации, если ADMIN_ID не задан
    try:
        ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
    except ValueError:
        ADMIN_ID = 0
        
    BETTING_PHONE = os.getenv("BETTING_PHONE", "+70000000000")
    BETTING_PASSWORD = os.getenv("BETTING_PASSWORD", "password123")

    TRIAL_PERIOD_MINUTES = 5

    # Database Settings
    DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), os.getenv("DB_PATH", "wnba_bot.db")))

    # API URLs
    NBA_STATS_BASE_URL = "https://stats.nba.com/stats/"
    FONBET_API_URL = os.getenv("FONBET_API_URL", "https://line-lb61-w.bk6bba-resources.com/ma")
    ROTOWIRE_NEWS_URL = os.getenv("ROTOWIRE_NEWS_URL", "https://www.rotowire.com/wnba/news.php")
    ROTOWIRE_INJURY_URL = os.getenv("ROTOWIRE_INJURY_URL", "https://www.rotowire.com/wnba/tables/injury-report.php?team=ALL&pos=ALL")
    NBA_REFEREE_URL = os.getenv("NBA_REFEREE_URL", "https://official.nba.com/referee-assignments/")
    NBA_PDF_BASE_URL = os.getenv("NBA_PDF_BASE_URL", "https://statsdmz.nba.com/pdfs/")
    WNBA_SCHEDULE_URL = os.getenv("WNBA_SCHEDULE_URL", "https://www.wnba.com/schedule?season=2026&month=all")

    # NBA Stats Headers
    NBA_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nba.com",
        "Referer": "https://www.nba.com/",
        "Connection": "keep-alive",
        "x-nba-stats-origin": "stats",
        "x-nba-stats-token": "true"
    }

    # Math Core Constants
    HOME_COURT_ADVANTAGE = float(os.getenv("HOME_COURT_ADVANTAGE", 2.5))

    @staticmethod
    def get_market_ids():
        import json
        market_ids_path = os.path.join(os.path.dirname(__file__), 'config', 'market_ids.json')
        if os.path.exists(market_ids_path):
            with open(market_ids_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    @staticmethod
    def get_mappings():
        import json
        import re
        mapping_path = os.path.join(os.path.dirname(__file__), 'config', 'mappings.json')
        if os.path.exists(mapping_path):
            with open(mapping_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Нормализуем ключи: убираем все пробелы
                if "PLAYER_MAP" in data:
                    new_player_map = {}
                    for k, v in data["PLAYER_MAP"].items():
                        new_player_map[re.sub(r'\s+', '', k)] = v
                        # Keep original as well in case some other components rely on it
                        new_player_map[k] = v
                    data["PLAYER_MAP"] = new_player_map
                return data
        return {}