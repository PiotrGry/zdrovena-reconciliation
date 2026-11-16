#!/usr/bin/env bash
# Roll a single-revision Container App back to a known revision.
#
# Usage: rollback-revision.sh <container-app> <resource-group> <target-revision> [bad-image]
#
# Revision history alone cannot tell which revision was healthy: after
# A (good) -> B (bad) -> rollback copy of A -> C (bad), "the previous revision"
# is B. So the caller names the target: the deploy records the revision that
# was active before it changed anything, and a manual rollback names one.
#
# The whole template of the target (image, env, resources) is copied into a new
# revision, so a configuration-only regression is rolled back too. If
# [bad-image] is given, a target that runs that image is refused.
set -euo pipefail

APP="${1:?container app name}"
RG="${2:?resource group}"
TARGET="${3:-}"
BAD_IMAGE="${4:-}"

if [[ -z "$TARGET" ]]; then
  echo "::error::No rollback target revision given; manual intervention required."
  exit 1
fi

TARGET_IMAGE=$(az containerapp revision show \
  --name "$APP" --resource-group "$RG" --revision "$TARGET" \
  --query "properties.template.containers[0].image" -o tsv) \
  || { echo "::error::Revision $TARGET not found."; exit 1; }
if [[ -n "$BAD_IMAGE" && "$TARGET_IMAGE" == "$BAD_IMAGE" ]]; then
  echo "::error::Revision $TARGET runs the image being rolled back ($BAD_IMAGE); manual intervention required."
  exit 1
fi

echo "Rolling back: copying revision $TARGET ($TARGET_IMAGE)"
az containerapp revision copy \
  --name "$APP" --resource-group "$RG" \
  --from-revision "$TARGET" \
  --revision-suffix "rb-$(date +%s)" \
  --output none
echo "Rollback complete: new revision copied from $TARGET"
