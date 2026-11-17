# Progress log

What has been built so far, in the order it happened. For the original design see [PLAN.md](PLAN.md). For the commands that recreate the GCP side see [gcp-setup.md](gcp-setup.md).

## 1. Repo and app (Oct 28)

- Repo created, plan committed to `docs/PLAN.md`.
- **PR #1**: FastAPI demo app with `/`, `/healthz` (liveness), `/readyz` (readiness, needs the DB) and `/db/health`. Unit tests with the DB mocked. A two-stage Alpine `Dockerfile` (the builder is Alpine too so packages are built for musl), run as a non-root user. First CI workflow: `ruff` and `pytest` on the PR merge commit.

## 2. CI hardening (Nov 6)

- **PR #2**: CI failed with `ModuleNotFoundError: No module named 'app'` because pytest did not put the repo root on the import path. Fixed with `pythonpath = ["."]` in `pyproject.toml`. This was the first run of CI, and the first bug it caught.
- **PR #3**: `docker` job. Builds the image, starts the container and polls `/healthz`. Starting uvicorn imports `psycopg`, so it also catches musl and wheel problems.
- **PR #4**: Helm chart (`charts/app`): a Deployment (liveness `/healthz`, readiness `/readyz`, non-root, read-only root filesystem, `DB_*` env vars) and a ClusterIP Service, with `values.yaml` and `values-ci.yaml`. The `unit` job now also runs `helm lint`, `helm template` and `kubeconform` against the Kubernetes 1.31 schema.

## 3. Google Cloud setup (Nov 6 to Nov 17)

Done by hand in the console.

1. **Free trial billing.** The billing account was shown as closed until the trial prepayment cleared. After that the trial was active (credit expires Jan 27, 2027).
2. **Cloud SQL instance `gke-flyway-db`.** PostgreSQL 16, Enterprise edition, `us-east1`, `db-g1-small` (1 vCPU, 1.7 GB), 10 GB SSD, single zone, public IP with no authorized networks. The Sandbox preset was changed from 2 vCPU and 8 GB to cut the cost from about $0.14 to about $0.04 an hour.
3. **Database `app`** created on the instance.
4. **APIs enabled** (project: `project-3b36b61c-c92a-46b7-994`). Needed for this pipeline:

   | API | Why |
   |---|---|
   | Identity and Access Management (IAM) API | Service accounts and role bindings |
   | IAM Service Account Credentials API | Lets GitHub's token act as the service account |
   | Security Token Service API | Exchanges the GitHub OIDC token for Google credentials |
   | Cloud Resource Manager API | Project lookups used by `gcloud` |
   | Cloud SQL Admin API | The Auth Proxy and instance management. Switched on when the instance was created |
   | Service Networking API | Present on the project, not used yet. Relevant if the instance moves to private IP |

   The other APIs in the list (BigQuery, Storage, Logging, Monitoring and so on) are project defaults and are not used. **Secret Manager API is not enabled.** It is only needed if the DB password route is used, and the plan is now IAM authentication.
5. **Service account `github-ci`.** The console rejected the ID `gh-ci` because service account IDs need at least 6 characters. Current roles on the project: **Cloud SQL Client** and **Secret Manager Secret Accessor**. The second one was granted at project level and is broader than needed. It will be removed once the switch to IAM authentication removes the secret.
6. **Workload Identity Federation (GitHub OIDC).**
   - Pool `github`.
   - Provider `github-oidc` (OIDC), issuer `https://token.actions.githubusercontent.com`, default audience.
   - Attribute mapping: `google.subject` = `assertion.sub`, `attribute.repository` = `assertion.repository`, `attribute.ref` = `assertion.ref`.
   - Attribute condition: `assertion.repository == 'karthikkhaderbad/gke-flyway-cicd'`, so tokens from any other repo are rejected.
   - **Grant access** on the pool, using service account impersonation: service account `github-ci`, principal attribute `repository` = `karthikkhaderbad/gke-flyway-cicd`. The direct "federated identities" option was not used, because not every service in the pipeline is known to support it.
7. **GitHub repository variables** (identifiers, not secrets): `CLOUDSQL_INSTANCE` (the instance connection name), `GCP_WORKLOAD_IDENTITY_PROVIDER` (the provider's resource path) and `GCP_CI_SERVICE_ACCOUNT` (the service account email).

## 4. Kubernetes stage in CI (Nov 15 to now, PR #5, open)

- New `k8s` job after `docker`. It builds the image, logs in to GCP with OIDC, starts the Cloud SQL Auth Proxy on the runner, creates a kind cluster, deploys the chart with `--wait` (readiness needs the DB, so this also proves DB connectivity), checks the pods are Ready with no restarts, then uses `kubectl exec` to resolve and connect to the Service by its DNS name and to connect to the DB from inside the pod. Diagnostics are dumped on failure.
- The job is skipped until the `CLOUDSQL_INSTANCE` variable exists, and on fork PRs, which cannot get an OIDC token or the variables.
- Bug found and fixed: the chart's name helper makes the Deployment and Service `app`, not `app-app`, so the workflow's `kubectl` commands used a wrong name.
- **Result so far:** the GCP authentication step passes. The next step, fetching the DB password from Secret Manager, fails because that secret does not exist. This was expected, and it led to the decision below.

## 5. Decisions made along the way

| Decision | Why |
|---|---|
| Cloud SQL for the database, not a Postgres pod | It is what the original brief asked for, it is managed, and it teaches IAM, Workload Identity and the Auth Proxy. |
| kind on the runner for the Kubernetes test | Matches the plan, no GKE cost per PR. |
| Probes as the main health test | `/readyz` already needs the DB, so `helm --wait` proves the pod and the DB connection. Liveness stays DB-free so a DB outage does not restart healthy pods. |
| Service check by DNS name and TCP, not `ping` | A Service IP does not answer ICMP, and the pod has no `NET_RAW`. |
| Keyless OIDC, no JSON keys | No long-lived credentials in GitHub. |
| Public IP for now | The runner is outside the VPC. Private IP with an IAP bastion is a later hardening step. |
| PostgreSQL 16 | Mature, supported until late 2028, no tooling surprises. |
| IAM database authentication for CI | Removes the DB password and the Secret Manager step, and avoids the over-privileged built-in user. |

## 6. Where things stand

- Done: app, Docker image, Helm chart, CI up to the GCP login, GCP project setup for CI.
- In progress: switching the CI database login to IAM authentication (instance flag, IAM database user for `github-ci`, Cloud SQL Instance User role, read-only grants, then removing the secret step and adding `--auto-iam-authn` to the proxy).
- Next: `cd.yml` (push to GHCR as `v<run_number>`, Helm deploy to GKE with a separate deploy service account limited to `main`), the Flyway migration job, the README.
- Known cleanup: remove the project-level Secret Manager Secret Accessor role from `github-ci`, and bring `docs/gcp-setup.md` in line with the console steps and IAM authentication.
