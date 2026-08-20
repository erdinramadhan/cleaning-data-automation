"""
cleaners/desty.py
-----------------
Desty cleaner v2: group by ID Pesanan, return list[OrderBundle].
File Desty itu XLSX disamarin .csv → pake read_excel.

DISCOUNT DESIGN (updated):
  - Diskon Voucher  → ORDER-LEVEL (orders.items_discount). Voucher gak di-attribute
                      ke SKU tertentu karena sifatnya order-wide. Format string
                      "-20,000" → parse pake parse_price_string (handle koma).
  - Diskon Produk   → ITEM-LEVEL (order_items.item_seller_discount). Per-item,
                      sesuai sifat raw Desty (kolom "Diskon Produk", int per row).
  - Distribusi voucher ke SKU cuma dilakukan di v_sku_sales_exploded (analytics),
    bukan di-store di table.
"""

import re
import pandas as pd
from .base import (
    BaseCleaner,
    parse_date_flexible,
    parse_price_string,
    safe_str,
    safe_int,
    normalize_phone_id,
    resolve_sku,
)
from config import hash_pii
from schemas import OrderHeader, OrderItem, OrderBundle, normalize_status


def extract_province_from_address(address):
    """
    Extract province dari format alamat Desty.
    Format: "[detail], [kelurahan], [kecamatan], [kabupaten/kota], [PROVINCE] [POSTAL_CODE]"

    Examples:
    - "...Kab. Rembang, Jawa Tengah 59265" → "Jawa Tengah"
    - "...Jakarta Timur, DKI Jakarta 13410" → "DKI Jakarta"
    """
    if not address or not isinstance(address, str):
        return None
    address = address.strip().replace("\n", " ")
    parts = [p.strip() for p in address.split(",")]
    if len(parts) < 2:
        return None
    last = parts[-1].strip()
    # Strip 5-digit postal code at end (e.g. "Jawa Tengah 59265" → "Jawa Tengah")
    match = re.match(r"^(.+?)\s+\d{5}\s*$", last)
    province = match.group(1).strip() if match else last
    # Normalize whitespace
    province = re.sub(r"\s+", " ", province)
    return province if province else None


def extract_city_from_address(address):
    """
    Extract city/kabupaten dari second-to-last segment.
    Format: "[detail], [kel], [kec], [KABUPATEN/KOTA], [province] [postal]"
    """
    if not address or not isinstance(address, str):
        return None
    address = address.strip().replace("\n", " ")
    parts = [p.strip() for p in address.split(",")]
    if len(parts) < 3:
        return None
    city = parts[-2].strip()
    city = re.sub(r"\s+", " ", city)
    return city if city else None


class DestyCleaner(BaseCleaner):
    marketplace = "desty"

    expected_columns = {
        "Tipe Order",
        "Waktu Pesanan",
        "ID Pesanan",
        "Waktu Pembayaran Pesanan",
        "Status",
        "SKU",
        "Produk",
        "Jumlah",
        "Harga Unit",
        "Jumlah dibayarkan oleh Customer",
    }

    def load(self, file_obj) -> pd.DataFrame:
        try:
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            return pd.read_excel(file_obj)
        except Exception:
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            return pd.read_csv(file_obj)

    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.dropna(how="all").copy()

        str_cols = df.select_dtypes(include="object").columns
        for col in str_cols:
            df[col] = df[col].astype(str).str.strip()
            df[col] = df[col].replace(["nan", "NaN", "", "-"], pd.NA)

        df = df.drop_duplicates(subset=["ID Pesanan", "SKU"], keep="first")
        return df

    def to_order_bundles(self, df: pd.DataFrame, source_file: str, barcode_map: dict = None) -> list[OrderBundle]:
        """
        Desty field mapping:
          - Biaya Pengiriman = shipping fee (string "25,000")
          - Diskon Ongkir = shipping discount (string "-20,000", abs-kan)
          - Biaya Pembayaran = payment fee
          - Asuransi Pengiriman = insurance
          - Jumlah dibayarkan oleh Customer = order total (string "125,000")
          - Diskon Voucher = voucher ORDER-LEVEL → items_discount (string "-20,000")
          - Diskon Produk = diskon ITEM-LEVEL → item_seller_discount (int per row)
        """
        bundles = []

        for order_id, group_df in df.groupby("ID Pesanan"):
            try:
                first = group_df.iloc[0]

                order_date = parse_date_flexible(first.get("Waktu Pesanan"))
                paid_at = parse_date_flexible(first.get("Waktu Pembayaran Pesanan"))

                if order_date is None:
                    continue

                # Order-level (Desty pake string format buat harga!)
                # Desty gak punya breakdown shipping platform vs seller
                shipping_fee = parse_price_string(first.get("Biaya Pengiriman")) or 0
                shipping_seller_discount = abs(parse_price_string(first.get("Diskon Ongkir")) or 0)
                shipping_platform_discount = 0
                payment_fee = safe_int(first.get("Biaya Pembayaran"), 0)
                insurance_fee = safe_int(first.get("Asuransi Pengiriman"), 0)
                order_total = parse_price_string(first.get("Jumlah dibayarkan oleh Customer")) or 0

                # === Voucher ORDER-LEVEL ===
                # Diskon Voucher = string "-20,000" (muncul di 1 row aja, item pertama).
                # parse_price_string handle koma; SUM across rows defensive (sisanya 0).
                order_voucher_total = sum(
                    abs(parse_price_string(row.get("Diskon Voucher")) or 0)
                    for _, row in group_df.iterrows()
                )

                # Customer
                customer_name = (
                    safe_str(first.get("Nama Pelanggan")) or
                    safe_str(first.get("Informasi Pengiriman (Nama)"))
                )
                customer_phone = (
                    safe_str(first.get("No Telp Pelanggan")) or
                    safe_str(first.get("Informasi Pengiriman (No Telp)"))
                )

                # Items
                items_subtotal = 0
                items = []

                for _, row in group_df.iterrows():
                    qty = safe_int(row.get("Jumlah"), 1)
                    unit_price = parse_price_string(row.get("Harga Unit")) or 0

                    # Discount mapping di Desty:
                    # - Diskon Produk = ITEM-LEVEL (per-item, int). Masuk item_seller_discount.
                    # - Diskon Voucher = ORDER-LEVEL. TIDAK di sini (di header items_discount).
                    # - Desty gak punya platform subsidy.
                    item_seller_disc = abs(safe_int(row.get("Diskon Produk"), 0))
                    item_platform_disc = 0
                    item_disc_total = item_seller_disc + item_platform_disc

                    subtotal = max(0, (unit_price * qty) - item_disc_total)

                    items_subtotal += unit_price * qty
                    # NOTE: items_discount (header) = order_voucher_total (order-level),
                    # bukan akumulasi item discount. Diskon produk cuma di item_seller_discount.

                    raw_sku = safe_str(row.get("SKU"))
                    resolved_sku, _ = resolve_sku(raw_sku, barcode_map)
                    
                    item = OrderItem(
                        sku=resolved_sku,
                        product_name=safe_str(row.get("Produk")),
                        variation=None,
                        quantity=qty,
                        unit_price=unit_price,
                        item_platform_discount=item_platform_disc,
                        item_seller_discount=item_seller_disc,
                        subtotal=subtotal,
                        is_bundle=False,
                        raw_data=row.dropna().to_dict(),
                    )
                    items.append(item)

                header = OrderHeader(
                    marketplace="desty",
                    order_id=safe_str(order_id),

                    raw_status=safe_str(first.get("Status")),
                    order_status=normalize_status(first.get("Status")),

                    order_date=order_date,
                    paid_at=paid_at,

                    items_subtotal=items_subtotal,
                    items_discount=order_voucher_total,  # voucher ORDER-LEVEL
                    shipping_fee=shipping_fee,
                    shipping_platform_discount=shipping_platform_discount,
                    shipping_seller_discount=shipping_seller_discount,
                    order_discount=0,  # Desty gak ada explicit order-level discount lain
                    payment_fee=payment_fee,
                    insurance_fee=insurance_fee,
                    order_total=order_total,

                    shipping_provider=safe_str(first.get("Metode Pengiriman")),
                    shipping_city=extract_city_from_address(safe_str(first.get("Alamat Pengiriman"))),
                    shipping_province=extract_province_from_address(safe_str(first.get("Alamat Pengiriman"))),

                    customer_name_hash=hash_pii(customer_name),
                    customer_phone_hash=hash_pii(customer_phone),

                    customer_name=safe_str(customer_name),
                    customer_phone=normalize_phone_id(customer_phone),

                    raw_data={"header_source_row": first.dropna().to_dict()},
                    source_file=source_file,
                )

                bundles.append(OrderBundle(header=header, items=items))

            except Exception as e:
                print(f"[Desty] Error processing order {order_id}: {e}")
                continue

        return bundles
