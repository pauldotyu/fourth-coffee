# Fourth Coffee Helm chart

This chart is the canonical Kubernetes definition for the three workloads. Each
Deployment uses the same image and selects its service through `SERVICE` and
`PORT`.

Default render:

```bash
helm template fourth-coffee deploy/helm/fourth-coffee --namespace demo
```

Local Kind profile:

```bash
docker compose up -d otel-collector loki tempo prometheus grafana
helm upgrade --install fourth-coffee deploy/helm/fourth-coffee \
  --namespace demo \
  --create-namespace \
  --values deploy/helm/values-local.yaml
```

The local values use `172.17.0.1` for host Ollama and OTLP. Override
`config.OPENAI_BASE_URL` and `config.OTEL_EXPORTER_OTLP_ENDPOINT` when Docker
uses a different host address.

### Local Kind networking

`values-local.yaml` enables `hostNetwork` for all three pods. They share the
Kind node network namespace, use distinct ports, and call each other through
`127.0.0.1`. `ClusterFirstWithHostNet` preserves Kubernetes DNS resolution.

The Kind node runs inside Docker, so `172.17.0.1` is its default bridge route
to Ollama and the Compose OTLP collector on the host. This address varies by
Docker setup and can be overridden through Helm values or the root Makefile.

The concierge uses NodePort `30001`; `deploy/kind-config.yaml` maps that node
port to host port `8301`. This host-network topology keeps the local demo small.
The default and Azure values retain normal pod networking.

Azure profile:

```bash
helm template fourth-coffee deploy/helm/fourth-coffee \
  --namespace demo \
  --values deploy/helm/values-azure.yaml
```

Use `secret.existingSecret` with `secret.create: false` to supply an existing
Secret. Non-secret runtime values belong under `config`; API keys and connection
strings belong under `secret.data`.

The optional scaling demo uses `config.OPS_DEMO_WORK_SECONDS` (default `"0"`,
disabled) and `config.OPS_DEMO_CONCURRENCY` (default `"2"`). Scale only
`components.opsAgent.replicas`; concierge and MCP retain process-local state.
See the [scaling walkthrough](../../../README.md#aks-scaling-demo).

For AKS, set `workloadIdentity.enabled`, `workloadIdentity.clientId`, and the
shared service account values. The Azure profile opts into authenticated native
OTLP with `config.OTEL_EXPORTER_OTLP_AUTH_MODE: workload_identity`. Replace the
three blank per-signal endpoint URLs and complete `microsoft.applicationId` with
the InstrumentationKey GUID from the Application Insights OTLP Connection Info.
The profile supplies the required protocol and metrics settings.

`workloadIdentity.serviceAccounts` supports component-specific overrides keyed by
service name. The Azure profile uses one for `fourth-coffee-mcp`, which has a
separate telemetry-only identity. Components without an override continue using
the shared ServiceAccount. Set an override's `create: false` to reference a
platform-owned ServiceAccount instead of creating it. Its `name` is still
required; `clientId` is required when the chart creates it.

The platform must configure each identity's federated trust for the selected
namespace and ServiceAccount. Grant every exporter `Monitoring Metrics Publisher`
on the application's DCR, and grant model access only to the model-calling
identity. This chart does not create Azure identities, DCRs, or role assignments.
It does not require the AKS application-monitoring feature.

Deploy a newly published image containing the authenticated exporter, setting
`image.tag` explicitly. A successful chart render does not verify the image's
capabilities.

For a classic connection-string deployment, set the auth mode to `none` and remove
the per-signal cloud endpoint variables. Local values already use unauthenticated
OTLP to the local collector. See the [application's native OTLP guide](../../../README.md#native-otlp-without-aks-application-monitoring)
for configuration, credential renewal, and live verification.
