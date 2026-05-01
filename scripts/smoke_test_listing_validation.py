from pathlib import Path
import sys

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from utils.listing_validation import validate_listing_metadata, validate_shopify_dataframe


def _base_df(**overrides):
    data = {
        "Handle": ["product-a", "product-a"],
        "Title": ["Product A", "Product A"],
        "Tags": ["tag one, tag two", "tag one, tag two"],
        "SEO Title": ["Product A", "Product A"],
        "SEO Description": ["Clean SEO description with enough useful text for validation."] * 2,
        "Variant SKU": ["SKU-A", "SKU-B"],
    }
    data.update(overrides)
    return pd.DataFrame(data)


def _base_metadata(**overrides):
    data = {
        "product_name": "Thriller",
        "sku_suffix": "THRILLER",
        "main_color": "Black",
        "tags": ["music", "shirt", "hoodie", "gift", "pop", "dance"],
        "page_titles": [f"Thriller Product {i}" for i in range(12)],
        "descriptions": ["A clean product description. " * 15 for _ in range(12)],
    }
    data.update(overrides)
    return data


def _assert_contains(items, expected):
    assert any(expected in item for item in items), f"Expected {expected!r} in {items!r}"


def _assert_not_contains(items, unexpected):
    assert not any(unexpected in item for item in items), f"Did not expect {unexpected!r} in {items!r}"


def test_duplicate_variant_sku_is_warning_only():
    result = validate_shopify_dataframe(_base_df(**{"Variant SKU": ["SKU-A", "sku-a"]}))

    _assert_contains(result["warnings"], "Duplicate Variant SKU")
    _assert_not_contains(result["errors"], "Duplicate Variant SKU")


def test_one_handle_multiple_titles_is_error():
    result = validate_shopify_dataframe(
        _base_df(Handle=["same-handle", "same-handle"], Title=["Product A", "Product B"])
    )

    _assert_contains(result["errors"], "One handle maps to multiple product titles")


def test_seo_title_length_70_is_error():
    result = validate_shopify_dataframe(_base_df(**{"SEO Title": ["x" * 70, "Product A"]}))

    _assert_contains(result["errors"], "SEO Title must be strictly under 70 characters")


def test_metadata_page_title_length_70_is_error():
    page_titles = [f"Thriller Product {i}" for i in range(12)]
    page_titles[0] = "x" * 70
    result = validate_listing_metadata(_base_metadata(page_titles=page_titles))

    _assert_contains(result["errors"], "Page titles must be strictly under 70 characters")


def test_low_tag_count_is_warning_only():
    result = validate_listing_metadata(_base_metadata(tags=["music", "shirt", "gift"]))

    _assert_contains(result["warnings"], "tags should include at least 6")
    _assert_not_contains(result["errors"], "tags should include at least 6")


def test_duplicate_tags_are_warning_only():
    tags = ["music", "shirt", "Shirt", "gift", "pop", "dance"]
    result = validate_listing_metadata(_base_metadata(tags=tags))

    _assert_contains(result["warnings"], "tags should be unique case-insensitively")
    _assert_not_contains(result["errors"], "tags should be unique case-insensitively")


def test_christmas_product_without_christmas_tag_is_error():
    result = validate_listing_metadata(
        _base_metadata(
            product_name="Christmas Thriller",
            tags=["music", "shirt", "hoodie", "gift", "pop", "dance"],
            page_titles=[f"Christmas Thriller Jumper {i}" for i in range(12)],
        )
    )

    _assert_contains(result["errors"], "Christmas products must include a 'christmas' tag")


def main():
    tests = [
        test_duplicate_variant_sku_is_warning_only,
        test_one_handle_multiple_titles_is_error,
        test_seo_title_length_70_is_error,
        test_metadata_page_title_length_70_is_error,
        test_low_tag_count_is_warning_only,
        test_duplicate_tags_are_warning_only,
        test_christmas_product_without_christmas_tag_is_error,
    ]

    for test in tests:
        test()
        print(f"PASS {test.__name__}")

    print(f"All {len(tests)} listing validation smoke tests passed.")


if __name__ == "__main__":
    main()
