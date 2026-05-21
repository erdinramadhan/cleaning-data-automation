"""
schemas.py
----------
Pydantic models buat validate data sebelum masuk Supabase.
3 schemas: OrderHeader, OrderItem, Product.
"""

from datetime import datetime
from typing import Optional, Literal, Any
from pydantic import BaseModel, Field, field_validator


# ============================================================
# Status normalization
# ============================================================
STATUS_MAPPING = {
    # Tokopedia
    "perlu dikirim": "pending",
    "menunggu pengiriman": "pending",
    "dikirim": "shipped",
    "terkirim": "shipped",
    "sampai": "completed",
    "selesai": "completed",
    "dibatalkan": "cancelled",
    "pesanan dibatalkan": "cancelled",
    
    # Iseller
    "paid": "paid",
    "unpaid": "pending",
    "pending": "pending",
    "fulfilled": "completed",
    "unfulfilled": "pending",
    "cancelled": "cancelled",
    "refunded": "cancelled",
    
    # Desty
    "shipping": "shipped",
    "shipped": "shipped",
    "delivered": "completed",
    "completed": "completed",
    "in process": "pending",
    "in-process": "pending",
    "in_process": "pending",
}


def normalize_status(raw_status: str) -> str:
    if not raw_status:
        return "unknown"
    return STATUS_MAPPING.get(str(raw_status).lower().strip(), "unknown")


# ============================================================
# OrderHeader - represents 1 row di table `orders`
# ============================================================
class OrderHeader(BaseModel):
    """Order-level data. 1 OrderHeader = 1 row di orders table."""
    
    # Identity
    marketplace: Literal["tokopedia", "iseller", "desty"]
    order_id: str = Field(min_length=1)
    
    # Status
    order_status: str = "unknown"
    raw_status: Optional[str] = None
    
    # Dates
    order_date: datetime
    paid_at: Optional[datetime] = None
    
    # Order-level financials (semua int IDR)
    items_subtotal: int = Field(default=0, ge=0)
    items_discount: int = Field(default=0, ge=0)
    shipping_fee: int = Field(default=0, ge=0)
    shipping_platform_discount: int = Field(default=0, ge=0)
    shipping_seller_discount: int = Field(default=0, ge=0)
    order_discount: int = Field(default=0, ge=0)
    payment_fee: int = Field(default=0, ge=0)
    insurance_fee: int = Field(default=0, ge=0)
    order_total: Optional[int] = Field(default=None, ge=0)
    
    # Logistics
    shipping_provider: Optional[str] = None
    shipping_city: Optional[str] = None
    shipping_province: Optional[str] = None
    
    # Customer (hashed - legacy, untuk backward compat)
    customer_name_hash: Optional[str] = None
    customer_phone_hash: Optional[str] = None
    
    # Customer (plain - normalized, untuk dedup analytics)
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None  # format: 62xxxxxxxxxx
    
    # Audit
    raw_data: Optional[dict[str, Any]] = None
    source_file: Optional[str] = None
    
    @field_validator("order_id", mode="before")
    @classmethod
    def stringify_order_id(cls, v):
        if v is None:
            return None
        return str(v).strip()
    
    def to_db_dict(self) -> dict:
        d = self.model_dump()
        for key in ["order_date", "paid_at"]:
            if d.get(key) is not None:
                d[key] = d[key].isoformat()
        return d


# ============================================================
# OrderItem - represents 1 row di table `order_items`
# ============================================================
class OrderItem(BaseModel):
    """Line item. Multiple OrderItem per OrderHeader."""
    
    # FK akan di-set setelah OrderHeader di-insert
    # (pas validate, kita gak set order_id yet, di-set di loader)
    sku: Optional[str] = None
    product_name: Optional[str] = None
    variation: Optional[str] = None
    quantity: int = Field(gt=0)
    
    # Item-level financials
    unit_price: int = Field(default=0, ge=0)
    item_platform_discount: int = Field(default=0, ge=0)
    item_seller_discount: int = Field(default=0, ge=0)
    subtotal: int = Field(default=0, ge=0)
    
    is_bundle: bool = False
    
    raw_data: Optional[dict[str, Any]] = None
    
    @field_validator("sku", mode="before")
    @classmethod
    def stringify_sku(cls, v):
        if v is None:
            return None
        return str(v).strip()
    
    def to_db_dict(self, order_pk: int) -> dict:
        """Convert ke dict, tambahin order_id (FK)."""
        d = self.model_dump()
        d["order_id"] = order_pk
        return d


# ============================================================
# OrderBundle - container OrderHeader + items
# ============================================================
class OrderBundle(BaseModel):
    """1 order = 1 header + N items. Container buat passing around."""
    header: OrderHeader
    items: list[OrderItem] = Field(min_length=1)


# ============================================================
# Product - master SKU
# ============================================================
class Product(BaseModel):
    sku: str = Field(min_length=1)
    product_name: str
    product_type: Literal["simple", "bundle"] = "simple"
    cogs: Optional[int] = None
    is_active: bool = True
    notes: Optional[str] = None
    
    @field_validator("sku", "product_name", mode="before")
    @classmethod
    def strip_strings(cls, v):
        if v is None:
            return None
        return str(v).strip()
    
    def to_db_dict(self) -> dict:
        return self.model_dump()
