"""
scripts/cfbd_client.py
----------------------
Thin wrapper around the College Football Data API.
Handles auth, rate limiting, and year parameterization.

All public methods return parsed JSON (list or dict).
Raises CFBDError on non-200 responses.

Usage:
    from scripts.cfbd_client import CFBDClient
    client = CFBDClient()
    teams = client.get_teams(year=2025)
"""

import os
import sys
import time
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config.loader import get_config

BASE_URL = 'https://api.collegefootballdata.com'

# Pause between requests to respect rate limits.
# At 1 second we are well within any tier's limits for a once-per-season init.
REQUEST_DELAY = 1.0


class CFBDError(Exception):
    pass


class CFBDClient:
    def __init__(self):
        self._api_key = self._load_key()
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {self._api_key}',
            'Accept': 'application/json',
        })

    def _load_key(self):
        key_file = os.path.join(
            os.path.dirname(__file__), '..', 'secrets', 'cfbd_api_key'
        )
        if os.path.exists(key_file):
            return open(key_file).read().strip()
        # Fall back to config.json for backwards compatibility
        key = get_config().get('cfbd', {}).get('api_key', '').strip()
        if not key:
            raise ValueError(
                "CFBD API key not found. Add it to secrets/cfbd_api_key "
                "or set cfbd.api_key in config/config.json."
            )
        return key

    def _get(self, endpoint, params=None):
        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        time.sleep(REQUEST_DELAY)
        response = self._session.get(url, params=params or {})
        if response.status_code != 200:
            raise CFBDError(
                f"CFBD API error {response.status_code} on {url}: {response.text[:200]}"
            )
        return response.json()

    # ------------------------------------------------------------------
    # Season initializer endpoints
    # ------------------------------------------------------------------

    def get_conferences(self):
        """
        All conferences (not year-scoped — CFBD conferences list is static).
        Returns list of dicts with keys: id, name, short_name, abbreviation, classification.
        """
        return self._get('/conferences')

    def get_teams(self, year=None, classification=None):
        """
        Teams filtered by year and/or classification.
        classification: 'fbs' | 'fcs' (omit for all teams)
        Returns list of dicts with keys: id, school, abbreviation, conference, classification, etc.
        """
        params = {}
        if year:
            params['year'] = year
        if classification:
            params['classification'] = classification
        return self._get('/teams', params=params)

    def get_games(self, year, season_type='regular'):
        """
        Full game schedule for a given year and season type.
        season_type: 'regular' | 'postseason' | 'both'
        Returns list of game dicts.
        """
        return self._get('/games', params={
            'year': year,
            'seasonType': season_type,
        })

    # ------------------------------------------------------------------
    # Scoring engine endpoints
    # ------------------------------------------------------------------

    def get_games_for_week(self, year, week, season_type='regular'):
        """Games for a specific week — used by the scoring engine."""
        return self._get('/games', params={
            'year': year,
            'week': week,
            'seasonType': season_type,
        })

    def get_scoreboard(self):
        """
        Live in-progress scores for the current week.
        No year parameter — always returns current live data.
        """
        return self._get('/scoreboard')
