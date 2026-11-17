"""Offline validation: the index mappings."""

import copy

from main.config import Settings
from main.mappings import index_body, validate_index


def test_mappings_are_valid_and_carry_the_version():
    s = Settings(index_version=3)
    body = index_body(s)
    assert s.index == "items-v3" and validate_index(s.index, body) == []
    assert body["mappings"]["_meta"]["index_version"] == 3 and body["mappings"]["dynamic"] is False
    assert body["mappings"]["_meta"]["writer"] == s.function


def test_mapping_mistakes_are_caught():
    s = Settings()
    body = index_body(s)
    assert validate_index("items", body)  # name without the version
    bad = copy.deepcopy(body)
    bad["mappings"]["properties"]["price_cad"]["type"] = "money"
    assert any("unknown type" in e for e in validate_index(s.index, bad))
    bad = copy.deepcopy(body)
    bad["mappings"]["dynamic"] = True
    assert any("dynamic" in e for e in validate_index(s.index, bad))
