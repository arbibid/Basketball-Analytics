# Version: 6.4 (Smart Scheduling, Dynamic Mappings)
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging
import asyncio
import datetime
from database.db_manager import DBManager
from config import Config
from data_collectors.wnba_scraper import WNBAScraper
from core.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)

# ИСПРАВЛЕНИЕ 2: Удален хардкод маппинга команд. Используем динамический из Config.
mappings = Config.get_mappings()
TEAM_MAP = mappings.get("TEAM_MAP", {})

notifier = TelegramNotifier()


def process_settlements():
    logger.info("--- Запуск локального процесса расчета ставок (process_settlements) ---")
    db = DBManager()
    conn = db.get_connection()
    c = conn.cursor()

    c.execute(
        "SELECT id, date, match_name, market, player_name, line, selection, kf, bet_amount FROM virtual_bets WHERE status = 'PENDING' AND date <= date('now')")
    pending_bets = c.fetchall()

    if not pending_bets:
        logger.info("Нет ставок со статусом PENDING в базе данных.")
        conn.close()
        return

    logger.info(f"Найдено {len(pending_bets)} нерассчитанных ставок. Начинаем проверку...")

    for bet in pending_bets:
        bet_id, b_date, match_name, market, player_name, line, selection, kf, amount = bet
        actual_result = None
        is_dnp = False

        # Пропускаем матчи из будущего, чтобы не спамить в лог
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        if b_date > today_str:
            continue

        logger.info(f"Проверка ставки ID {bet_id}: {market} | {match_name} | Цель: {player_name} | Линия: {line}")

        try:
            t1_ru, t2_ru = match_name.split(" - ")
            t1_eng = TEAM_MAP.get(t1_ru.strip())
            t2_eng = TEAM_MAP.get(t2_ru.strip())

            c.execute('''
                SELECT away_score, home_score, game_id, away_team, home_team
                FROM matches
                WHERE (away_team = ? OR home_team = ?) AND (away_team = ? OR home_team = ?)
                AND (date = ? OR date = date(?, '-1 day') OR date = date(?, '+1 day'))
            ''', (t1_eng, t1_eng, t2_eng, t2_eng, b_date, b_date, b_date))
            match_res = c.fetchone()
        except Exception as e:
            logger.error(f"Ошибка поиска матча {match_name} в БД: {repr(e)}")
            match_res = None

        if match_res and match_res[0] is not None and match_res[1] is not None:
            away_score = float(match_res[0])
            home_score = float(match_res[1])
            game_id = match_res[2]
            away_team = match_res[3]
            home_team = match_res[4]
            game_total = away_score + home_score
            logger.debug(f"Матч найден в БД. GameID: {game_id}, Общий тотал: {game_total}")

            # Резервный подсчет тотала, если счет нулевой
            if game_total == 0:
                c.execute("SELECT SUM(pts) FROM player_stats WHERE game_id = ?", (game_id,))
                sum_res = c.fetchone()
                if sum_res and sum_res[0]:
                    game_total = sum_res[0]
                    logger.debug(f"Тотал пересчитан по статистике игроков: {game_total}")

            if game_total == 0:
                logger.warning(f"Матч {match_name} найден, но очки равны 0. Скрапер еще не стянул данные?")
                continue

            if market == "PLAYER_PTS":
                c.execute('SELECT pts FROM player_stats WHERE player_name = ? AND game_id = ?', (player_name, game_id))
                p_res = c.fetchone()
                if p_res: actual_result = float(p_res[0])
                else: is_dnp = True

            elif market == "GAME_TOTAL":
                if game_total > 0: actual_result = float(game_total)

            elif market == "TEAM_TOTAL":
                team_abbr = TEAM_MAP.get(player_name.strip())
                if team_abbr == home_team: actual_result = float(home_score)
                elif team_abbr == away_team: actual_result = float(away_score)

            elif market == "HANDICAP":
                if t1_eng == home_team: actual_result = float(home_score - away_score)
                else: actual_result = float(away_score - home_score)

            elif market == "PLAYER_H2H":
                if " vs " in player_name:
                    p1, p2 = player_name.split(" vs ")
                    c.execute('SELECT pts FROM player_stats WHERE player_name = ? AND game_id = ?', (p1.strip(), game_id))
                    p1_res = c.fetchone()
                    c.execute('SELECT pts FROM player_stats WHERE player_name = ? AND game_id = ?', (p2.strip(), game_id))
                    p2_res = c.fetchone()

                    if p1_res and p2_res:
                        pts1, pts2 = float(p1_res[0]), float(p2_res[0])
                        if selection in ["ФОРА 1", "ФОРА 2"]: actual_result = pts1 - pts2
                        else: actual_result = pts1 + pts2
                    else:
                        is_dnp = True

        else:
            logger.warning(f"Матч {match_name} еще не появился в локальной таблице matches. Ждем скрапера.")

        if is_dnp:
            actual_result = 'DNP'

        if actual_result is not None:
            status = "PENDING"
            profit = 0.0

            # Защита от пустых значений (None), из-за которых падают расчеты
            safe_amount = float(amount) if amount is not None else 100.0
            safe_kf = float(kf) if kf is not None else 1.0

            if is_dnp:
                status = "REFUND"
                profit = 0.0
            else:
                if selection == "ФОРА 1":
                    if actual_result + line > 0: status, profit = "WON", safe_amount * (safe_kf - 1)
                    elif actual_result + line == 0: status, profit = "REFUND", 0.0
                    else: status, profit = "LOST", -safe_amount
                elif selection == "ФОРА 2":
                    if actual_result + line < 0: status, profit = "WON", safe_amount * (safe_kf - 1)
                    elif actual_result + line == 0: status, profit = "REFUND", 0.0
                    else: status, profit = "LOST", -safe_amount
                else:
                    if actual_result == line: status, profit = "REFUND", 0.0
                    elif (selection == "БОЛЬШЕ" and actual_result > line) or (selection == "МЕНЬШЕ" and actual_result < line):
                        status, profit = "WON", safe_amount * (safe_kf - 1)
                    else: status, profit = "LOST", -safe_amount

            logger.info(f"Ставка ID {bet_id} рассчитана! Статус: {status}, Профит: {profit}")

            # 1. СРАЗУ сохраняем всё в БД (включая ФАКТ и Профит)
            c.execute(
                "UPDATE virtual_bets SET status = ?, actual_result = ?, profit = ? WHERE id = ?",
                (status, actual_result if not is_dnp else 0, profit, bet_id)
            )
            # 2. КОММИТИМ каждую ставку индивидуально ДО отправки уведомления!
            conn.commit()

            # 3. Отправляем уведомление
            marker = "✅" if status == "WON" else "❌" if status == "LOST" else "🔄"

            # Умное скрытие строки "Цель" для общих тоталов и фор
            target_str = ""
            if market not in ["GAME_TOTAL", "HANDICAP"]:
                target_str = f"🎯 <b>Цель:</b> {player_name}\n"

            if is_dnp:
                msg = (
                    f"{marker} <b>РАСЧЕТ [{market}]</b>\n"
                    f"🏀 <b>Матч:</b> {match_name}\n"
                    f"{target_str}"
                    f"Игрок не вышел на паркет (DNP) -> <b>{status}</b> (Возврат ставки)"
                )
            else:
                msg = (
                    f"{marker} <b>РАСЧЕТ [{market}]</b>\n"
                    f"🏀 <b>Матч:</b> {match_name}\n"
                    f"{target_str}"
                    f"📈 <b>Линия:</b> {line} | <b>Выбор:</b> {selection}\n"
                    f"📊 <b>ФАКТ:</b> {actual_result}\n"
                    f"💰 <b>Статус:</b> {status} (Профит: {profit:.2f} RUB)"
                )
            notifier.send_simple_alert(msg)

    conn.commit()
    conn.close()
    logger.info("--- Цикл расчета (process_settlements) завершен ---")


async def settle_bets_loop():
    logger.info("Демон расчетов запущен и готов к работе...")
    db = DBManager()

    while True:
        try:
            current_time = datetime.datetime.now()
            logger.info("Проверка необходимости запуска расчетов...")

            run_scraper = False

            # ИСПРАВЛЕНИЕ 1: Smart Scheduling (запуск только через 2.5 часа после начала любого нерассчитанного матча или контрольный прогон в 12:00)
            conn = db.get_connection()
            try:
                c = conn.cursor()
                c.execute("SELECT match_date FROM match_tracking WHERE status NOT IN ('SETTLED', 'COMPLETED')")
                active_matches = c.fetchall()

                for (match_date_str,) in active_matches:
                    if match_date_str:
                        try:
                            match_dt = datetime.datetime.strptime(match_date_str, "%Y-%m-%d %H:%M:%S")
                            if current_time > match_dt + datetime.timedelta(hours=2, minutes=30):
                                run_scraper = True
                                break
                        except ValueError:
                            # Fallback if date is just YYYY-MM-DD
                            try:
                                match_dt = datetime.datetime.strptime(match_date_str, "%Y-%m-%d")
                                if current_time > match_dt + datetime.timedelta(days=1):
                                    run_scraper = True
                                    break
                            except ValueError:
                                pass
            finally:
                conn.close()

            # Контрольный прогон раз в сутки (например, между 12:00 и 12:05)
            if 12 == current_time.hour and 0 <= current_time.minute <= 5:
                run_scraper = True

            if run_scraper:
                logger.info("Условия для скрапинга выполнены (матч завершен или контрольный прогон). Запускаем WNBAScraper...")
                try:
                    scraper = WNBAScraper()
                    await scraper.run()
                except Exception as scrape_err:
                    logger.error(f"Сбой WNBAScraper при загрузке статистики: {repr(scrape_err)}")

                # 2. Выполняем логику расчёта
                await asyncio.to_thread(process_settlements)
            else:
                logger.info("Рано для расчетов (матчи еще идут или не начинались). Спим...")

            if run_scraper:
                # 3. Обновляем статус матчей, если все ставки по ним рассчитаны
                conn = db.get_connection()
                try:
                    c = conn.cursor()

                    # Ищем матчи в match_tracking, статус которых не SETTLED и не COMPLETED
                    c.execute("SELECT team1, team2 FROM match_tracking WHERE status NOT IN ('SETTLED', 'COMPLETED')")
                    active_matches = c.fetchall()

                    for t1_ru, t2_ru in active_matches:
                        match_name = f"{t1_ru} - {t2_ru}"

                        # Проверяем, есть ли PENDING ставки для этого матча
                        c.execute("SELECT count(*) FROM virtual_bets WHERE match_name = ? AND status = 'PENDING'", (match_name,))
                        pending_count = c.fetchone()[0]

                        # Проверяем, есть ли вообще ставки для этого матча
                        c.execute("SELECT count(*) FROM virtual_bets WHERE match_name = ?", (match_name,))
                        total_count = c.fetchone()[0]

                        if pending_count == 0 and total_count > 0:
                            c.execute(
                                "UPDATE match_tracking SET status = 'SETTLED' WHERE team1 = ? AND team2 = ?",
                                (t1_ru, t2_ru)
                            )
                            logger.info(f"Матч {match_name} полностью рассчитан, статус изменен на SETTLED.")

                    conn.commit()
                finally:
                    conn.close()

        except Exception as e:
            # exc_info=True выведет полную простыню ошибки с номером строки
            logger.error(f"Критическая ошибка в главном цикле демона расчетов: {repr(e)}", exc_info=True)

        await asyncio.sleep(300)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    asyncio.run(settle_bets_loop())