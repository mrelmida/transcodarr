from functools import wraps
from flask import request, abort, current_app, session

def require_api_key(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        s = current_app.config["SETTINGS"]
        key = request.headers.get("X-API-Key") or request.args.get("api_key")
        if s.ADMIN_API_KEY and key == s.ADMIN_API_KEY:
            return view(*args, **kwargs)
        abort(401)
    return wrapper

def require_login(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        s = current_app.config["SETTINGS"]
        # “Pro” paywall can simply check a flag in session/db
        if session.get("logged_in") or not s.UI_REQUIRES_LOGIN:
            return view(*args, **kwargs)
        abort(401)
    return wrapper