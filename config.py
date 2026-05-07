"""Central configuration for the Bugzilla → GitHub migration."""

# --- Bugzilla ---
BUGZILLA_URL = "https://your-bugzilla.example.com"
BUGZILLA_API_KEY = "your-bugzilla-api-key"

# URL used for back-links in imported issue headers (pointing to the original bug).
# Set this to a different host if you plan to redirect BUGZILLA_URL to GitHub and
# want to keep the old Bugzilla content accessible at an alternate address
# (e.g., "https://old-bugzilla.example.com/bugzilla").
# Defaults to BUGZILLA_URL if not set.
BUGZILLA_BACKLINK_URL = None

# --- GitHub ---
GITHUB_TOKEN = "ghp_your_token_here"
GITHUB_OWNER = "your-org"
GITHUB_REPO = "your-repo"
GITHUB_ATTACHMENTS_REPO = "your-repo-attachments"

# --- Paths ---
EXPORT_DIR = "bugzilla_export"
USER_MAPPING_FILE = "user_mapping.json"

# --- Behavior ---
# Seconds to wait between import API calls (rate limiting)
IMPORT_DELAY = 1.5
# Seconds to sleep between polling import status
POLL_INTERVAL = 2.0
# Seconds between Bugzilla API calls (be polite)
EXPORT_DELAY = 0.3
