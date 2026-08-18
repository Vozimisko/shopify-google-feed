"""
Generate product feeds for Google Ads (dynamic remarketing) and Google
Merchant Center from the public Shopify products.json endpoint.

Outputs:
    feed.tsv                  - Google Ads dynamic remarketing schema
    merchant_center_feed.tsv  - Merchant Center product feed schema

Both are UTF-8 (no BOM), tab-separated, written atomically after validation.

Prices are treated as VAT-inclusive (Croatian storefront), which is what
Merchant Center expects for HR.
"""

import argparse
import csv
import html
import json
import os
import re
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

STORE_URL = "https://babynatur.hr"
CURRENCY = "EUR"
TIMEOUT = 30
PAGE_LIMIT = 250
MAX_PAGES = 40  # safety stop: 10,000 products

ADS_OUTPUT = "feed.tsv"
MC_OUTPUT = "merchant_center_feed.tsv"

BRAND_FALLBACK = "Baby Natur"
CONDITION = "new"
# Set this once in Merchant Center's taxonomy and it stops a lot of
# mis-categorisation. Leave "" to let Google guess.
GOOGLE_PRODUCT_CATEGORY = ""

# Products matching any of these (in handle, title, or product_type) are kept
# out of the Merchant Center feed. Gift cards are restricted by Google policy.
MC_EXCLUDE_PATTERNS = [
    r"poklon[\s\-_]?bon",
    r"gift[\s\-_]?card",
]

# Shopify option name (lowercased) -> Merchant Center variant attribute.
# Extend this as you add options; unmapped names are reported at the end of
# every run so they don't silently disappear.
OPTION_MAP = {
    "boja": "color",
    "color": "color",
    "colour": "color",
    "veličina": "size",
    "velicina": "size",
    "size": "size",
    "dob": "size",
    "uzrast": "size",
    "godine": "size",
    "materijal": "material",
    "material": "material",
    "sloj": "material",
    "debljina": "material",
    "uzorak": "pattern",
    "dezen": "pattern",
    "pattern": "pattern",
    "motiv": "pattern",
}
# Slots an unmapped option may fall back into, in priority order.
FALLBACK_SLOTS = ("color", "size", "material", "pattern")

TITLE_MAX = 150
DESCRIPTION_MAX = 5000
MAX_ADDITIONAL_IMAGES = 10

# Validation guardrails.
MIN_EXPECTED_ROWS = 50
MAX_SHRINK_RATIO = 0.7  # fail if the feed drops below 70% of the previous run
MAX_REJECT_RATIO = 0.05  # fail if more than 5% of rows are unusable...
MIN_REJECT_ALLOWANCE = 3  # ...but always tolerate this many, so a small
#                           catalogue isn't held hostage by the percentage


class FeedError(Exception):
    """Raised when the feed is unfit to upload."""


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def clean_text(value: Optional[str]) -> str:
    """Strip HTML, unescape entities, collapse whitespace.

    Tags are removed before unescaping so that escaped markup in the source
    (&lt;script&gt;) cannot become live markup in the output.
    """
    if not value:
        return ""
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"</\s*(p|div|li|tr|h[1-6])\s*>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def tsv_safe(value: Any) -> str:
    """Neutralise characters that would break a tab-separated row."""
    if value is None:
        return ""
    text = str(value)
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def normalize_url(src: str) -> str:
    if not src:
        return ""
    if src.startswith("//"):
        return f"https:{src}"
    return src


def format_price(price_value: Any) -> str:
    if price_value in (None, ""):
        return ""
    try:
        return f"{float(price_value):.2f} {CURRENCY}"
    except (TypeError, ValueError):
        return ""


def to_float(price_value: Any) -> Optional[float]:
    if price_value in (None, ""):
        return None
    try:
        return float(price_value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch_products(session: Optional[requests.Session] = None) -> List[Dict[str, Any]]:
    """Fetch every published product, following pagination.

    products.json caps at 250 per page and gives no total count, so the only
    way to know you have everything is to page until a short page comes back.
    """
    session = session or requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "hr-HR,hr;q=0.9,en;q=0.8",
        "Referer": f"{STORE_URL}/",
    }

    products: List[Dict[str, Any]] = []

    for page in range(1, MAX_PAGES + 1):
        url = f"{STORE_URL}/products.json?limit={PAGE_LIMIT}&page={page}"
        response = session.get(url, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise FeedError(f"page {page} did not return JSON: {exc}") from exc

        batch = data.get("products", [])
        if not isinstance(batch, list):
            raise FeedError("unexpected JSON: 'products' is not a list")

        products.extend(batch)

        if len(batch) < PAGE_LIMIT:
            break
    else:
        raise FeedError(
            f"stopped after {MAX_PAGES} pages - pagination may not be terminating"
        )

    if not products:
        raise FeedError("endpoint returned zero products")

    return products


# --------------------------------------------------------------------------
# product / variant extraction
# --------------------------------------------------------------------------

def variant_option_values(product: Dict[str, Any], variant: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Pair each option name with this variant's value: [("Boja", "Krem"), ...]."""
    names = []
    for option in product.get("options", []) or []:
        if isinstance(option, dict):
            names.append(clean_text(option.get("name", "")))
        else:
            names.append(clean_text(str(option)))

    pairs = []
    for index, name in enumerate(names, start=1):
        value = clean_text(variant.get(f"option{index}", ""))
        if value and value.lower() != "default title":
            pairs.append((name, value))
    return pairs


def map_variant_attributes(
    pairs: List[Tuple[str, str]], unmapped: set
) -> Dict[str, str]:
    """Turn option pairs into Merchant Center color/size/material/pattern.

    Merchant Center requires variants sharing an item_group_id to differ by at
    least one of these, so an option that lands nowhere is a real problem -
    unmapped names are collected and reported rather than dropped.
    """
    attrs: Dict[str, str] = {}
    leftovers: List[str] = []

    for name, value in pairs:
        key = OPTION_MAP.get(name.lower())
        if key and key not in attrs:
            attrs[key] = value
        elif key:
            leftovers.append(value)  # duplicate mapping, e.g. two size-ish options
        else:
            unmapped.add(name)
            leftovers.append(value)

    for value in leftovers:
        for slot in FALLBACK_SLOTS:
            if slot not in attrs:
                attrs[slot] = value
                break

    return attrs


def build_variant_image(product: Dict[str, Any], variant: Dict[str, Any]) -> str:
    """Prefer the variant's own image, fall back to the product's first."""
    featured = variant.get("featured_image")
    if isinstance(featured, dict) and featured.get("src"):
        return normalize_url(featured["src"])

    images = product.get("images", []) or []
    if images:
        return normalize_url(images[0].get("src", ""))

    image = product.get("image")
    if isinstance(image, dict):
        return normalize_url(image.get("src", ""))
    return ""


def additional_images(product: Dict[str, Any], primary: str) -> List[str]:
    extras = []
    for image in product.get("images", []) or []:
        url = normalize_url(image.get("src", ""))
        if url and url != primary and url not in extras:
            extras.append(url)
        if len(extras) >= MAX_ADDITIONAL_IMAGES:
            break
    return extras


def is_excluded_from_mc(product: Dict[str, Any]) -> bool:
    haystack = " ".join(
        str(product.get(field, "") or "")
        for field in ("handle", "title", "product_type")
    ).lower()
    return any(re.search(pattern, haystack) for pattern in MC_EXCLUDE_PATTERNS)


def resolve_prices(variant: Dict[str, Any]) -> Tuple[str, str]:
    """Return (price, sale_price) in Google's semantics.

    Shopify's `price` is what the customer pays today and `compare_at_price`
    is the higher struck-through figure. Google inverts that: `price` is the
    regular price and `sale_price` the discounted one. A compare_at that is
    absent, zero, or not actually higher means there is no sale, and
    sale_price must then be omitted entirely rather than echoed.
    """
    current = to_float(variant.get("price"))
    compare = to_float(variant.get("compare_at_price"))

    if current is None:
        return "", ""

    if compare is not None and compare > current:
        return format_price(compare), format_price(current)

    return format_price(current), ""


def build_rows(products: List[Dict[str, Any]]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[str]]:
    """Build (ads_rows, mc_rows, warnings) from the fetched products."""
    ads_rows: List[Dict[str, str]] = []
    mc_rows: List[Dict[str, str]] = []
    warnings: List[str] = []
    unmapped_options: set = set()

    for product in products:
        product_id = product.get("id")
        handle = product.get("handle", "")
        if not product_id or not handle:
            warnings.append(f"product {product_id or '?'} missing id/handle - skipped")
            continue

        product_title = clean_text(product.get("title", ""))
        description = truncate(clean_text(product.get("body_html", "")), DESCRIPTION_MAX)
        brand = clean_text(product.get("vendor", "")) or BRAND_FALLBACK
        product_type = clean_text(product.get("product_type", ""))
        excluded = is_excluded_from_mc(product)

        if not description:
            # description is required by Merchant Center. Falling back to the
            # title keeps the item eligible instead of dropping it, but it is a
            # weak description - fix the product in Shopify.
            description = product_title
            warnings.append(
                f"product {product_id} ({product_title}) has no description - "
                "using the title as a fallback"
            )

        for variant in product.get("variants", []) or []:
            variant_id = variant.get("id")
            if not variant_id:
                continue

            pairs = variant_option_values(product, variant)
            variant_label = " / ".join(value for _, value in pairs)

            feed_id = f"shopify_ZZ_{product_id}_{variant_id}"
            link = f"{STORE_URL}/products/{handle}?variant={variant_id}"
            image = build_variant_image(product, variant)
            price, sale_price = resolve_prices(variant)

            title = f"{product_title} - {variant_label}" if variant_label else product_title
            title = truncate(title, TITLE_MAX)

            if not price:
                warnings.append(f"{feed_id}: no usable price - skipped")
                continue
            if not image:
                warnings.append(f"{feed_id}: no image")

            # ---- Google Ads dynamic remarketing row
            ads_rows.append({
                "ID": feed_id,
                "Item title": title,
                "Final URL": link,
                "Image URL": image,
                "Price": price,
                "Description": description,
                "Item subtitle": variant_label,
                "Sale price": sale_price,
            })

            if excluded:
                continue

            # ---- Merchant Center row
            attrs = map_variant_attributes(pairs, unmapped_options)
            sku = clean_text(variant.get("sku", ""))
            grams = variant.get("grams")

            row = {
                "id": feed_id,
                "item_group_id": str(product_id),
                "title": title,
                "description": description,
                "link": link,
                "image_link": image,
                "additional_image_link": ",".join(additional_images(product, image)),
                "availability": "in_stock" if variant.get("available") else "out_of_stock",
                "condition": CONDITION,
                "price": price,
                "sale_price": sale_price,
                "brand": brand,
                "mpn": sku,
                # With a brand and an MPN, Google can identify the product;
                # without either it needs to be told the identifiers don't exist.
                "identifier_exists": "yes" if sku else "no",
                "google_product_category": GOOGLE_PRODUCT_CATEGORY,
                "product_type": product_type,
                "color": attrs.get("color", ""),
                "size": attrs.get("size", ""),
                "material": attrs.get("material", ""),
                "pattern": attrs.get("pattern", ""),
                "shipping_weight": f"{int(grams)} g" if isinstance(grams, (int, float)) and grams else "",
            }
            mc_rows.append(row)

    if unmapped_options:
        warnings.append(
            "option names not in OPTION_MAP (assigned by fallback, please map "
            "them explicitly): " + ", ".join(sorted(unmapped_options))
        )

    return ads_rows, mc_rows, warnings


# --------------------------------------------------------------------------
# validation + atomic write
# --------------------------------------------------------------------------

ADS_COLUMNS = [
    "ID", "Item title", "Final URL", "Image URL",
    "Price", "Description", "Item subtitle", "Sale price",
]

MC_COLUMNS = [
    "id", "item_group_id", "title", "description", "link", "image_link",
    "additional_image_link", "availability", "condition", "price", "sale_price",
    "brand", "mpn", "identifier_exists", "google_product_category",
    "product_type", "color", "size", "material", "pattern", "shipping_weight",
]

MC_REQUIRED = ["id", "title", "description", "link", "image_link",
               "availability", "condition", "price", "brand"]


def previous_row_count(path: str) -> Optional[int]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return max(sum(1 for _ in fh) - 1, 0)
    except OSError:
        return None


def validate(rows: List[Dict[str, str]], columns: List[str], required: List[str],
             path: str, label: str) -> Tuple[List[Dict[str, str]], List[str]]:
    """Quarantine unusable rows; fail only if the feed as a whole is unsound.

    One malformed product out of hundreds should not stop the other hundreds
    from reaching Google, so bad rows are dropped and reported rather than
    raising. The job still fails if too many rows are bad at once, which is
    the signature of an upstream change rather than one neglected product.
    """
    good: List[Dict[str, str]] = []
    rejected: List[str] = []
    seen: set = set()

    for row in rows:
        row_id = row.get(columns[0], "?")

        missing = [field for field in required if not row.get(field)]
        if missing:
            rejected.append(f"{row_id}: missing {', '.join(missing)}")
            continue

        if row_id in seen:
            rejected.append(f"{row_id}: duplicate ID")
            continue

        seen.add(row_id)
        good.append(row)

    if not good:
        raise FeedError(f"{label}: every row was rejected")

    reject_ratio = len(rejected) / max(len(rows), 1)
    if len(rejected) > MIN_REJECT_ALLOWANCE and reject_ratio > MAX_REJECT_RATIO:
        raise FeedError(
            f"{label}: rejected {len(rejected)} of {len(rows)} rows "
            f"({reject_ratio:.0%}), above the {MAX_REJECT_RATIO:.0%} ceiling. "
            "This looks like an upstream change, not a data-entry problem."
        )

    if len(good) < MIN_EXPECTED_ROWS:
        raise FeedError(f"{label}: only {len(good)} usable rows, expected at least {MIN_EXPECTED_ROWS}")

    previous = previous_row_count(path)
    if previous and len(good) < previous * MAX_SHRINK_RATIO:
        raise FeedError(
            f"{label}: row count dropped from {previous} to {len(good)} - "
            "refusing to overwrite. Re-run with --force if this is intentional."
        )

    return good, rejected


def write_tsv(rows: List[Dict[str, str]], columns: List[str], path: str) -> None:
    """Write to a temp file in the same directory, then atomically rename.

    This is what stops a partial or failed run from replacing a good feed with
    a broken one that then gets uploaded.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=columns, delimiter="\t",
                quoting=csv.QUOTE_NONE, escapechar=None, lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({key: tsv_safe(row.get(key, "")) for key in columns})
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Shopify product feeds.")
    parser.add_argument("--from-json", help="read products from a local JSON file instead of the store")
    parser.add_argument("--force", action="store_true", help="skip the row-count shrink guard")
    parser.add_argument("--ads-output", default=ADS_OUTPUT)
    parser.add_argument("--mc-output", default=MC_OUTPUT)
    args = parser.parse_args()

    try:
        if args.from_json:
            with open(args.from_json, encoding="utf-8") as fh:
                products = json.load(fh).get("products", [])
        else:
            products = fetch_products()

        ads_rows, mc_rows, warnings = build_rows(products)
        rejected: List[str] = []

        if not args.force:
            ads_rows, ads_rejected = validate(
                ads_rows, ADS_COLUMNS, ["ID", "Item title", "Final URL", "Price"],
                args.ads_output, "ads feed")
            mc_rows, mc_rejected = validate(
                mc_rows, MC_COLUMNS, MC_REQUIRED, args.mc_output, "merchant center feed")
            rejected = [f"ads: {r}" for r in ads_rejected] + [f"mc: {r}" for r in mc_rejected]

        write_tsv(ads_rows, ADS_COLUMNS, args.ads_output)
        write_tsv(mc_rows, MC_COLUMNS, args.mc_output)

    except (FeedError, requests.RequestException, OSError) as exc:
        print(f"FEED GENERATION FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Products fetched:  {len(products)}")
    print(f"Ads feed rows:     {len(ads_rows)} -> {args.ads_output}")
    print(f"Merchant rows:     {len(mc_rows)} -> {args.mc_output}")
    print(f"On sale:           {sum(1 for r in mc_rows if r['sale_price'])}")
    print(f"Out of stock:      {sum(1 for r in mc_rows if r['availability'] == 'out_of_stock')}")

    if rejected:
        print(f"\n{len(rejected)} row(s) rejected and excluded from the feed:")
        for entry in rejected[:25]:
            print(f"  - {entry}")
        if len(rejected) > 25:
            print(f"  ... and {len(rejected) - 25} more")

    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for warning in warnings[:25]:
            print(f"  - {warning}")
        if len(warnings) > 25:
            print(f"  ... and {len(warnings) - 25} more")

    return 0


if __name__ == "__main__":
    sys.exit(main())
