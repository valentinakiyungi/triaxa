"""Entry point. Keeps `uvicorn main:app` working and allows `python main.py`."""

import os

from app.main import app  # noqa: F401  (re-exported for `uvicorn main:app`)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("RELOAD", "false").lower() == "true",
    )
