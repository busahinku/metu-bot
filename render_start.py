#!/usr/bin/env python3
"""
Render.com / Railway entry point for grade monitor
Runs continuously as a background worker
Only active between 07:00-02:00 Turkey time (UTC+3)
"""

import time
import logging
import os
from datetime import datetime
import pytz
from grade_monitor import ODTUClassMonitor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Turkey timezone
TURKEY_TZ = pytz.timezone('Europe/Istanbul')

def is_active_hours():
    """Check if current time is between 08:00 and 02:00 Turkey time (UTC+3)"""
    turkey_time = datetime.now(TURKEY_TZ)
    current_hour = turkey_time.hour
    
    # Active between 08:00 and 02:00 (sleep: 02:00-08:00)
    # This means: 8, 9, 10, ..., 23, 0, 1
    if current_hour >= 8 or current_hour < 2:
        return True
    return False

def main():
    """Run grade monitor continuously"""

    # Get credentials from environment
    username = os.environ.get('ODTU_USERNAME')
    password = os.environ.get('ODTU_PASSWORD')
    telegram_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')

    if not all([username, password, telegram_token, chat_id]):
        logger.error("Missing required environment variables!")
        logger.error("Need: ODTU_USERNAME, ODTU_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")
        return

    # Create monitor
    monitor = ODTUClassMonitor(
        username=username,
        password=password,
        telegram_token=telegram_token,
        chat_id=chat_id,
        grades_file_path='grades_history.json'
    )

    logger.info("🚀 Grade Monitor started")
    logger.info("📊 Active hours: 08:00-02:00 Turkey time (UTC+3)")
    logger.info("📊 Checking grades every 90 seconds during active hours...")

    logged_in = False

    # Run forever
    while True:
        try:
            # Check if we're in active hours
            if not is_active_hours():
                turkey_time = datetime.now(TURKEY_TZ)
                logger.info(f"😴 Outside active hours (current: {turkey_time.strftime('%H:%M')} Turkey time)")
                logger.info("⏳ Sleeping for 10 minutes...")
                logged_in = False  # Reset login when inactive
                time.sleep(600)  # Sleep 10 minutes
                continue

            # Login once when entering active hours
            if not logged_in:
                logger.info("🌅 Entering active hours - logging in...")
                if not monitor.login():
                    logger.error("❌ Login failed - will retry in 1 minute")
                    time.sleep(60)
                    continue
                logger.info("✅ Logged in successfully")
                logged_in = True

            # Check grades (will auto re-login if session expires)
            turkey_time = datetime.now(TURKEY_TZ)
            logger.info(f"🔍 Checking grades at {turkey_time.strftime('%H:%M:%S')} Turkey time...")
            monitor.check_grades()
            logger.info("✅ Grade check completed")

        except Exception as e:
            logger.error(f"❌ Error: {e}")
            logged_in = False  # Reset login on error

        # Wait 90 seconds
        logger.info("⏳ Waiting 90 seconds until next check...")
        time.sleep(90)  # 90 seconds = 1.5 minutes

if __name__ == '__main__':
    main()
