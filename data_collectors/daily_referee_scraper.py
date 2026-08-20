# Version: 6.1
import sqlite3
import logging
import asyncio
import re
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import os
import datetime
import sys

# Добавляем корневую директорию в sys.path, чтобы импортировать config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import Config

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

DB_PATH = Config.DB_PATH

mappings = Config.get_mappings()
REFEREE_MAP = mappings.get("REFEREE_MAP", {})
TEAM_MAP_REVERSE = mappings.get("TEAM_MAP_REVERSE", {})
NBA_TEAM_MAP = mappings.get("NBA_TEAM_MAP", {})

def get_playwright_proxy():
    """
    Парсит Config.PROXY_URL и возвращает словарь настроек для Playwright.
    Поддерживает форматы: http://ip:port и http://user:pass@ip:port
    """
    if not Config.PROXY_URL:
        return None

    proxy_str = Config.PROXY_URL.replace("http://", "").replace("https://", "")
    proxy_config = {}

    if "@" in proxy_str:
        auth_part, server_part = proxy_str.split("@")
        username, password = auth_part.split(":")
        proxy_config["server"] = f"http://{server_part}"
        proxy_config["username"] = username
        proxy_config["password"] = password
    else:
        proxy_config["server"] = f"http://{proxy_str}"

    return proxy_config

def translate_match_name(match_name_nba: str) -> str:
    """
    Конвертирует формат матча с сайта NBA/WNBA в формат БД ("Хозяева — Гости").
    На сайте может быть "Away @ Home" или "Home vs Away".
    """
    if '@' in match_name_nba:
        away, home = [x.strip() for x in match_name_nba.split('@')]
    elif 'vs' in match_name_nba.lower():
        home, away = [x.strip() for x in re.split(r'\s+vs\.?\s+', match_name_nba, flags=re.IGNORECASE)]
    else:
        return match_name_nba

    def get_ru_name(eng_name):
        for key in NBA_TEAM_MAP:
            if key.lower() in eng_name.lower():
                return TEAM_MAP_REVERSE[NBA_TEAM_MAP[key]]
        return eng_name

    return f"{get_ru_name(home)} — {get_ru_name(away)}"

def normalize_referee_name(raw_name: str) -> str:
    """
    Нормализует имя судьи, удаляя номер (например, 'Gerda Gatling (#24)' -> 'Gerda Gatling'),
    а затем применяет маппинг, если имя есть в словаре REFEREE_MAP.
    """
    # Удаляем номер в скобках, если он есть
    clean_name = re.sub(r'\s*\(\#\d+\)\s*', '', raw_name).strip()

    # Применяем маппинг
    return REFEREE_MAP.get(clean_name, clean_name)

def init_local_db():
    """
    Инициализирует локальную БД wnba_bot.db и создает таблицу daily_referee_assignments.
    """
    logger.info(f"Подключение к базе данных: {DB_PATH}")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    cursor = conn.cursor()

    # Включение Write-Ahead Logging (WAL) и NORMAL synchronous
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('PRAGMA synchronous=NORMAL;')

    create_table_query = """
    CREATE TABLE IF NOT EXISTS daily_referee_assignments (
        game_date DATE,
        match_name TEXT,
        referee_1 TEXT,
        referee_2 TEXT,
        referee_3 TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (game_date, match_name)
    )
    """
    cursor.execute(create_table_query)
    conn.commit()
    logger.info("Таблица daily_referee_assignments успешно проверена/создана.")

    return conn

async def parse_daily_assignments():
    """
    Парсит ежедневные назначения судей с помощью Playwright для обхода 403 (Akamai WAF).
    Использует headless=False и реалистичный контекст/прокси как в wnba_scraper.py.
    """
    logger.info("Начало парсинга ежедневных назначений судей...")

    url = Config.NBA_REFEREE_URL
    parsed_data = []

    proxy_config = get_playwright_proxy()
    if proxy_config:
        logger.info(f"🌐 Используем прокси: {proxy_config['server']}")
    else:
        logger.warning("⚠️ Прокси не найден в .env. Запуск с родного IP!")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, proxy=proxy_config)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                extra_http_headers={"Referer": "https://official.nba.com/"},
                viewport={"width": 1280, "height": 800}
            )
            page = await context.new_page()

            # Эмулируем обычный браузер
            await page.goto(url, wait_until="networkidle", timeout=60000)

            # Даем небольшую паузу на всякий случай для подгрузки таблиц
            await asyncio.sleep(5)

            html = await page.content()
            await browser.close()

            soup = BeautifulSoup(html, 'html.parser')

            # На сайте НБА/WNBA таблицы с назначениями
            tables = soup.find_all('table')

            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    # Столбцы: Game, Crew Chief, Referee, Umpire
                    cols = row.find_all('td')
                    if len(cols) >= 4:
                        match_name = cols[0].text.strip()
                        # Проверяем, что это строка с матчем (есть @ или vs)
                        if '@' in match_name or 'vs' in match_name.lower():
                            translated_match_name = translate_match_name(match_name)
                            ref1 = normalize_referee_name(cols[1].text.strip())
                            ref2 = normalize_referee_name(cols[2].text.strip())
                            ref3 = normalize_referee_name(cols[3].text.strip())

                            parsed_data.append({
                                "match_name": translated_match_name,
                                "referee_1": ref1,
                                "referee_2": ref2,
                                "referee_3": ref3
                            })
    except Exception as e:
        logger.error(f"Произошла ошибка при парсинге назначений: {e}")

    if not parsed_data:
        logger.info("Назначения не найдены или произошла ошибка. Возвращаем пустой список.")
    else:
        logger.info(f"Успешно получены назначения для {len(parsed_data)} матчей.")

    return parsed_data

def save_daily_assignments(conn, assignments):
    """
    Сохраняет назначения в базу данных на сегодняшний день.
    """
    logger.info("Начало сохранения назначений в БД...")
    cursor = conn.cursor()

    # В WNBA матчи идут по локальному времени США, берем текущую дату
    today = datetime.date.today().isoformat()

    insert_query = """
    INSERT OR REPLACE INTO daily_referee_assignments
    (game_date, match_name, referee_1, referee_2, referee_3, last_updated)
    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """

    count = 0
    for match in assignments:
        match_name = match['match_name'].replace(' — ', ' - ')
        try:
            cursor.execute(
                insert_query,
                (today, match_name, match['referee_1'], match['referee_2'], match['referee_3'])
            )
            count += 1
        except Exception as e:
            logger.error(f"Ошибка при сохранении назначений для {match['match_name']}: {e}")

    conn.commit()
    logger.info(f"Успешно сохранено/обновлено {count} записей о назначениях.")


async def main():
    logger.info("=== Запуск фонового демона ежедневных назначений судей (Smart Daemon, UTC) ===")
    conn = init_local_db()

    # Делаем один проверочный запуск сразу при старте демона
    logger.info("Выполняю первичный проверочный запуск...")
    initial_assignments = await parse_daily_assignments()
    if initial_assignments:
        save_daily_assignments(conn, initial_assignments)

    while True:
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # Проверяем, находимся ли мы в целевом окне: 12:50 - 13:30 UTC
        start_window = now_utc.replace(hour=12, minute=50, second=0, microsecond=0)
        end_window = now_utc.replace(hour=13, minute=30, second=0, microsecond=0)

        if start_window <= now_utc <= end_window:
            logger.info("Находимся в окне публикации (12:50 - 13:30 UTC). Начинаю парсинг...")
            assignments = await parse_daily_assignments()

            if assignments:
                save_daily_assignments(conn, assignments)
                logger.info("Назначения успешно получены и записаны! Демон засыпает до завтра (до 12:50 UTC).")

                # Считаем время до 12:50 завтрашнего дня
                tomorrow = now_utc + datetime.timedelta(days=1)
                next_run = tomorrow.replace(hour=12, minute=50, second=0, microsecond=0)
                sleep_seconds = (next_run - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                await asyncio.sleep(sleep_seconds)
            else:
                logger.info("Назначения пока не найдены. Ждем 5 минут...")
                await asyncio.sleep(300)  # 5 минут

        elif now_utc > end_window:
            logger.info("Окно публикации закрыто (>13:30 UTC). Переход в режим ожидания до завтрашнего окна.")
            # Считаем время до 12:50 завтрашнего дня
            tomorrow = now_utc + datetime.timedelta(days=1)
            next_run = tomorrow.replace(hour=12, minute=50, second=0, microsecond=0)
            sleep_seconds = (next_run - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            await asyncio.sleep(sleep_seconds)

        else:
            logger.info(f"Вне окна публикации. Ожидание до 12:50 UTC.")
            sleep_seconds = (start_window - now_utc).total_seconds()
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)

if __name__ == "__main__":
    asyncio.run(main())
