# MGS Read-Only Search Projection

This directory contains the first isolated integration prototype for MGS and index.

The prototype is intentionally independent from MGS production files and services:

- It reads sanitized `ResourceSearchDocument` JSONL records.
- It builds a disposable SQLite FTS5 projection.
- It never opens or writes the MGS production database.
- It never receives crypt keys, tokens, cloud identifiers, ciphertext paths, or commands.
- It replaces the projection atomically after a successful build.

## Files

- `resource_search_document.schema.json`: versioned document contract.
- `sensitive-fields.json`: rejected field and value patterns.
- `projection.py`: validation, projection build, rebuild, and search implementation.
- `sample-documents.jsonl`: sanitized test data.
- `test_projection.py`: isolated unit tests.

## Usage

```powershell
python projection.py build sample-documents.jsonl projection.sqlite
python projection.py search projection.sqlite "PAR2"
python projection.py rebuild sample-documents.jsonl projection.sqlite
```

The projection is derived data. It can be deleted and rebuilt from the source JSONL without affecting MGS.
