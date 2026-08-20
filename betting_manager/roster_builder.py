# Version: 6.3 (Dynamic Mappings)
import sqlite3
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from config import Config

async def fetch_rotowire_news():
    url = Config.ROTOWIRE_NEWS_URL
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    proxy_url = getattr(Config, 'PROXY_URL', None)

    news_status = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, proxy=proxy_url, timeout=30) as response:
                response.raise_for_status()
                html = await response.text()

        soup = BeautifulSoup(html, 'html.parser')

        # Structure on Rotowire news typically contains elements like this:
        # <div class="news-update">
        #   <div class="news-update__header">
        #       <a class="news-update__player-link" href="...">Player Name</a>
        #   </div>
        #   <div class="news-update__headline">Expected to play Tuesday</div>
        #   <div class="news-update__inj">Status: Out</div>

        # For our purposes, we just need to identify recent negative statuses for GTD players.
        news_updates = soup.find_all('div', class_='news-update')

        for update in news_updates:
            player_link = update.find('a', class_='news-update__player-link')
            if player_link:
                player_name = player_link.get_text(strip=True)

                # Check for an explicit injury/status tag
                inj_tag = update.find('a', class_='news-update__inj')
                status = None
                if inj_tag:
                    status = inj_tag.get_text(strip=True).upper()

                # Or check the headline
                headline_tag = update.find('div', class_='news-update__headline')
                if headline_tag:
                    headline = headline_tag.get_text(strip=True).lower()
                    if 'ruled out' in headline or 'will not play' in headline or 'out for' in headline or 'expected to miss' in headline:
                        status = 'OUT'
                    elif 'will play' in headline or 'available' in headline or 'expected to play' in headline or 'cleared' in headline:
                        status = 'ACTIVE'

                if status:
                    news_status[player_name] = status

    except Exception as e:
        print(f"Ошибка при получении новостей RotoWire: {e}")

    return news_status

def get_active_roster(cursor, team_ru, player_events):
    mappings = Config.get_mappings()
    TEAM_MAP = mappings.get("TEAM_MAP", {})
    PLAYER_MAP = mappings.get("PLAYER_MAP", {})
    
    team_abbr = TEAM_MAP.get(team_ru)
    if not team_abbr:
        return []

    # 1. Базовый слой: БД
    cursor.execute("""
        SELECT DISTINCT player_name
        FROM player_stats
        WHERE team_abbr = ? AND game_id IN (
            SELECT game_id FROM matches
            WHERE home_team = ? OR away_team = ?
            ORDER BY date DESC LIMIT 5
        )
    """, (team_abbr, team_abbr, team_abbr))

    base_roster = [row[0] for row in cursor.fetchall()]

    # Исключаем травмированных 'OUT' или 'Out For Season'
    cursor.execute("""
        SELECT player_name FROM player_injuries
        WHERE team = ? AND (UPPER(status) = 'OUT' OR UPPER(status) = 'OUT FOR SEASON')
    """, (team_abbr,))
    out_players = [row[0] for row in cursor.fetchall()]

    active_roster = [p for p in base_roster if p not in out_players]

    # 2. Слой новостей
    # Вызываем асинхронную функцию
    try:
        # Instead of skipping, we can use a new event loop in a synchronous context,
        # or use nest_asyncio, or just create a new thread if a loop is already running.
        # But a much simpler solution for this specific script is to use requests,
        # as the user explicitly allowed "requests or aiohttp".
        # However, since we wrote it in aiohttp, we can run it safely:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import threading
                def run_in_thread(coro):
                    result = None
                    def target():
                        nonlocal result
                        new_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(new_loop)
                        result = new_loop.run_until_complete(coro)
                        new_loop.close()
                    thread = threading.Thread(target=target)
                    thread.start()
                    thread.join()
                    return result
                news_status = run_in_thread(fetch_rotowire_news())
            else:
                news_status = loop.run_until_complete(fetch_rotowire_news())
        except RuntimeError:
            news_status = asyncio.run(fetch_rotowire_news())

        # Apply news filter
        for p_name, status in news_status.items():
            if status == 'OUT' and p_name in active_roster:
                active_roster.remove(p_name)
                print(f"  [RotoWire] {p_name} исключен (новости: статус OUT)")
            elif status == 'ACTIVE' and p_name in out_players and p_name not in active_roster:
                if p_name in base_roster:
                    active_roster.append(p_name)
                    print(f"  [RotoWire] {p_name} возвращен (новости: статус ACTIVE)")
    except Exception as e:
        print(f"Ошибка при обработке слоя новостей: {e}")

    # 3. Слой Букмекера
    # Игроки, на которых букмекер дал линию, должны быть в ростере обязательно
    bookmaker_players = []
    for p_ru in player_events.values():
        p_eng = PLAYER_MAP.get(p_ru)
        if p_eng:
            bookmaker_players.append(p_eng)
            if p_eng not in active_roster:
                active_roster.append(p_eng)
                print(f"  [Букмекер] {p_eng} добавлен принудительно (линия активна)")

    return active_roster
