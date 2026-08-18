# -*- coding: utf-8 -*-
"""export_mgs_snapshot 的隔离测试：内存库模拟生产 schema，不触碰真实数据库。"""
import json
import sqlite3
import unittest

from export_mgs_snapshot import export_documents

SCHEMA = """
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT, code TEXT, jellyfin_type TEXT, cloud_path TEXT);
CREATE TABLE virtual_folders (id INTEGER PRIMARY KEY, full_path TEXT, category_id INTEGER, item_count INTEGER);
CREATE TABLE resources (id INTEGER PRIMARY KEY, category_id INTEGER, folder_id INTEGER,
    original_name TEXT, normalized_name TEXT, title TEXT, file_size INTEGER, file_ext TEXT,
    status TEXT, encrypted_at TEXT, uploaded_at TEXT, deleted_at TEXT);
CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE resource_tags (resource_id INTEGER, tag_id INTEGER);
CREATE TABLE resource_integrity (resource_id INTEGER, integrity_state TEXT, updated_at TEXT);
CREATE TABLE recovery_sets (recovery_set_id TEXT, resource_id INTEGER, ciphertext_size INTEGER,
    redundancy_percent REAL, recovery_file_count INTEGER, recovery_bytes INTEGER, state TEXT, created_at TEXT);
"""

SEED = """
INSERT INTO categories VALUES (1, '动漫', 'anime', 'tvshows', '/apps/MGS/Anime');
INSERT INTO virtual_folders VALUES (10, 'Anime/BDMV/魔女之旅', NULL, 2);
INSERT INTO resources VALUES
 (100, 1, 10, 'witch.zip', '', 'witch.zip', 1024, '.zip', 'uploaded_detected', NULL, '2026-01-01', NULL),
 (101, 1, 10, 'k-on.zip', 'K-ON!', 'k-on.zip', 2048, '.zip', 'pending', NULL, NULL, NULL),
 (102, 1, 10, 'deleted.zip', 'deleted.zip', 'deleted.zip', 8, '.zip', 'deleted', NULL, NULL, '2026-01-02');
INSERT INTO tags VALUES (7, '1080p');
INSERT INTO resource_tags VALUES (101, 7);
INSERT INTO resource_integrity VALUES (100, 'local_verified', '2026-02-01');
INSERT INTO recovery_sets VALUES ('set-a', 100, 4096, 10.0, 17, 512, 'uploaded_detected', '2026-02-02');
"""


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(SCHEMA)
        self.connection.executescript(SEED)

    def tearDown(self):
        self.connection.close()

    def _docs_by_id(self):
        return {doc["resource_id"]: doc for doc in export_documents(self.connection)}

    def test_document_counts_by_type(self):
        docs = export_documents(self.connection)
        by_type = {}
        for doc in docs:
            by_type[doc["resource_type"]] = by_type.get(doc["resource_type"], 0) + 1
        self.assertEqual(by_type.get("directory"), 2)  # 1 分类 + 1 虚拟目录
        self.assertEqual(by_type.get("file"), 2)  # 已删除资源被排除
        self.assertEqual(by_type.get("integrity"), 1)
        self.assertEqual(by_type.get("recovery"), 1)

    def test_null_category_folder_derives_from_cloud_root(self):
        docs = self._docs_by_id()
        folder_doc = docs["dir:10"]
        self.assertEqual(folder_doc["subtitle"], "动漫 目录")
        self.assertTrue(folder_doc["virtual_path"].startswith("动漫"))

    def test_empty_normalized_name_falls_back_to_title(self):
        docs = self._docs_by_id()
        self.assertEqual(docs["file:100"]["title"], "witch.zip")
        self.assertEqual(
            docs["integrity:100"]["title"], "完整性 · witch.zip"
        )
        self.assertEqual(
            docs["recovery:set-a"]["title"], "PAR2 恢复集 · witch.zip"
        )

    def test_tags_are_attached_to_file_documents(self):
        docs = self._docs_by_id()
        self.assertEqual(docs["file:101"]["tags"], ["1080p"])

    def test_no_sensitive_payload_in_documents(self):
        raw = json.dumps(list(export_documents(self.connection)), ensure_ascii=False)
        for forbidden in ("encrypted_name", "encrypted_path", "local_encrypted_path",
                          "baidu_fs_id", "remote_path", "local_directory", "remote_directory",
                          "/apps/MGS/", "password", "access_token"):
            self.assertNotIn(forbidden, raw)
        # 密文 SHA-256 / 恢复卷本地路径等一律不出现
        self.assertNotIn("set-a.par2", raw)

    def test_all_documents_pass_contract(self):
        from projection import validate_document
        for doc in export_documents(self.connection):
            validate_document(doc)  # 不抛异常即通过


if __name__ == "__main__":
    unittest.main()
