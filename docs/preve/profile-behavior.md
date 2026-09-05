# Preve Personal Edition — Profile Behavior (Slice 1)

*Scope: this document describes exactly what the Slice 1 personal profile changes,
which mechanism it uses, and why. It is the companion to
[`personal-edition-migration-audit.md`](personal-edition-migration-audit.md) (the
frozen architecture baseline) and
[`personal-v1-runbook.md`](personal-v1-runbook.md) (the operational runbook).*

## What this slice is

Slice 1 proves that the existing upstream registry, auth-server, and MongoDB
Community Edition can run as a personal, single-owner fabric using **only
existing, already-shipped configuration surfaces** — no new auth
implementation, no deleted enterprise code, no redesigned config system.

Two additive artifacts implement it:

1. **`PREVE_PROFILE=personal`** — a config preset in
   [`registry/core/config.py`](../../registry/core/config.py)
   (`PREVE_PERSONAL_PROFILE_DEFAULTS` / `_apply_preve_personal_profile`).
2. **`docker-compose.preve.yml`** — a standalone Compose file that runs a
   3-container topology (`mongodb`, `registry`, `auth-server`) with the
   personal profile's environment variables set explicitly.

Both are pure "existing feature, new default value" changes. Neither adds a
second configuration system, changes an existing field's type or default, nor
touches a route handler, the nginx `/validate` contract, or a repository
interface signature.

## The `PREVE_PROFILE` config preset

`registry/core/config.py` defines `PREVE_PERSONAL_PROFILE_DEFAULTS`, a
dictionary of `ENV_VAR_NAME -> value`, and `_apply_preve_personal_profile()`,
which runs once at module import time, **before** `Settings()` is
instantiated:

```python
if os.environ.get("PREVE_PROFILE", "").strip().lower() != "personal":
    return
for key, value in PREVE_PERSONAL_PROFILE_DEFAULTS.items():
    os.environ.setdefault(key, value)
```

Two properties make this safe and additive:

- **No-op when unset.** If `PREVE_PROFILE` is not exactly `"personal"`
  (case-insensitive, trimmed), the function returns immediately and not a
  single environment variable is touched. Every `Settings` field resolves to
  its normal upstream default. This was verified directly (see the
  validation report) by importing `registry.core.config` with `PREVE_PROFILE`
  unset and confirming `deployment_mode == "with-gateway"` (the upstream
  default) and every other touched field also at its upstream default.
- **`os.environ.setdefault`, never `os.environ[...] =`.** An operator who has
  already set one of these variables explicitly (via shell, `.env`, or
  Compose) is never overridden. This was also verified directly: setting
  `PREVE_PROFILE=personal` **and** `DEPLOYMENT_MODE=with-gateway` in the same
  process resolves `deployment_mode == "with-gateway"` — the operator's
  explicit choice wins.

This is not a new configuration mechanism. It is the same
"environment-variable-with-a-default" pattern every other setting in this
file and in `docker-compose.yml` already uses; the preset just bundles a
documented, reversible group of them under one flag, mirroring the existing
`REGISTRY_MODE` feature-flag pattern (`docs/preve/personal-edition-migration-audit.md`
§19.1, tier 2).

## Every value the profile changes, and why

| Variable | Personal value | Upstream default | Already matched upstream default? | Why |
|---|---|---|---|---|
| `DEPLOYMENT_MODE` | `registry-only` | `with-gateway` | No — this is the one substantive change | Disables dynamic nginx location-block generation for MCP servers (`nginx_updates_enabled`); the control plane does not depend on the gateway being used as a data-plane hop. See "What `registry-only` does and does not eliminate" below. |
| `A2A_REVERSE_PROXY_ENABLED` | `false` | `false` | Yes | Restated explicitly so the peer-to-peer A2A invariant (audit §10/§22.2) is visible in the profile itself, defensive against a future upstream default change. |
| `STORAGE_BACKEND` | `mongodb-ce` | `mongodb-ce` | Yes | Restated explicitly; no AWS DocumentDB. |
| `RATE_LIMITING_ENABLED` | `false` | `false` | Yes | Governance feature; restated for explicitness/auditability. |
| `RATE_LIMIT_QUARANTINE_FAIL_CLOSED` | `false` | `false` | Yes | Quarantine is moot while rate limiting itself is off; restated for explicitness. |
| `REGISTRATION_GATE_ENABLED` | `false` | `false` | Yes | Admission-control webhook; enterprise governance, off by default already. |
| `EGRESS_AUTH_ENABLED` | `false` | `false` | Yes | Per-user SaaS credential vault; deferred per audit §6/§21. |
| `FEDERATION_STATIC_TOKEN_AUTH_ENABLED` | `false` | `false` | Yes | Peer-federation auth path; not needed for a single-owner deployment. |
| `AWS_REGISTRY_FEDERATION_ENABLED` | `false` | *(unset; not a pydantic field, read directly from `os.environ` in `registry/main.py:_apply_aws_registry_env_vars`)* | Effectively yes (unset already means the env-var override path is skipped) | Restated explicitly so the profile does not depend on whatever is already stored in the federation config document. |
| `TELEMETRY_ENABLED` | `false` | `true` | **No** | The startup-ping/heartbeat telemetry phones home to an external endpoint (audit §25); inappropriate for a personal deployment. Disabling it via the existing flag is in-scope for Slice 1. Physically removing `registry/core/telemetry.py` is a **separate, later** action (audit Phase 3 / §27 note 3) and is **not** done in this slice. |
| `MCP_TELEMETRY_DISABLED` | `1` | *(unset)* | No | Belt-and-braces: `core/telemetry.py` also reads this env var directly (independent of `settings.telemetry_enabled`) for the IMDS cloud-provider probe; setting both closes every telemetry code path. |

Everything not listed above (custom entity types, semantic search, scopes
configuration, audit subsystem, registration webhooks, RBAC, session
handling, etc.) is **untouched** by the preset and keeps its normal upstream
behavior. `REGISTRY_MODE` is deliberately left at its default (`full`): a
personal fabric benefits from having MCP servers, A2A agents, skills, and
custom entities all available, not a restricted subset.

## What `DEPLOYMENT_MODE=registry-only` does and does not eliminate

This is the one place a plain reading of the requirement ("gateway/nginx is
not silently required for the default personal control-plane path") needs a
precise, honest answer, because the registry Docker image bundles nginx
unconditionally (`docker/Dockerfile.registry`, `docker/registry-entrypoint.sh`)
— this is existing upstream behavior, not something Slice 1 changes or could
change without touching `docker/` nginx templates and the entrypoint, which
is out of scope.

**What `registry-only` does eliminate:**

- No dynamic nginx `location` blocks are generated per registered MCP server
  or per A2A agent (`nginx_updates_enabled` is `false`,
  `registry/core/config.py:1444-1447`). Registered MCP/A2A traffic is never
  forced through nginx as a mandatory data-plane hop.
- `A2A_REVERSE_PROXY_ENABLED` is force-disabled regardless of its own value
  (`a2a_reverse_proxy_effective`, `config.py:1449-1460`), so an agent's
  advertised `url` always equals its own backend — the registry/gateway can
  never silently become a mandatory hop for A2A calls. This was proven by
  reading `_apply_a2a_reverse_proxy_split` (`registry/api/agent_routes.py:692-727`):
  it returns immediately (a no-op) whenever `a2a_reverse_proxy_effective` is
  false, which it always is in `registry-only` mode.
- The registry's own container `HEALTHCHECK` targets `http://localhost:7860/health`
  directly (uvicorn, not nginx) — see `docker/Dockerfile.registry`. Health
  checks and any future direct/local access do not require nginx.

**What `registry-only` does *not* eliminate (existing, documented upstream
behavior, unchanged by this slice):**

- nginx still runs inside the same container (it is part of the shipped
  image regardless of `DEPLOYMENT_MODE`) and is still the **static** front
  door for the Registry API (`/api/*`, `/v0.1/*`). Per
  [`docs/registry-api-auth.md`](../registry-api-auth.md): "Every call to a
  Registry API endpoint passes through the auth server's `/validate`
  endpoint before reaching the registry application." This routing is part
  of the base nginx config template, not something `nginx_updates_enabled`
  toggles.
- Consequently, registering or discovering an MCP server or A2A agent via
  the Registry API in the default personal profile still goes through the
  bundled nginx (port 8080) and the auth-server's `/validate` hop, exactly
  as it does in the enterprise topology. This is **not** a new requirement
  introduced by Slice 1 — it is the existing, unmodified Registry API
  authentication architecture, and changing it would mean rewriting
  `registry/auth/dependencies.py`'s consumers or the nginx `/validate`
  contract, both explicitly forbidden for this slice.
- A direct call to port 7860 (bypassing nginx) is exposed for host health
  checks, but `nginx_proxied_auth`'s fallback path for a request with no
  internal token accepts only a session cookie
  (`registry/auth/dependencies.py:844-864`), not a bearer token. See "Known
  limitations" in the runbook for the practical effect of this.

In short: `registry-only` mode removes nginx as a **mandatory relay for MCP
tool calls / A2A traffic** (the invariant the architecture audit calls out
as critical, §10) and removes the requirement to run a *separate* gateway
container. It does not, and was never claimed to, remove nginx from the
**Registry API's own existing authentication path** — that would be an auth
rewrite, which is explicitly out of scope for Slice 1.

## Why the auth-server is included in the default personal topology

The architecture audit (§21, §27 note 2) leaves the auth-server's presence
in the default topology as an open, non-blocking question: "the auth-server
container is simply included-or-not in the Compose profile." Slice 1
includes it, for one concrete reason verified in code: the Registry API's
only existing IdP-independent, bearer-token credential is the **static
registry token** (`REGISTRY_STATIC_TOKEN_AUTH_ENABLED` /
`REGISTRY_API_TOKEN`, documented in
[`docs/registry-api-auth.md`](../registry-api-auth.md)), and that credential
is validated by the auth-server at the nginx `/validate` hop — there is no
code path in the registry process itself that accepts it directly. Using
this **already-shipped, already-documented** mechanism, with every
enterprise IdP explicitly disabled (`KEYCLOAK_ENABLED=false`,
`COGNITO_ENABLED=false`, `ENTRA_ENABLED=false`, `AUTH0_ENABLED=false`,
`PINGFEDERATE_ENABLED=false`, `GITHUB_ENABLED=false`, `GOOGLE_ENABLED=false`),
gives a personal deployment real, non-bypassed authentication without
writing a single line of new auth code — the "smallest explicit
configuration needed to boot/test the profile" the task's auth constraints
call for. No Keycloak/PingFederate/enterprise-IdP *provisioning* occurs
anywhere in this topology.

This is a Slice 1 implementation choice, not a re-opening of the frozen
topology decision in audit §26.1 (Option A vs. Option B). §20.1's embedded
personal-auth seam (Option B) is explicitly deferred to Phase 4/5; Slice 1
uses the unmodified Option A auth-server, only reduced to a single
IdP-independent credential via existing flags.

## Semantic search: explicitly kept as-is (not text-only in this slice)

The audit (§17 criterion 6, §25) asks the profile to "explicitly define"
search behavior, preferring a text-only default *if that can be done
cleanly with existing capabilities*, and otherwise to keep the existing
behavior and document the limitation rather than redesign search.

There is no existing on/off flag that switches `SearchRepositoryBase.search`
between "vector" and "text-only" modes — the hybrid search
(`registry/repositories/documentdb/search_repository.py`) always attempts
embedding-based scoring and fuses it with keyword scoring using Reciprocal
Rank Fusion (`search_fusion_method: rrf`), which — per the field's own
docstring in `config.py` — "avoids score saturation and handles missing
embeddings gracefully." Making the *default* text-only would require either
a new flag threaded through the search route/repository contract (forbidden:
"the search route contract... the `ServerRepositoryBase`/`AgentRepositoryBase`
search method signatures" are explicitly out of scope for Phase 1) or gating
the `sentence-transformers` dependency behind an install-time extra
(explicitly Phase 2, "no broad dependency cleanup" in this slice).

**Slice 1 decision: keep the existing default search behavior unchanged.**
`EMBEDDINGS_PROVIDER=sentence-transformers` and the default model
(`all-MiniLM-L6-v2`) are left as-is in `docker-compose.preve.yml`; the
personal profile does not remove or gate the ML dependencies. This is the
explicitly sanctioned fallback ("keep the existing search behavior and
document the limitation") rather than a redesign. Gating the embeddings
dependency behind an opt-in extra with a text-only default is deferred to
Phase 2 of the migration (`personal-edition-migration-audit.md` §5).

## What is deliberately *not* part of this slice

Per the frozen contract (§17/§27), none of the following are touched:

- No auth rewrite; `auth_server/server.py`, `registry/auth/dependencies.py`,
  and `registry/auth/access_resolver.py` are unmodified.
- No RBAC/scopes deletion; the full scopes engine and `mcp_scopes` collection
  are used unmodified (the personal profile's static token is simply granted
  the existing `mcp-registry-admin` group).
- No audit deletion; the compliance audit subsystem is unmodified (and not
  specifically enabled or disabled by this profile beyond its own defaults).
- No frontend redesign or page deletion; no frontend changes are made in
  this slice at all.
- No repository/storage replacement; `STORAGE_BACKEND=mongodb-ce` uses the
  existing MongoDB-family repositories unmodified.
- No dependency cleanup; the ML/vector search dependencies remain installed
  and enabled (see "Semantic search" above).
- No deletion of Terraform/Helm/CDK/AWS/IdP assets; `terraform/`, `infra/`,
  `charts/`, `keycloak/`, `pingfederate/`, and `setup/` are untouched.
- No edits to route handlers, the nginx `/validate` contract, the nginx
  config templates under `docker/`, or any repository interface signature.

## Future phases this slice deliberately defers

This section exists so nobody mistakes what is implemented for what is
planned. Per the frozen migration order (`personal-edition-migration-audit.md`
§5):

- **Auth simplification (Phase 4/5):** collapsing identity behind the
  `resolve_principal` / `classify_trust` seams and adding an embedded
  personal-auth implementation (Option B) so the auth-server becomes truly
  optional. Not started; Slice 1 uses the unmodified enterprise auth-server
  with only a static-token credential enabled.
- **Dependency cleanup (Phase 2):** gating `sentence-transformers`/`torch`
  behind an opt-in extra with a text-only default, and removing the LLM
  security-scanner dependencies. Not started.
- **Governance/audit reduction (Phase 3/6):** physically removing
  `core/telemetry.py` (only *disabled* in this slice, not removed) and
  adding the lightweight operational-event audit sink. Not started.
- **Enterprise-module removal (Phase 7):** deleting dormant enterprise IdP
  providers, Terraform/CDK/Helm, or other deployment-only assets. Not
  started, and explicitly out of scope until an owner-approved §25 review.
