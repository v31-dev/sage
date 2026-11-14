#!/bin/sh
set -eu

PLACEMENT_DIR="${PLACEMENT_DIR}"
# The archive holds its volume under that volume's id; it is restored into the volume `VOLUME_ID`, which may be another.
ARCHIVED_VOLUME_ID="${ARCHIVED_VOLUME_ID}"
VOLUME_ID="${VOLUME_ID}"
VOLUMES_DIR="$PLACEMENT_DIR/volumes"
VOLUME_DIR="$VOLUMES_DIR/$VOLUME_ID"
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
DOWNLOAD_CONFIG_PATH="$TMP_DIR/download.curl"

cat <<'__SAGE_RESTORE_KEY__' > "$PASSPHRASE_PATH"
${ENCRYPTION_KEY}
__SAGE_RESTORE_KEY__

# The link is a curl config file, so it stays out of curl's command line (visible to ps).
cat <<'__SAGE_DOWNLOAD_URL__' > "$DOWNLOAD_CONFIG_PATH"
url = "${DOWNLOAD_URL}"
__SAGE_DOWNLOAD_URL__

HTTP_STATUS="$(
  curl \
    --silent \
    --show-error \
    --output "$DOWNLOADED_PATH" \
    --write-out "%{http_code}" \
    --request GET \
    --config "$DOWNLOAD_CONFIG_PATH"
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
# in a command line.
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

mkdir "$TMP_DIR/unpacked"
tar -C "$TMP_DIR/unpacked" -xzf "$ARCHIVE_PATH"
if [ ! -d "$TMP_DIR/unpacked/$ARCHIVED_VOLUME_ID" ]; then
  echo "Backup archive does not hold the volume it was taken of" >&2
  exit 1
fi

rm -rf "$VOLUME_DIR"
mv "$TMP_DIR/unpacked/$ARCHIVED_VOLUME_ID" "$VOLUME_DIR"