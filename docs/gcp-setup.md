# GCP setup

One-time setup for what the CI pipeline needs in Google Cloud. This is what was actually done, mostly in the console. Each step also gives the equivalent `gcloud` command, which was **not** run, so check it against the current docs before relying on it. The deploy side (GKE, deploy service account) is not here yet. It will be added with the CD pipeline.

What this creates:

| Piece | Purpose |
|---|---|
| Cloud SQL instance `gke-flyway-db`, database `app` | The database the app connects to |
| IAM database authentication | The CI service account logs in to the database as itself, so there is no database password |
| Service account `github-ci` | The identity CI runs as. It can use the Cloud SQL Auth Proxy and log in to the database. |
| Workload Identity pool `github`, provider `github-oidc` | Lets GitHub Actions log in as `github-ci` with a short-lived OIDC token, with no stored keys |
| 3 GitHub repo variables | Tell the workflow which provider, service account and instance to use |

## 0. Names used below

```bash
export PROJECT_ID=project-3b36b61c-c92a-46b7-994
export REGION=us-east1
export INSTANCE=gke-flyway-db
export GITHUB_REPO=karthikkhaderbad/gke-flyway-cicd
export CI_SA=github-ci@${PROJECT_ID}.iam.gserviceaccount.com
# The database user for a service account is its email without ".gserviceaccount.com".
export CI_DB_USER=github-ci@${PROJECT_ID}.iam

gcloud config set project "$PROJECT_ID"
```

A service account ID must be at least 6 characters, so `gh-ci` was rejected and the account is called `github-ci`.

## 1. Enable the APIs

Console: **APIs & Services → Library**, search for each one and click Enable.

| API | Why |
|---|---|
| Identity and Access Management (IAM) API | Service accounts and role bindings |
| IAM Service Account Credentials API | Lets the GitHub token act as the service account |
| Security Token Service API | Exchanges the GitHub OIDC token for Google credentials |
| Cloud Resource Manager API | Project lookups used by `gcloud` |
| Cloud SQL Admin API | The Auth Proxy and instance management. It is switched on when the first instance is created. |

```bash
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com sqladmin.googleapis.com
```

Secret Manager is **not** needed, because there is no password to store.

## 2. Cloud SQL instance

Console: **Cloud SQL → Create instance → PostgreSQL**, then start from the Sandbox preset and change:

- Edition: **Enterprise**. Version: **PostgreSQL 16**. Region: `us-east1`. Instance ID: `gke-flyway-db`.
- Machine: **Shared core, 1 vCPU, 1.7 GB** (`db-g1-small`). The Sandbox default (2 vCPU, 8 GB) costs about three times as much for no benefit here.
- Storage: **10 GB SSD**, automatic storage increases off.
- Connections: **Public IP**, no authorized networks. Only the Auth Proxy can connect. Private IP is a later hardening step.
- Data protection: automated backups and point-in-time recovery **off**. Availability: **single zone**.
- Set a password for the `postgres` admin user and keep it in a password manager. It is needed once, for the grants in step 5.

```bash
gcloud sql instances create "$INSTANCE" --database-version=POSTGRES_16 --edition=ENTERPRISE \
  --tier=db-g1-small --region="$REGION" --storage-size=10GB --storage-type=SSD \
  --availability-type=ZONAL --no-backup \
  --database-flags=cloudsql.iam_authentication=on
```

The command above also sets the IAM authentication flag from step 3.

## 3. Database and IAM authentication flag

- Console: instance → **Databases → Create database**, name `app`.
- Console: instance → **Edit → Flags**, set `cloudsql.iam_authentication` to `on`. This restarts the instance. Confirm under **Configurations → Flags and parameters**.

```bash
gcloud sql databases create app --instance="$INSTANCE"
gcloud sql instances patch "$INSTANCE" --database-flags=cloudsql.iam_authentication=on
```

## 4. Service account `github-ci`

Console: **IAM & Admin → Service Accounts → Create service account**, name `github-ci`. On the permissions step, add **Cloud SQL Client**. Leave the "principals with access" step empty.

```bash
gcloud iam service-accounts create github-ci --display-name="GitHub Actions CI"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${CI_SA}" --role=roles/cloudsql.client
```

The second role, **Cloud SQL Instance User** (`roles/cloudsql.instanceUser`), is assigned automatically in step 5. Check **IAM & Admin → IAM** afterwards. The `github-ci` row should list exactly these two roles and nothing else. In particular it has no Secret Manager access and no GKE rights.

## 5. IAM database user and read-only grants

Console: instance → **Users → Add user account → Cloud IAM**, principal `github-ci@project-3b36b61c-c92a-46b7-994.iam.gserviceaccount.com`, leave **Database roles empty**, click Add. The console notes that it will assign the instance-level role automatically.

```bash
gcloud sql users create "$CI_DB_USER" --instance="$INSTANCE" --type=cloud_iam_service_account
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${CI_SA}" --role=roles/cloudsql.instanceUser
```

An IAM user has no database privileges until you grant them. In **Cloud SQL Studio**, signed in as `postgres` to database `app`:

```sql
GRANT CONNECT ON DATABASE app TO "github-ci@project-3b36b61c-c92a-46b7-994.iam";
GRANT USAGE ON SCHEMA public TO "github-ci@project-3b36b61c-c92a-46b7-994.iam";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO "github-ci@project-3b36b61c-c92a-46b7-994.iam";

-- Check that it is not a superuser. This must return false.
SELECT pg_has_role('github-ci@project-3b36b61c-c92a-46b7-994.iam', 'cloudsqlsuperuser', 'member');
```

The database has no tables yet, so the last grant covers nothing so far. When Flyway starts creating tables, add `ALTER DEFAULT PRIVILEGES FOR ROLE <migration user> IN SCHEMA public GRANT SELECT ON TABLES TO "<the CI user>";` so new tables are readable by CI.

## 6. Workload Identity Federation (GitHub OIDC)

Console: **IAM & Admin → Workload Identity Federation → Create pool**.

1. **Pool:** name `github`, ID `github`, enabled.
2. **Provider:** type **OpenID Connect (OIDC)**, name and ID `github-oidc`, issuer `https://token.actions.githubusercontent.com`, audience set to **Default audience**.
3. **Attributes:** `google.subject` = `assertion.sub`, `attribute.repository` = `assertion.repository`, `attribute.ref` = `assertion.ref`. Condition: `assertion.repository == 'karthikkhaderbad/gke-flyway-cicd'`. Save.
4. **Grant access** (on the pool page): choose *service account impersonation*, service account `github-ci`, principal attribute `repository` with value `karthikkhaderbad/gke-flyway-cicd`. Save.

```bash
gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github-oidc \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='${GITHUB_REPO}'"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud iam service-accounts add-iam-policy-binding "$CI_SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${GITHUB_REPO}"
```

The condition and the grant both name the repo, and both must match it exactly. Any workflow in this repo, including one on a PR branch, can act as `github-ci`, so keep that account's rights small. Fork PRs cannot get a token. The "direct federated identity" option was not used, because not every service in the pipeline is known to support it.

## 7. GitHub repo variables

Under **Settings → Secrets and variables → Actions → Variables**, add three **repository variables**. They are identifiers, not secrets, and they show up in public run logs.

| Name | Value |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github/providers/github-oidc` |
| `GCP_CI_SERVICE_ACCOUNT` | `github-ci@project-3b36b61c-c92a-46b7-994.iam.gserviceaccount.com` |
| `CLOUDSQL_INSTANCE` | `project-3b36b61c-c92a-46b7-994:us-east1:gke-flyway-db` |

`<PROJECT_NUMBER>` is the project's number, shown on the console home page (not the project ID). The `k8s` job is skipped while `CLOUDSQL_INSTANCE` is unset, so set it last.

## 8. How CI uses all of this

1. `google-github-actions/auth` swaps the run's GitHub token for credentials as `github-ci`.
2. The Cloud SQL Auth Proxy starts on the runner with `--auto-iam-authn` and listens on port 5432. It logs in to Cloud SQL as `github-ci`.
3. The pod in the kind cluster connects to the runner's IPv4 gateway address on port 5432 with a placeholder password, which the proxy ignores. Its `DB_USER` is `github-ci@project-3b36b61c-c92a-46b7-994.iam`.

In production the proxy runs as a sidecar in the pod, with the pod's Workload Identity in place of the runner's credentials. CI does not test that sidecar.

## 9. Check the current state

```bash
# Roles on the CI service account (expect Cloud SQL Client and Cloud SQL Instance User only).
gcloud projects get-iam-policy "$PROJECT_ID" --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${CI_SA}" --format="table(bindings.role)"

gcloud iam workload-identity-pools providers describe github-oidc \
  --location=global --workload-identity-pool=github
gcloud sql users list --instance="$INSTANCE"
gh variable list
```

Then re-run the CI workflow on a PR. All three jobs should pass, including `K8s: deploy chart and check Service and DB`.

## 10. If the `k8s` job fails

| Where it fails | Likely cause |
|---|---|
| Authenticate to GCP | A variable is wrong or missing, the repo name in the provider condition or the grant is wrong, or the `workloadIdentityUser` binding is missing. |
| Start Cloud SQL Auth Proxy | The Cloud SQL Admin API is off, `cloudsql.client` is missing, or `CLOUDSQL_INSTANCE` is wrong. The step prints `proxy.log`. |
| Deploy chart (times out) | The pod never became Ready, so `/readyz` returned 503. Check the diagnostics step. The log line `DB host (runner gateway): ...` must be an IPv4 address (the proxy listens on IPv4 only). Then check `proxy.log` for a login or scope error, and the DB error for a missing user or grant. |
| Check DB connection from inside the pod | The same causes as above, including a missing grant on the database. |

## Cleanup

The instance bills while it exists. Before deleting it, turn off **Prevent instance deletion** (Configurations → Data Protection), and decide whether to keep the final backup that is created on deletion by default. Stopping the instance pauses the compute charge but not the storage, and CI fails while it is stopped. The GKE cluster and deploy account will be added to this guide with CD.
