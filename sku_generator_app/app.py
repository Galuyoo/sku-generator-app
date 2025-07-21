import streamlit as st
import pandas as pd
import re
from itertools import product
import os

st.set_page_config(page_title="SKU Generator", layout="centered")

# --- Load previously used SKU suffixes ---
SKU_TRACKER_FILE = 'used_sku_suffixes.csv'
if os.path.exists(SKU_TRACKER_FILE):
    used_suffixes = pd.read_csv(SKU_TRACKER_FILE)['sku_suffix'].tolist()
else:
    used_suffixes = []

# --- Shopify Defaults ---
vendor = "Spoofy"
published = True
inventory_policy = "continue"
fulfillment_service = "manual"
requires_shipping = True
taxable = True
inventory_tracker = "shopify"

# --- UI ---
st.title("🧵 SKU Generator for Shopify")

with st.form("sku_form"):
    product_name = st.text_input("Enter product name")
    sku_suffix = st.text_input("Enter unique SKU suffix (e.g., IVARLULE)").strip().upper()
    main_color = st.text_input("Enter main color (e.g., Black)").strip()
    tags = st.text_input("Enter comma-separated tags").strip()
    submit = st.form_submit_button("Generate Excel")

# --- Maps and Configurations (trimmed for brevity here, insert full dicts below) ---
# Paste your original body_html_map, product_types, and correct_colors_by_type here
# I've shortened them below — replace them with your full versions
body_html_map = {
    "Oversized T Shirts": "XS - 44\" Chest<br>Small - 46\" Chest<br>Medium - 48\" Chest..."
    # ... add rest
}

product_types = {
    "kids t shirt": {"sizes": ["3-4 Years", "5-6 Years"], "price": 14.99},
    # ... add rest
}

correct_colors_by_type = {
    "kids t shirt": ["White", "Kelly", "Navy"],
    # ... add rest
}

# --- Process form submission ---
if submit:
    if sku_suffix in used_suffixes:
        st.error("❌ That SKU suffix is already used. Please enter a new one.")
    elif not all([product_name, sku_suffix, main_color, tags]):
        st.warning("⚠️ Please complete all fields.")
    else:
        rows = []
        for garment_type, config in product_types.items():
            sizes = config["sizes"]
            colors = correct_colors_by_type[garment_type]
            if main_color in colors:
                colors = [main_color] + [c for c in colors if c != main_color]
            for size, color in product(sizes, colors):
                if garment_type == "Oversized T Shirts" and color == "Pink":
                    continue
                price = config.get("price_by_size", {}).get(size, config.get("price"))
                title = f"{product_name} {garment_type}"
                handle = re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")
                sku = f"{'BY102' if garment_type == 'Oversized T Shirts' else 'SKU'}-{size}-{color.replace(' ', '')}-{sku_suffix}"
                rows.append({
                    "Handle": handle,
                    "Title": title,
                    "Body (HTML)": body_html_map.get(garment_type, ""),
                    "Vendor": vendor,
                    "Type": garment_type,
                    "Tags": tags,
                    "Published": published,
                    "Option1 Name": "Size",
                    "Option1 Value": size,
                    "Option2 Name": "Color",
                    "Option2 Value": color,
                    "Variant SKU": sku,
                    "Variant Grams": 300,
                    "Variant Inventory Tracker": inventory_tracker,
                    "Variant Inventory Qty": 25,
                    "Variant Inventory Policy": inventory_policy,
                    "Variant Fulfillment Service": fulfillment_service,
                    "Variant Price": price,
                    "Variant Requires Shipping": requires_shipping,
                    "Variant Taxable": taxable
                })

        df = pd.DataFrame(rows)
        filename = f"{handle}.xlsx"
        df.to_excel(filename, index=False)

        # Save suffix
        pd.DataFrame([{"sku_suffix": sku_suffix}]).to_csv(
            SKU_TRACKER_FILE,
            mode='a',
            header=not os.path.exists(SKU_TRACKER_FILE),
            index=False
        )

        with open(filename, "rb") as f:
            st.download_button("📥 Download Excel File", f, file_name=filename)

        st.success("✅ Excel generated! Ready to download.")

