# Version: 1.4 (Added WNBA ID mapping for player prop betting)
import os
import sys
import urllib.parse
from curl_cffi import requests
import logging
import json
import time
import uuid
from playwright.sync_api import sync_playwright

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config import Config
from utils.mapping_manager import MappingManager

logger = logging.getLogger("BetcityAPI")


class BetcityAPI:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome120")
        self.token = None
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://betcity.ru",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        self.session.headers.update(self.headers)
        self.mapping_manager = MappingManager()

    def login(self) -> bool:
        """Авторизация через Playwright и захват токена/кук"""
        logger.info("🌐 [BetcityAPI] Запуск браузера...")
        try:
            with sync_playwright() as p:
                # 1. ВЫКЛЮЧАЕМ НЕВИДИМКУ (headless=False), чтобы видеть капчи и ошибки!
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    user_agent=self.headers["User-Agent"]
                )
                page = context.new_page()

                logger.info("⏳ [BetcityAPI] Загружаем главную страницу...")

                # 2. Изолируем загрузку страницы.
                # wait_until="commit" означает, что мы ждем только первый ответ сервера, а не полной загрузки всех скриптов.
                try:
                    page.goto("https://betcity.ru/ru/", wait_until="commit", timeout=45000)
                    page.wait_for_timeout(5000)  # Даем 5 секунд на визуальную отрисовку
                except Exception as e:
                    logger.warning(f"⚠️ [BetcityAPI] Страница грузится слишком долго, но пробуем прорваться дальше...")

                try:
                    logger.info("🔑 [BetcityAPI] Выполняем вход...")

                    # Кликаем на Вход
                    login_btn = page.locator('text="Вход"').first
                    login_btn.click(timeout=10000)
                    page.wait_for_timeout(2000)

                    # Вводим данные
                    page.locator('input[type="text"], input[type="tel"]').first.fill(Config.BETTING_PHONE)
                    page.locator('input[type="password"]').first.fill(Config.BETTING_PASSWORD)
                    page.wait_for_timeout(1000)

                    # Жмем Войти
                    page.locator('button:has-text("Войти"), button:has-text("Вход")').first.click()

                    logger.info("⏳ [BetcityAPI] Ждем получения токена (5 сек)...")
                    page.wait_for_timeout(5000)

                except Exception as e:
                    logger.warning(f"⚠️ [BetcityAPI] Бот не нашел кнопку. Делаю снимок экрана...")
                    try:
                        page.screenshot(path="betcity_error.png", timeout=5000)
                    except Exception as e_snap:
                        logger.error(f"⚠️ Даже скриншот завис: {e_snap}")
                    logger.warning(f"⚠️ Скриншот сохранен как 'betcity_error.png'. Ошибка: {e}")

                playwright_cookies = context.cookies()
                browser.close()

                cookies_dict = {}
                for cookie in playwright_cookies:
                    cookies_dict[cookie['name']] = cookie['value']
                    if cookie['name'] == "tk":
                        self.token = urllib.parse.unquote(cookie['value'])

                if self.token:
                    self.session.cookies.update(cookies_dict)
                    logger.info("✅ [BetcityAPI] Токен успешно захвачен.")
                    return True
                else:
                    logger.warning("⚠️ Токена нет! Делаю финальный снимок экрана...")
                    page.screenshot(path="betcity_no_token.png")
                    logger.error("❌ [BetcityAPI] Токен не найден! Скриншот сохранен как 'betcity_no_token.png'.")
                    return False
        except Exception as e:
            logger.error(f"❌ [BetcityAPI] Ошибка Playwright: {e}")
            return False

    def _find_target(self, event_data: dict, bet_type: str, target_line: float, wnba_id: int = None):
        """Парсит JSON линии и ищет нужный исход"""
        try:
            chmps = event_data.get('reply', {}).get('sports', {}).get('3', {}).get('chmps', {})
            for chmp_id, chmp_data in chmps.items():
                evts = chmp_data.get('evts', {})
                for ev_id, ev_data in evts.items():
                    for section in ['main', 'ext']:
                        if section not in ev_data: continue
                        for market_id, market_data in ev_data[section].items():
                            inner_data = market_data.get('data', {})
                            for inner_ev_id, ev_blocks in inner_data.items():
                                blocks = ev_blocks.get('blocks', {})
                                for b_name, b_data in blocks.items():

                                    if wnba_id is not None:
                                        # If wnba_id is provided, check if it matches the current block's name
                                        parsed_wnba_id = self.mapping_manager.get_wnba_id(b_name, "BETCITY")
                                        if parsed_wnba_id != wnba_id:
                                            continue

                                    # Универсальная логика для Плееров (Подборы, Очки и тд)
                                    # Универсальная логика для Плееров (Подборы, Очки и тд)
                                    if target_line is not None:
                                        for key, val in b_data.items():
                                            if isinstance(val, dict) and 'kf' in val and 'ps' in val:
                                                line_key = key.replace('Kf_', '')
                                                if line_key in b_data:
                                                    line_val = b_data[line_key]
                                                    # ЗАЩИТА: проверяем, что это число, а не системный словарь
                                                    if isinstance(line_val, (int, float, str)):
                                                        try:
                                                            if float(line_val) == float(target_line):
                                                                return ev_id, val['ps'], val['kf'], line_val
                                                        except (ValueError, TypeError):
                                                            pass
        except Exception as e:
            logger.error(f"❌ [BetcityAPI] Ошибка парсинга линии: {e}")
        return None, None, None, None

    def _extract_bets_list(self, bets_obj):
        """Внутренний метод для поиска номера купона в истории"""
        bet_list = []
        if isinstance(bets_obj, dict):
            for k, v in bets_obj.items():
                if isinstance(v, dict):
                    v['_raw_key'] = k
                    bet_list.append(v)
        elif isinstance(bets_obj, list):
            bet_list = [b for b in bets_obj if isinstance(b, dict)]

        parsed_bets = []
        for idx, b in enumerate(bet_list):
            bet_id = 0
            for key in ["bnum", "id_num", "id_purch", "id_bet", "id_head", "id_bt", "id"]:
                val = b.get(key)
                if val is not None and str(val).isdigit():
                    candidate = int(val)
                    if candidate > 100:
                        bet_id = candidate
                        break
                    elif bet_id == 0 and candidate > 0:
                        bet_id = candidate

            if bet_id == 0:
                raw_k = b.get('_raw_key')
                if raw_k and str(raw_k).isdigit():
                    bet_id = int(raw_k)
                else:
                    bet_id = 999000 + idx
            parsed_bets.append((bet_id, b))
        return sorted(parsed_bets, key=lambda x: x[0], reverse=True)

    def place_bet(self, event_id: str, bet_type: str, line: float, amount: float, wnba_id: int = None) -> dict:
        """Главный метод: оформляет ставку и возвращает результат"""
        result = {'success': False, 'ticket_id': None, 'actual_kf': None}

        if not self.token:
            if not self.login():
                return result

        self.session.headers.update({"Referer": f"https://betcity.ru/ru/line/basketball/1498/{event_id}"})

        try:
            # 1. Загрузка линии
            ext_url = "https://ad.betcity.ru/d/off/events?rev=6&ext=1&add=dep_events&ver=87&csn=ooca9s"
            resp_ext = self.session.post(ext_url, data=f"ids_ev={event_id}", timeout=10).json()

            target_ev_id, pos, kf, actual_line = self._find_target(resp_ext, bet_type, line, wnba_id)

            if not pos:
                logger.error(f"❌ [BetcityAPI] Исход {bet_type} {line} не найден в линии!")
                return result

            logger.info(f"🎯 [BetcityAPI] Цель: POS {pos}, Кэф {kf}. Очищаем корзину...")

            # 2. Очистка и добавление в корзину
            self.session.post(f"https://hdr.betcity.ru/d/basket/del_all?token={self.token}&ver=87", timeout=10)

            add_url = "https://hdr.betcity.ru/d/basket/add"
            params_add = {
                "sys": 1, "id": target_ev_id, "pos": pos, "k": kf, "ts": 0, "lv": actual_line,
                "token": self.token, "tum": f"{int(time.time() * 1000)}_1", "ver": 87, "csn": "1"
            }
            data_add = {"cart": "{}", "settings": '{"remember_bet":true,"clear_cart":true,"cart_kf_type":"0"}'}
            resp_add = self.session.post(add_url, params=params_add, data=data_add, timeout=10).json()

            bsks = resp_add.get("reply", {}).get("bsks", [])
            if not bsks:
                logger.error("❌ [BetcityAPI] Ошибка добавления в корзину.")
                return result

            # 3. Оформление Checkout
            logger.info(f"🚀 [BetcityAPI] Отправляем ордер на {amount} RUB...")
            bet_key = f"{target_ev_id}_{pos}"
            payload = {
                bet_key: {"id_ev": int(target_ev_id), "ps": int(pos), "kf": float(kf), "is_live": 0, "t": bsks[0]["t"],
                          "s": bsks[0]["s"], "fora": str(actual_line)}}

            checkout_url = "https://hdr.betcity.ru/d/basket/checkout"
            params_chk = {"type": 0, "ts": resp_add["reply"]["ts"], "token": self.token,
                          "tum": f"{int(time.time() * 1000)}_2", "ver": 87, "csn": "1"}
            data_chk = {
                "data": json.dumps(payload),
                "settings": '{"clear_cart":false}',
                "context": f'{{"api_method":"checkout","max_bet":{amount},"shown_balance":{amount}}}',
                "uuid": str(uuid.uuid4()),
                f"bets[{bet_key}]": str(amount)
            }

            resp_chk = self.session.post(checkout_url, params=params_chk, data=data_chk, timeout=10).json()
            reply_chk = resp_chk.get("reply", {})

            # 4. Проверка результата
            if "id_bet" in reply_chk:
                result.update({'success': True, 'ticket_id': str(reply_chk['id_bet']), 'actual_kf': float(kf)})
                logger.info(f"🔥 [BetcityAPI] УСПЕХ! Ставка принята. Купон: {result['ticket_id']}")
                return result

            elif reply_chk.get("status") == 0:
                interval = reply_chk.get("interval_list", 7)
                logger.info(f"⏳ [BetcityAPI] Холд сервера {interval} сек. Ждем...")
                time.sleep(interval + 1)

                # Ищем купон в истории
                hist_url = f"https://hdr.betcity.ru/d/user/current?per_page=10&page=1&rev=1&token={self.token}&ver=87&csn=1"
                resp_hist = self.session.get(hist_url, timeout=10).json()

                if resp_hist.get("ok"):
                    sorted_bets = self._extract_bets_list(resp_hist.get("reply", {}).get("bets", {}))
                    if sorted_bets:
                        result.update({'success': True, 'ticket_id': str(sorted_bets[0][0]), 'actual_kf': float(kf)})
                        logger.info(f"🔥 [BetcityAPI] УСПЕХ (Из истории)! Купон: {result['ticket_id']}")
                        return result

            logger.error(f"❌ [BetcityAPI] Сервер отклонил ставку: {resp_chk}")
            return result

        except Exception as e:
            logger.error(f"❌ [BetcityAPI] Сетевая ошибка: {e}")
            return result