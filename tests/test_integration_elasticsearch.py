"""
Integration tests for pfc-export-elasticsearch
================================================
Requires a running Elasticsearch instance (security disabled for testing).

Start with Docker:
  docker run -d --name es-test \\
    -e "discovery.type=single-node" \\
    -e "xpack.security.enabled=false" \\
    -p 9200:9200 \\
    docker.elastic.co/elasticsearch/elasticsearch:8.14.0

Wait ~30s for ES to start, then:
  python -m pytest tests/test_integration_elasticsearch.py -v

Set ES_URL env var to override the default URL:
  ES_URL=http://10.0.0.1:9200 python -m pytest tests/test_integration_elasticsearch.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ES_URL     = os.environ.get("ES_URL",        "http://localhost:9200")
PFC_BINARY = os.environ.get("PFC_JSONL_BINARY", shutil.which("pfc_jsonl") or "/usr/local/bin/pfc_jsonl")

try:
    from elasticsearch import Elasticsearch
    ES_AVAILABLE = True
except ImportError:
    ES_AVAILABLE = False

SKIP_REASON = "elasticsearch package not installed or ES not reachable"


def _es_ready(url: str, timeout: int = 30) -> bool:
    """Poll until ES is ready or timeout expires."""
    if not ES_AVAILABLE:
        return False
    from elasticsearch import Elasticsearch
    es = Elasticsearch(hosts=[url], verify_certs=False, request_timeout=5)
    for _ in range(timeout):
        try:
            es.cluster.health(wait_for_status="yellow", timeout="3s")
            return True
        except Exception:
            time.sleep(1)
    return False


def _pfc_available() -> bool:
    return bool(PFC_BINARY and os.path.isfile(PFC_BINARY) and os.access(PFC_BINARY, os.X_OK))


@unittest.skipUnless(ES_AVAILABLE and _es_ready(ES_URL, timeout=5), SKIP_REASON)
class TestIntegrationElasticsearch(unittest.TestCase):

    INDEX_NAME = "pfc-test-integration"

    @classmethod
    def setUpClass(cls):
        cls.es = Elasticsearch(hosts=[ES_URL], verify_certs=False)

        # Delete leftover test index if present
        try:
            cls.es.indices.delete(index=cls.INDEX_NAME, ignore_unavailable=True)
        except Exception:
            pass

        # Create test index with explicit mapping
        cls.es.indices.create(
            index=cls.INDEX_NAME,
            mappings={
                "properties": {
                    "@timestamp": {"type": "date"},
                    "level":      {"type": "keyword"},
                    "message":    {"type": "text"},
                    "host":       {"type": "keyword"},
                    "code":       {"type": "integer"},
                }
            },
        )

        # Index test documents
        docs = []
        for i in range(100):
            docs.append({
                "_index": cls.INDEX_NAME,
                "_source": {
                    "@timestamp": f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T{i % 24:02d}:00:00Z",
                    "level":      "INFO" if i % 3 != 0 else "ERROR",
                    "message":    f"Test log entry number {i}",
                    "host":       f"server-{i % 5}",
                    "code":       i,
                },
            })

        from elasticsearch.helpers import bulk
        bulk(cls.es, docs)
        cls.es.indices.refresh(index=cls.INDEX_NAME)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.es.indices.delete(index=cls.INDEX_NAME, ignore_unavailable=True)
        except Exception:
            pass

    def test_list_indices_returns_test_index(self):
        from pfc_export_elasticsearch import list_indices
        result = list_indices(self.es, pattern=self.INDEX_NAME)
        self.assertGreaterEqual(len(result), 1)
        names = [idx["index"] for idx in result]
        self.assertIn(self.INDEX_NAME, names)

    def test_list_indices_doc_count(self):
        from pfc_export_elasticsearch import list_indices
        result = list_indices(self.es, pattern=self.INDEX_NAME)
        idx = next(r for r in result if r["index"] == self.INDEX_NAME)
        self.assertEqual(idx["docs_count"], 100)

    def test_list_indices_health_field(self):
        from pfc_export_elasticsearch import list_indices
        result = list_indices(self.es, pattern=self.INDEX_NAME)
        idx = next(r for r in result if r["index"] == self.INDEX_NAME)
        self.assertIn(idx["health"], ("green", "yellow", "red"))

    def test_list_indices_pattern_filter(self):
        from pfc_export_elasticsearch import list_indices
        result = list_indices(self.es, pattern="pfc-test-*")
        names = [idx["index"] for idx in result]
        self.assertIn(self.INDEX_NAME, names)

    def test_list_indices_no_match(self):
        from pfc_export_elasticsearch import list_indices
        result = list_indices(self.es, pattern="nonexistent-xyz-*")
        self.assertEqual(result, [])

    @unittest.skipUnless(_pfc_available(), "pfc_jsonl binary not found")
    def test_export_all_documents(self):
        from pfc_export_elasticsearch import export_to_pfc
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "test.pfc"
            stats = export_to_pfc(
                es=self.es,
                index=self.INDEX_NAME,
                output_path=out,
                pfc_binary=PFC_BINARY,
                verbose=True,
            )

        self.assertEqual(stats["rows"], 100)
        self.assertGreater(stats["output_mb"], 0)
        # Note: ratio_pct > 100 is expected for tiny synthetic test data —
        # PFC block overhead dominates when input is only ~15 KB.

    @unittest.skipUnless(_pfc_available(), "pfc_jsonl binary not found")
    def test_export_time_range_filter(self):
        from pfc_export_elasticsearch import export_to_pfc
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "test_range.pfc"
            stats = export_to_pfc(
                es=self.es,
                index=self.INDEX_NAME,
                output_path=out,
                pfc_binary=PFC_BINARY,
                from_ts="2024-01-01T00:00:00",
                to_ts="2024-02-01T00:00:00",
            )

        # Only January docs — some docs fall in January
        self.assertGreater(stats["rows"], 0)
        self.assertLess(stats["rows"], 100)

    @unittest.skipUnless(_pfc_available(), "pfc_jsonl binary not found")
    def test_export_zero_range_returns_empty(self):
        from pfc_export_elasticsearch import export_to_pfc
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "test_empty.pfc"
            stats = export_to_pfc(
                es=self.es,
                index=self.INDEX_NAME,
                output_path=out,
                pfc_binary=PFC_BINARY,
                from_ts="2023-01-01T00:00:00",
                to_ts="2023-12-31T00:00:00",
            )

        self.assertEqual(stats["rows"], 0)

    @unittest.skipUnless(_pfc_available(), "pfc_jsonl binary not found")
    def test_export_include_id(self):
        from pfc_export_elasticsearch import export_to_pfc
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "test_id.pfc"
            stats = export_to_pfc(
                es=self.es,
                index=self.INDEX_NAME,
                output_path=out,
                pfc_binary=PFC_BINARY,
                include_id=True,
            )

        self.assertEqual(stats["rows"], 100)

    @unittest.skipUnless(_pfc_available(), "pfc_jsonl binary not found")
    def test_exported_pfc_is_valid_and_decompressible(self):
        from pfc_export_elasticsearch import export_to_pfc
        with tempfile.TemporaryDirectory() as tmpdir:
            out   = Path(tmpdir) / "test.pfc"
            stats = export_to_pfc(
                es=self.es,
                index=self.INDEX_NAME,
                output_path=out,
                pfc_binary=PFC_BINARY,
            )

            # Decompress and verify row count
            verify_out = Path(tmpdir) / "verify.jsonl"
            proc = subprocess.run(
                [PFC_BINARY, "decompress", str(out), str(verify_out)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

            with open(verify_out) as f:
                lines = f.readlines()

        self.assertEqual(len(lines), 100)
        row = json.loads(lines[0])
        self.assertIn("@timestamp", row)
        self.assertIn("message",    row)
        self.assertIn("level",      row)

    @unittest.skipUnless(_pfc_available(), "pfc_jsonl binary not found")
    def test_pfc_pipeline_with_gateway_query(self):
        """
        Pipeline test: ES → PFC → pfc_jsonl query (verifies block index works).
        Simulates what pfc-gateway or DuckDB would do downstream.
        """
        from pfc_export_elasticsearch import export_to_pfc
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "pipeline.pfc"
            export_to_pfc(
                es=self.es,
                index=self.INDEX_NAME,
                output_path=out,
                pfc_binary=PFC_BINARY,
            )

            # Query time range via pfc_jsonl
            proc = subprocess.run(
                [
                    PFC_BINARY, "query", str(out),
                    "--from", "2024-01-01T00:00:00",
                    "--to",   "2024-03-01T00:00:00",
                ],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

            # Parse result — skip non-JSON header lines (e.g. "[PFC-JSONL v3.4] Query: ...")
            rows = [
                json.loads(line)
                for line in proc.stdout.splitlines()
                if line.strip() and line.strip().startswith("{")
            ]
            for row in rows:
                ts = row.get("@timestamp", "")
                self.assertTrue(
                    ts >= "2024-01-01" and ts < "2024-03-01",
                    msg=f"Row timestamp {ts} outside queried range",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
