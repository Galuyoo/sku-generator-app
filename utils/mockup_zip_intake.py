import os
import re
import zipfile
from io import BytesIO
from pathlib import PurePosixPath

import dropbox
from PIL import Image, UnidentifiedImageError


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
    invalid_images = validate_mockup_images(images[:expected_count])
    if image_count == 0:
        errors.append("No supported image files found.")
    elif image_count < expected_count:
        warnings.append(f"Found {image_count}/{expected_count} images.")
    elif image_count > expected_count:
        warnings.append(f"Found {image_count} images; only the first {expected_count} will be uploaded.")

    if invalid_images:
        invalid_labels = ", ".join(
            f"{item['target_number']} ({item['source']}: {item['error']})"
            for item in invalid_images[:8]
        )
        remaining = len(invalid_images) - 8
        if remaining > 0:
            invalid_labels = f"{invalid_labels}; +{remaining} more"
        errors.append(f"Invalid/corrupt image files: {invalid_labels}")

    return {
        "filename": filename,
        "sku": sku,
        "images_found": image_count,
        "invalid_images": invalid_images,
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


def validate_image_bytes(content: bytes) -> tuple[bool, str]:
    if not content:
        return False, "empty file"

    try:
        with Image.open(BytesIO(content)) as img:
            img.verify()
        with Image.open(BytesIO(content)) as img:
            img.load()
    except UnidentifiedImageError:
        return False, "not a readable image"
    except Exception as exc:
        return False, str(exc)

    return True, ""


def validate_mockup_images(images: list[tuple[str, bytes, str]]) -> list[dict]:
    invalid = []
    for index, (source_name, content, ext) in enumerate(images, start=1):
        ok, error = validate_image_bytes(content)
        if not ok:
            invalid.append({
                "source": source_name,
                "target_number": index,
                "target": f"{index}{ext.lower()}",
                "error": error,
            })
    return invalid


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

        ok, error = validate_image_bytes(content)
        if not ok:
            failed.append({"source": source_name, "target": target_name, "error": f"invalid image: {error}"})
            continue

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

def attach_metadata_to_pipeline_design(
    sku: str,
    uploaded_json,
    pipeline_root="pipeline_data",
    expected_count=80,
    overwrite=True,
) -> dict:
    import shutil
    from pathlib import Path

    report = {
        "ready": False,
        "status": "staged",
        "sku": sku,
        "design_folder": None,
        "metadata_path": None,
        "mockups_folder": None,
        "image_count": 0,
        "issues": [],
        "warnings": [],
    }

    sku = str(sku or "").strip()
    if not sku:
        report["issues"].append("Select a staged design first.")
        return report

    pipeline_root = Path(pipeline_root)
    staged_folder = pipeline_root / "staged" / sku
    ready_folder = pipeline_root / "ready" / sku

    if not staged_folder.exists():
        report["issues"].append(f"Staged design folder not found: {staged_folder}")
        return report

    try:
        metadata = load_metadata_json(uploaded_json)
    except Exception as exc:
        report["issues"].append(f"Could not read metadata JSON: {exc}")
        return report

    metadata_issues = validate_pipeline_metadata(metadata)
    json_sku = str(metadata.get("sku_suffix", "")).strip()

    if json_sku and json_sku != sku:
        metadata_issues.append(f"Selected SKU '{sku}' does not match JSON sku_suffix '{json_sku}'.")

    metadata_path = staged_folder / "metadata.json"
    if metadata_path.exists() and not overwrite:
        report["issues"].append("metadata.json already exists. Enable overwrite to replace it.")
        return report

    _write_json_file(metadata_path, metadata)

    validation = _validate_design_folder(staged_folder, expected_count=expected_count)
    issues = metadata_issues + validation["issues"]
    warnings = validation["warnings"]

    status = "ready" if not issues else "staged"
    design_folder = staged_folder

    if status == "ready":
        if ready_folder.exists():
            if overwrite:
                shutil.rmtree(ready_folder)
            else:
                report["issues"].append(f"Ready folder already exists: {ready_folder}")
                return report

        shutil.move(str(staged_folder), str(ready_folder))
        design_folder = ready_folder

    manifest = {
        "sku": sku,
        "status": status,
        "source_zip_name": None,
        "image_count": validation["image_count"],
        "has_metadata": True,
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
        "metadata_path": str(design_folder / "metadata.json"),
        "mockups_folder": str(design_folder / "mockups"),
        "image_count": validation["image_count"],
        "issues": issues,
        "warnings": warnings,
    })

    return report

def replace_pipeline_design_mockups(
    sku: str,
    uploaded_zip,
    pipeline_root="pipeline_data",
    expected_count=80,
    overwrite=True,
) -> dict:
    import shutil
    from pathlib import Path

    report = {
        "ready": False,
        "status": "staged",
        "sku": sku,
        "design_folder": None,
        "metadata_path": None,
        "mockups_folder": None,
        "image_count": 0,
        "issues": [],
        "warnings": [],
        "image_files": [],
    }

    sku = str(sku or "").strip()
    if not sku:
        report["issues"].append("Select a staged design first.")
        return report

    pipeline_root = Path(pipeline_root)
    staged_folder = pipeline_root / "staged" / sku
    ready_folder = pipeline_root / "ready" / sku

    if staged_folder.exists():
        design_folder = staged_folder
    elif ready_folder.exists():
        design_folder = ready_folder
        report["status"] = "ready"
    else:
        report["issues"].append(f"Design folder not found for SKU: {sku}")
        return report

    mockups_folder = design_folder / "mockups"

    if mockups_folder.exists():
        if overwrite:
            shutil.rmtree(mockups_folder)
        else:
            report["issues"].append("mockups folder already exists. Enable overwrite to replace it.")
            return report

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

    validation = _validate_design_folder(design_folder, expected_count=expected_count)
    issues = report["issues"] + validation["issues"]
    warnings = report["warnings"] + validation["warnings"]

    status = "ready" if not issues else "staged"

    if design_folder == ready_folder and status == "staged":
        if staged_folder.exists():
            shutil.rmtree(staged_folder)
        shutil.move(str(ready_folder), str(staged_folder))
        design_folder = staged_folder
        mockups_folder = design_folder / "mockups"

    elif design_folder == staged_folder and status == "ready":
        if ready_folder.exists():
            if overwrite:
                shutil.rmtree(ready_folder)
            else:
                report["issues"].append(f"Ready folder already exists: {ready_folder}")
                return report
        shutil.move(str(staged_folder), str(ready_folder))
        design_folder = ready_folder
        mockups_folder = design_folder / "mockups"

    manifest = {
        "sku": sku,
        "status": status,
        "source_zip_name": getattr(uploaded_zip, "name", None),
        "image_count": len(copied_files),
        "has_metadata": (design_folder / "metadata.json").exists(),
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

def _ensure_dropbox_folder(dbx, folder_path: str) -> None:
    folder_path = "/" + folder_path.strip("/")
    if folder_path == "/":
        return

    current = ""
    for part in [p for p in folder_path.strip("/").split("/") if p]:
        current = f"{current}/{part}"
        try:
            dbx.files_create_folder_v2(current)
        except Exception as exc:
            message = str(exc).lower()
            if "conflict" in message or "folder" in message and "already" in message:
                continue
            try:
                dbx.files_get_metadata(current)
            except Exception:
                raise exc


def _dropbox_direct_url(url: str) -> str:
    if not url:
        return url

    if "dl=0" in url:
        return url.replace("dl=0", "raw=1")

    if "dl=1" in url:
        return url.replace("dl=1", "raw=1")

    if "raw=1" in url:
        return url

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}raw=1"


def _get_or_create_dropbox_shared_link(dbx, path: str) -> str:
    try:
        links = dbx.sharing_list_shared_links(path=path, direct_only=True).links
        if links:
            return _dropbox_direct_url(links[0].url)
    except Exception:
        pass

    try:
        link = dbx.sharing_create_shared_link_with_settings(path)
        return _dropbox_direct_url(link.url)
    except Exception:
        links = dbx.sharing_list_shared_links(path=path, direct_only=True).links
        if links:
            return _dropbox_direct_url(links[0].url)
        raise


def upload_ready_design_to_dropbox_temp(
    dbx,
    sku: str,
    pipeline_root="pipeline_data",
    dropbox_root="/sku-generator-temp/active",
    expected_count=80,
    overwrite=True,
) -> dict:
    import shutil
    from pathlib import Path

    report = {
        "ready": False,
        "status": "ready",
        "sku": sku,
        "design_folder": None,
        "dropbox_folder": None,
        "image_links_path": None,
        "uploaded": 0,
        "linked": 0,
        "skipped": 0,
        "failed": 0,
        "issues": [],
        "warnings": [],
        "image_links": {},
    }

    sku = str(sku or "").strip()
    if not sku:
        report["issues"].append("Select a ready design first.")
        return report

    pipeline_root = Path(pipeline_root)
    ready_folder = pipeline_root / "ready" / sku
    active_folder = pipeline_root / "active" / sku

    if not ready_folder.exists():
        report["issues"].append(f"Ready design folder not found: {ready_folder}")
        return report

    validation = _validate_design_folder(ready_folder, expected_count=expected_count)
    if not validation["ready"]:
        report["issues"].extend(validation["issues"])
        return report

    mockups_folder = ready_folder / "mockups"
    image_files = [
        path for path in mockups_folder.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    image_files = sorted(image_files, key=lambda path: natural_sort_key(path.name))

    dropbox_folder = "/" + f"{dropbox_root.strip('/')}/{sku}".strip("/")
    report["dropbox_folder"] = dropbox_folder

    try:
        _ensure_dropbox_folder(dbx, dropbox_folder)
    except Exception as exc:
        report["issues"].append(f"Could not create Dropbox folder {dropbox_folder}: {exc}")
        return report

    uploaded = 0
    skipped = 0
    failed = 0
    image_links = {}

    for image_path in image_files[:expected_count]:
        stem = image_path.stem
        if not stem.isdigit():
            report["warnings"].append(f"Skipped non-numbered image: {image_path.name}")
            skipped += 1
            continue

        image_number = int(stem)
        dropbox_path = f"{dropbox_folder}/{image_path.name}"

        try:
            content = image_path.read_bytes()
            mode = dropbox.files.WriteMode.overwrite if overwrite else dropbox.files.WriteMode.add
            dbx.files_upload(content, dropbox_path, mode=mode, mute=True)
            uploaded += 1

            image_links[str(image_number)] = _get_or_create_dropbox_shared_link(dbx, dropbox_path)
        except Exception as exc:
            failed += 1
            report["issues"].append(f"Failed {image_path.name}: {exc}")

    if failed:
        status = "ready"
        design_folder = ready_folder
    else:
        if active_folder.exists():
            if overwrite:
                shutil.rmtree(active_folder)
            else:
                report["issues"].append(f"Active folder already exists: {active_folder}")
                return report

        shutil.move(str(ready_folder), str(active_folder))
        design_folder = active_folder
        status = "active"

    image_links_path = design_folder / "image_links.json"
    _write_json_file(image_links_path, image_links)

    manifest_path = design_folder / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = _read_json_file(manifest_path)
        except Exception:
            manifest = {}

    manifest.update({
        "sku": sku,
        "status": status,
        "dropbox_folder": dropbox_folder,
        "image_links_path": str(image_links_path),
        "dropbox_uploaded": failed == 0,
        "uploaded_image_count": uploaded,
        "linked_image_count": len(image_links),
        "issues": report["issues"],
        "warnings": report["warnings"],
        "updated_at": _pipeline_now_iso(),
    })
    _write_manifest(design_folder, manifest)

    report.update({
        "ready": failed == 0,
        "status": status,
        "design_folder": str(design_folder),
        "image_links_path": str(image_links_path),
        "uploaded": uploaded,
        "linked": len(image_links),
        "skipped": skipped,
        "failed": failed,
        "image_links": image_links,
    })

    return report

