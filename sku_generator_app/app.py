import streamlit as st
import pandas as pd
import re
from itertools import product
import os
from PIL import Image
import base64
from datetime import datetime

def logo_to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

try:
    logo = Image.open("logo.png")
    st.markdown(
        f"""
        <div style="text-align: center;">
            <img src="data:image/png;base64,{logo_to_base64('logo.png')}" width="200"/>
        </div>
        """,
        unsafe_allow_html=True
    )
except Exception as e:
    st.warning("⚠️ Logo couldn't be loaded.")

st.set_page_config(page_title="SKU Generator", layout="centered")

# --- Load previously used SKU suffixes ---
SKU_TRACKER_FILE = 'used_sku_suffixes.csv'
if os.path.exists(SKU_TRACKER_FILE):
    used_suffixes_df = pd.read_csv(SKU_TRACKER_FILE)
    used_suffixes = used_suffixes_df['sku_suffix'].tolist()
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
    lister = st.selectbox("Who is listing this?", ["Sal", "Hannan"])
    st.markdown("**Enter 10 product descriptions, separated by `|`**")
    raw_descriptions = st.text_area("Descriptions", height=300).strip()
    submit = st.form_submit_button("Generate Excel")

# --- Garment Order ---
garment_keys = [
    "T-Shirt", "Adult Hoodie", "Adult Sweatshirt", "Ladies Shirt",
    "Tank-Top", "Longsleeve T-Shirt", "Oversized T Shirts",
    "kids t shirt", "kids hoodie", "kids sweatshirt"
]

# --- Size Guides ---
body_html_map = {
    "Oversized T Shirts": "XS - 44\" Chest<br>Small - 46\" Chest<br>Medium - 48\" Chest<br>Large - 50\" Chest<br>XL - 53\" Chest<br>2XL - 55\" Chest<br>3XL - 59\" Chest<br>4XL - 62\" Chest<br>5XL - 66\" Chest",
    "T-Shirt": "Small – 34/36\"<br>Medium – 38/40\"<br>Large – 42/44\"<br>XL – 46/48\"<br>2XL – 50/52\"<br>3XL – 54/56\"<br>4XL – 58/60\"",
    "Adult Sweatshirt": "Small – 36\" Chest<br>Medium – 40\" Chest<br>Large – 44\" Chest<br>XL – 48\" Chest<br>2XL – 52\" Chest<br>3XL – 56\" Chest",
    "Adult Hoodie": "Small – 36\" Chest<br>Medium – 40\" Chest<br>Large – 44\" Chest<br>XL – 48\" Chest<br>2XL – 52\" Chest<br>3XL – 56\" Chest",
    "Ladies Shirt": "Small – UK 6/8<br>Medium – UK 8/10<br>Large – UK 12<br>XL – UK 14<br>2XL – UK 16",
    "Longsleeve T-Shirt": "Small – 34/36\"<br>Medium – 38/40\"<br>Large – 42/44\"<br>XL – 46/48\"<br>2XL – 50/52\"",
    "Tank-Top": "Small – 34/36\"<br>Medium – 38/40\"<br>Large – 42/44\"<br>XL – 46/48\"<br>2XL – 50/52\"",
    "kids t shirt": "3–4 Y (104 cm)<br>5–6 Y (116 cm)<br>7–8 Y (128 cm)<br>9–11 Y (140 cm)<br>12–13 Y (152 cm)",
    "kids sweatshirt": "3–4 Y (104 cm)<br>5–6 Y (116 cm)<br>7–8 Y (128 cm)<br>9–11 Y (140 cm)<br>12–13 Y (152 cm)",
    "kids hoodie": "3–4 Y (104 cm)<br>5–6 Y (116 cm)<br>7–8 Y (128 cm)<br>9–11 Y (140 cm)<br>12–13 Y (152 cm)"
}

# --- Product Configuration ---
product_types = {
    "T-Shirt": {"sizes": ["Small", "Medium", "Large", "XL", "2XL", "3XL"], "price": 12.99},
    "Adult Hoodie": {
        "sizes": ["Small", "Medium", "Large", "XL", "2XL", "3XL"],
        "price_by_size": {"Small": 28.99, "Medium": 28.99, "Large": 28.99, "XL": 28.99, "2XL": 30.99, "3XL": 30.99}
    },
    "Adult Sweatshirt": {
        "sizes": ["Small", "Medium", "Large", "XL", "2XL", "3XL"],
        "price_by_size": {"Small": 23.99, "Medium": 23.99, "Large": 23.99, "XL": 23.99, "2XL": 24.99, "3XL": 24.99}
    },
    "Ladies Shirt": {"sizes": ["Small", "Medium", "Large", "XL", "2XL"], "price": 16.99},
    "Tank-Top": {"sizes": ["Small", "Medium", "Large", "XL", "2XL"], "price": 15.99},
    "Longsleeve T-Shirt": {"sizes": ["Small", "Medium", "Large", "XL", "2XL"], "price": 16.99},
    "Oversized T Shirts": {
        "sizes": ["XS", "Small", "Medium", "Large", "XL", "2XL", "3XL", "4XL", "5XL"],
        "price_by_size": {
            "XS": 24.99, "Small": 24.99, "Medium": 24.99, "Large": 24.99, "XL": 24.99,
            "2XL": 24.99, "3XL": 25.99, "4XL": 25.99, "5XL": 25.99
        }
    },
    "kids t shirt": {"sizes": ["3-4 Years", "5-6 Years", "7-8 Years", "9-11 Years", "12-13 Years"], "price": 14.99},
    "kids hoodie": {"sizes": ["3-4 Years", "5-6 Years", "7-8 Years", "9-11 Years", "12-13 Years"], "price": 25.99},
    "kids sweatshirt": {"sizes": ["3-4 Years", "5-6 Years", "7-8 Years", "9-11 Years", "12-13 Years"], "price": 22.99}
}

correct_colors_by_type = {
    "T-Shirt": ["Black", "White"],
    "Adult Hoodie": ["Black", "Grey", "White"],
    "Adult Sweatshirt": ["Black", "Grey"],
    "Ladies Shirt": ["Black", "Pink", "White"],
    "Tank-Top": ["Black", "White"],
    "Longsleeve T-Shirt": ["Black", "White"],
    "Oversized T Shirts": ["Black", "White", "Pink"],
    "kids t shirt": ["White", "Kelly", "Navy"],
    "kids hoodie": ["White", "Grey"],
    "kids sweatshirt": ["White", "Grey"]
}

# --- Process form submission ---
if submit:
    desc_list = [d.strip() for d in raw_descriptions.split('|') if d.strip()]
    if len(desc_list) != len(garment_keys):
        st.error(f"❌ You provided {len(desc_list)} descriptions but {len(garment_keys)} are required.")
    elif sku_suffix in used_suffixes:
        st.error("❌ That SKU suffix is already used. Please enter a new one.")
    elif not all([product_name, sku_suffix, main_color, tags, lister]):
        st.warning("⚠️ Please complete all fields.")
    else:
        descriptions = {
            garment: f"{desc}<br><br>{body_html_map[garment]}"
            for garment, desc in zip(garment_keys, desc_list)
        }

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
                    "Body (HTML)": descriptions[garment_type],
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

        # Save suffix + lister + timestamp
        log_df = pd.DataFrame([{ 
            "sku_suffix": sku_suffix, 
            "lister": lister, 
            "timestamp": datetime.now().isoformat()
        }])
        log_df.to_csv(
            SKU_TRACKER_FILE,
            mode='a',
            header=not os.path.exists(SKU_TRACKER_FILE),
            index=False
        )

        with open(filename, "rb") as f:
            st.download_button("📥 Download Excel File", f, file_name=filename)

        st.success("✅ Excel generated! Ready to download.")

        # Optional preview
        st.subheader("Preview")
        st.dataframe(df.head())
