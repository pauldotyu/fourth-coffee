import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CHART = ROOT / "deploy/helm/fourth-coffee"


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        pytest.skip(f"{name} is not installed")


def rendered_documents(
    values: str | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[tuple[str, str], str]:
    require_command("helm")
    command = [
        "helm",
        "template",
        "fourth-coffee",
        str(CHART),
        "--namespace",
        "demo",
    ]
    if values is not None:
        command.extend(["--values", str(ROOT / values)])
    for name, value in (overrides or {}).items():
        command.extend(["--set", f"{name}={value}"])
    output = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    documents: dict[tuple[str, str], str] = {}
    for document in output.split("\n---\n"):
        lines = document.splitlines()
        kind = next(
            (
                line.removeprefix("kind: ")
                for line in lines
                if line.startswith("kind: ")
            ),
            None,
        )
        metadata_index = next(
            (index for index, line in enumerate(lines) if line == "metadata:"),
            None,
        )
        if kind is None or metadata_index is None:
            continue
        name = next(
            (
                line.strip().removeprefix("name: ")
                for line in lines[metadata_index + 1 :]
                if line.startswith("  name: ")
            ),
            None,
        )
        if name is not None:
            documents[(kind, name)] = "\n".join(line.rstrip() for line in lines)
    return documents


def test_compose_application_contract() -> None:
    require_command("docker")
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "IMAGE_TAG": "test"},
    )
    services = json.loads(result.stdout)["services"]

    expected = {
        "fourth-coffee-mcp": 8101,
        "ops-agent": 8202,
        "concierge": 8301,
    }
    for name, port in expected.items():
        service = services[name]
        assert service["image"] == "fourth-coffee:test"
        assert service["environment"]["SERVICE"] == name
        assert int(service["environment"]["PORT"]) == port
        assert service["extra_hosts"] == ["host.docker.internal=host-gateway"]
        assert service["healthcheck"]["test"] == [
            "CMD",
            "fourth-coffee-healthcheck",
        ]
        assert service["restart"] == "unless-stopped"
    assert str(services["ops-agent"]["environment"]["OPS_DEMO_WORK_SECONDS"]) == "0"
    assert str(services["ops-agent"]["environment"]["OPS_DEMO_CONCURRENCY"]) == "2"


@pytest.mark.parametrize(
    ("values", "image"),
    [
        (None, "ghcr.io/pauldotyu/fourth-coffee:0.1.0"),
        ("deploy/helm/values-local.yaml", "fourth-coffee:0.1.0"),
        (
            "deploy/helm/values-azure.yaml",
            "ghcr.io/pauldotyu/fourth-coffee:0.1.0",
        ),
    ],
)
def test_helm_workload_contract(values: str | None, image: str) -> None:
    documents = rendered_documents(values)
    config = documents[("ConfigMap", "app-config")]
    assert 'OPS_DEMO_WORK_SECONDS: "0"' in config
    assert 'OPS_DEMO_CONCURRENCY: "2"' in config
    expected = {
        "fourth-coffee-mcp": (8101, "cpu: 100m", "memory: 128Mi"),
        "ops-agent": (8202, "cpu: 200m", "memory: 256Mi"),
        "concierge": (8301, "cpu: 200m", "memory: 256Mi"),
    }

    for name, (port, cpu, memory) in expected.items():
        deployment = documents[("Deployment", name)]
        service = documents[("Service", name)]
        assert f'image: "{image}"' in deployment
        assert f"value: {name}" in deployment
        assert f'value: "{port}"' in deployment
        assert deployment.count("path: /healthz") == 3
        assert 'prometheus.io/scrape: "true"' in deployment
        assert cpu in deployment
        assert memory in deployment
        assert "runAsNonRoot: true" in deployment
        assert "readOnlyRootFilesystem: true" in deployment
        assert f"containerPort: {port}" in deployment
        assert "targetPort: http" in service
        assert "initContainers:" not in deployment


def test_helm_profile_specific_contracts() -> None:
    local = rendered_documents("deploy/helm/values-local.yaml")
    azure = rendered_documents("deploy/helm/values-azure.yaml")

    assert "nodePort: 30001" in local[("Service", "concierge")]
    for name in ("fourth-coffee-mcp", "ops-agent", "concierge"):
        assert "hostNetwork: true" in local[("Deployment", name)]
        assert "type: Recreate" in local[("Deployment", name)]
    local_config = local[("ConfigMap", "app-config")]
    assert 'FOURTH_COFFEE_MCP_URL: "http://127.0.0.1:8101/mcp/"' in local_config
    assert 'OPS_AGENT_URL: "http://127.0.0.1:8202"' in local_config
    assert 'azure.workload.identity/use: "true"' in azure[("Deployment", "concierge")]
    assert 'azure.workload.identity/use: "true"' in azure[("Deployment", "ops-agent")]
    assert "serviceAccountName: demo" in azure[("Deployment", "concierge")]


def test_azure_native_otlp_uses_a_telemetry_only_mcp_identity() -> None:
    documents = rendered_documents("deploy/helm/values-azure.yaml")
    config = documents[("ConfigMap", "app-config")]
    assert 'OTEL_EXPORTER_OTLP_AUTH_MODE: "workload_identity"' in config
    assert 'OTEL_EXPORTER_OTLP_PROTOCOL: "http/protobuf"' in config
    assert 'OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE: "DELTA"' in config
    assert (
        "OTEL_EXPORTER_OTLP_METRICS_DEFAULT_HISTOGRAM_AGGREGATION: "
        '"base2_exponential_bucket_histogram"'
    ) in config
    for signal in ("TRACES", "METRICS", "LOGS"):
        assert f'OTEL_EXPORTER_OTLP_{signal}_ENDPOINT: ""' in config
    assert 'OTEL_RESOURCE_ATTRIBUTES: "microsoft.applicationId="' in config
    mcp = documents[("Deployment", "fourth-coffee-mcp")]
    assert 'azure.workload.identity/use: "true"' in mcp
    assert "serviceAccountName: fourth-coffee-mcp-telemetry" in mcp
    assert (
        'azure.workload.identity/client-id: "22222222-2222-2222-2222-222222222222"'
    ) in documents[("ServiceAccount", "fourth-coffee-mcp-telemetry")]
    assert (
        'azure.workload.identity/client-id: "11111111-1111-1111-1111-111111111111"'
    ) in documents[("ServiceAccount", "demo")]


def test_component_can_reference_a_platform_owned_service_account() -> None:
    documents = rendered_documents(
        "deploy/helm/values-azure.yaml",
        {
            "workloadIdentity.serviceAccounts.fourth-coffee-mcp.name": "platform-mcp",
            "workloadIdentity.serviceAccounts.fourth-coffee-mcp.create": "false",
        },
    )
    assert ("ServiceAccount", "platform-mcp") not in documents
    assert (
        "serviceAccountName: platform-mcp"
        in documents[("Deployment", "fourth-coffee-mcp")]
    )


def test_component_identities_do_not_create_an_unused_shared_account() -> None:
    overrides: dict[str, str] = {}
    for component in ("concierge", "ops-agent", "fourth-coffee-mcp"):
        prefix = f"workloadIdentity.serviceAccounts.{component}"
        overrides[f"{prefix}.name"] = f"{component}-identity"
        overrides[f"{prefix}.create"] = "false"
    documents = rendered_documents("deploy/helm/values-azure.yaml", overrides)
    assert not any(kind == "ServiceAccount" for kind, _ in documents)
    for component in ("concierge", "ops-agent", "fourth-coffee-mcp"):
        assert (
            f"serviceAccountName: {component}-identity"
            in documents[("Deployment", component)]
        )
