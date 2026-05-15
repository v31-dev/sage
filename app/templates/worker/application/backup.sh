#!/bin/sh
set -eu

APP_ROOT="${WORKER_HOME_DIR}/applications"
APP_NAME="${APP_NAME}"
APP_DIR="$APP_ROOT/$APP_NAME"
VOLUME_NAME="${VOLUME_NAME}"
VOLUME_DIR="$APP_DIR/volumes/$VOLUME_NAME"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
  rm -f "$0"
}

trap cleanup EXIT

if [ ! -d "$APP_DIR" ]; then
  echo "Application directory not found: $APP_DIR" >&2
  exit 1
fi

if [ ! -d "$VOLUME_DIR" ]; then
  echo "Volume directory not found: $VOLUME_DIR" >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required on worker for encrypted backups" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required on worker for direct S3 uploads" >&2
  exit 1
fi

ARCHIVE_PATH="$TMP_DIR/${ARCHIVE_NAME}.tar.gz"
ENCRYPTED_PATH="$TMP_DIR/${ARCHIVE_NAME}.tar.gz.enc"
PASSPHRASE_PATH="$TMP_DIR/passphrase"
UPLOAD_URL_PATH="$TMP_DIR/upload_url"

cat <<'__SAGE_BACKUP_KEY__' > "$PASSPHRASE_PATH"
${ENCRYPTION_KEY}
__SAGE_BACKUP_KEY__

cat <<'__SAGE_UPLOAD_URL__' > "$UPLOAD_URL_PATH"
${UPLOAD_URL}
__SAGE_UPLOAD_URL__

tar -C "$APP_DIR/volumes" -czf "$ARCHIVE_PATH" "$VOLUME_NAME"

openssl enc -aes-256-cbc -pbkdf2 -salt \
  -in "$ARCHIVE_PATH" \
  -out "$ENCRYPTED_PATH" \
  -pass file:"$PASSPHRASE_PATH"

HTTP_STATUS="$(
  curl \
    --silent \
    --show-error \
    --output "$TMP_DIR/upload_response" \
    --write-out "%{http_code}" \
    --request PUT \
    --header "Content-Type: application/octet-stream" \
    --upload-file "$ENCRYPTED_PATH" \
    "$(cat "$UPLOAD_URL_PATH")"
)"

case "$HTTP_STATUS" in
  2*)
    ;;
  *)
    echo "Upload failed with status $HTTP_STATUS" >&2
    cat "$TMP_DIR/upload_response" >&2 || true
    exit 1
    ;;
esac