# Version: 6.12
import asyncio
import os
import sys
import uuid
import datetime
import sqlite3
import logging

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, \
    KeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from aiogram.client.session.aiohttp import AiohttpSession
from yookassa import Configuration, Payment

from config import Config
from database.db_manager import DBManager
from betting_manager.main_predictor import run_predictor

# Инициализируем YooKassa
Configuration.account_id = os.getenv("YOOKASSA_SHOP_ID", "")
Configuration.secret_key = os.getenv("YOOKASSA_SECRET_KEY", "")

# Включаем логирование, чтобы видеть всё, что делает бот
logging.basicConfig(level=logging.INFO)

dp = Dispatcher()


class SettingsState(StatesGroup):
    waiting_for_value = State()


def _build_settings_keyboard():
    """Вспомогательная функция для генерации клавиатуры настроек"""
    settings = db.get_all_system_settings()

    # 1. Фикс невидимой кнопки: принудительно добавляем betting_mode если его нет
    has_betting_mode = any(k == "betting_mode" for k, v in settings)
    if not has_betting_mode:
        settings.append(("betting_mode", "MANUAL"))

    ru_labels = {
        "base_bet_amount": "💰 Базовая ставка (RUB)",
        "game_edge_threshold": "🏀 Отклонение (Матч)",
        "player_edge_threshold": "👤 Отклонение (Игрок)",
        "win_edge_threshold": "📈 Мин. перевес (Edge %)",
        "standard_price": "💳 Цена: Standard",
        "pro_price": "💎 Цена: Pro",
        "betting_mode": "🔄 Режим работы (AUTO/MANUAL)"
    }

    inline_keyboard = []
    for k, v in settings:
        label = ru_labels.get(k, k)
        if k == "betting_mode":
            inline_keyboard.append([InlineKeyboardButton(text=f"{label}: {v}", callback_data="toggle_betting_mode")])
        else:
            inline_keyboard.append([InlineKeyboardButton(text=f"{label}: {v}", callback_data=f"edit_set_{k}")])

    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


@dp.message(F.text == "⚙️ Системные настройки")
async def show_system_settings(message: Message):
    if message.from_user.id != Config.ADMIN_ID:
        return

    keyboard = _build_settings_keyboard()
    await message.answer("<b>⚙️ Системные настройки:</b>\nНажмите на параметр, чтобы изменить его значение.",
                         reply_markup=keyboard, parse_mode="HTML")


@dp.callback_query(F.data == "toggle_betting_mode")
async def toggle_betting_mode(callback: CallbackQuery):
    if callback.from_user.id != Config.ADMIN_ID:
        await callback.answer()
        return

    current_mode = db.get_system_setting("betting_mode", "MANUAL")
    new_mode = "AUTO" if current_mode == "MANUAL" else "MANUAL"
    db.set_system_setting("betting_mode", new_mode)

    await callback.answer(f"Режим изменён на {new_mode}")

    # Фикс ошибки Pydantic Instance is Frozen: перерисовываем клавиатуру напрямую
    keyboard = _build_settings_keyboard()
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("edit_set_"))
async def edit_setting_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != Config.ADMIN_ID:
        await callback.answer()
        return

    setting_key = callback.data.replace("edit_set_", "")
    await state.update_data(setting_key=setting_key)
    await state.set_state(SettingsState.waiting_for_value)

    await callback.message.answer(f"Введите новое значение для настройки <b>{setting_key}</b>:", parse_mode="HTML")
    await callback.answer()


@dp.message(SettingsState.waiting_for_value)
async def edit_setting_finish(message: Message, state: FSMContext):
    if message.from_user.id != Config.ADMIN_ID:
        return

    data = await state.get_data()
    setting_key = data.get("setting_key")
    new_value = message.text.strip()

    db.set_system_setting(setting_key, new_value)
    await message.answer(f"✅ Настройка <b>{setting_key}</b> успешно обновлена на <b>{new_value}</b>.",
                         parse_mode="HTML")
    await state.clear()

    keyboard = _build_settings_keyboard()
    await message.answer("<b>⚙️ Системные настройки:</b>\nНажмите на параметр, чтобы изменить его значение.",
                         reply_markup=keyboard, parse_mode="HTML")


db = DBManager()
db.init_db()


def get_main_reply_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Расписание (24 часа)"), KeyboardButton(text="🛒 Тарифы и Подписка")]
    ], resize_keyboard=True)


def get_admin_reply_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👥 Подписчики"), KeyboardButton(text="🚀 Запустить предиктор")],
        [KeyboardButton(text="📊 Аналитика Edge"), KeyboardButton(text="📅 Расписание (24 часа)")],
        [KeyboardButton(text="ℹ️ Подсказка: /give_sub <id> <дней> | /revoke_sub <id>")],
        [KeyboardButton(text="🛒 Тарифы и Подписка"), KeyboardButton(text="⚙️ Системные настройки")]
    ], resize_keyboard=True)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name

    db.add_user(user_id, username, first_name)

    welcome_text = "Добро пожаловать в WNBA Analytics — профессиональный сервис сбора и анализа спортивной статистики."

    if user_id == Config.ADMIN_ID:
        await message.answer(welcome_text, reply_markup=get_admin_reply_keyboard())
    else:
        await message.answer(welcome_text, reply_markup=get_main_reply_keyboard())


def _fetch_predictions_db():
    import datetime
    conn = db.get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT match_id, team1, team2, status, match_date FROM match_tracking WHERE status IN ('READY_TO_CALCULATE', 'WAITING_REFS', 'WAITING_ROSTERS', 'NEW', 'PRELIM_READY', 'PRELIM_CALCULATED', 'FINAL_CHECK')"
    )
    all_matches = c.fetchall()
    conn.close()

    filtered_matches = []
    now = datetime.datetime.now()
    # Устанавливаем жесткий лимит: текущее время + 24 часа
    time_limit = now + datetime.timedelta(hours=24)

    for m in all_matches:
        match_date = m[4]
        if match_date and match_date != "Unknown":
            try:
                dt = datetime.datetime.strptime(match_date, "%Y-%m-%d %H:%M:%S")
                # Если матч стартует позже, чем через 24 часа — пропускаем его
                if dt > time_limit:
                    continue
            except ValueError:
                pass # Если формат даты кривой, оставляем матч в списке от греха подальше

        filtered_matches.append(m)

    return filtered_matches


async def _show_predictions_logic(message_or_call, user_id):
    if not db.check_subscription(user_id):
        if isinstance(message_or_call, CallbackQuery):
            await message_or_call.message.answer("У вас нет активной подписки. Перейдите в раздел тарифов.")
        else:
            await message_or_call.answer("У вас нет активной подписки. Перейдите в раздел тарифов.")
        return

    matches = await asyncio.to_thread(_fetch_predictions_db)

    if not matches:
        if isinstance(message_or_call, CallbackQuery):
            await message_or_call.message.answer(
                "На данный момент нет доступных матчей на сегодня. Ожидайте уведомлений.")
        else:
            await message_or_call.answer("На данный момент нет доступных матчей на сегодня. Ожидайте уведомлений.")
        return

    keyboard = []
    for m in matches:
        match_id, team1, team2, status, match_date = m

        # Красивое форматирование даты и времени
        date_str = ""
        if match_date and match_date != "Unknown":
            try:
                # Превращаем "2026-08-22 10:30:00" в "22.08 10:30"
                dt = datetime.datetime.strptime(match_date, "%Y-%m-%d %H:%M:%S")
                date_str = dt.strftime("%d.%m %H:%M")
            except ValueError:
                date_str = match_date[:10]

        # Собираем текст кнопки
        btn_text = f"{team1} - {team2}"
        if date_str:
            btn_text += f" | {date_str}"

        if status in ('PRELIM_READY', 'WAITING_REFS', 'WAITING_ROSTERS', 'PRELIM_CALCULATED'):
            btn_text += " (Предв.)"

        keyboard.append([InlineKeyboardButton(text=btn_text, callback_data=f"match_{match_id}")])

    keyboard.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="show_predictions")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    if isinstance(message_or_call, CallbackQuery):
        try:
            await message_or_call.message.edit_text("📅 Расписание матчей. Выберите игру:", reply_markup=markup)
        except:
            await message_or_call.message.answer("📅 Расписание матчей. Выберите игру:", reply_markup=markup)
    else:
        await message_or_call.answer("📅 Расписание матчей. Выберите игру:", reply_markup=markup)


@dp.message(F.text == "📅 Расписание (24 часа)")
async def show_predictions_message(message: Message):
    await _show_predictions_logic(message, message.from_user.id)


@dp.message(Command("edge_stats"))
@dp.message(F.text == "📊 Аналитика Edge")
async def cmd_edge_stats(message: Message):
    if message.from_user.id != Config.ADMIN_ID: return
    conn = db.get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT market, line, prediction, kf, status, profit FROM virtual_bets WHERE status IN ('WON', 'LOST')")
        bets = c.fetchall()
    finally:
        conn.close()

    stats = {"0-5": {"count": 0, "won": 0, "profit": 0.0},
             "5-10": {"count": 0, "won": 0, "profit": 0.0},
             "10-12": {"count": 0, "won": 0, "profit": 0.0},
             ">12": {"count": 0, "won": 0, "profit": 0.0}}

    for market, line, prediction, kf, status, profit in bets:
        if line is None or prediction is None or kf is None: continue
        delta = abs(prediction - line)
        implied_prob = 1.0 / kf
        multiplier = 0.05 if market == "PLAYER_PTS" else 0.025
        win_prob = min(implied_prob + (delta * multiplier), 0.90)
        edge = win_prob - implied_prob
        edge_pct = edge * 100

        key = "0-5" if 0 <= edge_pct < 5 else "5-10" if 5 <= edge_pct <= 10 else "10-12" if 10 < edge_pct <= 12 else ">12" if edge_pct > 12 else None
        if key:
            stats[key]["count"] += 1
            if status == "WON": stats[key]["won"] += 1
            stats[key]["profit"] += (profit or 0.0)

    text = "📊 <b>Аналитика эффективности (Edge)</b>\n〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"

    icons = {"0-5": "🧊", "5-10": "🌤", "10-12": "🔥", ">12": "💎"}
    labels = {"0-5": "0% - 5%", "5-10": "5% - 10%", "10-12": "10% - 12%", ">12": "Больше 12%"}

    for k in ["0-5", "5-10", "10-12", ">12"]:
        count = stats[k]["count"]
        winrate = (stats[k]["won"] / count * 100) if count > 0 else 0
        prof = stats[k]["profit"]

        # Динамические иконки и знаки для профита
        prof_icon = "🟢" if prof > 0 else "🔴" if prof < 0 else "⚪️"
        prof_sign = "+" if prof > 0 else ""

        text += f"{icons[k]} <b>Edge: {labels[k]}</b>\n"
        text += f"├ Ставок: <b>{count}</b>\n"
        text += f"├ Винрейт: <b>{winrate:.1f}%</b>\n"
        text += f"└ Профит: {prof_icon} <b>{prof_sign}{prof:.2f} RUB</b>\n\n"

    await message.answer(text, parse_mode="HTML")
    conn.close()


@dp.callback_query(F.data == "show_predictions")
async def show_predictions(callback: CallbackQuery):
    await _show_predictions_logic(callback, callback.from_user.id)
    await callback.answer()


@dp.callback_query(F.data.startswith("match_"))
async def show_match_categories(callback: CallbackQuery):
    match_id = callback.data.split("_")[1]

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔥 Топ-5 (Сейчас)", callback_data=f"topval_{match_id}"),
            InlineKeyboardButton(text="💎 Все валуи", callback_data=f"allval_{match_id}")
        ],
        [InlineKeyboardButton(text="Исход (Победа/Фора)", callback_data=f"cat_ИСХОД_{match_id}")],
        [InlineKeyboardButton(text="Тоталы (Матч/Команды)", callback_data=f"cat_ТОТАЛ_{match_id}")],
        [InlineKeyboardButton(text="Показатели игроков", callback_data=f"cat_ИГРОК_{match_id}")],
        [InlineKeyboardButton(text="⚔️ Дуэли", callback_data=f"cat_ДУЭЛИ_{match_id}")],
        [InlineKeyboardButton(text="📊 Детали расчета (Математика)", callback_data=f"mathlog_{match_id}")],
        [InlineKeyboardButton(text="🔙 Назад к расписанию", callback_data="show_predictions")]
    ])

    try:
        await callback.message.edit_text("Выберите интересующий рынок (категорию):", reply_markup=keyboard)
    except:
        await callback.message.answer("Выберите интересующий рынок (категорию):", reply_markup=keyboard)

    await callback.answer()


def _fetch_top_values_db(match_id, user_id, limit=5):
    conn = db.get_connection()
    c = conn.cursor()

    c.execute("SELECT team1, team2, status FROM match_tracking WHERE match_id = ?", (match_id,))
    res = c.fetchone()
    if not res:
        conn.close()
        return None, [], False, None

    team1, team2, status = res
    match_name = f"{team1} - {team2}"

    c.execute("SELECT tier, is_vip FROM users WHERE user_id = ?", (user_id,))
    user_row = c.fetchone()
    tier = 'free'
    is_vip = False
    if user_row:
        tier = user_row[0]
        is_vip = bool(user_row[1])
    if user_id == Config.ADMIN_ID:
        tier = 'pro'
        is_vip = True

    try:
        # ДОБАВЛЕНО: Извлекаем id (первый аргумент)
        c.execute(
            "SELECT id, market, category, player_name, line, prediction, selection, kf, bookmaker, published_at "
            "FROM virtual_bets WHERE match_name = ?",
            (match_name,)
        )
        bets = c.fetchall()
    except sqlite3.OperationalError:
        c.execute(
            "SELECT id, market, category, player_name, line, prediction, selection, kf, 'FONBET' as bookmaker, published_at "
            "FROM virtual_bets WHERE match_name = ?",
            (match_name,)
        )
        bets = c.fetchall()

    top_bets = []
    import datetime
    for b in bets:
        bet_id, market, category, p_name, line, proj, sel, kf, bookmaker, pub_at = b

        if tier == 'standard' and (bookmaker != 'FONBET' or category == 'ИГРОК'):
            continue

        all_kfs = get_all_kfs_for_bet(c, match_name, market, p_name, line, kf, bookmaker, match_id=match_id)
        real_kf = all_kfs.get(bookmaker, kf)

        try:
            pub_time = datetime.datetime.strptime(pub_at, '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            pub_time = datetime.datetime.now()
        time_passed = (datetime.datetime.now() - pub_time).total_seconds() / 60.0

        delta = abs(proj - line)
        implied_prob = 1.0 / float(real_kf)

        edge_mult = 0.025
        if category == "ИГРОК":
            if market in ["PLAYER_REB", "PLAYER_AST"]:
                edge_mult = 0.15
            elif market == "PLAYER_FG3M":
                edge_mult = 0.25
            else:
                edge_mult = 0.05

        win_prob = min(implied_prob + (delta * edge_mult), 0.90)
        edge = win_prob - implied_prob

        if edge > 0:
            top_bets.append({
                'bet_id': bet_id,  # Сохраняем ID для кнопки
                'market': market, 'category': category, 'p_name': p_name,
                'line': line, 'proj': proj, 'sel': sel, 'real_kf': real_kf,
                'orig_kf': kf,
                'edge': edge * 100, 'bookmaker': bookmaker,
                'time_passed': time_passed,
                'all_kfs': all_kfs
            })

    conn.close()

    top_bets.sort(key=lambda x: x['edge'], reverse=True)
    if limit:
        return match_name, top_bets[:limit], is_vip, status
    return match_name, top_bets, is_vip, status


@dp.callback_query(F.data.startswith("topval_") | F.data.startswith("allval_"))
async def show_value_lists(callback: CallbackQuery):
    user_id = callback.from_user.id
    action, match_id = callback.data.split("_")

    limit = 5 if action == "topval" else None
    title = "🔥 <b>ТОП-5 ВАЛУЕВ (Лайв)</b>" if action == "topval" else "💎 <b>ВСЕ ВАЛУИ (Лайв)</b>"

    match_name, bets_list, is_vip, status = await asyncio.to_thread(_fetch_top_values_db, match_id, user_id, limit)

    if not match_name:
        await callback.answer("Матч не найден", show_alert=True)
        return

    text = f"{title}\n🏀 {match_name}\n〰️〰️〰️〰️〰️〰️〰️〰️〰️\n\n"
    inline_keyboard = []

    if not bets_list:
        text += "Нет валуйных ставок для этого матча на данный момент."
    else:
        market_dict = {
            "PLAYER_PTS": "Очки", "PLAYER_REB": "Подборы",
            "PLAYER_FG3M": "Трёхи", "PLAYER_AST": "Передачи",
            "PLAYER_H2H": "Дуэль", "GAME_TOTAL": "Тотал Матча",
            "TEAM_TOTAL": "Инд. Тотал", "HANDICAP": "Фора"
        }

        # Эмодзи для нумерации кнопок
        num_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        for i, b in enumerate(bets_list, 1):
            is_hidden = not is_vip and b['time_passed'] < 30 and status != 'PRELIM_READY'

            if is_hidden:
                text += f"{i}. 🔒 <b>Скрытый Валуй</b> (Edge: ~{b['edge']:.1f}%)\n"
                text += f"   <i>Доступно через {int(30 - b['time_passed'])} мин. (Или VIP)</i>\n\n"
            else:
                book_icon = "🔴" if b['bookmaker'] == 'FONBET' else "🏙"
                m_name = market_dict.get(b['market'], b['market'])

                if b['market'] in ["GAME_TOTAL", "HANDICAP"]:
                    target = m_name
                else:
                    target = f"{b['p_name']} ({m_name})"

                if float(b['orig_kf']) != float(b['real_kf']):
                    kf_str = f"<s>{b['orig_kf']}</s> ➡️ <b>{b['real_kf']}</b>"
                else:
                    kf_str = f"<b>{b['real_kf']}</b>"

                text += f"{i}. <b>{target}</b> | Выбор: <b>{b['sel']} {b['line']}</b>\n"
                text += f"   Прогноз: {b['proj']:.1f} | {book_icon} Кэф: {kf_str}\n"
                text += f"   📈 Перевес (Edge): <b>{b['edge']:.1f}%</b>\n\n"

                idx_emoji = num_emojis[i - 1] if i <= 10 else f"[{i}]"
                
                # Создаем ряд кнопок для всех доступных контор
                bk_map = {
                    'FONBET': ('FON', '🔴 Фон'),
                    'BETCITY': ('BET', '🏙 Бетсити'),
                    'PARI': ('PAR', '🔵 Пари'),
                    'MELBET': ('MEL', '🟡 Мелбет')
                }
                
                row_buttons = []
                for bk_name, kf_val in b['all_kfs'].items():
                    abbr, icon = bk_map.get(bk_name, (bk_name[:3].upper(), f"🏢 {bk_name[:3]}"))
                    btn_text = f"{idx_emoji} {icon}: {kf_val}"
                    row_buttons.append(InlineKeyboardButton(text=btn_text, callback_data=f"pb_{b['bet_id']}_{abbr}"))
                    
                if row_buttons:
                    inline_keyboard.append(row_buttons)

    inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data=f"match_{match_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    await callback.answer()


@dp.callback_query(F.data.startswith("pb_"))
async def process_place_bet(callback: CallbackQuery):
    user_id = callback.from_user.id

    # ПРОВЕРКА ПРАВ: Если это не ты (не Админ), выдаем заглушку
    if user_id != Config.ADMIN_ID:
        await callback.answer("🚧 Авто-ставка в разработке (Скоро в PRO-тарифе)", show_alert=True)
        return

    parts = callback.data.split("_")
    bet_id = int(parts[1])
    bk_abbr = parts[2] if len(parts) > 2 else "FON"
    
    rev_bk_map = {
        'FON': 'FONBET',
        'BET': 'BETCITY',
        'PAR': 'PARI',
        'MEL': 'MELBET'
    }
    bk_full_name = rev_bk_map.get(bk_abbr, bk_abbr)

    conn = db.get_connection()
    c = conn.cursor()
    c.execute("SELECT match_name, market, category, player_name, line, selection, kf FROM virtual_bets WHERE id = ?",
              (bet_id,))
    bet_row = c.fetchone()

    if not bet_row:
        conn.close()
        await callback.answer("Ошибка: Валуй устарел или не найден в базе.", show_alert=True)
        return

    match_name, market, category, p_name, line, sel, kf = bet_row
    
    all_kfs = get_all_kfs_for_bet(c, match_name, market, p_name, line, kf, 'FONBET')
    target_kf = all_kfs.get(bk_full_name, kf)

    # Формируем правильный target для Роутера
    if market in ["GAME_TOTAL", "HANDICAP"]:
        target = f"GAME | {sel}"
    else:
        target = f"{p_name} | {sel}"

    # Записываем сигнал в боевую таблицу со статусом EXECUTE_MANUAL
    # Роутер (bet_router.py) моментально подхватит эту запись!
    c.execute(
        '''INSERT INTO bet_signals (match_name, market_type, target, line, expected_kf, edge, status, bookmaker) 
           VALUES (?, ?, ?, ?, ?, ?, 'EXECUTE_MANUAL', ?)''',
        (match_name, market, target, line, target_kf, 0.0, bk_full_name)
    )
    conn.commit()
    conn.close()

    # Тихое уведомление, что сигнал улетел
    await callback.answer("✅ Сигнал успешно отправлен в Роутер!", show_alert=False)



def extract_last_name(alias):
    if not alias:
        return ""
    name = alias.replace('ё', 'е').replace('Ё', 'Е')
    first_word = name.split()[0].lower()
    return first_word[:5]

def get_real_current_kf(cursor, match_name, market, player_name, line, default_kf, match_id=None):
    kfs = get_all_kfs_for_bet(cursor, match_name, market, player_name, line, default_kf, 'FONBET', match_id)
    return kfs.get('FONBET', default_kf)

def get_all_kfs_for_bet(cursor, match_name, market, player_name, line, default_kf, default_bookmaker, match_id=None):
    kfs = {default_bookmaker: default_kf}

    # ИСПРАВЛЕНИЕ: Мы не перезаписываем market! Используем реальный (PLAYER_PTS, PLAYER_REB и т.д.)
    is_player_prop = market.startswith("PLAYER_") and market != "PLAYER_H2H"

    # --- 1. ПЛЕЕРСКИЕ ПРОПЫ (Ищем по всем БК по имени игрока, без привязки к ID матча) ---
    if is_player_prop:
        from config import Config
        mappings = Config.get_mappings()
        player_map = mappings.get("PLAYER_MAP", {})

        # Находим все возможные русские имена этого игрока (и для Фонбета, и для Бетсити)
        aliases = [ru_name for ru_name, en_name in player_map.items() if en_name == player_name]
        if not aliases:
            aliases = [player_name]

        # Extract roots using the new robust function
        roots = list(set([extract_last_name(alias) for alias in aliases]))

        # SQLite LIKE is case-insensitive for ASCII, but might fail for Cyrillic LOWER().
        # So we pass the root with capitalized first letter as well to be safe.
        conditions = " OR ".join(["player_or_team LIKE ? OR player_or_team LIKE ?" for _ in roots])
        params_like = []
        for root in roots:
            params_like.extend([f"%{root}%", f"%{root.capitalize()}%"])

        query = f"""
            SELECT bookmaker, over_kf, under_kf
            FROM (
                SELECT bookmaker, over_kf, under_kf,
                       ROW_NUMBER() OVER(PARTITION BY bookmaker ORDER BY timestamp DESC) as rn
                FROM odds_history
                WHERE market_type = ? 
                  AND ({conditions})
                  AND ABS(line - ?) < 0.01
                  AND timestamp >= datetime('now', '-24 hours')
            )
            WHERE rn = 1
        """
        params = [market] + params_like + [line]
        cursor.execute(query, params)

        for row in cursor.fetchall():
            bk, o, u = row
            if abs(o - float(default_kf)) < abs(u - float(default_kf)):
                kfs[bk] = o
            else:
                kfs[bk] = u

    # --- 2. ДУЭЛИ ИГРОКОВ (H2H) ---
    elif market == "PLAYER_H2H":
        from config import Config
        mappings = Config.get_mappings()
        player_map = mappings.get("PLAYER_MAP", {})

        parts = player_name.split(' vs ')
        if len(parts) != 2:
            parts = player_name.split(' - ')

        if len(parts) == 2:
            p1_en, p2_en = parts[0].strip(), parts[1].strip()
            p1_aliases = [ru_name for ru_name, en_name in player_map.items() if en_name == p1_en]
            if not p1_aliases: p1_aliases = [p1_en]
            p2_aliases = [ru_name for ru_name, en_name in player_map.items() if en_name == p2_en]
            if not p2_aliases: p2_aliases = [p2_en]

            query = "SELECT bookmaker, over_kf, under_kf, player_or_team FROM odds_history WHERE market_type = 'PLAYER_H2H' AND timestamp >= datetime('now', '-24 hours') ORDER BY timestamp DESC"
            cursor.execute(query)

            # Dictionary to store the most recent odds per bookmaker
            found_kfs = {}
            for row in cursor.fetchall():
                bk, o, u, p_or_t = row
                p_or_t_lower = p_or_t.lower()

                # Use the robust root extraction for matching
                p1_roots = [extract_last_name(a) for a in p1_aliases]
                p2_roots = [extract_last_name(a) for a in p2_aliases]

                p1_match = any(root in p_or_t_lower for root in p1_roots)
                p2_match = any(root in p_or_t_lower for root in p2_roots)

                if p1_match and p2_match:
                    if bk not in found_kfs:
                        found_kfs[bk] = []
                    found_kfs[bk].append((o, u))

            for bk, kfs_list in found_kfs.items():
                o, u = kfs_list[0]
                if abs(o - float(default_kf)) < abs(u - float(default_kf)):
                    kfs[bk] = o
                else:
                    kfs[bk] = u

    # --- 3. ИСХОДЫ И ТОТАЛЫ (Строго по ID матча, чтобы не спутать форы разных игр) ---
    else:
        p_or_t = "GAME" if player_name == "GAME" else player_name
        if match_id is not None:
            query = """
                SELECT bookmaker, over_kf, under_kf
                FROM (
                    SELECT bookmaker, over_kf, under_kf,
                           ROW_NUMBER() OVER(PARTITION BY bookmaker ORDER BY timestamp DESC) as rn
                FROM odds_history
                WHERE market_type = ? AND player_or_team = ? AND line = ? 
                  AND (event_id = ? OR parent_event_id = ?)
                )
                WHERE rn = 1
            """
            cursor.execute(query, (market, p_or_t, line, str(match_id), str(match_id)))

            for row in cursor.fetchall():
                bk, o, u = row
                if abs(o - float(default_kf)) < abs(u - float(default_kf)):
                    kfs[bk] = o
                else:
                    kfs[bk] = u

    return kfs


def _fetch_math_log_db(match_id):
    conn = db.get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT math_log FROM match_tracking WHERE match_id = ?", (match_id,))
        row = c.fetchone()
        math_log = row[0] if row and row[0] else "Лог расчетов недоступен."
    except:
        math_log = "Лог расчетов недоступен."
    finally:
        conn.close()
    return math_log


@dp.callback_query(F.data.startswith("mathlog_"))
async def show_math_log(callback: CallbackQuery):
    user_id = callback.from_user.id
    if not db.check_subscription(user_id):
        await callback.answer("⏳ Только для подписчиков. Приобретите доступ.", show_alert=True)
        return

    match_id = callback.data.split("_")[1]

    math_log = await asyncio.to_thread(_fetch_math_log_db, match_id)

    back_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"match_{match_id}")]
    ])

    text = f"🧮 <b>Хронология расчета ядра</b>\n〰️〰️〰️〰️〰️〰️〰️〰️〰️\n{math_log}"

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_markup)
    except:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=back_markup)

    await callback.answer()


def _get_closest_lines_db(match_id, category, proj_score1, proj_score2):
    if proj_score1 is None or proj_score2 is None:
        return []
    conn = db.get_connection()
    c = conn.cursor()

    market_type = 'GAME_TOTAL' if category == "ТОТАЛ" else 'HANDICAP'

    try:
        c.execute("""
            SELECT line, over_kf, under_kf
            FROM (
                SELECT line, over_kf, under_kf,
                       ROW_NUMBER() OVER(PARTITION BY line ORDER BY timestamp DESC) as rn
                FROM odds_history
                WHERE event_id = ? AND market_type = ?
            ) WHERE rn = 1
        """, (match_id, market_type))
        lines = c.fetchall()
    except Exception:
        lines = []
    finally:
        conn.close()

    if not lines:
        return []

    if category == "ТОТАЛ":
        target = proj_score1 + proj_score2
        optimal_line = None
        best_margin = -1

        for line_val, over_kf, under_kf in lines:
            line_val_f = float(line_val)

            if target > line_val_f and over_kf >= 1.75:
                margin = target - line_val_f
                if margin > best_margin:
                    best_margin = margin
                    optimal_line = (line_val, over_kf, under_kf, "Тотал Больше", line_val, over_kf)

            elif target < line_val_f and under_kf >= 1.75:
                margin = line_val_f - target
                if margin > best_margin:
                    best_margin = margin
                    optimal_line = (line_val, over_kf, under_kf, "Тотал Меньше", line_val, under_kf)

        return [optimal_line] if optimal_line else []
    else:
        optimal_line = None
        max_handicap_val = -1

        for line_val, over_kf, under_kf in lines:
            line_val_f = float(line_val)

            if line_val_f > 0 and over_kf >= 1.75 and abs(line_val_f) < 35:
                h_val = line_val_f
                if h_val > max_handicap_val:
                    max_handicap_val = h_val
                    optimal_line = (line_val, over_kf, under_kf, "Фора 1", h_val, over_kf)

            elif line_val_f < 0 and under_kf >= 1.75 and abs(line_val_f) < 35:
                h_val = abs(line_val_f)
                if h_val > max_handicap_val:
                    max_handicap_val = h_val
                    optimal_line = (line_val, over_kf, under_kf, "Фора 2", h_val, under_kf)

        return [optimal_line] if optimal_line else []


def _fetch_category_predictions_db(user_id, match_id, category):
    conn = db.get_connection()
    c = conn.cursor()

    c.execute("SELECT is_vip, tier FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    is_vip = bool(row[0]) if row else False
    tier = row[1] if row and len(row) > 1 else 'free'
    if user_id == Config.ADMIN_ID:
        is_vip = True
        tier = 'pro'

    c.execute("SELECT team1, team2, status, proj_score1, proj_score2 FROM match_tracking WHERE match_id = ?",
              (match_id,))
    res = c.fetchone()
    if not res:
        conn.close()
        return None, None, None, None, None, None, False

    team1, team2, status, proj_score1, proj_score2 = res
    match_name = f"{team1} - {team2}"

    is_preliminary_filter = 1 if status in ('WAITING_REFS', 'WAITING_ROSTERS') else 0

    try:
        if category == 'ДУЭЛИ':
            c.execute(
                "SELECT market, player_name, line, prediction, selection, kf, vip_kf, published_at, bookmaker "
                "FROM virtual_bets "
                "WHERE match_name = ? AND market = 'PLAYER_H2H' AND is_preliminary = ? "
                "ORDER BY kf DESC",
                (match_name, is_preliminary_filter)
            )
        elif category == 'ИГРОК':
            c.execute(
                "SELECT market, player_name, line, prediction, selection, kf, vip_kf, published_at, bookmaker "
                "FROM virtual_bets "
                "WHERE match_name = ? AND category = ? AND market != 'PLAYER_H2H' AND is_preliminary = ? "
                "ORDER BY kf DESC",
                (match_name, category, is_preliminary_filter)
            )
        else:
            c.execute(
                "SELECT market, player_name, line, prediction, selection, kf, vip_kf, published_at, bookmaker "
                "FROM virtual_bets "
                "WHERE match_name = ? AND category = ? AND is_preliminary = ? "
                "ORDER BY kf DESC",
                (match_name, category, is_preliminary_filter)
            )
        bets = c.fetchall()
    except sqlite3.OperationalError:
        if category == 'ДУЭЛИ':
            c.execute(
                "SELECT market, player_name, line, prediction, selection, kf, vip_kf, published_at "
                "FROM virtual_bets "
                "WHERE match_name = ? AND market = 'PLAYER_H2H' AND is_preliminary = ? "
                "ORDER BY kf DESC",
                (match_name, is_preliminary_filter)
            )
        elif category == 'ИГРОК':
            c.execute(
                "SELECT market, player_name, line, prediction, selection, kf, vip_kf, published_at "
                "FROM virtual_bets "
                "WHERE match_name = ? AND category = ? AND market != 'PLAYER_H2H' AND is_preliminary = ? "
                "ORDER BY kf DESC",
                (match_name, category, is_preliminary_filter)
            )
        else:
            c.execute(
                "SELECT market, player_name, line, prediction, selection, kf, vip_kf, published_at "
                "FROM virtual_bets "
                "WHERE match_name = ? AND category = ? AND is_preliminary = ? "
                "ORDER BY kf DESC",
                (match_name, category, is_preliminary_filter)
            )
        raw_bets = c.fetchall()
        bets = [(*b, 'FONBET') for b in raw_bets]

    processed_bets = []
    import datetime

    # --- H2H PATCH START ---
    # If the category is 'ДУЭЛИ' and virtual_bets returned nothing, try to fetch raw lines from odds_history
    if category == 'ДУЭЛИ' and not bets:
        try:
            # We fetch H2H odds from the last 24 hours that are relevant to this match date (we don't have event_id for H2H reliably, so we take recent ones)
            c.execute("""
                SELECT DISTINCT player_or_team, line, over_kf, under_kf, bookmaker
                FROM odds_history
                WHERE market_type = 'PLAYER_H2H'
                  AND timestamp >= datetime('now', '-24 hours')
                ORDER BY timestamp DESC
            """)
            raw_h2h_odds = c.fetchall()

            # Map raw odds back into a format that looks like 'virtual_bets' records
            # b = (market, p_name, line, proj, sel, kf, vip_kf, pub_at, bookmaker)

            # Deduplicate by player_or_team and line
            seen_duels = set()
            for r in raw_h2h_odds:
                p_or_t, line, o_kf, u_kf, bk = r

                # Check if this duel likely belongs to the teams playing (basic heuristic, skip if we can't tell, but since it's raw, we might just show everything recent if we don't have a strict filter. For safety, we will just show them since H2H are rare)

                key = f"{p_or_t}_{line}"
                if key not in seen_duels:
                    seen_duels.add(key)
                    # Create pseudo-bets to display
                    # P1 wins: over
                    bets.append(('PLAYER_H2H', p_or_t, line, 0.0, 'П1', o_kf, o_kf, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), bk))
                    # P2 wins: under
                    bets.append(('PLAYER_H2H', p_or_t, line, 0.0, 'П2', u_kf, u_kf, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), bk))
        except Exception as e:
            import logging
            logging.error(f"Error fetching raw H2H for patch: {e}")
    # --- H2H PATCH END ---

    for b in bets:
        market, p_name, line, proj, sel, kf, vip_kf, pub_at, bookmaker = b

        if tier == 'standard' and (bookmaker != 'FONBET' or category == 'ИГРОК'):
            continue

        try:
            pub_time = datetime.datetime.strptime(pub_at, '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            pub_time = datetime.datetime.now()

        time_passed = (datetime.datetime.now() - pub_time).total_seconds() / 60.0

        hidden = False
        minutes_left = 0
        real_kf = kf
        all_kfs = {}

        # Always fetch odds to ensure we can filter by bookmaker for DUELS even if hidden
        all_kfs = get_all_kfs_for_bet(c, match_name, market, p_name, line, kf, bookmaker, match_id=match_id)
        if category == 'ДУЭЛИ':
            if 'BETCITY' not in all_kfs:
                continue
            real_kf = all_kfs['BETCITY']
            kf = real_kf
            bookmaker = 'BETCITY'
            all_kfs = {'BETCITY': real_kf}
        else:
            real_kf = all_kfs.get(bookmaker, kf)

        if not is_vip and time_passed < 30 and status != 'PRELIM_READY':
            hidden = True
            minutes_left = int(30 - time_passed)

        processed_bets.append({
            'market': market, 'p_name': p_name, 'line': line, 'proj': proj,
            'sel': sel, 'kf': kf, 'vip_kf': vip_kf, 'real_kf': real_kf,
            'hidden': hidden, 'minutes_left': minutes_left, 'bookmaker': bookmaker,
            'all_kfs': all_kfs
        })

    conn.close()
    return is_vip, match_name, status, proj_score1, proj_score2, processed_bets, True


@dp.callback_query(F.data.startswith("cat_"))
async def show_category_prediction(callback: CallbackQuery):
    user_id = callback.from_user.id
    data_parts = callback.data.split("_")
    category = data_parts[1]
    match_id = data_parts[2]
    page = int(data_parts[3]) if len(data_parts) > 3 else 0

    is_vip, match_name, status, proj_score1, proj_score2, bets, match_found = await asyncio.to_thread(
        _fetch_category_predictions_db, user_id, match_id, category
    )

    if not match_found:
        await callback.message.edit_text("Матч не найден.")
        return

    is_waiting = status in ('WAITING_REFS', 'WAITING_ROSTERS')

    text = f"📊 <b>Прогнозы: {category}</b>\n🏀 {match_name}\n"

    if category in ["ИСХОД", "ТОТАЛ", "ФОРА"]:
        if proj_score1 is not None and proj_score2 is not None:
            s1 = int(round(float(proj_score1)))
            s2 = int(round(float(proj_score2)))
            total_proj = s1 + s2

            text += f"🧮 Ожидаемый счет: <b>{s1} : {s2}</b>\n"

            if category == "ТОТАЛ":
                text += f"🎯 Расчетный Тотал: <b>{total_proj}</b>\n"
            elif category in ["ИСХОД", "ФОРА"]:
                margin = s1 - s2
                if margin > 0:
                    fora_str = f"Фора 1 (-{margin})"
                elif margin < 0:
                    fora_str = f"Фора 2 (-{abs(margin)})"
                else:
                    fora_str = "Равная игра"
                text += f"⚖️ <b>{fora_str}</b>\n"
        else:
            text += f"🧮 Ожидаемый счет: <b>Н/Д</b>\n"

        if is_waiting:
            text += f"⚠️ Внимание: Составы команд и судейские бригады еще не назначены. Представлен базовый математический расчет.\n"

    if not bets:
        if category == "ИГРОК":
            text += f"\nВ категории '{category}' пока нет валуйных прогнозов."
        else:
            text += f"\nВ категории '{category}' пока нет валуйных прогнозов."
            if proj_score1 is not None and proj_score2 is not None:
                closest_lines = await asyncio.to_thread(_get_closest_lines_db, match_id, category, proj_score1,
                                                        proj_score2)
                if closest_lines:
                    fonbet_url = f"https://fon.bet/sports/basketball/country/unitedstates/125064/{match_id}"
                    if category == "ТОТАЛ":
                        opt_line = closest_lines[0]
                        if len(opt_line) == 6:
                            _, _, _, total_name, l_val, kf_val = opt_line
                            text += f"\n\n🎯 Оптимальный выбор: {total_name} (<b>{l_val}</b>) | <a href='{fonbet_url}'>🚀 Кэф: {kf_val}</a>\n"
                    else:
                        text += f"\n\n"
                        opt_line = closest_lines[0]
                        if len(opt_line) == 6:
                            _, _, _, fora_name, h_val, kf_val = opt_line
                            text += f"🎯 Оптимальный выбор: {fora_name} (<b>+{h_val}</b>) | <a href='{fonbet_url}'>🚀 Кэф: {kf_val}</a>\n"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К категориям", callback_data=f"match_{match_id}")]
        ])
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard,
                                             disable_web_page_preview=True)
        except:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
        return

    if category in ["ИГРОК", "ДУЭЛИ"]:
        bets.sort(key=lambda x: (x['p_name'], -float(x['kf'])))
        ITEMS_PER_PAGE = 4
    else:
        ITEMS_PER_PAGE = 3

    total_pages = (len(bets) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_bets = bets[start_idx:end_idx]

    text += f"\nСтраница {page + 1} из {total_pages}\n\n"

    current_player = None

    for b in page_bets:
        if b['hidden']:
            if category in ["ИГРОК", "ДУЭЛИ"]:
                if b['p_name'] != current_player:
                    current_player = b['p_name']
                    if category == "ДУЭЛИ":
                        text += f"⚔️ <b>ДУЭЛЬ: Скрыто</b>\n"
                    else:
                        text += f"👤 <b>ИГРОК: Скрыто</b>\n"

                market_map = {
                    "PLAYER_PTS": "Очки",
                    "PLAYER_REB": "Подборы",
                    "PLAYER_FG3M": "Трехочковые",
                    "PLAYER_AST": "Передачи",
                    "PLAYER_PF": "Фолы",
                    "PLAYER_H2H": "Дуэль (H2H)"
                }
                market_ru = market_map.get(b['market'], b['market'])
                text += f"├ 🔒 {market_ru}\n"
                text += f"├ Выбор: <b>Скрыто</b> {b['line']}\n"
                text += f"├ Проекция: Скрыто\n"
                text += f"└ <i>Доступно через {b['minutes_left']} мин. (Или купите VIP)</i>\n\n"
            else:
                target = f"Рынок: <b>Скрыто</b>"
                text += f"🔒 {target}\n"
                text += f"   Выбор: <b>Скрыто</b> | Линия: {b['line']}\n"
                text += f"   <i>Доступно через {b['minutes_left']} мин. (Или купите VIP)</i>\n\n"
        else:
            fonbet_url = f"https://fon.bet/sports/basketball/country/unitedstates/125064/{match_id}"

            all_kfs = b.get('all_kfs', {})
            kfs_strs = []

            for bk, kf_val in all_kfs.items():
                bk_icon = "🔴 Fon" if bk == 'FONBET' else "🏙 Bet" if bk == 'BETCITY' else bk
                if bk == b.get('bookmaker') and b['vip_kf'] and float(kf_val) != float(b['vip_kf']):
                    kfs_strs.append(f"{bk_icon}: <s>{b['vip_kf']}</s> ➡️ <b>{kf_val}</b>")
                else:
                    kfs_strs.append(f"{bk_icon}: <b>{kf_val}</b>")

            if not kfs_strs:
                bk_icon = "🔴 Fon" if b.get('bookmaker') == 'FONBET' else "🏙 Bet" if b.get('bookmaker') == 'BETCITY' else b.get('bookmaker', '🔴 Fon')
                kf_display = f"<s>{b['vip_kf']}</s> ➡️ <b>{b['real_kf']}</b>" if b['vip_kf'] and float(
                    b['real_kf']) != float(b['vip_kf']) else f"<b>{b['kf']}</b>"
                kfs_strs.append(f"{bk_icon}: {kf_display}")

            kfs_str = " | ".join(kfs_strs)

            if category in ["ИГРОК", "ДУЭЛИ"]:
                if b['p_name'] != current_player:
                    current_player = b['p_name']
                    if category == "ДУЭЛИ":
                        text += f"⚔️ <b>ДУЭЛЬ: {current_player}</b>\n"
                    else:
                        text += f"👤 <b>ИГРОК: {current_player}</b>\n"

                market_map = {
                    "PLAYER_PTS": "Очки",
                    "PLAYER_REB": "Подборы",
                    "PLAYER_FG3M": "Трехочковые",
                    "PLAYER_AST": "Передачи",
                    "PLAYER_PF": "Фолы",
                    "PLAYER_H2H": "Дуэль (H2H)"
                }
                market_ru = market_map.get(b['market'], b['market'])

                text += f"├ {market_ru}\n"
                text += f"├ Выбор: <b>{b['sel']}</b> {b['line']}\n"
                text += f"├ Проекция: {b['proj']:.1f}\n"
                text += f"└ <a href='{fonbet_url}'>🚀 Кэфы: {kfs_str}</a>\n\n"
            else:
                target = f"Рынок: <b>{b['p_name']}</b>"
                text += f"🔹 {target}\n"
                text += f"   Прогноз: {b['proj']:.1f} | Линия бука: {b['line']}\n"
                text += f"   Выбор: <b>{b['sel']}</b> | <a href='{fonbet_url}'>🚀 Кэфы: {kfs_str}</a>\n\n"

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"cat_{category}_{match_id}_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"cat_{category}_{match_id}_{page + 1}"))

    inline_keyboard = []
    if nav_buttons:
        inline_keyboard.append(nav_buttons)

    inline_keyboard.append([InlineKeyboardButton(text="🔙 К категориям", callback_data=f"match_{match_id}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_keyboard)

    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        pass
    await callback.answer()


async def _show_tariffs_logic(message_or_call):
    std_price, std_days = db.get_standard_settings()
    vip_price, vip_days = db.get_vip_settings()

    std_formatted = int(std_price) if std_price.is_integer() else std_price
    vip_formatted = int(vip_price) if vip_price.is_integer() else vip_price

    user_id = message_or_call.from_user.id
    has_sub = db.check_subscription(user_id)

    inline_kb = [
        [InlineKeyboardButton(text=f"Standard: {std_days} дней — {std_formatted} руб", callback_data="buy_standard")],
        [InlineKeyboardButton(text=f"Pro: {vip_days} дней — {vip_formatted} руб", callback_data="buy_pro")],
        [InlineKeyboardButton(text="Auto-Pilot: Режим закрыт (Beta)", callback_data="buy_autopilot")]
    ]

    if not has_sub:
        inline_kb.insert(0, [InlineKeyboardButton(text="🎁 Получить Trial (PRO на 3 дня)", callback_data="get_trial")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_kb)

    text = ("<b>Тарифы на подписку:</b>\n\n"
            "<b>Standard:</b> Доступ к прогнозам Moneyline, Тоталы, Форы.\n"
            "<b>Pro:</b> Всё из Standard + индивидуальная статистика игроков (PLAYER_PTS, REB, FG3M).")

    if isinstance(message_or_call, CallbackQuery):
        try:
            await message_or_call.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except:
            await message_or_call.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await message_or_call.answer(text, reply_markup=keyboard, parse_mode="HTML")


@dp.message(F.text == "🛒 Тарифы и Подписка")
async def show_tariffs_message(message: Message):
    await _show_tariffs_logic(message)


@dp.callback_query(F.data == "show_tariffs")
async def show_tariffs(callback: CallbackQuery):
    await _show_tariffs_logic(callback)
    await callback.answer()


async def check_payment_status(payment_id: str, message: Message, user_id: int, invoice_id: str, tier: str, days: int,
                               max_attempts: int = 300):
    attempts = 0
    while attempts < max_attempts:
        try:
            payment = await asyncio.to_thread(Payment.find_one, payment_id)
            if payment.status == 'succeeded':
                db.update_invoice_status(invoice_id, 'PAID')

                is_vip = 1 if tier == 'pro' else 0
                db.grant_subscription(user_id, days, is_vip=is_vip)

                conn = db.get_connection()
                cursor = conn.cursor()
                cursor.execute("UPDATE users SET tier = ? WHERE user_id = ?", (tier, user_id))
                conn.commit()
                conn.close()

                tier_name = "Pro" if tier == "pro" else "Standard"
                success_text = (
                    f"✅ <b>Оплата успешно получена!</b>\n\n"
                    f"Вам начислено {days} дней подписки по тарифу <b>{tier_name}</b>."
                )

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🏀 К прогнозам", callback_data="show_predictions")]
                ])

                try:
                    await message.edit_text(success_text, reply_markup=keyboard, parse_mode="HTML")
                except:
                    await message.answer(success_text, reply_markup=keyboard, parse_mode="HTML")
                return

            elif payment.status == 'canceled':
                db.update_invoice_status(invoice_id, 'CANCELED')
                cancel_text = "❌ <b>Платёж был отменён.</b>\nЕсли это ошибка, попробуйте создать новый платёж в разделе тарифов."

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 К тарифам", callback_data="show_tariffs")]
                ])
                try:
                    await message.edit_text(cancel_text, reply_markup=keyboard, parse_mode="HTML")
                except:
                    await message.answer(cancel_text, reply_markup=keyboard, parse_mode="HTML")
                return

        except Exception as e:
            logging.error(f"Ошибка при проверке платежа {payment_id}: {e}")

        attempts += 1
        await asyncio.sleep(3)


@dp.callback_query(F.data == "buy_autopilot")
async def handle_buy_autopilot(callback: CallbackQuery):
    await callback.answer(
        "Режим автоматических ставок находится в закрытом бета-тесте. Релиз запланирован на старт сезона NBA",
        show_alert=True)


@dp.callback_query(F.data == "get_trial")
async def handle_get_trial(callback: CallbackQuery):
    user_id = callback.from_user.id

    conn = db.get_connection()
    c = conn.cursor()
    c.execute("SELECT has_used_trial FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        await callback.answer("Ошибка пользователя. Попробуйте нажать /start", show_alert=True)
        return

    has_used_trial = bool(row[0])

    if has_used_trial:
        conn.close()
        await callback.answer("Вы уже использовали пробный период.", show_alert=True)
        return

    now = datetime.datetime.now()
    trial_end = now + datetime.timedelta(days=3)

    c.execute("UPDATE users SET has_used_trial = 1, tier = 'pro', subscription_end = ?, is_vip = 1 WHERE user_id = ?",
              (trial_end, user_id))
    conn.commit()
    conn.close()

    await callback.answer("Успешно! Вам начислено 3 дня PRO-доступа.", show_alert=True)

    try:
        await callback.message.delete()
        await callback.message.answer(
            "🎉 Поздравляем! Вам активирован **PRO-доступ** на 3 дня!\n\nТеперь вам доступны индивидуальные прогнозы по игрокам и ставки через Betcity.",
            parse_mode="Markdown")
    except:
        pass


@dp.callback_query(F.data.in_({"buy_standard", "buy_pro"}))
async def buy_subscription(callback: CallbackQuery):
    if not Configuration.account_id or not Configuration.secret_key:
        await callback.message.answer("Оплата временно недоступна (не настроены ключи YooKassa).")
        await callback.answer()
        return

    tier = "pro" if callback.data == "buy_pro" else "standard"

    if tier == "pro":
        price, days = db.get_vip_settings()
        description = f"Pro Подписка на {days} дней"
    else:
        price, days = db.get_standard_settings()
        description = f"Подписка Standard на {days} дней"

    formatted_price = int(price) if price.is_integer() else price
    invoice_id = str(uuid.uuid4())
    user_id = callback.from_user.id

    try:
        payment_data = {
            "amount": {
                "value": f"{price:.2f}",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/" + (await callback.bot.me()).username
            },
            "capture": True,
            "description": description,
            "receipt": {
                "customer": {
                    "email": "customer@example.com"
                },
                "items": [
                    {
                        "description": description,
                        "quantity": "1.00",
                        "amount": {
                            "value": f"{price:.2f}",
                            "currency": "RUB"
                        },
                        "vat_code": 7,
                        "payment_mode": "full_prepayment",
                        "payment_subject": "service"
                    }
                ]
            },
            "metadata": {
                "invoice_id": invoice_id,
                "user_id": user_id,
                "days": days,
                "tier": tier
            }
        }
        payment = await asyncio.to_thread(Payment.create, payment_data, invoice_id)

        db.create_invoice(invoice_id, user_id, price)

        payment_url = payment.confirmation.confirmation_url

        text = (
            f"💳 <b>{description}</b>\n\n"
            f"<b>Сумма к оплате:</b> {formatted_price} RUB\n\n"
            f"Перейдите по кнопке ниже для безопасной оплаты. Мы автоматически проверим платёж в течение 15 минут."
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=payment_url)],
            [InlineKeyboardButton(text="🔙 Назад к тарифам", callback_data="show_tariffs")]
        ])

        msg = await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML",
                                               disable_web_page_preview=True)

        asyncio.create_task(check_payment_status(
            payment_id=payment.id,
            message=msg,
            user_id=user_id,
            invoice_id=invoice_id,
            tier=tier,
            days=days
        ))

    except Exception as e:
        logging.error(f"YooKassa Create Payment Error: {e}")
        await callback.message.answer("Произошла ошибка при создании платежа. Попробуйте позже.")

    await callback.answer()


# === АДМИНСКИЕ КОМАНДЫ ===
@dp.message(Command("set_tariff"))
async def cmd_set_tariff(message: Message):
    if message.from_user.id != Config.ADMIN_ID:
        return

    args = message.text.split()
    if len(args) != 3:
        await message.answer("Использование: /set_tariff <цена> <дни>\nПример: /set_tariff 1500 30")
        return

    try:
        new_price = float(args[1])
        new_days = int(args[2])
    except ValueError:
        await message.answer("Ошибка: Цена должна быть числом, а дни - целым числом.")
        return

    db.update_vip_settings(new_price, new_days)
    formatted_price = int(new_price) if new_price.is_integer() else new_price

    await message.answer(f"✅ Тариф успешно обновлен: {formatted_price} руб. за {new_days} дней")


@dp.message(Command("give_sub"))
async def cmd_give_sub(message: Message):
    if message.from_user.id != Config.ADMIN_ID: return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Формат: /give_sub <user_id> <days>")
        return
    try:
        user_id = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await message.answer("ID и количество дней должны быть числами.")
        return

    updated = db.grant_vip_days(user_id, days)
    if updated:
        await message.answer(f"Успех. Пользователю {user_id} добавлено {days} дней VIP.")
    else:
        await message.answer("Ошибка: пользователь не найден в БД. Пусть нажмет /start")


@dp.message(Command("revoke_sub"))
async def cmd_revoke_sub(message: Message):
    if message.from_user.id != Config.ADMIN_ID: return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("Формат: /revoke_sub <user_id>")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    updated = db.revoke_vip(user_id)
    if updated:
        await message.answer(f"Успех. Пользователь {user_id} лишен VIP-доступа.")
    else:
        await message.answer("Ошибка: пользователь не найден в БД.")


@dp.message(Command("add_player"))
async def cmd_add_player(message: Message):
    if message.from_user.id != Config.ADMIN_ID: return

    text = message.text.replace("/add_player", "").strip()
    if "|" not in text:
        await message.answer(
            "Ошибка формата. Используйте разделитель '|'. Пример:\n/add_player \"Грайнер Б\" | \"B. Griner\"")
        return

    parts = text.split("|")
    name_ru = parts[0].strip().strip('"').strip("'")
    name_en = parts[1].strip().strip('"').strip("'")

    if not name_ru or not name_en:
        await message.answer("Ошибка: Имя не может быть пустым.")
        return

    import json
    import os
    import re

    mapping_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'mappings.json')
    if os.path.exists(mapping_path):
        with open(mapping_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if "PLAYER_MAP" not in data:
            data["PLAYER_MAP"] = {}

        data["PLAYER_MAP"][name_ru] = name_en

        with open(mapping_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        cleaned_ru = re.sub(r'\s+', '', name_ru)
        await message.answer(
            f"✅ Успешно!\nДобавлено в базу: {name_ru} ({cleaned_ru}) -> {name_en}.\nИзменения вступят в силу автоматически при следующем расчете матча.")
    else:
        await message.answer("❌ Ошибка: файл mappings.json не найден.")


@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    terms_text = (
        "Сервис предоставляет доступ к агрегированным спортивным данным. "
        "Мы не принимаем ставки и не даем финансовых рекомендаций. "
        "Вся аналитика носит справочный характер."
    )
    await message.answer(terms_text)


@dp.message(F.text == "👥 Подписчики")
async def show_subscribers(message: Message):
    if message.from_user.id != Config.ADMIN_ID:
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активные", callback_data="subs_active")],
        [InlineKeyboardButton(text="❌ Неактивные (Истекли)", callback_data="subs_inactive")]
    ])

    await message.answer("Выберите список подписчиков:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("subs_"))
async def handle_subs_filter(callback: CallbackQuery):
    if callback.from_user.id != Config.ADMIN_ID:
        await callback.answer()
        return

    filter_type = callback.data.split("_")[1]
    users = db.get_all_users()
    if not users:
        await callback.message.edit_text("Список пользователей пуст.")
        return

    filtered_users = []

    for u in users:
        user_id, username, first_name, joined_at, sub_end, is_vip, tier = u

        is_active = False
        if sub_end:
            try:
                dt_end = datetime.datetime.strptime(sub_end, '%Y-%m-%d %H:%M:%S.%f')
            except ValueError:
                dt_end = datetime.datetime.strptime(sub_end, '%Y-%m-%d %H:%M:%S')
            is_active = datetime.datetime.now() <= dt_end

        if user_id == Config.ADMIN_ID:
            is_active = True

        if (filter_type == "active" and is_active) or (filter_type == "inactive" and not is_active):
            if tier == 'autopilot':
                tier_badge = "🤖 Auto"
            elif tier == 'pro' or is_vip:
                tier_badge = "👑 Pro"
            elif tier == 'standard':
                tier_badge = "👤 Std"
            else:
                tier_badge = "🆓 Free"

            status_icon = "✅" if is_active else "❌"
            filtered_users.append(
                f"{tier_badge} {first_name} (@{username if username else 'N/A'}) ID: {user_id} {status_icon}")

    if not filtered_users:
        response = f"Список {'активных' if filter_type == 'active' else 'неактивных'} пользователей пуст."
    else:
        title = "✅ Активные подписчики" if filter_type == "active" else "❌ Неактивные подписчики"
        response = f"{title}:\n\n" + "\n".join(filtered_users)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="subs_back")]
    ])

    if len(response) > 4000:
        chunks = [response[i:i + 4000] for i in range(0, len(response), 4000)]
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await callback.message.answer(chunk, reply_markup=keyboard)
            else:
                await callback.message.answer(chunk)
    else:
        await callback.message.edit_text(response, reply_markup=keyboard)

    await callback.answer()


@dp.callback_query(F.data == "subs_back")
async def handle_subs_back(callback: CallbackQuery):
    if callback.from_user.id != Config.ADMIN_ID:
        await callback.answer()
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Активные", callback_data="subs_active")],
        [InlineKeyboardButton(text="❌ Неактивные (Истекли)", callback_data="subs_inactive")]
    ])

    await callback.message.edit_text("Выберите список подписчиков:", reply_markup=keyboard)
    await callback.answer()


@dp.message(F.text == "🚀 Запустить предиктор")
async def handle_run_predictor_btn(message: Message):
    if message.from_user.id != Config.ADMIN_ID:
        return

    await message.answer("⏳ Запускаю ядро main_predictor.py в фоновом режиме...")

    try:
        script_path = os.path.join(os.path.dirname(__file__), '..', 'betting_manager', 'main_predictor.py')
        process = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await process.communicate()

        if process.returncode == 0:
            await message.answer(f"✅ Предиктор успешно отработал!")
        else:
            import html
            err_msg = html.escape(stderr.decode('utf-8')[:1000])
            await message.answer(
                f"❌ Предиктор завершился с ошибкой (код {process.returncode}):\n<code>{err_msg}</code>",
                parse_mode="HTML")

    except Exception as e:
        import html
        await message.answer(f"❌ Ошибка при запуске предиктора: {html.escape(str(e))}")


@dp.message(Command("force_predict"))
async def cmd_force_predict(message: Message):
    if message.from_user.id != Config.ADMIN_ID:
        return
    await handle_run_predictor_btn(message)


def _route_signal_sync(sig_id, action):
    """Синхронный обработчик маршрутизации для БД"""
    conn = db.get_connection()
    try:
        c = conn.cursor()
        if action == "storeA":
            c.execute("UPDATE bet_signals SET status = 'EXECUTE_A' WHERE id = ?", (sig_id,))
            status_text = "Заявка отправлена в работу (Store A)"
        elif action == "storeB":
            c.execute("UPDATE bet_signals SET status = 'EXECUTE_B' WHERE id = ?", (sig_id,))
            status_text = "Заявка отправлена в работу (Store B)"
        elif action == "skip":
            c.execute("UPDATE bet_signals SET status = 'REJECTED' WHERE id = ?", (sig_id,))
            status_text = "Заявка пропущена (REJECTED)"
        else:
            status_text = "Неизвестное действие"

        conn.commit()
        return status_text
    except Exception as e:
        logging.error(f"Route handler db error: {e}")
        return "Ошибка при обновлении БД"
    finally:
        conn.close()


@dp.callback_query(F.data.startswith("route_"))
async def handle_route_decision(callback: CallbackQuery):
    if callback.from_user.id != Config.ADMIN_ID:
        await callback.answer()
        return

    data_parts = callback.data.split("_")
    action = data_parts[1]
    sig_id = data_parts[2]

    # Фикс блокировки: выполняем запрос к БД в отдельном потоке
    status_text = await asyncio.to_thread(_route_signal_sync, sig_id, action)

    # Фикс HTML Entities Crash: скрываем клавиатуру без модификации html_text
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.reply(f"✅ Статус заявки: {status_text}", parse_mode="HTML")
    await callback.answer()


def _fetch_ready_signals_sync():
    """Синхронный фетчер заявок из БД"""
    conn = db.get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT id, match_name, market_type, target, line, expected_kf, edge FROM bet_signals WHERE status = 'READY'")
        return c.fetchall()
    except Exception as e:
        logging.error(f"Background scanner db fetch error: {e}")
        return []
    finally:
        conn.close()


def _update_signal_status_sync(sig_id, new_status):
    """Синхронный апдейтер статуса заявки"""
    conn = db.get_connection()
    try:
        c = conn.cursor()
        c.execute("UPDATE bet_signals SET status = ? WHERE id = ?", (new_status, sig_id))
        conn.commit()
    except Exception as e:
        logging.error(f"Background scanner db update error: {e}")
    finally:
        conn.close()


async def background_scanner(bot: Bot):
    while True:
        try:
            # Фикс блокировки: получение настроек асинхронно
            current_mode = await asyncio.to_thread(db.get_system_setting, "betting_mode", "MANUAL")
            if current_mode == "MANUAL":
                # Фикс блокировки: асинхронное чтение БД
                signals = await asyncio.to_thread(_fetch_ready_signals_sync)

                for sig in signals:
                    sig_id, match_name, market_type, target, line, expected_kf, edge = sig

                    text = (
                        f"🚨 <b>НОВЫЙ СИГНАЛ</b>\n"
                        f"Матч: {match_name} | Маркет: {market_type}\n"
                        f"Выбор: {target} | Линия: {line}\n"
                        f"Ожидаемый кэф: {expected_kf} | Edge: {edge}%"
                    )

                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Пробить в Store A", callback_data=f"route_storeA_{sig_id}")],
                        [InlineKeyboardButton(text="✅ Пробить в Store B", callback_data=f"route_storeB_{sig_id}")],
                        [InlineKeyboardButton(text="❌ Пропустить (Skip)", callback_data=f"route_skip_{sig_id}")]
                    ])

                    if Config.ADMIN_ID:
                        try:
                            # Фикс потери сигнала: сначала успешная отправка, потом обновление БД
                            await bot.send_message(Config.ADMIN_ID, text, reply_markup=keyboard, parse_mode="HTML")
                            await asyncio.to_thread(_update_signal_status_sync, sig_id, "PENDING_ADMIN")
                        except Exception as e:
                            logging.error(f"Error sending signal {sig_id} to admin: {e}")
        except Exception as e:
            logging.error(f"Background scanner loop error: {e}")

        await asyncio.sleep(5)


async def main():
    if Config.PROXY_URL:
        session = AiohttpSession(proxy=Config.PROXY_URL)
    else:
        session = AiohttpSession()

    bot = Bot(token=Config.TG_BOT_TOKEN, session=session)

    print("Бот запускается...")
    asyncio.create_task(background_scanner(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())