#!/bin/sh
# Self-upgrade: pull the target image, swap SAGE_IMAGE_TAG in .env, and recreate
# sage; roll the tag back and restore if the new image fails its health check.
# Env in: NEW_TAG, PROJECT_DIR, PROJECT_NAME, CONFIG_FILES.
set -eu
cd "$PROJECT_DIR"

# Rebuild the exact `-f` chain the stack was created with (comma-separated label).
IFS=','; set -- $CONFIG_FILES; unset IFS
FILE_ARGS=""
for f in "$@"; do FILE_ARGS="$FILE_ARGS -f $f"; done
compose() { docker compose -p "$PROJECT_NAME" $FILE_ARGS "$@"; }

OLD_TAG=$(grep '^SAGE_IMAGE_TAG=' .env | head -n1 | cut -d= -f2- | tr -d "\"'")

SAGE_IMAGE_TAG="$NEW_TAG" compose pull sage
sed -i "s|^SAGE_IMAGE_TAG=.*|SAGE_IMAGE_TAG='$NEW_TAG'|" .env

if compose up -d --wait --wait-timeout 180 sage; then
  echo "sage upgraded to $NEW_TAG"
  exit 0
fi

echo "upgrade to $NEW_TAG failed its health check; rolling back to $OLD_TAG"
sed -i "s|^SAGE_IMAGE_TAG=.*|SAGE_IMAGE_TAG='$OLD_TAG'|" .env
compose up -d --wait --wait-timeout 180 sage
exit 1
