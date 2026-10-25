import json
from unittest.mock import AsyncMock

import pytest

from fourth_coffee.concierge import ConciergeRuntime, Settings
from fourth_coffee.concierge.presentation import render_special_reply
from fourth_coffee.concierge.routing import special_order_plan
from fourth_coffee.shop import InMemoryCoffeeShop


@pytest.mark.asyncio
async def test_surprise_uses_live_stock_and_only_confirmation_consumes_it() -> None:
    shop = InMemoryCoffeeShop()
    for index in range(8):
        quote = shop.quote_order(
            session_id="seed",
            size="double",
            flavors=["the daily", "the daily"],
        )
        shop.submit_order(
            draft_id=quote["draft_id"], session_id="seed", idempotency_key=str(index)
        )
    before = shop.check_availability()
    ops_calls: list[str] = []

    async def mcp_call(name: str, arguments: dict) -> dict:
        return getattr(shop, name)(**arguments)

    async def ops_call(session_id: str, question: str) -> str:
        request = json.loads(question.removeprefix("BREW_OPS_REQUEST "))
        ops_calls.append(request["operation"])
        return json.dumps(shop.check_availability(**request["arguments"]))

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=mcp_call,
        ops_call=ops_call,
    )
    surprise = await runtime.chat("customer", "Surprise me")

    assert surprise.source == "surprise"
    assert surprise.needs_confirmation
    assert "Double Cold Brew" in surprise.reply
    assert "Oat Milk" in surprise.reply
    assert "Hot Fudge" not in surprise.reply
    assert shop.check_availability() == before
    assert ops_calls == ["inventory_special", "verify_fulfillment"]

    confirmed = await runtime.confirm("customer")

    assert confirmed.order["order"]["size"]["sku"] == "DOUBLE"
    assert [item["sku"] for item in confirmed.order["order"]["flavors"]] == [
        "COLD_BREW",
        "COLD_BREW",
    ]
    inventory = shop.check_availability()
    assert (
        next(
            item["remaining"]
            for item in inventory["flavors"]
            if item["sku"] == "COLD_BREW"
        )
        == 10
    )


def test_recommendation_combines_last_coffees_instead_of_overallocating() -> None:
    inventory = InMemoryCoffeeShop().check_availability()
    inventory["flavors"] = [
        {"name": "Cold Brew", "remaining": 1, "available": True},
        {"name": "Oat Latte", "remaining": 1, "available": True},
        {"name": "The Daily", "remaining": 0, "available": True},
    ]

    plan = special_order_plan(inventory)

    assert plan.flavors == ["Cold Brew", "Oat Latte"]
    assert "a pair of Cold Brew and Oat Latte" in render_special_reply(inventory)


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["flavors", "sauces", "toppings"])
async def test_surprise_does_not_quote_a_recipe_without_enough_stock(
    category: str,
) -> None:
    inventory = InMemoryCoffeeShop().check_availability()
    for item in inventory[category]:
        item["remaining"] = 0
    if category == "flavors":
        inventory[category][0]["remaining"] = 1
    mcp_call = AsyncMock()
    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=mcp_call,
        ops_call=AsyncMock(return_value=json.dumps(inventory)),
    )

    with pytest.raises(RuntimeError, match="Ops inventory response"):
        await runtime.chat("customer", "Surprise me")

    mcp_call.assert_not_called()
    with pytest.raises(RuntimeError, match="no coffee waiting"):
        await runtime.confirm("customer")
