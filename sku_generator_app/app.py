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
    used_suffixes_df = pd.read_csv(SKU_TRACKER_FILE, on_bad_lines='skip')
    if 'sku_suffix' in used_suffixes_df.columns:
        used_suffixes = used_suffixes_df['sku_suffix'].dropna().tolist()

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
    "Adult T Shirt", "Adult Hoodie", "Adult Sweatshirt", "Ladies Shirt",
    "Tank-Top", "Longsleeve T-Shirt", "Oversized T Shirts",
    "kids t shirt", "kids hoodie", "kids sweatshirt"
]

# --- Size Guides ---
body_html_map = {
    "Adult T Shirt": "Small - 34/36\" Inch Chest<br>Medium - 38/40\" Inch Chest<br>Large - 42/44\" Inch Chest<br>XL - 46/48\" Inch Chest<br>2XL - 50/52\" Inch Chest<br>3XL - 54/56\" Inch Chest<br>4XL - 58/60\" Inch Chest",
    "Oversized T Shirts": "XS - 44\" Chest<br>Small - 46\" Chest<br>Medium - 48\" Chest<br>Large - 50\" Chest<br>XL - 53\" Chest<br>2XL - 55\" Chest<br>3XL - 59\" Chest<br>4XL - 62\" Chest<br>5XL - 66\" Chest",
    "Adult Sweatshirt": "Small - 36\" Inch Chest<br>Medium - 40\" Inch Chest<br>Large - 44\" Inch Chest<br>XL - 48\" Inch Chest<br>2XL - 52\" Inch Chest<br>3XL - 56\" Inch Chest",
    "Adult Hoodie": "Small - 36\" Inch Chest<br>Medium - 40\" Inch Chest<br>Large - 44\" Inch Chest<br>XL - 48\" Inch Chest<br>2XL - 52\" Inch Chest<br>3XL - 56\" Inch Chest",
    "Ladies Shirt": "Small - UK 6/8<br>Medium - UK 8/10<br>Large - UK 12<br>XL - UK 14<br>2XL - UK 16",
    "Longsleeve T-Shirt": "Small - 34/36\" Inch Chest<br>Medium - 38/40\" Inch Chest<br>Large - 42/44\" Inch Chest<br>XL - 46/48\" Inch Chest<br>2XL - 50/52\" Inch Chest",
    "Tank-Top": "Small - 34/36\" Inch Chest<br>Medium - 38/40\" Inch Chest<br>Large - 42/44\" Inch Chest<br>XL - 46/48\" Inch Chest<br>2XL - 50/52\" Inch Chest",
    "kids t shirt": "3-4 Years - 26\" Inch Chest<br>5-6 Years - 28\" Inch Chest<br>7-8 Years - 30\" Inch Chest<br>9-11 Years - 32\" Inch Chest<br>12-13 Years - 34\" Inch Chest",
    "kids hoodie": "3-4 Years - 26\" Inch Chest<br>5-6 Years - 28\" Inch Chest<br>7-8 Years - 30\" Inch Chest<br>9-11 Years - 32\" Inch Chest<br>12-13 Years - 34\" Inch Chest",
    "kids sweatshirt": "3-4 Years - 26\" Inch Chest<br>5-6 Years - 28\" Inch Chest<br>7-8 Years - 30\" Inch Chest<br>9-11 Years - 32\" Inch Chest<br>12-13 Years - 34\" Inch Chest"
}

# --- Product Extras ---
# These are additional details that can be appended to the product descriptions
product_extras = {
    "Adult T Shirt": "<strong>Features<br></strong>Reactive Dyed<br>100% Cotton<br>Crew Neck<br>Taped Shoulder to Shoulder<br>Twin Needle Stitching on Neck &amp; Shoulders<br>Enzyme Washed<br><br><strong>T-Shirt Chest ½ Measurement <br></strong><em>1 Inch Below Armhole</em><br>XS - 18 Inches<br>Small - 19.5 Inches<br>Medium - 21 Inches<br>Large - 22.5 Inches<br>XL - 24 Inches<br>2XL - 25.5 Inches<br>3XL - 27 Inches<br>4XL - 28.5 Inches<br>5XL - 30 Inches<br>6XL - 31.5 Inches<br><br><strong>T-Shirt Length<br></strong><em>Neck Point on Shoulder to Hem</em><strong><br></strong>XS - 26 Inches<br>Small - 27 Inches<br>Medium - 28 Inches<br>Large - 29 Inches<br>XL - 30 Inches<br>2XL - 31 Inches<br>3XL - 32 Inches<br>4XL - 33 Inches<br>5XL - 34 Inches<br>6</span><span>XL - 35 Inches</span><span><br><br>*Garment Measurement are subject to tolerances. They are not be taken as absolute measurement values. When a garment is measured, the values may vary and can be above, below or the same as the values stated.</span></p>",
    "Adult Hoodie": "<strong><br>Specification<br></strong>Twin-needle stitching detailing<br>Double fabric hood with self coloured cords<br>Kangaroo pouch pocket<br>Ribbed cuffs and hem<br>Worldwide Responsible Accredited Production (WRAP) certified production<br>Brushed inner fabric<br>WRAP certified<br>SEDEX certified<br>Vegan certified<br><br><strong>Fabric</strong><br>80% Ringspun cotton, 20% Polyester<br>Charcoal: 52% Ringspun cotton, 48% Polyester<br>Heather Grey: 75% Ringspun cotton, 25% Polyester<br>Smoke colours: 70% Ringspun cotton, 30% Polyester.<br><br><strong>Washing Instructions</strong><br>Machine wash 30°<br>Do not bleach<br>Tumble dry low heat<br>Low iron<br>Do not dry clean<br>",
    "Adult Sweatshirt": "<strong><br>Specification<br></strong>Crew neck sweat<br>Set-in-sleeves<br>Taped neck<br>Stylish fit<br>Twin needle stitching detailing<br>Ribbed collar, cuff and hem<br>Worldwide Responsible Accredited Production (WRAP) certified production<br>Brushed inner fleece<br>SEDEX certified<br>Vegan certified<br><br><strong>Fabric</strong><br>80% Ringspun Cotton 20% Polyester<br>Black Smoke: 70% Ringspun Cotton, 30% Polyester<br>Heather Grey: 75% Ringspun Cotton, 25% Polyester<br>Charcoal: 52% Ringspun Cotton, 48% Polyester<br><br><strong>Washing Instructions</strong><br>Machine wash 30°<br>Do not bleach<br>Tumble dry low heat<br>Low iron<br>Do not dry clean<br>",
    "Ladies Shirt": "<strong><br>Fabric<br></strong>100% Ringspun Cotton. Sport Grey: 90% Cotton, 10% Polyester. All Heathers: 65% Polyester, 35% Cotton. Antique Cherry Red, Antique Heliconia, Antique Sapphire: 90% Cotton, 10% Polyester<br><br><strong>Washing Instructions</strong><br>Machine wash warm. Inside out, with like colours. Only non-chlorine bleach. Tumble dry medium. Do not iron if decorated. Do not dry clean<br>",
    "Tank-Top": "<strong><br>Specification<br></strong>Wide straps<br>Rib knit trim applied to neckline and armholes<br>Twin needle bottom hem<br>Quarter-turned to eliminate centre crease<br><br><strong>Fabric</strong><br>100% Ringspun Cotton<br>Sport Grey: 90% Cotton, 10% Polyester<br>",
    "Longsleeve T-Shirt": "",
    "Oversized T Shirts": "<strong><br>Features<br></strong>Streetwear Icon<br>Classic in its simplicity<br>Crew neck<br>Wide fit<br>Cropped shoulders<br>Casual fit<br>Thick, soft-cotton fabric<br><br><strong>Washing Instructions</strong><br></span>Wash and iron inside out<br>Wash with similar colours<br>Machine wash 30°C gentle<br>Do not bleach<br>Do not tumble dry<br>Iron at low temperature<br>Do not dry clean<span></span></p>",
    "kids t shirt": "<strong><br>T-Shirt Chest ½ Measurement<br></strong>1 Inch Below Armhole<br>3/4 Years - 13.5 Inches<br>5/6 Years - 14 Inches<br>7/8 Years - 15 Inches<br>9/11 Years - 16 Inches<br>12/13 Years - 17 Inches<br><br><strong>T-Shirt Length<br></strong>Neck Point on Shoulder to Hem<br>3/4 Years - 18 Inches<br>5/6 Years - 20 Inches<br>7/8 Years - 21.5 Inches<br>9/11 Years - 23 Inches<br>12/13 Years - 24.5 Inches<br><br><em>*Garment Measurement are subject to tolerances. They are not be taken as absolute measurement values. When a garment is measured, the values may vary and can be above, below or the same as the values stated.</em><br>",
    "kids hoodie": "<strong><br>Specification<br></strong>Twin-needle stitching detailing<br>Double fabric hood<br>Kangaroo pouch pocket<br>Ribbed cuffs and hem<br>No drawcords to comply with EU regulation<br>Soft cotton faced fabric creates ideal printing surface<br>Worldwide Responsible Apparel Production (WRAP) certified production<br>Brushed inner fleece<br>WRAP certified<br>SEDEX certified<br>Vegan certified<br><br><strong>Fabric</strong><br>80% Ringspun cotton, 20% Polyester<br>Charcoal: 52% Ringspun cotton, 48% Polyester<br>Heather Grey: 75% Ringspun cotton, 25% Polyester<br>",
    "kids sweatshirt": "<strong><br>Specification<br></strong>Crew Neck<br>Set-in sleeves<br>Taped neck<br>Stylish fit<br>Soft cotton faced fabric<br>Twin needle stitching detailing<br>Ribbed collar, cuffs and hem<br>Brushed inner fleece<br>WRAP certified<br>SEDEX certified<br>Vegan certified<br><br><strong>Fabric</strong><br>80% Ringspun cotton, 20% Polyester<br>Charcoal: 52% Ringspun cotton, 48% Polyester<br>Heather Grey: 75% Ringspun cotton, 25% Polyester.<br>"
}

# --- Product Configuration ---
product_types = {
    "Adult T Shirt": {"sizes": ["Small", "Medium", "Large", "XL", "2XL", "3XL", "4XL"], "price": 16.99},
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
    "Adult T Shirt": ["Black", "Heather Grey", "Kelly", "Navy", "Red", "Royal", "White", "Orange", "Pink"],
    "Adult Hoodie": ["Black", "Heather Grey", "Navy", "Kelly", "Red", "Royal", "White", "Orange", "Pink"],
    "Adult Sweatshirt": ["Kelly", "Navy", "Royal", "Red", "Sky", "White", "Black", "Heather Grey", "Orange", "Pink"],
    "Ladies Shirt": ["Red", "Black", "Heather Grey", "Navy", "Kelly", "Royal", "White", "Orange", "Pink"],
    "Tank-Top": ["White", "Royal", "Red", "Navy", "Black", "Heather Grey", "Orange", "Kelly", "Pink"],
    "Longsleeve T-Shirt": ["Navy", "Kelly", "Red", "Royal", "White", "Black", "Heather Grey", "Orange", "Pink"],
    "Oversized T Shirts": ["Black", "White", "Vintage Blue", "City Red", "Hibiskus Pink", "Dark Grey", "Intense Blue", "Retro Green"],
    "kids t shirt": ["White", "Kelly", "Navy", "Red", "Royal", "Black", "Heather Grey", "Orange", "Pink"],
    "kids hoodie": ["Black", "Heather Grey", "Navy", "White", "Red", "Royal", "Kelly", "Orange", "Pink"],
    "kids sweatshirt": ["White", "Black", "Heather Grey", "Kelly", "Navy", "Red", "Royal", "Orange", "Pink"]
}


# --- Handle Form Submission ---
if submit:
    desc_list = [d.strip() for d in raw_descriptions.split('|') if d.strip()]

    # 1. Check for correct description count and required fields FIRST
    if len(desc_list) != len(garment_keys):
        st.error(f"❌ You provided {len(desc_list)} descriptions but {len(garment_keys)} are required.")
    elif not all([product_name, sku_suffix, main_color, tags, lister]):
        st.warning("⚠️ Please complete all fields.")

    # 2. Check for duplicate SKU suffix AFTER required fields
    elif os.path.exists(SKU_TRACKER_FILE):
        used_df = pd.read_csv(SKU_TRACKER_FILE, on_bad_lines='skip')
        any_match = used_df[used_df['sku_suffix'] == sku_suffix]
        if not any_match.empty:
            who = any_match.iloc[0]['lister']
            when = any_match.iloc[0]['timestamp']
            st.error(f"❌ This SKU suffix is already used by {who} on {when}. Please enter a new one.")
        else:
            # No match, safe to generate
            # --- Generate & Save CSV ---
            descriptions = {
                garment: f"""{desc}<br><br><b>Size Guide:</b><br>{body_html_map[garment]}{product_extras.get(garment, '')}"""
                for garment, desc in zip(garment_keys, desc_list)
            }
            rows = []
            for garment_type, config in product_types.items():
                sizes = config["sizes"]
                colors = correct_colors_by_type[garment_type]
                # Handle normalized equivalents for oversized naming
                equivalents = {
                    "Pink": "Hibiskus Pink",
                    "Grey": "Dark Grey",
                    "Blue": "Intense Blue",
                    "Red": "City Red",
                    "Light Blue": "Vintage Blue",
                    "Green": "Retro Green"
                } if garment_type == "Oversized T Shirts" else {}
                target_color = equivalents.get(main_color, main_color)
                if target_color in colors:
                    colors = [target_color] + [c for c in colors if c != target_color]
                for size, color in product(sizes, colors):
                    if garment_type == "Oversized T Shirts" and color == "Pink":
                        continue
                    price = config.get("price_by_size", {}).get(size, config.get("price"))
                    title = f"{product_name} {garment_type}"
                    handle = re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")
                    sku_prefix_map = {
                        "Adult T Shirt": "UC301",
                        "Adult Hoodie": "JH1001",
                        "Adult Sweatshirt": "JH030",
                        "Ladies Shirt": "5000L",
                        "Tank-Top": "JD012",
                        "Longsleeve T-Shirt": "JD011",
                        "Oversized T Shirts": "BY102",
                        "kids t shirt": "T06",
                        "kids hoodie": "JH01J",
                        "kids sweatshirt": "JH30J"
                    }
                    sku = f"{sku_prefix_map.get(garment_type, 'SKU')}-{size}-{color.replace(' ', '')}-{sku_suffix}"
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
            filename = f"{handle}.csv"
            df.to_csv(filename, index=False)

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
                st.download_button("📥 Download CSV File", f, file_name=filename)

            st.success("✅ Excel generated! Ready to download.")

            # Optional preview
            st.subheader("Preview")
            st.dataframe(df.head())

            with st.expander("📝 Preview Descriptions"):
                for garment, html in descriptions.items():
                    st.markdown(f"**{garment}**", unsafe_allow_html=True)
                    st.markdown(html, unsafe_allow_html=True)
                    st.markdown("---")

    # 3. If log file does NOT exist, just generate and save
    else:
        descriptions = {
            garment: f"""{desc}<br><br><b>Size Guide:</b><br>{body_html_map[garment]}{product_extras.get(garment, '')}"""
            for garment, desc in zip(garment_keys, desc_list)
        }
        rows = []
        for garment_type, config in product_types.items():
            sizes = config["sizes"]
            colors = correct_colors_by_type[garment_type]
            equivalents = {
                "Pink": "Hibiskus Pink",
                "Grey": "Dark Grey",
                "Blue": "Intense Blue",
                "Red": "City Red",
                "Light Blue": "Vintage Blue",
                "Green": "Retro Green"
            } if garment_type == "Oversized T Shirts" else {}
            target_color = equivalents.get(main_color, main_color)
            if target_color in colors:
                colors = [target_color] + [c for c in colors if c != target_color]
            for size, color in product(sizes, colors):
                if garment_type == "Oversized T Shirts" and color == "Pink":
                    continue
                price = config.get("price_by_size", {}).get(size, config.get("price"))
                title = f"{product_name} {garment_type}"
                handle = re.sub(r"[^\w\s-]", "", title.lower()).replace(" ", "-")
                sku_prefix_map = {
                    "Adult T Shirt": "UC301",
                    "Adult Hoodie": "JH1001",
                    "Adult Sweatshirt": "JH030",
                    "Ladies Shirt": "5000L",
                    "Tank-Top": "JD012",
                    "Longsleeve T-Shirt": "JD011",
                    "Oversized T Shirts": "BY102",
                    "kids t shirt": "T06",
                    "kids hoodie": "JH01J",
                    "kids sweatshirt": "JH30J"
                }
                sku = f"{sku_prefix_map.get(garment_type, 'SKU')}-{size}-{color.replace(' ', '')}-{sku_suffix}"
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
        filename = f"{handle}.csv"
        df.to_csv(filename, index=False)

        # Save suffix + lister + timestamp
        log_df = pd.DataFrame([{ 
            "sku_suffix": sku_suffix, 
            "lister": lister, 
            "timestamp": datetime.now().isoformat()
        }])
        log_df.to_csv(
            SKU_TRACKER_FILE,
            mode='a',
            header=True,
            index=False
        )

        with open(filename, "rb") as f:
            st.download_button("📥 Download CSV File", f, file_name=filename)

        st.success("✅ Excel generated! Ready to download.")

        # Optional preview
        st.subheader("Preview")
        st.dataframe(df.head())

        with st.expander("📝 Preview Descriptions"):
            for garment, html in descriptions.items():
                st.markdown(f"**{garment}**", unsafe_allow_html=True)
                st.markdown(html, unsafe_allow_html=True)
                st.markdown("---")
