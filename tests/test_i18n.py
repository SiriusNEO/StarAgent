from __future__ import annotations

import re
from pathlib import Path

from staragent.dashboard.i18n import MESSAGES

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRANSLATION_CALL_PATTERN = re.compile(r"""(?<![\w$])(?:t|translate)\(\s*["']([^"']+)["']""")


def test_english_and_chinese_translation_catalogs_have_the_same_keys() -> None:
    assert set(MESSAGES["en"]) == set(MESSAGES["zh-CN"])


def test_literal_frontend_translation_keys_exist_in_both_catalogs() -> None:
    keys: set[str] = set()
    paths = (
        *(PROJECT_ROOT / "staragent" / "dashboard" / "static").glob("*.js"),
        *(PROJECT_ROOT / "staragent" / "dashboard" / "templates").rglob("*.html"),
    )
    for path in paths:
        keys.update(TRANSLATION_CALL_PATTERN.findall(path.read_text(encoding="utf-8")))

    # A trailing dot is a deliberate prefix completed dynamically, for example
    # t("agents.description." + agentName).
    literal_keys = {key for key in keys if not key.endswith(".")}
    assert literal_keys <= set(MESSAGES["en"])
    assert literal_keys <= set(MESSAGES["zh-CN"])
