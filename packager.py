# packager.py
import os, zipfile
from typing import List

def build_zip_parts(
    files: List[str],
    out_dir: str,
    base_name: str,
    part_limit_bytes: int,
    extra_first: list[str] | None = None
) -> list[str]:
    """
    Create ZIP parts <= part_limit_bytes (stored, no compression).
    Returns list of zip paths. Docs in Part 1 if provided.
    """
    extra_first = extra_first or []
    parts: list[str] = []
    bundle: list[str] = []
    total_in_bundle = 0

    def flush_bundle(idx: int):
        if not bundle:
            return None
        zip_path = os.path.join(out_dir, f"{base_name}_part_{idx:02d}.zip")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for fp in bundle:
                zf.write(fp, os.path.basename(fp))
        return zip_path

    idx = 1
    # docs in part 1
    for doc in extra_first:
        size = os.path.getsize(doc)
        if total_in_bundle and (total_in_bundle + size) > part_limit_bytes:
            zp = flush_bundle(idx)
            if zp: parts.append(zp); idx += 1
            bundle, total_in_bundle = [], 0
        bundle.append(doc); total_in_bundle += size

    for fp in files:
        size = os.path.getsize(fp)
        if size > part_limit_bytes and len(files) == 1:
            return []  # single file exceeds limit
        if total_in_bundle and (total_in_bundle + size) > part_limit_bytes:
            zp = flush_bundle(idx)
            if zp: parts.append(zp); idx += 1
            bundle, total_in_bundle = [], 0
        bundle.append(fp); total_in_bundle += size

    zp = flush_bundle(idx)
    if zp: parts.append(zp)
    return parts
