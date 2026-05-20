"""
loader.py
---------
Validate OrderBundle, terus insert ke Supabase dengan logic:
  1. Auto-populate `products` table (SKU baru → insert as 'simple')
  2. Upsert `orders` table → ambil id (PK)
  3. Insert `order_items` dengan order_id (FK) yang udah didapat
  4. Set is_bundle berdasarkan products.product_type
"""

from typing import Optional
from pydantic import ValidationError
from config import SUPABASE_URL, SUPABASE_KEY
from schemas import OrderHeader, OrderItem, OrderBundle, Product


# Singleton client
_supabase_client = None


def get_supabase():
    """Lazy init Supabase client."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError("SUPABASE_URL atau SUPABASE_KEY belum di-set")
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase_client


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
            # Re-validate (defensive)
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
    """
    Collect unique SKUs dari semua bundle.
    Return: {sku: product_name} (untuk insert ke products table)
    """
    sku_map = {}
    for bundle in bundles:
        for item in bundle.items:
            if item.sku and item.sku not in sku_map:
                sku_map[item.sku] = item.product_name or item.sku
    return sku_map


def upsert_products(sku_map: dict[str, str]) -> dict:
    """
    Upsert SKUs ke products table.
    SKU baru → insert as 'simple'. SKU yang udah ada → update product_name aja.
    
    Returns: {"new": int, "existing": int, "errors": list}
    """
    if not sku_map:
        return {"new": 0, "existing": 0, "errors": []}
    
    client = get_supabase()
    errors = []
    
    # Build records
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
        # Use upsert with ignore_duplicates=False so nama produk ke-update kalo berubah
        # tapi product_type GAK di-overwrite (karena kita pake on_conflict="sku" dengan default behavior)
        # Strategy: insert baru, ignore kalo conflict (biar product_type yang udah di-set manual gak ke-reset)
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
    """
    Fetch list of SKUs that are marked as 'bundle' di products table.
    Dipake buat set is_bundle flag di order_items.
    """
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
# Main upsert flow (orders → order_items)
# ============================================================

def upsert_orders_with_items(
    bundles: list[OrderBundle],
    bundle_skus: set[str] = None,
) -> dict:
    """
    Insert orders + order_items dengan handle FK.
    
    Strategy:
      1. Upsert order header → returns id (PK)
      2. Delete existing order_items untuk order_id ini (idempotent)
      3. Insert order_items dengan FK order_id
    
    Args:
        bundles: list of OrderBundle (validated)
        bundle_skus: set of SKUs yang di-flag as bundle. Items dengan SKU
                     ini bakal di-set is_bundle=True.
    
    Returns: {
        "orders_upserted": int,
        "items_inserted": int,
        "errors": list,
    }
    """
    if not bundles:
        return {"orders_upserted": 0, "items_inserted": 0, "errors": []}
    
    if bundle_skus is None:
        bundle_skus = set()
    
    client = get_supabase()
    orders_upserted = 0
    items_inserted = 0
    errors = []
    
    for bundle in bundles:
        try:
            # ============================================
            # Step 1: Upsert order header
            # ============================================
            header_dict = bundle.header.to_db_dict()
            
            response = (
                client
                .table("orders")
                .upsert(header_dict, on_conflict="marketplace,order_id")
                .execute()
            )
            
            if not response.data:
                errors.append({
                    "order_id": bundle.header.order_id,
                    "error": "Order upsert returned no data",
                })
                continue
            
            order_pk = response.data[0]["id"]  # the BIGSERIAL PK
            orders_upserted += 1
            
            # ============================================
            # Step 2: Delete existing items (idempotent re-import)
            # ============================================
            client.table("order_items").delete().eq("order_id", order_pk).execute()
            
            # ============================================
            # Step 3: Insert order_items with FK
            # ============================================
            item_records = []
            for item in bundle.items:
                item_dict = item.to_db_dict(order_pk=order_pk)
                
                # Set is_bundle flag based on products table
                if item.sku and item.sku in bundle_skus:
                    item_dict["is_bundle"] = True
                
                item_records.append(item_dict)
            
            if item_records:
                client.table("order_items").insert(item_records).execute()
                items_inserted += len(item_records)
        
        except Exception as e:
            errors.append({
                "order_id": bundle.header.order_id,
                "error": str(e),
            })
            continue
    
    return {
        "orders_upserted": orders_upserted,
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
      4. Upsert orders + items
    
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
    
    # 4. Upsert orders + items
    upsert_result = upsert_orders_with_items(valid_bundles, bundle_skus=bundle_skus)
    result["upsert"] = upsert_result
    
    return result


# ============================================================
# Connection test
# ============================================================

def test_connection() -> tuple[bool, str]:
    try:
        client = get_supabase()
        # Test query ke 4 table
        for table in ["products", "bundle_components", "orders", "order_items"]:
            client.table(table).select("*").limit(1).execute()
        return True, "Connection OK, all 4 tables accessible"
    except Exception as e:
        return False, f"Connection failed: {e}"
