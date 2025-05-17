#!/usr/bin/env python3
"""
Direct Supabase Uploader

This script uploads data directly to Supabase Storage without storing it locally first.
It's designed to work with Render's ephemeral filesystem.
"""

import os
import sys
import json
import logging
import argparse
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Any, Union, Tuple

from tqdm import tqdm
from supabase import create_client, Client

try:
    import supabase_config
except ImportError:
    print("ERROR: Please create supabase_config.py with your Supabase credentials/config.")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("direct_supabase_uploader.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("direct_supabase_uploader")

# Supabase configuration
SUPABASE_URL = supabase_config.SUPABASE_URL
SUPABASE_KEY = supabase_config.SUPABASE_KEY
BUCKET_NAME = getattr(supabase_config.DEFAULT_SETTINGS, "bucket", "demo-usingfastapi")
SOURCE_DIR = getattr(supabase_config.DEFAULT_SETTINGS, "source_dir", "/tmp/student_details")
WORKERS = getattr(supabase_config.DEFAULT_SETTINGS, "workers", 32)

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def get_all_files(base_dir):
    """Yield (absolute_path, relative_path) for all files under base_dir."""
    base_dir = Path(base_dir)
    if not base_dir.exists():
        logger.warning(f"Source directory {base_dir} does not exist.")
        return

    for root, _, files in os.walk(base_dir):
        for file in files:
            abs_path = os.path.join(root, file)
            rel_path = os.path.relpath(abs_path, base_dir)
            yield abs_path, rel_path.replace(os.sep, "/")  # Use '/' for Supabase paths

def upload_file(file_info, skip_existing=False):
    """Upload a single file to Supabase Storage.

    Args:
        file_info: Tuple of (absolute_path, relative_path)
        skip_existing: Whether to skip files that already exist in Supabase

    Returns:
        Tuple of (success, message)
    """
    abs_path, rel_path = file_info

    try:
        # Check if file exists in Supabase (if skip_existing is True)
        if skip_existing:
            try:
                # This will raise an exception if the file doesn't exist
                supabase.storage.from_(BUCKET_NAME).get_public_url(rel_path)
                return True, f"Skipped existing file: {rel_path}"
            except Exception:
                # File doesn't exist, continue with upload
                pass

        # Read file content
        with open(abs_path, 'rb') as f:
            file_content = f.read()

        # Upload to Supabase
        # The API might have changed, so let's try different approaches
        try:
            # Only log essential information
            logger.info(f"Uploading {rel_path} to bucket {BUCKET_NAME}")

            # Try to delete the file first if it exists (to handle the duplicate error)
            try:
                supabase.storage.from_(BUCKET_NAME).remove([rel_path])
                logger.debug(f"Removed existing file: {rel_path}")
            except Exception as e:
                # Ignore errors when trying to delete (file might not exist)
                logger.debug(f"File may not exist yet: {rel_path}")

            # New API (v2+)
            result = supabase.storage.from_(BUCKET_NAME).upload(
                rel_path,
                file_content
            )
        except TypeError as te:
            logger.debug(f"TypeError: {str(te)}")
            try:
                # Older API
                result = supabase.storage.from_(BUCKET_NAME).upload(
                    rel_path,
                    file_content
                )
            except Exception as e:
                # Check if it's a duplicate error (409)
                error_str = str(e)
                if "409" in error_str and "Duplicate" in error_str:
                    logger.info(f"File already exists: {rel_path}")
                    return True, f"File already exists: {rel_path}"

                logger.error(f"Error uploading {rel_path}: {str(e)}")
                return False, f"Error uploading {rel_path}: {str(e)}"
        except Exception as e:
            # Check if it's a duplicate error (409)
            error_str = str(e)
            if "409" in error_str and "Duplicate" in error_str:
                logger.info(f"File already exists: {rel_path}")
                return True, f"File already exists: {rel_path}"

            logger.error(f"Error uploading {rel_path}: {str(e)}")
            return False, f"Error uploading {rel_path}: {str(e)}"

        return True, f"Uploaded: {rel_path}"

    except Exception as e:
        return False, f"Error uploading {rel_path}: {str(e)}"

def upload_folder(source_dir, bucket_name=BUCKET_NAME, workers=WORKERS, skip_existing=False):
    """Upload all files in a folder to Supabase Storage.

    Args:
        source_dir: Source directory to upload
        bucket_name: Supabase Storage bucket name
        workers: Number of concurrent upload workers
        skip_existing: Whether to skip files that already exist in Supabase

    Returns:
        Tuple of (success_count, error_count, errors)
    """
    global BUCKET_NAME
    BUCKET_NAME = bucket_name

    # Get all files to upload
    all_files = list(get_all_files(source_dir))
    if not all_files:
        logger.warning(f"No files found in {source_dir}")
        return 0, 0, []

    logger.info(f"Found {len(all_files)} files to upload from {source_dir} to {bucket_name}")

    # Upload files in parallel
    success_count = 0
    error_count = 0
    errors = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        # Submit all upload tasks
        future_to_file = {
            executor.submit(upload_file, file_info, skip_existing): file_info[1]
            for file_info in all_files
        }

        # Process results as they complete
        with tqdm(total=len(all_files), desc="Uploading files") as pbar:
            for future in as_completed(future_to_file):
                file_path = future_to_file[future]
                try:
                    success, message = future.result()
                    if success:
                        success_count += 1
                        logger.debug(message)
                    else:
                        error_count += 1
                        errors.append(message)
                        logger.error(message)
                except Exception as e:
                    error_count += 1
                    error_msg = f"Error uploading {file_path}: {str(e)}"
                    errors.append(error_msg)
                    logger.error(error_msg)

                pbar.update(1)
                # Report progress for web interface
                progress = int((pbar.n / pbar.total) * 100)
                # Always print progress for the web interface to capture
                print(f"{progress}% complete - Uploaded {success_count} files, {error_count} errors")
                # But only log at intervals to avoid flooding the logs
                if progress % 10 == 0 or progress == 100:  # Only log at 10% intervals and at 100%
                    logger.info(f"{progress}% complete - Uploaded {success_count} files, {error_count} errors")

    logger.info(f"Upload complete: {success_count} successful, {error_count} errors")
    return success_count, error_count, errors

def main():
    """Main function to run the uploader."""
    parser = argparse.ArgumentParser(description='Upload files to Supabase Storage')
    parser.add_argument('--source-dir', default=SOURCE_DIR, help='Source directory to upload')
    parser.add_argument('--bucket', default=BUCKET_NAME, help='Supabase Storage bucket name')
    parser.add_argument('--workers', type=int, default=WORKERS, help='Number of concurrent upload workers')
    parser.add_argument('--skip-existing', action='store_true', help='Skip files that already exist in Supabase')

    # Add arguments that are passed by the web interface but not used by this script
    # This allows the script to be called with the same arguments as the scraper scripts
    parser.add_argument('--username', help='Ignored - for compatibility with scraper scripts')
    parser.add_argument('--password', help='Ignored - for compatibility with scraper scripts')
    parser.add_argument('--academic-year', help='Ignored - for compatibility with scraper scripts')
    parser.add_argument('--semester', help='Ignored - for compatibility with scraper scripts')
    parser.add_argument('--branch', help='Ignored - for compatibility with scraper scripts')
    parser.add_argument('--section', help='Ignored - for compatibility with scraper scripts')
    parser.add_argument('--data-dir', help='Alternative to --source-dir')
    parser.add_argument('--headless', action='store_true', help='Ignored - for compatibility with scraper scripts')
    parser.add_argument('--max-retries', type=int, help='Ignored - for compatibility with scraper scripts')
    parser.add_argument('--timeout', type=int, help='Ignored - for compatibility with scraper scripts')
    parser.add_argument('--force-requests', action='store_true', help='Ignored - for compatibility with scraper scripts')

    args = parser.parse_args()

    # If data-dir is provided, use it as the source directory
    if args.data_dir:
        args.source_dir = args.data_dir

    # Create source directory if it doesn't exist
    source_dir = Path(args.source_dir)
    source_dir.mkdir(parents=True, exist_ok=True)

    # Upload files
    success_count, error_count, errors = upload_folder(
        source_dir=args.source_dir,
        bucket_name=args.bucket,
        workers=args.workers,
        skip_existing=args.skip_existing
    )

    # Print summary
    print(f"\nUpload Summary:")
    print(f"  Source Directory: {args.source_dir}")
    print(f"  Bucket: {args.bucket}")
    print(f"  Files Uploaded: {success_count}")
    print(f"  Errors: {error_count}")

    if error_count > 0:
        print("\nError Details:")
        for error in errors[:10]:  # Show first 10 errors
            print(f"  - {error}")

        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more errors")

    # Print a clear completion message for the taskmaster to detect
    print(f"Upload to Supabase completed successfully with {success_count} files uploaded.")

    # Return success if no errors
    return 0 if error_count == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
