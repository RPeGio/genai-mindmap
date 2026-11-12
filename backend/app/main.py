import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.router import router as agent_router
from app.core.errors import register_error_handlers
from app.export.router import router as export_router
from app.ingestion.router import router as ingestion_router
from app.knowledge.router import router as knowledge_router
from app.mindmaps.router import router as mindmap_router
from app.optimization.router import router as optimization_router

def _app_version() -> str:
    """线上版本号。

    由 `deploy/install.sh` 从被打包的 tag 写进 `MINDMAP_VERSION`（systemd 的
    EnvironmentFile），/api/health 原样带出，页面标题旁显示同一个值——这样
    "域名上跑的是不是刚部署的那份"不用登服务器就能判断。

    本机跑源码时没有这个变量，回落 `dev`：写死一个版本号只会让人以为本地也
    是发布版。
    """
    return os.getenv("MINDMAP_VERSION", "").strip() or "dev"


app = FastAPI(title="生成式 AI 思维导图", version=_app_version())
# 所有错误出口统一成 {code, message, detail, request_id}，见 app/core/errors.py
register_error_handlers(app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)

# 已实现的完整闭环
app.include_router(mindmap_router, prefix="/api")      # 保存 / 读取 / 版本
app.include_router(optimization_router, prefix="/api")  # 结构诊断与建议
app.include_router(export_router, prefix="/api")       # 导出 puml
app.include_router(ingestion_router, prefix="/api")    # 多模态输入
app.include_router(agent_router, prefix="/api")        # AI 代理：文本/多来源 → 导图

# 已规划、接口已固定、实现待补
app.include_router(knowledge_router, prefix="/api")    # 知识检索（返回空）

# 生成只留 agent 这一条出口。旧的 `POST /api/generations`（同步、单段文本）与
# `POST /api/pipeline/generate`（多来源、返回阶段轨迹）已删除：前端默认走 agent，
# 它们独有的来源重建、引用核验与结构诊断都已搬进 `agent/tools.py` 的生成收尾，
# 阶段轨迹由 agent 的 `stage` 事件覆盖。留两条平行出口只会让同一件事有两处真相。


@app.get("/api/health", tags=["基础服务"])
def health():
    return {"status": "ok", "version": app.version}
