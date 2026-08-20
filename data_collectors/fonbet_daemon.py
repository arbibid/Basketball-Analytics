# Version: 7.14 (Async I/O, DB Batch Inserts, Error Logging)
import asyncio
import aiohttp
import time
import datetime
import os
import sys
import logging

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from database.db_manager import DBManager
from config import Config

# --- БИЗНЕС-ЛОГИКА (ДОМЕННЫЕ ЛИМИТЫ WNBA) ---
WNBA_MIN_GAME_TOTAL = 120.0
WNBA_MIN_TEAM_TOTAL = 60.0
WNBA_MAX_TEAM_TOTAL = 115.0
WNBA_MAX_HANDICAP = 35.0

config_ids = Config.get_market_ids()

VALID_OUTCOME_IDS = {921, 922, 923}
VALID_HANDICAP_IDS = set(config_ids.get("HANDICAP", [])) | {
    910, 912, 927, 928, 989, 991, 1569, 1572, 1672, 1675, 1677, 1678, 1680, 1681,
    1683, 1684, 1686, 1687, 1689, 1690, 1692, 1718, 4925, 4926, 4928, 4929, 4931,
    4932, 4934, 4935, 8990, 8992, 8994, 8996, 8998, 9000
}
VALID_GAME_TOTAL_IDS = set(config_ids.get("GAME_TOTAL", [])) | {
    930, 931, 1696, 1697, 1727, 1728, 1730, 1731, 1733, 1734, 1736, 1737, 1739,
    1791, 1793, 1794, 1796, 1797, 1799, 1800, 1802, 1803, 7319, 7320, 7322, 7323,
    8671, 8672, 8674, 8675, 8683, 8684, 8686, 8687, 8905, 8906, 8908, 8909, 8917,
    8918, 8920, 8921, 8929, 8930, 8932, 8933
}
VALID_TEAM_TOTAL_IDS = set(config_ids.get("TEAM_TOTAL", [])) | {
    1081, 1082, 1083, 1084, 1089, 1090, 1091, 1092, 1809, 1810, 1812, 1813, 1815,
    1816, 1818, 1819, 1821, 1822, 1854, 1871, 1873, 1874, 1880, 1881, 1883, 1884,
    1886, 1887, 2008, 2009, 2011, 2012, 2014, 2015, 2020, 2021, 2030, 2031, 2033,
    2034, 2036, 2037, 2042, 2043, 2324, 2325, 2327, 2328, 2546, 2547, 2549, 2550,
    2552, 2553, 2555, 2556
}

# Метрики игроков (включая передачи)
VALID_PLAYER_POINTS_IDS = (set(config_ids.get("PLAYER_POINTS", [])) | {1432, 1433})
VALID_PLAYER_REBOUNDS_IDS = set(config_ids.get("PLAYER_REBOUNDS", [])) | {1466, 1467}
VALID_PLAYER_THREES_IDS = set(config_ids.get("PLAYER_THREES", [])) | {1515, 1516}
VALID_PLAYER_ASSISTS_IDS = {1474, 1475} # <--- Перенес передачи сюда

EVENTS_LIST_URL = f"{Config.FONBET_API_URL}/events/list?lang=ru&scopeMarket=1600"
EVENT_URL_TEMPLATE = f"{Config.FONBET_API_URL}/events/event?lang=ru&eventId={{}}&scopeMarket=1600"
WNBA_SPORT_ID = 125064
FETCH_INTERVAL = 600


def extract_fonbet_data(factors_list):
    pairs = []
    outcomes = []
    f_map = {f.get('f'): f for f in factors_list if f.get('f') is not None}

    for f_id, f_data in f_map.items():
        if f_id in VALID_OUTCOME_IDS:
            outcomes.append((f_id, 0.0, float(f_data.get('v', 0)), 0.0))

        if f_id + 1 in f_map:
            f_under = f_map[f_id + 1]
            if f_data.get('pt') is not None and f_under.get('pt') is not None:
                pt1_str = str(f_data.get('pt')).replace('+', '').replace('-', '')
                pt2_str = str(f_under.get('pt')).replace('+', '').replace('-', '')
                if pt1_str == pt2_str:
                    try:
                        pairs.append((f_id, float(f_data['pt']), float(f_data['v']), float(f_under['v'])))
                    except ValueError:
                        pass  # Игнорируем диапазоны (например, '105 - 109'), они нам не нужны
                    except Exception as e:
                        logging.error(f"Error parsing pairs: {e}")
    return pairs, outcomes


async def run_daemon():
    db = DBManager()
    db.init_db()

    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    print("🚀 Fonbet Daemon v7.14 запущен. Сбор 100% целевой линии...\n")
    proxy = Config.PROXY_URL if Config.PROXY_URL else None

    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            try:
                try:
                    async with session.get(EVENTS_LIST_URL, proxy=proxy, timeout=30) as resp_list:
                        resp_list.raise_for_status()
                        list_data = await resp_list.json()
                except Exception as e:
                    logging.error(f"Error fetching list: {e}")
                    await asyncio.sleep(FETCH_INTERVAL)
                    continue

                events = list_data.get('events', [])
                wnba_events = []

                for event in events:
                    if event.get('sportId') == WNBA_SPORT_ID and event.get('level') == 1:
                        team1 = event.get('team1', '')
                        team2 = event.get('team2', '')
                        name = event.get('name', '')
                        if 'Хозяева' in team1 or 'Хозяева' in team2 or 'Хозяева' in name: continue
                        wnba_events.append({'id': event['id'], 'name': f"{team1} - {team2}"})

                if not wnba_events:
                    await asyncio.sleep(FETCH_INTERVAL)
                    continue

                for match in wnba_events:
                    current_event_id = match['id']
                    match_name = match['name']

                    try:
                        async with session.get(EVENT_URL_TEMPLATE.format(current_event_id), proxy=proxy, timeout=30) as resp_event:
                            event_data = await resp_event.json()
                    except Exception as e:
                        logging.error(f"Error fetching match {current_event_id}: {e}")
                        continue

                    await asyncio.sleep(1)
                    main_event_id = None
                    player_events = {}

                    for event in event_data.get('events', []):
                        if event.get('level') == 1:
                            main_event_id = event['id']
                        elif event.get('team2Id') == 0 and event.get('team1') and "Показатели" not in event['team1']:
                            player_events[event['id']] = event['team1']

                    conn = db.get_connection()
                    try:
                        c = conn.cursor()

                        stats = {"outcome": 0, "handicap": 0, "game_total": 0, "team_total": 0, "player_pts": 0,
                                 "player_reb": 0, "player_3pt": 0, "player_ast": 0}
                        
                        odds_to_insert = []

                        for cf in event_data.get('customFactors', []):
                            event_id = cf.get('e')
                            pairs, outcomes = extract_fonbet_data(cf.get('factors', []))

                            if event_id == main_event_id:
                                for f_id, pt, o, u in outcomes:
                                    odds_to_insert.append((event_id, f_id, "OUTCOME", "GAME", pt, o, u, str(main_event_id)))
                                    stats["outcome"] += 1

                                for f_id, pt, o, u in pairs:
                                    market = None
                                    if f_id in VALID_GAME_TOTAL_IDS and pt >= WNBA_MIN_GAME_TOTAL:
                                        market = "GAME_TOTAL"
                                        stats["game_total"] += 1
                                    elif f_id in VALID_TEAM_TOTAL_IDS and WNBA_MIN_TEAM_TOTAL <= pt <= WNBA_MAX_TEAM_TOTAL:
                                        market = "TEAM_TOTAL"
                                        stats["team_total"] += 1
                                    elif f_id in VALID_HANDICAP_IDS and abs(pt) <= WNBA_MAX_HANDICAP:
                                        market = "HANDICAP"
                                        stats["handicap"] += 1

                                    if market:
                                        odds_to_insert.append((event_id, f_id, market, "GAME", pt, o, u, str(main_event_id)))

                            elif event_id in player_events:
                                p_name = player_events[event_id]
                                for f_id, pt, o, u in pairs:
                                    prop_type = None
                                    if f_id in VALID_PLAYER_POINTS_IDS:
                                        prop_type = "PLAYER_POINTS"
                                        stats["player_pts"] += 1
                                    elif f_id in VALID_PLAYER_REBOUNDS_IDS:
                                        prop_type = "PLAYER_REBOUNDS"
                                        stats["player_reb"] += 1
                                    elif f_id in VALID_PLAYER_THREES_IDS:
                                        prop_type = "PLAYER_THREES"
                                        stats["player_3pt"] += 1
                                    elif f_id in VALID_PLAYER_ASSISTS_IDS:
                                        prop_type = "PLAYER_ASSISTS"
                                        stats["player_ast"] += 1

                                    if prop_type:
                                        odds_to_insert.append((event_id, f_id, "PLAYER_PROP", p_name, pt, o, u, str(main_event_id)))
                        
                        if odds_to_insert:
                            c.executemany(
                                "INSERT INTO odds_history (event_id, factor_id, market_type, player_or_team, line, over_kf, under_kf, parent_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                odds_to_insert
                            )
                        conn.commit()
                    finally:
                        conn.close()

                    print(f"📌 {match_name}:")
                    print(f"   ├ исход - {stats['outcome']} записей")
                    print(f"   ├ форы - {stats['handicap']} записей")
                    print(f"   ├ тотал - {stats['game_total']} записей")
                    print(f"   ├ индивидуальные тоталы - {stats['team_total']} записей")
                    print(f"   ├ показатели игроков (очки) - {stats['player_pts']} записей")
                    print(f"   ├ подборы - {stats['player_reb']} записей")
                    print(f"   ├ трехочковые - {stats['player_3pt']} записей")
                    print(f"   └ передачи - {stats['player_ast']} записей\n")

                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp}] ✅ Цикл сбора завершен. Ожидание {FETCH_INTERVAL} сек...\n")

            except Exception as e:
                logging.error(f"❌ Непредвиденная ошибка: {e}")

            await asyncio.sleep(FETCH_INTERVAL)


if __name__ == "__main__":
    asyncio.run(run_daemon())