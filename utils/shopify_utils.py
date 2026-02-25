# utils/shopify_utils.py
import os
import time
import random
import requests
import base64
import urllib.parse
import re
from collections import defaultdict

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")

TIMEOUT              = int(os.getenv("SHOPIFY_HTTP_TIMEOUT", "120"))
MAX_RETRIES          = int(os.getenv("SHOPIFY_MAX_RETRIES", "5"))
BACKOFF_BASE         = float(os.getenv("SHOPIFY_RETRY_BACKOFF_BASE", "1.8"))
INLINE_IMAGES        = os.getenv("SHOPIFY_INLINE_IMAGES", "false").lower() in ("1", "true", "yes")
CREATE_COOLDOWN      = float(os.getenv("SHOPIFY_PRODUCT_CREATE_COOLDOWN", "1.0"))
IMAGE_UPLOAD_SLEEP   = float(os.getenv("SHOPIFY_IMAGE_UPLOAD_SLEEP", "0"))
ATTACHMENT_FALLBACK  = os.getenv("SHOPIFY_IMAGE_ATTACHMENT_FALLBACK", "true").lower() in ("1","true","yes")
AFTER_EACH_DELAY     = float(os.getenv("SHOPIFY_AFTER_EACH_DELAY", "0"))

# Controls for title/SEO fallbacks if CSV columns aren't present:
TITLE_STRIP_AFTER_PIPE = os.getenv("SHOPIFY_TITLE_STRIP_AFTER_PIPE", "true").lower() in ("1","true","yes")
META_DESC_MAX          = int(os.getenv("SHOPIFY_META_DESC_MAX", "300"))

class ShopifyError(Exception):
    pass

_session = requests.Session()  # keep-alive

# ------------------ small text helpers ------------------

def _norm(s):
    return (s or "").strip()

def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()

def _split_title_for_seo(title: str):
    title = (title or "").strip()
    clean = title.split("|", 1)[0].strip()
    return clean, title  # (clean_title, long_title)

def _make_meta_description_from_html(body_html: str, fallback_title: str) -> str:
    text = _strip_html(body_html) or (fallback_title or "")
    if len(text) > META_DESC_MAX:
        text = text[:META_DESC_MAX-1].rstrip() + "…"
    return text

def _first_nonempty(series):
    try:
        for v in series:
            if v:
                return str(v)
    except Exception:
        pass
    return None

def _pick_image_src_per_color(group_df):
    if "Image Src" not in group_df.columns:
        return {}
    mapping = {}
    for color, sub in group_df.groupby("Option2 Value"):
        src = _first_nonempty(sub["Image Src"])
        if src:
            mapping[_norm(color)] = src
    return mapping

def _fmt_secs(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{int(m)}m {s:.1f}s"
    h, m = divmod(int(m), 60)
    return f"{h}h {m}m {s:.0f}s"

# ------------------ public entrypoint ------------------

def upload_products_from_df(df, progress=None, variant_budget=None):
    """
    Upload products defined in the CSV-style DataFrame.
    If 'SEO Title' and/or 'SEO Description' columns exist in df,
    we'll use them to set Shopify's SEO fields on create.
    Optional variant_budget caps total variants across all products.
    """
    overall_start = time.perf_counter()

    _say(progress, "✅ Shopify upload started")
    _say(progress, f"📦 Total rows in DataFrame: {len(df)}")
    _say(progress, f"🔑 Unique product handles: {df['Handle'].nunique()}")

    results = []
    grouped = df.groupby("Handle", sort=False)

    remaining_budget = None if variant_budget in (None, 0) else int(variant_budget)

    for handle, group in grouped:
        raw_title = group.iloc[0]["Title"]
        ptype     = group.iloc[0]["Type"]

        # stats
        sizes_raw  = group["Option1 Value"].dropna().astype(str).tolist()
        colors_raw = group["Option2 Value"].dropna().astype(str).tolist()
        sizes  = sorted(set(s.strip() for s in sizes_raw if s.strip()), key=str)
        colors = sorted(set(c.strip() for c in colors_raw if c.strip()), key=str)

        _say(progress, "────────────────────────────────────────────────────────")
        _say(progress, f"🚀 Creating product for handle: {handle} with {len(group)} rows")
        _say(progress, f"🧩 Variants to send (pre-sanitize): {len(group)} (sizes≈{len(sizes)}, colors≈{len(colors)})")

        t0 = time.perf_counter()

        # --- sanitize variants
        variants = []
        seen = set()
        dropped_missing = 0
        dropped_dupe = 0

        for _, row in group.iterrows():
            size  = _norm(row.get("Option1 Value"))
            color = _norm(row.get("Option2 Value"))
            if not size or not color:
                dropped_missing += 1
                continue
            key = (size.lower(), color.lower())
            if key in seen:
                dropped_dupe += 1
                continue
            seen.add(key)
            variants.append({
                "price": row.get("Variant Price", "0"),
                "sku": row["Variant SKU"],
                "inventory_quantity": int(row.get("Variant Inventory Qty", 0)),
                "requires_shipping": bool(row.get("Variant Requires Shipping", True)),
                "taxable": bool(row.get("Variant Taxable", True)),
                "option1": size,
                "option2": color,
            })

        if dropped_missing:
            _say(progress, f"⚠️ Dropped {dropped_missing} rows with missing Size/Colour.")
        if dropped_dupe:
            _say(progress, f"ℹ️ Skipped {dropped_dupe} duplicate (Size,Colour) combos.")

        if not variants:
            raise ShopifyError(
                f"No valid variants to send for handle={handle}. "
                f"(Check Option1/Option2 values in your DataFrame.)"
            )

        # Apply variant budget (if any)
        if remaining_budget is not None:
            if remaining_budget <= 0:
                _say(progress, "⏭️ Variant budget exhausted — skipping remaining products.")
                break
            if len(variants) > remaining_budget:
                _say(progress, f"🔪 Capping variants from {len(variants)} → {remaining_budget} due to budget")
                variants = variants[:remaining_budget]
            remaining_budget -= len(variants)

        _say(progress, f"🧩 Variants to send (final): {len(variants)} (unique combos)")

        # --- image inline mapping (optional)
        color_to_src = _pick_image_src_per_color(group)
        inline_images = []
        if INLINE_IMAGES and color_to_src:
            seen_src = set()
            for color, src in color_to_src.items():
                if not src or src in seen_src:
                    continue
                img = {"src": src}
                try:
                    pos = int(group.loc[group["Image Src"] == src, "Image Position"].dropna().astype(int).min())
                    if pos == 1:
                        img["position"] = 1
                except Exception:
                    pass
                alt = _first_nonempty(group.loc[group["Image Src"] == src, "Image Alt Text"])
                if alt:
                    img["alt"] = alt
                inline_images.append(img)
                seen_src.add(src)

        # --- titles & SEO
        # Prefer CSV-provided SEO fields if available
        csv_seo_title = group.iloc[0].get("SEO Title")
        csv_seo_desc  = group.iloc[0].get("SEO Description")

        clean_title, long_title = _split_title_for_seo(raw_title)
        seo_title = csv_seo_title if _norm(csv_seo_title) else long_title
        seo_desc  = csv_seo_desc  if _norm(csv_seo_desc)  else _make_meta_description_from_html(
            group.iloc[0]["Body (HTML)"], long_title
        )

        # If the CSV already supplied a stripped Title, use as-is; otherwise strip after pipe if flag enabled
        title_to_use = raw_title
        if TITLE_STRIP_AFTER_PIPE:
            title_to_use = clean_title

        # --- Build payload
        product_payload = {
            "title": title_to_use,
            "body_html": group.iloc[0]["Body (HTML)"],
            "vendor": group.iloc[0]["Vendor"],
            "product_type": ptype,
            "tags": group.iloc[0]["Tags"],
            "options": [{"name": "Size"}, {"name": "Colour"}],
            "variants": variants,
            "metafields_global_title_tag": seo_title,
            "metafields_global_description_tag": seo_desc,
        }
        if INLINE_IMAGES and inline_images:
            product_payload["images"] = inline_images

        # --- Create product
        product_data = _create_product(product_payload, progress=progress)
        product_id = product_data["id"]
        _say(progress, f"✅ Created product: {product_data.get('title')} (ID: {product_id})")

        # --- Upload images after create if not inlined
        src_to_image_id = {}
        if not INLINE_IMAGES and color_to_src:
            _say(progress, "⏳ Uploading images after create…")
            for src in list(dict.fromkeys(color_to_src.values())):
                if not src:
                    continue
                img = _upload_image(product_id, src, progress=progress)
                src_to_image_id[src] = img["id"]
        else:
            for img in product_data.get("images", []):
                if img.get("src"):
                    src_to_image_id[img["src"]] = img["id"]

        # --- Link variant images by color
        if src_to_image_id and color_to_src:
            color_to_image_id = { _norm(c): src_to_image_id.get(s)
                                  for c, s in color_to_src.items()
                                  if src_to_image_id.get(s) }
            linked = defaultdict(int)
            for v in product_data.get("variants", []):
                color = _norm(v.get("option2"))
                if not color:
                    continue
                img_id = color_to_image_id.get(color)
                if not img_id:
                    continue
                try:
                    _update_variant_image(v["id"], img_id, progress=progress)
                    linked[color] += 1
                except ShopifyError as e:
                    _say(progress, f"⚠️ Link failed for {color}: {e}")

            for color, count in linked.items():
                _say(progress, f"✅ Linked {count} variants to image for {ptype}|{color}")

        dt = time.perf_counter() - t0
        _say(progress, f"⏱ Product finished in {_fmt_secs(dt)}")

        results.append({
            "handle_or_title": product_data.get("handle") or product_data.get("title"),
            "product_id": product_id,
            "created_variants": len(product_data.get("variants", [])),
            "created_images": len(product_data.get("images", [])),
            "admin_url": f"https://{os.getenv('SHOPIFY_STORE_URL')}/admin/products/{product_id}"
        })

        if CREATE_COOLDOWN > 0:
            time.sleep(CREATE_COOLDOWN)

    total = time.perf_counter() - overall_start
    _say(progress, f"⏱ All products in this design uploaded in {_fmt_secs(total)}")
    return results

# ------------------ low-level HTTP ------------------

def _api_base():
    store = os.getenv("SHOPIFY_STORE_URL", "").strip().replace("https://", "").replace("http://", "")
    _require(store, "SHOPIFY_STORE_URL is not set")
    return f"https://{store}/admin/api/{SHOPIFY_API_VERSION}"

def _headers():
    token = os.getenv("SHOPIFY_API_PASSWORD") or os.getenv("SHOPIFY_ADMIN_API_ACCESS_TOKEN")
    _require(token, "SHOPIFY_API_PASSWORD (Admin API access token) is not set")
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def _post(url, json, progress=None):
    consecutive_429 = 0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _say(progress, f"📡 POST {url}")
            if isinstance(json, dict) and "product" in json:
                title = json["product"].get("title")
                if title:
                    _say(progress, f"📤 Payload (title): {title}")

            r = _session.post(url, headers=_headers(), json=json, timeout=TIMEOUT)
            _say(progress, f"📥 Response status: {r.status_code}")

            if r.status_code == 429:
                txt = (r.text or "").lower()
                if "daily variant creation limit" in txt:
                    raise ShopifyError("DAILY_VARIANT_LIMIT: " + r.text)
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        delay = float(ra)
                        _say(progress, f"⏳ Rate limited (429). Retry-After={delay:.1f}s")
                    except Exception:
                        delay = _exp_backoff(attempt, progress)
                else:
                    delay = _exp_backoff(attempt, progress)
                    if consecutive_429 >= 3: delay = max(delay, 15.0)
                    if consecutive_429 >= 5: delay = max(delay, 45.0)
                    if consecutive_429 >= 6: delay = max(delay, 75.0)
                consecutive_429 += 1
                time.sleep(delay)
                continue

            if 200 <= r.status_code < 300:
                consecutive_429 = 0
                _respect_call_limit(r, progress)
                _small_after_delay(progress)
                _say(progress, "✅ POST successful")
                return r.json()

            if r.status_code >= 500:
                delay = _exp_backoff(attempt, progress)
                time.sleep(delay)
                continue

            raise ShopifyError(f"POST {url} failed: {r.status_code} {r.text}")

        except (requests.Timeout, requests.ConnectionError) as e:
            _say(progress, f"⏳ POST timeout/conn error (attempt {attempt}/{MAX_RETRIES}): {e}")
            delay = _exp_backoff(attempt, progress)
            time.sleep(delay)
            continue
        except requests.RequestException as e:
            raise ShopifyError(f"POST {url} error: {e}")

    raise ShopifyError(f"POST {url} exhausted retries")

def _put(url, json, progress=None):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _say(progress, f"📡 PUT {url}")
            r = _session.put(url, headers=_headers(), json=json, timeout=TIMEOUT)

            if r.status_code == 429:
                delay = _retry_after_or_backoff(r, attempt, progress)
                time.sleep(delay)
                continue

            _say(progress, f"📥 Response status: {r.status_code}")

            if 200 <= r.status_code < 300:
                _respect_call_limit(r, progress)
                _small_after_delay(progress)
                _say(progress, "✅ PUT successful")
                return r.json()

            if r.status_code >= 500:
                delay = _exp_backoff(attempt, progress)
                time.sleep(delay)
                continue

            raise ShopifyError(f"PUT {url} failed: {r.status_code} {r.text}")

        except (requests.Timeout, requests.ConnectionError) as e:
            _say(progress, f"⏳ PUT timeout/conn error (attempt {attempt}/{MAX_RETRIES}): {e}")
            delay = _exp_backoff(attempt, progress)
            time.sleep(delay)
            continue
        except requests.RequestException as e:
            raise ShopifyError(f"PUT {url} error: {e}")

    raise ShopifyError(f"PUT {url} exhausted retries")

def _create_product(product_payload, progress=None):
    url = f"{_api_base()}/products.json"
    return _post(url, {"product": product_payload}, progress=progress)["product"]

def _download_image_bytes(src_url, progress=None):
    try:
        r = requests.get(src_url, timeout=TIMEOUT, stream=True)
        r.raise_for_status()
        data = r.content
        path = urllib.parse.urlparse(src_url).path
        name = os.path.basename(path) or "image"
        root, ext = os.path.splitext(name)
        if not ext:
            ctype = r.headers.get("Content-Type", "")
            if "png" in ctype: ext = ".png"
            elif "webp" in ctype: ext = ".webp"
            else: ext = ".jpg"
            name = root + ext
        _say(progress, f"⬇️ Downloaded {len(data)/1024:.1f} KB from origin")
        return data, name
    except Exception as ex:
        raise ShopifyError(f"Failed to fetch image bytes from {src_url}: {ex}")

def _upload_image(product_id, src_url, position=None, alt=None, progress=None):
    url = f"{_api_base()}/products/{product_id}/images.json"
    payload = {"image": {"src": src_url}}
    if position is not None:
        payload["image"]["position"] = position
    if alt:
        payload["image"]["alt"] = alt

    try:
        img = _post(url, payload, progress=progress)["image"]
        if IMAGE_UPLOAD_SLEEP > 0:
            time.sleep(IMAGE_UPLOAD_SLEEP)
        return img
    except ShopifyError as e:
        msg = str(e)
        if ATTACHMENT_FALLBACK and ("Could not download image" in msg or "422" in msg):
            _say(progress, "🛟 Fallback: uploading image as attachment (base64)…")
            data, filename = _download_image_bytes(src_url, progress=progress)
            attach = base64.b64encode(data).decode("ascii")
            payload2 = {"image": {"attachment": attach, "filename": filename}}
            if position is not None:
                payload2["image"]["position"] = position
            if alt:
                payload2["image"]["alt"] = alt
            img = _post(url, payload2, progress=progress)["image"]
            if IMAGE_UPLOAD_SLEEP > 0:
                time.sleep(IMAGE_UPLOAD_SLEEP)
            return img
        raise

def _update_variant_image(variant_id, image_id, progress=None):
    url = f"{_api_base()}/variants/{variant_id}.json"
    return _put(url, {"variant": {"id": variant_id, "image_id": image_id}}, progress=progress)["variant"]

# ------------------ rate limiting helpers ------------------

def _retry_after_or_backoff(resp, attempt, progress=None):
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            delay = float(ra)
            _say(progress, f"⏳ Rate limited (429). Respecting Retry-After: {delay:.1f}s")
            return delay
        except Exception:
            pass
    return _exp_backoff(attempt, progress)

def _exp_backoff(attempt, progress=None):
    delay = (BACKOFF_BASE ** (attempt - 1)) + random.uniform(0.0, 0.6)
    _say(progress, f"⏳ Backing off {delay:.1f}s before retry…")
    return delay

def _respect_call_limit(resp, progress=None):
    hdr = resp.headers.get("X-Shopify-Shop-Api-Call-Limit")
    if not hdr:
        return
    try:
        used, cap = hdr.split("/")
        used = int(used.strip()); cap = int(cap.strip())
        if cap > 0 and used >= int(0.75 * cap):
            target_used = int(0.50 * cap)
            delta = max(0, used - target_used)
            sleep_sec = max(0.5, delta / 2.0)  # ~2 tokens/sec
            _say(progress, f"🕒 Throttling for call limit {used}/{cap}. Sleeping {sleep_sec:.1f}s…")
            time.sleep(sleep_sec)
    except Exception:
        pass

def _small_after_delay(progress=None):
    if AFTER_EACH_DELAY > 0:
        time.sleep(AFTER_EACH_DELAY)

def _require(val, msg):
    if not val:
        raise ShopifyError(msg)

def _say(cb, msg):
    if cb:
        try: cb(str(msg))
        except Exception: pass
    else:
        print(msg)
