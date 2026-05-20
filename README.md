# 📦 Marketplace Data Cleaner v2

Aplikasi Streamlit buat upload CSV/XLSX dari marketplace (Tokopedia, Iseller, Desty), auto-clean, push ke Supabase.

**v2 Update**: Schema 3-tabel (`products` + `orders` + `order_items` + `bundle_components`). Order-level financials (shipping, discount, fees) dipisah dari item-level. **No more double counting.**

---

## 🆕 Apa yang Berubah dari v1?

| v1 | v2 |
|---|---|
| 1 tabel flat (`orders`) | 4 tabel: `products`, `orders`, `order_items`, `bundle_components` |
| Shipping di-duplicate per row | Shipping cuma di `orders` (header level) |
| Gak ada master SKU | Auto-populate `products` table |
| Gak handle bundle | Bundle mapping support |

**Untuk first-timer**: ikutin section "Setup Step-by-Step" di bawah. Gak usah pusing dengan v1.

---

## 🚀 Setup Step-by-Step

### Prerequisites
- Python 3.10+ (cek: `python3 --version`)
- Akun Supabase di [supabase.com](https://supabase.com)

### Step 1: Setup Supabase

**Kalo udah punya project Supabase** (yang sama untuk Meta Ads, dll):
- Skip ke Step 1.3

**Kalo belum punya project**:

#### 1.1 Bikin Project
1. Login Supabase
2. New Project → name: `your-project`
3. Database password: **generate & SAVE** ke notes
4. Region: **Singapore**
5. Plan: Free
6. Wait ~2 menit

#### 1.2 (Skip kalo udah ada project)

#### 1.3 Run Schema SQL
1. Sidebar → **SQL Editor** → New query
2. Copy isi `supabase_schema.sql`
3. Paste → klik **Run**
4. Verify: ada 4 table baru di Table Editor:
   - `products`
   - `bundle_components`
   - `orders`
   - `order_items`

#### 1.4 Ambil Credentials
- Settings → API
- Copy:
  - **Project URL** (`https://xxxxx.supabase.co`)
  - **service_role** key (yang panjang — JANGAN pake `anon`!)

### Step 2: Setup Project di Laptop (Mac)

```bash
# Buka terminal, pindah ke Documents
cd ~/Documents

# Extract zip ke sini (kalau belum)
# (atau drag & drop dari Finder)

# Masuk folder
cd marketplace_cleaner

# Bikin virtual environment
python3 -m venv venv

# Aktifin venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

Setelah aktif, prompt lo bakal ada `(venv)` di depan:
```
(venv) namalo@MacBook marketplace_cleaner %
```

### Step 3: Setup .env

```bash
cp .env.example .env
```

Edit `.env` (pake VS Code, TextEdit, atau `nano .env`):

```env
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJhbGc...service-role-key

# Generate random salt: 
#   openssl rand -hex 32
PII_HASH_SALT=ganti-dengan-string-random-min-32-char
```

### Step 4: Run!

```bash
streamlit run app.py
```

Browser kebuka di `http://localhost:8501`. ✅

---

## 🧪 Test Run

1. Sidebar → **Test Supabase Connection** → harus ✅
2. Upload file CSV/XLSX
3. App auto-detect marketplace
4. Cek preview & stats
5. Centang **Dry run** dulu kalo mau test cleaning aja
6. Uncheck dry run + **Push to Supabase**

---

## 📊 Cek Data di Supabase

Setelah push, query data:

```sql
-- Total orders per marketplace
SELECT marketplace, 
       COUNT(*) AS orders, 
       SUM(order_total) AS gmv
FROM orders
WHERE order_status IN ('paid', 'completed')
GROUP BY marketplace;

-- Top 10 SKU
SELECT oi.sku, 
       SUM(oi.quantity) AS units,
       SUM(oi.subtotal) AS revenue
FROM order_items oi
JOIN orders o ON o.id = oi.order_id
WHERE o.order_status IN ('paid', 'completed')
GROUP BY oi.sku
ORDER BY revenue DESC
LIMIT 10;

-- Daily revenue last 30 days (NO DOUBLE COUNTING - shipping fee bener)
SELECT DATE(order_date) AS date,
       COUNT(*) AS orders,
       SUM(items_subtotal) AS gross,
       SUM(shipping_fee - shipping_discount) AS net_shipping,
       SUM(order_total) AS net_revenue
FROM orders
WHERE order_date >= NOW() - INTERVAL '30 days'
  AND order_status IN ('paid', 'completed')
GROUP BY DATE(order_date)
ORDER BY date DESC;

-- Orders dengan multiple SKUs
SELECT o.marketplace, o.order_id, COUNT(oi.id) AS item_count
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
GROUP BY o.id
HAVING COUNT(oi.id) > 1
LIMIT 10;
```

Atau pake view yang udah disiapin:
```sql
SELECT * FROM v_orders_summary LIMIT 10;
SELECT * FROM v_sku_sales_exploded LIMIT 10;
```

---

## 🎁 Setup Bundle SKU (Manual)

Setelah upload pertama, semua SKU otomatis masuk `products` as `'simple'`. Untuk flag SKU sebagai bundle:

### Step 1: Edit `products` table
1. Supabase Dashboard → Table Editor → `products`
2. Cari SKU yang bundle (misal `BUNDLE-MIX`)
3. Ganti `product_type` dari `simple` jadi `bundle`
4. Save

### Step 2: Tambah component mapping ke `bundle_components`
1. Table Editor → `bundle_components` → Insert row
2. Isi:
   - `bundle_sku`: BUNDLE-MIX
   - `component_sku`: SKU-A (component pertama)
   - `quantity`: 1
3. Insert lagi untuk component lain (SKU-B, SKU-C, dll)

### Step 3: Re-import (optional)
Kalo lo re-import file CSV yang sama, `is_bundle` flag di `order_items` bakal otomatis ke-set TRUE untuk SKU yang udah lo tandain bundle.

---

## 🐛 Troubleshooting

| Error | Solusi |
|---|---|
| `ModuleNotFoundError: streamlit` | venv belum aktif. `source venv/bin/activate` |
| `SUPABASE_URL not set` | `.env` belum dibikin / salah path |
| `Connection failed: Invalid API key` | Pake `service_role` key, bukan `anon` |
| `relation "orders" does not exist` | Schema SQL belum di-run di Supabase |
| `Marketplace not detected` | Cek expander "Debug columns" — kolom file beda |
| `duplicate key violation` di order_items | Logic udah handle (delete dulu sebelum re-insert), report kalo masih kejadian |

---

## 📁 Struktur File

```
marketplace_cleaner/
├── app.py                       # Streamlit UI
├── config.py                    # env vars + PII hash
├── schemas.py                   # Pydantic (OrderHeader, OrderItem, Product)
├── loader.py                    # 2-step insert: orders → order_items
├── cleaners/
│   ├── __init__.py              # auto-detect
│   ├── base.py                  # base + utility (parse date, parse harga)
│   ├── tokopedia.py             # Tokopedia → OrderBundle
│   ├── iseller.py               # Iseller → OrderBundle
│   └── desty.py                 # Desty (XLSX) → OrderBundle
├── supabase_schema.sql          # DDL 4 tabel
├── requirements.txt
├── .env.example
└── README.md
```

---

## ⚠️ Penting!

1. **Pake `service_role`** key, bukan `anon`
2. **`PII_HASH_SALT` di `.env` jangan diubah** — kalau diubah, hash lama gak match
3. **Don't commit `.env`** ke Git
4. **Bundle harus di-flag manual** via Supabase Table Editor (auto-detect dari nama gak reliable)
5. **Idempotent**: re-upload file sama gak akan duplicate (orders di-upsert, items di-replace)

---

## 🚀 Roadmap (After MVP)

- Deploy ke Streamlit Cloud (multi-user)
- Add auth
- Tambah marketplace baru (Shopee, Lazada, TikTok Shop)
- Bikin dashboard di Metabase/Looker yang nyambung ke Supabase
- Schedule auto-import dari API
- Cohort analysis, churn, RFM
