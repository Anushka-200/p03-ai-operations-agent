"""FastAPI app: the Twilio webhook and a health check. No tunnel, no public URL: tests post signed fakes."""

import logging
from collections.abc import Callable
from urllib.parse import parse_qsl

import psycopg
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from app import inbound
from app.adapters.factory import make_context
from app.api.twilio_signature import is_valid
from app.log import setup_logging
from app.settings import Settings, get_settings
from app.tools.types import ToolContext

logger = logging.getLogger(__name__)
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'  # "got it, no immediate reply"


def default_agent_fn():
    """Imported lazily so the webhook (and its tests) work before/without the agent graph."""
    try:
        from app.agent.run import run_turn
    except ImportError:
        return None
    return run_turn


def create_app(
    settings: Settings | None = None, agent_fn: Callable | None = None, make_ctx: Callable | None = None
) -> FastAPI:
    s = settings or get_settings()
    setup_logging()
    app = FastAPI(title="CoolBreeze ops agent")
    agent = agent_fn if agent_fn is not None else default_agent_fn()

    def build_ctx(conn: psycopg.Connection) -> ToolContext:
        if make_ctx:
            return make_ctx(conn)
        return make_context(conn, s)

    def background(saved: inbound.Saved, body: str) -> None:
        # Own connection: the request's connection is gone by the time this runs.
        with psycopg.connect(s.database_url) as conn:
            try:
                inbound.process_inbound(build_ctx(conn), saved, body, agent)
            except Exception:
                conn.rollback()
                logger.exception("processing failed; message stays saved with processed_at NULL")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "dry_run": str(s.dry_run).lower()}

    @app.post("/webhooks/twilio")
    async def twilio_webhook(request: Request, tasks: BackgroundTasks) -> Response:
        raw = (await request.body()).decode()
        params = dict(parse_qsl(raw, keep_blank_values=True))
        if not is_valid(
            s.twilio_webhook_url, params, request.headers.get("X-Twilio-Signature"),
            s.twilio_auth_token.get_secret_value(),
        ):  # fmt: skip
            logger.warning("rejected webhook: bad signature")
            raise HTTPException(status_code=403, detail="invalid signature")
        sid, sender, body = params.get("MessageSid"), params.get("From"), params.get("Body", "")
        if not sid or not sender:
            raise HTTPException(status_code=400, detail="MessageSid and From are required")
        with psycopg.connect(s.database_url) as conn:
            saved = inbound.save_inbound(conn, sid, sender, body)
        if not saved.duplicate:  # a repeated delivery is acknowledged but not processed again
            tasks.add_task(background, saved, body)
        return Response(EMPTY_TWIML, media_type="application/xml")

    return app
