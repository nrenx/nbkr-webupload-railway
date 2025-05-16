#!/usr/bin/env python3
"""
Login utilities for the college website scraper.
This module handles authentication to the college portal.
"""

import os
import sys
import time
import logging
import requests
from bs4 import BeautifulSoup
from typing import Dict, Optional, Tuple

# Import configuration
from config import USERNAME, PASSWORD, ATTENDANCE_PORTAL_URL, MID_MARKS_PORTAL_URL, DEFAULT_SETTINGS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("scraper.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("login_utils")

# Portal URLs
BASE_URL = "http://103.203.175.90:94"
# Use the attendance portal URL directly for login
LOGIN_URL = ATTENDANCE_PORTAL_URL  # This will redirect to login page if not authenticated
ATTENDANCE_LOGIN_URL = f"{BASE_URL}/attendance/attendanceLogin.php"  # Additional login page for attendance and other sections
MID_MARKS_URL = MID_MARKS_PORTAL_URL  # URL for mid marks page

def create_session(headers: Optional[Dict[str, str]] = None) -> requests.Session:
    """
    Create a session object with default headers.

    Args:
        headers: Optional HTTP headers for the requests

    Returns:
        requests.Session object
    """
    session = requests.Session()
    default_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }

    if headers:
        default_headers.update(headers)

    session.headers.update(default_headers)
    return session

def login(session: requests.Session, username: str = USERNAME, password: str = PASSWORD) -> Tuple[bool, str]:
    """
    Log in to the college website.

    Args:
        session: requests.Session object
        username: Login username (defaults to config.USERNAME)
        password: Login password (defaults to config.PASSWORD)

    Returns:
        Tuple of (success status, error message if any)
    """
    try:
        # First, get the login page to extract any CSRF token if needed
        logger.info("Fetching login page...")
        response = session.get(LOGIN_URL)
        response.raise_for_status()

        # Parse the login page
        soup = BeautifulSoup(response.text, 'html.parser')

        # Extract CSRF token if present (adjust selector based on actual page)
        csrf_token = None
        csrf_input = soup.select_one('input[name="csrf_token"]')
        if csrf_input:
            csrf_token = csrf_input.get('value')
            logger.info("CSRF token extracted")

        # Prepare login data - using the field names from the actual form
        login_data = {
            'username': username,  # Field name confirmed from the login process
            'password': password,  # Field name confirmed from the login process
        }

        # Look for the submit button to get its name and value
        submit_button = soup.select_one('input[type="submit"]')
        if submit_button:
            name = submit_button.get('name')
            value = submit_button.get('value')
            if name and value:
                login_data[name] = value
                logger.info(f"Found submit button: {name}={value}")

        # Add CSRF token if found
        if csrf_token:
            login_data['csrf_token'] = csrf_token

        # Submit the login form
        logger.info("Submitting login form...")
        login_response = session.post(LOGIN_URL, data=login_data)
        login_response.raise_for_status()

        # Check if login was successful by checking if we're redirected away from login page
        current_url = login_response.url
        if "login" not in current_url.lower():
            logger.info("Login successful")
            return True, ""
        else:
            logger.warning("Login failed - incorrect credentials or form fields")
            return False, "Login failed - please check your credentials in config.py (USERNAME and PASSWORD variables). These credentials change frequently."

    except requests.exceptions.RequestException as e:
        error_msg = f"Error during login: {str(e)}"
        logger.error(error_msg)
        return False, error_msg

def login_to_attendance(session: requests.Session, username: str = USERNAME, password: str = PASSWORD) -> Tuple[bool, str]:
    """
    Log in to the attendance section of the college website.

    Args:
        session: requests.Session object
        username: Login username (defaults to config.USERNAME)
        password: Login password (defaults to config.PASSWORD)

    Returns:
        Tuple of (success status, error message if any)
    """
    try:
        # First, get the attendance login page
        logger.info("Fetching attendance login page...")
        response = session.get(ATTENDANCE_LOGIN_URL)
        response.raise_for_status()

        # Parse the login page
        soup = BeautifulSoup(response.text, 'html.parser')

        # Prepare login data - using the field names from the actual form
        login_data = {
            'username': username,
            'password': password,
        }

        # Look for the submit button to get its name and value
        submit_button = soup.select_one('input[type="submit"]')
        if submit_button:
            name = submit_button.get('name')
            value = submit_button.get('value')
            if name and value:
                login_data[name] = value
                logger.info(f"Found submit button: {name}={value}")

        # Submit the login form
        logger.info("Submitting attendance login form...")
        login_response = session.post(ATTENDANCE_LOGIN_URL, data=login_data)
        login_response.raise_for_status()

        # Check if login was successful by checking if we're redirected away from login page
        current_url = login_response.url
        if "login" not in current_url.lower():
            logger.info("Attendance login successful")
            return True, ""
        else:
            logger.warning("Attendance login failed - incorrect credentials or form fields")
            return False, "Attendance login failed - please check your credentials in config.py (USERNAME and PASSWORD variables). These credentials change frequently."

    except requests.exceptions.RequestException as e:
        error_msg = f"Error during attendance login: {str(e)}"
        logger.error(error_msg)
        return False, error_msg

def is_logged_in(session: requests.Session) -> bool:
    """
    Check if the session is logged in.

    Args:
        session: requests.Session object

    Returns:
        Boolean indicating login status
    """
    try:
        # Try to access a page that requires authentication
        # We'll use the attendance page as it requires login
        response = session.get(ATTENDANCE_PORTAL_URL)

        # Check if we're redirected to the login page
        current_url = response.url
        if "login" in current_url.lower():
            return False

        # Check for indicators of being logged in
        # If we can access the attendance page without being redirected to login,
        # we're likely logged in
        return True
    except Exception as e:
        logger.error(f"Error checking login status: {str(e)}")
        return False
