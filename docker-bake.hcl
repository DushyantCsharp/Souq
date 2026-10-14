# Image build definitions for every deployable. Build context is always the repo root; each Dockerfile has
# a sibling Dockerfile.dockerignore whitelist.
#
#   docker buildx bake                       # all images for the local platform, tagged souq/<name>:dev
#   docker buildx bake gateway indexer --load
#   docker buildx bake --print               # resolved config (CI checks this parses)
#   SEARCH_MODELS_DIR=/abs/verified/models docker buildx bake search   # bake ONNX models into search
#   (same for INDEXER_MODELS_DIR + indexer)
#
# CI (release.yml) layers docker/metadata-action's bake file on top, which overrides the per-image
# `meta-<name>` targets with GHCR tags + OCI labels, and sets platforms/cache/attestations via --set.

variable "REGISTRY" {
  default = "souq"
}

variable "TAG" {
  default = "dev"
}

# Comma-separated, e.g. "linux/amd64,linux/arm64". Empty = the builder's native platform.
variable "PLATFORMS" {
  default = ""
}

# Directories of checksum-verified ONNX artefacts (models/checksums.txt) to bake into /app/models.
# Empty = image without models (compose mounts models/artifacts at /app/models instead). search needs the
# reranker + embedder; the indexer embeds documents, so it needs the embedder only.
variable "SEARCH_MODELS_DIR" {
  default = ""
}

variable "INDEXER_MODELS_DIR" {
  default = ""
}

group "default" {
  targets = ["gateway", "storefront", "search", "indexer", "feeds", "outbox-relay"]
}

group "node" {
  targets = ["gateway", "storefront"]
}

group "python" {
  targets = ["search", "indexer", "feeds", "outbox-relay"]
}

target "_common" {
  context   = "."
  platforms = PLATFORMS == "" ? [] : split(",", PLATFORMS)
  labels = {
    "org.opencontainers.image.vendor" = "Souq"
  }
}

# Local defaults; metadata-action's bake file replaces these in CI (later definition files win).
target "meta-gateway" {
  tags = ["${REGISTRY}/gateway:${TAG}"]
}
target "meta-storefront" {
  tags = ["${REGISTRY}/storefront:${TAG}"]
}
target "meta-search" {
  tags = ["${REGISTRY}/search:${TAG}"]
}
target "meta-indexer" {
  tags = ["${REGISTRY}/indexer:${TAG}"]
}
target "meta-feeds" {
  tags = ["${REGISTRY}/feeds:${TAG}"]
}
target "meta-outbox-relay" {
  tags = ["${REGISTRY}/outbox-relay:${TAG}"]
}

target "gateway" {
  inherits   = ["_common", "meta-gateway"]
  dockerfile = "apps/gateway/Dockerfile"
}

target "storefront" {
  inherits   = ["_common", "meta-storefront"]
  dockerfile = "apps/storefront/Dockerfile"
}

target "search" {
  inherits   = ["_common", "meta-search"]
  dockerfile = "services/search/Dockerfile"
  # Overrides the Dockerfile's empty `models` stage with the verified artefact directory.
  contexts = SEARCH_MODELS_DIR == "" ? {} : { models = SEARCH_MODELS_DIR }
}

target "indexer" {
  inherits   = ["_common", "meta-indexer"]
  dockerfile = "services/indexer/Dockerfile"
  contexts   = INDEXER_MODELS_DIR == "" ? {} : { models = INDEXER_MODELS_DIR }
}

target "feeds" {
  inherits   = ["_common", "meta-feeds"]
  dockerfile = "services/feeds/Dockerfile"
}

target "outbox-relay" {
  inherits   = ["_common", "meta-outbox-relay"]
  dockerfile = "services/outbox-relay/Dockerfile"
}
