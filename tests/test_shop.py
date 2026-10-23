from typing import Any

import pytest

from fourth_coffee.shop import InMemoryCoffeeShop


def test_menu_preserves_catalog_order_and_response_shape() -> None:
    menu = InMemoryCoffeeShop().list_menu()

    assert list(menu) == [
        "shop_name",
        "currency",
        "confirm_flow",
        "sizes",
        "flavors",
        "sauces",
        "toppings",
    ]
    assert [item["sku"] for item in menu["sizes"]] == ["SINGLE", "DOUBLE", "TRIPLE"]
    assert [item["sku"] for item in menu["flavors"]] == [
        "THE_DAILY",
        "COLD_BREW",
        "OAT_LATTE",
        "BRIGHT_SIDE",
    ]
    assert [item["sku"] for item in menu["sauces"]] == ["OAT_MILK", "WHOLE_MILK"]
    assert [item["sku"] for item in menu["toppings"]] == [
        "WHIPPED_CREAM",
        "CINNAMON",
        "CHOCOLATE_SHAVINGS",
    ]


def test_aliases_and_repeated_ingredients_preserve_request_counts() -> None:
    shop = InMemoryCoffeeShop()

    availability = shop.check_availability(
        flavors=["cold brew", "cold brew"],
        sauce="oat milk",
        toppings=["cinnamon", "whipped cream"],
    )

    assert availability["can_make_now"] is True
    assert availability["unknown_items"] == []
    assert availability["requested_items"] == [
        {
            "category": "flavors",
            "sku": "COLD_BREW",
            "name": "Cold Brew",
            "requested": 2,
            "remaining": 12,
            "available": True,
        },
        {
            "category": "sauces",
            "sku": "OAT_MILK",
            "name": "Oat Milk",
            "requested": 1,
            "remaining": 25,
            "available": True,
        },
        {
            "category": "toppings",
            "sku": "CINNAMON",
            "name": "Cinnamon",
            "requested": 1,
            "remaining": 10,
            "available": True,
        },
        {
            "category": "toppings",
            "sku": "WHIPPED_CREAM",
            "name": "Whipped Cream",
            "requested": 1,
            "remaining": 13,
            "available": True,
        },
    ]


def test_quote_creates_ready_draft_with_integer_cents() -> None:
    shop = InMemoryCoffeeShop()

    result = shop.quote_order(
        session_id="session-1",
        size="double",
        flavors=["the daily", "the daily"],
        sauce="oat milk",
        toppings=["cinnamon", "whipped cream"],
        requested_ready_in_minutes=10,
    )

    assert result["status"] == "ready"
    assert result["draft_created"] is True
    assert result["quote"]["total_cents"] == 575
    assert result["quote"]["total_display"] == "$5.75"
    assert result["quote"]["can_meet_requested_time"] is True


def test_quote_reports_unavailable_items() -> None:
    shop = InMemoryCoffeeShop()

    first = shop.quote_order(
        session_id="session-1",
        size="double",
        flavors=["the daily", "the daily"],
        toppings=["cinnamon", "cinnamon", "cinnamon"],
    )
    shop.submit_order(
        draft_id=first["draft_id"],
        session_id="session-1",
        idempotency_key="key-1",
    )
    second = shop.quote_order(
        session_id="session-2",
        size="triple",
        flavors=["the daily", "the daily", "the daily"],
        toppings=["cinnamon"] * 8,
    )

    assert second["status"] == "unavailable"
    assert second["unavailable_items"]


def test_submit_order_replays_same_idempotency_key() -> None:
    shop = InMemoryCoffeeShop()
    draft = shop.quote_order(
        session_id="session-1",
        size="single",
        flavors=["the daily"],
    )

    first = shop.submit_order(
        draft_id=draft["draft_id"],
        session_id="session-1",
        idempotency_key="same-key",
        customer_name="Ava",
    )
    second = shop.submit_order(
        draft_id=draft["draft_id"],
        session_id="session-1",
        idempotency_key="same-key",
        customer_name="Ava",
    )

    assert first["order_id"] == second["order_id"]
    assert second["idempotent_replay"] is True


def test_submit_order_mutates_inventory_only_after_confirmation() -> None:
    shop = InMemoryCoffeeShop()
    before = shop.check_availability(flavors=["the daily", "the daily"])
    draft = shop.quote_order(
        session_id="session-1",
        size="double",
        flavors=["the daily", "the daily"],
        sauce="oat milk",
        toppings=["cinnamon", "cinnamon"],
    )
    after_quote = shop.check_availability(flavors=["the daily", "the daily"])

    shop.submit_order(
        draft_id=draft["draft_id"],
        session_id="session-1",
        idempotency_key="inventory-key",
    )
    after_submit = shop.check_availability(
        flavors=["the daily", "the daily"],
        sauce="oat milk",
        toppings=["cinnamon", "cinnamon"],
    )

    assert before["requested_items"][0]["remaining"] == 18
    assert after_quote["requested_items"][0]["remaining"] == 18
    assert after_submit["requested_items"][0]["remaining"] == 16


def test_submit_order_enforces_draft_ownership_and_single_submission() -> None:
    shop = InMemoryCoffeeShop()
    draft = shop.quote_order(
        session_id="owner",
        size="single",
        flavors=["the daily"],
    )

    with pytest.raises(RuntimeError, match="Draft does not belong to this session"):
        shop.submit_order(
            draft_id=draft["draft_id"],
            session_id="other",
            idempotency_key="wrong-owner",
        )

    shop.submit_order(
        draft_id=draft["draft_id"],
        session_id="owner",
        idempotency_key="first-submit",
    )
    with pytest.raises(RuntimeError, match="Draft already submitted"):
        shop.submit_order(
            draft_id=draft["draft_id"],
            session_id="owner",
            idempotency_key="second-submit",
        )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"size": "large", "flavors": ["the daily"]},
            {
                "status": "needs_clarification",
                "message": "Pick single, double, or triple for the coffee size.",
                "options": ["Single", "Double", "Triple"],
            },
        ),
        (
            {"size": "single", "flavors": []},
            {
                "status": "needs_clarification",
                "message": "Pick a coffee before I can price it.",
            },
        ),
        (
            {"size": "double", "flavors": ["the daily"]},
            {
                "status": "needs_clarification",
                "message": "Add 1 more coffee choice for Double.",
            },
        ),
        (
            {"size": "single", "flavors": ["pistachio"]},
            {
                "status": "needs_clarification",
                "message": "I could not match every requested item to the menu.",
                "unknown_items": ["pistachio"],
            },
        ),
    ],
)
def test_quote_clarification_messages_are_stable(
    arguments: dict[str, Any],
    expected: dict[str, object],
) -> None:
    result = InMemoryCoffeeShop().quote_order(session_id="session-1", **arguments)

    assert result == expected


def test_availability_snapshot_marks_low_stock() -> None:
    shop = InMemoryCoffeeShop()
    availability = shop.check_availability()

    assert availability["can_make_now"] is True
    assert isinstance(availability["low_stock"], list)
