import logging 
from pathlib import Path

log_path=Path(__file__).parent/"agent.log"

logger=logging.getLogger("agent")
logger.setLevel(logging.INFO)

file_handler=logging.FileHandler(
    log_path,
    encoding="utf-8"
)
formatter=logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
file_handler.setFormatter(formatter)
if not logger.handlers:
    logger.addHandler(file_handler)