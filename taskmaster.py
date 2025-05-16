#!/usr/bin/env python3
"""
TaskMaster - Job Management System

This module provides a job management system for running scraper scripts
sequentially and tracking their progress.
"""

import os
import sys
import time
import json
import logging
import threading
import subprocess
import uuid
from enum import Enum
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Union, Callable
from queue import Queue

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("taskmaster.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("taskmaster")

class JobStatus(Enum):
    """Enum for job status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class Job:
    """Class representing a job."""

    def __init__(self, id: str, scripts: List[str], params: Dict[str, Any]):
        """Initialize a job.

        Args:
            id: Unique job ID
            scripts: List of script names to run
            params: Parameters to pass to the scripts
        """
        self.id = id
        self.scripts = scripts
        self.params = params
        self.status = JobStatus.PENDING
        self.current_script = None
        self.progress = 0
        self.results = {}
        self.logs = []
        self.start_time = None
        self.end_time = None
        self.process = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert job to dictionary."""
        return {
            "id": self.id,
            "scripts": self.scripts,
            "params": self.params,
            "status": self.status.value,
            "current_script": self.current_script,
            "progress": self.progress,
            "results": self.results,
            "logs": self.logs[-50:],  # Only include the last 50 logs
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
        }

    def add_log(self, message: str):
        """Add a log message to the job."""
        timestamp = datetime.now().isoformat()
        self.logs.append(f"[{timestamp}] {message}")
        logger.info(f"Job {self.id}: {message}")

    def update_progress(self, progress: int):
        """Update the job progress."""
        self.progress = progress

class TaskMaster:
    """Class for managing jobs."""

    def __init__(self):
        """Initialize the TaskMaster."""
        self.jobs: Dict[str, Job] = {}
        self.job_queue: Queue = Queue()
        self.active_job: Optional[str] = None
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()
        self.lock = threading.Lock()

    def create_job(self, scripts: List[str], params: Dict[str, Any]) -> Job:
        """Create a new job.

        Args:
            scripts: List of script names to run
            params: Parameters to pass to the scripts

        Returns:
            The created job
        """
        job_id = str(uuid.uuid4())
        job = Job(id=job_id, scripts=scripts, params=params)

        with self.lock:
            self.jobs[job_id] = job

        return job

    def start_job(self, job_id: str) -> bool:
        """Start a job.

        Args:
            job_id: ID of the job to start

        Returns:
            True if the job was started, False otherwise
        """
        with self.lock:
            if job_id not in self.jobs:
                return False

            job = self.jobs[job_id]
            if job.status != JobStatus.PENDING:
                return False

            self.job_queue.put(job_id)
            job.add_log("Job queued for execution")
            return True

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a job.

        Args:
            job_id: ID of the job to cancel

        Returns:
            True if the job was cancelled, False otherwise
        """
        with self.lock:
            if job_id not in self.jobs:
                return False

            job = self.jobs[job_id]

            # If the job is running and it's the active job, terminate it
            if job.status == JobStatus.RUNNING and self.active_job == job_id and job.process:
                try:
                    job.process.terminate()
                    job.add_log("Job cancelled")
                    job.status = JobStatus.CANCELLED
                    self.active_job = None
                    return True
                except Exception as e:
                    job.add_log(f"Failed to cancel job: {e}")
                    return False

            # If the job is pending, just mark it as cancelled
            if job.status == JobStatus.PENDING:
                job.status = JobStatus.CANCELLED
                job.add_log("Job cancelled while pending")
                return True

            return False

    def get_job(self, job_id: str) -> Optional[Job]:
        """Get a job by ID.

        Args:
            job_id: ID of the job to get

        Returns:
            The job, or None if not found
        """
        with self.lock:
            return self.jobs.get(job_id)

    def get_active_jobs(self) -> List[Job]:
        """Get all active jobs.

        Returns:
            List of active jobs
        """
        with self.lock:
            return [job for job in self.jobs.values()
                   if job.status in (JobStatus.PENDING, JobStatus.RUNNING)]

    def get_completed_jobs(self, limit: int = 10) -> List[Job]:
        """Get completed jobs.

        Args:
            limit: Maximum number of jobs to return

        Returns:
            List of completed jobs
        """
        with self.lock:
            completed = [job for job in self.jobs.values()
                        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)]
            # Sort by end_time (most recent first)
            completed.sort(key=lambda j: j.end_time if j.end_time else datetime.min, reverse=True)
            return completed[:limit]

    def _worker(self):
        """Worker thread that processes jobs from the queue."""
        while True:
            try:
                # Get the next job from the queue
                job_id = self.job_queue.get()

                with self.lock:
                    if job_id not in self.jobs:
                        self.job_queue.task_done()
                        continue

                    job = self.jobs[job_id]
                    if job.status != JobStatus.PENDING:
                        self.job_queue.task_done()
                        continue

                    # Mark the job as running
                    job.status = JobStatus.RUNNING
                    job.start_time = datetime.now()
                    job.add_log("Job started")
                    self.active_job = job_id

                # Process each script in sequence
                success = True
                for i, script_name in enumerate(job.scripts):
                    job.current_script = script_name
                    job.progress = int((i / len(job.scripts)) * 100)
                    job.add_log(f"Running script: {script_name}")

                    # Run the script
                    script_success = self._run_script(job, script_name)

                    if not script_success:
                        success = False
                        break

                # Mark the job as completed or failed
                with self.lock:
                    job.end_time = datetime.now()
                    if success:
                        job.status = JobStatus.COMPLETED
                        job.progress = 100
                        job.add_log("Job completed successfully")
                    else:
                        job.status = JobStatus.FAILED
                        job.add_log("Job failed")

                    self.active_job = None

                self.job_queue.task_done()

            except Exception as e:
                logger.error(f"Error in worker thread: {e}")
                time.sleep(1)  # Avoid tight loop in case of persistent errors

    def _run_script(self, job: Job, script_name: str) -> bool:
        """Run a script for a job.

        Args:
            job: The job
            script_name: Name of the script to run

        Returns:
            True if the script ran successfully, False otherwise
        """
        try:
            # Build the command
            cmd = [sys.executable, script_name]

            # Add only essential parameters
            essential_params = ["username", "password", "academic_year", "data_dir"]

            # Always add headless mode
            cmd.append("--headless")

            # Add other essential parameters
            for key in essential_params:
                value = job.params.get(key)
                if value is not None and value != "":
                    cmd.append(f"--{key.replace('_', '-')}")
                    cmd.append(str(value))

            # Add fixed parameters for stability
            cmd.append("--workers")
            cmd.append("1")
            cmd.append("--max-retries")
            cmd.append("5")
            cmd.append("--timeout")
            cmd.append("60")

            # Log the command
            job.add_log(f"Running command: {' '.join(cmd)}")

            # Run the command
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            # Store the process for potential cancellation
            job.process = process

            # Read output line by line
            for line in iter(process.stdout.readline, ''):
                line = line.strip()
                if line:
                    job.add_log(line)

                    # Check for progress indicators
                    if "% complete" in line:
                        try:
                            progress_str = line.split("%")[0].strip().split()[-1]
                            script_progress = int(progress_str)
                            # Calculate overall progress
                            script_index = job.scripts.index(script_name)
                            overall_progress = int(((script_index + script_progress / 100) / len(job.scripts)) * 100)
                            job.update_progress(overall_progress)
                        except (ValueError, IndexError):
                            pass
                    # Also check for completion messages
                    elif any(x in line.lower() for x in ["completed successfully", "finished", "done", "complete", "uploaded"]):
                        # If we see a completion message, set progress to 100% for this script
                        script_index = job.scripts.index(script_name)
                        # Calculate overall progress (this script is 100% done)
                        overall_progress = int(((script_index + 1) / len(job.scripts)) * 100)
                        job.update_progress(overall_progress)

            # Wait for the process to complete
            return_code = process.wait()
            job.process = None

            if return_code != 0:
                job.add_log(f"Script failed with return code {return_code}")
                return False

            job.add_log(f"Script completed successfully")
            return True

        except Exception as e:
            job.add_log(f"Error running script: {e}")
            return False
