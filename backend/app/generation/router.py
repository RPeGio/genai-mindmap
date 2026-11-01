from fastapi import APIRouter, HTTPException
from app.contracts.mindmap import GenerateRequest, GenerateResponse
from app.generation.service import GenerationError, generate

router = APIRouter(prefix="/generations", tags=["AI 生成"])

@router.post("", response_model=GenerateResponse)
def generate_map(body: GenerateRequest):
    try:
        return generate(body.text)
    except GenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
