from dataclasses import dataclass

from utils.canva_pages import export_pages_for_sku
from utils.local_designs import load_local_design_folder


@dataclass
class DesignBundle:
    source_kind: str
    source_id: str
    display_name: str
    metadata: dict
    image_links: dict[int, str]
    missing_images: list[int]
    preview_asset: str | None = None


def build_local_canva_bundle(folder_path: str, sku: str | None = None) -> DesignBundle:
    design = load_local_design_folder(folder_path)
    metadata = design["metadata"]
    resolved_sku = (sku or metadata.get("sku_suffix") or "").strip()
    if not resolved_sku:
        raise RuntimeError("Missing SKU for local Canva bundle. Provide sku or set metadata['sku_suffix'].")

    image_links = export_pages_for_sku(resolved_sku)
    missing_images = [i for i in range(1, 81) if i not in image_links]

    return DesignBundle(
        source_kind="local_canva",
        source_id=design["folder_name"],
        display_name=design["folder_name"],
        metadata=metadata,
        image_links=image_links,
        missing_images=missing_images,
        preview_asset=design["preview_asset_path"],
    )
