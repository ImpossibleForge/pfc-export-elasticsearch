# Changelog

## [0.1.0] — 2026-05-21

### Added
- `list` subcommand — list all Elasticsearch indices with doc counts, sizes, and health status
- `export` subcommand — export index documents to a `.pfc` archive via search_after + Point-in-Time API
- Support for index patterns (`logs-2024.*`, `filebeat-*`)
- Time-range filtering via `--from-ts` / `--to-ts` on any timestamp field
- `--ts-field` flag for non-standard timestamp fields (default: `@timestamp`)
- `--include-id` flag to embed Elasticsearch `_id` as `_es_id` in output
- Three authentication modes: API key, basic auth (user/password), Elastic Cloud (Cloud ID + API key)
- `--no-verify-certs` and `--ca-certs` for custom TLS setups
- `--json` output mode for `list` subcommand (machine-readable)
- `--sort` flag for `list` subcommand (name / docs / size)
- Auto-generated output filename from index name and time range
- 37 unit tests + 11 integration tests
