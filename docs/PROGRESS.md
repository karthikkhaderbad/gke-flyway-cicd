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
2. **Cloud SQL instance `gke-flyway-db`.** PostgreSQL 16, Enterprise edition, `us-east1`, `db-g1-small` (1 vCPU, 1.7 GB), 10 GB SSD, single zone, public IP with no authorized networks. The Sandbox preset was changed from 2 vCPU and 8 GB to cut the cost from about $0.14 to about $0.04 an hour. Automated backups are disabled. **Prevent instance deletion is on** (the default), so turn it off before deleting the instance. "Final backup on instance deletion" is also on by default and keeps a backup for 30 days.
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
5. **Service account `github-ci`.** The console rejected the ID `gh-ci` because service account IDs need at least 6 characters. Roles on the project: **Cloud SQL Client** (lets the Auth Proxy connect) and **Cloud SQL Instance User** (lets it log in as a database user). Secret Manager Secret Accessor was granted at project level at first, which was broader than needed, and was removed when the plan moved to IAM database authentication.
6. **Workload Identity Federation (GitHub OIDC).**
   - Pool `github`.
   - Provider `github-oidc` (OIDC), issuer `https://token.actions.githubusercontent.com`, default audience.
   - Attribute mapping: `google.subject` = `assertion.sub`, `attribute.repository` = `assertion.repository`, `attribute.ref` = `assertion.ref`.
   - Attribute condition: `assertion.repository == 'karthikkhaderbad/gke-flyway-cicd'`, so tokens from any other repo are rejected.
   - **Grant access** on the pool, using service account impersonation: service account `github-ci`, principal attribute `repository` = `karthikkhaderbad/gke-flyway-cicd`. The direct "federated identities" option was not used, because not every service in the pipeline is known to support it.
7. **GitHub repository variables** (identifiers, not secrets): `CLOUDSQL_INSTANCE` (the instance connection name), `GCP_WORKLOAD_IDENTITY_PROVIDER` (the provider's resource path) and `GCP_CI_SERVICE_ACCOUNT` (the service account email).
8. **IAM database authentication (Nov 17).** The instance flag `cloudsql.iam_authentication` is **on**. The CI service account was added as a database user (Users, Add user account, Cloud IAM, principal `github-ci@project-3b36b61c-c92a-46b7-994.iam.gserviceaccount.com`, Database roles left empty). The console assigned the Cloud SQL Instance User role to it automatically. The database user name is the email without `.gserviceaccount.com`: `github-ci@project-3b36b61c-c92a-46b7-994.iam`. An IAM user has no database privileges by default, and it is not a member of `cloudsqlsuperuser`.
9. **Read-only grants.** In Cloud SQL Studio, signed in as `postgres` on database `app`:

   ```sql
   GRANT CONNECT ON DATABASE app TO "github-ci@project-3b36b61c-c92a-46b7-994.iam";
   GRANT USAGE ON SCHEMA public TO "github-ci@project-3b36b61c-c92a-46b7-994.iam";
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO "github-ci@project-3b36b61c-c92a-46b7-994.iam";
   ```

   All three ran successfully. The database has no tables yet, so the last grant covers nothing so far. Tables created later (by Flyway) will need a default-privileges grant for the CI user.
10. **Check that the user is not a superuser:** `SELECT pg_has_role('github-ci@project-3b36b61c-c92a-46b7-994.iam', 'cloudsqlsuperuser', 'member');` returned `false`.

## 4. Kubernetes stage in CI (Nov 15 to Nov 17, PR #5, merged)

- New `k8s` job after `docker`. It builds the image, logs in to GCP with OIDC, starts the Cloud SQL Auth Proxy on the runner, creates a kind cluster, deploys the chart with `--wait` (readiness needs the DB, so this also proves DB connectivity), checks the pods are Ready with no restarts, then uses `kubectl exec` to resolve and connect to the Service by its DNS name and to connect to the DB from inside the pod. Diagnostics are dumped on failure.
- The job is skipped until the `CLOUDSQL_INSTANCE` variable exists, and on fork PRs, which cannot get an OIDC token or the variables.
- Bug found and fixed: the chart's name helper makes the Deployment and Service `app`, not `app-app`, so the workflow's `kubectl` commands used a wrong name.
- The first run passed the GCP authentication step and failed at the next one, fetching the DB password from Secret Manager, because that secret did not exist. That led to switching to IAM database authentication: the "Fetch read-only DB password" step was removed, the proxy starts with `--auto-iam-authn`, and `values-ci.yaml` uses the IAM database user with a placeholder password the proxy ignores.
- Added a check that the Service's EndpointSlice addresses are exactly the Deployment's pod IPs, which catches a wrong or too-broad selector in `service.yaml`.
- **Second bug found by CI:** the pod stayed unready and `/readyz` returned 503 until the deploy timed out. The cause was visible in `kubectl describe`: `DB_HOST` was an IPv6 address. The kind Docker network has both IPv6 and IPv4 subnets, the workflow took the first gateway (IPv6), and the proxy listens on IPv4 only. The deploy step now picks the IPv4 gateway and prints it.
- **Result:** the whole `k8s` job passes. The GCP login, the proxy, the kind cluster, the chart deploy (the pod reaches Cloud SQL as the `github-ci` IAM user), the pod health and endpoint checks, the Service DNS and port check, and the DB check from inside the pod all succeed.

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
| Auth Proxy on the runner in CI, not a sidecar | kind has no Workload Identity, so a sidecar would need credentials handed in another way. In prod the proxy runs as a sidecar in the pod. CI therefore does not test the sidecar. |

## 6. Where things stand

- **CI is complete for phase 1:** lint and unit tests, chart validation, the Docker build and start check, and the kind deploy that tests the Service and the database. All of it passes on the PR merge commit.
- **Next: CD.** `cd.yml` pushes the image to GHCR as `v<run_number>` and runs the Helm deploy to GKE. It needs a GKE cluster, a separate `github-deploy` service account limited to `main`, an `app` service account and database user for the Auth Proxy sidecar (using Workload Identity), a `production` GitHub environment, and the GHCR package set to public. The chart needs the optional proxy sidecar and a `values-prod.yaml`.
- After that: the Flyway migration job, and the README.

### Known issues and to-do

- **The image is built several times:** in the `docker` job, again in the `k8s` job, and again in CD. Options are building once and passing the image between jobs as an artifact, or merging the jobs.
- **`/readyz` hides the reason for a failure.** `_check_db` in `app/main.py` swallows the exception, so a 503 has no cause in the pod logs. Logging the exception would have shown the IPv6 problem at once.
- **CI does not test the proxy sidecar** (see the decisions table). It gets its first real test on GKE.
- **The Auth Proxy version `v2.14.1` is pinned in the workflow and the setup doc.** It worked in CI, so it is a known-good version, but check for newer releases from time to time.
- **Cleanup at the end of the project:** turn off "Prevent instance deletion" on the Cloud SQL instance before deleting it, and decide whether to keep the final backup.
