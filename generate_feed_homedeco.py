import csv
import html
import re
import time
import requests
from typing import Any, Dict, List, Optional

STORE_URL = "https://www.homedeco.hr"
PRODUCTS_ENDPOINT = f"{STORE_URL}/wp-json/wc/store/v1/products"
OUTPUT_FILE = "feed_homedeco.csv"
PER_PAGE = 100
TIMEOUT = 30
REQUEST_DELAY = 0.5  # be polite to the server between pages

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "hr-HR,hr;q=0.9,en;q=0.8",
    "Referer": f"{STORE_URL}/",
}


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def format_price(minor_units: Any, minor_unit_count: int, currency: str) -> str:
    """Store API returns prices as strings in minor units, e.g. '1300' -> 13.00 EUR."""
    if minor_units in (None, ""):
        return ""
    try:
        amount = int(minor_units) / (10 ** minor_unit_count)
        return f"{amount:.2f} {currency}"
    except (TypeError, ValueError):
        return str(minor_units)


def fetch_all_products() -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    page = 1

    while True:
        response = requests.get(
            PRODUCTS_ENDPOINT,
            params={"per_page": PER_PAGE, "page": page},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        batch = response.json()

        if not isinstance(batch, list):
            raise ValueError("Unexpected JSON format: expected a list of products.")
        if not batch:
            break

        products.extend(batch)

        total_pages = response.headers.get("X-WP-TotalPages")
        if total_pages and page >= int(total_pages):
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    return products


def pick_image(product: Dict[str, Any]) -> str:
    images = product.get("images", []) or []
    if images:
        return images[0].get("src", "") or ""
    return ""


def build_rows(products: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    for product in products:
        product_id = product.get("id")
        permalink = product.get("permalink", "")
        if not product_id or not permalink:
            continue

        # Optionally skip out-of-stock products:
        # if not product.get("is_in_stock", True):
        #     continue

        prices = product.get("prices", {}) or {}
        currency = prices.get("currency_code", "EUR")
        minor = prices.get("currency_minor_unit", 2)

        regular_price = format_price(prices.get("regular_price"), minor, currency)
        current_price = format_price(prices.get("price"), minor, currency)
        on_sale = bool(product.get("on_sale"))

        # Google Ads convention: Price = regular price, Sale price = discounted price
        price = regular_price or current_price
        sale_price = current_price if (on_sale and current_price != regular_price) else ""

        title = clean_text(product.get("name", ""))
        description = clean_text(
            product.get("description") or product.get("short_description") or ""
        )

        categories = product.get("categories", []) or []
        subtitle = clean_text(categories[0].get("name", "")) if categories else ""

        rows.append({
            "ID": f"woo_HR_{product_id}",
            "Item title": title,
            "Final URL": permalink,
            "Image URL": pick_image(product),
            "Price": price,
            "Description": description,
            "Item subtitle": subtitle,
            "Sale price": sale_price,
        })

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
    products = fetch_all_products()
    rows = build_rows(products)
    write_csv(rows, OUTPUT_FILE)

    print(f"Products fetched: {len(products)}")
    print(f"Feed rows written: {len(rows)}")
    print(f"Saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
