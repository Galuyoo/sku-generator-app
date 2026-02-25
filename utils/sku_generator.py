import pandas as pd
import re
from itertools import product
from constants.mapping import HARD_CODED_IMAGE_MAPPING  # for image number mapping


def generate_sku_dataframe(
    product_name, sku_suffix, main_color, tags,
    garment_keys, raw_descriptions,
    body_html_map, product_extras, product_types, correct_colors_by_type,
    vendor, published, inventory_policy, fulfillment_service, requires_shipping, taxable, inventory_tracker,
    image_links=None,
    excluded_colors: list[str] = None,
    collection: str = None,
    page_titles: list[str] = None,
):
    def generate_alt_text(title):
        cleaned = re.sub(r"\b(Bootleg|Adult)\b", "", title, flags=re.IGNORECASE)
        cleaned = cleaned.replace("-", " ").strip()
        return re.sub(r"\s+", " ", cleaned)

    if excluded_colors is None:
        excluded_colors = []

    if isinstance(raw_descriptions, list):
        desc_list = [d.strip() for d in raw_descriptions if d.strip()]
    else:
        desc_list = [d.strip() for d in raw_descriptions.split("|") if d.strip()]

    descriptions = {
        garment: f"""{desc}<br><br><b>Size Guide:</b><br>{body_html_map[garment]}{product_extras.get(garment, '')}"""
        for garment, desc in zip(garment_keys, desc_list)
    }

    # Optional: SEO Title map from page_titles
    seo_title_map = {}
    if page_titles and len(page_titles) == len(garment_keys):
        seo_title_map = dict(zip(garment_keys, page_titles))

    # ✅ Adult garment name map for consistent naming (Shopify "Type" only)
    adult_map = {
        "T Shirt": "Adult T Shirt",
        "Hoodie": "Adult Hoodie",
        "Sweatshirt": "Adult Sweatshirt",
    }

    rows = []
    for garment_type, config in product_types.items():
        base_type = garment_type  # original, unmapped
        shopify_type = adult_map.get(base_type, base_type)  # ONLY for Shopify "Type" column

        sizes = config["sizes"]
        colors = correct_colors_by_type[base_type]

        # Handle color renaming for Oversized T Shirts
        if base_type == "Oversized T Shirts":
            equivalents = {
                "Pink": "Hibiskus Pink",
                "Grey": "Dark Grey",
                "Blue": "Intense Blue",
                "Red": "City Red",
                "Light Blue": "Vintage Blue",
                "Green": "Retro Green",
                "Kelly Green": "Retro Green",
                "Kelly": "Retro Green",
                "Royal": "Intense Blue",
                "Royal Blue": "Intense Blue",
            }
        else:
            equivalents = {
                "Royal Blue": "Royal",
                "Navy Blue": "Navy",
            }

        target_color = equivalents.get(main_color, main_color)
        if target_color in colors:
            colors = [target_color] + [c for c in colors if c != target_color]

        # 💥 Exclude banned colors from metadata or UI
        colors = [c for c in colors if c.lower() not in [ex.lower() for ex in excluded_colors]]

        for size, color in product(sizes, colors):
            if base_type == "Oversized T Shirts" and color == "Pink":
                continue

            price = config.get("price_by_size", {}).get(size, config.get("price"))

            # ✅ Title uses original base type (no "Adult" injected here)
            base_title = f"{product_name} {base_type}"
            seo_title_value = seo_title_map.get(base_type, base_title)

            # ✅ Clean handle (Shopify slug)
            handle = re.sub(
                r"[^\w\s-]", "",
                seo_title_value.split("|")[0].strip().lower()
            ).replace(" ", "-")
            handle = handle.replace("-bootleg", "").replace("-adult", "")

            sku_prefix_map = {
                "T Shirt": "UC301", "Hoodie": "JH1001", "Sweatshirt": "JH030",
                "Ladies Shirt": "5000L", "Tank-Top": "JD012", "Longsleeve T-Shirt": "JD011",
                "Oversized T Shirts": "BY102", "Kids T Shirt": "T06", "Kids Hoodie": "JH01J",
                "Kids Sweatshirt": "JH30J", "Ringer T-Shirt": "JH300", "Raglan T-Shirt": "JH400",
            }
            sku = f"{sku_prefix_map.get(base_type, 'SKU')}-{size}-{color.replace(' ', '')}-{sku_suffix}"

            rows.append({
                "Handle": handle,
                "Title": base_title,
                "SEO Title": seo_title_value,
                "Body (HTML)": descriptions[base_type],
                "Vendor": vendor,
                "Type": shopify_type,       # ✅ only place adult_map is applied
                "Base Type": base_type,     # ✅ keep raw type for internal logic (images, etc.)
                "Tags": tags,
                "Published": published,
                "Option1 Name": "Colour",
                "Option1 Value": color,
                "Option2 Name": "Size",
                "Option2 Value": size,
                "Variant SKU": sku,
                "Variant Grams": 300,
                "Variant Inventory Tracker": inventory_tracker,
                "Variant Inventory Qty": 25,
                "Variant Inventory Policy": inventory_policy,
                "Variant Fulfillment Service": fulfillment_service,
                "Variant Price": price,
                "Variant Requires Shipping": requires_shipping,
                "Variant Taxable": taxable,
                "Collection": collection or "",
            })

    df = pd.DataFrame(rows)

    # ✅ Assign image URLs and alt text if links provided
    if image_links is not None:
        df["Image URL"] = ""
        df["Image Alt Text"] = ""
        df["Image Position"] = ""
        df["Variant Image"] = ""

        for idx, row in df.iterrows():
            garment_type = row["Base Type"]  # use original type for mapping
            color = row["Option1 Value"].strip()
            garment_mapping = HARD_CODED_IMAGE_MAPPING.get(garment_type, {})
            image_number = garment_mapping.get(color)

            if image_number and image_links.get(image_number):
                image_url = image_links[image_number]
                df.at[idx, "Image URL"] = image_url
                df.at[idx, "Variant Image"] = image_url

                alt_source_title = seo_title_map.get(garment_type, row["Title"]).split("|")[0].strip()
                df.at[idx, "Image Alt Text"] = alt_source_title

                if color.lower() == main_color.strip().lower():
                    df.at[idx, "Image Position"] = 1

    return df
