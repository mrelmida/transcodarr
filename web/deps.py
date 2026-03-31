# web/deps.py
# FastAPI dependency injection helpers
from fastapi import Request


def get_settings(request: Request):
    return request.app.state.settings


def get_worker_pool(request: Request):
    return request.app.state.worker_pool
