"""Backend server configuration.

Centralizes all Flask server settings.
"""

# Server configuration
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5055
SERVER_DEBUG = False
SERVER_AUTO_RELOAD = False

# CORS origins (if needed)
CORS_ORIGINS = ["*"]

# Request limits
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max request size
