"""
app.py
------
Streamlit app v2: upload CSV/XLSX → clean → push ke 3-table schema (orders + order_items + products).
"""

import streamlit as st
import pandas as pd
import traceback
from pathlib import Path

from cleaners import detect_marketplace, ALL_CLEANERS
from loader import push_to_supabase, test_connection
from config import validate_config, APP_UPLOAD_PASSWORD


# ============================================================
# Page config
# ============================================================
st.set_page_config(
    page_title="Marketplace Data Cleaner",
    page_icon="📦",
    layout="wide",
)

# ============================================================
# Header: Logo (kiri atas, kecil) + Title (di bawahnya)
# ============================================================
logo_path = Path(__file__).parent / "assets" / "logo.png"
if logo_path.exists():
    # Logo di kiri atas, ukuran kecil (~120px)
    # Pake column biar logo gak full-width
    logo_col, _ = st.columns([1, 5])
    with logo_col:
        st.image(str(logo_path), use_container_width=True)

st.title("📦 Marketplace Data Cleaner v2")
st.caption("Upload CSV/XLSX dari Tokopedia, Iseller, atau Desty → auto-clean → push ke Supabase (orders + order_items)")


# ============================================================
# Sidebar
# ============================================================
with st.sidebar:
    st.header("⚙️ Status")
    
    config_ok, config_msg = validate_config()
    if config_ok:
        st.success("✅ Config loaded")
    else:
        st.error(f"❌ Config error: {config_msg}")
        st.info("Edit `.env`, terus restart app")
        st.stop()
    
    if st.button("🔌 Test Supabase Connection"):
        with st.spinner("Testing..."):
            ok, msg = test_connection()
            if ok:
                st.success(msg)
            else:
                st.error(msg)
    
    st.divider()
    
    st.header("📋 Marketplaces Supported")
    for cleaner in ALL_CLEANERS:
        st.write(f"• **{cleaner.marketplace.title()}**")
    
    st.divider()
    
    st.header("📊 Database Schema")
    st.markdown("""
    **4 tables**:
    - `products` — master SKU
    - `bundle_components` — bundle mapping
    - `orders` — order header
    - `order_items` — line items
    """)
    
    st.divider()
    
    st.header("ℹ️ How it works")
    st.markdown("""
    1. Upload file
    2. Auto-detect marketplace
    3. Group by Order ID
    4. Clean & validate
    5. Push:
       - SKU baru → auto-insert ke `products`
       - Order header → `orders`
       - Line items → `order_items`
    
    **Bundle SKUs**: edit manual di Supabase Table Editor (set `product_type='bundle'`).
    """)
    
    # ========================================================
    # Footer: Copyright
    # ========================================================
    st.divider()
    st.caption("© 2026 Powered By Data Analyst -ER")


# ============================================================
# Main flow
# ============================================================

st.header("1️⃣ Upload File")
uploaded_file = st.file_uploader(
    "Drop CSV/XLSX di sini",
    type=["csv", "xlsx", "xls"],
)

if uploaded_file is None:
    st.info("👆 Upload file dulu")
    st.stop()


# Detect marketplace
st.header("2️⃣ Detect Marketplace")

try:
    # Auto-detect separator dulu (comma vs semicolon)
    def sniff_separator(file_obj):
        """Detect CSV separator from first line."""
        file_obj.seek(0)
        first_line = file_obj.readline()
        if isinstance(first_line, bytes):
            first_line = first_line.decode("utf-8-sig", errors="ignore")
        n_comma = first_line.count(",")
        n_semi = first_line.count(";")
        return ";" if n_semi > n_comma else ","
    
    try:
        # Try CSV with auto-detected separator
        sep = sniff_separator(uploaded_file)
        uploaded_file.seek(0)
        preview_df = pd.read_csv(uploaded_file, nrows=5, encoding="utf-8-sig", sep=sep)
    except Exception:
        uploaded_file.seek(0)
        preview_df = pd.read_excel(uploaded_file, nrows=5)
    
    columns = preview_df.columns.tolist()
    cleaner = detect_marketplace(columns)
    
    if cleaner is None:
        st.error("❌ Marketplace tidak terdeteksi")
        with st.expander("Debug columns"):
            st.code("\n".join(columns))
        st.stop()
    
    st.success(f"✅ Detected: **{cleaner.marketplace.upper()}**")
    
except Exception as e:
    st.error(f"❌ Gagal baca file: {e}")
    st.code(traceback.format_exc())
    st.stop()


uploaded_file.seek(0)


# Load
st.header("3️⃣ Load File")
with st.spinner("Loading..."):
    try:
        df_raw = cleaner.load(uploaded_file)
        st.write(f"📊 Loaded: **{len(df_raw):,}** rows × **{len(df_raw.columns)}** cols")
    except Exception as e:
        st.error(f"❌ Load error: {e}")
        st.code(traceback.format_exc())
        st.stop()

with st.expander("🔍 Raw preview"):
    st.dataframe(df_raw.head(10), use_container_width=True)


# Clean
st.header("4️⃣ Clean Data")
with st.spinner("Cleaning..."):
    try:
        df_clean = cleaner.clean(df_raw)
        st.write(f"🧹 After cleaning: **{len(df_clean):,}** rows ({len(df_raw) - len(df_clean):,} dropped)")
    except Exception as e:
        st.error(f"❌ Clean error: {e}")
        st.code(traceback.format_exc())
        st.stop()


# Transform to bundles (group by order)
st.header("5️⃣ Group by Order ID")
with st.spinner("Grouping..."):
    try:
        bundles = cleaner.to_order_bundles(df_clean, source_file=uploaded_file.name)
        total_items = sum(len(b.items) for b in bundles)
        
        col1, col2, col3 = st.columns(3)
        col1.metric("📦 Total Orders", f"{len(bundles):,}")
        col2.metric("🛒 Total Line Items", f"{total_items:,}")
        col3.metric("📊 Avg Items/Order", f"{total_items / max(len(bundles), 1):.2f}")
    except Exception as e:
        st.error(f"❌ Transform error: {e}")
        st.code(traceback.format_exc())
        st.stop()


# Preview
if bundles:
    with st.expander("✨ Sample order (first 1)"):
        sample = bundles[0]
        st.write("**Header:**")
        header_dict = sample.header.to_db_dict()
        st.json({k: v for k, v in header_dict.items() if k not in ["raw_data"]})
        
        st.write("**Items:**")
        items_df = pd.DataFrame([
            {
                "sku": i.sku,
                "product": (i.product_name or "")[:50],
                "qty": i.quantity,
                "unit_price": i.unit_price,
                "disc_seller": i.item_seller_discount,
                "disc_platform": i.item_platform_discount,
                "subtotal": i.subtotal,
            }
            for i in sample.items
        ])
        st.dataframe(items_df, use_container_width=True)


# Stats
if bundles:
    total_value = sum(b.header.order_total or 0 for b in bundles)
    total_shipping = sum(
        (b.header.shipping_fee or 0) 
        - (b.header.shipping_platform_discount or 0) 
        - (b.header.shipping_seller_discount or 0) 
        for b in bundles
    )
    
    st.subheader("📊 Aggregated Stats")
    col1, col2, col3 = st.columns(3)
    col1.metric("💰 Total Order Value", f"Rp {total_value:,}")
    col2.metric("🚚 Net Shipping", f"Rp {total_shipping:,}")
    col3.metric("🎯 Marketplace", cleaner.marketplace.title())


# Push
st.header("6️⃣ Push to Supabase")

if not bundles:
    st.warning("⚠️ Gak ada order valid")
    st.stop()

st.info(
    f"📤 Akan di-push:\n"
    f"- **{len(bundles):,}** order headers ke table `orders`\n"
    f"- **~{total_items:,}** line items ke table `order_items`\n"
    f"- SKU baru (kalau ada) auto-insert ke `products` as 'simple'"
)

dry_run = st.checkbox(
    "🧪 Dry run (skip insert)",
    help="Validate aja tanpa write ke DB",
)

# Tombol Push (trigger password prompt)
if st.button("🚀 Push to Supabase", type="primary", disabled=dry_run):
    st.session_state["show_password_prompt"] = True

# Password gate prompt
if st.session_state.get("show_password_prompt", False) and not dry_run:
    st.warning("🔒 **Confirm: Push ke Supabase butuh password**")
    
    password_input = st.text_input(
        "Password",
        type="password",
        key="push_password_input",
        placeholder="Masukin password buat lanjut...",
    )
    
    col1, col2, _ = st.columns([1, 1, 4])
    with col1:
        confirm = st.button("✅ Confirm", type="primary")
    with col2:
        cancel = st.button("❌ Cancel")
    
    if cancel:
        st.session_state["show_password_prompt"] = False
        st.rerun()
    
    if confirm:
        if not APP_UPLOAD_PASSWORD:
            st.error("⚠️ APP_UPLOAD_PASSWORD belum di-set di Secrets. Hubungi admin.")
        elif password_input != APP_UPLOAD_PASSWORD:
            st.error("❌ Password salah. Coba lagi.")
        else:
            # Password bener, lanjut push
            st.session_state["show_password_prompt"] = False
            with st.spinner(f"Pushing {len(bundles):,} orders..."):
                try:
                    result = push_to_supabase(bundles)
                    
                    # Show validation result
                    v = result.get("validation", {})
                    if v.get("errors"):
                        st.warning(f"⚠️ {len(v['errors'])} validation errors")
                        with st.expander("Validation errors"):
                            for err in v["errors"][:10]:
                                st.write(f"- Order {err.get('order_id')}: {err.get('error')[:200]}")
                    
                    # Products
                    p = result.get("products", {})
                    if p:
                        st.success(f"📦 Products: **{p.get('new', 0)} new** SKUs added")
                    
                    # Upsert
                    u = result.get("upsert", {})
                    if u:
                        st.success(
                            f"✅ Inserted: **{u['orders_upserted']:,}** orders + "
                            f"**{u['items_inserted']:,}** line items"
                        )
                        
                        if u.get("errors"):
                            st.error(f"⚠️ {len(u['errors'])} errors during upsert")
                            with st.expander("Upsert errors"):
                                for err in u["errors"][:10]:
                                    st.write(f"- {err}")
                        else:
                            st.balloons()
                    
                    bundle_count = result.get("bundle_skus_count", 0)
                    if bundle_count > 0:
                        st.info(f"🎁 {bundle_count} SKUs flagged as bundle (from products table)")
                    else:
                        st.info(
                            "💡 Tip: Untuk flag SKU sebagai bundle, edit di Supabase Table Editor → "
                            "table `products` → ubah `product_type` ke `bundle`. "
                            "Lalu mapping component-nya di table `bundle_components`."
                        )
                        
                except Exception as e:
                    st.error(f"❌ Push failed: {e}")
                    st.code(traceback.format_exc())

if dry_run:
    st.info("☝️ Uncheck 'Dry run' biar bisa push beneran")
