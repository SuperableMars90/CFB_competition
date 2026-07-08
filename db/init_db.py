"""
init_db.py
----------
Run once to create the cfb_game database and all tables.
Safe to re-run -- uses IF NOT EXISTS throughout.

Usage:
    python db/init_db.py

Reads connection config from config/config.json.
Copy config/config.example.json to config/config.json and fill in your credentials.
"""

import os
import sys
import mysql.connector
from mysql.connector import errorcode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from config.loader import get_config

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'schema.sql')


def get_connection(use_db=True):
    cfg = get_config()['database']
    params = {
        'host':     cfg['host'],
        'user':     cfg['user'],
        'password': cfg['password'],
        'port':     cfg.get('port', 3306),
    }
    if use_db:
        params['database'] = cfg['database']
    return mysql.connector.connect(**params)


def run_schema():
    with open(SCHEMA_PATH, 'r') as f:
        raw = f.read()

    statements = [s.strip() for s in raw.split(';') if s.strip() and not s.strip().startswith('--')]

    try:
        conn = get_connection(use_db=False)
        cursor = conn.cursor()
        db_name = get_config()['database']['database']
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cursor.execute(f"USE `{db_name}`")
        print(f"Database `{db_name}` ready.")

        created = 0
        skipped = 0
        for stmt in statements:
            if not stmt or stmt.upper().startswith('CREATE DATABASE') or stmt.upper().startswith('USE '):
                continue
            try:
                cursor.execute(stmt)
                if stmt.upper().startswith('CREATE TABLE'):
                    name = stmt.split('EXISTS')[1].strip().split()[0].strip('(` ')
                    print(f"  + Table: {name}")
                    created += 1
            except mysql.connector.Error as e:
                if e.errno == errorcode.ER_TABLE_EXISTS_ERROR:
                    skipped += 1
                else:
                    print(f"\nERROR executing statement:\n{stmt[:120]}\n{e}")
                    conn.rollback()
                    sys.exit(1)

        conn.commit()
        print(f"\nDone. {created} table(s) created, {skipped} already existed.")
        cursor.close()
        conn.close()

    except mysql.connector.Error as e:
        print(f"Connection error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    cfg = get_config()['database']
    print(f"Connecting to {cfg['host']} as {cfg['user']}...")
    run_schema()
