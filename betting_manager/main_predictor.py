# Version: 7.6 (Single Responsibility: removed bettor API & sleep)
import requests
import sqlite3
import datetime
import os
import sys
import random
import time
import re
import json

try:
    from google import genai
except ImportError:
    genai = None

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from data_collectors.fonbet_daemon import extract_fonbet_data

from core.wnba_math import WNBAMathCore
from database.db_manager import DBManager
from core.money_management import calculate_kelly_bet
from config import Config
from data_collectors.injury_scraper import fetch_and_update_injuries

mappings = Config.get_mappings()
PLAYER_MAP = mappings.get("PLAYER_MAP", {})
TEAM_MAP = mappings.get("TEAM_MAP", {})

# Синхронизированные пулы ID из парсера
config_ids = Config.get_market_ids()
VALID_OUTCOME_IDS = {921, 922, 923}
HANDICAP_IDS = set(config_ids.get("HANDICAP", [])) | {
    910, 912, 927, 928, 989, 991, 1569, 1572, 1672, 1675, 1677, 1678, 1680, 1681,
    1683, 1684, 1686, 1687, 1689, 1690, 1692, 1718, 4925, 4926, 4928, 4929, 4931,
    4932, 4934, 4935, 8990, 8992, 8994, 8996, 8998, 9000
}
GAME_TOTAL_IDS = set(config_ids.get("GAME_TOTAL", [])) | {
    930, 931, 1696, 1697, 1727, 1728, 1730, 1731, 1733, 1734, 1736, 1737, 1739,
    1791, 1793, 1794, 1796, 1797, 1799, 1800, 1802, 1803, 7319, 7320, 7322, 7323,
    8671, 8672, 8674, 8675, 8683, 8684, 8686, 8687, 8905, 8906, 8908, 8909, 8917,
    8918, 8920, 8921, 8929, 8930, 8932, 8933
}
TEAM_TOTAL_IDS = set(config_ids.get("TEAM_TOTAL", [])) | {
    1081, 1082, 1083, 1084, 1089, 1090, 1091, 1092, 1809, 1810, 1812, 1813, 1815,
    1816, 1818, 1819, 1821, 1822, 1854, 1871, 1873, 1874, 1880, 1881, 1883, 1884,
    1886, 1887, 2008, 2009, 2011, 2012, 2014, 2015, 2020, 2021, 2030, 2031, 2033,
    2034, 2036, 2037, 2042, 2043, 2324, 2325, 2327, 2328, 2546, 2547, 2549, 2550,
    2552, 2553, 2555, 2556
}
PLAYER_POINTS_IDS = set(config_ids.get("PLAYER_POINTS", [])) | {1432, 1433}
PLAYER_REBOUNDS_IDS = set(config_ids.get("PLAYER_REBOUNDS", [])) | {1466, 1467}
PLAYER_THREES_IDS = set(config_ids.get("PLAYER_THREES", [])) | {1515, 1516}
PLAYER_ASSISTS_IDS = {1474, 1475}

# Кэши
_unmapped_players_alerted = set()
failed_api_resolves = set()
VIRTUAL_BANKROLL = 10000.0


def auto_resolve_player_name(p_name_ru, normalized_p_name_ru, roster, current_player_map):
    """Безопасный вызов Gemini API для авто-маппинга новых игроков с защитой от Rate Limit"""
    if not genai:
        print(f"❌ Ошибка: Не установлена библиотека google-genai. Авто-маппинг '{p_name_ru}' пропущен.")
        return None
    if not getattr(Config, 'GEMINI_API_KEY', None):
        print(f"❌ Ошибка: Отсутствует GEMINI_API_KEY в конфигурации. Авто-маппинг '{p_name_ru}' пропущен.")
        return None

    if normalized_p_name_ru in failed_api_resolves:
        return None

    try:
        if getattr(Config, 'PROXY_URL', None):
            import httpx
            from google.genai import types
            proxy_url = Config.PROXY_URL
            http_options = types.HttpOptions(
                client_args={
                    "transport": httpx.HTTPTransport(proxy=proxy_url),
                    "timeout": 15.0
                }
            )
            client = genai.Client(api_key=Config.GEMINI_API_KEY, http_options=http_options)
        else:
            client = genai.Client(api_key=Config.GEMINI_API_KEY)

        prompt = f"""Ты баскетбольный аналитик. Букмекер написал имя игрока как "{p_name_ru}". Вот официальный ростер ее команды: {roster}. Найди точное совпадение с учетом особенностей транслитерации и двойных фамилий. В ответе выдай СТРОГО инициал и фамилию на английском (например: "K. Cardoso") без лишнего текста и кавычек."""

        response = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=prompt
                )
                break
            except Exception as e:
                if '429' in str(e) or 'quota' in str(e).lower() or 'timeout' in str(e).lower():
                    time.sleep(2 ** attempt)
                else:
                    raise e

        if response and response.text:
            english_name = response.text.strip().replace('"', '')
            if not english_name:
                return None

            if english_name not in roster:
                print(f"  ⚠️ Авто-маппинг проигнорирован (Gemini): Игрок {english_name} не найден в ростере матча.")
                time.sleep(4.2)
                return None

            mapping_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'config', 'mappings.json'))
            if os.path.exists(mapping_path):
                with open(mapping_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if "PLAYER_MAP" not in data:
                    data["PLAYER_MAP"] = {}
                data["PLAYER_MAP"][p_name_ru] = english_name
                with open(mapping_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())

            current_player_map[normalized_p_name_ru] = english_name
            current_player_map[p_name_ru] = english_name

            print(f"  🤖 Авто-маппинг (Gemini): {p_name_ru} -> {english_name}")

            # 🔥 ТОРМОЗ ДЛЯ API: Ждем 4.2 секунды (60 сек / 15 запросов = 4 сек)
            time.sleep(4.2)
            return english_name

    except Exception as e:
        # Если словили лимит, спим 60 секунд и пробуем снова
        if '429' in str(e) or 'RESOURCE_EXHAUSTED' in str(e):
            print(f"  ⏳ Лимит запросов Gemini (15 в минуту) исчерпан. Спим 60 секунд перед '{p_name_ru}'...")
            time.sleep(60)
            return auto_resolve_player_name(p_name_ru, normalized_p_name_ru, roster, current_player_map)
        else:
            print(f"  ⚠️ Ошибка Gemini API при маппинге '{p_name_ru}': {e}")

    return None


def init_virtual_bank(db_path):
    db_manager = DBManager(db_path)
    conn = db_manager.get_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS virtual_bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        match_name TEXT,
        market TEXT,
        player_name TEXT,
        line REAL,
        prediction REAL,
        selection TEXT,
        kf REAL,
        bet_amount REAL,
        status TEXT DEFAULT 'PENDING'
    )''')

    for col in ["category", "vip_kf", "published_at", "is_preliminary"]:
        try:
            default_val = "0" if col == "is_preliminary" else "NULL"
            c.execute(f"ALTER TABLE virtual_bets ADD COLUMN {col} TEXT DEFAULT {default_val}")
        except sqlite3.OperationalError:
            pass

    try:
        c.execute("ALTER TABLE virtual_bets ADD COLUMN bookmaker TEXT DEFAULT 'FONBET'")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE virtual_bets ADD COLUMN market_type TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    return conn


def get_daily_referees(target_date, team1_ru, team2_ru):
    try:
        db_manager = DBManager()
        conn = db_manager.get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT referee_1, referee_2, referee_3 FROM daily_referee_assignments WHERE game_date = ? AND (match_name LIKE ? OR match_name LIKE ?)",
            (target_date, f"%{team1_ru}%", f"%{team2_ru}%"))
        row = c.fetchone()
        conn.close()
        if row:
            r1, r2, r3 = row
            r1 = r1 if r1 and str(r1).strip() else "Не указан"
            r2 = r2 if r2 and str(r2).strip() else "Не указан"
            r3 = r3 if r3 and str(r3).strip() else "Не указан"
            return r1, r2, r3
    except Exception as e:
        print(f"Ошибка получения судей: {e}")
    return "Не указан", "Не указан", "Не указан"


def run_predictor():
    db = DBManager()
    db.init_db()
    game_edge_threshold = float(db.get_system_setting("game_edge_threshold", 2.0))
    # ИСПРАВЛЕНИЕ: устанавливаем адекватный порог для игроков 0.05
    player_edge_threshold = float(db.get_system_setting("player_edge_threshold", 0.05))
    win_edge_threshold = float(db.get_system_setting("win_edge_threshold", 0.015))
    base_bet_amount = float(db.get_system_setting("base_bet_amount", 50.0))

    print("=" * 70)
    print("🚀 MAIN PREDICTOR: ПОИСК ВАЛУЕВ + РАСЧЕТ МАТЧЕЙ")
    print("=" * 70)
    conn_fix = db.get_connection()
    try:
        conn_fix.execute('''CREATE TABLE IF NOT EXISTS player_injuries (
            player_name TEXT PRIMARY KEY,
            team TEXT,
            position TEXT,
            status TEXT,
            injury TEXT,
            description TEXT,
            updated_at TEXT
        )''')

        conn_fix.execute('''CREATE TABLE IF NOT EXISTS player_projections (
            team_name TEXT,
            player_name TEXT,
            projected_pts REAL,
            PRIMARY KEY (team_name, player_name)
        )''')
        conn_fix.commit()
    except Exception:
        pass
    finally:
        conn_fix.close()

    print("\n--- Обновление данных по травмам ---")
    try:
        fetch_and_update_injuries()
    except Exception as e:
        print(f"⚠️ Ошибка при обновлении травм: {e}")
    print("------------------------------------\n")

    db.init_db()
    conn = init_virtual_bank(db.db_path)
    cursor = conn.cursor()

    target_date = datetime.datetime.now().strftime("%Y-%m-%d")
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ИСПРАВЛЕНИЕ 1: Жесткая фильтрация по времени. Отсекаем матчи, которые уже начались!
    cursor.execute("""
            SELECT match_id, team1, team2 
            FROM match_tracking 
            WHERE status NOT IN ('COMPLETED', 'SETTLED') 
            AND match_date >= ?
        """, (current_time,))
    active_matches_db = cursor.fetchall()

    if not active_matches_db:
        print("💤 Нет активных матчей WNBA в БД.")
        conn.close()
        return

    print(f"📊 Найдено матчей: {len(active_matches_db)}")
    total_bets_placed = 0

    cursor.execute("DELETE FROM virtual_bets WHERE date = ?", (target_date,))
    conn.commit()

    from core.telegram_notifier import TelegramNotifier
    notifier = TelegramNotifier()
    match_carts = {}

    from core.wnba_math import WNBAMathCore
    from betting_manager.roster_builder import get_active_roster

    for match_id, team1_ru, team2_ru in active_matches_db:
        try:
            if 'Хозяева' in team1_ru or 'Гости' in team2_ru:
                continue

            # Извлекаем дату/время из БД для логов (если она есть в таблице)
            cursor.execute("SELECT match_date FROM match_tracking WHERE match_id = ?", (str(match_id),))
            date_row = cursor.fetchone()
            match_dt_str = date_row[0] if date_row else target_date

            match_name = f"{team1_ru} - {team2_ru}"
            print(f"\n🏀 Анализ матча: {match_name} | Запланирован на: {match_dt_str}")

            t1_abbr = TEAM_MAP.get(team1_ru)
            t2_abbr = TEAM_MAP.get(team2_ru)

            if not t1_abbr or not t2_abbr:
                print(
                    f"  ❌ СКИП: Ошибка маппинга! '{team1_ru}'={t1_abbr}, '{team2_ru}'={t2_abbr}. Проверь файл mappings.json на забытые запятые!")
                continue

            ref1, ref2, ref3 = get_daily_referees(target_date, team1_ru, team2_ru)
            is_waiting_refs = not ref1 or not str(ref1).strip() or ref1 == "Не указан"

            cursor.execute(
                "SELECT DISTINCT player_name FROM player_stats WHERE team_abbr = ? AND game_id IN (SELECT game_id FROM matches WHERE home_team = ? OR away_team = ? ORDER BY date DESC LIMIT 5)",
                (t1_abbr, t1_abbr, t1_abbr))
            base1 = [r[0] for r in cursor.fetchall()]

            cursor.execute(
                "SELECT DISTINCT player_name FROM player_stats WHERE team_abbr = ? AND game_id IN (SELECT game_id FROM matches WHERE home_team = ? OR away_team = ? ORDER BY date DESC LIMIT 5)",
                (t2_abbr, t2_abbr, t2_abbr))
            base2 = [r[0] for r in cursor.fetchall()]

            player_events = {}
            player_events_t1 = {}
            player_events_t2 = {}

            current_mappings = Config.get_mappings()
            current_player_map = current_mappings.get("PLAYER_MAP", {})

            # ИСПРАВЛЕНИЕ: Ищем под-события Фонбета в диапазоне +5000 ID от основного матча
            cursor.execute("""
                        SELECT event_id, player_or_team, bookmaker
                        FROM odds_history
                        WHERE market_type LIKE 'PLAYER_%' AND (event_id = ? OR parent_event_id = ?)
                    """, (match_id, str(match_id)))
            local_player_events = cursor.fetchall()

            for sub_id, p_name_ru, bookmaker in local_player_events:
                player_events[sub_id] = p_name_ru

                normalized_p_name_ru = re.sub(r'\s+', '', p_name_ru.strip())
                db_name = current_player_map.get(normalized_p_name_ru)

                combined_roster = list(set(base1 + base2))
                if (not db_name or (
                        db_name and db_name not in combined_roster)) and normalized_p_name_ru not in failed_api_resolves:
                    db_name = auto_resolve_player_name(p_name_ru, normalized_p_name_ru, combined_roster,
                                                       current_player_map)
                    if not db_name:
                        failed_api_resolves.add(normalized_p_name_ru)

                if db_name:
                    if db_name in base1:
                        player_events_t1[sub_id] = p_name_ru
                    elif db_name in base2:
                        player_events_t2[sub_id] = p_name_ru
                    else:
                        cursor.execute('''
                            SELECT p.team_abbr
                            FROM player_stats p
                            JOIN matches m ON p.game_id = m.game_id
                            WHERE p.player_name = ?
                            ORDER BY m.date DESC LIMIT 1
                        ''', (db_name,))
                        team_row = cursor.fetchone()
                        if team_row:
                            p_team = team_row[0]
                            if p_team == t1_abbr:
                                player_events_t1[sub_id] = p_name_ru
                            elif p_team == t2_abbr:
                                player_events_t2[sub_id] = p_name_ru
                else:
                    if normalized_p_name_ru not in _unmapped_players_alerted:
                        _unmapped_players_alerted.add(normalized_p_name_ru)
                        alert_msg = f"🚨 Неизвестный игрок в линии: \"{p_name_ru}\" ({normalized_p_name_ru}). Матч: {match_name}."
                        notifier._send_message(alert_msg)

            is_waiting_rosters = not player_events

            prelim_flag = 1 if (is_waiting_refs or is_waiting_rosters) else 0
            calc_status = 'WAITING_REFS' if is_waiting_refs else (
                'WAITING_ROSTERS' if is_waiting_rosters else 'READY_TO_CALCULATE')

            try:
                cursor.execute("DELETE FROM virtual_bets WHERE match_name = ? AND is_preliminary = 1", (match_name,))
            except sqlite3.OperationalError:
                pass

            try:
                cursor.execute("SELECT status FROM match_tracking WHERE match_id = ?", (str(match_id),))
                row = cursor.fetchone()
                old_status = row[0] if row else None

                if calc_status == 'READY_TO_CALCULATE' and old_status != 'READY_TO_CALCULATE':
                    notifier._send_message(f"✅ Готов расчет для игры {team1_ru} - {team2_ru}")

                cursor.execute("UPDATE match_tracking SET status = ? WHERE match_id = ?",
                               (calc_status, str(match_id)))
            except sqlite3.OperationalError:
                pass

            if prelim_flag == 1:
                print(f"  ⚠️ Предварительный расчет ({calc_status}). Запуск математического ядра V6.6...")
            else:
                print("  ✅ Данные готовы. Запуск математического ядра V6.6...")

            roster_t1 = get_active_roster(cursor, team1_ru, player_events_t1)
            roster_t2 = get_active_roster(cursor, team2_ru, player_events_t2)

            dnp1 = [p for p in base1 if p not in roster_t1]
            dnp2 = [p for p in base2 if p not in roster_t2]

            crew_mod = WNBAMathCore.get_crew_modifier(cursor, ref1, ref2, ref3, target_date)
            exp_spread_t1 = WNBAMathCore.predict_outcome(cursor, t2_abbr, t1_abbr)
            exp_spread_t2 = -exp_spread_t1

            t1_total, t1_preds, log_t1 = WNBAMathCore.calculate_team_projection(cursor, t1_abbr, roster_t1, dnp1,
                                                                                target_date, crew_mod, is_home=True,
                                                                                opp_team=t2_abbr,
                                                                                expected_spread=exp_spread_t1)
            t2_total, t2_preds, log_t2 = WNBAMathCore.calculate_team_projection(cursor, t2_abbr, roster_t2, dnp2,
                                                                                target_date, crew_mod, is_home=False,
                                                                                opp_team=t1_abbr,
                                                                                expected_spread=exp_spread_t2)

            math_log = f"{log_t1}\n〰️〰️〰️〰️〰️〰️〰️〰️〰️\n{log_t2}"

            if is_waiting_refs:
                now = datetime.datetime.now(datetime.timezone.utc)
                # Find the next 00:15 GMT
                target = now.replace(hour=0, minute=15, second=0, microsecond=0)
                if now > target:
                    target += datetime.timedelta(days=1)

                diff = target - now
                hours, remainder = divmod(diff.seconds, 3600)
                minutes, _ = divmod(remainder, 60)

                if diff.days > 0:
                    hours += diff.days * 24

                time_str = f"{hours} ч. {minutes} мин."
                warning_msg = f"⚠️ Расчет предварительный (без судей). Судьи ожидаются через {time_str}\n"
                math_log = warning_msg + math_log

            expected_total = t1_total + t2_total
            expected_margin_t1 = t1_total - t2_total

            try:
                cursor.execute(
                    "UPDATE match_tracking SET proj_score1 = ?, proj_score2 = ?, math_log = ? WHERE match_id = ?",
                    (t1_total, t2_total, math_log, match_id))
            except sqlite3.OperationalError:
                pass

            match_carts[match_name] = []
            published_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # === ПЕРВЫЙ ЦИКЛ: ИСХОДЫ И ТОТАЛЫ ===
            cursor.execute(
                "SELECT factor_id, line, over_kf, under_kf, market_type, player_or_team, bookmaker FROM odds_history WHERE event_id = ? AND market_type NOT LIKE 'PLAYER_%'",
                (str(match_id),))
            game_odds = cursor.fetchall()

            max_f1, max_f2 = -1, -1
            opt_f1, opt_f2 = None, None

            for f_id, pt, over_kf, under_kf, m_type, pt_name, bookmaker in game_odds:
                if pt > 120 or m_type == 'GAME_TOTAL':
                    delta_total = expected_total - pt
                    if delta_total >= game_edge_threshold:
                        implied_prob = 1.0 / over_kf
                        win_prob = min(implied_prob + (abs(delta_total) * 0.025), 0.90)
                        edge = win_prob - implied_prob
                        if edge >= win_edge_threshold:
                            cursor.execute(
                                "SELECT COUNT(*) FROM virtual_bets WHERE date = ? AND match_name = ? AND market = ? AND selection = ? AND is_preliminary = ?",
                                (target_date, match_name, "GAME_TOTAL", "БОЛЬШЕ", prelim_flag))
                            if cursor.fetchone()[0] == 0 and 1.91 <= over_kf <= 2.15:
                                print(f"  🔥 ВАЛУЙ: ТОТАЛ БОЛЬШЕ (Линия: {pt} | Кэф: {over_kf}) | Edge: {edge * 100:.1f}%")
                                coupon_id = None
                                cursor.execute(
                                    '''INSERT INTO virtual_bets (date, match_name, market, category, player_name, line, prediction, selection, kf, vip_kf, bet_amount, published_at, is_preliminary, coupon_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                    (target_date, match_name, "GAME_TOTAL", "ТОТАЛ", "GAME", pt, expected_total, "БОЛЬШЕ",
                                     over_kf, over_kf, base_bet_amount, published_at, prelim_flag, coupon_id))
                                if prelim_flag == 0:
                                    cursor.execute(
                                        '''INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge) VALUES (?, ?, ?, ?, ?, ?)''',
                                        (match_name, "GAME_TOTAL", "GAME | БОЛЬШЕ", pt, over_kf, round(edge * 100, 2))
                                    )
                                total_bets_placed += 1
                    elif delta_total <= -game_edge_threshold:
                        implied_prob = 1.0 / under_kf
                        win_prob = min(implied_prob + (abs(delta_total) * 0.025), 0.90)
                        edge = win_prob - implied_prob
                        if edge >= win_edge_threshold:
                            cursor.execute(
                                "SELECT COUNT(*) FROM virtual_bets WHERE date = ? AND match_name = ? AND market = ? AND selection = ? AND is_preliminary = ?",
                                (target_date, match_name, "GAME_TOTAL", "МЕНЬШЕ", prelim_flag))
                            if cursor.fetchone()[0] == 0 and 1.91 <= under_kf <= 2.15:
                                print(f"  🔥 ВАЛУЙ: ТОТАЛ МЕНЬШЕ (Линия: {pt} | Кэф: {under_kf}) | Edge: {edge * 100:.1f}%")
                                coupon_id = None
                                cursor.execute(
                                    '''INSERT INTO virtual_bets (date, match_name, market, category, player_name, line, prediction, selection, kf, vip_kf, bet_amount, published_at, is_preliminary, coupon_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                    (target_date, match_name, "GAME_TOTAL", "ТОТАЛ", "GAME", pt, expected_total, "МЕНЬШЕ",
                                     under_kf, under_kf, base_bet_amount, published_at, prelim_flag, coupon_id))
                                if prelim_flag == 0:
                                    cursor.execute(
                                        '''INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge) VALUES (?, ?, ?, ?, ?, ?)''',
                                        (match_name, "GAME_TOTAL", "GAME | МЕНЬШЕ", pt, under_kf, round(edge * 100, 2))
                                    )
                                total_bets_placed += 1
                elif 60 < pt < 115 or m_type == 'TEAM_TOTAL':
                    team_target, team_proj = (team1_ru, t1_total) if abs(pt - t1_total) < abs(pt - t2_total) else (
                        team2_ru, t2_total)
                    delta_team = team_proj - pt
                    if delta_team >= game_edge_threshold:
                        implied_prob = 1.0 / over_kf
                        win_prob = min(implied_prob + (abs(delta_team) * 0.025), 0.90)
                        edge = win_prob - implied_prob
                        if edge >= win_edge_threshold:
                            cursor.execute(
                                "SELECT COUNT(*) FROM virtual_bets WHERE date = ? AND match_name = ? AND player_name = ? AND market = ? AND selection = ? AND is_preliminary = ?",
                                (target_date, match_name, team_target, "TEAM_TOTAL", "БОЛЬШЕ", prelim_flag))
                            if cursor.fetchone()[0] == 0 and 1.91 <= over_kf <= 2.15:
                                print(f"  🔥 ВАЛУЙ: ИТ БОЛЬШЕ {team_target} (Линия: {pt} | Кэф: {over_kf})")
                                coupon_id = None
                                cursor.execute(
                                    '''INSERT INTO virtual_bets (date, match_name, market, category, player_name, line, prediction, selection, kf, vip_kf, bet_amount, published_at, is_preliminary, coupon_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                    (target_date, match_name, "TEAM_TOTAL", "ТОТАЛ", team_target, pt, team_proj, "БОЛЬШЕ",
                                     over_kf, over_kf, base_bet_amount, published_at, prelim_flag, coupon_id))
                                if prelim_flag == 0:
                                    cursor.execute(
                                        '''INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge) VALUES (?, ?, ?, ?, ?, ?)''',
                                        (match_name, "TEAM_TOTAL", f"{team_target} | БОЛЬШЕ", pt, over_kf, round(edge * 100, 2))
                                    )
                                total_bets_placed += 1
                    elif delta_team <= -game_edge_threshold:
                        implied_prob = 1.0 / under_kf
                        win_prob = min(implied_prob + (abs(delta_team) * 0.025), 0.90)
                        edge = win_prob - implied_prob
                        if edge >= win_edge_threshold:
                            cursor.execute(
                                "SELECT COUNT(*) FROM virtual_bets WHERE date = ? AND match_name = ? AND player_name = ? AND market = ? AND selection = ? AND is_preliminary = ?",
                                (target_date, match_name, team_target, "TEAM_TOTAL", "МЕНЬШЕ", prelim_flag))
                            if cursor.fetchone()[0] == 0 and 1.91 <= under_kf <= 2.15:
                                print(f"  🔥 ВАЛУЙ: ИТ МЕНЬШЕ {team_target} (Линия: {pt} | Кэф: {under_kf})")
                                coupon_id = None
                                cursor.execute(
                                    '''INSERT INTO virtual_bets (date, match_name, market, category, player_name, line, prediction, selection, kf, vip_kf, bet_amount, published_at, is_preliminary, coupon_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                    (target_date, match_name, "TEAM_TOTAL", "ТОТАЛ", team_target, pt, team_proj, "МЕНЬШЕ",
                                     under_kf, under_kf, base_bet_amount, published_at, prelim_flag, coupon_id))
                                if prelim_flag == 0:
                                    cursor.execute(
                                        '''INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge) VALUES (?, ?, ?, ?, ?, ?)''',
                                        (match_name, "TEAM_TOTAL", f"{team_target} | МЕНЬШЕ", pt, under_kf, round(edge * 100, 2))
                                    )
                                total_bets_placed += 1
                elif abs(pt) < 35 and pt != 0:
                    if pt > 0 and over_kf >= 1.75 and pt > max_f1:
                        max_f1, opt_f1 = pt, (f_id, pt, over_kf)
                    elif pt < 0 and under_kf >= 1.75 and abs(pt) > max_f2:
                        max_f2, opt_f2 = abs(pt), (f_id, pt, under_kf)

            if opt_f1:
                f_id, pt, over_kf = opt_f1
                delta = expected_margin_t1 + pt
                if delta > 0:
                    implied_prob = 1.0 / over_kf
                    win_prob = min(implied_prob + (abs(delta) * 0.025), 0.90)
                    edge = win_prob - implied_prob
                    if edge >= win_edge_threshold:
                        cursor.execute(
                            "SELECT COUNT(*) FROM virtual_bets WHERE date = ? AND match_name = ? AND market = ? AND selection = ? AND is_preliminary = ?",
                            (target_date, match_name, "HANDICAP", "ФОРА 1", prelim_flag))
                        if cursor.fetchone()[0] == 0:
                            # ДОБАВЛЕН ВЫВОД В КОНСОЛЬ
                            print(f"  🔥 ВАЛУЙ: ФОРА 1 (Линия: {pt} | Кэф: {over_kf}) | Edge: {edge * 100:.1f}%")

                            coupon_id = None
                            cursor.execute(
                                '''INSERT INTO virtual_bets (date, match_name, market, category, player_name, line, prediction, selection, kf, vip_kf, bet_amount, published_at, is_preliminary, coupon_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                (target_date, match_name, "HANDICAP", "ИСХОД", "GAME", pt, expected_margin_t1, "ФОРА 1",
                                 over_kf, over_kf, base_bet_amount, published_at, prelim_flag, coupon_id))
                            if prelim_flag == 0:
                                cursor.execute(
                                    '''INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge) VALUES (?, ?, ?, ?, ?, ?)''',
                                    (match_name, "HANDICAP", "GAME | ФОРА 1", pt, over_kf, round(edge * 100, 2))
                                )
                            total_bets_placed += 1

            if opt_f2:
                f_id, pt, under_kf = opt_f2
                delta = expected_margin_t1 + pt
                if delta < 0:
                    implied_prob = 1.0 / under_kf
                    win_prob = min(implied_prob + (abs(delta) * 0.025), 0.90)
                    edge = win_prob - implied_prob
                    if edge >= win_edge_threshold:
                        cursor.execute(
                            "SELECT COUNT(*) FROM virtual_bets WHERE date = ? AND match_name = ? AND market = ? AND selection = ? AND is_preliminary = ?",
                            (target_date, match_name, "HANDICAP", "ФОРА 2", prelim_flag))
                        if cursor.fetchone()[0] == 0:
                            # ДОБАВЛЕН ВЫВОД В КОНСОЛЬ
                            print(f"  🔥 ВАЛУЙ: ФОРА 2 (Линия: {pt} | Кэф: {under_kf}) | Edge: {edge * 100:.1f}%")

                            coupon_id = None
                            cursor.execute(
                                '''INSERT INTO virtual_bets (date, match_name, market, category, player_name, line, prediction, selection, kf, vip_kf, bet_amount, published_at, is_preliminary, coupon_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                (target_date, match_name, "HANDICAP", "ИСХОД", "GAME", pt, expected_margin_t1, "ФОРА 2",
                                 under_kf, under_kf, base_bet_amount, published_at, prelim_flag, coupon_id))
                            if prelim_flag == 0:
                                cursor.execute(
                                    '''INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge) VALUES (?, ?, ?, ?, ?, ?)''',
                                    (match_name, "HANDICAP", "GAME | ФОРА 2", pt, under_kf, round(edge * 100, 2))
                                )
                            total_bets_placed += 1

            # === ВТОРОЙ ЦИКЛ: ПОКАЗАТЕЛИ ИГРОКОВ (С РЕНТГЕНОМ) ===
            cursor.execute("""
                SELECT event_id, factor_id, line, over_kf, under_kf, market_type, player_or_team
                FROM odds_history
                WHERE (event_id = ? OR parent_event_id = ?)
                AND market_type LIKE 'PLAYER_%' AND market_type != 'PLAYER_H2H'
            """, (match_id, str(match_id)))
            player_odds = cursor.fetchall()

            for sub_id, f_id, pt, over_kf, under_kf, market_name, p_name_ru in player_odds:
                db_name = current_player_map.get(re.sub(r'\s+', '', p_name_ru.strip()))

                # РЕНТГЕН 1: Проверка маппинга
                if not db_name:
                    print(f"  ⚠️ [СЛЕПАЯ ЗОНА] Нет маппинга для имени: '{p_name_ru}'")
                    continue

                proj_data = t1_preds.get(db_name) or t2_preds.get(db_name)

                # РЕНТГЕН 2: Проверка наличия проекции
                if not proj_data:
                    print(f"  ⚠️ [СЛЕПАЯ ЗОНА] Нет проекции для: '{db_name}'")
                    continue

                # РЕНТГЕН 3: Вывод ВСЕХ ID маркетов, которые сейчас дает букмекер
                print(f"  🔍 [DEBUG] Игрок: {db_name} | ID маркета: {f_id} | Линия: {pt} | Кэфы: {over_kf}/{under_kf}")

                is_valid_market = False
                proj_value = 0.0
                db_market_name = ""
                delta_threshold = 0.0

                if market_name == "PLAYER_PTS" or f_id in PLAYER_POINTS_IDS:
                    is_valid_market, proj_value, db_market_name, delta_threshold = True, proj_data.get('pts',
                                                                                                       0) if isinstance(
                        proj_data, dict) else proj_data, "PLAYER_PTS", 1.0
                elif market_name == "PLAYER_REB" or f_id in PLAYER_REBOUNDS_IDS:
                    is_valid_market, proj_value, db_market_name, delta_threshold = True, proj_data.get('reb',
                                                                                                       0) if isinstance(
                        proj_data, dict) else 0, "PLAYER_REB", 0.5
                elif market_name == "PLAYER_FG3M" or f_id in PLAYER_THREES_IDS:
                    is_valid_market, proj_value, db_market_name, delta_threshold = True, proj_data.get('fg3m',
                                                                                                       0) if isinstance(
                        proj_data, dict) else 0, "PLAYER_FG3M", 0.35
                elif market_name == "PLAYER_AST" or f_id in PLAYER_ASSISTS_IDS:
                    is_valid_market, proj_value, db_market_name, delta_threshold = True, proj_data.get('ast',
                                                                                                       0) if isinstance(
                        proj_data, dict) else 0, "PLAYER_AST", 0.5

                if is_valid_market:
                    delta = proj_value - pt
                    # Проверка дельты (разницы)
                    if abs(delta) >= delta_threshold:
                        selection = "БОЛЬШЕ" if delta > 0 else "МЕНЬШЕ"
                        target_kf = over_kf if delta > 0 else under_kf

                        # ИСПРАВЛЕНИЕ: Расширенный фильтр кэфов для игроков (1.70 - 2.30)
                        if 1.70 <= target_kf <= 2.30:
                            implied_prob = 1.0 / target_kf

                            edge_mult = 0.05
                            if db_market_name in ["PLAYER_REB", "PLAYER_AST"]:
                                edge_mult = 0.15
                            elif db_market_name == "PLAYER_FG3M":
                                edge_mult = 0.25

                            win_prob = min(implied_prob + (abs(delta) * edge_mult), 0.90)
                            edge = win_prob - implied_prob

                            if edge >= player_edge_threshold:
                                cursor.execute(
                                    "SELECT COUNT(*) FROM virtual_bets WHERE date = ? AND match_name = ? AND player_name = ? AND market = ? AND selection = ? AND is_preliminary = ?",
                                    (target_date, match_name, db_name, db_market_name, selection, prelim_flag))
                                if cursor.fetchone()[0] > 0: continue

                                print(
                                    f"  🔥 ВАЛУЙ НА ИГРОКА: {db_name} Маркет: {db_market_name} Выбор: {selection} (Линия: {pt} | Кэф: {target_kf}) | Edge: {edge * 100:.1f}%")
                                coupon_id = None
                                published_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                cursor.execute(
                                    '''INSERT INTO virtual_bets (date, match_name, market, category, player_name, line, prediction, selection, kf, vip_kf, bet_amount, published_at, is_preliminary, coupon_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                    (target_date, match_name, db_market_name, "ИГРОК", db_name, pt, proj_value, selection,
                                     target_kf, target_kf, base_bet_amount, published_at, prelim_flag, coupon_id))
                                if prelim_flag == 0:
                                    cursor.execute(
                                        '''INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge) VALUES (?, ?, ?, ?, ?, ?)''',
                                        (match_name, db_market_name, f"{db_name} | {selection}", pt, target_kf, round(edge * 100, 2))
                                    )
                                total_bets_placed += 1
                            else:
                                print(
                                    f"  🚫 СКИП ({db_name}): Edge слишком мал ({edge * 100:.1f}% < {player_edge_threshold * 100:.1f}%)")
                        else:
                            print(f"  🚫 СКИП ({db_name}): Кэф {target_kf} не прошел фильтр 1.70-2.30. Выбор: {selection}")
                    else:
                        print(f"  🚫 СКИП ({db_name}): Дельта ({proj_value:.1f} vs {pt}) меньше {delta_threshold} очка.")

            # === ТРЕТИЙ ЦИКЛ: ДУЭЛИ ИГРОКОВ (H2H) ===
            cursor.execute("""
                SELECT factor_id, line, over_kf, under_kf, player_or_team, event_id
                FROM odds_history
                WHERE market_type = 'PLAYER_H2H' AND (event_id = ? OR parent_event_id = ?)
            """, (match_id, str(match_id)))
            local_h2h_lines = cursor.fetchall()

            for f_id, pt, over_kf, under_kf, p_name_ru, sub_id in local_h2h_lines:
                if ' - ' not in p_name_ru:
                    continue

                p1_ru, p2_ru = p_name_ru.split(' - ', 1)

                # Resolve player 1
                norm_p1 = re.sub(r'\s+', '', p1_ru.strip())
                db_p1 = current_player_map.get(norm_p1)
                if not db_p1:
                    combined_roster = list(set(base1 + base2))
                    if norm_p1 not in failed_api_resolves:
                        db_p1 = auto_resolve_player_name(p1_ru, norm_p1, combined_roster, current_player_map)
                        if not db_p1:
                            failed_api_resolves.add(norm_p1)

                # Resolve player 2
                norm_p2 = re.sub(r'\s+', '', p2_ru.strip())
                db_p2 = current_player_map.get(norm_p2)
                if not db_p2:
                    combined_roster = list(set(base1 + base2))
                    if norm_p2 not in failed_api_resolves:
                        db_p2 = auto_resolve_player_name(p2_ru, norm_p2, combined_roster, current_player_map)
                        if not db_p2:
                            failed_api_resolves.add(norm_p2)

                if not db_p1 or not db_p2:
                    continue

                proj_data1 = t1_preds.get(db_p1) or t2_preds.get(db_p1)
                proj_data2 = t1_preds.get(db_p2) or t2_preds.get(db_p2)

                if not proj_data1 or not proj_data2:
                    continue

                p1_team = t1_abbr if db_p1 in base1 else t2_abbr
                p2_team = t1_abbr if db_p2 in base1 else t2_abbr
                is_same_team = (p1_team == p2_team)

                pts1 = proj_data1.get('pts', 0) if isinstance(proj_data1, dict) else proj_data1
                pts2 = proj_data2.get('pts', 0) if isinstance(proj_data2, dict) else proj_data2

                h2h_handicap, h2h_total = WNBAMathCore.calculate_h2h_duels(pts1, pts2, is_same_team)

                is_handicap = f_id in HANDICAP_IDS

                proj_value = h2h_handicap if is_handicap else h2h_total
                delta = proj_value - pt

                if abs(delta) >= 1.0:
                    selection = "БОЛЬШЕ" if delta > 0 else "МЕНЬШЕ"
                    if is_handicap:
                        selection = "ФОРА 1" if delta > 0 else "ФОРА 2"

                    target_kf = over_kf if delta > 0 else under_kf

                    if 1.75 <= target_kf <= 2.15:
                        implied_prob = 1.0 / target_kf
                        win_prob = min(implied_prob + (abs(delta) * 0.05), 0.90)
                        edge = win_prob - implied_prob

                        if edge >= 0.10:  # > 10% edge required for H2H
                            h2h_display_name = f"{db_p1} vs {db_p2}"
                            cursor.execute(
                                "SELECT COUNT(*) FROM virtual_bets WHERE date = ? AND match_name = ? AND player_name = ? AND market = ? AND selection = ? AND is_preliminary = ?",
                                (target_date, match_name, h2h_display_name, "PLAYER_H2H", selection, prelim_flag))
                            if cursor.fetchone()[0] == 0:
                                print(
                                    f"  🔥 ВАЛУЙ ДУЭЛЬ: {h2h_display_name} | {selection} (Линия: {pt} | Кэф: {target_kf}) | Прогноз: {proj_value:.1f} | Edge: {edge * 100:.1f}%")
                                coupon_id = None
                                published_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                                cursor.execute(
                                    '''INSERT INTO virtual_bets (date, match_name, market, category, player_name, line, prediction, selection, kf, vip_kf, bet_amount, published_at, is_preliminary, coupon_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                    (target_date, match_name, "PLAYER_H2H", "ИГРОК", h2h_display_name, pt, proj_value,
                                     selection, target_kf, target_kf, base_bet_amount, published_at, prelim_flag,
                                     coupon_id))
                                if prelim_flag == 0:
                                    cursor.execute(
                                        '''INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge) VALUES (?, ?, ?, ?, ?, ?)''',
                                        (match_name, "PLAYER_H2H", f"{h2h_display_name} | {selection}", pt, target_kf, round(edge * 100, 2))
                                    )
                                total_bets_placed += 1

        except Exception as e:
            import traceback
            print(f"  ⚠️ Ошибка при обработке матча {team1_ru} - {team2_ru}: {e}")
            traceback.print_exc()
    conn.commit()
    conn.close()
    print(f"\n✅ АНАЛИЗ ЗАВЕРШЕН. Сделано ставок: {total_bets_placed}")


if __name__ == "__main__":
    run_predictor()