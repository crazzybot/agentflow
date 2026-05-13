import os
import uvicorn
from dotenv import load_dotenv
from agentflow.logging_config import setup_logging


if __name__ == "__main__":
    _ = load_dotenv()

    # Configure logging
    setup_logging(level=os.getenv("LOG_LEVEL", "INFO"), json_format=False)

    uvicorn.run(
        app="agentflow.main:app",
        host="127.0.0.1",
        port=8001,
        reload=False,
        log_level="info",
    )
