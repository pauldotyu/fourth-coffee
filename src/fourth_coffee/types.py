"""Shared application value types."""

from enum import StrEnum


class ServiceName(StrEnum):
    FOURTH_COFFEE_MCP = "fourth-coffee-mcp"
    OPS_AGENT = "ops-agent"
    CONCIERGE = "concierge"


class CatalogCategory(StrEnum):
    FLAVORS = "flavors"
    SAUCES = "sauces"
    TOPPINGS = "toppings"


class RouteName(StrEnum):
    MENU = "menu"
    QUOTE = "quote"
    OPERATIONS = "operations"
    SURPRISE = "surprise"
    GENERAL = "general"
