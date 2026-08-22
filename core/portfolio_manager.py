# Version: 1.0
class PortfolioManager:
    """
    Управляет корзиной ставок H2H, используя матрицу рисков (Smart Portfolio H2H).
    Защищает банк от травм и зависимости от одного игрока.
    """

    def __init__(self, allocated_bankroll: float, max_exposure_limit: float = 0.35):
        """
        :param allocated_bankroll: Размер выделенного банка на матч
        :param max_exposure_limit: Максимальная доля пула (35-40%), зависящая от одного игрока
        """
        self.allocated_bankroll = allocated_bankroll
        self.max_exposure_limit = max_exposure_limit

    def optimize_portfolio(self, h2h_basket: list) -> list:
        """
        Оптимизирует ставки, срезая веса там, где превышен лимит зависимости от одного игрока.
        h2h_basket: list of dicts. Каждая ставка - это словарь с ключами:
            - date, match_name, market, category, player_name, line, prediction, selection
            - kf, is_preliminary, coupon_id, wnba_id, edge, implied_prob, win_prob
        """
        if not h2h_basket:
            return []

        # 1. Считаем сырые веса по Келли (или просто берем edge как базу, если Келли вернет много)
        # Мы будем использовать нормированный edge/Келли
        total_raw_fraction = 0.0
        for bet in h2h_basket:
            # Формула Келли: f* = (bp - q) / b
            b = bet['kf'] - 1.0
            q = 1.0 - bet['win_prob']
            kelly_fraction = (b * bet['win_prob'] - q) / b

            # Если Келли отрицательный, значит edge посчитан с ошибкой, но мы и так фильтруем по edge
            safe_fraction = max(0.001, kelly_fraction * 0.25) # Fractional Kelly 25%
            bet['raw_weight'] = safe_fraction
            total_raw_fraction += safe_fraction

        # Нормализуем веса так, чтобы их сумма равнялась 1.0 (или меньше, если мы не хотим тратить весь банк,
        # но в контексте пула мы распределяем его)
        if total_raw_fraction > 0:
            for bet in h2h_basket:
                bet['weight'] = bet['raw_weight'] / total_raw_fraction
        else:
            return []

        # 2. Матрица рисков: считаем вовлеченность (Exposure) каждого игрока
        # wnba_id в дуэлях хранится как "ID1_ID2" (или None, если не найдено)
        player_exposure = {}
        for bet in h2h_basket:
            weight = bet['weight']
            wnba_ids = bet.get('wnba_id')
            if wnba_ids and '_' in str(wnba_ids):
                id1, id2 = str(wnba_ids).split('_', 1)
                player_exposure[id1] = player_exposure.get(id1, 0.0) + weight
                player_exposure[id2] = player_exposure.get(id2, 0.0) + weight

        # 3. Применяем Exposure Limit
        # Если exposure игрока > limit, находим коэффициент урезания
        exposure_cuts = {}
        for p_id, exposure in player_exposure.items():
            if exposure > self.max_exposure_limit:
                # Коэффициент, на который надо умножить веса ставок с этим игроком
                cut_factor = self.max_exposure_limit / exposure
                exposure_cuts[p_id] = cut_factor

        # 4. Пересчитываем веса с учетом срезов
        for bet in h2h_basket:
            wnba_ids = bet.get('wnba_id')
            min_cut_factor = 1.0
            if wnba_ids and '_' in str(wnba_ids):
                id1, id2 = str(wnba_ids).split('_', 1)
                cut1 = exposure_cuts.get(id1, 1.0)
                cut2 = exposure_cuts.get(id2, 1.0)
                # Берем самый строгий срез
                min_cut_factor = min(cut1, cut2)

            bet['final_weight'] = bet['weight'] * min_cut_factor

        # 5. Переводим финальные веса в RUB и округляем
        for bet in h2h_basket:
            bet['amount'] = round(self.allocated_bankroll * bet['final_weight'], 2)

        return h2h_basket
