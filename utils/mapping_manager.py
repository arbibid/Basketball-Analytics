import os
import json
import logging

logger = logging.getLogger("MappingManager")

class MappingManager:
    def __init__(self, config_dir='config'):
        self.mappings_file = os.path.join(config_dir, 'mappings.json')
        self.official_players_file = os.path.join(config_dir, 'wnba_official_players.json')

        self.player_map = {}
        self.official_players = {}
        self.english_to_wnba_id = {}

        self._load_configs()

    def _load_configs(self):
        try:
            with open(self.mappings_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.player_map = data.get('PLAYER_MAP', {})
        except Exception as e:
            logger.error(f"Failed to load {self.mappings_file}: {e}")

        try:
            with open(self.official_players_file, 'r', encoding='utf-8') as f:
                self.official_players = json.load(f)
                for wnba_id_str, info in self.official_players.items():
                    try:
                        wnba_id = int(wnba_id_str)
                        name_en = info.get('name_en', '').strip()
                        if name_en:
                            self.english_to_wnba_id[name_en] = wnba_id

                            # Also map variants like "First Initial. Lastname"
                            parts = name_en.split(' ', 1)
                            if len(parts) == 2:
                                first, last = parts
                                short_name = f"{first[0]}. {last}"
                                self.english_to_wnba_id[short_name] = wnba_id
                    except ValueError:
                        continue
        except Exception as e:
            logger.error(f"Failed to load {self.official_players_file}: {e}")

    def get_wnba_id(self, player_name: str, bookmaker: str) -> int | None:
        """
        Translates a raw player name (from bookmaker) to an official WNBA ID.
        """
        if not player_name:
            return None

        cleaned_name = player_name.strip()

        # 1. First, map Russian alias to English name
        english_name = self.player_map.get(cleaned_name, cleaned_name)

        # 2. Map English name to wnba_id
        wnba_id = self.english_to_wnba_id.get(english_name)

        # 3. Fallback: fuzzy matching based on the last name or initials if exact match fails
        if not wnba_id:
            # Try to match the provided english_name with the dict
            for mapped_en_name, p_id in self.english_to_wnba_id.items():
                if english_name.lower() == mapped_en_name.lower():
                    return p_id

            # Try matching by last name for Russian aliases that might have flipped parts
            if " " in cleaned_name:
                parts = cleaned_name.split()
                if len(parts) == 2:
                    # Attempt mapping reversed
                    rev_name = f"{parts[1]} {parts[0]}"
                    if rev_name in self.player_map:
                        en_name = self.player_map[rev_name]
                        if en_name in self.english_to_wnba_id:
                            return self.english_to_wnba_id[en_name]

        return wnba_id
