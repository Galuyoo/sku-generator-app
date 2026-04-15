import time

import requests

from utils.canva_auth import get_canva_bearer_headers


CANVA_API_BASE = "https://api.canva.com/rest/v1"


def create_export_job(design_id: str, fmt: str = "png") -> str:
    response = requests.post(
        f"{CANVA_API_BASE}/exports",
        headers={**get_canva_bearer_headers(), "Content-Type": "application/json"},
        json={"design_id": design_id, "format": {"type": fmt}},
        timeout=30,
    )

    if response.status_code >= 400:
        response.raise_for_status()

    data = response.json()

    if isinstance(data, dict):
        if "id" in data:
            return data["id"]
        if "job" in data and isinstance(data["job"], dict) and "id" in data["job"]:
            return data["job"]["id"]
        if "export" in data and isinstance(data["export"], dict) and "id" in data["export"]:
            return data["export"]["id"]

    raise KeyError("No export job id found in response")


def wait_for_export(export_id: str, timeout_s: int = 600, poll_s: float = 1.5) -> list[str]:
    start = time.time()
    last = None

    while True:
        response = requests.get(
            f"{CANVA_API_BASE}/exports/{export_id}",
            headers=get_canva_bearer_headers(),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        last = data

        job = data.get("job", data)
        status = job.get("status")

        if status == "success":
            urls = job.get("urls") or []
            if not urls:
                raise RuntimeError(f"Export success but no urls. Full response: {data}")
            return urls

        if status == "failed":
            raise RuntimeError(f"Export failed: {data}")

        if time.time() - start > timeout_s:
            raise TimeoutError(f"Export timed out after {timeout_s}s. Last job: {last}")

        time.sleep(poll_s)
