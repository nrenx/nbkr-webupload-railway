"""
Upload a local folder (with all subfolders/files) to Supabase Storage bucket, preserving structure and overwriting existing files.

Requirements:
- pip install supabase tqdm
- Configure supabase_config.py with your credentials and bucket info.

Usage:
    python upload_folder_to_supabase.py
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from supabase import create_client, Client

try:
    import supabase_config
except ImportError:
    print("ERROR: Please create supabase_config.py with your Supabase credentials/config.")
    sys.exit(1)

SUPABASE_URL = supabase_config.SUPABASE_URL
SUPABASE_KEY = supabase_config.SUPABASE_KEY
BUCKET_NAME = getattr(supabase_config, "BUCKET_NAME", "student_data")  # Use the bucket from config or default
SOURCE_DIR = getattr(supabase_config, "SOURCE_DIR", "student_details")
WORKERS = getattr(supabase_config, "WORKERS", 32)

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_all_files(base_dir):
    """Yield (absolute_path, relative_path) for all files under base_dir."""
    for root, _, files in os.walk(base_dir):
        for file in files:
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, base_dir)
            yield abs_path, rel_path.replace(os.sep, "/")  # Use '/' for Supabase paths


def upload_file(abs_path, rel_path):
    """Upload a file to Supabase Storage, overwriting if it exists."""
    with open(abs_path, "rb") as f:
        data = f.read()
    # Remove if exists (Supabase will overwrite, but ensure consistency)
    try:
        supabase.storage.from_(BUCKET_NAME).remove([rel_path])
    except Exception:
        pass  # If it doesn't exist, ignore
    # Upload
    supabase.storage.from_(BUCKET_NAME).upload(rel_path, data)
    return rel_path


def main():
    if not os.path.isdir(SOURCE_DIR):
        print(f"ERROR: Source directory '{SOURCE_DIR}' not found.")
        sys.exit(1)

    files = list(get_all_files(SOURCE_DIR))
    print(f"Uploading {len(files)} files from '{SOURCE_DIR}' to bucket '{BUCKET_NAME}'...")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(upload_file, abs_path, rel_path) for abs_path, rel_path in files]
        for f in tqdm(as_completed(futures), total=len(futures), desc="Uploading"):
            try:
                f.result()
            except Exception as e:
                print(f"Error uploading file: {e}")

    print("Upload complete.")


if __name__ == "__main__":
    main()
