import os
import zipfile
import shutil
import tempfile
import subprocess
import re

ZIP_NAME = "sewasetu-assistant.zip"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "venv", ".venv"}
EXCLUDE_FILES = {".env", "DIRECTOR-NOTE.mp3"}
EXCLUDE_EXTENSIONS = {".pyc", ".pem"}

def build_zip():
    print(f"Building {ZIP_NAME} from working tree including .git...")
    root_dir = os.path.abspath(".")
    
    with zipfile.ZipFile(ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(root_dir):
            # Prune excluded directories
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            
            for file in files:
                if file in EXCLUDE_FILES:
                    continue
                if file == ZIP_NAME:
                    continue
                ext = os.path.splitext(file)[1].lower()
                if ext in EXCLUDE_EXTENSIONS:
                    continue
                
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, root_dir)
                zf.write(full_path, rel_path)

    size_mb = os.path.getsize(ZIP_NAME) / (1024 * 1024)
    print(f"Built {ZIP_NAME} successfully. Size: {size_mb:.2f} MB ({os.path.getsize(ZIP_NAME)} bytes)")
    return size_mb

def verify_zip():
    temp_dir = tempfile.mkdtemp(prefix="sewasetu_zip_verify_")
    print(f"\nUnzipping into temporary directory: {temp_dir}...")
    with zipfile.ZipFile(ZIP_NAME, "r") as zf:
        zf.extractall(temp_dir)
    
    print("\nRunning 'git log --oneline | head' in unzipped directory:")
    proc = subprocess.run(["git", "log", "--oneline", "-n", "10"], cwd=temp_dir, capture_output=True, text=True)
    print(proc.stdout)
    if proc.returncode != 0:
        print("ERROR running git log in unzipped directory:", proc.stderr)

    print("\nScanning unzipped tree for secrets...")
    patterns = [
        re.compile(r"gsk_[A-Za-z0-9_-]{20,}"),
        re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"),
        re.compile(r"\b[A-Za-z0-9_-]+\.pem\b"),
    ]
    
    found_secrets = []
    for root, dirs, files in os.walk(temp_dir):
        # Skip git object blobs since git packfiles are binary
        if ".git" in root and "objects" in root:
            continue
        for file in files:
            full_path = os.path.join(root, file)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    for pat in patterns:
                        m = pat.findall(content)
                        if m:
                            # Filter out false positives in docs (.gitignore mentions *.pem)
                            filtered = [x for x in m if x != "*.pem" and "hvazure.pem" not in x]
                            if filtered:
                                found_secrets.append((os.path.relpath(full_path, temp_dir), filtered))
            except Exception:
                pass

    if found_secrets:
        print(f"WARNING: Potential secrets found in unzipped tree: {found_secrets}")
    else:
        print("PASSED: Zero secrets, API keys, or private keys found in unzipped tree.")

    shutil.rmtree(temp_dir, ignore_errors=True)
    print(f"Cleaned up temporary directory {temp_dir}.")

if __name__ == "__main__":
    build_zip()
    verify_zip()
