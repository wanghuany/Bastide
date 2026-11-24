"""Official MCP SDK stdio adapter. No DB/password access; calls the unlocked local API."""

import base64
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from app.core.errors import DomainError
from app.features.personal.config import resolve_vault_path
from app.features.personal.machine_access import MachineAccess
from app.features.personal.schemas import HoldingValue


def start_authorized_service(base: str, vault: Path) -> bool:
    """Launch only a previously authorized local vault; never pass a password or MCP token."""
    if os.name != "nt":
        return False
    python = Path(sys.executable).with_name("pythonw.exe")
    arguments = [
        str(python),
        "-m",
        "app.personal_cli",
        "--no-browser",
        "--keep-running",
        "--vault",
        str(vault),
        "--port",
        str(urlparse(base).port or 80),
    ]
    backend = str(Path(__file__).resolve().parents[1])

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    # MCP clients terminate their entire Windows child job when disconnecting. Ask the
    # local Windows process provider to own this separately authorized background service.
    environment = [
        "BASTIDE_DB_PATH=:memory:",
        "PYTHONPATH=" + backend,
        *[
            f"{key}={os.environ[key]}"
            for key in ("LOCALAPPDATA", "SYSTEMROOT", "PATH")
            if key in os.environ
        ],
    ]
    script = (
        "$ErrorActionPreference='Stop'\n"
        "$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{"
        "ShowWindow=[uint16]0; EnvironmentVariables=[string[]]@("
        + ",".join(map(literal, environment))
        + ")}\n"
        "$result=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{"
        "CommandLine="
        + literal(subprocess.list2cmdline(arguments))
        + ";CurrentDirectory="
        + literal(backend)
        + ";ProcessStartupInformation=$startup}\nif($result.ReturnValue -ne 0){exit 1}\n"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
        ],
        timeout=20,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        return False
    with httpx.Client(trust_env=False, timeout=0.5) as client:
        for _ in range(80):
            try:
                if client.get(base + "/api/window").status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    return False


mcp = FastMCP(
    "Bastide Personal",
    instructions=(
        "本机个人记账工具。金额使用整数分，先读取账户/分类目录。写入操作由用户请求触发；"
        "父母给用户的钱记 income，分类父母转入；给父母的钱记 expense，分类家庭支持/给父母。"
        "transfer 仅用于用户自己的账户互转。分析个人开销时设置 expense_scope=personal。"
        "累计统计基准从2026-07-15起（含当天）。基金估值是资产变化，不记为收入/支出；"
        "每周调用record_weekly_valuations填所有基金新金额，不需要百分比。期间买卖先用record_fund_trade登记。"
        "零花钱转存用transfer_allowance_remainder，先让用户确认已真实转账，不自动卖基金。"
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
            try:
                health = client.get(base + "/api/window", timeout=1)
            except httpx.ConnectError, httpx.ConnectTimeout:
                try:
                    vault = resolve_vault_path()
                except DomainError, OSError:
                    return {
                        "error": {
                            "code": "VAULT_CONFIGURATION_MISSING",
                            "message": (
                                "找不到本机账本配置。请检查 PersonalLedger/personal.local.json。"
                            ),
                        }
                    }
                if not MachineAccess(vault).enabled:
                    return {
                        "error": {
                            "code": "MACHINE_ACCESS_NOT_CONFIGURED",
                            "message": (
                                "本机自动解锁尚未授权。请打开账本网页，"
                                "在“安全与智能体”完成本机后台授权。"
                            ),
                        }
                    }
                if not start_authorized_service(base, vault):
                    return {
                        "error": {
                            "code": "LOCAL_SERVICE_START_FAILED",
                            "message": (
                                "本机后台启动失败。请在当前 Windows 用户桌面"
                                "打开账本、解锁并检查本机授权。"
                            ),
                        }
                    }
                try:
                    health = client.get(base + "/api/window", timeout=5)
                except httpx.HTTPError:
                    return {
                        "error": {
                            "code": "LOCAL_SERVICE_START_FAILED",
                            "message": (
                                "已请求启动本机账本服务，但服务未能就绪。"
                                "请检查 Windows 登录任务或在账本界面启动。"
                            ),
                        }
                    }
            if health.status_code != 200:
                return {
                    "error": {
                        "code": "LOCAL_SERVICE_MISMATCH",
                        "message": "8899 端口上的服务不是个人账本。请确认账本服务地址和端口。",
                    }
                }
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
    except httpx.HTTPError, OSError, subprocess.SubprocessError, DomainError:
        return {
            "error": {
                "code": "LOCAL_APP_UNAVAILABLE",
                "message": "无法连接本机账本服务。请确认服务正在运行，然后重新调用。",
            }
        }


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
    expense_scope: Literal["all", "personal"] = "all",
) -> dict:
    """本地汇总，金额为分。expense_scope=personal 排除给父母等非个人支出；all 保留全部。

    自己账户之间的配对转账不计收支。父母转入为收入，给父母为支出，不使用 transfer。
    """
    params = {"report": report, "start": start, "end": end, "expense_scope": expense_scope}
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


@mcp.tool()
def finance_since_start() -> dict:
    """统计2026-07-15（含当天）至今天所有账户（含归档）的收支，排除投资买卖和自有账户转账。"""
    return call("GET", "/api/agent/finance/since-start")


@mcp.tool()
def get_account_portfolio(account_id: str) -> dict:
    """返回账户现金、各基金最新金额、估值变化与本月零花钱结余额度；不含逐笔现金流水。"""
    return call("GET", f"/api/agent/accounts/{account_id}/portfolio")


@mcp.tool()
def add_fund_holding(
    account_id: str, name: str, opening_date: str, opening_value_minor: int, request_id: str
) -> dict:
    """登记已有基金期初金额（分），不扣现金。新买基金应先以0建立持仓再记录买入，勿重复计现金。"""
    return call(
        "POST",
        f"/api/agent/accounts/{account_id}/holdings",
        {
            "name": name,
            "opening_date": opening_date,
            "opening_value_minor": opening_value_minor,
            "request_id": request_id,
        },
    )


@mcp.tool()
def record_weekly_valuations(
    account_id: str, booked_date: str, holdings: list[HoldingValue], request_id: str
) -> dict:
    """填写该账户全部基金的新总金额（整数分），不是涨幅百分比。期间有买卖先登记买卖，再填金额。"""
    return call(
        "POST",
        f"/api/agent/accounts/{account_id}/valuations",
        {
            "booked_date": booked_date,
            "holdings": [x.model_dump() for x in holdings],
            "request_id": request_id,
        },
    )


@mcp.tool()
def record_fund_trade(
    holding_id: str,
    kind: Literal["buy", "sell"],
    booked_date: str,
    amount_minor: int,
    request_id: str,
) -> dict:
    """登记真实基金买入/赎回，金额单位分。现金与基金间转换，不计收入/开销；先核对最新市值。"""
    return call(
        "POST",
        f"/api/agent/holdings/{holding_id}/trades",
        {
            "kind": kind,
            "booked_date": booked_date,
            "amount_minor": amount_minor,
            "request_id": request_id,
        },
    )


@mcp.tool()
def transfer_allowance_remainder(
    account_id: str,
    destination_account_id: str,
    booked_date: str,
    amount_minor: int,
    request_id: str,
) -> dict:
    """用户已完成真实转账后，登记零花钱到理财通的转存。请先展示建议并确认金额；不会自动赎回基金。"""
    return call(
        "POST",
        f"/api/agent/accounts/{account_id}/savings-transfers",
        {
            "destination_account_id": destination_account_id,
            "booked_date": booked_date,
            "amount_minor": amount_minor,
            "request_id": request_id,
        },
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
