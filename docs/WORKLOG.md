# SKU Generator Worklog

Last updated: 2026-05-04

## Current stable branch

`main`

Use this as the stable baseline before merging feature work.

## Active feature branches

### `auto-ui-session-cleanup`

Purpose:
- Clean up Auto tab session-state behavior.
- Separate build validation from upload-specific validation.

Status:
- `auto_upload_validation` added for upload-only checks.
- Selected upload no longer mutates `st.session_state.auto_validation`.
- `batch_only_selected` wired to the batch checkbox.
- Compile and smoke tests passed.

Next:
- Live-test Auto tab upload flow.
- Confirm duplicate SKU Tracker warning appears only in upload validation.
- Merge after testing.

### `manual-listing-builder`

Purpose:
- Replace old Manual Entry with a Manual Listing Builder.
- Generate metadata JSON, imageless CSV, or both.

Status:
- Manual JSON generation works.
- Imageless CSV generation works.
- Validation warnings do not block.
- Hard errors block downloads.
- Page title parser fixed so `|` inside titles is allowed.
- UX polish added: counts, example fill, clear form, title preview, cleaner safety display.
- Product-level CSV validation added for SEO/Tags.

Next:
- Final browser pass.
- Decide whether to merge into `main`.

### `mockup-zip-intake`

Purpose:
- Temporary replacement for manual Canva ZIP handling.
- Upload Canva ZIPs into matching Dropbox design folders.

Status:
- Added `utils/mockup_zip_intake.py`.
- Added Auto tab `Mockup ZIP intake` section.
- Supports multiple ZIPs.
- ZIP filename maps to Dropbox folder/SKU.
- Checks folder exists and metadata JSON exists.
- Extracts images, natural-sorts them, uploads as numbered files.
- Skips existing images by default.
- Optional overwrite.
- Refreshes folder analysis after successful upload.
- Compile and smoke tests passed.

Next:
- Live-test with real Dropbox folders.
- Use a ZIP named exactly like the Dropbox folder, e.g. `TESTSKU.zip`.
- Confirm folder becomes ready after upload.
- Confirm normal CSV build works after ZIP staging.

### `home-setup-dropbox-optional`

Purpose:
- Home laptop experiments for Dropbox-optional / Canva-local work.

Status:
- Parked.
- Do not merge until reviewed.

### `manual-ai-metadata-generator`

Purpose:
- Accidental/parked branch from AI discussion.

Status:
- Parked.
- Do not merge unless reviewed.
- AI generation is skipped for now until a real API/provider plan exists.

## Local tooling notes

Installed project/global skills:
- `developing-with-streamlit`
- `find-skills`

Repo-specific rule:
- `.agents/` and `skills-lock.json` should stay ignored unless we intentionally commit a project-specific skill later.

## Validation commands

Run before commits:

```powershell
python -m py_compile app.py utils\listing_validation.py utils\sku_generator.py
python scripts\smoke_test_listing_validation.py