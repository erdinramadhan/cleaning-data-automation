"""
fulfillment.py
--------------
Import tagihan vendor fulfillment center → tabel fulfillment_costs.

Alur:
  1. load_file()            baca file vendor (xlsx, sering dinamai .xls)
  2. parse()                gabung baris lanjutan per transaksi, bersihin order_id,
                            normalisasi gudang, ambil komponen biaya
  3. push_fulfillment()     upsert by no_transaksi_raw, lalu sambungin order_pk

Aturan upsert:
  - Transaksi BARU      → insert penuh.
  - Transaksi SUDAH ADA → angka biaya + status diperbarui (tagihan PENDING bisa berubah
                          sampai DONE), tapi order_id & order_pk yang ada di DB TIDAK ditimpa
                          (biar perbaikan manual tetap aman).
"""

import json
from datetime import datetime, timezone
import math
import re
from typing import Optional

import pandas as pd

CHUNK_SIZE = 200
TABLE = "fulfillment_costs"

# Kolom penanda file vendor (dipakai app.py buat deteksi)
SIGNATURE_COLUMNS = {"No. Transaksi", "Total Biaya Transaksi", "Biaya Transaksi", "Gudang"}

# Gudang vendor → nama kanonik. Main Warehouse = gudang internal, gak boleh ada di sini.
GUDANG_MAP = [
    ("depo kuta", "Depo Kuta Bali"),
    ("gudang andalan", "Gudang Andalan"),
    ("hea", "HEA Fulfillment"),
]

# Kolom biaya di file → kolom di DB
COST_COLUMNS = {
    "Total Nilai Transaksi": "nilai_transaksi",
    "Biaya Transaksi": "biaya_transaksi",
    "PPN Biaya Transaksi": "ppn_biaya_transaksi",
    "Total Biaya Packaging": "biaya_packaging",
    "PPN Total Biaya Packaging": "ppn_biaya_packaging",
    "Total Biaya QC": "biaya_qc",
    "PPN Total Biaya QC": "ppn_biaya_qc",
    "Total Biaya Shipping Label": "biaya_shipping_label",
    "PPN Total Biaya Shipping Label": "ppn_biaya_shipping_label",
    "Biaya Logistik": "biaya_logistik",
    "Total Biaya Transaksi": "total_biaya",
}

# Kolom yang ikut diperbarui kalau transaksi sudah ada
UPDATABLE_FIELDS = ["gudang", "tanggal_transaksi", "status", "logistik", "no_awb",
                    "raw_data", "source_file"] + list(COST_COLUMNS.values())


# ============================================================
# Helpers
# ============================================================

def _is_blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip() == ""


def _str(v) -> Optional[str]:
    return None if _is_blank(v) else str(v).strip()


def _int(v) -> int:
    if _is_blank(v):
        return 0
    try:
        return int(round(float(str(v).replace(",", ""))))
    except (ValueError, TypeError):
        return 0


def normalize_gudang(raw: Optional[str]) -> Optional[str]:
    g = (raw or "").strip().lower()
    for prefix, canonical in GUDANG_MAP:
        if g.startswith(prefix):
            return canonical
    return None


def clean_order_id(raw: str) -> str:
    """'447770240289099777#9335f156' → '447770240289099777'
       '586333025517077750(-1)'      → '586333025517077750'"""
    x = raw.split("#")[0].strip()
    return re.sub(r"\(-?\d+\)$", "", x).strip()


def guess_marketplace(no_transaksi_raw: str) -> str:
    """Desty pakai format 'orderid#kodegudang', Tokopedia angka polos."""
    return "desty" if "#" in no_transaksi_raw else "tokopedia"


def is_fulfillment_file(columns) -> bool:
    cols = {str(c).strip() for c in columns}
    return SIGNATURE_COLUMNS.issubset(cols)


# ============================================================
# Load + parse
# ============================================================

def load_file(file_obj) -> pd.DataFrame:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    df = pd.read_excel(file_obj)  # pandas deteksi xlsx dari isinya walau namanya .xls
    df.columns = [str(c).strip() for c in df.columns]
    return df


def parse(df: pd.DataFrame, source_file: str) -> tuple[list[dict], list[str]]:
    """
    Return (records, warnings).
    1 transaksi = 1 baris utama (kolom 'No' terisi) + baris lanjutan (item/material tambahan).
    """
    records, warnings = [], []
    current = None
    groups = []

    for _, row in df.iterrows():
        if not _is_blank(row.get("No")):
            current = {"row": row, "materials": []}
            groups.append(current)
        if current is not None and _str(row.get("Material Packaging")):
            current["materials"].append({
                "material": _str(row.get("Material Packaging")),
                "harga": _int(row.get("Harga Material Packaging")),
                "qty": _int(row.get("Qty Material Packaging")),
                "total": _int(row.get("Total Harga Material Packaging")),
            })

    for g in groups:
        row = g["row"]
        raw_no = _str(row.get("No. Transaksi"))
        if not raw_no:
            warnings.append(f"Baris No {row.get('No')}: No. Transaksi kosong, dilewati")
            continue

        gudang_raw = _str(row.get("Gudang"))
        gudang = normalize_gudang(gudang_raw)
        if gudang is None:
            warnings.append(f"{raw_no}: gudang '{gudang_raw}' tidak dikenal (disimpan NULL)")

        tgl = pd.to_datetime(row.get("Tanggal Transaksi"), errors="coerce")
        tanggal = None if pd.isna(tgl) else tgl.strftime("%Y-%m-%dT%H:%M:%S") + "+07:00"  # asumsi WIB

        rec = {
            "no_transaksi_raw": raw_no,
            "order_id": clean_order_id(raw_no),
            "gudang": gudang,
            "tanggal_transaksi": tanggal,
            "status": _str(row.get("Status")),
            "logistik": _str(row.get("Logistik")),
            "no_awb": _str(row.get("No. AWB")),
            "raw_data": {
                "kode_booking": _str(row.get("Kode Booking")),
                "gudang_raw": gudang_raw,
                "materials": g["materials"],
            },
            "source_file": source_file,
        }
        for src, dst in COST_COLUMNS.items():
            rec[dst] = _int(row.get(src))

        # Sanity: total harus = jumlah komponen
        komponen = sum(rec[c] for c in COST_COLUMNS.values() if c not in ("nilai_transaksi", "total_biaya"))
        if komponen != rec["total_biaya"]:
            warnings.append(f"{raw_no}: total_biaya {rec['total_biaya']:,} ≠ jumlah komponen {komponen:,}")

        records.append(rec)

    # Duplikat No. Transaksi di file yang sama
    seen = set()
    for r in records:
        if r["no_transaksi_raw"] in seen:
            warnings.append(f"{r['no_transaksi_raw']}: duplikat di file, baris terakhir yang dipakai")
        seen.add(r["no_transaksi_raw"])
    dedup = {r["no_transaksi_raw"]: r for r in records}
    return list(dedup.values()), warnings


# ============================================================
# Push
# ============================================================

def _chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def push_fulfillment(records: list[dict]) -> dict:
    from loader import get_supabase  # import di sini biar parse() bisa dites tanpa DB

    client = get_supabase()
    result = {"new": 0, "updated": 0, "linked": 0, "still_unlinked": 0, "errors": []}
    if not records:
        return result

    # 1) Cek mana yang sudah ada → pakai order_id versi DB (jangan timpa perbaikan manual)
    existing = {}
    keys = [r["no_transaksi_raw"] for r in records]
    for chunk in _chunked(keys, CHUNK_SIZE):
        resp = client.table(TABLE).select("no_transaksi_raw, order_id").in_("no_transaksi_raw", chunk).execute()
        for row in resp.data or []:
            existing[row["no_transaksi_raw"]] = row["order_id"]

    now_iso = datetime.now(timezone.utc).isoformat()
    payload = []
    for r in records:
        rec = dict(r)
        if rec["no_transaksi_raw"] in existing:
            rec["order_id"] = existing[rec["no_transaksi_raw"]]
            result["updated"] += 1
        else:
            result["new"] += 1
        rec["updated_at"] = now_iso
        payload.append(rec)

    # 2) Upsert (order_pk sengaja gak dikirim → nilai di DB tetap)
    for chunk in _chunked(payload, CHUNK_SIZE):
        try:
            client.table(TABLE).upsert(chunk, on_conflict="no_transaksi_raw").execute()
        except Exception as e:
            result["errors"].append(f"upsert: {e}")

    # 3) Sambungin order_pk untuk SEMUA tagihan yang belum nyambung (termasuk upload lama)
    try:
        unlinked = client.table(TABLE).select("id, no_transaksi_raw, order_id") \
            .is_("order_pk", "null").execute().data or []
        for mp in ("desty", "tokopedia"):
            rows = [u for u in unlinked if guess_marketplace(u["no_transaksi_raw"]) == mp]
            ids = list({u["order_id"] for u in rows})
            found = {}
            for chunk in _chunked(ids, CHUNK_SIZE):
                resp = client.table("orders").select("id, order_id") \
                    .eq("marketplace", mp).in_("order_id", chunk).execute()
                for o in resp.data or []:
                    found[str(o["order_id"])] = o["id"]
            for u in rows:
                pk = found.get(u["order_id"])
                if pk:
                    client.table(TABLE).update({"order_pk": pk}).eq("id", u["id"]).execute()
                    result["linked"] += 1
                else:
                    result["still_unlinked"] += 1
    except Exception as e:
        result["errors"].append(f"link order_pk: {e}")

    return result
