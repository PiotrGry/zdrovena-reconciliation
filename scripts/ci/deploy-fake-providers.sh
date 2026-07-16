#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <image> [min_replicas] [max_replicas]" >&2
  exit 2
fi

APP="${FAKE_PROVIDER_APP:-zdrovena-fake-providers-staging}"
RG="${RG:-zdrovena-rg}"
IMAGE="$1"
MIN_REPLICAS="${2:-1}"
MAX_REPLICAS="${3:-1}"

CURRENT=$(az containerapp show --name "$APP" --resource-group "$RG" -o json)
APP_ID=$(jq -r '.id' <<<"$CURRENT")

BODY=$(jq \
  --arg image "$IMAGE" \
  --argjson min_replicas "$MIN_REPLICAS" \
  --argjson max_replicas "$MAX_REPLICAS" \
  '{
    properties: {
      configuration: {
        registries: .properties.configuration.registries
      },
      template: {
        containers: [
          {
            name: .properties.template.containers[0].name,
            image: $image,
            command: [
              "uvicorn",
              "zdrovena.fake_providers.app:app",
              "--host",
              "0.0.0.0",
              "--port",
              "9009"
            ],
            args: [],
            env: .properties.template.containers[0].env,
            resources: {
              cpu: .properties.template.containers[0].resources.cpu,
              memory: .properties.template.containers[0].resources.memory
            }
          }
        ],
        scale: {
          minReplicas: $min_replicas,
          maxReplicas: $max_replicas,
          rules: .properties.template.scale.rules
        }
      }
    }
  }' <<<"$CURRENT")

az rest \
  --method PATCH \
  --uri "https://management.azure.com${APP_ID}?api-version=2024-03-01" \
  --body "$BODY" \
  -o none
