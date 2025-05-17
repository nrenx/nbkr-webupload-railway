#!/usr/bin/env python3
"""
Attendance Scraper for College Website

This script logs into the college portal and navigates to the attendance page.
It extracts attendance data and stores it in a structured format.
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
import concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Union
from functools import wraps

import requests
from bs4 import BeautifulSoup

# Import Selenium for browser automation
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    print("Warning: Selenium is not installed. Browser automation will not be available.")

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
                except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as e:
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
    A class to scrape attendance data from the college website.
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
        self.driver = None

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
            logger.info("Initialized attendance scraper in headless mode")
        else:
            logger.info("Initialized attendance scraper in interactive mode")

        # Initialize Selenium WebDriver if available
        if SELENIUM_AVAILABLE:
            try:
                options = Options()

                # Common options for both headless and non-headless mode
                options.add_argument('--disable-gpu')
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-dev-shm-usage')

                # Check if running on Render.com or Railway.app (environment detection)
                is_render = os.environ.get('RENDER') == 'true'
                is_railway = 'RAILWAY_ENVIRONMENT' in os.environ

                # Log the environment for debugging
                if is_render:
                    logger.info("Running on Render.com, using special Chrome configuration")
                elif is_railway:
                    logger.info("Running on Railway.app, using special Chrome configuration")

                # Configure for cloud environments
                if is_render or is_railway:
                    # Always use headless mode on cloud platforms
                    options.add_argument('--headless=new')

                    # Try different Chrome binary locations based on platform
                    if is_render:
                        # Render.com Chrome locations
                        options.binary_location = "/usr/bin/google-chrome-stable"
                    elif is_railway:
                        # Railway.app Chrome locations
                        options.binary_location = "/usr/bin/google-chrome-stable"

                    # Additional options for cloud environments
                    options.add_argument('--disable-setuid-sandbox')
                    options.add_argument('--disable-dev-shm-usage')
                    options.add_argument('--single-process')
                elif self.headless:
                    # Local headless mode
                    options.add_argument('--headless=new')
                else:
                    # Local non-headless mode
                    # Make sure the browser window is visible
                    options.add_argument('--start-maximized')
                    options.add_argument('--disable-extensions')
                    options.add_argument('--disable-infobars')
                    options.add_argument('--window-size=1920,1080')
                    options.add_experimental_option('detach', True)  # Keep browser open
                    options.add_experimental_option('excludeSwitches', ['enable-automation'])
                    options.add_experimental_option('useAutomationExtension', False)

                # Use the Service class to specify the chromedriver path if needed
                # Uncomment and modify the line below if you need to specify a custom path
                # service = Service('/path/to/chromedriver')

                # Try different approaches to initialize the Chrome driver
                try:
                    # First try: Use webdriver_manager to get the correct driver
                    try:
                        from webdriver_manager.chrome import ChromeDriverManager
                        from selenium.webdriver.chrome.service import Service as ChromeService

                        # Get the latest ChromeDriver
                        driver_path = ChromeDriverManager().install()
                        service = ChromeService(driver_path)

                        # Set the service path explicitly to avoid using system ChromeDriver
                        self.driver = webdriver.Chrome(service=service, options=options)
                        logger.debug(f"Initialized Chrome WebDriver using webdriver_manager with path: {driver_path}")
                    except Exception as e:
                        logger.warning(f"Failed to initialize Chrome WebDriver using webdriver_manager: {e}")

                        # Second try: Use default Chrome driver without webdriver_manager
                        try:
                            logger.info("Trying to initialize Chrome WebDriver without webdriver_manager")
                            self.driver = webdriver.Chrome(options=options)
                            logger.debug("Initialized Chrome WebDriver using default Chrome driver")
                        except Exception as e2:
                            logger.error(f"Failed to initialize Chrome WebDriver using default approach: {e2}")

                            # Third try: If on Render or Railway, try with specific binary locations
                            if is_render or is_railway:
                                # Try different Chrome binary locations
                                chrome_locations = [
                                    "/opt/render/chrome/chrome",  # Render location
                                    "/opt/google/chrome/chrome",  # Another possible location
                                    "/usr/bin/chromium",          # Chromium as fallback
                                    "/usr/bin/chromium-browser",  # Another Chromium name
                                ]

                                success = False
                                for chrome_path in chrome_locations:
                                    try:
                                        logger.info(f"Trying Chrome at location: {chrome_path}")
                                        options.binary_location = chrome_path
                                        self.driver = webdriver.Chrome(options=options)
                                        logger.debug(f"Initialized Chrome WebDriver using binary at {chrome_path}")
                                        success = True
                                        break
                                    except Exception as e3:
                                        logger.warning(f"Failed to initialize Chrome WebDriver with binary at {chrome_path}: {e3}")

                                if not success:
                                    logger.error("Failed to initialize Chrome WebDriver with any known binary location")
                                    # Don't raise, just continue with requests-based approach
                            else:
                                raise
                except Exception as e:
                    logger.error(f"All attempts to initialize Chrome WebDriver failed: {e}")
                    # Don't raise here, just set driver to None and continue with requests-based approach
                    self.driver = None

                # Only set window size and implicit wait if driver was successfully initialized
                if self.driver:
                    self.driver.set_window_size(1366, 768)
                    self.driver.implicitly_wait(10)  # Wait up to 10 seconds for elements to appear
                    logger.info("Initialized Chrome WebDriver")
                else:
                    logger.warning("Chrome WebDriver initialization failed, falling back to requests-based scraping")
            except Exception as e:
                logger.error(f"Error initializing Chrome WebDriver: {e}")
                self.driver = None
        else:
            logger.warning("Selenium is not available. Using requests-based scraping only.")

    def __del__(self):
        """Clean up resources when the object is destroyed."""
        if self.driver:
            try:
                self.driver.quit()
                logger.debug("Chrome WebDriver closed")
            except Exception as e:
                logger.error(f"Error closing Chrome WebDriver: {e}")

    @retry_on_network_error()
    def authenticate(self) -> bool:
        """
        Authenticate with the college portal.

        Returns:
            Boolean indicating success
        """
        if self.logged_in:
            return True

        # Try to authenticate using Selenium if available
        if self.driver:
            try:
                logger.info("Authenticating using Selenium...")
                # Go directly to the attendance portal URL which will redirect to login page
                self.driver.get(ATTENDANCE_PORTAL_URL)

                # Wait for the login form to load
                WebDriverWait(self.driver, self.timeout).until(
                    EC.presence_of_element_located((By.NAME, "username"))
                )

                # Log the current URL to verify we're on the login page
                logger.debug(f"Current URL after navigation: {self.driver.current_url}")

                # Fill in the login form
                username_field = self.driver.find_element(By.NAME, "username")
                password_field = self.driver.find_element(By.NAME, "password")

                username_field.clear()
                password_field.clear()

                username_field.send_keys(self.username)
                password_field.send_keys(self.password)

                # Submit the form
                password_field.submit()

                # Wait for the page to load
                time.sleep(2)

                # Check if login was successful
                if "login" not in self.driver.current_url.lower():
                    self.logged_in = True
                    logger.info("Login successful using Selenium")
                    return True
                else:
                    logger.error("Login failed using Selenium")
                    return False

            except Exception as e:
                logger.error(f"Error authenticating using Selenium: {e}")
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
    def navigate_to_attendance_page(self) -> Optional[BeautifulSoup]:
        """
        Navigate to the attendance page.

        Returns:
            BeautifulSoup object of the attendance page or None if failed
        """
        if not self.logged_in and not self.authenticate():
            return None

        # Try to navigate using Selenium if available
        if self.driver:
            try:
                # We should already be on the attendance page after authentication
                # But let's navigate there explicitly to be sure
                logger.info(f"Navigating to attendance page using Selenium: {ATTENDANCE_PORTAL_URL}")
                self.driver.get(ATTENDANCE_PORTAL_URL)

                # Wait for the page to load
                time.sleep(2)

                # Log the current URL for debugging
                logger.debug(f"Current URL after navigation: {self.driver.current_url}")

                # Check if we're on the correct page
                if "attendance" in self.driver.page_source.lower():
                    logger.info("Successfully navigated to attendance page using Selenium")
                    # Parse the HTML content
                    soup = BeautifulSoup(self.driver.page_source, 'html.parser')
                    return soup
                else:
                    logger.warning("Navigation to attendance page failed using Selenium - redirected to another page")
                    # Fall back to requests-based navigation
            except Exception as e:
                logger.error(f"Error navigating to attendance page using Selenium: {e}")
                # Fall back to requests-based navigation

        # Use requests-based navigation as fallback
        try:
            # We should already be on the attendance page after authentication
            # But let's navigate there explicitly to be sure
            logger.info(f"Navigating to attendance page using requests: {ATTENDANCE_PORTAL_URL}")
            response = self.session.get(ATTENDANCE_PORTAL_URL, timeout=self.timeout)
            response.raise_for_status()

            # Log the current URL for debugging
            logger.debug(f"Current URL after navigation: {response.url}")

            # Parse the HTML content
            soup = BeautifulSoup(response.text, 'html.parser')

            # Check if we're on the correct page
            if "attendance" in response.text.lower():
                logger.info("Successfully navigated to attendance page using requests")
                return soup
            else:
                logger.warning("Navigation to attendance page failed using requests - redirected to another page")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Error navigating to attendance page using requests: {e}")
            return None

    @retry_on_network_error()
    def select_form_filters(self, academic_year: str, semester: str, branch: str, section: str) -> Optional[BeautifulSoup]:
        """
        Select form filters for attendance data.

        Args:
            academic_year: Academic year to select (e.g., "2023-24")
            semester: Semester to select (e.g., "First Yr - First Sem")
            branch: Branch to select (e.g., "CSE")
            section: Section to select (e.g., "A")

        Returns:
            BeautifulSoup object of the results page or None if failed
        """
        soup = self.navigate_to_attendance_page()
        if not soup:
            return None

        # Try to use Selenium if available
        if self.driver:
            try:
                logger.info(f"Using Selenium to select form filters: academic_year={academic_year}, semester={semester}, branch={branch}, section={section}")

                # Wait for the form to load
                time.sleep(2)

                # Find all select elements
                select_elements = self.driver.find_elements(By.TAG_NAME, 'select')
                if not select_elements:
                    logger.error("No select elements found in the form using Selenium")
                    # Fall back to requests-based form submission
                else:
                    # Find and set academic year
                    academic_year_set = False
                    for select in select_elements:
                        select_name = select.get_attribute('name') or ''
                        select_id = select.get_attribute('id') or ''
                        if 'year' in select_name.lower() or 'year' in select_id.lower() or 'academic' in select_name.lower():
                            # Create a Select object
                            from selenium.webdriver.support.ui import Select as SeleniumSelect
                            select_obj = SeleniumSelect(select)

                            # Try to find an option with text or value matching the academic year
                            option_found = False
                            for option in select.find_elements(By.TAG_NAME, 'option'):
                                option_text = option.text.strip()
                                if academic_year in option_text:
                                    select_obj.select_by_visible_text(option_text)
                                    academic_year_set = True
                                    option_found = True
                                    break

                            if not option_found:
                                # If no match found, select the first option
                                select_obj.select_by_index(0)
                                academic_year_set = True
                            break

                    if not academic_year_set:
                        logger.warning("Could not find academic year select element using Selenium")

                    # Find and set semester
                    semester_set = False
                    for select in select_elements:
                        select_name = select.get_attribute('name') or ''
                        select_id = select.get_attribute('id') or ''
                        if 'sem' in select_name.lower() or 'sem' in select_id.lower():
                            # Create a Select object
                            from selenium.webdriver.support.ui import Select as SeleniumSelect
                            select_obj = SeleniumSelect(select)

                            # Try to find an option with text matching the semester
                            option_found = False
                            for option in select.find_elements(By.TAG_NAME, 'option'):
                                option_text = option.text.strip()
                                if semester.lower() in option_text.lower():
                                    select_obj.select_by_visible_text(option_text)
                                    semester_set = True
                                    option_found = True
                                    break

                            if not option_found:
                                # If no match found, select the first option
                                select_obj.select_by_index(0)
                                semester_set = True
                            break

                    if not semester_set:
                        logger.warning("Could not find semester select element using Selenium")

                    # Find and set branch
                    branch_set = False
                    for select in select_elements:
                        select_name = select.get_attribute('name') or ''
                        select_id = select.get_attribute('id') or ''
                        if 'branch' in select_name.lower() or 'branch' in select_id.lower() or 'dept' in select_name.lower():
                            # Create a Select object
                            from selenium.webdriver.support.ui import Select as SeleniumSelect
                            select_obj = SeleniumSelect(select)

                            # Try to find an option with text matching the branch
                            option_found = False
                            for option in select.find_elements(By.TAG_NAME, 'option'):
                                option_text = option.text.strip()
                                if branch.lower() in option_text.lower():
                                    select_obj.select_by_visible_text(option_text)
                                    branch_set = True
                                    option_found = True
                                    break

                            if not option_found:
                                # If no match found, select the first option
                                select_obj.select_by_index(0)
                                branch_set = True
                            break

                    if not branch_set:
                        logger.warning("Could not find branch select element using Selenium")

                    # Find and set section
                    section_set = False
                    for select in select_elements:
                        select_name = select.get_attribute('name') or ''
                        select_id = select.get_attribute('id') or ''
                        if 'section' in select_name.lower() or 'section' in select_id.lower():
                            # Create a Select object
                            from selenium.webdriver.support.ui import Select as SeleniumSelect
                            select_obj = SeleniumSelect(select)

                            # Try to find an option with text matching the section
                            option_found = False
                            for option in select.find_elements(By.TAG_NAME, 'option'):
                                option_text = option.text.strip()
                                if section == option_text or section == option.get_attribute('value'):
                                    select_obj.select_by_visible_text(option_text)
                                    section_set = True
                                    option_found = True
                                    break

                            if not option_found:
                                # If no match found, select the first option
                                select_obj.select_by_index(0)
                                section_set = True
                            break

                    if not section_set:
                        logger.warning("Could not find section select element using Selenium")

                    # Find and click the show button (using the approach from your old project)
                    try:
                        # First try: Look for input button with value 'Show' using XPath
                        show_button = None
                        try:
                            # This is the most specific XPath that should find the Show button
                            show_button = WebDriverWait(self.driver, 10).until(
                                EC.element_to_be_clickable((By.XPATH, "//input[@type='button'][@value='Show']"))
                            )
                            logger.info("Found Show button using XPath")
                        except Exception as e:
                            logger.debug(f"Could not find Show button using XPath: {e}")

                        # Second try: Look for any button with 'show' in its value
                        if not show_button:
                            try:
                                buttons = self.driver.find_elements(By.TAG_NAME, 'input')
                                for button in buttons:
                                    if button.get_attribute('type') == 'button' and button.get_attribute('value') and 'show' in button.get_attribute('value').lower():
                                        show_button = button
                                        logger.info(f"Found Show button with value: {button.get_attribute('value')}")
                                        break
                            except Exception as e:
                                logger.debug(f"Error searching for Show button by tag name: {e}")

                        # Third try: Look for any input with type='submit'
                        if not show_button:
                            try:
                                buttons = self.driver.find_elements(By.TAG_NAME, 'input')
                                for button in buttons:
                                    if button.get_attribute('type') == 'submit':
                                        show_button = button
                                        logger.info(f"Found submit button as fallback: {button.get_attribute('value')}")
                                        break
                            except Exception as e:
                                logger.debug(f"Error searching for submit button: {e}")

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
                            self.driver.save_screenshot(str(screenshot_path))
                            logger.debug(f"Saved screenshot before clicking button to {screenshot_path}")

                        if show_button:
                            logger.info(f"Found button with value: {show_button.get_attribute('value')}")
                            # Scroll to the button to make sure it's visible
                            self.driver.execute_script("arguments[0].scrollIntoView(true);", show_button)
                            time.sleep(1)  # Wait for scroll to complete

                            # Try JavaScript click first (more reliable)
                            try:
                                self.driver.execute_script("arguments[0].click();", show_button)
                                logger.info("Clicked show button using JavaScript")
                            except Exception as js_e:
                                logger.warning(f"JavaScript click failed: {js_e}, trying regular click")
                                show_button.click()
                                logger.info("Clicked show button using regular click")

                            # Wait for the results page to load
                            time.sleep(5)  # Increased wait time

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
                                html_path = debug_folder / "after_click.html"
                                with open(html_path, 'w', encoding='utf-8') as f:
                                    f.write(self.driver.page_source)
                                logger.debug(f"Saved HTML content after clicking to {html_path}")

                                # Also take a screenshot after clicking
                                screenshot_path = debug_folder / "after_click.png"
                                self.driver.save_screenshot(str(screenshot_path))
                                logger.debug(f"Saved screenshot after clicking button to {screenshot_path}")

                            # Check if we got results
                            if 'No Records Found' in self.driver.page_source:
                                logger.warning(f"No records found for {academic_year}, {semester}, {branch}, {section} using Selenium")
                                return None

                            # Parse the HTML content
                            result_soup = BeautifulSoup(self.driver.page_source, 'html.parser')

                            # Check if we have student rows with IDs (a good indicator of success)
                            student_rows = result_soup.find_all('tr', attrs={'id': True})
                            if student_rows:
                                logger.info(f"Found {len(student_rows)} student rows with IDs - form submission successful")
                                return result_soup
                            else:
                                logger.warning("No student rows found in the result - form submission may have failed")
                                # Continue with fallback
                        else:
                            logger.warning("Could not find show button using Selenium")
                            # Fall back to requests-based form submission
                    except Exception as e:
                        logger.error(f"Error clicking show button: {e}")
                        # Fall back to requests-based form submission
            except Exception as e:
                logger.error(f"Error submitting form using Selenium: {e}")
                # Fall back to requests-based form submission

        # Use requests-based form submission as fallback
        try:
            # Get the form and its action URL
            form = soup.find('form')
            if not form:
                logger.error("Could not find form on attendance page")
                return None

            form_action = form.get('action', ATTENDANCE_PORTAL_URL)
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
    def extract_attendance_data(self, soup: BeautifulSoup, academic_year: str, semester: str, branch: str, section: str) -> List[Dict[str, Any]]:
        """
        Extract attendance data from the page.

        Args:
            soup: BeautifulSoup object of the page
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
                    f.write(str(soup))
                logger.debug(f"Saved HTML content to {filepath}")

            # Direct extraction using the pattern from the old project
            attendance_data = []

            # Find all rows with IDs (these are student rows in the attendance table)
            student_rows = soup.find_all('tr', attrs={'id': True})
            if student_rows:
                logger.info(f"Found {len(student_rows)} student rows with IDs")

                for tr_tag in student_rows:
                    try:
                        # Get the roll number from the row ID
                        roll_number = tr_tag.get('id', '').strip()
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
                        td_roll_no = tr_tag.find('td', {'class': 'tdRollNo'})
                        if td_roll_no:
                            # First try to get the roll number from the id attribute (removing 'td' prefix)
                            id_attr = td_roll_no.get('id', '')
                            if id_attr and id_attr.startswith('td'):
                                roll_number = id_attr[2:]  # Remove 'td' prefix
                                student_data['roll_number'] = roll_number
                            # If no id attribute or it doesn't start with 'td', use the text content
                            else:
                                roll_number_text = td_roll_no.text.strip().replace(' ', '')
                                if roll_number_text:
                                    # Extract just the roll number part if it has a date in parentheses
                                    if '(' in roll_number_text and ')' in roll_number_text:
                                        roll_number_text = roll_number_text.split('(')[0].strip().replace(' ', '')
                                    student_data['roll_number'] = roll_number_text

                        # Extract attendance percentage from tdPercent class
                        td_percent = tr_tag.find('td', {'class': 'tdPercent'})
                        if td_percent:
                            # The percentage is the first text content
                            if td_percent.contents:
                                student_data['data']['attendance_percentage'] = td_percent.contents[0].strip()

                            # The total classes is in a font tag
                            font_tag = td_percent.find('font')
                            if font_tag:
                                student_data['data']['total_classes'] = font_tag.text.strip()

                        # Extract subject data from cells with title attributes
                        subject_cells = [td for td in tr_tag.find_all('td') if 'title' in td.attrs]
                        for cell in subject_cells:
                            subject_name = cell.get('title', '').strip()
                            if subject_name:
                                value = cell.text.strip()
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

            # Try different approaches to find student rows if direct extraction failed

            # Approach 1: Look for cells with class tdRollNo
            roll_no_cells = soup.find_all('td', {'class': 'tdRollNo'})

            if roll_no_cells:
                logger.info(f"Found {len(roll_no_cells)} student rows using tdRollNo class")
                return self.extract_attendance_data_approach1(soup, roll_no_cells, academic_year, semester, branch, section)

            # Approach 2: Look for tables with student data
            tables = soup.find_all('table')
            if tables:
                logger.info(f"Found {len(tables)} tables, trying to extract data from them")
                result = self.extract_attendance_data_approach2(soup, tables, academic_year, semester, branch, section)
                if result:
                    return result

            # Approach 4: Try to extract data from saved HTML files if available (only if --save-debug is enabled)
            if self.settings.get('save_debug', False):
                debug_dir = Path("debug_output")
                if debug_dir.exists():
                    # Convert semester to year_of_study format for folder structure
                    year_of_study = self.convert_semester_to_year_of_study(semester)

                    # Create a structured folder path
                    debug_folder = debug_dir / academic_year / year_of_study / branch / section

                    # Check if the folder exists
                    if debug_folder.exists():
                        # Look for HTML files in the folder
                        html_files = list(debug_folder.glob("*.html"))
                        if html_files:
                            logger.info(f"Found {len(html_files)} saved HTML files in {debug_folder}, trying to extract data from them")
                            for html_file in html_files:
                                try:
                                    # Read the HTML file
                                    with open(html_file, 'r', encoding='utf-8') as f:
                                        html_content = f.read()
                                    file_soup = BeautifulSoup(html_content, 'html.parser')

                                    # Try direct extraction from the file
                                    student_rows = file_soup.find_all('tr', attrs={'id': True})
                                    if student_rows:
                                        logger.info(f"Found {len(student_rows)} student rows with IDs in saved file {html_file}")
                                        # Extract data from student rows
                                        attendance_data = []
                                        for tr_tag in student_rows:
                                            try:
                                                # Get the roll number from the row ID
                                                roll_number = tr_tag.get('id', '').strip()
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
                                                td_roll_no = tr_tag.find('td', {'class': 'tdRollNo'})
                                                if td_roll_no:
                                                    # First try to get the roll number from the id attribute (removing 'td' prefix)
                                                    id_attr = td_roll_no.get('id', '')
                                                    if id_attr and id_attr.startswith('td'):
                                                        roll_number = id_attr[2:]  # Remove 'td' prefix
                                                        student_data['roll_number'] = roll_number
                                                    # If no id attribute or it doesn't start with 'td', use the text content
                                                    else:
                                                        roll_number_text = td_roll_no.text.strip().replace(' ', '')
                                                        if roll_number_text:
                                                            # Extract just the roll number part if it has a date in parentheses
                                                            if '(' in roll_number_text and ')' in roll_number_text:
                                                                roll_number_text = roll_number_text.split('(')[0].strip().replace(' ', '')
                                                            student_data['roll_number'] = roll_number_text

                                                # Extract attendance percentage from tdPercent class
                                                td_percent = tr_tag.find('td', {'class': 'tdPercent'})
                                                if td_percent:
                                                    # The percentage is the first text content
                                                    if td_percent.contents:
                                                        student_data['data']['attendance_percentage'] = td_percent.contents[0].strip()

                                                    # The total classes is in a font tag
                                                    font_tag = td_percent.find('font')
                                                    if font_tag:
                                                        student_data['data']['total_classes'] = font_tag.text.strip()

                                                # Extract subject data from cells with title attributes
                                                subject_cells = [td for td in tr_tag.find_all('td') if 'title' in td.attrs]
                                                for cell in subject_cells:
                                                    subject_name = cell.get('title', '').strip()
                                                    if subject_name:
                                                        value = cell.text.strip()
                                                        if value:  # Only add non-empty values
                                                            student_data['data'][self.normalize_key(subject_name)] = value

                                                # Only add if we have actual data
                                                if student_data['data']:
                                                    attendance_data.append(student_data)

                                            except Exception as e:
                                                logger.error(f"Error extracting data from student row in saved file: {e}")

                                        if attendance_data:
                                            logger.info(f"Extracted attendance data for {len(attendance_data)} students from saved file {html_file}")
                                            return attendance_data

                                    # Try to extract data from tables in the file
                                    tables = file_soup.find_all('table')
                                    if tables:
                                        result = self.extract_attendance_data_approach2(file_soup, tables, academic_year, semester, branch, section)
                                        if result:
                                            logger.info(f"Successfully extracted data from saved file {html_file}")
                                            return result
                                except Exception as e:
                                    logger.error(f"Error extracting data from saved file {html_file}: {e}")

            # Approach 3: Look for any rows with roll numbers
            all_rows = soup.find_all('tr')
            if all_rows:
                logger.info(f"Found {len(all_rows)} rows, trying to extract data from them")
                return self.extract_attendance_data_approach3(soup, all_rows, academic_year, semester, branch, section)

            logger.warning(f"No student rows found for {academic_year}, {semester}, {branch}, {section}")
            return []

        except Exception as e:
            logger.error(f"Error extracting attendance data: {str(e)}")
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

            # First try to get the roll number from the id attribute (removing 'td' prefix)
            id_attr = roll_cell.get('id', '')
            if id_attr and id_attr.startswith('td'):
                roll_number = id_attr[2:]  # Remove 'td' prefix
            else:
                # If no id attribute or it doesn't start with 'td', use the text content
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

                    # First try to get the roll number from the id attribute (removing 'td' prefix)
                    id_attr = roll_cell.get('id', '')
                    if id_attr and id_attr.startswith('td'):
                        roll_number = id_attr[2:]  # Remove 'td' prefix
                    else:
                        # If no id attribute or it doesn't start with 'td', use the text content
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

    def close(self):
        """
        Close the browser and clean up resources.
        """
        if self.driver:
            try:
                self.driver.quit()
                logger.debug("Browser closed successfully")
            except Exception as e:
                logger.error(f"Error closing browser: {str(e)}")
            finally:
                self.driver = None
                self.logged_in = False

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
        result_queue.put((worker_id, "auth_failed", None))
        return

    # Navigate to attendance page
    soup = scraper.navigate_to_attendance_page()
    if not soup:
        worker_logger.error(f"Worker {worker_id}: Failed to navigate to attendance page. Exiting.")
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

            # Add delay between requests if specified
            if combinations_processed > 1 and args.delay > 0:
                worker_logger.debug(f"Worker {worker_id}: Sleeping for {args.delay} seconds...")
                time.sleep(args.delay)

            # Select form filters and submit
            result_soup = scraper.select_form_filters(academic_year, semester, branch, section)
            if not result_soup:
                worker_logger.warning(f"Worker {worker_id}: Failed to get results for {academic_year}, {semester}, {branch}, {section}")
                result_queue.put((worker_id, "no_results", combination))
                # Skip task_done for process mode as multiprocessing.Queue doesn't have this method
                if hasattr(combination_queue, 'task_done'):
                    combination_queue.task_done()
                continue

            # Extract and save data
            worker_logger.info(f"Worker {worker_id}: Successfully navigated to attendance results page")
            attendance_data = scraper.extract_attendance_data(result_soup, academic_year, semester, branch, section)

            if not attendance_data:
                worker_logger.warning(f"Worker {worker_id}: No attendance data found for {academic_year}, {semester}, {branch}, {section}")
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

            # Put the result in the result queue
            result_queue.put((worker_id, "success", (combination, len(attendance_data))))
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
        logger.error("Authentication failed. Exiting.")
        sys.exit(1)

    # Navigate to attendance page
    soup = scraper.navigate_to_attendance_page()
    if not soup:
        logger.error("Failed to navigate to attendance page. Exiting.")
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
            result_soup = scraper.select_form_filters(academic_year, semester, branch, section)
            if not result_soup:
                logger.warning(f"Failed to get results for {academic_year}, {semester}, {branch}, {section}")
                continue

            # Extract and save data
            logger.info("Successfully navigated to attendance results page")
            attendance_data = scraper.extract_attendance_data(result_soup, academic_year, semester, branch, section)

            if not attendance_data:
                logger.warning(f"No attendance data found for {academic_year}, {semester}, {branch}, {section}")
                empty_combinations_in_a_row += 1

                # If we've seen too many empty combinations in a row, stop
                if empty_combinations_in_a_row >= max_empty_combinations and args.skip_empty:
                    logger.warning(f"Found {empty_combinations_in_a_row} empty combinations in a row. Stopping.")
                    break

                continue

            # Reset the counter since we found data
            empty_combinations_in_a_row = 0
            total_combinations_with_data += 1

            # Store data in structured format
            success_count, update_count = scraper.store_attendance_data(attendance_data, args.force_update)
            logger.info(f"Processed {success_count} students with {update_count} updates")

            # Also save to CSV if not disabled
            if not args.no_csv:
                # Convert semester to year_of_study format for folder structure
                year_of_study = scraper.convert_semester_to_year_of_study(semester)

                # Create a filename for the CSV
                output_file = f"{branch}_{section}_{args.output}"

                # Save to CSV with structured folder path
                scraper.save_to_csv(attendance_data, output_file, academic_year, year_of_study)
            else:
                logger.debug("CSV generation disabled")

            total_students_found += len(attendance_data)

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

    # Close the scraper
    if num_workers <= 1:  # Only close in single-worker mode, workers close their own scrapers
        scraper.close()


if __name__ == "__main__":
    main()
