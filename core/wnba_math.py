# Version: 6.4
import sqlite3
from datetime import datetime
import math

class WNBAMathCore:
    @staticmethod
    def parse_minutes(minutes_str):
        if not minutes_str or ':' not in str(minutes_str): return 0.0
        try:
            m, s = map(int, str(minutes_str).split(':'))
            return float(m) + (float(s) / 60.0)
        except:
            return 0.0

    @staticmethod
    def warm_up_elo(cursor, current_date):
        cursor.execute("SELECT COUNT(*) FROM team_elo")
        if cursor.fetchone()[0] == 0:
            teams = ["ATL", "CHI", "CON", "DAL", "IND", "LVA", "LAS", "MIN", "NYL", "PHO", "SEA", "WAS"]
            for t in teams:
                cursor.execute("INSERT INTO team_elo (team_abbr, elo_rating, last_updated) VALUES (?, 1500.0, '2020-01-01')", (t,))

        cursor.execute("SELECT MAX(last_updated) FROM team_elo")
        last_dt = cursor.fetchone()[0]
        if not last_dt:
            last_dt = "2020-01-01"

        # Incremental update: process only matches strictly AFTER the last_updated date
        q = """
            SELECT game_id, date, away_team, home_team, away_score, home_score
            FROM matches
            WHERE date > ? AND date < ?
            ORDER BY date ASC
        """
        matches = cursor.execute(q, (last_dt, current_date)).fetchall()

        for m in matches:
            game_id, date, away_team, home_team, away_score, home_score = m
            if away_score is None or home_score is None:
                continue
            WNBAMathCore.update_elo(cursor, away_team, home_team, away_score, home_score, date)

    @staticmethod
    def get_elo(cursor, team_abbr):
        row = cursor.execute("SELECT elo_rating FROM team_elo WHERE team_abbr = ?", (team_abbr,)).fetchone()
        return row[0] if row else 1500.0

    @staticmethod
    def update_elo(cursor, away_team, home_team, away_score, home_score, match_date):
        away_elo = WNBAMathCore.get_elo(cursor, away_team)
        home_elo = WNBAMathCore.get_elo(cursor, home_team)

        expected_home = 1.0 / (1.0 + math.pow(10.0, (away_elo - (home_elo + 50.0)) / 400.0))
        expected_away = 1.0 - expected_home

        actual_home = 1.0 if home_score > away_score else (0.5 if home_score == away_score else 0.0)
        actual_away = 1.0 - actual_home

        mov = abs(home_score - away_score)
        elo_diff = (home_elo + 50.0 - away_elo) if home_score > away_score else (away_elo - home_elo - 50.0)
        multiplier = math.log(max(mov, 1) + 1.0) * (2.2 / (elo_diff * 0.001 + 2.2))

        K = 20.0
        new_home_elo = home_elo + K * multiplier * (actual_home - expected_home)
        new_away_elo = away_elo + K * multiplier * (actual_away - expected_away)

        cursor.execute("UPDATE team_elo SET elo_rating = ?, last_updated = ? WHERE team_abbr = ?", (new_home_elo, match_date, home_team))
        cursor.execute("UPDATE team_elo SET elo_rating = ?, last_updated = ? WHERE team_abbr = ?", (new_away_elo, match_date, away_team))

    @staticmethod
    def predict_outcome(cursor, away_team, home_team):
        away_elo = WNBAMathCore.get_elo(cursor, away_team)
        home_elo = WNBAMathCore.get_elo(cursor, home_team)
        expected_home_margin = (home_elo + 50.0 - away_elo) / 25.0
        return expected_home_margin

    @staticmethod
    def get_referee_modifier(cursor, ref_name, game_date):
        if not ref_name or ref_name == "Не указан": return 1.0

        q_league = """
            SELECT SUM(p.fta + p.pf), COUNT(DISTINCT m.game_id)
            FROM matches m JOIN player_stats p ON m.game_id = p.game_id 
            WHERE m.date < ? AND p.fta IS NOT NULL AND p.pf IS NOT NULL
        """
        l_row = cursor.execute(q_league, (game_date,)).fetchone()
        if not l_row or not l_row[0] or l_row[1] == 0: return 1.0
        league_avg_strictness = float(l_row[0]) / float(l_row[1])

        q_ref = """
            SELECT SUM(p.fta + p.pf), COUNT(DISTINCT m.game_id)
            FROM matches m JOIN player_stats p ON m.game_id = p.game_id
            WHERE (m.referee_1 = ? OR m.referee_2 = ? OR m.referee_3 = ?) 
            AND m.date < ? AND p.fta IS NOT NULL AND p.pf IS NOT NULL
        """
        r_row = cursor.execute(q_ref, (ref_name, ref_name, ref_name, game_date)).fetchone()

        if not r_row or not r_row[0] or r_row[1] < 2: return 1.0
        ref_avg_strictness = float(r_row[0]) / float(r_row[1])

        return ref_avg_strictness / league_avg_strictness if league_avg_strictness > 0 else 1.0

    @staticmethod
    def get_crew_modifier(cursor, referee_1, referee_2, referee_3, game_date):
        mods = []
        for ref in [referee_1, referee_2, referee_3]:
            if ref and ref.strip() and ref != "Не указан":
                mods.append(WNBAMathCore.get_referee_modifier(cursor, ref.strip(), game_date))
        return sum(mods) / len(mods) if mods else 1.0

    @staticmethod
    def get_player_long_memory(cursor, player_name, team, target_date):
        if team == "ANY":
            q = """
                    SELECT p.pts, p.minutes, p.fga, p.fta, p.tov, p.position, p.reb, p.fg3m, p.ast, p.pf
                    FROM player_stats p JOIN matches m ON p.game_id = m.game_id
                    WHERE p.player_name = ? AND m.date < ? AND p.minutes IS NOT NULL
                """
            rows = cursor.execute(q, (player_name, target_date)).fetchall()
        else:
            q = """
                    SELECT p.pts, p.minutes, p.fga, p.fta, p.tov, p.position, p.reb, p.fg3m, p.ast, p.pf
                    FROM player_stats p JOIN matches m ON p.game_id = m.game_id
                    WHERE p.player_name = ? AND p.team_abbr = ? AND m.date < ? AND p.minutes IS NOT NULL
                """
            rows = cursor.execute(q, (player_name, team, target_date)).fetchall()

        if not rows: return {'pts': 5.0, 'reb': 1.0, 'fg3m': 0.0, 'ast': 1.0, 'pf': 1.0, 'mins': 0.5, 'usage': 0.0, 'pos': 'Bench', 'is_star': False}

        total_pts = sum(float(r[0]) for r in rows if r[0] is not None)
        total_mins = sum(WNBAMathCore.parse_minutes(r[1]) for r in rows)
        total_reb = sum(float(r[6]) for r in rows if len(r) > 6 and r[6] is not None)
        total_fg3m = sum(float(r[7]) for r in rows if len(r) > 7 and r[7] is not None)
        total_ast = sum(float(r[8]) for r in rows if len(r) > 8 and r[8] is not None)
        total_pf = sum(float(r[9]) for r in rows if len(r) > 9 and r[9] is not None)

        total_usage_actions = sum( (float(r[2] or 0) + 0.44 * float(r[3] or 0) + float(r[4] or 0)) for r in rows )
        avg_usage = total_usage_actions / len(rows)

        epm = (total_pts / total_mins) if total_mins > 0 else 0.5
        avg_mins = (total_mins / len(rows)) if len(rows) > 0 else 5.0
        avg_pts = (total_pts / len(rows)) if len(rows) > 0 else 5.0
        avg_reb = (total_reb / len(rows)) if len(rows) > 0 else 1.0
        avg_fg3m = (total_fg3m / len(rows)) if len(rows) > 0 else 0.0
        avg_ast = (total_ast / len(rows)) if len(rows) > 0 else 1.0
        avg_pf = (total_pf / len(rows)) if len(rows) > 0 else 1.0

        pos = rows[0][5] if rows[0][5] else 'Bench'
        if pos != 'Bench': pos = pos[0]

        is_star = avg_pts >= 12.0 and avg_mins >= 25.0

        return {'pts': avg_pts, 'reb': avg_reb, 'fg3m': avg_fg3m, 'ast': avg_ast, 'pf': avg_pf, 'mins': avg_mins, 'usage': avg_usage, 'pos': pos, 'is_star': is_star, 'epm': epm}

    @staticmethod
    def get_team_dvp(cursor, defending_team, target_date, window=8):
        q_league = """
            SELECT SUBSTR(p.position, 1, 1) as pos, AVG(p.pts)
            FROM player_stats p JOIN matches m ON p.game_id = m.game_id
            WHERE m.date < ? AND p.position IS NOT NULL AND p.position != 'Bench'
            AND m.game_id IN (
                SELECT game_id FROM matches WHERE date < ? ORDER BY date DESC LIMIT ?
            )
            GROUP BY pos
        """
        league_pos_avg = dict(cursor.execute(q_league, (target_date, target_date, window * 12)).fetchall())

        q_team = """
            SELECT SUBSTR(p.position, 1, 1) as pos, AVG(p.pts)
            FROM player_stats p JOIN matches m ON p.game_id = m.game_id
            WHERE (m.home_team = ? OR m.away_team = ?) AND p.team_abbr != ? AND m.date < ?
            AND p.position IS NOT NULL AND p.position != 'Bench'
            AND m.game_id IN (
                SELECT game_id FROM matches WHERE (home_team = ? OR away_team = ?) AND date < ? ORDER BY date DESC LIMIT ?
            )
            GROUP BY pos
        """
        team_pos_avg = dict(cursor.execute(q_team, (defending_team, defending_team, defending_team, target_date, defending_team, defending_team, target_date, window)).fetchall())

        dvp = {}
        for pos in ['G', 'F', 'C']:
            l_avg = league_pos_avg.get(pos, 10.0)
            t_avg = team_pos_avg.get(pos, 10.0)
            dvp[pos] = t_avg / l_avg if l_avg > 0 else 1.0
        return dvp

    @staticmethod
    def get_team_rest_days(cursor, team_abbr, target_date):
        q = "SELECT MAX(date) FROM matches WHERE (home_team = ? OR away_team = ?) AND date < ?"
        last_match = cursor.execute(q, (team_abbr, team_abbr, target_date)).fetchone()[0]
        if not last_match: return 3

        d1 = datetime.strptime(last_match, "%Y-%m-%d")
        d2 = datetime.strptime(target_date, "%Y-%m-%d")
        return (d2 - d1).days - 1

    @staticmethod
    def calculate_defense_rating(cursor, team_abbr, target_date, window=None):
        if window is not None:
            q = """
                SELECT m.home_team, m.away_team, m.home_score, m.away_score
                FROM matches m
                WHERE (m.home_team = ? OR m.away_team = ?) AND m.date < ? AND m.home_score IS NOT NULL
                ORDER BY m.date DESC LIMIT ?
            """
            rows = cursor.execute(q, (team_abbr, team_abbr, target_date, window)).fetchall()
        else:
            q = """
                SELECT m.home_team, m.away_team, m.home_score, m.away_score
                FROM matches m
                WHERE (m.home_team = ? OR m.away_team = ?) AND m.date < ? AND m.home_score IS NOT NULL
            """
            rows = cursor.execute(q, (team_abbr, team_abbr, target_date)).fetchall()

        if not rows: return 0.0

        pts_allowed = []
        for r in rows:
            if r[0] == team_abbr:
                pts_allowed.append(r[3])
            else:
                pts_allowed.append(r[2])

        avg_allowed = sum(pts_allowed) / len(pts_allowed)
        return avg_allowed

    @staticmethod
    def get_team_offense_rating(cursor, team_abbr, target_date, window=None):
        if window is not None:
            q = """
                SELECT m.home_team, m.away_team, m.home_score, m.away_score
                FROM matches m
                WHERE (m.home_team = ? OR m.away_team = ?) AND m.date < ? AND m.home_score IS NOT NULL
                ORDER BY m.date DESC LIMIT ?
            """
            rows = cursor.execute(q, (team_abbr, team_abbr, target_date, window)).fetchall()
        else:
            q = """
                SELECT m.home_team, m.away_team, m.home_score, m.away_score
                FROM matches m
                WHERE (m.home_team = ? OR m.away_team = ?) AND m.date < ? AND m.home_score IS NOT NULL
            """
            rows = cursor.execute(q, (team_abbr, team_abbr, target_date)).fetchall()

        if not rows: return 0.0

        pts_scored = []
        for r in rows:
            if r[0] == team_abbr:
                pts_scored.append(r[2]) # home_score
            else:
                pts_scored.append(r[3]) # away_score

        avg_scored = sum(pts_scored) / len(pts_scored)
        return avg_scored

    @staticmethod
    def calculate_pace(cursor, team_abbr, target_date, window=8):
        q = """
            SELECT SUM(p.fga), SUM(p.fta), SUM(p.oreb), SUM(p.tov), COUNT(DISTINCT m.game_id)
            FROM player_stats p JOIN matches m ON p.game_id = m.game_id
            WHERE p.team_abbr = ? AND m.date < ?
            AND m.game_id IN (
                SELECT game_id FROM matches WHERE (home_team = ? OR away_team = ?) AND date < ? ORDER BY date DESC LIMIT ?
            )
        """
        row = cursor.execute(q, (team_abbr, target_date, team_abbr, team_abbr, target_date, window)).fetchone()
        if not row or row[4] == 0: return 80.0

        poss = float(row[0] or 0) + 0.44 * float(row[1] or 0) - float(row[2] or 0) + float(row[3] or 0)
        return poss / float(row[4])

    @staticmethod
    def get_league_avg_pace(cursor, target_date, window=50):
        q = """
            SELECT SUM(p.fga), SUM(p.fta), SUM(p.oreb), SUM(p.tov), COUNT(DISTINCT m.game_id)
            FROM player_stats p JOIN matches m ON p.game_id = m.game_id
            WHERE m.date < ?
            AND m.game_id IN (
                SELECT game_id FROM matches WHERE date < ? ORDER BY date DESC LIMIT ?
            )
        """
        row = cursor.execute(q, (target_date, target_date, window)).fetchone()
        if not row or row[4] == 0: return 80.0

        poss = float(row[0] or 0) + 0.44 * float(row[1] or 0) - float(row[2] or 0) + float(row[3] or 0)
        return poss / (float(row[4]) * 2.0)

    @staticmethod
    def calculate_team_projection(cursor, team_abbr, roster, dnp_players, target_date, crew_mod, is_home, opp_team, expected_spread):
        team_data = []
        log_lines = [f"<b>Команда:</b> {team_abbr} ({'Дома' if is_home else 'В гостях'})", f"<b>Влияние судей (Crew Mod):</b> {crew_mod:.3f}"]

        WNBAMathCore.warm_up_elo(cursor, target_date)

        dvp = WNBAMathCore.get_team_dvp(cursor, opp_team, target_date, window=8)
        rest_days = WNBAMathCore.get_team_rest_days(cursor, team_abbr, target_date)

        log_lines.append(f"<b>Отдых:</b> {rest_days} дней | <b>Ожидаемая фора:</b> {expected_spread:.1f}")

        missing_pts = 0.0
        for dnp in dnp_players:
            hist = WNBAMathCore.get_player_long_memory(cursor, dnp, team_abbr, target_date)
            missing_pts += hist['pts']

        log_lines.append(f"<b>DNP Очки (перераспределение):</b> {missing_pts:.1f}")

        total_active_usage = 0.0
        for p in roster:
            hist = WNBAMathCore.get_player_long_memory(cursor, p, team_abbr, target_date)
            hist['name'] = p
            team_data.append(hist)
            total_active_usage += hist['usage']

        log_lines.append("<pre><code>")
        log_lines.append("Игрок      |PTS |REB |AST |3P  |PF  ")
        log_lines.append("-" * 38)

        predictions = {}

        # 1. INDIVIDUAL PLAYER PROJECTIONS
        for p in team_data:
            proj_pts = p['pts']
            proj_reb = p['reb']
            proj_fg3m = p['fg3m']
            proj_ast = p['ast']
            proj_pf = p['pf']
            base_pts = proj_pts

            usage_share = p['usage'] / total_active_usage if total_active_usage > 0 else (1.0 / len(team_data))
            bonus_pts = (missing_pts * 0.4) * usage_share
            proj_pts += bonus_pts

            dvp_mod = dvp.get(p['pos'], 1.0)
            proj_pts *= dvp_mod
            proj_reb *= dvp_mod
            proj_fg3m *= dvp_mod
            proj_ast *= dvp_mod

            if p['is_star']:
                if rest_days == 0:
                    proj_pts *= 0.85
                    proj_reb *= 0.85
                    proj_fg3m *= 0.85
                    proj_ast *= 0.85
                    proj_pf *= 0.85
                if abs(expected_spread) >= 12.0:
                    proj_pts *= 0.85
                    proj_reb *= 0.85
                    proj_fg3m *= 0.85
                    proj_ast *= 0.85
            else:
                if is_home:
                    proj_pts *= 1.10
                    proj_reb *= 1.10
                    proj_fg3m *= 1.10
                    proj_ast *= 1.10
                    proj_pf *= 1.05
                else:
                    proj_pts *= 0.90
                    proj_reb *= 0.90
                    proj_fg3m *= 0.90
                    proj_ast *= 0.90
                    proj_pf *= 0.95

            crew_effect = (proj_pts * 0.8) + (proj_pts * 0.2 * crew_mod)
            proj_pts = crew_effect
            proj_reb = (proj_reb * 0.8) + (proj_reb * 0.2 * crew_mod)
            proj_fg3m = (proj_fg3m * 0.8) + (proj_fg3m * 0.2 * crew_mod)
            proj_ast = (proj_ast * 0.8) + (proj_ast * 0.2 * crew_mod)
            proj_pf = (proj_pf * 0.8) + (proj_pf * 0.2 * (1.0 / crew_mod))

            predictions[p['name']] = {'pts': proj_pts, 'reb': proj_reb, 'fg3m': proj_fg3m, 'ast': proj_ast, 'pf': proj_pf}

            # Pad name for simple table alignment, limit to 10 chars
            padded_name = p['name'][:10].ljust(10)
            log_lines.append(f"{padded_name}|{proj_pts:4.1f}|{proj_reb:4.1f}|{proj_ast:4.1f}|{proj_fg3m:4.1f}|{proj_pf:4.1f}")

        log_lines.append("</code></pre>")

        # 2. TOP-DOWN TEAM TOTAL PROJECTION
        own_off_season = WNBAMathCore.get_team_offense_rating(cursor, team_abbr, target_date, window=None)
        own_off_recent = WNBAMathCore.get_team_offense_rating(cursor, team_abbr, target_date, window=8)
        own_final_off = (own_off_season + own_off_recent) / 2.0

        opp_def_season = WNBAMathCore.calculate_defense_rating(cursor, opp_team, target_date, window=None)
        opp_def_recent = WNBAMathCore.calculate_defense_rating(cursor, opp_team, target_date, window=8)
        opp_final_def = (opp_def_season + opp_def_recent) / 2.0

        base_team_proj = (own_final_off + opp_final_def) / 2.0

        log_lines.append(f"<b>Сглаживание дисперсии (Атака {team_abbr}):</b> Сезон={own_off_season:.1f}, Последние 8={own_off_recent:.1f} -> Итог={own_final_off:.1f}")
        log_lines.append(f"<b>Сглаживание дисперсии (Защита {opp_team}):</b> Сезон={opp_def_season:.1f}, Последние 8={opp_def_recent:.1f} -> Итог={opp_final_def:.1f}")

        if is_home:
            base_team_proj += 3.0

        # Apply Pace Modifier BEFORE flat adjustments (matching backtester logic perfectly)
        team_pace = WNBAMathCore.calculate_pace(cursor, team_abbr, target_date, window=8)
        opp_pace = WNBAMathCore.calculate_pace(cursor, opp_team, target_date, window=8)
        avg_match_pace = (team_pace + opp_pace) / 2.0

        league_pace = WNBAMathCore.get_league_avg_pace(cursor, target_date, window=50)
        pace_modifier = avg_match_pace / league_pace if league_pace > 0 else 1.0

        final_team_total = base_team_proj * pace_modifier

        # Apply Flat Adjustments
        final_team_total -= missing_pts * 0.6
        fatigue_penalty = -2.0 if rest_days == 0 else 0.0
        final_team_total += fatigue_penalty
        final_team_total += (crew_mod - 1.0) * 10.0

        # --- ГЛОБАЛЬНАЯ ПОПРАВКА ТОТАЛА (TOTAL BIAS CORRECTION) ---
        # Смещение V2.0 модели равно -3.98. Вычитаем 2.0 очка из каждой команды.
        final_team_total -= 2.0
        log_lines.append(f"<b>Корректировка системного смещения (Bias Correction):</b> -2.0 очка\n")

        log_lines.append(f"<b>Итого проекция команды (С учетом Pace & Defense):</b> {final_team_total:.1f}\n")
        math_log = "\n".join(log_lines)

        # We need to save the dictionary predictions to player_projections instead of just floats!
        try:
            cursor.execute("DELETE FROM player_projections WHERE team_name = ?", (team_abbr,))
            for p_name, p_data in predictions.items():
                cursor.execute(
                    "INSERT INTO player_projections (team_name, player_name, projected_pts) VALUES (?, ?, ?)",
                    (team_abbr, p_name, p_data['pts'])
                )
        except Exception as e:
            print(f"Failed to update player_projections: {e}")

        return final_team_total, predictions, math_log

    @staticmethod
    def calculate_h2h_duels(proj_1: float, proj_2: float, is_same_team: bool):
        """
        Расчет дуэлей (H2H) для игроков.
        Возвращает (h2h_handicap, h2h_total)
        h2h_handicap: Фора (Proj 1 - Proj 2) - насколько первый игрок результативнее второго.
        h2h_total: Тотал (Proj 1 + Proj 2) - общая результативность.
        """
        h2h_handicap = proj_1 - proj_2
        h2h_total = proj_1 + proj_2

        # Корреляционная поправка: если игроки из одной команды и делят мяч,
        # их совместный тотал тяготеет к "Меньше" (коэффициент 0.95).
        if is_same_team:
            h2h_total *= 0.95

        return h2h_handicap, h2h_total
