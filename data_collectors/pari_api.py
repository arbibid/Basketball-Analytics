# Version: 5.2 (Security Fix: removed hardcoded credentials)
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

logger = logging.getLogger("PariAPI")


class PariAPI:
    def __init__(self):
        self.session = requests.Session()
        self.client_id = None
        self.fsid = None
        self.sys_id = 21
        self.device_id = "47B027965382F4F272BB9DE6EB542ACA"
        self.base_url = "https://clientsapi61.pb06e2-resources.ru"

        self.headers = {
            'accept': '*/*',
            'origin': 'https://pari.ru',
            'referer': 'https://pari.ru/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36'
        }
        self.session.headers.update(self.headers)

    def login(self) -> bool:
        """Авторизация через Playwright и захват токенов из localStorage"""
        logger.info("🌐 [PariAPI] Открываем браузер для авто-логина...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=['--headless=new'])
                context = browser.new_context(viewport={"width": 1280, "height": 720})
                page = context.new_page()
                page.goto("https://pari.ru/")
                logger.info("⏳ [PariAPI] Загружаем главную страницу...")
                page.wait_for_load_state("networkidle")

                try:
                    logger.info("🔑 [PariAPI] Выполняем вход...")
                    page.click('a:has-text("Войти"), button:has-text("Войти")')
                    page.wait_for_timeout(2000)
                    page.locator('input[type="text"], input[type="tel"]').first.fill(Config.BETTING_PHONE)
                    page.locator('input[type="password"]').first.fill(Config.BETTING_PASSWORD)
                    page.wait_for_timeout(1000)
                    page.click('button[type="submit"], button:has-text("Войти")')
                    page.wait_for_timeout(10000)
                except Exception as e:
                    logger.warning(f"⚠️ [PariAPI] Авто-логин требует внимания. Ждем 30 сек: {e}")
                    page.wait_for_timeout(30000)

                playwright_cookies = context.cookies()
                local_storage = page.evaluate("() => JSON.stringify(localStorage)")
                browser.close()

                dump_str = json.dumps(playwright_cookies) + local_storage
                match_cid = re.search(r'headerApi\.cid["\':\s\\]*(\d+)', dump_str)
                match_fsid = re.search(r'headerApi\.FSID["\':\s\\]*([A-Za-z0-9_-]+)', dump_str)

                if match_cid: self.client_id = match_cid.group(1)
                if match_fsid: self.fsid = match_fsid.group(1)

                cookies_dict = {c['name']: c['value'] for c in playwright_cookies}
                self.session.cookies.update(cookies_dict)

                if self.client_id and self.fsid:
                    logger.info(f"✅ [PariAPI] Токены успешно захвачены! CID: {self.client_id}")
                    return True
                else:
                    logger.error("❌ [PariAPI] Токены не найдены!")
                    return False
        except Exception as e:
            logger.error(f"❌ [PariAPI] Ошибка Playwright: {e}")
            return False

    def place_bet(self, event_id: str, factor_id: str, param: int, amount: float, expected_kf: float) -> dict:
        result = {'success': False, 'ticket_id': None, 'actual_kf': None}

        if not self.client_id or not self.fsid:
            if not self.login():
                return result

        url = f"{self.base_url}/coupon/bet"
        payload = {
            "requestId": int(time.time() * 1000), "lang": "ru",
            "fsid": self.fsid, "sysId": self.sys_id, "clientId": int(self.client_id),
            "coupon": {
                "amount": float(amount), "flexBet": "up", "flexParam": False, "mirror": "https://pari.ru",
                "bets": [{"num": 1, "event": int(event_id), "factor": int(factor_id), "value": float(expected_kf),
                          "param": int(param), "zone": "es"}]
            }
        }

        try:
            logger.info(f"🚀 [PariAPI] Отправляем {amount} руб. на кэф {expected_kf}...")
            resp = self.session.post(url, json=payload, timeout=15)

            if not resp.text.strip():
                logger.error("❌ [PariAPI] Сервер вернул пустой ответ (купон отклонен).")
                return result

            resp.raise_for_status()
            data = resp.json()

            if "coupon" in data and data["coupon"].get("resultCode") == 0:
                ticket_id = data["coupon"].get("regId")
                logger.info(f"🔥🔥🔥 [PariAPI] УСПЕХ! Купон: {ticket_id}")
                result.update({'success': True, 'ticket_id': str(ticket_id), 'actual_kf': float(expected_kf)})
            else:
                logger.warning(f"⚠️ [PariAPI] Отказ сервера (изменение кэфа/линии): {data}")

        except Exception as e:
            logger.error(f"❌ [PariAPI] Ошибка сети при ставке: {e}")

        return result