#!/usr/bin/env python3
"""
NBKRIST Student Portal - Web Interface

This Flask application provides a web interface for running the scraper scripts
and uploading data to Supabase. It's designed to work on Render's free tier.
"""

import os
import sys
import time
import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Union

from flask import Flask, render_template, request, jsonify, redirect, url_for, session

# Import the taskmaster for job management
from taskmaster import TaskMaster, Job, JobStatus

# Import configuration
from config import (
    USERNAME, PASSWORD, DEFAULT_ACADEMIC_YEARS, DEFAULT_SEMESTERS,
    DEFAULT_BRANCHES, DEFAULT_SECTIONS, DEFAULT_SETTINGS
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("web_app.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("web_app")

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nbkrist_student_portal_secret_key")

# Check if running on Railway and set environment variables
if 'RAILWAY_ENVIRONMENT' in os.environ:
    # Force requests-based scraping on Railway to reduce memory usage
    os.environ['FORCE_REQUESTS_SCRAPING'] = 'true'

    # Add the --force-requests parameter to personal_details_scraper.py
    # This is needed because personal_details_scraper.py has a specific parameter for this
    # The other scripts will use the environment variable approach
    logger.info("Running on Railway, forcing requests-based scraping to reduce memory usage")

# Initialize the TaskMaster
task_master = TaskMaster()

# Create temp directory for data storage
TEMP_DATA_DIR = Path("/tmp/student_details")
TEMP_DATA_DIR.mkdir(parents=True, exist_ok=True)

@app.route('/')
def index():
    """Render the main page with the form for running scripts."""
    return render_template(
        'index.html',
        academic_years=DEFAULT_ACADEMIC_YEARS,
        semesters=DEFAULT_SEMESTERS,
        branches=DEFAULT_BRANCHES,
        sections=DEFAULT_SECTIONS,
        active_jobs=task_master.get_active_jobs(),
        completed_jobs=task_master.get_completed_jobs(limit=5)
    )

@app.route('/submit', methods=['POST'])
def submit_job():
    """Handle form submission and create a new job."""
    # Get form data
    username = request.form.get('username', USERNAME)
    password = request.form.get('password', PASSWORD)
    academic_year = request.form.get('academic_year', DEFAULT_ACADEMIC_YEARS[0])
    semester = request.form.get('semester', DEFAULT_SEMESTERS[0])
    branch = request.form.get('branch', DEFAULT_BRANCHES[0])
    section = request.form.get('section', DEFAULT_SECTIONS[0])

    # Get selected scripts
    selected_scripts = request.form.getlist('scripts')
    if not selected_scripts:
        return jsonify({"error": "No scripts selected"}), 400

    # Create a new job
    params = {
        "username": username,
        "password": password,
        "academic_year": academic_year,
        "semester": semester,
        "branch": branch,
        "section": section,
        "data_dir": str(TEMP_DATA_DIR),
        "headless": True,  # Always use headless mode
        "workers": 1,  # Use single worker for stability
        "max_retries": 5,  # Increase retries for better reliability
        "timeout": 60,  # Increase timeout for slower connections
    }

    # Add Railway-specific parameters
    if 'RAILWAY_ENVIRONMENT' in os.environ:
        # Use lower memory settings on Railway
        # Only add force-requests parameter for personal_details_scraper.py
        if 'personal_details_scraper.py' in selected_scripts:
            params["force_requests"] = True
            logger.info("Adding force_requests=True parameter for personal_details_scraper.py on Railway")
        # For other scripts, we'll use the environment variable approach without adding the parameter
        # This is because attendance_scraper.py and mid_marks_scraper.py don't have a --force-requests parameter
        logger.info(f"Using FORCE_REQUESTS_SCRAPING=true environment variable for scripts on Railway")

    job = task_master.create_job(
        scripts=selected_scripts,
        params=params
    )

    # Start the job (TaskMaster will handle queueing)
    task_master.start_job(job.id)

    # Redirect to the status page
    return redirect(url_for('job_status', job_id=job.id))

@app.route('/status/<job_id>')
def job_status(job_id):
    """Show the status of a specific job."""
    job = task_master.get_job(job_id)
    if not job:
        return "Job not found", 404

    return render_template('status.html', job=job)

@app.route('/api/status/<job_id>')
def api_job_status(job_id):
    """API endpoint to get the status of a job."""
    job = task_master.get_job(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "id": job.id,
        "status": job.status.value,
        "scripts": job.scripts,
        "current_script": job.current_script,
        "progress": job.progress,
        "start_time": job.start_time.isoformat() if job.start_time else None,
        "end_time": job.end_time.isoformat() if job.end_time else None,
        "logs": job.logs[-50:],  # Return the last 50 log entries
    })

@app.route('/api/jobs')
def api_jobs():
    """API endpoint to get all jobs."""
    active_jobs = task_master.get_active_jobs()
    completed_jobs = task_master.get_completed_jobs(limit=10)

    return jsonify({
        "active_jobs": [job.to_dict() for job in active_jobs],
        "completed_jobs": [job.to_dict() for job in completed_jobs],
    })

@app.route('/api/worker-status')
def api_worker_status():
    """API endpoint to get the status of the worker thread."""
    return jsonify(task_master.get_worker_status())

@app.route('/api/restart-worker', methods=['POST'])
def api_restart_worker():
    """API endpoint to restart the worker thread."""
    return jsonify(task_master.restart_worker())

@app.route('/api/restart-monitor', methods=['POST'])
def api_restart_monitor():
    """API endpoint to restart the monitor thread."""
    return jsonify(task_master.restart_monitor())

@app.route('/cancel/<job_id>', methods=['POST'])
def cancel_job(job_id):
    """Cancel a running job."""
    success = task_master.cancel_job(job_id)
    if not success:
        return jsonify({"error": "Failed to cancel job"}), 400

    return redirect(url_for('job_status', job_id=job_id))

@app.route('/results/<job_id>')
def job_results(job_id):
    """Show the results of a completed job."""
    job = task_master.get_job(job_id)
    if not job:
        return "Job not found", 404

    if job.status != JobStatus.COMPLETED:
        return redirect(url_for('job_status', job_id=job_id))

    return render_template('results.html', job=job)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
