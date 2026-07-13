"""Entry point for the browser/Docker version (default port 4091)."""
import os

import uvicorn

from web.server import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(),
                host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "4091")))
