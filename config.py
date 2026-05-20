"""
config.py
---------
Load environment variables dan provide helper functions.
"""

import os
import hashlib
from dotenv import load_dotenv

# Load .env file kalau ada
load_dotenv()

# Supabase credentials
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Salt buat hashing PII
PII_HASH_SALT = os.getenv("PII_HASH_SALT", "default-change-me")


def hash_pii(value: str) -> str:
    """
    Hash PII pakai SHA-256 + salt.
    
    Kenapa pake salt? Biar kalau ada hash leaked, attacker gak bisa
    rainbow-table attack. Salt harus konsisten kalau lo mau bisa
    detect "customer yang sama" antar order.
    
    Args:
        value: string yang mau di-hash (nama, no HP, dll)
    
    Returns:
        Hex string hash (64 char), atau None kalau input None/empty
    """
    if not value or str(value).strip() == "" or str(value).lower() == "nan":
        return None
    
    # Normalize dulu: lowercase + strip whitespace
    normalized = str(value).lower().strip()
    
    # Hash dengan salt
    salted = (normalized + PII_HASH_SALT).encode("utf-8")
    return hashlib.sha256(salted).hexdigest()


def validate_config() -> tuple[bool, str]:
    """
    Validate config sebelum app jalan.
    
    Returns:
        (is_valid, error_message)
    """
    if not SUPABASE_URL:
        return False, "SUPABASE_URL belum di-set di .env"
    if not SUPABASE_KEY:
        return False, "SUPABASE_KEY belum di-set di .env"
    if PII_HASH_SALT == "default-change-me":
        return False, "PII_HASH_SALT masih default! Ganti di .env"
    
    return True, "OK"
