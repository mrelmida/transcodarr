from dotenv import load_dotenv, set_key
import os

ENV_FILE = ".env"

def set_stop_flag(value: bool):
    set_key(ENV_FILE, "STOP_FLAG", "true" if value else "false")
    load_dotenv(override=True)  # refresh env immediately

def get_stop_flag() -> bool:
    return os.getenv("STOP_FLAG", "false").lower() == "true"