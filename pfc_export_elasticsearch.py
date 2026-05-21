#!/usr/bin/env python3
"""
pfc-export-elasticsearch v0.1.0 — Export Elasticsearch indices to PFC cold-storage archives.

Streams documents from one or more Elasticsearch indices directly into a compressed
.pfc archive with block-level timestamp index — ready for time-range queries via
DuckDB or pfc-gateway without loading the full archive.

Subcommands:
  list    List all indices in the cluster with doc counts and sizes.
  export  Export one or more indices to a .pfc archive.

Usage:
  pfc-export-elasticsearch list --url http://localhost:9200 --api-key KEY
  pfc-export-elasticsearch export --url http://localhost:9200 --api-key KEY \\
      --index "logs-2024.*" --output logs_2024.pfc

  # Elastic Cloud
  pfc-export-elasticsearch list --cloud-id "my-dep:dXMtZWFzdDQ..." --api-key KEY
  pfc-export-elasticsearch export --cloud-id "my-dep:dXMtZWFzdDQ..." --api-key KEY \\
      --index "logs-2024.*" --ts-field "@timestamp" \\
      --from-ts "2024-01-01T00:00:00" --to-ts "2024-07-01T00:00:00" \\
      --output logs_h1_2024.pfc --verbose

Requires: pip install elasticsearch
"""

__version__ = "0.1.0"

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from elasticsearch import Elasticsearch
except ImportError:
    Elasticsearch = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# PFC binary detection
# ---------------------------------------------------------------------------

def find_pfc_binary(override=None):
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        raise FileNotFoundError(f"pfc_jsonl binary not found at: {override}")

    env = os.environ.get("PFC_JSONL_BINARY")
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env

    default = "/usr/local/bin/pfc_jsonl"
    if os.path.isfile(default) and os.access(default, os.X_OK):
        return default

    found = shutil.which("pfc_jsonl")
    if found:
        return found

    return None


# ---------------------------------------------------------------------------
# Elasticsearch connection
# ---------------------------------------------------------------------------

def _connect(args) -> "Elasticsearch":
    """Build an Elasticsearch client from parsed CLI args."""
    if Elasticsearch is None:
        print(
            "ERROR: elasticsearch package not installed.\n"
            "Install: pip install elasticsearch",
            file=sys.stderr,
        )
        sys.exit(1)

    kwargs = {
        "request_timeout": 30,
        "retry_on_timeout": True,
        "max_retries": 3,
    }

    verify_certs = not getattr(args, "no_verify_certs", False)
    kwargs["verify_certs"] = verify_certs

    ca_certs = getattr(args, "ca_certs", None)
    if ca_certs:
        kwargs["ca_certs"] = ca_certs

    api_key  = getattr(args, "api_key",  None)
    user     = getattr(args, "user",     None)
    password = getattr(args, "password", None)

    if api_key:
        kwargs["api_key"] = api_key
    elif user:
        kwargs["basic_auth"] = (user, password or "")

    cloud_id = getattr(args, "cloud_id", None)
    if cloud_id:
        return Elasticsearch(cloud_id=cloud_id, **kwargs)

    url = getattr(args, "url", None) or "http://localhost:9200"
    return Elasticsearch(hosts=[url], **kwargs)


# ---------------------------------------------------------------------------
# list subcommand
# ---------------------------------------------------------------------------

def list_indices(es: "Elasticsearch", pattern: str = "*", sort_by: str = "name") -> list:
    """
    Return index metadata from the cluster.

    Returns a list of dicts with keys: index, docs_count, store_size, health, status.
    """
    cat_sort = {
        "name": "index:asc",
        "docs": "docs.count:desc",
        "size": "store.size_in_bytes:desc",
    }.get(sort_by, "index:asc")

    raw = es.cat.indices(
        index=pattern,
        format="json",
        h="index,docs.count,store.size,store.size_in_bytes,health,status",
        s=cat_sort,
    )

    result = []
    for item in raw:
        try:
            docs = int(item.get("docs.count") or 0)
        except (ValueError, TypeError):
            docs = 0
        try:
            size_bytes = int(item.get("store.size_in_bytes") or 0)
        except (ValueError, TypeError):
            size_bytes = 0
        result.append({
            "index":       item.get("index", ""),
            "docs_count":  docs,
            "store_size":  item.get("store.size", "?"),
            "size_bytes":  size_bytes,
            "health":      item.get("health", "?"),
            "status":      item.get("status", "?"),
        })
    return result


def cmd_list(args):
    """List all indices in the Elasticsearch cluster."""
    es = _connect(args)

    try:
        info     = es.info()
        es_ver   = info["version"]["number"]
    except Exception as exc:
        print(f"ERROR: Could not connect to Elasticsearch: {exc}", file=sys.stderr)
        sys.exit(1)

    pattern  = getattr(args, "pattern", None) or "*"
    sort_by  = getattr(args, "sort",    "name")
    as_json  = getattr(args, "json",    False)

    try:
        indices = list_indices(es, pattern=pattern, sort_by=sort_by)
    except Exception as exc:
        print(f"ERROR: Could not list indices: {exc}", file=sys.stderr)
        sys.exit(1)

    if as_json:
        print(json.dumps(indices, indent=2))
        return

    if not indices:
        print(f"No indices found matching '{pattern}'")
        return

    print(f"\nConnected to Elasticsearch {es_ver}\n")

    col_w = max((len(idx["index"]) for idx in indices), default=5)

    header = f"  {'INDEX':<{col_w}}  {'DOCS':>14}  {'SIZE':>10}  HEALTH   STATUS"
    sep    = "  " + "─" * (col_w + 42)
    print(header)
    print(sep)

    total_docs = 0
    for idx in indices:
        total_docs += idx["docs_count"]
        marker = {"green": "●", "yellow": "◕", "red": "○"}.get(idx["health"], "?")
        print(
            f"  {idx['index']:<{col_w}}  "
            f"{idx['docs_count']:>14,}  "
            f"{idx['store_size']:>10}  "
            f"{marker} {idx['health']:<7}  {idx['status']}"
        )

    print(sep)
    print(f"  {len(indices)} {'index' if len(indices) == 1 else 'indices'}  |  {total_docs:,} docs total\n")


# ---------------------------------------------------------------------------
# JSON encoder for ES _source documents
# ---------------------------------------------------------------------------

class _ESEncoder(json.JSONEncoder):
    """Handle edge-case types that can appear in ES _source documents."""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, bytes):
            return obj.hex()
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


# ---------------------------------------------------------------------------
# export subcommand — core
# ---------------------------------------------------------------------------

def export_to_pfc(
    es: "Elasticsearch",
    index: str,
    output_path: Path,
    pfc_binary: str,
    ts_field: str = "@timestamp",
    from_ts: str = None,
    to_ts: str = None,
    batch_size: int = 1_000,
    include_id: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Stream documents from an Elasticsearch index (or pattern) into a PFC archive.

    Uses search_after + Point-in-Time API for stateless, resource-efficient pagination.
    Requires Elasticsearch 7.12+ (for _shard_doc tiebreaker).

    Flow: ES PIT → search_after loop → JSONL temp file → pfc_jsonl compress → .pfc + .pfc.bidx

    Returns dict: rows, jsonl_mb, output_mb, ratio_pct, output
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build ES query
    if from_ts or to_ts:
        range_clause = {}
        if from_ts:
            range_clause["gte"] = from_ts
        if to_ts:
            range_clause["lt"] = to_ts
        query = {"range": {ts_field: range_clause}}
    else:
        query = {"match_all": {}}

    # Stable sort: timestamp asc + _shard_doc tiebreaker (PIT-specific, ES 7.12+)
    sort = [{ts_field: "asc"}, {"_shard_doc": "asc"}]

    if verbose:
        target = f"index '{index}'"
        if from_ts or to_ts:
            target += f"  [{from_ts or '∞'} → {to_ts or '∞'}]"
        print(f"  → Opening PIT for {target} ...")

    pit_resp = es.open_point_in_time(index=index, keep_alive="2m")
    pit_id   = pit_resp["id"]

    row_count    = 0
    jsonl_bytes  = 0
    search_after = None

    tmp_fd, tmp_jsonl = tempfile.mkstemp(suffix=".jsonl", prefix="pfc_es_")
    os.close(tmp_fd)

    try:
        if verbose:
            print(f"  → Streaming documents (batch: {batch_size:,}) ...")

        with open(tmp_jsonl, "w", encoding="utf-8") as fout:
            while True:
                call_kwargs = {
                    "query": query,
                    "size":  batch_size,
                    "pit":   {"id": pit_id, "keep_alive": "2m"},
                    "sort":  sort,
                }
                if search_after is not None:
                    call_kwargs["search_after"] = search_after

                resp   = es.search(**call_kwargs)
                pit_id = resp["pit_id"]
                hits   = resp["hits"]["hits"]

                if not hits:
                    break

                for hit in hits:
                    doc = dict(hit["_source"])

                    if include_id:
                        doc["_es_id"] = hit["_id"]

                    # pfc_jsonl recognises "timestamp" and "@timestamp" natively.
                    # For any other ts_field, add a "timestamp" alias so the block
                    # index and time-range queries work correctly.
                    if ts_field not in ("timestamp", "@timestamp"):
                        ts_val = doc.get(ts_field)
                        if ts_val is not None and "timestamp" not in doc and "@timestamp" not in doc:
                            doc["timestamp"] = ts_val

                    line = json.dumps(doc, cls=_ESEncoder, ensure_ascii=False) + "\n"
                    fout.write(line)
                    jsonl_bytes += len(line.encode("utf-8"))
                    row_count   += 1

                search_after = hits[-1]["sort"]

                if verbose and row_count % 100_000 == 0 and row_count > 0:
                    print(f"     {row_count:,} docs  ({jsonl_bytes / 1_048_576:.1f} MiB) ...")

        # Close PIT — non-fatal if it already expired
        try:
            es.close_point_in_time(id=pit_id)
        except Exception:
            pass

        if verbose:
            print(f"  → Exported {row_count:,} docs  ({jsonl_bytes / 1_048_576:.1f} MiB JSONL)")

        if row_count == 0:
            if verbose:
                print("  → 0 documents matched — no .pfc written.")
            return {"rows": 0, "jsonl_mb": 0, "output_mb": 0, "ratio_pct": 0,
                    "output": str(output_path)}

        if verbose:
            print("  → Compressing ...")

        proc = subprocess.run(
            [pfc_binary, "compress", tmp_jsonl, str(output_path)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"pfc_jsonl compress failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
            )

        jsonl_mb  = jsonl_bytes / 1_048_576
        output_mb = output_path.stat().st_size / 1_048_576
        ratio_pct = (output_mb / jsonl_mb * 100) if jsonl_mb > 0 else 0.0

        if verbose:
            print(
                f"  ✓ {row_count:,} docs  |  "
                f"JSONL {jsonl_mb:.1f} MiB  →  PFC {output_mb:.1f} MiB  "
                f"({ratio_pct:.1f}%)  →  {output_path.name}"
            )

        return {
            "rows":      row_count,
            "jsonl_mb":  jsonl_mb,
            "output_mb": output_mb,
            "ratio_pct": ratio_pct,
            "output":    str(output_path),
        }

    except Exception:
        try:
            es.close_point_in_time(id=pit_id)
        except Exception:
            pass
        raise

    finally:
        if os.path.exists(tmp_jsonl):
            os.unlink(tmp_jsonl)


def _sanitize_filename(s: str) -> str:
    """Replace characters unsafe in filenames with underscores."""
    return re.sub(r"[^\w\-.]", "_", s)


def cmd_export(args):
    """Export one or more Elasticsearch indices to a PFC archive."""
    try:
        pfc_binary = find_pfc_binary(getattr(args, "pfc_binary", None))
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if not pfc_binary:
        print(
            "ERROR: pfc_jsonl binary not found. Install from "
            "https://github.com/ImpossibleForge/pfc-jsonl/releases",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        parts = [_sanitize_filename(args.index)]
        if args.from_ts:
            parts.append(args.from_ts.replace(":", "").replace("-", "")[:8])
        if args.to_ts:
            parts.append(args.to_ts.replace(":", "").replace("-", "")[:8])
        output_path = Path("_".join(parts) + ".pfc")

    es = _connect(args)

    try:
        info   = es.info()
        es_ver = info["version"]["number"]
        if args.verbose:
            print(f"\nConnected to Elasticsearch {es_ver}")
            print(f"Exporting: {args.index}")
            print(f"Output:    {output_path}\n")
    except Exception as exc:
        print(f"ERROR: Could not connect to Elasticsearch: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        stats = export_to_pfc(
            es=es,
            index=args.index,
            output_path=output_path,
            pfc_binary=pfc_binary,
            ts_field=args.ts_field,
            from_ts=args.from_ts,
            to_ts=args.to_ts,
            batch_size=args.batch_size,
            include_id=args.include_id,
            verbose=args.verbose,
        )
        if not args.verbose:
            if stats["rows"] == 0:
                print("Done: 0 documents matched — no .pfc written.")
            else:
                print(
                    f"Done: {stats['rows']:,} docs  →  {stats['output']}"
                    f"  ({stats['ratio_pct']:.1f}% of JSONL)"
                )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Shared connection argument group
# ---------------------------------------------------------------------------

def _add_connection_args(parser):
    conn = parser.add_argument_group("connection")
    conn.add_argument(
        "--url", default=None, metavar="URL",
        help="Elasticsearch URL (default: http://localhost:9200). Ignored when --cloud-id is set.",
    )
    conn.add_argument(
        "--cloud-id", default=None, metavar="CLOUD_ID",
        help="Elastic Cloud deployment ID (e.g. 'my-dep:dXMtZWFzdDQ...'). Overrides --url.",
    )
    conn.add_argument(
        "--api-key", default=None, metavar="KEY",
        help="Elasticsearch API key. Recommended over --user/--password.",
    )
    conn.add_argument(
        "--user", default=None, metavar="USER",
        help="Username for basic auth.",
    )
    conn.add_argument(
        "--password", default=None, metavar="PASSWORD",
        help="Password for basic auth.",
    )
    conn.add_argument(
        "--ca-certs", default=None, metavar="PATH",
        help="Path to CA certificate file for TLS verification.",
    )
    conn.add_argument(
        "--no-verify-certs", action="store_true",
        help="Disable TLS certificate verification (use only in dev/test environments).",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="pfc-export-elasticsearch",
        description="Export Elasticsearch indices to PFC cold-storage archives.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # List all indices
  pfc-export-elasticsearch list --url http://localhost:9200 --api-key KEY

  # List with pattern filter (Elastic Cloud)
  pfc-export-elasticsearch list --cloud-id "my-dep:dXMtZWFzdDQ..." --api-key KEY --pattern "logs-*"

  # Export all docs in an index pattern
  pfc-export-elasticsearch export --url http://localhost:9200 --api-key KEY \\
      --index "logs-2024.*" --output logs_2024.pfc

  # Export time range (Elastic Cloud)
  pfc-export-elasticsearch export --cloud-id "my-dep:dXMtZWFzdDQ..." --api-key KEY \\
      --index "logs-2024.*" --from-ts "2024-01-01T00:00:00" --to-ts "2024-07-01T00:00:00" \\
      --output logs_h1_2024.pfc --verbose

  # Export with document IDs included
  pfc-export-elasticsearch export --url http://localhost:9200 \\
      --index "events-*" --include-id --output events.pfc

Install pfc_jsonl binary first (export subcommand only):
  curl -L https://github.com/ImpossibleForge/pfc-jsonl/releases/latest/download/pfc_jsonl-linux-x64 \\
       -o /usr/local/bin/pfc_jsonl && chmod +x /usr/local/bin/pfc_jsonl
        """,
    )
    parser.add_argument(
        "--version", action="version", version=f"pfc-export-elasticsearch {__version__}"
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # ── list ──────────────────────────────────────────────────────────────
    p_list = sub.add_parser(
        "list",
        help="List all indices in the Elasticsearch cluster.",
        description="List all indices in the Elasticsearch cluster with doc counts and sizes.",
    )
    _add_connection_args(p_list)
    p_list.add_argument(
        "--pattern", default=None, metavar="PATTERN",
        help="Index name pattern to filter (e.g. 'logs-*'). Default: all indices.",
    )
    p_list.add_argument(
        "--sort", default="name", choices=["name", "docs", "size"],
        help="Sort order: name (alphabetical), docs (most first), size (largest first). Default: name.",
    )
    p_list.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of a formatted table.",
    )

    # ── export ────────────────────────────────────────────────────────────
    p_export = sub.add_parser(
        "export",
        help="Export one or more indices to a .pfc archive.",
        description="Export Elasticsearch index documents to a compressed .pfc archive.",
    )
    _add_connection_args(p_export)
    p_export.add_argument(
        "--index", required=True, metavar="PATTERN",
        help="Index name or pattern (e.g. 'logs-2024.*', 'filebeat-*'). Wildcards are supported.",
    )
    p_export.add_argument(
        "--output", default=None, metavar="FILE",
        help="Output .pfc file path. Default: auto-generated from index name and time range.",
    )
    p_export.add_argument(
        "--ts-field", default="@timestamp", metavar="FIELD",
        help="Timestamp field for sorting and time-range filtering. Default: @timestamp.",
    )
    p_export.add_argument(
        "--from-ts", default=None, metavar="ISO_DATETIME",
        help="Start of time range (ISO 8601, inclusive). Example: 2024-01-01T00:00:00.",
    )
    p_export.add_argument(
        "--to-ts", default=None, metavar="ISO_DATETIME",
        help="End of time range (ISO 8601, exclusive). Example: 2025-01-01T00:00:00.",
    )
    p_export.add_argument(
        "--include-id", action="store_true",
        help="Include the Elasticsearch document _id as '_es_id' field in the output.",
    )
    p_export.add_argument(
        "--batch-size", type=int, default=1_000, metavar="N",
        help="Documents per search_after page (default: 1,000). Raise to 5,000–10,000 for large exports.",
    )
    p_export.add_argument(
        "--pfc-binary", default=None, metavar="PATH",
        help="Path to pfc_jsonl binary (default: auto-detect from PATH and /usr/local/bin/).",
    )
    p_export.add_argument("--verbose", "-v", action="store_true")

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "export":
        cmd_export(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
