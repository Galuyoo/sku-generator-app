import requests

from utils.canva_auth import get_canva_bearer_headers


CANVA_API_BASE = "https://api.canva.com/rest/v1"


def list_designs_search(term: str, limit: int = 30) -> list[dict]:
    response = requests.get(
        f"{CANVA_API_BASE}/designs",
        headers=get_canva_bearer_headers(),
        params={"search": term, "limit": limit},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("items", [])


def pick_best_match(items: list[dict], sku: str) -> dict | None:
    sku_l = sku.lower().strip()

    for design in items:
        title = (design.get("title") or "").lower().strip()
        if title == sku_l:
            return design

    for design in items:
        title = (design.get("title") or "").lower().strip()
        if title.startswith(sku_l):
            return design

    return None
