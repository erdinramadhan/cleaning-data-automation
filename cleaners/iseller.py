"""
cleaners/iseller.py
-------------------
Iseller cleaner v2: group by Nomor Pesanan, return list[OrderBundle].
"""

import pandas as pd
from .base import (
    BaseCleaner,
    parse_date_flexible,
    safe_str,
    safe_int,
    normalize_phone_id,
)
from config import hash_pii
from schemas import OrderHeader, OrderItem, OrderBundle, normalize_status


class IsellerCleaner(BaseCleaner):
    marketplace = "iseller"
    
    expected_columns = {
        "Tanggal Pesanan",
        "Nomor Pesanan",
        "Status Pembayaran",
        "Sku",
        "Produk",
        "Jumlah",
        "Harga Asli",
        "Total Jumlah Pesanan",
        "Metode Pembayaran",
        "Toko",
    }
    
    def load(self, file_obj) -> pd.DataFrame:
        """Auto-detect comma vs semicolon separator."""
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        first_line = file_obj.readline()
        if isinstance(first_line, bytes):
            first_line = first_line.decode("utf-8")
        sep = ";" if first_line.count(";") > first_line.count(",") else ","
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        return pd.read_csv(file_obj, sep=sep)
    
    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.dropna(how="all").copy()
        
        str_cols = df.select_dtypes(include="object").columns
        for col in str_cols:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace(["nan", "NaN", ""], pd.NA)
        
        df = df.drop_duplicates(subset=["Nomor Pesanan", "Sku"], keep="first")
        return df
    
    def to_order_bundles(self, df: pd.DataFrame, source_file: str) -> list[OrderBundle]:
        """
        Iseller field mapping:
          - Subtotal Per Order = items subtotal (order-level)
          - Pengiriman = shipping fee
          - Discount Shipping = shipping discount
          - Platform Fee = payment fee
          - Pajak per Pesanan = could be insurance/tax
          - Total Jumlah Pesanan = order total
        """
        bundles = []
        
        for order_id, group_df in df.groupby("Nomor Pesanan"):
            try:
                first = group_df.iloc[0]
                
                order_date = parse_date_flexible(first.get("Tanggal Pesanan"))
                paid_at = parse_date_flexible(first.get("Terbayar Pada"))
                
                if order_date is None:
                    continue
                
                # Order-level financials
                # Iseller gak ada breakdown platform vs seller, semua dianggap seller
                shipping_fee = safe_int(first.get("Pengiriman"), 0)
                shipping_seller_discount = abs(safe_int(first.get("Discount Shipping"), 0))
                shipping_platform_discount = 0
                payment_fee = safe_int(first.get("Platform Fee"), 0)
                order_total = safe_int(first.get("Total Jumlah Pesanan"), 0)
                
                # Customer
                customer_name = safe_str(first.get("Pelanggan")) or safe_str(first.get("Nama tagihan"))
                customer_phone = safe_str(first.get("Nomor Telepon Pelanggan"))
                
                # Items
                items_subtotal = 0
                items_discount = 0
                items = []
                
                for _, row in group_df.iterrows():
                    qty = safe_int(row.get("Jumlah"), 1)
                    unit_price = safe_int(row.get("Harga Asli"), 0)
                    # Iseller gak punya breakdown, semua diskon dianggap seller discount
                    item_seller_disc = safe_int(row.get("Jumlah Diskon per item"), 0)
                    item_platform_disc = 0
                    item_disc_total = item_seller_disc + item_platform_disc
                    
                    subtotal = safe_int(row.get("Jumlah subtotal per Item"), 0)
                    if subtotal == 0:
                        subtotal = max(0, (unit_price * qty) - item_disc_total)
                    
                    items_subtotal += unit_price * qty
                    items_discount += item_disc_total
                    
                    item = OrderItem(
                        sku=safe_str(row.get("Sku")),
                        product_name=safe_str(row.get("Produk")),
                        variation=safe_str(row.get("Variant 1")),
                        quantity=qty,
                        unit_price=unit_price,
                        item_platform_discount=item_platform_disc,
                        item_seller_discount=item_seller_disc,
                        subtotal=subtotal,
                        is_bundle=False,
                        raw_data=row.dropna().to_dict(),
                    )
                    items.append(item)
                
                # Order-level voucher discount (kalo ada)
                order_discount = safe_int(first.get("Jumlah Diskon"), 0) - items_discount
                order_discount = max(0, order_discount)
                
                header = OrderHeader(
                    marketplace="iseller",
                    order_id=safe_str(order_id),
                    
                    raw_status=safe_str(first.get("Status Pembayaran")),
                    order_status=normalize_status(first.get("Status Pembayaran")),
                    
                    order_date=order_date,
                    paid_at=paid_at,
                    
                    items_subtotal=items_subtotal,
                    items_discount=items_discount,
                    shipping_fee=shipping_fee,
                    shipping_platform_discount=shipping_platform_discount,
                    shipping_seller_discount=shipping_seller_discount,
                    order_discount=order_discount,
                    payment_fee=payment_fee,
                    insurance_fee=0,
                    order_total=order_total,
                    
                    shipping_provider=safe_str(first.get("Perusahaan Ekspedisi")),
                    shipping_city=safe_str(first.get("Kota Pengiriman")),
                    shipping_province=safe_str(first.get("Provinsi Penagihan")),
                    
                    customer_name_hash=hash_pii(customer_name),
                    customer_phone_hash=hash_pii(customer_phone),
                    
                    customer_name=safe_str(customer_name),
                    customer_phone=normalize_phone_id(customer_phone),
                    
                    raw_data={"header_source_row": first.dropna().to_dict()},
                    source_file=source_file,
                )
                
                bundles.append(OrderBundle(header=header, items=items))
                
            except Exception as e:
                print(f"[Iseller] Error processing order {order_id}: {e}")
                continue
        
        return bundles
