"""gunicorn settings, read automatically when gunicorn starts in this folder.

    gunicorn wsgi:app

ONE worker process with FOUR threads (docs/PRODUCTION_DESIGN.md 10.1):

  * It fits in Render's 512 MB instance with room to spare -- every extra
    worker is another full copy of the app in memory.
  * The rate limiter keeps its counts in memory. With one process the
    counts are exact; with several, each would count separately and the
    limits would quietly loosen.
  * The app spends its time waiting on PostgreSQL and Google, which is what
    threads are good at. A handful of staff can't saturate four.

Raising `workers` is a one-line change, but move the rate limiter to shared
storage first (see RATELIMIT_STORAGE_URI in app/config.py).
"""

import os

# All interfaces, on the port Render assigns: Render's proxy reaches the app
# inside its own network. The app itself is never exposed any other way.
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
threads = 4
worker_class = "gthread"

# A request still running after 30 seconds is stuck; restart the worker.
timeout = 30
graceful_timeout = 20
keepalive = 5

# Access log to stdout, where Render collects it. The default format records
# the request line (method, path, query string), status, size, referrer and
# browser. Paths in this app carry ids, never participant codes or names --
# keep it that way (SECURITY.md, "Logs").
accesslog = "-"
errorlog = "-"
loglevel = "info"
