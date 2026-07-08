"""
db/connection.py
----------------
Shared database connection helper for CLI scripts (init_season, scoring_engine, etc.).
Reads credentials from secrets/aiven.toml so scripts can run outside Streamlit context.

Usage:
    from db.connection import get_connection

    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM seasons")
    rows = cursor.fetchall()
    conn.close()
"""

import os
import sys
import tomllib
import mysql.connector

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_SECRETS_PATH = os.path.join(os.path.dirname(__file__), '..', 'secrets', 'aiven.toml')


def get_connection():
    """Return a live mysql.connector connection using secrets/aiven.toml."""
    with open(_SECRETS_PATH, 'rb') as f:
        cfg = tomllib.load(f)['aiven_mysql']

    return mysql.connector.connect(
        host=cfg['host'],
        port=cfg['port'],
        user=cfg['user'],
        password=cfg['password'],
        database=cfg['database'],
        ssl_ca=cfg['ssl_ca_path'],
        ssl_verify_cert=True,
        ssl_verify_identity=True,
        autocommit=False,
    )
