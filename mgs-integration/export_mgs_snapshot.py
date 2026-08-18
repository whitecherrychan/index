# -*- coding: utf-8 -*-
"""从 MGS 生产数据库只读导出脱敏搜索文档（ResourceSearchDocument JSONL）。

安全边界（依据 MGS与index融合发展计划.md）：
- 仅以只读 URI 连接生产库，绝不写入。
- 不导出 encrypted_name / encrypted_path / local_encrypted_path /
  baidu_fs_id / remote_path_snapshot / local_directory / remote_directory /
  任何 Token、密码、crypt 配置。
- 每条文档经 projection.validate_document 二次校验后才写出。
"""
import argparse
import json
import sqlite3
from pathlib import Path

from projection import validate_document

DEFAULT_DB = r"D:\MGS\MGS Database\mgs_resources.db"

MEDIA_TYPE_BY_JELLYFIN = {
    "tvshows": "剧集",
    "movies": "电影",
    "music": "音乐",
    "musicvideos": "音乐视频",
    "books": "图书",
    "homevideos": "家庭视频",
    "mixed": "混合",
}
MEDIA_TYPE_BY_EXT = {
    ".mp4": "视频", ".mkv": "视频", ".avi": "视频", ".wmv": "视频", ".mov": "视频",
    ".m2ts": "视频", ".iso": "光盘镜像", ".bdmv": "视频",
    ".mp3": "音乐", ".flac": "音乐", ".wav": "音乐", ".ape": "音乐",
    ".zip": "压缩包", ".rar": "压缩包", ".7z": "压缩包",
    ".jpg": "图片", ".png": "图片", ".webp": "图片",
    ".pdf": "文档", ".epub": "图书",
}
STATUS_LABELS = {
    "pending": "待处理",
    "encrypting": "加密中",
    "encrypted": "已加密",
    "uploading": "上传中",
    "uploaded_detected": "已上传",
    "missing": "已缺失",
    "deleted": "已删除",
}
INTEGRITY_LABELS = {
    "local_verified": "本地已验证",
    "uploaded_detected": "云端已发现",
    "sample_verified": "抽样已验证",
    "failed": "验证失败",
    "unknown": "未知",
}
RECOVERY_LABELS = {
    "local_verified": "本地已验证",
    "uploading": "上传中",
    "uploaded_detected": "已上传",
    "pending": "待上传",
    "failed": "失败",
}


def _format_size(size):
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def _safe_text(value, fallback=""):
    return value if isinstance(value, str) and value else fallback


def export_documents(connection):
    """只读遍历生产库，产出已通过契约校验的搜索文档。"""
    cur = connection.cursor()
    lookup = connection.cursor()

    categories = {
        row[0]: {"name": _safe_text(row[1], "未分类"), "jellyfin_type": _safe_text(row[2])}
        for row in cur.execute("SELECT id, name, jellyfin_type FROM categories")
    }
    # 云端扫描产生的目录 category_id 可能为 NULL：用 full_path 根段对照分类 cloud_path 推导
    category_by_root = {}
    for row in cur.execute("SELECT id, name, jellyfin_type, cloud_path FROM categories"):
        cloud_root = _safe_text(row[3]).rstrip("/").rsplit("/", 1)[-1].lower()
        if cloud_root:
            category_by_root[cloud_root] = {"name": _safe_text(row[1], "未分类"), "jellyfin_type": _safe_text(row[2])}
    folders = {
        row[0]: {"full_path": _safe_text(row[1], "/"), "category_id": row[2], "item_count": row[3]}
        for row in cur.execute("SELECT id, full_path, category_id, item_count FROM virtual_folders")
    }
    tag_names = {}
    resource_tags = {}
    for resource_id, tag_id in cur.execute("SELECT resource_id, tag_id FROM resource_tags"):
        resource_tags.setdefault(resource_id, []).append(tag_id)
    for tag_id, name in cur.execute("SELECT id, name FROM tags"):
        tag_names[tag_id] = _safe_text(name)

    integrity = {
        row[0]: {"state": _safe_text(row[1]), "updated_at": _safe_text(row[2])}
        for row in cur.execute(
            "SELECT resource_id, integrity_state, updated_at FROM resource_integrity"
        )
    }
    recoveries = list(cur.execute(
        "SELECT recovery_set_id, resource_id, ciphertext_size, redundancy_percent, "
        "recovery_file_count, recovery_bytes, state, created_at FROM recovery_sets"
    ))

    def category_of(category_id):
        return categories.get(category_id) or {"name": "未分类", "jellyfin_type": ""}

    def category_of_folder(folder):
        if folder and folder["category_id"] in categories:
            return categories[folder["category_id"]]
        if folder:
            root = _safe_text(folder["full_path"]).split("/")[0].lower()
            if root in category_by_root:
                return category_by_root[root]
        return {"name": "未分类", "jellyfin_type": ""}

    def virtual_path_of(category_id, folder_id, name):
        category = category_of(category_id)
        folder = folders.get(folder_id)
        parts = [category["name"]]
        if folder and folder["full_path"]:
            parts.extend(part for part in folder["full_path"].split("/") if part)
        if name:
            parts.append(name)
        return " / ".join(parts)

    documents = []

    # 1. 分类 → directory
    for category_id, name, jellyfin_type in cur.execute(
        "SELECT id, name, jellyfin_type FROM categories ORDER BY id"
    ):
        documents.append(validate_document({
            "schema_version": "mgs-resource-search-v1",
            "resource_type": "directory",
            "resource_id": f"category:{category_id}",
            "title": _safe_text(name, "未命名分类"),
            "subtitle": "分类根目录",
            "virtual_path": _safe_text(name),
            "tags": [],
            "media_type": MEDIA_TYPE_BY_JELLYFIN.get(_safe_text(jellyfin_type), "混合"),
            "status_badges": [],
            "summary_fields": {
                "类型": "分类根目录",
                "媒体类型": MEDIA_TYPE_BY_JELLYFIN.get(_safe_text(jellyfin_type), "混合"),
            },
            "available_actions": ["show_details", "open_location"],
        }))

    # 2. 虚拟目录 → directory
    for folder_id, full_path, category_id, item_count in cur.execute(
        "SELECT id, full_path, category_id, item_count FROM virtual_folders ORDER BY id"
    ):
        folder = folders.get(folder_id)
        category = category_of_folder(folder)
        name = full_path.rsplit("/", 1)[-1] if full_path else "未命名目录"
        documents.append(validate_document({
            "schema_version": "mgs-resource-search-v1",
            "resource_type": "directory",
            "resource_id": f"dir:{folder_id}",
            "title": _safe_text(name, "未命名目录"),
            "subtitle": f"{category['name']} 目录",
            "virtual_path": f"{category['name']} / {_safe_text(full_path)}",
            "tags": [],
            "media_type": MEDIA_TYPE_BY_JELLYFIN.get(category["jellyfin_type"], "混合"),
            "status_badges": [],
            "summary_fields": {
                "登记子项数": str(item_count or 0),
                "所属分类": category["name"],
            },
            "available_actions": ["show_details", "open_location"],
        }))

    # 3. 资源 → file
    for row in cur.execute(
        "SELECT id, category_id, folder_id, normalized_name, title, file_ext, "
        "file_size, status, encrypted_at, uploaded_at, deleted_at "
        "FROM resources ORDER BY id"
    ):
        (resource_id, category_id, folder_id, normalized_name, title, file_ext,
         file_size, status, encrypted_at, uploaded_at, deleted_at) = row
        if deleted_at is not None:
            continue
        category = category_of(category_id)
        ext = _safe_text(file_ext).lower()
        media_type = MEDIA_TYPE_BY_JELLYFIN.get(category["jellyfin_type"]) or MEDIA_TYPE_BY_EXT.get(ext, "文件")
        tags = [tag_names[tag_id] for tag_id in resource_tags.get(resource_id, []) if tag_id in tag_names]
        badges = [STATUS_LABELS.get(_safe_text(status), _safe_text(status) or "未知")]
        summary = {
            "大小": _format_size(file_size),
            "类型": ext.lstrip(".") or "-",
            "分类": category["name"],
        }
        if encrypted_at:
            summary["加密时间"] = encrypted_at
        if uploaded_at:
            summary["上传时间"] = uploaded_at
        documents.append(validate_document({
            "schema_version": "mgs-resource-search-v1",
            "resource_type": "file",
            "resource_id": f"file:{resource_id}",
            "title": _safe_text(normalized_name, _safe_text(title, f"资源 {resource_id}")),
            "subtitle": _safe_text(title) if title != normalized_name else "",
            "virtual_path": virtual_path_of(category_id, folder_id, normalized_name),
            "tags": tags,
            "media_type": media_type,
            "status_badges": badges,
            "summary_fields": summary,
            "available_actions": ["show_details", "open_location", "copy_display_name"],
        }))

    def resource_title(resource_id):
        row = lookup.execute(
            "SELECT normalized_name, title, original_name FROM resources WHERE id = ?",
            (resource_id,),
        ).fetchone()
        if not row:
            return f"资源 {resource_id}"
        return _safe_text(row[0]) or _safe_text(row[1]) or _safe_text(row[2]) or f"资源 {resource_id}"

    # 4. 完整性记录 → integrity
    integrity_rows = list(cur.execute(
        "SELECT resource_id, integrity_state, updated_at FROM resource_integrity"
    ))
    for resource_id, state, updated_at in integrity_rows:
        source = lookup.execute(
            "SELECT file_size, category_id, folder_id FROM resources WHERE id = ?",
            (resource_id,),
        ).fetchone()
        title = resource_title(resource_id)
        documents.append(validate_document({
            "schema_version": "mgs-resource-search-v1",
            "resource_type": "integrity",
            "resource_id": f"integrity:{resource_id}",
            "title": f"完整性 · {title}",
            "subtitle": INTEGRITY_LABELS.get(_safe_text(state), _safe_text(state) or "未知"),
            "virtual_path": virtual_path_of(source[1], source[2], title) if source else "",
            "tags": [],
            "media_type": "完整性",
            "status_badges": [INTEGRITY_LABELS.get(_safe_text(state), _safe_text(state) or "未知")],
            "summary_fields": {
                "明文大小": _format_size(source[0]) if source else "-",
                "最近更新": _safe_text(updated_at),
            },
            "available_actions": ["show_details", "show_integrity_status"],
        }))

    # 5. PAR2 恢复集 → recovery
    for (set_id, resource_id, cipher_size, redundancy, volume_count,
         recovery_bytes, state, created_at) in recoveries:
        source = lookup.execute(
            "SELECT category_id, folder_id FROM resources WHERE id = ?",
            (resource_id,),
        ).fetchone()
        title = resource_title(resource_id)
        documents.append(validate_document({
            "schema_version": "mgs-resource-search-v1",
            "resource_type": "recovery",
            "resource_id": f"recovery:{set_id}",
            "title": f"PAR2 恢复集 · {title}",
            "subtitle": RECOVERY_LABELS.get(_safe_text(state), _safe_text(state) or "未知"),
            "virtual_path": virtual_path_of(source[0], source[1], title) if source else "",
            "tags": [],
            "media_type": "恢复卷",
            "status_badges": [RECOVERY_LABELS.get(_safe_text(state), _safe_text(state) or "未知")],
            "summary_fields": {
                "密文大小": _format_size(cipher_size),
                "冗余比例": f"{redundancy}%",
                "分卷数": str(volume_count),
                "恢复数据": _format_size(recovery_bytes),
                "创建时间": _safe_text(created_at),
            },
            "available_actions": ["show_details", "show_recovery_status"],
        }))

    return documents


def export_snapshot(db_path, output_path):
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        documents = export_documents(connection)
    finally:
        connection.close()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for document in documents:
            handle.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(documents)


def main():
    parser = argparse.ArgumentParser(description="Export sanitized MGS search documents (read-only).")
    parser.add_argument("--db", default=DEFAULT_DB, help="MGS production SQLite path (opened read-only)")
    parser.add_argument("--output", required=True, help="output JSONL path")
    args = parser.parse_args()
    count = export_snapshot(args.db, args.output)
    print(f"exported {count} documents -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
