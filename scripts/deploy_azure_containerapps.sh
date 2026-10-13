#!/usr/bin/env bash
# Deploy the application to Azure Container Apps with a managed PostgreSQL
# Flexible Server.
#
# This is the only way production configuration changes. Merging to main
# deploys code, but it updates the container image and nothing else -- scale,
# probes, secrets, environment variables, volume mounts and the bound custom
# domain all come from here.
#
# The shape of the deployment:
#
#   - Postgres is a managed Flexible Server reachable only over the VNet.
#   - Images are built in Azure Container Registry, so a deploy does not depend
#     on a working local Docker daemon or on the machine's architecture.
#   - Migrations and content ingestion run as a Container Apps *job* that must
#     succeed before the new revision is created, rather than on every start.
#   - Media, private content and caselaw artifacts live on Azure Files shares.
#   - TLS is handled by Container Apps ingress.
#
# Configuration lives in .env.containerapps, which is gitignored. Every key in
# that file is pushed as a Container Apps secret.
set -Eeuo pipefail

SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-4f62b1f4-b38c-44f3-9c3f-aedaf2d12d2a}"
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-agentic-housing-rg}"
LOCATION="${AZURE_LOCATION:-centralus}"
REGISTRY="${AZURE_REGISTRY:-agentichousingacr}"
ENVIRONMENT="${AZURE_CONTAINERAPP_ENV:-agentic-housing-env}"
APP_NAME="${AZURE_CONTAINERAPP:-agentic-housing-app}"
JOB_NAME="${AZURE_BOOTSTRAP_JOB:-agentic-housing-bootstrap}"
IDENTITY_NAME="${AZURE_IDENTITY:-agentic-housing-identity}"
FILES_ACCOUNT="${AZURE_FILES_ACCOUNT:-ahfiles1786114234}"
POSTGRES_SERVER="${AZURE_POSTGRES_SERVER:-agentic-housing-db}"
ENV_FILE="${AZURE_ENV_FILE:-.env.containerapps}"
# Whether the caller named an image. Without one a fresh build is tagged with
# the time, and BUILD_IMAGE=false would point the app at a tag that never
# existed.
IMAGE_TAG_GIVEN="${IMAGE_TAG:+true}"
IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
IMAGE_REPO="agentic-housing-drafting"

# 0 lets the app sleep when idle and pays a cold start to wake it; 1 keeps one
# replica warm so there is no cold start at all. A replica sitting at the
# minimum replica count with no traffic bills at Azure's reduced idle rate, so
# MIN_REPLICAS=1 costs a small fraction of an actively serving replica.
MIN_REPLICAS="${MIN_REPLICAS:-0}"
BUILD_IMAGE="${BUILD_IMAGE:-true}"

# Both default off: side-loaded documents change far less often than code, and
# uploading thousands of small files makes an ordinary deploy much slower.
SYNC_CASELAW="${SYNC_CASELAW:-false}"
CASELAW_DIR="${CASELAW_DIR:-downloaded_cases}"
SYNC_PRIVATE_CONTENT="${SYNC_PRIVATE_CONTENT:-false}"
PRIVATE_CONTENT_DIR="${PRIVATE_CONTENT_DIR:-private-content}"
SYNC_ORDINANCES="${SYNC_ORDINANCES:-false}"
ORDINANCE_DIR="${ORDINANCE_DIR:-content/ordinances}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

for command in az python3 rsync; do
  command -v "$command" >/dev/null || { echo "Missing required command: $command" >&2; exit 1; }
done
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE. Copy .env.example and fill in production values." >&2; exit 1; }

az account set --subscription "$SUBSCRIPTION_ID"
ACTIVE_SUBSCRIPTION="$(az account show --query id -o tsv)"
[[ "$ACTIVE_SUBSCRIPTION" == "$SUBSCRIPTION_ID" ]] || {
  echo "Refusing deployment: active subscription is $ACTIVE_SUBSCRIPTION, expected $SUBSCRIPTION_ID" >&2
  exit 1
}
echo "Azure context verified: $(az account show --query user.name -o tsv) / $ACTIVE_SUBSCRIPTION"

IDENTITY_ID="$(az identity show -g "$RESOURCE_GROUP" -n "$IDENTITY_NAME" --query id -o tsv)"
ENVIRONMENT_ID="$(az containerapp env show -g "$RESOURCE_GROUP" -n "$ENVIRONMENT" --query id -o tsv)"
REGISTRY_SERVER="$(az acr show -g "$RESOURCE_GROUP" -n "$REGISTRY" --query loginServer -o tsv)"
IMAGE="${REGISTRY_SERVER}/${IMAGE_REPO}:${IMAGE_TAG}"

# --- Azure Files shares ------------------------------------------------------
# Registered on the environment so both the app and the bootstrap job can mount
# the same shares by name.
echo "Registering Azure Files shares on $ENVIRONMENT..."
FILES_KEY="$(az storage account keys list -g "$RESOURCE_GROUP" -n "$FILES_ACCOUNT" --query "[0].value" -o tsv)"
# "raw" and "published" are the two document storage areas; "media" is Django's
# MEDIA_ROOT for admin uploads.
for share in media raw published; do
  az containerapp env storage set -g "$RESOURCE_GROUP" -n "$ENVIRONMENT" \
    --storage-name "$share" --azure-file-account-name "$FILES_ACCOUNT" \
    --azure-file-account-key "$FILES_KEY" --azure-file-share-name "$share" \
    --access-mode ReadWrite -o none
done

if [[ "$SYNC_CASELAW" == "true" ]]; then
  # Thousands of small files, so this is opt-in rather than part of every deploy.
  # Uploads land in the raw area; the bootstrap job ingests them and writes the
  # derived artifacts to the published area.
  echo "Uploading caselaw bundles from $CASELAW_DIR into raw/caselaw/ (this takes a while)..."
  az storage file upload-batch --account-name "$FILES_ACCOUNT" --account-key "$FILES_KEY" \
    --destination raw --destination-path caselaw --source "$CASELAW_DIR" --no-progress -o none
fi

if [[ "$SYNC_PRIVATE_CONTENT" == "true" ]]; then
  # The caselaw artifacts under private-content/ belong to the caselaw area, not
  # here, and this CLI has no exclude flag — so stage a clean tree first.
  echo "Uploading private content into raw/private-content/..."
  PRIVATE_STAGE="$WORKDIR/private-content"
  mkdir -p "$PRIVATE_STAGE"
  # storage/ is a local checkout's document-storage area -- filled documents
  # from template-fill sessions among it -- and is never organization content.
  rsync -a --exclude 'caselaw-artifacts/' --exclude 'storage/' --exclude '.git' "$PRIVATE_CONTENT_DIR/" "$PRIVATE_STAGE/"
  az storage file upload-batch --account-name "$FILES_ACCOUNT" --account-key "$FILES_KEY" \
    --destination raw --destination-path private-content --source "$PRIVATE_STAGE" --no-progress -o none
fi

if [[ "$SYNC_ORDINANCES" == "true" ]]; then
  # The generated corpus is data, not source: manifests, sections and chunks
  # written by scripts/ingest_local_ordinances.py, plus the source documents they
  # were read from. Uploads land in the raw area; the bootstrap job publishes
  # them under published/content/ordinances/, where the content library reads.
  echo "Uploading the local-ordinance corpus into raw/content/ordinances/..."
  az storage file upload-batch --account-name "$FILES_ACCOUNT" --account-key "$FILES_KEY" \
    --destination raw --destination-path content/ordinances --source "$ORDINANCE_DIR" --no-progress -o none
fi

# --- Image -------------------------------------------------------------------
# Built in ACR rather than locally, so the deploy does not depend on a working
# local Docker daemon or on the machine's architecture.
if [[ "$BUILD_IMAGE" != "true" ]]; then
  [[ "$IMAGE_TAG_GIVEN" == "true" ]] || {
    echo "BUILD_IMAGE=false needs IMAGE_TAG naming an image already in $REGISTRY." >&2
    exit 1
  }
  az acr repository show-tags -n "$REGISTRY" --repository "$IMAGE_REPO" -o tsv | grep -qx "$IMAGE_TAG" || {
    echo "No image ${IMAGE_REPO}:${IMAGE_TAG} in $REGISTRY." >&2
    exit 1
  }
fi
if [[ "$BUILD_IMAGE" == "true" ]]; then
  echo "Building $IMAGE in ACR..."
  az acr build --registry "$REGISTRY" --image "${IMAGE_REPO}:${IMAGE_TAG}" \
    --image "${IMAGE_REPO}:latest" --file Dockerfile . -o none
  echo "Image built: $IMAGE"
else
  echo "Using existing image: $IMAGE"
fi

# --- Configuration -----------------------------------------------------------
# Every key in the env file becomes a Container Apps secret, and every container
# env var references its secret. Secret names are lowercased with underscores
# turned into dashes, which is the character set Container Apps allows.
POSTGRES_FQDN="$(az postgres flexible-server show -g "$RESOURCE_GROUP" -n "$POSTGRES_SERVER" \
  --query fullyQualifiedDomainName -o tsv)"

# Read back any bound custom domains. The spec below replaces the ingress block
# wholesale, so without this a deploy would unbind cle-draft.lemmalegal.com and
# its managed certificate.
CUSTOM_DOMAINS="$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
  --query "properties.configuration.ingress.customDomains" -o json 2>/dev/null || echo "")"
[[ "$CUSTOM_DOMAINS" == "null" ]] && CUSTOM_DOMAINS=""
if [[ -n "$CUSTOM_DOMAINS" ]]; then
  echo "Preserving custom domains: $(python3 -c 'import json,sys; print(", ".join(d["name"] for d in json.load(sys.stdin)))' <<<"$CUSTOM_DOMAINS")"
fi

python3 scripts/render_containerapp_spec.py \
  --env-file "$ENV_FILE" \
  --image "$IMAGE" \
  --location "$LOCATION" \
  --environment-id "$ENVIRONMENT_ID" \
  --identity-id "$IDENTITY_ID" \
  --registry-server "$REGISTRY_SERVER" \
  --postgres-host "$POSTGRES_FQDN" \
  --custom-domains-json "$CUSTOM_DOMAINS" \
  --min-replicas "$MIN_REPLICAS" \
  --app-output "$WORKDIR/app.yaml" \
  --job-output "$WORKDIR/job.yaml"

# --- Bootstrap job -----------------------------------------------------------
# Migrations and content ingestion run once here. The deploy stops if they fail,
# so a bad migration never reaches a revision that serves traffic.
echo "Applying bootstrap job definition..."
if az containerapp job show -g "$RESOURCE_GROUP" -n "$JOB_NAME" -o none 2>/dev/null; then
  az containerapp job update -g "$RESOURCE_GROUP" -n "$JOB_NAME" --yaml "$WORKDIR/job.yaml" -o none
else
  az containerapp job create -g "$RESOURCE_GROUP" -n "$JOB_NAME" --yaml "$WORKDIR/job.yaml" -o none
fi

echo "Running migrations and content ingestion..."
EXECUTION="$(az containerapp job start -g "$RESOURCE_GROUP" -n "$JOB_NAME" --query name -o tsv)"
echo "Job execution: $EXECUTION"
for attempt in $(seq 1 120); do
  STATUS="$(az containerapp job execution show -g "$RESOURCE_GROUP" -n "$JOB_NAME" \
    --job-execution-name "$EXECUTION" --query properties.status -o tsv 2>/dev/null || echo Unknown)"
  case "$STATUS" in
    Succeeded) echo "Bootstrap succeeded."; break ;;
    Failed|Degraded)
      echo "Bootstrap job failed. Recent logs:" >&2
      az containerapp job logs show -g "$RESOURCE_GROUP" -n "$JOB_NAME" \
        --container bootstrap --execution "$EXECUTION" --tail 100 >&2 || true
      exit 1 ;;
    *) sleep 10 ;;
  esac
  [[ $attempt -eq 120 ]] && { echo "Bootstrap job did not finish in 20 minutes." >&2; exit 1; }
done

# --- Application -------------------------------------------------------------
echo "Applying application definition..."
if az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" -o none 2>/dev/null; then
  az containerapp update -g "$RESOURCE_GROUP" -n "$APP_NAME" --yaml "$WORKDIR/app.yaml" -o none
else
  az containerapp create -g "$RESOURCE_GROUP" -n "$APP_NAME" --yaml "$WORKDIR/app.yaml" -o none
fi

FQDN="$(az containerapp show -g "$RESOURCE_GROUP" -n "$APP_NAME" \
  --query properties.configuration.ingress.fqdn -o tsv)"

echo "Waiting for https://$FQDN/healthz ..."
for attempt in $(seq 1 45); do
  if curl --fail --silent --show-error "https://$FQDN/healthz" >/dev/null 2>&1; then
    echo
    echo "Deployment complete: https://$FQDN"
    echo "Image: $IMAGE"
    exit 0
  fi
  sleep 4
done

echo "Revision created, but https://$FQDN/healthz did not respond in time." >&2
az containerapp logs show -g "$RESOURCE_GROUP" -n "$APP_NAME" --tail 100 >&2 || true
exit 1
