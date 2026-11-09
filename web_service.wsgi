"""Entry point for Apache mod_wsgi (see apache/video-service.conf).

mod_wsgi looks for a variable called "application". create_app() waits for the
database and creates the table, so this runs when the daemon process starts.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web_service import create_app  # noqa: E402

application = create_app()
