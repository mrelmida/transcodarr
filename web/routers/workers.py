# web/routers/workers.py
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/workers/status")
def api_workers_status(request: Request):
    """Get worker pool status."""
    worker_pool = request.app.state.worker_pool
    if not worker_pool:
        return JSONResponse({"error": "Worker pool not initialized"}, status_code=500)

    status = worker_pool.get_status()
    return status
