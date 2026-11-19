"""Official MCP SDK stdio adapter. No DB/password access; calls the unlocked local API."""

import os
from typing import Literal
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "Bastide Personal",
    instructions=(
        "本机个人记账工具。金额使用整数分，先读取账户/分类目录。写入操作由用户请求触发；"
        "为每次记账生成唯一 request_id，网络重试保持原 request_id，避免重复入账。"
        "分析使用 run_analysis，只返回本地 Python 计算的摘要，不要索取数据库或要求用户上传账单。"
        "批量分类先预览、向用户说明影响数量，获得确认后再提交预览。"
        "账本锁定时请提示用户在电脑上的账本界面解锁，不要索取密码或恢复密钥。"
    ),
)


def call(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    """Never send credentials through proxies, redirects, non-loopback URLs, or logs."""
    base = os.environ.get("BASTIDE_PERSONAL_URL", "http://127.0.0.1:8899").rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return {
            "error": {
                "code": "INVALID_LOCAL_URL",
                "message": "只能连接本机 127.0.0.1 或 localhost。",
            }
        }
    token = os.environ.get("BASTIDE_PERSONAL_TOKEN")
    if not token:
        return {"error": {"code": "TOKEN_MISSING", "message": "请在账本界面生成并配置 MCP 令牌。"}}
    try:
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
            response = client.request(
                method,
                base + path,
                json=payload,
                params=params,
                headers={"Authorization": "Bearer " + token},
            )
        if response.status_code >= 300:
            try:
                return {"error": response.json().get("error", {"message": "本地接口拒绝了请求。"})}
            except ValueError:
                return {"error": {"code": "API_ERROR", "message": "本地接口返回异常。"}}
        return response.json()
    except httpx.HTTPError:
        return {"error": {"code": "LOCAL_APP_UNAVAILABLE", "message": "请先启动并解锁本地账本。"}}


@mcp.tool()
def list_accounts_and_categories() -> dict:
    """列出账户 ID/名称和分类 ID/层级；不读取余额或逐笔账单。"""
    return call("GET", "/api/agent/catalog")


@mcp.tool()
def create_transaction(
    account_id: str,
    booked_date: str,
    kind: Literal["expense", "income", "transfer"],
    amount_minor: int,
    request_id: str,
    category_id: str | None = None,
    merchant: str = "",
    note: str = "",
    destination_account_id: str | None = None,
) -> dict:
    """记一笔收支或配对转账。金额为正整数分，日期为 YYYY-MM-DD；不确定的日期请先询问用户。"""
    return call(
        "POST",
        "/api/agent/transactions",
        {
            "account_id": account_id,
            "booked_date": booked_date,
            "kind": kind,
            "amount_minor": amount_minor,
            "request_id": request_id,
            "category_id": category_id,
            "merchant": merchant,
            "note": note,
            "destination_account_id": destination_account_id,
        },
    )


@mcp.tool()
def update_transaction(
    transaction_id: str,
    account_id: str,
    booked_date: str,
    kind: Literal["expense", "income", "transfer"],
    amount_minor: int,
    request_id: str,
    category_id: str | None = None,
    merchant: str = "",
    note: str = "",
    destination_account_id: str | None = None,
) -> dict:
    """完整更新用户指定的一笔账目（包括转账两端）；返回新的账目 ID 和可撤销操作 ID。"""
    return call(
        "PUT",
        "/api/agent/transactions/" + transaction_id,
        {
            "account_id": account_id,
            "booked_date": booked_date,
            "kind": kind,
            "amount_minor": amount_minor,
            "request_id": request_id,
            "category_id": category_id,
            "merchant": merchant,
            "note": note,
            "destination_account_id": destination_account_id,
        },
    )


@mcp.tool()
def run_analysis(
    report: Literal[
        "summary", "monthly", "annual", "category_ranking", "merchant_ranking", "year_over_year"
    ],
    start: str,
    end: str,
    category_id: str | None = None,
    account_id: str | None = None,
) -> dict:
    """在本地 Python 中计算统计，只返回汇总结果。金额单位为分；转账不计入收支。"""
    params = {"report": report, "start": start, "end": end}
    if category_id:
        params["category_id"] = category_id
    if account_id:
        params["account_id"] = account_id
    return call("GET", "/api/agent/analysis", params=params)


@mcp.tool()
def preview_category_change(source_category_id: str, target_category_id: str) -> dict:
    """预览原分类中直接归类账目的迁移笔数，不包含子分类。不会修改交易。"""
    return call(
        "POST",
        "/api/agent/categories/preview",
        {"source_category_id": source_category_id, "target_category_id": target_category_id},
    )


@mcp.tool()
def commit_category_change(preview_id: str, user_confirmed: bool = False) -> dict:
    """用户已确认预览影响后提交批量分类，30 分钟过期；失败需重新预览。"""
    if not user_confirmed:
        return {
            "error": {"code": "CONFIRM_REQUIRED", "message": "请先向用户说明影响笔数并获得确认。"}
        }
    return call("POST", "/api/agent/categories/commit", {"preview_id": preview_id})


@mcp.tool()
def query_transactions(start: str, end: str, page: int = 1) -> dict:
    """读取逐笔账单，仅在界面明确授予 details 权限时可用。通常应改用 run_analysis。"""
    return call("GET", "/api/agent/transactions", params={"start": start, "end": end, "page": page})


if __name__ == "__main__":
    mcp.run(transport="stdio")
