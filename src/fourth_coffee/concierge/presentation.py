"""Concierge customer-facing response rendering."""

import json
from typing import Any

from fourth_coffee.concierge.api import RoutingPlan
from fourth_coffee.concierge.routing import special_order_plan


def compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def render_special_reply(result: dict[str, Any]) -> str:
    plan = special_order_plan(result)
    if plan.flavors[0] == plan.flavors[1]:
        drink = f"Double {plan.flavors[0]}"
    else:
        drink = f"a pair of {plan.flavors[0]} and {plan.flavors[1]}"
    return (
        f"Today's special is {drink} with {plan.sauce}, "
        f"{plan.toppings[0]}, and {plan.toppings[1]}. "
    )


def render_fulfillment_failure(result: dict[str, Any]) -> str:
    unavailable = [
        str(item["name"])
        for item in result.get("requested_items", [])
        if isinstance(item, dict) and item.get("available") is False
    ]
    detail = f" Unavailable right now: {', '.join(unavailable)}." if unavailable else ""
    return f"Oops! That coffee cannot be fulfilled as requested.{detail}"


def render_operations_reply(result: dict[str, Any]) -> str:
    if isinstance(result.get("can_make_now"), bool):
        low_stock = result.get("low_stock")
        if not isinstance(low_stock, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("remaining"), int)
            for item in low_stock
        ):
            raise RuntimeError("Coffee operations returned invalid inventory data")
        stock = ", ".join(
            f"{item['name']} ({item['remaining']} left)" for item in low_stock
        )
        availability = (
            "The requested items are available."
            if result["can_make_now"]
            else render_fulfillment_failure(result)
        )
        return availability + (f" Low stock: {stock}." if stock else " No low stock.")
    if result.get("shop_name") == "Fourth Coffee":
        return render_menu_reply(result)
    if result.get("status") in ("ready", "needs_clarification", "unavailable"):
        return render_quote_reply(result)
    raise RuntimeError("Coffee operations returned an unsupported result")


def display_order_number(order_id: Any) -> str:
    digits = "".join(character for character in str(order_id) if character.isdigit())
    return digits[:2]


def ops_request(operation: str, **arguments: Any) -> str:
    return "BREW_OPS_REQUEST " + compact_json(
        {"operation": operation, "arguments": arguments}
    )


def render_menu_reply(menu: dict[str, Any]) -> str:
    sizes = ", ".join(
        f"{item['name']} {item['price_display']}" for item in menu.get("sizes", [])
    )
    flavors = ", ".join(item["name"] for item in menu.get("flavors", []))
    toppings = ", ".join(item["name"] for item in menu.get("toppings", [])[:4])
    return (
        f"Sizes: {sizes}. Flavors: {flavors}. Popular add-ons include {toppings}. "
        "Tell me a size, flavors, milk, and add-ons and I will build it."
    )


def render_quote_reply(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "needs_clarification":
        return str(
            result.get("message", "I need a little more detail for that coffee.")
        )
    if status == "unavailable":
        items = ", ".join(
            item["name"]
            for item in result.get("unavailable_items", [])
            if isinstance(item, dict)
        )
        detail = f" Unavailable right now: {items}." if items else ""
        return f"I cannot build that exactly as requested.{detail}"
    quote = result.get("quote")
    order = result.get("order")
    if not isinstance(quote, dict) or not isinstance(order, dict):
        raise RuntimeError("The coffee quote is missing order or pricing details")
    sauce = order.get("sauce")
    sauce_text = f" with {sauce['name']}" if isinstance(sauce, dict) else ""
    toppings = ", ".join(str(item.get("name")) for item in order.get("toppings", []))
    toppings_text = f" plus {toppings}" if toppings else ""
    eta = quote.get("eta_minutes")
    eta_text = f" It should take about {eta} minutes." if isinstance(eta, int) else ""
    if quote.get("requested_ready_in_minutes") is not None:
        if quote.get("can_meet_requested_time"):
            eta_text += " That fits your requested timing."
        else:
            eta_text += " That is slower than your requested timing."
    names = [str(item["name"]) for item in order.get("flavors", [])]
    opening = (
        f"Your coffee is ready: Double {names[0]}"
        if len(names) == 2 and names[0] == names[1]
        else f"Your coffee is ready: {order['size']['name']} with {', '.join(names)}"
    )
    return (
        f"{opening}{sauce_text}{toppings_text}. "
        f"Total {quote['total_display']}.{eta_text} Press confirm to submit it."
    )


def render_general_reply() -> str:
    return (
        "I can show the menu, build a coffee, or check inventory for you. "
        "What sounds good?"
    )


def build_writer_prompt(
    context: str,
    message: str,
    plan: RoutingPlan,
    tool_result: Any,
    *,
    needs_confirmation: bool,
) -> str:
    return (
        f"Conversation context:\n{context}\n\n"
        f"Current customer message:\n{message}\n\n"
        f"Routing plan:\n{compact_json(plan.model_dump(mode='json'))}\n\n"
        "Needs confirmation:\n"
        f"{compact_json({'needs_confirmation': needs_confirmation})}\n\n"
        f"Authoritative result:\n{compact_json(tool_result)}"
    )
