# Deployment

The application runs on **Azure Container Apps** with a managed **Azure Database
for PostgreSQL Flexible Server**. The earlier single-VM Docker Compose
deployment is documented at the end as the rollback path.

## Topology

Everything lives in the `agentic-housing-rg` resource group in `centralus`, on
the pre-existing `AIDraftingTool-vnet` (`10.0.0.0/16`).

| Resource | Name | Notes |
| --- | --- | --- |
| Container Apps environment | `agentic-housing-env` | VNet-injected into `containerapps-subnet` (`10.0.1.0/24`) |
| Container app | `agentic-housing-app` | External ingress, TLS terminated by Azure |
| Bootstrap job | `agentic-housing-bootstrap` | Manual trigger; migrations and content ingestion |
| Registry | `agentichousingacr` | Basic tier |
| Database | `agentic-housing-db` | PostgreSQL 16, `Standard_B1ms`, private access in `postgres-subnet` (`10.0.2.0/24`) |
| Files | `ahfiles1786114234` | SMB shares `raw`, `published`, and `media` |
| Pull identity | `agentic-housing-identity` | User-assigned, `AcrPull` on the registry |
| Legacy VM | `AIDraftingTool` | In `default` subnet (`10.0.0.0/24`) |

The database has **no public endpoint**. It is reachable only from inside the
VNet. The private DNS zone `privatelink.postgres.database.azure.com` is linked to
the VNet, so anything in it resolves the server's FQDN to its private address
(`10.0.2.4`).

**Ad-hoc database access.** Once the legacy VM is deleted, nothing outside the
Container Apps environment can reach the server — the VM was the only other host
in the VNet. To run a query or a one-off management command, use a Container Apps
job (the bootstrap job already has the credentials and the network path):

```bash
az containerapp job start -g agentic-housing-rg -n agentic-housing-bootstrap
```

For an interactive shell, `az containerapp exec -g agentic-housing-rg -n
agentic-housing-app` attaches to a running replica — but note the app scales to
zero, so send a request first to wake one. A temporary jumpbox in the `default`
subnet is the fallback if you need `psql` directly.

## Two hosts, not one

The app and the API are deployed separately:

```text
cle-draft.lemmalegal.com        Azure Static Web Apps (Free)   the built SPA
api.cle-draft.lemmalegal.com    Azure Container Apps           Django + nginx
```

The reason is cold start. The API scales to zero and takes something like twenty
seconds to wake. When nginx *inside that same container* was also what served
`index.html`, nobody saw anything at all for those twenty seconds — a blank tab
with no way to explain itself, because the thing that would do the explaining
was the thing still starting. Served from a static host that is always warm, the
page paints in under a second and the wait becomes visible and named while the
first API call wakes the container. See
[`frontend/src/api/wakeNotice.js`](../frontend/src/api/wakeNotice.js).

**They must be sibling subdomains of one registrable domain.** A cookie is
*same-site* across subdomains of `lemmalegal.com`, so the session survives on an
ordinary `SameSite=Lax` cookie. Hosting the app on an unrelated name — the
default `*.azurestaticapps.net`, say — makes the session a third-party cookie,
which Safari and Firefox drop. That failure looks exactly like login silently
not working, so it is worth stating plainly rather than discovering.

What this costs, and where each piece lives:

| Concern | Where |
|---|---|
| CORS, and which origins may send credentials | `DJANGO_CORS_ALLOWED_ORIGINS` |
| CSRF for cross-origin POSTs | `DJANGO_CSRF_TRUSTED_ORIGINS` |
| Where the SPA sends API calls | `VITE_API_BASE`, baked in at build time |
| Where Django sends people after OAuth | `FRONTEND_SITE_URL` |
| Which origins may frame a PDF the API serves | `DJANGO_FRAME_ANCESTORS`, defaulting to the CORS origins |
| Response headers the browser may reveal | `CorsMiddleware.EXPOSED_HEADERS` |

That last one is the subtle one. `Content-Disposition` carries download
filenames and four `X-LegalServer-*` headers carry whether a save landed. A
cross-origin caller cannot read any of them unless the server names them, and
the failure is *silent*: the header arrives, `headers.get()` returns null, and
the feature quietly does nothing. Adding a response header the frontend reads
means adding it to that tuple too.

Framing is the other one worth naming. The app previews decision scans,
library sources, and case documents in an iframe, and `X-Frame-Options` cannot
describe a split deployment: its only useful value is SAMEORIGIN, which means
*the API's* origin, so the browser refused to display a PDF the app had
already fetched. Those responses send `Content-Security-Policy:
frame-ancestors` instead, and nothing else may frame them.

### Publishing the app

```bash
./scripts/deploy_static_frontend.sh
```

Merging to `main` does this as well, after the API rolls — the app is the
client, so publishing it ahead of the API it calls would put a build expecting
new endpoints in front of a deployment that lacks them.

### The DNS records

`lemmalegal.com` is hosted at SiteGround, not Azure, so these are added there by
hand. `cle-draft` already exists and points at the container app, so this is a
move rather than a clean addition — **do it in two phases**, because the app
cannot be pointed at the static host until the API answers on its own name.

Both are subdomains, which decides the validation method for each: Container
Apps wants an `asuid.` TXT record, Static Web Apps validates a subdomain from
the CNAME itself and needs no TXT. (A TXT record *is* required for a Static Web
App on an apex domain — not the case here.)

**Phase 1 — give the API its own hostname.** Nothing is serving on it yet, so
this disturbs nothing.

| Host | Type | Value |
|---|---|---|
| `asuid.api.cle-draft` | TXT | the container app's `customDomainVerificationId` |
| `api.cle-draft` | CNAME | the container app's ingress FQDN |

```bash
az containerapp show -g agentic-housing-rg -n agentic-housing-app \
  --query "{fqdn:properties.configuration.ingress.fqdn,asuid:properties.customDomainVerificationId}" -o json

az containerapp hostname add -g agentic-housing-rg -n agentic-housing-app \
  --hostname api.cle-draft.lemmalegal.com
az containerapp hostname bind -g agentic-housing-rg -n agentic-housing-app \
  --hostname api.cle-draft.lemmalegal.com --environment agentic-housing-env \
  --validation-method CNAME
```

Then point the API at itself and rebuild the app against it:

```bash
# .env.containerapps
#   DJANGO_ALLOWED_HOSTS       += api.cle-draft.lemmalegal.com
#   DJANGO_CSRF_TRUSTED_ORIGINS += https://cle-draft.lemmalegal.com
#   DJANGO_CORS_ALLOWED_ORIGINS  = https://cle-draft.lemmalegal.com
#   DJANGO_CSRF_COOKIE_DOMAIN    = .lemmalegal.com
#   FRONTEND_SITE_URL            = https://cle-draft.lemmalegal.com
./scripts/deploy_azure_containerapps.sh
VITE_API_BASE=https://api.cle-draft.lemmalegal.com/api ./scripts/deploy_static_frontend.sh
```

`DJANGO_CSRF_COOKIE_DOMAIN` is the one to not forget. The frontend copies the
CSRF token out of `document.cookie`, and a cookie set by `api.cle-draft` is
invisible to script on `cle-draft` unless it names the parent domain. Left
unset, every save fails 403 while everything else keeps working.

**Phase 2 — move the app hostname.** This is the cutover; allow a few minutes of
inconsistency.

| Host | Type | Value |
|---|---|---|
| `cle-draft` | CNAME | the static web app's `defaultHostname` — **replaces** the record pointing at the container app |

```bash
az containerapp hostname delete -g agentic-housing-rg -n agentic-housing-app \
  --hostname cle-draft.lemmalegal.com
az staticwebapp hostname set -g agentic-housing-rg -n agentic-housing-web \
  --hostname cle-draft.lemmalegal.com --validation-method cname-delegation
```

Finally, point CI at the real hostnames:

```bash
gh variable set APP_URL --body https://cle-draft.lemmalegal.com
gh variable set API_URL --body https://api.cle-draft.lemmalegal.com
```

Worth checking once DNS is live, because the local harness separates the app and
the API by *port* and cookies ignore ports — so the cookie-domain behaviour is
the one thing it could not exercise: sign in, then save something (a profile
edit will do). A 403 there means `DJANGO_CSRF_COOKIE_DOMAIN` did not take.

## Decisions worth knowing

- **Migrations do not run at container start.** `docker/bootstrap.sh` runs as a
  Container Apps job that must succeed before a new revision is created.
  Migrating on start cannot work once more than one replica exists — they race
  each other — and it would put schema changes on the cold-start path.
- **Static files are collected, and bytecode compiled, at image build time.**
  Both are identical on every replica and depend only on the code.
- **TLS is handled by Container Apps ingress.** Certificates for the custom
  domain are Azure-managed and renew automatically.
- **Side-loaded documents live on Azure Files shares**, not image layers.
  Keeping the corpus out of the image is what makes build and pull times
  reasonable — doubly so given that every cold start may pull the image.
- **The web container mounts only what it serves.** `media` and
  `published`, not `raw`. Every share is mounted before the container may
  start, so each one costs cold-start seconds, and `raw/` is only ever read by
  ingestion commands that run in the bootstrap job. The practical consequence
  is in [Document storage](#document-storage).
- **The app scales to zero**, so the first request after five idle minutes
  waits for a replica to start. See [Sleeping and waking](WARM_START.md).
- Django is told to trust `X-Forwarded-Proto` via
  `DJANGO_TRUST_PROXY_SSL_HEADER=true`, because TLS terminates upstream.

## Configuration

`.env.containerapps` in the repository root holds production settings. It is
gitignored, and every key in it is pushed to Container Apps as a *secret* — none
of the values appear in plain text in `az containerapp show` or in the portal.

`POSTGRES_HOST` is filled in by the deploy script from the live server, so it is
not stored in the file.

Settings that matter for this topology:

```
POSTGRES_SSLMODE=require            # Flexible Server rejects plaintext
DJANGO_TRUST_PROXY_SSL_HEADER=true  # TLS terminates at ingress
DJANGO_SESSION_COOKIE_SECURE=true
DJANGO_CSRF_COOKIE_SECURE=true
DJANGO_CSRF_TRUSTED_ORIGINS=https://<app fqdn>,https://cle-draft.lemmalegal.com
DOCUMENT_STORAGE_BACKEND=filesystem
DOCUMENT_STORAGE_ROOT=/app/storage
ORGANIZATION_CONTENT_LIBRARY_DIR=/app/storage/published/private-content
```

### Application URLs

Every screen has a readable, reload-safe address — `/drafting/26-0222/sessions/184/plan`,
`/triage/26-0222/assessments/52` — built and read only in
`frontend/src/routes/paths.js`. Both hosts already rewrite unknown paths to the
SPA (`nginx.conf`'s `try_files`, `staticwebapp.config.json`'s
`navigationFallback`), so no server route is needed for a new screen.

Nested routes roll out one family at a time. To switch a family off, list it at
build time; its links then point at the family's collection screen, and its URLs
say they cannot be opened rather than showing something else:

```bash
VITE_DISABLED_ROUTE_FAMILIES=chat-threads,triage-assessments ./scripts/deploy_static_frontend.sh
```

The families are listed in `ROUTE_FAMILIES` in `paths.js`. The full catalog,
its rules, and the tests behind each are in [`docs/ROUTES.md`](ROUTES.md).

To check that the production `nginx.conf` sends every route family to the SPA
and leaves `/api/`, `/admin/`, readiness, and health alone (needs Docker):

```bash
npm --prefix frontend run build
scripts/check_spa_routes.sh
```

## Document storage

Side-loaded documents — the case-law corpus and private organization content —
go through [`apps.core.storage`](../backend/apps/core/storage.py), which splits
every store into two areas:

```text
/app/storage/raw/caselaw/...              you upload here; nothing serves from it
/app/storage/raw/private-content/...
/app/storage/published/caselaw/...        written by ingestion; served by the app
/app/storage/published/private-content/   written by publish_private_content
```

`raw` and `published` are **separate Azure Files shares** mounted under one root,
so the filesystem backend sees them as ordinary directories. Separate shares
rather than two directories in one share keeps the operator-writable area and the
application-served area independently sized and permissionable.

The boundary is what makes a slow upload safe. Thousands of small files can
accumulate under `raw/` for as long as the transfer takes without a running
replica ever seeing a half-finished corpus; ingestion or publishing is the single
moment the change becomes visible.

**Only the bootstrap job mounts `raw/`.** The web replicas mount `media` and
`published` and nothing else, because every share has to be mounted before the
container is allowed to start and that time lands on the cold start. So run
`ingest_caselaw`, `fetch_cap_opinions`, `enrich_caselaw_metadata` and
`publish_private_content` in the job — an `az containerapp exec` into a web
replica will not find `raw/` there:

```bash
az containerapp job start -g agentic-housing-rg -n agentic-housing-bootstrap
```

To upload new material:

```bash
SYNC_CASELAW=true CASELAW_DIR=~/downloaded_cases \
  ./scripts/deploy_azure_containerapps.sh      # → raw/caselaw/, then ingested

SYNC_PRIVATE_CONTENT=true \
  ./scripts/deploy_azure_containerapps.sh      # → raw/private-content/, then published
```

To upload content or change configuration without rebuilding, reuse an image
already in the registry. `IMAGE_TAG` is required then, and the script checks the
tag exists before deploying it:

```bash
BUILD_IMAGE=false IMAGE_TAG=<tag> SYNC_PRIVATE_CONTENT=true \
  ./scripts/deploy_azure_containerapps.sh
```

The private-content upload leaves out `caselaw-artifacts/` (it has its own
area) and `storage/`, a checkout's local document store, which holds filled
documents from template-fill sessions.

Switching to S3 is configuration, not code: set `DOCUMENT_STORAGE_BACKEND=s3`
with a bucket and keys, and install `boto3`. See
[`docs/CASELAW_INGESTION.md`](CASELAW_INGESTION.md) for the one caveat about
`ORGANIZATION_CONTENT_LIBRARY_DIR` under object storage.

`DJANGO_SECURE_HSTS_SECONDS` is deliberately `0`. Enabling HSTS is difficult to
reverse in browsers that have cached the policy, so turn it on as its own change
once the custom domain has been stable on HTTPS for a while.

## Deploying

There are two paths, and the difference between them matters.

### Code changes: merge to main

[`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml) deploys on every
push to `main`. It runs the backend and frontend test suites, builds the image in
ACR tagged with the commit SHA, runs the bootstrap job, waits for it to succeed,
rolls the app, and then verifies the live site.

The workflow updates **only the container image** (`az containerapp update
--image`). It never applies a full spec. Two consequences worth understanding:

- **No production configuration lives in GitHub.** Secrets, env vars, volume
  mounts, scale rules, and the bound custom domain all stay as they are, so the
  repository needs none of them. Verified: an image-only update leaves all 27
  secrets, all three mounts, `minReplicas: 0`, and the `SniEnabled` custom domain
  binding untouched.
- **Configuration changes will not deploy themselves.** Adding an environment
  variable or changing scale settings requires the local script below.

CI needs no submodule credentials: `private-content` is a private repository, but
production reads organization content from the published storage share rather than
from the image, and the test suite passes without it.

### Configuration changes: the local script

```bash
./scripts/deploy_azure_containerapps.sh
```

This renders and applies the *full* app and job specs from
`.env.containerapps`, so it is what picks up new settings. It reads the live
`ingress.customDomains` and echoes them back, so applying a full spec does not
unbind the hostname.

Run it whenever `.env.containerapps` changes; otherwise let CI deploy.

### Required GitHub secrets

Authentication is federated (OIDC) — there is no client secret to rotate.

| Secret | Value |
| --- | --- |
| `AZURE_CLIENT_ID` | `f7554e71-ab71-4b95-9814-e69e7dcc847a` |
| `AZURE_TENANT_ID` | `95fa40d6-b304-4081-bb6a-48d99c2b75ad` |
| `AZURE_SUBSCRIPTION_ID` | `4f62b1f4-b38c-44f3-9c3f-aedaf2d12d2a` |

The app registration is `CLARA-GitHubActions-Deploy`. Its service principal
holds `Contributor` scoped to exactly three resources — the registry, the
container app, and the bootstrap job — and nothing at subscription or resource
group level. Registry access is `Contributor` rather than `AcrPush` because
`az acr build` needs `registries/scheduleRun/action`, which `AcrPush` lacks.

The GitHub repository is `LemmaLegalConsulting/CLARA`. The Azure resource names,
image repository, database names, and public hostnames retain their existing
identifiers; renaming the product does not provision replacement infrastructure.
If the GitHub repository is renamed again, update both OIDC subjects below to
match before running a deployment. Repository secrets and variables remain on
the renamed repository.

This repository uses GitHub's immutable OIDC subject format, including numeric
owner and repository IDs. Read the exact prefix rather than constructing it from
the repository name:

```bash
gh api repos/LemmaLegalConsulting/CLARA/actions/oidc/customization/sub
```

Append `:ref:refs/heads/main` or `:environment:production` to the returned
`sub_claim_prefix`. A name-only subject fails Azure login with `AADSTS700213`.
See [GitHub's OIDC reference](https://docs.github.com/en/actions/reference/security/oidc).

Two federated credentials exist:

- `gh-main` — `repo:LemmaLegalConsulting@83832478/CLARA@1279505923:ref:refs/heads/main`, used by
  the previous workflow. Retire this branch-only credential after the protected-environment workflow is deployed.
- `gh-env-production` — `repo:LemmaLegalConsulting@83832478/CLARA@1279505923:environment:production`,
  used by the protected `production` deployment job. The environment requires approval from `nonprofittechy` and permits protected branches only. Main requires the `test` status check, including for administrators. PR approval is required by default; `nonprofittechy` has an explicit review-bypass allowance so the operator can merge their own PRs. This exception does not bypass required status checks. Verify these external settings before each release-policy change.

To also re-upload the caselaw corpus (slow — thousands of small files, and only
necessary when the corpus itself changes):

```bash
SYNC_CASELAW=true ./scripts/deploy_azure_containerapps.sh
```

## Verifying a deployment

The deploy script polls `/healthz`, but that only proves nginx is up. A fuller
check, against the app's default hostname:

```bash
FQDN=agentic-housing-app.victorioussea-d703d3b6.centralus.azurecontainerapps.io

curl -s -o /dev/null -w '%{http_code}\n' "https://$FQDN/healthz"            # 200
curl -s -o /dev/null -w '%{http_code}\n' "https://$FQDN/admin/login/"       # 200, proves the database is reachable
curl -s -o /dev/null -w '%{http_code}\n' "https://$FQDN/static/admin/css/base.css"  # 200, proves build-time collectstatic
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' "https://$FQDN/admin/"     # 302 to an https:// URL
```

The last one is the one worth reading closely. If the redirect target comes back
as `http://`, Django is not honouring `X-Forwarded-Proto` and
`DJANGO_TRUST_PROXY_SSL_HEADER` is not set — which breaks secure cookies and
CSRF in ways that are awkward to diagnose from the browser.

## DNS and the custom domain

The Container Apps environment has its own static IP. **The VM's public IP
(`20.118.35.106`) cannot be carried over** — Container Apps does not accept a
bring-your-own public IP, and neither does Container Instances. Preserving it
would require putting an Application Gateway in front of an internal
environment, which costs several times more than the application itself.

`cle-draft.lemmalegal.com` is **bound** with an Azure-managed certificate
(`mc-agentic-housin-cle-draft-lemmal-6817`), which renews automatically.

The deploy script reads the live `ingress.customDomains` and echoes them back
into the rendered spec. That matters because the spec replaces the ingress block
wholesale — without it, a deploy would silently unbind the hostname and its
certificate, leaving the site answering on its default FQDN while failing on the
real one.

To attach a further hostname:

1. Create these two records:

   ```text
   CNAME  cle-draft.lemmalegal.com        →  agentic-housing-app.victorioussea-d703d3b6.centralus.azurecontainerapps.io
   TXT    asuid.cle-draft.lemmalegal.com  →  7B899F654280032793A07DBA3A18119DA1E90BE0015178ADADFE6EB39C11419C
   ```

   The TXT value is the app's `customDomainVerificationId`. Re-read it rather
   than trusting this copy if the app is ever recreated:

   ```bash
   az containerapp show -g agentic-housing-rg -n agentic-housing-app \
     --query properties.customDomainVerificationId -o tsv
   ```

2. Once they resolve, add and bind the hostname. `bind` provisions a free
   managed certificate that renews automatically:

   ```bash
   az containerapp hostname add -g agentic-housing-rg -n agentic-housing-app \
     --hostname cle-draft.lemmalegal.com
   az containerapp hostname bind -g agentic-housing-rg -n agentic-housing-app \
     --hostname cle-draft.lemmalegal.com --environment agentic-housing-env \
     --validation-method CNAME
   ```

An `A` record to the environment's static IP (`20.118.11.206`) also works, but
the CNAME is safer: that IP is stable for the environment's lifetime, not
guaranteed beyond it.

## History: the single-VM deployment

This application previously ran on one VM (`AIDraftingTool`, `20.118.35.106`)
with Postgres in a container, `nginx-proxy` and `acme-companion` for TLS, and
`scripts/deploy_azure.sh` deploying over SSH. That VM was decommissioned on
2026-08-07 after its database was copied into the managed server, and its
deployment artifacts — `deploy_azure.sh`, `migrate_vm_database.sh`,
`compose.yaml`, `cloud-init.yml`, `start.sh` and `build_docker.sh` — were
removed on 2026-08-13. Git history has them if they are ever wanted.

For local development, use `./run_all.sh`, which runs Vite and Django directly.
There is no longer a way to run the full production shape on one machine, which
is the trade for not maintaining a second deployment path that nothing
exercises.

One thing from that era is worth remembering, because the reasoning generalizes:
the database copy had to happen *before* the first bootstrap run, since a dump
from the VM (which had zero case-law decisions) would otherwise have wiped the
corpus the bootstrap job had just ingested.
