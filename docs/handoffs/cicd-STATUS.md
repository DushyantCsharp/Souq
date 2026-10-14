# cicd status (stopped on usage limit)
- Workflows: `.github/workflows/{ci,eval,release}.yml` plus composite actions `.github/actions/{setup,eval-inputs}`. actionlint 1.7.12 passes (brew and docker image), shellcheck passes; third-party actions SHA-pinned.
- ci.yml: node/python matrix with turbo `...{scope}[origin/<base>]`, ci/ tooling, integration on service containers, migration round-trip and reindex dry-run, contract drift-check, Docker lint/check/build, plus a `ci ok` aggregator job.
- ci/ scripts: eval_compare, verify_checksums, fetch_models, check_image_size, migrations_roundtrip.sh, fetch_eval_baseline.sh, upsert_pr_comment.sh. 70 pytest tests pass; ruff and mypy --strict are clean. Round-trip verified on scratch DBs, including the contract cycle.
- Dockerfiles + Dockerfile.dockerignore whitelists for all six images; `docker-bake.hcl` (`--print` parses). hadolint and `bake --call check` are clean.
- Built locally (arm64; uncompressed size = docker history sum): gateway 261 MiB, storefront 209, indexer 391, feeds 419, outbox-relay 393, search 453 (no models). Gateway, indexer and search HEALTHCHECKs were seen passing.
- LEFT 1: release.yml bake steps need `allow: fs.read=${{ runner.temp }}/models` (buildx entitlement). The local baked-models build failed with "additional privileges requested", so search/indexer images with baked models are still unmeasured.
- LEFT 2: update `ci/image-budgets.json` (indexer/feeds/relay are over 300 today). Suggested values: gateway 325, storefront 275, feeds 500, relay 475, indexer 1100, search 1350.
- LEFT 3: `models/checksums.txt` and the `models-v1` release assets (search agent). Until they exist, eval uses the fake embedder and release fails for search/indexer.
- Pending handoffs: workers (feeds startup blocks /healthz while the DB is down), scaffold (stale indexer comment, memory limits), search (tarball procedure, optional onnxruntime extra).
