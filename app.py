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
from constants.config import shopify_defaults
from constants.data_loader import load_json
from utils.sku_generator import generate_sku_dataframe
from utils.google_utils import connect_to_sheet
from utils.dropbox_utils import (
    get_dropbox_client,
    get_shared_link,     # used for art preview
    move_to_finished,    # used to archive processed folder
)
from utils.ui_utils import render_logo
from utils.shopify_utils import upload_products_from_df, ShopifyError
from utils.dropbox_utils import load_dropbox_image_links_parallel as load_dropbox_image_links

import io, zipfile

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


# ---------- Streamlit config ----------
st.set_page_config(page_title="SKU Generator", layout="centered")

# ---------- Env / validation ----------
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
if "auto_df" not in st.session_state: st.session_state.auto_df = None
if "auto_csv_name" not in st.session_state: st.session_state.auto_csv_name = None
if "auto_folder" not in st.session_state: st.session_state.auto_folder = None
if "auto_meta" not in st.session_state: st.session_state.auto_meta = None

# ---------- Small helpers ----------
def analyze_design_folders(dbx: dropbox.Dropbox, root: str):
    """Return (ready_list, not_ready_list), with deeper .json validation (e.g. description count)."""
    from constants.data_loader import load_json

    garment_keys = load_json("garment_keys.json")
    ready, not_ready = [], []

    try:
        res = dbx.files_list_folder(root)
        IGNORE_FOLDERS = {"finished", "images", "designs", "1_Ready"}

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
            numbered_pngs = [fn for fn in files if fn.lower().endswith(".png") and fn.split(".")[0].isdigit()]
            numbered_count = len(numbered_pngs)


            if not has_art:
                errors.append("Missing matching artwork")
            if numbered_count < 80:
                errors.append(f"Only {numbered_count}/80 images")

            if errors:
                not_ready.append({
                    "Folder": name,
                    "Has .json": "✅" if has_meta else "❌",
                    "Has notes": "✅" if has_txt else "❌",
                    "Has art": "✅" if has_art else "❌",
                    "Image count": f"{numbered_count} / 80",
                    "Issues": ", ".join(errors),
                })
            else:
                ready.append(name)

        except Exception as e:
            not_ready.append({
                "Folder": name,
                "Has metadata": "❌",
                "Has .txt": "❌",
                "Has art": "❌",
                "Image count": "0 / 80",
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


def build_design_dataframe(dbx: dropbox.Dropbox, folder: str, excluded_colors: list[str] = None):
    folder_path = f"{DESIGNS_ROOT}/{folder}"
    meta = download_metadata(dbx, folder_path)
    collection = meta.get("Collection", "").strip() or None


    # --- NEW: extract Restrictions field ---
    restrictions = meta.get("Restrictions", "")
    if restrictions:
        excluded_colors = [c.strip() for c in restrictions.split(",") if c.strip()]
    else:
        excluded_colors = []  # Treat missing or empty as "no restriction"

    product_name = meta.get("product_name","").strip()
    sku_suffix   = meta.get("sku_suffix","").strip().upper()
    main_color   = meta.get("main_color","").strip()
    tags_list    = meta.get("tags", [])
    descriptions = meta.get("descriptions", [])
    page_titles  = meta.get("page_titles", [])

    if not product_name or not sku_suffix or not main_color:
        raise ValueError("metadata.json missing product_name / sku_suffix / main_color")
    if not isinstance(tags_list, list):
        raise ValueError("metadata.json 'tags' must be a list")
    if len(descriptions) != len(garment_keys):
        raise ValueError(f"metadata.json 'descriptions' must have {len(garment_keys)} items")

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
        collection=collection,
        page_titles=page_titles
    )
    df = ensure_image_src_column(df)

    # >>> Your requested CSV fields <<<
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
tab_manual, tab_auto = st.tabs(["📝 Manual entry", "🤖 Auto from Dropbox"])

# =========================
# Tab 1: Manual entry
# =========================
with tab_manual:
    with st.form("sku_form"):
        product_name = st.text_input("Enter product name")
        sku_suffix   = st.text_input("Enter unique SKU suffix (e.g., IVARLULE)").strip().upper()
        main_color   = st.text_input("Enter main color (e.g., Black)").strip()
        tags         = st.text_input("Enter comma-separated tags").strip()
        lister       = st.selectbox("Who is listing this?", ["Sal", "Hannan"])
        st.markdown("**Enter 10 product descriptions, separated by `|`**")
        raw_descriptions = st.text_area("Descriptions", height=300).strip()
        submit = st.form_submit_button("Generate CSV")

    if submit:
        st.session_state.generating = True
        try:
            desc_list = [d.strip() for d in raw_descriptions.split("|") if d.strip()]
            if len(desc_list) != len(garment_keys):
                st.error(f"❌ You provided {len(desc_list)} descriptions but {len(garment_keys)} are required.")
                st.stop()
            if not all([product_name, sku_suffix, main_color, tags, lister]):
                st.warning("⚠️ Please complete all fields.")
                st.stop()

            sheet = connect_to_sheet("SKU Tracker")
            existing_suffixes = [row[0].strip().upper() for row in sheet.get_all_values()[1:]]
            if sku_suffix in existing_suffixes:
                st.error("❌ That SKU suffix is already used in Google Sheets. Please enter a new one.")
                st.stop()
            sheet.append_row([sku_suffix, lister, datetime.now().isoformat()])

            image_links = st.session_state.dropbox_image_links if st.session_state.dropbox_links_loaded else None

            df = generate_sku_dataframe(
                product_name, sku_suffix, main_color, tags,
                garment_keys, desc_list,
                body_html_map, product_extras, product_types, correct_colors_by_type,
                vendor, published, inventory_policy, fulfillment_service, requires_shipping, taxable, inventory_tracker,
                image_links=image_links,excluded_colors=excluded_colors,collection=collection
            )

            # >>> Your requested CSV fields <<<
            df = ensure_shopify_csv_fields(df)

            filename = f"{sku_suffix}.csv"
            df.to_csv(filename, index=False, encoding="utf-8-sig")
            with open(filename, "rb") as f:
                st.download_button("📥 Download CSV File", f, file_name=filename)

            if st.button("Send to Shopify"):
                with st.status("🚀 Uploading to Shopify…", expanded=True) as s:
                    try:
                        def emit(msg: str): s.write(msg)
                        results = upload_products_from_df(df, progress=emit)
                        s.update(label="✅ Upload complete")
                        st.success(f"Uploaded {len(results)} products.")
                        st.json(results)
                    except ShopifyError as e:
                        if str(e).startswith("DAILY_VARIANT_LIMIT:"):
                            s.update(label="⛔ Daily variant creation limit hit")
                            st.error("You’ve hit Shopify’s daily variant creation limit. Use CSV import now or resume via API tomorrow.")
                        else:
                            s.update(label="❌ Shopify upload failed")
                            st.error(f"Shopify error: {e}")
                    except Exception as e:
                        s.update(label="❌ Unexpected error during upload")
                        st.error(f"Unexpected error: {e}")

            with st.expander("📝 Preview Descriptions"):
                for garment in garment_keys:
                    st.markdown(f"**{garment}**", unsafe_allow_html=True)
                    st.markdown(df[df["Type"] == garment].iloc[0]["Body (HTML)"], unsafe_allow_html=True)
                    st.markdown("---")
        except Exception as e:
            st.error("❌ Something went wrong while generating the CSV.")
            st.exception(e)
        finally:
            st.session_state.generating = False

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
    st.subheader("Auto-generate from Dropbox design folders")

    if not DESIGNS_ROOT:
        st.warning("Set `FOLDER_PATH_Design` in dpbox.env to your `/designs` root to use this tab.")
        st.stop()

    dbx = get_dropbox_client()

    colA, colB = st.columns([1,1])
    with colA:
        if st.button("🔄 Refresh ready folders"):
            ready, not_ready = analyze_design_folders(dbx, DESIGNS_ROOT)
            st.session_state.ready_folders = ready
            st.session_state.not_ready_folders = not_ready

    with colB:
        if not st.session_state.ready_folders:
            ready, not_ready = analyze_design_folders(dbx, DESIGNS_ROOT)
            st.session_state.ready_folders = ready
            st.session_state.not_ready_folders = not_ready

    if "ready_folders" not in st.session_state or "not_ready_folders" not in st.session_state:
        st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(dbx, DESIGNS_ROOT)

    colA, colB = st.columns([1, 1])
    with colA:
        if st.button("🔄 Refresh folder analysis"):
            st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(dbx, DESIGNS_ROOT)

    ready_folders = st.session_state.ready_folders
    not_ready_info = st.session_state.not_ready_folders


    folder = st.selectbox("Choose a ready folder", ready_folders, index=0)
    folder_path = f"{DESIGNS_ROOT}/{folder}"

    if not_ready_info:
        with st.expander("📂 Not Ready Folders"):
            st.markdown(f"❌ **{len(not_ready_info)} folders not ready**")
            df_not_ready = pd.DataFrame(not_ready_info)

            # --- Option 1: Interactive table ---
            st.data_editor(df_not_ready, disabled=True, use_container_width=True)

    else:
        st.success("✅ All folders are ready!")

    col1, col2, col3 = st.columns(3)
    do_google_guard     = col1.checkbox("Google SKU guard", value=True)
    show_preview        = col2.checkbox("Show design preview", value=True)
    show_descs          = col3.checkbox("Show description preview", value=False)

    col4, col5 = st.columns(2)
    move_after_upload   = col4.checkbox("Move to /finished after upload", value=False)
    variant_cap         = col5.number_input("Max variants to create this run (0 = no cap)",
                                            min_value=0, value=0, step=50)

    if show_preview:
        try:
            entries = dbx.files_list_folder(folder_path).entries
            art = next(
                (e.name for e in entries
                 if isinstance(e, dropbox.files.FileMetadata)
                 and e.name.split(".")[0]==folder
                 and e.name.lower().split(".")[-1] in {"png","jpg","jpeg","webp"}), None
            )
            if art:
                art_url = get_shared_link(dbx, f"{folder_path}/{art}")
                if art_url:
                    st.image(art_url, caption=art, use_container_width=True)
        except Exception:
            pass

    # -------- Build CSV (no upload) --------
    if st.button("🧱 Build CSV (no upload)"):
        design_start = time.perf_counter()
        try:
            with st.status("Building design…", expanded=True) as s:
                df, meta, missing = build_design_dataframe(dbx, folder, excluded_colors=excluded_colors)

                if missing:
                    s.write(f"⚠️ Missing numbered images: {missing[:10]}{'…' if len(missing)>10 else ''}")
                else:
                    s.write("✅ All image links fetched")
                s.write("✅ DataFrame ready")

                if do_google_guard:
                    sheet = connect_to_sheet("SKU Tracker")
                    existing = [row[0].strip().upper() for row in sheet.get_all_values()[1:]]
                    sku_suffix = meta.get("sku_suffix","").strip().upper()
                    if sku_suffix in existing:
                        s.update(label=f"❌ SKU suffix already used: {sku_suffix}")
                        st.stop()

                local_name = f"{meta.get('sku_suffix','').strip().upper()}.csv"
                df.to_csv(local_name, index=False, encoding="utf-8-sig")
                s.write(f"📝 CSV saved: {local_name}")

                st.session_state.auto_df = df
                st.session_state.auto_csv_name = local_name
                st.session_state.auto_folder = folder
                st.session_state.auto_meta = meta

            st.success("Build complete. You can download the CSV below or upload when ready.")
            try:
                with open(st.session_state.auto_csv_name, "rb") as f:
                    st.download_button("📥 Download CSV File", f, file_name=st.session_state.auto_csv_name)
            except Exception:
                pass

            st.dataframe(st.session_state.auto_df.head(15))

            col_m, col_c = st.columns(2)
            if col_m.button("📦 Move this design to /finished now"):
                try:
                    dest = move_selected_to_finished(dbx, folder)
                    st.success(f"Moved to: {dest}")
                except Exception as e:
                    st.error(f"Move failed: {e}")

            if col_c.button("🧹 Delete images 1–127 in /finished and archive to Completed"):
                try:
                    deleted, dest = clean_and_archive_to_completed(dbx, folder)
                    st.success(f"Deleted {deleted} numbered images and archived to: {dest}")
                except Exception as e:
                    st.error(f"Clean & archive failed: {e}. Tip: move to /finished first.")
        finally:
            st.info(f"⏱ Build finished in {fmt_secs(time.perf_counter() - design_start)}")

    # -------- Upload built CSV (separate step) --------
    upload_disabled = st.session_state.auto_df is None or st.session_state.auto_folder != folder
    if st.button("🚀 Upload built CSV to Shopify", disabled=upload_disabled):
        if upload_disabled:
            st.warning("Build the CSV first for this folder.")
        else:
            design_start = time.perf_counter()
            df = st.session_state.auto_df
            meta = st.session_state.auto_meta

            if do_google_guard:
                sheet = connect_to_sheet("SKU Tracker")
                existing = [row[0].strip().upper() for row in sheet.get_all_values()[1:]]
                sku_suffix = meta.get("sku_suffix","").strip().upper()
                if sku_suffix in existing:
                    st.error(f"❌ SKU suffix already used: {sku_suffix}")
                    st.stop()
                sheet.append_row([sku_suffix, "StreamlitAuto", datetime.now().isoformat()])

            with st.status("🚀 Uploading to Shopify…", expanded=True) as s:
                try:
                    def emit(msg: str): s.write(msg)
                    cap = variant_cap if variant_cap > 0 else None
                    results = upload_products_from_df(df, progress=emit, variant_budget=cap)
                    s.update(label="✅ Upload complete")
                    st.success(f"Uploaded {len(results)} products.")
                    st.json(results)
                except ShopifyError as e:
                    if str(e).startswith("DAILY_VARIANT_LIMIT:"):
                        s.update(label="⛔ Daily variant creation limit hit")
                        st.error("You’ve hit Shopify’s daily variant creation limit. Use CSV import now or resume via API tomorrow.")
                    else:
                        s.update(label="❌ Shopify upload failed"); st.error(f"Shopify error: {e}")
                except Exception as e:
                    s.update(label="❌ Unexpected error during upload"); st.error(f"Unexpected error: {e}")
                else:
                    if move_after_upload:
                        try:
                            final_path = move_to_finished(get_dropbox_client(), DESIGNS_ROOT, folder, finished_dir="finished")
                            st.success(f"📦 Moved folder to: {final_path}")
                        except Exception as e:
                            st.warning(f"Uploaded, but move_to_finished failed: {e}")
            st.info(f"⏱ Upload finished in {fmt_secs(time.perf_counter() - design_start)}")

    # ---------- Batch CSV (no upload) ----------
    st.markdown("### 🧾 Batch CSV (no upload)")
    only_selected = st.checkbox("Only include selected folder", value=False)

    if st.button("📦 Build CSV(s) for batch (no upload)"):
        batch_start = time.perf_counter()
        try:
            targets = [folder] if only_selected else list(ready_folders)
            dfs = []
            for fname in targets:
                df_i, _, _ = build_design_dataframe(dbx, fname, excluded_colors=excluded_colors)

                dfs.append(df_i)

            if not dfs:
                st.warning("No dataframes built.")
                st.stop()

            all_df = pd.concat(dfs, ignore_index=True)
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

            # Store in session so buttons persist
            _stash_downloads("batch_csv_files", files)

            _render_downloads("batch_csv_files", "Your batch CSV file(s)", zip_name_prefix="BATCH")

            total_rows = sum(r for _, r, _ in built)
            st.success(f"Built {len(built)} CSV file(s) under {CSV_MAX_MB} MB each, total {total_rows} rows.")
            st.session_state.batch_targets = targets

        finally:
            st.info(f"⏱ Batch CSV build finished in {fmt_secs(time.perf_counter() - batch_start)}")

    # -------- Batch post-build actions --------
    c1, c2 = st.columns(2)
    if c1.button("📦 Move batch to /finished"):
        targets = st.session_state.get("batch_targets") or ([folder] if only_selected else list(ready_folders))
        with st.status("Moving folders to /finished…", expanded=True) as s:
            ok = 0
            for fname in targets:
                try:
                    dest = move_selected_to_finished(dbx, fname)
                    s.write(f"• {fname}: ✅ moved → {dest}")
                    ok += 1
                except Exception as e:
                    s.write(f"• {fname}: ❌ {e}")
            s.update(label=f"Done. {ok}/{len(targets)} moved.")

    if c2.button("🧹 Clean 1–127 imgs & archive batch to Completed"):
        targets = st.session_state.get("batch_targets") or ([folder] if only_selected else list(ready_folders))
        with st.status("Cleaning numbered images and archiving to Completed…", expanded=True) as s:
            ok = 0
            for fname in targets:
                try:
                    deleted, dest = clean_and_archive_to_completed(dbx, fname)
                    s.write(f"• {fname}: ✅ deleted {deleted} and archived → {dest}")
                    ok += 1
                except Exception as e:
                    s.write(f"• {fname}: ❌ {e}")
            s.update(label=f"Done. {ok}/{len(targets)} archived.")

    # -------- Original batch uploader (unchanged) --------
    if st.button("⚙️ Build & Upload ALL ready folders"):
        batch_start = time.perf_counter()
        summary = []
        for fname in ready_folders:
            fpath = f"{DESIGNS_ROOT}/{fname}"
            with st.status(f"📦 {fname}: starting…", expanded=True) as s:
                t0 = time.perf_counter()
                try:
                    df, meta, missing = build_design_dataframe(dbx, folder, excluded_colors=excluded_colors)

                    if missing: s.write(f"⚠️ Missing images: {missing[:10]}{'…' if len(missing)>10 else ''}")
                    else: s.write("✅ All image links fetched")
                    s.write("✅ DataFrame ready")

                    if do_google_guard:
                        sheet = connect_to_sheet("SKU Tracker")
                        existing = [row[0].strip().upper() for row in sheet.get_all_values()[1:]]
                        sku_suffix = meta.get("sku_suffix","").strip().upper()
                        if sku_suffix in existing:
                            s.update(label=f"❌ {fname}: SKU already used")
                            summary.append((fname, False, "SKU used", time.perf_counter()-t0))
                            continue
                        sheet.append_row([sku_suffix, "StreamlitBatch", datetime.now().isoformat()])

                    local_name = f"{meta.get('sku_suffix','').strip().upper()}.csv"
                    df.to_csv(local_name, index=False, encoding="utf-8-sig")
                    s.write(f"📝 CSV saved: {local_name}")

                    def emit(msg: str): s.write(msg)
                    results = upload_products_from_df(df, progress=emit)
                    s.update(label=f"✅ {fname}: upload complete")
                    summary.append((fname, True, "", time.perf_counter()-t0))

                    if move_after_upload:
                        try:
                            final_path = move_to_finished(get_dropbox_client(), DESIGNS_ROOT, fname, finished_dir="finished")
                            s.write("📦 Moved to /finished")
                        except Exception as e:
                            s.write(f"⚠️ Move failed: {e}")

                except ShopifyError as e:
                    if str(e).startswith("DAILY_VARIANT_LIMIT:"):
                        s.update(label=f"⛔ {fname}: daily variant creation limit hit")
                        summary.append((fname, False, "Daily variant limit", time.perf_counter()-t0))
                        break
                    else:
                        s.update(label=f"❌ {fname}: failed")
                        summary.append((fname, False, str(e), time.perf_counter()-t0))
                except Exception as e:
                    s.update(label=f"❌ {fname}: failed")
                    summary.append((fname, False, str(e), time.perf_counter()-t0))

        total = time.perf_counter() - batch_start
        st.subheader("Batch summary")
        for name, ok, err, secs in summary:
            if ok:
                st.write(f"• {name}: ✅ {fmt_secs(secs)}")
            else:
                st.write(f"• {name}: ❌ {fmt_secs(secs)} — {err}")
        st.info(f"⏱ All ready folders processed in {fmt_secs(total)}")
