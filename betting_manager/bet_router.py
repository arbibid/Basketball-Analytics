# Version: 1.1 (Умный Маршрутизатор Ордеров, wnba_id propagation)
import os
import sys
import logging
import time

# Подтягиваем пути
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database.db_manager import DBManager
from data_collectors.betcity_api import BetcityAPI
from data_collectors.fonbet_api import FonbetAPI
from data_collectors.pari_api import PariAPI

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BetRouter")


class BetRouter:
    def __init__(self):
        self.db = DBManager()
        self.betcity = BetcityAPI()
        # self.fonbet = FonbetAPI()

    def _execute_bet(self, signal_id: int, bookmaker: str):
        """Метод физической простановки ставки через API"""
        # 1. Получаем данные сигнала из БД
        conn = self.db.get_connection()
        c = conn.cursor()
        c.execute("SELECT match_name, market_type, target, line, expected_kf, wnba_id FROM bet_signals WHERE id = ?",
                  (signal_id,))
        signal = c.fetchone()
        conn.close()

        if not signal:
            logger.error(f"❌ Сигнал {signal_id} не найден в БД!")
            return False

        match_name, market_type, target, line, expected_kf, wnba_id = signal
        amount = float(self.db.get_system_setting('base_bet_amount', 50.0))

        logger.info(f"🚀 Инициирую пробитие: {match_name} | {target} {line} в БК: {bookmaker}")

        # 2. Вызываем нужный API
        result = {'success': False}
        if bookmaker == 'BETCITY':
            logger.info("  👉 Отправка запроса в BetcityAPI (Playwright)...")

            # Вытягиваем реальный event_id Бетсити для этого маркета из истории
            conn = self.db.get_connection()
            c = conn.cursor()
            c.execute("""
                        SELECT event_id FROM odds_history 
                        WHERE bookmaker = 'BETCITY' AND market_type = ? AND ABS(line - ?) < 0.01
                        ORDER BY timestamp DESC LIMIT 1
                    """, (market_type, line))
            row = c.fetchone()
            conn.close()

            bc_event_id = row[0] if row else None

            if not bc_event_id:
                logger.error(f"❌ Не удалось найти event_id Бетсити для маркета {market_type} {line}!")
                return False

            # Жестко фиксируем 100 рублей для нашего главного теста
            test_amount = 100.0
            logger.info(f"  🔥 ЗАПУСК PLAYWRIGHT... Цель: event {bc_event_id}, Сумма: {test_amount} RUB")

            # РУБИЛЬНИК ВКЛЮЧЕН: Вызываем настоящий метод простановки ставки!
            kwargs = {'event_id': bc_event_id, 'bet_type': target, 'line': line, 'amount': test_amount}
            if wnba_id and ('PLAYER' in market_type):
                kwargs['wnba_id'] = wnba_id
            result = self.betcity.place_bet(**kwargs)

            # (Тестовая заглушка удалена)

        elif bookmaker == 'FONBET':
            logger.info("  👉 Отправка запроса в FonbetAPI...")
            # kwargs = {'event_id': ..., 'bet_type': target, 'line': line, 'amount': test_amount}
            # if wnba_id and ('PLAYER' in market_type):
            #     kwargs['wnba_id'] = wnba_id
            # result = self.fonbet.place_bet(**kwargs)

            # Имитация успешного ответа для тестов:
            result = {'success': True, 'ticket_id': f'TEST_FON_{signal_id}', 'actual_kf': expected_kf}

        # 3. Сохраняем результат
        if result.get('success'):
            self.db.save_real_order(
                order_id=result['ticket_id'],
                bookmaker=bookmaker,
                item_name=f"{match_name} | {target} {line}",
                price=result['actual_kf'],
                amount=amount
            )
            self.db.update_signal_status(signal_id, 'PROCESSED')
            logger.info(f"✅ УСПЕХ: Сигнал {signal_id} отработан!")
            return True
        else:
            self.db.update_signal_status(signal_id, 'FAILED')
            logger.error(f"❌ ПРОВАЛ: БК {bookmaker} отклонила ставку по сигналу {signal_id}.")
            return False

    def process_manual_queue(self):
        """Метод для Telegram-бота: Ищет сигналы, на которые Админ нажал кнопку 'Пробить'"""
        conn = self.db.get_connection()
        c = conn.cursor()
        # Ищем сигналы, где статус изменен кнопкой из Телеграма
        c.execute("SELECT id, status, bookmaker FROM bet_signals WHERE status IN ('EXECUTE_A', 'EXECUTE_B', 'EXECUTE_MANUAL')")
        tasks = c.fetchall()
        conn.close()

        for sig_id, status, db_bookmaker in tasks:
            if status == 'EXECUTE_MANUAL':
                bookmaker = db_bookmaker
            else:
                bookmaker = 'BETCITY' if status == 'EXECUTE_A' else 'FONBET'
            self._execute_bet(sig_id, bookmaker)

    def run_auto_mode(self):
        """Полностью автоматический режим: сам выбирает лучшую линию и бьет"""
        signals = self.db.get_ready_signals()
        for sig in signals:
            sig_id, match_name, market_type, target, line, expected_kf, edge, status, _ = sig

            # Правило 1: Эксклюзивность
            if market_type == 'PLAYER_H2H':
                logger.info(f"🤖 AUTO: Дуэль игроков, маршрутизирую строго в BETCITY")
                self._execute_bet(sig_id, 'BETCITY')
                continue

            # Правило 2: Line Shopping (Поиск лучшего кэфа в odds_history)
            # ТУТ БУДЕТ ЗАПРОС К БД ДЛЯ СРАВНЕНИЯ КЭФОВ
            # ...
            # Временно шлем все остальные в Фонбет
            logger.info(f"🤖 AUTO: Стандартный маркет, маршрутизирую в FONBET")
            self._execute_bet(sig_id, 'FONBET')


def start_daemon():
    router = BetRouter()
    db = DBManager()

    logger.info("🛡️ BetRouter Daemon запущен и слушает базу...")

    while True:
        try:
            mode = db.get_system_setting('betting_mode', 'MANUAL')

            if mode == 'AUTO':
                router.run_auto_mode()
            else:
                # В ручном режиме Роутер просто ждет, пока Телеграм-бот поменяет статус на EXECUTE
                router.process_manual_queue()

            time.sleep(3)
        except Exception as e:
            logger.error(f"Сбой в цикле маршрутизатора: {e}")
            time.sleep(10)


if __name__ == "__main__":
    start_daemon()