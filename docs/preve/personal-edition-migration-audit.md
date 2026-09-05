# Preve Personal Edition — Migration Audit

*Date: 2026-09-05. Branch: `preve-personal-migration-audit`. Audited commit: `7d2f85b` (main).*
*Scope: full repository — `registry/`, `auth_server/`, `docker/` (nginx + Lua), `frontend/`, `metrics-service/`, `credentials-provider/`, `keycloak/`, `pingfederate/`, `tests/`, `docs/`, `scripts/`, `cli/`, `api/`, `docker-compose*.yml`, `charts/`, `terraform/`, `infra/`, `.github/workflows/`, `pyproject.toml`, `.env.example`.*
*This is a documentation-only audit. No runtime code, dependency, test, deployment, or CI behavior was changed. All claims below cite concrete files; where docs and code disagree, the disagreement is stated explicitly.*

---

## 1. Executive Summary

**What the current system actually is.** A single FastAPI control-plane monolith (`registry/`, 262 Python files across 33 sub-packages, entrypoint `registry/main.py`) that keeps one schema-driven inventory for four asset types — MCP servers, A2A agents, skills, and admin-defined custom entities — on top of MongoDB-compatible persistence. It is paired with: an nginx generic reverse proxy (data plane) whose location blocks are regenerated from the database by `registry/core/nginx_service.py`; a separate FastAPI `auth_server/server.py` (8,303 lines) that brokers OAuth2/OIDC login against six-plus identity providers and serves nginx's `auth_request /validate` subrequest; a React/Vite admin UI (`frontend/`, ~70 components, 9 routes); and MongoDB/DocumentDB as the system of record (`registry/repositories/documentdb/`). The design center is stated in `docs/design/theory-of-the-system.md`: "discovery, authorization, and audit are centralized; data movement is not centralized unless it must be."

**What makes it enterprise-heavy.** The enterprise weight is concentrated in five places, all verified in code rather than assumed from the README:

1. **Workforce IAM.** Six IdP provider implementations (`auth_server/providers/{keycloak,entra,okta,auth0,cognito,pingfederate}.py`), group→scope mapping (`auth_server/group_filter.py`, `auth_server/mongodb_groups_enrichment.py`), OBO token exchange (`auth_server/egress_obo.py`, Keycloak/Entra only), and full IdP provisioning templates (`setup/idp/`, `keycloak/`, `pingfederate/`).
2. **Fine-grained RBAC.** A scopes engine (`registry/auth/access_resolver.py`) driven by a `SCOPES_CONFIG` YAML with Keycloak group mappings, server→tool ACLs with sticky-wildcard semantics, and fail-closed behavior; 14+ scope repository tests under `tests/unit/repositories/`.
3. **Compliance audit.** A durable, fail-closed audit pipeline (`registry/audit/` — middleware, service, MongoDB repository) with three record types and HMAC non-repudiation, mirrored by a full audit UI (`frontend/src/components/AuditLogsPage.tsx` and siblings).
4. **Governance machinery.** Application-level rate limiting with quarantine (`registry/rate_limiting/`, enforced at the auth-server `/validate` hop, default off per `registry/core/config.py:1825`), registration webhooks/admission gates, and LLM-based security scanning (`cisco-ai-*-scanner` dependencies in `pyproject.toml:48-50`).
5. **AWS-shaped deployment.** `terraform/aws-ecs/` (a 95 KB `ecs-services.tf`), a parallel CDK stack in `infra/`, Helm charts in `charts/`, `buildspec.yml` for CodeBuild, and 7 of 16 GitHub workflows coupled to AWS (ECR, OIDC role assumption, Terraform).

**What already aligns well with a personal agent fabric.** More than the enterprise surface suggests:

- **A2A is peer-to-peer by default.** `A2A_REVERSE_PROXY_ENABLED` defaults to `false` (`registry/core/config.py:588-598`), and even when set it only takes effect in `with-gateway` mode (`a2a_reverse_proxy_effective`, `config.py:1449-1460`). The URL rewrite that would route agent calls through the gateway (`registry/api/agent_routes.py:_apply_a2a_reverse_proxy_split`, line 692) is a no-op by default. The registry never becomes a mandatory agent data-plane hop unless an operator opts in twice.
- **The gateway is already optional.** `DEPLOYMENT_MODE=registry-only` (`config.py:132-136`) disables nginx regeneration (`nginx_updates_enabled`, `config.py:1444-1447`) and is a first-class, validated mode.
- **The entity spine is exactly what a fabric needs.** One registration/search/access/audit path for servers, agents, skills, and custom entities (theory-of-the-system §2.1) is precisely "agent registration + capability discovery + endpoint resolution," and the custom-entity mechanism (`registry/api/custom_type_routes.py`) means new personal asset kinds cost a schema, not a subsystem.
- **Feature flags already shrink the runtime.** `REGISTRY_MODE` can restrict the service to `agents-only` or `mcp-servers-only` (`config.py:139-146`); rate limiting, federation, telemetry, and the A2A reverse proxy are all default-off.
- **The datastore is not AWS-locked at runtime.** `STORAGE_BACKEND` accepts `mongodb-ce` (community MongoDB) alongside `documentdb` (`config.py:73-110`); the same motor client serves both, with DocumentDB retained only for SCRAM-SHA-1 auth selection (`registry/utils/mongodb_connection.py`).

**How difficult the migration appears.** Moderate, and mostly *subtractive at the edges rather than surgical at the core*. The core fabric (registry + MongoDB + optional nginx) is already personal-scale: one process, one datastore, one optional proxy. The hard part is not removing code but *decoupling identity*: auth_server, the scopes engine, and the audit sink are woven through every route via `registry/auth/dependencies.py` and the nginx `/validate` hop. Replacing workforce IAM with an owner/trusted/external model touches auth, frontend guards, and ~89K lines of tests that assume scopes exist.

**Reduction, isolation, replacement, or selective retention?** **Selective retention with isolation.** The evidence does not support a wholesale rewrite (the entity spine, repository abstraction, and protocol metadata are exactly right) nor a naive delete-the-enterprise-stuff pass (auth and audit are load-bearing everywhere). The correct shape is: keep the registry core and its repository/protocol abstractions; isolate IdP-specific auth behind a simplified personal provider; disable (not delete) the dormant governance subsystems; and defer removal of the deeply-wired, cheap-when-off machinery.

---

## 2. Current Architecture Map

Each subsystem lists: responsibility · runtime role · inbound deps · outbound deps · persistence · authN/Z · deployment deps · classification.

### 2.1 `registry/` — Control plane (primary)

- **Responsibility:** unified inventory, registration, discovery, search, health, access model, audit, federation, egress-auth facade, virtual MCP aggregation for servers/agents/skills/custom entities.
- **Runtime role:** single FastAPI process; lifespan in `registry/main.py:449+` seeds scopes, loads server/agent state, initializes search indexes, starts 5 background schedulers (peer sync, ARD ingestion, ANS sync, debounced nginx reload via `core/nginx_service.py`, telemetry heartbeat).
- **Routers (29 registered):** `registry/api/` — `server_routes.py` (6,574 lines), `agent_routes.py` (2,894), `skill_routes.py`, `custom_entity_routes.py`/`custom_type_routes.py`, `search_routes.py`, `virtual_server_routes.py`, `federation_routes.py`/`peer_management_routes.py`, `egress_auth_routes.py`/`egress_oauth_facade_routes.py`, `rate_limit_routes.py`, `iam_user_groups_routes.py`, `m2m_management_routes.py`, `wellknown_routes.py`, `health` and `internal_routes.py`, plus auth/OAuth routes in `registry/auth/routes.py`.
- **Inbound:** nginx (data-plane `/validate`-gated calls), browser (session cookie), agents/MCP clients (Bearer JWT), peer registries (federation), auth-server (`X-Internal-Token-Registry`).
- **Outbound:** auth-server `/api/internal/tokens/generate`; MongoDB/DocumentDB (motor); nginx config regen + reload; IdP JWKS endpoints (JWT verification); embeddings provider (LiteLLM/external HTTP); federation peers (httpx) and AWS AgentCore (boto3); webhooks; metrics-service.
- **Persistence:** `registry/repositories/` — abstract bases in `interfaces.py`, DocumentDB/MongoDB impls in `documentdb/`, singleton factories in `factory.py`; 15+ collections; **no FAISS** — hybrid text+vector cosine search in `documentdb/search_repository.py` with sentence-transformers lazy-loaded.
- **AuthN/Z:** `registry/auth/` — `dependencies.py` (session→user context), `access_resolver.py` (scope→ACL RBAC engine), `session_store.py` (motor-backed), `internal.py` (shared-secret internal hop), `csrf.py`; scopes loaded from `SCOPES_CONFIG` YAML with Keycloak group mappings.
- **Deployment:** `docker/Dockerfile.registry`, `docker/registry-entrypoint.sh`; runs registry-only or with-gateway.
- **Classification:** **control plane** (with identity/security sub-modules embedded).

### 2.2 `auth_server/` — Identity broker

- **Responsibility:** OAuth2/OIDC login against 6+ IdPs, session issuance, token validation for nginx, M2M token minting, egress OBO exchange, group→scope enrichment.
- **Runtime role:** separate FastAPI service (`server.py`, 8,303 lines); the `/validate` endpoint is nginx's `auth_request` subrequest and returns identity as `X-User`/`X-Scopes`/`X-Groups` headers.
- **Inbound:** nginx `/validate` subrequests; browser OAuth callbacks; registry internal token requests.
- **Outbound:** IdP endpoints (Keycloak, Entra, Okta, Auth0, Cognito, PingFederate, GitHub, Google) via `providers/*`; MongoDB sessions (`session_store.py`); secret store for egress.
- **Persistence:** MongoDB (sessions, group enrichment).
- **AuthN/Z:** it *is* the auth layer; internal hops guarded by `X-Internal-Secret`/static bearer (`internal_request_token.py`, HS256, 30s TTL).
- **Deployment:** `docker/Dockerfile.auth`; `oauth2_providers.yml` holds 8 IdP configs.
- **Classification:** **identity/security** (control-plane adjacent).

### 2.3 nginx + Lua (`docker/`) — Data plane

- **Responsibility:** TLS, routing, auth enforcement at one ingress, edge rate limiting, metrics capture, virtual-MCP JSON-RPC routing, A2A proxying (opt-in).
- **Runtime role:** generic HTTP reverse proxy; config template `docker/nginx_rev_proxy_http_and_https.conf` with `{{LOCATION_BLOCKS}}`/`{{AGENT_LOCATION_BLOCKS}}`/`{{VIRTUAL_SERVER_BLOCKS}}` placeholders regenerated from the DB by `registry/core/nginx_service.py` and reloaded debounced.
- **Key mechanics:** `location = /validate` internal auth subrequest; `auth_request_set` captures identity headers; edge `limit_req` zones (50 r/s edge, 5 r/s register, 2 r/s well-known); Lua: `virtual_router.lua` (JSON-RPC + 30s session cache), `agent_card_rewrite.lua`, `emit/flush_metrics.lua`, `capture_body.lua`.
- **Inbound:** all external traffic. **Outbound:** auth-server, registry, registered MCP servers, A2A agents (when reverse-proxy on), IdPs (OAuth redirects).
- **Classification:** **data plane**.

### 2.4 `frontend/` — Admin UI

- **Responsibility:** operator UI for registration, discovery, IAM, federation, audit, settings.
- **Runtime role:** React/Vite SPA, ~70 components, 9 routes (`pages/`). Talks only to the registry API with session cookie + CSRF (`contexts/AuthContext.tsx`).
- **Enterprise surface in UI:** IAM pages (`IAMGroups/IAMUsers/IAMUserGroups/IAMM2M/IAMRateLimits.tsx`), audit pages (`AuditLogsPage`, `AuditStatistics`, `AuditExecutiveSummary`), federation pages (`FederationPeers`, `ExternalRegistries`), connected-accounts/egress page.
- **Classification:** **frontend**.

### 2.5 `metrics-service/` — Observability sink

- **Responsibility:** central metrics ingest (nginx Lua flush, registry/auth OTel) with retention.
- **Runtime role:** small FastAPI service, SQLite storage (`storage/database.py`), Prometheus scrape endpoint, admin API gated by `METRICS_ADMIN_API_KEY`.
- **Classification:** **observability**; optional (components fall back to OTel/logs).

### 2.6 `credentials-provider/` — Egress token brokerage

- **Responsibility:** per-user OAuth token vending/refresh for third-party SaaS MCP servers (`oauth/egress_oauth.py`, `token_refresher.py`); M2M helper (`agentcore-auth/get_m2m_token.py`); provider configs (`oauth/oauth_providers.yaml` — Atlassian, GitHub, ServiceNow, Slack).
- **Persistence:** secret store — AWS Secrets Manager or OpenBao (`SECRET_STORE_BACKEND`, `config.py:115-120`).
- **Classification:** **identity/security** (data-plane credential vault).

### 2.7 `keycloak/`, `pingfederate/`, `setup/idp/` — IdP provisioning

- Realm import (`keycloak/import/realm-config.json`), M2M/agent service-account scripts (`keycloak/setup/*.sh`), PingFederate config, and per-IdP provisioning templates (`setup/idp/{keycloak,entra,okta,cognito}`). **Identity/security**, deployment-time only.

### 2.8 Deployment & CI surfaces

- **Docker Compose:** `docker-compose.yml` (full stack, ~28 services incl. MongoDB-CE, Keycloak, metrics, Grafana) + `.prebuilt`, `.podman`, `.dhi` variants; `build_and_run.sh` with extra-env preflight validation (`scripts/validate-extra-env.sh`). **Deployment.**
- **Helm:** `charts/` — umbrella `mcp-gateway-registry-stack` + subcharts (registry, auth-server, mcpgw, keycloak/mongodb configure); reserved-env-name guards and `helm-unittest` suites. **Deployment.**
- **Terraform:** `terraform/aws-ecs/` — ECS Fargate, DocumentDB, Aurora, ALB, Secrets Manager; 95 KB `ecs-services.tf`. **Deployment (AWS).**
- **CDK:** `infra/` — TypeScript stacks kept in manual parity with Terraform. **Deployment (AWS).**
- **CI:** `.github/workflows/` — 16 workflows; registry tests, Helm unittest, security scans (CodeQL/Bandit/Semgrep), docs, release; 7 AWS-coupled. `buildspec.yml` builds 14 images in CodeBuild. **Tooling/deployment.**

### 2.9 Tooling

- **`cli/`** — admin CLI (`agent_mgmt.py`, `mcp_security_scanner.py`, `registry_cli_wrapper.py`) + shell wrappers; **`api/`** — OpenAPI spec (666 KB), typed client `registry_client.py` (219 KB), CLI `registry_management.py` (277 KB); **`scripts/`** — init/migration/deploy/benchmark; **`agents/` + `servers/`** — sample A2A agents and MCP servers (demos, disabled by default); **`landing/`** — generated docs page. **Tooling.**

---

## 3. KEEP / SIMPLIFY / REMOVE / DEFER Matrix

Classification is based on dependency tracing, not names. "Dormant" means default-off and cheap-when-off.

| Subsystem | Verdict | Why | What breaks if removed | Runtime-critical? |
|---|---|---|---|---|
| Registry entity spine (servers/agents/skills/custom) | **KEEP** | It *is* the fabric: registration + discovery + endpoint/protocol metadata | Everything | Yes |
| Repository/storage abstraction (`repositories/interfaces.py`) | **KEEP** | Decouples fabric from MongoDB; enables a lighter store later | All persistence | Yes |
| Agent/A2A entity model + discovery (`api/agent_routes.py`, `schemas/agent_models.py`) | **KEEP** | A2A registration, agent cards, peer discovery — core to Preve | Agent interop | Yes |
| MCP registration + tool discovery (`api/server_routes.py`, `services/server_service.py`) | **KEEP** | MCP surface is a target protocol | MCP interop | Yes |
| Health service (`health/`) | **KEEP** | Availability is a stated fabric responsibility | Health surface | Yes |
| Custom entity abstraction (`custom_type_routes.py`) | **KEEP** | Lets owner add personal asset kinds without code | Extensibility | No (but cheap) |
| Semantic search (`services/semantic_search_service.py`, embeddings) | **KEEP BUT SIMPLIFY** | Valuable capability discovery, but torch/sentence-transformers/scikit-learn are heavy for one user | Natural-language discovery | No — make optional |
| auth_server | **KEEP BUT SIMPLIFY** | Need *some* identity; collapse 6 IdPs to one personal provider | All auth | Yes (in reduced form) |
| Scopes/RBAC engine (`auth/access_resolver.py`) | **KEEP BUT SIMPLIFY** | Replace org/group scope-mapping with owner/trusted/external | Fine-grained ACLs | Partially |
| Audit subsystem (`audit/`) | **KEEP BUT SIMPLIFY** | Reduce compliance audit to lightweight operational event log | Compliance trail | No |
| nginx gateway | **KEEP (optional)** | Already optional via `DEPLOYMENT_MODE`; useful as TLS/auth front door | Gateway mode only | No |
| Rate limiting + quarantine (`rate_limiting/`) | **DEFER (leave disabled)** | Default off (`config.py:1825`); edge nginx limits suffice for one owner | Nothing when off | No |
| Federation (peer + AgentCore/Anthropic/ASOR) | **DEFER** | Peer federation may matter later; AgentCore/Anthropic/ASOR are enterprise/vendor | Nothing when off | No |
| Egress credential brokerage (`egress_auth/`, `credentials-provider/`) | **DEFER (leave disabled)** | Per-user SaaS token vault is enterprise; a single owner can hold their own tokens | 3rd-party OAuth MCP servers | No |
| LLM security scanning (`cisco-ai-*-scanner`) | **REMOVE** | Heavy deps (torch, langchain, strands) for an enterprise compliance feature | Scan-on-register only | No |
| Keycloak/Entra/Okta/Auth0/Cognito/PingFederate providers | **SIMPLIFY (→ one personal provider; seam first)** | Workforce IAM; keep `providers/base.py` + one personal provider; delete the rest only in Phase 7 if §25 confirms low merge cost | Multi-IdP login | No |
| Terraform `aws-ecs/`, CDK `infra/`, `buildspec.yml` | **DEFER (keep dormant in fork)** | AWS-shaped, but deployment-only and zero-runtime; deletion permanently diverges from upstream for no runtime benefit (§25) | AWS deploys | No |
| Helm `charts/` | **DEFER (keep dormant in fork)** | No Kubernetes requirement, but dormant K8s manifests cost nothing at runtime and are upstream-mergeable (§25) | K8s deploys | No |
| `metrics-service/` + Grafana/Prometheus stack | **SIMPLIFY (leave optional/off)** | Nice-to-have; SQLite metrics are cheap but not needed V1 | Dashboards | No |
| Registration webhooks/admission gates | **DEFER (leave disabled)** | Governance workflow | Nothing when off | No |
| `agents/`, `servers/` samples | **DEFER (keep as reference examples)** | Demo content; harmless, useful as A2A/MCP references | Nothing | No |
| Telemetry heartbeat (`core/telemetry.py`) | **REMOVE** | Phones home to an external endpoint; inappropriate for personal; small, self-contained, low upstream-coupling so removal is safe | Nothing (already opt-out) | No |

> **REMOVE→DEFER note (revised, §25):** only `core/telemetry.py` and the heavy ML scanner *dependencies* remain firm REMOVEs — both are low-upstream-coupling and provide no personal value. Deployment-only assets (Terraform/CDK/Helm) and dormant governance subsystems moved from REMOVE to DEFER because retaining dormant upstream code is cheaper than permanently diverging.

---

## 4. Enterprise Complexity vs Portability Abstractions

These two categories must not be conflated: the first is deployment/operational weight to shed; the second is *migration-cost reduction* to keep even for one user.

### 4.1 Enterprise complexity candidates (traced, not assumed)

| Candidate | Real dependency trace | Verdict |
|---|---|---|
| **Keycloak** | Realm JSON, setup scripts, `providers/keycloak.py`, group mappings parsed from SCOPES_CONFIG, OBO in `egress_obo.py` | Remove; keep generic OIDC seam |
| **Entra/Okta/Auth0/Cognito/PingFederate** | One `providers/*.py` each + `oauth2_providers.yml` entries + `setup/idp/*` | Remove; provider interface is the seam to keep |
| **Group→scope mapping** | `auth_server/group_filter.py`, `mongodb_groups_enrichment.py`, scope repo group tests | Simplify to 3 fixed trust tiers |
| **Enterprise RBAC (scopes)** | `access_resolver.py`, `SCOPES_CONFIG`, 14+ repo tests | Simplify; keep the resolver, shrink the config |
| **Compliance audit** | `registry/audit/` durable fail-closed MongoDB sink, HMAC | Reduce to operational event log |
| **Rate-limit governance / quarantine admin** | `rate_limiting/`, admin UI `IAMRateLimits.tsx`; default off | Leave disabled; defer |
| **Per-user SaaS credential brokerage** | `egress_auth/`, `credentials-provider/`, secret-store backends | Leave disabled; defer |
| **EKS/ECS/Helm/Terraform/CDK/CodeBuild** | `charts/`, `terraform/`, `infra/`, `buildspec.yml`, 7 CI workflows | Remove from personal fork |
| **AWS workshop/setup flows** | `setup/`, README workshop links, macos/remote-desktop guides | Remove |
| **Multi-deployment config parity** | `ALLOWED_STORAGE_BACKENDS` "keep in sync with Terraform" comment (`config.py:70-72`), infra-sync skill | Remove with the second IaC surface |
| **LLM security scanning** | `cisco-ai-*-scanner` deps, `services/security_scanner*` | Remove |
| **Telemetry** | `core/telemetry.py` heartbeat to external endpoint | Remove |

### 4.2 Portability / interoperability abstractions worth preserving

| Abstraction | Location | Why it reduces future migration cost |
|---|---|---|
| **Repository/storage abstraction** | `registry/repositories/interfaces.py` + `factory.py` | Swap MongoDB→lighter store without touching routes; the single most valuable seam |
| **Agent entity model (A2A AgentCard)** | `registry/schemas/agent_models.py` | Protocol-native agent identity/capability/endpoint; runtime-agnostic (works for EVIE, Letta, Pi, Claude Code, Codex) |
| **MCP server entity model** | `registry/schemas/mcp_registry_schema.py` | Upstream `server.json` format + gateway extensions; tracks the MCP ecosystem |
| **Protocol metadata** | `supported_protocol` field + `docs/supported-protocol-and-trust-fields.md` | "Protocol, not platform": endpoint + protocol advertised without coupling to runtime internals |
| **Gateway/control-plane separation** | `DEPLOYMENT_MODE`, `nginx_updates_enabled` | Lets the fabric run with or without a data-plane hop; protects peer-to-peer default |
| **Custom entity abstraction** | `custom_type_routes.py`, `custom_entity_routes.py` | New personal asset kinds = schema, not subsystem; avoids registry-core edits per platform |
| **Health abstraction** | `registry/health/` | Uniform availability signal across heterogeneous runtimes |
| **Optional semantic search seam** | `semantic_search_service.py` facade over search repo | Capability discovery that can drop to text-only without schema change |
| **Identity seam (provider interface)** | `auth_server/providers/base.py` + registry `auth/dependencies.py` | Swap workforce IAM for a personal provider at one boundary |
| **Well-known / discovery endpoints** | `api/wellknown_routes.py` | Anonymous, standard discovery surface (agent cards, OAuth protected-resource metadata) |

---

## 5. Dependency-Safe Migration Order

The correct order is derived from the dependency graph, not from "clean up deployment first." Auth and audit are load-bearing (they sit in every request path — 217 route injection points consume `Depends(nginx_proxied_auth)`/`Depends(enhanced_auth)` from `registry/auth/dependencies.py`), so they are migrated *last among core changes*, behind a stable seam. Deployment/IaC removal is safe early precisely because nothing in the runtime imports it. The ordering minimizes irreversible steps: everything before Phase 4 is reversible.

Each phase below is an **execution gate** — independently reviewable, with an explicit allowed/forbidden change surface, entry criteria, acceptance criteria, a rollback condition, and the evidence required to move forward. No phase bundles unrelated changes; if a phase cannot meet its acceptance criteria, the migration stops at that gate rather than forcing the next.

### Phase 0 — Baseline & characterization (reversible)
- **Objective:** freeze current behavior so later phases are verifiable.
- **Allowed change surface:** test/config capture only — record the personal baseline (`DEPLOYMENT_MODE=registry-only`, `RATE_LIMITING_ENABLED=false`, `A2A_REVERSE_PROXY_ENABLED=false`, `AWS_REGISTRY_FEDERATION_ENABLED=false`).
- **Forbidden change surface:** any runtime code, any dependency, any manifest.
- **Entry criteria:** clean checkout of `main`; MongoDB-CE available for tests.
- **Acceptance criteria:** full `pytest tests/unit` green on MongoDB-CE; baseline config committed as a documented profile.
- **Rollback condition:** n/a (no mutation).
- **Evidence required:** CI/unit run link; the captured baseline profile file.

### Phase 1 — Personal deployment profile (reversible; supersedes "delete AWS first")
- **Objective:** prove a personal Compose topology runs *alongside* — not instead of — the enterprise one. **Revised:** do NOT delete `terraform/`/`infra/`/`charts/` yet; add a minimal profile (see §17/§23).
- **Allowed change surface:** a new `docker-compose.preve.yml`, a `docs/preve/` profile doc, an optional frontend nav feature-flag.
- **Forbidden change surface:** `registry/`, `auth_server/`, `docker/` nginx templates, existing `docker-compose.yml`, Terraform/Helm/CDK, CI workflows.
- **Entry criteria:** Phase 0 green.
- **Acceptance criteria:** `docker compose -f docker-compose.preve.yml up` boots registry + MongoDB-CE + simplified-auth path; MCP + A2A register/discover smoke tests pass; no AWS env required.
- **Rollback condition:** delete the new profile/compose file; defaults untouched.
- **Evidence required:** boot log excerpt, smoke-test output, `config.py` effective-settings dump showing governance flags off.

### Phase 2 — Make semantic search optional (low risk, dependency-lean)
- **Objective:** gate torch/sentence-transformers/scikit-learn behind an opt-in extra so the default personal image is light; keep the search *abstraction* intact.
- **Allowed change surface:** `pyproject.toml` (extras), the embeddings lazy-load path, a text-only search fallback in `semantic_search_service.py`.
- **Forbidden change surface:** the search route contract (`api/search_routes.py`), the `ServerRepositoryBase`/`AgentRepositoryBase` search method signatures, entity schemas.
- **Entry criteria:** Phase 1 green.
- **Acceptance criteria:** text-only search returns correct results with no ML deps installed; vector search still works when the extra is installed; image size measurably smaller.
- **Rollback condition:** `git revert` + `uv sync`.
- **Evidence required:** unit tests with mocked embeddings (`tests/conftest.py` auto-mocks) green in both modes; image-size before/after.

### Phase 3 — Disable-and-dormant governance subsystems (reversible via flags)
- **Objective:** prove personal-scale operation with governance off; remove only the telemetry phone-home.
- **Allowed change surface:** config flags (rate limiting, quarantine, federation, egress brokerage, webhooks off); deletion of `core/telemetry.py` heartbeat only.
- **Forbidden change surface:** `rate_limiting/`, `egress_auth/`, `credentials-provider/`, federation services — left dormant, not deleted.
- **Entry criteria:** Phases 1–2 green.
- **Acceptance criteria:** boot in registry-only mode with no scheduler chatter from federation/rate-limit; telemetry makes no external call.
- **Rollback condition:** re-enable flags; restore `core/telemetry.py` by revert.
- **Evidence required:** startup log showing no peer-sync/ARD/ANS/telemetry scheduler activity; a network-egress check confirming no phone-home.

### Phase 4 — Introduce the identity/trust/audit seams (no behavior change yet)
- **Objective:** land the stable abstraction boundaries of §21 as *interfaces only*, with the enterprise implementations moved behind them unchanged. This phase adds seams; it does not simplify anything.
- **Allowed change surface:** `registry/auth/dependencies.py` (introduce principal-resolution contract), `registry/auth/access_resolver.py` (trust-classification seam), `registry/audit/service.py` (event-sink seam). Existing enterprise classes become the default implementations.
- **Forbidden change surface:** any route handler, the nginx `/validate` contract, scope *config* contents, frontend.
- **Entry criteria:** Phases 1–3 green; §21 contracts reviewed.
- **Acceptance criteria:** all 217 route injection points still resolve through the seam with identical behavior; full auth unit suite green with zero route edits.
- **Rollback condition:** `git revert` (seams are additive).
- **Evidence required:** auth + audit unit suites green; a diff proving no route handler changed.

### Phase 5 — Personal trust implementation behind the seam (highest coupling, do late)
- **Objective:** add the owner/trusted-agent/external implementation of the §21 trust seam, selectable by config, leaving the enterprise implementation intact.
- **Allowed change surface:** a new personal trust-classification module, a 3-tier scope preset, `auth_server/` reduced to one provider behind `providers/base.py`, frontend auth guards.
- **Forbidden change surface:** the seam contracts themselves, the repository layer, the nginx `/validate` contract, deletion of the enterprise scope engine.
- **Entry criteria:** Phase 4 seams landed and green.
- **Acceptance criteria:** personal mode answers who-is-calling / which-target / trusted-yes-no without any enterprise IdP; enterprise mode still passes its own suite unchanged.
- **Rollback condition:** select the enterprise implementation by config; the seam makes this a flip.
- **Evidence required:** new trust-tier unit suite green; enterprise auth suite green; manual login/discovery/invoke flow in both modes.

### Phase 6 — Reduce audit to operational events (after identity settles)
- **Objective:** add the lightweight operational-event implementation of the §21 audit seam, keeping the compliance implementation available.
- **Allowed change surface:** a slim event record + sink behind the audit seam, an optional single chronological event view.
- **Forbidden change surface:** the compliance audit models/middleware (kept for enterprise mode), the audit seam contract.
- **Entry criteria:** Phase 5 green.
- **Acceptance criteria:** discover/invoke/register events emitted with actor/protocol/target/outcome; personal mode does not require the fail-closed durable sink; enterprise audit still available by config.
- **Rollback condition:** select the compliance implementation by config.
- **Evidence required:** event-log entries for a scripted discover/invoke/register run; enterprise audit tests green.

### Phase 7 — Prune dormant enterprise code (optional, last, irreversible)
- **Objective:** physically remove subsystems proven dormant, only where §25 shows deletion is cheaper than dormancy for upstream mergeability.
- **Allowed change surface:** only items reconfirmed as safe-to-delete in §25 (deployment-only or weakly coupled), and only after a full personal-edition soak.
- **Forbidden change surface:** the repository seam, protocol metadata, A2A/MCP surfaces, any seam contract, anything still reachable by a retained config.
- **Entry criteria:** Phases 1–6 green; §25 divergence review signed off by owner.
- **Acceptance criteria:** full suite green after deletion; no retained config references the removed code.
- **Rollback condition:** hard — this is why it is last and optional.
- **Evidence required:** full unit suite green; a grep proving no live reference to removed modules.

---

## 6. Minimal Target Architecture — Preve Personal Edition V1

Design rule: *boring infrastructure, single owner, Docker Compose on one Mac/VPS, no Kubernetes, no workforce IAM, low operational overhead.* Reuse the existing core; do not rewrite.

**Runtime processes (default topology per §21 = Option B; the auth-server is optional, not mandatory):**

| Service | Source | Role in V1 | Default? |
|---|---|---|---|
| **registry** (with embedded personal auth via the §20.1 seam) | `registry/` via `docker/Dockerfile.registry` | Control plane: registration, discovery, endpoint/protocol resolution, health, trust metadata, event log, owner/agent identity | **Default** |
| **mongodb-ce** | compose service | Single datastore (`STORAGE_BACKEND=mongodb-ce`) | **Default** |
| **auth-server (simplified)** | `auth_server/` reduced to one personal provider | OAuth/OIDC login + token validation for owners who want it (Option A) | **Optional** |
| **nginx** | `docker/` | TLS + optional front door; **off** in the p2p-default profile | **Optional** |
| **frontend (trimmed)** | `frontend/` | Optional admin UI; IAM/audit/federation pages hidden | **Optional** |

> §21 evaluates Options A/B/C and recommends **Option B** (embedded personal auth) as the default for lowest operational burden, with the auth-server retained as an optional process. This table reflects that recommendation; the final default-vs-optional call for the auth-server is owner decision §26.1.

**Explicitly absent in V1:** Keycloak, metrics-service, Grafana/Prometheus, credentials-provider, Helm/Terraform/CDK, LLM scanners, telemetry.

- **Datastore:** MongoDB-CE single node (existing `documentdb` repositories already run on it). No Aurora/DocumentDB.
- **Gateway:** optional. Default profile is `DEPLOYMENT_MODE=registry-only`; enable `with-gateway` only when a front door is genuinely useful (e.g., exposing to other devices).
- **Health checks:** existing `registry/health/` service + per-entity polling.
- **Identity model:** one owner account; agents hold bearer tokens. See §8.
- **Trust model:** three tiers — `owner`, `trusted-agent`, `external` — replacing org/group scopes for personal use.
- **Operational event log:** a slimmed `audit` collection recording discover/invoke/register events (§9).
- **MCP surface:** server registration, tool discovery, virtual MCP (optional), well-known OAuth metadata.
- **A2A surface:** agent card registration, discovery, peer-to-peer invocation by default (`A2A_REVERSE_PROXY_ENABLED=false`).
- **Discovery surface:** list/filter + well-known endpoints; semantic search **optional** (text-only default to keep the image light; vector search behind an opt-in extra).

---

## 7. Coupling Assessment

| Coupling | Verdict | Why |
|---|---|---|
| **AWS (boto3)** | **Remove** | Only used by AgentCore federation + secrets backend; both deferred/removed |
| **EKS/ECS** | **Remove** | Deploy target only; no runtime dep |
| **Helm** | **Remove** | No K8s requirement for personal |
| **Terraform / CDK (`infra/`)** | **Remove** | AWS IaC; owner uses Compose |
| **Enterprise IdPs** | **Remove (keep one OIDC seam)** | Workforce IAM out of scope |
| **OAuth/OIDC provider impls** | **Simplify to one** | Keep `providers/base.py` seam; ship a single personal provider |
| **Scopes/RBAC** | **Simplify** | Keep resolver; collapse config to 3 trust tiers |
| **MongoDB** | **Retain** | Portable (community edition), abstraction already present; not worth swapping for V1 |
| **nginx** | **Retain as optional** | Generic proxy; valuable front door, p2p-safe by default |
| **React admin UI** | **Simplify** | Keep entity/discovery pages; hide IAM/audit/federation |
| **Semantic embeddings** | **Isolate behind flag/adapter** | Heavy deps; make vector search opt-in, text default |
| **Federation** | **Leave disabled (defer)** | Peer federation may matter later; vendor clients removable |
| **Egress credential brokerage** | **Leave disabled (defer)** | Enterprise per-user vault; owner holds own tokens |
| **Admission gates / webhooks** | **Leave disabled** | Governance workflow |
| **Observability (OTel/metrics-service)** | **Simplify** | Keep OTel hooks; drop metrics-service/Grafana from V1 compose |
| **Rate limiting** | **Leave disabled** | nginx edge limits suffice for one owner |
| **Quarantine** | **Leave disabled** | Governance feature |
| **Enterprise audit** | **Replace with event log** | See §9 |

---

## 8. Lightweight Identity and Trust Model

The system must still answer: who is calling · which agent is being called · which credential/service identity is used · is the caller trusted · is the target trusted. Identity here is **not** workforce IAM.

**Recommended model (single owner):**

- **Owner** — one human, authenticated by a single personal IdP (or even a long-lived owner credential + optional passkey/OIDC). Full control.
- **Trusted agent** — a registered agent holding a fabric-issued bearer token (the existing self-signed JWT path, `registry/auth/internal.py` and the `mcp_token` mechanism, already supports agent tokens without any IdP). Trust = "registered and enabled by owner."
- **External / untrusted** — anything else; read-only discovery at most, no invocation, no registration.

**Can existing abstractions be simplified rather than replaced? Yes.** The identity seam is already concentrated:

- `registry/auth/dependencies.py` resolves a request to a user/agent context — swap the *source* of that context (one provider instead of six) without touching route handlers.
- `auth/access_resolver.py` maps scopes→ACLs. For V1, collapse `SCOPES_CONFIG` to three entries (owner / trusted-agent / external) and let the resolver evaluate tier membership instead of group-mapped tool ACLs. The resolver engine survives; the enterprise *configuration* does not.
- The nginx `/validate` contract is identity-agnostic (it forwards `X-User`/`X-Scopes`), so the data plane needs no change.

**Non-goal:** do not rebuild OBO, group sync, DCR, or multi-IdP brokering. Those are the enterprise machinery being shed.

> **Sequencing note (§20):** this target model is reached *behind the §20.1/§20.2 seams* — the enterprise resolver/RBAC engine is retained unchanged as the enterprise implementation, and the 3-tier model is added as the personal implementation. Nothing here is an in-place rewrite.

---

## 9. Lightweight Operational Event Model

Goal: replace compliance audit with a minimal log sufficient to answer *who discovered whom, who invoked whom, protocol used, timestamp, target endpoint/entity, success/failure where observable.*

**Recommended event record (one collection, e.g. `preve_events`):**

```
{ ts, actor (owner|agent-id), action (discover|register|invoke|health),
  target_entity, protocol (a2a|mcp|http), endpoint, outcome (ok|err), detail? }
```

**Can the current audit subsystem be reduced/reused? Yes — reuse the pipe, shrink the payload.** `registry/audit/` already provides middleware capture (`middleware.py`), a service (`service.py`), and a MongoDB repository (`audit_repository.py`). The reduction is:

1. Drop the fail-closed durable-sink requirement and HMAC non-repudiation (compliance features).
2. Narrow the record taxonomy from the three compliance record types to the single event shape above.
3. Keep health/noise suppression (already an allowlist concern — see `tests/unit/audit/test_audit_health_allowlist_drift.py`).
4. Replace the audit UI pages with a single chronological event view (or omit the UI in V1 and query the collection).

This is intentionally lightweight: no retention policy engine, no export workflow, no executive-summary statistics.

> **Sequencing note (§20.3):** the lightweight model is added as the personal implementation of the audit event-sink seam. The fail-closed durable sink, HMAC, and compliance record taxonomy are *retained* as the enterprise implementation (selectable by config), so adopting the personal event log does not foreclose stronger audit semantics later.

---

## 10. A2A Invariants

Verified against `registry/api/agent_routes.py`, `registry/schemas/agent_models.py`, `registry/core/config.py`, and `docker/lua/agent_card_rewrite.lua`.

**Invariants that must survive migration:**

1. **Peer-to-peer is the default.** `A2A_REVERSE_PROXY_ENABLED` defaults to `false` (`config.py:588-598`). When false, `_apply_a2a_reverse_proxy_split` returns immediately (`agent_routes.py:715`) and the agent card's `url` remains the agent's own backend — callers connect directly.
2. **Reverse proxy is double-opt-in.** It activates only when the flag is true **and** `DEPLOYMENT_MODE=with-gateway` (`a2a_reverse_proxy_effective`, `config.py:1449-1460`). In registry-only mode it is force-disabled with a startup warning (`config.py:2406-2424`).
3. **Discovery flow:** agents register an `AgentCard` (`schemas/agent_models.py`: url, providers[], skills[], supported_protocol); discovery is list/filter + semantic search + well-known. The registry returns the card; it does not mediate the call.
4. **Endpoint advertisement:** in p2p mode `url` == backend. In reverse-proxy mode the gateway URL is advertised and the real backend is preserved in `proxy_pass_url` (`agent_routes.py:726-727`) — the split is explicit and idempotent.
5. **Authentication assumption:** A2A discovery/calls rely on the same token model; there is **no** A2A-specific runtime dependency on enterprise auth. p2p calls go agent→agent directly and are not gateway-authorized.
6. **Gateway involvement is optional.** nginx only emits `/agent/*` location blocks when reverse-proxy is effective.

**What could accidentally turn the registry into a mandatory data-plane hop:**

- Setting `A2A_REVERSE_PROXY_ENABLED=true` (or defaulting it on) — this rewrites every advertised agent `url` to the gateway, making the registry/gateway the mandatory path. **Keep the default `false` and do not "simplify" by hard-coding it on.**
- Removing the `proxy_pass_url` split such that the advertised URL and backend conflate — would lose the ability to distinguish gateway-facing from direct addressing.
- Any discovery response that strips the agent's direct endpoint and returns only a gateway URL.

---

## 11. MCP Invariants

Verified against `registry/api/server_routes.py`, `registry/services/server_service.py`, `registry/schemas/mcp_registry_schema.py`, `registry/api/virtual_server_routes.py`, `registry/api/wellknown_routes.py`, and `docker/lua/virtual_router.lua`.

**MCP protocol requirements (must survive):**

1. **Registration model:** servers described by the upstream MCP `server.json` schema plus gateway extensions (`mcp_registry_schema.py`) — auth credential (encrypted), backends[], metadata[]. Registration is a control-plane write, not a gateway dependency.
2. **Discovery model:** list/filter + semantic search + dynamic tool discovery (`docs/dynamic-tool-discovery.md`). Callers learn *what tools exist* and *where the server is* from the registry.
3. **OAuth metadata behavior:** well-known protected-resource endpoints (`wellknown_routes.py`) serve OAuth metadata per server connection URL — required for spec-compliant MCP client auth discovery. This is a **protocol requirement**, not enterprise surface; keep it.
4. **Protocol compliance / dynamic tool discovery:** virtual MCP servers aggregate tools across backends with aliasing; JSON-RPC routing is done by `virtual_router.lua` at the gateway.
5. **Gateway behavior:** in `with-gateway` mode nginx proxies `/mcp-proxy/{server_id}/*` to backends with auth enforcement. In `registry-only` mode, clients reach MCP servers directly.

**Enterprise product requirements (separable, not protocol):**

- Virtual-MCP tool *governance* (scope-filtered tool lists per group), per-user credential injection on egress, and LLM security scanning of tools are product/governance layers, not MCP requirements. They ride on the same routes but can be disabled without breaking protocol compliance.
- Credential handling: server auth credentials are stored encrypted; the *per-user SaaS vault* (egress_auth) is the enterprise part and is separable from the basic "server has a credential" model.

**Routing assumption to preserve:** the registry decides *what may be routed to*; the gateway moves bytes. Do not collapse them into an application-layer gateway — the reverse-proxy choice (`docs/design/architectural-decision-reverse-proxy-vs-application-layer-gateway.md`) was made specifically to stay protocol-agnostic.

---

## 12. Top 5 Highest-Value Removals

Ranked by value of removal/simplification. **Revised after divergence tracing (§25):** Preve is not trying to "clean the repository" — it is trying to create a maintainable personal edition that stays mergeable with upstream. Items whose deletion would raise upstream merge cost without reducing personal runtime burden are moved to **DEFER/dormant** rather than deleted. See §25 for the full re-evaluation.

| # | Capability | Complexity removed | Operational burden removed | Code/dep impact | Migration risk | Verdict (revised) |
|---|---|---|---|---|---|---|
| 1 | **Enterprise IdP fleet + group→scope mapping** (6 providers, `group_filter.py`, `setup/idp/`) | Very high | Very high (no IdP to run) | Large code + config | Medium (auth is load-bearing → seam first, Phase 4/5) | **Simplify behind seam**: one personal provider; keep `providers/base.py`; remove other providers only in Phase 7 if §25 confirms low merge cost |
| 2 | **LLM security scanning** (`cisco-ai-*-scanner`, torch, langchain, strands) | High | Medium | Huge dependency impact (largest image-size win) | Low | **Remove dependencies** (Phase 2); these are heavy third-party deps, not upstream-mergeable code, so removal has no upstream-conflict cost |
| 3 | **AWS deployment machinery** (Terraform, CDK, Helm, CodeBuild) | High (ops) | High | Large repo surface, **zero runtime impact** | Low (nothing imports it) | **DEFER — keep dormant in fork.** Deletion is deployment-only and safe, but it permanently diverges from upstream for no runtime benefit. Keep as unused dirs; revisit only if upstream churn makes merges painful |
| 4 | **Compliance audit** (durable fail-closed sink, HMAC, audit UI) | Medium | Medium | Medium | Low-Medium | **Reduce behind seam** (Phase 6): add the operational-event implementation; keep the compliance implementation available by config |
| 5 | **Per-user egress credential brokerage** (`egress_auth/`, `credentials-provider/`) | Medium-High | Medium | Medium | Low (default-off) | **DEFER — leave disabled.** Dormant, default-off, and deeply wired; deletion risk exceeds benefit |

---

## 13. Top 5 Abstractions to Preserve

| # | Abstraction | Why preserve even for one user |
|---|---|---|
| 1 | **Repository/storage abstraction** (`repositories/interfaces.py` + `factory.py`) | The single most valuable portability seam: swap the datastore without touching routes |
| 2 | **Unified entity model + registration spine** (servers/agents/skills/custom) | Runtime-agnostic registration/discovery for EVIE, Letta, Pi, Claude Code, Codex — the fabric's core value |
| 3 | **Gateway/control-plane separation** (`DEPLOYMENT_MODE`) | Keeps peer-to-peer the default and the gateway optional; protects against accidental data-plane lock-in |
| 4 | **Protocol metadata on entities** (`supported_protocol` + endpoint) | "Protocol, not platform": interoperability without coupling to any runtime's internals |
| 5 | **Identity seam** (`auth/dependencies.py` + `providers/base.py`) | Lets workforce IAM be swapped for the personal trust model at one boundary instead of across every route |

---

## 14. Documentation vs Implementation Contradictions

| # | Claim (doc) | Reality (code) | References |
|---|---|---|---|
| 1 | README/theory imply a FAISS-style vector search ("embeddings") | **No FAISS.** Vectors stored in MongoDB (`mcp_embeddings_*`); hybrid search does cosine on in-memory result sets; sentence-transformers lazy-loaded | `pyproject.toml` (no faiss), `repositories/documentdb/search_repository.py`, `services/semantic_search_service.py` |
| 2 | README positions the gateway as the single entry point for all calls | A2A is **peer-to-peer by default**; the gateway only fronts agents when double-opted-in | `config.py:588`, `agent_routes.py:715`; README line 70 does note p2p default — the tension is with the "one gateway for all" framing |
| 3 | "MongoDB abstraction layer" implies pluggable backends | All factories unconditionally return the DocumentDB/MongoDB impl; the abstraction is real but **only one backend family is implemented** (v1.24.8+) | `repositories/factory.py`, `config.py:99-110` |
| 4 | Docs list Cognito/Entra/Okta/Auth0 as first-class | OBO egress exchange is **Keycloak/Entra only**; others lack OBO | `auth_server/egress_obo.py`, `credentials-provider/oauth/egress_oauth.py` |
| 5 | Rate limiting presented as a feature | **Default off**; enforcement lives in the auth-server `/validate` hop, mirrored config in registry | `config.py:1825` (`default=False`) |
| 6 | `docs/design/storage-architecture-mongodb-documentdb.md` suggests multi-backend | `STORAGE_BACKEND` values all route to the same MongoDB-family repositories; `documentdb` differs only in SCRAM-SHA-1 auth selection | `config.py:99-110`, `utils/mongodb_connection.py` |
| 7 | `docs/index.md` describes "Amazon Cognito, Google, GitHub" OAuth as the headline | The full provider set is 6+ enterprise IdPs; index understates IAM scope | `docs/index.md`, `auth_server/oauth2_providers.yml` |
| 8 | README line-count budget comment says 350 lines CI-enforced | Verified present as an HTML comment; confirms docs are CI-gated (a docs-only PR must not break the line budget) | `README.md:1` |

---

## 15. Components Expensive to Remove but Harmless When Disabled

These look enterprise-heavy and add source size, but are deeply wired **and** cheap when off. Recommend leaving dormant rather than deleting, because deletion risk exceeds benefit:

| Component | Why expensive to remove | Why harmless when disabled | Recommendation |
|---|---|---|---|
| **Rate limiting + quarantine** (`registry/rate_limiting/`) | Woven into auth-server `/validate` + admin API + 9 test files + UI | Default off (`config.py:1825`); no counters written when off | **Leave dormant** |
| **Federation** (peer + vendor clients) | 5 schedulers, schema models, UI pages, peer sync | All behind flags; schedulers no-op when unconfigured | **Leave dormant**; delete only vendor (AgentCore/Anthropic/ASOR) clients if deps demand it |
| **Egress credential brokerage** (`egress_auth/`, `credentials-provider/`) | OAuth state codec, facade routes, secret-store factory, 18 test files | Disabled when no secret store configured | **Leave dormant** |
| **Registration webhooks/admission gates** | Fire-and-forget dispatch on register/delete | No-op when no webhook URLs configured | **Leave dormant** |
| **OTel instrumentation** | Auto-instrumentation across the app | No-op when `OTEL_EXPORTER_OTLP_ENDPOINT` unset | **Leave dormant** |

Deleting these buys little (they cost nothing at runtime when off) but risks breaking the shared registration/auth paths they hook into. Per frozen principle 6, *migration cost matters more than code-count reduction* — dormancy preserves the option to re-enable and keeps upstream cherry-picks clean.

---

## 16. Proposed Target Repository Shape

Optimized for one owner, coding-agent maintainability, clear module boundaries, easy upstream cherry-picks, and protocol/runtime portability — **not** minimum file count. Keep the upstream layout for anything retained so cherry-picks stay clean.

```
mcp-gateway-registry/                 (fork, tracking upstream where possible)
├── registry/                         # KEEP — control plane (unchanged layout)
│   ├── api/                          #   routes; enterprise route modules may be
│   │                                 #   feature-flagged off rather than moved
│   ├── repositories/                 #   KEEP — storage seam
│   ├── schemas/                      #   KEEP — agent/mcp/skill/custom models
│   ├── services/                     #   KEEP core; dormant services stay but flagged
│   ├── auth/                         #   SEAM (§20.1/§20.2) — add identity/trust seams;
│   │                                 #     enterprise impl retained, personal impl added
│   ├── audit/                        #   SEAM (§20.3) — add event-sink seam; compliance
│   │                                 #     impl retained, lightweight sink added (§9)
│   ├── health/                       #   KEEP
│   └── core/config.py                #   personal-edition defaults
├── auth_server/                      # SIMPLIFY — one personal provider + /validate
│   └── providers/                    #   base.py + one personal provider only
├── docker/                           # KEEP — nginx + Lua (optional front door)
├── frontend/                         # SIMPLIFY — entity/discovery pages; hide IAM/audit
├── docker-compose.yml                # KEEP — primary personal deployment
├── Dockerfile*                       # KEEP
├── api/                              # KEEP — registry_client.py / registry_management.py
├── scripts/                          # TRIM — keep init/backup; drop AWS/deploy scripts
├── docs/
│   └── preve/                        # personal-edition docs (this audit, decisions)
└── tests/                            # KEEP — trimmed to retained features
```

**Dormant (kept in the fork but not in the personal runtime profile; per §25):** `terraform/`, `infra/`, `charts/`, `buildspec.yml`, `setup/`, `keycloak/`, `pingfederate/`, `metrics-service/`, `credentials-provider/`, `landing/`, `agents/`+`servers/` samples, AWS-coupled workflows. These are excluded from the personal Compose profile and CI-required checks but **retained in the tree** to preserve upstream mergeability; they are physically deleted only in the optional, irreversible Phase 7 if §25 later confirms deletion is cheaper than dormancy.

**Removed outright (firm removals per §25):** `core/telemetry.py` (phones home; low coupling) and the heavy ML scanner *dependencies* (`cisco-ai-*`, torch, langchain — third-party deps, not upstream-mergeable code). Everything else is dormancy, not deletion.

**Why keep layout for retained code:** minimizing structural churn preserves the ability to cherry-pick upstream fixes (security patches to `registry/auth`, entity-model updates) with low merge friction — directly serving frozen principle 6 and the §19 survivability rule.

---

## 17. Exactly One Recommended First Implementation Slice

**Slice: introduce one validated Personal V1 deployment/configuration profile** that runs the existing system as a personal fabric using only existing capabilities, without deleting enterprise code and without simplifying auth yet.

This is an **implementation contract** for the next PR. It is deliberately low-risk: it proves the personal topology is achievable through configuration alone and establishes the boundary every later phase respects.

**What the slice must prove (acceptance criteria):**
1. **Registry-only:** boots with `DEPLOYMENT_MODE=registry-only` (`config.py:132-136`); nginx is not required for the default path.
2. **Peer-to-peer A2A default:** `A2A_REVERSE_PROXY_ENABLED=false` (`config.py:588`); a registered A2A agent's advertised `url` is its own backend, not a gateway URL (asserted via the `_apply_a2a_reverse_proxy_split` no-op path at `agent_routes.py:715`).
3. **MongoDB CE:** runs against `STORAGE_BACKEND=mongodb-ce` (`config.py:73-110`); no DocumentDB.
4. **Governance disabled unless explicitly enabled:** rate limiting, quarantine, federation, egress brokerage, and webhooks all default off; enabling any one requires an explicit flag.
5. **No AWS dependency for local/personal runtime:** the profile boots and passes smoke tests with no AWS credentials, region, or service env set.
6. **Search behavior explicitly defined:** the profile declares text-only vs. vector search; if vector search is off, search still returns correct keyword results (no silent breakage).
7. **Documented Compose/profile startup:** `docker compose -f docker-compose.preve.yml up` (or equivalent documented profile) reaches healthy from a clean checkout.
8. **MCP + A2A smoke tests pass:** register + discover one MCP server and one A2A agent end-to-end against the profile.

**Files/modules likely touched (additive only):**
- `registry/core/config.py` — add a composable profile preset (e.g., `PREVE_PROFILE`) that sets the flags above; must not change any default when unset.
- `docker-compose.preve.yml` (new) — registry + mongodb-ce (+ simplified auth path) only.
- `docs/preve/` — profile documentation and the runbook for the smoke tests.
- Optionally a frontend feature-flag to hide IAM/audit/federation nav (presentation only; no page deletion).

**Explicit non-goals (must NOT do):**
- No auth rewrite.
- No RBAC/scopes deletion.
- No audit deletion.
- No frontend redesign.
- No repository/storage replacement.
- No broad dependency cleanup (no torch/scanner removal in this slice).
- No deletion of Terraform/Helm/AWS assets unless proven entirely isolated (deferred to §25 review).

**Acceptance evidence required in the future PR:**
- Boot log excerpt showing healthy startup from the documented Compose path.
- Output of the MCP and A2A registration/discovery smoke tests.
- An effective-settings dump (or config unit test) proving the governance flags resolve to off and search mode is explicit.
- A network-egress check confirming no AWS endpoint is contacted.
- The existing `tests/unit` suite green (proving the preset does not alter default behavior when unset).

**Rollback strategy:** delete the profile/compose file; defaults untouched, so rollback is a trivial `git revert`.

This slice is low-risk, independently testable, and establishes the personal-edition architectural boundary (profile as the unit of "personal vs enterprise") without any irreversible change.

---

## 18. Final Recommendation

**Recommended V1 boundary (default = Option B, §21).** Inside Preve Personal Edition V1: the FastAPI registry control plane (entity spine for MCP servers, A2A agents, skills, custom entities) **with personal auth embedded via the §20.1 seam** (owner/trusted-agent/external), the repository/storage abstraction on MongoDB-CE, the health service, the discovery + well-known surface, and Docker Compose as the only deployment path. **Optional (not in the default topology):** the nginx front door (off by default, p2p A2A preserved), a simplified single-provider auth-server for OAuth/OIDC login, a trimmed frontend, and opt-in vector search. **Enterprise-only / dormant (outside the personal profile, not deleted):** all enterprise IdPs, group RBAC, compliance audit (→ event log behind the §20.3 seam), egress brokerage, federation, rate-limit/quarantine governance, LLM scanning, telemetry, and all AWS/K8s deployment machinery.

**Top 5 removal/simplification targets (revised per §25 — most are simplify/defer, not delete).**
1. Enterprise IdP fleet + group→scope mapping (→ one personal provider behind `providers/base.py`; seam first, delete other providers only if §25 confirms low merge cost).
2. LLM security scanning + torch/langchain dependency stack (→ remove dependencies; no upstream-merge cost).
3. AWS deployment machinery (Terraform, CDK, Helm, CodeBuild) (→ **keep dormant in fork**; deployment-only, zero runtime cost, deletion would diverge from upstream).
4. Compliance audit (→ reduce to a lightweight operational-event implementation behind the audit seam; keep compliance mode available).
5. Per-user egress credential brokerage (→ leave disabled/dormant).

**Top 5 abstractions to preserve.**
1. Repository/storage abstraction.
2. Unified entity model + registration spine.
3. Gateway/control-plane separation (`DEPLOYMENT_MODE`).
4. Protocol metadata on entities (protocol, not platform).
5. Identity seam (`auth/dependencies.py` + provider interface).

**First implementation slice.** The personal-profile configuration boundary (§17): a validated Compose profile running registry-only + p2p A2A + governance-off + text-only search on MongoDB-CE, with no code deletion — reversible and independently testable.

**Open questions (require owner decision).**
1. **Datastore:** keep MongoDB-CE for V1 (recommended — zero code change) or invest in a lighter store behind the existing repository seam?
2. **Personal identity provider:** single owner credential + agent tokens (simplest) vs. one self-hosted OIDC (e.g., a minimal provider) — how much does the owner value standards-based login?
3. **Semantic search:** ship V1 text-only (light image) and add vector search as an opt-in extra, or is natural-language capability discovery core to the Preve experience from day one?
4. **Peer federation:** is cross-registry (e.g., a second personal instance or a friend's fabric) a near-term need? If yes, keep the dormant peer-federation code; if no, it becomes a removal candidate in Phase 7.
5. **Gateway default:** should the personal fabric default to *no* gateway (pure p2p, registry-only) or ship the nginx front door enabled for reaching agents from other devices?

> **Superseded by §26.** The open-questions list above is retained for narrative flow; the authoritative, deduplicated owner-decision list is §26 (Owner Decisions Required Before Implementation).

---

## 19. Upstream Survivability Strategy

Preve is a long-lived fork of `agentic-community/mcp-gateway-registry`. The migration must not paint the fork into a corner where every upstream security patch or entity-model change requires a hand-merge. This section defines the preferred customization hierarchy, ordered from **lowest to highest long-term upstream-conflict risk**, and the rule the fork will follow.

### 19.1 Customization hierarchy (low → high conflict risk)

| # | Approach | Upstream conflict risk | Maintenance cost | Reversibility | Acceptable for Preve when… | Example in this repo |
|---|---|---|---|---|---|---|
| 1 | **Configuration / deployment profiles** | Lowest | Lowest | Trivially reversible (delete the profile) | The desired behavior already exists behind flags/env | `docker-compose.preve.yml` (new), `DEPLOYMENT_MODE=registry-only`, `RATE_LIMITING_ENABLED=false`, `A2A_REVERSE_PROXY_ENABLED=false`, `STORAGE_BACKEND=mongodb-ce` (`config.py`) |
| 2 | **Feature flags** | Low | Low | Reversible (flip flag) | Behavior diverges but both paths are cheap to keep | `REGISTRY_MODE` (`config.py:139-146`), `enable_wellknown_discovery`, a new `PREVE_PROFILE` preset |
| 3 | **Adapters / interfaces / provider implementations** | Low-Medium | Medium | Reversible (select a different impl) | Upstream already defines a seam, or one can be added without touching callers | `auth_server/providers/base.py` + per-IdP impls, `registry/repositories/interfaces.py` + `factory.py`, `registry/secrets/factory.py` |
| 4 | **Isolated replacement modules** | Medium | Medium | Reversible (swap module) | A whole subsystem can be swapped behind an existing seam without editing its consumers | a personal trust-classification module behind the §20.2 trust seam; a slim event sink behind the §20.3 audit seam |
| 5 | **Route / service rewrites** | High | High | Hard (callers change) | Only when no seam exists and the behavior is core to Preve's value | rewriting `agent_routes.py` discovery semantics — avoid for V1 |
| 6 | **Deletion of upstream modules** | High (merge conflicts on every upstream touch of that module) | Low after deletion | Hard (must restore from upstream) | Only when the module is deployment-only or provably weakly coupled and upstream churn on it is low | `terraform/`, `infra/`, `charts/` (deployment-only) — but see §25: deletion still diverges |
| 7 | **Invasive core-model rewrites** | Highest | Highest | Effectively irreversible | Never for V1; only if upstream abandons the model | changing the `AgentCard`/MCP `server.json` schema — forbidden by invariant §22 |

### 19.2 The standing rule

> **Prefer configuration boundary → stable abstraction seam → replacement implementation → deletion only when upstream coupling is proven negligible.**

Concretely: try (1) a config/profile first; if that is insufficient, (2) add or use a feature flag; if the divergence is structural, (3) introduce or reuse an adapter/interface and supply a personal implementation; only (4) replace a module wholesale when a seam already isolates it; and (5) delete upstream code only after tracing shows it is deployment-only or weakly coupled (§25) and upstream churn on it is low. Core-model rewrites (7) are off the table for Personal V1.

### 19.3 High-conflict upstream surfaces to avoid touching

These files/dirs change frequently upstream or sit on shared paths; Preve should route around them via seams/flags rather than edit them:

- `registry/main.py` — the lifespan/router registry; every upstream feature adds routers here. Add Preve routes via new modules, not edits.
- `registry/core/config.py` — the central `Settings`; upstream adds settings constantly. Add Preve flags additively at the end; never reorder or rename upstream fields.
- `registry/api/server_routes.py` (6,574 lines) and `registry/api/agent_routes.py` (2,894 lines) — the highest-churn route files. Prefer new route modules or feature-flagged branches over rewrites.
- `registry/auth/dependencies.py` — 217 route injection points converge here. Touch only to *add* a seam, never to change the enterprise path's behavior.
- `registry/repositories/interfaces.py` — the storage contract; upstream extends it per entity. Do not change existing method signatures.
- `auth_server/server.py` (8,303 lines) — monolithic, high-churn; isolate rather than edit.
- `docker/nginx_rev_proxy_http_and_https.conf` — templated; upstream adds location blocks. Do not hand-edit generated regions.

### 19.4 Areas where deletion is relatively safe (deployment-only or weakly coupled)

Deletion here has near-zero runtime blast radius and minimal upstream-merge cost *because nothing in the runtime imports them*:

- `terraform/`, `infra/` (CDK), `charts/`, `buildspec.yml`, `setup/`, `landing/` — deployment/marketing only.
- AWS-coupled GitHub workflows (ECR push, Terraform plan, CodeBuild triggers) — CI-only.
- `agents/`, `servers/` sample content — demos, no runtime consumer.
- `keycloak/`, `pingfederate/` provisioning scripts — deployment-time only.

**However** — see §25 — "safe to delete" (no runtime coupling) is not the same as "cheap to delete" (upstream-merge cost). Deployment-only dirs are *safe* to delete but still *diverge* the fork; §25 recommends keeping them dormant unless upstream churn makes them a merge burden.

---

## 20. Freezing the Architectural Seams (no rewrites)

The audit correctly identified auth, RBAC/scopes, and audit as deeply wired (217 `Depends(...)` injection points converge on `registry/auth/dependencies.py`). **The migration must not rewrite these systems in place.** Instead, it must first freeze stable abstraction seams, move the enterprise implementations behind them unchanged, and only then add personal implementations. This is Phase 4 (seams) before Phase 5 (personal implementation) in §5.

### 20.1 Identity seam

- **Conceptual interface:** principal resolution (who is calling), authentication (proof), session/token validation (is the credential valid now), machine identity (agent/service credential). No new class names are mandated — the existing `registry/auth/dependencies.py` already exposes the right functions (`resolve_session_from_cookie`, `get_current_user`, `nginx_proxied_auth`, `enhanced_auth`, `_context_from_internal_token`).
- **Current implementation location:** `registry/auth/dependencies.py` (resolution + user-context derivation), `registry/auth/session_store.py` (motor-backed sessions), `registry/auth/internal.py` (HS256 machine tokens over shared `SECRET_KEY`), `registry/auth/proxied_token.py`, `registry/auth/session_crypto.py`.
- **Consumers:** all 217 route `Depends(...)` injection points across `registry/api/*.py`; the nginx `/validate` subrequest contract (`X-User`/`X-Scopes`/`X-Groups`).
- **Proposed stable contract:** a single `resolve_principal(request) -> Principal` seam where `Principal = { id, kind: owner|agent|external, credential_id, auth_method }`. Everything downstream consumes `Principal`, never IdP-specific claims.
- **Enterprise implementation:** the current multi-IdP resolution (session cookie via auth-server, IdP JWT, internal token) behind the same `resolve_principal`.
- **Personal implementation:** owner credential + agent bearer tokens (the existing self-signed JWT path), no IdP.
- **Migration risk:** **High if edited in place; Low if added as a seam.** The contract must be additive: existing resolvers become the enterprise implementation with zero route changes.

### 20.2 Authorization / trust seam

Separate three concerns that the current engine fuses:

- **Identity** — who the principal is (from §20.1).
- **Trust classification** — a coarse label: `owner` / `trusted-agent` / `external`.
- **Authorization decision** — whether principal X may perform action Y on entity Z.

- **Current implementation location:** `registry/auth/access_resolver.py` (`UserAccess`, `resolve_scope_access`, `get_user_accessible_servers/tools`), fed by `ScopeRepositoryBase` (`registry/repositories/interfaces.py:526+`) and `SCOPES_CONFIG` group mappings; group enrichment in `auth_server/group_filter.py` / `mongodb_groups_enrichment.py`.
- **Consumers:** route handlers via `get_accessible_servers_for_user`, `user_can_access_server`, `ui_permission_required`, etc. (`registry/auth/dependencies.py`).
- **Proposed stable contract:** `classify_trust(principal) -> TrustTier` and `authorize(principal, action, resource) -> bool`. The trust classifier is pluggable; the authorizer consumes `(principal, trust_tier, resource)`.
- **Enterprise implementation:** group→scope→ACL resolution (today's engine) as the `classify_trust`/`authorize` backing.
- **Personal implementation:** `classify_trust` returns the 3 tiers from a static owner map + agent-token presence; `authorize` allows owner everything, trusted-agent invoke/discover, external read-only discovery. **The registry core never learns about enterprise IdPs** — it only sees `TrustTier`.
- **How this differs from group→scope RBAC:** enterprise RBAC derives fine-grained per-tool ACLs from IdP group membership; the personal model derives a coarse tier from a local trust map. The seam keeps the *decision point* (`authorize`) identical so route handlers are untouched; only the *classification source* changes.
- **Migration risk:** **Medium.** The resolver is centralized, but scope config and group mapping are broad; the seam must preserve the enterprise path byte-for-byte.

### 20.3 Audit / operational-event seam

Separate **security/compliance audit** (durable, fail-closed, non-repudiable) from **personal operational event history** (lightweight, best-effort).

- **Current implementation location:** `registry/audit/service.py` (`AuditLogger`, `enforce_durable_audit_sink`, `NonDurableAuditError`), `registry/audit/middleware.py` (request/response capture), `registry/audit/models.py` (record taxonomy), `registry/repositories/audit_repository.py` (MongoDB sink).
- **Consumers:** audit middleware on the app; auth-server via `registry.audit.mcp_logger.MCPLogger`; audit UI pages.
- **Proposed stable contract:** an `EventSink` seam with `emit(event) -> None`, where `event = { ts, actor, action, target_entity, protocol, endpoint, outcome }` (§9). The fail-closed/durable/HMAC behavior is a *property of the compliance implementation*, not of the seam.
- **Enterprise implementation:** today's durable MongoDB sink with `enforce_durable_audit_sink` and HMAC.
- **Personal implementation:** a lightweight append to a `preve_events` collection (or stdout), **no fail-closed startup requirement** for ordinary operation.
- **Preserving stronger semantics later:** the seam keeps the compliance implementation intact and selectable by config, so personal mode does not foreclose re-enabling durable audit.
- **Migration risk:** **Low-Medium.** The sink is centralized in `AuditLogger`; the main risk is the middleware capture path, which must keep working for whichever sink is selected.

---

## 21. Personal V1 Topology — Challenging the Default

The earlier recommendation (§6) assumed a separate auth-server. That assumption is challenged here. Three runtime shapes are evaluated on equal footing.

- **Option A — Registry + MongoDB-CE + simplified auth-server + optional nginx.** Keeps the current two-service split, reduces auth-server to one personal provider.
- **Option B — Registry + MongoDB-CE with personal auth embedded in the registry + optional nginx.** Collapses the identity broker into the registry process; no separate auth-server in the default topology.
- **Option C — Registry + lighter persistence abstraction + personal auth + no gateway.** Swaps MongoDB for a lighter store and drops nginx from the default topology.

### 21.1 Comparison

| Criterion | Option A (separate auth-server) | Option B (embedded personal auth) | Option C (lighter store + no gateway) |
|---|---|---|---|
| **Upstream compatibility** | **Highest** — preserves the two-process split and the `/validate` contract; merges stay clean | Medium — registry gains an embedded resolver, but via the §20.1 seam so routes are untouched; auth-server becomes optional | Low-Medium — adds a new repository backend; the seam absorbs it, but a second backend is a maintenance surface |
| **Complexity** | Medium (two services to run) | **Lower** (one app process) | Medium (new backend code) |
| **Operational burden** | Medium (auth-server container + config) | **Lowest** (one process + MongoDB) | Low (no gateway) but new-store ops unknown |
| **Security** | High — isolation between broker and registry; `/validate` is a separate trust boundary | Medium — auth logic shares the registry process; the shared-`SECRET_KEY` HS256 path (`registry/auth/internal.py:53-95`) already proves embedded validation is feasible, but the blast radius of a registry compromise grows | Medium — depends on the new store's auth/maturity |
| **Migration effort** | **Lowest** — auth-server is reduced by config/provider selection, not moved | Medium — requires the §20.1 seam + an embedded personal resolver | **Highest** — new repository backend + auth + topology change at once |
| **Future multi-agent scalability** | High — separate broker scales independently; M2M minting already centralized | Medium — fine for a personal fleet; embedding does not block many agents, just co-locates identity | Medium — depends on store |
| **MCP + A2A support** | Clean — unchanged | Clean — unchanged (p2p A2A preserved; nginx optional) | Clean for A2A (no gateway = pure p2p); MCP gateway features lost if nginx dropped entirely |

### 21.2 Recommendation

**Default Personal V1 = Option B (registry + MongoDB-CE + embedded personal auth), with the auth-server retained as an optional process for owners who want the existing OAuth/OIDC login or a future return to multi-user.** Rationale:

- Option B has the lowest operational burden (the owner's stated priority) and the repository already proves embedded token validation is feasible — registry and auth-server share `SECRET_KEY` for HS256 (`registry/auth/internal.py:53-95`), and `registry/auth/dependencies.py:814` already handles a signed-token path without a network hop to auth-server.
- Crucially, Option B is reached **through the §20.1 seam**, not by rewriting routes: the 217 `Depends(...)` points keep calling `resolve_principal`; only the resolver's backing changes. This preserves upstream compatibility far better than editing route handlers.
- Option A remains fully supported (the auth-server is not deleted) for owners who prefer the existing login UX. Option C is **deferred**: a lighter store is not worth a second repository backend in V1, and dropping nginx entirely is a topology choice the owner can make via `DEPLOYMENT_MODE=registry-only` without removing the gateway code.

**Do not optimize for the fewest processes at the expense of maintainability** — Option B is chosen because it is *simpler to operate*, not merely smaller; it keeps the identity logic behind a seam so it remains testable and reversible.

**Component classification for Personal V1:**

- **Default:** registry (with embedded personal auth via the §20.1 seam), MongoDB-CE, the discovery + well-known + health surfaces, the operational-event sink (§20.3).
- **Optional:** nginx front door (`with-gateway`), auth-server (for OAuth/OIDC login), frontend (trimmed), semantic search (opt-in extra).
- **Deferred:** peer federation, egress credential brokerage, a lighter persistence backend (Option C).
- **Enterprise-only (dormant, not in the personal profile):** workforce IdP fleet, group→scope RBAC, compliance audit sink, rate-limit/quarantine governance, registration webhooks, LLM security scanning, telemetry, Terraform/Helm/CDK.

---

## 22. Migration Invariants

These properties must remain true **throughout** the migration, in every phase. A phase that would violate an invariant is not allowed to proceed. Each is grounded in current code.

1. **MCP registration and discovery must remain functional.** The `server.json` registration model and discovery (`registry/api/server_routes.py`, `services/server_service.py`, `schemas/mcp_registry_schema.py`) keep working in every phase.
2. **A2A agents must remain directly addressable peer-to-peer by default.** `A2A_REVERSE_PROXY_ENABLED` stays `false` by default (`config.py:588`); `_apply_a2a_reverse_proxy_split` (`agent_routes.py:715`) stays a no-op unless double-opted-in.
3. **The registry must not silently become a mandatory data-plane proxy.** No change may rewrite advertised agent/server endpoints to a gateway URL without an explicit, visible opt-in; the `proxy_pass_url` split (`agent_routes.py:726-727`) must be preserved.
4. **Protocol metadata must remain transport-neutral.** `supported_protocol` and endpoint advertisement (`schemas/agent_models.py`, `docs/supported-protocol-and-trust-fields.md`) must not be coupled to any single runtime's internals.
5. **The repository abstraction must remain intact.** `registry/repositories/interfaces.py` method signatures and the `factory.py` seam are not broken; consumers keep using the abstraction, never a concrete store directly.
6. **Personal mode must not require enterprise IdPs.** A personal deployment boots and serves discovery/invoke with no Keycloak/Entra/Okta/Auth0/Cognito/PingFederate.
7. **Enterprise mode must not be broken merely to support personal mode.** With the enterprise implementation selected, the existing auth/RBAC/audit suites pass unchanged; personal additions are additive (flags/seams), not destructive edits.
8. **Security-sensitive behavior must fail explicitly, never silently downgrade.** Examples to preserve: CORS fail-closed, scopes fail-closed on missing `tools` key, audit `enforce_durable_audit_sink` (`registry/audit/service.py:29`), SSRF guards (`registry/utils/url_guard.py`). Personal mode may *select* a lighter implementation, but must not silently weaken one.
9. **Personal configuration must be reproducible and testable.** The personal profile is a versioned, documented artifact (§17) with a config unit test and a Compose smoke test.
10. **No hidden dependency on AWS-specific infrastructure.** The personal runtime path must not require AWS credentials/services; AWS-specific code (AgentCore federation, Secrets Manager backend, DocumentDB SCRAM path) stays behind its existing opt-in flags/backends.

---

## 23. First Implementation Slice — Precise Contract

*(Defined in §17.)* The first slice is the validated Personal V1 configuration/deployment profile. It is intentionally limited to the **lowest-risk tier of the §19 hierarchy (configuration/profile)**: it adds a Compose profile and a config preset, proves the personal topology through configuration alone, and touches no auth, RBAC, audit, repository, frontend-structure, or deployment-asset code. Its acceptance criteria and required evidence are enumerated in §17. It is the concrete realization of Phase 0–1 in §5.

---

## 24. Fork-Divergence Risk Table

Classifies each major migration candidate by how much it diverges the fork from upstream (higher divergence = harder future merges). Distinct from runtime risk.

| Candidate | Divergence risk | Why |
|---|---|---|
| **Docker Compose profiles** | **LOW** | Additive new file (`docker-compose.preve.yml`); upstream rarely touches a new file; zero edits to existing compose |
| **AWS Terraform/CDK** | **LOW if kept dormant / HIGH if deleted** | Deployment-only; upstream edits it, so *deleting* it creates merge conflicts on every upstream infra change — keep dormant |
| **Helm `charts/`** | **LOW if dormant / HIGH if deleted** | Same reasoning as Terraform; upstream actively maintains charts |
| **IdP provider implementations** (`auth_server/providers/*.py`) | **MEDIUM** | Adding a personal provider is additive (LOW); *deleting* upstream providers diverges (HIGH). Prefer add + select |
| **`auth_server/server.py`** | **HIGH** | Monolithic, high-churn, central to upstream auth; any edit conflicts. Isolate, don't edit |
| **Scopes engine** (`auth/access_resolver.py`, scope repo) | **HIGH** | Core to upstream RBAC; editing diverges. Add the §20.2 trust seam instead of rewriting |
| **Registry route files** (`server_routes.py`, `agent_routes.py`) | **HIGH** | Highest-churn files; rewrites guarantee conflicts. Add routes/flags, don't rewrite |
| **Repository interfaces** (`repositories/interfaces.py`) | **HIGH if signatures change / LOW if only extended** | Upstream extends per entity; changing existing signatures breaks merges and all backends |
| **A2A models/routes** (`schemas/agent_models.py`, `agent_routes.py`) | **MEDIUM** | Core to Preve but also upstream-active; change additively, preserve the p2p invariant |
| **MCP models/routes** (`mcp_registry_schema.py`, `server_routes.py`) | **MEDIUM** | Tracks upstream MCP spec; additive extension only |
| **Frontend IAM pages** (`IAM*.tsx`, `Audit*.tsx`, `Federation*.tsx`) | **LOW** | Hiding via a feature-flag/nav flag is additive; upstream UI churn is contained to these components |
| **Audit subsystem** (`registry/audit/`) | **MEDIUM** | Add the §20.3 sink seam (additive) = LOW; rewriting record taxonomy = HIGH |
| **Semantic search stack** (`semantic_search_service.py`, embeddings) | **MEDIUM** | Making it opt-in via extras/lazy-load is additive; removing the search route contract = HIGH |

---

## 25. Re-evaluation of REMOVE Recommendations

Preve's goal is a **maintainable personal edition**, not a "clean repository." Every item previously marked REMOVE is re-judged on four questions: Is deletion necessary for Personal V1? Can it stay dormant? Does deletion raise upstream-merge cost? Does unused dependency weight affect runtime?

| Item | Necessary for V1? | Can stay dormant? | Deletion raises merge cost? | Dep weight affects runtime? | **Revised verdict** |
|---|---|---|---|---|---|
| Terraform `aws-ecs/`, CDK `infra/` | No | Yes | **Yes** (upstream edits infra) | No (deployment-only, never imported) | **DEFER — keep dormant** |
| Helm `charts/` | No | Yes | **Yes** (upstream maintains charts) | No | **DEFER — keep dormant** |
| `buildspec.yml` | No | Yes | Low (single file, rarely edited) | No | **DEFER — keep dormant** (delete only if it rots) |
| Extra IdP providers (5 of 6) | No (one suffices) | Yes (config-selected) | Yes (upstream may patch providers) | No (unused provider code is idle) | **DEFER — add personal provider, select it; delete others only in Phase 7 if merge cost proves low** |
| Egress brokerage (`egress_auth/`, `credentials-provider/`) | No | Yes (default-off) | Medium | No (dormant) | **DEFER — leave disabled** |
| Rate limiting / quarantine | No | Yes (default-off) | Medium | No | **DEFER — leave disabled** |
| `agents/`, `servers/` samples | No | Yes | Low | No | **DEFER — keep as reference examples** |
| LLM security scanner **deps** (`cisco-ai-*`, torch, langchain) | No | Partially (deps still install) | **No** (third-party deps, not upstream-mergeable code) | **Yes** (image size, install time) | **REMOVE dependencies (Phase 2)** — no upstream-merge cost, real runtime/image win |
| Telemetry heartbeat (`core/telemetry.py`) | No | Could, but it phones home | Low (small, self-contained) | Minor (external call) | **REMOVE** — inappropriate for personal, low coupling |

**Net change:** of the original REMOVE set, only `core/telemetry.py` and the heavy ML scanner *dependencies* remain firm removals — both are low-upstream-coupling and provide no personal value. Everything else moved to **DEFER/dormant**, because retaining dormant upstream code is cheaper than permanently diverging from a fork Preve wants to keep mergeable.

---

## 26. Owner Decisions Required Before Implementation

Only decisions that cannot be inferred from repository evidence or the stated Project Preve goal. Everything else is derivable from the audit.

1. **Default topology: Option B (embedded personal auth) vs. Option A (separate simplified auth-server)?** §21 recommends B for lowest operational burden while keeping A available, but the choice hinges on the owner's tolerance for co-locating identity logic with the registry versus running one extra container. Not decidable from code.
2. **Personal identity mechanism: a single owner credential + agent bearer tokens, or a minimal self-hosted OIDC provider?** Both are implementable behind the §20.1 seam; the trade-off is standards-based login UX versus operational simplicity. A values call, not a technical one.
3. **Semantic search in V1: ship text-only by default (lighter image, faster installs) and make vector search an opt-in extra — or is natural-language capability discovery core enough to Preve to pay the dependency cost from day one?** A product-priority decision; the code supports either.
4. **Peer federation: near-term need or not?** If cross-registry federation (a second personal instance, a collaborator's fabric) is anticipated, keep the dormant peer-federation code; otherwise it becomes a Phase 7 removal candidate. Only the owner knows the roadmap.
