# Version: 6.1
def calculate_kelly_bet(win_probability: float, odds: float, bankroll: float = 10000.0) -> float:
    """
    Рассчитывает рекомендуемую сумму ставки по критерию Келли.

    :param win_probability: Наша оценка вероятности прохода события (от 0.0 до 1.0)
    :param odds: Коэффициент букмекера (десятичный, например 1.95)
    :param bankroll: Текущий размер банка (по умолчанию 10000 RUB)
    :return: Сумма ставки в валюте банка (округленная до 2 знаков)
    """
    # Если вероятность меньше или равна 0, либо коэффициент меньше или равен 1, ставка 0
    if win_probability <= 0.0 or odds <= 1.0:
        return 0.0

    # b - это чистый выигрыш с 1 единицы ставки (odds - 1)
    b = odds - 1.0
    q = 1.0 - win_probability

    # Формула Келли: f* = (bp - q) / b
    kelly_fraction = (b * win_probability - q) / b

    # Если перевеса нет (фракция < 0), мы не ставим
    if kelly_fraction <= 0.0:
        return 0.0

    # Дробный Келли (Fractional Kelly) для снижения дисперсии.
    # Обычно ставят 25% или 50% от расчетного значения по Келли. Используем 25% (0.25).
    fractional_multiplier = 0.25
    safe_fraction = kelly_fraction * fractional_multiplier

    # Рассчитываем итоговую сумму
    bet_amount = bankroll * safe_fraction

    return round(bet_amount, 2)
