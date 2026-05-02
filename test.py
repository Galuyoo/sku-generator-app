import os
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

token = os.getenv("CANVA_ACCESS_TOKEN")
headers = {"Authorization": f"Bearer {token}"}

all_designs = []
continuation = None

while True:
    params = {"limit": 100}
    if continuation:
        params["continuation"] = continuation

    r = requests.get(
        "https://api.canva.com/rest/v1/designs",
        headers=headers,
        params=params,
        timeout=30,
    )
    r.raise_for_status()

    data = r.json()
    items = data.get("items", [])
    continuation = data.get("continuation")

    print(f"fetched {len(items)} | total {len(all_designs) + len(items)}")

    all_designs.extend(items)

    if not continuation:
        break

# build lookup map
design_map = {}
for d in all_designs:
    title = (d.get("title") or "").strip().upper()
    if title:
        design_map[title] = d["id"]

print("\nTOTAL:", len(design_map))

# test
print("ASHTONLULE:", design_map.get("ASHTONLULE"))