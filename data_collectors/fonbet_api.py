# Version: 2.2 (Security Fix: removed hardcoded credentials)
import os
import sys
import requests
import json
import logging
import time
import re
from playwright.sync_api import sync_playwright

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config import Config

logger = logging.getLogger("FonbetAPI")


class FonbetAPI:
    def __init__(self):
        self.session = requests.Session()
        self.client_id = None
        self.fsid = None
        self.sys_id = 21
        self.device_id = "LOCAL_17809857911380756574482360"
        self.base_url = "https://clientsapi-lb54-w.bk6bba-resources.com"

        self.headers = {
            'accept': '*/*',
            'origin': 'https://fon.bet',
            'referer': 'https://fon.bet/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36'
        }
        self.session.headers.update(self.headers)

    def login(self) -> bool:
        """Авторизация через Playwright и захват токенов из localStorage"""
        logger.info("🌐 [FonbetAPI] Открываем браузер для авто-логина...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--headless=new'])
                context = browser.new_context(viewport={"width": 1280, "height": 720})
                page = context.new_page()
                page.goto("https://fon.bet/")
                logger.info("⏳ [FonbetAPI] Загружаем главную страницу...")
                page.wait_for_load_state("networkidle")

                try:
                    logger.info("🔑 [FonbetAPI] Выполняем вход...")
                    page.click('a:has-text("Войти"), button:has-text("Войти")')
                    page.wait_for_timeout(2000)
                    page.locator('input[type="text"], input[type="tel"]').first.fill(Config.BETTING_PHONE)
                    page.locator('input[type="password"]').first.fill(Config.BETTING_PASSWORD)
                    page.wait_for_timeout(1000)
                    page.click('button[type="submit"], button:has-text("Войти")')
                    page.wait_for_timeout(10000)
                except Exception as e:
                    logger.warning(f"⚠️ [FonbetAPI] Авто-логин требует внимания (капча?). Ждем 30 сек: {e}")
                    page.wait_for_timeout(30000)

                # Вытаскиваем куки и localStorage
                playwright_cookies = context.cookies()
                local_storage = page.evaluate("() => JSON.stringify(localStorage)")
                browser.close()

                # Парсим токены регулярками (они могут быть в куках или в кэше)
                dump_str = json.dumps(playwright_cookies) + local_storage
                match_cid = re.search(r'headerApi\.cid["\':\s\\]*(\d+)', dump_str)
                match_fsid = re.search(r'headerApi\.FSID["\':\s\\]*([A-Za-z0-9_-]+)', dump_str)

                if match_cid: self.client_id = match_cid.group(1)
                if match_fsid: self.fsid = match_fsid.group(1)

                cookies_dict = {c['name']: c['value'] for c in playwright_cookies}
                self.session.cookies.update(cookies_dict)

                if self.client_id and self.fsid:
                    logger.info(f"✅ [FonbetAPI] Токены успешно захвачены! CID: {self.client_id}")
                    return True
                else:
                    logger.error("❌ [FonbetAPI] Токены не найдены!")
                    return False
        except Exception as e:
            logger.error(f"❌ [FonbetAPI] Ошибка Playwright: {e}")
            return False

    def _check_coupon_limits(self, event_id: int, factor_id: int, param: int):
        url = f'{self.base_url}/coupon/betSlipInfo'
        payload = {
            "lang": "ru", "scopeMarketId": "1600",
            "bets": [{"eventId": event_id, "factorId": factor_id, "param": param}],
            "fsid": self.fsid, "sysId": self.sys_id, "clientId": self.client_id
        }
        response = self.session.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if "sums" in data:
                return True, data.get("K")
        return False, None

    def _get_request_id(self):
        url = f'{self.base_url}/coupon/betRequestId'
        payload = {
            "lang": "ru", "CDI": 499, "deviceId": self.device_id,
            "fsid": self.fsid, "sysId": self.sys_id, "clientId": self.client_id
        }
        response = self.session.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            return response.json().get("requestId")
        return None

    def place_bet(self, event_id: int, factor_id: int, param: int, amount: float) -> dict:
        result = {'success': False, 'ticket_id': None, 'actual_kf': None}

        if not self.client_id or not self.fsid:
            if not self.login():
                return result

        is_valid, current_k = self._check_coupon_limits(event_id, factor_id, param)
        if not is_valid:
            logger.error(f"🚫 [FonbetAPI] Отмена: купон не прошел проверку.")
            return result

        req_id = self._get_request_id()
        if not req_id: return result

        logger.info(f"🚀 [FonbetAPI] Отправляем {amount} руб. на кэф {current_k}...")
        url = f'{self.base_url}/coupon/bet'
        payload = {
            "requestId": req_id, "lang": "ru", "fsid": self.fsid, "sysId": int(self.sys_id),
            "clientId": int(self.client_id),
            "coupon": {
                "amount": amount, "flexBet": "up", "flexParam": False, "mirror": "https://fon.bet",
                "bets": [{"num": 1, "event": int(event_id), "factor": int(factor_id), "value": float(current_k),
                          "param": int(param), "zone": "es"}]
            }
        }

        try:
            response = self.session.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if "coupon" in data and data["coupon"].get("resultCode") == 0:
                    ticket_id = data["coupon"]["regId"]
                    logger.info(f"🔥🔥🔥 [FonbetAPI] УСПЕХ! Купон: {ticket_id}")
                    result.update({'success': True, 'ticket_id': str(ticket_id), 'actual_kf': float(current_k)})
                else:
                    logger.warning(f"⚠️ [FonbetAPI] Отказ сервера: {data}")
            else:
                logger.error(f"❌ [FonbetAPI] Ошибка сети: {response.status_code}")
        except Exception as e:
            logger.error(f"❌ [FonbetAPI] Исключение: {e}")

        return result