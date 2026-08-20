# Version: 6.1
import requests
import json
import time


class FonbetAPI:
    def __init__(self):
        # Твои токены авторизации (сохраняем актуальные из логов)
        self.fsid = "cAG86rHc4MN4eXYOJdg5ZDfr"
        self.client_id = 7931422
        self.sys_id = 21
        self.device_id = "LOCAL_17809857911380756574482360"

        # Базовый URL серверов Фонбета (балансировщик)
        self.base_url = "https://clientsapi-lb54-w.bk6bba-resources.com"

        self.headers = {
            'accept': '*/*',
            'accept-language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'content-type': 'text/plain;charset=UTF-8',
            'origin': 'https://fon.bet',
            'referer': 'https://fon.bet/',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36'
        }

    def check_coupon_limits(self, event_id: int, factor_id: int, param: int):
        """Проверяет купон, лимиты и забирает актуальный кэф перед ставкой"""
        url = f'{self.base_url}/coupon/betSlipInfo'
        payload = {
            "lang": "ru",
            "bets": [{"eventId": event_id, "factorId": factor_id, "param": param}],
            "fsid": self.fsid,
            "sysId": self.sys_id,
            "clientId": self.client_id,
            "scopeMarketId": "1600"
        }

        print("🔍 [API] Проверяем актуальность линии...")
        response = requests.post(url, headers=self.headers, data=json.dumps(payload))

        if response.status_code == 200:
            data = response.json()
            if "sums" in data:
                current_k = data.get("K")
                print(
                    f"✅ [API] Купон валиден! Кэф: {current_k} | Лимиты: {data['sums']['min']} - {data['sums']['max']} руб.")
                return True, current_k
        print(f"❌ [API] Ошибка валидации: {response.text}")
        return False, None

    def get_request_id(self):
        """Генерирует уникальный ID транзакции для ставки"""
        url = f'{self.base_url}/coupon/betRequestId'
        payload = {
            "lang": "ru",
            "fsid": self.fsid,
            "sysId": self.sys_id,
            "clientId": self.client_id,
            "CDI": 499,
            "deviceId": self.device_id
        }
        response = requests.post(url, headers=self.headers, data=json.dumps(payload))
        if response.status_code == 200:
            return response.json().get("requestId")
        return None

    def place_bet(self, event_id: int, factor_id: int, param: int, amount: float):
        """Полный цикл простановки ставки за доли секунды"""

        # 1. Забираем кэф и проверяем лимиты
        is_valid, current_k = self.check_coupon_limits(event_id, factor_id, param)
        if not is_valid:
            print("🚫 Отмена ставки: купон не прошел проверку.")
            return None

        # 2. Получаем токен транзакции
        req_id = self.get_request_id()
        if not req_id:
            print("🚫 Отмена ставки: не удалось получить betRequestId.")
            return None

        print(f"🚀 [API] Отправляем {amount} руб. на кэф {current_k}...")

        # 3. Финальный POST-запрос списания
        url = f'{self.base_url}/coupon/bet'
        payload = {
            "requestId": req_id,
            "lang": "ru",
            "coupon": {
                "amount": amount,
                "flexBet": "up",
                "flexParam": False,
                "mirror": "https://fon.bet",
                "bets": [
                    {
                        "num": 1,
                        "event": int(event_id),  # ПРИНУДИТЕЛЬНО int
                        "factor": int(factor_id),  # ПРИНУДИТЕЛЬНО int
                        "value": float(current_k),  # float
                        "param": int(param),  # ПРИНУДИТЕЛЬНО int
                        "zone": "es"
                    }
                ]
            },
            "fsid": self.fsid,
            "sysId": int(self.sys_id),
            "clientId": int(self.client_id)
        }

        response = requests.post(url, headers=self.headers, data=json.dumps(payload))

        if response.status_code == 200:
            result = response.json()
            if "coupon" in result and result["coupon"].get("resultCode") == 0:
                coupon_data = result["coupon"]
                print(f"🔥🔥🔥 УСПЕХ! Ставка принята. Купон: {coupon_data['regId']}")
                print(f"💰 Баланс: {coupon_data['clientSaldo']} руб.")
                return coupon_data['regId']
            else:
                print(f"⚠️ [API] Отказ сервера (изменение кэфа/линии): {result}")
                return None
        else:
            print(f"❌ [API] Ошибка сети при ставке: {response.status_code}")
            return None


if __name__ == "__main__":
    # Оставляем класс для импорта, но блокируем случайную тестовую ставку
    pass
    # bettor = FonbetAPI()
    # bettor.place_bet(event_id=66509605, factor_id=1803, param=18950, amount=30.0)