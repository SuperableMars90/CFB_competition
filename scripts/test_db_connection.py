"""
Quick smoke test for the DB connection.

Run from project root:
    python -m scripts.test_db_connection
"""

import sys

from lib.db import healthcheck


def main() -> int:
    print("Testing Aiven MySQL connection...")
    result = healthcheck()

    if result["ok"]:
        print("  ✓ Connected successfully")
        print(f"  MySQL version : {result['mysql_version']}")
        print(f"  Database      : {result['database']}")
        print(f"  Server time   : {result['server_time']}")
        return 0
    else:
        print("  ✗ Connection failed")
        print(f"  Error type    : {result['error']}")
        print(f"  Message       : {result['message']}")
        return 1


if __name__ == "__main__":
    sys.exit(main())