from fastapi import APIRouter, HTTPException, status
from app.contracts.mindmap import MapRecord, SaveRequest, UpdateRequest
from app.mindmaps import repository

router = APIRouter(prefix="/mindmaps", tags=["导图管理"])

@router.get("", response_model=list[MapRecord])
def list_maps():
    return repository.list_maps()

@router.post("", response_model=MapRecord, status_code=status.HTTP_201_CREATED)
def create_map(body: SaveRequest):
    return repository.create_map(body.content)

@router.get("/{map_id}", response_model=MapRecord)
def get_map(map_id: str):
    result = repository.get_map(map_id)
    if result is None:
        raise HTTPException(status_code=404, detail="导图不存在")
    return result

@router.put("/{map_id}", response_model=MapRecord)
def update_map(map_id: str, body: UpdateRequest):
    result = repository.update_map(map_id, body.version, body.content)
    if result == "missing":
        raise HTTPException(status_code=404, detail="导图不存在")
    if result == "conflict":
        raise HTTPException(status_code=409, detail="导图已被修改，请重新打开后再保存")
    return repository.get_map(map_id)
