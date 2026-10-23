"""OpenTelemetry setup for local OTLP and Azure Monitor."""

import logging
import os
from contextlib import ExitStack
from importlib.util import find_spec
from threading import Lock
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from azure.core.credentials import TokenCredential
from azure.identity import (
    DefaultAzureCredential,
    WorkloadIdentityCredential,
    get_bearer_token_provider,
)
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import inject
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import OTELResourceDetector, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import make_asgi_app
from requests import HTTPError, PreparedRequest, Response, Session

_configure_lock = Lock()
_otel_configured = False
_OTLP_SIGNALS = ("TRACES", "METRICS", "LOGS")
_AZURE_MONITOR_SCOPE = "https://monitor.azure.com/.default"


class _CloseableCredential(TokenCredential, Protocol):
    def close(self) -> None: ...


class _AzureOTLPSession(Session):
    def __init__(self, credential: _CloseableCredential, endpoint: str) -> None:
        super().__init__()
        self._credential = credential
        prepared = PreparedRequest()
        prepared.prepare_url(endpoint, None)
        self._endpoint = prepared.url
        self._token_provider = get_bearer_token_provider(
            credential, _AZURE_MONITOR_SCOPE
        )
        self._token_lock = Lock()
        self._closed = False

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        if self._closed:
            raise RuntimeError("Azure OTLP session is closed")
        if request.url != self._endpoint:
            raise ValueError(
                "Azure OTLP request does not match its configured endpoint"
            )
        with self._token_lock:
            request.headers["Authorization"] = f"Bearer {self._token_provider()}"
        kwargs["allow_redirects"] = False
        response = super().send(request, **kwargs)
        if 300 <= response.status_code < 400:
            raise HTTPError(
                "Azure OTLP ingestion must not redirect",
                request=request,
                response=response,
            )
        return response

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            super().close()
        finally:
            self._credential.close()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def inject_trace_headers(_: dict[str, Any] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    inject(headers)
    return headers


def _azure_otlp_endpoints() -> dict[str, str]:
    endpoints: dict[str, str] = {}
    for signal in _OTLP_SIGNALS:
        name = f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT"
        endpoint = os.getenv(name, "").strip()
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".ingest.monitor.azure.com")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/datacollectionRules/")
            or "/streams/" not in parsed.path
            or not parsed.path.endswith(f"/otlp/v1/{signal.lower()}")
        ):
            raise ValueError(
                f"{name} must be a complete Azure Monitor HTTPS OTLP "
                f"{signal.lower()} endpoint"
            )
        endpoints[signal] = endpoint

    for signal in ("", *[f"_{signal}" for signal in _OTLP_SIGNALS]):
        name = f"OTEL_EXPORTER_OTLP{signal}_PROTOCOL"
        if os.getenv(name, "http/protobuf").strip() != "http/protobuf":
            raise ValueError(f"{name} must be http/protobuf for native Azure OTLP")
    required_metrics = {
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
        "OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION": (
            "base2_exponential_bucket_histogram"
        ),
    }
    for name, expected in required_metrics.items():
        if os.getenv(name, "").strip().lower() != expected:
            raise ValueError(f"{name} must be {expected} for native Azure OTLP")

    application_id = (
        OTELResourceDetector().detect().attributes.get("microsoft.applicationId")
    )
    message = (
        "OTEL_RESOURCE_ATTRIBUTES must include microsoft.applicationId "
        "with the nonzero InstrumentationKey GUID from OTLP Connection Info"
    )
    if not isinstance(application_id, str):
        raise ValueError(message)
    try:
        parsed_application_id = UUID(application_id)
    except ValueError as error:
        raise ValueError(message) from error
    if parsed_application_id.int == 0:
        raise ValueError(message)
    return endpoints


def _build_azure_otlp_exporters(
    auth_mode: str,
) -> tuple[OTLPSpanExporter, OTLPMetricExporter, OTLPLogExporter]:
    endpoints = _azure_otlp_endpoints()
    credential_factory = (
        WorkloadIdentityCredential
        if auth_mode == "workload_identity"
        else DefaultAzureCredential
    )
    with ExitStack() as stack:
        sessions = {
            signal: stack.enter_context(
                _AzureOTLPSession(credential_factory(), endpoint)
            )
            for signal, endpoint in endpoints.items()
        }
        exporters = (
            OTLPSpanExporter(endpoint=endpoints["TRACES"], session=sessions["TRACES"]),
            OTLPMetricExporter(
                endpoint=endpoints["METRICS"], session=sessions["METRICS"]
            ),
            OTLPLogExporter(endpoint=endpoints["LOGS"], session=sessions["LOGS"]),
        )
        stack.pop_all()
    logging.getLogger(__name__).info(
        "Exporting native Azure OTLP using %s authentication", auth_mode
    )
    return exporters


def _build_exporters() -> tuple[Any, Any, Any]:
    auth_mode = os.getenv("OTEL_EXPORTER_OTLP_AUTH_MODE", "none").strip()
    if auth_mode not in ("none", "default_credential", "workload_identity"):
        raise ValueError(
            "OTEL_EXPORTER_OTLP_AUTH_MODE must be none, default_credential, "
            "or workload_identity"
        )
    if auth_mode != "none":
        return _build_azure_otlp_exporters(auth_mode)
    for name in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        *[f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT" for signal in _OTLP_SIGNALS],
    ):
        endpoint = os.getenv(name, "").strip()
        if (urlsplit(endpoint).hostname or "").endswith(".ingest.monitor.azure.com"):
            raise ValueError(
                "Azure Monitor cloud OTLP endpoints require "
                "OTEL_EXPORTER_OTLP_AUTH_MODE=workload_identity or default_credential"
            )

    if os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip():
        return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if connection_string:
        from azure.monitor.opentelemetry.exporter import (
            AzureMonitorLogExporter,
            AzureMonitorMetricExporter,
            AzureMonitorTraceExporter,
        )

        return (
            AzureMonitorTraceExporter(connection_string=connection_string),
            AzureMonitorMetricExporter(connection_string=connection_string),
            AzureMonitorLogExporter(connection_string=connection_string),
        )
    return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()


def uninstrument_httpx_client(client: Any) -> None:
    if not env_bool("ENABLE_INSTRUMENTATION", True):
        return
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor.uninstrument_client(client)


def configure(service_name: str) -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not env_bool("ENABLE_INSTRUMENTATION", True):
        return

    global _otel_configured
    with _configure_lock:
        if _otel_configured:
            return
        attributes = {"service.name": service_name}
        namespace = os.getenv("OTEL_SERVICE_NAMESPACE", "").strip()
        if namespace:
            attributes["service.namespace"] = namespace
        resource = Resource.create(attributes)
        span_exporter, metric_exporter, log_exporter = _build_exporters()

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[metric_reader])
        )

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        set_logger_provider(logger_provider)
        logging.getLogger().addHandler(
            LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        )

        if find_spec("httpx") is not None:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        _otel_configured = True


def create_metrics_app() -> Any:
    return make_asgi_app()


def instrument_asgi(app: Any) -> Any:
    if not env_bool("ENABLE_INSTRUMENTATION", True):
        return app
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    return OpenTelemetryMiddleware(
        app,
        excluded_urls=r".*/healthz$|.*/metrics/?$",
        exclude_spans=["receive", "send"],
    )
