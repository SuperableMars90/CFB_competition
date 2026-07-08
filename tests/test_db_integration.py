"""
Integration test hitting the live Aiven database.

Kept separate from the unit tests so the whole suite doesn't require network
access / valid secrets to run — see pytest.ini's `integration` marker.
"""

import pytest

from lib.db import healthcheck


@pytest.mark.integration
def test_healthcheck_connects_to_aiven():
    result = healthcheck()
    assert result["ok"], result.get("message")
    assert result["database"] == "defaultdb"
