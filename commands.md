# NBKRIST Student Portal - Script Commands

This file contains the recommended commands for running the various scripts in the NBKRIST Student Portal project.

## Web Scraping Scripts

### Attendance Scraper
```bash
python3 attendance_scraper.py --workers 16 --worker-mode thread --headless --delay 1.0 --max-retries 3
```
*This command runs the attendance scraper with 16 worker threads, in headless mode, with a 1-second delay between requests and up to 3 retries for failed requests.*

### Mid Marks Scraper
```bash
python3 mid_marks_scraper.py --workers 16 --worker-mode thread --headless --delay 1.0 --max-retries 3
```
*This command runs the mid marks scraper with 16 worker threads, in headless mode, with a 1-second delay between requests and up to 3 retries for failed requests.*

### Personal Details Scraper
```bash
python3 personal_details_scraper.py --workers 16 --worker-mode thread --headless --delay 1.0
```
*This command runs the personal details scraper with 16 worker threads, in headless mode, with a 1-second delay between requests.*

## Supabase Upload Scripts

### Optimized Supabase Uploader (Skip Existing Files)
```bash
python3 supabase_uploader_new.py --workers 32 --student-batch 20 --skip-existing
```
*This command uploads data to Supabase using 32 worker threads, processing 20 students in parallel, and skips files that already exist in Supabase.*

### Legacy Supabase Uploader (Overwrite All Data)
```bash
python3 upload_folder_to_supabase.py
```
*This command uploads all data to Supabase, overwriting any existing files.*

## Installation

To install all required dependencies:
```bash
pip install -r requirements.txt
```

## Notes

- The `--worker-mode thread` option is recommended for better compatibility and performance
- The `--headless` option runs the scrapers without opening a browser window
- Adjust the `--workers` and `--student-batch` parameters based on your system's capabilities
- Use `--skip-existing` with the uploader to avoid re-uploading files that haven't changed
