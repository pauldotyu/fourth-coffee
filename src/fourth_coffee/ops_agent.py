"""A2A operations specialist grounded in Coffee MCP tools."""

import asyncio
import contextlib
import json
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, Literal, Protocol, Self

import httpx
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    TaskState,
)
from agent_framework import (
    Agent,
    AgentSession,
    MCPStreamableHTTPTool,
)
from agent_framework.a2a import A2AExecutor
from agent_framework.exceptions import ToolException
from mcp.types import CallToolResult
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from fourth_coffee.agent_runtime import run_structured
from fourth_coffee.catalog import DEFAULT_SIZE
from fourth_coffee.model_client import (
    OpenAIAuthMode,
    create_openai_chat_client,
    model_enabled,
    validate_openai_auth,
)
from fourth_coffee.protocol import result_text
from fourth_coffee.settings import AppSettings, normalize_url
from fourth_coffee.telemetry import (
    configure,
    create_metrics_app,
    inject_trace_headers,
    instrument_asgi,
    uninstrument_httpx_client,
)

logger = logging.getLogger("ops-agent")
tracer = trace.get_tracer("fourth-coffee.ops")


class ConnectableMCPTool(Protocol):
    async def connect(self, *, reset: bool = False) -> None: ...


OPS_AGENT_NAME = "BrewOps"

OPS_INSTRUCTIONS = """
Return one MCP tool plan for the customer's operations question.
list_menu: menu choices, sizes, or prices.
check_availability: stock, shortages, specials, or fulfillment checks.
Use exact requested ingredients for fulfillment; no filters for overall inventory.
quote_order: a hypothetical build, price, or ETA; extract ingredients and timing.
Choose a plan only. The application executes it and returns authoritative data.
Order submission is not an available operation.
""".strip()


class OperationsPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tool: Literal["list_menu", "check_availability", "quote_order"]
    size: str = DEFAULT_SIZE
    flavors: list[str] = Field(default_factory=list)
    sauce: str | None = None
    toppings: list[str] = Field(default_factory=list)
    requested_ready_in_minutes: int | None = Field(default=None, ge=1)


class Settings(AppSettings):
    ops_agent_public_base_url: str = "http://ops-agent:8202"
    fourth_coffee_mcp_url: str = "http://fourth-coffee-mcp:8101/mcp/"
    openai_base_url: str = ""
    openai_chat_model: str = ""
    openai_auth_mode: OpenAIAuthMode = OpenAIAuthMode.API_KEY
    openai_api_key: str | None = None
    mcp_startup_attempts: int = 27
    mcp_startup_backoff_seconds: float = 1.0
    ops_demo_work_seconds: float = Field(default=0, ge=0, le=10)
    ops_demo_concurrency: int = Field(default=2, ge=1, le=64)

    @property
    def model_is_enabled(self) -> bool:
        return model_enabled(self.openai_base_url, self.openai_chat_model)

    @model_validator(mode="after")
    def require_model_auth(self) -> Self:
        if self.model_is_enabled:
            validate_openai_auth(self.openai_auth_mode, self.openai_api_key)
        return self

    @property
    def normalized_fourth_coffee_mcp_url(self) -> str:
        return normalize_url(self.fourth_coffee_mcp_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def parse_coffee_result(result: CallToolResult) -> str:
    if result.isError:
        raise RuntimeError("Coffee MCP tool failed")
    return result_text(result, sort_keys=True)


class StructuredOnlyAgent:
    """Session provider for A2A when no ops model is configured."""

    def create_session(self, *, session_id: str | None = None) -> AgentSession:
        return AgentSession(session_id=session_id)


class BrewOpsExecutor(A2AExecutor):
    def __init__(
        self,
        agent: Agent | None,
        coffee_tools: MCPStreamableHTTPTool,
        settings: Settings,
    ) -> None:
        super().__init__(agent or StructuredOnlyAgent())  # type: ignore[arg-type]
        self._ops_agent = agent
        self._coffee_tools = coffee_tools
        self._work_seconds = settings.ops_demo_work_seconds
        self._capacity = (
            asyncio.Semaphore(settings.ops_demo_concurrency)
            if self._work_seconds
            else contextlib.nullcontext()
        )
        self._pod = socket.gethostname()
        self._operations: dict[
            str,
            Callable[[dict[str, Any]], Awaitable[list[str]]],
        ] = {
            "inventory_special": self._inventory_special,
            "verify_fulfillment": self._verify_fulfillment,
        }

    async def _tool_text(
        self,
        name: str,
        **arguments: Any,
    ) -> list[str]:
        result = await self._coffee_tools.call_tool(name, **arguments)
        if isinstance(result, str):
            return [result]
        return [
            content.text
            for content in result
            if content.type == "text" and isinstance(content.text, str)
        ]

    async def _inventory_special(self, _: dict[str, Any]) -> list[str]:
        return await self._tool_text("check_availability")

    async def _verify_fulfillment(
        self,
        arguments: dict[str, Any],
    ) -> list[str]:
        return await self._tool_text(
            "check_availability",
            flavors=arguments.get("flavors"),
            sauce=arguments.get("sauce"),
            toppings=arguments.get("toppings"),
        )

    async def _run_structured_request(self, query: Any) -> list[str] | None:
        if not isinstance(query, str) or not query.startswith("BREW_OPS_REQUEST "):
            return None
        try:
            request = json.loads(query.removeprefix("BREW_OPS_REQUEST "))
        except json.JSONDecodeError as error:
            raise RuntimeError("Invalid structured operations request") from error
        if not isinstance(request, dict):
            raise RuntimeError("Invalid structured operations request")
        operation = request.get("operation")
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            raise RuntimeError("Invalid structured operations arguments")
        handler = self._operations.get(str(operation))
        if handler is None:
            raise RuntimeError(f"Unsupported operations request: {operation}")
        return await handler(arguments)

    async def _run_model_request(self, query: Any) -> list[str]:
        if self._ops_agent is None:
            raise RuntimeError(
                "Ops model is not configured; only structured operations requests "
                "are supported"
            )
        plan = await run_structured(self._ops_agent, str(query), OperationsPlan)
        if plan is None:
            raise RuntimeError("Ops agent did not return a valid operations plan")
        arguments = plan.model_dump(exclude={"tool"})
        if plan.tool == "list_menu":
            arguments = {}
        elif plan.tool == "check_availability":
            arguments.pop("size")
            arguments.pop("requested_ready_in_minutes")
        else:
            arguments["session_id"] = "ops-agent"
        return await self._tool_text(plan.tool, **arguments)

    async def _run(
        self,
        query: Any,
        session: AgentSession,
        updater: TaskUpdater,
    ) -> None:
        with tracer.start_as_current_span("ops.request") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", OPS_AGENT_NAME)
            queued = time.perf_counter()
            started: float | None = None
            outcome = "error"
            try:
                async with self._capacity:
                    started = time.perf_counter()
                    if self._work_seconds:
                        await asyncio.sleep(self._work_seconds)
                    results = await self._run_structured_request(query)
                    if results is None:
                        results = await self._run_model_request(query)
                    if not results:
                        raise RuntimeError(
                            "Ops agent completed without a Coffee MCP result"
                        )
                    await updater.update_status(
                        state=TaskState.TASK_STATE_WORKING,
                        message=updater.new_agent_message(
                            parts=[Part(text="\n".join(results))]
                        ),
                    )
                    outcome = "ok"
            finally:
                finished = time.perf_counter()
                queue_ms = (
                    (started if started is not None else finished) - queued
                ) * 1000
                processing_ms = (
                    (finished - started) * 1000 if started is not None else 0
                )
                span.set_attribute("ops.queue_wait_ms", queue_ms)
                span.set_attribute("ops.processing_ms", processing_ms)
                span.set_attribute("ops.demo_work_seconds", self._work_seconds)
                logger.log(
                    logging.INFO if outcome == "ok" else logging.ERROR,
                    "operation=brew_ops pod=%s trace_id=%032x "
                    "queue_wait_ms=%.1f processing_ms=%.1f outcome=%s",
                    self._pod,
                    span.get_span_context().trace_id,
                    queue_ms,
                    processing_ms,
                    outcome,
                )


def create_agent_card(settings: Settings | None = None) -> AgentCard:
    settings = settings or get_settings()
    skill = AgentSkill(
        id="support_coffee_operations",
        name="Support Coffee Operations",
        description=(
            "Makes tool grounded recommendations about inventory, availability, "
            "pricing, and prep timing."
        ),
        tags=["coffee", "operations", "inventory", "eta", "pricing"],
        examples=[
            "What coffees are running low tonight?",
            "Can you make a double cold brew in ten minutes?",
            "Which coffee build is easiest to make right now?",
        ],
    )
    return AgentCard(
        name=OPS_AGENT_NAME,
        description="Model driven operations specialist grounded in Coffee MCP.",
        version=settings.app_version,
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                url=normalize_url(settings.ops_agent_public_base_url),
                protocol_binding="JSONRPC",
            )
        ],
        skills=[skill],
    )


def create_ops_agent(
    settings: Settings,
    client: Any | None = None,
) -> Agent:
    model_client = client or create_openai_chat_client(
        model=settings.openai_chat_model,
        base_url=settings.openai_base_url,
        auth_mode=settings.openai_auth_mode,
        api_key=settings.openai_api_key,
    )
    return Agent(
        client=model_client,
        name=OPS_AGENT_NAME,
        description="Coffee operations specialist",
        instructions=OPS_INSTRUCTIONS,
        default_options={
            "temperature": 0,
            "max_tokens": 700,
        },
    )


async def connect_mcp_with_retry(
    tool: ConnectableMCPTool,
    *,
    attempts: int,
    initial_backoff_seconds: float,
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await tool.connect(reset=attempt > 1)
            return
        except ToolException:
            if attempt == attempts:
                raise
            delay = min(initial_backoff_seconds * (2 ** (attempt - 1)), 5.0)
            logger.warning(
                "Coffee MCP startup connection failed on attempt %s/%s; "
                "retrying in %.1f seconds",
                attempt,
                attempts,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)


def create_app(settings: Settings | None = None) -> Any:
    settings = settings or get_settings()
    configure("ops-agent")

    coffee_http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(20, read=120),
    )
    uninstrument_httpx_client(coffee_http_client)
    coffee_tools = MCPStreamableHTTPTool(
        name="coffee_tools",
        url=settings.normalized_fourth_coffee_mcp_url,
        description=(
            "Deterministic coffee menu, availability, quote, and submission tools. "
            "Do not submit orders from the operations specialist."
        ),
        parse_tool_results=parse_coffee_result,
        header_provider=inject_trace_headers,
        http_client=coffee_http_client,
    )
    agent = create_ops_agent(settings) if settings.model_is_enabled else None
    agent_card = create_agent_card(settings)
    handler = DefaultRequestHandler(
        agent_executor=BrewOpsExecutor(agent, coffee_tools, settings),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    async def health(_: Any) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": settings.app_version,
                "model_enabled": settings.model_is_enabled,
                "model": settings.openai_chat_model,
                "model_auth_mode": settings.openai_auth_mode.value,
            }
        )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        async with coffee_http_client:
            await connect_mcp_with_retry(
                coffee_tools,
                attempts=settings.mcp_startup_attempts,
                initial_backoff_seconds=settings.mcp_startup_backoff_seconds,
            )
            try:
                yield
            finally:
                await coffee_tools.close()

    app = Starlette(
        routes=[
            Route("/healthz", health),
            Mount("/metrics", create_metrics_app()),
            *create_agent_card_routes(agent_card),
            *create_jsonrpc_routes(handler, "/"),
        ],
        lifespan=lifespan,
    )
    return instrument_asgi(app)
