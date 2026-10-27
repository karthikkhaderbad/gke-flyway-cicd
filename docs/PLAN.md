# Plan: GitOps CI/CD for a DB-backed app on GKE (phase 1: k8s; phase 2: Flyway)

## Context
Portfolio project (public GitHub repo `karthikkhaderbad/gke-flyway-cicd`, currently empty) derived from a system-design interview prompt: an end-to-end, GitOps-style pipeline deploying an app on GKE that talks to Cloud SQL. The DevOps pieces (CI, CD, Helm, k8s) are the showcase; the app is a deliberately tiny demo that I write and maintain. Phase 1 covers CI on PR + CD on push to main + Helm chart. Flyway migrations are phase 2 and are only planned for (a hook point), not built.

Decisions: Python 3.12 + FastAPI, Postgres (Cloud SQL for Postgres in prod, throwaway in-cluster Postgres in CI), Workload Identity Federation (keyless) for GitHub -> GCP, image on GHCR.

## Repo layout
```
app/
  main.py            # FastAPI: /, /healthz (liveness), /readyz (readiness, checks DB), /db/health (SELECT 1 + server version)
  db.py              # psycopg connection from env (DB_HOST/PORT/NAME/USER/PASSWORD)
  requirements.txt / requirements-dev.txt
tests/unit/          # pytest, DB mocked; TestClient for endpoints
Dockerfile           # multi-stage, python:3.12-slim, non-root, uvicorn
pyproject.toml       # ruff + pytest config
charts/app/
  Chart.yaml, values.yaml, values-ci.yaml, values-prod.yaml
  templates/_helpers.tpl, deployment.yaml, service.yaml, serviceaccount.yaml, secret(optional)
ci/
  postgres.yaml      # throwaway Postgres Deployment+Service for kind
  smoke.sh           # port-forward + curl assertions
.github/workflows/ci.yml, cd.yml
docs/gcp-setup.md    # one-time: GKE, Cloud SQL, WIF pool/provider, SAs, IAM
Makefile             # lint, test, build, kind-up, smoke (same commands CI runs)
README.md            # architecture diagram, pipeline description, badges
```

## CI (`ci.yml`, on `pull_request` -> main)
`actions/checkout` on `pull_request` already checks out the **merge commit of PR + main**, which satisfies "merge with main, then test".
1. **unit job**: setup-python (pip cache) -> `ruff check` + `ruff format --check` -> `pytest tests/unit` (with coverage). Also `helm lint charts/app` + `helm template` render check.
2. **integration job** (`needs: unit`): docker buildx build (`load: true`, tag `pr-<sha>`, GHA layer cache) -> `helm/kind-action` creates cluster -> `kind load docker-image` -> apply `ci/postgres.yaml`, wait ready -> `helm upgrade --install app charts/app -f values-ci.yaml --set image.tag=pr-<sha> --wait` -> `kubectl rollout status` + assert all pods Ready -> `ci/smoke.sh`: port-forward svc, assert 200 on `/`, `/healthz`, `/readyz`, and `/db/health` (proves real DB connectivity, returns PG version). On failure: dump `kubectl describe`/logs as step output.
Concurrency group cancels superseded runs; permissions `contents: read`.

## CD (`cd.yml`, on `push` -> main)
Tag = `v${{ github.run_number }}`.
1. **build-push**: login to GHCR with `GITHUB_TOKEN` (`packages: write`), buildx build+push `ghcr.io/<owner>/<repo>:v<run_number>` (+ `latest`). Package set to public so GKE pulls without a pull secret.
2. **deploy** (`needs: build-push`, GitHub `production` environment): `google-github-actions/auth` via WIF (`id-token: write`) -> `get-gke-credentials` -> `helm upgrade --install app charts/app -f values-prod.yaml --set image.tag=v<run_number> --atomic --timeout 5m --namespace app --create-namespace` -> post-deploy `kubectl rollout status` + smoke check against the Service.
Cloud SQL access: Cloud SQL Auth Proxy sidecar (toggled by `cloudSqlProxy.enabled` in values-prod), authenticated via GKE Workload Identity on the pod's ServiceAccount; DB password from a k8s Secret created out-of-band (documented, not committed).

## Helm chart
Deployment (probes: liveness `/healthz`, readiness `/readyz`; resources; securityContext non-root/readOnlyRootFilesystem; env from values/secret; optional proxy sidecar), Service (ClusterIP, 80 -> 8000), ServiceAccount (with Workload Identity annotation in prod). Values separated per environment. Chart left with room for a Flyway pre-install/pre-upgrade Job hook in phase 2 (`templates/flyway-job.yaml`, `migrations/` dir).

## One-time manual prerequisites (documented in `docs/gcp-setup.md`, not automated in phase 1)
GKE cluster (Workload Identity on), Cloud SQL Postgres instance + db/user, GCP SA for deploy (`container.developer`) bound to a WIF pool restricted to this repo, SA for the app with `cloudsql.client`, GitHub repo variables (project, region, cluster, WIF provider, SA) and `production` environment.

## Build order
1. App + unit tests + Dockerfile + pyproject/Makefile
2. Helm chart + `values-*.yaml`
3. `ci/` assets + `ci.yml`
4. `cd.yml` + `docs/gcp-setup.md`
5. README (diagram, badges), initial commit on a branch, open a PR so CI runs against itself

## Verification
- Local: `make lint test`; `docker build`; `kind create cluster` + `make smoke` reproduces the integration job (needs docker, kind, helm installed locally; currently none are installed, so CI is the primary verification, and I'd flag install needs before local runs).
- `helm lint` and `helm template` for both values files.
- Open a PR: confirm unit + integration jobs go green on GitHub, including the 200 checks and `/db/health`.
- After GCP setup and merge to main: confirm image `v<run_number>` appears in GHCR, `helm list -n app` shows the new revision, and the Service responds with DB connectivity through Cloud SQL.
- Negative test: a failing unit test and a bad probe path each turn the PR red / roll back via `--atomic`.

## Out of scope (phase 2+)
Flyway migrations job, Terraform for GCP infra, Argo CD/Flux pull-based GitOps, ingress/TLS.
