"""The packaged model catalog (`career_copilot/model_catalog.yaml`), read once."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources

import yaml

from career_copilot.domain.model_settings import Catalog, parse_catalog

CATALOG_FILE = "model_catalog.yaml"


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    text = resources.files("career_copilot").joinpath(CATALOG_FILE).read_text()
    return parse_catalog(yaml.safe_load(text))
