import csv
import html
import re
from typing import Any, Dict, List, Optional

import requests

STORE_URL = "https://babynatur.hr"
PRODUCTS_ENDPOINT = f"{STORE_URL}/products.json?limit=250"
OUTPUT_FILE = "babynatur_google_ads_feed.csv"
CURRENCY = "EUR"
TIMEOUT = 30


def clean_text(value: Optional[str]) -> str:
    """Clean HTML/text for CSV output."""
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def to_shopify_cdn_url(src: str) -> str:
    """Normalize image URL."""
    if not src:
        return ""
    if src.startswith("//"):
        return f"https:{src}"
    return src


def get_products() -> List[Dict[str, Any]]:
    response = requests.get(PRODUCTS_ENDPOINT, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()

    products = data.get("products", [])
    if not isinstance(products, list):
        raise ValueError("Unexpected JSON format: 'products' is not a list.")

    return products


def build_variant_image_map(product: Dict[str, Any]) -> Dict[int, str]:
    """
    Map variant_id -> image_url using image.variant_ids where available.
    """
    variant_image_map: Dict[int, str] = {}

    for image in product.get("images", []) or []:
        image_url = to_shopify_cdn_url(image.get("src", ""))
        for variant_id in image.get("variant_ids", []) or []:
            variant_image_map[int(variant_id)] = image_url

    return variant_image_map


def pick_default_image(product: Dict[str, Any]) -> str:
    images = product.get("images", []) or []
    if images:
        return to_shopify_cdn_url(images[0].get("src", ""))
    image = product.get("image")
    if isinstance(image, dict):
        return to_shopify_cdn_url(image.get("src", ""))
    return ""


def format_price(price_value: Any) -> str:
    """
    Format price as expected in feed, e.g. '42.00 EUR'
    """
    if price_value in (None, ""):
        return ""
    try:
        return f"{float(price_value):.2f} {CURRENCY}"
    except (TypeError, ValueError):
        return str(price_value)


def build_rows(products: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    for product in products:
        product_id = product.get("id")
        handle = product.get("handle", "")
        product_title = clean_text(product.get("title", ""))
        product_description = clean_text(product.get("body_html", ""))
        default_image = pick_default_image(product)
        variant_image_map = build_variant_image_map(product)

        if not product_id or not handle:
            continue

        variants = product.get("variants", []) or []
        for variant in variants:
            variant_id = variant.get("id")
            if not variant_id:
                continue

            variant_title = clean_text(variant.get("title", ""))
            variant_price = variant.get("price", "")
            variant_compare_at = variant.get("compare_at_price", "")

            # Build feed ID to match current tracking format
            feed_id = f"shopify_ZZ_{product_id}_{variant_id}"

            # Build title
            if variant_title and variant_title.lower() != "default title":
                item_title = f"{product_title} - {variant_title}"
            else:
                item_title = product_title

            # Build product URL
            final_url = f"{STORE_URL}/products/{handle}?variant={variant_id}"

            # Pick best image
            image_url = variant_image_map.get(int(variant_id), default_image)

            # Subtitle can be variant title if useful
            item_subtitle = variant_title if variant_title.lower() != "default title" else ""

            # Prefer variant title + product type in description if available
            description = product_description

            row = {
                "ID": feed_id,
                "Item title": item_title,
                "Final URL": final_url,
                "Image URL": image_url,
                "Price": format_price(variant_price),
                "Description": description,
                "Item subtitle": item_subtitle,
                "Sale price": format_price(variant_compare_at) if variant_compare_at else "",
            }
            rows.append(row)

    return rows


def write_csv(rows: List[Dict[str, str]], output_file: str) -> None:
    fieldnames = [
        "ID",
        "Item title",
        "Final URL",
        "Image URL",
        "Price",
        "Description",
        "Item subtitle",
        "Sale price",
    ]

    with open(output_file, "w", newline="", encoding="utf-8-sig") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    products = get_products()
    rows = build_rows(products)
    write_csv(rows, OUTPUT_FILE)

    print(f"Products fetched: {len(products)}")
    print(f"Feed rows written: {len(rows)}")
    print(f"Saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()