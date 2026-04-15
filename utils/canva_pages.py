from utils.canva_cache import CACHE_FILE, load_cache, save_cache
from utils.canva_exports import create_export_job, wait_for_export
from utils.canva_lookup import list_designs_search, pick_best_match


def export_pages_for_sku(sku: str, cache_file: str = CACHE_FILE, fmt: str = "png") -> dict[int, str]:
    cache = load_cache(cache_file)
    cached = cache.get(sku)

    design_id = None
    if isinstance(cached, dict):
        design_id = cached.get("design_id")
    elif isinstance(cached, str):
        design_id = cached

    if not design_id:
        items = list_designs_search(sku)
        best = pick_best_match(items, sku)
        if not best:
            raise RuntimeError(f"Could not find a Canva design with title matching SKU '{sku}'")

        design_id = best["id"]
        cache[sku] = {"design_id": design_id}
        save_cache(cache, cache_file)

    export_id = create_export_job(design_id, fmt=fmt)
    urls = wait_for_export(export_id)
    return {i + 1: url for i, url in enumerate(urls)}
