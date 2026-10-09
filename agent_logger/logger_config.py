import logging 
import os
from pathlib import Path

log_path=Path(os.getenv("INCIDENT_AGENT_LOG_PATH") or Path(__file__).parent/"agent.log")

logger=logging.getLogger("agent")
logger.setLevel(logging.INFO)

if not logger.handlers:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler=logging.FileHandler(log_path, encoding="utf-8")
    formatter=logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
