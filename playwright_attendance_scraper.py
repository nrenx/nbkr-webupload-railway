#!/usr/bin/env python3
"""
Attendance Scraper for College Website

This script logs into the college portal and navigates to the attendance page.
It extracts attendance data and stores it in a structured format.
Uses Playwright for browser automation.
"""

import os
import sys
import logging
import argparse
import json
import time
import queue
import threading
import multiprocessing
import asyncio
import concurrent.futures
import requests
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Union
from functools import wraps

# Import Playwright for browser automation
try:
    from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("Warning: Playwright is not installed. Install it with: pip install playwright")
    print("Then run: playwright install chromium")

# Make pandas optional
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    print("Warning: pandas is not installed. CSV/Excel export functionality will be limited.")

# Import login utilities and configuration
from config import (
    USERNAME, PASSWORD, ATTENDANCE_PORTAL_URL,
    DEFAULT_ACADEMIC_YEARS, DEFAULT_SEMESTERS,
    DEFAULT_BRANCHES, DEFAULT_SECTIONS,
    YEAR_SEM_CODES, BRANCH_CODES, DEFAULT_SETTINGS
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("attendance_scraper.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("attendance_scraper")


def login(session, username, password):
    """
    Log in to the attendance portal using requests.

    Args:
        session: Requests session
        username: Login username
        password: Login password

    Returns:
        Tuple of (success, error_message)
    """
    try:
        # First, get the login page to get any cookies or tokens
        response = session.get(ATTENDANCE_PORTAL_URL, timeout=30)
        response.raise_for_status()

        # Prepare login data
        login_data = {
            'username': username,
            'password': password,
            'submit': 'Login'
        }

        # Submit the login form
        response = session.post(ATTENDANCE_PORTAL_URL, data=login_data, timeout=30)
        response.raise_for_status()

        # Check if login was successful
        if "login" not in response.url.lower():
            return True, ""
        else:
            return False, "Login failed. Check your credentials."
    except Exception as e:
        return False, str(e)


def retry_on_network_error(max_retries=3, initial_backoff=1):
    """
    Decorator to retry a function on network errors with exponential backoff.

    Args:
        max_retries: Maximum number of retries
        initial_backoff: Initial backoff time in seconds

    Returns:
        Decorated function
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Get the max_retries from the instance if available
            instance = args[0] if args else None
            retries = getattr(instance, 'max_retries', max_retries) if hasattr(instance, 'max_retries') else max_retries

            backoff = initial_backoff
            last_exception = None

            for attempt in range(retries + 1):
                try:
                    return func(*args, **kwargs)
                except (PlaywrightTimeoutError, ConnectionError, TimeoutError) as e:
                    last_exception = e
                    if attempt < retries:
                        wait_time = backoff * (2 ** attempt)
                        logger.warning(f"Network error: {str(e)}. Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{retries})")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Max retries ({retries}) exceeded. Last error: {str(e)}")
                        raise

            # This should not be reached, but just in case
            if last_exception:
                raise last_exception
            return None
        return wrapper
    return decorator

class AttendanceScraper:
    """
    A class to scrape attendance data from the college website using Playwright.
    """

    def __init__(self, username: str = USERNAME, password: str = PASSWORD,
                 base_dir: str = DEFAULT_SETTINGS['data_dir'],
                 headless: bool = DEFAULT_SETTINGS['headless'],
                 max_retries: int = DEFAULT_SETTINGS['max_retries'],
                 timeout: int = DEFAULT_SETTINGS['timeout'],
                 save_debug: bool = False):
        """
        Initialize the scraper with login credentials and settings.

        Args:
            username: Login username (defaults to config.USERNAME)
            password: Login password (defaults to config.PASSWORD)
            base_dir: Base directory for storing data (defaults to DEFAULT_SETTINGS['data_dir'])
            headless: Whether to run in headless mode (defaults to DEFAULT_SETTINGS['headless'])
            max_retries: Maximum number of retries for network errors (defaults to DEFAULT_SETTINGS['max_retries'])
            timeout: Timeout in seconds for waiting for elements (defaults to DEFAULT_SETTINGS['timeout'])
            save_debug: Whether to save debug files (HTML, screenshots, etc.)
        """
        self.username = username
        self.password = password
        self.logged_in = False
        self.base_dir = Path(base_dir)
        self.headless = headless
        self.max_retries = max_retries
        self.timeout = timeout
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

        # Store settings in a dictionary for easy access
        self.settings = {
            'save_debug': save_debug
        }

        # Create a requests session for fallback
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })

        # Check if Playwright is available
        if not PLAYWRIGHT_AVAILABLE:
            logger.error("Playwright is not available. Please install it with: pip install playwright")
            logger.error("Then run: playwright install chromium")
            raise ImportError("Playwright is required for this scraper")

        logger.info(f"Initialized attendance scraper (headless: {headless})")

        # Initialize the browser (but don't fail if it doesn't work)
        try:
            browser_initialized = self.initialize()
            if not browser_initialized:
                logger.warning("Browser initialization failed, will use requests-based scraping only")
        except Exception as e:
            logger.error(f"Error during browser initialization: {e}")
            logger.warning("Browser initialization failed, will use requests-based scraping only")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def initialize(self):
        """Initialize the browser and context."""
        # Try multiple approaches to initialize the browser
        try:
            # Start playwright
            p = sync_playwright().start()

            # Check if running on Render.com (environment detection)
            is_render = os.environ.get('RENDER') == 'true'

            browser_args = []
            if is_render:
                logger.info("Running on Render.com, using special Chrome configuration")
                browser_args = [
                    '--disable-gpu',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                ]

            # First try: Launch using default Playwright browser
            try:
                logger.info("Attempting to launch browser with default Playwright installation")
                browser = p.chromium.launch(
                    headless=self.headless,
                    args=browser_args
                )
                logger.info("Successfully launched browser with default Playwright installation")
            except Exception as e1:
                logger.warning(f"Failed to launch browser with default Playwright installation: {e1}")

                # Second try: Try with system Chrome if on Render
                if is_render:
                    try:
                        logger.info("Attempting to launch browser with system Chrome")
                        # Try with the Chrome binary installed by Render
                        chrome_paths = [
                            "/usr/bin/google-chrome-stable",
                            "/usr/bin/google-chrome",
                            "/usr/local/bin/chrome",
                            "/usr/local/bin/google-chrome",
                            "/opt/google/chrome/chrome"
                        ]

                        # Log all paths for debugging
                        for path in chrome_paths:
                            exists = os.path.exists(path)
                            logger.info(f"Chrome path check: {path}: {'EXISTS' if exists else 'NOT FOUND'}")

                        executable_path = None
                        for path in chrome_paths:
                            if os.path.exists(path):
                                executable_path = path
                                logger.info(f"Found Chrome at {path}")
                                break

                        if executable_path:
                            browser = p.chromium.launch(
                                headless=self.headless,
                                executable_path=executable_path,
                                args=browser_args
                            )
                            logger.info(f"Successfully launched browser with system Chrome at {executable_path}")
                        else:
                            logger.error("Could not find Chrome executable in any standard location")
                            # Instead of raising an exception, we'll fall back to requests-based scraping
                            logger.warning("Falling back to requests-based scraping")
                            p.stop()
                            return False
                    except Exception as e2:
                        logger.error(f"Failed to launch browser with system Chrome: {e2}")
                        logger.warning("Falling back to requests-based scraping")
                        p.stop()
                        return False
                else:
                    # Not on Render, just raise the original exception
                    raise e1

            # Create context
            context = browser.new_context(
                viewport={'width': 1366, 'height': 768},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            )

            # Create page
            page = context.new_page()

            # Set timeout
            page.set_default_timeout(self.timeout * 1000)  # Convert to milliseconds

            # Store references
            self.playwright = p
            self.browser = browser
            self.context = context
            self.page = page

            logger.info("Browser initialized successfully")
            return True
        except Exception as e:
            logger.error(f"Error initializing browser: {e}")
            # Clean up any resources that might have been created
            self.close()

            # Instead of raising an exception, we'll fall back to requests-based scraping
            logger.warning("Falling back to requests-based scraping")
            return False

    @retry_on_network_error()
    def authenticate(self) -> bool:
        """
        Authenticate with the college portal.

        Returns:
            Boolean indicating success
        """
        if self.logged_in:
            return True

        # Try to authenticate using Playwright if available
        if hasattr(self, 'page') and self.page:
            try:
                # Take screenshots if debug is enabled
                if self.settings.get('save_debug', False):
                    debug_dir = Path("debug_screenshots")
                    debug_dir.mkdir(exist_ok=True)
                    self.page.screenshot(path=str(debug_dir / "before_login.png"))

                logger.info("Authenticating using Playwright...")
                # Go directly to the attendance portal URL which will redirect to login page
                self.page.goto(ATTENDANCE_PORTAL_URL)

                # Wait for the login form to load
                self.page.wait_for_selector('input[name="username"]', state='visible')

                # Log the current URL to verify we're on the login page
                current_url = self.page.url
                logger.debug(f"Current URL after navigation: {current_url}")

                # Fill in the login form
                self.page.fill('input[name="username"]', self.username)
                self.page.fill('input[name="password"]', self.password)

                # Take screenshots if debug is enabled
                if self.settings.get('save_debug', False):
                    self.page.screenshot(path=str(debug_dir / "login_form_filled.png"))

                # Submit the form
                self.page.click('input[type="submit"]')

                # Wait for navigation to complete
                self.page.wait_for_load_state('networkidle')

                # Take screenshots if debug is enabled
                if self.settings.get('save_debug', False):
                    self.page.screenshot(path=str(debug_dir / "after_login.png"))

                # Check if login was successful
                current_url = self.page.url
                if "login" not in current_url.lower():
                    self.logged_in = True
                    logger.info("Login successful using Playwright")
                    return True
                else:
                    logger.error("Login failed using Playwright")

                    # Save page content for debugging
                    if self.settings.get('save_debug', False):
                        content = self.page.content()
                        with open(debug_dir / "login_failed.html", "w") as f:
                            f.write(content)

                    return False

            except Exception as e:
                logger.error(f"Error authenticating using Playwright: {e}")
                # Fall back to requests-based authentication

        # Use requests-based authentication as fallback
        logger.info("Authenticating using requests...")
        success, error_msg = login(self.session, self.username, self.password)
        if success:
            self.logged_in = True
            return True
        else:
            logger.error(f"Authentication failed: {error_msg}")
            return False

    @retry_on_network_error()
    def navigate_to_attendance_page(self) -> Optional[str]:
        """
        Navigate to the attendance page.

        Returns:
            HTML content of the attendance page or None if failed
        """
        if not self.logged_in and not self.authenticate():
            return None

        # Try to navigate using Playwright if available
        if hasattr(self, 'page') and self.page:
            try:
                # We should already be on the attendance page after authentication
                # But let's navigate there explicitly to be sure
                logger.info(f"Navigating to attendance page using Playwright: {ATTENDANCE_PORTAL_URL}")
                self.page.goto(ATTENDANCE_PORTAL_URL)

                # Wait for the page to load
                self.page.wait_for_load_state('networkidle')

                # Log the current URL for debugging
                current_url = self.page.url
                logger.debug(f"Current URL after navigation: {current_url}")

                # Check if we're on the correct page
                content = self.page.content()
                if "attendance" in content.lower():
                    logger.info("Successfully navigated to attendance page using Playwright")
                    return content
                else:
                    logger.warning("Navigation to attendance page failed using Playwright - redirected to another page")
                    # Fall back to requests-based navigation
            except Exception as e:
                logger.error(f"Error navigating to attendance page using Playwright: {e}")
                # Fall back to requests-based navigation

        # Use requests-based navigation as fallback
        try:
            logger.info(f"Navigating to attendance page using requests: {ATTENDANCE_PORTAL_URL}")
            response = self.session.get(ATTENDANCE_PORTAL_URL, timeout=self.timeout)
            response.raise_for_status()

            # Log the current URL for debugging
            logger.debug(f"Current URL after navigation: {response.url}")

            # Check if we're on the correct page
            if "attendance" in response.text.lower():
                logger.info("Successfully navigated to attendance page using requests")
                return response.text
            else:
                logger.warning("Navigation to attendance page failed using requests - redirected to another page")
                return None
        except Exception as e:
            logger.error(f"Error navigating to attendance page using requests: {e}")
            return None

    @retry_on_network_error()
    def select_form_filters(self, academic_year: str, semester: str, branch: str, section: str) -> Optional[str]:
        """
        Select form filters for attendance data.

        Args:
            academic_year: Academic year to select (e.g., "2023-24")
            semester: Semester to select (e.g., "First Yr - First Sem")
            branch: Branch to select (e.g., "CSE")
            section: Section to select (e.g., "A")

        Returns:
            HTML content of the results page or None if failed
        """
        content = self.navigate_to_attendance_page()
        if not content:
            return None

        # Only proceed with Playwright if the page is available
        if not hasattr(self, 'page') or not self.page:
            logger.warning("Playwright page not available, cannot select form filters")
            return None

        try:
            logger.info(f"Using Playwright to select form filters: academic_year={academic_year}, semester={semester}, branch={branch}, section={section}")

            # Wait for the form to load
            self.page.wait_for_selector('select', state='visible')

            # Find all select elements
            select_elements = self.page.query_selector_all('select')
            if not select_elements:
                logger.error("No select elements found in the form")
                return None

            # Find and set academic year
            academic_year_set = False
            for select in select_elements:
                select_name = select.get_attribute('name') or ''
                select_id = select.get_attribute('id') or ''

                if 'year' in select_name.lower() or 'year' in select_id.lower() or 'academic' in select_name.lower():
                    # Get all options
                    options = select.query_selector_all('option')

                    # Try to find an option with text matching the academic year
                    option_found = False
                    for option in options:
                        option_text = option.text_content()
                        option_text = option_text.strip()

                        if academic_year in option_text:
                            # Select this option
                            option_value = option.get_attribute('value')
                            select.select_option(option_value)
                            academic_year_set = True
                            option_found = True
                            logger.debug(f"Selected academic year: {option_text}")
                            break

                    if not option_found:
                        # If no match found, select the first option
                        select.select_option(index=0)
                        academic_year_set = True
                        logger.debug("Selected first academic year option as fallback")

                    break

            if not academic_year_set:
                logger.warning("Could not find academic year select element")

            # Find and set semester
            semester_set = False
            for select in select_elements:
                select_name = select.get_attribute('name') or ''
                select_id = select.get_attribute('id') or ''

                if 'sem' in select_name.lower() or 'sem' in select_id.lower():
                    # Get all options
                    options = select.query_selector_all('option')

                    # Try to find an option with text matching the semester
                    option_found = False
                    for option in options:
                        option_text = option.text_content()
                        option_text = option_text.strip()

                        if semester.lower() in option_text.lower():
                            # Select this option
                            option_value = option.get_attribute('value')
                            select.select_option(option_value)
                            semester_set = True
                            option_found = True
                            logger.debug(f"Selected semester: {option_text}")
                            break

                    if not option_found:
                        # If no match found, select the first option
                        select.select_option(index=0)
                        semester_set = True
                        logger.debug("Selected first semester option as fallback")

                    break

            if not semester_set:
                logger.warning("Could not find semester select element")

            # Find and set branch
            branch_set = False
            for select in select_elements:
                select_name = select.get_attribute('name') or ''
                select_id = select.get_attribute('id') or ''

                if 'branch' in select_name.lower() or 'branch' in select_id.lower() or 'dept' in select_name.lower():
                    # Get all options
                    options = select.query_selector_all('option')

                    # Try to find an option with text matching the branch
                    option_found = False
                    for option in options:
                        option_text = option.text_content()
                        option_text = option_text.strip()

                        if branch.lower() in option_text.lower():
                            # Select this option
                            option_value = option.get_attribute('value')
                            select.select_option(option_value)
                            branch_set = True
                            option_found = True
                            logger.debug(f"Selected branch: {option_text}")
                            break

                    if not option_found:
                        # If no match found, select the first option
                        select.select_option(index=0)
                        branch_set = True
                        logger.debug("Selected first branch option as fallback")

                    break

            if not branch_set:
                logger.warning("Could not find branch select element")

            # Find and set section
            section_set = False
            for select in select_elements:
                select_name = select.get_attribute('name') or ''
                select_id = select.get_attribute('id') or ''

                if 'section' in select_name.lower() or 'section' in select_id.lower():
                    # Get all options
                    options = select.query_selector_all('option')

                    # Try to find an option with text matching the section
                    option_found = False
                    for option in options:
                        option_text = option.text_content()
                        option_text = option_text.strip()
                        option_value = option.get_attribute('value')

                        if section == option_text or section == option_value:
                            # Select this option
                            select.select_option(option_value)
                            section_set = True
                            option_found = True
                            logger.debug(f"Selected section: {option_text}")
                            break

                    if not option_found:
                        # If no match found, select the first option
                        select.select_option(index=0)
                        section_set = True
                        logger.debug("Selected first section option as fallback")

                    break

            if not section_set:
                logger.warning("Could not find section select element")

            # Find and click the show button
            show_button = None

            # First try: Look for input button with value 'Show'
            show_button = self.page.query_selector('input[type="button"][value="Show"]')
            if show_button:
                logger.info("Found Show button by value")

            # Second try: Look for any button with 'show' in its value
            if not show_button:
                input_buttons = self.page.query_selector_all('input[type="button"]')
                for button in input_buttons:
                    value = button.get_attribute('value') or ''
                    if 'show' in value.lower():
                        show_button = button
                        logger.info(f"Found Show button with value: {value}")
                        break

            # Third try: Look for any input with type='submit'
            if not show_button:
                submit_buttons = self.page.query_selector_all('input[type="submit"]')
                if submit_buttons:
                    show_button = submit_buttons[0]
                    value = show_button.get_attribute('value') or ''
                    logger.info(f"Using submit button as fallback: {value}")

            # Take a screenshot for debugging if enabled
            if self.settings.get('save_debug', False):
                debug_dir = Path("debug_output")
                debug_dir.mkdir(parents=True, exist_ok=True)

                # Convert semester to year_of_study format for folder structure
                year_of_study = self.convert_semester_to_year_of_study(semester)

                # Create a structured folder path
                debug_folder = debug_dir / academic_year / year_of_study / branch / section
                debug_folder.mkdir(parents=True, exist_ok=True)

                # Save screenshot
                screenshot_path = debug_folder / "before_click.png"
                self.page.screenshot(path=str(screenshot_path))
                logger.debug(f"Saved screenshot before clicking button to {screenshot_path}")

            if show_button:
                # Scroll to the button to make sure it's visible
                show_button.scroll_into_view_if_needed()
                time.sleep(1)  # Wait for scroll to complete

                # Click the button
                show_button.click()
                logger.info("Clicked show button")

                # Wait for the results page to load
                self.page.wait_for_load_state('networkidle')

                # Save debug information if enabled
                if self.settings.get('save_debug', False):
                    debug_dir = Path("debug_output")
                    debug_dir.mkdir(parents=True, exist_ok=True)

                    # Convert semester to year_of_study format for folder structure
                    year_of_study = self.convert_semester_to_year_of_study(semester)

                    # Create a structured folder path
                    debug_folder = debug_dir / academic_year / year_of_study / branch / section
                    debug_folder.mkdir(parents=True, exist_ok=True)

                    # Save HTML content
                    html_content = self.page.content()
                    html_path = debug_folder / "after_click.html"
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(html_content)
                    logger.debug(f"Saved HTML content after clicking to {html_path}")

                    # Also take a screenshot after clicking
                    screenshot_path = debug_folder / "after_click.png"
                    self.page.screenshot(path=str(screenshot_path))
                    logger.debug(f"Saved screenshot after clicking button to {screenshot_path}")

                # Check if we got results
                page_content = self.page.content()
                if 'No Records Found' in page_content:
                    logger.warning(f"No records found for {academic_year}, {semester}, {branch}, {section}")
                    return None

                # Check if we have student rows (a good indicator of success)
                student_rows = self.page.query_selector_all('tr[id]')
                if student_rows:
                    logger.info(f"Found {len(student_rows)} student rows with IDs - form submission successful")
                    return page_content
                else:
                    logger.warning("No student rows found in the result - form submission may have failed")
                    return None
            else:
                logger.warning("Could not find show button")
                return None

        except Exception as e:
            logger.error(f"Error submitting form: {e}")
            return None

    def convert_semester_to_year_of_study(self, semester: str) -> str:
        """
        Convert semester string to year-of-study format.

        Args:
            semester: Semester string (e.g., "First Yr - First Sem")

        Returns:
            Year of study string (e.g., "1-1")
        """
        semester_map = {
            "First Yr - First Sem": "1-1",
            "First Yr - Second Sem": "1-2",
            "Second Yr - First Sem": "2-1",
            "Second Yr - Second Sem": "2-2",
            "Third Yr - First Sem": "3-1",
            "Third Yr - Second Sem": "3-2",
            "Final Yr - First Sem": "4-1",
            "Final Yr - Second Sem": "4-2",
            "Fourth Yr - First Sem": "4-1",  # Keep for backward compatibility
            "Fourth Yr - Second Sem": "4-2"   # Keep for backward compatibility
        }

        return semester_map.get(semester, semester)

    @retry_on_network_error()
    def extract_attendance_data(self, html_content: str, academic_year: str, semester: str, branch: str, section: str) -> List[Dict[str, Any]]:
        """
        Extract attendance data from the page.

        Args:
            html_content: HTML content of the page
            academic_year: Academic year
            semester: Semester
            branch: Branch (e.g., "CSE")
            section: Section (e.g., "A")

        Returns:
            List of dictionaries containing attendance data
        """
        try:
            # Save the HTML content for debugging in a structured folder (only if --save-debug is enabled)
            if self.settings.get('save_debug', False):
                debug_dir = Path("debug_output")
                debug_dir.mkdir(parents=True, exist_ok=True)  # Create debug_output directory if it doesn't exist

                # Convert semester to year_of_study format for folder structure
                year_of_study = self.convert_semester_to_year_of_study(semester)

                # Create a structured folder path
                debug_folder = debug_dir / academic_year / year_of_study / branch / section
                debug_folder.mkdir(parents=True, exist_ok=True)

                # Create a filename
                filename = f"attendance_debug.html"
                filepath = debug_folder / filename

                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                logger.debug(f"Saved HTML content to {filepath}")

            # Direct extraction using Playwright
            attendance_data = []

            # Find all rows with IDs (these are student rows in the attendance table)
            student_rows = self.page.query_selector_all('tr[id]')
            if student_rows:
                logger.info(f"Found {len(student_rows)} student rows with IDs")

                for tr in student_rows:
                    try:
                        # Get the roll number from the row ID
                        roll_number = tr.get_attribute('id') or ''
                        roll_number = roll_number.strip()
                        if not roll_number:
                            continue

                        # Extract just the roll number part if it has a date in parentheses
                        if '(' in roll_number and ')' in roll_number:
                            # Extract the part before the opening parenthesis
                            roll_number = roll_number.split('(')[0].strip().replace(' ', '')

                        # Create student data dictionary
                        student_data = {
                            'roll_number': roll_number,
                            'data_type': 'attendance',
                            'academic_year': academic_year,
                            'semester': semester,
                            'branch': branch,
                            'section': section,
                            'data': {}
                        }

                        # Extract roll number from tdRollNo class if available
                        td_roll_no = tr.query_selector('td.tdRollNo')
                        if td_roll_no:
                            # First try to get the roll number from the id attribute (removing 'td' prefix)
                            id_attr = td_roll_no.get_attribute('id') or ''
                            if id_attr and id_attr.startswith('td'):
                                roll_number = id_attr[2:]  # Remove 'td' prefix
                                student_data['roll_number'] = roll_number
                            # If no id attribute or it doesn't start with 'td', use the text content
                            else:
                                roll_number_text = td_roll_no.text_content()
                                roll_number_text = roll_number_text.strip().replace(' ', '')
                                if roll_number_text:
                                    # Extract just the roll number part if it has a date in parentheses
                                    if '(' in roll_number_text and ')' in roll_number_text:
                                        roll_number_text = roll_number_text.split('(')[0].strip().replace(' ', '')
                                    student_data['roll_number'] = roll_number_text

                        # Extract attendance percentage from tdPercent class
                        td_percent = tr.query_selector('td.tdPercent')
                        if td_percent:
                            # The percentage is the first text content
                            percent_text = td_percent.text_content()
                            percent_text = percent_text.strip()

                            # Extract percentage (first part) and total classes (in parentheses)
                            if '(' in percent_text and ')' in percent_text:
                                parts = percent_text.split('(')
                                student_data['data']['attendance_percentage'] = parts[0].strip()
                                total_classes = parts[1].replace(')', '').strip()
                                student_data['data']['total_classes'] = total_classes
                            else:
                                student_data['data']['attendance_percentage'] = percent_text

                        # Extract subject data from cells with title attributes
                        subject_cells = tr.query_selector_all('td[title]')
                        for cell in subject_cells:
                            subject_name = cell.get_attribute('title') or ''
                            subject_name = subject_name.strip()
                            if subject_name:
                                value = cell.text_content()
                                value = value.strip()
                                if value:  # Only add non-empty values
                                    student_data['data'][self.normalize_key(subject_name)] = value

                        # Only add if we have actual data
                        if student_data['data']:
                            attendance_data.append(student_data)

                    except Exception as e:
                        logger.error(f"Error extracting data from student row: {e}")

                if attendance_data:
                    logger.info(f"Extracted attendance data for {len(attendance_data)} students using direct extraction")
                    return attendance_data

            # If we couldn't extract data using the direct method, try using a more general approach
            # This would be similar to the original code's fallback approaches, but using Playwright selectors

            # For simplicity, we'll just implement one fallback approach here
            # Look for any table that might contain attendance data
            tables = self.page.query_selector_all('table')
            logger.info(f"Found {len(tables)} tables, trying to extract data from them")

            for table in tables:
                rows = table.query_selector_all('tr')
                if len(rows) <= 1:  # Skip tables with only header row
                    continue

                # Get headers from first row
                header_row = rows[0]
                header_cells = header_row.query_selector_all('th, td')
                headers = []
                for cell in header_cells:
                    header_text = cell.text_content()
                    headers.append(header_text.strip())

                # Find the roll number column index
                roll_idx = -1
                roll_patterns = ['roll', 'id', 'no', 'number', 'student id', 'student no', 'admission']

                for i, header in enumerate(headers):
                    header_lower = header.lower()
                    if any(pattern in header_lower for pattern in roll_patterns):
                        roll_idx = i
                        logger.debug(f"Found roll number column at index {i}: '{header}'")
                        break

                # If we can't find a roll number column, skip this table
                if roll_idx == -1:
                    continue

                # Process data rows
                for row_idx in range(1, len(rows)):  # Skip header row
                    row = rows[row_idx]
                    cells = row.query_selector_all('td, th')

                    if len(cells) <= roll_idx:
                        continue  # Skip rows with insufficient cells

                    # Extract roll number
                    roll_cell = cells[roll_idx]
                    roll_number = roll_cell.text_content()
                    roll_number = roll_number.strip().replace(' ', '')

                    if not roll_number:
                        continue  # Skip rows without roll number

                    # Extract just the roll number part if it has a date in parentheses
                    if '(' in roll_number and ')' in roll_number:
                        roll_number = roll_number.split('(')[0].strip().replace(' ', '')

                    # Create student data dictionary
                    student_data = {
                        'roll_number': roll_number,
                        'data_type': 'attendance',
                        'academic_year': academic_year,
                        'semester': semester,
                        'branch': branch,
                        'section': section,
                        'data': {}
                    }

                    # Extract other data
                    for i, cell in enumerate(cells):
                        if i != roll_idx and i < len(headers):
                            key = self.normalize_key(headers[i])
                            value = cell.text_content()
                            value = value.strip()

                            if value:  # Only add non-empty values
                                student_data['data'][key] = value

                            # Also check for title attribute which might contain subject names
                            title = cell.get_attribute('title')
                            if title:
                                title_key = self.normalize_key(title)
                                if title_key != key and value:  # Avoid duplicates and empty values
                                    student_data['data'][title_key] = value

                    # Only add student data if we have actual data
                    if student_data['data']:
                        attendance_data.append(student_data)

                # If we found data in this table, return it
                if attendance_data:
                    logger.info(f"Extracted attendance data for {len(attendance_data)} students from table")
                    return attendance_data

            # If we still couldn't extract any data, return an empty list
            logger.warning(f"No student rows found for {academic_year}, {semester}, {branch}, {section}")
            return []

        except Exception as e:
            logger.error(f"Error extracting attendance data: {str(e)}")
            return []

    def close(self):
        """
        Close the browser and clean up resources.
        """
        # Use a flag to track if we've already closed resources
        already_closed = False

        try:
            if hasattr(self, 'page') and self.page:
                try:
                    self.page.close()
                    logger.debug("Page closed successfully")
                except Exception as e:
                    logger.error(f"Error closing page: {str(e)}")
                finally:
                    self.page = None

            if hasattr(self, 'context') and self.context:
                try:
                    self.context.close()
                    logger.debug("Browser context closed successfully")
                except Exception as e:
                    logger.error(f"Error closing browser context: {str(e)}")
                finally:
                    self.context = None

            if hasattr(self, 'browser') and self.browser:
                try:
                    self.browser.close()
                    logger.debug("Browser closed successfully")
                except Exception as e:
                    logger.error(f"Error closing browser: {str(e)}")
                finally:
                    self.browser = None

            if hasattr(self, 'playwright') and self.playwright:
                try:
                    self.playwright.stop()
                    logger.debug("Playwright stopped successfully")
                except Exception as e:
                    logger.error(f"Error stopping playwright: {str(e)}")
                finally:
                    self.playwright = None

            self.logged_in = False
            already_closed = True
        except Exception as e:
            logger.error(f"Error during close: {str(e)}")
            if not already_closed:
                # Force cleanup if we haven't already done it
                self.page = None
                self.context = None
                self.browser = None
                self.playwright = None
                self.logged_in = False



    def normalize_key(self, key: str) -> str:
        """
        Normalize a key string by converting to lowercase and replacing spaces with underscores.

        Args:
            key: The key string to normalize

        Returns:
            Normalized key string
        """
        return key.lower().replace(' ', '_').replace('-', '_')

    def convert_semester_to_year_of_study(self, semester: str) -> str:
        """
        Convert semester string to year-of-study format.

        Args:
            semester: Semester string (e.g., "First Yr - First Sem")

        Returns:
            Year of study string (e.g., "1-1")
        """
        semester_map = {
            "First Yr - First Sem": "1-1",
            "First Yr - Second Sem": "1-2",
            "Second Yr - First Sem": "2-1",
            "Second Yr - Second Sem": "2-2",
            "Third Yr - First Sem": "3-1",
            "Third Yr - Second Sem": "3-2",
            "Final Yr - First Sem": "4-1",
            "Final Yr - Second Sem": "4-2",
            "Fourth Yr - First Sem": "4-1",  # Keep for backward compatibility
            "Fourth Yr - Second Sem": "4-2"   # Keep for backward compatibility
        }

        return semester_map.get(semester, semester)

    def save_to_csv(self, data: List[Dict[str, Any]], filename: str = "attendance_data.csv",
                   academic_year: str = None, year_of_study: str = None) -> None:
        """
        Save the attendance data to a CSV file in a structured folder system.

        Args:
            data: List of dictionaries containing attendance data
            filename: Name of the output CSV file
            academic_year: Academic year (e.g., "2023-24")
            year_of_study: Year of study (e.g., "1-1")
        """
        if not data:
            logger.warning("No attendance data to save")
            return

        # Create a structured folder path for CSV files
        csv_base_dir = Path("csv_details")

        # If academic_year and year_of_study are provided, use them for folder structure
        if academic_year and year_of_study:
            csv_folder = csv_base_dir / academic_year / year_of_study
        else:
            # Otherwise, just use the base directory
            csv_folder = csv_base_dir

        # Create the folder if it doesn't exist
        csv_folder.mkdir(parents=True, exist_ok=True)

        # Full path to the CSV file
        csv_path = csv_folder / filename

        try:
            if PANDAS_AVAILABLE:
                # Use pandas if available
                df = pd.DataFrame(data)
                df.to_csv(csv_path, index=False)
                logger.info(f"Saved {len(data)} attendance records to {csv_path}")
            else:
                # Fallback to using the csv module
                import csv
                with open(csv_path, 'w', newline='') as csvfile:
                    # Get field names from the first dictionary
                    fieldnames = data[0].keys()
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(data)
                logger.info(f"Saved {len(data)} attendance records to {csv_path}")
        except Exception as e:
            logger.error(f"Error saving data to CSV: {e}")


    def store_attendance_data(self, attendance_data: List[Dict[str, Any]], force_update: bool = False) -> Tuple[int, int]:
        """
        Store attendance data in a structured folder system.

        Args:
            attendance_data: List of dictionaries containing attendance data
            force_update: Whether to force update even if data already exists

        Returns:
            Tuple of (success_count, update_count)
        """
        success_count = 0
        update_count = 0

        # Check if we have valid data to store
        if not attendance_data:
            logger.warning("No attendance data to store")
            return success_count, update_count

        # Validate data structure before storing
        for student in attendance_data:
            # Check if student data has the required fields and non-empty data
            if not all(k in student for k in ['roll_number', 'academic_year', 'semester', 'branch', 'section', 'data']):
                logger.warning(f"Skipping invalid student data: {student}")
                continue

            # Check if data dictionary is not empty
            if not student['data']:
                logger.warning(f"Skipping student with empty data: {student['roll_number']}")
                continue

            try:
                # Extract student information
                roll_number = student['roll_number']
                academic_year = student['academic_year']
                semester = student['semester']
                branch = student['branch']
                section = student['section']
                data = student['data']

                # Convert semester to year_of_study format
                year_of_study = self.convert_semester_to_year_of_study(semester)

                # Create folder structure (without branch and section folders)
                student_folder = self.base_dir / academic_year / year_of_study / roll_number
                student_folder.mkdir(parents=True, exist_ok=True)

                # Save attendance data
                attendance_file = student_folder / "attendance.json"

                # Save branch and section information in roll_number.json file
                self.store_student_info(student_folder, roll_number, branch, section)

                # Check if file exists and compare data
                should_update = True
                if attendance_file.exists() and not force_update:
                    try:
                        with open(attendance_file, 'r') as f:
                            existing_data = json.load(f)

                        # Simple comparison - if data is the same, don't update
                        if existing_data == data:
                            should_update = False
                        else:
                            # Log what changed
                            changes = []
                            for key in set(data.keys()) | set(existing_data.keys()):
                                if key not in existing_data:
                                    changes.append(f"Added {key}: {data[key]}")
                                elif key not in data:
                                    changes.append(f"Removed {key}")
                                elif existing_data[key] != data[key]:
                                    changes.append(f"Changed {key}: {existing_data[key]} -> {data[key]}")

                            if changes:
                                logger.debug(f"Changes for {roll_number}: {', '.join(changes[:3])}" +
                                             (f" and {len(changes) - 3} more" if len(changes) > 3 else ""))
                    except Exception as e:
                        logger.warning(f"Error reading existing data for {roll_number}: {e}")
                        should_update = True

                if should_update:
                    with open(attendance_file, 'w') as f:
                        json.dump(data, f, indent=2)

                    # No need to update roll index as we now store this info in the student folder

                    logger.info(f"Updated attendance data for student {roll_number}")
                    update_count += 1
                else:
                    logger.debug(f"No changes detected for student {roll_number}, skipping update")

                success_count += 1

            except Exception as e:
                logger.error(f"Error storing attendance data for student {student.get('roll_number', 'unknown')}: {str(e)}")

        return success_count, update_count

    def store_student_info(self, student_folder: Path, roll_number: str, branch: str, section: str) -> bool:
        """
        Store student branch and section information in the student folder.

        Args:
            student_folder: Path to the student folder
            roll_number: Student roll number
            branch: Branch
            section: Section

        Returns:
            True if successful, False otherwise
        """
        try:
            # Create student info file
            student_info_file = student_folder / f"{roll_number}.json"
            student_info_data = {
                "roll_number": roll_number,
                "branch": branch,
                "section": section,
                "last_updated": datetime.now().isoformat()
            }

            with open(student_info_file, 'w') as f:
                json.dump(student_info_data, f, indent=2)

            logger.debug(f"Stored student info for {roll_number}")
            return True
        except Exception as e:
            logger.error(f"Error storing student info for {roll_number}: {str(e)}")
            return False






def worker_function(worker_id: int, combinations: List[Tuple], args: argparse.Namespace):
    """
    Worker function to process combinations.

    Args:
        worker_id: ID of the worker
        combinations: List of combinations to process
        args: Command line arguments

    Returns:
        List of results
    """
    # Initialize logging for this worker
    worker_logger = logging.getLogger(f"worker-{worker_id}")
    worker_logger.setLevel(getattr(logging, args.log_level))

    # Initialize the scraper with command line credentials or defaults from config
    username = args.username if args.username else USERNAME
    password = args.password if args.password else PASSWORD
    headless = args.headless if args.headless is not None else DEFAULT_SETTINGS['headless']
    save_debug = args.save_debug

    # Create the scraper with all settings
    scraper = AttendanceScraper(
        username=username,
        password=password,
        base_dir=args.data_dir,
        headless=headless,
        max_retries=args.max_retries,
        timeout=args.timeout,
        save_debug=save_debug
    )

    # Authenticate
    if not scraper.authenticate():
        worker_logger.error(f"Worker {worker_id}: Authentication failed. Exiting.")
        return [(worker_id, "auth_failed", None)]

    # Navigate to attendance page
    content = scraper.navigate_to_attendance_page()
    if not content:
        worker_logger.warning(f"Worker {worker_id}: Failed to navigate to attendance page using Playwright.")
        worker_logger.info(f"Worker {worker_id}: Will try to continue with requests-based scraping.")
        # We'll continue and let the individual methods handle the fallback to requests

    worker_logger.info(f"Worker {worker_id}: Ready to process combinations")

    # Process combinations
    results = []
    empty_combinations_in_a_row = 0
    max_empty_combinations = 10  # Stop after this many empty combinations in a row
    combinations_processed = 0
    combinations_with_data = 0

    for i, combination in enumerate(combinations):
        try:
            academic_year, semester, branch, section = combination
            combinations_processed += 1
            worker_logger.info(f"Worker {worker_id}: Processing combination {i+1}/{len(combinations)}: {academic_year}, {semester}, {branch}, {section}")

            # Add delay between requests if specified
            if i > 0 and args.delay > 0:
                worker_logger.debug(f"Worker {worker_id}: Sleeping for {args.delay} seconds...")
                time.sleep(args.delay)

            # Select form filters and submit
            result_content = scraper.select_form_filters(academic_year, semester, branch, section)
            if not result_content:
                worker_logger.warning(f"Worker {worker_id}: Failed to get results for {academic_year}, {semester}, {branch}, {section}")
                results.append((worker_id, "no_results", combination))
                continue

            # Extract and save data
            worker_logger.info(f"Worker {worker_id}: Successfully navigated to attendance results page")
            attendance_data = scraper.extract_attendance_data(result_content, academic_year, semester, branch, section)

            if not attendance_data:
                worker_logger.warning(f"Worker {worker_id}: No attendance data found for {academic_year}, {semester}, {branch}, {section}")
                empty_combinations_in_a_row += 1
                results.append((worker_id, "no_data", combination))

                # If we've seen too many empty combinations in a row, stop
                if empty_combinations_in_a_row >= max_empty_combinations and args.skip_empty:
                    worker_logger.warning(f"Worker {worker_id}: Found {empty_combinations_in_a_row} empty combinations in a row. Stopping.")
                    break

                continue

            # Reset the counter since we found data
            empty_combinations_in_a_row = 0
            combinations_with_data += 1

            # Store data in structured format
            success_count, update_count = scraper.store_attendance_data(attendance_data, args.force_update)
            worker_logger.info(f"Worker {worker_id}: Processed {success_count} students with {update_count} updates")

            # Also save to CSV if not disabled
            if not args.no_csv:
                # Convert semester to year_of_study format for folder structure
                year_of_study = scraper.convert_semester_to_year_of_study(semester)

                # Create a filename for the CSV
                csv_filename = f"{branch}_{section}_attendance_data.csv"

                # Save to CSV
                scraper.save_to_csv(attendance_data, csv_filename, academic_year, year_of_study)

            # Add the result
            results.append((worker_id, "success", (combination, len(attendance_data))))

        except Exception as e:
            worker_logger.error(f"Worker {worker_id}: Error processing combination: {str(e)}")
            results.append((worker_id, "error", (combination if 'combination' in locals() else None, str(e))))

    # Clean up
    try:
        scraper.close()
    except Exception as e:
        worker_logger.error(f"Worker {worker_id}: Error closing scraper: {str(e)}")

    worker_logger.info(f"Worker {worker_id}: Finished processing {combinations_processed} combinations with {combinations_with_data} containing data")
    results.append((worker_id, "finished", (combinations_processed, combinations_with_data)))

    return results

def main():
    """Main function to run the scraper."""
    parser = argparse.ArgumentParser(description='Scrape attendance data from college website')
    parser.add_argument('--username', help='Login username (defaults to config.USERNAME)')
    parser.add_argument('--password', help='Login password (defaults to config.PASSWORD)')
    parser.add_argument('--output', default='attendance_data.csv', help='Output file name')
    parser.add_argument('--no-csv', action='store_true', help='Disable CSV file generation')
    parser.add_argument('--academic-year', choices=DEFAULT_ACADEMIC_YEARS, help='Academic year')
    parser.add_argument('--semester', choices=DEFAULT_SEMESTERS, help='Semester')
    parser.add_argument('--branch', choices=list(BRANCH_CODES.keys()), help='Branch')
    parser.add_argument('--section', choices=DEFAULT_SECTIONS, help='Section')
    parser.add_argument('--data-dir', default=DEFAULT_SETTINGS['data_dir'], help='Directory to store student data')
    parser.add_argument('--force-update', action='store_true', help='Force update even if data already exists')
    parser.add_argument('--headless', action='store_true', help='Run in headless mode')
    parser.add_argument('--max-retries', type=int, default=DEFAULT_SETTINGS['max_retries'], help='Maximum number of retries for network errors')
    parser.add_argument('--timeout', type=int, default=DEFAULT_SETTINGS['timeout'], help='Timeout in seconds for waiting for elements')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    parser.add_argument('--save-debug', action='store_true', help='Save debug files (HTML, screenshots, etc.)')
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default='INFO', help='Set logging level')
    parser.add_argument('--playwright', action='store_true', help='Use Playwright for browser automation (default)')

    # Additional arguments for controlling the scraping process
    parser.add_argument('--max-combinations', type=int, default=0, help='Maximum number of combinations to try (0 for all)')
    parser.add_argument('--delay', type=float, default=1.0, help='Delay between requests in seconds')
    parser.add_argument('--skip-empty', action='store_true', help='Skip combinations with no data')
    parser.add_argument('--reverse', action='store_true', help='Reverse the order of combinations (oldest first)')
    parser.add_argument('--only-years', nargs='+', choices=DEFAULT_ACADEMIC_YEARS, help='Only scrape specific academic years')
    parser.add_argument('--only-semesters', nargs='+', choices=DEFAULT_SEMESTERS, help='Only scrape specific semesters')
    parser.add_argument('--only-branches', nargs='+', choices=list(BRANCH_CODES.keys()), help='Only scrape specific branches')
    parser.add_argument('--only-sections', nargs='+', choices=DEFAULT_SECTIONS, help='Only scrape specific sections')

    # Multi-worker options
    parser.add_argument('--workers', type=int, default=1, help='Number of worker processes for parallel scraping')

    args = parser.parse_args()

    # Set logging level
    if args.log_level:
        logging.getLogger().setLevel(getattr(logging, args.log_level))
        for handler in logging.getLogger().handlers:
            handler.setLevel(getattr(logging, args.log_level))

    # Set save_debug flag based on args.save_debug
    save_debug = args.save_debug

    # Create debug directory if needed and save_debug is enabled
    if save_debug:
        debug_dir = Path("debug_output")
        debug_dir.mkdir(exist_ok=True)
        logger.info(f"Debug file saving enabled. Debug files will be saved to {debug_dir}")

    # Use the provided filters or try all combinations
    if args.academic_year and args.semester and args.branch and args.section:
        # Use the provided filters
        combinations = [
            (args.academic_year, args.semester, args.branch, args.section)
        ]
    else:
        # Determine which academic years to use
        if args.only_years:
            academic_years = args.only_years
        elif args.academic_year:
            academic_years = [args.academic_year]
        else:
            academic_years = DEFAULT_ACADEMIC_YEARS

        # Determine which semesters to use
        if args.only_semesters:
            semesters = args.only_semesters
        elif args.semester:
            semesters = [args.semester]
        else:
            semesters = DEFAULT_SEMESTERS

        # Determine which branches to use
        if args.only_branches:
            branches = args.only_branches
        elif args.branch:
            branches = [args.branch]
        else:
            branches = DEFAULT_BRANCHES

        # Determine which sections to use
        if args.only_sections:
            sections = args.only_sections
        elif args.section:
            sections = [args.section]
        else:
            sections = DEFAULT_SECTIONS

        # Create all combinations
        combinations = [
            (year, sem, branch, section)
            for year in academic_years
            for sem in semesters
            for branch in branches
            for section in sections
        ]

        # Reverse the order if requested (oldest first)
        if args.reverse:
            combinations.reverse()

        # Limit the number of combinations if requested
        if args.max_combinations > 0 and len(combinations) > args.max_combinations:
            logger.info(f"Limiting to {args.max_combinations} combinations out of {len(combinations)}")
            combinations = combinations[:args.max_combinations]

        logger.info(f"Generated {len(combinations)} combinations to try")

    # Determine whether to use multi-worker mode
    num_workers = min(args.workers, len(combinations))
    logger.info(f"Number of workers: {num_workers}")

    if num_workers > 1:
        logger.info(f"Using {num_workers} workers in parallel mode")

        # Split combinations among workers
        combinations_per_worker = [[] for _ in range(num_workers)]
        for i, combination in enumerate(combinations):
            combinations_per_worker[i % num_workers].append(combination)

        # Create and run workers using multiprocessing
        with multiprocessing.Pool(processes=num_workers) as pool:
            worker_args = [(i+1, combinations_per_worker[i], args) for i in range(num_workers)]
            all_results = pool.starmap(worker_function, worker_args)

        # Flatten results
        results = []
        for worker_results in all_results:
            results.extend(worker_results)

        # Process results
        total_combinations_tried = 0
        total_combinations_with_data = 0
        total_students_found = 0

        for worker_id, status, data in results:
            if status == "success":
                combination, num_students = data
                total_combinations_with_data += 1
                total_students_found += num_students
                logger.info(f"Worker {worker_id} successfully processed {combination} with {num_students} students")
            elif status == "finished":
                combinations_processed, combinations_with_data = data
                total_combinations_tried += combinations_processed
                logger.info(f"Worker {worker_id} finished processing {combinations_processed} combinations with {combinations_with_data} containing data")

    else:
        # Use single-worker mode
        logger.info("Using single-worker mode")
        results = worker_function(1, combinations, args)

        # Process results
        total_combinations_tried = 0
        total_combinations_with_data = 0
        total_students_found = 0

        for worker_id, status, data in results:
            if status == "success":
                combination, num_students = data
                total_combinations_with_data += 1
                total_students_found += num_students
            elif status == "finished":
                combinations_processed, combinations_with_data = data
                total_combinations_tried += combinations_processed

    # Print summary statistics
    logger.info("\n" + "="*80)
    logger.info("SCRAPING SUMMARY")
    logger.info("="*80)
    logger.info(f"Total combinations tried: {total_combinations_tried} / {len(combinations)}")
    logger.info(f"Combinations with data: {total_combinations_with_data}")
    logger.info(f"Total students found: {total_students_found}")
    logger.info(f"Data directory: {args.data_dir}")

    if total_students_found > 0:
        logger.info("\nAttendance scraping completed successfully!")
        logger.info(f"Found data for {total_students_found} students across {total_combinations_with_data} combinations.")
    else:
        logger.warning("\nNo attendance data found for any combination.")
        logger.warning("Try different parameters or check the website structure.")

    logger.info("="*80)


if __name__ == "__main__":
    main()
