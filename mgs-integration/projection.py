import argparse
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path


SCHEMA_VERSION = "mgs-resource-search-v1"
ALLOWED_TYPES = {"file", "directory", "encryption_record", "integrity", "recovery", "archive"}
ALLOWED_ACTIONS = {
    "show_details",
    "open_location",
    "copy_display_name",
    "show_integrity_status",
    "show_recovery_status",
}
REQUIRED_FIELDS = {
    "schema_version",
    "resource_type",
    "resource_id",
    "title",
    "subtitle",
    "virtual_path",
    "tags",
    "media_type",
    "status_badges",
    "summary_fields",
    "available_actions",
}
def _load_sensitive_contract():
    contract_path = Path(__file__).with_name("sensitive-fields.json")
    with contract_path.open("r", encoding="utf-8") as handle:
        contract = json.load(handle)
    forbidden_keys = frozenset(key.lower() for key in contract["forbidden_keys"])
    forbidden_fragments = tuple(fragment.lower() for fragment in contract["forbidden_value_fragments"])
    return forbidden_keys, forbidden_fragments


FORBIDDEN_KEYS, FORBIDDEN_FRAGMENTS = _load_sensitive_contract()
RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


class ContractError(ValueError):
    pass


def _walk_sensitive(value, location="document"):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_KEYS:
                raise ContractError(f"forbidden field at {location}.{key}")
            _walk_sensitive(child, f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _walk_sensitive(child, f"{location}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.lower()
        for fragment in FORBIDDEN_FRAGMENTS:
            if fragment in lowered:
                raise ContractError(f"forbidden value at {location}")


def validate_document(document):
    if not isinstance(document, dict):
        raise ContractError("document must be an object")
    _walk_sensitive(document)
    if set(document) != REQUIRED_FIELDS:
        missing = sorted(REQUIRED_FIELDS - set(document))
        extra = sorted(set(document) - REQUIRED_FIELDS)
        raise ContractError(f"invalid fields missing={missing} extra={extra}")
    if document["schema_version"] != SCHEMA_VERSION:
        raise ContractError("unsupported schema version")
    if document["resource_type"] not in ALLOWED_TYPES:
        raise ContractError("unsupported resource type")
    resource_id = document["resource_id"]
    if not isinstance(resource_id, str) or not 1 <= len(resource_id) <= 200 or not RESOURCE_ID_RE.fullmatch(resource_id):
        raise ContractError("invalid resource id")
    for field in ("title", "subtitle", "virtual_path", "media_type"):
        if not isinstance(document[field], str) or len(document[field]) > 2000:
            raise ContractError(f"invalid {field}")
    for field in ("tags", "status_badges", "available_actions"):
        if not isinstance(document[field], list) or not all(isinstance(item, str) for item in document[field]):
            raise ContractError(f"invalid {field}")
    if not set(document["available_actions"]) <= ALLOWED_ACTIONS:
        raise ContractError("unsupported action")
    if not isinstance(document["summary_fields"], dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in document["summary_fields"].items()
    ):
        raise ContractError("invalid summary fields")
    return document


def read_documents(source):
    with Path(source).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                document = json.loads(line)
                yield validate_document(document)
            except (json.JSONDecodeError, ContractError) as exc:
                raise ContractError(f"line {line_number}: {exc}") from exc


def _flatten(document):
    summary = " ".join(f"{key} {value}" for key, value in sorted(document["summary_fields"].items()))
    return (
        document["resource_id"],
        document["resource_type"],
        document["title"],
        document["subtitle"],
        document["virtual_path"],
        " ".join(document["tags"]),
        document["media_type"],
        " ".join(document["status_badges"]),
        summary,
        json.dumps(document["available_actions"], ensure_ascii=False, separators=(",", ":")),
    )


def _create_database(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE projection_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE resources (
            resource_id TEXT PRIMARY KEY,
            resource_type TEXT NOT NULL,
            title TEXT NOT NULL,
            subtitle TEXT NOT NULL,
            virtual_path TEXT NOT NULL,
            tags TEXT NOT NULL,
            media_type TEXT NOT NULL,
            status_badges TEXT NOT NULL,
            summary TEXT NOT NULL,
            available_actions TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE resources_fts USING fts5(
            resource_id UNINDEXED,
            resource_type UNINDEXED,
            title,
            subtitle,
            virtual_path,
            tags,
            media_type,
            status_badges,
            summary,
            content='resources',
            content_rowid='rowid'
        );
        """
    )
    return connection


def build_projection(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    connection = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
        connection = _create_database(temporary)
        documents = list(read_documents(source))
        rows = [_flatten(document) for document in documents]
        connection.executemany(
            "INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        connection.executemany(
            "INSERT INTO resources_fts(rowid, resource_id, resource_type, title, subtitle, virtual_path, tags, media_type, status_badges, summary) "
            "SELECT rowid, resource_id, resource_type, title, subtitle, virtual_path, tags, media_type, status_badges, summary FROM resources WHERE resource_id = ?",
            [(row[0],) for row in rows],
        )
        connection.execute("INSERT INTO projection_meta VALUES (?, ?)", ("schema_version", SCHEMA_VERSION))
        connection.execute("INSERT INTO projection_meta VALUES (?, ?)", ("source_documents", str(len(rows))))
        connection.commit()
        connection.close()
        connection = None
        os.replace(temporary, destination)
        temporary = None
    finally:
        if connection is not None:
            connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _has_cjk(text):
    return any(ord(ch) >= 0x2E80 for ch in text)


def search_projection(database, query):
    """混合检索：ASCII 词走 FTS5 MATCH，CJK 词走 LIKE 子串匹配。

    FTS5 默认 unicode61 分词器把连续中文视为单 token，无法做子串命中，
    因此含中文的查询改用 LIKE 兜底（当前数据规模下性能足够）。
    """
    tokens = [token for token in query.split() if token]
    if not tokens:
        return []
    fts_tokens = [token.replace('"', "") for token in tokens if not _has_cjk(token)]
    fts_tokens = [token for token in fts_tokens if token]
    like_tokens = [token for token in tokens if _has_cjk(token)]
    if not fts_tokens and not like_tokens:
        return []
    conditions = []
    params = []
    if fts_tokens:
        match_expr = " AND ".join(f'"{token}"' for token in fts_tokens)
        conditions.append(
            "rowid IN (SELECT rowid FROM resources_fts WHERE resources_fts MATCH ?)"
        )
        params.append(match_expr)
    for token in like_tokens:
        conditions.append(
            "(title LIKE ? OR subtitle LIKE ? OR virtual_path LIKE ? OR tags LIKE ? OR summary LIKE ?)"
        )
        like = f"%{token}%"
        params.extend([like] * 5)
    connection = sqlite3.connect(database)
    rows = connection.execute(
        "SELECT resource_id, resource_type, title, subtitle, virtual_path, tags, status_badges, available_actions "
        f"FROM resources WHERE {' AND '.join(conditions)}",
        params,
    ).fetchall()
    connection.close()
    return rows


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="build the projection from a JSONL source")
    build.add_argument("source")
    build.add_argument("destination")
    rebuild = subparsers.add_parser("rebuild", help="rebuild the projection from a JSONL source (atomic replace)")
    rebuild.add_argument("source")
    rebuild.add_argument("destination")
    search = subparsers.add_parser("search")
    search.add_argument("database")
    search.add_argument("query")
    args = parser.parse_args()
    if args.command in ("build", "rebuild"):
        build_projection(args.source, args.destination)
        return 0
    for row in search_projection(args.database, args.query):
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
