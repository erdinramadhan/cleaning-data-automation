"""
config.py
---------
Load environment variables dan provide helper functions.

Support 2 environment:
1. Local dev (Mac/laptop)        → baca dari .env file via python-dotenv
2. Streamlit Community Cloud     → baca dari st.secrets (set via UI Settings)
"""

import os
import hashlib
from dotenv import load_dotenv

# Load .env file kalau ada (buat dev lokal)
load_dotenv()


def _get_secret(key: str, default=None):
    """
    Get credential dengan fallback chain:
    1. Env var (set via .env atau export di terminal)
    2. Streamlit Cloud secrets (st.secrets)
    3. Default value

    Streamlit Cloud expose secret via st.secrets, bukan env var.
    Jadi kalau os.getenv() return None, coba st.secrets sebagai fallback.
    """
    # Priority 1: env var (dev lokal + .env)
    value = os.getenv(key)
    if value:
        return value

    # Priority 2: Streamlit Cloud secrets
    try:
        import streamlit as st
        # st.secrets bisa di-akses kayak dict
        if key in st.secrets:
            return st.secrets[key]
    except (ImportError, FileNotFoundError, Exception):
        # ImportError      → streamlit gak installed (rare, krn req.txt)
        # FileNotFoundError → run lokal tanpa secrets.toml (normal)
        # Exception lain   → suppress, fallback ke default
        pass

    return default


# Supabase credentials
SUPABASE_URL = _get_secret("SUPABASE_URL")
SUPABASE_KEY = _get_secret("SUPABASE_KEY")

# Salt buat hashing PII
PII_HASH_SALT = _get_secret("PII_HASH_SALT", "default-change-me")


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
        return False, "SUPABASE_URL belum di-set (cek .env lokal atau Streamlit Secrets)"
    if not SUPABASE_KEY:
        return False, "SUPABASE_KEY belum di-set (cek .env lokal atau Streamlit Secrets)"
    if PII_HASH_SALT == "default-change-me":
        return False, "PII_HASH_SALT masih default! Ganti di .env atau Streamlit Secrets"
    
    return True, "OK"
