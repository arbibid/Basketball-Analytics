# Version: 1.0
import os
import json
import re

class MarketMapper:
    """
    MarketMapper handles translation between internal canonical market keys 
    (e.g., GAME_TOTAL_OVER, PLAYER_PTS_OVER) and bookmaker-specific identifiers.
    """
    
    _instance = None
    _mapping_cache = None

    def __new__(cls, config_path=None):
        if cls._instance is None:
            cls._instance = super(MarketMapper, cls).__new__(cls)
            if config_path is None:
                config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'canonical_markets.json')
            cls._instance.config_path = os.path.abspath(config_path)
            cls._instance._load_mapping()
        return cls._instance

    def _load_mapping(self):
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._mapping_cache = json.load(f)
        except Exception as e:
            print(f"Error loading canonical markets mapping from {self.config_path}: {e}")
            self._mapping_cache = {}
            
    def reload(self):
        self._load_mapping()

    def get_mapping(self):
        return self._mapping_cache

    def to_canonical(self, bookmaker, **kwargs):
        """
        Translates a bookmaker's raw market ID/parameters to an internal canonical key.
        
        Usage example (Fonbet):
            to_canonical('FONBET', factor_id=930) -> 'GAME_TOTAL_OVER'
            
        Usage example (Betcity):
            to_canonical('BETCITY', market_id=72, param='Tb') -> 'GAME_TOTAL_OVER'
        """
        bookmaker = bookmaker.upper()
        
        for canonical_key, bmk_config in self._mapping_cache.items():
            if bookmaker not in bmk_config:
                continue
                
            bmk_params = bmk_config[bookmaker]
            
            if bookmaker == 'FONBET':
                factor_id = kwargs.get('factor_id')
                if factor_id and factor_id in bmk_params.get('factor_id', []):
                    return canonical_key
                    
            elif bookmaker == 'BETCITY':
                market_id = kwargs.get('market_id')
                param = kwargs.get('param')
                team = kwargs.get('team')
                is_h2h = kwargs.get('is_h2h', False)
                
                # Betcity matches specific market_id and param
                if market_id and market_id in bmk_params.get('market_id', []):
                    if param and param == bmk_params.get('param'):
                        # Check additional qualifiers if specified in config
                        config_team = bmk_params.get('team')
                        if config_team and config_team != team:
                            continue
                            
                        config_h2h = bmk_params.get('is_h2h', False)
                        if config_h2h != is_h2h:
                            continue
                            
                        return canonical_key
                        
        return None

    def from_canonical(self, bookmaker, canonical_key):
        """
        Translates an internal canonical key back to bookmaker-specific identifiers for placing bets.
        
        Usage example:
            from_canonical('FONBET', 'GAME_TOTAL_OVER') -> {'factor_id': [930, 1696, ...]}
            from_canonical('BETCITY', 'GAME_TOTAL_OVER') -> {'market_id': [72, 112], 'param': 'Tb'}
        """
        bookmaker = bookmaker.upper()
        if canonical_key in self._mapping_cache:
            return self._mapping_cache[canonical_key].get(bookmaker)
        return None

    @staticmethod
    def generate_canonical_match_id(date_str, away_team, home_team):
        """
        Generates a standardized match ID independent of bookmaker IDs.
        e.g. '2023-08-15', 'Liberty', 'Aces' -> '20230815_LIBERTY_ACES'
        """
        # Strip non-alphanumeric chars from date (e.g. 2023-08-15 -> 20230815)
        clean_date = re.sub(r'\D', '', str(date_str))
        
        # Upper and strip spaces from teams
        clean_away = re.sub(r'\s+', '', str(away_team)).upper()
        clean_home = re.sub(r'\s+', '', str(home_team)).upper()
        
        return f"{clean_date}_{clean_away}_{clean_home}"

