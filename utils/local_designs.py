import json
from pathlib import Path


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
NOTES_EXTENSIONS = (".txt", ".pdf")
VALID_CLASSIFICATIONS = {"square", "long", "wide"}
VALID_DESIGN_COUNT_TYPES = {"single", "dual"}


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


def analyze_local_design_folders(root_path: str | Path) -> tuple[list[str], list[dict]]:
    root = Path(root_path)
    ready: list[str] = []
    not_ready: list[dict] = []

    if not root.exists():
        return ready, [{
            "Folder": str(root),
            "Has JSON": "❌",
            "Has Preview": "❌",
            "Issues": "Root path does not exist",
        }]

    for folder in sorted(
        p for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name != "__pycache__"
    ):
        metadata_path = find_first_metadata_json(folder)
        preview_asset = find_preview_asset(folder)
        notes_file = next(
            (p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in NOTES_EXTENSIONS),
            None,
        )
        design_images = [
            p for p in sorted(folder.iterdir())
            if p.is_file()
            and p.suffix.lower() in IMAGE_EXTENSIONS
        ]
        issues: list[str] = []
        metadata: dict = {}
        classification = ""
        design_count_type = ""

        if not metadata_path:
            issues.append("Missing metadata JSON")
        else:
            try:
                metadata = load_metadata_json(metadata_path)
            except Exception as e:
                issues.append(f"Invalid metadata JSON: {e}")

        if not notes_file:
            issues.append("Missing notes file")

        if not design_images:
            issues.append("Missing design images")

        if metadata:
            classification = str(metadata.get("classification", "")).strip().lower()
            design_count_type = str(metadata.get("design_count_type", "")).strip().lower()
            sku_suffix = str(metadata.get("sku_suffix", "")).strip()

            if not classification:
                issues.append("Missing classification")
            elif classification not in VALID_CLASSIFICATIONS:
                issues.append(f"Invalid classification: {classification}")

            if not design_count_type:
                issues.append("Missing design_count_type")
            elif design_count_type not in VALID_DESIGN_COUNT_TYPES:
                issues.append(f"Invalid design_count_type: {design_count_type}")

            if sku_suffix != folder.name:
                issues.append("sku_suffix does not match folder name")

            if design_count_type == "single" and len(design_images) != 1:
                issues.append(f"Expected 1 design image, found {len(design_images)}")
            if design_count_type == "dual" and len(design_images) != 2:
                issues.append(f"Expected 2 design images, found {len(design_images)}")

        info = {
            "Folder": folder.name,
            "Has JSON": "✅" if metadata_path else "❌",
            "Has Notes": "✅" if notes_file else "❌",
            "Design Image Count": len(design_images),
            "Classification": classification,
            "Design Count Type": design_count_type,
            "Issues": ", ".join(issues),
        }

        if issues:
            not_ready.append(info)
        else:
            ready.append(folder.name)

    return ready, not_ready
