#!/bin/sh
set -eu

APP_ROOT="${WORKER_HOME_DIR}/applications"
APP_NAME="${APP_NAME}"
APP_DIR="$APP_ROOT/$APP_NAME"
VOLUME_NAME="${VOLUME_NAME}"
VOLUMES_DIR="$APP_DIR/volumes"
VOLUME_DIR="$VOLUMES_DIR/$VOLUME_NAME"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
  rm -f "$0"
}

trap cleanup EXIT

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required on worker for encrypted restores" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required on worker for direct S3 downloads" >&2
  exit 1
fi

mkdir -p "$VOLUMES_DIR"

ARCHIVE_PATH="$TMP_DIR/restore.tar.gz"
ENCRYPTED_PATH="$TMP_DIR/restore.tar.gz.enc"
PASSPHRASE_PATH="$TMP_DIR/passphrase"
DOWNLOAD_URL_PATH="$TMP_DIR/download_url"

cat <<'__SAGE_RESTORE_KEY__' > "$PASSPHRASE_PATH"
${ENCRYPTION_KEY}
__SAGE_RESTORE_KEY__

cat <<'__SAGE_DOWNLOAD_URL__' > "$DOWNLOAD_URL_PATH"
${DOWNLOAD_URL}
__SAGE_DOWNLOAD_URL__

HTTP_STATUS="$(
  curl \
    --silent \
    --show-error \
    --output "$ENCRYPTED_PATH" \
    --write-out "%{http_code}" \
    --request GET \
    "$(cat "$DOWNLOAD_URL_PATH")"
)"

case "$HTTP_STATUS" in
  2*)
    ;;
  *)
    echo "Download failed with status $HTTP_STATUS" >&2
    exit 1
    ;;
esac

openssl enc -d -aes-256-cbc -pbkdf2 \
  -in "$ENCRYPTED_PATH" \
  -out "$ARCHIVE_PATH" \
  -pass file:"$PASSPHRASE_PATH"

tar -tzf "$ARCHIVE_PATH" >/dev/null

rm -rf "$VOLUME_DIR"
tar -C "$VOLUMES_DIR" -xzf "$ARCHIVE_PATH"

if [ ! -d "$VOLUME_DIR" ]; then
  echo "Restore did not recreate expected volume directory: $VOLUME_DIR" >&2
  exit 1
fi