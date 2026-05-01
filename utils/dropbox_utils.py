# utils/dropbox_utils.py

import os
import re
import time
import dropbox
from dropbox.exceptions import ApiError
from dotenv import load_dotenv

load_dotenv("dpbox.env")

## Trying to speed up image link fetching with threading
from concurrent.futures import ThreadPoolExecutor, as_completed

NUMBERED_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def _list_numbered_image_paths(
    dbx: dropbox.Dropbox,
    folder_path: str,
    total_images: int,
) -> dict[int, str]:
    """
    Build a map like {1: "/root/1.jpg", 2: "/root/2.png"} for supported
    numbered mockup images in a folder.
    """
    image_paths = {}
    entries = dbx.files_list_folder(folder_path).entries
    for entry in entries:
        if not isinstance(entry, dropbox.files.FileMetadata):
            continue

        lower_name = entry.name.lower()
        stem, ext = os.path.splitext(lower_name)
        if not stem.isdigit() or ext not in NUMBERED_IMAGE_EXTENSIONS:
            continue

        index = int(stem)
        if 1 <= index <= total_images and index not in image_paths:
            image_paths[index] = f"{folder_path}/{entry.name}"
    return image_paths

def load_dropbox_image_links_parallel(
    dbx: dropbox.Dropbox,
    folder_path: str,
    total_images: int = 80,
    max_attempts: int = 3,
    delay: float = 0.1,
    workers: int = 15,
):
    """Parallel version of load_dropbox_image_links using ThreadPoolExecutor."""
    image_links = {}
    failed = []
    image_paths = _list_numbered_image_paths(dbx, folder_path, total_images)

    def try_get_link(i):
        path = image_paths.get(i)
        if not path:
            return i, None
        attempt = 0
        while attempt < max_attempts:
            url = get_shared_link(dbx, path)
            if url:
                return i, url
            attempt += 1
            time.sleep(delay)
        return i, None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(try_get_link, i): i for i in range(1, total_images + 1)}
        for future in as_completed(futures):
            i, url = future.result()
            if url:
                image_links[i] = url
            else:
                failed.append(i)

    return image_links, failed

# -----------------------------
# Client / link helpers (yours)
# -----------------------------
def get_dropbox_client():
    """Return an authenticated Dropbox client."""
    return dropbox.Dropbox(
        app_key=os.getenv("DROPBOX_APP_KEY"),
        app_secret=os.getenv("DROPBOX_APP_SECRET"),
        oauth2_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN"),
    )


def to_direct_dropbox_link(url: str) -> str:
    """Convert a Dropbox share URL into a direct link."""
    url = re.sub(r"https://www\.dropbox\.com", "https://dl.dropboxusercontent.com", url)
    return re.sub(r"[?&](dl|raw)=\d", "", url)


def get_shared_link(dbx: dropbox.Dropbox, path: str) -> str | None:
    """Get or create a Dropbox shared link for a file and return a direct link."""
    try:
        links = dbx.sharing_list_shared_links(path=path, direct_only=True).links
        if links:
            return to_direct_dropbox_link(links[0].url)
        res = dbx.sharing_create_shared_link_with_settings(path)
        return to_direct_dropbox_link(res.url)
    except ApiError:
        return None


def load_dropbox_image_links(
    dbx: dropbox.Dropbox,
    folder_path: str,
    total_images: int = 80,
    max_attempts: int = 5,
    delay: float = 0.1,
):
    """Load direct links for a numbered set of images in a Dropbox folder."""
    image_links = {}
    failed = []
    image_paths = _list_numbered_image_paths(dbx, folder_path, total_images)
    for i in range(1, total_images + 1):
        path = image_paths.get(i)
        if not path:
            failed.append(i)
            continue

        attempt = 0
        success = False
        while attempt < max_attempts:
            url = get_shared_link(dbx, path)
            if url:
                image_links[i] = url
                success = True
                break
            attempt += 1
            time.sleep(delay)
        if not success:
            failed.append(i)
    return image_links, failed


# -----------------------------
# New: path / move utilities
# -----------------------------
def path_exists(dbx: dropbox.Dropbox, path: str) -> bool:
    """Return True if a file/folder exists at path."""
    try:
        dbx.files_get_metadata(path)
        return True
    except ApiError:
        return False


def _ensure_folder(dbx: dropbox.Dropbox, path: str):
    """Create a folder if it doesn't already exist."""
    try:
        dbx.files_get_metadata(path)
    except ApiError:
        dbx.files_create_folder_v2(path, autorename=False)


def move_to_finished(
    dbx: dropbox.Dropbox,
    designs_root: str,
    folder_name: str,
    finished_dir: str = "finished",
) -> str:
    """
    Move /<root>/<folder_name> -> /<root>/<finished_dir>/<folder_name>.
    Returns the final destination path (autorename may add a suffix if needed).
    """
    # Normalize paths for Dropbox (always '/')
    root = designs_root.rstrip("/")
    src = f"{root}/{folder_name}"
    dst_root = f"{root}/{finished_dir}"
    dst = f"{dst_root}/{folder_name}"

    # No-op if it's already under finished/
    if src.startswith(dst_root + "/"):
        return src

    # Ensure the destination folder exists
    _ensure_folder(dbx, dst_root)

    # Move (server-side, fast). autorename handles name collisions.
    res = dbx.files_move_v2(src, dst, autorename=True)
    return res.metadata.path_display
