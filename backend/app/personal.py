"""Chinese personal-edition application factory, separate from the original Flutter sidecar."""

import asyncio
import base64
import binascii
import json
import secrets
import sys
import time
from contextlib import asynccontextmanager, suppress
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import AuthError, DomainError, ValidationError, register_exception_handlers
from app.features.personal.config import resolve_vault_path
from app.features.personal.schemas import (
    AccountInput,
    BulkCategory,
    CategoryInput,
    Confirm,
    EntryInput,
    ImportInput,
    Password,
    RestoreInput,
    RuleInput,
    TokenInput,
    Unlock,
)
from app.features.personal.service import PersonalService
from app.features.personal.vault import Vault

STATIC = Path(__file__).parent / "features" / "personal" / "static"
IDLE_SECONDS = 15 * 60
COOKIE = "bastide_personal_session"


class LocalGuard:
    """Reject rebinding/CSRF and oversized bodies before parsing or dispatching."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode().lower(): value.decode() for key, value in scope["headers"]}
        host = headers.get("host", "")
        hostname = host.split(":")[0].lower()
        origin = headers.get("origin")
        if hostname not in ("127.0.0.1", "localhost") or (origin and origin != "http://" + host):
            await JSONResponse(
                {"error": {"code": "LOCAL_ONLY", "message": "仅允许同源本机访问。"}}, 403
            )(scope, receive, send)
            return
        maximum = 181_000_000 if scope["path"] == "/api/restore" else 5_000_000
        try:
            if int(headers.get("content-length", "0")) > maximum:
                raise ValueError
        except ValueError:
            await JSONResponse({"error": {"code": "TOO_LARGE", "message": "请求文件过大。"}}, 413)(
                scope, receive, send
            )
            return
        consumed = 0

        async def bounded_receive() -> Message:
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > maximum:
                raise ValidationError("请求文件过大。")
            return message

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; "
                        b"style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                        b"connect-src 'self'; frame-ancestors 'none'; "
                        b"form-action 'self'; base-uri 'none'",
                    ),
                ]
            await send(message)

        await self.app(scope, bounded_receive, secured_send)


class DesktopState:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.service = PersonalService(vault)
        self.token: str | None = None
        self.last_activity = 0.0
        self.failures = 0
        self.next_unlock = 0.0

    def authenticated(self, request: Request) -> bool:
        supplied = request.cookies.get(COOKIE, "")
        return bool(
            self.vault.unlocked and self.token and secrets.compare_digest(supplied, self.token)
        )

    def expire(self) -> None:
        with self.vault.mutex:
            if self.vault.unlocked and time.monotonic() - self.last_activity >= IDLE_SECONDS:
                self.lock()

    def lock(self) -> None:
        self.vault.lock()
        self.token = None

    def login(self, credential: str, recovery: bool = False, create: bool = False) -> dict:
        with self.vault.mutex:
            if time.monotonic() < self.next_unlock:
                raise AuthError("尝试过于频繁，请稍后再试。")
            try:
                result = {}
                if create:
                    result["recovery_key"] = self.vault.create(credential)
                elif self.vault.unlocked:
                    Vault.decode(self.vault.path.read_bytes(), credential, recovery)
                else:
                    self.vault.unlock(credential, recovery)
                self.service.bootstrap()
                self.token = secrets.token_urlsafe(32)
                self.last_activity = time.monotonic()
                self.failures, self.next_unlock = 0, 0
                return result
            except AuthError:
                self.failures += 1
                self.next_unlock = time.monotonic() + min(30, 2 ** min(self.failures - 1, 5))
                raise


def create_personal_app(vault_path: Path | None = None) -> FastAPI:
    """Build a single-process local app without starting the original plaintext datastore."""
    path = resolve_vault_path(vault_path)
    state = DesktopState(Vault(path))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def auto_lock() -> None:
            while True:
                await asyncio.sleep(10)
                await asyncio.to_thread(state.expire)

        task = asyncio.create_task(auto_lock())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            state.lock()

    app = FastAPI(title="Bastide 本地账本", version="1.0.0", lifespan=lifespan)
    app.state.personal = state
    app.add_middleware(LocalGuard)
    register_exception_handlers(app)

    # Pydantic's default error response echoes rejected inputs, which might include passwords.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def input_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "输入格式不正确，请检查必填项、日期和整数金额。",
                    "details": [
                        {"field": ".".join(str(x) for x in e["loc"]), "type": e["type"]}
                        for e in exc.errors()
                    ],
                }
            },
            422,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "操作未完成，原有数据已保留。请检查磁盘空间或联系维护者。",
                }
            },
            500,
        )

    def desktop(request: Request) -> PersonalService:
        state.expire()
        if not state.authenticated(request):
            raise AuthError("请先在本地界面解锁账本。")
        state.last_activity = time.monotonic()
        return state.service

    Desktop = Annotated[PersonalService, Depends(desktop)]

    def agent_permission(request: Request, required: str) -> PersonalService:
        state.expire()
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise AuthError("需要 Agent Bearer 令牌。")
        scopes = state.service.invoke("token_scopes", authorization[7:])
        if required not in scopes:
            error = DomainError("该 Agent 令牌未获此项权限。")
            error.code, error.status_code = "SCOPE_DENIED", 403
            raise error
        # Agent activity does not keep a desktop session unlocked indefinitely.
        return state.service

    def agent_analysis(request: Request) -> PersonalService:
        return agent_permission(request, "analysis")

    def agent_write(request: Request) -> PersonalService:
        return agent_permission(request, "write")

    def agent_details(request: Request) -> PersonalService:
        return agent_permission(request, "details")

    Analysis = Annotated[PersonalService, Depends(agent_analysis)]
    Writer = Annotated[PersonalService, Depends(agent_write)]
    Details = Annotated[PersonalService, Depends(agent_details)]

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status(request: Request) -> dict:
        state.expire()
        return {
            "initialized": state.vault.path.exists(),
            "unlocked": state.vault.unlocked,
            "authenticated": state.authenticated(request),
            "idle_lock_minutes": IDLE_SECONDS // 60,
        }

    def login_response(result: dict) -> JSONResponse:
        response = JSONResponse({"ok": True, **result})
        response.set_cookie(COOKIE, state.token or "", httponly=True, samesite="strict", path="/")
        return response

    @app.post("/api/setup")
    def setup(data: Password) -> JSONResponse:
        return login_response(state.login(data.password, create=True))

    @app.post("/api/unlock")
    def unlock(data: Unlock) -> JSONResponse:
        return login_response(state.login(data.credential, data.recovery))

    @app.post("/api/lock")
    def lock(service: Desktop) -> dict:
        state.lock()
        return {"locked": True}

    @app.post("/api/password")
    def password(data: Password, service: Desktop) -> dict:
        state.vault.change_password(data.password)
        return {"ok": True}

    @app.get("/api/overview")
    def overview(service: Desktop, start: date, end: date) -> dict:
        return service.overview(start, end)

    @app.get("/api/accounts")
    def accounts(service: Desktop) -> list[dict]:
        return service.invoke("accounts")

    @app.post("/api/accounts", status_code=201)
    def create_account(data: AccountInput, service: Desktop) -> dict:
        return service.invoke("save_account", data, write=True)

    @app.put("/api/accounts/{account_id}")
    def update_account(account_id: str, data: AccountInput, service: Desktop) -> dict:
        return service.invoke("save_account", data, account_id, write=True)

    @app.get("/api/categories")
    def categories(service: Desktop) -> list[dict]:
        return service.invoke("categories")

    @app.post("/api/categories", status_code=201)
    def create_category(data: CategoryInput, service: Desktop) -> dict:
        return service.invoke("save_category", data, write=True)

    @app.put("/api/categories/{category_id}")
    def update_category(category_id: str, data: CategoryInput, service: Desktop) -> dict:
        return service.invoke("save_category", data, category_id, write=True)

    @app.delete("/api/categories/{category_id}")
    def delete_category(category_id: str, service: Desktop) -> dict:
        service.invoke("delete_category", category_id, write=True)
        return {"ok": True}

    @app.get("/api/transactions")
    def transactions(
        service: Desktop,
        start: date | None = None,
        end: date | None = None,
        category_id: str | None = None,
        account_id: str | None = None,
        q: str = Query(default="", max_length=200),
        page: int = Query(default=1, ge=1),
    ) -> dict:
        return service.invoke(
            "entries",
            start=start,
            end=end,
            category_id=category_id,
            account_id=account_id,
            q=q,
            page=page,
        )

    @app.post("/api/transactions", status_code=201)
    def create_entry(data: EntryInput, service: Desktop) -> dict:
        return service.invoke("create_entry", data, write=True)

    @app.get("/api/transactions/{entry_id}")
    def entry_detail(entry_id: str, service: Desktop) -> dict:
        return service.invoke("entry_detail", entry_id)

    @app.put("/api/transactions/{entry_id}")
    def update_entry(entry_id: str, data: EntryInput, service: Desktop) -> dict:
        return service.invoke("update_entry", entry_id, data, write=True)

    @app.delete("/api/transactions/{entry_id}")
    def delete_entry(entry_id: str, service: Desktop) -> dict:
        return service.invoke("delete_entry", entry_id, write=True)

    @app.post("/api/categories/preview")
    def category_preview(data: BulkCategory, service: Desktop) -> dict:
        return service.invoke(
            "preview_category", data.source_category_id, data.target_category_id, write=True
        )

    @app.post("/api/categories/commit")
    def category_commit(data: Confirm, service: Desktop) -> dict:
        return service.invoke("commit_category", data.preview_id, write=True)

    @app.get("/api/history")
    def history(service: Desktop) -> list[dict]:
        return service.invoke("history")

    @app.post("/api/history/{action_id}/undo")
    def undo(action_id: str, service: Desktop) -> dict:
        return service.invoke("undo", action_id, write=True)

    @app.get("/api/rules")
    def rules(service: Desktop) -> list[dict]:
        return service.invoke("rules")

    @app.post("/api/rules", status_code=201)
    def create_rule(data: RuleInput, service: Desktop) -> dict:
        return service.invoke("save_rule", data, write=True)

    @app.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: str, service: Desktop) -> dict:
        service.invoke("delete_rule", rule_id, write=True)
        return {"ok": True}

    @app.get("/api/analysis")
    def analysis(
        service: Desktop,
        start: date,
        end: date,
        report: str = "summary",
        category_id: str | None = None,
        account_id: str | None = None,
    ) -> dict:
        return service.analysis(report, start, end, category_id, account_id)

    @app.post("/api/imports/preview")
    def import_preview(data: ImportInput, service: Desktop) -> dict:
        return service.preview_import(data)

    @app.post("/api/imports/commit")
    def import_commit(data: Confirm, service: Desktop) -> dict:
        return service.commit_import(data.preview_id)

    @app.get("/api/backup")
    def backup(service: Desktop) -> Response:
        filename = f"finance-{date.today():%Y-%m-%d}.bfin"
        return Response(
            state.vault.backup(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/restore")
    def restore(data: RestoreInput) -> dict:
        # A locked vault still needs its password or 256-bit recovery secret to restore.
        if state.vault.unlocked:
            raise ValidationError("请先锁定账本后再恢复备份。")
        if time.monotonic() < state.next_unlock:
            raise AuthError("尝试过于频繁，请稍后再试。")
        try:
            blob = base64.b64decode(data.content_base64, validate=True)
            state.vault.restore(blob, data.credential, data.recovery)
        except (ValueError, binascii.Error) as exc:
            raise ValidationError("备份文件编码不正确。") from exc
        except AuthError:
            state.failures += 1
            state.next_unlock = time.monotonic() + min(30, 2 ** min(state.failures, 5))
            raise
        state.token = None
        return {"ok": True, "message": "备份已恢复，请解锁。"}

    @app.get("/api/export")
    def export(service: Desktop, confirm_plaintext: bool = False) -> Response:
        if not confirm_plaintext:
            raise ValidationError("请明确确认明文导出。同步/迁移应优先使用加密备份。")
        return Response(
            json.dumps(service.export(), ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="finance-plaintext.json"'},
        )

    @app.post("/api/agent-token")
    def agent_token(data: TokenInput, request: Request, service: Desktop) -> dict:
        issued = service.invoke("issue_token", data.allow_writes, data.allow_details, write=True)
        backend = Path(__file__).resolve().parents[1]
        issued["config"] = {
            "mcpServers": {
                "bastide-personal": {
                    "command": sys.executable,
                    "args": ["-m", "app.personal_mcp"],
                    "env": {
                        "PYTHONPATH": str(backend),
                        "BASTIDE_PERSONAL_URL": str(request.base_url).rstrip("/"),
                        "BASTIDE_PERSONAL_TOKEN": issued["token"],
                    },
                }
            }
        }
        return issued

    @app.delete("/api/agent-token")
    def revoke_token(service: Desktop) -> dict:
        service.invoke("set_setting", "agent", {"hash": "revoked", "scopes": []}, write=True)
        return {"ok": True}

    @app.get("/api/agent/catalog")
    def agent_catalog(service: Analysis) -> dict:
        return {
            "accounts": [
                {"id": a["id"], "name": a["name"]}
                for a in service.invoke("accounts")
                if not a["archived"]
            ],
            "categories": service.invoke("categories"),
            "currency": "CNY",
            "amount_unit": "fen",
        }

    @app.get("/api/agent/analysis")
    def agent_report(
        service: Analysis,
        start: date,
        end: date,
        report: str = "monthly",
        category_id: str | None = None,
        account_id: str | None = None,
    ) -> dict:
        return service.analysis(report, start, end, category_id, account_id)

    @app.post("/api/agent/transactions", status_code=201)
    def agent_create(data: EntryInput, service: Writer) -> dict:
        return service.invoke("create_entry", data, write=True)

    @app.put("/api/agent/transactions/{entry_id}")
    def agent_update(entry_id: str, data: EntryInput, service: Writer) -> dict:
        return service.invoke("update_entry", entry_id, data, write=True)

    @app.get("/api/agent/transactions")
    def agent_transactions(
        service: Details, start: date, end: date, page: int = Query(default=1, ge=1)
    ) -> dict:
        return service.invoke("entries", start=start, end=end, page=page)

    @app.post("/api/agent/categories/preview")
    def agent_category_preview(data: BulkCategory, service: Writer) -> dict:
        return service.invoke(
            "preview_category", data.source_category_id, data.target_category_id, write=True
        )

    @app.post("/api/agent/categories/commit")
    def agent_category_commit(data: Confirm, service: Writer) -> dict:
        return service.invoke("commit_category", data.preview_id, write=True)

    app.mount("/static", StaticFiles(directory=STATIC), name="personal-static")
    return app
