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
DOWNLOADED_PATH="$TMP_DIR/restore.tar.gz.enc.mac"
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
    --output "$DOWNLOADED_PATH" \
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

# The last 32 bytes are an HMAC-SHA256 of the rest (see backup.sh). Check it before
# decrypting, so a corrupted or altered archive is refused.
# The MAC key is derived from the passphrase over stdin, so the passphrase never appears
# in a command line. (The derived key does, briefly; it can't decrypt anything.)
MAC_KEY="$({ printf 'sage-archive-mac:'; cat "$PASSPHRASE_PATH"; } \
  | openssl dgst -sha256 -binary | od -An -tx1 | tr -d ' \n')"
SIZE="$(wc -c < "$DOWNLOADED_PATH")"
if [ "$SIZE" -le 32 ]; then
  echo "Backup archive is too short to be valid" >&2
  exit 1
fi
head -c "$((SIZE - 32))" "$DOWNLOADED_PATH" > "$ENCRYPTED_PATH"
EXPECTED_MAC="$(openssl dgst -sha256 -mac HMAC -macopt hexkey:"$MAC_KEY" -binary "$ENCRYPTED_PATH" | od -An -tx1 | tr -d ' \n')"
ACTUAL_MAC="$(tail -c 32 "$DOWNLOADED_PATH" | od -An -tx1 | tr -d ' \n')"
if [ "$EXPECTED_MAC" != "$ACTUAL_MAC" ]; then
  echo "Backup archive failed its integrity check: it is corrupted or altered, or was made with a different encryption key" >&2
  exit 1
fi

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