# MGS 只读搜索投影

本目录是 MGS 与 index 融合的第一阶段隔离原型（详见 MGS 侧《MGS与index融合发展计划.md》）。

原型与 MGS 生产文件、服务刻意隔离：

- 读取脱敏后的 `ResourceSearchDocument` JSONL 记录。
- 构建可随时删除重建的 SQLite FTS5 投影。
- 投影本身绝不打开 MGS 生产库；可选导出器仅以只读（`mode=ro`）方式
  打开生产库，且只输出脱敏 JSONL。
- 全程不接收 crypt 密钥、Token、云端对象 ID、密文路径或命令。
- 投影构建成功后原子替换，失败保留旧投影。

## 文件

- `resource_search_document.schema.json`：版本化文档契约（JSON Schema）。
- `sensitive-fields.json`：敏感字段与敏感值拒绝清单。
- `projection.py`：契约校验、投影构建、重建与搜索实现。
- `export_mgs_snapshot.py`：生产库只读导出器（绝不写入生产库）。
- `sample-documents.jsonl`：脱敏模拟数据。
- `test_projection.py`、`test_export.py`：隔离单元测试。

## 使用方法

模拟数据闭环：

```powershell
python projection.py build sample-documents.jsonl projection.sqlite
python projection.py search projection.sqlite "PAR2"
python projection.py rebuild sample-documents.jsonl projection.sqlite
```

真实数据快照（对生产库只读）：

```powershell
python export_mgs_snapshot.py --output mgs-snapshot.jsonl
python projection.py rebuild mgs-snapshot.jsonl projection.sqlite
python projection.py search projection.sqlite "魔女"
```

每条文档写出前都会再过一次敏感字段契约校验，
因此映射错误不可能泄露密文路径、云端对象 ID 或密钥。

## 搜索语义

- ASCII 词走 FTS5 `MATCH`（引号包裹短语、`AND` 组合）。
- 中文词回退到 `LIKE` 子串匹配：默认 unicode61 分词器把连续中文
  视为单个 token，无法命中「魔女之旅」中的「魔女」这类子串。
- 混合查询对两种条件取交集。
- 用户输入中的双引号在加引号前被剥离，FTS 语法无法注入。

MGS 生产侧的阶段 3 已将该契约落地为 Tkinter 统一搜索卡片窗口
（`MGS Database/unified_search.py`），本目录继续作为契约源头与
跨实现一致性测试场所。
