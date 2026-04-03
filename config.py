# config.py
import os
from dotenv import load_dotenv

# Explicitly load your config.env file
load_dotenv("config.env")

# Messenger Config
VERIFY_TOKEN = os.getenv("MESSENGER_VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.getenv("MESSENGER_PAGE_ACCESS_TOKEN")

# Database Config
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# Ollama Config
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL_NAME")

# # รหัสลับที่เราตั้งเอง (ใช้ตอนยืนยัน Webhook)
# VERIFY_TOKEN = os.getenv("MESSENGER_VERIFY_TOKEN")

# # รหัสยาวๆ ที่ได้จาก Facebook (ใช้ส่งข้อความกลับ)
# PAGE_ACCESS_TOKEN = os.getenv("MESSENGER_PAGE_ACCESS_TOKEN")

# Admin Alert Email Config
ADMIN_ALERT_EMAIL = os.getenv("ADMIN_ALERT_EMAIL")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"