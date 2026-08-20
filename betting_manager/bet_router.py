# Version: 1.0 (Умный Маршрутизатор Ордеров)
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
        c.execute("SELECT match_name, market_type, target, line, expected_kf FROM bet_signals WHERE id = ?",
                  (signal_id,))
        signal = c.fetchone()
        conn.close()

        if not signal:
            logger.error(f"❌ Сигнал {signal_id} не найден в БД!")
            return False

        match_name, market_type, target, line, expected_kf = signal
        amount = float(self.db.get_system_setting('base_bet_amount', 50.0))

        logger.info(f"🚀 Инициирую пробитие: {match_name} | {target} {line} в БК: {bookmaker}")

        # 2. Вызываем нужный API
        result = {'success': False}
        if bookmaker == 'BETCITY':
            # Здесь нам понадобится смаппить наш ID матча на ID Бетсити (сделаем это чуть позже)
            # Временно заглушка для логики
            logger.info("  👉 Отправка запроса в BetcityAPI...")
            # result = self.betcity.place_bet(event_id="ТУТ_МАППИНГ", bet_type=target, line=line, amount=amount)

            # Имитация успешного ответа для тестов:
            result = {'success': True, 'ticket_id': f'TEST_BC_{signal_id}', 'actual_kf': expected_kf}

        elif bookmaker == 'FONBET':
            logger.info("  👉 Отправка запроса в FonbetAPI...")
            # result = self.fonbet.place_bet(...)

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