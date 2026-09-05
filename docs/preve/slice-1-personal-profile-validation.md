# Preve Personal Edition — Slice 1 Validation Report

*Companion to [`profile-behavior.md`](profile-behavior.md) and
[`personal-v1-runbook.md`](personal-v1-runbook.md). Records the evidence
gathered for the Slice 1 acceptance criteria in
[`personal-edition-migration-audit.md`](personal-edition-migration-audit.md) §17/§23.*

## Identification

| Field | Value |
|---|---|
| Base SHA (`main` at session start) | `1802cf1bda3e2d8703d44f8f8238f426321e6f54` |
| Branch | `claude/preve-slice-1-personal-zay08g` |
| Head SHA at validation time | *(see PR — same commit that adds this file)* |

## Changed files

- `registry/core/config.py` — additive `PREVE_PROFILE=personal` preset (`PREVE_PERSONAL_PROFILE_DEFAULTS`, `_apply_preve_personal_profile`); one new `import os`. No existing field, default, signature, or behavior changed when `PREVE_PROFILE` is unset.
- `docker-compose.preve.yml` (new) — standalone personal-profile Compose file (mongodb + registry + auth-server, no AWS/Keycloak/metrics-service/OpenBao).
- `tests/unit/core/test_preve_profile.py` (new) — unit tests for the preset function.
- `docs/preve/profile-behavior.md` (new) — exact list of overridden defaults and rationale.
- `docs/preve/personal-v1-runbook.md` (new) — operational runbook and smoke-test procedure.
- `docs/preve/slice-1-personal-profile-validation.md` (this file, new).

No file under `registry/api/`, `registry/auth/`, `auth_server/`, `docker/` (nginx templates/entrypoints), `registry/repositories/`, `frontend/`, `terraform/`, `infra/`, `charts/`, `keycloak/`, or `pingfederate/` was touched.

## Architecture constraints checked

Each Slice 1 acceptance criterion from the audit §17, and the invariants from §22, checked against the implementation:

| Constraint | Status | Evidence |
|---|---|---|
| Registry-only boots; nginx not required for the default control-plane path | Verified by code | `DEPLOYMENT_MODE=registry-only` set by the preset and in the compose file; `nginx_updates_enabled` gate at `registry/core/config.py:1444-1447` confirmed by reading; see `profile-behavior.md` for the precise, honest scope of what this does and does not eliminate. |
| Peer-to-peer A2A default; advertised `url` is the agent's own backend | Verified by code + existing tests | `A2A_REVERSE_PROXY_ENABLED=false`; `a2a_reverse_proxy_effective` forced false in registry-only mode (`config.py:1449-1460`); `_apply_a2a_reverse_proxy_split` no-op confirmed by reading `registry/api/agent_routes.py:692-727`; `tests/unit/api/test_a2a_reverse_proxy_split.py` and `tests/unit/test_agent_nginx.py` (both pre-existing, unmodified) pass unchanged — see Test results. |
| MongoDB CE, no DocumentDB | Verified by code | `STORAGE_BACKEND=mongodb-ce` (already the upstream default; restated in the preset and compose file). |
| Governance disabled unless explicitly enabled | Verified by code | Rate limiting, quarantine, registration gate, egress auth, federation static-token auth all confirmed `false` by default and restated by the preset/compose file. |
| No AWS dependency for local/personal runtime | Verified by code + config | `AWS_REGISTRY_FEDERATION_ENABLED=false`; `AWS_SHARED_CREDENTIALS_FILE=/dev/null`; no AWS-only service (DocumentDB, Secrets Manager, AgentCore) is reachable or required in the compose topology. |
| Search behavior explicitly defined | Documented (not code-changed) | See `profile-behavior.md` "Semantic search": existing hybrid vector+keyword (RRF) behavior kept as-is per the audit's own fallback instruction; text-only default deferred to Phase 2. |
| Documented Compose/profile startup | Done | `docker compose -f docker-compose.preve.yml up`; see `personal-v1-runbook.md`. |
| No auth rewrite / no RBAC deletion / no audit deletion / no frontend redesign / no repository replacement / no dependency cleanup / no deletion of enterprise assets / no edits to route handlers or the nginx `/validate` contract | Verified by diff | See "Changed files" above; the full diff against `main` touches only the two additive files, one doc-only file set, and one new test file. |
| Personal configuration is reproducible and testable (Invariant 9) | Verified | `docker compose config` validation (below) + `tests/unit/core/test_preve_profile.py` (new, passing). |

## Compose validation

Exact command:

```bash
docker compose -f docker-compose.preve.yml config --quiet
```

Result: **passes** with all five required secrets (`DOCUMENTDB_USERNAME`, `DOCUMENTDB_PASSWORD`, `SECRET_KEY`, `AUTH_SERVER_NGINX_MARKER_SECRET`, `REGISTRY_API_TOKEN`) set.

Fail-closed behavior confirmed: with any one of those five unset, the same command exits non-zero with a message naming the missing variable and pointing at the runbook, e.g.:

```
error while interpolating services.registry.environment.[]: required variable REGISTRY_API_TOKEN
is missing a value: set REGISTRY_API_TOKEN in .env (32+ bytes; see docs/preve/personal-v1-runbook.md)
```

`docker compose -f docker-compose.preve.yml config --services` lists exactly the five intended services: `mongodb-keyfile-init`, `mongodb`, `mongodb-init`, `auth-server`, `registry`. No Keycloak, PingFederate, metrics-service, OpenBao, or AWS-dependent service appears.

## Startup / MCP / A2A live-boot evidence: NOT AVAILABLE IN THIS SESSION (environment limitation)

**This is the one section where required evidence is missing, and the reason is a
sandbox network-policy restriction, not an implementation defect.**

This validation was performed inside a managed remote execution sandbox whose
egress policy blocks the two hosts required to boot the stack:

- `production.cloudfront.docker.com` (Docker Hub's blob-storage CDN — every
  `docker pull`/`docker build` base-image fetch, including the already-cached
  `python:3.14.7-slim` and a plain `alpine:3.21` pull, failed with `403
  Forbidden` at the CONNECT tunnel).
- `fastdl.mongodb.org` (MongoDB's own binary distribution — also `403
  Forbidden`), so a real `mongod` could not be obtained as a fallback outside
  Docker either.

Per this environment's own operating instructions, a `403`/`407` from the
egress proxy is a policy denial to be reported, not routed around — no
mirror, alternate registry, or credential-embedding workaround was
attempted. The Docker **daemon itself runs successfully** in this sandbox
(confirmed via `docker version`/`docker compose version`); only the outbound
image pull is blocked.

**What this means concretely:** the live-boot evidence the audit's §17
acceptance criteria ask for (`docker compose ... up` reaching healthy, a
live `curl` MCP/A2A registration + discovery round trip, a live shutdown)
could not be produced in this session. Everything in this report that does
not depend on pulling a Docker image was still verified directly (compose
config validation above; full code-path tracing with line numbers in
`profile-behavior.md`; the regression test evidence below, which exercises
the exact same `_apply_a2a_reverse_proxy_split`/`a2a_reverse_proxy_effective`
code path the live A2A smoke test would exercise, just via FastAPI's
`TestClient` instead of a real container).

**To complete this evidence**, run the exact commands in
`docs/preve/personal-v1-runbook.md` ("Exact startup command" through
"A2A smoke-test procedure") in an environment with normal Docker Hub
egress (a laptop, an unrestricted CI runner, or any host without this
sandbox's egress policy). No code change is expected to be needed — the
Compose file and preset were both validated as far as this sandbox permits,
and the auth flow, endpoints, and payload shapes were confirmed by reading
the exact route handlers they call (`registry/api/server_routes.py:3900`
`POST /api/servers/register`, `registry/api/agent_routes.py:930`
`POST /api/agents/register`, both `Depends(nginx_proxied_auth)`, both cited
with line numbers in the runbook).

## Regression test results (what could be run in this sandbox)

The project targets Python `>=3.14`. This sandbox's only available Python
3.14 build is `3.14.0rc2` (release candidate; final 3.14 is not yet
released), and that interpreter combination with the pinned `pydantic`
version fails at import time with an unrelated, pre-existing typing
incompatibility (`TypeError: _eval_type() got an unexpected keyword
argument 'prefer_fwd_module'`, inside `pydantic/_internal/_typing_extra.py`,
triggered by the `langsmith` pytest plugin's own Pydantic models). This
reproduces on a clean `git show origin/main` checkout with no Preve changes
applied, so it is an environment/toolchain issue, not something this slice
introduced or should "fix" (out of scope per the task's own instructions).
Tests were therefore run under Python 3.13.12 (the next available
interpreter) with the `langsmith` pytest plugin disabled
(`-p no:langsmith_plugin`); `torch` (blocked at its pinned
`download.pytorch.org` CPU-wheel index — see above) was installed instead
from the default PyPI index, which is unaffected by this sandbox's Docker
Hub/MongoDB restrictions.

Exact commands and results:

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/unit/core/test_preve_profile.py \
  tests/unit/core/test_config.py \
  tests/unit/core/test_config_validation.py \
  -q -p no:langsmith_plugin
# 133 passed in 29.70s
# (the suite's global --cov-fail-under=35 gate fails on this narrow subset,
#  as expected -- it is a full-suite gate, not a per-file one; see below for
#  the coverage-disabled run)
```

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/unit/test_deployment_mode.py \
  tests/unit/api/test_a2a_reverse_proxy_split.py \
  tests/unit/test_agent_nginx.py \
  -q -p no:langsmith_plugin --no-cov -o addopts=""
# 79 passed, 6 warnings in 35.43s
```

These are the exact, pre-existing, unmodified tests that assert the two
Slice 1 safety-critical invariants: A2A stays peer-to-peer by default, and
`DEPLOYMENT_MODE=registry-only` behaves as documented. All 79 pass unchanged.

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/unit/core/ \
  -q -p no:langsmith_plugin --no-cov -o addopts=""
# 770 passed in 172.16s
```

Every unit test under `tests/unit/core/` (the package containing the
touched `config.py`) passes unchanged.

The full `tests/unit/` suite was also run to completion for maximum
assurance:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/unit/ \
  -q -p no:langsmith_plugin --no-cov -o addopts="" -n 4
# 6221 passed, 29 skipped, 14 errors in 414.31s
```

The 14 "errors" were all `ModuleNotFoundError: No module named 'factory'`
collection failures — a gap in this session's own hand-assembled virtual
environment (the `[dependency-groups] dev` extras `factory-boy`,
`hypothesis`, and `faker` were not yet installed in the Python 3.13 venv;
see "Regression test results" above for why this session could not simply
run `uv sync --group dev` under the project's declared Python 3.14).
Installing those three packages and re-running exactly the 14 affected
files resolved every one of them cleanly:

```bash
PYTHONPATH=. .venv/bin/python -m pytest \
  tests/unit/api/test_agent_routes.py tests/unit/api/test_agent_routes_etag.py \
  tests/unit/api/test_agent_routes_patch_batch.py tests/unit/api/test_pull_card_endpoint.py \
  tests/unit/api/test_pull_card_helpers.py tests/unit/api/test_search_routes.py \
  tests/unit/audit/test_mcp_logger.py tests/unit/audit/test_models_properties.py \
  tests/unit/audit/test_routes.py tests/unit/services/test_agent_batch_item_processor.py \
  tests/unit/services/test_agent_service.py tests/unit/test_skill_routes_security.py \
  tests/unit/test_skill_scanner_service.py tests/unit/test_skill_security_schemas.py \
  -q -p no:langsmith_plugin --no-cov -o addopts=""
# 346 passed, 2 skipped in 56.03s
```

**Combined full-suite result: 6567 passed, 31 skipped, 0 failed, 0 errors.**
This includes `tests/unit/api/test_agent_routes.py`, which directly
exercises A2A agent registration through the same route this slice's
invariants depend on.

New test coverage added for this slice:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/unit/core/test_preve_profile.py -v \
  -p no:langsmith_plugin --no-cov -o addopts=""
# 6 tests, all passing: no-op when unset, no-op for an unrelated profile value,
# every documented default applied when personal, case/whitespace-insensitive
# matching, operator-supplied values never overridden, and Settings() built
# after the preset resolves the documented personal defaults.
```

`ruff check` on the two touched/added Python files (`registry/core/config.py`,
`tests/unit/core/test_preve_profile.py`) reports zero new findings; the three
pre-existing `UP042` findings in `config.py` (enum classes inheriting from
both `str` and `Enum`) are unchanged from `origin/main` (confirmed via
`git show origin/main:registry/core/config.py | ruff check -`).
`ruff format --diff` reports both files already formatted.

## Direct interpreter-level verification of the preset

Because `_apply_preve_personal_profile()` runs once at module import time
(not per `Settings()` call), its no-op and setdefault-precedence behavior
was additionally verified by importing `registry.core.config` fresh in a
lightweight interpreter (only `pydantic`/`pydantic-settings` installed, no
project dependencies) under four scenarios:

| `PREVE_PROFILE` | `deployment_mode` | `telemetry_enabled` | Notes |
|---|---|---|---|
| unset | `with-gateway` | `True` | Upstream default, completely untouched. |
| `personal` | `registry-only` | `False` | Every documented default applied. |
| `personal` + operator sets `DEPLOYMENT_MODE=with-gateway` | `with-gateway` | `False` | Operator's explicit value wins; every other default still applies. |
| `  Personal  ` (mixed case, whitespace) | `registry-only` | `False` | Matching is case/whitespace-insensitive. |

## Infrastructure-independence review (static)

- No file in the changed set imports `boto3`, references AWS DocumentDB,
  ECS, Terraform, CDK, or Helm.
- `docker-compose.preve.yml` never sets AWS credentials, and explicitly
  mounts `/dev/null` in place of a credentials file (`AWS_SHARED_CREDENTIALS_FILE=/dev/null`).
- `AWS_REGISTRY_FEDERATION_ENABLED=false` in both the config preset and the
  compose file.
- No Keycloak, PingFederate, or any other enterprise IdP container appears
  in the compose topology; every IdP `*_ENABLED` flag is explicitly `false`.

A live network-egress check (confirming zero outbound calls to
`*.amazonaws.com` during a real boot) is part of the live-boot evidence
that could not be produced in this sandbox (see above); the static review
above is the strongest available substitute.

## Known limitations

See `docs/preve/personal-v1-runbook.md` "Known limitations" for the
in-product limitations (Registry API auth still goes through nginx +
auth-server; direct port 7860 doesn't accept the bearer token; semantic
search unchanged; auth-server remains required in the default topology).

In addition, specific to this validation session:

1. **Live Docker Compose boot could not be executed** (Docker Hub and
   MongoDB binary downloads both blocked by sandbox egress policy). The
   compose file's syntax, secret-requirement fail-closed behavior, and
   service topology were verified statically; the actual healthy-startup,
   MCP/A2A HTTP round trip, and clean-shutdown evidence must be gathered in
   an unrestricted environment using the exact runbook commands.
2. Full-suite regression (`pytest tests/unit/`) was run under Python 3.13
   rather than the project's declared `>=3.14`, because the only 3.14 build
   available in this sandbox (`3.14.0rc2`) hits a pre-existing
   pydantic/typing incompatibility unrelated to this change (reproduces on
   an unmodified `origin/main` checkout).

## Rollback procedure

This slice is purely additive:

```bash
git revert <this-PR's-merge-commit>
```

removes `docker-compose.preve.yml`, the three `docs/preve/*.md` files added
here, `tests/unit/core/test_preve_profile.py`, and the `PREVE_PROFILE` block
in `registry/core/config.py`. No other file changes, no data migrations, and
no default behavior change when `PREVE_PROFILE` is unset — reverting has zero
effect on any existing (enterprise) deployment.

## Acceptance verdict

**NOT ACCEPTED — pending live-boot verification outside this sandbox.**

Rationale: every acceptance criterion that can be verified through static
code review, config validation, and the existing/added automated test suite
passes cleanly, with no regressions and no scope violations (see the
constraints table above). However, the audit's §17 acceptance criteria
explicitly require live evidence — a real `docker compose up` reaching
healthy, and a live MCP/A2A registration + discovery HTTP round trip — which
this session's sandboxed network policy makes impossible to produce (Docker
Hub and MongoDB direct-download are both blocked; see above). Per this
task's own instruction ("Do not declare PASS if evidence is missing"), this
report records **NOT ACCEPTED** rather than asserting a live-boot result
that was not actually observed.

**Recommended next step:** run the exact commands in
`docs/preve/personal-v1-runbook.md` in an environment with normal Docker Hub
egress, capture the boot log and the MCP/A2A curl output, and append that
evidence to this report (or file it as a follow-up validation note) before
treating Slice 1 as fully accepted. No code change is anticipated to be
necessary based on the static verification performed here.
