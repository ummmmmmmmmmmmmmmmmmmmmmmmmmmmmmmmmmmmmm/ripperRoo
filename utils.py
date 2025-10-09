import shutil, zipfile, os

def progress_bar(percent: float, length: int = 10) -> str:
    filled = int(length * percent)
    return "[" + "■"*filled + "□"*(length-filled) + f"] {int(percent*100)}%"

def validate_link(link: str, allowed_domains: set[str]) -> bool:
    return any(domain in link for domain in allowed_domains)

def zip_folder(folder_path: str) -> str:
    zip_path = folder_path.rstrip(os.sep) + ".zip"
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, folder_path)
                zipf.write(file_path, arcname)
    return zip_path

def clean_dir(path: str):
    try: shutil.rmtree(path)
    except FileNotFoundError: pass
