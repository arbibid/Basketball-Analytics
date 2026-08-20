# Version: 6.1
import os
import requests

class TelegramNotifier:
    def __init__(self):
        from config import Config
        self.bot_token = Config.TG_BOT_TOKEN
        self.admin_id = Config.ADMIN_ID

        # Determine notification channel/users here
        # For now, it sends to ADMIN_ID, but can be expanded
        self.target_chat = self.admin_id

    def _send_message(self, text, parse_mode="HTML", reply_markup=None):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        target_chats = [self.admin_id]
        try:
            from database.db_manager import DBManager
            db = DBManager()
            all_users = db.get_all_users()
            vip_users = [u for u in all_users if u[5] == 1]  # is_vip

            if vip_users:
                for user in vip_users:
                    if user[0] not in target_chats:
                        target_chats.append(user[0])
        except Exception as e:
            print(f"Failed to load VIP users for notifications: {e}")

        from config import Config
        proxies = None
        if Config.PROXY_URL:
            proxies = {
                "http": Config.PROXY_URL,
                "https": Config.PROXY_URL
            }

        for chat_id in target_chats:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup

            try:
                if proxies:
                    requests.post(url, json=payload, proxies=proxies, timeout=10)
                else:
                    requests.post(url, json=payload, timeout=10)
            except Exception as e:
                print(f"Telegram Notification Error for chat {chat_id}: {e}")

    def send_match_cart(self, match_id, match_name, cart_items, proj1=None, proj2=None):
        if not cart_items:
            return

        # Separate items based on tier requirements
        pro_items = [item for item in cart_items if item['market'] in ('PLAYER_PTS', 'PLAYER_REB', 'PLAYER_FG3M')]
        standard_items = [item for item in cart_items if item['market'] not in ('PLAYER_PTS', 'PLAYER_REB', 'PLAYER_FG3M')]

        # Sort items by edge descending
        pro_items.sort(key=lambda x: x.get('edge', 0), reverse=True)
        standard_items.sort(key=lambda x: x.get('edge', 0), reverse=True)

        team1, team2 = match_name.split(" - ", 1)
        base_text = f"✅ <b>Готов анализ матча:</b>\n🏀 {match_name}\n"
        if proj1 is not None and proj2 is not None:
            base_text += f"📊 Расчетный счет: {team1} [<b>{int(round(proj1))} - {int(round(proj2))}</b>] {team2}\n"

        # Construct specific messages
        pro_text = base_text + f"\nНайдено валуев (Pro/AutoPilot): <b>{len(pro_items) + len(standard_items)}</b>\n\n💎 <b>ТОП-3 ПРОГНОЗА:</b>\n"
        standard_text = base_text + f"\nНайдено валуев (Standard): <b>{len(standard_items)}</b>\n\n💎 <b>ТОП-3 ПРОГНОЗА:</b>\n"

        combined_all = (pro_items + standard_items)
        combined_all.sort(key=lambda x: x.get('edge', 0), reverse=True)
        top_3_pro = combined_all[:3]
        top_3_standard = standard_items[:3]

        for i, item in enumerate(top_3_pro, 1):
            pro_text += f"{i}. {item['market']} | {item['selection']} {item['line']} | Кэф: <b>{item['kf']}</b> (Edge: {item['edge']*100:.1f}%)\n"

        for i, item in enumerate(top_3_standard, 1):
            standard_text += f"{i}. {item['market']} | {item['selection']} {item['line']} | Кэф: <b>{item['kf']}</b> (Edge: {item['edge']*100:.1f}%)\n"

        reply_markup = {
            "inline_keyboard": [
                [{"text": "📋 Полная роспись", "callback_data": f"match_{match_id}"}]
            ]
        }

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        try:
            from database.db_manager import DBManager
            db = DBManager()
            all_users = db.get_all_users()
        except Exception as e:
            print(f"Failed to load users for notifications: {e}")
            all_users = []

        # Add Admin explicitly
        users_to_notify = [{'user_id': self.admin_id, 'tier': 'autopilot'}]
        for u in all_users:
            if u[0] != self.admin_id and (u[5] == 1 or u[6] != 'free'):  # u[5] is_vip, u[6] tier
                tier_val = u[6] if len(u) > 6 else 'free'
                users_to_notify.append({'user_id': u[0], 'tier': tier_val})

        from config import Config
        proxies = None
        if Config.PROXY_URL:
            proxies = {
                "http": Config.PROXY_URL,
                "https": Config.PROXY_URL
            }

        sent_ids = set()
        for user in users_to_notify:
            chat_id = user['user_id']
            if chat_id in sent_ids:
                continue
            sent_ids.add(chat_id)

            tier = user.get('tier', 'free')
            # Fallback for old vip logic
            if tier == 'free' and any(u[0] == chat_id and u[5] == 1 for u in all_users):
                tier = 'pro'

            if tier in ('pro', 'autopilot'):
                msg_text = pro_text
            elif tier == 'standard':
                msg_text = standard_text
            else:
                continue

            payload = {
                "chat_id": chat_id,
                "text": msg_text,
                "parse_mode": "HTML",
                "reply_markup": reply_markup
            }

            try:
                if proxies:
                    requests.post(url, json=payload, proxies=proxies, timeout=10)
                else:
                    requests.post(url, json=payload, timeout=10)
            except Exception as e:
                print(f"Telegram Notification Error for chat {chat_id}: {e}")

    def send_settlement_report(self, report_text):
        self._send_message(f"💰 <b>Отчет о расчете ставок</b>\n\n{report_text}")

    def send_simple_alert(self, text):
        self._send_message(text)
