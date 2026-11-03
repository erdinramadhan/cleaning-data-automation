"""
loader.py
---------
Validate OrderBundle, terus push ke Supabase (DB dashboard / DB-B) dengan logic:
  1. Auto-populate `products` table (SKU baru → insert as 'simple')
  2. Order BARU  → INSERT full (header + items)
  3. Order EXISTING milik desty-api (source_file = 'desty-api', masuk lewat
     desty-sync dengan ongkir/diskon/asuransi/no telp bolong) → DIAMBIL ALIH:
     header ditimpa data export + items diganti isi export. raw_data respons API
     dibiarkan. Setelah ini source_file = nama file export, jadi desty-sync tidak
     menimpanya lagi.
  4. Order EXISTING milik export → UPDATE order_status (kalau berubah) +
     ISI customer_phone / customer_name / gudang yang masih kosong di DB.
     Angka & items TIDAK di-touch (data final).
  5. Batch operations (chunk) biar cepet — bukan loop per-order.
  6. dry_run=True → cuma hitung rencana (baru / ambil alih / update), tidak menulis.

Kenapa gini:
  - Order yang udah masuk DB = transaksi final. Yang berubah cuma STATUS
    (lifecycle: paid → shipped → completed).
  - Loader lama pakai upsert overwrite-all + delete+reinsert items → bikin
    phone backfill & manual fix ILANG, plus lambat (3 API call per order).
"""

from typing import Optional
from pydantic import ValidationError
from config import SUPABASE_URL, SUPABASE_KEY
from schemas import OrderHeader, OrderItem, OrderBundle, Product


# Singleton client
_supabase_client = None

# Batch chunk size (konservatif, aman buat payload Supabase)
CHUNK_SIZE = 200


def get_supabase():
    """Lazy init Supabase client."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError("SUPABASE_URL atau SUPABASE_KEY belum di-set")
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


def _chunked(items: list, size: int = CHUNK_SIZE):
    """Yield potongan list ukuran `size`."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ============================================================
# Validation
# ============================================================

def validate_bundles(bundles: list[OrderBundle]) -> tuple[list[OrderBundle], list[dict]]:
    """
    Validate bundles. Sebenarnya udah ke-validate pas init OrderBundle,
    tapi kita catch lagi buat safety + collect errors.
    """
    valid = []
    errors = []

    for idx, bundle in enumerate(bundles):
        try:
            OrderBundle.model_validate(bundle.model_dump())
            valid.append(bundle)
        except ValidationError as e:
            errors.append({
                "index": idx,
                "order_id": bundle.header.order_id if bundle.header else None,
                "error": str(e),
            })

    return valid, errors


# ============================================================
# Products auto-populate
# ============================================================

def collect_skus_from_bundles(bundles: list[OrderBundle]) -> dict[str, str]:
    """Collect unique SKUs dari semua bundle. Return {sku: product_name}."""
    sku_map = {}
    for bundle in bundles:
        for item in bundle.items:
            if item.sku and item.sku not in sku_map:
                sku_map[item.sku] = item.product_name or item.sku
    return sku_map


def upsert_products(sku_map: dict[str, str]) -> dict:
    """
    Upsert SKUs ke products table.
    SKU baru → insert as 'simple'. SKU yang udah ada → ignore (biar
    product_type/cogs yang di-set manual GAK ke-reset).
    """
    if not sku_map:
        return {"new": 0, "existing": 0, "errors": []}

    client = get_supabase()
    errors = []

    records = [
        {
            "sku": sku,
            "product_name": name,
            "product_type": "simple",  # default; user edit manual jadi 'bundle'
            "is_active": True,
        }
        for sku, name in sku_map.items()
    ]

    try:
        # ignore_duplicates=True → SKU existing gak ke-overwrite (product_type/cogs aman)
        response = (
            client
            .table("products")
            .upsert(records, on_conflict="sku", ignore_duplicates=True)
            .execute()
        )
        new_count = len(response.data) if response.data else 0
        return {
            "new": new_count,
            "existing": len(records) - new_count,
            "errors": [],
        }
    except Exception as e:
        errors.append(str(e))
        return {"new": 0, "existing": 0, "errors": errors}


def get_bundle_skus() -> set[str]:
    """Fetch SKUs yang di-flag 'bundle' di products. Dipake set is_bundle flag."""
    try:
        client = get_supabase()
        response = (
            client
            .table("products")
            .select("sku")
            .eq("product_type", "bundle")
            .execute()
        )
        return {row["sku"] for row in (response.data or [])}
    except Exception as e:
        print(f"Warning: failed to fetch bundle SKUs: {e}")
        return set()


# ============================================================
# Fetch existing orders (buat split NEW vs EXISTING)
# ============================================================

API_SOURCE = "desty-api"   # source_file baris yang masuk lewat desty-sync
FILL_IF_EMPTY = ("customer_phone", "customer_name", "gudang")
# Saat ambil alih order API: kalau kolom ini KOSONG di export, nilai lama di DB
# dipertahankan (mis. export Tokopedia tidak berisi no. telp — jangan dihapus).
KEEP_IF_EMPTY = ("customer_phone", "customer_name", "customer_phone_hash", "customer_name_hash",
                 "gudang", "shipping_provider", "shipping_city", "shipping_province", "paid_at")


def fetch_existing_orders(marketplace: str, order_ids: list[str]) -> dict[str, dict]:
    """
    Fetch order yang UDAH ADA di DB untuk marketplace + list order_id ini.
    Return: {order_id: {"id": pk, "order_status": status}}

    Di-chunk biar query `in_` gak kepanjangan.
    """
    if not order_ids:
        return {}

    client = get_supabase()
    existing = {}

    for chunk in _chunked(order_ids, CHUNK_SIZE):
        try:
            response = (
                client
                .table("orders")
                .select("id, order_id, order_status, source_file, " + ", ".join(KEEP_IF_EMPTY))
                .eq("marketplace", marketplace)
                .in_("order_id", chunk)
                .execute()
            )
            for row in (response.data or []):
                existing[str(row["order_id"])] = {
                    "id": row["id"],
                    "order_status": row["order_status"],
                    "source_file": row.get("source_file"),
                    **{f: row.get(f) for f in KEEP_IF_EMPTY},
                }
        except Exception as e:
            print(f"Warning: fetch_existing_orders chunk failed: {e}")
            continue

    return existing


# ============================================================
# Main push flow (orders → order_items)
# ============================================================



def merge_items(items_dicts: list[dict]) -> list[dict]:
    """Gabung item dengan SKU sama dalam 1 order (mis. barcode & SKU yang
    ter-resolve jadi SKU yang sama) → qty/subtotal/diskon dijumlah.
    order_items punya UNIQUE (order_id, sku); tanpa ini insert 1 chunk gagal."""
    out, idx = [], {}
    for d in items_dicts:
        key = (d.get("order_id"), d.get("sku"))
        if d.get("sku") and key in idx:
            m = out[idx[key]]
            for f in ("quantity", "subtotal", "item_seller_discount", "item_platform_discount"):
                m[f] = (m.get(f) or 0) + (d.get(f) or 0)
            m["is_bundle"] = m.get("is_bundle") or d.get("is_bundle")
        else:
            idx[key] = len(out)
            out.append(dict(d))
    return out


def _items_for(bundle, order_pk: int, bundle_skus: set) -> list[dict]:
    rows = []
    for item in bundle.items:
        d = item.to_db_dict(order_pk=order_pk)
        if item.sku and item.sku in bundle_skus:
            d["is_bundle"] = True
        rows.append(d)
    return merge_items(rows)


def push_orders(
    bundles: list[OrderBundle],
    bundle_skus: set[str] = None,
    dry_run: bool = False,
) -> dict:
    """
    Push orders dengan logic:
      - NEW                       → INSERT header + items (batch)
      - EXISTING milik desty-api  → AMBIL ALIH: header ditimpa export, items diganti
      - EXISTING milik export     → UPDATE order_status (kalau beda) + isi
                                    phone/nama/gudang yang masih kosong

    dry_run=True → tidak menulis apa pun, cuma isi angka rencana.

    Returns: {
        "new_orders", "taken_over", "status_updated", "filled", "status_unchanged",
        "items_inserted", "errors", "dry_run",
    }
    """
    result = {
        "new_orders": 0, "taken_over": 0, "status_updated": 0, "filled": 0,
        "status_unchanged": 0, "items_inserted": 0, "errors": [], "dry_run": dry_run,
    }
    if not bundles:
        return result
    if bundle_skus is None:
        bundle_skus = set()

    client = get_supabase()
    errors = result["errors"]

    # Asumsi 1 upload = 1 marketplace (detect_marketplace di app.py)
    marketplace = bundles[0].header.marketplace

    # Step 1: fetch existing (1 batch query per chunk)
    existing = fetch_existing_orders(marketplace, [b.header.order_id for b in bundles])

    # Step 2: split
    new_bundles, takeover, patches = [], [], []   # patches: (order_pk, {kolom: nilai})
    for bundle in bundles:
        ex = existing.get(bundle.header.order_id)
        if ex is None:
            new_bundles.append(bundle)
        elif ex.get("source_file") == API_SOURCE:
            takeover.append((ex["id"], bundle, ex))
        else:
            patch = {}
            if bundle.header.order_status != ex["order_status"]:
                patch["order_status"] = bundle.header.order_status
            filled = {f: getattr(bundle.header, f) for f in FILL_IF_EMPTY
                      if not ex.get(f) and getattr(bundle.header, f)}
            patch.update(filled)
            if patch:
                patches.append((ex["id"], patch, "order_status" in patch, bool(filled)))
            else:
                result["status_unchanged"] += 1

    if dry_run:
        result["new_orders"] = len(new_bundles)
        result["taken_over"] = len(takeover)
        result["status_updated"] = sum(1 for p in patches if p[2])
        result["filled"] = sum(1 for p in patches if p[3])
        result["items_inserted"] = sum(len(merge_items([i.to_db_dict(order_pk=0) for i in b.items]))
                                       for b in new_bundles + [b for _, b, _ in takeover])
        return result

    # Step 3: EXISTING milik export → patch status / isi yang kosong
    for order_pk, patch, st_changed, filled in patches:
        try:
            client.table("orders").update(patch).eq("id", order_pk).execute()
            result["status_updated"] += int(st_changed)
            result["filled"] += int(filled)
        except Exception as e:
            errors.append({"order_pk": order_pk, "error": f"update: {e}"})

    # Step 4: EXISTING milik desty-api → ambil alih (header + items)
    for chunk in _chunked(takeover, CHUNK_SIZE):
        try:
            headers = []
            for _, b, ex in chunk:
                h = b.header.to_db_dict()
                h.pop("raw_data", None)   # respons API di raw_data dipertahankan
                for f in KEEP_IF_EMPTY:   # kosong di export → pakai nilai DB
                    if h.get(f) in (None, "") and ex.get(f) not in (None, ""):
                        h[f] = ex[f]
                headers.append(h)
            client.table("orders").upsert(headers, on_conflict="marketplace,order_id").execute()
            pks = [pk for pk, _, _ in chunk]
            client.table("order_items").delete().in_("order_id", pks).execute()
            item_records = [r for pk, b, _ in chunk for r in _items_for(b, pk, bundle_skus)]
            for item_chunk in _chunked(item_records, CHUNK_SIZE):
                client.table("order_items").insert(item_chunk).execute()
                result["items_inserted"] += len(item_chunk)
            result["taken_over"] += len(chunk)
        except Exception as e:
            errors.append({"chunk_size": len(chunk), "error": f"ambil alih: {e}"})

    # Step 5: NEW → batch INSERT header + items
    for chunk in _chunked(new_bundles, CHUNK_SIZE):
        try:
            resp = client.table("orders").insert([b.header.to_db_dict() for b in chunk]).execute()
            inserted_rows = resp.data or []
            result["new_orders"] += len(inserted_rows)
            oid_to_pk = {str(r["order_id"]): r["id"] for r in inserted_rows}
            item_records = []
            for b in chunk:
                order_pk = oid_to_pk.get(b.header.order_id)
                if order_pk is None:
                    errors.append({"order_id": b.header.order_id, "error": "PK not found after header insert"})
                    continue
                item_records.extend(_items_for(b, order_pk, bundle_skus))
            for item_chunk in _chunked(item_records, CHUNK_SIZE):
                client.table("order_items").insert(item_chunk).execute()
                result["items_inserted"] += len(item_chunk)
        except Exception as e:
            errors.append({"chunk_size": len(chunk), "error": f"header/items insert: {e}"})

    return result


# ============================================================
# All-in-one pipeline
# ============================================================

def push_to_supabase(bundles: list[OrderBundle], dry_run: bool = False) -> dict:
    """
    Full pipeline:
      1. Validate
      2. Auto-populate products (SKU baru)
      3. Fetch bundle SKUs
      4. Push orders (NEW insert / EXISTING update status)

    Returns: dict with all step results.
    """
    result = {}

    # 1. Validate
    valid_bundles, validation_errors = validate_bundles(bundles)
    result["validation"] = {
        "valid": len(valid_bundles),
        "errors": validation_errors,
    }

    if not valid_bundles:
        return result

    # 2. Auto-populate products (dilewati saat dry_run)
    sku_map = collect_skus_from_bundles(valid_bundles)
    products_result = {"new": 0, "existing": 0, "errors": [], "skus_in_file": len(sku_map)} if dry_run \
        else upsert_products(sku_map)
    result["products"] = products_result

    # 3. Fetch bundle SKUs (yang udah di-flag manual sebelumnya)
    bundle_skus = get_bundle_skus()
    result["bundle_skus_count"] = len(bundle_skus)

    # 4. Push orders
    push_result = push_orders(valid_bundles, bundle_skus=bundle_skus, dry_run=dry_run)
    result["upsert"] = push_result

    return result


# ============================================================
# Connection test
# ============================================================

def test_connection() -> tuple[bool, str]:
    try:
        client = get_supabase()
        for table in ["products", "bundle_components", "orders", "order_items"]:
            client.table(table).select("*").limit(1).execute()
        return True, "Connection OK, all 4 tables accessible"
    except Exception as e:
        return False, f"Connection failed: {e}"
