-- ============================================================
-- Marketplace Orders - Supabase Schema v2 (3-table normalized)
-- ============================================================
-- Jalanin SQL ini di Supabase SQL Editor
-- Caranya: Supabase Dashboard > SQL Editor > New Query > paste > Run
-- ============================================================

-- ⚠️ KALAU LO UDAH PUNYA TABLE 'orders' DARI SCHEMA LAMA, JALANIN INI DULU:
-- DROP TABLE IF EXISTS orders CASCADE;
-- (CASCADE bakal hapus semua dependency, hati-hati kalo udah ada data!)


-- ============================================================
-- 1. PRODUCTS - Master SKU
-- ============================================================
-- Auto-populated dari order data (SKU baru = insert as 'simple')
-- Lo edit manual jadi 'bundle' lewat Supabase Table Editor
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    product_type TEXT NOT NULL DEFAULT 'simple',  -- 'simple' | 'bundle'
    
    -- Optional: harga modal per unit (buat profit margin nanti)
    cogs BIGINT,
    
    -- Status & tracking
    is_active BOOLEAN DEFAULT TRUE,
    notes TEXT,                                    -- catatan manual
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    CHECK (product_type IN ('simple', 'bundle'))
);

CREATE INDEX IF NOT EXISTS idx_products_type ON products (product_type);
CREATE INDEX IF NOT EXISTS idx_products_active ON products (is_active);


-- ============================================================
-- 2. BUNDLE_COMPONENTS - Mapping bundle → component SKUs
-- ============================================================
-- Manual input via Supabase Table Editor
-- Contoh: BUNDLE-MIX terdiri dari 1x SKU-A + 2x SKU-B
CREATE TABLE IF NOT EXISTS bundle_components (
    id BIGSERIAL PRIMARY KEY,
    bundle_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE CASCADE,
    component_sku TEXT NOT NULL REFERENCES products(sku) ON DELETE RESTRICT,
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE (bundle_sku, component_sku),
    CHECK (bundle_sku <> component_sku)
);

CREATE INDEX IF NOT EXISTS idx_bundle_components_bundle ON bundle_components (bundle_sku);
CREATE INDEX IF NOT EXISTS idx_bundle_components_component ON bundle_components (component_sku);


-- ============================================================
-- 3. ORDERS - Order header (ORDER-LEVEL data)
-- ============================================================
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    
    -- Identity
    marketplace TEXT NOT NULL,
    order_id TEXT NOT NULL,
    
    -- Status
    order_status TEXT,
    raw_status TEXT,
    
    -- Dates
    order_date TIMESTAMPTZ NOT NULL,
    paid_at TIMESTAMPTZ,
    
    -- ORDER-LEVEL FINANCIALS (semua dalam IDR, integer)
    items_subtotal BIGINT DEFAULT 0,
    items_discount BIGINT DEFAULT 0,
    shipping_fee BIGINT DEFAULT 0,
    shipping_discount BIGINT DEFAULT 0,
    order_discount BIGINT DEFAULT 0,
    payment_fee BIGINT DEFAULT 0,
    insurance_fee BIGINT DEFAULT 0,
    order_total BIGINT,
    
    -- Logistics
    shipping_provider TEXT,
    shipping_city TEXT,
    shipping_province TEXT,
    
    -- Customer (HASHED)
    customer_name_hash TEXT,
    customer_phone_hash TEXT,
    
    -- Audit
    raw_data JSONB,
    source_file TEXT,
    imported_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE (marketplace, order_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_marketplace_date ON orders (marketplace, order_date DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (order_status);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders (order_date DESC);


-- ============================================================
-- 4. ORDER_ITEMS - Line items (PER SKU PER ORDER)
-- ============================================================
CREATE TABLE IF NOT EXISTS order_items (
    id BIGSERIAL PRIMARY KEY,
    
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    
    sku TEXT,
    product_name TEXT,
    variation TEXT,
    quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
    
    unit_price BIGINT DEFAULT 0,
    item_discount BIGINT DEFAULT 0,
    subtotal BIGINT DEFAULT 0,
    
    is_bundle BOOLEAN DEFAULT FALSE,
    
    raw_data JSONB,
    
    UNIQUE (order_id, sku)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items (sku);
CREATE INDEX IF NOT EXISTS idx_order_items_bundle ON order_items (is_bundle) WHERE is_bundle = TRUE;


-- ============================================================
-- COMMENTS
-- ============================================================
COMMENT ON TABLE products IS 'Master SKU data. Auto-populated dari orders.';
COMMENT ON TABLE bundle_components IS 'Mapping bundle SKU ke component SKUs.';
COMMENT ON TABLE orders IS 'Order header (1 row per order).';
COMMENT ON TABLE order_items IS 'Line items per SKU per order.';


-- ============================================================
-- HELPFUL VIEWS
-- ============================================================

-- View 1: Order summary
CREATE OR REPLACE VIEW v_orders_summary AS
SELECT 
    o.id,
    o.marketplace,
    o.order_id,
    o.order_status,
    o.order_date,
    o.items_subtotal,
    o.shipping_fee - o.shipping_discount AS net_shipping,
    o.order_total,
    COUNT(oi.id) AS item_count,
    SUM(oi.quantity) AS total_qty,
    BOOL_OR(oi.is_bundle) AS has_bundle
FROM orders o
LEFT JOIN order_items oi ON oi.order_id = o.id
GROUP BY o.id;

-- View 2: SKU sales dengan bundle exploded
CREATE OR REPLACE VIEW v_sku_sales_exploded AS
SELECT 
    o.id AS order_pk,
    o.marketplace,
    o.order_date,
    o.order_status,
    COALESCE(bc.component_sku, oi.sku) AS effective_sku,
    oi.quantity * COALESCE(bc.quantity, 1) AS effective_quantity,
    oi.sku AS sold_as_sku,
    oi.is_bundle
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
LEFT JOIN bundle_components bc ON oi.is_bundle = TRUE AND bc.bundle_sku = oi.sku;


-- ============================================================
-- VERIFY: cek tabel udah kebuat
-- ============================================================
-- SELECT table_name FROM information_schema.tables 
-- WHERE table_schema = 'public' 
--   AND table_name IN ('products', 'bundle_components', 'orders', 'order_items')
-- ORDER BY table_name;
