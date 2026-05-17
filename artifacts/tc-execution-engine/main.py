"""Entry point for gunicorn / uvicorn.

  dev:  uvicorn main:app --reload --port $PORT
  prod: gunicorn main:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
"""
import os
from app.main import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
