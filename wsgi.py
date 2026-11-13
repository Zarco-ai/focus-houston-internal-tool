"""The production entry point.

    gunicorn wsgi:app                 serve it (Render's start command)
    flask --app wsgi db upgrade       apply migrations (Render's pre-deploy command)

No FOCUS_ENV is set on Render, so this builds the PRODUCTION configuration,
which refuses to start without SECRET_KEY and DATABASE_URL in the
environment. For a laptop, use run.py instead.
"""

from app import create_app

app = create_app()
