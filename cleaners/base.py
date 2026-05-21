"""
cleaners/base.py
----------------
Base class & shared utilities buat semua marketplace cleaner.

KEY CHANGE dari v1: cleaner return list[OrderBundle], bukan list[dict].
OrderBundle = OrderHeader + list[OrderItem].
"""

from abc import ABC, abstractmethod
from typing import Optional
import pandas as pd
import re
from datetime import datetime


class BaseCleaner(ABC):
    """Abstract base class untuk semua marketplace cleaner."""
    
    marketplace: str = ""
    expected_columns: set[str] = set()
    
    @abstractmethod
    def load(self, file_obj) -> pd.DataFrame:
        """Read file (CSV/XLSX) jadi DataFrame."""
        pass
    
    @abstractmethod
    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        """Marketplace-specific cleaning."""
        pass
    
    @abstractmethod
    def to_order_bundles(self, df: pd.DataFrame, source_file: str) -> list:
        """
        Convert cleaned DataFrame ke list[OrderBundle].
        
        KEY LOGIC: group by order_id, lalu:
          - first row → ambil order-level fields buat header
          - all rows → jadi line items
        
        Return: list of OrderBundle (uvalidated raw, validation di loader)
        """
        pass
    
    def matches(self, columns: list[str]) -> bool:
        """Auto-detect: file ini match marketplace ini?"""
        col_set = set(columns)
        if not self.expected_columns:
            return False
        overlap = col_set & self.expected_columns
        return len(overlap) / len(self.expected_columns) >= 0.8


# ============================================================
# Shared utilities
# ============================================================

def parse_price_string(value) -> Optional[int]:
    """
    Parse harga dari berbagai format:
    - "120,000"      -> 120000
    - "-20,000"      -> -20000
    - "Rp 150.000"   -> 150000
    - 155000         -> 155000
    """
    if value is None or pd.isna(value):
        return None
    
    if isinstance(value, (int, float)):
        return int(value)
    
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    
    cleaned = re.sub(r"[Rp\s]", "", s, flags=re.IGNORECASE)
    cleaned = cleaned.replace(",", "").replace(".", "")
    
    try:
        return int(cleaned)
    except ValueError:
        return None


def parse_date_flexible(value, formats: list[str] = None) -> Optional[datetime]:
    """Parse date dengan multiple format fallback."""
    if value is None or pd.isna(value):
        return None
    
    s = str(value).strip().rstrip("\t")
    if not s or s.lower() == "nan":
        return None
    
    if formats is None:
        formats = [
            "%Y-%m-%d %H:%M:%S %z",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y",
        ]
    
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    
    try:
        result = pd.to_datetime(s)
        return result.to_pydatetime() if not pd.isna(result) else None
    except (ValueError, TypeError):
        return None


def safe_str(value) -> Optional[str]:
    """Convert ke string, None kalau NaN/empty."""
    if value is None or pd.isna(value):
        return None
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def safe_int(value, default: int = 0) -> int:
    """Convert ke int, default kalau NaN/error."""
    if value is None or pd.isna(value):
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def safe_int_abs(value, default: int = 0) -> int:
    """Same as safe_int tapi return absolute value (untuk diskon yang negatif)."""
    return abs(safe_int(value, default))


def normalize_phone_id(value) -> Optional[str]:
    """
    Normalize nomor telepon Indonesia ke format '62xxxxxxxxxx'.
    
    Handle berbagai format input:
    - 6281234567890       -> 6281234567890   (already correct)
    - +6281234567890      -> 6281234567890   (strip "+")
    - 081234567890        -> 6281234567890   (0xx -> 62xx)
    - 81234567890         -> 6281234567890   (prepend 62)
    - 6281234567890.0     -> 6281234567890   (strip float decimal suffix)
    - "(+62)812***890"    -> None            (masked, gak bisa dipakai)
    - ""  / None / NaN    -> None
    - format invalid      -> None
    
    Length validation (Indonesia phone setelah normalize ke 62xxx):
    - 11 digit (62811xxxxxx)    -> valid (Telkomsel/Indosat lama)
    - 12-14 digit               -> valid
    - lainnya                   -> None (corrupt)
    
    Returns: string format '62xxxxxxxxxx' atau None.
    """
    if value is None or pd.isna(value):
        return None
    
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    
    # Strip float decimal suffix (".0", ".00", dll) yang muncul karena
    # pandas baca number sebagai float
    s = s.split(".")[0]
    
    # Strip semua karakter non-digit (kurung, plus, spasi, dash, dll)
    digits = re.sub(r"[^0-9]", "", s)
    
    if not digits:
        return None
    
    # Normalize ke format 62xxx
    if digits.startswith("62") and 11 <= len(digits) <= 15:
        return digits
    elif digits.startswith("08") and 10 <= len(digits) <= 13:
        return "62" + digits[1:]
    elif digits.startswith("8") and 9 <= len(digits) <= 12:
        return "62" + digits
    else:
        return None
