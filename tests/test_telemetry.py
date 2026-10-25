import os
import time
from contextlib import contextmanager
from typing import Any

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import Counter, Histogram
from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricsData
from opentelemetry.sdk.metrics.view import ExponentialBucketHistogramAggregation
from opentelemetry.sdk.resources import Resource
from requests import PreparedRequest, Response, Session

from fourth_coffee import telemetry

CONNECTION_STRING = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://example.in.applicationinsights.azure.com/"
)
APPLICATION_ID = "12345678-1234-1234-1234-123456789abc"
ENDPOINTS = {
    "TRACES": (
        "https://fixture.eastus-1.ingest.monitor.azure.com/"
        "datacollectionRules/dcr-fixture/streams/Microsoft-OTLP-Traces/otlp/v1/traces"
    ),
    "METRICS": (
        "https://fixture.eastus-1.metrics.ingest.monitor.azure.com/"
        "datacollectionRules/dcr-fixture/streams/Custom-Metrics-Otel/otlp/v1/metrics"
    ),
    "LOGS": (
        "https://fixture.eastus-1.ingest.monitor.azure.com/"
        "datacollectionRules/dcr-fixture/streams/Microsoft-OTLP-Logs/otlp/v1/logs"
    ),
}


class FakeCredential:
    def __init__(self) -> None:
        self.tokens = [AccessToken("test-token", int(time.time()) + 3600)]
        self.scopes: list[tuple[str, ...]] = []
        self.close_count = 0
        self.error: ClientAuthenticationError | None = None

    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.scopes.append(scopes)
        if self.error is not None:
            raise self.error
        return self.tokens.pop(0) if len(self.tokens) > 1 else self.tokens[0]

    def close(self) -> None:
        self.close_count += 1


@pytest.fixture(autouse=True)
def clean_telemetry_environment(monkeypatch):
    for name in os.environ:
        if name.startswith("OTEL_EXPORTER_OTLP") or name in (
            "APPLICATIONINSIGHTS_CONNECTION_STRING",
            "OTEL_RESOURCE_ATTRIBUTES",
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv("APPLICATIONINSIGHTS_STATSBEAT_DISABLED", "true")


@pytest.fixture
def native_environment(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_AUTH_MODE", "workload_identity")
    for signal, endpoint in ENDPOINTS.items():
        monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "DELTA")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION",
        "base2_exponential_bucket_histogram",
    )
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES", f"microsoft.applicationId={APPLICATION_ID}"
    )


@pytest.fixture
def credentials(monkeypatch):
    created: list[tuple[str, FakeCredential]] = []

    def create(mode: str) -> FakeCredential:
        credential = FakeCredential()
        created.append((mode, credential))
        return credential

    monkeypatch.setattr(
        telemetry, "WorkloadIdentityCredential", lambda: create("workload_identity")
    )
    monkeypatch.setattr(
        telemetry, "DefaultAzureCredential", lambda: create("default_credential")
    )
    return created


@pytest.fixture
def transport(monkeypatch):
    requests: list[PreparedRequest] = []
    options: list[dict[str, Any]] = []
    status = {"code": 200}

    def send(self: Session, request: PreparedRequest, **kwargs: Any) -> Response:
        requests.append(request)
        options.append(kwargs)
        response = Response()
        response.status_code = status["code"]
        response.reason = "test response"
        response._content = b""
        response.request = request
        return response

    monkeypatch.setattr(Session, "send", send)
    return requests, options, status


@contextmanager
def configured_exporters():
    exporters = telemetry._build_exporters()
    try:
        yield exporters
    finally:
        for exporter in exporters:
            exporter.shutdown()


def export_all(exporters):
    spans, metrics, logs = exporters
    return spans.export([]), metrics.export(MetricsData(())), logs.export([])


def test_connection_string_selects_azure_monitor(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", CONNECTION_STRING)
    with configured_exporters() as (spans, metrics, logs):
        assert type(spans).__name__ == "AzureMonitorTraceExporter"
        assert type(metrics).__name__ == "AzureMonitorMetricExporter"
        assert type(logs).__name__ == "AzureMonitorLogExporter"


def test_platform_otlp_endpoint_wins_over_connection_string(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", CONNECTION_STRING)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://10.0.0.1:28331/v1/traces"
    )
    with configured_exporters() as (spans, metrics, logs):
        assert isinstance(spans, OTLPSpanExporter)
        assert isinstance(metrics, OTLPMetricExporter)
        assert isinstance(logs, OTLPLogExporter)


def test_no_configuration_uses_otlp(credentials):
    with configured_exporters() as (spans, _, _):
        assert isinstance(spans, OTLPSpanExporter)
    assert credentials == []


@pytest.mark.parametrize("mode", ["workload_identity", "default_credential"])
def test_native_authenticates_every_signal(
    monkeypatch, native_environment, credentials, transport, mode
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_AUTH_MODE", mode)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", CONNECTION_STRING)
    with configured_exporters() as exporters:
        assert all(result.name == "SUCCESS" for result in export_all(exporters))
        _, metrics, _ = exporters
        assert metrics._preferred_temporality[Counter] == AggregationTemporality.DELTA
        assert isinstance(
            metrics._preferred_aggregation[Histogram],
            ExponentialBucketHistogramAggregation,
        )
        assert (
            Resource.create({}).attributes["microsoft.applicationId"] == APPLICATION_ID
        )
    requests, options, _ = transport
    assert [request.url for request in requests] == list(ENDPOINTS.values())
    assert all(
        request.headers["Authorization"] == "Bearer test-token" for request in requests
    )
    assert all(option["allow_redirects"] is False for option in options)
    assert [used_mode for used_mode, _ in credentials] == [mode] * 3
    for _, credential in credentials:
        assert credential.scopes == [("https://monitor.azure.com/.default",)]
        assert credential.close_count == 1


def test_native_accepts_endpoint_casing_returned_by_azure(
    monkeypatch, native_environment, credentials, transport
):
    endpoint = ENDPOINTS["TRACES"].replace("datacollectionRules", "dataCollectionRules")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    with configured_exporters() as exporters:
        assert all(result.name == "SUCCESS" for result in export_all(exporters))
    requests, _, _ = transport
    assert requests[0].url == endpoint


def test_native_renews_near_expiry_token(transport):
    credential = FakeCredential()
    credential.tokens = [
        AccessToken("first-token", int(time.time()) + 1),
        AccessToken("renewed-token", int(time.time()) + 3600),
    ]
    with telemetry._AzureOTLPSession(credential, ENDPOINTS["TRACES"]) as session:
        session.post(ENDPOINTS["TRACES"], data=b"first")
        session.post(ENDPOINTS["TRACES"], data=b"second")
    requests, _, _ = transport
    assert [request.headers["Authorization"] for request in requests] == [
        "Bearer first-token",
        "Bearer renewed-token",
    ]
    assert credential.scopes == [("https://monitor.azure.com/.default",)] * 2
    assert credential.close_count == 1


@pytest.mark.parametrize("status_code", [301, 302, 307, 401, 403])
def test_native_http_failure_does_not_fall_back(
    native_environment, credentials, transport, caplog, status_code
):
    transport[2]["code"] = status_code
    with configured_exporters() as exporters:
        assert all(result.name == "FAILURE" for result in export_all(exporters))
    assert len(transport[0]) == 3
    assert len(credentials) == 3
    assert "Failed to export" in caplog.text
    assert "test-token" not in caplog.text


def test_native_credential_failure_is_not_anonymous(
    native_environment, credentials, transport
):
    with configured_exporters() as exporters:
        for _, credential in credentials:
            credential.error = ClientAuthenticationError("test credential failure")
        for exporter, payload in zip(exporters, ([], MetricsData(()), []), strict=True):
            with pytest.raises(ClientAuthenticationError, match="test credential"):
                exporter.export(payload)
    assert transport[0] == []


def test_native_token_cannot_be_sent_to_another_endpoint(transport):
    credential = FakeCredential()
    with telemetry._AzureOTLPSession(credential, ENDPOINTS["TRACES"]) as session:
        with pytest.raises(ValueError, match="configured endpoint"):
            session.post("https://example.invalid/otlp/v1/traces", data=b"test")
    assert credential.scopes == []
    assert transport[0] == []


def test_native_session_closes_credential_only_once():
    credential = FakeCredential()
    session = telemetry._AzureOTLPSession(credential, ENDPOINTS["TRACES"])
    session.close()
    session.close()
    assert credential.close_count == 1


def test_native_failed_construction_closes_sessions(
    monkeypatch, native_environment, credentials
):
    def fail(**kwargs):
        raise ValueError("test exporter construction failure")

    monkeypatch.setattr(telemetry, "OTLPMetricExporter", fail)
    with pytest.raises(ValueError, match="test exporter construction"):
        telemetry._build_exporters()
    assert len(credentials) == 3
    assert all(credential.close_count == 1 for _, credential in credentials)


@pytest.mark.parametrize("signal", ENDPOINTS)
@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://localhost:4318",
        "https://example.invalid/otlp/v1/traces",
        "https://fixture.ingest.monitor.azure.com/v1/traces",
    ],
)
def test_native_rejects_missing_or_invalid_endpoints(
    monkeypatch, native_environment, credentials, signal, endpoint
):
    name = f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT"
    monkeypatch.setenv(name, endpoint)
    with pytest.raises(ValueError, match=name):
        telemetry._build_exporters()
    assert credentials == []


@pytest.mark.parametrize("suffix", ["PROTOCOL", "TRACES_PROTOCOL", "METRICS_PROTOCOL"])
def test_native_rejects_non_http_protobuf_protocol(
    monkeypatch, native_environment, suffix
):
    name = f"OTEL_EXPORTER_OTLP_{suffix}"
    monkeypatch.setenv(name, "grpc")
    with pytest.raises(ValueError, match=name):
        telemetry._build_exporters()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "CUMULATIVE"),
        ("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", ""),
        (
            "OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION",
            "explicit_bucket_histogram",
        ),
        ("OTEL_RESOURCE_ATTRIBUTES", ""),
        ("OTEL_RESOURCE_ATTRIBUTES", "microsoft.applicationId=not-a-guid"),
        (
            "OTEL_RESOURCE_ATTRIBUTES",
            "microsoft.applicationId=00000000-0000-0000-0000-000000000000",
        ),
    ],
)
def test_native_rejects_incomplete_configuration(
    monkeypatch, native_environment, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        telemetry._build_exporters()


def test_azure_cloud_endpoint_requires_explicit_auth(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ENDPOINTS["TRACES"])
    with pytest.raises(ValueError, match="OTEL_EXPORTER_OTLP_AUTH_MODE"):
        telemetry._build_exporters()


def test_unknown_auth_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_AUTH_MODE", "static_token")
    with pytest.raises(ValueError, match="OTEL_EXPORTER_OTLP_AUTH_MODE"):
        telemetry._build_exporters()


def test_disabled_instrumentation_does_not_require_cloud_credentials(
    monkeypatch, credentials
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_AUTH_MODE", "workload_identity")
    monkeypatch.setenv("ENABLE_INSTRUMENTATION", "false")
    telemetry.configure("fourth-coffee-mcp")
    assert credentials == []
