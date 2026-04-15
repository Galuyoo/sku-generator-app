import os


def get_canva_bearer_headers() -> dict[str, str]:
    token = (os.getenv("CANVA_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("CANVA_ACCESS_TOKEN is missing. Set it in your environment.")
    return {"Authorization": f"Bearer {token}"}
