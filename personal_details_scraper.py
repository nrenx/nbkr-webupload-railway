#!/usr/bin/env python3
"""
Personal Details Scraper for College Website

This script logs into the college portal and navigates to the personal details page.
It extracts personal details data and stores it in a structured format.
"""

import sys
import logging
import argparse
import json
import time
import re
# Imports for parallel processing
import queue
import threading
import multiprocessing
import concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from functools import wraps

import requests
from bs4 import BeautifulSoup

# Make selenium optional
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, NoSuchElementException
    SELENIUM_AVAILABLE = True

    # Make ChromeDriverManager optional
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        WEBDRIVER_MANAGER_AVAILABLE = True
    except ImportError:
        WEBDRIVER_MANAGER_AVAILABLE = False
        print("Warning: webdriver_manager is not installed. Will use default ChromeDriver.")
except ImportError:
    SELENIUM_AVAILABLE = False
    WEBDRIVER_MANAGER_AVAILABLE = False
    print("Warning: selenium is not installed. Browser automation will be disabled.")

# Make pandas optional
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    print("Warning: pandas is not installed. CSV/Excel export functionality will be limited.")

# Import login utilities and configuration
from login_utils import create_session, login, login_to_attendance, is_logged_in, BASE_URL
from config import (
    USERNAME, PASSWORD, PERSONAL_DETAILS_URL,
    DEFAULT_ACADEMIC_YEARS, DEFAULT_SEMESTERS,
    DEFAULT_BRANCHES, DEFAULT_SECTIONS,
    YEAR_SEM_CODES, BRANCH_CODES, DEFAULT_SETTINGS
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("personal_details_scraper.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("personal_details_scraper")

# Define a retry decorator for network errors
def retry_on_network_error(max_retries=2, delay=1):
    """
    Decorator to retry a function on network errors.

    Args:
        max_retries: Maximum number of retries
        delay: Delay between retries in seconds

    Returns:
        Decorated function
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            while retries <= max_retries:
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.RequestException, TimeoutException) as e:
                    retries += 1
                    if retries > max_retries:
                        logger.error(f"Max retries ({max_retries}) exceeded for {func.__name__}: {e}")
                        # Return a default value based on the function's return type
                        # For boolean functions, return False
                        if func.__name__ == 'authenticate':
                            logger.error(f"Authentication failed after {max_retries} retries. Please check your network connection.")
                            return False
                        # For functions that return BeautifulSoup, return None
                        elif func.__name__ in ['navigate_to_personal_details_page', 'select_form_filters']:
                            return None
                        # For other functions, raise the exception
                        else:
                            raise

                    # Use a shorter delay for faster feedback
                    retry_delay = min(delay * (2 ** retries), 5)  # Cap at 5 seconds
                    logger.warning(f"Network error in {func.__name__}: {e}. Retrying ({retries}/{max_retries}) in {retry_delay} seconds...")
                    time.sleep(retry_delay)
        return wrapper
    return decorator


class PersonalDetailsScraper:
    """
    A class to scrape personal details data from the college website.
    """

    def __init__(self, username: str = USERNAME, password: str = PASSWORD,
                 base_dir: str = DEFAULT_SETTINGS['data_dir'],
                 headless: bool = DEFAULT_SETTINGS['headless'],
                 max_retries: int = DEFAULT_SETTINGS['max_retries'],
                 timeout: int = DEFAULT_SETTINGS['timeout'],
                 save_debug: bool = False,
                 academic_year: str = None,
                 year_of_study: str = None,
                 branch: str = None,
                 section: str = None):
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
            academic_year: Academic year (e.g., "2023-24")
            year_of_study: Year of study (e.g., "Third Yr - First Sem")
            branch: Branch (e.g., "CSE")
            section: Section (e.g., "A")
        """
        self.username = username
        self.password = password
        self.base_dir = Path(base_dir)
        self.headless = headless
        self.max_retries = max_retries
        self.timeout = timeout
        self.save_debug = save_debug
        self.academic_year = academic_year
        self.year_of_study = year_of_study
        self.branch = branch
        self.section = section
        self.logged_in = False
        self.session = create_session()
        self.driver = None

        # Initialize Selenium if available
        if SELENIUM_AVAILABLE:
            try:
                logger.info(f"Initializing personal details scraper in {'headless' if headless else 'visible'} mode")
                chrome_options = Options()
                if headless:
                    chrome_options.add_argument("--headless")
                chrome_options.add_argument("--window-size=1920,1080")
                chrome_options.add_argument("--disable-gpu")
                chrome_options.add_argument("--no-sandbox")
                chrome_options.add_argument("--disable-dev-shm-usage")

                # Initialize Chrome WebDriver
                try:
                    # Use webdriver_manager to get the correct driver and AVOID system ChromeDriver
                    if WEBDRIVER_MANAGER_AVAILABLE:
                        try:
                            # Get the latest ChromeDriver
                            driver_path = ChromeDriverManager().install()
                            service = Service(driver_path)

                            # Set the service path explicitly to avoid using system ChromeDriver
                            self.driver = webdriver.Chrome(service=service, options=chrome_options)
                            logger.info(f"Initialized Chrome WebDriver using webdriver_manager with path: {driver_path}")
                        except Exception as e:
                            logger.error(f"Failed to initialize Chrome WebDriver with webdriver_manager: {e}")
                            raise
                    else:
                        # If webdriver_manager is not available, try the default approach
                        try:
                            # Try to use Selenium's built-in manager
                            self.driver = webdriver.Chrome(options=chrome_options)
                            logger.info("Initialized Chrome WebDriver using default approach")
                        except Exception as e:
                            logger.error(f"Failed to initialize Chrome WebDriver: {e}")
                            raise
                except Exception as e:
                    logger.error(f"All Chrome initialization methods failed: {e}")
                    raise

                self.driver.implicitly_wait(self.timeout)
            except Exception as e:
                logger.error(f"Error initializing Chrome WebDriver: {e}")
                self.driver = None
        else:
            logger.warning("Selenium is not available. Using requests-based scraping only.")

    def __del__(self):
        """Clean up resources when the object is destroyed."""
        self.close()

    def close(self):
        """Close the browser and clean up resources."""
        if self.driver:
            try:
                self.driver.quit()
                logger.debug("Chrome WebDriver closed")
            except Exception as e:
                logger.error(f"Error closing Chrome WebDriver: {e}")
            finally:
                self.driver = None

    @retry_on_network_error(max_retries=1)  # Reduced retries for network errors
    def authenticate(self, retry_count=0) -> bool:
        """
        Authenticate with the college portal.

        Args:
            retry_count: Current retry count (used internally for recursion)

        Returns:
            Boolean indicating success
        """
        if self.logged_in:
            # Verify that the session is still valid
            try:
                if self.driver:
                    # Check if we're still logged in by looking for a login form
                    current_url = self.driver.current_url
                    if "login" not in current_url.lower():
                        return True
                else:
                    # Use requests to check if we're still logged in
                    response = self.session.get(PERSONAL_DETAILS_URL, allow_redirects=False)
                    if response.status_code == 200 and "login" not in response.url.lower():
                        return True
            except Exception as e:
                logger.warning(f"Error verifying session: {e}. Re-authenticating...")
                self.logged_in = False

        # Try to authenticate using Selenium if available
        if self.driver:
            try:
                logger.info(f"Authenticating using Selenium (attempt {retry_count + 1}/{min(2, self.max_retries) + 1})...")
                # Go directly to the personal details URL which will redirect to login page
                self.driver.get(PERSONAL_DETAILS_URL)

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
                    # Check for error messages that indicate invalid credentials
                    page_source = self.driver.page_source.lower()
                    if "invalid" in page_source or "incorrect" in page_source or "failed" in page_source:
                        logger.error(f"Login failed: Invalid credentials for username '{self.username}'. Please check your username and password.")
                        # Don't retry for invalid credentials
                        return False

                    logger.error("Login failed using Selenium")

                    # Limit retries for authentication failures to 2 at most
                    max_auth_retries = min(2, self.max_retries)
                    if retry_count < max_auth_retries:
                        retry_delay = min(2 ** retry_count, 10)  # Reduced max delay to 10 seconds
                        logger.info(f"Retrying authentication in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        return self.authenticate(retry_count + 1)
                    return False

            except Exception as e:
                logger.error(f"Error authenticating using Selenium: {e}")
                # Fall back to requests-based authentication

        # Use requests-based authentication as fallback
        logger.info(f"Authenticating using requests (attempt {retry_count + 1}/{min(2, self.max_retries) + 1})...")

        # First, log in to the main portal
        success, error_msg = login(self.session, self.username, self.password)
        if not success:
            # Check for error messages that indicate invalid credentials
            if "invalid" in error_msg.lower() or "incorrect" in error_msg.lower() or "failed" in error_msg.lower():
                logger.error(f"Main portal authentication failed: Invalid credentials for username '{self.username}'. Please check your username and password.")
                # Don't retry for invalid credentials
                return False

            logger.error(f"Main portal authentication failed: {error_msg}")

            # Limit retries for authentication failures to 2 at most
            max_auth_retries = min(2, self.max_retries)
            if retry_count < max_auth_retries:
                retry_delay = min(2 ** retry_count, 10)  # Reduced max delay to 10 seconds
                logger.info(f"Retrying authentication in {retry_delay} seconds...")
                time.sleep(retry_delay)
                return self.authenticate(retry_count + 1)
            return False

        # Then, log in to the attendance portal (which is required for personal details)
        success, error_msg = login_to_attendance(self.session, self.username, self.password)
        if success:
            self.logged_in = True
            return True
        else:
            # Check for error messages that indicate invalid credentials
            if "invalid" in error_msg.lower() or "incorrect" in error_msg.lower() or "failed" in error_msg.lower():
                logger.error(f"Attendance portal authentication failed: Invalid credentials for username '{self.username}'. Please check your username and password.")
                # Don't retry for invalid credentials
                return False

            logger.error(f"Attendance portal authentication failed: {error_msg}")

            # Limit retries for authentication failures to 2 at most
            max_auth_retries = min(2, self.max_retries)
            if retry_count < max_auth_retries:
                retry_delay = min(2 ** retry_count, 10)  # Reduced max delay to 10 seconds
                logger.info(f"Retrying authentication in {retry_delay} seconds...")
                time.sleep(retry_delay)
                return self.authenticate(retry_count + 1)
            return False

    @retry_on_network_error()
    def navigate_to_personal_details_page(self) -> Optional[BeautifulSoup]:
        """
        Navigate to the personal details page.

        Returns:
            BeautifulSoup object of the personal details page or None if failed
        """
        if not self.logged_in and not self.authenticate():
            return None

        try:
            # Use Selenium if available
            if self.driver:
                logger.info(f"Navigating to personal details page using Selenium: {PERSONAL_DETAILS_URL}")
                self.driver.get(PERSONAL_DETAILS_URL)

                # Wait for the page to load (look for form elements)
                WebDriverWait(self.driver, self.timeout).until(
                    EC.presence_of_element_located((By.TAG_NAME, "form"))
                )

                # Check if we're on the correct page
                if "selectionForRollNos.php" in self.driver.current_url:
                    logger.info("Successfully navigated to personal details page using Selenium")
                    # Get the page source and parse it with BeautifulSoup
                    page_source = self.driver.page_source
                    return BeautifulSoup(page_source, 'html.parser')
                else:
                    logger.warning(f"Navigation to personal details page failed - redirected to {self.driver.current_url}")
                    return None
            else:
                # Use requests as fallback
                logger.info(f"Navigating to personal details page using requests: {PERSONAL_DETAILS_URL}")
                response = self.session.get(PERSONAL_DETAILS_URL)
                response.raise_for_status()

                # Parse the HTML content
                soup = BeautifulSoup(response.text, 'html.parser')

                # Check if we're on the correct page by looking for specific elements
                # that would indicate we're on the personal details page
                form_elements = soup.select('form')
                select_elements = soup.select('select')

                # If we find form elements and select dropdowns, we're likely on the right page
                if form_elements and select_elements:
                    logger.info("Successfully navigated to personal details page using requests")
                    return soup
                else:
                    # Get the URL we were redirected to
                    current_url = response.url
                    logger.warning(f"Navigation to personal details page failed - redirected to {current_url}")
                    # Log the page content for debugging
                    if self.save_debug:
                        logger.debug(f"Page content: {response.text[:500]}...")
                    return None

        except Exception as e:
            logger.error(f"Error navigating to personal details page: {e}")
            return None

    def select_class_or_student(self, class_id: Optional[str] = None, student_id: Optional[str] = None) -> Optional[BeautifulSoup]:
        """
        Select class or specific student for personal details.

        Args:
            class_id: ID of the class to select (optional)
            student_id: ID of the specific student to select (optional)

        Returns:
            BeautifulSoup object of the results page or None if failed
        """
        soup = self.navigate_to_personal_details_page()
        if not soup:
            return None

        try:
            # This is a placeholder - you'll need to adjust based on actual form
            form_data = {
                'submit': 'Submit'
            }

            if class_id:
                form_data['class_id'] = class_id
                logger.info(f"Submitting form with class_id={class_id}")

            if student_id:
                form_data['student_id'] = student_id
                logger.info(f"Submitting form with student_id={student_id}")

            response = self.session.post(PERSONAL_DETAILS_URL, data=form_data)
            response.raise_for_status()

            # Parse the HTML content
            result_soup = BeautifulSoup(response.text, 'html.parser')
            return result_soup

        except requests.exceptions.RequestException as e:
            logger.error(f"Error submitting form: {e}")
            return None

    @retry_on_network_error()
    def select_form_filters(self, academic_year: str, year_of_study: str, branch: str, section: str) -> Optional[BeautifulSoup]:
        """
        Select form filters for personal details.

        Args:
            academic_year: Academic year (e.g., "2023-24")
            year_of_study: Year of study (e.g., "First Yr - First Sem")
            branch: Branch (e.g., "CSE")
            section: Section (e.g., "A")

        Returns:
            BeautifulSoup object of the result page or None if failed
        """
        # Navigate to the personal details page first
        soup = self.navigate_to_personal_details_page()
        if not soup:
            return None

        try:
            # Use Selenium if available
            if self.driver:
                logger.info(f"Selecting form filters using Selenium: {academic_year}, {year_of_study}, {branch}, {section}")

                # Select academic year
                academic_year_select = Select(self.driver.find_element(By.NAME, "acadYear"))
                academic_year_select.select_by_visible_text(academic_year)
                time.sleep(0.5)  # Wait for the page to update

                # Select year of study
                year_select = Select(self.driver.find_element(By.NAME, "yearSem"))

                # Map the year_of_study to the correct value
                year_sem_mapping = {
                    "First": "01",
                    "First Yr - First Sem": "11",
                    "First Yr - Second Sem": "12",
                    "Second Yr - First Sem": "21",
                    "Second Yr - Second Sem": "22",
                    "Third Yr - First Sem": "31",
                    "Third Yr - Second Sem": "32",
                    "Final Yr - First Sem": "41",
                    "Final Yr - Second Sem": "42"
                }

                # Use the value if it's in the mapping, otherwise use the visible text
                if year_of_study in year_sem_mapping:
                    year_select.select_by_value(year_sem_mapping[year_of_study])
                else:
                    year_select.select_by_visible_text(year_of_study)

                time.sleep(0.5)  # Wait for the page to update

                # Select branch
                branch_select = Select(self.driver.find_element(By.NAME, "branch"))

                # Map the branch to the correct value
                branch_mapping = {
                    "MECH": "7",
                    "CSE": "5",
                    "ECE": "4",
                    "EEE": "2",
                    "CIVIL": "11",
                    "IT": "22",
                    "AI_DS": "23",
                    "CSE_DS": "32",
                    "CSE_AIML": "33"
                }

                # Excluded branches (no longer useful)
                excluded_branches = ["MTech_PS", "MTech_CSE", "MTech_ECE", "MTech_AMS"]

                # Check if the branch is excluded
                if branch in excluded_branches:
                    logger.warning(f"Branch {branch} is excluded from scraping")
                    return None

                # Use the value if it's in the mapping, otherwise use the visible text
                if branch in branch_mapping:
                    branch_select.select_by_value(branch_mapping[branch])
                else:
                    branch_select.select_by_visible_text(branch)

                time.sleep(0.5)  # Wait for the page to update

                # Select section
                section_select = Select(self.driver.find_element(By.NAME, "section"))

                # Try to find an option with text matching the section
                option_found = False
                for option in section_select.options:
                    option_text = option.text.strip()
                    if section == option_text or section == option.get_attribute('value'):
                        section_select.select_by_visible_text(option_text)
                        option_found = True
                        logger.info(f"Selected section: {option_text}")
                        break

                if not option_found:
                    # If no match found, select the first option
                    section_select.select_by_index(0)
                    logger.warning(f"Could not find section '{section}', selected first option: {section_select.first_selected_option.text}")

                time.sleep(1.0)  # Increased wait time for the page to update

                # Important: Select additional checkboxes required for personal details
                # These are the checkboxes that must be selected before clicking the "Get List of RollNos" button
                try:
                    # Find all checkboxes on the page
                    all_checkboxes = self.driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
                    logger.debug(f"Found {len(all_checkboxes)} checkboxes on the page")

                    # Try to select all checkboxes - this ensures we get all available data
                    selected_count = 0
                    for checkbox in all_checkboxes:
                        try:
                            if not checkbox.is_selected():
                                # Try JavaScript click first (more reliable)
                                try:
                                    self.driver.execute_script("arguments[0].scrollIntoView(true);", checkbox)
                                    time.sleep(0.2)  # Wait for scroll
                                    self.driver.execute_script("arguments[0].click();", checkbox)
                                except Exception as js_e:
                                    logger.debug(f"JavaScript click failed: {js_e}, trying regular click")
                                    checkbox.click()

                                time.sleep(0.3)  # Increased delay between clicks
                                selected_count += 1
                        except Exception as e:
                            logger.debug(f"Checkbox could not be selected: {e}")

                    logger.info(f"Selected {selected_count} additional checkboxes")

                    # If no checkboxes were found or selected, try specific selectors as fallback
                    if selected_count == 0:
                        # Find the specific checkboxes by name and value
                        checkbox_selectors = [
                            "input[name='chkOterhFields[]'][value='Parent Name~fathersname']",
                            "input[name='chkOterhFields[]'][value='Parent Mobile~mobile']",
                            "input[name='chkOterhFields[]'][value='Student Mobile~studentMobile']",
                            "input[name='chkOterhFields[]'][value='Aadhaar~aadhaar']"
                        ]

                        for selector in checkbox_selectors:
                            try:
                                checkbox = self.driver.find_element(By.CSS_SELECTOR, selector)
                                if not checkbox.is_selected():
                                    # Try JavaScript click first
                                    try:
                                        self.driver.execute_script("arguments[0].scrollIntoView(true);", checkbox)
                                        time.sleep(0.2)  # Wait for scroll
                                        self.driver.execute_script("arguments[0].click();", checkbox)
                                    except Exception as js_e:
                                        logger.debug(f"JavaScript click failed: {js_e}, trying regular click")
                                        checkbox.click()

                                    time.sleep(0.3)  # Increased delay between clicks
                                    selected_count += 1
                            except Exception as e:
                                logger.debug(f"Checkbox not found or could not be selected: {selector}")

                        logger.info(f"Selected {selected_count} additional checkboxes using specific selectors")
                except Exception as e:
                    logger.warning(f"Error selecting checkboxes: {e}")

                # Take a screenshot for debugging if enabled
                if self.save_debug:
                    debug_dir = Path("debug_output")
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    screenshot_path = debug_dir / f"{academic_year}_{year_of_study}_{branch}_{section}_before_click.png"
                    self.driver.save_screenshot(str(screenshot_path))
                    logger.debug(f"Saved screenshot before clicking button to {screenshot_path}")

                # Find and click the submit button with multiple strategies
                submit_button = None

                # Strategy 1: Look for the exact submit button
                try:
                    submit_button = self.driver.find_element(By.CSS_SELECTOR, "input[type='submit'][value='Get List of RollNos']")
                    logger.info("Found submit button using exact CSS selector")
                except Exception as e:
                    logger.debug(f"Could not find submit button using exact CSS selector: {e}")

                # Strategy 2: Look for any submit button
                if not submit_button:
                    try:
                        submit_buttons = self.driver.find_elements(By.CSS_SELECTOR, "input[type='submit']")
                        if submit_buttons:
                            submit_button = submit_buttons[0]
                            logger.info(f"Found submit button with value: {submit_button.get_attribute('value')}")
                    except Exception as e:
                        logger.debug(f"Could not find any submit buttons: {e}")

                # Strategy 3: Look for any button with 'roll' or 'get' in its value
                if not submit_button:
                    try:
                        buttons = self.driver.find_elements(By.TAG_NAME, "input")
                        for button in buttons:
                            button_type = button.get_attribute("type")
                            button_value = button.get_attribute("value") or ""
                            if button_type in ["submit", "button"] and ("roll" in button_value.lower() or "get" in button_value.lower()):
                                submit_button = button
                                logger.info(f"Found button with value: {button_value}")
                                break
                    except Exception as e:
                        logger.debug(f"Error searching for buttons by tag name: {e}")

                if submit_button:
                    # Scroll to the button to make sure it's visible
                    self.driver.execute_script("arguments[0].scrollIntoView(true);", submit_button)
                    time.sleep(1)  # Wait for scroll to complete

                    # Try JavaScript click first (more reliable)
                    try:
                        self.driver.execute_script("arguments[0].click();", submit_button)
                        logger.info("Clicked submit button using JavaScript")
                    except Exception as js_e:
                        logger.warning(f"JavaScript click failed: {js_e}, trying regular click")
                        submit_button.click()
                        logger.info("Clicked submit button using regular click")
                else:
                    logger.error("Could not find any submit button")
                    return None

                # Wait for the page to load with increased timeout
                try:
                    # First, wait for any tables to appear
                    WebDriverWait(self.driver, self.timeout * 2).until(
                        EC.presence_of_element_located((By.TAG_NAME, "table"))
                    )

                    # Then wait a bit more for the data to fully load
                    time.sleep(3)  # Increased wait time after form submission

                    # Take a screenshot for debugging if enabled
                    if self.save_debug:
                        debug_dir = Path("debug_output")
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        screenshot_path = debug_dir / f"{academic_year}_{year_of_study}_{branch}_{section}_after_click.png"
                        self.driver.save_screenshot(str(screenshot_path))
                        logger.debug(f"Saved screenshot after clicking button to {screenshot_path}")

                        # Also save the HTML content
                        html_path = debug_dir / f"{academic_year}_{year_of_study}_{branch}_{section}_after_click.html"
                        with open(html_path, 'w', encoding='utf-8') as f:
                            f.write(self.driver.page_source)
                        logger.debug(f"Saved HTML content after clicking to {html_path}")

                    # Get the page source and parse it with BeautifulSoup
                    page_source = self.driver.page_source

                    # Check if we got results
                    if 'No Records Found' in page_source:
                        logger.warning(f"No records found for {academic_year}, {year_of_study}, {branch}, {section}")
                        return None

                    # Parse the HTML content
                    result_soup = BeautifulSoup(page_source, 'html.parser')

                    # Check if we have a table with student data
                    tables = result_soup.select('table')
                    if tables and len(tables) > 0:
                        # Check if any table has more than 1 row (header + at least one data row)
                        has_data = False
                        for table in tables:
                            rows = table.select('tr')
                            if len(rows) > 1:
                                has_data = True
                                break

                        if has_data:
                            logger.info("Form submitted successfully using Selenium - found tables with data")
                            return result_soup
                        else:
                            logger.warning("Form submitted but no data rows found in tables")
                            return None
                    else:
                        logger.warning("Form submitted but no tables found in the response")
                        return None
                except Exception as e:
                    logger.error(f"Error waiting for page to load after form submission: {e}")
                    return None
            else:
                # Use requests as fallback
                logger.info(f"Selecting form filters using requests: {academic_year}, {year_of_study}, {branch}, {section}")

                # Extract form action URL
                form = soup.select_one('form')
                if not form:
                    logger.error("No form found on the page")
                    return None

                form_action = form.get('action', '')
                form_url = PERSONAL_DETAILS_URL if not form_action else form_action

                # Find all checkboxes
                checkboxes = soup.select('input[type="checkbox"]')
                checkbox_names = [checkbox.get('name') for checkbox in checkboxes if checkbox.get('name')]

                # Prepare form data
                form_data = {
                    'acadYear': academic_year,
                    'yearSem': year_of_study,
                    'branch': branch,
                    'section': section,
                }

                # Add all checkboxes to the form data
                for checkbox_name in checkbox_names:
                    form_data[checkbox_name] = 'on'

                # Add the submit button value
                form_data['submit'] = 'Get List of RollNos'

                # Submit the form
                response = self.session.post(form_url, data=form_data)
                response.raise_for_status()

                # Parse the response
                result_soup = BeautifulSoup(response.text, 'html.parser')

                # Check if the form submission was successful by looking for tables
                tables = result_soup.select('table')
                if tables:
                    logger.info("Form submitted successfully using requests")
                    return result_soup
                else:
                    logger.warning("Form submission failed - no tables found in the response")
                    if self.save_debug:
                        logger.debug(f"Response content: {response.text[:500]}...")
                    return None

        except Exception as e:
            logger.error(f"Error submitting form: {e}")
            return None

    def extract_personal_details(self, soup: BeautifulSoup) -> List[Dict[str, Any]]:
        """
        Extract personal details from the page.

        Args:
            soup: BeautifulSoup object of the page

        Returns:
            List of dictionaries containing personal details
        """
        if not soup:
            logger.error("No soup provided for extraction")
            return []

        try:
            # Find all tables in the page
            tables = soup.select('table')
            if not tables:
                logger.warning("No tables found in the page")
                return []

            # Debug: Print information about each table
            logger.debug(f"Found {len(tables)} tables on the page")
            for i, table in enumerate(tables):
                rows = table.select('tr')
                logger.debug(f"Table {i+1} has {len(rows)} rows")
                if rows:
                    cells = rows[0].select('td, th')
                    logger.debug(f"First row has {len(cells)} cells")
                    if cells:
                        logger.debug(f"First cell text: {cells[0].get_text(strip=True)}")

                # Check for links in the table
                links = table.select('a')
                logger.debug(f"Table {i+1} has {len(links)} links")
                if links:
                    logger.debug(f"First link text: {links[0].get_text(strip=True)}")

            # The personal details page can have different structures
            # 1. It might have a table with roll numbers as links, and clicking on each link shows the details
            # 2. It might have a table with student details directly in the rows

            # First, try to find the main student details table
            # This is typically the largest table with student data
            student_table = None
            max_rows = 0

            for table in tables:
                rows = table.select('tr')
                if len(rows) > max_rows:
                    # Check if this looks like a student table (has roll numbers)
                    if len(rows) > 1:  # At least a header row and one data row
                        cells = rows[1].select('td')  # Check the first data row
                        if cells and len(cells) > 1:
                            # Check if any cell contains text that looks like a roll number
                            # Roll numbers typically contain digits and possibly letters
                            for cell in cells:
                                text = cell.get_text(strip=True)
                                if text and any(c.isdigit() for c in text):
                                    student_table = table
                                    max_rows = len(rows)
                                    break

            if not student_table:
                # Try a different approach - look for tables with specific headers
                for table in tables:
                    rows = table.select('tr')
                    if rows:
                        header_row = rows[0]
                        header_cells = header_row.select('th, td')
                        header_texts = [cell.get_text(strip=True).lower() for cell in header_cells]

                        # Check for common headers in student tables
                        if any(text in ['roll no', 'rollno', 'roll number', 'student id'] for text in header_texts):
                            student_table = table
                            break

            if not student_table:
                logger.warning("No student details table found")
                return []

            # Extract student details from the table
            students = []
            rows = student_table.select('tr')

            # Check if the first row contains actual headers or if it's the first data row
            header_row = rows[0]
            header_cells = header_row.select('th, td')
            header_texts = [cell.get_text(strip=True) for cell in header_cells]
            logger.debug(f"Found table with first row texts: {header_texts}")

            # Check if the first row looks like a header row or a data row
            # If it contains roll numbers (digits), it's likely a data row
            is_data_row = any(any(c.isdigit() for c in text) for text in header_texts)

            # If the first row is a data row, use default headers
            if is_data_row:
                logger.debug("First row appears to be a data row, using default headers")
                # Use default headers based on common structure
                standardized_headers = ['S.No', 'Roll No', 'Name', 'Father Name', 'Parent Mobile', 'Student Mobile', 'Aadhaar']
                # Adjust the number of headers to match the number of columns
                if len(header_cells) > len(standardized_headers):
                    # Add additional columns if needed
                    for i in range(len(standardized_headers), len(header_cells)):
                        standardized_headers.append(f"Column_{i+1}")
                elif len(header_cells) < len(standardized_headers):
                    # Trim headers if needed
                    standardized_headers = standardized_headers[:len(header_cells)]

                # Start processing from the first row (don't skip)
                start_row = 0
            else:
                # Clean up headers - remove any empty headers and standardize names
                cleaned_headers = []
                for header in header_texts:
                    if not header.strip():
                        # Use a placeholder for empty headers
                        cleaned_headers.append(f"Column_{len(cleaned_headers)+1}")
                    else:
                        cleaned_headers.append(header)

                # Map common header variations to standard names
                header_mapping = {
                    'sno': 'S.No',
                    's.no': 'S.No',
                    'sl.no': 'S.No',
                    'slno': 'S.No',
                    'roll no': 'Roll No',
                    'rollno': 'Roll No',
                    'roll number': 'Roll No',
                    'student id': 'Roll No',
                    'name of the student': 'Name',
                    'student name': 'Name',
                    'father name': 'Father Name',
                    'father\'s name': 'Father Name',
                    'parent name': 'Father Name',
                    'phone': 'Phone',
                    'phone no': 'Phone',
                    'parent mobile': 'Parent Mobile',
                    'parent phone': 'Parent Mobile',
                    'student mobile': 'Student Mobile',
                    'mobile': 'Mobile',
                    'mobile no': 'Mobile',
                    'email': 'Email',
                    'email id': 'Email',
                    'aadhaar': 'Aadhaar',
                    'aadhar': 'Aadhaar',
                    'aadhaar no': 'Aadhaar'
                }

                # Standardize headers
                standardized_headers = []
                for header in cleaned_headers:
                    lower_header = header.lower()
                    if lower_header in header_mapping:
                        standardized_headers.append(header_mapping[lower_header])
                    else:
                        standardized_headers.append(header)

                # Start processing from the second row (skip header)
                start_row = 1

            logger.debug(f"Using standardized headers: {standardized_headers}")

            # Process each student row, starting from the appropriate row
            for row in rows[start_row:]:
                cells = row.select('td')
                if not cells or len(cells) < 2:  # Skip rows with too few cells
                    continue

                # Extract data from each cell
                student_data = {}
                for i, cell in enumerate(cells):
                    if i < len(standardized_headers):
                        header = standardized_headers[i]
                        value = cell.get_text(strip=True)
                        if header:  # Always add the header
                            student_data[header] = value if value else None

                # Skip rows that don't have a roll number
                if 'Roll No' not in student_data or not student_data['Roll No']:
                    # Try to find a roll number pattern in any field
                    roll_number = None
                    for _, value in student_data.items():  # Use _ to indicate unused variable
                        # Look for patterns like 21KB1A0501 (typical roll number format)
                        if re.search(r'\d{2}[A-Z]{2}\d{1}[A-Z]\d{4}', value):
                            roll_number = value
                            student_data['Roll No'] = roll_number
                            break

                    if not roll_number:
                        continue

                # Clean up roll number if it has a date in parentheses
                if 'Roll No' in student_data and student_data['Roll No']:
                    roll_number = student_data['Roll No']
                    if '(' in roll_number and ')' in roll_number:
                        # Extract the part before the opening parenthesis
                        roll_number = roll_number.split('(')[0].strip()
                        student_data['Roll No'] = roll_number

                # Add additional metadata
                student_data['extracted_at'] = datetime.now().isoformat()

                # Ensure all important fields are present, even if null
                important_fields = ['Roll No', 'Name', 'Father Name', 'Parent Mobile', 'Student Mobile', 'Aadhaar']
                for field in important_fields:
                    if field not in student_data:
                        student_data[field] = None

                # Add the student data to the list
                students.append(student_data)

                # Debug output for the first few students
                if len(students) <= 3:
                    logger.debug(f"Extracted student data: {student_data}")

            if not students:
                logger.warning("No student details found in the table")
                return []

            logger.info(f"Extracted personal details for {len(students)} students")
            return students

        except Exception as e:
            logger.error(f"Error extracting personal details: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return []

    def save_to_csv(self, data: List[Dict[str, Any]], filename: str = "personal_details_data.csv",
                   academic_year: str = None, year_of_study: str = None) -> bool:
        """
        Save the personal details data to a CSV file in a structured folder system.

        Args:
            data: List of dictionaries containing personal details
            filename: Name of the output CSV file
            academic_year: Academic year (e.g., "2023-24")
            year_of_study: Year of study (e.g., "Third Yr - First Sem")

        Returns:
            Boolean indicating success
        """
        if not data:
            logger.warning("No personal details data to save")
            return False

        try:
            # Create a structured folder path for CSV files
            csv_base_dir = Path("csv_details")

            # If academic_year and year_of_study are provided, use them for folder structure
            if academic_year and year_of_study:
                # Convert semester to year_of_study format for folder structure
                year_of_study_folder = self.convert_semester_to_year_of_study(year_of_study)
                csv_folder = csv_base_dir / academic_year / year_of_study_folder
            else:
                # Otherwise, just use the base directory
                csv_folder = csv_base_dir

            # Create the folder if it doesn't exist
            csv_folder.mkdir(parents=True, exist_ok=True)

            # Full path to the CSV file
            csv_path = csv_folder / filename

            if PANDAS_AVAILABLE:
                # Use pandas for better CSV handling
                df = pd.DataFrame(data)
                df.to_csv(csv_path, index=False)
                logger.info(f"Saved {len(data)} personal details records to {csv_path} using pandas")
            else:
                # Fallback to built-in csv module
                import csv

                # Get all unique keys from all dictionaries
                fieldnames = set()
                for item in data:
                    fieldnames.update(item.keys())
                fieldnames = sorted(list(fieldnames))

                with open(csv_path, 'w', newline='') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(data)

                logger.info(f"Saved {len(data)} personal details records to {csv_path} using csv module")

            return True
        except Exception as e:
            logger.error(f"Error saving data to CSV: {e}")
            return False



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

    def store_personal_details_data(self, personal_details_data: List[Dict[str, Any]], force_update: bool = False) -> Tuple[int, int]:
        """
        Store personal details data in a structured folder system.

        Args:
            personal_details_data: List of dictionaries containing personal details data
            force_update: Whether to force update even if data already exists

        Returns:
            Tuple of (success_count, update_count)
        """
        success_count = 0
        update_count = 0

        # Check if we have valid data to store
        if not personal_details_data:
            logger.warning("No personal details data to store")
            return success_count, update_count

        # Process each student
        for student in personal_details_data:
            try:
                # Extract student information
                roll_number = student.get('Roll No', '')
                if not roll_number:
                    logger.warning(f"No roll number found for student: {student}")
                    continue

                # Extract other metadata
                academic_year = student.get('academic_year', self.academic_year)
                year_of_study = student.get('year_of_study', self.year_of_study)
                branch = student.get('branch', self.branch)
                section = student.get('section', self.section)

                # Create a structured data dictionary similar to attendance_scraper.py
                personal_details_data = {
                    'roll_number': roll_number,
                    'data_type': 'personal_details',
                    'academic_year': academic_year,
                    'year_of_study': year_of_study,
                    'branch': branch,
                    'section': section,
                    'data': {}
                }

                # Extract personal details into the data field
                for key, value in student.items():
                    if key not in ['academic_year', 'year_of_study', 'branch', 'section']:
                        personal_details_data['data'][key] = value

                # Convert semester to year_of_study format for folder structure
                year_of_study_folder = self.convert_semester_to_year_of_study(year_of_study)

                # Create folder structure (without branch and section folders)
                student_folder = self.base_dir / academic_year / year_of_study_folder / roll_number
                student_folder.mkdir(parents=True, exist_ok=True)

                # Save personal details data
                details_file = student_folder / "personal_details.json"

                # Save branch and section information in roll_number.json file
                self.store_student_info(student_folder, roll_number, branch, section)

                # Check if file exists and compare data
                should_update = True
                if details_file.exists() and not force_update:
                    try:
                        with open(details_file, 'r') as f:
                            existing_data = json.load(f)

                        # Simple comparison - if data is the same, don't update
                        if existing_data == personal_details_data['data']:
                            should_update = False
                        else:
                            # Log what changed
                            changes = []
                            for key in set(personal_details_data['data'].keys()) | set(existing_data.keys()):
                                if key not in existing_data:
                                    changes.append(f"Added {key}: {personal_details_data['data'][key]}")
                                elif key not in personal_details_data['data']:
                                    changes.append(f"Removed {key}")
                                elif existing_data[key] != personal_details_data['data'][key]:
                                    changes.append(f"Changed {key}: {existing_data[key]} -> {personal_details_data['data'][key]}")

                            if changes:
                                logger.debug(f"Changes for {roll_number}: {', '.join(changes[:3])}" +
                                             (f" and {len(changes) - 3} more" if len(changes) > 3 else ""))
                    except Exception as e:
                        logger.warning(f"Error reading existing data for {roll_number}: {e}")
                        should_update = True

                if should_update:
                    with open(details_file, 'w') as f:
                        json.dump(personal_details_data['data'], f, indent=2)

                    # No need to update roll index as we now store this info in the student folder

                    logger.info(f"Stored personal details for {roll_number} in {details_file}")
                    update_count += 1
                else:
                    logger.debug(f"No changes detected for student {roll_number}, skipping update")

                success_count += 1

            except Exception as e:
                logger.error(f"Error storing personal details for student {student.get('Roll No', 'unknown')}: {str(e)}")

        return success_count, update_count


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
    worker_logger.setLevel(logging.DEBUG if args.debug else logging.INFO)

    # Initialize the scraper with command line credentials or defaults from config
    username = args.username if args.username else USERNAME
    password = args.password if args.password else PASSWORD
    headless = args.headless if args.headless is not None else DEFAULT_SETTINGS['headless']
    save_debug = args.debug

    # Use max_retries and timeout from command line if provided
    max_retries = args.max_retries if args.max_retries is not None else DEFAULT_SETTINGS['max_retries']
    timeout = args.timeout if args.timeout is not None else DEFAULT_SETTINGS['timeout']

    # Create the scraper with all settings
    scraper = PersonalDetailsScraper(
        username=username,
        password=password,
        base_dir=args.data_dir,
        headless=headless,
        max_retries=max_retries,
        timeout=timeout,
        save_debug=save_debug,
        # Pass specific academic year and other parameters to ensure the scraper only processes the assigned combination
        academic_year=None,  # Will be set for each combination
        year_of_study=None,  # Will be set for each combination
        branch=None,         # Will be set for each combination
        section=None         # Will be set for each combination
    )

    # Authenticate
    if not scraper.authenticate():
        worker_logger.error(f"Worker {worker_id}: Authentication failed. Exiting.")
        result_queue.put((worker_id, "auth_failed", None))
        return

    # Navigate to personal details page
    soup = scraper.navigate_to_personal_details_page()
    if not soup:
        worker_logger.error(f"Worker {worker_id}: Failed to navigate to personal details page. Exiting.")
        result_queue.put((worker_id, "nav_failed", None))
        return

    worker_logger.info(f"Worker {worker_id}: Ready to process combinations")

    # Process combinations from the queue
    combinations_processed = 0
    combinations_with_data = 0
    total_students = 0

    while True:
        try:
            # Get the next combination from the queue (non-blocking)
            try:
                combination_index, combination = combination_queue.get(block=False)
                academic_year, year_of_study, branch, section = combination
            except queue.Empty:
                # No more combinations to process
                worker_logger.info(f"Worker {worker_id}: No more combinations to process. Exiting.")
                break

            worker_logger.info(f"Worker {worker_id}: Processing combination {combination_index}: {academic_year}, {year_of_study}, {branch}, {section}")
            combinations_processed += 1

            # Update scraper with the current combination parameters
            scraper.academic_year = academic_year
            scraper.year_of_study = year_of_study
            scraper.branch = branch
            scraper.section = section

            # Add delay between requests if specified
            if combinations_processed > 1 and args.delay > 0:
                worker_logger.debug(f"Worker {worker_id}: Sleeping for {args.delay} seconds...")
                time.sleep(args.delay)

            # Skip excluded branches
            excluded_branches = ["MTech_PS", "MTech_CSE", "MTech_ECE", "MTech_AMS"]
            if branch in excluded_branches:
                worker_logger.warning(f"Worker {worker_id}: Branch {branch} is excluded from scraping. Skipping.")
                result_queue.put((worker_id, "excluded_branch", combination))
                # Skip task_done for process mode as multiprocessing.Queue doesn't have this method
                if hasattr(combination_queue, 'task_done'):
                    combination_queue.task_done()
                continue

            # Select form filters and submit
            result_soup = scraper.select_form_filters(academic_year, year_of_study, branch, section)
            if not result_soup:
                worker_logger.warning(f"Worker {worker_id}: Failed to get results for {academic_year}, {year_of_study}, {branch}, {section}")
                result_queue.put((worker_id, "no_results", combination))
                # Skip task_done for process mode as multiprocessing.Queue doesn't have this method
                if hasattr(combination_queue, 'task_done'):
                    combination_queue.task_done()
                continue

            # Extract personal details
            students = scraper.extract_personal_details(result_soup)

            if not students:
                worker_logger.warning(f"Worker {worker_id}: No student details found for {academic_year}, {year_of_study}, {branch}, {section}")
                result_queue.put((worker_id, "no_data", combination))
                # Skip task_done for process mode as multiprocessing.Queue doesn't have this method
                if hasattr(combination_queue, 'task_done'):
                    combination_queue.task_done()
                continue

            # Add metadata to each student record
            for student in students:
                student['academic_year'] = academic_year
                student['year_of_study'] = year_of_study
                student['branch'] = branch
                student['section'] = section

            # Store data in structured format
            success_count, update_count = scraper.store_personal_details_data(students, args.force_update)
            worker_logger.info(f"Worker {worker_id}: Processed {success_count} students with {update_count} updates")
            total_students += len(students)
            combinations_with_data += 1

            # Also save to CSV if not disabled
            if not args.no_csv:
                # Create a filename for the CSV
                csv_filename = f"{branch}_{section}_personal_details.csv"

                # Save to CSV with structured folder path
                scraper.save_to_csv(students, csv_filename, academic_year, year_of_study)

            # Put the result in the result queue
            result_queue.put((worker_id, "success", (combination, len(students))))
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
        if hasattr(scraper, 'close'):
            scraper.close()
    except Exception as e:
        worker_logger.error(f"Worker {worker_id}: Error closing scraper: {str(e)}")

    worker_logger.info(f"Worker {worker_id}: Finished processing {combinations_processed} combinations with {combinations_with_data} containing data and {total_students} students")
    result_queue.put((worker_id, "finished", (combinations_processed, combinations_with_data, total_students)))


def main():
    """Main function to run the scraper."""
    parser = argparse.ArgumentParser(description='Scrape personal details from college website')
    parser.add_argument('--username', help='Login username (defaults to config.USERNAME)')
    parser.add_argument('--password', help='Login password (defaults to config.PASSWORD)')
    parser.add_argument('--output', default='personal_details_data.csv', help='Output file name')
    parser.add_argument('--academic-year', choices=DEFAULT_ACADEMIC_YEARS, help='Academic year')
    parser.add_argument('--year-of-study', dest='year_of_study', choices=DEFAULT_SEMESTERS, help='Year of study')
    parser.add_argument('--branch', choices=list(BRANCH_CODES.keys()), help='Branch')
    parser.add_argument('--section', choices=DEFAULT_SECTIONS, help='Section')
    parser.add_argument('--headless', action='store_true', help='Run in headless mode')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--data-dir', default=DEFAULT_SETTINGS['data_dir'], help='Data directory')
    parser.add_argument('--force-update', action='store_true', help='Force update even if data already exists')
    parser.add_argument('--no-csv', action='store_true', help='Disable CSV generation')

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

    # Add arguments that are passed by the web interface but not used by this script
    # This allows the script to be called with the same arguments as other scraper scripts
    parser.add_argument('--semester', help='Alias for year_of_study - for compatibility with web interface')
    parser.add_argument('--max-retries', type=int, help='Maximum number of retries for network errors')
    parser.add_argument('--timeout', type=int, help='Timeout in seconds for waiting for elements')

    args = parser.parse_args()

    # Set up logging level based on debug flag
    if args.debug:
        logger.setLevel(logging.DEBUG)

    # Handle semester argument as an alias for year_of_study
    if args.semester and not args.year_of_study:
        args.year_of_study = args.semester
        logger.info(f"Using semester '{args.semester}' as year_of_study")

    # Initialize the scraper with command line credentials or defaults from config
    username = args.username if args.username else USERNAME
    password = args.password if args.password else PASSWORD

    # Use max_retries and timeout from command line if provided
    max_retries = args.max_retries if args.max_retries is not None else DEFAULT_SETTINGS['max_retries']
    timeout = args.timeout if args.timeout is not None else DEFAULT_SETTINGS['timeout']

    scraper = PersonalDetailsScraper(
        username=username,
        password=password,
        base_dir=args.data_dir,
        headless=args.headless,
        max_retries=max_retries,
        timeout=timeout,
        save_debug=args.debug,
        academic_year=args.academic_year,
        year_of_study=args.year_of_study,
        branch=args.branch,
        section=args.section
    )

    # Authenticate
    if not scraper.authenticate():
        logger.error("Authentication failed. Please check your username and password.")
        logger.error(f"Username provided: '{username}'")
        # Don't log the actual password for security reasons
        logger.error("If you believe your credentials are correct, please check your network connection.")
        logger.error("Exiting with error code 2 (authentication failure).")
        sys.exit(2)

    # Navigate to personal details page
    soup = scraper.navigate_to_personal_details_page()
    if not soup:
        logger.error("Failed to navigate to personal details page. Exiting.")
        sys.exit(1)

    # Use the provided filters or try all combinations
    if args.academic_year and args.year_of_study and args.branch and args.section:
        # Use the provided filters
        combinations = [
            (args.academic_year, args.year_of_study, args.branch, args.section)
        ]
    else:
        # Determine which academic years to use
        academic_years = args.only_years if args.only_years else DEFAULT_ACADEMIC_YEARS
        if args.academic_year and args.academic_year not in academic_years:
            academic_years = [args.academic_year] + academic_years

        # Determine which years of study to use
        years_of_study = args.only_semesters if args.only_semesters else DEFAULT_SEMESTERS
        if args.year_of_study and args.year_of_study not in years_of_study:
            years_of_study = [args.year_of_study] + years_of_study

        # Determine which branches to use
        branches = args.only_branches if args.only_branches else DEFAULT_BRANCHES
        if args.branch and args.branch not in branches:
            branches = [args.branch] + branches

        # Exclude MTech branches
        excluded_branches = ["MTech_PS", "MTech_CSE", "MTech_ECE", "MTech_AMS"]
        branches = [b for b in branches if b not in excluded_branches]

        # Determine which sections to use
        sections = args.only_sections if args.only_sections else DEFAULT_SECTIONS
        if args.section and args.section not in sections:
            sections = [args.section] + sections

        # Create all combinations
        combinations = [
            (year, sem, branch, section)
            for year in academic_years
            for sem in years_of_study
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
                combinations_processed, combinations_with_data, students_found = data
                total_combinations_tried += combinations_processed
                logger.info(f"Worker {worker_id} finished processing {combinations_processed} combinations with {combinations_with_data} containing data and {students_found} students")

        # Print summary
        if total_students_found > 0:
            logger.info(f"Summary: Processed {total_students_found} students across {total_combinations_with_data} combinations")
        else:
            logger.warning("No student details found for any combination")

    else:
        # Use single-worker mode (original code)
        logger.info("Using single-worker mode")

        # Process each combination
        total_success = 0
        total_updates = 0
        total_students = 0
        empty_combinations_in_a_row = 0
        max_empty_combinations = 10  # Stop after this many empty combinations in a row

        for i, (academic_year, year_of_study, branch, section) in enumerate(combinations, 1):
            logger.info(f"Processing combination {i}/{len(combinations)}: {academic_year}, {year_of_study}, {branch}, {section}")

            # Add delay between requests if specified
            if i > 1 and args.delay > 0:
                logger.debug(f"Sleeping for {args.delay} seconds...")
                time.sleep(args.delay)

            # Skip excluded branches
            excluded_branches = ["MTech_PS", "MTech_CSE", "MTech_ECE", "MTech_AMS"]
            if branch in excluded_branches:
                logger.warning(f"Branch {branch} is excluded from scraping. Skipping.")
                continue

            result_soup = scraper.select_form_filters(
                academic_year=academic_year,
                year_of_study=year_of_study,
                branch=branch,
                section=section
            )

            if result_soup:
                # Extract personal details
                students = scraper.extract_personal_details(result_soup)

                if students:
                    # Reset the counter since we found data
                    empty_combinations_in_a_row = 0

                    # Add metadata to each student record
                    for student in students:
                        student['academic_year'] = academic_year
                        student['year_of_study'] = year_of_study
                        student['branch'] = branch
                        student['section'] = section

                    # Store in structured folder system
                    success_count, update_count = scraper.store_personal_details_data(students, args.force_update)
                    total_success += success_count
                    total_updates += update_count
                    total_students += len(students)

                    logger.info(f"Processed {success_count} students with {update_count} updates")

                    # Also save to CSV if not disabled
                    if not args.no_csv:
                        # Create a filename for the CSV
                        csv_filename = f"{branch}_{section}_personal_details.csv"

                        # Save to CSV with structured folder path
                        scraper.save_to_csv(students, csv_filename, academic_year, year_of_study)
                    else:
                        logger.debug("CSV generation disabled")

                    logger.info(f"Successfully extracted and stored personal details for {len(students)} students")
                else:
                    logger.warning(f"No student details found for {academic_year}, {year_of_study}, {branch}, {section}")
                    empty_combinations_in_a_row += 1

                    # If we've seen too many empty combinations in a row, stop
                    if empty_combinations_in_a_row >= max_empty_combinations and args.skip_empty:
                        logger.warning(f"Found {empty_combinations_in_a_row} empty combinations in a row. Stopping.")
                        break
            else:
                logger.warning(f"Failed to select form filters for {academic_year}, {year_of_study}, {branch}, {section}. Skipping.")
                empty_combinations_in_a_row += 1

                # If we've seen too many empty combinations in a row, stop
                if empty_combinations_in_a_row >= max_empty_combinations and args.skip_empty:
                    logger.warning(f"Found {empty_combinations_in_a_row} empty combinations in a row. Stopping.")
                    break

        # Print summary
        if total_students > 0:
            logger.info(f"Summary: Processed {total_students} students across {len(combinations)} combinations")
            logger.info(f"Total success: {total_success}, Total updates: {total_updates}")
        else:
            logger.warning("No student details found for any combination")

        # Close the scraper
        scraper.close()

        # Return success
        logger.info("Personal details scraping completed successfully!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
