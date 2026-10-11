#!/usr/bin/env python3
"""Convert a Pegasus DL catalog (pegasus-ps4-catalog.json) into an SSPI .gssource.

SSPI (github.com/Xyhlo/SSPI) imports a ZIP with this layout (engine embedded-catalog-v1):

    source.json            descriptor + SHA-256/size manifest of every other file
    catalog.json           format header, file lists, counts
    index/titles-NNN.json  <= 500 titles per file: id, name, region, icon, shard
    packages/pkgs-NNN.json one record per package choice, keyed by title id

Limits enforced by SSPI (app/Package*.cs): <= 63 payload files, 4 MiB per file,
8 MiB archive, 1.5 MB per package shard, deflate ratio <= 20:1, no code payloads.

Usage: pegasus_to_sspi.py INPUT.json OUTPUT.gssource [--id ID] [--version X.Y.Z]
"""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

INDEX_PAGE = 500  # SSPI: "titles" array cap per index file
SHARD_TITLES = 300  # keeps every shard far below the 1.5 MB read limit
REGIONS = {"USA": "US", "EUR": "EU", "JAP": "JP", "JPN": "JP", "ASI": "AS", "ASIA": "AS"}
TITLE_ID = re.compile(r"[A-Z]{4}[0-9]{5}")


def dump(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")


def parse_description(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip()
    return out


def convert(packages: list) -> tuple[list, dict]:
    """Return (index rows sorted by name, {titleId: [package choices]})."""
    rows, choices = [], {}
    for pkg in packages:
        tid = pkg["titleId"].strip().upper()
        if not TITLE_ID.fullmatch(tid) or tid.startswith("PPSA"):
            raise SystemExit(f"unsupported title id {tid!r} (SSPI accepts PS4-style ids only)")
        if tid in choices:
            raise SystemExit(f"duplicate title id {tid}")
        links = pkg["downloadLinks"]
        if not links:
            raise SystemExit(f"{tid} has no download links")
        meta = parse_description(pkg.get("description", ""))
        region = REGIONS.get(meta.get("region", "").upper(), meta.get("region", "").upper())
        # Every link is a full pkg on archive.org; the catalog states patches and DLC are merged.
        recs = []
        for n, link in enumerate(links, 1):
            version = pkg.get("version") or meta.get("version", "")
            rec = {
                "id": f"{tid}-base-{n}",
                "titleId": tid,
                "region": region,
                "kind": "base",
                "url": link["url"],
                "label": link["name"],
                "hoster": "archive.org",
                "accessType": "Direct",
            }
            if version:
                rec["version"] = version
            if meta.get("min fw"):
                rec["requiredFirmware"] = meta["min fw"]
            recs.append(rec)
        choices[tid] = recs
        row = {
            "titleId": tid,
            "name": pkg["title"].strip(),
            "region": region,
            "releaseCount": len(recs),
            "kinds": ["base"],
        }
        if pkg.get("posterUrl"):
            row["icon"] = pkg["posterUrl"]
        rows.append(row)
    rows.sort(key=lambda r: (r["name"].casefold(), r["titleId"]))
    return rows, choices


def build(src: dict, source_id: str, version: str) -> dict[str, bytes]:
    rows, choices = convert(src["packages"])
    files: dict[str, bytes] = {}
    index_files, package_files = [], []
    shard_no = 0
    for page_no, start in enumerate(range(0, len(rows), INDEX_PAGE), 1):
        page = rows[start : start + INDEX_PAGE]
        page_name = f"index/titles-{page_no:03d}.json"
        for s in range(0, len(page), SHARD_TITLES):
            shard_no += 1
            chunk = page[s : s + SHARD_TITLES]
            shard_name = f"packages/pkgs-{shard_no:03d}.json"
            for row in chunk:
                row["packagesFile"] = shard_name
            files[shard_name] = dump({"titles": {r["titleId"]: choices[r["titleId"]] for r in chunk}})
            package_files.append(shard_name)
        files[page_name] = dump({"titles": page})
        index_files.append(page_name)

    files["catalog.json"] = dump(
        {
            "format": "embedded-catalog-v1",
            "formatVersion": 1,
            "searchNormalization": "nfkd-lower-alnum-spaces-v1",
            "indexFiles": index_files,
            "packageFiles": package_files,
            "counts": {
                "titles": len(rows),
                "readyTitles": len(rows),
                "releases": sum(len(v) for v in choices.values()),
                "multipartGroups": 0,
                "unresolved": 0,
            },
        }
    )
    manifest = {
        "schema": "gamesearch.source/v1",
        "id": source_id,
        "version": version,
        "name": src.get("name") or "PS4 FPKG Collection Catalog",
        "description": (
            f"{len(rows)} English PS4 FPKGs (base game with patches/DLC merged) hosted on "
            "archive.org (Maelly Pooh, Software Capsules). Direct links, no debrid needed."
        ),
        "homepage": "https://github.com/pattyboi/pegasus-ps4-collection-catalog",
        "engine": {"type": "embedded-catalog-v1", "entryFile": "catalog.json"},
        "capabilities": ["titles.search", "titles.catalog", "packages.resolve"],
        "files": [
            {"path": p, "size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
            for p, b in sorted(files.items())
        ],
    }
    files["source.json"] = dump(manifest)
    return files


def write_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in ["source.json", *sorted(n for n in files if n != "source.json")]:
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))  # fixed time: reproducible bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16  # regular file; SSPI rejects links
            z.writestr(info, files[name], compresslevel=9)
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.compress_size and info.file_size > info.compress_size * 20:
                raise SystemExit(f"{info.filename}: deflate ratio > 20:1, SSPI would reject it")
            if info.file_size > 4 * 1024 * 1024:
                raise SystemExit(f"{info.filename}: over SSPI's 4 MiB entry cap")
    if path.stat().st_size > 8 * 1024 * 1024:
        raise SystemExit("archive over SSPI's 8 MiB cap")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--id", default="ps4-fpkg-collection-english")
    ap.add_argument("--version", default="1.0.0")
    args = ap.parse_args()
    src = json.loads(args.input.read_text(encoding="utf-8"))
    files = build(src, args.id, args.version)
    write_zip(args.output, files)
    print(
        f"{args.output}: {args.output.stat().st_size} bytes, {len(files)} files, "
        f"{json.loads(files['catalog.json'])['counts']['titles']} titles",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
