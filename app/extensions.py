
from authlib.integrations.flask_client import OAuth
from flask import current_app, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()

csrf = CSRFProtect()

migrate = Migrate()

oauth = OAuth()

def visitor_address():
    header = current_app.config["CLIENT_IP_HEADER"]
    return (header and request.headers.get(header)) or get_remote_address()


limiter = Limiter(key_func=visitor_address)
