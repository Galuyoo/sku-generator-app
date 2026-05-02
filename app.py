# app.py
import os
import json
import pandas as pd
import dropbox
import streamlit as st
from datetime import datetime
from dotenv import load_dotenv
import time
import requests
import re
# --- your existing imports (unchanged) ---
from utils import shopify_utils
from utils.canva_utils import load_canva_image_links_by_sku
from constants.config import shopify_defaults
from constants.data_loader import load_json
from utils.sku_generator import generate_sku_dataframe
from utils.google_utils import connect_to_sheet
from utils.listing_validation import (
    combine_validation_results,
    has_validation_errors,
    validate_listing_metadata,
    validate_shopify_dataframe,
)
from utils.dropbox_utils import (
    get_dropbox_client,
    get_shared_link,     # used for art preview
    move_to_finished,    # used to archive processed folder
)
from utils.ui_utils import render_logo
from utils.shopify_utils import upload_products_from_df, ShopifyError
from utils.dropbox_utils import load_dropbox_image_links_parallel as load_dropbox_image_links

import io, zipfile

def analyze_design_folders(
    dbx: dropbox.Dropbox,
    root: str,
    mockup_source: str = "Dropbox",
):
    ready, not_ready = [], []

    try:
        res = dbx.files_list_folder(root)
        IGNORE_FOLDERS = {"finished", "images", "designs", "1_ready"}

        folders = [
            e.name for e in res.entries
            if isinstance(e, dropbox.files.FolderMetadata) and e.name.lower() not in IGNORE_FOLDERS
        ]
    except Exception as e:
        return ready, [{"Folder": "N/A", "Issues": f"Failed to list root: {e}"}]

    for name in folders:
        path = f"{root}/{name}"
        try:
            entries = dbx.files_list_folder(path).entries
            files = {e.name for e in entries if isinstance(e, dropbox.files.FileMetadata)}

            errors = []

            json_files = [fn for fn in files if fn.lower().endswith(".json")]
            has_meta = bool(json_files)
            has_txt = any(fn.lower().endswith((".txt", ".pdf")) for fn in files)
            has_art = any(
                fn.split(".")[0] == name and fn.lower().split(".")[-1] in {"png", "jpg", "jpeg", "webp"}
                for fn in files
            )

            sku_suffix = ""
            descriptions_count_label = "N/A"
            if has_meta:
                try:
                    meta = download_metadata(dbx, path)
                    sku_suffix = (meta.get("sku_suffix") or "").strip().upper()
                    meta_issues, descriptions_count_label = _metadata_issues(meta)
                    errors.extend(meta_issues)
                except Exception as e:
                    errors.append(f"Invalid metadata.json: {e}")

            if not has_meta:
                errors.append("Missing metadata .json")

            if not has_art:
                errors.append("Missing matching artwork")

            image_count_label = "N/A"

            if mockup_source == "Canva":
                if not sku_suffix:
                    errors.append("Missing sku_suffix in metadata.json")
            else:
                numbered_images = [
                    fn for fn in files
                    if fn.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) and fn.split(".")[0].isdigit()
                ]
                numbered_count = len(numbered_images)
                image_count_label = f"{numbered_count} / 80"

                if numbered_count < 80:
                    errors.append(f"Only {numbered_count}/80 images")

            if errors:
                not_ready.append({
                    "Folder": name,
                    "Has .json": "✅" if has_meta else "❌",
                    "Has notes": "✅" if has_txt else "❌",
                    "Has art": "✅" if has_art else "❌",
                    "Image count": image_count_label,
                    "Descriptions": descriptions_count_label,
                    "SKU in json": "✅" if sku_suffix else "❌",
                    "Issues": ", ".join(errors),
                })
            else:
                ready.append(name)

        except Exception as e:
            not_ready.append({
                "Folder": name,
                "Has .json": "❌",
                "Has notes": "❌",
                "Has art": "❌",
                "Image count": "N/A" if mockup_source == "Canva" else "0 / 80",
                "Descriptions": "N/A",
                "SKU in json": "❌",
                "Issues": f"Error: {e}",
            })

    return ready, not_ready

def download_metadata(dbx: dropbox.Dropbox, folder_path: str) -> dict:
    try:
        entries = dbx.files_list_folder(folder_path).entries
        json_files = [e.name for e in entries if isinstance(e, dropbox.files.FileMetadata) and e.name.lower().endswith(".json")]
        if not json_files:
            raise FileNotFoundError(f"No .json metadata file found in {folder_path}")
        target_file = json_files[0]  # Use first one found
        _, res = dbx.files_download(f"{folder_path}/{target_file}")
        return json.loads(res.content)
    except dropbox.exceptions.ApiError as e:
        raise RuntimeError(f"Error accessing {folder_path}: {e}")


def _listing_min_tag_count() -> int:
    try:
        return max(0, int(os.getenv("LISTING_MIN_TAG_COUNT", "6")))
    except ValueError:
        return 6


def _validate_metadata_for_listing(meta: dict, label: str | None = None) -> dict:
    return validate_listing_metadata(
        meta,
        expected_item_count=len(garment_keys),
        min_tag_count=_listing_min_tag_count(),
        label=label,
    )


def _metadata_issues(meta: dict) -> tuple[list[str], str]:
    descriptions = meta.get("descriptions", [])

    if not isinstance(descriptions, list):
        descriptions_count_label = "invalid"
    else:
        descriptions_count = len([d for d in descriptions if str(d).strip()])
        descriptions_count_label = f"{descriptions_count} / {len(garment_keys)}"

    validation = _validate_metadata_for_listing(meta)
    return validation["errors"], descriptions_count_label

def _stash_downloads(key: str, files: list[tuple[str, bytes]]):
    """
    Store a list of (filename, file_bytes) in session_state under `key`.
    """
    st.session_state[key] = [{"name": n, "bytes": b} for (n, b) in files]

def _render_downloads(key: str, title: str, zip_name_prefix: str = "FILES"):
    """
    Render persisted downloads from session_state[key].
    Provides: individual download buttons, 'Download all as ZIP', and 'Clear' button.
    """
    items = st.session_state.get(key) or []
    if not items:
        return

    st.markdown(f"### {title}")

    # Individual download buttons
    for i, item in enumerate(items, start=1):
        st.download_button(
            label=f"📥 Download {item['name']}",
            data=item["bytes"],
            file_name=item["name"],
            mime="text/csv",
            key=f"{key}_dl_{i}"
        )

    # Download all as ZIP
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            zf.writestr(item["name"], item["bytes"])
    zip_buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    st.download_button(
        label="⬇️ Download ALL as ZIP",
        data=zip_buf.getvalue(),
        file_name=f"{zip_name_prefix}_{ts}.zip",
        mime="application/zip",
        key=f"{key}_zip"
    )

    # Clear button
    if st.button("🧹 Clear Downloads", key=f"{key}_clear"):
        del st.session_state[key]
        st.experimental_rerun()


def _format_validation_errors(validation: dict, limit: int = 5) -> str:
    errors = validation.get("errors", [])
    shown = "; ".join(errors[:limit])
    remaining = len(errors) - limit
    if remaining > 0:
        return f"{shown}; +{remaining} more"
    return shown


def _render_listing_safety_check_body(validation: dict) -> None:
    errors = validation.get("errors", [])
    warnings = validation.get("warnings", [])
    summary = validation.get("summary", {})

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Errors", len(errors))
    col2.metric("Warnings", len(warnings))
    col3.metric("Products checked", summary.get("products_checked", 0))
    col4.metric("Rows checked", summary.get("rows_checked", 0))

    metadata_checked = summary.get("metadata_files_checked", 0)
    if metadata_checked:
        st.caption(f"Metadata files checked: {metadata_checked}")

    if errors:
        st.error("Fix listing safety errors before download, export, or upload.")
    elif warnings:
        st.warning("Listing checks passed with warnings. Export is allowed.")
    else:
        st.success("Listing checks passed")

    if errors:
        st.markdown("**Errors**")
        for item in errors:
            st.write(f"- {item}")

    if warnings:
        st.markdown("**Warnings**")
        for item in warnings:
            st.write(f"- {item}")


def _render_listing_safety_checks(validation: dict, *, expanded: bool = False) -> None:
    errors = validation.get("errors", [])
    warnings = validation.get("warnings", [])

    with st.expander("Listing safety checks", expanded=expanded or bool(errors) or bool(warnings)):
        _render_listing_safety_check_body(validation)


def _render_auto_listing_safety_overview() -> None:
    shown = False

    if st.session_state.get("auto_validation"):
        st.markdown("#### Selected CSV")
        _render_listing_safety_check_body(st.session_state.auto_validation)
        shown = True

    if st.session_state.get("batch_validation"):
        if shown:
            st.divider()
        st.markdown("#### Batch CSV files")
        _render_listing_safety_check_body(st.session_state.batch_validation)
        shown = True

    if not shown:
        st.caption("Build a CSV to run listing safety checks.")


def render_section_header(title: str, caption: str | None = None):
    st.markdown(f"### {title}")
    if caption:
        st.caption(caption)


def render_action_summary(folder=None, sku_suffix=None, mockup_source=None, store=None):
    with st.container(border=True):
        st.markdown("#### Current selection")
        col1, col2, col3 = st.columns(3)
        col1.metric("Folder", folder or "None")
        col2.metric("SKU", sku_suffix or "N/A")
        col3.metric("Mockups", mockup_source or "N/A")
        if store:
            st.caption(f"Target store: {store}")


def _split_manual_items(raw: str, *, allow_pipe_separator: bool = False) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > 1:
        return lines

    if allow_pipe_separator:
        return [part.strip() for part in raw.split("|") if part.strip()]

    return lines


def _build_manual_metadata(
    product_name: str,
    sku_suffix: str,
    main_color: str,
    tags: str,
    page_titles: str,
    descriptions: str,
) -> dict:
    return {
        "product_name": (product_name or "").strip(),
        "sku_suffix": (sku_suffix or "").strip().upper(),
        "main_color": (main_color or "").strip(),
        "tags": [tag.strip() for tag in (tags or "").split(",") if tag.strip()],
        "page_titles": _split_manual_items(page_titles, allow_pipe_separator=False),
        "descriptions": _split_manual_items(descriptions, allow_pipe_separator=False),
    }


def _metadata_to_json_bytes(metadata: dict) -> bytes:
    return json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")


def _metadata_to_imageless_dataframe(metadata: dict) -> pd.DataFrame:
    tags_csv = ", ".join(str(tag).strip() for tag in metadata.get("tags", []) if str(tag).strip())
    df = generate_sku_dataframe(
        product_name=metadata.get("product_name", ""),
        sku_suffix=metadata.get("sku_suffix", ""),
        main_color=metadata.get("main_color", ""),
        tags=tags_csv,
        garment_keys=garment_keys,
        raw_descriptions=metadata.get("descriptions", []),
        body_html_map=body_html_map,
        product_extras=product_extras,
        product_types=product_types,
        correct_colors_by_type=correct_colors_by_type,
        vendor=vendor,
        published=published,
        inventory_policy=inventory_policy,
        fulfillment_service=fulfillment_service,
        requires_shipping=requires_shipping,
        taxable=taxable,
        inventory_tracker=inventory_tracker,
        image_links=None,
        excluded_colors=excluded_colors,
        page_titles=metadata.get("page_titles", []),
    )
    return ensure_shopify_csv_fields(df)


# ---------- Streamlit config ----------
st.set_page_config(page_title="SKU Generator", layout="centered")

# ---------- Env / validation ----------
# Load .env automatically (default)
load_dotenv()

# Optional fallback for legacy file name
if not os.getenv("DROPBOX_REFRESH_TOKEN"):
    load_dotenv("dpbox.env")

FINISHED_DIR_NAME = os.getenv("FINISHED_DIR_NAME", "finished")
COMPLETED_ROOT = os.getenv(
    "COMPLETED_ROOT",
    "/Spoofy/Portrait/1. uk office folder/1. Uk office Completed"
)

CSV_MAX_MB   = float(os.getenv("SHOPIFY_PRODUCT_CSV_MAX_MB", "14.5"))
CSV_MAX_ROWS = int(os.getenv("SHOPIFY_PRODUCT_CSV_MAX_ROWS", "0"))

# ---- Store picker (after load_dotenv) ----
def _profile(label, url_env, token_env):
    url   = (os.getenv(url_env) or "").strip()
    token = (os.getenv(token_env) or "").strip()
    return {"label": label, "url": url, "token": token}

STORE_PROFILES = []
test_p = _profile("Galuyoo (test)", "SHOPIFY_STORE_URL_TEST", "SHOPIFY_API_PASSWORD_TEST")
if test_p["url"] and test_p["token"]:
    STORE_PROFILES.append(test_p)
prod_p = _profile("Spoofytees (prod)", "SHOPIFY_STORE_URL_PROD", "SHOPIFY_API_PASSWORD_PROD")
if prod_p["url"] and prod_p["token"]:
    STORE_PROFILES.append(prod_p)

legacy_url   = (os.getenv("SHOPIFY_STORE_URL") or "").strip()
legacy_token = (os.getenv("SHOPIFY_API_PASSWORD") or os.getenv("SHOPIFY_ADMIN_API_ACCESS_TOKEN") or "").strip()
if not STORE_PROFILES and legacy_url and legacy_token:
    STORE_PROFILES.append({"label": f"{legacy_url} (legacy env)", "url": legacy_url, "token": legacy_token})

with st.sidebar:
    st.header("🛍️ Target Shopify Store")
    if not STORE_PROFILES:
        st.error("No store profiles found. Set SHOPIFY_STORE_URL_* and SHOPIFY_API_PASSWORD_* in dpbox.env.")
    else:
        labels = [p["label"] for p in STORE_PROFILES]
        default_idx = 0
        if "shop_profile_label" in st.session_state:
            try:
                default_idx = labels.index(st.session_state.shop_profile_label)
            except ValueError:
                pass

        selected_label = st.selectbox("Choose store", labels, index=default_idx)
        st.session_state.shop_profile_label = selected_label
        sel = next(p for p in STORE_PROFILES if p["label"] == selected_label)
        os.environ["SHOPIFY_STORE_URL"] = sel["url"]
        os.environ["SHOPIFY_API_PASSWORD"] = sel["token"]
        st.caption(f"Active store: `{sel['url']}`")

        if st.button("🔎 Check connection"):
            try:
                api_ver = os.getenv("SHOPIFY_API_VERSION", "2024-10")
                r = requests.get(
                    f"https://{sel['url']}/admin/api/{api_ver}/shop.json",
                    headers={"X-Shopify-Access-Token": sel["token"], "Accept": "application/json"},
                    timeout=int(os.getenv("SHOPIFY_HTTP_TIMEOUT", "120"))
                )
                limit = r.headers.get("X-Shopify-Shop-Api-Call-Limit")
                st.write(f"Status: {r.status_code} — Call-Limit: {limit}")
                if r.ok:
                    st.success("Connected ✅")
                else:
                    st.error(r.text)
            except Exception as e:
                st.error(f"Check failed: {e}")

REQUIRED_ENV = [
    "GOOGLE_KEYFILE",
    "DROPBOX_APP_KEY",
    "DROPBOX_APP_SECRET",
    "DROPBOX_REFRESH_TOKEN",
    "FOLDER_PATH",
]
missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
if missing:
    st.warning(f"Environment missing: {', '.join(missing)}. Image mapping will be disabled until fixed.")

FOLDER_PATH  = os.getenv("FOLDER_PATH", "").strip()
DESIGNS_ROOT = os.getenv("FOLDER_PATH_Design", "").strip()

# ---------- Session defaults ----------
if "generating" not in st.session_state: st.session_state.generating = False
if "ENABLE_IMAGE_MAPPING" not in st.session_state: st.session_state.ENABLE_IMAGE_MAPPING = False
if "ENABLE_PREVIEW_IMAGE" not in st.session_state: st.session_state.ENABLE_PREVIEW_IMAGE = False
if "dropbox_image_links" not in st.session_state: st.session_state.dropbox_image_links = {}
if "dropbox_links_loaded" not in st.session_state: st.session_state.dropbox_links_loaded = False
if "loaded_folder_path" not in st.session_state: st.session_state.loaded_folder_path = None
if "ready_folders" not in st.session_state: st.session_state.ready_folders = []
if "not_ready_folders" not in st.session_state: st.session_state.not_ready_folders = []
if "last_mockup_source" not in st.session_state: st.session_state.last_mockup_source = None
if "auto_df" not in st.session_state: st.session_state.auto_df = None
if "auto_csv_name" not in st.session_state: st.session_state.auto_csv_name = None
if "auto_folder" not in st.session_state: st.session_state.auto_folder = None
if "auto_meta" not in st.session_state: st.session_state.auto_meta = None
if "auto_validation" not in st.session_state: st.session_state.auto_validation = None
if "batch_validation" not in st.session_state: st.session_state.batch_validation = None

# ---------- Small helpers ----------
def ensure_image_src_column(df: pd.DataFrame) -> pd.DataFrame:
    if "Image Src" not in df.columns and "Image URL" in df.columns:
        df["Image Src"] = df["Image URL"]
    return df

def fmt_secs(sec: float) -> str:
    if sec < 60: return f"{sec:.1f}s"
    m, s = divmod(sec, 60)
    if m < 60: return f"{int(m)}m {s:.1f}s"
    h, m = divmod(int(m), 60)
    return f"{h}h {m}m {s:.0f}s"

# ----- SEO / CSV helpers you asked for -----

def _strip_after_pipe(title: str) -> str:
    return (title or "").split("|", 1)[0].strip()

def _html_to_text(html: str) -> str:
    s = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", s).strip()

def _meta_150_last_sentence(html: str) -> str:
    """
    Plain text from HTML, then cut at the last '.' before 150 chars.
    If no '.' exists before 150, return the first 150 chars trimmed.
    """
    text = _html_to_text(html)
    if len(text) <= 150:
        return text
    cut = text[:150]
    last_dot = cut.rfind(".")
    if last_dot != -1:
        return cut[:last_dot+1].strip()
    return cut.strip()

def ensure_shopify_csv_fields(df: pd.DataFrame) -> pd.DataFrame:
    """
    - Title = product_name + garment type (already in df)
    - SEO Title = full title with pipe (already in df["SEO Title"])
    - SEO Description: from Body (HTML), cut at last '.' before 150 chars
    - Google Shopping / Custom Label 0: 'Sal'
    """
    # Use existing df["Title"] and df["SEO Title"], no modification
    # Just ensure the SEO Description is processed

    df["SEO Description"] = df["Body (HTML)"].astype(str).apply(_meta_150_last_sentence)

    col = "Google Shopping / Custom Label 0"
    if col not in df.columns:
        df[col] = "Sal"
    else:
        df[col] = df[col].fillna("Sal").replace("", "Sal")

    return df


# Future migration note:
# Keep workflow logic in functions outside Streamlit UI so Django can reuse it later.
def build_design_dataframe(
    dbx: dropbox.Dropbox,
    folder: str,
    excluded_colors: list[str] = None,
    mockup_source: str = "Dropbox",
    metadata: dict | None = None,
):
    folder_path = f"{DESIGNS_ROOT}/{folder}" if folder else None
    meta = metadata if metadata is not None else download_metadata(dbx, folder_path)

    restrictions = meta.get("Restrictions", "")
    if restrictions:
        excluded_colors = [c.strip() for c in restrictions.split(",") if c.strip()]
    else:
        excluded_colors = []

    product_name = meta.get("product_name", "").strip()
    sku_suffix   = meta.get("sku_suffix", "").strip().upper()
    main_color   = meta.get("main_color", "").strip()
    tags_list    = meta.get("tags", [])
    descriptions = meta.get("descriptions", [])
    page_titles  = meta.get("page_titles", [])

    if not product_name or not sku_suffix or not main_color:
        raise ValueError("metadata.json missing product_name / sku_suffix / main_color")
    if not isinstance(tags_list, list):
        raise ValueError("metadata.json 'tags' must be a list")
    if not isinstance(descriptions, list):
        raise ValueError(f"{folder}: metadata.json 'descriptions' must be a list")
    desc_count = len([d for d in descriptions if str(d).strip()])
    if desc_count != len(garment_keys):
        raise ValueError(
            f"{folder}: metadata.json 'descriptions' has {desc_count}/{len(garment_keys)} items"
        )

    if mockup_source == "Canva":
        if not sku_suffix:
            raise ValueError("metadata.json missing sku_suffix required for Canva lookup")
        image_links, missing = load_canva_image_links_by_sku(sku_suffix, total_images=80)
    else:
        image_links, missing = load_dropbox_image_links(dbx, folder_path, total_images=80)

    tags_csv = ", ".join(t.strip() for t in tags_list if t.strip())
    df = generate_sku_dataframe(
        product_name=product_name,
        sku_suffix=sku_suffix,
        main_color=main_color,
        tags=tags_csv,
        garment_keys=garment_keys,
        raw_descriptions=descriptions,
        body_html_map=body_html_map,
        product_extras=product_extras,
        product_types=product_types,
        correct_colors_by_type=correct_colors_by_type,
        vendor=vendor,
        published=published,
        inventory_policy=inventory_policy,
        fulfillment_service=fulfillment_service,
        requires_shipping=requires_shipping,
        taxable=taxable,
        inventory_tracker=inventory_tracker,
        image_links=image_links,
        excluded_colors=excluded_colors,
        page_titles=page_titles,
    )
    df = ensure_image_src_column(df)
    df = ensure_shopify_csv_fields(df)

    return df, meta, missing


# ---------- Header / logo ----------
# render_logo()
st.title("🧵 SKU Generator for Shopify")

# ---------- Sidebar (manual image loader) ----------
if not st.session_state.generating:
    with st.sidebar:
        st.header("🖼️ Dropbox Image Loader (Manual tab)")
        if st.button("🔄 Get / Refresh Image Links"):
            try:
                dbx = get_dropbox_client() 

                with st.spinner("⏳ Fetching image links from Dropbox..."):
                    links, failed = load_dropbox_image_links(dbx, FOLDER_PATH, total_images=80)
                st.session_state.dropbox_image_links = links
                st.session_state.dropbox_links_loaded = (len(links) == 80 and len(failed) == 0)
                if st.session_state.dropbox_links_loaded:
                    st.success("✅ Dropbox image links loaded successfully.")
                else:
                    st.warning(f"Loaded {len(links)} images. Missing: {len(failed)}.")
            except Exception as e:
                st.session_state.dropbox_links_loaded = False
                st.error("Failed to load Dropbox image links.")
                st.exception(e)

        if st.session_state.dropbox_links_loaded:
            img_num = st.number_input("Image # to Preview", 1, 80, value=1)
            url = st.session_state.dropbox_image_links.get(int(img_num))
            if url:
                st.markdown("### 🎨 Preview")
                st.markdown(f'<img src="{url}" style="width:100%; border-radius:10px;" />', unsafe_allow_html=True)
            else:
                st.warning("No URL for that image number.")

# ---------- Shopify defaults ----------
vendor = shopify_defaults["vendor"]
published = shopify_defaults["published"]
inventory_policy = shopify_defaults["inventory_policy"]
fulfillment_service = shopify_defaults["fulfillment_service"]
requires_shipping = shopify_defaults["requires_shipping"]
taxable = shopify_defaults["taxable"]
inventory_tracker = shopify_defaults["inventory_tracker"]

# ---------- Config JSON ----------
garment_keys           = load_json("garment_keys.json")
body_html_map          = load_json("size_guides.json")
product_extras         = load_json("product_extras.json")
product_types          = load_json("product_types.json")
correct_colors_by_type = load_json("colors.json")

# After loading colors.json
ALL_COLORS = sorted({c for colors in correct_colors_by_type.values() for c in colors})

excluded_colors = st.multiselect(
    "Exclude these garment colors from the CSV",
    options=ALL_COLORS,
    help="If selected, variants in these colors will be skipped from CSV generation."
)


# ---------- Tabs ----------
tab_manual, tab_auto = st.tabs(["Manual listing builder", "🤖 Auto from Dropbox"])

# =========================
# Tab 1: Manual listing builder
# =========================
with tab_manual:
    render_section_header("Manual listing builder")

    if "manual_listing_builder" not in st.session_state:
        st.session_state.manual_listing_builder = None

    with st.form("manual_listing_builder_form"):
        output_mode = st.radio(
            "Build output",
            ["Metadata JSON only", "Imageless CSV only", "Both JSON and CSV"],
            horizontal=True,
        )
        product_name = st.text_input("Product name")
        sku_suffix = st.text_input("SKU suffix").strip().upper()
        main_color = st.text_input("Main color").strip()
        tags = st.text_input("Tags, comma-separated").strip()
        page_titles = st.text_area("Page titles", height=160)
        descriptions = st.text_area("Descriptions", height=300)
        lister = st.selectbox("Lister", ["Sal", "Hannan"])
        track_sku = st.checkbox("Enable SKU tracking")
        submit = st.form_submit_button("Build listing")

    if submit:
        st.session_state.generating = True
        st.session_state.manual_listing_builder = None
        try:
            metadata = _build_manual_metadata(
                product_name=product_name,
                sku_suffix=sku_suffix,
                main_color=main_color,
                tags=tags,
                page_titles=page_titles,
                descriptions=descriptions,
            )
            sku_label = metadata.get("sku_suffix") or "Manual listing"
            wants_json = output_mode in {"Metadata JSON only", "Both JSON and CSV"}
            wants_csv = output_mode in {"Imageless CSV only", "Both JSON and CSV"}
            metadata_validation = validate_listing_metadata(metadata, label=sku_label)
            csv_validation = None
            df = None
            tracking_error = None

            if track_sku and metadata.get("sku_suffix"):
                sheet = connect_to_sheet("SKU Tracker")
                existing_suffixes = [row[0].strip().upper() for row in sheet.get_all_values()[1:] if row]
                if metadata["sku_suffix"] in existing_suffixes:
                    tracking_error = "That SKU suffix is already used in Google Sheets. Please enter a new one."

            if wants_csv and not has_validation_errors(metadata_validation):
                df = _metadata_to_imageless_dataframe(metadata)
                csv_validation = validate_shopify_dataframe(df, label=sku_label)

            has_blocking_errors = has_validation_errors(metadata_validation) or bool(tracking_error)
            if wants_csv:
                has_blocking_errors = has_blocking_errors or df is None or has_validation_errors(csv_validation or {})

            if track_sku and not has_blocking_errors:
                sheet = connect_to_sheet("SKU Tracker")
                sheet.append_row([metadata["sku_suffix"], lister, datetime.now().isoformat()])

            st.session_state.manual_listing_builder = {
                "mode": output_mode,
                "wants_json": wants_json,
                "wants_csv": wants_csv,
                "metadata": metadata,
                "metadata_validation": metadata_validation,
                "df": df,
                "csv_validation": csv_validation,
                "tracking_error": tracking_error,
                "has_blocking_errors": has_blocking_errors,
                "json_filename": f"{metadata.get('sku_suffix', '').strip().upper()}_metadata.json",
                "csv_filename": f"{metadata.get('sku_suffix', '').strip().upper()}.csv",
            }
        except Exception as e:
            st.error("Something went wrong while building the manual listing.")
            st.exception(e)
        finally:
            st.session_state.generating = False

    manual_result = st.session_state.manual_listing_builder
    if manual_result:
        st.markdown("#### Metadata validation")
        _render_listing_safety_checks(manual_result["metadata_validation"])

        if manual_result.get("tracking_error"):
            st.error(manual_result["tracking_error"])

        if manual_result["wants_csv"] and manual_result.get("csv_validation"):
            st.markdown("#### CSV validation")
            _render_listing_safety_checks(manual_result["csv_validation"])

        if not manual_result["has_blocking_errors"]:
            metadata = manual_result["metadata"]
            if manual_result["wants_json"]:
                st.download_button(
                    "Download metadata JSON",
                    data=_metadata_to_json_bytes(metadata),
                    file_name=manual_result["json_filename"],
                    mime="application/json",
                )

            if manual_result["wants_csv"]:
                df = manual_result["df"]
                csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "Download imageless CSV",
                    data=csv_bytes,
                    file_name=manual_result["csv_filename"],
                    mime="text/csv",
                )

                if st.button("Send to Shopify"):
                    with st.status("Uploading to Shopify...", expanded=True) as s:
                        try:
                            def emit(msg: str): s.write(msg)
                            results = upload_products_from_df(df, progress=emit)
                            s.update(label="Upload complete")
                            st.success(f"Uploaded {len(results)} products.")
                            st.json(results)
                        except ShopifyError as e:
                            if str(e).startswith("DAILY_VARIANT_LIMIT:"):
                                s.update(label="Daily variant creation limit hit")
                                st.error("You've hit Shopify's daily variant creation limit. Use CSV import now or resume via API tomorrow.")
                            else:
                                s.update(label="Shopify upload failed")
                                st.error(f"Shopify error: {e}")
                        except Exception as e:
                            s.update(label="Unexpected error during upload")
                            st.error(f"Unexpected error: {e}")

                with st.expander("Preview Descriptions"):
                    key_col = "Base Type" if "Base Type" in df.columns else "Type"
                    for garment in garment_keys:
                        st.markdown(f"**{garment}**", unsafe_allow_html=True)

                        sub = df[df[key_col] == garment]
                        if sub.empty:
                            st.warning(f"No rows found for '{garment}' (preview only).")
                            st.markdown("---")
                            continue

                        st.markdown(sub.iloc[0]["Body (HTML)"], unsafe_allow_html=True)
                        st.markdown("---")
# ------------------------------------------------------------
# Helpers for Auto tab
# ------------------------------------------------------------

# --- CSV chunking helpers (batch, no upload) ---
def _csv_bytes_len(df: pd.DataFrame) -> int:
    return len(df.to_csv(index=False).encode("utf-8-sig"))

def _split_df_by_limits(df: pd.DataFrame, *, max_mb: float = None, max_rows: int = None) -> list[pd.DataFrame]:
    if max_mb is None:
        max_mb = CSV_MAX_MB
    if max_rows is None:
        max_rows = CSV_MAX_ROWS

    bytes_limit = int(max_mb * 1024 * 1024)
    chunks: list[pd.DataFrame] = []
    cur_parts: list[pd.DataFrame] = []

    def flush_current():
        if cur_parts:
            out = pd.concat(cur_parts, ignore_index=True)
            chunks.append(out)
            cur_parts.clear()

    def fits_with(piece: pd.DataFrame) -> bool:
        tmp = piece if not cur_parts else pd.concat(cur_parts + [piece], ignore_index=True)
        size = _csv_bytes_len(tmp)
        rows = len(tmp)
        if size > bytes_limit: return False
        if max_rows and rows > max_rows: return False
        return True

    for _, g in df.groupby("Handle", sort=False):
        if _csv_bytes_len(g) > bytes_limit or (max_rows and len(g) > max_rows):
            flush_current()
            start, step = 0, max(1, min(len(g), max_rows if max_rows else len(g)))
            while start < len(g):
                piece = g.iloc[start:start+step]
                while (_csv_bytes_len(piece) > bytes_limit or (max_rows and len(piece) > max_rows)) and len(piece) > 1:
                    step = max(1, step // 2)
                    piece = g.iloc[start:start+step]
                chunks.append(piece.reset_index(drop=True))
                start += len(piece)
            continue

        if not fits_with(g):
            flush_current()
        cur_parts.append(g.reset_index(drop=True))

    flush_current()
    return chunks

def _dbx_exists(dbx: dropbox.Dropbox, path: str) -> bool:
    try:
        dbx.files_get_metadata(path)
        return True
    except dropbox.exceptions.ApiError:
        return False

def _ensure_folder(dbx: dropbox.Dropbox, path: str):
    if not _dbx_exists(dbx, path):
        dbx.files_create_folder_v2(path)

def move_selected_to_finished(dbx: dropbox.Dropbox, folder: str) -> str:
    from utils.dropbox_utils import move_to_finished, get_dropbox_client
    final_path = move_to_finished(get_dropbox_client(), DESIGNS_ROOT, folder, finished_dir=FINISHED_DIR_NAME)
    return final_path

def clean_and_archive_to_completed(dbx: dropbox.Dropbox, folder: str) -> tuple[int, str]:
    finished_path = f"{DESIGNS_ROOT}/{FINISHED_DIR_NAME}/{folder}"
    if not _dbx_exists(dbx, finished_path):
        raise RuntimeError(f"Folder not in /{FINISHED_DIR_NAME}: {finished_path}")

    pat = re.compile(r"^([1-9]\d{0,2})\.(png|jpg|jpeg|webp)$", re.IGNORECASE)
    deleted = 0
    entries = dbx.files_list_folder(finished_path).entries
    for e in entries:
        if isinstance(e, dropbox.files.FileMetadata):
            m = pat.match(e.name)
            if not m:
                continue
            num = int(m.group(1))
            if 1 <= num <= 127:
                dbx.files_delete_v2(f"{finished_path}/{e.name}")
                deleted += 1

    _ensure_folder(dbx, COMPLETED_ROOT)
    dest = f"{COMPLETED_ROOT}/{folder}"
    dbx.files_move_v2(finished_path, dest, autorename=True)
    return deleted, dest

# =========================
# Tab 2: Auto from Dropbox
# =========================
with tab_auto:
    render_section_header(
        "Auto from Dropbox",
        "Build Shopify CSVs from ready design folders, then download or upload after safety checks pass.",
    )

    build_selected_clicked = False
    build_batch_clicked = False
    upload_selected_clicked = False
    upload_all_clicked = False
    only_selected = bool(st.session_state.get("batch_only_selected", False))

    with st.expander("1. Source and folder selection", expanded=True):
        with st.container(border=True):
            render_section_header("Source settings")
            mockup_source = st.selectbox("Mockup source", ["Dropbox", "Canva"], index=0, key="mockup_source_select")

            col1, col2, col3 = st.columns(3)
            do_google_guard = col1.checkbox("Google SKU guard", value=True, key="google_sku_guard")
            show_preview = col2.checkbox("Show design preview", value=True, key="show_design_preview")
            show_descs = col3.checkbox("Show description preview", value=False, key="show_description_preview")

            col4, col5 = st.columns(2)
            move_after_upload = col4.checkbox("Move to /finished after upload", value=False, key="move_after_upload")
            variant_cap = col5.number_input(
                "Max variants to create this run (0 = no cap)",
                min_value=0,
                value=0,
                step=50,
                key="variant_cap",
            )

        if not DESIGNS_ROOT:
            st.warning("Set `FOLDER_PATH_Design` in dpbox.env to your `/designs` root to use this tab.")
            st.stop()

        dbx = get_dropbox_client()

        cached_ready_folders = st.session_state.get("ready_folders", [])
        cached_not_ready_folders = st.session_state.get("not_ready_folders", [])

        if (
            st.session_state.get("last_mockup_source") != mockup_source
            or (not cached_ready_folders and not cached_not_ready_folders)
        ):
            st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                dbx, DESIGNS_ROOT, mockup_source=mockup_source
            )
            st.session_state.last_mockup_source = mockup_source

        ready_folders = st.session_state.get("ready_folders", [])
        not_ready_info = st.session_state.get("not_ready_folders", [])

        with st.container(border=True):
            render_section_header("Folder readiness")
            if st.button("Refresh folder analysis", key="refresh_folder_analysis_btn"):
                st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                    dbx, DESIGNS_ROOT, mockup_source=mockup_source
                )
                ready_folders = st.session_state.get("ready_folders", [])
                not_ready_info = st.session_state.get("not_ready_folders", [])

            col_ready, col_not_ready = st.columns(2)
            col_ready.metric("Ready folders", len(ready_folders))
            col_not_ready.metric("Not ready", len(not_ready_info))

            if not_ready_info:
                st.markdown("#### Not ready folders")
                df_not_ready = pd.DataFrame(not_ready_info)
                st.data_editor(df_not_ready, disabled=True, width="stretch")
            else:
                st.success("All folders are ready.")

        with st.container(border=True):
            render_section_header("Selected design")

            if ready_folders:
                folder = st.selectbox("Choose a ready folder", ready_folders, index=0, key="ready_folder_select")
            else:
                folder = None
                st.info("No ready folders for the selected mockup source.")

            folder_path = f"{DESIGNS_ROOT}/{folder}" if folder else None
            selected_sku_suffix = None
            if folder_path:
                try:
                    selected_sku_suffix = download_metadata(dbx, folder_path).get("sku_suffix", "").strip().upper()
                except Exception as e:
                    st.warning(f"Could not read selected metadata: {e}")

            render_action_summary(
                folder=folder,
                sku_suffix=selected_sku_suffix,
                mockup_source=mockup_source,
                store=st.session_state.get("shop_profile_label"),
            )

            if show_preview and folder_path:
                try:
                    entries = dbx.files_list_folder(folder_path).entries
                    art = next(
                        (e.name for e in entries
                         if isinstance(e, dropbox.files.FileMetadata)
                         and e.name.split(".")[0] == folder
                         and e.name.lower().split(".")[-1] in {"png", "jpg", "jpeg", "webp"}), None
                    )
                    if art:
                        art_url = get_shared_link(dbx, f"{folder_path}/{art}")
                        if art_url:
                            st.image(art_url, caption=art, width="stretch")
                except Exception:
                    pass

    with st.expander("2. Build CSV", expanded=True):
        build_scope = st.radio(
            "Build scope",
            ["Selected folder", "Batch"],
            horizontal=True,
            key="build_scope",
        )

        if build_scope == "Selected folder":
            selected_build_disabled = folder is None
            if selected_build_disabled:
                st.caption("Select a ready folder first.")
            build_selected_clicked = st.button(
                "Build selected CSV",
                disabled=selected_build_disabled,
                key="build_selected_csv_btn",
            )

            if build_selected_clicked:
                design_start = time.perf_counter()
                build_succeeded = False
                st.session_state.auto_validation = None
                try:
                    with st.status("Building selected design...", expanded=True) as s:
                        meta = download_metadata(dbx, f"{DESIGNS_ROOT}/{folder}")
                        metadata_validation = _validate_metadata_for_listing(meta, label=folder)

                        s.write(f"Mockup source: {mockup_source}")
                        s.write(f"Folder: {folder}")
                        s.write(f"SKU from metadata: {meta.get('sku_suffix', '').strip().upper()}")

                        if has_validation_errors(metadata_validation):
                            st.session_state.auto_df = None
                            st.session_state.auto_csv_name = None
                            st.session_state.auto_folder = None
                            st.session_state.auto_meta = None
                            st.session_state.auto_validation = metadata_validation
                            s.update(label="Listing safety checks failed")
                        else:
                            df, meta, missing = build_design_dataframe(
                                dbx,
                                folder,
                                excluded_colors=excluded_colors,
                                mockup_source=mockup_source,
                                metadata=meta,
                            )

                            if missing:
                                s.write(f"Missing numbered images: {missing[:10]}{'...' if len(missing)>10 else ''}")
                            else:
                                s.write("All image links fetched")
                            s.write("DataFrame ready")

                            csv_validation = validate_shopify_dataframe(df, label=folder)
                            listing_validation = combine_validation_results(metadata_validation, csv_validation)

                            if do_google_guard:
                                sheet = connect_to_sheet("SKU Tracker")
                                existing = [row[0].strip().upper() for row in sheet.get_all_values()[1:]]
                                sku_suffix = meta.get("sku_suffix", "").strip().upper()
                                if sku_suffix in existing:
                                    listing_validation["errors"].append(f"SKU suffix already used in SKU Tracker: {sku_suffix}")
                                    s.update(label=f"SKU suffix already used: {sku_suffix}")

                            st.session_state.auto_validation = listing_validation

                            if has_validation_errors(listing_validation):
                                st.session_state.auto_df = None
                                st.session_state.auto_csv_name = None
                                st.session_state.auto_folder = None
                                st.session_state.auto_meta = None
                                s.update(label="Listing safety checks failed")
                            else:
                                local_name = f"{meta.get('sku_suffix', '').strip().upper()}.csv"
                                df.to_csv(local_name, index=False, encoding="utf-8-sig")
                                s.write(f"CSV saved: {local_name}")

                                st.session_state.auto_df = df
                                st.session_state.auto_csv_name = local_name
                                st.session_state.auto_folder = folder
                                st.session_state.auto_meta = meta
                                build_succeeded = True

                    if has_validation_errors(st.session_state.auto_validation):
                        st.error("CSV export and Shopify upload blocked by listing safety errors.")
                    elif build_succeeded:
                        st.success("Build complete. Download or upload from the sections below.")
                finally:
                    st.info(f"Build finished in {fmt_secs(time.perf_counter() - design_start)}")

        else:
            only_selected = st.checkbox(
                "Only include selected folder",
                value=only_selected,
                key="batch_only_selected",
            )
            batch_build_disabled = (only_selected and folder is None) or (not only_selected and not ready_folders)
            if batch_build_disabled:
                st.caption("Select a ready folder first." if only_selected else "No ready folders available.")
            build_batch_clicked = st.button(
                "Build batch CSV files",
                disabled=batch_build_disabled,
                key="build_batch_csv_btn",
            )

            if build_batch_clicked:
                batch_start = time.perf_counter()
                try:
                    st.session_state.batch_targets = []
                    st.session_state.batch_validation = None
                    st.session_state.pop("batch_csv_files", None)
                    targets = [folder] if only_selected else list(ready_folders)
                    dfs = []
                    built_targets = []
                    failed_builds = []
                    batch_validation_parts = []
                    for fname in targets:
                        try:
                            meta_i = download_metadata(dbx, f"{DESIGNS_ROOT}/{fname}")
                            metadata_validation = _validate_metadata_for_listing(meta_i, label=fname)
                            batch_validation_parts.append(metadata_validation)

                            if has_validation_errors(metadata_validation):
                                failed_builds.append((fname, _format_validation_errors(metadata_validation)))
                                continue

                            df_i, _, _ = build_design_dataframe(
                                dbx,
                                fname,
                                excluded_colors=excluded_colors,
                                mockup_source=mockup_source,
                                metadata=meta_i,
                            )
                            batch_validation_parts.append(validate_shopify_dataframe(df_i, label=fname))
                            dfs.append(df_i)
                            built_targets.append(fname)
                        except Exception as e:
                            failed_builds.append((fname, str(e)))

                    batch_validation = combine_validation_results(*batch_validation_parts)
                    all_df = None
                    if not has_validation_errors(batch_validation) and dfs:
                        all_df = pd.concat(dfs, ignore_index=True)
                        batch_validation = combine_validation_results(
                            batch_validation,
                            validate_shopify_dataframe(all_df, label="Batch combined"),
                        )

                    st.session_state.batch_validation = batch_validation

                    if has_validation_errors(batch_validation):
                        st.session_state.pop("batch_csv_files", None)
                        st.error("Batch CSV export blocked by listing safety errors.")
                        for fname, reason in failed_builds:
                            st.error(f"{fname}: {reason}")
                    elif not dfs:
                        st.warning("No dataframes built.")
                        for fname, reason in failed_builds:
                            st.error(f"{fname}: {reason}")
                        st.session_state.pop("batch_csv_files", None)
                    else:
                        chunks = _split_df_by_limits(all_df)

                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        built = []
                        files = []
                        for i, cdf in enumerate(chunks, start=1):
                            csv_bytes = cdf.to_csv(index=False).encode("utf-8-sig")
                            size_kb = len(csv_bytes) / 1024.0
                            fname = f"BATCH_{ts}_part{i}.csv"
                            files.append((fname, csv_bytes))
                            built.append((fname, len(cdf), size_kb))

                        # Store in session so buttons persist.
                        _stash_downloads("batch_csv_files", files)

                        total_rows = sum(r for _, r, _ in built)
                        st.success(f"Built {len(built)} CSV file(s) under {CSV_MAX_MB} MB each, total {total_rows} rows.")
                        st.session_state.batch_targets = built_targets
                        for fname, reason in failed_builds:
                            st.warning(f"{fname}: {reason}")

                finally:
                    st.info(f"Batch CSV build finished in {fmt_secs(time.perf_counter() - batch_start)}")

    with st.expander("3. Listing safety checks", expanded=False):
        safety_slot = st.container()

    with st.expander("4. Download CSV files", expanded=True):
        if st.session_state.auto_df is not None and st.session_state.auto_csv_name:
            st.caption(f"Selected CSV: {st.session_state.auto_csv_name}")
            try:
                with open(st.session_state.auto_csv_name, "rb") as f:
                    st.download_button(
                        "Download selected CSV",
                        f,
                        file_name=st.session_state.auto_csv_name,
                        key="download_selected_csv_btn",
                    )
            except Exception as e:
                st.warning(f"Could not open selected CSV: {e}")
            st.dataframe(st.session_state.auto_df.head(15))
        else:
            st.caption("Build a selected CSV first.")

        if st.session_state.get("batch_csv_files"):
            _render_downloads("batch_csv_files", "Batch CSV file(s)", zip_name_prefix="BATCH")
        else:
            st.caption("Build batch CSV files first.")

    upload_disabled = st.session_state.auto_df is None or st.session_state.auto_folder != folder
    with st.expander("5. Upload to Shopify", expanded=False):
        upload_scope = st.radio(
            "Upload scope",
            ["Selected built CSV", "All ready folders"],
            horizontal=True,
            key="upload_scope",
        )

        if upload_scope == "Selected built CSV":
            if upload_disabled:
                st.caption("Build a CSV for the selected folder before uploading.")
            upload_selected_clicked = st.button(
                "Upload selected CSV to Shopify",
                disabled=upload_disabled,
                key="upload_selected_csv_btn",
            )

            if upload_selected_clicked:
                if upload_disabled:
                    st.warning("Build a CSV for the selected folder before uploading.")
                else:
                    design_start = time.perf_counter()
                    df = st.session_state.auto_df
                    meta = st.session_state.auto_meta
                    base_validation = st.session_state.auto_validation or validate_shopify_dataframe(df, label=folder)
                    upload_validation = combine_validation_results(base_validation)
                    pending_tracker_row = None

                    if do_google_guard:
                        sheet = connect_to_sheet("SKU Tracker")
                        existing = [row[0].strip().upper() for row in sheet.get_all_values()[1:]]
                        sku_suffix = meta.get("sku_suffix", "").strip().upper()
                        if sku_suffix in existing:
                            upload_validation["errors"].append(f"SKU suffix already used in SKU Tracker: {sku_suffix}")
                        else:
                            pending_tracker_row = [sku_suffix, "StreamlitAuto", datetime.now().isoformat()]

                    st.session_state.auto_validation = upload_validation

                    if has_validation_errors(upload_validation):
                        st.error("Shopify upload blocked by listing safety errors.")
                    else:
                        with st.status("Uploading selected CSV to Shopify...", expanded=True) as s:
                            try:
                                def emit(msg: str): s.write(msg)
                                cap = variant_cap if variant_cap > 0 else None
                                results = upload_products_from_df(df, progress=emit, variant_budget=cap)
                                if pending_tracker_row:
                                    sheet.append_row(pending_tracker_row)
                                s.update(label="Upload complete")
                                st.success(f"Uploaded {len(results)} products.")
                                st.json(results)
                            except ShopifyError as e:
                                if str(e).startswith("DAILY_VARIANT_LIMIT:"):
                                    s.update(label="Daily variant creation limit hit")
                                    st.error("You've hit Shopify's daily variant creation limit. Use CSV import now or resume via API tomorrow.")
                                else:
                                    s.update(label="Shopify upload failed")
                                    st.error(f"Shopify error: {e}")
                            except Exception as e:
                                s.update(label="Unexpected error during upload")
                                st.error(f"Unexpected error: {e}")
                            else:
                                if move_after_upload:
                                    try:
                                        final_path = move_to_finished(get_dropbox_client(), DESIGNS_ROOT, folder, finished_dir="finished")
                                        st.success(f"Moved folder to: {final_path}")
                                    except Exception as e:
                                        st.warning(f"Uploaded, but move_to_finished failed: {e}")
                    st.info(f"Upload finished in {fmt_secs(time.perf_counter() - design_start)}")
        else:
            st.warning("This uploads all ready folders to Shopify. Use only after reviewing readiness and safety checks.")
            upload_all_clicked = st.button(
                "Upload all ready folders to Shopify",
                key="upload_all_ready_btn",
            )

            if upload_all_clicked:
                batch_start = time.perf_counter()
                summary = []
                bulk_validation_parts = []
                for fname in ready_folders:
                    with st.status(f"{fname}: starting...", expanded=True) as s:
                        t0 = time.perf_counter()
                        try:
                            meta = download_metadata(dbx, f"{DESIGNS_ROOT}/{fname}")
                            metadata_validation = _validate_metadata_for_listing(meta, label=fname)
                            if has_validation_errors(metadata_validation):
                                bulk_validation_parts.append(metadata_validation)
                                s.update(label=f"{fname}: listing safety checks failed")
                                summary.append((fname, False, _format_validation_errors(metadata_validation), time.perf_counter()-t0))
                                continue

                            df, meta, missing = build_design_dataframe(
                                dbx,
                                fname,
                                excluded_colors=excluded_colors,
                                mockup_source=mockup_source,
                                metadata=meta,
                            )

                            if missing:
                                s.write(f"Missing images: {missing[:10]}{'...' if len(missing)>10 else ''}")
                            else:
                                s.write("All image links fetched")
                            s.write("DataFrame ready")

                            listing_validation = combine_validation_results(
                                metadata_validation,
                                validate_shopify_dataframe(df, label=fname),
                            )
                            pending_tracker_row = None

                            if do_google_guard:
                                sheet = connect_to_sheet("SKU Tracker")
                                existing = [row[0].strip().upper() for row in sheet.get_all_values()[1:]]
                                sku_suffix = meta.get("sku_suffix", "").strip().upper()
                                if sku_suffix in existing:
                                    listing_validation["errors"].append(f"SKU suffix already used in SKU Tracker: {sku_suffix}")
                                else:
                                    pending_tracker_row = [sku_suffix, "StreamlitBatch", datetime.now().isoformat()]

                            bulk_validation_parts.append(listing_validation)

                            if has_validation_errors(listing_validation):
                                s.update(label=f"{fname}: listing safety checks failed")
                                summary.append((fname, False, _format_validation_errors(listing_validation), time.perf_counter()-t0))
                                continue

                            local_name = f"{meta.get('sku_suffix', '').strip().upper()}.csv"
                            df.to_csv(local_name, index=False, encoding="utf-8-sig")
                            s.write(f"CSV saved: {local_name}")

                            def emit(msg: str): s.write(msg)
                            results = upload_products_from_df(df, progress=emit)
                            if pending_tracker_row:
                                sheet.append_row(pending_tracker_row)
                            s.update(label=f"{fname}: upload complete")
                            summary.append((fname, True, "", time.perf_counter()-t0))

                            if move_after_upload:
                                try:
                                    final_path = move_to_finished(get_dropbox_client(), DESIGNS_ROOT, fname, finished_dir="finished")
                                    s.write(f"Moved to: {final_path}")
                                except Exception as e:
                                    s.write(f"Move failed: {e}")

                        except ShopifyError as e:
                            if str(e).startswith("DAILY_VARIANT_LIMIT:"):
                                s.update(label=f"{fname}: daily variant creation limit hit")
                                summary.append((fname, False, "Daily variant limit", time.perf_counter()-t0))
                                break
                            else:
                                s.update(label=f"{fname}: failed")
                                summary.append((fname, False, str(e), time.perf_counter()-t0))
                        except Exception as e:
                            s.update(label=f"{fname}: failed")
                            summary.append((fname, False, str(e), time.perf_counter()-t0))

                if bulk_validation_parts:
                    st.session_state.batch_validation = combine_validation_results(*bulk_validation_parts)

                total = time.perf_counter() - batch_start
                st.subheader("Batch summary")
                for name, ok, err, secs in summary:
                    if ok:
                        st.write(f"- {name}: uploaded in {fmt_secs(secs)}")
                    else:
                        st.write(f"- {name}: failed in {fmt_secs(secs)} - {err}")
                st.info(f"All ready folders processed in {fmt_secs(total)}")

    with st.expander("6. Advanced folder actions", expanded=False):
        render_section_header("Selected folder actions")
        selected_action_disabled = folder is None
        if selected_action_disabled:
            st.caption("Select a ready folder first.")

        col_m, col_c = st.columns(2)
        if col_m.button("Move this design to /finished", disabled=selected_action_disabled, key="move_selected_finished_btn"):
            try:
                dest = move_selected_to_finished(dbx, folder)
                st.success(f"Moved to: {dest}")
            except Exception as e:
                st.error(f"Move failed: {e}")

        if col_c.button("Delete images 1-127 and archive selected", disabled=selected_action_disabled, key="archive_selected_btn"):
            try:
                deleted, dest = clean_and_archive_to_completed(dbx, folder)
                st.success(f"Deleted {deleted} numbered images and archived to: {dest}")
            except Exception as e:
                st.error(f"Clean and archive failed: {e}. Tip: move to /finished first.")

        st.divider()
        render_section_header("Batch folder actions")
        batch_targets = st.session_state.get("batch_targets") or ([folder] if only_selected and folder else list(ready_folders))
        batch_action_disabled = not batch_targets
        if batch_action_disabled:
            st.caption("Build batch CSV files first.")

        c1, c2 = st.columns(2)
        if c1.button("Move batch to /finished", disabled=batch_action_disabled, key="move_batch_finished_btn"):
            targets = batch_targets
            with st.status("Moving folders to /finished...", expanded=True) as s:
                ok = 0
                for fname in targets:
                    try:
                        dest = move_selected_to_finished(dbx, fname)
                        s.write(f"- {fname}: moved to {dest}")
                        ok += 1
                    except Exception as e:
                        s.write(f"- {fname}: {e}")
                s.update(label=f"Done. {ok}/{len(targets)} moved.")

        if c2.button("Clean 1-127 imgs and archive batch", disabled=batch_action_disabled, key="archive_batch_btn"):
            targets = batch_targets
            with st.status("Cleaning numbered images and archiving to Completed...", expanded=True) as s:
                ok = 0
                for fname in targets:
                    try:
                        deleted, dest = clean_and_archive_to_completed(dbx, fname)
                        s.write(f"- {fname}: deleted {deleted} and archived to {dest}")
                        ok += 1
                    except Exception as e:
                        s.write(f"- {fname}: {e}")
                s.update(label=f"Done. {ok}/{len(targets)} archived.")

    with safety_slot:
        _render_auto_listing_safety_overview()
