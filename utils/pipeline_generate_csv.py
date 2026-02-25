import os
import dropbox
from dotenv import load_dotenv
from PIL import Image
from io import BytesIO
import pandas as pd

load_dotenv("dpbox.env")

DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
TARGET_FOLDER = os.getenv("TARGET_FOLDER")
EXCLUDED_FOLDERS = os.getenv("EXCLUDED_FOLDERS").split(",")
VALID_EXTENSIONS = os.getenv("VALID_EXTENSIONS").split(",")

# === Allow large images (disables DecompressionBombWarning) ===
Image.MAX_IMAGE_PIXELS = None

# === Authenticate with Dropbox ===
dbx = dropbox.Dropbox(
    app_key=DROPBOX_APP_KEY,
    app_secret=DROPBOX_APP_SECRET,
    oauth2_refresh_token=DROPBOX_REFRESH_TOKEN
)

# === Helper: Check if a file is inside an excluded folder ===
def is_excluded(path):
    return any(path.startswith(excl.lower()) for excl in EXCLUDED_FOLDERS)

# === Helper: List image files recursively ===
def list_image_files(folder_path):
    print(f"Scanning folder: {folder_path}")
    image_files = []
    result = dbx.files_list_folder(folder_path, recursive=True)
    entries = result.entries

    while result.has_more:
        result = dbx.files_list_folder_continue(result.cursor)
        entries.extend(result.entries)

    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            lower_path = entry.path_lower
            ext = os.path.splitext(lower_path)[1].lower()
            if not is_excluded(lower_path) and ext in VALID_EXTENSIONS:
                image_files.append(entry)
    return image_files

# === Helper: Get image dimensions ===
def get_image_dimensions(file_path):
    _, res = dbx.files_download(file_path)
    img = Image.open(BytesIO(res.content))
    return img.width, img.height

# === Main: Scan and record designs ===
print("Scanning designs from Dropbox...")
image_files = list_image_files(TARGET_FOLDER)
design_data = []

for file in image_files:
    try:
        width, height = get_image_dimensions(file.path_lower)
        aspect_ratio = round(width / height, 4)
        design_data.append({
            "Design Name": os.path.basename(file.path_display),
            "Dropbox Path": file.path_display,
            "Width": width,
            "Height": height,
            "Aspect Ratio": aspect_ratio
        })
    except Exception as e:
        design_data.append({
            "Design Name": os.path.basename(file.path_display),
            "Dropbox Path": file.path_display,
            "Error": str(e)
        })

# === Save to CSV ===
df = pd.DataFrame(design_data)
df.to_csv("design_dimensions.csv", index=False)
print(" Done. File saved as 'design_dimensions.csv'")
