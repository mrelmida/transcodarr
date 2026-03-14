# web/blueprints/ui.py
from flask import Blueprint, current_app, render_template

ui_bp = Blueprint("ui", __name__, template_folder="../templates", static_folder="../static")

@ui_bp.get("/")
def home():
    settings = current_app.config["SETTINGS"]

    return render_template(
        "ui.html",
        api_base="/api",  # hardcode or make configurable later
        ui_boot={"watch": settings.WATCH_FOLDER, "output": settings.OUTPUT_FOLDER},
    )