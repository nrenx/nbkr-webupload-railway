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
        self.last_worker_activity = time.time()
        self.last_monitor_activity = time.time()
        self.last_supervisor_activity = time.time()

        # Start a thread to monitor worker health
        self.monitor_thread = threading.Thread(target=self._monitor_worker_health, daemon=True)
        self.monitor_thread.start()

        # Start a supervisor thread to monitor both worker and monitor threads
        self.supervisor_thread = threading.Thread(target=self._supervisor, daemon=True)
        self.supervisor_thread.start()

        logger.info("TaskMaster initialized with worker, monitor, and supervisor threads")

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

    def get_worker_status(self) -> Dict[str, Any]:
        """Get the status of the worker thread.

        Returns:
            Dictionary with worker thread status information
        """
        with self.lock:
            current_time = time.time()
            last_worker_activity_seconds_ago = current_time - self.last_worker_activity
            last_monitor_activity_seconds_ago = current_time - self.last_monitor_activity
            last_supervisor_activity_seconds_ago = current_time - self.last_supervisor_activity

            return {
                "worker_alive": self.worker_thread.is_alive(),
                "monitor_alive": self.monitor_thread.is_alive(),
                "supervisor_alive": self.supervisor_thread.is_alive(),
                "last_worker_activity_seconds_ago": int(last_worker_activity_seconds_ago),
                "last_monitor_activity_seconds_ago": int(last_monitor_activity_seconds_ago),
                "last_supervisor_activity_seconds_ago": int(last_supervisor_activity_seconds_ago),
                "active_job": self.active_job,
                "pending_jobs_count": len([job for job in self.jobs.values()
                                         if job.status == JobStatus.PENDING]),
                "queue_size": self.job_queue.qsize(),
                "queue_empty": self.job_queue.empty()
            }

    def restart_worker(self) -> Dict[str, Any]:
        """Restart the worker thread.

        Returns:
            Dictionary with restart status information
        """
        with self.lock:
            # If there's an active job, mark it as failed
            if self.active_job:
                job = self.jobs.get(self.active_job)
                if job and job.status == JobStatus.RUNNING:
                    job.status = JobStatus.FAILED
                    job.add_log("Job failed due to worker restart")
                    job.end_time = datetime.now()
                    logger.warning(f"Marked job {self.active_job} as failed due to worker restart")
                self.active_job = None

            # Create a new worker thread
            old_thread_alive = self.worker_thread.is_alive()
            self.worker_thread = threading.Thread(target=self._worker, daemon=True)
            self.worker_thread.start()
            self.last_worker_activity = time.time()

            logger.warning("Worker thread restarted manually")

            return {
                "success": True,
                "old_thread_alive": old_thread_alive,
                "new_thread_alive": self.worker_thread.is_alive(),
                "timestamp": datetime.now().isoformat()
            }

    def restart_monitor(self) -> Dict[str, Any]:
        """Restart the monitor thread.

        Returns:
            Dictionary with restart status information
        """
        with self.lock:
            # Create a new monitor thread
            old_thread_alive = self.monitor_thread.is_alive()
            self.monitor_thread = threading.Thread(target=self._monitor_worker_health, daemon=True)
            self.monitor_thread.start()
            self.last_monitor_activity = time.time()

            logger.warning("Monitor thread restarted manually")

            return {
                "success": True,
                "old_thread_alive": old_thread_alive,
                "new_thread_alive": self.monitor_thread.is_alive(),
                "timestamp": datetime.now().isoformat()
            }

    def _supervisor(self):
        """Supervisor thread that monitors both worker and monitor threads."""
        while True:
            try:
                # Update the last activity timestamp
                self.last_supervisor_activity = time.time()

                # Check if the monitor thread is alive
                if not self.monitor_thread.is_alive():
                    logger.error("Monitor thread is not alive, restarting it")
                    self.restart_monitor()

                # Check if the worker thread is alive
                if not self.worker_thread.is_alive():
                    logger.error("Worker thread is not alive, supervisor is restarting it")
                    self.restart_worker()

                # Sleep for 30 seconds before checking again
                # Shorter interval than monitor thread to ensure quick recovery
                time.sleep(30)
            except Exception as e:
                logger.error(f"Error in supervisor thread: {e}")
                import traceback
                logger.error(f"Stack trace: {traceback.format_exc()}")
                time.sleep(30)  # Sleep for 30 seconds before trying again

    def _monitor_worker_health(self):
        """Monitor the health of the worker thread and restart it if necessary."""
        while True:
            try:
                # Update the last activity timestamp
                self.last_monitor_activity = time.time()

                # Check if the worker thread is alive
                if not self.worker_thread.is_alive():
                    logger.error("Worker thread is not alive, restarting it")
                    self.worker_thread = threading.Thread(target=self._worker, daemon=True)
                    self.worker_thread.start()

                # Check if the worker thread is stuck (no activity for 5 minutes)
                current_time = time.time()
                if current_time - self.last_worker_activity > 300:  # 5 minutes
                    logger.warning("Worker thread appears to be stuck, checking queue")
                    # If there are pending jobs but the worker is inactive, log this
                    with self.lock:
                        pending_jobs = [job_id for job_id, job in self.jobs.items()
                                      if job.status == JobStatus.PENDING]
                        if pending_jobs and not self.active_job:
                            logger.error(f"Worker thread is stuck with pending jobs: {pending_jobs}")
                            # We can't safely restart the thread if it's still alive,
                            # but we can log this condition for monitoring

                # Sleep for 60 seconds before checking again
                time.sleep(60)
            except Exception as e:
                logger.error(f"Error in monitor thread: {e}")
                import traceback
                logger.error(f"Stack trace: {traceback.format_exc()}")
                time.sleep(60)  # Sleep for 60 seconds before trying again

    def _worker(self):
        """Worker thread that processes jobs from the queue."""
        while True:
            try:
                # Update the last activity timestamp
                self.last_worker_activity = time.time()

                # Get the next job from the queue with a timeout
                # This ensures the thread doesn't block indefinitely
                try:
                    job_id = self.job_queue.get(timeout=60)  # 60 second timeout
                except Exception as queue_error:
                    logger.info(f"Job queue empty or timed out: {queue_error}")
                    time.sleep(5)  # Sleep for 5 seconds before checking again
                    continue

                # Update the last activity timestamp after getting a job
                self.last_worker_activity = time.time()
                logger.info(f"Worker thread processing job: {job_id}")

                with self.lock:
                    if job_id not in self.jobs:
                        logger.warning(f"Job {job_id} not found in jobs dictionary")
                        self.job_queue.task_done()
                        continue

                    job = self.jobs[job_id]
                    if job.status != JobStatus.PENDING:
                        logger.warning(f"Job {job_id} not in PENDING state: {job.status}")
                        self.job_queue.task_done()
                        continue

                    # Mark the job as running
                    job.status = JobStatus.RUNNING
                    job.start_time = datetime.now()
                    job.add_log("Job started")
                    self.active_job = job_id
                    logger.info(f"Job {job_id} marked as RUNNING")

                # Process each script in sequence
                success = True
                for i, script_name in enumerate(job.scripts):
                    # Update activity timestamp
                    self.last_worker_activity = time.time()

                    job.current_script = script_name
                    job.progress = int((i / len(job.scripts)) * 100)
                    job.add_log(f"Running script: {script_name}")
                    logger.info(f"Job {job_id} running script: {script_name}")

                    # Run the script
                    script_success = self._run_script(job, script_name)

                    # Update activity timestamp after script execution
                    self.last_worker_activity = time.time()

                    if not script_success:
                        logger.warning(f"Job {job_id} script {script_name} failed")
                        success = False
                        break

                # Mark the job as completed or failed
                with self.lock:
                    # Update activity timestamp
                    self.last_worker_activity = time.time()

                    job.end_time = datetime.now()
                    if success:
                        job.status = JobStatus.COMPLETED
                        job.progress = 100
                        job.add_log("Job completed successfully")
                        logger.info(f"Job {job_id} completed successfully")
                    else:
                        job.status = JobStatus.FAILED
                        job.add_log("Job failed")
                        logger.info(f"Job {job_id} failed")

                    self.active_job = None

                self.job_queue.task_done()

                # Final activity timestamp update
                self.last_worker_activity = time.time()

            except Exception as e:
                # Update activity timestamp even on error
                self.last_worker_activity = time.time()

                logger.error(f"Error in worker thread: {e}")
                # Log the full stack trace for better debugging
                import traceback
                logger.error(f"Stack trace: {traceback.format_exc()}")

                # If there was an active job, mark it as failed
                if self.active_job:
                    try:
                        with self.lock:
                            job = self.jobs.get(self.active_job)
                            if job and job.status == JobStatus.RUNNING:
                                job.status = JobStatus.FAILED
                                job.add_log(f"Job failed due to worker error: {e}")
                                job.end_time = datetime.now()
                                logger.error(f"Marked job {self.active_job} as failed due to worker error")
                            self.active_job = None
                    except Exception as mark_error:
                        logger.error(f"Error marking job as failed: {mark_error}")

                time.sleep(5)  # Avoid tight loop in case of persistent errors

    def _run_script(self, job: Job, script_name: str) -> bool:
        """Run a script for a job.

        Args:
            job: The job
            script_name: Name of the script to run

        Returns:
            True if the script ran successfully, False otherwise
        """
        try:
            # Check if we should use Playwright version of the script
            use_playwright = True
            playwright_script = None

            # Map traditional scripts to Playwright versions
            script_mapping = {
                "attendance_scraper.py": "playwright_attendance_scraper.py",
                # Add more mappings as they become available
                # "mid_marks_scraper.py": "playwright_mid_marks_scraper.py",
                # "personal_details_scraper.py": "playwright_personal_details_scraper.py",
            }

            # Check if a Playwright version exists for this script
            if script_name in script_mapping:
                playwright_script = script_mapping[script_name]
                # Check if the Playwright script file exists
                if os.path.exists(playwright_script):
                    script_name = playwright_script
                    job.add_log(f"Using Playwright version: {playwright_script}")
                else:
                    job.add_log(f"Playwright script {playwright_script} not found, using traditional script")
                    use_playwright = False
            else:
                use_playwright = False

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
                            script_index = job.scripts.index(script_name.replace("playwright_", ""))
                            overall_progress = int(((script_index + script_progress / 100) / len(job.scripts)) * 100)
                            job.update_progress(overall_progress)
                        except (ValueError, IndexError):
                            pass
                    # Also check for completion messages
                    elif any(x in line.lower() for x in ["completed successfully", "finished", "done", "complete", "uploaded"]):
                        # If we see a completion message, set progress to 100% for this script
                        script_index = job.scripts.index(script_name.replace("playwright_", ""))
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
