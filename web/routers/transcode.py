# web/routers/transcode.py
from fastapi import APIRouter, Request, Body, Query
from fastapi.responses import JSONResponse
import os, logging

from transcodarr_core.database import is_ignored

router = APIRouter()


@router.post("/transcode/manual")
def api_transcode_manual(request: Request, data: dict = Body(default={})):
    """Queue a manual transcode job."""
    worker_pool = request.app.state.worker_pool
    if not worker_pool:
        return JSONResponse({"error": "Worker pool not initialized"}, status_code=500)

    file_path = data.get("file_path")
    media_type = data.get("media_type", "movie")

    if not file_path:
        return JSONResponse({"error": "file_path is required"}, status_code=400)

    if not os.path.exists(file_path):
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        if is_ignored(file_path):
            return JSONResponse({"error": "File is on ignore list. Remove from ignore first."}, status_code=400)
    except Exception:
        pass

    if worker_pool.manual_workers <= 0:
        return JSONResponse({"error": "Manual transcoding is disabled (MANUAL_WORKERS=0)"}, status_code=503)

    if not worker_pool.can_accept_job():
        status = worker_pool.get_status()
        return JSONResponse({
            "error": "All manual workers busy",
            "active_manual_jobs": status["active_manual_jobs"],
            "manual_workers": status["manual_workers"],
        }, status_code=503)

    job = worker_pool.submit_manual_job(
        file_path=file_path,
        media_type=media_type,
        title=data.get("title"),
        year=data.get("year"),
        show=data.get("show"),
        season=data.get("season"),
        episode=data.get("episode"),
    )

    if job:
        return {"status": "queued", "job": job.to_dict()}
    else:
        return JSONResponse({"error": "Failed to queue job"}, status_code=500)


@router.post("/transcode/batch")
def api_transcode_batch(request: Request, data: dict = Body(default={})):
    """Queue a batch of files for sequential transcoding on one worker."""
    worker_pool = request.app.state.worker_pool
    if not worker_pool:
        return JSONResponse({"error": "Worker pool not initialized"}, status_code=500)

    items = data.get("items", [])
    if not items:
        return JSONResponse({"error": "items list is required"}, status_code=400)

    if worker_pool.manual_workers <= 0:
        return JSONResponse({"error": "Manual transcoding is disabled (MANUAL_WORKERS=0)"}, status_code=503)

    if not worker_pool.can_accept_job():
        status = worker_pool.get_status()
        return JSONResponse({
            "error": "All manual workers busy",
            "active_manual_jobs": status["active_manual_jobs"],
            "manual_workers": status["manual_workers"],
        }, status_code=503)

    valid = []
    for it in items:
        fp = it.get("file_path")
        if not fp or not os.path.exists(fp):
            continue
        if is_ignored(fp):
            continue
        valid.append(it)

    if not valid:
        return JSONResponse({"error": "No valid files in batch"}, status_code=400)

    job = worker_pool.submit_batch_job(valid)
    if job:
        return {"status": "queued", "job": job.to_dict(), "batch_size": len(valid)}
    else:
        return JSONResponse({"error": "Failed to queue batch"}, status_code=500)


@router.get("/transcode/jobs")
def api_transcode_jobs(
    request: Request,
    include_completed: str = Query(default="true"),
    limit: int = Query(default=100),
):
    """List all transcode jobs."""
    worker_pool = request.app.state.worker_pool
    if not worker_pool:
        return JSONResponse({"error": "Worker pool not initialized"}, status_code=500)

    inc = include_completed.lower() == "true"
    jobs = worker_pool.get_all_jobs(include_completed=inc, limit=limit)
    return {
        "jobs": [j.to_dict() for j in jobs],
        "count": len(jobs),
    }


@router.get("/transcode/jobs/{job_id}")
def api_transcode_job(job_id: str, request: Request):
    """Get a specific transcode job."""
    worker_pool = request.app.state.worker_pool
    if not worker_pool:
        return JSONResponse({"error": "Worker pool not initialized"}, status_code=500)

    job = worker_pool.get_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    return job.to_dict()


@router.delete("/transcode/jobs/{job_id}")
def api_cancel_job(job_id: str, request: Request):
    """Cancel a queued transcode job."""
    worker_pool = request.app.state.worker_pool
    if not worker_pool:
        return JSONResponse({"error": "Worker pool not initialized"}, status_code=500)

    cancelled = worker_pool.cancel_job(job_id)
    if cancelled:
        return {"status": "cancelled"}
    else:
        return JSONResponse({"error": "Cannot cancel (job may be running or completed)"}, status_code=400)


@router.post("/transcode/stop")
def api_stop_transcode(request: Request, data: dict = Body(default={})):
    """Stop a running or queued transcode for a specific file."""
    from transcodarr_core.worker_pool import terminate_proc_for_file

    file_path = data.get("file_path")
    if not file_path:
        return JSONResponse({"error": "file_path required"}, status_code=400)

    worker_pool = request.app.state.worker_pool
    if not worker_pool:
        return JSONResponse({"error": "Worker pool not initialized"}, status_code=500)

    killed = terminate_proc_for_file(file_path)
    worker_pool._remove_processing_file(file_path)

    cancelled = False
    batch_stopped = False
    for job in worker_pool.get_jobs_for_file(file_path):
        if job.status.value in ("queued", "running"):
            if worker_pool.cancel_job(job.job_id):
                cancelled = True
        if job.batch_id and not batch_stopped:
            worker_pool.stop_batch(job.batch_id)
            batch_stopped = True

    if killed or cancelled or batch_stopped:
        return {"status": "stopped", "killed": killed, "cancelled": cancelled, "batch_stopped": batch_stopped}
    else:
        return JSONResponse({"error": "No active transcode found for this file"}, status_code=404)


@router.get("/transcode/batch/{batch_id}")
def api_batch_info(batch_id: str, request: Request):
    """Get all jobs in a batch."""
    worker_pool = request.app.state.worker_pool
    if not worker_pool:
        return JSONResponse({"error": "Worker pool not initialized"}, status_code=500)

    jobs = worker_pool.get_batch_jobs(batch_id)
    if not jobs:
        return JSONResponse({"error": "Batch not found"}, status_code=404)

    return {"batch_id": batch_id, "jobs": [j.to_dict() for j in jobs], "count": len(jobs)}


@router.post("/transcode/batch/{batch_id}/stop")
def api_stop_batch(batch_id: str, request: Request):
    """Stop a running batch and cancel remaining items."""
    worker_pool = request.app.state.worker_pool
    if not worker_pool:
        return JSONResponse({"error": "Worker pool not initialized"}, status_code=500)

    jobs = worker_pool.get_batch_jobs(batch_id)
    if not jobs:
        return JSONResponse({"error": "Batch not found"}, status_code=404)

    worker_pool.stop_batch(batch_id)
    return {"status": "stopping", "batch_id": batch_id}
