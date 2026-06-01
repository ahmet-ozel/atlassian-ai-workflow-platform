#!/usr/bin/env bash
# =============================================================================
# infra/minio/init.sh — MinIO bucket bootstrap (platform-mimari-ops task 13.4)
# =============================================================================
# Purpose
# -------
# Idempotently provisions the MinIO buckets the platform requires:
#
#   * ``ai-runs``       — execution artifacts (pre-existing convention,
#                         created lazily by execution-runner-worker via
#                         _ensure_bucket_exists; declared here for
#                         single-source-of-truth visibility).
#   * ``audit-archive`` — daily-partitioned audit log archive populated
#                         by the AuditPruneWorkflow's
#                         ``archive_audit_to_minio`` activity.
#                         Layout: ``audit-archive/{Y}/{M}/{D}/audit-N.jsonl.gz``
#                         (platform-mimari-ops design §"MinIO arşiv yapısı",
#                         requirement 6.3).
#
# Usage
# -----
# Run **after** the ``minio`` Compose service is healthy. Two modes:
#
#   1. From the host (default — connects to MinIO on http://localhost:9000):
#        bash platform/infra/minio/init.sh
#
#   2. From inside any container on the compose network:
#        MINIO_ENDPOINT=minio:9000 bash /workspace/infra/minio/init.sh
#
# The script auto-detects the bootstrap method:
#
#   * If ``mc`` (the MinIO client) is on PATH it is used (idiomatic +
#     supports object-lock / lifecycle when production hardening lands).
#   * Otherwise the script falls back to the S3-compatible HTTP API via
#     ``curl`` with AWS Signature V4 — no extra runtime requirement
#     beyond ``curl`` and ``openssl`` (both ubiquitous on dev machines
#     and the busybox-based Compose images).
#
# Environment variables
# ---------------------
#   MINIO_ENDPOINT       Host:port the script reaches MinIO on
#                        (default: localhost:9000).
#   MINIO_USE_SSL        ``true`` to use https:// (default: false).
#   MINIO_ROOT_USER      Access key (default: minio).
#   MINIO_ROOT_PASSWORD  Secret key (default: miniosecret_dev_only).
#   AUDIT_ARCHIVE_BUCKET Override the bucket name (default: audit-archive).
#
# Exit codes
# ----------
#   0  All required buckets exist (created or already present).
#   1  MinIO unreachable, credentials missing, or bucket creation
#      failed for a reason other than "already exists".
#
# Idempotency
# -----------
# Re-running the script is a no-op when every required bucket already
# exists; ``BucketAlreadyOwnedByYou`` (HTTP 409) is treated as success.
#
# Production hardening (deferred — Requirement 18.5)
# --------------------------------------------------
# Object-lock + lifecycle policies are NOT applied in this dev-mode
# bootstrap. Production deployments should:
#   * Create ``audit-archive`` with ``--with-lock`` and apply a
#     ``COMPLIANCE`` retention of ≥ RETENTION_DAYS days
#     (immutability — write-once retention).
#   * Apply a transition lifecycle to a cold tier after N days.
# See ``platform/infra/minio/README.md`` for the production checklist.
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration (env-overridable defaults)
# ---------------------------------------------------------------------------

MINIO_ENDPOINT="${MINIO_ENDPOINT:-localhost:9000}"
MINIO_USE_SSL="${MINIO_USE_SSL:-false}"
MINIO_ROOT_USER="${MINIO_ROOT_USER:-minio}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-miniosecret_dev_only}"
AUDIT_ARCHIVE_BUCKET="${AUDIT_ARCHIVE_BUCKET:-audit-archive}"
AI_RUNS_BUCKET="${AI_RUNS_BUCKET:-ai-runs}"

# Buckets that MUST exist after this script returns 0. The ordering is
# stable so log output is reproducible across runs.
REQUIRED_BUCKETS=("${AI_RUNS_BUCKET}" "${AUDIT_ARCHIVE_BUCKET}")

if [[ "${MINIO_USE_SSL,,}" == "true" ]]; then
    SCHEME="https"
else
    SCHEME="http"
fi
ENDPOINT_URL="${SCHEME}://${MINIO_ENDPOINT}"

log() {
    # POSIX-friendly leading timestamp; printf so the format string
    # is not interpreted as a printf spec when callers pass arbitrary
    # text containing ``%``.
    printf '[minio-init] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

die() {
    log "ERROR: $*" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Reachability probe — fails fast with a friendly message if MinIO is down
# ---------------------------------------------------------------------------

probe_minio() {
    local health_url="${ENDPOINT_URL}/minio/health/live"
    local attempts=0
    local max_attempts=30

    while (( attempts < max_attempts )); do
        if curl --silent --show-error --fail --max-time 2 \
                "${health_url}" >/dev/null 2>&1; then
            log "MinIO is reachable at ${ENDPOINT_URL}"
            return 0
        fi
        attempts=$((attempts + 1))
        sleep 2
    done

    die "MinIO health probe failed after ${max_attempts} attempts (${health_url})"
}

# ---------------------------------------------------------------------------
# mc-based bucket creation (preferred when available)
# ---------------------------------------------------------------------------

bootstrap_with_mc() {
    local alias_name="platformminio"

    log "Using mc client for bucket bootstrap"

    # ``mc alias set`` is idempotent — re-applying overwrites credentials.
    mc alias set "${alias_name}" "${ENDPOINT_URL}" \
        "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" \
        --api S3v4 >/dev/null

    local bucket
    for bucket in "${REQUIRED_BUCKETS[@]}"; do
        # ``mb --ignore-existing`` returns 0 if the bucket already exists.
        if mc mb --ignore-existing "${alias_name}/${bucket}" >/dev/null 2>&1; then
            log "bucket ready: ${bucket}"
        else
            die "failed to create bucket ${bucket}"
        fi
    done

    # Optional: enable versioning on audit-archive for tamper-evidence.
    # Left as a best-effort step (some MinIO versions / dev modes
    # decline versioning); failure here does not abort the script.
    if mc version enable "${alias_name}/${AUDIT_ARCHIVE_BUCKET}" \
            >/dev/null 2>&1; then
        log "versioning enabled on ${AUDIT_ARCHIVE_BUCKET}"
    else
        log "versioning unavailable on ${AUDIT_ARCHIVE_BUCKET} (dev mode)"
    fi
}

# ---------------------------------------------------------------------------
# curl-based bucket creation (fallback — no mc on PATH)
# ---------------------------------------------------------------------------

# AWS SigV4 helpers (mirroring the worker's minio.py implementation).
# These are intentionally minimal: a single signed PUT to /<bucket>
# is enough to create or verify each bucket.

hex_sha256() {
    # $1: the bytes to hash. We pipe through openssl to keep coreutils
    # variants (``sha256sum`` GNU vs BSD) out of the picture.
    printf '%s' "$1" | openssl dgst -sha256 -hex | awk '{print $NF}'
}

hmac_sha256_hex() {
    local key_hex="$1" msg="$2"
    printf '%s' "${msg}" \
        | openssl dgst -sha256 -mac HMAC -macopt "hexkey:${key_hex}" -hex \
        | awk '{print $NF}'
}

put_bucket_curl() {
    local bucket="$1"
    local host="${MINIO_ENDPOINT}"
    local now_iso amz_date date_stamp
    now_iso="$(date -u +%Y%m%dT%H%M%SZ)"
    amz_date="${now_iso}"
    date_stamp="${now_iso:0:8}"  # YYYYMMDD

    local region="us-east-1"
    local service="s3"
    local payload_hash
    payload_hash="$(hex_sha256 '')"  # empty body for PUT bucket

    # Canonical request
    local canonical_uri="/${bucket}/"
    # Canonical headers MUST be sorted lexicographically; the empty
    # query string line is required by SigV4.
    local canonical_headers
    canonical_headers="host:${host}
x-amz-content-sha256:${payload_hash}
x-amz-date:${amz_date}
"
    local signed_headers="host;x-amz-content-sha256;x-amz-date"
    local canonical_request
    canonical_request="PUT
${canonical_uri}

${canonical_headers}
${signed_headers}
${payload_hash}"

    local credential_scope="${date_stamp}/${region}/${service}/aws4_request"
    local string_to_sign
    string_to_sign="AWS4-HMAC-SHA256
${amz_date}
${credential_scope}
$(hex_sha256 "${canonical_request}")"

    # Derive signing key (SigV4: chained HMACs, "AWS4" + secret as initial key).
    local k_secret_hex k_date_hex k_region_hex k_service_hex k_signing_hex
    k_secret_hex="$(printf 'AWS4%s' "${MINIO_ROOT_PASSWORD}" | xxd -p -c 256 | tr -d '\n')"
    k_date_hex="$(hmac_sha256_hex "${k_secret_hex}" "${date_stamp}")"
    k_region_hex="$(hmac_sha256_hex "${k_date_hex}" "${region}")"
    k_service_hex="$(hmac_sha256_hex "${k_region_hex}" "${service}")"
    k_signing_hex="$(hmac_sha256_hex "${k_service_hex}" "aws4_request")"

    local signature
    signature="$(hmac_sha256_hex "${k_signing_hex}" "${string_to_sign}")"

    local authorization
    authorization="AWS4-HMAC-SHA256 Credential=${MINIO_ROOT_USER}/${credential_scope}, SignedHeaders=${signed_headers}, Signature=${signature}"

    # Send the PUT. Treat HTTP 200 (created) and 409 (already exists,
    # ``BucketAlreadyOwnedByYou``) as success. Any other status code is
    # surfaced as a fatal error with the response body for debuggability.
    local http_status response_body tmp
    tmp="$(mktemp)"
    http_status="$(curl --silent --show-error \
        --request PUT \
        --header "Host: ${host}" \
        --header "x-amz-content-sha256: ${payload_hash}" \
        --header "x-amz-date: ${amz_date}" \
        --header "Authorization: ${authorization}" \
        --output "${tmp}" \
        --write-out '%{http_code}' \
        "${ENDPOINT_URL}${canonical_uri}")"

    response_body="$(cat "${tmp}")"
    rm -f "${tmp}"

    case "${http_status}" in
        200|201|204)
            log "bucket created: ${bucket}"
            ;;
        409)
            log "bucket already exists: ${bucket}"
            ;;
        *)
            die "PUT bucket ${bucket} failed: HTTP ${http_status}: ${response_body}"
            ;;
    esac
}

bootstrap_with_curl() {
    log "Using curl + SigV4 fallback for bucket bootstrap"

    # ``openssl`` and ``xxd`` are required for the SigV4 helpers.
    command -v openssl >/dev/null 2>&1 || die "openssl is required for the curl fallback"
    command -v xxd >/dev/null 2>&1 || die "xxd is required for the curl fallback"

    local bucket
    for bucket in "${REQUIRED_BUCKETS[@]}"; do
        put_bucket_curl "${bucket}"
    done
}

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

main() {
    log "starting MinIO bucket bootstrap (endpoint=${ENDPOINT_URL})"

    [[ -n "${MINIO_ROOT_USER}" ]]     || die "MINIO_ROOT_USER is empty"
    [[ -n "${MINIO_ROOT_PASSWORD}" ]] || die "MINIO_ROOT_PASSWORD is empty"

    probe_minio

    if command -v mc >/dev/null 2>&1; then
        bootstrap_with_mc
    else
        bootstrap_with_curl
    fi

    log "all required buckets are ready: ${REQUIRED_BUCKETS[*]}"
}

main "$@"
