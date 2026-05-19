#!/usr/bin/env bash
# Promotes staging image to production.
# If staging image not found, builds directly from current directory.
#
# Usage:
#   ./promote-image.sh <acr-login-server> <staging-sha> [<prod-sha>]
#
#   staging-sha: SHA of the develop branch tip (HEAD^2 of merge commit)
#   prod-sha:    SHA to tag the production image with (github.sha / merge commit)
#                Defaults to staging-sha when they are the same (non-merge pushes).
set -euo pipefail

ACR="${1:?Podaj ACR login server (np. zdrovenaacr.azurecr.io)}"
STAGING_SHA="${2:?Podaj staging git SHA}"
PROD_SHA="${3:-$STAGING_SHA}"

IMAGE="$ACR/zdrovena-api"

if docker manifest inspect "$IMAGE:staging-$STAGING_SHA" > /dev/null 2>&1; then
    # Happy path: promote the image that was tested on staging (same binary).
    docker pull "$IMAGE:staging-$STAGING_SHA"
    docker tag  "$IMAGE:staging-$STAGING_SHA" "$IMAGE:$STAGING_SHA"
    docker tag  "$IMAGE:staging-$STAGING_SHA" "$IMAGE:$PROD_SHA"
    docker tag  "$IMAGE:staging-$STAGING_SHA" "$IMAGE:latest"
    docker push "$IMAGE:$STAGING_SHA"
    [ "$PROD_SHA" != "$STAGING_SHA" ] && docker push "$IMAGE:$PROD_SHA"
    docker push "$IMAGE:latest"
    echo "Promoted: $IMAGE:staging-$STAGING_SHA → $IMAGE:$PROD_SHA"
else
    # Fallback: no staging image — build directly from source.
    # Happens on direct pushes to main (bypassing full-test-suite PR flow).
    echo "WARNING: No staging image for $STAGING_SHA — building directly from source"
    echo "         To avoid this, open a PR from develop → main instead of pushing directly."
    docker buildx build \
        --platform linux/amd64 \
        --cache-from type=gha \
        --cache-to  type=gha,mode=max \
        --label "git.sha=$PROD_SHA" \
        -t "$IMAGE:$STAGING_SHA" \
        -t "$IMAGE:$PROD_SHA" \
        -t "$IMAGE:latest" \
        --push \
        .
    echo "Built and pushed: $IMAGE:$PROD_SHA"
fi

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "image=$IMAGE:$PROD_SHA" >> "$GITHUB_OUTPUT"
fi
