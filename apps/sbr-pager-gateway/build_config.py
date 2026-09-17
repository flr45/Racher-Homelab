"""Build-time values embedded in the Windows application.

The release workflow replaces these placeholders immediately before PyInstaller
runs. Never commit a real provisioning token to the repository.
"""

APP_VERSION = "0.1.0-dev"
SYSTEM_LINK_PROVISION_URL = ""
SYSTEM_LINK_MESSAGE_ENDPOINT = ""
SYSTEM_LINK_PROVISION_TOKEN = ""
