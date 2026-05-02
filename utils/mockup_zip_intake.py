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
