#!/usr/bin/env python3
"""
ODTÜClass Grade Monitor with Telegram Notifications
Monitors grades and sends notifications via Telegram
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import logging
from datetime import datetime
from pathlib import Path
import schedule
import sys
import os
import re

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s", "level":"%(levelname)s", "message":"%(message)s"}',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class BackoffManager:
    """Manages exponential backoff for failed grade checks"""

    def __init__(self):
        self.consecutive_failures = 0
        self.last_success = datetime.now()

    def record_failure(self):
        """Record a failed attempt"""
        self.consecutive_failures += 1
        logger.warning(f"Consecutive failures: {self.consecutive_failures}")

    def record_success(self):
        """Record a successful attempt"""
        if self.consecutive_failures > 0:
            logger.info(f"Recovered after {self.consecutive_failures} failures")
        self.consecutive_failures = 0
        self.last_success = datetime.now()

    def get_wait_time(self):
        """Calculate wait time based on failures (exponential backoff)"""
        if self.consecutive_failures == 0:
            return 60  # Default 1 minute

        # Exponential backoff: 2^n minutes, max 30 minutes
        wait_minutes = min(2 ** self.consecutive_failures, 30)
        return wait_minutes * 60


class ODTUClassMonitor:
    def __init__(self, username, password, telegram_token, chat_id,
                 base_url=None, grades_file_path=None):
        self.username = username
        self.password = password
        self.telegram_token = telegram_token
        self.chat_id = chat_id

        # Configurable base URL (supports different semesters)
        self.base_url = base_url or os.getenv('ODTU_BASE_URL',
                                               'https://odtuclass2025f.metu.edu.tr')

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                         'AppleWebKit/537.36 (KHTML, like Gecko) '
                         'Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,tr;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        })

        # Persistent storage path
        if grades_file_path:
            self.grades_file = Path(grades_file_path)
        else:
            self.grades_file = Path('grades_history.json')

        # Ensure data directory exists
        self.grades_file.parent.mkdir(parents=True, exist_ok=True)

        # User ID extracted after login
        self.user_id = None

        # Backoff manager for rate limiting
        self.backoff = BackoffManager()

        logger.info(f"Monitor initialized - Base URL: {self.base_url}")
        logger.info(f"Grades file: {self.grades_file}")

    def send_telegram_message(self, message):
        """Send message via Telegram API (direct HTTP, no async)"""
        if not self.telegram_token or self.telegram_token == "YOUR_BOT_TOKEN_FROM_BOTFATHER":
            logger.warning("Telegram not configured - skipping notification")
            return False

        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            data = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }

            response = requests.post(url, json=data, timeout=10)
            response.raise_for_status()

            logger.info("Telegram message sent successfully")
            return True

        except requests.exceptions.RequestException as e:
            logger.error(f"Telegram API error: {e}")
            return False

    def extract_user_id(self):
        """Extract user ID from dashboard after successful login"""
        try:
            # Try to get user profile page
            dashboard_url = f"{self.base_url}/my/"
            response = self.session.get(dashboard_url, timeout=10)

            if response.status_code != 200:
                logger.warning("Could not fetch dashboard for user ID extraction")
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # Look for user profile links (multiple patterns)
            patterns = [
                r'/user/profile\.php\?id=(\d+)',
                r'/user/view\.php\?id=(\d+)',
                r'[&?]user=(\d+)',
                r'userid["\']:\s*["\']?(\d+)'
            ]

            for pattern in patterns:
                match = re.search(pattern, response.text)
                if match:
                    user_id = match.group(1)
                    logger.info(f"Extracted user ID: {user_id}")
                    return user_id

            logger.warning("Could not extract user ID from dashboard")
            return None

        except Exception as e:
            logger.error(f"Error extracting user ID: {e}")
            return None

    def load_previous_grades(self):
        """Load previously saved grades"""
        try:
            if self.grades_file.exists():
                with open(self.grades_file, 'r', encoding='utf-8') as f:
                    grades = json.load(f)
                logger.info(f"Loaded {len(grades)} courses from history")
                return grades
            logger.info("No previous grades found - first run")
            return {}
        except Exception as e:
            logger.error(f"Error loading grades history: {e}")
            return {}

    def save_grades(self, grades):
        """Save current grades to file"""
        try:
            with open(self.grades_file, 'w', encoding='utf-8') as f:
                json.dump(grades, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved {len(grades)} courses to history")
        except Exception as e:
            logger.error(f"Error saving grades: {e}")

    def login(self):
        """Login to ODTÜClass"""
        login_url = f"{self.base_url}/login/index.php"

        try:
            logger.info("Attempting login...")

            # Small delay to avoid rate limiting
            time.sleep(2)

            # Get login page to retrieve logintoken
            response = self.session.get(login_url, timeout=15, allow_redirects=True)

            if response.status_code != 200:
                logger.error(f"Login page returned status {response.status_code}")
                logger.error(f"Final URL: {response.url}")
                return False

            # Debug: Check response size
            logger.info(f"Login page received: {len(response.text)} bytes")

            soup = BeautifulSoup(response.text, 'html.parser')
            logintoken = soup.find('input', {'name': 'logintoken'})

            if not logintoken:
                # Try alternative selectors
                logintoken = soup.select_one('input[name="logintoken"]')

            if not logintoken:
                logger.error("Could not find login token in page")
                logger.error(f"Page title: {soup.title.string if soup.title else 'No title'}")

                # Check if we got an error page or redirect
                if 'error' in response.text.lower() or 'blocked' in response.text.lower():
                    logger.error("Possible blocking or error page detected")

                # Save first 500 chars for debugging
                logger.error(f"Page preview: {response.text[:500]}")
                return False

            # Perform login
            login_data = {
                'username': self.username,
                'password': self.password,  # Never logged
                'logintoken': logintoken.get('value', '')
            }

            # Add referer header for login POST
            headers = {'Referer': login_url}
            response = self.session.post(login_url, data=login_data,
                                        headers=headers, timeout=15,
                                        allow_redirects=True)

            # Check if login successful
            if 'logout.php' in response.text:
                logger.info("✅ Login successful")

                # Extract user ID after successful login
                self.user_id = self.extract_user_id()
                if not self.user_id:
                    logger.warning("Could not extract user ID - will try without it")

                return True
            else:
                logger.error("❌ Login failed - check credentials")
                # Check for common error messages
                if 'Invalid login' in response.text or 'invalid' in response.text.lower():
                    logger.error("Invalid credentials detected")
                return False

        except requests.exceptions.Timeout:
            logger.error("Login request timed out - server may be slow or blocking")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error during login: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during login: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def safe_find(self, soup, *args, **kwargs):
        """Safe BeautifulSoup find with logging"""
        result = soup.find(*args, **kwargs)
        if result is None:
            logger.debug(f"Could not find element: {args}, {kwargs}")
        return result

    def fetch_grades(self):
        """Fetch all courses and their detailed assignment grades"""
        grades_url = f"{self.base_url}/grade/report/overview/index.php"

        try:
            logger.info("Fetching grades overview...")
            response = self.session.get(grades_url, timeout=15)

            if response.status_code != 200:
                logger.error(f"Grades page returned status {response.status_code}")
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # Check if we're still logged in
            if 'Log in to ODTUCLASS' in response.text or 'login/index.php' in response.text:
                logger.warning("Session expired, re-logging in...")
                if not self.login():
                    return None
                response = self.session.get(grades_url, timeout=15)
                soup = BeautifulSoup(response.text, 'html.parser')

            # Parse courses list with error handling
            all_grades = {}
            table = self.safe_find(soup, 'table', {'id': 'overview-grade'})

            if not table:
                logger.error("Could not find grades table - HTML structure may have changed")
                return None

            tbody = self.safe_find(table, 'tbody')
            if not tbody:
                logger.error("Could not find table body")
                return None

            rows = tbody.find_all('tr')
            logger.info(f"Found {len(rows)} course rows")

            for idx, row in enumerate(rows):
                try:
                    cells = row.find_all('td')
                    if len(cells) < 2:
                        continue

                    course_cell = cells[0]
                    grade_cell = cells[1]

                    # Extract course info
                    course_link = course_cell.find('a')
                    if not course_link:
                        continue

                    course_name = course_link.text.strip()
                    overall_grade = grade_cell.text.strip()

                    # Extract course ID from URL
                    course_url = course_link.get('href', '')
                    if 'id=' not in course_url:
                        continue

                    course_id = course_url.split('id=')[1].split('&')[0]

                    # Fetch detailed grades for this course
                    logger.info(f"  📖 Fetching: {course_name[:50]}...")
                    assignments = self.fetch_course_details(course_id)

                    # Add delay between course fetches to avoid rate limiting
                    if idx < len(rows) - 1:  # Don't sleep after last course
                        time.sleep(0.5)

                    all_grades[course_name] = {
                        'course_id': course_id,
                        'overall_grade': overall_grade,
                        'assignments': assignments if assignments else {},
                        'last_updated': datetime.now().isoformat()
                    }

                except Exception as e:
                    logger.warning(f"Error parsing course row {idx}: {e}")
                    continue

            logger.info(f"Successfully fetched {len(all_grades)} courses")
            return all_grades

        except requests.exceptions.Timeout:
            logger.error("Grades fetch timed out")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error fetching grades: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching grades: {e}")
            return None

    def fetch_course_details(self, course_id):
        """Fetch detailed assignment grades for a specific course"""
        try:
            # Use extracted user ID if available, otherwise try without user parameter
            if self.user_id:
                details_url = f"{self.base_url}/course/user.php?mode=grade&id={course_id}&user={self.user_id}"
            else:
                details_url = f"{self.base_url}/course/user.php?mode=grade&id={course_id}"

            response = self.session.get(details_url, timeout=10)

            if response.status_code != 200:
                logger.debug(f"Course details returned status {response.status_code}")
                return {}

            soup = BeautifulSoup(response.text, 'html.parser')
            table = self.safe_find(soup, 'table', {'class': 'user-grade'})

            if not table:
                logger.debug("Could not find user-grade table")
                return {}

            tbody = self.safe_find(table, 'tbody')
            if not tbody:
                return {}

            assignments = {}
            rows = tbody.find_all('tr')

            for row in rows:
                try:
                    # Look for rows with gradeitemheader (can be <a> for assignments or <span> for manual items)
                    assignment_element = row.find(['a', 'span'], class_='gradeitemheader')

                    if not assignment_element:
                        continue

                    assignment_name = assignment_element.get_text(strip=True)

                    # Get all cells in this row
                    cells = row.find_all(['th', 'td'])

                    if len(cells) < 3:
                        continue

                    # Weight is usually in cell index 1 (after name cell)
                    weight = cells[1].get_text(strip=True) if len(cells) > 1 else '-'

                    # Grade is in the cell with 'column-grade' class
                    grade_cell = row.find('td', class_=lambda x: x and 'column-grade' in x)

                    if not grade_cell:
                        continue

                    # Get grade text with multiple fallback patterns
                    grade_text = None
                    grade_div = grade_cell.find('div', class_='d-flex')

                    if grade_div:
                        inner_div = grade_div.find('div')
                        grade_text = inner_div.get_text(strip=True) if inner_div else grade_div.get_text(strip=True)
                    else:
                        grade_text = grade_cell.get_text(strip=True)

                    # Clean up - remove any extra text after newlines
                    grade_text = grade_text.split('\n')[0].strip() if grade_text else '-'

                    # Store all assignments regardless of grade status
                    # This includes assignments without grades yet, zero grades, etc.
                    assignments[assignment_name] = {
                        'grade': grade_text if grade_text else '-',
                        'weight': weight,
                        'average': cells[3].get_text(strip=True) if len(cells) > 3 else '-'
                    }

                except Exception as e:
                    logger.debug(f"Error parsing assignment row: {e}")
                    continue

            return assignments

        except Exception as e:
            logger.warning(f"Error fetching course details: {e}")
            return {}

    def compare_and_notify(self, old_grades, new_grades):
        """Compare assignment grades and send Telegram messages for changes"""
        changes = []

        # Check each course
        for course_name, course_data in new_grades.items():
            new_assignments = course_data.get('assignments', {})

            if course_name not in old_grades:
                # New course - notify about all graded assignments (except Course total)
                for assignment_name, assignment_data in new_assignments.items():
                    # Skip "Course total" - it will be added as footer
                    if assignment_name.lower() == 'course total':
                        continue

                    changes.append({
                        'type': 'new_assignment',
                        'course': course_name,
                        'assignment': assignment_name,
                        'grade': assignment_data['grade'],
                        'average': assignment_data.get('average', '-'),
                        'weight': assignment_data.get('weight', '-'),
                        'course_total': new_assignments.get('Course total', {}).get('grade', '-')
                    })
            else:
                # Existing course - check for new/changed assignments
                old_assignments = old_grades[course_name].get('assignments', {})
                old_course_total = old_assignments.get('Course total', {}).get('grade', '-')
                new_course_total = new_assignments.get('Course total', {}).get('grade', '-')

                for assignment_name, assignment_data in new_assignments.items():
                    # Skip "Course total" - it will be added as footer
                    if assignment_name.lower() == 'course total':
                        continue

                    new_grade = assignment_data['grade']

                    if assignment_name not in old_assignments:
                        # New assignment grade
                        changes.append({
                            'type': 'new_assignment',
                            'course': course_name,
                            'assignment': assignment_name,
                            'grade': new_grade,
                            'average': assignment_data.get('average', '-'),
                            'weight': assignment_data.get('weight', '-'),
                            'course_total': new_course_total,
                            'old_course_total': old_course_total
                        })
                    else:
                        # Check if grade changed
                        old_grade = old_assignments[assignment_name]['grade']
                        if old_grade != new_grade:
                            changes.append({
                                'type': 'updated_assignment',
                                'course': course_name,
                                'assignment': assignment_name,
                                'old_grade': old_grade,
                                'new_grade': new_grade,
                                'average': assignment_data.get('average', '-'),
                                'weight': assignment_data.get('weight', '-'),
                                'course_total': new_course_total,
                                'old_course_total': old_course_total
                            })

        if changes:
            logger.info(f"⚡ {len(changes)} grade change(s) detected!")
            print(f"\n{'='*60}")
            print(f"⚡ NEW GRADES DETECTED!")
            print(f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"{'='*60}")

            for change in changes:
                if change['type'] == 'new_assignment':
                    # Console output
                    console_msg = f"🆕 {change['course']}\n   {change['assignment']}: {change['grade']}"
                    print(console_msg)

                    # Build course total footer
                    course_total_line = ""
                    if change.get('course_total') and change['course_total'] != '-':
                        if change.get('old_course_total') and change['old_course_total'] != '-' and change['old_course_total'] != change['course_total']:
                            # Course total changed
                            course_total_line = f"\n📈 <b>Course Total:</b> {change['old_course_total']} → {change['course_total']}"
                        else:
                            # Course total exists (no change or new course)
                            course_total_line = f"\n📈 <b>Course Total:</b> {change['course_total']}"

                    # Telegram message with HTML formatting
                    telegram_msg = f"""🎓 <b>New Grade Posted!</b>

<b>Course:</b> {change['course']}
    <b>Assignment:</b> {change['assignment']}
    ── <b>Your Grade:</b> {change['grade']}
    ── <b>Class Average:</b> {change['average']}
    ── <b>Weight:</b> {change['weight']}{course_total_line}"""

                    self.send_telegram_message(telegram_msg)

                else:
                    # Console output
                    console_msg = f"📝 {change['course']}\n   {change['assignment']}: {change['old_grade']} → {change['new_grade']}"
                    print(console_msg)

                    # Build course total footer
                    course_total_line = ""
                    if change.get('course_total') and change['course_total'] != '-':
                        if change.get('old_course_total') and change['old_course_total'] != '-' and change['old_course_total'] != change['course_total']:
                            # Course total changed
                            course_total_line = f"\n    ── <b>Course Total:</b> {change['old_course_total']} → {change['course_total']}"
                        else:
                            # Course total exists (no change)
                            course_total_line = f"\n    ── <b>Course Total:</b> {change['course_total']}"

                    # Telegram message
                    telegram_msg = f"""📝 <b>Grade Updated!</b>

<b>Course:</b> {change['course']}
    <b>Assignment:</b> {change['assignment']}
    ── <b>Change:</b> {change['old_grade']} → {change['new_grade']}
    ── <b>Class Average:</b> {change['average']}
    ── <b>Weight:</b> {change['weight']}{course_total_line}"""

                    self.send_telegram_message(telegram_msg)

            print(f"{'='*60}\n")

    def calculate_statistics(self, grades):
        """Calculate and display assignment statistics"""
        total_assignments = 0
        total_courses = len(grades)

        for course, data in grades.items():
            assignments = data.get('assignments', {})
            total_assignments += len(assignments)

        print(f"\n📊 Summary:")
        print(f"   📚 Courses monitored: {total_courses}")
        print(f"   📝 Graded assignments: {total_assignments}")

        logger.info(f"Statistics - Courses: {total_courses}, Assignments: {total_assignments}")

    def check_grades(self):
        """Main function to check for grade changes"""
        logger.info("Starting grade check...")
        print(f"\n🔍 Checking grades at {datetime.now().strftime('%H:%M:%S')}")

        # Load previous grades
        old_grades = self.load_previous_grades()

        # Fetch current grades
        new_grades = self.fetch_grades()

        if new_grades is None:
            logger.error("Failed to fetch grades")
            print("❌ Failed to fetch grades")
            self.backoff.record_failure()
            return

        # Success - reset backoff
        self.backoff.record_success()

        print(f"✅ Found {len(new_grades)} courses")

        # Compare and notify
        if old_grades:
            self.compare_and_notify(old_grades, new_grades)
        else:
            logger.info("First run - saving initial grades")
            print("📋 First run - saving initial grades")
            self.send_telegram_message(
                "🎓 <b>Grade Monitor Started</b>\n\nNow monitoring your grades"
            )

        # Save current grades
        self.save_grades(new_grades)

        # Show statistics
        self.calculate_statistics(new_grades)

    def run(self):
        """Start the monitoring service with intelligent scheduling"""
        print("╔" + "="*58 + "╗")
        print("║  🎓 ODTÜClass Grade Monitor" + " "*29 + "║")
        print("║  Intelligent polling with exponential backoff" + " "*9 + "║")
        print("║  Press Ctrl+C to stop" + " "*36 + "║")
        print("╚" + "="*58 + "╝\n")

        logger.info("Starting grade monitor service")

        # Initial login
        if not self.login():
            logger.error("Cannot start - login failed")
            print("❌ Cannot start - login failed")
            print("   Check your credentials")
            return

        # First check
        self.check_grades()

        # Get check interval from environment or use default
        check_interval = int(os.getenv('CHECK_INTERVAL_MINUTES', '1'))
        logger.info(f"Check interval: {check_interval} minute(s)")

        # Dynamic scheduling with backoff
        try:
            while True:
                # Calculate next check time based on backoff
                wait_seconds = self.backoff.get_wait_time()

                if wait_seconds > 60:  # More than 1 minute
                    wait_minutes = wait_seconds / 60
                    logger.info(f"Waiting {wait_minutes:.1f} minutes until next check (backoff active)")
                    print(f"⏳ Next check in {wait_minutes:.1f} minutes (backoff active)")
                else:
                    logger.info(f"Waiting {check_interval} minute(s) until next check")
                    print(f"⏳ Next check in {check_interval} minute(s)")

                time.sleep(wait_seconds if wait_seconds > 60 else check_interval * 60)
                self.check_grades()

        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            print("\n\n👋 Stopping monitor...")
            sys.exit(0)


def load_config_local():
    """Load configuration from local config.json"""
    config_file = Path("config.json")

    if not config_file.exists():
        logger.info("Creating config.json template")
        print("❌ config.json not found!\n")
        print("Creating config.json template...")
        config = {
            "username": "your_student_id",
            "password": "your_password",
            "telegram_bot_token": "YOUR_BOT_TOKEN_FROM_BOTFATHER",
            "telegram_chat_id": "YOUR_CHAT_ID"
        }
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
        print("✅ Created config.json")
        print("\n📝 Please edit config.json with your credentials:")
        print(f"   {config_file.absolute()}\n")
        sys.exit(1)

    with open(config_file, 'r') as f:
        config = json.load(f)

    # Validate credentials
    if config['username'] == 'your_student_id' or config['password'] == 'your_password':
        logger.error("Default credentials detected in config.json")
        print("❌ Please update config.json with your ODTÜClass credentials!\n")
        sys.exit(1)

    if config.get('telegram_bot_token', 'YOUR_BOT_TOKEN_FROM_BOTFATHER') == 'YOUR_BOT_TOKEN_FROM_BOTFATHER':
        logger.warning("Telegram not configured")
        print("⚠️  Warning: Telegram not configured - messages won't be sent")
        print("   Get bot token from @BotFather on Telegram")
        print("   Get your chat_id by messaging your bot and visiting:")
        print("   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates\n")

    return config


def main():
    """Main entry point"""
    logger.info("=== ODTÜClass Grade Monitor Starting ===")

    # Check if running in production (environment variables set)
    if os.getenv('ODTU_USERNAME'):
        # Production: Load from environment variables (Railway, Render, etc.)
        logger.info("Running in production mode - using environment variables")
        print("🔐 Loading credentials from environment variables...")
        config = {
            'username': os.getenv('ODTU_USERNAME'),
            'password': os.getenv('ODTU_PASSWORD'),
            'telegram_bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
            'telegram_chat_id': os.getenv('TELEGRAM_CHAT_ID')
        }
    else:
        # Local development: Load from config.json
        logger.warning("Running in local mode - using config.json")
        print("⚠️  Using local config.json")
        config = load_config_local()

    # Get optional base URL from config or environment
    base_url = config.get('base_url') or os.getenv('ODTU_BASE_URL')

    # Use current directory for grades file
    grades_file = "grades_history.json"

    # Create monitor instance
    monitor = ODTUClassMonitor(
        username=config['username'],
        password=config['password'],
        telegram_token=config.get('telegram_bot_token', 'YOUR_BOT_TOKEN_FROM_BOTFATHER'),
        chat_id=config.get('telegram_chat_id', 'YOUR_CHAT_ID'),
        base_url=base_url,
        grades_file_path=grades_file
    )

    # Start monitoring
    monitor.run()


if __name__ == "__main__":
    main()
