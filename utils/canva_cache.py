import json
from pathlib import Path


CACHE_FILE = "sku_canva_cache.json"


def load_cache(cache_file: str = CACHE_FILE) -> dict:
    path = Path(cache_file)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(cache: dict, cache_file: str = CACHE_FILE) -> None:
    path = Path(cache_file)
    path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
