#!/usr/bin/env bash
# Promuje obraz staging-latest → <sha> + latest w ACR.
# Retag: build-once, promote — ten sam obraz co był testowany na stagingu.
#
# Użycie:
#   ./promote-image.sh <acr-login-server> <git-sha>
#
# Output: wypisuje pełny tag promowanego obrazu (do użycia w kolejnym kroku).
set -euo pipefail

ACR="${1:?Podaj ACR login server (np. zdrovenaacr.azurecr.io)}"
SHA="${2:?Podaj git SHA}"

IMAGE="$ACR/zdrovena-api"

docker pull "$IMAGE:staging-$SHA"
docker tag  "$IMAGE:staging-$SHA" "$IMAGE:$SHA"
docker tag  "$IMAGE:staging-$SHA" "$IMAGE:latest"
docker push "$IMAGE:$SHA"
docker push "$IMAGE:latest"

echo "Promoted: $IMAGE:$SHA"

# Jeśli uruchomiony w GitHub Actions — eksportuj output
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "image=$IMAGE:$SHA" >> "$GITHUB_OUTPUT"
fi
