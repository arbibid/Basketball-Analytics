import asyncio
import os
import json
import re
import requests
import PyPDF2
import logging
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database.db_manager import DBManager
from config import Config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_DIR = os.path.join(PROJECT_ROOT, "data", "raw_boxscores")
PDF_DIR = os.path.join(PROJECT_ROOT, "data", "gamebooks")

os.makedirs(JSON_DIR, exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)


def get_playwright_proxy():
    proxy_url = Config.PROXY_URL
    if not proxy_url:
        return None
    clean = proxy_url.replace("http://", "").replace("https://", "")
    if "@" in clean:
        auth, host = clean.split("@")
        user, password = auth.split(":")
        return {"server": f"http://{host.strip()}", "username": user.strip(), "password": password.strip()}
    return {"server": f"http://{clean.strip()}"}


class WNBAScraper:
    def __init__(self):
        self.db = DBManager()
        self.db.init_db()

    def _extract_from_pdf(self, pdf_path):
        metadata = {"refs": ["Не указан", "Не указан", "Не указан"], "attendance": "0", "duration": "0:00"}
        if not os.path.exists(pdf_path): return metadata
        try:
            with open(pdf_path, 'rb') as file:
                reader = PyPDF2.PdfReader(file)
                text = reader.pages[0].extract_text().replace('\n', ' ')
                officials_match = re.search(r"Officials:\s*(.*?)(?=OFFICIAL SCORER|VISITOR|Game Duration)", text)
                attendance_match = re.search(r"Attendance:\s*([0-9,]+)", text)
                duration_match = re.search(r"Game Duration:\s*([0-9:]+)", text)
                if officials_match:
                    ref_list = [r.strip() for r in officials_match.group(1).strip().split(',') if r.strip()]
                    for i in range(min(3, len(ref_list))): metadata["refs"][i] = ref_list[i]
                if attendance_match: metadata["attendance"] = attendance_match.group(1).strip()
                if duration_match: metadata["duration"] = duration_match.group(1).strip()
        except Exception as e:
            pass
        return metadata

    def _download_pdf(self, game_id, date_str, away_team, home_team):
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        dates_to_try = [(date_obj - timedelta(days=i)).strftime("%Y%m%d") for i in range(6)]
        teams_to_try = [f"{away_team}{home_team}", f"{home_team}{away_team}"]

        filename = os.path.join(PDF_DIR, f"{game_id}_book.pdf")
        if os.path.exists(filename): return filename

        print(f"    [>] PDF не найден. Ищу на серверах NBA...")
        proxies = {"http": Config.PROXY_URL, "https": Config.PROXY_URL} if Config.PROXY_URL else None

        for d in dates_to_try:
            for t in teams_to_try:
                url = f"{Config.NBA_PDF_BASE_URL}{d}/{d}_{t}_book.pdf"
                try:
                    resp = requests.get(url, proxies=proxies, timeout=10)
                    if resp.status_code == 200:
                        with open(filename, 'wb') as f: f.write(resp.content)
                        print(f"    [+] УСПЕХ: PDF скачан ({d}_{t})")
                        return filename
                except Exception:
                    pass
        print(f"    [-] ОШИБКА: PDF для {game_id} не найден.")
        return filename

    async def run(self):
        print("🚀 Инициализация скрапера WNBA...")
        proxy_config = get_playwright_proxy()

        async with async_playwright() as p:
            # Скрытый режим включен, чтобы не мозолить глаза
            browser = await p.chromium.launch(headless=False, proxy=proxy_config)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                extra_http_headers={"Referer": "https://www.wnba.com/"},
                viewport={"width": 780, "height": 600}
            )
            page = await context.new_page()

            print("📅 Запрашиваю календарь...")
            await page.goto(Config.WNBA_SCHEDULE_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(4)

            print("🎛 Переключаю фильтр на ALL...")
            await page.evaluate('''() => {
                const checkbox = document.querySelector('input#color_mode');
                const label = document.querySelector('label[for="color_mode"]');
                if (checkbox && !checkbox.checked && label) {
                    label.click();
                } else {
                    const buttons = Array.from(document.querySelectorAll('button, a, div, label, span'));
                    const allBtn = buttons.find(b => b.textContent && b.textContent.trim().toUpperCase() === 'ALL');
                    if (allBtn) allBtn.click();
                }
            }''')
            await asyncio.sleep(3)

            print("📜 Листаю страницу вниз...")
            for _ in range(5):
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                await asyncio.sleep(1)

            links = await page.evaluate('''() => {
                const anchors = Array.from(document.querySelectorAll('a[href*="/game/"]'));
                const finalUrls = new Set();
                anchors.forEach(a => {
                    let node = a; let isFinal = false;
                    for (let i=0; i<6; i++) {
                        if (node && node.innerText && node.innerText.toUpperCase().includes('FINAL')) { isFinal = true; break; }
                        if (node) node = node.parentElement;
                    }
                    if (isFinal) finalUrls.add(a.getAttribute('href'));
                });
                return Array.from(finalUrls);
            }''')

            raw_ids = [re.search(r'(\d{10})', href).group(1) for href in links if re.search(r'(\d{10})', href)]
            game_ids = sorted(list(set([gid for gid in raw_ids if gid.startswith('102')])))

            print(f"📊 Найдено {len(game_ids)} уникальных матчей регулярного сезона.")
            print("============================================================")

            conn = self.db.get_connection()
            c = conn.cursor()

            for idx, gid in enumerate(game_ids, 1):
                trad_file = os.path.join(JSON_DIR, f"{gid}_traditional.json")
                pdf_file = os.path.join(PDF_DIR, f"{gid}_book.pdf")

                c.execute("SELECT game_id FROM matches WHERE game_id = ?", (gid,))
                in_db = c.fetchone() is not None

                # 🚀 ИДЕАЛЬНАЯ ЦЕЛОСТНОСТЬ: пропускаем матч ТОЛЬКО если есть запись в БД, JSON и PDF
                if in_db and os.path.exists(trad_file) and os.path.exists(pdf_file):
                    continue

                print(f"\n[{idx}/{len(game_ids)}] Восстановление матча {gid}:")

                if not os.path.exists(trad_file):
                    print(f"    [>] JSON не найден. Загружаю статистику...")
                    url = f"https://stats.nba.com/stats/boxscoretraditionalv3?GameID={gid}&LeagueID=10&endPeriod=0&endRange=28800&rangeType=0&startPeriod=0&startRange=0"
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(2)
                        text = await page.evaluate("() => document.body.innerText")
                        if "{" in text:
                            data = json.loads(text[text.find('{'):])
                            with open(trad_file, 'w', encoding='utf-8') as f:
                                json.dump(data, f)
                            print(f"    [+] УСПЕХ: JSON скачан.")
                        else:
                            print(f"    [-] ОШИБКА: Сервер не вернул JSON.")
                            continue
                    except Exception as e:
                        print(f"    [-] ОШИБКА скачивания JSON: {e}")
                        continue

                try:
                    with open(trad_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    box = data['boxScoreTraditional']
                    date_str = data['meta']['time'][:10]
                    away = box['awayTeam']
                    home = box['homeTeam']

                    if not home.get('teamName') or not away.get('teamName'): continue
                    if not home.get('players') and not away.get('players'): continue
                except Exception as e:
                    print(f"    [-] ОШИБКА чтения JSON: {e}")
                    continue

                pdf_path = self._download_pdf(gid, date_str, away['teamTricode'], home['teamTricode'])
                pdf_meta = self._extract_from_pdf(pdf_path)

                try:
                    c.execute('''INSERT OR REPLACE INTO matches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                              (gid, date_str, away['teamTricode'], home['teamTricode'],
                               away['statistics']['points'], home['statistics']['points'],
                               pdf_meta['refs'][0], pdf_meta['refs'][1], pdf_meta['refs'][2],
                               pdf_meta['attendance'], pdf_meta['duration']))

                    for team in [away, home]:
                        for p in team['players']:
                            stats = p.get('statistics', {})
                            if not stats.get('minutes'): continue
                            pos = p.get('position', 'Bench')

                            c.execute('''INSERT OR REPLACE INTO player_stats VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                      (gid, team['teamTricode'], p['nameI'], pos if pos else "Bench",
                                       stats.get('minutes'),
                                       stats.get('fieldGoalsMade', 0), stats.get('fieldGoalsAttempted', 0),
                                       stats.get('fieldGoalsPercentage', 0.0),
                                       stats.get('threePointersMade', 0), stats.get('threePointersAttempted', 0),
                                       stats.get('threePointersPercentage', 0.0),
                                       stats.get('freeThrowsMade', 0), stats.get('freeThrowsAttempted', 0),
                                       stats.get('freeThrowsPercentage', 0.0),
                                       stats.get('reboundsOffensive', 0), stats.get('reboundsDefensive', 0),
                                       stats.get('reboundsTotal', 0),
                                       stats.get('assists', 0), stats.get('steals', 0), stats.get('blocks', 0),
                                       stats.get('turnovers', 0), stats.get('foulsPersonal', 0), stats.get('points', 0),
                                       stats.get('plusMinusPoints', 0.0)))
                    print("    💾 Данные записаны/обновлены в SQLite.")
                except Exception as e:
                    print(f"    [-] ОШИБКА записи в БД: {e}")

            conn.commit()
            conn.close()
            await browser.close()
            print("\n🎉 Работа скрапера успешно завершена!")


if __name__ == "__main__":
    asyncio.run(WNBAScraper().run())