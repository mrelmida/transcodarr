# gunicorn.conf.py
bind = "0.0.0.0:5025"

# Keep a single worker so your background thread + in-process state behaves.
workers = 1

# Use threaded worker so concurrent requests (UI + log polling) are handled smoothly.
worker_class = "gthread"
threads = 8

# Do NOT auto-recycle workers due to request counts.
max_requests = 0
max_requests_jitter = 0

# Give plenty of time for long requests (debug dumps, slow FS reads, etc).
timeout = 600
graceful_timeout = 30
keepalive = 75

# Logging
loglevel = "info"
errorlog = "-"     # stderr
accesslog = None#"-"    # stdout
#capture_output = True

# You can keep this false in prod; only turn on when actively editing code inside the container.
reload = False

# Forwarded headers if you ever run behind a proxy
# secure_scheme_headers = {"X-Forwarded-Proto": "https"}