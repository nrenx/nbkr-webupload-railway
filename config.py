"""
Configuration settings for the NBKRIST student portal scrapers.

This module contains configuration settings for both the attendance and mid marks scrapers.
It includes portal URLs, credentials, and default settings for the scrapers.
"""

# Portal URLs
ATTENDANCE_PORTAL_URL = "http://103.203.175.90:94/attendance/attendanceTillADate.php"
MID_MARKS_PORTAL_URL = "http://103.203.175.90:94/mid_marks/classSelectionForMarksDisplay.php"
PERSONAL_DETAILS_URL = "http://103.203.175.90:94/attendance/selectionForRollNos.php"

# Authentication credentials
# NOTE: These credentials change frequently. Update them as needed.
# Current credentials as of May 2025
# Try these credentials if the current ones don't work:
# USERNAME = "pds"
# PASSWORD = "sarwagnya"
USERNAME = "sumayya"
PASSWORD = "09041994"

# Default academic years (newest to oldest)
DEFAULT_ACADEMIC_YEARS = [
    "2024-25", "2023-24", "2022-23", "2021-22", "2020-21",
    "2019-20", "2018-19", "2017-18", "2016-17", "2015-16",
    "2014-15", "2013-14", "2012-13", "2011-12", "2010-11",
    "2009-10", "2008-09", "2007-08", "2006-07", "2005-06"
]

# Default semesters (in order)
DEFAULT_SEMESTERS = [
    "First Yr - First Sem",
    "First Yr - Second Sem",
    "Second Yr - First Sem",
    "Second Yr - Second Sem",
    "Third Yr - First Sem",
    "Third Yr - Second Sem",
    "Final Yr - First Sem",
    "Final Yr - Second Sem"
]

# Default branches (in logical order)
DEFAULT_BRANCHES = [
    # Computer-related branches first
    "CSE", "CSE_DS", "CSE_AIML", "AI_DS", "IT",
    # Electronics branches
    "ECE", "EEE",
    # Core engineering branches
    "MECH", "CIVIL"
]

# Default sections (including null section)
DEFAULT_SECTIONS = ["-", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]

# Year/Semester codes mapping
YEAR_SEM_CODES = {
    "First Yr - First Sem": "11",
    "First Yr - Second Sem": "12",
    "Second Yr - First Sem": "21",
    "Second Yr - Second Sem": "22",
    "Third Yr - First Sem": "31",
    "Third Yr - Second Sem": "32",
    "Final Yr - First Sem": "41",
    "Final Yr - Second Sem": "42",
    "Fourth Yr - First Sem": "41",  # Keep for backward compatibility
    "Fourth Yr - Second Sem": "42"   # Keep for backward compatibility
}
BRANCH_CODES = {
    # Core branches
    "MECH": "7",
    "CSE": "5",
    "ECE": "4",
    "EEE": "2",
    "CIVIL": "11",
    "IT": "22",

    # Specialized branches
    "AI_DS": "23",
    "CSE_DS": "32",
    "CSE_AIML": "33",

    # Alternative spellings/formats that might be used
    "AI&DS": "23",  # Alternative for AI_DS
    "CSE-DS": "32",  # Alternative for CSE_DS
    "CSE-AIML": "33",  # Alternative for CSE_AIML
    "CSEAIML": "33",  # Alternative without separator
    "CSEDS": "32"  # Alternative without separator
}

# Default scraper settings
DEFAULT_SETTINGS = {
    "headless": True,  # Always run browser in headless mode for Render compatibility
    "data_dir": "/tmp/student_details",  # Use Render's temporary storage
    "max_retries": 3,  # Maximum number of retries for network errors
    "workers": 1,  # Number of parallel workers
    "timeout": 30,  # Timeout in seconds for waiting for elements
    "progress_reporting": True  # Enable progress reporting for web interface
}
