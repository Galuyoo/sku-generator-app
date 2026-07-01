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
from pathlib import Path
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
from utils.mockup_zip_intake import (
    load_metadata_json,
    upload_ready_design_to_dropbox_temp,
    replace_pipeline_design_mockups,
    attach_metadata_to_pipeline_design,
    digest_mockup_zip_to_pipeline,
    extract_mockup_images,
    inspect_mockup_zip,
    scan_pipeline_folders,
    upload_mockup_images_to_dropbox,
    validate_image_bytes,
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
    validate_dropbox_images: bool = False,
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
            has_art = any(_is_design_art_filename(fn) for fn in files)

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
                image_count_label = f"{numbered_count} / {MOCKUP_ZIP_EXPECTED_IMAGES}"

                if numbered_count < MOCKUP_ZIP_EXPECTED_IMAGES:
                    errors.append(f"Only {numbered_count}/{MOCKUP_ZIP_EXPECTED_IMAGES} images")
                elif validate_dropbox_images:
                    invalid_images = _invalid_dropbox_numbered_images(
                        dbx,
                        path,
                        MOCKUP_ZIP_EXPECTED_IMAGES,
                    )
                    if invalid_images:
                        invalid_labels = ", ".join(
                            f"{item['target']} ({item['error']})"
                            for item in invalid_images[:5]
                        )
                        remaining = len(invalid_images) - 5
                        if remaining > 0:
                            invalid_labels = f"{invalid_labels}; +{remaining} more"
                        errors.append(f"Invalid images: {invalid_labels}")

            if errors:
                not_ready.append({
                    "Folder": name,
                    "Has .json": "✅" if has_meta else "",
                    "Has notes": "✅" if has_txt else "",
                    "Has art": "✅" if has_art else "",
                    "Image count": image_count_label,
                    "Descriptions": descriptions_count_label,
                    "SKU in json": "✅" if sku_suffix else "",
                    "Issues": ", ".join(errors),
                })
            else:
                ready.append(name)

        except Exception as e:
            not_ready.append({
                "Folder": name,
                "Has .json": "",
                "Has notes": "",
                "Has art": "",
                "Image count": "N/A" if mockup_source == "Canva" else "0 / 80",
                "Descriptions": "N/A",
                "SKU in json": "",
                "Issues": f"Error: {e}",
            })

    return ready, not_ready


def _invalid_dropbox_numbered_images(
    dbx: dropbox.Dropbox,
    folder_path: str,
    expected_count: int,
) -> list[dict]:
    try:
        entries = _list_dropbox_folder_entries(dbx, folder_path)
    except Exception as exc:
        return [{"target": "N/A", "error": f"Could not list folder: {exc}"}]

    numbered_files = []
    for entry in entries:
        if not isinstance(entry, dropbox.files.FileMetadata):
            continue

        stem, ext = os.path.splitext(entry.name.lower())
        if stem.isdigit() and ext in {".png", ".jpg", ".jpeg", ".webp"}:
            number = int(stem)
            if 1 <= number <= expected_count:
                numbered_files.append((number, entry.name))

    invalid = []
    for number, filename in sorted(numbered_files):
        path = f"{folder_path.rstrip('/')}/{filename}"
        try:
            _, response = dbx.files_download(path)
            ok, error = validate_image_bytes(response.content)
        except Exception as exc:
            ok = False
            error = str(exc)

        if not ok:
            invalid.append({"number": number, "target": filename, "error": error})

    return invalid


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
        label="⬇ Download ALL as ZIP",
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
        st.error("Must fix before export.")
    elif warnings:
        st.warning("Review recommended improvements. Export is allowed.")
    else:
        st.success("Listing checks passed")

    if errors:
        with st.expander("Must-fix details", expanded=True):
            for item in errors:
                st.write(f"- {item}")

    if warnings:
        with st.expander("Recommended improvement details", expanded=not errors):
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


MANUAL_MIN_TAG_COUNT = 4


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
        excluded_garments=excluded_garments,
        page_titles=metadata.get("page_titles", []),
    )
    return ensure_shopify_csv_fields(df)


def _split_exclusion_values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[,|\n]", str(value))
    return [str(item).strip() for item in values if str(item).strip()]


def _metadata_exclusions(metadata: dict, keys: list[str]) -> list[str]:
    values = []
    for key in keys:
        values.extend(_split_exclusion_values(metadata.get(key)))
    return values


def _merge_exclusions(*groups) -> list[str]:
    merged = []
    seen = set()
    for group in groups:
        for item in group or []:
            cleaned = str(item).strip()
            key = cleaned.lower()
            if cleaned and key not in seen:
                merged.append(cleaned)
                seen.add(key)
    return merged


def _render_manual_page_title_lengths(page_title_items: list[str]) -> None:
    for index, title in enumerate(page_title_items, start=1):
        length = len(title)
        if length >= 70:
            st.error(f"{index}. {length} chars - {title}")
        else:
            st.write(f"{index}. {length} chars - {title}")


def _build_manual_example_listing(expected_item_count: int) -> dict:
    descriptions = []
    for index in range(1, expected_item_count + 1):
        descriptions.append(
            "This safe test listing description is written for manual builder checks. "
            "It describes a comfortable graphic garment with a clean print, everyday styling, "
            "and an easy gifting angle for customers browsing Shopify. "
            f"Example item {index} keeps the text unique while avoiding brand names, restricted terms, "
            "or claims that would need extra review. It is intentionally long enough to exercise "
            "the listing validation preview without relying on Dropbox images."
        )

    return {
        "product_name": "Test Tag",
        "sku_suffix": "TESTMANUAL",
        "main_color": "Black",
        "tags": "test listing, graphic tee, gift idea, casual wear, unisex style, manual builder",
        "page_titles": "\n".join(
            f"Test Tag {garment} | Classic Graphic Style"
            for garment in garment_keys[:expected_item_count]
        ),
        "descriptions": "\n".join(descriptions),
        "lister": "Sal",
        "track_sku": False,
        "output_mode": "Both JSON and CSV",
    }


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

LIVE_DEPLOYMENT_MODE = os.getenv("SKU_APP_LIVE_DEPLOYMENT", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

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

if not LIVE_DEPLOYMENT_MODE:
    with st.sidebar:
        st.header(" Target Shopify Store")
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
    "DROPBOX_APP_KEY",
    "DROPBOX_APP_SECRET",
    "DROPBOX_REFRESH_TOKEN",
    "FOLDER_PATH_Design",
]
if not LIVE_DEPLOYMENT_MODE:
    REQUIRED_ENV.extend(["GOOGLE_KEYFILE", "FOLDER_PATH"])
missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
if missing:
    st.warning(f"Environment missing: {', '.join(missing)}. Some features will be disabled until fixed.")

FOLDER_PATH  = os.getenv("FOLDER_PATH", "").strip()
DESIGNS_ROOT = os.getenv("FOLDER_PATH_Design", "").strip()
MOCKUP_ZIP_EXPECTED_IMAGES = int(os.getenv("MOCKUP_ZIP_EXPECTED_IMAGES", "80") or "80")


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
    excluded_garments: list[str] = None,
    mockup_source: str = "Dropbox",
    metadata: dict | None = None,
):
    folder_path = f"{DESIGNS_ROOT}/{folder}" if folder else None
    meta = metadata if metadata is not None else download_metadata(dbx, folder_path)

    metadata_excluded_colors = _metadata_exclusions(
        meta,
        [
            "Restrictions",
            "excluded_colors",
            "excluded_colours",
            "not_allowed_colors",
            "not_allowed_colours",
            "restricted_colors",
            "restricted_colours",
        ],
    )
    metadata_excluded_garments = _metadata_exclusions(
        meta,
        [
            "excluded_garments",
            "not_allowed_garments",
            "restricted_garments",
            "excluded_products",
            "not_allowed_products",
        ],
    )
    excluded_colors = _merge_exclusions(excluded_colors, metadata_excluded_colors)
    excluded_garments = _merge_exclusions(excluded_garments, metadata_excluded_garments)

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
        excluded_garments=excluded_garments,
        page_titles=page_titles,
    )
    df = ensure_image_src_column(df)
    df = ensure_shopify_csv_fields(df)

    return df, meta, missing


# ---------- Header / logo ----------
# render_logo()
st.title("🧵 SKU Generator for Shopify")

# ---------- Sidebar (manual image loader) ----------
if not LIVE_DEPLOYMENT_MODE and not st.session_state.generating:
    with st.sidebar:
        st.header("🖼 Dropbox Image Loader (Manual tab)")
        if st.button("🔄 Get / Refresh Image Links"):
            try:
                dbx = get_dropbox_client() 

                with st.spinner(" Fetching image links from Dropbox..."):
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

restriction_col1, restriction_col2 = st.columns(2)
with restriction_col1:
    excluded_colors = st.multiselect(
        "Not allowed colours",
        options=ALL_COLORS,
        help="Variants in these colours will be skipped from CSV generation.",
    )
with restriction_col2:
    excluded_garments = st.multiselect(
        "Not allowed garments",
        options=garment_keys,
        help="Selected garment/product types will be skipped entirely.",
    )



def _load_pipeline_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_pipeline_csv_for_active_design(design_folder: str, excluded_colors=None, excluded_garments=None):
    design_path = Path(design_folder)
    metadata_path = design_path / "metadata.json"
    image_links_path = design_path / "image_links.json"

    if not metadata_path.exists():
        raise RuntimeError(f"Missing metadata JSON: {metadata_path}")

    if not image_links_path.exists():
        raise RuntimeError(f"Missing image_links.json: {image_links_path}")

    metadata = _load_pipeline_json(metadata_path)
    image_links_raw = _load_pipeline_json(image_links_path)

    image_links = {
        int(key): value
        for key, value in image_links_raw.items()
        if str(key).isdigit() and value
    }

    tags_csv = ", ".join(
        str(tag).strip()
        for tag in metadata.get("tags", [])
        if str(tag).strip()
    )

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
        image_links=image_links,
        excluded_colors=excluded_colors or [],
        excluded_garments=excluded_garments or [],
        page_titles=metadata.get("page_titles", []),
    )

    sku = str(metadata.get("sku_suffix", design_path.name)).strip() or design_path.name
    csv_path = design_path / f"{sku}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    return {
        "sku": sku,
        "csv_path": str(csv_path),
        "rows": len(df),
        "columns": list(df.columns),
        "df": df,
    }



# ---------- Tabs ----------
if LIVE_DEPLOYMENT_MODE:
    tab_auto = st.container()
else:
    tab_manual, tab_pipeline, tab_auto = st.tabs(["Manual listing builder", "Pipeline Intake", "Auto from Dropbox"])

# =========================
# Tab 1: Manual listing builder
# =========================
if not LIVE_DEPLOYMENT_MODE:
    with tab_manual:
        render_section_header("Manual listing builder")

        if "manual_listing_builder" not in st.session_state:
            st.session_state.manual_listing_builder = None

        manual_expected_count = len(garment_keys)
        manual_defaults = {
            "manual_output_mode": "Metadata JSON only",
            "manual_product_name": "",
            "manual_sku_suffix": "",
            "manual_main_color": "",
            "manual_tags": "",
            "manual_page_titles": "",
            "manual_descriptions": "",
            "manual_lister": "Sal",
            "manual_track_sku": False,
        }
        for key, value in manual_defaults.items():
            st.session_state.setdefault(key, value)

        action_col1, action_col2 = st.columns(2)
        if action_col1.button("Fill example test listing"):
            example = _build_manual_example_listing(manual_expected_count)
            st.session_state.manual_output_mode = example["output_mode"]
            st.session_state.manual_product_name = example["product_name"]
            st.session_state.manual_sku_suffix = example["sku_suffix"]
            st.session_state.manual_main_color = example["main_color"]
            st.session_state.manual_tags = example["tags"]
            st.session_state.manual_page_titles = example["page_titles"]
            st.session_state.manual_descriptions = example["descriptions"]
            st.session_state.manual_lister = example["lister"]
            st.session_state.manual_track_sku = example["track_sku"]
            st.session_state.manual_listing_builder = None
            st.rerun()

        if action_col2.button("Clear manual form"):
            for key, value in manual_defaults.items():
                st.session_state[key] = value
            st.session_state.manual_listing_builder = None
            st.rerun()

        output_mode = st.radio(
            "Build output",
            ["Metadata JSON only", "Imageless CSV only", "Both JSON and CSV"],
            horizontal=True,
            key="manual_output_mode",
        )
        product_name = st.text_input("Product name", key="manual_product_name")
        sku_suffix = st.text_input("SKU suffix", key="manual_sku_suffix").strip().upper()
        main_color = st.text_input("Main color", key="manual_main_color").strip()
        tags = st.text_input("Tags, comma-separated", key="manual_tags").strip()
        tag_count = len([tag.strip() for tag in tags.split(",") if tag.strip()])
        st.caption(f"Tags: {tag_count}")
        if tags and tag_count < MANUAL_MIN_TAG_COUNT:
            st.warning(
                f"Recommended: add at least {MANUAL_MIN_TAG_COUNT} tags. "
                f"You currently have {tag_count}."
            )

        page_titles = st.text_area("Page titles", height=160, key="manual_page_titles")
        page_title_items = _split_manual_items(page_titles, allow_pipe_separator=False)
        st.caption(f"Page titles: {len(page_title_items)} / {manual_expected_count}")
        if page_title_items and len(page_title_items) != manual_expected_count:
            st.warning(f"Paste one page title per line. You have {len(page_title_items)}; expected {manual_expected_count}.")

        if page_title_items:
            with st.expander("Page title length preview", expanded=False):
                try:
                    with st.container(height=260):
                        _render_manual_page_title_lengths(page_title_items)
                except TypeError:
                    _render_manual_page_title_lengths(page_title_items)

        descriptions = st.text_area("Descriptions", height=300, key="manual_descriptions")
        description_items = _split_manual_items(descriptions, allow_pipe_separator=False)
        st.caption(f"Descriptions: {len(description_items)} / {manual_expected_count}")
        if description_items and len(description_items) != manual_expected_count:
            st.warning(f"Paste one description per line. You have {len(description_items)}; expected {manual_expected_count}.")

        lister = st.selectbox("Lister", ["Sal", "Hannan"], key="manual_lister")
        track_sku = st.checkbox("Enable SKU tracking", key="manual_track_sku")
        submit = st.button("Build listing")

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
                metadata_validation = validate_listing_metadata(
                    metadata,
                    label=sku_label,
                    min_tag_count=MANUAL_MIN_TAG_COUNT,
                )
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
                    with st.container(border=True):
                        st.markdown("#### Metadata JSON")
                        st.caption(manual_result["json_filename"])
                        st.download_button(
                            "Download metadata JSON",
                            data=_metadata_to_json_bytes(metadata),
                            file_name=manual_result["json_filename"],
                            mime="application/json",
                        )

                if manual_result["wants_csv"]:
                    df = manual_result["df"]
                    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
                    with st.container(border=True):
                        st.markdown("#### Imageless CSV")
                        st.caption(manual_result["csv_filename"])
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

def _dropbox_folder_has_metadata(dbx: dropbox.Dropbox, folder_path: str) -> bool:
    try:
        entries = dbx.files_list_folder(folder_path).entries
    except Exception:
        return False
    return any(
        isinstance(entry, dropbox.files.FileMetadata)
        and entry.name.lower().endswith(".json")
        for entry in entries
    )

def _normalise_sku(value: str) -> str:
    return str(value or "").strip().upper()

def _uploaded_file_stem(uploaded_file) -> str:
    filename = getattr(uploaded_file, "name", "") or ""
    return os.path.splitext(os.path.basename(filename))[0].strip()

def _metadata_index_from_uploads(uploaded_jsons) -> tuple[dict[str, dict], list[dict]]:
    index = {}
    rows = []

    for uploaded_json in uploaded_jsons or []:
        filename = getattr(uploaded_json, "name", "") or "metadata.json"
        row = {
            "JSON file": filename,
            "Detected SKU": "",
            "Status": "Blocked",
            "Errors": "",
        }

        try:
            metadata = load_metadata_json(uploaded_json)
            sku = _normalise_sku(metadata.get("sku_suffix"))
            issues = _metadata_issues(metadata)[0]
        except Exception as exc:
            rows.append({**row, "Errors": str(exc)})
            continue

        row["Detected SKU"] = sku
        if not sku:
            row["Errors"] = "Missing sku_suffix."
        elif sku in index:
            row["Errors"] = f"Duplicate metadata JSON for SKU {sku}."
        elif issues:
            row["Errors"] = "; ".join(issues)
        else:
            row["Status"] = "Ready"
            index[sku] = {"file": uploaded_json, "metadata": metadata, "filename": filename}

        rows.append(row)

    return index, rows

def _upload_metadata_json_to_dropbox(
    dbx: dropbox.Dropbox,
    folder_path: str,
    metadata: dict,
    overwrite: bool = True,
) -> dict:
    target_path = f"{folder_path.rstrip('/')}/metadata.json"
    exists = _dbx_exists(dbx, target_path)

    if exists and not overwrite:
        return {"uploaded": False, "skipped": True, "path": target_path, "error": ""}

    mode = dropbox.files.WriteMode.overwrite if overwrite else dropbox.files.WriteMode.add
    try:
        dbx.files_upload(_metadata_to_json_bytes(metadata), target_path, mode=mode, mute=True)
    except Exception as exc:
        return {"uploaded": False, "skipped": False, "path": target_path, "error": str(exc)}

    return {"uploaded": True, "skipped": False, "path": target_path, "error": ""}

def _dropbox_join(*parts: str) -> str:
    cleaned = [str(part or "").strip("/") for part in parts if str(part or "").strip("/")]
    return "/" + "/".join(cleaned)

def _ensure_dropbox_folder(dbx: dropbox.Dropbox, path: str) -> None:
    path = _dropbox_join(path)
    current = ""
    for part in [part for part in path.strip("/").split("/") if part]:
        current = f"{current}/{part}"
        if not _dbx_exists(dbx, current):
            dbx.files_create_folder_v2(current)

def _list_dropbox_folder_entries(dbx: dropbox.Dropbox, path: str):
    result = dbx.files_list_folder(path)
    entries = list(result.entries)
    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)
    return entries

def _list_dropbox_note_files(dbx: dropbox.Dropbox, folder_path: str) -> list:
    try:
        entries = _list_dropbox_folder_entries(dbx, folder_path)
    except Exception:
        return []

    return [
        entry for entry in entries
        if isinstance(entry, dropbox.files.FileMetadata)
        and entry.name.lower().endswith((".txt", ".pdf"))
    ]

def _is_design_art_filename(filename: str) -> bool:
    stem, ext = os.path.splitext(str(filename or ""))
    return ext.lower() in {".png", ".jpg", ".jpeg", ".webp"} and not stem.isdigit()

def _list_design_art_files(dbx: dropbox.Dropbox, folder_path: str, folder_name: str):
    try:
        entries = _list_dropbox_folder_entries(dbx, folder_path)
    except Exception:
        return []

    expected_stem = str(folder_name or "").strip()
    art_files = []
    for entry in entries:
        if not isinstance(entry, dropbox.files.FileMetadata):
            continue
        stem, ext = os.path.splitext(entry.name)
        if ext.lower() in {".png", ".jpg", ".jpeg", ".webp"} and not stem.isdigit():
            art_files.append(entry)

    return sorted(
        art_files,
        key=lambda entry: (
            os.path.splitext(entry.name)[0] != expected_stem,
            entry.name.lower(),
        ),
    )

def _download_dropbox_file_bytes(dbx: dropbox.Dropbox, path: str) -> bytes:
    _, response = dbx.files_download(path)
    return response.content

def _decode_note_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")

def move_selected_to_finished(dbx: dropbox.Dropbox, folder: str) -> str:
    folder = str(folder or "").strip().strip("/")
    if not folder:
        raise RuntimeError("No folder selected.")

    source_path = _dropbox_join(DESIGNS_ROOT, folder)
    finished_path = _dropbox_join(DESIGNS_ROOT, FINISHED_DIR_NAME, folder)

    if _dbx_exists(dbx, finished_path):
        return finished_path

    if not _dbx_exists(dbx, source_path):
        raise RuntimeError(f"Folder not found at {source_path} or {finished_path}")

    return move_to_finished(dbx, DESIGNS_ROOT, folder, finished_dir=FINISHED_DIR_NAME)

def clean_and_archive_to_completed(dbx: dropbox.Dropbox, folder: str) -> tuple[int, str]:
    finished_path = move_selected_to_finished(dbx, folder)

    pat = re.compile(r"^([1-9]\d{0,2})\.(png|jpg|jpeg|webp)$", re.IGNORECASE)
    deleted = 0
    entries = _list_dropbox_folder_entries(dbx, finished_path)
    for e in entries:
        if isinstance(e, dropbox.files.FileMetadata):
            m = pat.match(e.name)
            if not m:
                continue
            num = int(m.group(1))
            if 1 <= num <= 127:
                dbx.files_delete_v2(f"{finished_path}/{e.name}")
                deleted += 1

    _ensure_dropbox_folder(dbx, COMPLETED_ROOT)
    dest = _dropbox_join(COMPLETED_ROOT, folder)
    res = dbx.files_move_v2(finished_path, dest, autorename=True)
    return deleted, res.metadata.path_display


# =========================
# Tab 2: Pipeline Intake
# =========================
if not LIVE_DEPLOYMENT_MODE:
    with tab_pipeline:
        render_section_header(
            "Pipeline Intake",
            "Digest Canva mockup ZIPs into local staged or ready folders.",
        )

        pipeline_root = st.text_input(
            "Pipeline root folder",
            value="pipeline_data",
            key="pipeline_root",
            help="Local folder where staged, ready, active, and finished designs are stored.",
        )

        uploaded_pipeline_zip = st.file_uploader(
            "Mockup ZIP",
            type=["zip"],
            key="pipeline_mockup_zip",
            help="Upload one Canva mockup ZIP for one design/SKU.",
        )

        uploaded_pipeline_json = st.file_uploader(
            "Metadata JSON optional",
            type=["json"],
            key="pipeline_metadata_json",
            help="Upload metadata JSON now, or leave empty and add it later.",
        )

        overwrite_pipeline_design = st.checkbox(
            "Overwrite existing design folder",
            value=True,
            key="pipeline_overwrite_design",
        )

        if st.button("Digest ZIP", key="pipeline_digest_zip_btn"):
            if uploaded_pipeline_zip is None:
                st.error("Upload a mockup ZIP first.")
            else:
                report = digest_mockup_zip_to_pipeline(
                    uploaded_pipeline_zip,
                    uploaded_json=uploaded_pipeline_json,
                    pipeline_root=pipeline_root,
                    expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                    overwrite=overwrite_pipeline_design,
                )

                if report.get("ready"):
                    st.success("Design is ready.")
                else:
                    st.warning("Design staged but not ready.")

                col1, col2, col3 = st.columns(3)
                col1.metric("SKU", report.get("sku") or "Missing")
                col2.metric("Images", report.get("image_count", 0))
                col3.metric("Status", report.get("status", "unknown"))

                st.write("Design folder:", report.get("design_folder"))
                st.write("Mockups folder:", report.get("mockups_folder"))
                st.write("Metadata path:", report.get("metadata_path"))

                issues = report.get("issues", [])
                warnings = report.get("warnings", [])

                if issues:
                    st.error("Issues")
                    for issue in issues:
                        st.write(f"- {issue}")

                if warnings:
                    st.warning("Warnings")
                    for warning in warnings:
                        st.write(f"- {warning}")

                image_files = report.get("image_files", [])
                if image_files:
                    st.write("First mapped images")
                    st.code("\n".join(image_files[:10]))

        st.divider()

        render_section_header("Pipeline folders")

        pipeline_scan = scan_pipeline_folders(
            pipeline_root=pipeline_root,
            expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
        )

        staged_rows = pipeline_scan.get("staged", [])
        ready_rows = pipeline_scan.get("ready", [])

        col_staged, col_ready = st.columns(2)
        col_staged.metric("Staged", len(staged_rows))
        col_ready.metric("Ready", len(ready_rows))

        st.markdown("#### Staged designs")
        if staged_rows:
            st.dataframe(pd.DataFrame(staged_rows), width="stretch")
        else:
            st.info("No staged designs yet.")

        st.markdown("#### Add metadata to staged design")

        if staged_rows:
            staged_skus = [row["SKU"] for row in staged_rows]
            selected_staged_sku = st.selectbox(
                "Select staged design",
                staged_skus,
                key="pipeline_attach_json_sku",
            )

            uploaded_late_json = st.file_uploader(
                "Metadata JSON for selected staged design",
                type=["json"],
                key="pipeline_attach_metadata_json",
            )

            overwrite_late_metadata = st.checkbox(
                "Overwrite metadata if it already exists",
                value=True,
                key="pipeline_attach_overwrite_metadata",
            )

            if st.button("Attach JSON and Revalidate", key="pipeline_attach_json_btn"):
                if uploaded_late_json is None:
                    st.error("Upload a metadata JSON first.")
                else:
                    attach_report = attach_metadata_to_pipeline_design(
                        selected_staged_sku,
                        uploaded_late_json,
                        pipeline_root=pipeline_root,
                        expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                        overwrite=overwrite_late_metadata,
                    )

                    if attach_report.get("ready"):
                        st.success("Metadata attached. Design moved to ready.")
                    else:
                        st.warning("Metadata attached, but design is still not ready.")

                    col1, col2, col3 = st.columns(3)
                    col1.metric("SKU", attach_report.get("sku") or "Missing")
                    col2.metric("Images", attach_report.get("image_count", 0))
                    col3.metric("Status", attach_report.get("status", "unknown"))

                    st.write("Design folder:", attach_report.get("design_folder"))
                    st.write("Metadata path:", attach_report.get("metadata_path"))

                    issues = attach_report.get("issues", [])
                    warnings = attach_report.get("warnings", [])

                    if issues:
                        st.error("Issues")
                        for issue in issues:
                            st.write(f"- {issue}")

                    if warnings:
                        st.warning("Warnings")
                        for warning in warnings:
                            st.write(f"- {warning}")
        else:
            st.info("No staged designs available for metadata attachment.")

        st.markdown("#### Replace mockups for staged design")

        if staged_rows:
            staged_skus_for_mockups = [row["SKU"] for row in staged_rows]
            selected_mockup_replace_sku = st.selectbox(
                "Select staged design to replace mockups",
                staged_skus_for_mockups,
                key="pipeline_replace_mockups_sku",
            )

            uploaded_replacement_zip = st.file_uploader(
                "Replacement mockup ZIP",
                type=["zip"],
                key="pipeline_replacement_mockup_zip",
            )

            overwrite_replacement_mockups = st.checkbox(
                "Overwrite existing mockups",
                value=True,
                key="pipeline_replace_mockups_overwrite",
            )

            if st.button("Replace Mockups and Revalidate", key="pipeline_replace_mockups_btn"):
                if uploaded_replacement_zip is None:
                    st.error("Upload a replacement mockup ZIP first.")
                else:
                    replace_report = replace_pipeline_design_mockups(
                        selected_mockup_replace_sku,
                        uploaded_replacement_zip,
                        pipeline_root=pipeline_root,
                        expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                        overwrite=overwrite_replacement_mockups,
                    )

                    if replace_report.get("ready"):
                        st.success("Mockups replaced. Design moved to ready.")
                    else:
                        st.warning("Mockups replaced, but design is still not ready.")

                    col1, col2, col3 = st.columns(3)
                    col1.metric("SKU", replace_report.get("sku") or "Missing")
                    col2.metric("Images", replace_report.get("image_count", 0))
                    col3.metric("Status", replace_report.get("status", "unknown"))

                    st.write("Design folder:", replace_report.get("design_folder"))
                    st.write("Mockups folder:", replace_report.get("mockups_folder"))

                    issues = replace_report.get("issues", [])
                    warnings = replace_report.get("warnings", [])

                    if issues:
                        st.error("Issues")
                        for issue in issues:
                            st.write(f"- {issue}")

                    if warnings:
                        st.warning("Warnings")
                        for warning in warnings:
                            st.write(f"- {warning}")

                    image_files = replace_report.get("image_files", [])
                    if image_files:
                        st.write("First mapped images")
                        st.code("\n".join(image_files[:10]))
        else:
            st.info("No staged designs available for mockup replacement.")

        st.markdown("#### Ready designs")
        if ready_rows:
            st.dataframe(pd.DataFrame(ready_rows), width="stretch")
        else:
            st.info("No ready designs yet.")

        st.markdown("#### Host ready design images on Dropbox")

        if ready_rows:
            ready_skus = [row["SKU"] for row in ready_rows]
            selected_ready_sku = st.selectbox(
                "Select ready design",
                ready_skus,
                key="pipeline_dropbox_ready_sku",
            )

            dropbox_temp_root = st.text_input(
                "Dropbox temp root folder",
                value="/sku-generator-temp/active",
                key="pipeline_dropbox_temp_root",
                help="The app will create this folder automatically if it does not exist.",
            )

            overwrite_dropbox_temp = st.checkbox(
                "Overwrite existing Dropbox temp files",
                value=True,
                key="pipeline_dropbox_overwrite",
            )

            if st.button("Upload Images to Dropbox Temp Hosting", key="pipeline_upload_dropbox_temp_btn"):
                try:
                    dbx = get_dropbox_client()
                except Exception as exc:
                    st.error(f"Could not connect to Dropbox: {exc}")
                else:
                    with st.status("Uploading ready design images to Dropbox...", expanded=True) as s:
                        upload_report = upload_ready_design_to_dropbox_temp(
                            dbx,
                            selected_ready_sku,
                            pipeline_root=pipeline_root,
                            dropbox_root=dropbox_temp_root,
                            expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                            overwrite=overwrite_dropbox_temp,
                        )

                        s.write(f"Dropbox folder: {upload_report.get('dropbox_folder')}")
                        s.write(f"Uploaded: {upload_report.get('uploaded', 0)}")
                        s.write(f"Linked: {upload_report.get('linked', 0)}")
                        s.write(f"Failed: {upload_report.get('failed', 0)}")

                        if upload_report.get("ready"):
                            s.update(label="Dropbox temp hosting complete.")
                            st.success("Images uploaded and linked. Design moved to active.")
                        else:
                            s.update(label="Dropbox temp hosting completed with issues.")
                            st.warning("Dropbox hosting did not fully complete.")

                    col1, col2, col3 = st.columns(3)
                    col1.metric("SKU", upload_report.get("sku") or "Missing")
                    col2.metric("Uploaded", upload_report.get("uploaded", 0))
                    col3.metric("Linked", upload_report.get("linked", 0))

                    st.write("Design folder:", upload_report.get("design_folder"))
                    st.write("Dropbox folder:", upload_report.get("dropbox_folder"))
                    st.write("Image links path:", upload_report.get("image_links_path"))

                    issues = upload_report.get("issues", [])
                    warnings = upload_report.get("warnings", [])

                    if issues:
                        st.error("Issues")
                        for issue in issues:
                            st.write(f"- {issue}")

                    if warnings:
                        st.warning("Warnings")
                        for warning in warnings:
                            st.write(f"- {warning}")

                    image_links = upload_report.get("image_links", {})
                    if image_links:
                        st.write("First image links")
                        sample_lines = [
                            f"{key}: {value}"
                            for key, value in list(image_links.items())[:5]
                        ]
                        st.code("\n".join(sample_lines))
        else:
            st.info("No ready designs available for Dropbox temp hosting.")


        st.divider()

        render_section_header("Active designs")

        pipeline_scan_after_hosting = scan_pipeline_folders(
            pipeline_root=pipeline_root,
            expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
        )
        active_rows = pipeline_scan_after_hosting.get("active", [])

        if active_rows:
            st.dataframe(pd.DataFrame(active_rows), width="stretch")

            active_skus = [row["SKU"] for row in active_rows]
            selected_active_sku = st.selectbox(
                "Select active design for CSV generation",
                active_skus,
                key="pipeline_csv_active_sku",
            )

            active_folder_by_sku = {
                row["SKU"]: row["Folder"]
                for row in active_rows
            }

            if st.button("Generate CSV from Active Design", key="pipeline_generate_csv_btn"):
                try:
                    csv_result = _build_pipeline_csv_for_active_design(
                        active_folder_by_sku[selected_active_sku],
                        excluded_colors=excluded_colors,
                        excluded_garments=excluded_garments,
                    )
                except Exception as exc:
                    st.error(f"Could not generate CSV: {exc}")
                else:
                    st.success("CSV generated successfully.")
                    st.metric("Rows", csv_result["rows"])
                    st.write("CSV path:", csv_result["csv_path"])

                    csv_bytes = csv_result["df"].to_csv(index=False).encode("utf-8-sig")
                    st.download_button(
                        "Download Shopify CSV",
                        data=csv_bytes,
                        file_name=f"{csv_result['sku']}.csv",
                        mime="text/csv",
                        key="pipeline_download_generated_csv_btn",
                    )

                    with st.expander("Preview generated rows", expanded=False):
                        st.dataframe(csv_result["df"].head(20), width="stretch")
        else:
            st.info("No active designs yet. Upload a ready design to Dropbox temp hosting first.")


# =========================
# Tab 2: Auto from Dropbox
# =========================
with tab_auto:
    if LIVE_DEPLOYMENT_MODE:
        render_section_header(
            "Mockup ZIP intake",
            "Complete Dropbox design folders, then download ready listings.",
        )

        mockup_source = "Dropbox"
        if not DESIGNS_ROOT:
            st.warning("Set `FOLDER_PATH_Design` in dpbox.env to your `/designs` root.")
            st.stop()

        dbx = get_dropbox_client()

        cached_ready_folders = st.session_state.get("ready_folders", [])
        cached_not_ready_folders = st.session_state.get("not_ready_folders", [])
        if not cached_ready_folders and not cached_not_ready_folders:
            st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                dbx, DESIGNS_ROOT, mockup_source=mockup_source
            )

        ready_folders = st.session_state.get("ready_folders", [])
        not_ready_info = st.session_state.get("not_ready_folders", [])

        with st.container(border=True):
            render_section_header("Folder readiness")
            live_deep_check_images = st.checkbox(
                "Deep-check image files",
                value=False,
                key="live_deep_check_images",
                help="Slower: downloads numbered Dropbox images to catch corrupt files.",
            )
            if st.button("Refresh folder analysis", key="live_refresh_folder_analysis_btn"):
                st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                    dbx,
                    DESIGNS_ROOT,
                    mockup_source=mockup_source,
                    validate_dropbox_images=live_deep_check_images,
                )
                ready_folders = st.session_state.get("ready_folders", [])
                not_ready_info = st.session_state.get("not_ready_folders", [])

            col_ready, col_not_ready = st.columns(2)
            col_ready.metric("Ready folders", len(ready_folders))
            col_not_ready.metric("Not ready", len(not_ready_info))

            if not_ready_info:
                st.markdown("#### Not ready folders")
                st.data_editor(pd.DataFrame(not_ready_info), disabled=True, width="stretch")

                st.markdown("#### Complete a not-ready folder")
                not_ready_folder_names = [
                    row["Folder"] for row in not_ready_info
                    if row.get("Folder") and row.get("Folder") != "N/A"
                ]
                selected_not_ready_folder = st.selectbox(
                    "Select folder to complete",
                    not_ready_folder_names or [""],
                    key="live_repair_not_ready_folder_select",
                )
                selected_not_ready_path = f"{DESIGNS_ROOT}/{selected_not_ready_folder}"
                selected_note_files = _list_dropbox_note_files(dbx, selected_not_ready_path) if selected_not_ready_folder else []
                selected_art_files = _list_design_art_files(dbx, selected_not_ready_path, selected_not_ready_folder) if selected_not_ready_folder else []

                design_col, notes_col = st.columns([1.2, 1.4])
                with design_col:
                    st.markdown("##### Design")
                    if selected_art_files:
                        st.metric("Files", len(selected_art_files))
                        for selected_art_file in selected_art_files:
                            art_path = f"{selected_not_ready_path}/{selected_art_file.name}"
                            try:
                                art_bytes = _download_dropbox_file_bytes(dbx, art_path)
                                st.image(art_bytes, caption=selected_art_file.name, width="stretch")
                                art_ext = os.path.splitext(selected_art_file.name)[1].lower().lstrip(".")
                                art_mime = f"image/{'jpeg' if art_ext == 'jpg' else art_ext or 'png'}"
                                st.download_button(
                                    f"Download {selected_art_file.name}",
                                    data=art_bytes,
                                    file_name=selected_art_file.name,
                                    mime=art_mime,
                                    key=f"live_download_art_{selected_not_ready_folder}_{selected_art_file.name}",
                                )
                            except Exception as exc:
                                st.warning(f"Could not preview {selected_art_file.name}: {exc}")
                    else:
                        st.caption("No design image found.")

                with notes_col:
                    st.markdown("##### Notes")
                    st.metric("Files", len(selected_note_files))
                    if selected_note_files:
                        for note_index, note_file in enumerate(selected_note_files, start=1):
                            note_path = f"{selected_not_ready_path}/{note_file.name}"
                            try:
                                note_bytes = _download_dropbox_file_bytes(dbx, note_path)
                            except Exception as exc:
                                st.warning(f"Could not read {note_file.name}: {exc}")
                                continue

                            note_mime = "application/pdf" if note_file.name.lower().endswith(".pdf") else "text/plain"
                            st.download_button(
                                f"Download {note_file.name}",
                                data=note_bytes,
                                file_name=note_file.name,
                                mime=note_mime,
                                key=f"live_repair_note_download_{note_index}_{note_file.name}",
                            )
                            if note_file.name.lower().endswith(".txt"):
                                with st.expander(f"Read {note_file.name}", expanded=False):
                                    st.text(_decode_note_text(note_bytes))
                    else:
                        st.caption("No notes found in the selected folder.")

                repair_col1, repair_col2 = st.columns(2)
                with repair_col1:
                    repair_metadata_json = st.file_uploader(
                        "Metadata JSON for selected folder",
                        type=["json"],
                        key="live_repair_not_ready_metadata_json",
                    )
                with repair_col2:
                    repair_mockup_zip = st.file_uploader(
                        "Mockup ZIP for selected folder",
                        type=["zip"],
                        key="live_repair_not_ready_mockup_zip",
                    )

                repair_overwrite_metadata = st.checkbox(
                    "Overwrite selected folder metadata JSON",
                    value=True,
                    key="live_repair_not_ready_overwrite_metadata",
                )
                repair_overwrite_mockups = st.checkbox(
                    "Overwrite selected folder numbered images",
                    value=True,
                    key="live_repair_not_ready_overwrite_mockups",
                )

                repair_preview_rows = []
                repair_metadata = None
                repair_metadata_blocked = False
                if repair_metadata_json is not None:
                    try:
                        repair_metadata = load_metadata_json(repair_metadata_json)
                        metadata_errors, descriptions_count_label = _metadata_issues(repair_metadata)
                        repair_preview_rows.append({
                            "File": getattr(repair_metadata_json, "name", "metadata.json"),
                            "Type": "Metadata JSON",
                            "Detected SKU": _normalise_sku(repair_metadata.get("sku_suffix")),
                            "Count": descriptions_count_label,
                            "Status": "Blocked" if metadata_errors else "Ready",
                            "Issues": "; ".join(metadata_errors),
                        })
                        repair_metadata_blocked = bool(metadata_errors)
                    except Exception as exc:
                        repair_preview_rows.append({
                            "File": getattr(repair_metadata_json, "name", "metadata.json"),
                            "Type": "Metadata JSON",
                            "Detected SKU": "",
                            "Count": "",
                            "Status": "Blocked",
                            "Issues": str(exc),
                        })
                        repair_metadata_blocked = True

                repair_images = None
                repair_zip_blocked = False
                if repair_mockup_zip is not None:
                    inspection = inspect_mockup_zip(
                        repair_mockup_zip,
                        expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                    )
                    zip_errors = [
                        error for error in inspection["errors"]
                        if "Could not detect SKU from ZIP filename" not in error
                    ]
                    zip_warnings = list(inspection["warnings"])
                    try:
                        repair_images = extract_mockup_images(repair_mockup_zip)
                    except Exception:
                        repair_images = None
                    repair_preview_rows.append({
                        "File": inspection["filename"],
                        "Type": "Mockup ZIP",
                        "Detected SKU": inspection["sku"],
                        "Count": f"{inspection['images_found']} / {MOCKUP_ZIP_EXPECTED_IMAGES}",
                        "Status": "Blocked" if zip_errors else "Ready with warnings" if zip_warnings else "Ready",
                        "Issues": "; ".join(zip_errors + zip_warnings),
                    })
                    repair_zip_blocked = bool(zip_errors)

                if repair_preview_rows:
                    st.data_editor(
                        pd.DataFrame(repair_preview_rows),
                        disabled=True,
                        width="stretch",
                        key="live_repair_not_ready_preview_table",
                    )

                repair_disabled = (
                    not selected_not_ready_folder
                    or (repair_metadata_json is None and repair_mockup_zip is None)
                    or repair_metadata_blocked
                    or repair_zip_blocked
                )
                if st.button(
                    "Add files to selected folder",
                    disabled=repair_disabled,
                    key="live_repair_not_ready_upload_btn",
                ):
                    upload_rows = []
                    with st.status(f"Updating {selected_not_ready_folder}...", expanded=True) as s:
                        if repair_metadata is not None:
                            metadata_result = _upload_metadata_json_to_dropbox(
                                dbx,
                                selected_not_ready_path,
                                repair_metadata,
                                overwrite=repair_overwrite_metadata,
                            )
                            metadata_status = (
                                f"Failed: {metadata_result['error']}"
                                if metadata_result["error"]
                                else "Skipped existing"
                                if metadata_result["skipped"]
                                else "Uploaded"
                            )
                            upload_rows.append({"Item": "Metadata JSON", "Uploaded": metadata_status})
                            s.write(f"Metadata JSON: {metadata_status}")

                        if repair_images is not None:
                            image_result = upload_mockup_images_to_dropbox(
                                dbx,
                                selected_not_ready_path,
                                repair_images,
                                overwrite=repair_overwrite_mockups,
                                expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                            )
                            upload_rows.append({
                                "Item": "Mockup ZIP",
                                "Uploaded": image_result["uploaded"],
                                "Skipped": image_result["skipped"],
                                "Failed": image_result["failed"],
                                "Truncated": image_result["truncated"],
                                "Details": "; ".join(
                                    f"{item['target']}: {item['error']}"
                                    for item in image_result.get("failed_files", [])[:5]
                                ),
                            })
                            s.write(
                                f"Mockups: uploaded {image_result['uploaded']}, "
                                f"skipped {image_result['skipped']}, failed {image_result['failed']}."
                            )

                        st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                            dbx, DESIGNS_ROOT, mockup_source=mockup_source
                        )
                        ready_folders = st.session_state.get("ready_folders", [])
                        not_ready_info = st.session_state.get("not_ready_folders", [])
                        s.update(label="Folder updated. Readiness refreshed.")

                    st.dataframe(pd.DataFrame(upload_rows), width="stretch")
            else:
                st.success("All folders are ready.")

        with st.container(border=True):
            render_section_header("Bulk ZIP and JSON upload")
            st.caption("Upload files named after the Dropbox folder, for example `BBRPWLULE.zip` and `BBRPWLULE.json`.")
            mpn_bulk_zip_uploads = st.file_uploader(
                "Upload MPN-named ZIP files",
                type=["zip"],
                accept_multiple_files=True,
                key="live_mpn_mockup_zip_uploader",
            )
            mpn_bulk_json_uploads = st.file_uploader(
                "Upload MPN-named JSON files",
                type=["json"],
                accept_multiple_files=True,
                key="live_mpn_metadata_json_uploader",
            )
            mpn_overwrite_mockups = st.checkbox(
                "Overwrite existing numbered images",
                value=False,
                key="live_mpn_mockup_zip_overwrite",
            )
            mpn_overwrite_metadata_json = st.checkbox(
                "Overwrite existing metadata JSON",
                value=True,
                key="live_mpn_metadata_json_overwrite",
            )

            mpn_zip_map = {}
            mpn_json_map = {}
            mpn_duplicate_warnings = []
            for uploaded_zip in mpn_bulk_zip_uploads or []:
                mpn = _uploaded_file_stem(uploaded_zip)
                normalised_mpn = _normalise_sku(mpn)
                if not normalised_mpn:
                    mpn_duplicate_warnings.append(f"{getattr(uploaded_zip, 'name', 'ZIP')}: missing MPN filename.")
                elif normalised_mpn in mpn_zip_map:
                    mpn_duplicate_warnings.append(f"Duplicate ZIP for MPN {mpn}.")
                else:
                    mpn_zip_map[normalised_mpn] = {"mpn": mpn, "file": uploaded_zip}

            for uploaded_json in mpn_bulk_json_uploads or []:
                mpn = _uploaded_file_stem(uploaded_json)
                normalised_mpn = _normalise_sku(mpn)
                if not normalised_mpn:
                    mpn_duplicate_warnings.append(f"{getattr(uploaded_json, 'name', 'JSON')}: missing MPN filename.")
                elif normalised_mpn in mpn_json_map:
                    mpn_duplicate_warnings.append(f"Duplicate JSON for MPN {mpn}.")
                else:
                    mpn_json_map[normalised_mpn] = {"mpn": mpn, "file": uploaded_json}

            if mpn_duplicate_warnings:
                st.warning(" ".join(mpn_duplicate_warnings))

            mpn_bulk_rows = []
            for normalised_mpn in sorted(set(mpn_zip_map) | set(mpn_json_map)):
                zip_entry = mpn_zip_map.get(normalised_mpn)
                json_entry = mpn_json_map.get(normalised_mpn)
                mpn = (zip_entry or json_entry)["mpn"]
                target_folder_path = f"{DESIGNS_ROOT}/{mpn}"
                target_exists = _dbx_exists(dbx, target_folder_path)
                errors = []
                warnings = []
                metadata = None
                json_sku = ""
                zip_images_found = "No ZIP"

                if not target_exists:
                    errors.append(f"Target folder not found: {target_folder_path}")

                if zip_entry:
                    inspection = inspect_mockup_zip(
                        zip_entry["file"],
                        expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                    )
                    zip_images_found = f"{inspection['images_found']} / {MOCKUP_ZIP_EXPECTED_IMAGES}"
                    errors.extend(
                        error for error in inspection["errors"]
                        if "Could not detect SKU from ZIP filename" not in error
                    )
                    warnings.extend(inspection["warnings"])

                if json_entry:
                    try:
                        metadata = load_metadata_json(json_entry["file"])
                        json_sku = str(metadata.get("sku_suffix", "") or "")
                        metadata_errors, _ = _metadata_issues(metadata)
                        errors.extend(metadata_errors)
                        if json_sku and _normalise_sku(json_sku) != normalised_mpn:
                            warnings.append(f"JSON sku_suffix is {json_sku}; target folder is {mpn}.")
                    except Exception as exc:
                        errors.append(f"JSON error: {exc}")

                mpn_bulk_rows.append({
                    "MPN": mpn,
                    "Target folder exists": target_exists,
                    "ZIP file": getattr(zip_entry["file"], "name", "") if zip_entry else "",
                    "Images found": zip_images_found,
                    "JSON file": getattr(json_entry["file"], "name", "") if json_entry else "",
                    "JSON SKU": json_sku,
                    "Status": "Blocked" if errors else "Ready with warnings" if warnings else "Ready",
                    "Issues": "; ".join(errors),
                    "Warnings": "; ".join(warnings),
                    "_target_folder_path": target_folder_path,
                    "_zip_file": zip_entry["file"] if zip_entry else None,
                    "_metadata": metadata,
                    "_blocked": bool(errors),
                })

            if mpn_bulk_rows:
                st.data_editor(
                    pd.DataFrame([
                        {key: value for key, value in row.items() if not key.startswith("_")}
                        for row in mpn_bulk_rows
                    ]),
                    disabled=True,
                    width="stretch",
                    key="live_mpn_bulk_preview_table",
                )

            if st.button(
                "Upload ZIP and JSON to Dropbox",
                disabled=not mpn_bulk_rows,
                key="live_mpn_bulk_upload_btn",
            ):
                successful_upload = False
                upload_rows = []
                with st.status("Uploading ZIP and JSON files to Dropbox...", expanded=True) as s:
                    for row in mpn_bulk_rows:
                        mpn = row["MPN"]
                        if row["_blocked"]:
                            s.write(f"{mpn}: blocked - {row['Issues']}")
                            upload_rows.append({
                                "MPN": mpn,
                                "Metadata": "Blocked",
                                "Uploaded": 0,
                                "Skipped": 0,
                                "Failed": 0,
                                "Truncated": 0,
                                "Status": "Blocked",
                                "Details": row["Issues"],
                            })
                            continue

                        metadata_status = "No change"
                        image_result = {"uploaded": 0, "skipped": 0, "failed": 0, "truncated": 0, "failed_files": []}
                        try:
                            if row.get("_metadata"):
                                metadata_result = _upload_metadata_json_to_dropbox(
                                    dbx,
                                    row["_target_folder_path"],
                                    row["_metadata"],
                                    overwrite=mpn_overwrite_metadata_json,
                                )
                                if metadata_result["error"]:
                                    metadata_status = f"Failed: {metadata_result['error']}"
                                elif metadata_result["skipped"]:
                                    metadata_status = "Skipped existing"
                                else:
                                    metadata_status = "Uploaded"

                            if row.get("_zip_file"):
                                images = extract_mockup_images(row["_zip_file"])
                                image_result = upload_mockup_images_to_dropbox(
                                    dbx,
                                    row["_target_folder_path"],
                                    images,
                                    overwrite=mpn_overwrite_mockups,
                                    expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                                )

                            successful_upload = (
                                successful_upload
                                or image_result["uploaded"] > 0
                                or metadata_status == "Uploaded"
                            )
                            upload_rows.append({
                                "MPN": mpn,
                                "Metadata": metadata_status,
                                "Uploaded": image_result["uploaded"],
                                "Skipped": image_result["skipped"],
                                "Failed": image_result["failed"],
                                "Truncated": image_result["truncated"],
                                "Details": "; ".join(
                                    f"{item['target']}: {item['error']}"
                                    for item in image_result.get("failed_files", [])[:5]
                                ),
                                "Status": (
                                    "Completed with failures"
                                    if image_result["failed"] or metadata_status.startswith("Failed:")
                                    else "Done"
                                ),
                            })
                            s.write(
                                f"{mpn}: metadata {metadata_status.lower()}, uploaded {image_result['uploaded']}, "
                                f"skipped {image_result['skipped']}, failed {image_result['failed']}."
                            )
                        except Exception as exc:
                            upload_rows.append({
                                "MPN": mpn,
                                "Metadata": metadata_status,
                                "Uploaded": image_result["uploaded"],
                                "Skipped": image_result["skipped"],
                                "Failed": image_result["failed"] + 1,
                                "Truncated": image_result["truncated"],
                                "Status": str(exc),
                                "Details": "",
                            })
                            s.write(f"{mpn}: failed - {exc}")

                    if successful_upload:
                        st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                            dbx, DESIGNS_ROOT, mockup_source=mockup_source
                        )
                        ready_folders = st.session_state.get("ready_folders", [])
                        not_ready_info = st.session_state.get("not_ready_folders", [])
                        s.update(label="ZIP and JSON upload complete. Folder analysis refreshed.")
                    else:
                        s.update(label="ZIP and JSON upload finished. No new files were uploaded.")

                st.dataframe(pd.DataFrame(upload_rows), width="stretch")

        with st.container(border=True):
            render_section_header("Download ready listings")
            if ready_folders:
                st.dataframe(pd.DataFrame({"Ready folders": ready_folders}), width="stretch")
                download_targets = st.multiselect(
                    "Ready folders to include",
                    options=ready_folders,
                    default=ready_folders,
                    key="live_download_targets",
                )
            else:
                download_targets = []
                st.info("No ready folders available yet.")

            download_mode = st.radio(
                "Download mode",
                ["One combined CSV", "Batch CSV parts"],
                horizontal=True,
                key="live_download_mode",
            )
            build_download_disabled = not download_targets
            if st.button(
                "Build download files",
                disabled=build_download_disabled,
                key="live_build_download_files_btn",
            ):
                build_start = time.perf_counter()
                st.session_state.batch_validation = None
                st.session_state.pop("live_listing_csv_files", None)
                dfs = []
                failed_builds = []
                validation_parts = []

                with st.status("Building ready listing CSV files...", expanded=True) as s:
                    for fname in download_targets:
                        try:
                            meta_i = download_metadata(dbx, f"{DESIGNS_ROOT}/{fname}")
                            metadata_validation = _validate_metadata_for_listing(meta_i, label=fname)
                            validation_parts.append(metadata_validation)
                            if has_validation_errors(metadata_validation):
                                failed_builds.append((fname, _format_validation_errors(metadata_validation)))
                                continue

                            df_i, _, missing = build_design_dataframe(
                                dbx,
                                fname,
                                excluded_colors=excluded_colors,
                                excluded_garments=excluded_garments,
                                mockup_source=mockup_source,
                                metadata=meta_i,
                            )
                            if missing:
                                s.write(f"{fname}: missing image links {missing[:10]}{'...' if len(missing) > 10 else ''}")
                            validation_parts.append(validate_shopify_dataframe(df_i, label=fname))
                            dfs.append(df_i)
                            s.write(f"{fname}: ready")
                        except Exception as exc:
                            failed_builds.append((fname, str(exc)))
                            s.write(f"{fname}: failed - {exc}")

                    combined_validation = combine_validation_results(*validation_parts) if validation_parts else None
                    st.session_state.batch_validation = combined_validation

                    if combined_validation and has_validation_errors(combined_validation):
                        s.update(label="CSV build blocked by listing safety errors.")
                    elif not dfs:
                        s.update(label="No CSV files were built.")
                    else:
                        all_df = pd.concat(dfs, ignore_index=True)
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        if download_mode == "One combined CSV":
                            csv_bytes = all_df.to_csv(index=False).encode("utf-8-sig")
                            files = [(f"READY_LISTINGS_{ts}.csv", csv_bytes)]
                        else:
                            chunks = _split_df_by_limits(all_df)
                            files = [
                                (f"READY_LISTINGS_{ts}_part{i}.csv", cdf.to_csv(index=False).encode("utf-8-sig"))
                                for i, cdf in enumerate(chunks, start=1)
                            ]
                        _stash_downloads("live_listing_csv_files", files)
                        s.update(label=f"Built {len(files)} download file(s).")

                for fname, reason in failed_builds:
                    st.warning(f"{fname}: {reason}")
                st.info(f"CSV build finished in {fmt_secs(time.perf_counter() - build_start)}")

            if st.session_state.get("live_listing_csv_files"):
                _render_downloads("live_listing_csv_files", "Ready listing CSV file(s)", zip_name_prefix="READY_LISTINGS")
            else:
                st.caption("Build download files first.")

            if st.session_state.get("batch_validation"):
                _render_listing_safety_checks(st.session_state.batch_validation)

        with st.container(border=True):
            render_section_header("Finish processed folders")
            if ready_folders:
                finish_targets = st.multiselect(
                    "Ready folders to finish",
                    options=ready_folders,
                    default=ready_folders,
                    key="live_finish_targets",
                )
            else:
                finish_targets = []
                st.caption("No ready folders available to finish.")

            finish_disabled = not finish_targets
            finish_col1, finish_col2 = st.columns(2)
            if finish_col1.button(
                "Move selected to /finished",
                disabled=finish_disabled,
                key="live_move_finished_btn",
            ):
                with st.status("Moving selected folders to /finished...", expanded=True) as s:
                    ok = 0
                    for fname in finish_targets:
                        try:
                            dest = move_selected_to_finished(dbx, fname)
                            s.write(f"{fname}: moved to {dest}")
                            ok += 1
                        except Exception as exc:
                            s.write(f"{fname}: failed - {exc}")
                    st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                        dbx, DESIGNS_ROOT, mockup_source=mockup_source
                    )
                    s.update(label=f"Done. {ok}/{len(finish_targets)} moved.")

            if finish_col2.button(
                "Delete numbered images and archive",
                disabled=finish_disabled,
                key="live_clean_archive_btn",
            ):
                with st.status("Deleting numbered images and archiving selected folders...", expanded=True) as s:
                    ok = 0
                    for fname in finish_targets:
                        try:
                            deleted, dest = clean_and_archive_to_completed(dbx, fname)
                            s.write(f"{fname}: deleted {deleted} numbered images and archived to {dest}")
                            ok += 1
                        except Exception as exc:
                            s.write(f"{fname}: failed - {exc}")
                    st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                        dbx, DESIGNS_ROOT, mockup_source=mockup_source
                    )
                    s.update(label=f"Done. {ok}/{len(finish_targets)} archived.")

        st.stop()

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

                st.markdown("#### Complete a not-ready folder")
                not_ready_folder_names = [row["Folder"] for row in not_ready_info if row.get("Folder") and row.get("Folder") != "N/A"]
                selected_not_ready_folder = st.selectbox(
                    "Select folder to complete",
                    not_ready_folder_names or [""],
                    key="repair_not_ready_folder_select",
                )
                if not not_ready_folder_names:
                    st.caption("No selectable not-ready folders found.")
                selected_not_ready_path = f"{DESIGNS_ROOT}/{selected_not_ready_folder}"
                selected_note_files = _list_dropbox_note_files(dbx, selected_not_ready_path) if selected_not_ready_folder else []
                selected_art_files = _list_design_art_files(dbx, selected_not_ready_path, selected_not_ready_folder) if selected_not_ready_folder else []

                design_col, notes_col = st.columns([1.2, 1.4])
                with design_col:
                    st.markdown("##### Design")
                    if selected_art_files:
                        st.metric("Files", len(selected_art_files))
                        for selected_art_file in selected_art_files:
                            art_path = f"{selected_not_ready_path}/{selected_art_file.name}"
                            try:
                                art_bytes = _download_dropbox_file_bytes(dbx, art_path)
                                st.image(art_bytes, caption=selected_art_file.name, width="stretch")
                                art_ext = os.path.splitext(selected_art_file.name)[1].lower().lstrip(".")
                                art_mime = f"image/{'jpeg' if art_ext == 'jpg' else art_ext or 'png'}"
                                st.download_button(
                                    f"Download {selected_art_file.name}",
                                    data=art_bytes,
                                    file_name=selected_art_file.name,
                                    mime=art_mime,
                                    key=f"download_art_{selected_not_ready_folder}_{selected_art_file.name}",
                                )
                            except Exception as exc:
                                st.warning(f"Could not preview {selected_art_file.name}: {exc}")
                    else:
                        st.caption("No design image found.")

                with notes_col:
                    st.markdown("##### Notes")
                    st.metric("Files", len(selected_note_files))
                    if selected_note_files:
                        for note_index, note_file in enumerate(selected_note_files, start=1):
                            note_path = f"{selected_not_ready_path}/{note_file.name}"
                            try:
                                note_bytes = _download_dropbox_file_bytes(dbx, note_path)
                            except Exception as exc:
                                st.warning(f"Could not read {note_file.name}: {exc}")
                                continue

                            note_mime = "application/pdf" if note_file.name.lower().endswith(".pdf") else "text/plain"
                            st.download_button(
                                f"Download {note_file.name}",
                                data=note_bytes,
                                file_name=note_file.name,
                                mime=note_mime,
                                key=f"repair_not_ready_note_download_{note_index}_{note_file.name}",
                            )

                            if note_file.name.lower().endswith(".txt"):
                                with st.expander(f"Read {note_file.name}", expanded=False):
                                    st.text(_decode_note_text(note_bytes))
                    else:
                        st.caption("No notes found in the selected folder.")

                repair_col1, repair_col2 = st.columns(2)
                with repair_col1:
                    repair_metadata_json = st.file_uploader(
                        "Metadata JSON for selected folder",
                        type=["json"],
                        key="repair_not_ready_metadata_json",
                    )
                with repair_col2:
                    repair_mockup_zip = st.file_uploader(
                        "Mockup ZIP for selected folder",
                        type=["zip"],
                        key="repair_not_ready_mockup_zip",
                    )

                repair_overwrite_metadata = st.checkbox(
                    "Overwrite selected folder metadata JSON",
                    value=True,
                    key="repair_not_ready_overwrite_metadata",
                )
                repair_overwrite_mockups = st.checkbox(
                    "Overwrite selected folder numbered images",
                    value=True,
                    key="repair_not_ready_overwrite_mockups",
                )

                repair_preview_rows = []
                repair_metadata = None
                repair_metadata_blocked = False
                if repair_metadata_json is not None:
                    try:
                        repair_metadata = load_metadata_json(repair_metadata_json)
                        metadata_errors, descriptions_count_label = _metadata_issues(repair_metadata)
                        repair_preview_rows.append({
                            "File": getattr(repair_metadata_json, "name", "metadata.json"),
                            "Type": "Metadata JSON",
                            "Detected SKU": _normalise_sku(repair_metadata.get("sku_suffix")),
                            "Count": descriptions_count_label,
                            "Status": "Blocked" if metadata_errors else "Ready",
                            "Issues": "; ".join(metadata_errors),
                        })
                        repair_metadata_blocked = bool(metadata_errors)
                    except Exception as exc:
                        repair_preview_rows.append({
                            "File": getattr(repair_metadata_json, "name", "metadata.json"),
                            "Type": "Metadata JSON",
                            "Detected SKU": "",
                            "Count": "",
                            "Status": "Blocked",
                            "Issues": str(exc),
                        })
                        repair_metadata_blocked = True

                repair_images = None
                repair_zip_blocked = False
                if repair_mockup_zip is not None:
                    inspection = inspect_mockup_zip(
                        repair_mockup_zip,
                        expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                    )
                    zip_errors = [
                        error for error in inspection["errors"]
                        if "Could not detect SKU from ZIP filename" not in error
                    ]
                    zip_warnings = list(inspection["warnings"])
                    try:
                        repair_images = extract_mockup_images(repair_mockup_zip)
                    except Exception:
                        repair_images = None
                    repair_preview_rows.append({
                        "File": inspection["filename"],
                        "Type": "Mockup ZIP",
                        "Detected SKU": inspection["sku"],
                        "Count": f"{inspection['images_found']} / {MOCKUP_ZIP_EXPECTED_IMAGES}",
                        "Status": "Blocked" if zip_errors else "Ready with warnings" if zip_warnings else "Ready",
                        "Issues": "; ".join(zip_errors + zip_warnings),
                    })
                    repair_zip_blocked = bool(zip_errors)

                if repair_preview_rows:
                    st.data_editor(
                        pd.DataFrame(repair_preview_rows),
                        disabled=True,
                        width="stretch",
                        key="repair_not_ready_preview_table",
                    )

                repair_disabled = (
                    not selected_not_ready_folder
                    or (repair_metadata_json is None and repair_mockup_zip is None)
                    or repair_metadata_blocked
                    or repair_zip_blocked
                )
                if st.button(
                    "Add files to selected folder",
                    disabled=repair_disabled,
                    key="repair_not_ready_upload_btn",
                ):
                    upload_rows = []
                    with st.status(f"Updating {selected_not_ready_folder}...", expanded=True) as s:
                        if repair_metadata is not None:
                            metadata_result = _upload_metadata_json_to_dropbox(
                                dbx,
                                selected_not_ready_path,
                                repair_metadata,
                                overwrite=repair_overwrite_metadata,
                            )
                            metadata_status = (
                                f"Failed: {metadata_result['error']}"
                                if metadata_result["error"]
                                else "Skipped existing"
                                if metadata_result["skipped"]
                                else "Uploaded"
                            )
                            upload_rows.append({
                                "Item": "Metadata JSON",
                                "Uploaded": metadata_status,
                            })
                            s.write(f"Metadata JSON: {metadata_status}")

                        if repair_images is not None:
                            image_result = upload_mockup_images_to_dropbox(
                                dbx,
                                selected_not_ready_path,
                                repair_images,
                                overwrite=repair_overwrite_mockups,
                                expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                            )
                            upload_rows.append({
                                "Item": "Mockup ZIP",
                                "Uploaded": image_result["uploaded"],
                                "Skipped": image_result["skipped"],
                                "Failed": image_result["failed"],
                                "Truncated": image_result["truncated"],
                                "Details": "; ".join(
                                    f"{item['target']}: {item['error']}"
                                    for item in image_result.get("failed_files", [])[:5]
                                ),
                            })
                            s.write(
                                f"Mockups: uploaded {image_result['uploaded']}, "
                                f"skipped {image_result['skipped']}, failed {image_result['failed']}."
                            )

                        st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                            dbx, DESIGNS_ROOT, mockup_source=mockup_source
                        )
                        ready_folders = st.session_state.get("ready_folders", [])
                        not_ready_info = st.session_state.get("not_ready_folders", [])
                        s.update(label="Folder updated. Readiness refreshed.")

                    st.dataframe(pd.DataFrame(upload_rows), width="stretch")
            else:
                st.success("All folders are ready.")

        with st.container(border=True):
            render_section_header("Mockup ZIP intake")
            uploaded_zips = st.file_uploader(
                "Upload Canva mockup ZIP",
                type=["zip"],
                accept_multiple_files=True,
                key="mockup_zip_uploader",
            )
            uploaded_metadata_jsons = st.file_uploader(
                "Upload metadata JSON",
                type=["json"],
                accept_multiple_files=True,
                key="mockup_metadata_json_uploader",
                help="Optional. JSON files are matched to ZIPs by sku_suffix.",
            )
            overwrite_mockups = st.checkbox(
                "Overwrite existing numbered images",
                value=False,
                key="mockup_zip_overwrite",
            )
            overwrite_metadata_json = st.checkbox(
                "Overwrite existing metadata JSON",
                value=True,
                key="mockup_metadata_json_overwrite",
            )

            zip_previews = []
            metadata_by_sku, metadata_preview_rows = _metadata_index_from_uploads(uploaded_metadata_jsons)
            if metadata_preview_rows:
                st.markdown("#### Metadata JSON preview")
                st.data_editor(
                    pd.DataFrame(metadata_preview_rows),
                    disabled=True,
                    width="stretch",
                    key="mockup_metadata_json_preview_table",
                )

            if uploaded_zips:
                for uploaded_zip in uploaded_zips:
                    inspection = inspect_mockup_zip(
                        uploaded_zip,
                        expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                    )
                    sku = inspection["sku"]
                    normalised_sku = _normalise_sku(sku)
                    target_folder_path = f"{DESIGNS_ROOT}/{sku}" if sku else ""
                    target_exists = bool(target_folder_path and _dbx_exists(dbx, target_folder_path))
                    metadata_exists = bool(target_exists and _dropbox_folder_has_metadata(dbx, target_folder_path))
                    matched_metadata = metadata_by_sku.get(normalised_sku)
                    metadata_status = (
                        "Will upload"
                        if matched_metadata
                        else "Already exists"
                        if metadata_exists
                        else "Missing"
                    )

                    errors = list(inspection["errors"])
                    warnings = list(inspection["warnings"])
                    if sku and not target_exists:
                        errors.append(f"Target folder not found: {target_folder_path}")
                    if target_exists and not metadata_exists and not matched_metadata:
                        errors.append("Metadata JSON is missing in target folder.")

                    status = "Blocked" if errors else "Ready with warnings" if warnings else "Ready"
                    zip_previews.append({
                        "ZIP file": inspection["filename"],
                        "Detected SKU": sku,
                        "Target folder exists": target_exists,
                        "Metadata": metadata_status,
                        "Images found": inspection["images_found"],
                        "Status": status,
                        "Errors": "; ".join(errors),
                        "Warnings": "; ".join(warnings),
                        "_uploaded_file": uploaded_zip,
                        "_target_folder_path": target_folder_path,
                        "_metadata": matched_metadata["metadata"] if matched_metadata else None,
                        "_blocked": bool(errors),
                    })

                preview_df = pd.DataFrame([
                    {key: value for key, value in row.items() if not key.startswith("_")}
                    for row in zip_previews
                ])
                st.data_editor(
                    preview_df,
                    disabled=True,
                    width="stretch",
                    key="mockup_zip_preview_table",
                )

            if st.button("Upload ZIP and JSON to Dropbox", disabled=not uploaded_zips, key="mockup_zip_upload_btn"):
                if not zip_previews:
                    st.warning("Upload at least one ZIP file first.")
                else:
                    successful_upload = False
                    upload_rows = []
                    with st.status("Uploading ZIP and JSON files to Dropbox...", expanded=True) as s:
                        for row in zip_previews:
                            zip_name = row["ZIP file"]
                            if row["_blocked"]:
                                s.write(f"{zip_name}: blocked - {row['Errors']}")
                                upload_rows.append({
                                    "ZIP file": zip_name,
                                    "Metadata": "Blocked",
                                    "Uploaded": 0,
                                    "Skipped": 0,
                                    "Failed": 0,
                                    "Truncated": 0,
                                    "Status": "Blocked",
                                })
                                continue

                            try:
                                images = extract_mockup_images(row["_uploaded_file"])
                                metadata_status = "No change"
                                if row.get("_metadata"):
                                    metadata_result = _upload_metadata_json_to_dropbox(
                                        dbx,
                                        row["_target_folder_path"],
                                        row["_metadata"],
                                        overwrite=overwrite_metadata_json,
                                    )
                                    if metadata_result["error"]:
                                        metadata_status = f"Failed: {metadata_result['error']}"
                                    elif metadata_result["skipped"]:
                                        metadata_status = "Skipped existing"
                                    else:
                                        metadata_status = "Uploaded"

                                result = upload_mockup_images_to_dropbox(
                                    dbx,
                                    row["_target_folder_path"],
                                    images,
                                    overwrite=overwrite_mockups,
                                    expected_count=MOCKUP_ZIP_EXPECTED_IMAGES,
                                )
                                successful_upload = successful_upload or result["uploaded"] > 0 or metadata_status == "Uploaded"
                                upload_rows.append({
                                    "ZIP file": zip_name,
                                    "Metadata": metadata_status,
                                    "Uploaded": result["uploaded"],
                                    "Skipped": result["skipped"],
                                    "Failed": result["failed"],
                                    "Truncated": result["truncated"],
                                    "Details": "; ".join(
                                        f"{item['target']}: {item['error']}"
                                        for item in result.get("failed_files", [])[:5]
                                    ),
                                    "Status": (
                                        "Completed with failures"
                                        if result["failed"] or metadata_status.startswith("Failed:")
                                        else "Done"
                                    ),
                                })
                                s.write(
                                    f"{zip_name}: metadata {metadata_status.lower()}, uploaded {result['uploaded']}, "
                                    f"skipped {result['skipped']}, failed {result['failed']}."
                                )
                            except Exception as e:
                                upload_rows.append({
                                    "ZIP file": zip_name,
                                    "Metadata": "Failed",
                                    "Uploaded": 0,
                                    "Skipped": 0,
                                    "Failed": 1,
                                    "Truncated": 0,
                                    "Status": str(e),
                                })
                                s.write(f"{zip_name}: failed - {e}")

                        if successful_upload:
                            st.session_state.ready_folders, st.session_state.not_ready_folders = analyze_design_folders(
                                dbx, DESIGNS_ROOT, mockup_source=mockup_source
                            )
                            ready_folders = st.session_state.get("ready_folders", [])
                            not_ready_info = st.session_state.get("not_ready_folders", [])
                            s.update(label="Upload complete. Folder analysis refreshed.")
                        else:
                            s.update(label="Upload finished. No new files were uploaded.")

                    st.dataframe(
                        pd.DataFrame(upload_rows),
                        width="stretch",
                        key="mockup_zip_upload_results_table",
                    )

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
                                excluded_garments=excluded_garments,
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
                                excluded_garments=excluded_garments,
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
                                        final_path = move_selected_to_finished(dbx, folder)
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
                                excluded_garments=excluded_garments,
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
                                    final_path = move_selected_to_finished(dbx, fname)
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
                st.error(f"Clean and archive failed: {e}")

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
