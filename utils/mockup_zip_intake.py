import os
import re
import zipfile
from pathlib import PurePosixPath

import dropbox


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
IGNORED_NAMES = {".ds_store", "thumbs.db"}


def natural_sort_key(value: str) -> list:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value or "")
    ]


def infer_sku_from_zip_name(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    return stem.strip()


def inspect_mockup_zip(uploaded_file, expected_count: int = 80) -> dict:
    filename = getattr(uploaded_file, "name", "") or ""
    sku = infer_sku_from_zip_name(filename)
    errors = []
    warnings = []

    if not sku:
        errors.append("Could not detect SKU from ZIP filename.")

    try:
        images = extract_mockup_images(uploaded_file)
    except zipfile.BadZipFile:
        images = []
        errors.append("ZIP file could not be read.")

    image_count = len(images)
    if image_count == 0:
        errors.append("No supported image files found.")
    elif image_count < expected_count:
        warnings.append(f"Found {image_count}/{expected_count} images.")
    elif image_count > expected_count:
        warnings.append(f"Found {image_count} images; only the first {expected_count} will be uploaded.")

    return {
        "filename": filename,
        "sku": sku,
        "images_found": image_count,
        "errors": errors,
        "warnings": warnings,
    }


def extract_mockup_images(uploaded_file) -> list[tuple[str, bytes, str]]:
    uploaded_file.seek(0)
    images = []
    with zipfile.ZipFile(uploaded_file) as zf:
        for info in zf.infolist():
            if info.is_dir() or _is_ignored_zip_member(info.filename):
                continue

            ext = PurePosixPath(info.filename).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                continue

            images.append((info.filename, zf.read(info), ext))

    uploaded_file.seek(0)
    return sorted(images, key=lambda item: natural_sort_key(item[0]))


def upload_mockup_images_to_dropbox(
    dbx,
    target_folder_path,
    images,
    overwrite=False,
    expected_count=80,
) -> dict:
    existing_numbers = set()
    if not overwrite:
        existing_numbers = _existing_numbered_image_indexes(dbx, target_folder_path, expected_count)

    uploaded = []
    skipped = []
    failed = []

    for index, (source_name, content, ext) in enumerate(images[:expected_count], start=1):
        target_name = f"{index}{ext.lower()}"
        target_path = f"{target_folder_path.rstrip('/')}/{target_name}"

        if not overwrite and index in existing_numbers:
            skipped.append({"source": source_name, "target": target_name, "reason": "already exists"})
            continue

        try:
            mode = dropbox.files.WriteMode.overwrite if overwrite else dropbox.files.WriteMode.add
            dbx.files_upload(content, target_path, mode=mode, mute=True)
            uploaded.append({"source": source_name, "target": target_name})
        except Exception as exc:
            failed.append({"source": source_name, "target": target_name, "error": str(exc)})

    return {
        "uploaded": len(uploaded),
        "skipped": len(skipped),
        "failed": len(failed),
        "uploaded_files": uploaded,
        "skipped_files": skipped,
        "failed_files": failed,
        "truncated": max(0, len(images) - expected_count),
    }


def _is_ignored_zip_member(filename: str) -> bool:
    path = PurePosixPath(filename)
    parts = path.parts
    if any(part == "__MACOSX" for part in parts):
        return True
    if any(part.startswith(".") for part in parts):
        return True
    return path.name.lower() in IGNORED_NAMES


def _existing_numbered_image_indexes(dbx, folder_path: str, expected_count: int) -> set[int]:
    existing = set()
    result = dbx.files_list_folder(folder_path)

    while True:
        for entry in result.entries:
            if not isinstance(entry, dropbox.files.FileMetadata):
                continue

            stem, ext = os.path.splitext(entry.name.lower())
            if stem.isdigit() and ext in IMAGE_EXTENSIONS:
                index = int(stem)
                if 1 <= index <= expected_count:
                    existing.add(index)

        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)

    return existing

def load_metadata_json(uploaded_file) -> dict:
    import json

    uploaded_file.seek(0)
    try:
        metadata = json.load(uploaded_file)
    finally:
        uploaded_file.seek(0)

    if not isinstance(metadata, dict):
        raise ValueError("Metadata JSON must contain a JSON object.")

    return metadata


def validate_pipeline_metadata(metadata: dict) -> list[str]:
    required_fields = [
        "product_name",
        "sku_suffix",
        "main_color",
        "tags",
        "page_titles",
        "descriptions",
    ]
    issues = []

    for field in required_fields:
        if field not in metadata:
            issues.append(f"Missing metadata field: {field}")

    sku = str(metadata.get("sku_suffix", "")).strip()
    if not sku:
        issues.append("metadata['sku_suffix'] is required.")

    if "tags" in metadata and not isinstance(metadata["tags"], list):
        issues.append("metadata['tags'] must be a list.")

    if "page_titles" in metadata and not isinstance(metadata["page_titles"], list):
        issues.append("metadata['page_titles'] must be a list.")

    if "descriptions" in metadata and not isinstance(metadata["descriptions"], list):
        issues.append("metadata['descriptions'] must be a list.")

    return issues


def _pipeline_now_iso() -> str:
    from datetime import datetime

    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _write_json_file(path, data) -> None:
    import json
    from pathlib import Path

    path = Path(path)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_json_file(path) -> dict:
    import json
    from pathlib import Path

    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _design_folder_state(pipeline_root, sku: str):
    from pathlib import Path

    root = Path(pipeline_root)
    for state in ("staged", "ready", "active", "finished"):
        folder = root / state / sku
        if folder.exists():
            return state, folder
    return None, None


def _validate_design_folder(design_folder, expected_count=80) -> dict:
    from pathlib import Path

    design_folder = Path(design_folder)
    metadata_path = design_folder / "metadata.json"
    mockups_folder = design_folder / "mockups"

    issues = []
    warnings = []

    image_files = []
    if mockups_folder.exists():
        image_files = [
            path for path in mockups_folder.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]
        image_files = sorted(image_files, key=lambda path: natural_sort_key(path.name))

    image_count = len(image_files)
    if image_count == 0:
        issues.append("No mockup images found.")
    elif image_count < expected_count:
        issues.append(f"Only {image_count}/{expected_count} mockup images.")
    elif image_count > expected_count:
        warnings.append(f"Found {image_count} images; expected {expected_count}.")

    metadata = None
    if not metadata_path.exists():
        issues.append("Missing metadata JSON.")
    else:
        try:
            metadata = _read_json_file(metadata_path)
            issues.extend(validate_pipeline_metadata(metadata))
        except Exception as exc:
            issues.append(f"Invalid metadata JSON: {exc}")

    sku = design_folder.name
    if metadata:
        json_sku = str(metadata.get("sku_suffix", "")).strip()
        if json_sku and json_sku != sku:
            issues.append(f"Folder SKU '{sku}' does not match JSON sku_suffix '{json_sku}'.")

    return {
        "ready": not issues,
        "sku": sku,
        "image_count": image_count,
        "has_metadata": metadata_path.exists(),
        "issues": issues,
        "warnings": warnings,
    }


def _write_manifest(design_folder, manifest: dict) -> None:
    from pathlib import Path

    design_folder = Path(design_folder)
    manifest_path = design_folder / "manifest.json"
    _write_json_file(manifest_path, manifest)


def digest_mockup_zip_to_pipeline(
    uploaded_zip,
    uploaded_json=None,
    pipeline_root="pipeline_data",
    expected_count=80,
    overwrite=True,
) -> dict:
    import shutil
    from pathlib import Path

    report = {
        "ready": False,
        "status": "staged",
        "sku": None,
        "design_folder": None,
        "metadata_path": None,
        "mockups_folder": None,
        "image_count": 0,
        "issues": [],
        "warnings": [],
        "image_files": [],
    }

    zip_filename = getattr(uploaded_zip, "name", "") or ""
    zip_sku = infer_sku_from_zip_name(zip_filename)

    metadata = None
    metadata_issues = []
    json_sku = ""

    if uploaded_json is not None:
        try:
            metadata = load_metadata_json(uploaded_json)
            metadata_issues = validate_pipeline_metadata(metadata)
            json_sku = str(metadata.get("sku_suffix", "")).strip()
        except Exception as exc:
            metadata_issues.append(f"Could not read metadata JSON: {exc}")

    sku = json_sku or zip_sku
    sku = str(sku or "").strip()

    if not sku:
        report["issues"].append("Could not detect SKU from JSON or ZIP filename.")
        return report

    if zip_sku and json_sku and zip_sku != json_sku:
        metadata_issues.append(f"ZIP filename SKU '{zip_sku}' does not match JSON sku_suffix '{json_sku}'.")

    report["sku"] = sku

    pipeline_root = Path(pipeline_root)
    staged_folder = pipeline_root / "staged" / sku
    ready_folder = pipeline_root / "ready" / sku

    for folder in (pipeline_root / "staged", pipeline_root / "ready", pipeline_root / "active", pipeline_root / "finished"):
        folder.mkdir(parents=True, exist_ok=True)

    if overwrite:
        if staged_folder.exists():
            shutil.rmtree(staged_folder)
        if ready_folder.exists():
            shutil.rmtree(ready_folder)

    design_folder = staged_folder
    mockups_folder = design_folder / "mockups"
    mockups_folder.mkdir(parents=True, exist_ok=True)

    try:
        images = extract_mockup_images(uploaded_zip)
    except zipfile.BadZipFile:
        report["issues"].append("ZIP file could not be read.")
        return report
    except Exception as exc:
        report["issues"].append(f"Could not extract ZIP: {exc}")
        return report

    if not images:
        report["issues"].append("No supported image files found in ZIP.")

    copied_files = []
    for index, (source_name, content, ext) in enumerate(images[:expected_count], start=1):
        target_path = mockups_folder / f"{index}{ext.lower()}"
        target_path.write_bytes(content)
        copied_files.append(str(target_path))

    if len(images) > expected_count:
        report["warnings"].append(f"Found {len(images)} images; only the first {expected_count} were staged.")

    if metadata is not None:
        metadata_path = design_folder / "metadata.json"
        _write_json_file(metadata_path, metadata)
        report["metadata_path"] = str(metadata_path)

    validation = _validate_design_folder(design_folder, expected_count=expected_count)
    issues = report["issues"] + metadata_issues + validation["issues"]
    warnings = report["warnings"] + validation["warnings"]

    status = "ready" if not issues else "staged"

    if status == "ready":
        if ready_folder.exists() and overwrite:
            shutil.rmtree(ready_folder)
        shutil.move(str(staged_folder), str(ready_folder))
        design_folder = ready_folder
        mockups_folder = design_folder / "mockups"

    manifest = {
        "sku": sku,
        "status": status,
        "source_zip_name": zip_filename,
        "image_count": len(copied_files),
        "has_metadata": metadata is not None,
        "issues": issues,
        "warnings": warnings,
        "created_at": _pipeline_now_iso(),
        "updated_at": _pipeline_now_iso(),
    }
    _write_manifest(design_folder, manifest)

    report.update({
        "ready": status == "ready",
        "status": status,
        "design_folder": str(design_folder),
        "metadata_path": str(design_folder / "metadata.json") if (design_folder / "metadata.json").exists() else None,
        "mockups_folder": str(mockups_folder),
        "image_count": len(copied_files),
        "issues": issues,
        "warnings": warnings,
        "image_files": copied_files,
    })

    return report


def scan_pipeline_folders(pipeline_root="pipeline_data", expected_count=80) -> dict:
    from pathlib import Path

    pipeline_root = Path(pipeline_root)
    result = {
        "staged": [],
        "ready": [],
        "active": [],
        "finished": [],
    }

    for state in result:
        state_folder = pipeline_root / state
        if not state_folder.exists():
            continue

        for design_folder in sorted([path for path in state_folder.iterdir() if path.is_dir()]):
            manifest_path = design_folder / "manifest.json"
            validation = _validate_design_folder(design_folder, expected_count=expected_count)

            manifest = {}
            if manifest_path.exists():
                try:
                    manifest = _read_json_file(manifest_path)
                except Exception:
                    manifest = {}

            row = {
                "SKU": design_folder.name,
                "Status": state,
                "Images": validation["image_count"],
                "Has metadata": "Yes" if validation["has_metadata"] else "No",
                "Ready": "Yes" if validation["ready"] else "No",
                "Issues": "; ".join(validation["issues"]),
                "Warnings": "; ".join(validation["warnings"]),
                "Folder": str(design_folder),
            }

            if state == "ready" and not validation["ready"]:
                row["Status"] = "ready_with_issues"

            result[state].append(row)

    return result

