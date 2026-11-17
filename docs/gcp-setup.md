# GCP setup

One-time setup for the pieces the pipeline needs in Google Cloud. Run the commands on your own machine with `gcloud` logged in (`gcloud auth login`). Nothing here is automated yet.

What gets created:

| Piece | Purpose |
|---|---|
| Database `app`, users `app` and `ci_ro` | `app` is read-write (prod app, later Flyway). `ci_ro` is read-only, for CI. |
| Secrets `db-app-password`, `db-ci-ro-password` | Passwords live in Secret Manager, never in GitHub or git. |
| Service account `gh-ci` | The identity CI runs as. Can use the Cloud SQL proxy and read one secret. |
| Workload Identity pool and provider | Lets GitHub Actions log in as `gh-ci` with a short-lived OIDC token. No keys. |
| 3 GitHub repo variables | Tell the workflow which provider, service account and instance to use. |

The Cloud SQL instance itself is created in the console (PostgreSQL 16, Enterprise, `db-g1-small`, public IP with no authorized networks, backups off).

## 0. Variables used below

```bash
export PROJECT_ID=project-3b36b61c-c92a-46b7-994
export REGION=us-east1
export INSTANCE=gke-flyway-db
export GITHUB_REPO=karthikkhaderbad/gke-flyway-cicd
export CI_SA=gh-ci@${PROJECT_ID}.iam.gserviceaccount.com

gcloud config set project "$PROJECT_ID"
```

Check these against the console (project ID, region and instance ID) before going on.

Enable the APIs:

```bash
gcloud services enable \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  cloudresourcemanager.googleapis.com
```

## 1. Database, users and secrets

```bash
gcloud sql databases create app --instance="$INSTANCE"

# Generate the passwords, create the users and store the passwords in Secret Manager.
for user in app ci_ro; do
  pw=$(openssl rand -base64 24 | tr -d '/+=')
  gcloud sql users create "$user" --instance="$INSTANCE" --password="$pw"
  secret="db-$(echo "$user" | tr '_' '-')-password"
  printf '%s' "$pw" | gcloud secrets create "$secret" --data-file=-
done
unset pw
```

This creates the secrets `db-app-password` and `db-ci-ro-password`. The CI workflow reads `db-ci-ro-password` by that exact name.

## 2. Grants

The users exist but have no rights yet. Connect as the `postgres` admin (the password you set in the console) through the Auth Proxy from your machine.

```bash
# Once: install the proxy and the psql client, and give the proxy your credentials.
sudo apt install postgresql-client
curl -fsSL -o cloud-sql-proxy \
  https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.14.1/cloud-sql-proxy.linux.amd64
chmod +x cloud-sql-proxy
gcloud auth application-default login

# Terminal 1: the tunnel.
./cloud-sql-proxy --port 5433 "${PROJECT_ID}:${REGION}:${INSTANCE}"

# Terminal 2: connect (prompts for the postgres password).
psql "host=127.0.0.1 port=5433 user=postgres dbname=app"
```

In `psql`:

```sql
-- Cloud SQL's postgres user must be a member of a role before it can hand things to it.
GRANT app TO postgres;
ALTER DATABASE app OWNER TO app;

-- CI user: connect and read only.
GRANT CONNECT ON DATABASE app TO ci_ro;
GRANT USAGE ON SCHEMA public TO ci_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ci_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE app IN SCHEMA public GRANT SELECT ON TABLES TO ci_ro;
```

The last line makes tables that `app` creates later (via Flyway) readable by `ci_ro` automatically.

## 3. CI service account

```bash
gcloud iam service-accounts create gh-ci --display-name="GitHub Actions CI"

# Lets it use the Cloud SQL Auth Proxy.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CI_SA}" --role=roles/cloudsql.client

# Lets it read the CI password secret, and only that secret.
gcloud secrets add-iam-policy-binding db-ci-ro-password \
  --member="serviceAccount:${CI_SA}" --role=roles/secretmanager.secretAccessor
```

It has no rights on the app password secret, on GKE, or anything else.

## 4. Workload Identity Federation (GitHub OIDC)

```bash
gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github-oidc \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="assertion.repository=='${GITHUB_REPO}'"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

# Allow tokens from this repo to act as the CI service account.
gcloud iam service-accounts add-iam-policy-binding "$CI_SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${GITHUB_REPO}"
```

The provider only accepts tokens whose repository is `karthikkhaderbad/gke-flyway-cicd`. Any workflow run in that repo, including one on a PR branch, can act as `gh-ci`, so keep that account's rights small (as above). Fork PRs cannot get a token.

## 5. GitHub repo variables

These are identifiers, not secrets. Set them with the CLI or under Settings, Secrets and variables, Actions, Variables.

```bash
PROVIDER=$(gcloud iam workload-identity-pools providers describe github-oidc \
  --location=global --workload-identity-pool=github --format='value(name)')

gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --body "$PROVIDER"
gh variable set GCP_CI_SERVICE_ACCOUNT --body "$CI_SA"

# Set this one last: it is the switch that turns the k8s job on.
gh variable set CLOUDSQL_INSTANCE --body "${PROJECT_ID}:${REGION}:${INSTANCE}"
```

## 6. Check it works

Re-run the CI workflow on a PR. The `K8s: deploy chart and check Service and DB` job should now run instead of being skipped.

If it fails:

| Where it fails | Likely cause |
|---|---|
| Authenticate to GCP | A variable is wrong or missing, the repo condition does not match, or the `workloadIdentityUser` binding is missing. |
| Fetch read-only DB password | `gh-ci` lacks `secretAccessor` on `db-ci-ro-password`, or the secret name differs. |
| Start Cloud SQL Auth Proxy | The Cloud SQL Admin API is not enabled, `cloudsql.client` is missing, or the instance name is wrong. Check the printed proxy log. |
| Deploy chart (timeout) | The pod never became Ready. Check the diagnostics step: wrong DB password, `ci_ro` lacks CONNECT, or the pod cannot reach the proxy. |

## Cleanup

Delete the Cloud SQL instance when you are done with the project, because it bills while it exists. Stopping it pauses the compute charge but not the storage. The GKE deploy account and cluster will be added here when the CD pipeline is set up.
