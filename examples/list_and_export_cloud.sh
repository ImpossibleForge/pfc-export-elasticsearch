#!/bin/bash
# Example: Elastic Cloud — list indices, then export oldest ones

CLOUD_ID="my-deployment:dXMtZWFzdDQuZ2NwLmVsYXN0aWMtY2xvdWQuY29tOjQ0MyQ..."
API_KEY="your-api-key-here"

# Step 1: List all indices, sorted by size
pfc-export-elasticsearch list \
    --cloud-id "$CLOUD_ID" \
    --api-key  "$API_KEY" \
    --pattern  "logs-*" \
    --sort     size

# Step 2: Export a specific year to cold storage
pfc-export-elasticsearch export \
    --cloud-id "$CLOUD_ID" \
    --api-key  "$API_KEY" \
    --index    "logs-2023.*" \
    --output   logs_2023_archive.pfc \
    --verbose
