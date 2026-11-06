import argparse
import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp


SITE_URL = "https://shop.chaibukhta.ru"
CATALOG_URL = f"{SITE_URL}/collection/all"
REQUEST_DELAY_SECONDS = 0.3
PRODUCTS_PER_PAGE = 30
EXPECTED_CATEGORY_COUNT = 10
SOURCE_PRICE_WEIGHT_G = 1
TARGET_PRICE_WEIGHT_G = 10
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Category:
    name: str
    url: str


class _CategoryNavigationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.categories: dict[str, Category] = {}
        self._waiting_for_tea_submenu = False
        self._submenu_depth = 0
        self._active_anchor: tuple[str, list[str]] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag == "a":
            href = attributes.get("href")
            if href == "/collection/chay":
                self._waiting_for_tea_submenu = True
                return
            if (
                self._submenu_depth > 0
                and href
                and href.startswith("/collection/")
            ):
                self._active_anchor = (href, [])
        elif tag == "ul":
            classes = (attributes.get("class") or "").split()
            if self._waiting_for_tea_submenu and "header__collections-submenu" in classes:
                self._waiting_for_tea_submenu = False
                self._submenu_depth = 1
            elif self._submenu_depth:
                self._submenu_depth += 1

    def handle_data(self, data: str) -> None:
        if self._active_anchor is not None:
            self._active_anchor[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_anchor is not None:
            href, text_parts = self._active_anchor
            name = " ".join("".join(text_parts).split())
            self._active_anchor = None
            if name:
                self.categories[href] = Category(
                    name=name,
                    url=urljoin(SITE_URL, href),
                )
        elif tag == "ul" and self._submenu_depth:
            self._submenu_depth -= 1


class _PageConfigParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.product_count: int | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "meta":
            return

        attributes = dict(attrs)
        if attributes.get("name") != "page-config":
            return

        config_text = attributes.get("data-config")
        if not config_text:
            return

        config = json.loads(config_text)
        count = config.get("collection_products_count")
        if isinstance(count, int) and count >= 0:
            self.product_count = count


def discover_categories(html: str) -> list[Category]:
    parser = _CategoryNavigationParser()
    parser.feed(html)
    categories = list(parser.categories.values())
    if len(categories) != EXPECTED_CATEGORY_COUNT:
        raise ValueError(
            "Не удалось получить все категории чая из меню сайта: "
            f"ожидалось {EXPECTED_CATEGORY_COUNT}, найдено {len(categories)}."
        )
    return categories


def parse_page(
    html: str, category_name: str
) -> tuple[int, list[dict[str, Any]], int]:
    page_config = _PageConfigParser()
    page_config.feed(html)
    if page_config.product_count is None:
        raise ValueError(
            f"Не удалось определить количество товаров в категории «{category_name}»."
        )

    match = re.search(r"\bitems\s*:\s*(\[.*?\])\s*\n\s*\}", html, re.DOTALL)
    if match is None:
        raise ValueError(
            f"Не удалось найти список товаров в категории «{category_name}»."
        )

    raw_items = json.loads(match.group(1))
    products: list[dict[str, Any]] = []
    item_errors = 0
    for item in raw_items:
        try:
            if not isinstance(item, dict):
                raise ValueError("Некорректная запись товара.")
            name = item.get("item_name")
            raw_price = item.get("item_price")
            external_id = str(item.get("item_id", "")).strip()
            if not isinstance(name, str) or not name.strip() or not external_id:
                raise ValueError("У товара отсутствует название или ID.")
            if raw_price is None:
                raise ValueError(f"У товара «{name}» отсутствует цена.")
            source_price = Decimal(str(raw_price))
            if not source_price.is_finite() or source_price < 0:
                raise ValueError(f"Некорректная цена у товара «{name}».")
            price_per_10g = (
                source_price * TARGET_PRICE_WEIGHT_G / SOURCE_PRICE_WEIGHT_G
            )
            rounded_price = int(
                price_per_10g.to_integral_value(rounding=ROUND_CEILING)
            )
            if rounded_price <= 0:
                raise ValueError(f"Некорректная цена у товара «{name}».")
            products.append(
                {
                    "external_id": external_id,
                    "category": category_name,
                    "name": name.strip(),
                    "price_per_10g": rounded_price,
                }
            )
        except (InvalidOperation, TypeError, ValueError) as error:
            item_errors += 1
            logger.warning(
                "Skipped malformed product record in category %s: %s",
                category_name,
                error,
            )

    return page_config.product_count, products, item_errors


async def _fetch_catalog(
    session: aiohttp.ClientSession,
) -> str:
    async with session.get(CATALOG_URL) as response:
        response.raise_for_status()
        return await response.text()


async def _fetch_category_page(
    session: aiohttp.ClientSession,
    category: Category,
    page: int,
) -> tuple[int, list[dict[str, Any]], int]:
    params = {"page": page} if page > 1 else None
    async with session.get(category.url, params=params) as response:
        response.raise_for_status()
        return parse_page(await response.text(), category.name)


async def _fetch_category_products(
    session: aiohttp.ClientSession,
    category: Category,
) -> tuple[list[dict[str, Any]], int]:
    product_count, first_page, errors = await _fetch_category_page(
        session, category, 1
    )
    if product_count == 0:
        return [], errors
    if not first_page and not errors:
        raise ValueError(f"В категории «{category.name}» не найдены товары.")

    products = list(first_page)
    page_count = math.ceil(product_count / PRODUCTS_PER_PAGE)
    for page in range(2, page_count + 1):
        await asyncio.sleep(REQUEST_DELAY_SECONDS)
        page_total, page_products, page_errors = await _fetch_category_page(
            session, category, page
        )
        if page_total != product_count:
            raise ValueError(
                f"Количество товаров в категории «{category.name}» "
                "изменилось во время загрузки."
            )
        if not page_products and not page_errors:
            raise ValueError(
                f"На странице {page} категории «{category.name}» нет товаров."
            )
        products.extend(page_products)
        errors += page_errors

    if len(products) + errors != product_count:
        raise ValueError(
            f"В категории «{category.name}» ожидалось {product_count} записей, "
            f"получено {len(products) + errors}."
        )
    return products, errors


async def fetch_tea_prices() -> tuple[list[dict[str, Any]], int]:
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "TeaPriceParser/1.0"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        catalog_html = await _fetch_catalog(session)
        categories = discover_categories(catalog_html)
        products: list[dict[str, Any]] = []
        error_count = 0

        for index, category in enumerate(categories):
            if index:
                await asyncio.sleep(REQUEST_DELAY_SECONDS)
            category_products, category_errors = await _fetch_category_products(
                session, category
            )
            products.extend(category_products)
            error_count += category_errors

    return products, error_count


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Собрать цены чая с сайта shop.chaibukhta.ru в JSON."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(__file__).with_name("tea_prices.json"),
        help="путь для JSON-файла (по умолчанию tea_prices.json рядом со скриптом)",
    )
    args = parser.parse_args()

    products, errors = await fetch_tea_prices()
    args.output.write_text(
        json.dumps(products, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Сохранено товаров: {len(products)}, ошибок: {errors}")
    print(f"JSON-файл: {args.output.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
