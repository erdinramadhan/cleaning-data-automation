"""
loader.py
---------
Validate OrderBundle, terus push ke Supabase dengan logic:
  1. Auto-populate `products` table (SKU baru → insert as 'simple')
  2. Order BARU  → INSERT full (header + items)
  3. Order EXISTING → UPDATE order_status DOANG (kalau berubah)
     → phone, discount, items, harga TIDAK di-touch (data final).
  4. Batch operations (chunk) biar cepet — bukan loop per-order.

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
                .select("id, order_id, order_status")
                .eq("marketplace", marketplace)
                .in_("order_id", chunk)
                .execute()
            )
            for row in (response.data or []):
                existing[str(row["order_id"])] = {
                    "id": row["id"],
                    "order_status": row["order_status"],
                }
        except Exception as e:
            print(f"Warning: fetch_existing_orders chunk failed: {e}")
            continue

    return existing


# ============================================================
# Main push flow (orders → order_items)
# ============================================================

def push_orders(
    bundles: list[OrderBundle],
    bundle_skus: set[str] = None,
) -> dict:
    """
    Push orders dengan logic:
      - EXISTING → UPDATE order_status doang (kalau beda)
      - NEW      → INSERT header + items (batch)

    Returns: {
        "new_orders": int,
        "status_updated": int,
        "status_unchanged": int,
        "items_inserted": int,
        "errors": list,
    }
    """
    if not bundles:
        return {
            "new_orders": 0, "status_updated": 0,
            "status_unchanged": 0, "items_inserted": 0, "errors": [],
        }

    if bundle_skus is None:
        bundle_skus = set()

    client = get_supabase()
    errors = []

    # Asumsi 1 upload = 1 marketplace (detect_marketplace di app.py)
    marketplace = bundles[0].header.marketplace

    # ============================================
    # Step 1: Fetch existing orders (1 batch query)
    # ============================================
    all_order_ids = [b.header.order_id for b in bundles]
    existing = fetch_existing_orders(marketplace, all_order_ids)

    # ============================================
    # Step 2: Split NEW vs EXISTING
    # ============================================
    new_bundles = []
    status_updates = []  # list of (order_pk, new_status)
    status_unchanged = 0

    for bundle in bundles:
        oid = bundle.header.order_id
        if oid in existing:
            # EXISTING → cek status beda?
            db_status = existing[oid]["order_status"]
            new_status = bundle.header.order_status
            if new_status != db_status:
                status_updates.append((existing[oid]["id"], new_status))
            else:
                status_unchanged += 1
        else:
            # NEW → insert full
            new_bundles.append(bundle)

    # ============================================
    # Step 3: EXISTING → batch UPDATE order_status
    # (hanya yang beda; per-row update, tapi cuma buat yg berubah)
    # ============================================
    status_updated = 0
    for order_pk, new_status in status_updates:
        try:
            client.table("orders").update(
                {"order_status": new_status}
            ).eq("id", order_pk).execute()
            status_updated += 1
        except Exception as e:
            errors.append({"order_pk": order_pk, "error": f"status update: {e}"})

    # ============================================
    # Step 4: NEW → batch INSERT header
    # ============================================
    new_orders = 0
    items_inserted = 0

    if new_bundles:
        # Build header records + keep ref ke bundle buat items nanti
        for chunk in _chunked(new_bundles, CHUNK_SIZE):
            header_records = [b.header.to_db_dict() for b in chunk]

            try:
                resp = (
                    client
                    .table("orders")
                    .insert(header_records)
                    .execute()
                )
                inserted_rows = resp.data or []
                new_orders += len(inserted_rows)

                # Map order_id → id (PK) dari response
                # Response order sama dengan insert order (Supabase preserve order)
                oid_to_pk = {str(r["order_id"]): r["id"] for r in inserted_rows}

                # ============================================
                # Step 5: NEW → batch INSERT items (per chunk)
                # ============================================
                item_records = []
                for b in chunk:
                    order_pk = oid_to_pk.get(b.header.order_id)
                    if order_pk is None:
                        errors.append({
                            "order_id": b.header.order_id,
                            "error": "PK not found after header insert",
                        })
                        continue

                    for item in b.items:
                        item_dict = item.to_db_dict(order_pk=order_pk)
                        # Set is_bundle flag dari products table
                        if item.sku and item.sku in bundle_skus:
                            item_dict["is_bundle"] = True
                        item_records.append(item_dict)

                if item_records:
                    # Insert items batch (chunk lagi kalau item > CHUNK_SIZE)
                    for item_chunk in _chunked(item_records, CHUNK_SIZE):
                        client.table("order_items").insert(item_chunk).execute()
                        items_inserted += len(item_chunk)

            except Exception as e:
                errors.append({
                    "chunk_size": len(chunk),
                    "error": f"header/items insert: {e}",
                })
                continue

    return {
        "new_orders": new_orders,
        "status_updated": status_updated,
        "status_unchanged": status_unchanged,
        "items_inserted": items_inserted,
        "errors": errors,
    }


# ============================================================
# All-in-one pipeline
# ============================================================

def push_to_supabase(bundles: list[OrderBundle]) -> dict:
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

    # 2. Auto-populate products
    sku_map = collect_skus_from_bundles(valid_bundles)
    products_result = upsert_products(sku_map)
    result["products"] = products_result

    # 3. Fetch bundle SKUs (yang udah di-flag manual sebelumnya)
    bundle_skus = get_bundle_skus()
    result["bundle_skus_count"] = len(bundle_skus)

    # 4. Push orders
    push_result = push_orders(valid_bundles, bundle_skus=bundle_skus)
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
