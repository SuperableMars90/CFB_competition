"""
config/loader.py
----------------
Single config loader for the entire project.
All modules import get_config() from here.

Usage:
    from config.loader import get_config

    config = get_config()
    api_key = config['cfbd']['api_key']
    db = config['database']
"""

import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
_config_cache = None


def get_config():
    """Load and cache config.json. Raises FileNotFoundError if missing."""
    global _config_cache
    if _config_cache is None:
        if not os.path.exists(_CONFIG_PATH):
            raise FileNotFoundError(
                f"Config file not found at {_CONFIG_PATH}. "
                "Copy config/config.example.json to config/config.json "
                "and fill in your credentials."
            )
        with open(_CONFIG_PATH) as f:
            _config_cache = json.load(f)
    return _config_cache
