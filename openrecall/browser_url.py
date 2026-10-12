"""Browser tab capture - cross-platform entry point.

On Windows: uses UI Automation (original implementation)
On Linux: uses Firefox Remote Debugging Protocol + native messaging
On macOS: placeholder
"""
import sys

if sys.platform.startswith("linux"):
    from openrecall.browser_url_linux import get_browser_tabs, get_browser_url
else:
    # Windows implementation (kept for compatibility)
    from openrecall.browser_url_win import get_browser_tabs, get_browser_url

__all__ = ["get_browser_tabs", "get_browser_url"]