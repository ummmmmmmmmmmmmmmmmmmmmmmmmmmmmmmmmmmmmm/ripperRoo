import os, shutil, time, zipfile, tempfile

# -------------------- PROGRESS BAR --------------------
def progress_bar(percent: float, length: int = 10) -> str:
    filled = int(length * percent)
    return "[" + "■" * filled + "□" * (length - filled) + f"] {int(percent * 100)}%"

# -------------------- VALIDATION --------------------
def validate_link(link: str, allowed_domains: set[str]) -> bool:
    return any(domain in link for domain in allowed_domains)

# -------------------- ZIP HELPER --------------------
def zip_folder(folder_path: str) -> str:
    zip_path = folder_path.rstrip(os.sep) + ".zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(folder_path):
            for file in files:
                fp = os.path.join(root, file)
                arcname = os.path.relpath(fp, folder_path)
                zipf.write(fp, arcname)
    return zip_path

# -------------------- SAFE CLEANUP --------------------
def clean_dir(path: str):
    """Safely remove a directory, ignoring locked temp files still in use."""
    if not os.path.exists(path):
        return
    for _ in range(3):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return
        except PermissionError:
            time.sleep(1)
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            fp = os.path.join(root, f)
            try:
                os.unlink(fp)
            except PermissionError:
                if fp.endswith(".part"):
                    continue
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except Exception:
                pass
    try:
        os.rmdir(path)
    except Exception:
        pass

# -------------------- AUTO CLEANUP ON STARTUP --------------------
def auto_clean_temp(prefix: str = "ripperroo_", older_than_hours: float = 1.0):
    """Delete stale temporary ripperroo_* folders older than given age."""
    tempdir = tempfile.gettempdir()
    now = time.time()
    cutoff = older_than_hours * 3600

    for name in os.listdir(tempdir):
        if not name.startswith(prefix):
            continue
        path = os.path.join(tempdir, name)
        try:
            mtime = os.path.getmtime(path)
            if (now - mtime) > cutoff:
                shutil.rmtree(path, ignore_errors=True)
                print(f"🧹 Removed old temp folder: {path}")
        except Exception:
            pass
