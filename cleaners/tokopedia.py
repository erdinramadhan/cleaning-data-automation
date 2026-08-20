"""
cleaners/tokopedia.py
---------------------
Tokopedia cleaner v2: group by Order ID, return list[OrderBundle].
"""

import pandas as pd
from .base import (
    BaseCleaner,
    parse_date_flexible,
    safe_str,
    safe_int,
    resolve_sku,
)
from config import hash_pii
from schemas import OrderHeader, OrderItem, OrderBundle, normalize_status


class TokopediaCleaner(BaseCleaner):
    marketplace = "tokopedia"
    
    expected_columns = {
        "Order ID",
        "Order Status",
        "SKU ID",
        "Seller SKU",
        "Product Name",
        "Quantity",
        "SKU Unit Original Price",
        "Order Amount",
        "Created Time",
        "Tokopedia Invoice Number",
    }
    
    def load(self, file_obj) -> pd.DataFrame:
        """
        Auto-detect separator (comma vs semicolon).
        Tokopedia kadang export pake ; (kalau dari Excel/Google Sheets locale Eropa).
        """
        # Read first line buat sniff separator
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        
        first_line = file_obj.readline()
        if isinstance(first_line, bytes):
            first_line = first_line.decode("utf-8-sig")
        
        # Hitung jumlah comma vs semicolon
        n_comma = first_line.count(",")
        n_semi = first_line.count(";")
        
        # Pilih separator yang paling banyak (Tokopedia ada 63 kolom, jadi pasti banyak)
        sep = ";" if n_semi > n_comma else ","
        
        # Reset & re-read full file dengan separator yang benar
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        return pd.read_csv(file_obj, encoding="utf-8-sig", sep=sep)
    
    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.dropna(how="all").copy()
        
        # Strip whitespace + trailing tab (Tokopedia quirk)
        str_cols = df.select_dtypes(include="object").columns
        for col in str_cols:
            df[col] = df[col].astype(str).str.strip().str.rstrip("\t")
            df[col] = df[col].replace(["nan", "NaN", ""], pd.NA)
        
        # Drop dupes by Order ID + SKU (line item level)
        df = df.drop_duplicates(subset=["Order ID", "Seller SKU"], keep="first")
        
        return df
    
    def to_order_bundles(self, df: pd.DataFrame, source_file: str, barcode_map: dict = None) -> list[OrderBundle]:
        """
        Group by Order ID. Untuk tiap order:
          - First row → header (order-level fields: shipping, total, customer, etc)
          - All rows → line items
        """
        bundles = []
        
        # Group by Order ID
        for order_id, group_df in df.groupby("Order ID"):
            try:
                first = group_df.iloc[0]
                
                # Parse dates
                order_date = parse_date_flexible(first.get("Created Time"))
                paid_at = parse_date_flexible(first.get("Paid Time"))
                
                if order_date is None:
                    print(f"[Tokopedia] Skip order {order_id}: no order_date")
                    continue
                
                # ====================================================
                # ORDER-LEVEL FINANCIALS (ambil dari first row)
                # ====================================================
                # Tokopedia field mapping:
                #   - Shipping Fee After Discount = ongkir setelah diskon
                #   - Original Shipping Fee = ongkir asli
                #   - Shipping Fee Seller Discount + Platform Discount = total shipping discount
                #   - Payment platform discount = order-level discount (voucher)
                #   - Buyer Service Fee = payment fee
                #   - Handling Fee = payment fee
                #   - Item Insurance = insurance
                #   - Order Amount = total final
                
                shipping_fee = safe_int(first.get("Original Shipping Fee"), 0)
                shipping_seller_discount = safe_int(first.get("Shipping Fee Seller Discount"), 0)
                shipping_platform_discount = safe_int(first.get("Shipping Fee Platform Discount"), 0)
                order_discount = safe_int(first.get("Payment platform discount"), 0)
                payment_fee = (
                    safe_int(first.get("Buyer Service Fee"), 0) +
                    safe_int(first.get("Handling Fee"), 0)
                )
                insurance_fee = (
                    safe_int(first.get("Item Insurance"), 0) +
                    safe_int(first.get("Shipping Insurance"), 0)
                )
                order_total = safe_int(first.get("Order Amount"), 0)
                
                # ====================================================
                # ITEMS aggregation (sum semua row dalam group)
                # ====================================================
                items_subtotal = 0
                items_discount = 0
                items = []
                
                for _, row in group_df.iterrows():
                    qty = safe_int(row.get("Quantity"), 1)
                    unit_price = safe_int(row.get("SKU Unit Original Price"), 0)
                    
                    # Item-level discount: pisah platform vs seller
                    item_platform_disc = safe_int(row.get("SKU Platform Discount"), 0)
                    item_seller_disc = safe_int(row.get("SKU Seller Discount"), 0)
                    item_disc_total = item_platform_disc + item_seller_disc
                    
                    # Subtotal: SKU Subtotal After Discount kalo ada, fallback hitung manual
                    subtotal = safe_int(row.get("SKU Subtotal After Discount"), 0)
                    if subtotal == 0:
                        subtotal = max(0, (unit_price * qty) - item_disc_total)
                    
                    items_subtotal += unit_price * qty
                    items_discount += item_disc_total

                    raw_sku = safe_str(row.get("Seller SKU"))
                    resolved_sku, _ = resolve_sku(raw_sku, barcode_map)

                    item = OrderItem(
                        sku=resolved_sku,
                        product_name=safe_str(row.get("Product Name")),
                        variation=safe_str(row.get("Variation")),
                        quantity=qty,
                        unit_price=unit_price,
                        item_platform_discount=item_platform_disc,
                        item_seller_discount=item_seller_disc,
                        subtotal=subtotal,
                        is_bundle=False,  # auto-detect later via products table
                        raw_data=row.dropna().to_dict(),
                    )
                    items.append(item)
                
                # ====================================================
                # HEADER
                # ====================================================
                header = OrderHeader(
                    marketplace="tokopedia",
                    order_id=safe_str(order_id),
                    
                    raw_status=safe_str(first.get("Order Status")),
                    order_status=normalize_status(first.get("Order Status")),
                    
                    order_date=order_date,
                    paid_at=paid_at,
                    
                    items_subtotal=items_subtotal,
                    items_discount=items_discount,
                    shipping_fee=shipping_fee,
                    shipping_platform_discount=shipping_platform_discount,
                    shipping_seller_discount=shipping_seller_discount,
                    order_discount=order_discount,
                    payment_fee=payment_fee,
                    insurance_fee=insurance_fee,
                    order_total=order_total,
                    
                    shipping_provider=safe_str(first.get("Shipping Provider Name")),
                    shipping_city=safe_str(first.get("Regency and City")),
                    shipping_province=safe_str(first.get("Province")),
                    
                    customer_name_hash=hash_pii(safe_str(first.get("Recipient"))),
                    customer_phone_hash=hash_pii(safe_str(first.get("Phone #"))),
                    
                    raw_data={"header_source_row": first.dropna().to_dict()},
                    source_file=source_file,
                )
                
                bundles.append(OrderBundle(header=header, items=items))
                
            except Exception as e:
                print(f"[Tokopedia] Error processing order {order_id}: {e}")
                continue
        
        return bundles
