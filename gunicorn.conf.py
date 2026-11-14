
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
threads = 4
worker_class = "gthread"

timeout = 30
graceful_timeout = 20
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = "info"
