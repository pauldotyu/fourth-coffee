# Fourth Coffee

Fourth Coffee is a compact MCP and A2A demo. A browser concierge builds coffee quotes, an operations agent checks fulfillment, and a deterministic MCP service owns catalog, inventory, drafts, and submissions.

## Architecture

```mermaid
flowchart TD
    Browser --> Concierge
    Concierge -->|menu, quote, confirm| MCP[Fourth Coffee MCP]
    Concierge -->|A2A operations| Ops[Concierge]
    Ops -->|grounded tools| MCP
    Concierge -. telemetry .-> Observe[OTel or Azure Monitor]
    Ops -. telemetry .-> Observe
    MCP -. telemetry .-> Observe
```

All three services are installed from the `fourth_coffee` Python package and run from one container image. `SERVICE` selects `fourth-coffee-mcp`, `ops-agent`, or `concierge` at runtime.

| Service      | Port | Responsibility                                       |
| ------------ | ---: | ---------------------------------------------------- |
| `fourth-coffee-mcp` | 8101 | Menu, availability, quotes, and order submission     |
| `ops-agent`  | 8202 | Tool-grounded inventory and fulfillment decisions    |
| `concierge`  | 8301 | Browser, sessions, routing, drafts, and confirmation |

The MCP service exposes `list_menu`, `check_availability`, `quote_order`, and `submit_order`. Only the concierge confirmation action submits a draft.

Routing and operations planning use Chat Completions JSON-schema output, validated
with Pydantic. Invalid output gets one retry with validation feedback. The router
then logs and falls back to deterministic routing; an invalid Concierge plan fails
explicitly without executing a tool. Models must support `response_format` with
`json_schema`. Model endpoint failures are reported, not silently treated as valid
plans.

Specials are a routing intent, not a keyword override ahead of the model. With the
concierge model disabled, or after both validation attempts fail, the fallback
router still recognizes the demo prompts. Concierge executes only its validated
menu, availability, or quote plan; submission is not in its model-facing schema.

"Surprise me" reuses the specials recipe: a Double with the highest-stock coffee,
milk, and two available add-ons. When only one drink is left, it
combines two drinks rather than promising two of one. It fetches live
inventory, quotes through MCP, and asks Concierge to verify fulfillment. The recipe
may repeat until stock changes, and quoting still does not reserve inventory.

## Local quick start

Requirements:

- Docker Engine with Docker Compose
- Ollama on the host

```bash
cp .env.example .env
ollama pull qwen3:8b
docker compose up -d --build --wait
```

Open [http://localhost:8301](http://localhost:8301).

> [!note]
> The Linux host mapping for `host.docker.internal` is included in Compose.

Useful prompts:

- `Show me the menu.`
- `Build me a double the daily with oat milk and chocolate shavings.`
- `Got any coffee specials tonight?`
- `Surprise me.`

Stop the stack:

```bash
docker compose down
```

## Development

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --dev
make check
```

> [!note]
> `make check` validates without modifying files. Formatting is explicit:

```bash
make format
```

Build the shared image:

```bash
make build
```

The image version defaults to the project version in `pyproject.toml`. `APP_VERSION` is the only runtime version override.

## Kubernetes

Helm is used to package the app for Kubernetes.

```bash
helm lint deploy/helm/fourth-coffee
helm template fourth-coffee deploy/helm/fourth-coffee --namespace demo
```

Local Kind:

> [!note]
> The `compose.yaml` runs the observability stack in containers and publishes it on the host. Kind workloads export telemetry to the OpenTelemetry Collector through the Docker host, and Grafana is available at [http://localhost:3000](http://localhost:3000).

```bash
make kind-create
make kind-delete
```

## Local networking

The `deploy/helm/values-local.yaml` sends model requests and telemetry to the Docker host at `172.17.0.1`.

Find the Kind node's host gateway with:

```bash
docker inspect fourth-coffee-control-plane \
  --format '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}'
```

You can override the URLs if the reported address differs:

```bash
make kind-create \
  OPENAI_BASE_URL=http://host-address:11434/v1 \
  OTEL_EXPORTER_OTLP_ENDPOINT=http://host-address:4318
```

The three application pods share the Kind node network. They call each other through `127.0.0.1`, reach host services through the Docker bridge, and expose the concierge at [http://localhost:8301](http://localhost:8301).

The `deploy/kind-config.yaml` creates the single control-plane node and maps its NodePort `30001` to host port `8301`. See the [chart README](deploy/helm/fourth-coffee/README.md#local-kind-networking) for the full network path.

## Azure deployment

For Azure, see the [aks-for-agents-with-hcp-tf-stacks](https://github.com/pauldotyu/aks-for-agents-with-hcp-tf-stacks) for an example of Terraform provisioning. Using the Terraform outputs, update the `deploy/helm/values-azure.yaml` to include your deployment values.

Connect to the AKS cluster:

```bash
az aks get-credentials --resource-group <rg> --name <aks>
```

Publish an image containing the native-authentication changes and use its tag.
The exporter-selection-only implementation is not sufficient for authenticated
cloud OTLP. No compatible published replacement tag is assumed here.

After completing the native OTLP configuration below, install the Helm chart:

```bash
helm upgrade --install fourth-coffee deploy/helm/fourth-coffee \
  --namespace demo \
  --create-namespace \
  --values deploy/helm/values-azure.yaml \
  --set-string image.tag=<your-new-image-tag>
```

### Native OTLP without AKS application monitoring

The Azure profile exports traces, metrics, and logs directly to Azure Monitor's
native OTLP cloud endpoints. It does not require AKS `appMonitoring`, an
`Instrumentation` resource, or a collector pod.

Before installing, replace the example client IDs and model settings in
`deploy/helm/values-azure.yaml`. Supply the three complete HTTPS URLs from the
Application Insights resource's **OTLP Connection Info**. Set
`OTEL_RESOURCE_ATTRIBUTES` to `microsoft.applicationId=<InstrumentationKey GUID>`.
This is routing metadata, not a bearer credential. The profile deliberately leaves
these values incomplete rather than guessing a destination.

`OTEL_EXPORTER_OTLP_AUTH_MODE=workload_identity` uses the pod's projected identity
to obtain tokens for `https://monitor.azure.com/.default`. Azure Identity caches
and refreshes those tokens before expiry for each signal's HTTP session.
`default_credential` is also available for a process with an existing Azure
credential, such as an authenticated local Azure CLI session. Native mode targets
Azure public-cloud ingestion endpoints and requires HTTP/protobuf, delta metrics,
and exponential histograms; the Azure profile already supplies those settings.
Missing or invalid configuration fails explicitly. Authentication or ingestion
errors never trigger a switch to classic ingestion.

The platform must provision the OTLP-enabled Application Insights resource,
DCR/DCE, workload identities, and federated credentials. Grant **Monitoring
Metrics Publisher on the application's DCR** to every exporting identity. The
model identity retains its separate model-access grant. The Azure profile gives
`fourth-coffee-mcp` its own `fourth-coffee-mcp-telemetry` ServiceAccount and identity,
with only the telemetry grant. This preserves all three services in the
distributed trace without giving the deterministic MCP backend model access.
Helm creates ServiceAccounts and annotations, not Azure identities or IAM grants.

Leave `OTEL_EXPORTER_OTLP_AUTH_MODE=none` for the existing local collector or
classic Application Insights exporter. In that mode, a per-signal traces endpoint
still selects OTLP ahead of the connection string; otherwise a connection string
selects the classic exporters. Clear native cloud endpoint variables when
switching back. A native Azure cloud URL without an explicit authenticated mode
is rejected, and a static bearer header is not a token-renewal strategy.

After deploying the newly built image, send a fresh chat and verify its correlated
model call in `OTelSpans` in the destination Log Analytics workspace. Repeat after
token renewal. A running pod is not proof that cloud ingestion succeeded. For
export failures, inspect the relevant pod logs: 401 points to authentication, 403
to the DCR publisher grant or its propagation, and 404 to an incorrect URL or
stream. Never print tokens while diagnosing these failures.

See [Azure Monitor native OTLP ingestion](https://learn.microsoft.com/azure/azure-monitor/containers/opentelemetry-protocol-ingestion)
for the cloud resource and authorization contract.

## AKS scaling demo

Keep the story focused: run locally, prepare the container and deployment
configuration with AKS dev tools, deploy, then use AKS desktop to inspect and scale
Concierge. The Dockerfile and Helm chart are the known-good reference. For live
configuration generation, prepare a separate starter checkout without those
artifacts in advance; do not overwrite the working deployment during the demo.
Provision the cluster, registry access, model access, and developer permissions
before presenting.

### Enable a repeatable capacity problem

Concierge has an opt-in simulation of finite per-pod capacity. With these settings,
each pod processes two operations at a time, with one second of simulated work
per operation. Requests still use the real concierge -> A2A -> MCP path.
This demonstrates agent-service queueing, not faster LLM inference.

Deploy your built image using the configured Azure values:

```bash
helm upgrade --install fourth-coffee deploy/helm/fourth-coffee \
  --namespace demo --create-namespace \
  --values deploy/helm/values-azure.yaml \
  --set-string image.tag=<your-image-tag> \
  --set-string config.OPS_DEMO_WORK_SECONDS=1 \
  --set-string config.OPS_DEMO_CONCURRENCY=2 \
  --set components.opsAgent.replicas=1 \
  --wait
```

For a local preview of queueing, set `OPS_DEMO_WORK_SECONDS=1` in `.env` and
run `make up`. Zero (the default) disables both simulated work and the concurrency
cap. Use normal pod networking on AKS for the scaling demonstration, not the
single-node, host-network Kind profile.

### Measure, inspect, scale, repeat

For a repeatable **capacity-only** measurement, disable model calls in the
concierge before the run. This restarts that pod and clears its conversations;
finish any interactive orders first. Concierge keeps its existing model settings,
but the structured specials operation does not invoke its model.

```bash
kubectl -n demo set env deployment/concierge OPENAI_BASE_URL= OPENAI_CHAT_MODEL=
kubectl -n demo rollout status deployment/concierge
```

1. Open the app in the browser and ask `Got any specials today?`.
2. In AKS desktop, open the app's namespace or import it as a Project. Inspect
   the three workloads and the `ops-agent` pod logs.
3. Run the bounded load below against the concierge URL. It starts independent
   customer sessions asking for specials; it never quotes or submits orders.
4. While load is running, inspect Concierge's `queue_wait_ms`, `processing_ms`,
   `pod`, and `outcome` log fields. `trace_id` links to distributed traces when
   tracing is enabled. Queued I/O work need not show high CPU.
5. Scale **only `ops-agent` from 1 to 3** in AKS desktop. Wait for all three pods
   to become ready, then rerun the identical command for a clean comparison.
   Look for requests across all three pods, less queueing, and lower p95 latency.

```bash
uv run python scripts/load.py --url http://<concierge-address> \
  --requests 120 --concurrency 12
```

The JSON report includes successful requests/second, p50/p95 latency in
milliseconds, and failures. Percentiles cover successful requests only; any failed
or malformed response makes the command exit nonzero. Compare runs with zero
failures, the same request count/concurrency, and no rollout in progress.
This is fixed-concurrency load, not a fixed arrival rate. With the concierge
model disabled as above, the specials path skips model calls so a shared model
endpoint cannot obscure the scaling result. With models enabled, specials use the
model router too, so the measured latency also includes inference.

After the capacity demo, remove the temporary overrides to restore model settings
from the chart's ConfigMap:

```bash
kubectl -n demo set env deployment/concierge OPENAI_BASE_URL- OPENAI_CHAT_MODEL-
kubectl -n demo rollout status deployment/concierge
```

Keep concierge and MCP at **one replica**: their conversation, inventory, draft,
and idempotency state is in memory. Concierge calls use fresh A2A tasks, and its
client does not retain idle HTTP connections that could pin subsequent calls to
one pod. This demo does not support cross-pod continuation of long-running tasks.

To reset, scale Concierge back to one replica. To disable simulation, set
`config.OPS_DEMO_WORK_SECONDS` back to `"0"` and redeploy with the same image and
Azure values. A Helm upgrade reapplies the replica count from its values.
Use synthetic data and restricted demo access; this is not a production blueprint.
