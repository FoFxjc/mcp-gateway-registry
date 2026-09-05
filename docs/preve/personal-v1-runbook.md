# Preve Personal V1 — Runbook (Slice 1)

*Companion to [`profile-behavior.md`](profile-behavior.md) (what changes and
why) and [`personal-edition-migration-audit.md`](personal-edition-migration-audit.md)
(the frozen architecture baseline). This runbook covers only the Slice 1
scope: proving the personal profile boots and passes an MCP + A2A smoke test
using exclusively existing capabilities.*

## Prerequisites

- Docker Engine and Docker Compose v2 (the same requirement as the existing
  `docker-compose.yml`).
- No AWS credentials, no AWS region, no ECS/DocumentDB/Terraform/CDK/Helm,
  no Keycloak/PingFederate. None of these are required to boot this profile.
- `openssl` (or any other way to generate random secrets) available on the
  host, to generate the required secret values below.

## Required environment variables

Create a `.env` file next to `docker-compose.preve.yml` (or export these in
your shell) with the following. There are **no working defaults** for these
— the compose file fails closed with a clear message if any is missing,
per this repository's security guidelines (never ship a default for a
required secret):

| Variable | Purpose | How to generate |
|---|---|---|
| `DOCUMENTDB_USERNAME` | MongoDB root username | any value, e.g. `preveadmin` |
| `DOCUMENTDB_PASSWORD` | MongoDB root password | `python3 -c "import secrets; print(secrets.token_urlsafe(24))"` |
| `SECRET_KEY` | Signs internal JWTs/sessions; shared by registry + auth-server | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `AUTH_SERVER_NGINX_MARKER_SECRET` | Proves a `/validate` call actually came through nginx | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `REGISTRY_API_TOKEN` | The personal owner's static bearer credential (IdP-independent; see [`docs/registry-api-auth.md`](../registry-api-auth.md)) | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |

Example:

```bash
cat > .env <<EOF
DOCUMENTDB_USERNAME=preveadmin
DOCUMENTDB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
AUTH_SERVER_NGINX_MARKER_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
REGISTRY_API_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
EOF
```

## Optional environment variables

All have working defaults; override only if needed.

| Variable | Default | Notes |
|---|---|---|
| `REGISTRY_URL` | `http://localhost:8080` | External URL the registry advertises about itself. |
| `REGISTRY_NAME` / `REGISTRY_ORGANIZATION_NAME` | `Preve Personal Registry` / `Personal` | Cosmetic, shown in the UI and agent-card metadata. |
| `REGISTRY_MODE` | `full` | Set to `agents-only`, `mcp-servers-only`, or `skills-only` to narrow the inventory. |
| `PREVE_HTTP_PORT` | `8080` | Host port mapped to the registry container's nginx (HTTP). |
| `PREVE_DIRECT_PORT` | `7860` | Host port mapped directly to uvicorn, bypassing nginx (health checks only — see "Known limitations"). |
| `HOST_BIND_IP` | `127.0.0.1` | Bind address for the loopback-only MongoDB (27017) and auth-server (8888) debug ports. |
| `REGISTRY_STATIC_TOKEN_AUTH_ENABLED` | `true` | The personal profile's auth mechanism; set `false` only if you plan to authenticate purely via browser session cookie instead. |
| `EMBEDDINGS_PROVIDER` / `EMBEDDINGS_MODEL_NAME` | `sentence-transformers` / `all-MiniLM-L6-v2` | Search behavior is intentionally unchanged in Slice 1 — see [`profile-behavior.md`](profile-behavior.md) "Semantic search". |

## Exact startup command

```bash
docker compose -f docker-compose.preve.yml up -d
```

This starts, in order: `mongodb-keyfile-init` (one-shot) → `mongodb` →
`mongodb-init` (one-shot: replica set, indexes, seeds the `mcp-registry-admin`
scope) → `auth-server` → `registry`.

## Exact shutdown command

```bash
docker compose -f docker-compose.preve.yml down
```

Add `-v` to also remove the named volumes (`preve-mongodb-data`,
`preve-mongodb-config`, `preve-mongodb-keyfile`, `preve-registry-logs`,
`preve-registry-scans`, `preve-auth-logs`) and start completely fresh next
time.

## Exposed ports

| Port (host) | Service | Purpose |
|---|---|---|
| `8080` | registry (nginx) | Registry API front door (`/api/*`, `/v0.1/*`); use with `Authorization: Bearer $REGISTRY_API_TOKEN`. |
| `7860` | registry (uvicorn, direct) | Health check only (`/health`); bypasses nginx and does **not** accept the static bearer token (see "Known limitations"). |
| `8888` (loopback only) | auth-server | Host-side health checks / debugging only; normal traffic reaches it through nginx. |
| `27017` (loopback only) | mongodb | `mongosh` / debugging access only. |

## Expected healthy state

```bash
docker compose -f docker-compose.preve.yml ps
```

`mongodb`, `auth-server`, and `registry` should show `healthy` (the
`mongodb-keyfile-init` and `mongodb-init` one-shot jobs show `Exited (0)`,
which is their success state). Confirm the registry directly:

```bash
curl -sf http://localhost:7860/health
```

A 200 response with a JSON health payload indicates the control plane is up.

## MCP smoke-test procedure

1. Register a sample MCP server via the JWT/bearer-authenticated API
   (`POST /api/servers/register`, `registry/api/server_routes.py:3900`,
   `Depends(nginx_proxied_auth)` — form-encoded, matching the endpoint's
   existing contract):

   ```bash
   TOKEN="$REGISTRY_API_TOKEN"
   curl -sS -X POST "http://localhost:8080/api/servers/register" \
     -H "Authorization: Bearer $TOKEN" \
     -F "name=preve-smoke-test-server" \
     -F "description=Preve Slice 1 MCP smoke test" \
     -F "path=/preve-smoke-test" \
     -F "proxy_pass_url=http://example-mcp-server:8000"
   ```

   (Any reachable-looking URL works for this check — the point is proving
   the registration API accepts and stores protocol/endpoint metadata, not
   exercising a live tool call against a real server.)

2. Confirm it appears in discovery/listing with its endpoint metadata intact
   (`GET /api/servers`, `registry/api/server_routes.py:736`):

   ```bash
   curl -sS "http://localhost:8080/api/servers?query=preve-smoke-test" \
     -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
   ```

   Confirm the returned entry echoes `proxy_pass_url` and `path` unchanged
   from what was registered.

3. Optionally, retrieve it directly by path
   (`GET /api/servers/{path:path}`, `registry/api/server_routes.py:6485`):

   ```bash
   curl -sS "http://localhost:8080/api/servers/preve-smoke-test" \
     -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
   ```

## A2A smoke-test procedure

1. Register a sample A2A agent with a direct backend URL:

   ```bash
   curl -sS -X POST "http://localhost:8080/api/agents/register" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
           "name": "preve-smoke-test-agent",
           "path": "/preve-smoke-test-agent",
           "url": "http://my-agent-host:9000",
           "supported_protocol": "a2a",
           "description": "Preve Slice 1 A2A smoke test",
           "skills": []
         }'
   ```

2. Retrieve it back and confirm the **critical invariant**: the advertised
   `url` is still the agent's own direct backend (`http://my-agent-host:9000`),
   **not** rewritten to the gateway:

   ```bash
   curl -sS "http://localhost:8080/api/agents/preve-smoke-test-agent" \
     -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
   ```

   Expect `"url": "http://my-agent-host:9000"` and no gateway rewrite. This
   is true by construction: `A2A_REVERSE_PROXY_ENABLED=false` and
   `DEPLOYMENT_MODE=registry-only` together force
   `a2a_reverse_proxy_effective` to `false`
   (`registry/core/config.py:1449-1460`), so
   `_apply_a2a_reverse_proxy_split` (`registry/api/agent_routes.py:692`)
   is a no-op on every register/update call.

3. Confirm it appears in discovery/listing:

   ```bash
   curl -sS "http://localhost:8080/api/agents" \
     -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
   ```

## Infrastructure-independence check

With the stack running, confirm no AWS calls occur:

```bash
docker compose -f docker-compose.preve.yml logs registry auth-server | grep -i "amazonaws\|botocore\|boto3" || echo "no AWS references found"
```

The personal profile also never mounts AWS credentials
(`AWS_SHARED_CREDENTIALS_FILE=/dev/null` in `docker-compose.preve.yml`), so
even attempted AWS SDK calls would fail authentication rather than
succeed against a real account.

## Known limitations

- **The Registry API's control-plane calls still go through the bundled
  nginx + auth-server `/validate` hop.** This is existing, unmodified
  upstream behavior (see [`profile-behavior.md`](profile-behavior.md)
  "What `DEPLOYMENT_MODE=registry-only` does and does not eliminate"), not
  something introduced by this slice. `DEPLOYMENT_MODE=registry-only`
  removes nginx as a mandatory relay for *MCP tool-call / A2A traffic*, and
  removes the requirement for a *separate* gateway container — it does not
  remove nginx from the Registry API's own auth path. Rewriting that path
  is explicitly out of scope for Slice 1 (no auth rewrite).
- **The direct port (`7860`) does not accept the static bearer token.**
  `nginx_proxied_auth`'s no-token fallback only accepts a session cookie
  (`registry/auth/dependencies.py:844-864`). Use port `8080` (through nginx)
  for all bearer-token API calls; port `7860` is for health checks only.
- **Semantic search keeps its existing default behavior** (vector +
  keyword hybrid via RRF fusion); it is not made text-only in this slice.
  See [`profile-behavior.md`](profile-behavior.md) "Semantic search" for
  why, and for the deferred Phase 2 plan.
- **The auth-server remains a required process** in the default Slice 1
  topology (Option A). Embedding personal auth directly into the registry
  (Option B, audit §21) is deferred to Phase 4/5.
- **This is a Slice 1 profile, not a hardened personal-deployment guide.**
  `REGISTRY_API_TOKEN` grants full `mcp-registry-admin` access — protect it
  like a password (see [`docs/registry-api-auth.md`](../registry-api-auth.md)
  "Threat model for static tokens").
