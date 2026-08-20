# Version: 6.6 (State Machine check date fix)
import asyncio
import datetime
import os
import sys
import aiohttp
import logging
import traceback
import sqlite3
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from config import Config
from database.db_manager import DBManager
from betting_manager.main_predictor import run_predictor

FETCH_INTERVAL = 600  # 10 minutes
FONBET_URL = f"{Config.FONBET_API_URL}/events/list?lang=ru&scopeMarket=1600"
EVENT_URL_TEMPLATE = f"{Config.FONBET_API_URL}/events/event?lang=ru&version=0&eventId={{}}&scopeMarket=1600"


async def fetch_json(url, max_retries=5):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.fon.bet",
        "Referer": "https://www.fon.bet/"
    }
    proxy = Config.PROXY_URL if Config.PROXY_URL else None

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, proxy=proxy, timeout=30) as response:
                    if response.status == 429:
                        wait_time = 2 ** attempt
                        logging.warning(f"Rate limit 429 API. Ждем {wait_time} сек...")
                        await asyncio.sleep(wait_time)
                        continue
                    response.raise_for_status()
                    return await response.json()
        except Exception as e:
            if attempt == max_retries - 1:
                raise e
            wait_time = 2 ** attempt
            logging.warning(f"Ошибка fetch_json: {e}. Повтор {attempt+1}/{max_retries} через {wait_time} сек...")
            await asyncio.sleep(wait_time)

    raise Exception(f"Failed to fetch {url} after {max_retries} retries")


def init_bot():
    if Config.PROXY_URL:
        session = AiohttpSession(proxy=Config.PROXY_URL)
        return Bot(token=Config.TG_BOT_TOKEN, session=session)
    return Bot(token=Config.TG_BOT_TOKEN)


async def send_push(bot, db, text, vip_only=False):
    users = db.get_all_users()
    tasks = []
    for u in users:
        user_id, username, first_name, joined_at, sub_end, is_vip = u
        is_active = False
        if sub_end:
            try:
                dt_end = datetime.datetime.strptime(sub_end, '%Y-%m-%d %H:%M:%S.%f')
            except ValueError:
                dt_end = datetime.datetime.strptime(sub_end, '%Y-%m-%d %H:%M:%S')
            is_active = datetime.datetime.now() <= dt_end

        if not is_active and user_id != Config.ADMIN_ID:
            continue

        if vip_only and not is_vip and user_id != Config.ADMIN_ID:
            continue

        tasks.append(bot.send_message(user_id, text))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def process_matches(bot, db):
    try:
        data = await fetch_json(FONBET_URL)
        # ИСПРАВЛЕНИЕ 1: Игнорируем виртуальные матчи "Хозяева"
        active_events = []
        for e in data.get('events', []):
            if e.get('sportId') == 125064 and e.get('level') == 1:
                team1 = e.get('team1', '')
                team2 = e.get('team2', '')
                if 'Хозяева' not in team1 and 'Хозяева' not in team2:
                    active_events.append(e)
    except Exception as e:
        print(f"Ошибка API Фонбета: {e}\n{traceback.format_exc()}")
        return

    conn = db.get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT match_id, status FROM match_tracking")
        tracked_matches = {row[0]: row[1] for row in c.fetchall()}

        active_event_ids = [str(e['id']) for e in active_events]
        matches_to_finish = []

        for m_id, m_status in tracked_matches.items():
            # Если матча больше нет в линии Фонбета, и он еще не закрыт
            if m_id not in active_event_ids and m_status not in ('COMPLETED', 'SETTLED'):
                matches_to_finish.append((m_id,))

        if matches_to_finish:
            # Обновляем все завершенные матчи одним быстрым запросом (защита от database is locked)
            c.executemany("UPDATE match_tracking SET status = 'COMPLETED' WHERE match_id = ?", matches_to_finish)
            conn.commit()
    finally:
        conn.close()

    run_core_needed = False
    calculated_matches = []

    for event in active_events:
        match_id = str(event['id'])
        team1 = event.get('team1')
        team2 = event.get('team2')
        match_date = datetime.datetime.fromtimestamp(event.get('startTime', 0)).strftime(
            "%Y-%m-%d %H:%M:%S") if event.get('startTime') else "Unknown"

        status = tracked_matches.get(match_id)

        try:
            event_data = await fetch_json(EVENT_URL_TEMPLATE.format(match_id))
        except Exception as e:
            logging.error(f"Ошибка при получении данных матча {match_id}: {e}")
            if "404" in str(e):
                conn = db.get_connection()
                try:
                    c = conn.cursor()
                    c.execute("UPDATE match_tracking SET status = 'COMPLETED' WHERE match_id = ?", (match_id,))
                    conn.commit()
                finally:
                    conn.close()
            continue

        has_player_props = False
        for e in event_data.get('events', []):
            if e.get('team2Id') == 0 and e.get('team1') and "Показатели" not in e['team1']:
                has_player_props = True
                break

        is_close_to_start = False
        is_live = False
        if event.get('startTime'):
            time_to_start = event.get('startTime') - datetime.datetime.now().timestamp()
            if 0 < time_to_start <= 1800:
                is_close_to_start = True
            elif time_to_start <= 0:
                is_live = True

        if is_live:
            continue

        if not status:
            print(f"🏀 Новая игра найдена: {team1} - {team2}")
            conn = db.get_connection()
            try:
                c = conn.cursor()
                c.execute(
                    "INSERT INTO match_tracking (match_id, match_date, team1, team2, status) VALUES (?, ?, ?, ?, 'NEW')",
                    (match_id, match_date, team1, team2))
                c.execute("UPDATE match_tracking SET status = 'PRELIM_READY' WHERE match_id = ?", (match_id,))
                conn.commit()
            finally:
                conn.close()

            run_core_needed = True

        elif status in ['PRELIM_READY', 'WAITING_REFS', 'WAITING_ROSTERS'] and has_player_props:
            # Предохранитель: проверяем, считали ли мы этот матч (как пре-матч, так и основной)
            conn = db.get_connection()
            try:
                c = conn.cursor()
                target_date = datetime.datetime.now().strftime("%Y-%m-%d")
                c.execute("SELECT COUNT(*) FROM virtual_bets WHERE match_name LIKE ? AND date = ?", (f"%{team1}%", target_date))
                bets_count = c.fetchone()[0]
                
                if bets_count > 0:
                    c.execute("UPDATE match_tracking SET status = 'PRELIM_CALCULATED' WHERE match_id = ?", (match_id,))
                    conn.commit()
                else:
                    print(f"🔥 Составы утверждены для: {team1} - {team2}")
                    c.execute("UPDATE match_tracking SET status = 'ROSTERS_CONFIRMED' WHERE match_id = ?", (match_id,))
                    conn.commit()
                    run_core_needed = True
                    calculated_matches.append(f"{team1} - {team2}")
            finally:
                conn.close()

        elif status in ['ROSTERS_CONFIRMED', 'WAITING_REFS', 'WAITING_ROSTERS', 'PRELIM_CALCULATED'] and is_close_to_start:
            print(f"⚠️ Контрольный парсинг за 30 мин для: {team1} - {team2}")
            conn = db.get_connection()
            try:
                c = conn.cursor()
                c.execute("UPDATE match_tracking SET status = 'FINAL_CHECK' WHERE match_id = ?", (match_id,))
                conn.commit()
            finally:
                conn.close()

            run_core_needed = True

        # ИСПРАВЛЕНИЕ 2: Мы полностью удалили блок проверки bets_count == 0, чтобы избежать зацикливания!

    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    if run_core_needed:
        print(f"[{current_time}] Парсинг завершен. Запускаю ядро...")
        await asyncio.to_thread(run_predictor)
        # ОТКЛЮЧЕНА РАССЫЛКА ИЗ ДЕМОНА ДЛЯ ПРЕДОТВРАЩЕНИЯ СПАМА
        # for match in calculated_matches:
        #     await send_push(bot, db, f"Готов расчет для игры {match}")
    else:
        print(f"[{current_time}] Парсинг завершен. Жду {FETCH_INTERVAL // 60} мин.")


async def main():
    print("🚀 Оркестратор запущен. Отслеживаем статусы матчей...")
    db = DBManager()

    while True:
        try:
            db.init_db()
            break
        except sqlite3.OperationalError as e:
            logging.warning(f"Ошибка БД при инициализации: {e}. Повтор через 10 секунд...")
            await asyncio.sleep(10)

    bot = init_bot()

    try:
        while True:
            try:
                await process_matches(bot, db)
            except sqlite3.OperationalError as e:
                logging.warning(f"Ошибка БД в цикле process_matches: {e}. Повтор через 10 секунд...")
                await asyncio.sleep(10)
                continue

            await asyncio.sleep(FETCH_INTERVAL)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())