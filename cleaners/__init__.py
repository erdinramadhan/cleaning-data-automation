"""
cleaners/__init__.py
--------------------
Auto-detect marketplace dari uploaded file dan return cleaner yang sesuai.
"""

from .base import BaseCleaner
from .tokopedia import TokopediaCleaner
from .iseller import IsellerCleaner
from .desty import DestyCleaner

# Registry semua cleaner yang available
ALL_CLEANERS: list[BaseCleaner] = [
    TokopediaCleaner(),
    IsellerCleaner(),
    DestyCleaner(),
]


def detect_marketplace(columns: list[str]) -> BaseCleaner | None:
    """
    Auto-detect marketplace berdasarkan column header file.
    
    Args:
        columns: list of column names dari uploaded file
    
    Returns:
        Cleaner instance kalau cocok, None kalau gak ada yang match
    """
    for cleaner in ALL_CLEANERS:
        if cleaner.matches(columns):
            return cleaner
    return None


__all__ = [
    "BaseCleaner",
    "TokopediaCleaner",
    "IsellerCleaner",
    "DestyCleaner",
    "ALL_CLEANERS",
    "detect_marketplace",
]
