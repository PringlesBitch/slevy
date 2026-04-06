"""
Hlídač slev – FastAPI backend
Spuštění: uvicorn main:app --reload --port 8000
"""

import json
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from scraper import analyze_eshop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Hlídač slev API", version="1.0.0")


@app.on_event("shutdown")
async def shutdown_event():
    """Zavře Playwright browser při ukončení serveru."""
    try:
        from browser import close_browser
        await close_browser()
    except Exception:
        pass

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL nesmí být prázdné")
        if len(v) > 500:
            raise ValueError("URL je příliš dlouhé")
        return v


@app.post("/analyze")
async def analyze(request: AnalyzeRequest):
    """
    Spustí analýzu e-shopu a streamuje průběh jako Server-Sent Events.
    Frontend čte stream a postupně aktualizuje UI.
    """
    logger.info(f"Analýza spuštěna: {request.url}")

    async def event_stream():
        try:
            async for event in analyze_eshop(request.url):
                payload = json.dumps(event, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except Exception as e:
            logger.exception("Neočekávaná chyba")
            error = json.dumps({"type": "error", "message": f"Interní chyba: {e}"}, ensure_ascii=False)
            yield f"data: {error}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",   # Vypne buffering v nginx
            "Connection": "keep-alive",
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "hlidac-slev"}
