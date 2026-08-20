# Version: 1.0
import asyncio
import logging
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import datetime
import sys
import os

# Ensure the parent directory is in sys.path so we can import project modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database.db_manager import DBManager
from config import Config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('LineupParser')

class LineupParser:
    def __init__(self):
        self.db = DBManager()
        self.db.init_db()  # Ensure tables exist
        self.interval_seconds = 300 # 5 minutes

    async def route_interceptor(self, route):
        if route.request.resource_type in ["image", "media", "font"]:
            await route.abort()
        else:
            await route.continue_()

    async def run(self, run_once=False):
        logger.info("Starting WNBA Lineup Parser Demon...")
        while True:
            try:
                await self._parse_rotowire()
            except Exception as e:
                logger.error(f"Error parsing rotowire: {e}", exc_info=True)

            if run_once:
                break

            logger.info(f"Sleeping for {self.interval_seconds} seconds...")
            await asyncio.sleep(self.interval_seconds)

    async def _parse_rotowire(self):
        url = "https://www.rotowire.com/wnba/lineups.php"
        logger.info(f"Scraping RotoWire: {url}")

        async with async_playwright() as p:
            launch_args = {
                "headless": True,
                "args": ["--headless=new"]
            }
            #if Config.PROXY_URL:
             #   launch_args["proxy"] = {"server": Config.PROXY_URL}

            browser = await p.chromium.launch(**launch_args)
            context = await browser.new_context()
            page = await context.new_page()

            stealth = Stealth()
            await stealth.apply_stealth_async(page)

            await page.route("**/*", self.route_interceptor)

            await page.goto(url, timeout=60000, wait_until="domcontentloaded")
            html = await page.content()
            await browser.close()

        await self._process_rotowire_html(html)

    async def _process_rotowire_html(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        boxes = soup.find_all(class_='lineup__box')

        if not boxes:
            logger.warning("No lineup boxes found. Maybe season is over or no games today.")
            return

        parsed_data = []
        for box in boxes:
            top = box.find(class_='lineup__top')
            if not top:
                continue

            match_teams = [t.text.strip() for t in top.find_all(class_='lineup__abbr')]
            if len(match_teams) != 2:
                continue

            # Create a simple match ID (e.g., AWAY@HOME or just sort them)
            # RotoWire shows Away then Home
            away_team = match_teams[0]
            home_team = match_teams[1]
            match_id = f"{away_team}@{home_team}_{datetime.date.today().isoformat()}"

            lists = box.find_all('ul', class_='lineup__list')
            if len(lists) != 2:
                continue

            for i, lst in enumerate(lists):
                team = match_teams[i]
                status_div = lst.find(class_='lineup__status')
                list_status = status_div.text.strip().lower() if status_div else ""

                # We can determine if it's expected or confirmed based on the list_status
                # But individual players also have their own status
                is_lineup_expected = "expected" in list_status

                players = lst.find_all(class_='lineup__player')

                # RotoWire lists the starting 5 first
                for p_idx, player in enumerate(players):
                    name_a = player.find('a')
                    name = name_a.text.strip() if name_a else player.text.strip()
                    if not name:
                        continue

                    # Figure out status
                    title = (player.get('title') or "").lower()
                    classes = player.get('class', [])

                    status = 'BENCH'

                    if 'is-pct-play-0' in classes or 'unlikely' in title or 'out' in title:
                        status = 'OUT'
                    elif p_idx < 5:
                        if is_lineup_expected:
                            status = 'EXPECTED'
                        else:
                            status = 'STARTING'

                    parsed_data.append((match_id, team, name, status, 'rotowire'))

        logger.info(f"Parsed {len(parsed_data)} player records from RotoWire.")
        if parsed_data:
            self._save_to_db(parsed_data)

    def _save_to_db(self, parsed_data):
        conn = self.db.get_connection()
        cursor = conn.cursor()
        now = datetime.datetime.now().isoformat()

        # parsed_data is list of tuples: (match_id, team, player_name, status, source)
        records = [(r[0], r[1], r[2], r[3], r[4], now) for r in parsed_data]

        cursor.executemany('''
            INSERT OR REPLACE INTO actual_lineups (match_id, team, player_name, status, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', records)

        conn.commit()
        conn.close()
        logger.info("Saved lineups to database.")

if __name__ == '__main__':
    parser = LineupParser()
    asyncio.run(parser.run())