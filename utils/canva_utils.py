# utils/canva_utils.py

import json
import os
import time
from typing import Dict, List, Tuple, Optional

import requests
from dotenv import load_dotenv

load_dotenv()
load_dotenv("dpbox.env")  # harmless fallback if you keep envs there too

BASE = "https://api.canva.com/rest/v1"
CACHE_FILE = os.getenv("CANVA_CACHE_FILE", "sku_canva_cache.json")


def _get_access_token() -> str:
    token = (os.getenv("CANVA_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("CANVA_ACCESS_TOKEN is missing. Set it in your .env file.")
    return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_access_token()}"}


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def list_designs_search(term: str, limit: int = 30) -> List[dict]:
    r = requests.get(
        f"{BASE}/designs",
        headers=_headers(),
        params={"search": term, "limit": limit},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("items", [])


def pick_best_match(items: List[dict], sku: str) -> Optional[dict]:
    sku_l = sku.lower().strip()

    for d in items:
        title = (d.get("title") or "").lower().strip()
        if title == sku_l:
            return d

    for d in items:
        title = (d.get("title") or "").lower().strip()
        if title.startswith(sku_l):
            return d

    return None


def resolve_design_id_for_sku(sku: str, use_cache: bool = True) -> str:
    cache = load_cache() if use_cache else {}
    cached = cache.get(sku)

    design_id = None
    if isinstance(cached, dict):
        design_id = cached.get("design_id")
    elif isinstance(cached, str):
        design_id = cached

    if design_id:
        return design_id

    items = list_designs_search(sku)
    best = pick_best_match(items, sku)
    if not best:
        raise RuntimeError(f"Could not find a Canva design with title matching SKU '{sku}'")

    design_id = best["id"]

    if use_cache:
        cache[sku] = {"design_id": design_id}
        save_cache(cache)

    return design_id


def create_export_job(design_id: str, fmt: str = "png") -> str:
    r = requests.post(
        f"{BASE}/exports",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"design_id": design_id, "format": {"type": fmt}},
        timeout=30,
    )

    if r.status_code >= 400:
        raise RuntimeError(f"Canva export creation failed ({r.status_code}): {r.text}")

    data = r.json()

    if isinstance(data, dict):
        if "id" in data:
            return data["id"]
        if "job" in data and isinstance(data["job"], dict) and "id" in data["job"]:
            return data["job"]["id"]
        if "export" in data and isinstance(data["export"], dict) and "id" in data["export"]:
            return data["export"]["id"]

    raise RuntimeError(f"No export job id found in Canva response: {data}")


def wait_for_export(
    export_id: str,
    timeout_s: int = 600,
    poll_s: float = 1.5,
) -> List[str]:
    start = time.time()
    last = None

    while True:
        r = requests.get(
            f"{BASE}/exports/{export_id}",
            headers=_headers(),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        last = data

        job = data.get("job", data)
        status = job.get("status")

        if status == "success":
            urls = job.get("urls") or []
            if not urls:
                raise RuntimeError(f"Export succeeded but no URLs were returned. Response: {data}")
            return urls

        if status == "failed":
            raise RuntimeError(f"Canva export failed: {data}")

        if time.time() - start > timeout_s:
            raise TimeoutError(f"Canva export timed out after {timeout_s}s. Last job: {last}")

        time.sleep(poll_s)


def _urls_to_page_map(urls: List[str], total_images: int) -> Tuple[Dict[int, str], List[int]]:
    image_links: Dict[int, str] = {}
    returned_count = len(urls)

    upper = min(returned_count, total_images)
    for i in range(upper):
        image_links[i + 1] = urls[i]

    missing = [i for i in range(1, total_images + 1) if i not in image_links]
    return image_links, missing


def load_canva_image_links_by_sku(
    sku: str,
    total_images: int = 80,
    fmt: str = "png",
    use_cache: bool = True,
) -> Tuple[Dict[int, str], List[int]]:
    """
    Dropbox-compatible return shape:
        image_links: {1: url1, 2: url2, ...}
        missing: [missing_page_numbers]

    This makes Canva a drop-in replacement for load_dropbox_image_links(...).
    """
    sku = (sku or "").strip().upper()
    if not sku:
        raise ValueError("SKU is required for Canva export")

    design_id = resolve_design_id_for_sku(sku, use_cache=use_cache)
    export_id = create_export_job(design_id, fmt=fmt)
    urls = wait_for_export(export_id)

    image_links, missing = _urls_to_page_map(urls, total_images=total_images)
    return image_links, missing