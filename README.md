# pfc-export-elasticsearch

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://python.org)
[![PFC-JSONL](https://img.shields.io/badge/PFC--JSONL-green.svg)](https://github.com/ImpossibleForge/pfc-jsonl)
[![Version](https://img.shields.io/badge/pfc--export--elasticsearch-v0.1.0-brightgreen.svg)](https://github.com/ImpossibleForge/pfc-export-elasticsearch/releases)

Export Elasticsearch indices to [PFC](https://github.com/ImpossibleForge/pfc-jsonl) cold-storage archives.

Stream documents from any Elasticsearch index (or index pattern like `logs-2024.*`) directly into a compressed `.pfc` archive — up to **90%+ smaller** than raw JSONL.

Uses the modern **search_after + Point-in-Time API** (ES 7.12+) for resource-efficient, stateless pagination. No scroll contexts, no cluster degradation. Supports **self-hosted Elasticsearch** and **Elastic Cloud** (Cloud ID).

---

## Why export from Elasticsearch?

Elasticsearch stores JSON documents in an inverted index — the actual storage overhead is typically **3–10× the raw data size**. Old indices you keep "just in case" silently drain your cluster resources and your budget.

`pfc-export-elasticsearch` gives you a clean exit path:

```
ES index storage:  300 GB  (inverted index + replicas)
Raw JSONL:         100 GB
PFC archive:         9 GB  (~9% of raw JSONL)
Savings:           ~97% vs. ES storage
```

The resulting `.pfc` archive is queryable via `pfc_jsonl query`, [pfc-gateway](https://github.com/ImpossibleForge/pfc-gateway), or DuckDB — no Elasticsearch required.

---

## Install

```bash
pip install pfc-export-elasticsearch
```

Or from source:

```bash
git clone https://github.com/ImpossibleForge/pfc-export-elasticsearch
cd pfc-export-elasticsearch
pip install -r requirements.txt
```

**The `pfc_jsonl` binary must be installed** (required for `export` subcommand):

```bash
# Linux x64:
curl -L https://github.com/ImpossibleForge/pfc-jsonl/releases/latest/download/pfc_jsonl-linux-x64 \
     -o /usr/local/bin/pfc_jsonl && chmod +x /usr/local/bin/pfc_jsonl

# macOS Apple Silicon (M1–M4):
curl -L https://github.com/ImpossibleForge/pfc-jsonl/releases/latest/download/pfc_jsonl-macos-arm64 \
     -o /usr/local/bin/pfc_jsonl && chmod +x /usr/local/bin/pfc_jsonl

# macOS Intel (x64):
curl -L https://github.com/ImpossibleForge/pfc-jsonl/releases/latest/download/pfc_jsonl-macos-x64 \
     -o /usr/local/bin/pfc_jsonl && chmod +x /usr/local/bin/pfc_jsonl

# Windows (x64) — PowerShell:
Invoke-WebRequest -Uri https://github.com/ImpossibleForge/pfc-jsonl/releases/latest/download/pfc_jsonl-windows-x64.exe `
    -OutFile "$env:LOCALAPPDATA\Microsoft\WindowsApps\pfc_jsonl.exe"
```

Requires Elasticsearch **7.12+** and `elasticsearch-py` **7.12–8.x**.

---

## Usage

### list — discover what's in your cluster

```bash
# Self-hosted
pfc-export-elasticsearch list --url http://localhost:9200 --api-key KEY

# Elastic Cloud
pfc-export-elasticsearch list --cloud-id "my-dep:dXMtZWFzdDQ..." --api-key KEY

# Filter by pattern, sort by size
pfc-export-elasticsearch list \
    --url http://localhost:9200 --api-key KEY \
    --pattern "logs-*" --sort size

# Machine-readable JSON output
pfc-export-elasticsearch list --url http://localhost:9200 --json
```

Example output:

```
Connected to Elasticsearch 8.17.0

  INDEX                               DOCS          SIZE    HEALTH   STATUS
  ────────────────────────────────────────────────────────────────────────────
  filebeat-8.14-2024-01-15-000001    1,234,567     2.3 gb  ● green   open
  logs-2024.01.15                    5,432,100     8.7 gb  ● green   open
  logs-2024.01.16                    4,891,234     7.8 gb  ◕ yellow  open
  ────────────────────────────────────────────────────────────────────────────
  3 indices  |  11,557,901 docs total
```

### export — archive to PFC

```bash
# Export all docs in an index pattern
pfc-export-elasticsearch export \
    --url http://localhost:9200 --api-key KEY \
    --index "logs-2024.*" --output logs_2024.pfc

# Export with time range
pfc-export-elasticsearch export \
    --url http://localhost:9200 --api-key KEY \
    --index "logs-2024.*" \
    --from-ts "2024-01-01T00:00:00" \
    --to-ts   "2025-01-01T00:00:00" \
    --output  logs_2024_full.pfc \
    --verbose

# Elastic Cloud with time range
pfc-export-elasticsearch export \
    --cloud-id "my-dep:dXMtZWFzdDQ..." --api-key KEY \
    --index "filebeat-*" \
    --from-ts "2024-01-01T00:00:00" \
    --to-ts   "2024-07-01T00:00:00" \
    --output  filebeat_h1_2024.pfc

# Include Elasticsearch document IDs in the output
pfc-export-elasticsearch export \
    --url http://localhost:9200 \
    --index "events-*" --include-id \
    --output events.pfc

# Custom timestamp field (non-ECS data)
pfc-export-elasticsearch export \
    --url http://localhost:9200 \
    --index "custom-app-*" --ts-field "event_time" \
    --output custom_app.pfc
```

---

## Authentication

| Method | Flags |
|--------|-------|
| **API key** (recommended) | `--api-key KEY` |
| **Basic auth** | `--user elastic --password changeme` |
| **Elastic Cloud** | `--cloud-id "dep:dXMt..." --api-key KEY` |
| **Custom TLS** | `--ca-certs /path/to/ca.crt` |
| **Dev/test (skip TLS)** | `--no-verify-certs` |

---

## Options reference

### Shared connection options (both subcommands)

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | `http://localhost:9200` | Elasticsearch URL |
| `--cloud-id` | — | Elastic Cloud deployment ID |
| `--api-key` | — | API key authentication |
| `--user` | — | Username for basic auth |
| `--password` | — | Password for basic auth |
| `--ca-certs` | — | CA certificate file path |
| `--no-verify-certs` | false | Disable TLS verification |

### list options

| Flag | Default | Description |
|------|---------|-------------|
| `--pattern` | `*` | Index name pattern to filter (e.g. `logs-*`) |
| `--sort` | `name` | Sort by: `name`, `docs`, `size` |
| `--json` | false | Raw JSON output |

### export options

| Flag | Default | Description |
|------|---------|-------------|
| `--index` | *(required)* | Index name or pattern (e.g. `logs-2024.*`) |
| `--output` | auto | Output `.pfc` file path |
| `--ts-field` | `@timestamp` | Timestamp field for sorting and filtering |
| `--from-ts` | — | Start of time range (ISO 8601, inclusive) |
| `--to-ts` | — | End of time range (ISO 8601, exclusive) |
| `--include-id` | false | Include `_id` as `_es_id` field in output |
| `--batch-size` | `1000` | Documents per search_after page |
| `--pfc-binary` | auto | Path to `pfc_jsonl` binary |
| `--verbose` / `-v` | false | Detailed progress output |

---

## How it works

1. Opens a **Point-in-Time snapshot** of the index — consistent view, no resource leak
2. Paginates through all documents with **search_after** (stateless, no scroll context held on the cluster)
3. Streams documents as **JSONL** to a temporary file
4. Compresses the JSONL to a **.pfc archive** using `pfc_jsonl compress`
5. The resulting `.pfc` file includes a **block-level timestamp index** (`.pfc.bidx`) for efficient time-range queries without full decompression

---

## Query the archive

```bash
# Time-range query (no Elasticsearch needed)
pfc_jsonl query logs_2024.pfc --from "2024-06-01" --to "2024-07-01"

# Via DuckDB
duckdb -c "
  INSTALL pfc FROM community; LOAD pfc;
  SELECT level, count(*) FROM pfc_read('logs_2024.pfc')
  WHERE \"@timestamp\" >= '2024-06-01'
  GROUP BY level;
"
```

---

## Running tests

```bash
# Unit tests (no Elasticsearch needed)
pip install pytest elasticsearch
python -m pytest tests/test_export.py -v

# Integration tests (requires Docker)
docker run -d --name es-test \
  -e "discovery.type=single-node" \
  -e "xpack.security.enabled=false" \
  -p 9200:9200 \
  docker.elastic.co/elasticsearch/elasticsearch:8.17.0

# Wait ~30s, then:
python -m pytest tests/test_integration_elasticsearch.py -v
```

---

## Part of the PFC Ecosystem

**[→ View all PFC tools & integrations](https://github.com/ImpossibleForge/pfc-jsonl#ecosystem)**

| Direct integration | Why |
|---|---|
| [pfc-archiver-elasticsearch](https://github.com/ImpossibleForge/pfc-archiver-elasticsearch) | Same DB, continuous daemon — auto-archives old indices on a schedule |
| [pfc-export-timescaledb](https://github.com/ImpossibleForge/pfc-export-timescaledb) | Same concept for TimescaleDB |
| [pfc-export-influxdb](https://github.com/ImpossibleForge/pfc-export-influxdb) | Same concept for InfluxDB |
| [pfc-gateway](https://github.com/ImpossibleForge/pfc-gateway) | Query exported archives via HTTP REST |
| [pfc-duckdb](https://github.com/ImpossibleForge/pfc-duckdb) | Query `.pfc` files directly from DuckDB |

---

## Disclaimer

pfc-export-elasticsearch is an independent open-source project and is not affiliated with, endorsed by, or associated with Elasticsearch B.V. or the Elastic project. Elasticsearch and Elastic Cloud are trademarks of Elasticsearch B.V.

---

## License

pfc-export-elasticsearch (this repository) is released under the MIT License — see [LICENSE](LICENSE).

The PFC-JSONL binary (`pfc_jsonl`) is proprietary software — free for personal and open-source use. Commercial use requires a license: [info@impossibleforge.com](mailto:info@impossibleforge.com)
