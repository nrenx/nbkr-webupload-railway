#!/usr/bin/env python3
"""
Mid Marks Scraper for College Website

This script logs into the college portal and navigates to the mid marks page.
It extracts mid-term marks data and stores it in a structured format.
"""

import os
import sys
import logging
import argparse
import json
import time
import re
import queue
import threading
import multiprocessing
import concurrent.futures
import requests
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Union
from functools import wraps

from bs4 import BeautifulSoup

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
from login_utils import create_session, login, BASE_URL
from config import (
    USERNAME, PASSWORD, MID_MARKS_PORTAL_URL,
    DEFAULT_ACADEMIC_YEARS, DEFAULT_SEMESTERS,
    DEFAULT_BRANCHES, DEFAULT_SECTIONS,
    YEAR_SEM_CODES, BRANCH_CODES, DEFAULT_SETTINGS
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("mid_marks_scraper.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("mid_marks_scraper")


# We already have a login function imported from login_utils


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
                except (requests.exceptions.RequestException, ConnectionError, TimeoutError, PlaywrightTimeoutError) as e:
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

class MidMarksScraper:
    """
    A class to scrape mid-term marks data from the college website.
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

        # Initialize session for requests-based scraping
        self.session = create_session()

        # Configure session based on headless mode
        if self.headless:
            # In headless mode, use a more browser-like User-Agent
            self.session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            })
            logger.info("Initialized mid marks scraper in headless mode")
        else:
            logger.info("Initialized mid marks scraper in interactive mode")

        # Initialize Playwright if available
        if PLAYWRIGHT_AVAILABLE:
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
                                logger.warning("Playwright initialization failed, falling back to requests-based scraping")
                                p.stop()
                                return
                        except Exception as e2:
                            logger.error(f"Failed to launch browser with system Chrome: {e2}")
                            logger.warning("Playwright initialization failed, falling back to requests-based scraping")
                            p.stop()
                            return
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
            except Exception as e:
                logger.error(f"Error initializing browser: {e}")
                # Clean up any resources that might have been created
                self.close()
                logger.warning("Playwright initialization failed, falling back to requests-based scraping")
        else:
            logger.warning("Playwright is not available. Using requests-based scraping only.")

    def __del__(self):
        """Clean up resources when the object is destroyed."""
        self.close()

    def close(self):
        """Close the browser and clean up resources."""
        # Use a flag to track if we've already closed resources
        already_closed = False

        try:
            # Close Playwright resources if available
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

            # Reset login state
            self.logged_in = False

            already_closed = True
        except Exception as e:
            logger.error(f"Error during close: {str(e)}")
            if not already_closed:
                # Force cleanup if we haven't already done it
                if hasattr(self, 'page'):
                    self.page = None
                if hasattr(self, 'context'):
                    self.context = None
                if hasattr(self, 'browser'):
                    self.browser = None
                if hasattr(self, 'playwright'):
                    self.playwright = None
                self.logged_in = False

    @retry_on_network_error()
    def authenticate(self, retry_count=0) -> bool:
        """
        Authenticate with the college portal.

        Args:
            retry_count: Current retry attempt (used for recursive retries)

        Returns:
            Boolean indicating success
        """
        # Check if we've exceeded max retries
        if retry_count >= self.max_retries:
            logger.error(f"Authentication failed after {retry_count} retries. Giving up.")
            return False

        # If already logged in, check if the session is still valid
        if self.logged_in:
            try:
                # Try to access a protected page to verify session
                if self.page:
                    # Get current URL to check after verification
                    current_url = self.page.url

                    # Try to access the mid marks page
                    self.page.goto(MID_MARKS_PORTAL_URL)
                    time.sleep(1)

                    # If we're redirected to login page, session is invalid
                    if "login" in self.page.url.lower():
                        logger.warning("Session expired. Re-authenticating...")
                        self.logged_in = False
                    else:
                        # Session is still valid
                        logger.debug("Session is still valid")
                        # Navigate back to original URL if needed
                        if current_url != self.page.url:
                            self.page.goto(current_url)
                        return True
                else:
                    # Use requests to verify session
                    response = self.session.get(MID_MARKS_PORTAL_URL, timeout=self.timeout, allow_redirects=False)
                    if response.status_code >= 300 and response.status_code < 400:
                        # Redirect indicates session expired
                        logger.warning("Session expired. Re-authenticating...")
                        self.logged_in = False
                    else:
                        # Session is still valid
                        logger.debug("Session is still valid")
                        return True
            except Exception as e:
                logger.warning(f"Error verifying session: {e}. Re-authenticating...")
                self.logged_in = False

        # Try to authenticate using Playwright if available
        if self.page:
            try:
                logger.info(f"Authenticating using Playwright (attempt {retry_count + 1}/{self.max_retries + 1})...")
                # Go directly to the mid marks portal URL which will redirect to login page
                self.page.goto(MID_MARKS_PORTAL_URL)

                # Wait for the login form to load
                self.page.wait_for_selector('input[name="username"]', state='visible')

                # Log the current URL to verify we're on the login page
                logger.debug(f"Current URL after navigation: {self.page.url}")

                # Fill in the login form
                self.page.fill('input[name="username"]', self.username)
                self.page.fill('input[name="password"]', self.password)

                # Submit the form
                self.page.click('input[type="submit"]')

                # Wait for navigation to complete
                self.page.wait_for_load_state('networkidle')

                # Check if login was successful
                if "login" not in self.page.url.lower():
                    self.logged_in = True
                    logger.info("Login successful using Playwright")
                    return True
                else:
                    logger.error("Login failed using Playwright")

                    # If we still have retries left, try again after a delay
                    if retry_count < self.max_retries:
                        retry_delay = min(2 ** retry_count, 30)  # Exponential backoff with max of 30 seconds
                        logger.info(f"Retrying authentication in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        return self.authenticate(retry_count + 1)
                    return False

            except Exception as e:
                logger.error(f"Error authenticating using Playwright: {e}")
                # If we still have retries left, try again after a delay
                if retry_count < self.max_retries:
                    retry_delay = min(2 ** retry_count, 30)  # Exponential backoff with max of 30 seconds
                    logger.info(f"Retrying authentication in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    return self.authenticate(retry_count + 1)
                # Fall back to requests-based authentication

        # Use requests-based authentication as fallback
        logger.info(f"Authenticating using requests (attempt {retry_count + 1}/{self.max_retries + 1})...")
        success, error_msg = login(self.session, self.username, self.password)
        if success:
            self.logged_in = True
            return True
        else:
            logger.error(f"Authentication failed: {error_msg}")

            # If we still have retries left, try again after a delay
            if retry_count < self.max_retries:
                retry_delay = min(2 ** retry_count, 30)  # Exponential backoff with max of 30 seconds
                logger.info(f"Retrying authentication in {retry_delay} seconds...")
                time.sleep(retry_delay)
                return self.authenticate(retry_count + 1)
            return False

    @retry_on_network_error()
    def navigate_to_mid_marks_page(self) -> Optional[BeautifulSoup]:
        """
        Navigate to the mid marks page.

        Returns:
            BeautifulSoup object of the page if successful, None otherwise
        """
        if not self.logged_in and not self.authenticate():
            return None

        # Try to navigate using Playwright if available
        if self.page:
            try:
                # Navigate to the mid marks page
                logger.info(f"Navigating to mid marks page using Playwright: {MID_MARKS_PORTAL_URL}")
                self.page.goto(MID_MARKS_PORTAL_URL)

                # Wait for the page to load
                self.page.wait_for_load_state('networkidle')

                # Log the current URL for debugging
                logger.debug(f"Current URL after navigation: {self.page.url}")

                # Get the page content
                page_content = self.page.content()

                # Check if we're on the correct page
                if "mid_marks" in page_content.lower() or "marks" in page_content.lower():
                    logger.info("Successfully navigated to mid marks page using Playwright")
                    # Parse the HTML content
                    soup = BeautifulSoup(page_content, 'html.parser')
                    return soup
                else:
                    logger.warning("Navigation to mid marks page failed using Playwright - redirected to another page")
                    # Fall back to requests-based navigation
            except Exception as e:
                logger.error(f"Error navigating to mid marks page using Playwright: {e}")
                # Fall back to requests-based navigation

        # Use requests-based navigation as fallback
        try:
            logger.info(f"Navigating to mid marks page using requests: {MID_MARKS_PORTAL_URL}")
            response = self.session.get(MID_MARKS_PORTAL_URL, timeout=self.timeout)
            response.raise_for_status()

            # Parse the HTML content
            soup = BeautifulSoup(response.text, 'html.parser')

            # Check if we're on the correct page
            if "mid_marks" in response.text.lower() or "marks" in response.text.lower():
                logger.info("Successfully navigated to mid marks page using requests")
                return soup
            else:
                logger.warning("Navigation to mid marks page failed using requests - redirected to another page")
                return None
        except Exception as e:
            logger.error(f"Error navigating to mid marks page using requests: {e}")
            return None



    @retry_on_network_error()
    def select_form_filters(self, academic_year: str, semester: str, branch: str, section: str, is_mid_marks: bool = True) -> Optional[BeautifulSoup]:
        """
        Select form filters for attendance or mid marks data.

        Args:
            academic_year: Academic year to select (e.g., "2023-24")
            semester: Semester to select (e.g., "First Yr - First Sem")
            branch: Branch to select (e.g., "CSE")
            section: Section to select (e.g., "A")
            is_mid_marks: Whether to select filters for mid marks (True) or attendance (False)

        Returns:
            BeautifulSoup object of the results page or None if failed
        """
        # Navigate to the mid marks page
        soup = self.navigate_to_mid_marks_page()
        if not soup:
            return None

        # Try to use Playwright if available
        if self.page:
            try:
                logger.info(f"Using Playwright to select form filters: academic_year={academic_year}, semester={semester}, branch={branch}, section={section}")

                # Wait for the form to load
                self.page.wait_for_selector('select', state='visible')

                # Find all select elements
                select_elements = self.page.query_selector_all('select')
                if not select_elements:
                    logger.error("No select elements found in the form using Playwright")
                    # Fall back to requests-based form submission
                else:
                    # Find and set academic year
                    academic_year_set = False
                    try:
                        # Look for the academic year select element
                        acadYear = self.page.wait_for_selector('select[name="acadYear"]', state='visible')

                        # Select by value directly (more reliable)
                        acadYear.select_option(value=academic_year)
                        academic_year_set = True
                        logger.info(f"Selected academic year {academic_year} by value")
                    except Exception as e:
                        # Fall back to the original approach
                        for select in select_elements:
                            select_name = select.get_attribute('name') or ''
                            select_id = select.get_attribute('id') or ''
                            if 'year' in select_name.lower() or 'year' in select_id.lower() or 'academic' in select_name.lower():
                                # Try to find an option with text or value matching the academic year
                                option_found = False
                                options = select.query_selector_all('option')

                                for option in options:
                                    option_text = option.text_content().strip()
                                    if academic_year in option_text:
                                        option_value = option.get_attribute('value')
                                        select.select_option(value=option_value)
                                        academic_year_set = True
                                        option_found = True
                                        logger.info(f"Selected academic year {academic_year} by text")
                                        break

                                if not option_found:
                                    # If no match found, select the first option
                                    select.select_option(index=0)
                                    academic_year_set = True
                                    logger.info("Selected first academic year option as fallback")
                                break

                    if not academic_year_set:
                        logger.warning("Could not find academic year select element using Playwright")

                    # Find and set semester
                    semester_set = False
                    try:
                        # Convert semester to year_of_study format (e.g., "31" for 3rd year 1st sem)
                        year_of_study = self.convert_semester_to_year_of_study(semester)

                        # Look for the semester select element
                        yearSem = self.page.wait_for_selector('select[name="yearSem"]', state='visible')

                        # Select by value directly (more reliable)
                        yearSem.select_option(value=year_of_study)
                        semester_set = True
                        logger.info(f"Selected semester {semester} (code: {year_of_study}) by value")
                    except Exception as e:
                        # Fall back to the original approach
                        for select in select_elements:
                            select_name = select.get_attribute('name') or ''
                            select_id = select.get_attribute('id') or ''
                            if 'sem' in select_name.lower() or 'sem' in select_id.lower():
                                # Try to find an option with text matching the semester
                                option_found = False
                                options = select.query_selector_all('option')

                                for option in options:
                                    option_text = option.text_content().strip()
                                    if semester.lower() in option_text.lower():
                                        option_value = option.get_attribute('value')
                                        select.select_option(value=option_value)
                                        semester_set = True
                                        option_found = True
                                        logger.info(f"Selected semester {semester} by text")
                                        break

                                if not option_found:
                                    # If no match found, select the first option
                                    select.select_option(index=0)
                                    semester_set = True
                                    logger.info("Selected first semester option as fallback")
                                break

                    if not semester_set:
                        logger.warning("Could not find semester select element using Playwright")

                    # Find and set branch
                    branch_set = False
                    try:
                        # Get branch code from BRANCH_CODES mapping
                        branch_code = BRANCH_CODES.get(branch, branch)  # Use the code if branch is a name, otherwise use as is

                        # Look for the branch select element
                        branch_select = self.page.wait_for_selector('select[name="branch"]', state='visible')

                        # Select by value directly (more reliable)
                        branch_select.select_option(value=branch_code)
                        branch_set = True
                        logger.info(f"Selected branch {branch} (code: {branch_code}) by value")
                    except Exception as e:
                        # Fall back to the original approach
                        for select in select_elements:
                            select_name = select.get_attribute('name') or ''
                            select_id = select.get_attribute('id') or ''
                            if 'branch' in select_name.lower() or 'branch' in select_id.lower() or 'dept' in select_name.lower():
                                # Try to find an option with text matching the branch
                                option_found = False
                                options = select.query_selector_all('option')

                                for option in options:
                                    option_text = option.text_content().strip()
                                    if branch.lower() in option_text.lower():
                                        option_value = option.get_attribute('value')
                                        select.select_option(value=option_value)
                                        branch_set = True
                                        option_found = True
                                        logger.info(f"Selected branch {branch} by text")
                                        break

                                if not option_found:
                                    # If no match found, select the first option
                                    select.select_option(index=0)
                                    branch_set = True
                                    logger.info("Selected first branch option as fallback")
                                break

                    if not branch_set:
                        logger.warning("Could not find branch select element using Playwright")

                    # Find and set section
                    section_set = False
                    try:
                        # Look for the section select element
                        section_select = self.page.wait_for_selector('select[name="section"]', state='visible')

                        # Select by value directly (more reliable)
                        section_select.select_option(value=section)
                        section_set = True
                        logger.info(f"Selected section {section} by value")
                    except Exception as e:
                        # Fall back to the original approach
                        for select in select_elements:
                            select_name = select.get_attribute('name') or ''
                            select_id = select.get_attribute('id') or ''
                            if 'section' in select_name.lower() or 'section' in select_id.lower():
                                # Try to find an option with text matching the section
                                option_found = False
                                options = select.query_selector_all('option')

                                for option in options:
                                    option_text = option.text_content().strip()
                                    option_value = option.get_attribute('value')
                                    if section == option_text or section == option_value:
                                        select.select_option(value=option_value)
                                        section_set = True
                                        option_found = True
                                        logger.info(f"Selected section {section} by text")
                                        break

                                if not option_found:
                                    # If no match found, select the first option
                                    select.select_option(index=0)
                                    section_set = True
                                    logger.info("Selected first section option as fallback")
                                break

                    if not section_set:
                        logger.warning("Could not find section select element using Playwright")

                    # Find and click the show button (using the approach from your old project)
                    try:
                        # First try: Look for the Show button
                        show_button = None
                        try:
                            # For mid marks page, look for the Show button
                            show_button = self.page.wait_for_selector('input[type="button"][value="Show"]', state='visible', timeout=10000)
                            logger.info("Found Show button for mid marks using selector")
                        except Exception as e:
                            logger.debug(f"Could not find button using selector: {e}")

                        # Second try: Look for Show button
                        if not show_button:
                            try:
                                buttons = self.page.query_selector_all('input')
                                for button in buttons:
                                    # For mid marks page, look for button with 'show' in its value
                                    button_type = button.get_attribute('type')
                                    button_value = button.get_attribute('value')
                                    if button_type == 'button' and button_value and 'show' in button_value.lower():
                                        show_button = button
                                        logger.info(f"Found Show button for mid marks with value: {button_value}")
                                        break
                            except Exception as e:
                                logger.debug(f"Error searching for button by tag name: {e}")

                        # Third try: Look for any button as a fallback
                        if not show_button:
                            try:
                                buttons = self.page.query_selector_all('input')
                                for button in buttons:
                                    button_type = button.get_attribute('type')
                                    button_value = button.get_attribute('value')
                                    if button_type in ['button', 'submit']:
                                        show_button = button
                                        logger.info(f"Found button as fallback: {button_value} (type: {button_type})")
                                        break
                            except Exception as e:
                                logger.debug(f"Error searching for any button by tag name: {e}")

                        # Take a screenshot for debugging in a structured folder (only if --save-debug is enabled)
                        if self.settings.get('save_debug', False):
                            debug_dir = Path("debug_output")
                            debug_dir.mkdir(parents=True, exist_ok=True)  # Create debug_output directory if it doesn't exist

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
                            logger.info(f"Found button with value: {show_button.get_attribute('value')}")
                            # Scroll to the button to make sure it's visible
                            show_button.scroll_into_view_if_needed()
                            time.sleep(1)  # Wait for scroll to complete

                            # Click the button
                            show_button.click()
                            logger.info("Clicked show button")

                            # Wait for the results page to load
                            self.page.wait_for_load_state('networkidle')

                            # Save HTML content in debug mode in a structured folder (only if --save-debug is enabled)
                            if self.settings.get('save_debug', False):
                                debug_dir = Path("debug_output")
                                debug_dir.mkdir(parents=True, exist_ok=True)  # Create debug_output directory if it doesn't exist

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
                                logger.warning(f"No records found for {academic_year}, {semester}, {branch}, {section} using Playwright")
                                return None

                            # Parse the HTML content
                            result_soup = BeautifulSoup(page_content, 'html.parser')

                            # Check if we have student rows with IDs (a good indicator of success)
                            student_rows = result_soup.find_all('tr', attrs={'id': True})
                            if student_rows:
                                logger.info(f"Found {len(student_rows)} student rows with IDs - form submission successful")
                                return result_soup
                            else:
                                logger.warning("No student rows found in the result - form submission may have failed")
                                # Continue with fallback
                        else:
                            logger.warning("Could not find show button using Playwright")
                            # Fall back to requests-based form submission
                    except Exception as e:
                        logger.error(f"Error clicking show button: {e}")
                        # Fall back to requests-based form submission
            except Exception as e:
                logger.error(f"Error submitting form using Playwright: {e}")
                # Fall back to requests-based form submission

        # Use requests-based form submission as fallback
        try:
            # Get the form and its action URL
            form = soup.find('form')
            if not form:
                logger.error("Could not find form on attendance page")
                return None

            form_action = form.get('action', MID_MARKS_PORTAL_URL)
            if not form_action.startswith('http'):
                form_action = f"{BASE_URL}/{form_action.lstrip('/')}"

            # Extract the form fields and their values
            form_data = {}

            # Find all select elements and their options
            select_elements = form.find_all('select')
            for select in select_elements:
                select_name = select.get('name')
                if not select_name:
                    continue

                # Map our parameters to the form field names
                if 'year' in select_name.lower() or 'academic' in select_name.lower():
                    # This is likely the academic year field
                    form_data[select_name] = self.get_academic_year_value(select, academic_year)
                    logger.debug(f"Selected academic year: {academic_year} -> {form_data[select_name]}")

                elif 'sem' in select_name.lower():
                    # This is likely the semester field
                    form_data[select_name] = self.get_semester_value(select, semester)
                    logger.debug(f"Selected semester: {semester} -> {form_data[select_name]}")

                elif 'branch' in select_name.lower():
                    # This is likely the branch field
                    form_data[select_name] = self.get_branch_value(select, branch)
                    logger.debug(f"Selected branch: {branch} -> {form_data[select_name]}")

                elif 'section' in select_name.lower():
                    # This is likely the section field
                    form_data[select_name] = self.get_section_value(select, section)
                    logger.debug(f"Selected section: {section} -> {form_data[select_name]}")

                else:
                    # For other fields, just use the first option value
                    options = select.find_all('option')
                    if options and options[0].get('value'):
                        form_data[select_name] = options[0].get('value')

            # Find all input elements
            input_elements = form.find_all('input')
            for input_elem in input_elements:
                input_type = input_elem.get('type', '').lower()
                input_name = input_elem.get('name')

                if not input_name:
                    continue

                if input_type == 'submit':
                    # Add the submit button value
                    form_data[input_name] = input_elem.get('value', 'Submit')
                elif input_type in ['text', 'hidden', 'date']:
                    # Add other input values
                    form_data[input_name] = input_elem.get('value', '')

            # Log the form data
            logger.info(f"Submitting form with filters: academic_year={academic_year}, semester={semester}, branch={branch}, section={section}")
            logger.debug(f"Form data: {form_data}")

            # Submit the form
            response = self.session.post(form_action, data=form_data, timeout=self.timeout)
            response.raise_for_status()

            # Parse the HTML content
            result_soup = BeautifulSoup(response.text, 'html.parser')

            # Save HTML content in debug mode
            debug_dir = Path("debug_output")
            if debug_dir.exists():
                # Create a filename based on the parameters
                filename = f"{academic_year}_{semester.replace(' ', '_')}_{branch}_{section}.html"
                filepath = debug_dir / filename
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(response.text)
                logger.debug(f"Saved HTML content to {filepath}")

            # Check if we got results
            if 'No Records Found' in response.text:
                logger.warning(f"No records found for {academic_year}, {semester}, {branch}, {section}")
                return None

            return result_soup

        except requests.exceptions.RequestException as e:
            logger.error(f"Error submitting form: {e}")
            return None

    def get_academic_year_value(self, select_element: BeautifulSoup, academic_year: str) -> str:
        """
        Get the value for the academic year select element.

        Args:
            select_element: The select element
            academic_year: The academic year to select

        Returns:
            The value to use in the form
        """
        # Try to find an option with text or value matching the academic year
        for option in select_element.find_all('option'):
            option_text = option.text.strip()
            option_value = option.get('value', '')

            if academic_year in option_text or academic_year in option_value:
                return option_value

        # If no match found, return the first option value
        first_option = select_element.find('option')
        if first_option:
            return first_option.get('value', '')

        # If all else fails, return the academic year itself
        return academic_year

    def get_semester_value(self, select_element: BeautifulSoup, semester: str) -> str:
        """
        Get the value for the semester select element.

        Args:
            select_element: The select element
            semester: The semester to select

        Returns:
            The value to use in the form
        """
        # Try to find an option with text matching the semester
        for option in select_element.find_all('option'):
            option_text = option.text.strip()
            option_value = option.get('value', '')

            if semester.lower() in option_text.lower():
                return option_value

        # If no match found, try to match using the YEAR_SEM_CODES mapping
        if semester in YEAR_SEM_CODES:
            code = YEAR_SEM_CODES[semester]
            for option in select_element.find_all('option'):
                option_value = option.get('value', '')
                if option_value == code:
                    return option_value

        # If no match found, return the first option value
        first_option = select_element.find('option')
        if first_option:
            return first_option.get('value', '')

        # If all else fails, return the semester itself
        return semester

    def get_branch_value(self, select_element: BeautifulSoup, branch: str) -> str:
        """
        Get the value for the branch select element.

        Args:
            select_element: The select element
            branch: The branch to select

        Returns:
            The value to use in the form
        """
        # Try to find an option with text matching the branch
        for option in select_element.find_all('option'):
            option_text = option.text.strip()
            option_value = option.get('value', '')

            if branch.lower() in option_text.lower():
                return option_value

        # If no match found, try to match using the BRANCH_CODES mapping
        if branch in BRANCH_CODES:
            code = BRANCH_CODES[branch]
            for option in select_element.find_all('option'):
                option_value = option.get('value', '')
                if option_value == code:
                    return option_value

        # If no match found, return the first option value
        first_option = select_element.find('option')
        if first_option:
            return first_option.get('value', '')

        # If all else fails, return the branch itself
        return branch

    def get_section_value(self, select_element: BeautifulSoup, section: str) -> str:
        """
        Get the value for the section select element.

        Args:
            select_element: The select element
            section: The section to select

        Returns:
            The value to use in the form
        """
        # Try to find an option with text or value matching the section
        for option in select_element.find_all('option'):
            option_text = option.text.strip()
            option_value = option.get('value', '')

            if section == option_text or section == option_value:
                return option_value

        # If no match found, return the first option value
        first_option = select_element.find('option')
        if first_option:
            return first_option.get('value', '')

        # If all else fails, return the section itself
        return section

    @retry_on_network_error()
    def extract_mid_marks_data(self, soup: BeautifulSoup, academic_year: str, semester: str, branch: str, section: str) -> List[Dict[str, Any]]:
        """
        Extract mid marks data from the page.

        Args:
            soup: BeautifulSoup object of the page
            academic_year: Academic year
            semester: Semester
            branch: Branch (e.g., "CSE")
            section: Section (e.g., "A")

        Returns:
            List of dictionaries containing mid marks data
        """
        # Note: This method primarily uses BeautifulSoup for parsing, so it doesn't need many Playwright-specific changes
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
                filename = f"mid_marks_debug.html"
                filepath = debug_folder / filename

                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(str(soup))
                logger.debug(f"Saved HTML content to {filepath}")

            # Try to find table with mid marks data
            tables = soup.find_all('table')
            if not tables:
                logger.warning("No tables found on the page")
                return []

            # Find the table with mid marks data using multiple approaches
            # Approach 1: Look for tables with student rows that have name or id attributes (used for roll numbers)
            mid_marks_table = None
            for table in tables:
                # Check if this table has student rows with roll numbers
                rows = table.find_all('tr')
                if len(rows) > 1:  # At least header row and one data row
                    # Check if any row has a name or id attribute (used for roll numbers)
                    for row in rows[1:]:  # Skip header row
                        if row.get('name') or row.get('id'):
                            mid_marks_table = table
                            logger.info(f"Found mid marks table using row name/id attributes")
                            break
                if mid_marks_table:
                    break

            # Approach 2: Look for tables with roll number and name headers
            if not mid_marks_table:
                for table in tables:
                    headers = table.find_all('th')
                    header_texts = [h.get_text(strip=True).lower() for h in headers]

                    # Check if this table has roll number and name headers
                    if any('roll' in h for h in header_texts) and any('name' in h for h in header_texts):
                        mid_marks_table = table
                        logger.info(f"Found mid marks table using header text search")
                        break

            # Approach 3: Try to find the largest table with many rows (likely the data table)
            if not mid_marks_table:
                max_rows = 0
                for table in tables:
                    rows = table.find_all('tr')
                    if len(rows) > max_rows:
                        max_rows = len(rows)
                        mid_marks_table = table
                if mid_marks_table and max_rows > 5:  # Only use if it has a reasonable number of rows
                    logger.info(f"Found mid marks table using row count ({max_rows} rows)")

            if not mid_marks_table:
                logger.warning("Could not find mid marks table on the page")
                return []

            # Check if the table has rows with name/id attributes (format from your old script)
            rows = mid_marks_table.find_all('tr')
            has_named_rows = any(row.get('name') or row.get('id') for row in rows)

            mid_marks_data = []

            if has_named_rows:
                # Process using the format from your old script
                logger.info("Processing mid marks table with named rows")

                for row in rows[1:]:  # Skip header row
                    # Get roll number from row attributes
                    roll_number = row.get('name') or row.get('id')
                    if not roll_number:
                        continue

                    cells = row.find_all('td')
                    if len(cells) < 2:  # Need at least some data
                        continue

                    # Try to find student name in the first few cells
                    student_name = ""
                    for i in range(min(3, len(cells))):
                        cell_text = cells[i].get_text(strip=True)
                        # If it looks like a name (not a number or code)
                        if cell_text and not cell_text.isdigit() and len(cell_text) > 3:
                            student_name = cell_text
                            break

                    # Initialize student data
                    student_data = {
                        'roll_number': roll_number,
                        'name': student_name,
                        'subjects': {},
                        'labs': {},
                        'academic_year': academic_year,
                        'semester': semester,
                        'branch': branch,
                        'section': section,
                        'last_updated': datetime.now().isoformat()
                    }

                    # First process cells with name attributes (these are subject cells)
                    named_cells = []
                    for cell in cells:
                        subject_name = cell.get('name', '').strip()
                        cell_text = cell.get_text(strip=True)

                        if not subject_name or not cell_text:
                            continue

                        named_cells.append(cell)  # Keep track of cells with name attributes

                        # Check if it's a lab subject
                        if 'LAB' in subject_name.upper() or 'SKILLS' in subject_name.upper():
                            student_data['labs'][subject_name] = cell_text
                        else:
                            # Initialize marks dictionary
                            marks_dict = {'mid1': '', 'mid2': '', 'total': ''}

                            # Extract marks - handle different formats
                            if '/' in cell_text:
                                # Format: "34/25(33)" or "34/25"
                                parts = cell_text.split('/')
                                marks_dict['mid1'] = parts[0].strip()

                                second_part = parts[1]
                                if '(' in second_part:
                                    mid2, total = second_part.split('(')
                                    marks_dict['mid2'] = mid2.strip()
                                    marks_dict['total'] = total.rstrip(')').strip()
                                else:
                                    marks_dict['mid2'] = second_part.strip()
                            else:
                                # Single mark format: "16"
                                marks_dict['mid1'] = cell_text

                            student_data['subjects'][subject_name] = marks_dict

                    # Now look for lab marks in unnamed cells at the end of the row
                    # Get cells that don't have name attributes
                    unnamed_cells = [cell for cell in cells if cell not in named_cells and cell.get_text(strip=True)]

                    # Log the unnamed cells for debugging
                    logger.debug(f"Found {len(unnamed_cells)} unnamed cells: {[cell.get_text(strip=True) for cell in unnamed_cells]}")

                    # If we have unnamed cells with content, they might be lab marks
                    if unnamed_cells:
                        # Try to identify lab cells - typically they're at the end of the row
                        # and contain numeric values (marks)

                        # First, try to get lab names from the header row if possible
                        header_row = None
                        lab_names = []
                        all_lab_names = []
                        subject_count = 0

                        # Look for header rows (usually the second row contains the subject/lab names)
                        if len(mid_marks_table.find_all('tr')) > 1:
                            # The second row typically contains the subject and lab names
                            header_row = mid_marks_table.find_all('tr')[1]
                            header_cells = header_row.find_all('td') or header_row.find_all('th')

                            # Log the header row HTML for debugging
                            logger.debug(f"Header row HTML: {header_row}")

                            # Log the header cells for debugging
                            logger.debug(f"Found {len(header_cells)} header cells")
                            for i, cell in enumerate(header_cells):
                                cell_text = cell.get_text(strip=True)
                                cell_html = str(cell)
                                logger.debug(f"Header cell {i}: text='{cell_text}', html='{cell_html}'")

                                # Check if this cell contains a lab name
                                if cell_text and cell_text != "REMARKS" and ('LAB' in cell_text.upper() or
                                                                             'SKILLS' in cell_text.upper() or
                                                                             'WORKSHOP' in cell_text.upper() or
                                                                             'PRACTICE' in cell_text.upper()):
                                    all_lab_names.append(cell_text)
                                    logger.info(f"Found lab name directly in header: {cell_text}")

                            # First, count how many subject cells we have (cells with 'name' attribute in a student row)
                            if len(mid_marks_table.find_all('tr')) > 2:  # Make sure we have at least one student row
                                student_row = mid_marks_table.find_all('tr')[2]  # First student row
                                subject_cells = [cell for cell in student_row.find_all('td') if cell.get('name')]
                                subject_count = len(subject_cells)
                                logger.debug(f"Found {subject_count} subject cells in student row")

                                # Log all cells in the student row for debugging
                                all_cells = student_row.find_all('td')
                                logger.debug(f"Total cells in student row: {len(all_cells)}")
                                for i, cell in enumerate(all_cells):
                                    cell_text = cell.get_text(strip=True)
                                    cell_name = cell.get('name', 'unnamed')
                                    logger.debug(f"Cell {i}: name='{cell_name}', text='{cell_text}'")

                            # Extract lab names from header cells
                            # Skip the first two cells (S.No. and Roll_No.) and any subject cells
                            # The remaining cells should be labs
                            subject_and_header_offset = 2  # S.No. and Roll_No.

                            # Get the HTML of the header row for debugging
                            logger.debug(f"Header row HTML: {header_row}")

                            # If we have subject count, use it to find lab cells more accurately
                            if subject_count > 0:
                                # Calculate where lab cells should start
                                lab_start_index = subject_and_header_offset + subject_count
                                logger.debug(f"Lab cells should start at index {lab_start_index}")

                                # Check if we have enough header cells
                                if len(header_cells) > lab_start_index:
                                    # Extract lab names from the header cells after subjects
                                    lab_header_cells = header_cells[lab_start_index:]

                                    # Clear any existing lab names
                                    lab_names = []

                                    # Log all header cells for debugging
                                    logger.debug(f"All header cells: {[cell.get_text(strip=True) for cell in header_cells]}")

                                    # Extract all lab names from the header cells
                                    for i, cell in enumerate(lab_header_cells):
                                        cell_text = cell.get_text(strip=True)
                                        cell_html = str(cell)
                                        logger.debug(f"Lab header cell {i}: text='{cell_text}', html='{cell_html}'")

                                        if cell_text and cell_text != "REMARKS":  # Skip the remarks column
                                            # Check if it's likely a lab name
                                            if ('LAB' in cell_text.upper() or 'SKILLS' in cell_text.upper() or
                                                'WORKSHOP' in cell_text.upper() or 'PRACTICE' in cell_text.upper()):
                                                lab_names.append(cell_text)
                                                logger.debug(f"Found lab name in header (position-based): {cell_text}")
                                            else:
                                                # If it doesn't contain lab keywords but is in the lab position, still consider it
                                                logger.debug(f"Found potential lab name without keywords: {cell_text}")
                                                lab_names.append(cell_text)

                                    # Log all lab names found
                                    logger.debug(f"Found {len(lab_names)} lab names from header: {lab_names}")
                            else:
                                # Fallback: look for cells with lab-related keywords
                                for i, cell in enumerate(header_cells[subject_and_header_offset:]):
                                    cell_text = cell.get_text(strip=True)
                                    if cell_text and cell_text != "REMARKS":  # Skip the remarks column
                                        # If it contains LAB or SKILLS, it's likely a lab
                                        if ('LAB' in cell_text.upper() or 'SKILLS' in cell_text.upper() or
                                            'WORKSHOP' in cell_text.upper() or 'PRACTICE' in cell_text.upper()):
                                            lab_names.append(cell_text)
                                            logger.debug(f"Found lab name in header (keyword-based): {cell_text}")

                        # If we couldn't find lab names from headers, use common patterns based on semester and branch
                        if not lab_names:
                            # Comprehensive mapping of lab names by year, branch, and semester
                            lab_mapping = {
                                # First Year Labs
                                'First Yr': {
                                    'CSE': [
                                        "PROGRAMMING FOR PROBLEM SOLVING LAB",
                                        "ENGINEERING DRAWING LAB",
                                        "COMMUNICATION and SOFT SKILLS LAB"
                                    ],
                                    'IT': [
                                        "PROGRAMMING FOR PROBLEM SOLVING LAB",
                                        "ENGINEERING DRAWING LAB",
                                        "COMMUNICATION and SOFT SKILLS LAB"
                                    ],
                                    'AI_DS': [
                                        "PROGRAMMING FOR PROBLEM SOLVING LAB",
                                        "ENGINEERING DRAWING LAB",
                                        "COMMUNICATION and SOFT SKILLS LAB"
                                    ],
                                    'ECE': [
                                        "BASIC ELECTRICAL ENGINEERING LAB",
                                        "ENGINEERING DRAWING LAB",
                                        "COMMUNICATION and SOFT SKILLS LAB"
                                    ],
                                    'EEE': [
                                        "BASIC ELECTRICAL ENGINEERING LAB",
                                        "ENGINEERING DRAWING LAB",
                                        "COMMUNICATION and SOFT SKILLS LAB"
                                    ],
                                    'MECH': [
                                        "ENGINEERING WORKSHOP",
                                        "ENGINEERING DRAWING LAB",
                                        "COMMUNICATION and SOFT SKILLS LAB"
                                    ],
                                    'CIVIL': [
                                        "ENGINEERING WORKSHOP",
                                        "ENGINEERING DRAWING LAB",
                                        "COMMUNICATION and SOFT SKILLS LAB"
                                    ],
                                    'default': [
                                        "LAB 1",
                                        "LAB 2",
                                        "LAB 3"
                                    ]
                                },
                                # Second Year Labs
                                'Second Yr': {
                                    'CSE': [
                                        "DATA STRUCTURES LAB",
                                        "DIGITAL LOGIC DESIGN LAB",
                                        "PYTHON PROGRAMMING LAB",
                                        "DATABASE MANAGEMENT SYSTEMS LAB",
                                        "OBJECT ORIENTED PROGRAMMING LAB"
                                    ],
                                    'IT': [
                                        "DATA STRUCTURES LAB",
                                        "DIGITAL LOGIC DESIGN LAB",
                                        "PYTHON PROGRAMMING LAB",
                                        "DATABASE MANAGEMENT SYSTEMS LAB"
                                    ],
                                    'ECE': [
                                        "ELECTRONIC DEVICES & CIRCUITS LAB",
                                        "DIGITAL SYSTEM DESIGN LAB",
                                        "SIGNALS & SYSTEMS LAB",
                                        "ELECTRICAL TECHNOLOGY LAB"
                                    ],
                                    'EEE': [
                                        "ELECTRICAL MACHINES LAB",
                                        "CONTROL SYSTEMS LAB",
                                        "POWER ELECTRONICS LAB"
                                    ],
                                    'MECH': [
                                        "MACHINE DRAWING LAB",
                                        "MANUFACTURING PROCESSES LAB",
                                        "FLUID MECHANICS LAB",
                                        "MATERIAL TESTING LAB"
                                    ],
                                    'CIVIL': [
                                        "SURVEYING LAB",
                                        "FLUID MECHANICS LAB",
                                        "BUILDING MATERIALS TESTING LAB",
                                        "CONCRETE TECHNOLOGY LAB"
                                    ],
                                    'default': [
                                        "LAB 1",
                                        "LAB 2",
                                        "LAB 3",
                                        "LAB 4"
                                    ]
                                },
                                # Third Year Labs
                                'Third Yr': {
                                    'CSE': [
                                        "WEB TECHNOLOGIES LAB",
                                        "COMPILER DESIGN LAB",
                                        "SOFTWARE ENGINEERING LAB",
                                        "MACHINE LEARNING LAB"
                                    ],
                                    'IT': [
                                        "WEB TECHNOLOGIES LAB",
                                        "SOFTWARE ENGINEERING LAB",
                                        "DATA MINING LAB",
                                        "COMPUTER NETWORKS LAB"
                                    ],
                                    'ECE': [
                                        "MICROPROCESSORS & MICROCONTROLLERS LAB",
                                        "DIGITAL SIGNAL PROCESSING LAB",
                                        "COMMUNICATION SYSTEMS LAB",
                                        "VLSI DESIGN LAB"
                                    ],
                                    'EEE': [
                                        "POWER SYSTEMS LAB",
                                        "ELECTRICAL MEASUREMENTS LAB",
                                        "MICROPROCESSORS & MICROCONTROLLERS LAB"
                                    ],
                                    'MECH': [
                                        "HEAT TRANSFER LAB",
                                        "DESIGN OF MACHINE ELEMENTS LAB",
                                        "CAD/CAM LAB",
                                        "THERMAL ENGINEERING LAB"
                                    ],
                                    'CIVIL': [
                                        "STRUCTURAL ANALYSIS LAB",
                                        "GEOTECHNICAL ENGINEERING LAB",
                                        "ENVIRONMENTAL ENGINEERING LAB",
                                        "TRANSPORTATION ENGINEERING LAB"
                                    ],
                                    'default': [
                                        "LAB 1",
                                        "LAB 2",
                                        "LAB 3",
                                        "LAB 4"
                                    ]
                                },
                                # Fourth Year Labs
                                'Final Yr': {
                                    'CSE': [
                                        "CLOUD COMPUTING LAB",
                                        "BIG DATA ANALYTICS LAB",
                                        "ARTIFICIAL INTELLIGENCE LAB"
                                    ],
                                    'IT': [
                                        "CLOUD COMPUTING LAB",
                                        "INTERNET OF THINGS LAB",
                                        "MOBILE APPLICATION DEVELOPMENT LAB"
                                    ],
                                    'ECE': [
                                        "EMBEDDED SYSTEMS LAB",
                                        "WIRELESS COMMUNICATIONS LAB",
                                        "OPTICAL COMMUNICATIONS LAB"
                                    ],
                                    'EEE': [
                                        "POWER SYSTEM SIMULATION LAB",
                                        "DIGITAL CONTROL SYSTEMS LAB",
                                        "HIGH VOLTAGE ENGINEERING LAB"
                                    ],
                                    'MECH': [
                                        "COMPUTATIONAL FLUID DYNAMICS LAB",
                                        "ROBOTICS LAB",
                                        "AUTOMOBILE ENGINEERING LAB"
                                    ],
                                    'CIVIL': [
                                        "ADVANCED STRUCTURAL DESIGN LAB",
                                        "REMOTE SENSING & GIS LAB",
                                        "WATER RESOURCES ENGINEERING LAB"
                                    ],
                                    'default': [
                                        "LAB 1",
                                        "LAB 2",
                                        "LAB 3"
                                    ]
                                },
                                # Default if year not recognized
                                'default': {
                                    'default': [
                                        "LAB 1",
                                        "LAB 2",
                                        "LAB 3",
                                        "LAB 4",
                                        "LAB 5"
                                    ]
                                }
                            }

                            # Determine the year pattern (First Yr, Second Yr, etc.)
                            year_pattern = next((y for y in lab_mapping.keys() if y in semester), 'default')

                            # Get the branch-specific labs or default if branch not found
                            branch_labs = lab_mapping[year_pattern].get(branch, lab_mapping[year_pattern]['default'])

                            # Use these labs
                            lab_names = branch_labs.copy()

                        # Only add generic lab names if we couldn't find any real lab names
                        if not lab_names and unnamed_cells:
                            # Ensure we have enough lab names for all unnamed cells
                            for i in range(len(unnamed_cells)):
                                lab_names.append(f"ADDITIONAL_LAB_{i+1}")
                            logger.warning(f"No lab names found in header, using generic names: {lab_names}")

                        # If we have subject count, we can more accurately identify lab cells
                        # Lab cells are typically after subject cells in the row
                        lab_cells = []

                        # Log the entire row for debugging
                        logger.debug(f"Row HTML: {row}")
                        logger.debug(f"Total cells in row: {len(cells)}")
                        for i, cell in enumerate(cells):
                            cell_text = cell.get_text(strip=True)
                            cell_name = cell.get('name', 'unnamed')
                            logger.debug(f"Cell {i}: name='{cell_name}', text='{cell_text}'")

                        if subject_count > 0:
                            # Skip the first two cells (S.No. and Roll_No.) and subject cells
                            # The remaining cells should be lab marks
                            all_cells = cells
                            if len(all_cells) > subject_count + 2:  # +2 for S.No. and Roll_No.
                                # Get lab cells based on position
                                potential_lab_cells = all_cells[subject_count + 2:-1]  # Skip the last cell (REMARKS)

                                # Log all potential lab cells for debugging
                                logger.debug(f"Found {len(potential_lab_cells)} potential lab cells")
                                for i, cell in enumerate(potential_lab_cells):
                                    cell_text = cell.get_text(strip=True)
                                    cell_name = cell.get('name', 'unnamed')
                                    logger.debug(f"Potential lab cell {i}: name='{cell_name}', text='{cell_text}'")

                                # Filter out cells that are likely not lab marks
                                for cell in potential_lab_cells:
                                    cell_text = cell.get_text(strip=True)
                                    if (cell_text and
                                        not any(pattern in cell_text for pattern in ['KB1A', 'KB5A', 'KB1E', 'KB5E']) and
                                        not cell_text.isalpha() and
                                        cell_text != "REMARKS"):
                                        lab_cells.append(cell)

                                logger.debug(f"Found {len(lab_cells)} lab cells based on position")
                        else:
                            # Fallback: look for unnamed cells that might contain lab marks
                            for cell in cells:
                                if not cell.get('name') and cell.get_text(strip=True):
                                    # Skip cells that are likely not lab marks
                                    cell_text = cell.get_text(strip=True)
                                    if (not any(pattern in cell_text for pattern in ['KB1A', 'KB5A', 'KB1E', 'KB5E']) and
                                        not cell_text.isalpha() and
                                        cell_text != "REMARKS"):
                                        lab_cells.append(cell)

                            logger.debug(f"Found {len(lab_cells)} potential lab cells (unnamed cells)")

                        # Use all lab names found in the header
                        lab_names = all_lab_names
                        logger.debug(f"Found {len(lab_names)} lab names from header: {lab_names}")

                        # Match lab names with lab marks
                        if lab_names and lab_cells:
                            # Log all lab names and cells for debugging
                            logger.debug(f"Lab names: {lab_names}")
                            logger.debug(f"Lab cells: {[cell.get_text(strip=True) for cell in lab_cells]}")

                            # If we have more lab names than cells, use only the first lab names
                            # (corresponding to the cells at the beginning of the lab section)
                            if len(lab_names) > len(lab_cells):
                                logger.warning(f"More lab names ({len(lab_names)}) than cells ({len(lab_cells)}): {lab_names}")
                                lab_names = lab_names[:len(lab_cells)]
                                logger.debug(f"Trimmed lab names to match cell count: {lab_names}")

                            # If we have more cells than lab names, use only the first cells
                            if len(lab_cells) > len(lab_names):
                                logger.warning(f"More lab cells ({len(lab_cells)}) than names ({len(lab_names)})")
                                lab_cells = lab_cells[:len(lab_names)]
                                logger.debug(f"Trimmed lab cells to match name count: {len(lab_cells)}")

                            # Match lab names with lab marks
                            for i, (lab_name, cell) in enumerate(zip(lab_names, lab_cells)):
                                lab_mark = cell.get_text(strip=True)
                                logger.debug(f"Processing lab {i}: {lab_name} with mark '{lab_mark}'")

                                # Validate and clean the mark
                                if lab_mark and lab_mark.strip():
                                    # Check for "Not Entered" or similar values
                                    if lab_mark.lower() in ['not entered', 'n/a', 'na', 'not available', '-']:
                                        student_data['labs'][lab_name] = "NOT_ENTERED"
                                        logger.info(f"Stored 'NOT_ENTERED' for {lab_name} (original: '{lab_mark}')")
                                    else:
                                        # Try to extract just the numeric part if it's mixed with text
                                        import re
                                        numeric_part = re.search(r'\d+', lab_mark)
                                        if numeric_part:
                                            lab_mark = numeric_part.group(0)
                                            student_data['labs'][lab_name] = lab_mark
                                            logger.debug(f"Added lab mark for {lab_name}: {lab_mark}")
                                        else:
                                            # If no numeric part found, store as is with a warning
                                            student_data['labs'][lab_name] = "INVALID_FORMAT"
                                            logger.warning(f"No numeric mark found for {lab_name} in '{lab_mark}', storing as INVALID_FORMAT")

                            # Log the labs found for this student
                            if student_data['labs']:
                                logger.debug(f"Found {len(student_data['labs'])} labs for student {roll_number}: {', '.join(student_data['labs'].keys())}")
                            else:
                                logger.warning(f"No lab data found for student {roll_number}")

                    # Add student data if we have any subjects or labs
                    if student_data['subjects'] or student_data['labs']:
                        mid_marks_data.append(student_data)
            else:
                # Process using the standard table format with headers
                logger.info("Processing mid marks table with standard header format")

                # Extract headers (subject names)
                headers = mid_marks_table.find_all('th')
                if not headers:
                    headers = mid_marks_table.find_all('tr')[0].find_all('td')  # Try first row as header

                header_texts = [h.get_text(strip=True) for h in headers]

                # Find the indices of roll number and name columns
                roll_idx = next((i for i, h in enumerate(header_texts) if 'roll' in h.lower()), 0)  # Default to first column
                name_idx = next((i for i, h in enumerate(header_texts) if 'name' in h.lower()), 1)  # Default to second column

                # Get subject names (all headers except roll and name)
                subject_indices = [i for i, h in enumerate(header_texts)
                                if i != roll_idx and i != name_idx and h.strip()]
                subject_names = [header_texts[i] for i in subject_indices]

                # Extract data from rows
                for row in rows[1:]:  # Skip header row
                    cells = row.find_all(['td', 'th'])
                    if len(cells) <= max(roll_idx, name_idx):
                        continue  # Skip rows with insufficient cells

                    roll_number = cells[roll_idx].get_text(strip=True)
                    student_name = cells[name_idx].get_text(strip=True) if name_idx < len(cells) else ""

                    # Skip rows without a valid roll number
                    if not roll_number or not roll_number.strip():
                        continue

                    # Extract marks for each subject
                    subject_marks = {}
                    lab_marks = {}

                    # Track which columns are likely labs based on header names
                    lab_indices = []
                    for i, subject_name in enumerate(subject_names):
                        if 'LAB' in subject_name.upper() or 'SKILLS' in subject_name.upper() or 'WORKSHOP' in subject_name.upper():
                            lab_indices.append(i)

                    # Process each subject column
                    for i, subject_idx in enumerate(subject_indices):
                        if subject_idx < len(cells):
                            mark_text = cells[subject_idx].get_text(strip=True)
                            if mark_text:
                                subject_name = subject_names[i]
                                # Check if it's a lab subject based on name or position
                                if i in lab_indices or 'LAB' in subject_name.upper() or 'SKILLS' in subject_name.upper() or 'WORKSHOP' in subject_name.upper():
                                    # For labs, check for special values
                                    if mark_text.lower() in ['not entered', 'n/a', 'na', 'not available', '-']:
                                        lab_marks[subject_name] = "NOT_ENTERED"
                                        logger.info(f"Stored 'NOT_ENTERED' for {subject_name} (original: '{mark_text}')")
                                    else:
                                        # Try to extract numeric part
                                        import re
                                        numeric_part = re.search(r'\d+', mark_text)
                                        if numeric_part:
                                            lab_marks[subject_name] = numeric_part.group(0)
                                        else:
                                            # If no numeric part found, store as is with a warning
                                            lab_marks[subject_name] = "INVALID_FORMAT"
                                            logger.warning(f"No numeric mark found for {subject_name} in '{mark_text}', storing as INVALID_FORMAT")
                                else:
                                    # For regular subjects, try to parse marks in different formats
                                    if '/' in mark_text:
                                        # Format: "34/25(33)" or "34/25"
                                        parts = mark_text.split('/')
                                        mid1 = parts[0].strip()

                                        second_part = parts[1]
                                        mid2 = ''
                                        total = ''
                                        if '(' in second_part:
                                            mid2, total = second_part.split('(')
                                            mid2 = mid2.strip()
                                            total = total.rstrip(')').strip()
                                        else:
                                            mid2 = second_part.strip()

                                        subject_marks[subject_name] = {
                                            'mid1': mid1,
                                            'mid2': mid2,
                                            'total': total
                                        }
                                    else:
                                        # Single mark format
                                        subject_marks[subject_name] = {
                                            'mid1': mark_text,
                                            'mid2': '',
                                            'total': ''
                                        }

                    # Look for additional lab marks in unnamed cells at the end
                    # This handles cases where labs are in extra columns not covered by headers
                    if len(cells) > len(subject_indices) + 2:  # +2 for roll and name columns
                        extra_cells = cells[len(subject_indices) + 2:]
                        for i, cell in enumerate(extra_cells):
                            mark_text = cell.get_text(strip=True)
                            if mark_text and not mark_text.isalpha():  # Only consider non-alphabetic values as marks
                                lab_name = f"ADDITIONAL_LAB_{i+1}"
                                # Check for special values
                                if mark_text.lower() in ['not entered', 'n/a', 'na', 'not available', '-']:
                                    lab_marks[lab_name] = "NOT_ENTERED"
                                    logger.info(f"Stored 'NOT_ENTERED' for {lab_name} (original: '{mark_text}')")
                                else:
                                    # Try to extract numeric part
                                    import re
                                    numeric_part = re.search(r'\d+', mark_text)
                                    if numeric_part:
                                        lab_marks[lab_name] = numeric_part.group(0)
                                    else:
                                        # If no numeric part found, store as is with a warning
                                        lab_marks[lab_name] = "INVALID_FORMAT"
                                        logger.warning(f"No numeric mark found for {lab_name} in '{mark_text}', storing as INVALID_FORMAT")

                    # Create student data dictionary
                    student_data = {
                        "roll_number": roll_number,
                        "name": student_name,
                        "academic_year": academic_year,
                        "semester": semester,
                        "branch": branch,
                        "section": section,
                        "subjects": subject_marks,
                        "labs": lab_marks,
                        "last_updated": datetime.now().isoformat()
                    }

                    # Only add if we have any data
                    if subject_marks or lab_marks:
                        mid_marks_data.append(student_data)

            logger.info(f"Extracted mid marks data for {len(mid_marks_data)} students")
            return mid_marks_data

        except Exception as e:
            logger.error(f"Error extracting mid marks data: {str(e)}")
            return []

    def extract_attendance_data_approach1(self, soup: BeautifulSoup, roll_no_cells: List,
                                         academic_year: str, semester: str, branch: str, section: str) -> List[Dict[str, Any]]:
        """
        Extract attendance data using approach 1 (tdRollNo class).

        Args:
            soup: BeautifulSoup object of the page
            roll_no_cells: List of cells with class tdRollNo
            academic_year: Academic year
            semester: Semester
            branch: Branch (e.g., "CSE")
            section: Section (e.g., "A")

        Returns:
            List of dictionaries containing attendance data
        """
        attendance_data = []

        for roll_cell in roll_no_cells:
            # Get the parent row
            row = roll_cell.parent

            # Extract roll number
            roll_number = roll_cell.text.strip().replace(' ', '')

            # Extract just the roll number part if it has a date in parentheses
            if '(' in roll_number and ')' in roll_number:
                # Extract the part before the opening parenthesis
                roll_number = roll_number.split('(')[0].strip().replace(' ', '')

            # Find percentage cell
            percent_cell = row.find('td', {'class': 'tdPercent'})
            attendance_percentage = "N/A"
            total_classes = "N/A"

            if percent_cell:
                # Extract attendance percentage
                attendance_percentage = percent_cell.contents[0].strip() if percent_cell.contents else "N/A"
                # Extract total classes if available
                font_tag = percent_cell.find('font')
                if font_tag:
                    total_classes = font_tag.text.strip()

            # Create student data dictionary
            student_data = {
                'roll_number': roll_number,
                'data_type': 'attendance',
                'academic_year': academic_year,
                'semester': semester,
                'branch': branch,
                'section': section,
                'data': {
                    'attendance_percentage': attendance_percentage,
                    'total_classes': total_classes
                }
            }

            # Extract subject-wise attendance
            subject_cells = row.find_all('td', {'title': True})
            for cell in subject_cells:
                subject_name = cell.get('title')
                attendance_value = cell.text.strip()
                student_data['data'][self.normalize_key(subject_name)] = attendance_value

            attendance_data.append(student_data)

        logger.info(f"Extracted attendance data for {len(attendance_data)} students using approach 1")
        return attendance_data

    def extract_attendance_data_approach2(self, soup: BeautifulSoup, tables: List,
                                         academic_year: str, semester: str, branch: str, section: str) -> List[Dict[str, Any]]:
        """
        Extract attendance data using approach 2 (tables).

        Args:
            soup: BeautifulSoup object of the page
            tables: List of tables
            academic_year: Academic year
            semester: Semester
            branch: Branch (e.g., "CSE")
            section: Section (e.g., "A")

        Returns:
            List of dictionaries containing attendance data
        """
        attendance_data = []

        # Debug: Print all tables found
        logger.debug(f"Found {len(tables)} tables on the page")
        for i, table in enumerate(tables):
            rows = table.find_all('tr')
            logger.debug(f"Table {i+1} has {len(rows)} rows")
            if rows:
                # Print the first row to see what it contains
                first_row = rows[0]
                cells = first_row.find_all(['th', 'td'])
                cell_texts = [cell.text.strip() for cell in cells]
                logger.debug(f"First row of Table {i+1}: {cell_texts}")

        # First approach: Look for rows with tdRollNo class (from old project)
        roll_no_cells = soup.find_all('td', {'class': 'tdRollNo'})
        if roll_no_cells:
            logger.info(f"Found {len(roll_no_cells)} cells with tdRollNo class")

            for roll_cell in roll_no_cells:
                try:
                    # Get the parent row
                    tr_tag = roll_cell.parent
                    if not tr_tag:
                        continue

                    # Extract roll number
                    roll_number = roll_cell.text.strip().replace(' ', '')
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

                    # Extract attendance percentage
                    td_percent = tr_tag.find('td', {'class': 'tdPercent'})
                    if td_percent:
                        student_data['data']['attendance_percentage'] = td_percent.contents[0].strip()
                        font_tag = td_percent.find('font')
                        if font_tag:
                            student_data['data']['total_classes'] = font_tag.text.strip()

                    # Extract subject data
                    subject_cells = [td for td in tr_tag.find_all('td') if 'title' in td.attrs]
                    for cell in subject_cells:
                        subject_name = cell.get('title', '').strip()
                        if subject_name:
                            value = cell.text.strip()
                            student_data['data'][self.normalize_key(subject_name)] = value

                    # Only add if we have actual data
                    if student_data['data']:
                        attendance_data.append(student_data)

                except Exception as e:
                    logger.error(f"Error extracting data from tdRollNo cell: {e}")

            if attendance_data:
                logger.info(f"Extracted attendance data for {len(attendance_data)} students using tdRollNo class")
                return attendance_data

        # Second approach: Try to find a table with attendance data
        # Look for tables with specific keywords in headers
        attendance_table = None
        for table in tables:
            rows = table.find_all('tr')
            if not rows:
                continue

            # Check if this table has headers that look like attendance data
            first_row = rows[0]
            cells = first_row.find_all(['th', 'td'])
            cell_texts = [cell.text.strip().lower() for cell in cells]

            # Look for attendance-related keywords
            attendance_keywords = ['attendance', 'present', 'absent', 'total', 'percentage', '%', 'roll', 'name', 'student']
            if any(keyword in ' '.join(cell_texts) for keyword in attendance_keywords):
                attendance_table = table
                logger.debug(f"Found potential attendance table with keywords: {[kw for kw in attendance_keywords if kw in ' '.join(cell_texts)]}")
                break

        # If we didn't find a table with attendance keywords, use the largest table
        if not attendance_table:
            # Find the main data table (usually the largest one)
            main_table = None
            max_rows = 0

            for table in tables:
                rows = table.find_all('tr')
                if len(rows) > max_rows:
                    max_rows = len(rows)
                    main_table = table

            attendance_table = main_table
            logger.debug(f"Using largest table with {max_rows} rows as attendance table")

        if not attendance_table or len(attendance_table.find_all('tr')) <= 1:  # Skip tables with only header row
            logger.warning("No suitable table found for attendance data")
            return []

        # Get all rows from the attendance table
        rows = attendance_table.find_all('tr')

        # Extract header row to identify columns
        header_row = rows[0]
        headers = [th.text.strip() for th in header_row.find_all(['th', 'td'])]
        logger.debug(f"Table headers: {headers}")

        # Find the roll number column index using various patterns
        roll_idx = -1
        roll_patterns = ['roll', 'id', 'no', 'number', 'student id', 'student no', 'admission']

        for i, header in enumerate(headers):
            header_lower = header.lower()
            if any(pattern in header_lower for pattern in roll_patterns):
                roll_idx = i
                logger.debug(f"Found roll number column at index {i}: '{header}'")
                break

        # If we still can't find a roll number column, look for a column with numeric values
        if roll_idx == -1 and len(rows) > 1:
            for i, cell in enumerate(rows[1].find_all(['td', 'th'])):
                cell_text = cell.text.strip()
                # Check if the cell contains a numeric value or a pattern that looks like a roll number
                if cell_text.isdigit() or (len(cell_text) >= 5 and any(c.isdigit() for c in cell_text)):
                    roll_idx = i
                    logger.debug(f"Using column {i} as roll number column based on numeric content: '{cell_text}'")
                    break

        # If we still can't find a roll number column, use the first column
        if roll_idx == -1 and len(headers) > 0:
            roll_idx = 0
            logger.warning(f"Could not identify roll number column, using first column: '{headers[0]}'")

        if roll_idx == -1 or len(headers) == 0:
            logger.warning("Could not find roll number column in the table")
            return []

        # Process data rows
        for row in rows[1:]:  # Skip header row
            cells = row.find_all(['td', 'th'])
            if len(cells) <= roll_idx:
                continue  # Skip rows with insufficient cells

            # Extract roll number
            roll_number = cells[roll_idx].text.strip().replace(' ', '')
            if not roll_number:
                continue  # Skip rows without roll number

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

            # Extract other data
            for i, cell in enumerate(cells):
                if i != roll_idx and i < len(headers):
                    key = self.normalize_key(headers[i])
                    value = cell.text.strip()
                    if value:  # Only add non-empty values
                        student_data['data'][key] = value

                    # Also check for title attribute which might contain subject names
                    title = cell.get('title')
                    if title:
                        title_key = self.normalize_key(title)
                        if title_key != key and value:  # Avoid duplicates and empty values
                            student_data['data'][title_key] = value

            # Only add student data if we have actual data
            if student_data['data']:
                attendance_data.append(student_data)

        logger.info(f"Extracted attendance data for {len(attendance_data)} students using approach 2")
        return attendance_data

    def extract_attendance_data_approach3(self, soup: BeautifulSoup, rows: List,
                                         academic_year: str, semester: str, branch: str, section: str) -> List[Dict[str, Any]]:
        """
        Extract attendance data using approach 3 (any rows with roll numbers).

        Args:
            soup: BeautifulSoup object of the page
            rows: List of rows
            academic_year: Academic year
            semester: Semester
            branch: Branch (e.g., "CSE")
            section: Section (e.g., "A")

        Returns:
            List of dictionaries containing attendance data
        """
        attendance_data = []

        # Look for rows that might contain roll numbers
        for row in rows:
            cells = row.find_all(['td', 'th'])
            if len(cells) < 2:  # Need at least roll number and some data
                continue

            # Try to find a cell that looks like a roll number
            roll_number = None
            for cell in cells:
                text = cell.text.strip()
                # Roll numbers are often numeric or have a specific format
                if text and (text.isdigit() or (len(text) >= 5 and any(c.isdigit() for c in text))):
                    # Extract just the roll number part if it has a date in parentheses
                    if '(' in text and ')' in text:
                        # Extract the part before the opening parenthesis
                        roll_number = text.split('(')[0].strip().replace(' ', '')
                    else:
                        roll_number = text.replace(' ', '')
                    break

            if not roll_number:
                continue  # Skip rows without roll number

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
                # Skip the cell we identified as roll number
                if cell.text.strip() != roll_number:
                    # Try to determine what this cell represents
                    key = f"column_{i}"
                    value = cell.text.strip()

                    # Look for percentage indicators
                    if '%' in value:
                        key = 'attendance_percentage'
                    # Look for subject names in title attribute
                    elif cell.get('title'):
                        key = self.normalize_key(cell.get('title'))

                    student_data['data'][key] = value

            attendance_data.append(student_data)

        logger.info(f"Extracted attendance data for {len(attendance_data)} students using approach 3")
        return attendance_data

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


    def store_mid_marks_data(self, mid_marks_data: List[Dict[str, Any]], force_update: bool = False) -> Tuple[int, int]:
        """
        Store mid marks data in a structured folder system.

        Args:
            mid_marks_data: List of dictionaries containing mid marks data
            force_update: Whether to force update even if data already exists

        Returns:
            Tuple of (success_count, update_count)
        """
        success_count = 0
        update_count = 0

        # Check if we have valid data to store
        if not mid_marks_data:
            logger.warning("No mid marks data to store")
            return success_count, update_count

        # Validate data structure before storing
        for student in mid_marks_data:
            # Check if student data has the required fields and non-empty data
            if not all(k in student for k in ['roll_number', 'academic_year', 'semester', 'branch', 'section', 'subjects']):
                logger.warning(f"Skipping invalid student data: {student}")
                continue

            # Check if subjects dictionary is not empty
            if not student['subjects']:
                logger.warning(f"Skipping student with empty subjects: {student['roll_number']}")
                continue

            try:
                # Extract student information
                roll_number = student['roll_number']
                academic_year = student['academic_year']
                semester = student['semester']
                branch = student['branch']
                section = student['section']
                subjects = student['subjects']

                # Convert semester to year_of_study format
                year_of_study = self.convert_semester_to_year_of_study(semester)

                # Create folder structure (without branch and section folders)
                student_folder = self.base_dir / academic_year / year_of_study / roll_number
                student_folder.mkdir(parents=True, exist_ok=True)

                # Save mid marks data
                mid_marks_file = student_folder / "mid_marks.json"

                # Save branch and section information in roll_number.json file
                self.store_student_info(student_folder, roll_number, branch, section)

                # Check if file exists and compare data
                should_update = True
                if mid_marks_file.exists() and not force_update:
                    try:
                        with open(mid_marks_file, 'r') as f:
                            existing_data = json.load(f)

                        # Get labs from student data
                        labs = student.get('labs', {})

                        # Compare both subjects and labs
                        if existing_data.get('subjects') == subjects and existing_data.get('labs', {}) == labs:
                            should_update = False
                        else:
                            # Log what changed in subjects
                            changes = []
                            existing_subjects = existing_data.get('subjects', {})
                            for key in set(subjects.keys()) | set(existing_subjects.keys()):
                                if key not in existing_subjects:
                                    changes.append(f"Added subject {key}: {subjects[key]}")
                                elif key not in subjects:
                                    changes.append(f"Removed subject {key}")
                                elif existing_subjects[key] != subjects[key]:
                                    changes.append(f"Changed subject {key}: {existing_subjects[key]} -> {subjects[key]}")

                            # Log what changed in labs
                            existing_labs = existing_data.get('labs', {})
                            for key in set(labs.keys()) | set(existing_labs.keys()):
                                if key not in existing_labs:
                                    changes.append(f"Added lab {key}: {labs[key]}")
                                elif key not in labs:
                                    changes.append(f"Removed lab {key}")
                                elif existing_labs[key] != labs[key]:
                                    changes.append(f"Changed lab {key}: {existing_labs[key]} -> {labs[key]}")

                            if changes:
                                logger.debug(f"Changes for {roll_number}: {', '.join(changes[:3])}" +
                                             (f" and {len(changes) - 3} more" if len(changes) > 3 else ""))
                    except Exception as e:
                        logger.warning(f"Error reading existing data for {roll_number}: {e}")
                        should_update = True

                if should_update:
                    # Create student data dictionary for JSON file
                    student_json = {
                        'roll_number': roll_number,
                        'data_type': 'mid_marks',
                        'academic_year': academic_year,
                        'semester': semester,
                        'branch': branch,
                        'section': section,
                        'subjects': subjects,
                        'labs': student.get('labs', {}),  # Include lab data
                        'last_updated': datetime.now().isoformat()
                    }

                    # Validate lab data
                    if student.get('labs', {}):
                        logger.debug(f"Student {roll_number} has {len(student.get('labs', {}))} labs: {', '.join(student.get('labs', {}).keys())}")
                    else:
                        logger.warning(f"Student {roll_number} has no lab data")

                    with open(mid_marks_file, 'w') as f:
                        json.dump(student_json, f, indent=2)

                    # No need to update roll index as we now store this info in the student folder

                    # Update success count
                    success_count += 1
                    if mid_marks_file.exists():
                        update_count += 1

                    logger.info(f"Updated mid marks data for student {roll_number}")
                else:
                    logger.debug(f"No changes detected for student {roll_number}, skipping update")

            except Exception as e:
                logger.error(f"Error storing mid marks data for student {student.get('roll_number', 'unknown')}: {str(e)}")

        return success_count, update_count

    def export_mid_marks_to_csv(self, academic_year: str, year_of_study: str, branch: str, section: str) -> Optional[str]:
        """
        Export mid marks data to CSV file.

        Args:
            academic_year: Academic year (e.g., '2023-24')
            year_of_study: Year of study (e.g., 'I', 'II', 'III', 'IV')
            branch: Branch code (e.g., 'CSE', 'ECE')
            section: Section (e.g., 'A', 'B')

        Returns:
            Path to the CSV file if successful, None otherwise
        """
        if not PANDAS_AVAILABLE:
            logger.error("Pandas is required for CSV export. Please install pandas.")
            return None

        try:
            # Create the folder structure for CSV files
            csv_dir = Path("csv_details") / academic_year / year_of_study
            csv_dir.mkdir(parents=True, exist_ok=True)

            # Create the CSV file path
            csv_file = csv_dir / f"{branch}_{section}_mid_marks.csv"

            # Get the folder path for the student data (without branch and section folders)
            student_folder = self.base_dir / academic_year / year_of_study

            if not student_folder.exists():
                logger.warning(f"No data found for {academic_year}, {year_of_study}, {branch}, {section}")
                return None

            # Get all student folders
            student_folders = [f for f in student_folder.iterdir() if f.is_dir()]

            if not student_folders:
                logger.warning(f"No student data found for {academic_year}, {year_of_study}, {branch}, {section}")
                return None

            # Collect data for all students
            all_data = []
            subject_set = set()

            for student_dir in student_folders:
                roll_number = student_dir.name
                mid_marks_file = student_dir / "mid_marks.json"

                if not mid_marks_file.exists():
                    continue

                try:
                    with open(mid_marks_file, 'r') as f:
                        student_data = json.load(f)

                    # Extract student information
                    student_info = {
                        'roll_number': roll_number,
                        'name': student_data.get('name', ''),
                    }

                    # Extract subject marks
                    subjects = student_data.get('subjects', {})
                    for subject, mark in subjects.items():
                        student_info[subject] = mark
                        subject_set.add(subject)

                    # Extract lab marks
                    labs = student_data.get('labs', {})
                    for lab, mark in labs.items():
                        # Add 'LAB_' prefix to avoid column name conflicts
                        lab_column = f"LAB_{lab}"
                        student_info[lab_column] = mark
                        subject_set.add(lab_column)

                    all_data.append(student_info)
                except Exception as e:
                    logger.error(f"Error reading mid marks data for {roll_number}: {str(e)}")

            if not all_data:
                logger.warning(f"No mid marks data found for {academic_year}, {year_of_study}, {branch}, {section}")
                return None

            # Create DataFrame
            df = pd.DataFrame(all_data)

            # Reorder columns to put roll_number and name first
            columns = ['roll_number', 'name'] + sorted(list(subject_set))
            df = df.reindex(columns=columns)

            # Sort by roll number
            df = df.sort_values('roll_number')

            # Save to CSV
            df.to_csv(csv_file, index=False)
            logger.info(f"Exported mid marks data to {csv_file}")

            return str(csv_file)
        except Exception as e:
            logger.error(f"Error exporting mid marks data to CSV: {str(e)}")
            return None

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

    def close(self):
        """
        Close the browser and clean up resources.
        """
        # No Selenium driver to close - already handled Playwright resources above
        self.logged_in = False

def should_skip_combination(academic_year: str, semester: str, branch: str, section: str, data_type: str,
                         base_dir: str, cache_ttl: int, force_update: bool) -> bool:
    """
    Check if a combination should be skipped based on cache TTL.

    Args:
        academic_year: Academic year
        semester: Semester
        branch: Branch
        section: Section
        data_type: Type of data (mid_marks or attendance)
        base_dir: Base directory for data
        cache_ttl: Cache TTL in minutes (0 to disable caching)
        force_update: Whether to force update regardless of cache

    Returns:
        True if the combination should be skipped, False otherwise
    """
    if force_update or cache_ttl <= 0:
        return False  # Don't skip if force update is enabled or caching is disabled

    # Convert semester to year_of_study format for folder structure
    # Extract year and semester from the format like "First Yr - First Sem"
    year_match = re.search(r'(First|Second|Third|Fourth|Final)\s+Yr', semester, re.IGNORECASE)
    sem_match = re.search(r'(First|Second)\s+Sem', semester, re.IGNORECASE)

    if year_match and sem_match:
        year = year_match.group(1).lower()
        sem = sem_match.group(1).lower()

        # Map to year-semester format
        year_map = {'first': '1', 'second': '2', 'third': '3', 'fourth': '4', 'final': '4'}
        sem_map = {'first': '1', 'second': '2'}

        if year in year_map and sem in sem_map:
            year_of_study = f"{year_map[year]}-{sem_map[sem]}"
        else:
            # Default to a safe value if mapping fails
            year_of_study = "1-1"
    else:
        # Default to a safe value if parsing fails
        year_of_study = "1-1"

    # Check if the directory exists
    directory = Path(base_dir) / academic_year / year_of_study / branch / section
    if not directory.exists():
        return False  # Directory doesn't exist, don't skip

    # Check if any files exist in the directory
    files = list(directory.glob(f"*/{data_type}.json"))
    if not files:
        return False  # No files found, don't skip

    # Check if any file was modified within the cache TTL
    now = datetime.now().timestamp()
    cache_ttl_seconds = cache_ttl * 60  # Convert minutes to seconds

    for file in files:
        if file.exists():
            mtime = file.stat().st_mtime
            if now - mtime < cache_ttl_seconds:
                # File was modified within the cache TTL
                return True

    return False  # No recent files found, don't skip


def worker_function(worker_id: int, combination_queue: queue.Queue, result_queue: queue.Queue, args: argparse.Namespace):
    """
    Worker function to process combinations from a queue.

    Args:
        worker_id: ID of the worker
        combination_queue: Queue of combinations to process
        result_queue: Queue to store results
        args: Command line arguments
    """
    # Initialize logging for this worker
    worker_logger = logging.getLogger(f"worker-{worker_id}")
    worker_logger.setLevel(getattr(logging, args.log_level))

    # This script is only for mid marks
    data_type = "mid_marks"

    # Initialize the scraper with command line credentials or defaults from config
    username = args.username if args.username else USERNAME
    password = args.password if args.password else PASSWORD
    headless = args.headless if args.headless is not None else DEFAULT_SETTINGS['headless']
    save_debug = args.save_debug

    # Create the scraper with all settings
    scraper = MidMarksScraper(
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
        result_queue.put((worker_id, "auth_failed", None))
        return

    # Navigate to the mid marks page
    soup = scraper.navigate_to_mid_marks_page()
    if not soup:
        worker_logger.error(f"Worker {worker_id}: Failed to navigate to mid marks page. Exiting.")
        result_queue.put((worker_id, "nav_failed", None))
        return

    worker_logger.info(f"Worker {worker_id}: Ready to process combinations")

    # Process combinations from the queue
    empty_combinations_in_a_row = 0
    max_empty_combinations = 10  # Stop after this many empty combinations in a row
    combinations_processed = 0
    combinations_with_data = 0

    while True:
        try:
            # Get the next combination from the queue (non-blocking)
            try:
                combination_index, combination = combination_queue.get(block=False)
                academic_year, semester, branch, section = combination
            except queue.Empty:
                # No more combinations to process
                worker_logger.info(f"Worker {worker_id}: No more combinations to process. Exiting.")
                break

            worker_logger.info(f"Worker {worker_id}: Processing combination {combination_index}: {academic_year}, {semester}, {branch}, {section}")
            combinations_processed += 1

            # Check if we should skip this combination based on cache
            if should_skip_combination(academic_year, semester, branch, section, data_type,
                                      args.data_dir, args.cache_ttl, args.force_update):
                worker_logger.info(f"Worker {worker_id}: Skipping combination {combination_index} due to cache")
                result_queue.put((worker_id, "cached", combination))
                # Skip task_done for process mode as multiprocessing.Queue doesn't have this method
                if hasattr(combination_queue, 'task_done'):
                    combination_queue.task_done()
                continue

            # Add delay between requests if specified
            if combinations_processed > 1 and args.delay > 0:
                worker_logger.debug(f"Worker {worker_id}: Sleeping for {args.delay} seconds...")
                time.sleep(args.delay)

            # Select form filters and submit
            result_soup = scraper.select_form_filters(academic_year, semester, branch, section, is_mid_marks=True)
            if not result_soup:
                worker_logger.warning(f"Worker {worker_id}: Failed to get results for {academic_year}, {semester}, {branch}, {section}")
                result_queue.put((worker_id, "no_results", combination))
                # Skip task_done for process mode as multiprocessing.Queue doesn't have this method
                if hasattr(combination_queue, 'task_done'):
                    combination_queue.task_done()
                continue

            # Extract mid marks data
            worker_logger.info(f"Worker {worker_id}: Successfully navigated to mid marks results page")
            student_data = scraper.extract_mid_marks_data(result_soup, academic_year, semester, branch, section)

            if not student_data:
                worker_logger.warning(f"Worker {worker_id}: No {data_type} data found for {academic_year}, {semester}, {branch}, {section}")
                empty_combinations_in_a_row += 1
                result_queue.put((worker_id, "no_data", combination))

                # If we've seen too many empty combinations in a row, stop
                if empty_combinations_in_a_row >= max_empty_combinations and args.skip_empty:
                    worker_logger.warning(f"Worker {worker_id}: Found {empty_combinations_in_a_row} empty combinations in a row. Stopping.")
                    break

                # Skip task_done for process mode as multiprocessing.Queue doesn't have this method
                if hasattr(combination_queue, 'task_done'):
                    combination_queue.task_done()
                continue

            # Reset the counter since we found data
            empty_combinations_in_a_row = 0
            combinations_with_data += 1

            # Store mid marks data
            success_count, update_count = scraper.store_mid_marks_data(student_data, args.force_update)
            worker_logger.info(f"Worker {worker_id}: Processed {success_count} students with {update_count} mid marks updates")

            # Also save to CSV if not disabled
            if not args.no_csv:
                # Convert semester to year_of_study format for folder structure
                year_of_study = scraper.convert_semester_to_year_of_study(semester)

                # Export mid marks to CSV
                csv_path = scraper.export_mid_marks_to_csv(academic_year, year_of_study, branch, section)
                if csv_path:
                    worker_logger.info(f"Worker {worker_id}: Exported mid marks data to {csv_path}")

            # Put the result in the result queue
            result_queue.put((worker_id, "success", (combination, len(student_data))))
            # Skip task_done for process mode as multiprocessing.Queue doesn't have this method
            if hasattr(combination_queue, 'task_done'):
                combination_queue.task_done()

        except Exception as e:
            worker_logger.error(f"Worker {worker_id}: Error processing combination: {str(e)}")
            result_queue.put((worker_id, "error", (combination if 'combination' in locals() else None, str(e))))
            if 'combination' in locals():
                # Skip task_done for process mode as multiprocessing.Queue doesn't have this method
                if hasattr(combination_queue, 'task_done'):
                    combination_queue.task_done()

    # Clean up
    try:
        scraper.close()
    except Exception as e:
        worker_logger.error(f"Worker {worker_id}: Error closing scraper: {str(e)}")

    worker_logger.info(f"Worker {worker_id}: Finished processing {combinations_processed} combinations with {combinations_with_data} containing data")
    result_queue.put((worker_id, "finished", (combinations_processed, combinations_with_data)))


def main():
    """Main function to run the scraper."""
    parser = argparse.ArgumentParser(description='Scrape mid marks data from college website')
    parser.add_argument('--username', help='Login username (defaults to config.USERNAME)')
    parser.add_argument('--password', help='Login password (defaults to config.PASSWORD)')
    parser.add_argument('--output', default='mid_marks_data.csv', help='Output file name for mid marks data')
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
    parser.add_argument('--cache-ttl', type=int, default=60, help='Cache TTL in minutes (0 to disable caching)')

    # Multi-worker options
    parser.add_argument('--workers', type=int, default=1, help='Number of worker processes/threads for parallel scraping')
    parser.add_argument('--worker-mode', choices=['process', 'thread'], default='process',
                        help='Worker mode: process (separate processes) or thread (separate threads)')

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

    # Initialize the scraper with command line credentials or defaults from config
    username = args.username if args.username else USERNAME
    password = args.password if args.password else PASSWORD
    headless = args.headless if args.headless is not None else DEFAULT_SETTINGS['headless']

    # Create the scraper with all settings
    scraper = MidMarksScraper(
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
        logger.error("Authentication failed. Exiting.")
        sys.exit(1)

    # This script is only for mid marks
    data_type = "mid_marks"

    # Navigate to the mid marks page
    soup = scraper.navigate_to_mid_marks_page()
    if not soup:
        logger.error("Failed to navigate to mid marks page. Exiting.")
        sys.exit(1)

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
    num_workers = args.workers
    worker_mode = args.worker_mode

    if num_workers > 1:
        logger.info(f"Using {num_workers} workers in {worker_mode} mode")

        # Create queues for combinations and results
        if worker_mode == 'thread':
            # Use thread-safe queues
            combination_queue = queue.Queue()
            result_queue = queue.Queue()
        else:
            # Use process-safe queues
            combination_queue = multiprocessing.Queue()
            result_queue = multiprocessing.Queue()

        # Add combinations to the queue
        for i, combination in enumerate(combinations):
            combination_queue.put((i+1, combination))

        logger.info(f"Added {len(combinations)} combinations to the queue")

        # Create and start workers
        workers = []
        if worker_mode == 'thread':
            # Use threads
            for i in range(num_workers):
                worker = threading.Thread(
                    target=worker_function,
                    args=(i+1, combination_queue, result_queue, args)
                )
                workers.append(worker)
                worker.start()
                logger.info(f"Started worker thread {i+1}")
        else:
            # Use processes
            for i in range(num_workers):
                worker = multiprocessing.Process(
                    target=worker_function,
                    args=(i+1, combination_queue, result_queue, args)
                )
                workers.append(worker)
                worker.start()
                logger.info(f"Started worker process {i+1}")

        # Wait for all workers to finish
        for worker in workers:
            worker.join()

        logger.info("All workers have finished")

        # Process results
        total_combinations_tried = 0
        total_combinations_with_data = 0
        total_students_found = 0

        # Get all results from the queue
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        # Process results
        for worker_id, status, data in results:
            if status == "success":
                combination, num_students = data
                total_combinations_with_data += 1
                total_students_found += num_students
                logger.info(f"Worker {worker_id} successfully processed {combination} with {num_students} students")
            elif status == "cached":
                combination = data
                # Count cached combinations as processed and with data
                total_combinations_tried += 1
                total_combinations_with_data += 1
                # We don't know how many students, but we can estimate based on average
                estimated_students = 30  # Reasonable estimate for a class
                total_students_found += estimated_students
                logger.info(f"Worker {worker_id} skipped {combination} due to cache (estimated {estimated_students} students)")
            elif status == "finished":
                combinations_processed, combinations_with_data = data
                total_combinations_tried += combinations_processed
                logger.info(f"Worker {worker_id} finished processing {combinations_processed} combinations with {combinations_with_data} containing data")

    else:
        # Use single-worker mode (original code)
        logger.info("Using single-worker mode")

        total_students_found = 0
        total_combinations_tried = 0
        total_combinations_with_data = 0
        empty_combinations_in_a_row = 0
        max_empty_combinations = 10  # Stop after this many empty combinations in a row

        for i, (academic_year, semester, branch, section) in enumerate(combinations):
            total_combinations_tried += 1
            logger.info(f"Trying combination {i+1}/{len(combinations)}: {academic_year}, {semester}, {branch}, {section}")

            # Add delay between requests if specified
            if i > 0 and args.delay > 0:
                logger.debug(f"Sleeping for {args.delay} seconds...")
                time.sleep(args.delay)

            # Select form filters and submit
            result_soup = scraper.select_form_filters(academic_year, semester, branch, section, is_mid_marks=True)
            if not result_soup:
                logger.warning(f"Failed to get results for {academic_year}, {semester}, {branch}, {section}")
                continue

            # Extract mid marks data
            logger.info("Successfully navigated to mid marks results page")
            student_data = scraper.extract_mid_marks_data(result_soup, academic_year, semester, branch, section)

            if not student_data:
                logger.warning(f"No {data_type} data found for {academic_year}, {semester}, {branch}, {section}")
                empty_combinations_in_a_row += 1

                # If we've seen too many empty combinations in a row, stop
                if empty_combinations_in_a_row >= max_empty_combinations and args.skip_empty:
                    logger.warning(f"Found {empty_combinations_in_a_row} empty combinations in a row. Stopping.")
                    break

                continue

            # Reset the counter since we found data
            empty_combinations_in_a_row = 0
            total_combinations_with_data += 1

            # Store mid marks data
            success_count, update_count = scraper.store_mid_marks_data(student_data, args.force_update)
            logger.info(f"Processed {success_count} students with {update_count} mid marks updates")

            # Also save to CSV if not disabled
            if not args.no_csv:
                # Convert semester to year_of_study format for folder structure
                year_of_study = scraper.convert_semester_to_year_of_study(semester)

                # Export mid marks to CSV
                csv_path = scraper.export_mid_marks_to_csv(academic_year, year_of_study, branch, section)
                if csv_path:
                    logger.info(f"Exported mid marks data to {csv_path}")
            else:
                logger.debug("CSV generation disabled")

            total_students_found += len(student_data)

    # Print summary statistics
    logger.info("\n" + "="*80)
    logger.info(f"SCRAPING SUMMARY ({data_type.upper()})")
    logger.info("="*80)
    logger.info(f"Total combinations tried: {total_combinations_tried} / {len(combinations)}")
    logger.info(f"Combinations with data: {total_combinations_with_data}")
    logger.info(f"Total students found: {total_students_found}")
    logger.info(f"Data directory: {args.data_dir}")

    if total_students_found > 0:
        logger.info(f"\n{data_type.capitalize()} scraping completed successfully!")
        logger.info(f"Found data for {total_students_found} students across {total_combinations_with_data} combinations.")
    else:
        logger.warning(f"\nNo {data_type} data found for any combination.")
        logger.warning("Try different parameters or check the website structure.")

    logger.info("="*80)

    # Close the scraper
    if num_workers <= 1:  # Only close in single-worker mode, workers close their own scrapers
        try:
            scraper.close()
        except Exception as e:
            logger.error(f"Error closing scraper: {e}")


if __name__ == "__main__":
    main()
