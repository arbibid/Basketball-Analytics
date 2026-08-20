# Version: 4.0 (Слияние визуального дерева V3.0 и записи в БД)
import time
import datetime
import os
import sys
import random
import traceback
from curl_cffi import requests

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from database.db_manager import DBManager

BASE_URL = 'https://ad.betcity.ru/d/off/events'

HEADERS = {
    'Origin': 'https://betcity.ru',
    'Referer': 'https://betcity.ru/',
}

PROXY_URL = 'http://user283911:8nfkmh@138.124.21.137:8632'


class BetcityClient:
    """Менеджер сессии с маскировкой под Chrome 120"""

    def __init__(self):
        self.session = self._create_session()

    def _create_session(self):
        session = requests.Session(
            impersonate="chrome120",
            #proxy=PROXY_URL
        )
        session.headers.update(HEADERS)
        return session

    def fetch_with_retry(self, method, url, max_retries=3, **kwargs):
        for attempt in range(max_retries):
            try:
                if method.upper() == 'POST':
                    response = self.session.post(url, **kwargs)
                else:
                    response = self.session.get(url, **kwargs)

                if response.status_code in [200, 201]:
                    return response
                elif response.status_code in [429, 500, 502, 503, 504]:
                    print(f"Server error {response.status_code}. Retrying ({attempt + 1}/{max_retries})...")
                    time.sleep(2 ** attempt)
                else:
                    print(f"Unexpected status code: {response.status_code}")
                    return response

            except Exception as e:
                err_str = str(e)
                print(f"Request failed: {err_str}. Retrying ({attempt + 1}/{max_retries})...")

                if "PROTOCOL_ERROR" in err_str or "Session is closed" in err_str:
                    print("🔄 Пересоздание TLS-сессии...")
                    try:
                        self.session.close()
                    except:
                        pass
                    self.session = self._create_session()

                time.sleep(2 ** attempt)

        return None


def fetch_wnba_matches(client):
    params = {'rev': 6, 'ver': 87}
    payload = {'ids': 1498}
    req_headers = {'Content-Type': 'application/x-www-form-urlencoded'}

    response = client.fetch_with_retry('POST', BASE_URL, params=params, data=payload, headers=req_headers, timeout=30)
    if response and response.status_code == 200:
        return response.json()
    return None


def fetch_match_details(client, main_id):
    params = {'rev': 6, 'ext': 1, 'add': 'dep_events', 'ver': 87}
    payload = {'id_ev': main_id}
    req_headers = {'Content-Type': 'application/x-www-form-urlencoded'}

    response = client.fetch_with_retry('POST', BASE_URL, params=params, data=payload, headers=req_headers, timeout=30)
    if response and response.status_code == 200:
        return response.json()
    return None


# --- БЛОК ВИЗУАЛИЗАЦИИ V3.0 ---

def extract_kf_count(market_data):
    count = 0
    if not isinstance(market_data, dict):
        return count
    data_block = market_data.get('data', {})
    for sub_id, sub_val in data_block.items():
        if isinstance(sub_val, dict):
            blocks = sub_val.get('blocks', {})
            for b_key, b_val in blocks.items():
                if isinstance(b_val, dict):
                    for v in b_val.values():
                        if isinstance(v, dict) and 'kf' in v:
                            count += 1
    return count


def count_markets(event_dict):
    counts = {'outcome': 0, 'handicap': 0, 'total': 0, 'ind_total': 0}
    markets = []
    if 'main' in event_dict:
        markets.extend(event_dict['main'].items())
    if 'ext' in event_dict:
        markets.extend(event_dict['ext'].items())
    for market_id, data in markets:
        market_id = str(market_id)
        cnt = extract_kf_count(data)
        if market_id in ('69', '203'):
            counts['outcome'] += cnt
        elif market_id == '71':
            counts['handicap'] += cnt
        elif market_id in ('72', '112'):
            counts['total'] += cnt
        elif market_id == '3':
            counts['ind_total'] += cnt
    return counts


def parse_player_markets(ext_data):
    stats_counts = {'pts': 0, 'reb': 0, 'ast': 0, '3pt': 0}
    player_lines = []
    category_map = {
        '971': ('pts', 'Очки/Дуэли'),
        '431': ('reb', 'Подборы'),
        '432': ('ast', 'Передачи'),
        '433': ('3pt', 'Трехочковые')
    }
    if not isinstance(ext_data, dict):
        return stats_counts, player_lines

    for cat_id, cat_info in category_map.items():
        if cat_id in ext_data:
            cat_block = ext_data[cat_id]
            rows = cat_block.get('rows', {})
            for row_id, row_data in rows.items():
                player_name = row_data.get('name', 'Unknown')
                data_block = row_data.get('data', {})
                for d_id, d_val in data_block.items():
                    blocks = d_val.get('blocks', {})
                    for b_key, b_val in blocks.items():
                        tm_val = b_val.get('Tm')
                        tb_val = b_val.get('Tb')
                        if tm_val and tb_val:
                            # Умная проверка для извлечения кэфов Бетсити
                            if isinstance(tm_val, dict):
                                lv = tm_val.get('lv', '')
                                kf_m = tm_val.get('kf', '')
                                kf_b = tb_val.get('kf', '')
                            else:
                                lv = b_val.get('lv', '')
                                kf_m = tm_val
                                kf_b = tb_val

                            stats_counts[cat_info[0]] += 2
                            player_lines.append(
                                f"[{cat_info[1]}] {player_name} | Тотал {lv} | Меньше: {kf_m}, Больше: {kf_b}")
    return stats_counts, player_lines


# --- БЛОК ЗАПИСИ В БД ---

def save_odds(cursor, event_id, market_type, target_name, line, over_kf, under_kf, factor_id):
    cursor.execute("""
        SELECT id FROM odds_history 
        WHERE event_id = ? AND market_type = ? AND player_or_team = ? AND line = ? AND bookmaker = 'BETCITY'
    """, (event_id, market_type, target_name, line))
    row = cursor.fetchone()

    if row:
        cursor.execute("""
            UPDATE odds_history 
            SET over_kf = ?, under_kf = ?, timestamp = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (over_kf, under_kf, row[0]))
    else:
        cursor.execute("""
            INSERT INTO odds_history (event_id, market_type, player_or_team, line, over_kf, under_kf, factor_id, bookmaker)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'BETCITY')
        """, (event_id, market_type, target_name, line, over_kf, under_kf, factor_id))


def extract_h2h_duels(ext_block):
    duels = []
    if '971' in ext_block:
        rows = ext_block['971'].get('rows', {})
        for row_id, row_data in rows.items():
            name = row_data.get('name', '')
            if ' - ' in name:
                data_block = row_data.get('data', {})
                for d_id, d_val in data_block.items():
                    blocks = d_val.get('blocks', {})
                    for b_key, b_val in blocks.items():
                        tm_val = b_val.get('Tm')
                        tb_val = b_val.get('Tb')
                        if tm_val and tb_val:
                            if isinstance(tm_val, dict):
                                lv = tm_val.get('lv', 0.0)
                                kf_m = tm_val.get('kf', 0.0)
                                kf_b = tb_val.get('kf', 0.0)
                            else:
                                lv = b_val.get('lv', 0.0)
                                kf_m = tm_val
                                kf_b = tb_val

                            duels.append({
                                'name': name,
                                'line': float(lv),
                                'kf_1': float(kf_m),
                                'kf_2': float(kf_b),
                                'factor_id': int(d_id)
                            })
    return duels


def process_and_save_match(main_id, data_main, db_manager):
    try:
        reply = data_main.get('reply', {})
        sports = reply.get('sports', {})
        evts_main = {}

        for sp_id, sp_data in sports.items():
            chmps = sp_data.get('chmps', {})
            for ch_id, ch_data in chmps.items():
                if 'evts' in ch_data and str(main_id) in ch_data['evts']:
                    evts_main = ch_data['evts']
                    break
            if evts_main: break

        if not evts_main:
            print(f"События для матча {main_id} не найдены.")
            return

        main_event = evts_main[str(main_id)]

        # --- ВЫВОД В КОНСОЛЬ V3.0 ---
        team1 = main_event.get('name_ht') or main_event.get('name_1', 'Команда 1')
        team2 = main_event.get('name_at') or main_event.get('name_2', 'Команда 2')
        match_title = f"{team1} - {team2}"

        main_counts = count_markets(main_event)
        stats_counts = {'pts': 0, 'reb': 0, 'ast': 0, '3pt': 0}
        player_lines = []

        ext_block = main_event.get('ext', {})
        if ext_block:
            stats, lines = parse_player_markets(ext_block)
            for k in stats:
                stats_counts[k] += stats[k]
            player_lines.extend(lines)

        print(f"[{main_id}] Запрашиваем основные события и роспись игроков...")
        print(f"📌 {match_title}:")
        print(f"   ├ исход - {main_counts['outcome']} записей")
        print(f"   ├ форы - {main_counts['handicap']} записей")
        print(f"   ├ тотал - {main_counts['total']} записей")
        print(f"   ├ индивидуальные тоталы - {main_counts['ind_total']} записей")
        print(f"   ├ показатели игроков (очки) - {stats_counts['pts']} записей")
        print(f"   ├ подборы - {stats_counts['reb']} записей")
        print(f"   ├ трехочковые - {stats_counts['3pt']} записей")
        print(f"   └ передачи - {stats_counts['ast']} записей\n")

        if player_lines:
            print("   --- ЛИНИЯ НА ИГРОКОВ ---")
            for line in player_lines:
                print(f"   {line}")
        else:
            print("   --- ЛИНИЯ НА ИГРОКОВ ПУСТА ---")
        print("")

        # --- ЗАПИСЬ ДАННЫХ В БД ДЛЯ PREDICTOR ---
        conn = db_manager.get_connection()
        try:
            c = conn.cursor()

            main_block = main_event.get('main', {})
            for m_id in ['72', '112']:
                if m_id in main_block:
                    for d_id, d_val in main_block[m_id].get('data', {}).items():
                        for b_key, b_val in d_val.get('blocks', {}).items():
                            tm, tb = b_val.get('Tm'), b_val.get('Tb')
                            if tm is not None and tb is not None:
                                if isinstance(tm, dict):
                                    lv = float(tm.get('lv', b_val.get('lv', 0)))
                                    kf_m, kf_b = float(tm.get('kf', 0)), float(tb.get('kf', 0))
                                else:
                                    lv = float(b_val.get('lv', 0))
                                    kf_m, kf_b = float(tm), float(tb)
                                save_odds(c, str(main_id), 'GAME_TOTAL', 'GAME', lv, kf_b, kf_m, int(m_id))

            if '71' in main_block:
                for d_id, d_val in main_block['71'].get('data', {}).items():
                    for b_key, b_val in d_val.get('blocks', {}).items():
                        f1, f2 = b_val.get('F1'), b_val.get('F2')
                        if f1 is not None and f2 is not None:
                            if isinstance(f1, dict):
                                lv = float(f1.get('lv', b_val.get('lv', 0)))
                                kf1, kf2 = float(f1.get('kf', 0)), float(f2.get('kf', 0))
                            else:
                                lv = float(b_val.get('lv', 0))
                                kf1, kf2 = float(f1), float(f2)
                            save_odds(c, str(main_id), 'HANDICAP', 'GAME', lv, kf1, kf2, 71)

            if ext_block:
                duels = extract_h2h_duels(ext_block)
                for d in duels:
                    save_odds(c, str(main_id), 'PLAYER_H2H', d['name'], d['line'], d['kf_2'], d['kf_1'], d['factor_id'])

                category_map_db = {'971': 'PLAYER_PTS', '431': 'PLAYER_REB', '432': 'PLAYER_AST', '433': 'PLAYER_FG3M'}
                for cat_id, market_type in category_map_db.items():
                    if cat_id in ext_block:
                        for row_id, row_data in ext_block[cat_id].get('rows', {}).items():
                            player_name = row_data.get('name', 'Unknown')
                            if ' - ' in player_name: continue
                            for d_id, d_val in row_data.get('data', {}).items():
                                for b_key, b_val in d_val.get('blocks', {}).items():
                                    tm, tb = b_val.get('Tm'), b_val.get('Tb')
                                    if tm and tb:
                                        if isinstance(tm, dict):
                                            lv = float(tm.get('lv', b_val.get('lv', 0)))
                                            kf_m, kf_b = float(tm.get('kf', 0)), float(tb.get('kf', 0))
                                        else:
                                            lv = float(b_val.get('lv', 0))
                                            kf_m, kf_b = float(tm), float(tb)
                                        save_odds(c, str(main_id), market_type, player_name, lv, kf_b, kf_m, int(d_id))

            conn.commit()

        except Exception as db_err:
            print(f"Ошибка при работе с БД в матче {main_id}: {db_err}")
            conn.rollback()  # Откатываем битые данные
        finally:
            # Гарантированно закрываем базу и снимаем блокировку
            conn.close()

    except Exception as e:
        print(f"Ошибка при обработке матча {main_id}: {e}")
        traceback.print_exc()


def calculate_jitter(match_ids):
    if match_ids:
        return random.randint(8 * 60, 16 * 60)
    return random.randint(10 * 60, 50 * 60)


def main_loop(run_once=False):
    db_manager = DBManager()
    client = BetcityClient()

    while True:
        try:
            print("Инициализация клиента Betcity (impersonate=chrome120)...")
            print("Запрашиваем список матчей WNBA...")
            list_data = fetch_wnba_matches(client)
            match_ids = []

            if list_data:
                try:
                    chmps = list_data['reply']['sports']['3']['chmps']
                    for chmp_data in chmps.values():
                        if 'evts' in chmp_data:
                            for ev_id in chmp_data['evts'].keys():
                                match_ids.append(ev_id)
                except KeyError:
                    pass

            print(f"Найдено матчей: {len(match_ids)}\n")

            for main_id in match_ids:
                time.sleep(2)
                detail_data = fetch_match_details(client, main_id)
                if detail_data:
                    process_and_save_match(main_id, detail_data, db_manager)

            if run_once: break

            sleep_time = calculate_jitter(match_ids)
            print(f"Спим {sleep_time // 60} минут до следующего опроса...\n")
            time.sleep(sleep_time)

        except Exception as e:
            print(f"Критическая ошибка демона Betcity: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run_once = "--run-once" in sys.argv
    main_loop(run_once=run_once)