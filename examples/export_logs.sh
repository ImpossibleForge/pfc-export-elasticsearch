#!/bin/bash
# Example: Export all logs-2024.* indices to a PFC archive

pfc-export-elasticsearch export \
    --url http://localhost:9200 \
    --api-key "your-api-key-here" \
    --index "logs-2024.*" \
    --from-ts "2024-01-01T00:00:00" \
    --to-ts   "2025-01-01T00:00:00" \
    --output  logs_2024.pfc \
    --verbose
