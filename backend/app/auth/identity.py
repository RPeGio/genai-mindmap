"""每个请求"当前是谁"，以及会话 cookie 的读写。

**为什么用 `ContextVar` 而不是把身份塞进 `request.state`**：需要身份的地方不只有
路由函数，还有 `settings.store.resolve()` 这类**被深层函数调用**的地方（它在每次
模型调用的开头被调用，而那时手上只有"这次请求"，没有 FastAPI 的 `Request`）。
`ContextVar` 让"当前用户"在整个调用栈里可读，且不需要给五个调用点改签名——这是
后面 BYOK 那一批能不动 `_config()` 的前提。`asyncio.to_thread` 会复制当前上下文，
所以 `ingestion/vision_pdf.py` 里那些线程调用也能读到同一个身份。

**为什么是纯 ASGI 中间件，不是 `BaseHTTPMiddleware`**：后者把下游包进一个独立
任务，历史上对 SSE（本项目的主链路）有两处坑——一是它会等下游任务结束，二是
`http.disconnect` 的传递不完全。这里只需要"进入时解析一次、退出时复位"，十几行
纯 ASGI 就够，且对流式响应零影响。

**这一层不拒绝任何请求**。它只回答"是谁"，`kind` 可能是 `user` / `guest` /
`anonymous`。门禁在后面的批次里按接口逐个打开——一次全打开会让合并当天所有人
（包括自己和队友）都进不去，而"进不去"没有旁路可验证。
"""
from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from uuid import uuid4

from app.auth import store

logger = logging.getLogger(__name__)

#: 当前请求的身份。缺省是"既没登录也没游客 cookie"。
#:
#: 用 `ContextVar` 而不是模块级全局：同一进程里并发的两个请求必须看到各自的
#: 身份，否则会在多用户场景下把 A 的密钥用在 B 的请求上——那是最坏的一种 bug，
#: 而且它只在并发下出现。
_current: ContextVar[store.Identity] = ContextVar("auth_identity", default=store.Identity())

#: 游客 id 的形状。只接受 uuid4 的十六进制串，别的一律当作"没有游客 cookie"：
#: 这个值会被写进额度计数表，接受任意字符串等于接受任意长度的键。
_GUEST_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def current_identity() -> store.Identity:
    """当前请求的身份。在任何地方（含被深层调用的库函数）都可调用。"""
    return _current.get()


def current_user() -> store.User | None:
    """当前登录的人；没登录返回 None。"""
    return _current.get().user


# ---------------------------------------------------------------- cookie

#: `Secure` 属性怎么判：只有真的走 https 才加。
#:
#: 不能无条件加：本机开发是 `http://127.0.0.1:5173`，浏览器对 `Secure` cookie 的
#: 宽容度各版本不一致（Chrome 对 localhost 放行，但对 `127.0.0.1` 的旧版本不放），
#: 无条件加会让"本机跑起来登录不了"变成一个只在部分浏览器上出现的故障。
#:
#: 反向代理后面由 `X-Forwarded-Proto` 判断：`deploy/nginx.conf` 会设这个头，
#: 而 uvicorn 是否信任它取决于启动参数，所以两个来源都看。
_FORWARDED_PROTO = "x-forwarded-proto"


def _is_https(scope: dict) -> bool:
    if scope.get("scheme") == "https":
        return True
    for name, value in scope.get("headers") or ():
        if name.decode("latin-1").lower() == _FORWARDED_PROTO:
            return value.decode("latin-1").split(",")[0].strip().lower() == "https"
    return False


def _cookies(scope: dict) -> dict[str, str]:
    """从 ASGI scope 里解析 cookie。

    自己解而不是用 `starlette.requests.Request(scope).cookies`：中间件里没有
    `Request` 对象，构造一个只为了拿 cookie 反而更绕；而 cookie 的解析规则很简单
    （`;` 分隔、第一个 `=` 切分），不需要一整个库。
    """
    found: dict[str, str] = {}
    for name, value in scope.get("headers") or ():
        if name.decode("latin-1").lower() != "cookie":
            continue
        for chunk in value.decode("latin-1").split(";"):
            key, sep, raw = chunk.strip().partition("=")
            if not sep or not key or key in found:
                continue
            found[key] = raw
    return found


def cookie_header(name: str, value: str, *, scope: dict, max_age: int | None) -> str:
    """拼一条 `Set-Cookie`。

    `HttpOnly` + `SameSite=Strict` 是安全要求（用户拍板）：前者让 JS 拿不到会话
    token（XSS 就偷不走），后者让跨站 POST 不携带它（CSRF 的第一道防线）。代价是
    "从别的站点点链接过来会显示未登录"——这个站没有那种入口，代价为零。
    """
    parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Strict"]
    if max_age is not None:
        parts.append(f"Max-Age={int(max_age)}")
    if _is_https(scope):
        parts.append("Secure")
    return "; ".join(parts)


def cleared_cookie(name: str, *, scope: dict) -> str:
    """让浏览器立刻丢掉这条 cookie 的 `Set-Cookie`。"""
    return cookie_header(name, "", scope=scope, max_age=0)


def new_guest_id() -> str:
    """一个新的游客 id。

    `uuid4().hex` 而不是 `token_urlsafe`：这个值不需要保密（它不是一个凭证，
    只是"这台浏览器"的编号），固定 32 位十六进制让 cookie 与库里的列都定长。
    """
    return uuid4().hex


def read_cookie(scope: dict, name: str) -> str:
    """读一条 cookie 的值；没有就是空串。"""
    return (_cookies(scope).get(name) or "").strip()


def _read_guest(cookies: dict[str, str]) -> str | None:
    raw = (cookies.get(store.GUEST_COOKIE) or "").strip().lower()
    return raw if _GUEST_PATTERN.match(raw) else None


def resolve_identity(scope: dict) -> store.Identity:
    """从 cookie 解析身份。

    只有**带着会话 cookie** 时才查库；没有 cookie 的请求（大多数 GET、健康检查、
    静态资源）零数据库开销——这一点很重要，健康检查在 CD 里每个版本都会打。
    """
    cookies = _cookies(scope)
    guest_id = _read_guest(cookies)
    token = (cookies.get(store.SESSION_COOKIE) or "").strip()
    if token:
        user = store.session_user(token)
        if user is not None:
            return store.Identity(user=user, session_hash=store.session_hash(token))
        # cookie 有但不认：可能是被撤销了、过期了、或者库换过了。**不清它**——
        # 清 cookie 需要写响应头，而中间件这一层还没有响应；由 `/auth/me` 顺手清。
        logger.info("会话 cookie 存在但无效，本次按未登录处理")
    return store.Identity(guest_id=guest_id)


class IdentityMiddleware:
    """把 `resolve_identity()` 的结果放进 `ContextVar`，包住整个下游。

    `finally` 里 `reset()` 是必须的：不 reset 的话，ASGI server 复用任务时上一个
    请求的身份会漏到下一个请求——在 `anyio` 的任务模型下这不一定发生，但**依赖
    "它一定不发生"是没必要的赌**。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        identity = resolve_identity(scope)
        token = _current.set(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)


__all__ = [
    "IdentityMiddleware",
    "cleared_cookie",
    "cookie_header",
    "current_identity",
    "current_user",
    "new_guest_id",
    "resolve_identity",
]
