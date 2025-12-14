#!/usr/bin/env python3
"""
Render.com entry point for grade monitor
Runs continuously as a background worker
"""

import time
import logging
import os
from grade_monitor import ODTUClassMonitor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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

    logger.info("🚀 Grade Monitor started on Render.com")
    logger.info("📊 Checking grades every 1 minute...")

    # Initial login (only once at startup)
    if not monitor.login():
        logger.error("❌ Initial login failed - cannot start")
        return

    logger.info("✅ Logged in successfully")

    # Run forever
    while True:
        try:
            # Check grades (will auto re-login if session expires)
            monitor.check_grades()
            logger.info("✅ Grade check completed")

        except Exception as e:
            logger.error(f"❌ Error: {e}")

        # Wait 1 minute
        logger.info("⏳ Waiting 1 minute until next check...")
        time.sleep(60)

if __name__ == '__main__':
    main()
