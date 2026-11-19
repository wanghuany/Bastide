"""Local CSV parsers for WeChat, Alipay, MMEX exports and the documented template.

Files never leave this machine. Ambiguous/non-income-expense rows are skipped with reasons;
refunds are not silently rewritten, and payment-provider transfers require manual review.
"""

import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.errors import ValidationError

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000


def amount_minor(value: str) -> int:
    text = re.sub(r"[¥￥,，\s]", "", value)
    try:
        number = Decimal(text)
        if not number.is_finite() or number == 0 or abs(number) > Decimal("90000000000"):
            raise ValueError
        scaled = number * 100
        if scaled != scaled.to_integral_value():
            raise ValueError
        return abs(int(scaled))
    except (ValueError, InvalidOperation) as exc:
        raise ValidationError("金额必须是非零且最多两位小数的数字。") from exc


def parse_csv(raw: bytes, requested_format: str = "auto") -> dict:
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValidationError("CSV 文件最大 2 MiB。")
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if text is None or "\x00" in text:
        raise ValidationError("请选择 UTF-8 或 GB18030 编码的 CSV 文件；不支持 XLSX。")
    lines = text.splitlines()
    header_index = None
    headers = []
    for index, line in enumerate(lines[:100]):
        candidate = [value.strip().strip("\ufeff") for value in next(csv.reader([line]))]
        if (
            ("交易时间" in candidate or "交易创建时间" in candidate)
            and any("金额" in key for key in candidate)
        ) or (
            ("date" in [x.lower() for x in candidate] or "日期" in candidate)
            and any(key.lower() in ("amount", "金额") for key in candidate)
        ):
            header_index, headers = index, candidate
            break
    if header_index is None:
        raise ValidationError("找不到账单表头，请使用微信/支付宝 CSV 或示例模板。")
    detected = "csv"
    if "交易单号" in headers and "收/支" in headers:
        detected = "wechat"
    elif "交易订单号" in headers or "交易创建时间" in headers or "收/支" in headers:
        detected = "alipay"
    elif "Payee" in headers or "Type" in headers:
        detected = "mmex"
    if (
        requested_format != "auto"
        and detected != requested_format
        and requested_format not in ("csv", "mmex")
    ):
        raise ValidationError("选择的账单类型与文件表头不匹配。")
    records, skipped = [], []
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])), skipinitialspace=True)
    for index, raw_row in enumerate(reader, start=header_index + 2):
        if index - header_index > MAX_IMPORT_ROWS + 1:
            raise ValidationError("一次最多导入 5000 行。")
        row = {
            str(k).strip(): str(v or "").strip().rstrip("\t")
            for k, v in raw_row.items()
            if k is not None
        }
        if not any(row.values()):
            continue

        def cell(*keys: str, values: dict[str, str] = row) -> str:
            return next((values[key] for key in keys if values.get(key)), "")

        try:
            raw_date = cell("date", "Date", "日期", "交易时间", "交易创建时间")
            if not re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", raw_date):
                raise ValidationError("日期需为 YYYY-MM-DD；请检查 MMEX 导出日期格式。")
            booked = date.fromisoformat(raw_date[:10].replace("/", "-"))
            status = cell("当前状态", "交易状态")
            if any(word in status for word in ("关闭", "失败", "撤销", "退款")):
                raise ValidationError("未成功或涉及退款的记录，请核对后手动入账。")
            raw_kind = cell("kind", "Type", "类型", "收/支", "收支", "收/支/其他").casefold()
            if raw_kind in ("expense", "withdrawal", "支出", "支出(-)"):
                kind = "expense"
            elif raw_kind in ("income", "deposit", "收入", "收入(+)"):
                kind = "income"
            else:
                raise ValidationError("转账/不计收支/未知类型，请核对后手动入账。")
            value = cell("amount", "Amount", "金额", "金额(元)", "金额（元）")
            merchant = cell("merchant", "Payee", "商家", "交易对方")[:200]
            note = cell("note", "Notes", "备注", "商品", "商品说明")[:2000]
            provider_id = cell("external_id", "交易单号", "交易订单号", "交易号")
            if provider_id in ("/", "-", "无"):
                provider_id = ""
            records.append(
                {
                    "booked_date": booked.isoformat(),
                    "kind": kind,
                    "amount_minor": amount_minor(value),
                    "merchant": merchant,
                    "note": note,
                    "original_category": cell("category", "Category", "分类", "交易类型"),
                    "external_id": f"{detected}:{provider_id}" if provider_id else None,
                    "line": index,
                }
            )
        except (ValidationError, ValueError) as exc:
            reason = exc.message if isinstance(exc, ValidationError) else "日期格式无效。"
            skipped.append({"line": index, "reason": reason})
    return {"format": detected, "records": records, "skipped": skipped}
