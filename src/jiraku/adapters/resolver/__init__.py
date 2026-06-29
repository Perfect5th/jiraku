"""Repo-resolution adapters."""

from __future__ import annotations

from .catalog import RepoCatalogEntry, default_catalog, load_catalog
from .composite import CompositeRepoResolver
from .keyword_resolver import KeywordRepoResolver
from .learned_resolver import (
    FileLearnedRulesStore,
    InMemoryLearnedRulesStore,
    LearnedRulesRepoResolver,
)
from .registry_resolver import RegistryRepoResolver

__all__ = [
    "RepoCatalogEntry",
    "default_catalog",
    "load_catalog",
    "CompositeRepoResolver",
    "KeywordRepoResolver",
    "RegistryRepoResolver",
    "LearnedRulesRepoResolver",
    "InMemoryLearnedRulesStore",
    "FileLearnedRulesStore",
]
