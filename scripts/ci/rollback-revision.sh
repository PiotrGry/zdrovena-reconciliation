#!/usr/bin/env bash
# Roll a single-revision Container App back to the previous deployed revision.
#
# Usage: rollback-revision.sh <container-app> <resource-group>
#
# - Copies the whole template of the target revision (image, env, resources),
#   so a configuration-only change is rolled back too, not just the image.
# - Rollback copies get the suffix "rb-<epoch>" and are never picked as a
#   target. The target is the second-newest revision that is not a rollback
#   copy: after A (good) -> B (bad) -> rb (copy of A), it is still A. A retry or
#   a second manual run therefore copies A again instead of restoring B.
set -euo pipefail

APP="${1:?container app name}"
RG="${2:?resource group}"

TARGET=$(az containerapp revision list --all --name "$APP" --resource-group "$RG" -o json \
  | jq -r 'map(select(.name | test("--rb-") | not))
           | sort_by(.properties.createdTime) | .[-2].name // empty')
if [[ -z "$TARGET" ]]; then
  echo "::error::No previous deployed revision to roll back to; manual intervention required."
  exit 1
fi

echo "Rolling back: copying revision $TARGET"
az containerapp revision copy \
  --name "$APP" --resource-group "$RG" \
  --from-revision "$TARGET" \
  --revision-suffix "rb-$(date +%s)" \
  --output none
echo "Rollback complete: new revision copied from $TARGET"
