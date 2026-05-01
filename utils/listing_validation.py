"""Validation helpers for Shopify listing metadata and generated CSV data."""

import re
from collections import Counter
from typing import Any

import pandas as pd


REQUIRED_METADATA_KEYS = [
    "product_name",
    "sku_suffix",
    "main_color",
    "tags",
    "page_titles",
    "descriptions",
]

REQUIRED_CSV_COLUMNS = [
    "Handle",
    "Title",
    "Tags",
    "SEO Title",
    "SEO Description",
    "Variant SKU",
]

DEFAULT_EXPECTED_ITEM_COUNT = 12
DEFAULT_MIN_TAG_COUNT = 6
SEO_TITLE_MAX_CHARS = 70
DESCRIPTION_MIN_CHARS = 300
SEO_DESCRIPTION_MIN_CHARS = 120

BOOTLEG_RE = re.compile(r"\bbootleg\b", re.IGNORECASE)
SWEATSHIRT_RE = re.compile(r"\bsweatshirt\b", re.IGNORECASE)
JUMPER_RE = re.compile(r"\bjumper\b", re.IGNORECASE)


def empty_validation_result() -> dict[str, Any]:
    return {
        "errors": [],
        "warnings": [],
        "summary": {
            "metadata_files_checked": 0,
            "products_checked": 0,
            "rows_checked": 0,
        },
    }


def has_validation_errors(result: dict[str, Any] | None) -> bool:
    return bool(result and result.get("errors"))


def combine_validation_results(*results: dict[str, Any] | None) -> dict[str, Any]:
    combined = empty_validation_result()

    for result in results:
        if not result:
            continue

        combined["errors"].extend(result.get("errors", []))
        combined["warnings"].extend(result.get("warnings", []))

        summary = result.get("summary", {})
        for key in combined["summary"]:
            combined["summary"][key] += int(summary.get(key, 0) or 0)

    return combined


def validate_listing_metadata(
    metadata: dict[str, Any],
    *,
    expected_item_count: int = DEFAULT_EXPECTED_ITEM_COUNT,
    min_tag_count: int = DEFAULT_MIN_TAG_COUNT,
    label: str | None = None,
) -> dict[str, Any]:
    result = empty_validation_result()
    result["summary"]["metadata_files_checked"] = 1

    if not isinstance(metadata, dict):
        _error(result, "metadata.json must contain a JSON object", label)
        return result

    missing_keys = [key for key in REQUIRED_METADATA_KEYS if key not in metadata]
    if missing_keys:
        _error(result, f"metadata.json is missing required keys: {', '.join(missing_keys)}", label)

    product_name = _clean_text(metadata.get("product_name"))
    sku_suffix = _clean_text(metadata.get("sku_suffix"))
    main_color = _clean_text(metadata.get("main_color"))

    if "product_name" in metadata and not product_name:
        _error(result, "metadata.json product_name cannot be empty", label)
    if "sku_suffix" in metadata and not sku_suffix:
        _error(result, "metadata.json sku_suffix cannot be empty", label)
    if "main_color" in metadata and not main_color:
        _error(result, "metadata.json main_color cannot be empty", label)

    if "page_titles" in metadata:
        _validate_page_titles(
            result,
            metadata.get("page_titles"),
            expected_item_count,
            product_name,
            label,
        )

    if "descriptions" in metadata:
        _validate_descriptions(
            result,
            metadata.get("descriptions"),
            expected_item_count,
            label,
        )

    if "tags" in metadata:
        _validate_tags(
            result,
            metadata.get("tags"),
            min_tag_count,
            product_name,
            label,
        )

    return result


def validate_shopify_dataframe(
    df: pd.DataFrame,
    *,
    seo_title_max_chars: int = SEO_TITLE_MAX_CHARS,
    seo_description_min_chars: int = SEO_DESCRIPTION_MIN_CHARS,
    label: str | None = None,
) -> dict[str, Any]:
    result = empty_validation_result()

    if not isinstance(df, pd.DataFrame):
        _error(result, "Generated CSV data must be a pandas DataFrame", label)
        return result

    result["summary"]["rows_checked"] = len(df)
    if "Title" in df.columns:
        result["summary"]["products_checked"] = _non_empty_series(df["Title"]).nunique()
    elif "Handle" in df.columns:
        result["summary"]["products_checked"] = _non_empty_series(df["Handle"]).nunique()

    missing_columns = [column for column in REQUIRED_CSV_COLUMNS if column not in df.columns]
    if missing_columns:
        _error(result, f"Generated CSV is missing required columns: {', '.join(missing_columns)}", label)

    if {"Handle", "Title"}.issubset(df.columns):
        handles = _text_series(df["Handle"])
        titles = _text_series(df["Title"])

        empty_handle_rows = _row_numbers(handles.eq(""))
        if empty_handle_rows:
            _error(result, f"Empty Handle values at CSV rows: {_sample(empty_handle_rows)}", label)

        empty_title_rows = _row_numbers(titles.eq(""))
        if empty_title_rows:
            _error(result, f"Empty Title values at CSV rows: {_sample(empty_title_rows)}", label)

        handle_title_counts = (
            pd.DataFrame({"Handle": handles, "Title": titles})
            .groupby("Handle", dropna=False)["Title"]
            .nunique(dropna=False)
        )
        conflicting_handles = handle_title_counts[handle_title_counts > 1]
        if not conflicting_handles.empty:
            source = pd.DataFrame({"Handle": handles, "Title": titles})
            details = []
            for handle in conflicting_handles.index[:5]:
                title_values = sorted(source.loc[source["Handle"] == handle, "Title"].unique())
                display_handle = handle or "<empty>"
                details.append(f"{display_handle} -> {_sample(title_values, limit=4)}")
            _error(
                result,
                "One handle maps to multiple product titles: " + "; ".join(details),
                label,
            )

    if "SEO Title" in df.columns:
        seo_titles = _text_series(df["SEO Title"])
        too_long_mask = seo_titles.ne("") & seo_titles.str.len().ge(seo_title_max_chars)
        if too_long_mask.any():
            details = [
                f"row {row}: {title} ({len(title)} chars)"
                for row, title in _row_value_pairs(seo_titles, too_long_mask)[:8]
            ]
            _error(
                result,
                f"SEO Title must be strictly under {seo_title_max_chars} characters: {_sample(details, limit=8)}",
                label,
            )

    if "SEO Description" in df.columns:
        seo_descriptions = _text_series(df["SEO Description"])

        bootleg_rows = _row_numbers(seo_descriptions.str.contains(BOOTLEG_RE, na=False))
        if bootleg_rows:
            _error(result, f"SEO Description contains 'Bootleg' at CSV rows: {_sample(bootleg_rows)}", label)

        short_rows = _row_numbers(seo_descriptions.str.len().lt(seo_description_min_chars))
        if short_rows:
            _warning(
                result,
                f"SEO Description is under {seo_description_min_chars} characters at CSV rows: {_sample(short_rows)}",
                label,
            )

    if "Variant SKU" in df.columns:
        skus = _text_series(df["Variant SKU"])

        empty_sku_rows = _row_numbers(skus.eq(""))
        if empty_sku_rows:
            _error(result, f"Empty Variant SKU values at CSV rows: {_sample(empty_sku_rows)}", label)

        normalized_skus = skus.str.upper()
        duplicate_mask = normalized_skus.ne("") & normalized_skus.duplicated(keep=False)
        if duplicate_mask.any():
            duplicate_values = sorted(normalized_skus[duplicate_mask].unique())
            _warning(result, f"Duplicate Variant SKU values found: {_sample(duplicate_values)}", label)

    return result


def _validate_page_titles(
    result: dict[str, Any],
    page_titles: Any,
    expected_item_count: int,
    product_name: str,
    label: str | None,
) -> None:
    if not isinstance(page_titles, list):
        _error(result, "metadata.json page_titles must be a list", label)
        return

    if len(page_titles) != expected_item_count:
        _error(
            result,
            f"metadata.json page_titles must contain exactly {expected_item_count} items; found {len(page_titles)}",
            label,
        )

    cleaned_titles = [_clean_text(title) for title in page_titles]

    empty_indexes = [idx for idx, title in enumerate(cleaned_titles, start=1) if not title]
    if empty_indexes:
        _error(result, f"metadata.json page_titles has empty titles at positions: {_sample(empty_indexes)}", label)

    long_titles = [
        f"{idx}: {title} ({len(title)} chars)"
        for idx, title in enumerate(cleaned_titles, start=1)
        if title and len(title) >= SEO_TITLE_MAX_CHARS
    ]
    if long_titles:
        _error(result, f"Page titles must be strictly under {SEO_TITLE_MAX_CHARS} characters: {_sample(long_titles)}", label)

    duplicate_titles = _case_insensitive_duplicates(cleaned_titles)
    if duplicate_titles:
        _error(result, f"metadata.json page_titles must be unique: {_sample(duplicate_titles)}", label)

    bootleg_titles = [
        f"{idx}: {title}"
        for idx, title in enumerate(cleaned_titles, start=1)
        if BOOTLEG_RE.search(title)
    ]
    if bootleg_titles:
        _error(result, f"Page titles cannot contain 'Bootleg': {_sample(bootleg_titles)}", label)

    if "christmas" in product_name.lower():
        sweatshirt_titles = [
            f"{idx}: {title}"
            for idx, title in enumerate(cleaned_titles, start=1)
            if SWEATSHIRT_RE.search(title)
        ]
        if sweatshirt_titles:
            _warning(
                result,
                f"Christmas page titles should use 'Jumper' instead of 'Sweatshirt': {_sample(sweatshirt_titles)}",
                label,
            )

        if not any(JUMPER_RE.search(title) for title in cleaned_titles):
            _warning(result, "Christmas product page_titles should include at least one title using 'Jumper'", label)


def _validate_descriptions(
    result: dict[str, Any],
    descriptions: Any,
    expected_item_count: int,
    label: str | None,
) -> None:
    if not isinstance(descriptions, list):
        _error(result, "metadata.json descriptions must be a list", label)
        return

    if len(descriptions) != expected_item_count:
        _error(
            result,
            f"metadata.json descriptions must contain exactly {expected_item_count} items; found {len(descriptions)}",
            label,
        )

    cleaned_descriptions = [_clean_text(description) for description in descriptions]

    empty_indexes = [
        idx for idx, description in enumerate(cleaned_descriptions, start=1)
        if not description
    ]
    if empty_indexes:
        _error(result, f"metadata.json descriptions has empty descriptions at positions: {_sample(empty_indexes)}", label)

    short_descriptions = [
        f"{idx} ({len(description)} chars)"
        for idx, description in enumerate(cleaned_descriptions, start=1)
        if description and len(description) < DESCRIPTION_MIN_CHARS
    ]
    if short_descriptions:
        _warning(
            result,
            f"Descriptions should ideally be at least {DESCRIPTION_MIN_CHARS} characters: {_sample(short_descriptions)}",
            label,
        )

    bootleg_descriptions = [
        idx for idx, description in enumerate(cleaned_descriptions, start=1)
        if BOOTLEG_RE.search(description)
    ]
    if bootleg_descriptions:
        _error(result, f"Descriptions cannot contain 'Bootleg' at positions: {_sample(bootleg_descriptions)}", label)


def _validate_tags(
    result: dict[str, Any],
    tags: Any,
    min_tag_count: int,
    product_name: str,
    label: str | None,
) -> None:
    if not isinstance(tags, list):
        _error(result, "metadata.json tags must be a list", label)
        return

    cleaned_tags = [_clean_text(tag) for tag in tags]
    non_empty_tags = [tag for tag in cleaned_tags if tag]

    if not tags:
        _error(result, "metadata.json tags must not be empty", label)

    if len(non_empty_tags) < min_tag_count:
        _warning(
            result,
            f"metadata.json tags should include at least {min_tag_count} non-empty tags; found {len(non_empty_tags)}",
            label,
        )

    empty_indexes = [idx for idx, tag in enumerate(cleaned_tags, start=1) if not tag]
    if empty_indexes:
        _error(result, f"metadata.json tags has empty tags at positions: {_sample(empty_indexes)}", label)

    duplicate_tags = _case_insensitive_duplicates(cleaned_tags)
    if duplicate_tags:
        _warning(result, f"metadata.json tags should be unique case-insensitively: {_sample(duplicate_tags)}", label)

    if "christmas" in product_name.lower():
        has_christmas_tag = any(tag.lower() == "christmas" for tag in non_empty_tags)
        if not has_christmas_tag:
            _error(result, "Christmas products must include a 'christmas' tag", label)


def _error(result: dict[str, Any], message: str, label: str | None = None) -> None:
    result["errors"].append(_with_label(message, label))


def _warning(result: dict[str, Any], message: str, label: str | None = None) -> None:
    result["warnings"].append(_with_label(message, label))


def _with_label(message: str, label: str | None) -> str:
    return f"{label}: {message}" if label else message


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _text_series(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()


def _non_empty_series(series: pd.Series) -> pd.Series:
    cleaned = _text_series(series)
    return cleaned[cleaned.ne("")]


def _case_insensitive_duplicates(values: list[str]) -> list[str]:
    normalized = [value.lower() for value in values if value]
    counts = Counter(normalized)
    duplicate_keys = {value for value, count in counts.items() if count > 1}

    duplicates = []
    seen = set()
    for value in values:
        key = value.lower()
        if value and key in duplicate_keys and key not in seen:
            duplicates.append(value)
            seen.add(key)
    return duplicates


def _row_numbers(mask: pd.Series) -> list[int]:
    return [idx + 2 for idx, matches in enumerate(mask.tolist()) if matches]


def _row_value_pairs(series: pd.Series, mask: pd.Series) -> list[tuple[int, str]]:
    return [
        (idx + 2, value)
        for idx, (value, matches) in enumerate(zip(series.tolist(), mask.tolist()))
        if matches
    ]


def _sample(values: list[Any], *, limit: int = 10) -> str:
    values = list(values)
    shown = ", ".join(str(value) for value in values[:limit])
    remaining = len(values) - limit
    if remaining > 0:
        return f"{shown} (+{remaining} more)"
    return shown
