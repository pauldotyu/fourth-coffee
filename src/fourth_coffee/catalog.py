"""Immutable authoritative coffee catalog."""

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

from fourth_coffee.types import CatalogCategory


@dataclass(frozen=True, slots=True)
class Size:
    name: str
    included_shots: int
    price_cents: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Ingredient:
    name: str
    stock: int
    price_cents: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"name": self.name}
        if self.price_cents is not None:
            result["price_cents"] = self.price_cents
        result["stock"] = self.stock
        return result


SIZES: Mapping[str, Size] = MappingProxyType(
    {
        "SINGLE": Size("Single", included_shots=1, price_cents=325),
        "DOUBLE": Size("Double", included_shots=2, price_cents=475),
        "TRIPLE": Size("Triple", included_shots=3, price_cents=625),
    }
)

# Sizes are named for the number of espresso shots in the drink.
DEFAULT_SIZE = "DOUBLE"

FLAVORS: Mapping[str, Ingredient] = MappingProxyType(
    {
        "THE_DAILY": Ingredient("The Daily", stock=18),
        "COLD_BREW": Ingredient("Cold Brew", stock=12),
        "OAT_LATTE": Ingredient("Oat Latte", stock=8),
        "BRIGHT_SIDE": Ingredient("Bright Side", stock=6),
    }
)

SAUCES: Mapping[str, Ingredient] = MappingProxyType(
    {
        "OAT_MILK": Ingredient("Oat Milk", stock=25, price_cents=0),
        "WHOLE_MILK": Ingredient("Whole Milk", stock=25, price_cents=0),
    }
)

TOPPINGS: Mapping[str, Ingredient] = MappingProxyType(
    {
        "WHIPPED_CREAM": Ingredient("Whipped Cream", stock=13, price_cents=75),
        "CINNAMON": Ingredient("Cinnamon", stock=10, price_cents=25),
        "CHOCOLATE_SHAVINGS": Ingredient("Chocolate Shavings", stock=9, price_cents=75),
    }
)

CATALOGS: Mapping[CatalogCategory, Mapping[str, Ingredient]] = MappingProxyType(
    {
        CatalogCategory.FLAVORS: FLAVORS,
        CatalogCategory.SAUCES: SAUCES,
        CatalogCategory.TOPPINGS: TOPPINGS,
    }
)

ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "LATTE": "OAT_LATTE",
        "OAT": "OAT_MILK",
        "WHOLE": "WHOLE_MILK",
    }
)


def normalize_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.strip().upper()).strip("_")


def routing_hints(category: CatalogCategory) -> dict[str, str]:
    catalog = CATALOGS[category]
    hints: dict[str, str] = {}
    for sku, item in catalog.items():
        terms = [item.name, sku.replace("_", " ")]
        for term in terms:
            hints.setdefault(term.lower(), sku)
    for alias, sku in ALIASES.items():
        if sku in catalog:
            hints.setdefault(alias.replace("_", " ").lower(), sku)
    return hints


def size_routing_hints() -> dict[str, str]:
    hints: dict[str, str] = {}
    for sku, size in SIZES.items():
        hints[size.name.lower()] = sku
    return hints
