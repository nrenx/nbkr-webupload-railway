#!/usr/bin/env python3
"""
Supabase Configuration for Fast Uploader

This file contains configuration settings for the fast Supabase uploader.
Copy this file to supabase_config.py and update the values.
"""

# Supabase credentials
SUPABASE_URL = "https://ndeagjkuhzyozgimudow.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5kZWFnamt1aHp5b3pnaW11ZG93Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc0NDg5OTY4NiwiZXhwIjoyMDYwNDc1Njg2fQ.qyjFWHusv_o03P_eS_j_kCemXLD45wvioD3lxIqYlbM"

# Default settings
DEFAULT_SETTINGS = {
    # Storage settings
    "bucket": "demo-usingfastapi",
    "source_dir": "/tmp/student_details",

    # Performance settings
    "workers": 32,              # Number of worker threads for connection pool
    "student_batch": 20,        # Number of students to process in parallel

    # Feature settings
    "skip_existing": True,      # Skip files that already exist in Supabase
}
