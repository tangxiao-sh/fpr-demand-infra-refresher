"""Small JSON-backed translation helper for Accessor's user-facing copy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SUPPORTED_LANGUAGES = ("zh", "en")
DEFAULT_LANGUAGE = "zh"
_language = DEFAULT_LANGUAGE
_catalogs: dict[str, dict[str, str]] = {}


def set_language(language: str) -> None:
    """Select the process-wide UI language after CLI arguments are parsed."""
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"unsupported language: {language}")
    global _language
    _language = language


def t(key: str, **values: Any) -> str:
    """Return a translated template and interpolate named values safely."""
    catalog = _catalogs.get(_language)
    if catalog is None:
        catalog = _load_catalog(_language)
        _catalogs[_language] = catalog
    try:
        template = catalog[key]
    except KeyError as error:
        raise KeyError(f"missing {_language} translation: {key}") from error
    return template.format(**values)


def _load_catalog(language: str) -> dict[str, str]:
    path = Path(__file__).with_name("locales") / f"{language}.json"
    with path.open(encoding="utf-8") as source:
        catalog = json.load(source)
    if not isinstance(catalog, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in catalog.items()
    ):
        raise ValueError(f"invalid translation catalog: {path}")
    return catalog
