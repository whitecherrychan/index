import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from projection import ContractError, build_projection, main, search_projection, validate_document


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "documents.jsonl"
        self.database = self.root / "projection.sqlite"
        self.source.write_text(
            json.dumps(
                {
                    "schema_version": "mgs-resource-search-v1",
                    "resource_type": "recovery",
                    "resource_id": "recovery.test-001",
                    "title": "测试 PAR2",
                    "subtitle": "10% 冗余",
                    "virtual_path": "Recovery / Test",
                    "tags": ["PAR2", "测试"],
                    "media_type": "application/x-par2",
                    "status_badges": ["验证通过"],
                    "summary_fields": {"来源": "模拟"},
                    "available_actions": ["show_recovery_status"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_build_and_search(self):
        build_projection(self.source, self.database)
        rows = search_projection(self.database, "PAR2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "recovery.test-001")

    def test_rebuild_replaces_previous_projection(self):
        build_projection(self.source, self.database)
        self.source.write_text("", encoding="utf-8")
        build_projection(self.source, self.database)
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 0)
        finally:
            connection.close()

    def test_rebuild_cli_replaces_previous_projection(self):
        build_projection(self.source, self.database)
        self.source.write_text("", encoding="utf-8")
        with mock.patch(
            "sys.argv",
            ["projection.py", "rebuild", str(self.source), str(self.database)],
        ):
            self.assertEqual(main(), 0)
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 0)
        finally:
            connection.close()

    def test_sensitive_fields_are_rejected(self):
        document = json.loads(self.source.read_text(encoding="utf-8"))
        document["summary_fields"]["fs_id"] = "123"
        with self.assertRaises(ContractError):
            validate_document(document)

    def test_unknown_action_is_rejected(self):
        document = json.loads(self.source.read_text(encoding="utf-8"))
        document["available_actions"] = ["delete_file"]
        with self.assertRaises(ContractError):
            validate_document(document)

    def test_cjk_substring_search(self):
        build_projection(self.source, self.database)
        rows = search_projection(self.database, "测试")
        self.assertEqual(len(rows), 1)
        rows = search_projection(self.database, "PAR2 测试")
        self.assertEqual(len(rows), 1)
        rows = search_projection(self.database, "不存在词")
        self.assertEqual(rows, [])

    def test_mixed_ascii_and_cjk_query(self):
        build_projection(self.source, self.database)
        rows = search_projection(self.database, "PAR2 冗余")
        self.assertEqual(len(rows), 1)
        rows = search_projection(self.database, "PAR2 无关词")
        self.assertEqual(rows, [])

    def test_fts_query_injection_is_contained(self):
        build_projection(self.source, self.database)
        # 用户输入的引号被剥离，恶意 FTS 语法退化为字面短语 AND 组合
        rows = search_projection(self.database, '"PAR2" OR 1=1')
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
