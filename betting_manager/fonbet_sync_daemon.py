# Version: 6.2 (Dynamic Mappings Update)
import logging
import requests
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from database.db_manager import DBManager
from core.telegram_notifier import TelegramNotifier

# Настраиваем красивый и понятный вывод логов
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("FonbetSyncDaemon")
notifier = TelegramNotifier()


def process_sync():
    logger.info("--- Запуск цикла синхронизации с БК (Fonbet/Melbet) ---")
    db = DBManager()
    conn = db.get_connection()
    c = conn.cursor()

    # Ищем только те ставки, у которых есть купон реальной БК
    c.execute(
        "SELECT id, date, match_name, market, player_name, line, selection, kf, bet_amount, coupon_id FROM virtual_bets WHERE status = 'PENDING' AND coupon_id IS NOT NULL")
    pending_bets = c.fetchall()

    if not pending_bets:
        logger.info("Нет ожидающих ставок с привязанным coupon_id. Синхронизация не требуется.")
        conn.close()
        return

    logger.info(f"Найдено {len(pending_bets)} ставок с купонами для проверки в БК.")

    try:
        from config import Config
        mappings = Config.get_mappings()
        TEAM_MAP = mappings.get("TEAM_MAP", {})
    except ImportError:
        logger.error("Не удалось импортировать Config и TEAM_MAP. Проверьте пути.")
        conn.close()
        return

    for bet in pending_bets:
        bet_id, b_date, match_name, market, player_name, line, selection, kf, amount, coupon_id = bet
        actual_result = None
        is_dnp = False

        logger.info(f"Проверка купона [{coupon_id}] | Ставка ID: {bet_id} | Матч: {match_name}")

        try:
            t1_ru, t2_ru = match_name.split(" - ")
            t1_eng = TEAM_MAP.get(t1_ru.strip())
            t2_eng = TEAM_MAP.get(t2_ru.strip())

            c.execute('''
                SELECT away_score, home_score, game_id
                FROM matches
                WHERE (away_team = ? OR home_team = ?) AND (away_team = ? OR home_team = ?)
                AND (date = ? OR date = date(?, '-1 day') OR date = date(?, '+1 day'))
            ''', (t1_eng, t1_eng, t2_eng, t2_eng, b_date, b_date, b_date))
            match_res = c.fetchone()
        except Exception as e:
            logger.error(f"Ошибка поиска матча {match_name} в БД: {repr(e)}")
            match_res = None

        if match_res and match_res[0] is not None and match_res[1] is not None:
            game_id = match_res[2]

            if market == "PLAYER_PTS":
                c.execute('SELECT pts FROM player_stats WHERE player_name = ? AND game_id = ?', (player_name, game_id))
                p_res = c.fetchone()
                if p_res:
                    actual_result = float(p_res[0])
                else:
                    is_dnp = True
                    actual_result = "DNP"
            elif market in ("GAME_TOTAL", "TEAM_TOTAL", "HANDICAP"):
                if match_res[0] + match_res[1] > 0:
                    actual_result = float(match_res[0] + match_res[1])
        else:
            logger.debug(f"Матч {match_name} пока не завершен в локальной БД. Ждем данных.")

        # API Request к серверу букмекера
        if actual_result is not None or is_dnp:
            url = "https://clientsapi-lb54-w.bk6bba-resources.com/coupon/info"
            payload = {"regId": coupon_id, "lang": "ru"}
            headers = {
                'accept': '*/*',
                'content-type': 'text/plain;charset=UTF-8',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }

            logger.info(f"Отправка запроса к API БК для купона {coupon_id}...")

            try:
                response = requests.post(url, headers=headers, json=payload, timeout=15)

                if response.status_code == 200:
                    data = response.json()
                    bk_state = data.get("state")
                    logger.info(f"Ответ БК получен! Статус купона в БК: {bk_state}")

                    math_status = "PENDING"
                    if is_dnp or actual_result == line:
                        math_status = "REFUND"
                    elif market in ("GAME_TOTAL", "TEAM_TOTAL", "PLAYER_PTS"):
                        if (selection == "БОЛЬШЕ" and (actual_result != "DNP" and actual_result > line)) or \
                                (selection == "МЕНЬШЕ" and (actual_result != "DNP" and actual_result < line)):
                            math_status = "WON"
                        else:
                            math_status = "LOST"
                    elif market == "HANDICAP":
                        math_status = "PENDING"

                    if bk_state:
                        bk_status = "WON" if str(bk_state).lower() == "won" else "LOST" if str(
                            bk_state).lower() == "lost" else "REFUND"

                        logger.info(f"Сверка: Наш расчет [{math_status}] <--> Расчет БК [{bk_status}]")

                        if math_status != "PENDING" and math_status != bk_status:
                            logger.warning(f"ОБНАРУЖЕНО РАСХОЖДЕНИЕ! Купон {coupon_id}")
                            notifier.send_simple_alert(
                                f"⚠️ Расхождение с БК по купону {coupon_id}. Наш расчет: {math_status} (Счет: {actual_result}), БК: {bk_status}")

                        profit = amount * (kf - 1) if bk_status == "WON" else -amount if bk_status == "LOST" else 0.0

                        # Фиксируем результат от БК в нашей базе
                        c.execute("UPDATE virtual_bets SET status = ?, actual_result = ?, profit = ? WHERE id = ?",
                                  (bk_status, actual_result if not is_dnp else 0, profit, bet_id))
                        logger.info(f"Ставка ID {bet_id} успешно обновлена статусом от БК: {bk_status}")

                else:
                    logger.error(f"Ошибка API БК: HTTP {response.status_code}")

            except requests.exceptions.RequestException as e:
                logger.error(f"Сетевая ошибка при запросе к БК: {repr(e)}")
            except Exception as e:
                logger.error(f"Непредвиденная ошибка при обработке купона {coupon_id}: {repr(e)}", exc_info=True)

    conn.commit()
    conn.close()
    logger.info("--- Цикл синхронизации завершен ---")


if __name__ == "__main__":
    logger.info("Аудитор-демон (Fonbet Sync) успешно запущен!")
    while True:
        try:
            process_sync()
        except Exception as e:
            logger.error(f"Критическая ошибка в главном цикле: {repr(e)}", exc_info=True)

        logger.info("Ожидание 5 минут до следующей проверки...")
        time.sleep(300)