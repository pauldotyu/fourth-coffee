import json
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fourth_coffee.concierge import (
    INDEX_HTML,
    ConciergeRuntime,
    RoutingPlan,
    Settings,
    display_order_number,
    heuristic_plan,
)
from fourth_coffee.concierge import runtime as concierge_runtime
from fourth_coffee.shop import InMemoryCoffeeShop


class FakeAgent:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    async def run(self, prompt: str, **_: Any) -> Any:
        self.prompts.append(prompt)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_concierge_quote_flow_uses_confirm() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        if name == "quote_order":
            return {
                "status": "ready",
                "draft_created": True,
                "draft_id": "draft-123",
                "order": {
                    "size": {"name": "Double"},
                    "flavors": [{"name": "The Daily"}, {"name": "The Daily"}],
                    "sauce": {"name": "Oat Milk"},
                    "toppings": [{"name": "Cinnamon"}],
                },
                "quote": {
                    "total_display": "$5.75",
                    "eta_minutes": 10,
                    "requested_ready_in_minutes": None,
                    "can_meet_requested_time": True,
                },
            }
        if name == "submit_order":
            return {
                "order_id": "brew-1",
                "customer_name": arguments["customer_name"],
                "pickup_eta_minutes": 10,
            }
        raise AssertionError(name)

    settings = Settings(
        openai_base_url="",
        openai_chat_model="",
        ops_agent_url="http://ops-agent:8202",
        fourth_coffee_mcp_url="http://fourth-coffee-mcp:8101/mcp/",
    )

    async def fake_ops(session_id: str, question: str) -> str:
        assert session_id == "session-1"
        assert '"operation":"verify_fulfillment"' in question
        return json.dumps({"can_make_now": True, "requested_items": []})

    runtime = ConciergeRuntime(
        settings,
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    chat = await runtime.chat(
        "session-1",
        "Build me a double the daily with oat milk and chocolate shavings.",
    )
    confirm = await runtime.confirm("session-1", "Ava")

    assert chat.needs_confirmation is True
    assert confirm.order["order_id"] == "brew-1"
    assert confirm.reply.startswith("Order 1 is submitted")
    assert calls[0][0] == "quote_order"
    assert calls[1][0] == "submit_order"


@pytest.mark.asyncio
async def test_concierge_operations_route_uses_ops_agent() -> None:
    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        raise AssertionError(f"unexpected MCP call: {name} {arguments}")

    async def fake_ops(session_id: str, question: str) -> str:
        assert session_id == "session-1"
        assert "running low" in question
        return json.dumps(
            {
                "can_make_now": True,
                "low_stock": [{"name": "Cold Brew", "remaining": 2}],
            }
        )

    settings = Settings(openai_base_url="", openai_chat_model="")
    runtime = ConciergeRuntime(settings, mcp_call=fake_mcp, ops_call=fake_ops)

    response = await runtime.chat("session-1", "What are you running low on tonight?")

    assert response.source == "operations"
    assert "Cold Brew" in response.reply
    assert "2 left" in response.reply
    assert "ready" not in response.reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "Sorry, the ops agent is unavailable.",
        "Your coffee is ready.",
        "{}",
        '{"status":"error","message":"MCP unavailable"}',
        '{"status":"ready"}',
        '{"can_make_now":true,"low_stock":"invalid"}',
    ],
)
async def test_operations_reject_invalid_results_instead_of_claiming_ready(
    reply: str,
) -> None:
    async def fake_ops(session_id: str, question: str) -> str:
        return reply

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        ops_call=fake_ops,
    )
    with pytest.raises(RuntimeError):
        await runtime.chat("invalid-result", "What are you running low on tonight?")


def test_heuristic_plan_extracts_quote_details() -> None:
    plan = heuristic_plan(
        "Build me a triple cold brew with oat milk, cinnamon, and whipped "
        "cream in 12 minutes"
    )

    assert plan == RoutingPlan(
        route="quote",
        size="TRIPLE",
        flavors=["COLD_BREW"],
        sauce="OAT_MILK",
        toppings=["CINNAMON", "WHIPPED_CREAM"],
        requested_ready_in_minutes=12,
        operations_question=None,
    )


def test_page_keeps_confirmation_action_visible() -> None:
    assert 'id="confirm" type="button" disabled' in INDEX_HTML
    assert "Pick your coffee..." in INDEX_HTML
    assert "Confirm and brew" in INDEX_HTML
    assert 'id="confirm" type="button" hidden' not in INDEX_HTML
    assert "form.requestSubmit(sendButton)" in INDEX_HTML
    assert "line-height: 50px" in INDEX_HTML


def test_heuristic_plan_prefers_quote_when_order_hints_present() -> None:
    plan = heuristic_plan("Triple cold brew with oat milk, how fast can it be ready?")

    assert plan.route == "quote"
    assert plan.size == "TRIPLE"
    assert "COLD_BREW" in plan.flavors


def test_heuristic_plan_handles_ops_question_after_quote() -> None:
    plan = heuristic_plan("why does it take 10 minutes to make?")
    assert plan.route == "general"


@pytest.mark.asyncio
async def test_concierge_surprise_me_generates_coffee() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        assert name == "quote_order"
        return {
            "status": "ready",
            "draft_created": True,
            "draft_id": "draft-surprise",
            "order": {
                "size": {"name": "Double"},
                "flavors": [{"name": "The Daily"}, {"name": "The Daily"}],
                "sauce": {"name": "Oat Milk"},
                "toppings": [{"name": "Whipped Cream"}, {"name": "Cinnamon"}],
            },
            "quote": {
                "total_display": "$5.75",
                "eta_minutes": 10,
                "requested_ready_in_minutes": None,
                "can_meet_requested_time": True,
            },
        }

    settings = Settings(
        openai_base_url="",
        openai_chat_model="",
        ops_agent_url="http://ops-agent:8202",
        fourth_coffee_mcp_url="http://fourth-coffee-mcp:8101/mcp/",
    )

    async def fake_ops(session_id: str, question: str) -> str:
        assert session_id == "session-surprise"
        if '"operation":"inventory_special"' in question:
            return json.dumps(InMemoryCoffeeShop().check_availability())
        assert '"operation":"verify_fulfillment"' in question
        return json.dumps({"can_make_now": True, "requested_items": []})

    runtime = ConciergeRuntime(
        settings,
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    response = await runtime.chat("session-surprise", "Surprise me!")

    assert response.needs_confirmation is True
    assert calls[0][0] == "quote_order"
    assert calls[0][1]["size"] == "DOUBLE"
    assert calls[0][1]["flavors"] == ["The Daily", "The Daily"]
    assert calls[0][1]["sauce"] == "Oat Milk"


def test_heuristic_plan_surprise_message() -> None:
    plan = heuristic_plan("Surprise me — pick whatever you're feeling today!")
    assert plan.route == "surprise"


def test_heuristic_plan_routes_specials_to_operations() -> None:
    plan = heuristic_plan("Got any coffee specials today?")

    assert plan.route == "operations"
    assert plan.operations_intent == "specials"
    assert plan.operations_question == "Got any coffee specials today?"


@pytest.mark.asyncio
async def test_special_uses_highest_inventory_from_ops() -> None:
    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        raise AssertionError(f"unexpected MCP call: {name} {arguments}")

    async def fake_ops(session_id: str, question: str) -> str:
        assert session_id == "special-session"
        assert '"operation":"inventory_special"' in question
        return json.dumps(
            {
                "can_make_now": True,
                "flavors": [
                    {
                        "name": "The Daily",
                        "remaining": 18,
                        "available": True,
                    },
                    {"name": "Cold Brew", "remaining": 6, "available": True},
                ],
                "sauces": [
                    {"name": "Oat Milk", "remaining": 25, "available": True},
                    {"name": "Whole Milk", "remaining": 25, "available": True},
                ],
                "toppings": [
                    {
                        "name": "Whipped Cream",
                        "remaining": 13,
                        "available": True,
                    },
                    {
                        "name": "Cinnamon",
                        "remaining": 10,
                        "available": True,
                    },
                ],
            }
        )

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    response = await runtime.chat("special-session", "Got any specials today?")

    assert response.source == "operations"
    assert "The Daily" in response.reply
    assert "Whipped Cream" in response.reply
    assert "Cinnamon" in response.reply


@pytest.mark.asyncio
async def test_customer_can_accept_inventory_special_in_follow_up() -> None:
    quote_arguments: dict[str, object] = {}

    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        assert name == "quote_order"
        quote_arguments.update(arguments)
        return {
            "status": "ready",
            "draft_created": True,
            "draft_id": "draft-special",
            "order": {
                "size": {"name": "Double"},
                "flavors": [
                    {"name": "The Daily"},
                    {"name": "The Daily"},
                ],
                "sauce": {"name": "Oat Milk"},
                "toppings": [
                    {"name": "Whipped Cream"},
                    {"name": "Cinnamon"},
                ],
            },
            "quote": {
                "total_display": "$5.75",
                "eta_minutes": 10,
                "requested_ready_in_minutes": None,
                "can_meet_requested_time": True,
            },
        }

    async def fake_ops(session_id: str, question: str) -> str:
        if '"operation":"inventory_special"' in question:
            return json.dumps(
                {
                    "flavors": [
                        {
                            "name": "The Daily",
                            "remaining": 18,
                            "available": True,
                        }
                    ],
                    "sauces": [
                        {
                            "name": "Oat Milk",
                            "remaining": 25,
                            "available": True,
                        }
                    ],
                    "toppings": [
                        {
                            "name": "Whipped Cream",
                            "remaining": 13,
                            "available": True,
                        },
                        {
                            "name": "Cinnamon",
                            "remaining": 10,
                            "available": True,
                        },
                    ],
                }
            )
        assert '"operation":"verify_fulfillment"' in question
        return json.dumps({"can_make_now": True, "requested_items": []})

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    special = await runtime.chat("special-follow-up", "Any specials today?")
    quote = await runtime.chat(
        "special-follow-up",
        "The daily is what I want",
    )

    assert "Double The Daily" in special.reply
    assert quote.needs_confirmation is True
    assert quote_arguments["size"] == "DOUBLE"
    assert quote_arguments["flavors"] == ["The Daily", "The Daily", "THE_DAILY"]
    assert quote_arguments["sauce"] == "Oat Milk"
    assert quote_arguments["toppings"] == ["Whipped Cream", "Cinnamon"]


@pytest.mark.asyncio
async def test_quote_details_accumulate_across_chat_turns() -> None:
    calls: list[dict[str, object]] = []

    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        assert name == "quote_order"
        calls.append(arguments)
        flavors = arguments["flavors"]
        if not isinstance(flavors, list) or len(flavors) < 2:
            return {
                "status": "needs_clarification",
                "message": "Add 1 more coffee choice for Double.",
            }
        return {
            "status": "needs_clarification",
            "message": "Ready for test assertions.",
        }

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
        ops_call=None,
    )

    await runtime.chat("multi-turn", "I want a double cold brew with oat milk")
    await runtime.chat("multi-turn", "and a second cold brew")

    assert calls[-1]["size"] == "DOUBLE"
    assert calls[-1]["flavors"] == ["COLD_BREW"]
    assert calls[-1]["sauce"] == "OAT_MILK"
    assert calls[-1]["toppings"] == []


@pytest.mark.asyncio
async def test_quote_requires_ops_fulfillment_approval() -> None:
    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        assert name == "quote_order"
        return {
            "status": "ready",
            "draft_created": True,
            "draft_id": "draft-unavailable",
            "order": {
                "size": {"name": "Single"},
                "flavors": [{"name": "The Daily"}],
                "sauce": None,
                "toppings": [{"name": "Chocolate Shavings"}],
            },
            "quote": {
                "total_display": "$5.75",
                "eta_minutes": 10,
                "requested_ready_in_minutes": None,
            },
        }

    async def fake_ops(session_id: str, question: str) -> str:
        return json.dumps(
            {
                "can_make_now": False,
                "requested_items": [
                    {"name": "Chocolate Shavings", "available": False},
                ],
            }
        )

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    response = await runtime.chat(
        "unavailable-session",
        "Make me a single the daily with chocolate shavings",
    )

    assert response.needs_confirmation is False
    assert "Oops! That coffee cannot be fulfilled as requested." in response.reply
    assert "Unavailable right now: Chocolate Shavings." in response.reply


def test_display_order_number_uses_first_two_digits() -> None:
    assert display_order_number("brew-55b8472e") == "55"
    assert display_order_number("brew-a8b3c7") == "83"


@pytest.mark.asyncio
async def test_model_router_retries_then_uses_heuristic_fallback() -> None:
    runtime = ConciergeRuntime(
        Settings(
            openai_base_url="",
            openai_chat_model="",
        ),
        mcp_call=None,
        ops_call=None,
    )
    router = FakeAgent(
        [
            SimpleNamespace(text='{"route":"invalid"}'),
            SimpleNamespace(text=""),
        ]
    )
    runtime._router = cast(Any, router)

    plan = await runtime.plan_turn("show me the menu", "No prior conversation.")

    assert plan.route == "menu"
    assert len(router.prompts) == 2
    assert "Validation errors:" in router.prompts[1]
    assert "literal_error" in router.prompts[1]
    await runtime.close()


@pytest.mark.asyncio
async def test_model_writer_retries_empty_response_then_uses_fallback() -> None:
    runtime = ConciergeRuntime(
        Settings(
            openai_base_url="",
            openai_chat_model="",
        ),
        mcp_call=None,
        ops_call=None,
    )
    writer = FakeAgent(
        [
            SimpleNamespace(text=""),
            SimpleNamespace(text="   "),
        ]
    )
    runtime._writer = cast(Any, writer)

    reply = await runtime.write_reply("writer prompt", "deterministic fallback")

    assert reply == "deterministic fallback"
    assert len(writer.prompts) == 2
    assert "previous response was empty" in writer.prompts[1]
    await runtime.close()


def route_span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(concierge_runtime, "tracer", provider.get_tracer("test"))
    return exporter


@pytest.mark.asyncio
async def test_route_span_records_validation_failures_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = route_span_exporter(monkeypatch)
    runtime = ConciergeRuntime(Settings(openai_base_url="", openai_chat_model=""))
    invalid = '{"route":"general","operations_intent":"specials"}'
    runtime._router = cast(
        Any,
        FakeAgent([SimpleNamespace(text=invalid), SimpleNamespace(text=invalid)]),
    )

    plan = await runtime.plan_turn("Got any specials today?", "No prior conversation.")

    assert plan.route == "operations"
    assert plan.operations_intent == "specials"
    (span,) = exporter.get_finished_spans()
    assert span.name == "concierge.route"
    assert span.attributes is not None
    assert span.attributes["concierge.router.outcome"] == "fallback"
    assert span.attributes["concierge.route"] == "operations"
    assert [event.name for event in span.events] == [
        "structured_output.validation_failed"
    ] * 2
    attributes = [dict(event.attributes or {}) for event in span.events]
    assert [item["attempt"] for item in attributes] == [1, 2]
    assert "specials requires route=operations" in str(attributes[0]["error.message"])
    await runtime.close()


@pytest.mark.asyncio
async def test_route_span_records_model_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = route_span_exporter(monkeypatch)
    runtime = ConciergeRuntime(Settings(openai_base_url="", openai_chat_model=""))
    runtime._router = cast(Any, FakeAgent([SimpleNamespace(text='{"route":"menu"}')]))

    await runtime.plan_turn("show me the menu", "No prior conversation.")

    (span,) = exporter.get_finished_spans()
    assert span.attributes is not None
    assert span.attributes["concierge.router.outcome"] == "model"
    assert span.events == ()
    await runtime.close()


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_router_prompt_v2_is_opt_in(version: Literal["v1", "v2"]) -> None:
    regression = version == "v2"
    runtime = ConciergeRuntime(
        Settings(
            openai_base_url="http://model.invalid/v1",
            openai_chat_model="demo",
            openai_api_key="test",
            concierge_router_prompt_version=version,
        ),
        mcp_call=None,
        ops_call=None,
    )
    assert runtime._router is not None
    instructions = runtime._router.default_options["instructions"]
    assert ("use general with" in instructions) is regression
    assert ('use operations with\noperations_intent="specials"' in instructions) is (
        not regression
    )
