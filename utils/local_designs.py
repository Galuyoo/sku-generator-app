import json
from pathlib import Path


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def find_first_metadata_json(folder_path: str | Path) -> Path | None:
    path = Path(folder_path)
    for candidate in sorted(path.iterdir()):
        if candidate.is_file() and candidate.suffix.lower() == ".json":
            return candidate
    return None


def load_metadata_json(metadata_path: str | Path) -> dict:
    path = Path(metadata_path)
    return json.loads(path.read_text(encoding="utf-8"))


def find_preview_asset(folder_path: str | Path) -> Path | None:
    path = Path(folder_path)
    for ext in IMAGE_EXTENSIONS:
        candidate = path / f"{path.name}{ext}"
        if candidate.is_file():
            return candidate
    return None


def load_local_design_folder(folder_path: str | Path) -> dict:
    path = Path(folder_path)
    metadata_path = find_first_metadata_json(path)
    if not metadata_path:
        raise FileNotFoundError(f"No .json metadata file found in {path}")

    return {
        "folder_name": path.name,
        "metadata": load_metadata_json(metadata_path),
        "preview_asset_path": str(find_preview_asset(path)) if find_preview_asset(path) else None,
    }
