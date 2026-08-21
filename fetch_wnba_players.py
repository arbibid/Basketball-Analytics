import os
import sys
import json
from curl_cffi import requests

sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from config import Config


def fetch_all_wnba_players():
    url = "https://stats.nba.com/stats/commonallplayers"

    params = {
        "LeagueID": "10",
        "Season": "2024",
        # 0 = выгрузить всю историю лиги
        "IsOnlyCurrentSeason": "0"
    }

    headers = {
        "Origin": "https://www.nba.com",
        "Referer": "https://www.nba.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9"
    }

    proxies = {}
    if hasattr(Config, 'PROXY_URL') and Config.PROXY_URL:
        proxies = {
            "http": Config.PROXY_URL,
            "https": Config.PROXY_URL
        }
        print(f"🌍 Подключен прокси: {Config.PROXY_URL}")

    print("⏳ Запрашиваем ПОЛНУЮ базу WNBA без фильтра текущего ростера...")
    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            impersonate="chrome120",
            proxies=proxies,
            timeout=20
        )

        if response.status_code != 200:
            print(f"❌ Ошибка API: {response.status_code}")
            return

        data = response.json()

        headers_list = data['resultSets'][0]['headers']
        players_data = data['resultSets'][0]['rowSet']

        id_idx = headers_list.index("PERSON_ID")
        name_idx = headers_list.index("DISPLAY_FIRST_LAST")
        team_idx = headers_list.index("TEAM_ABBREVIATION")
        # API отдает год последнего матча игрока
        to_year_idx = headers_list.index("TO_YEAR")

        players_db = {}

        for row in players_data:
            player_id = str(row[id_idx])
            name = row[name_idx]
            team = row[team_idx]
            to_year = str(row[to_year_idx])

            # Фильтр: берем всех, кто играл хотя бы с 2023 года.
            # Это гарантированно захватит весь текущий ростер, травмированных и свободных агентов.
            if to_year >= "2023":
                players_db[player_id] = {
                    "name_en": name,
                    "team": team if team else "FA",  # Если без команды, помечаем как Free Agent
                    "last_active": to_year
                }

        output_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'config', 'wnba_official_players.json'))
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(players_db, f, ensure_ascii=False, indent=4)

        print(f"✅ Успешно! Спарсено {len(players_db)} актуальных игроков WNBA (2023-2024).")
        print(f"📁 База сохранена: {output_path}")

    except Exception as e:
        print(f"❌ Произошла ошибка при парсинге: {e}")


if __name__ == "__main__":
    fetch_all_wnba_players()