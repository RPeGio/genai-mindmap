from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.router import router as agent_router
from app.export.router import router as export_router
from app.generation.router import router as generation_router
from app.ingestion.router import router as ingestion_router
from app.knowledge.router import router as knowledge_router
from app.mindmaps.router import router as mindmap_router
from app.optimization.router import router as optimization_router
from app.pipeline.router import router as pipeline_router

app = FastAPI(title="生成式 AI 思维导图", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)

# 已实现的完整闭环
app.include_router(generation_router, prefix="/api")   # 文本 → 导图（demo / model）
app.include_router(mindmap_router, prefix="/api")      # 保存 / 读取 / 版本
app.include_router(pipeline_router, prefix="/api")     # 多来源 → 导图（四段流水线）
app.include_router(optimization_router, prefix="/api")  # 结构诊断与建议
app.include_router(export_router, prefix="/api")       # 导出 puml

# 已规划、接口已固定、实现待补
app.include_router(ingestion_router, prefix="/api")    # 多模态输入（仅文本可用）
app.include_router(knowledge_router, prefix="/api")    # 知识检索（返回空）
app.include_router(agent_router, prefix="/api")        # AI 代理（返回阶段骨架）


@app.get("/api/health", tags=["基础服务"])
def health():
    return {"status": "ok", "version": app.version}
