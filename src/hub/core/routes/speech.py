"""Ручки быстрого работника распознавания (Ф79-1, docs/architecture.md).

Работник — служба `hub transcribe run` с `device=cuda` (ноутбук, к ядру — через `hub-link`), bearer со скоупом
`transcribe` (та же зависимость `_worker`, что у `/transcripts/lease`; кука интерфейса — 403):
- `POST /speech/lease {wait_s ≤ 25}` -> 200 `{job_id, lang, audio_b64}` или 204 — долгий опрос;
- `POST /speech/{job_id}/result {text}` или `{error}` -> 204; задание снято, не выдавалось или выдано другому
  токену — 409 `stale`.

Посредник — `app.state.speech_broker` (`speech/local.py`), только память процесса ядра. Звук и текст в журнал
не пишутся.
"""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ...speech.local import ERROR_MAX, TEXT_MAX, broker_of
from ..security import Principal
from .transcripts import _worker_dep

public_router = APIRouter(tags=["speech"])

_JOB_ID_MAX = 64


class SpeechLeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait_s: float = 0.0


class SpeechResultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=TEXT_MAX)
    error: str | None = Field(default=None, max_length=ERROR_MAX)


def _broker(request: Request):
    broker = broker_of(request.app.state)
    if broker is None:
        raise HTTPException(503, "speech_unavailable")
    return broker


@public_router.post("/speech/lease")
async def speech_lease(body: SpeechLeaseIn, request: Request, principal: Principal = _worker_dep):
    broker = _broker(request)
    job = await broker.lease(body.wait_s, token_id=principal.token_id, disconnected=request.is_disconnected)
    if job is None:
        return Response(status_code=204)
    return job


@public_router.post("/speech/{job_id}/result")
async def speech_result(
    job_id: str, body: SpeechResultIn, request: Request, principal: Principal = _worker_dep
) -> Response:
    if (body.text is None) == (body.error is None):
        raise HTTPException(422, "invalid request")
    broker = _broker(request)
    if len(job_id) > _JOB_ID_MAX or not broker.result(
        job_id, token_id=principal.token_id, text=body.text, error=body.error
    ):
        raise HTTPException(409, "stale")
    return Response(status_code=204)
