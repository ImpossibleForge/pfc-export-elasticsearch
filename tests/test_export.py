"""
Unit tests for pfc-export-elasticsearch
=========================================
All tests run without a live Elasticsearch instance — the elasticsearch
client is mocked throughout.

Run with:  python -m pytest tests/ -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from pfc_export_elasticsearch import (
    export_to_pfc,
    find_pfc_binary,
    list_indices,
    build_parser,
    _sanitize_filename,
    _ESEncoder,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hit(source: dict, doc_id: str = "abc123", sort_val=None):
    """Build a minimal ES search hit."""
    return {
        "_id":     doc_id,
        "_index":  "test-index",
        "_source": source,
        "sort":    sort_val or ["2024-01-01T00:00:00.000Z", 1],
    }


def _make_es_mock(pages: list[list]):
    """
    Build a mock Elasticsearch client that returns the given pages on successive
    search() calls (last call returns empty hits to signal end of results).
    """
    mock_es = MagicMock()
    mock_es.open_point_in_time.return_value = {"id": "pit_id_initial"}
    mock_es.close_point_in_time.return_value = {"succeeded": True}

    search_responses = []
    for i, page in enumerate(pages):
        search_responses.append({
            "pit_id": f"pit_id_{i + 1}",
            "hits":   {"hits": page},
        })
    # Terminal empty response
    search_responses.append({
        "pit_id": f"pit_id_{len(pages) + 1}",
        "hits":   {"hits": []},
    })
    mock_es.search.side_effect = search_responses
    return mock_es


# ---------------------------------------------------------------------------
# find_pfc_binary
# ---------------------------------------------------------------------------

class TestFindPfcBinary(unittest.TestCase):

    def test_override_valid_path(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            os.chmod(f.name, 0o755)
            result = find_pfc_binary(f.name)
            self.assertEqual(result, f.name)
        os.unlink(f.name)

    def test_override_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_pfc_binary("/nonexistent/pfc_jsonl")

    def test_env_var_used_when_set(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            os.chmod(f.name, 0o755)
            with patch.dict(os.environ, {"PFC_JSONL_BINARY": f.name}):
                result = find_pfc_binary()
            self.assertEqual(result, f.name)
        os.unlink(f.name)

    def test_returns_none_when_nothing_found(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("pfc_export_elasticsearch.shutil.which", return_value=None):
                with patch("os.path.isfile", return_value=False):
                    result = find_pfc_binary()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename(unittest.TestCase):

    def test_wildcards_replaced(self):
        self.assertEqual(_sanitize_filename("logs-2024.*"), "logs-2024._")

    def test_safe_chars_unchanged(self):
        self.assertEqual(_sanitize_filename("logs-2024.01"), "logs-2024.01")

    def test_spaces_replaced(self):
        result = _sanitize_filename("my index")
        self.assertNotIn(" ", result)

    def test_colon_replaced(self):
        result = _sanitize_filename("filebeat:2024")
        self.assertNotIn(":", result)


# ---------------------------------------------------------------------------
# _ESEncoder
# ---------------------------------------------------------------------------

class TestESEncoder(unittest.TestCase):

    def test_datetime_to_isoformat(self):
        from datetime import datetime, timezone
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = json.dumps({"ts": dt}, cls=_ESEncoder)
        self.assertIn("2024-01-15", result)

    def test_bytes_to_hex(self):
        result = json.dumps({"raw": b"\xca\xfe"}, cls=_ESEncoder)
        self.assertIn("cafe", result)

    def test_plain_types_unchanged(self):
        doc = {"a": 1, "b": "hello", "c": True, "d": None, "e": [1, 2], "f": {"x": 3}}
        result = json.loads(json.dumps(doc, cls=_ESEncoder))
        self.assertEqual(result, doc)


# ---------------------------------------------------------------------------
# export_to_pfc — ImportError guard
# ---------------------------------------------------------------------------

class TestExportImportGuard(unittest.TestCase):

    def test_connect_raises_when_elasticsearch_not_installed(self):
        """_connect() must exit when elasticsearch package is missing."""
        import types
        import argparse

        fake_args = argparse.Namespace(
            url="http://localhost:9200",
            cloud_id=None,
            api_key=None,
            user=None,
            password=None,
            ca_certs=None,
            no_verify_certs=False,
        )

        with patch("pfc_export_elasticsearch.Elasticsearch", None):
            with self.assertRaises(SystemExit):
                from pfc_export_elasticsearch import _connect
                _connect(fake_args)


# ---------------------------------------------------------------------------
# export_to_pfc — zero results
# ---------------------------------------------------------------------------

class TestExportZeroResults(unittest.TestCase):

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_no_hits_returns_zero_stats(self, mock_run):
        mock_es = _make_es_mock(pages=[[]])

        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=Path(tmpdir) / "out.pfc",
                pfc_binary="/fake/pfc_jsonl",
            )

        self.assertEqual(result["rows"], 0)
        self.assertEqual(result["jsonl_mb"], 0)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# export_to_pfc — document writing
# ---------------------------------------------------------------------------

class TestExportDocumentWriting(unittest.TestCase):

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_correct_jsonl_written(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        doc = {"@timestamp": "2024-01-01T00:00:00Z", "message": "hello", "level": "INFO"}
        mock_es = _make_es_mock(pages=[[_make_hit(doc)]])

        written_lines = []

        def capture(cmd, **kw):
            jsonl_path = cmd[2]
            with open(jsonl_path) as f:
                written_lines.extend(f.readlines())
            m = MagicMock()
            m.returncode = 0
            return m

        mock_run.side_effect = capture

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out.pfc"
            out.write_bytes(b"fake")
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=out,
                pfc_binary="/fake/pfc_jsonl",
            )

        self.assertEqual(len(written_lines), 1)
        row = json.loads(written_lines[0])
        self.assertEqual(row["message"], "hello")
        self.assertEqual(row["@timestamp"], "2024-01-01T00:00:00Z")

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_include_id_adds_es_id_field(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        doc = {"@timestamp": "2024-01-01T00:00:00Z", "msg": "test"}
        mock_es = _make_es_mock(pages=[[_make_hit(doc, doc_id="doc-xyz")]])

        written_lines = []

        def capture(cmd, **kw):
            with open(cmd[2]) as f:
                written_lines.extend(f.readlines())
            m = MagicMock()
            m.returncode = 0
            return m

        mock_run.side_effect = capture

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out.pfc"
            out.write_bytes(b"fake")
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=out,
                pfc_binary="/fake/pfc_jsonl",
                include_id=True,
            )

        row = json.loads(written_lines[0])
        self.assertEqual(row["_es_id"], "doc-xyz")

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_standard_timestamp_field_no_alias_needed(self, mock_run):
        """@timestamp is natively recognised by pfc_jsonl — no alias should be added."""
        mock_run.return_value = MagicMock(returncode=0)

        doc = {"@timestamp": "2024-01-01T00:00:00Z", "val": 42}
        mock_es = _make_es_mock(pages=[[_make_hit(doc)]])

        written_lines = []

        def capture(cmd, **kw):
            with open(cmd[2]) as f:
                written_lines.extend(f.readlines())
            m = MagicMock()
            m.returncode = 0
            return m

        mock_run.side_effect = capture

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out.pfc"
            out.write_bytes(b"fake")
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=out,
                pfc_binary="/fake/pfc_jsonl",
                ts_field="@timestamp",
            )

        row = json.loads(written_lines[0])
        # No extra "timestamp" alias added — @timestamp is sufficient
        self.assertNotIn("timestamp", row)
        self.assertIn("@timestamp", row)

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_custom_ts_field_adds_timestamp_alias(self, mock_run):
        """Non-standard ts_field should get a 'timestamp' alias for pfc_jsonl index."""
        mock_run.return_value = MagicMock(returncode=0)

        doc = {"event_time": "2024-01-01T00:00:00Z", "val": 1}
        mock_es = _make_es_mock(pages=[[_make_hit(doc)]])

        written_lines = []

        def capture(cmd, **kw):
            with open(cmd[2]) as f:
                written_lines.extend(f.readlines())
            m = MagicMock()
            m.returncode = 0
            return m

        mock_run.side_effect = capture

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out.pfc"
            out.write_bytes(b"fake")
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=out,
                pfc_binary="/fake/pfc_jsonl",
                ts_field="event_time",
            )

        row = json.loads(written_lines[0])
        self.assertIn("timestamp", row)
        self.assertIn("event_time", row)
        self.assertEqual(row["timestamp"], row["event_time"])


# ---------------------------------------------------------------------------
# export_to_pfc — query building
# ---------------------------------------------------------------------------

class TestExportQueryBuilding(unittest.TestCase):

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_no_filter_uses_match_all(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        mock_es = _make_es_mock(pages=[[]])

        with tempfile.TemporaryDirectory() as tmpdir:
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=Path(tmpdir) / "out.pfc",
                pfc_binary="/fake/pfc_jsonl",
            )

        first_call_kwargs = mock_es.search.call_args_list[0][1]
        self.assertEqual(first_call_kwargs["query"], {"match_all": {}})

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_from_ts_builds_range_query(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        mock_es = _make_es_mock(pages=[[]])

        with tempfile.TemporaryDirectory() as tmpdir:
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=Path(tmpdir) / "out.pfc",
                pfc_binary="/fake/pfc_jsonl",
                from_ts="2024-01-01T00:00:00",
            )

        first_call_kwargs = mock_es.search.call_args_list[0][1]
        query = first_call_kwargs["query"]
        self.assertIn("range", query)
        self.assertIn("gte", query["range"]["@timestamp"])
        self.assertEqual(query["range"]["@timestamp"]["gte"], "2024-01-01T00:00:00")

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_both_from_and_to_ts_in_range(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        mock_es = _make_es_mock(pages=[[]])

        with tempfile.TemporaryDirectory() as tmpdir:
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=Path(tmpdir) / "out.pfc",
                pfc_binary="/fake/pfc_jsonl",
                from_ts="2024-01-01T00:00:00",
                to_ts="2025-01-01T00:00:00",
            )

        kwargs = mock_es.search.call_args_list[0][1]
        rng = kwargs["query"]["range"]["@timestamp"]
        self.assertEqual(rng["gte"], "2024-01-01T00:00:00")
        self.assertEqual(rng["lt"],  "2025-01-01T00:00:00")

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_search_after_passed_on_second_page(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        sort_val = ["2024-01-01T00:00:00.000Z", 1]
        doc = {"@timestamp": "2024-01-01T00:00:00Z", "val": 1}
        mock_es = _make_es_mock(pages=[[_make_hit(doc, sort_val=sort_val)]])

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out.pfc"
            out.write_bytes(b"fake")
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=out,
                pfc_binary="/fake/pfc_jsonl",
            )

        # Second search call should have search_after set
        second_call_kwargs = mock_es.search.call_args_list[1][1]
        self.assertEqual(second_call_kwargs.get("search_after"), sort_val)

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_pit_id_refreshed_between_pages(self, mock_run):
        """PIT id returned by each search response should be used for the next call."""
        mock_run.return_value = MagicMock(returncode=0)

        doc = {"@timestamp": "2024-01-01T00:00:00Z"}
        mock_es = MagicMock()
        mock_es.open_point_in_time.return_value = {"id": "pit_v0"}
        mock_es.search.side_effect = [
            {"pit_id": "pit_v1", "hits": {"hits": [_make_hit(doc)]}},
            {"pit_id": "pit_v2", "hits": {"hits": []}},
        ]
        mock_es.close_point_in_time.return_value = {"succeeded": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out.pfc"
            out.write_bytes(b"fake")
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=out,
                pfc_binary="/fake/pfc_jsonl",
            )

        second_pit = mock_es.search.call_args_list[1][1]["pit"]["id"]
        self.assertEqual(second_pit, "pit_v1")


# ---------------------------------------------------------------------------
# export_to_pfc — compress handling
# ---------------------------------------------------------------------------

class TestExportCompress(unittest.TestCase):

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_compress_called_with_correct_binary_and_subcommand(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        doc = {"@timestamp": "2024-01-01T00:00:00Z"}
        mock_es = _make_es_mock(pages=[[_make_hit(doc)]])

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out.pfc"
            out.write_bytes(b"fake")
            export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=out,
                pfc_binary="/usr/local/bin/pfc_jsonl",
            )

        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "/usr/local/bin/pfc_jsonl")
        self.assertEqual(cmd[1], "compress")

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_compress_failure_raises_runtime_error(self, mock_run):
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr     = "compress error"
        mock_run.return_value = proc

        doc = {"@timestamp": "2024-01-01T00:00:00Z"}
        mock_es = _make_es_mock(pages=[[_make_hit(doc)]])

        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError):
                export_to_pfc(
                    es=mock_es,
                    index="test-index",
                    output_path=Path(tmpdir) / "out.pfc",
                    pfc_binary="/fake/pfc_jsonl",
                )

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_pit_closed_on_compress_failure(self, mock_run):
        """PIT must be closed even when compress fails."""
        proc = MagicMock(returncode=1, stderr="error")
        mock_run.return_value = proc

        doc = {"@timestamp": "2024-01-01T00:00:00Z"}
        mock_es = _make_es_mock(pages=[[_make_hit(doc)]])

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                export_to_pfc(
                    es=mock_es,
                    index="test-index",
                    output_path=Path(tmpdir) / "out.pfc",
                    pfc_binary="/fake/pfc_jsonl",
                )
            except RuntimeError:
                pass

        mock_es.close_point_in_time.assert_called()

    @patch("pfc_export_elasticsearch.subprocess.run")
    def test_multi_page_export_row_count(self, mock_run):
        """Verify row count across multiple search_after pages."""
        mock_run.return_value = MagicMock(returncode=0)

        docs = [{"@timestamp": f"2024-01-0{i+1}T00:00:00Z", "val": i} for i in range(3)]
        page1 = [_make_hit(docs[0], doc_id="1", sort_val=["2024-01-01T00:00:00Z", 1]),
                 _make_hit(docs[1], doc_id="2", sort_val=["2024-01-02T00:00:00Z", 2])]
        page2 = [_make_hit(docs[2], doc_id="3", sort_val=["2024-01-03T00:00:00Z", 3])]
        mock_es = _make_es_mock(pages=[page1, page2])

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "out.pfc"
            out.write_bytes(b"fake")
            stats = export_to_pfc(
                es=mock_es,
                index="test-index",
                output_path=out,
                pfc_binary="/fake/pfc_jsonl",
            )

        self.assertEqual(stats["rows"], 3)


# ---------------------------------------------------------------------------
# list_indices
# ---------------------------------------------------------------------------

class TestListIndices(unittest.TestCase):

    def test_returns_parsed_index_list(self):
        mock_es = MagicMock()
        mock_es.cat.indices.return_value = [
            {
                "index":                  "logs-2024.01.15",
                "docs.count":             "1000",
                "store.size":             "2.1mb",
                "store.size_in_bytes":    "2202009",
                "health":                 "green",
                "status":                 "open",
            },
            {
                "index":                  "logs-2024.01.16",
                "docs.count":             "2500",
                "store.size":             "4.8mb",
                "store.size_in_bytes":    "5033164",
                "health":                 "yellow",
                "status":                 "open",
            },
        ]

        result = list_indices(mock_es, pattern="logs-*")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["index"],      "logs-2024.01.15")
        self.assertEqual(result[0]["docs_count"], 1000)
        self.assertEqual(result[0]["health"],     "green")
        self.assertEqual(result[1]["docs_count"], 2500)

    def test_handles_missing_doc_count(self):
        mock_es = MagicMock()
        mock_es.cat.indices.return_value = [
            {
                "index":               "empty-index",
                "docs.count":          None,
                "store.size":          "0b",
                "store.size_in_bytes": "0",
                "health":              "green",
                "status":              "open",
            }
        ]

        result = list_indices(mock_es)
        self.assertEqual(result[0]["docs_count"], 0)


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

class TestCLIParser(unittest.TestCase):

    def test_list_subcommand_parsed(self):
        parser = build_parser()
        args = parser.parse_args(["list", "--url", "http://localhost:9200"])
        self.assertEqual(args.command, "list")
        self.assertEqual(args.url, "http://localhost:9200")

    def test_list_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["list"])
        self.assertEqual(args.sort, "name")
        self.assertFalse(args.json)
        self.assertIsNone(args.pattern)

    def test_list_pattern_and_sort(self):
        parser = build_parser()
        args = parser.parse_args(["list", "--pattern", "logs-*", "--sort", "docs"])
        self.assertEqual(args.pattern, "logs-*")
        self.assertEqual(args.sort,    "docs")

    def test_export_subcommand_required_args(self):
        parser = build_parser()
        args = parser.parse_args(["export", "--index", "logs-2024.*"])
        self.assertEqual(args.command, "export")
        self.assertEqual(args.index,   "logs-2024.*")

    def test_export_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["export", "--index", "logs-*"])
        self.assertEqual(args.ts_field,   "@timestamp")
        self.assertEqual(args.batch_size, 1_000)
        self.assertFalse(args.include_id)
        self.assertFalse(args.verbose)
        self.assertIsNone(args.from_ts)
        self.assertIsNone(args.to_ts)
        self.assertIsNone(args.output)

    def test_cloud_id_connection(self):
        parser = build_parser()
        args = parser.parse_args([
            "list",
            "--cloud-id", "my-dep:dXMtZWFzdDQ...",
            "--api-key",  "id:secret",
        ])
        self.assertEqual(args.cloud_id, "my-dep:dXMtZWFzdDQ...")
        self.assertEqual(args.api_key,  "id:secret")

    def test_basic_auth_args(self):
        parser = build_parser()
        args = parser.parse_args([
            "export", "--index", "logs-*",
            "--user", "elastic", "--password", "changeme",
        ])
        self.assertEqual(args.user,     "elastic")
        self.assertEqual(args.password, "changeme")

    def test_export_time_range_args(self):
        parser = build_parser()
        args = parser.parse_args([
            "export", "--index", "logs-*",
            "--from-ts", "2024-01-01T00:00:00",
            "--to-ts",   "2025-01-01T00:00:00",
        ])
        self.assertEqual(args.from_ts, "2024-01-01T00:00:00")
        self.assertEqual(args.to_ts,   "2025-01-01T00:00:00")

    def test_no_verify_certs_flag(self):
        parser = build_parser()
        args = parser.parse_args(["list", "--no-verify-certs"])
        self.assertTrue(args.no_verify_certs)

    def test_export_missing_index_fails(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["export"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
