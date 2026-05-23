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
# Asset paths
# ============================================================
ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_TRILOGY = ASSETS_DIR / "logo.png"
LOGO_DESTY = ASSETS_DIR / "desty.png"
LOGO_TOKOPEDIA = ASSETS_DIR / "tokopedia.png"
LOGO_ISELLER = ASSETS_DIR / "iseller.png"
LOGO_WEBSTORE = ASSETS_DIR / "webstore.png"

MARKETPLACE_LOGOS = {
    "desty": LOGO_DESTY,
    "tokopedia": LOGO_TOKOPEDIA,
    "iseller": LOGO_ISELLER,
}


# ============================================================
# Sidebar (ULTRA COMPACT)
# ============================================================
with st.sidebar:
    if LOGO_TRILOGY.exists():
        st.image(str(LOGO_TRILOGY), use_container_width=True)
    
    # Config check
    config_ok, config_msg = validate_config()
    if not config_ok:
        st.error(f"❌ Config error: {config_msg}")
        st.stop()
    
    if st.button("🌐 Test Database Connection", use_container_width=True):
        with st.spinner("Testing..."):
            ok, msg = test_connection()
            if ok:
                st.success(msg)
            else:
                st.error(msg)
    
    st.divider()
    
    # Database Schema (compact)
    st.markdown("**📊 Database Schema** (4 tables)")
    st.caption(
        "• `products` — master SKU  \n"
        "• `bundle_components` — bundle mapping  \n"
        "• `orders` — order header  \n"
        "• `order_items` — line items"
    )
    
    st.divider()
    
    # How it works (compact)
    st.markdown("**ℹ️ How it works**")
    st.caption(
        "1. Upload file  \n"
        "2. Auto-detect marketplace  \n"
        "3. Group by Order ID  \n"
        "4. Clean & validate  \n"
        "5. Push to Database"
    )
    
    st.divider()
    
    # Footer
    st.markdown(
        "<small>linkedin: <a href='https://www.linkedin.com/in/muhammad-erdin-ramadhan-5a9823253/' target='_blank'>Muhammad Erdin Ramadhan</a></small>",
        unsafe_allow_html=True)
    st.caption("© 2026 Powered By ER")


# ============================================================
# Modal dialog: Password gate (native popup)
# ============================================================
@st.dialog("🔒 Confirm Push to Database")
def password_dialog(bundles, total_items):
    """Modal popup: password gate + execute push kalau bener."""
    st.warning("This action will push data to the database.  \nPlease enter your password to continue.")
    
    password_input = st.text_input(
        "Password",
        type="password",
        key="modal_password_input",
        placeholder="Masukin password...",
    )
    
    col1, col2 = st.columns(2)
    
    with col1:
        confirm = st.button("✅ Confirm Push", type="primary", use_container_width=True)
    
    with col2:
        cancel = st.button("❌ Cancel", use_container_width=True)
    
    if cancel:
        st.rerun()
    
    # Auto-confirm kalau password udah ada (user tekan Enter) ATAU klik Confirm
    if password_input or confirm:
        # Skip kalau password masih kosong (user belum input)
        if not password_input:
            return
        
        if not APP_UPLOAD_PASSWORD:
            st.error("⚠️ APP_UPLOAD_PASSWORD belum di-set di Secrets.")
            return
        
        if password_input != APP_UPLOAD_PASSWORD:
            st.error("❌ Password salah. Coba lagi.")
            return
        with st.spinner(f"Pushing {len(bundles):,} orders..."):
            try:
                result = push_to_supabase(bundles)
                
                v = result.get("validation", {})
                if v.get("errors"):
                    st.warning(f"⚠️ {len(v['errors'])} validation errors")
                    with st.expander("Validation errors"):
                        for err in v["errors"][:10]:
                            st.write(f"- Order {err.get('order_id')}: {err.get('error')[:200]}")
                
                p = result.get("products", {})
                if p:
                    st.success(f"📦 Products: **{p.get('new', 0)} new** SKUs added")
                
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
                
                bundle_count = result.get("bundle_skus_count", 0)
                if bundle_count > 0:
                    st.info(f"🎁 {bundle_count} SKUs flagged as bundle")
                
            except Exception as e:
                st.error(f"❌ Push failed: {e}")
                st.code(traceback.format_exc())

# ============================================================
# Main: Upload section (always visible)
# ============================================================
header_col1, header_col2 = st.columns([3, 2])

with header_col1:
    st.markdown("### Upload CSV/XLSX from Marketplace")

with header_col2:
    uploaded_file = st.file_uploader(
        "Upload",
        type=["csv", "xlsx", "xls"],
        label_visibility="collapsed",
    )

st.divider()


# ============================================================
# State 1: Before upload — Marketplaces Supported
# ============================================================
if uploaded_file is None:
    mp_col1, mp_col2, mp_col3, mp_col4, mp_col5 = st.columns([3, 1, 1, 1, 1])
    
    with mp_col1:
        st.markdown("**Marketplaces Supported :**")
    
    with mp_col2:
        if LOGO_DESTY.exists():
            st.image(str(LOGO_DESTY), width=40)
    
    with mp_col3:
        if LOGO_TOKOPEDIA.exists():
            st.image(str(LOGO_TOKOPEDIA), width=40)
    
    with mp_col4:
        if LOGO_WEBSTORE.exists():
            st.image(str(LOGO_WEBSTORE), width=40)
    
    with mp_col5:
        if LOGO_ISELLER.exists():
            st.image(str(LOGO_ISELLER), width=40)
    
    st.stop()


# ============================================================
# State 2: After upload — process flow
# ============================================================
st.markdown("##### Detect Marketplace")

try:
    def sniff_separator(file_obj):
        file_obj.seek(0)
        first_line = file_obj.readline()
        if isinstance(first_line, bytes):
            first_line = first_line.decode("utf-8-sig", errors="ignore")
        n_comma = first_line.count(",")
        n_semi = first_line.count(";")
        return ";" if n_semi > n_comma else ","
    
    try:
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
    
    # Logo + nama marketplace inline
    detect_col1, detect_col2 = st.columns([1, 10])
    with detect_col1:
        marketplace_logo = MARKETPLACE_LOGOS.get(cleaner.marketplace)
        if marketplace_logo and marketplace_logo.exists():
            st.image(str(marketplace_logo), width=40)
    with detect_col2:
        st.markdown(f"**{cleaner.marketplace.title()}**")
    
except Exception as e:
    st.error(f"❌ Gagal baca file: {e}")
    st.code(traceback.format_exc())
    st.stop()


uploaded_file.seek(0)


# Load
st.markdown("##### Load File")
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
st.markdown("##### Clean Data")
with st.spinner("Cleaning..."):
    try:
        df_clean = cleaner.clean(df_raw)
        st.write(f"🧹 After cleaning: **{len(df_clean):,}** rows ({len(df_raw) - len(df_clean):,} dropped)")
    except Exception as e:
        st.error(f"❌ Clean error: {e}")
        st.code(traceback.format_exc())
        st.stop()


# Group by Order ID
st.markdown("##### Group by Order ID")
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


# Sample order preview
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
    
    st.markdown("##### Aggregated Stats")
    col1, col2, col3 = st.columns(3)
    col1.metric("💰 Total Order Value", f"Rp {total_value:,}")
    col2.metric("🚚 Net Shipping", f"Rp {total_shipping:,}")
    col3.metric("🎯 Marketplace", cleaner.marketplace.title())


# Push section
st.markdown("##### Push to Database")

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

# Push button — TRIGGER MODAL (popup)
if st.button("Push to Database", type="primary", disabled=dry_run):
    password_dialog(bundles, total_items)

if dry_run:
    st.info("☝️ Uncheck 'Dry run' biar bisa push beneran")