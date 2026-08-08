#!/usr/bin/env python3
"""Fetch non-zero stock positions from an IBKR Activity Flex Query.

The command writes a comma-separated STOCK_LIST value to stdout. Diagnostics
go to stderr so GitHub Actions can capture stdout without exposing credentials.
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping


BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
USER_AGENT = "daily-stock-analysis-ibkr-flex/1.0"
RETRYABLE_CODES = {"1001", "1003", "1004", "1005", "1006", "1007", "1008", "1009", "1019", "1021"}


class FlexError(RuntimeError):
    """Raised when IBKR cannot generate or return a usable Flex report."""


def _request(path: str, params: Mapping[str, str], timeout: float) -> bytes:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{BASE_URL}/{path}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _xml_error(root: ET.Element) -> tuple[str, str] | None:
    status = (root.findtext("Status") or "").strip()
    if status.lower() not in {"fail", "failed"}:
        return None
    return (
        (root.findtext("ErrorCode") or "unknown").strip(),
        (root.findtext("ErrorMessage") or "IBKR Flex request failed").strip(),
    )


def _parse_xml(payload: bytes) -> ET.Element:
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FlexError("IBKR returned a report that is not valid XML") from exc


def fetch_statement(token: str, query_id: str, *, timeout: float = 30.0) -> bytes:
    response = _parse_xml(
        _request("SendRequest", {"t": token, "q": query_id, "v": "3"}, timeout)
    )
    if error := _xml_error(response):
        raise FlexError(f"IBKR Flex error {error[0]}: {error[1]}")

    reference_code = (response.findtext("ReferenceCode") or "").strip()
    if not reference_code:
        raise FlexError("IBKR Flex did not return a reference code")

    delay = 2.0
    last_error: tuple[str, str] | None = None
    for attempt in range(6):
        if attempt:
            time.sleep(delay)
            delay = min(delay * 1.7, 12.0)
        payload = _request(
            "GetStatement",
            {"t": token, "q": reference_code, "v": "3"},
            timeout,
        )
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            return payload

        error = _xml_error(root)
        if not error:
            return payload
        last_error = error
        if error[0] not in RETRYABLE_CODES:
            break

    code, message = last_error or ("unknown", "statement was not available")
    raise FlexError(f"IBKR Flex error {code}: {message}")


def _is_nonzero(value: str | None) -> bool:
    try:
        return Decimal((value or "0").replace(",", "")) != 0
    except InvalidOperation:
        return False


def _normalize_symbol(row: Mapping[str, str]) -> str | None:
    asset_category = (row.get("assetCategory") or row.get("AssetCategory") or "").upper()
    if asset_category and asset_category != "STK":
        return None
    if not _is_nonzero(row.get("position") or row.get("Position")):
        return None

    symbol = (row.get("symbol") or row.get("Symbol") or "").strip().upper()
    if not symbol:
        return None
    symbol = re.sub(r"\s+", ".", symbol)

    currency = (row.get("currency") or row.get("Currency") or "").upper()
    exchange = (
        row.get("listingExchange")
        or row.get("ListingExchange")
        or row.get("primaryExchange")
        or row.get("PrimaryExchange")
        or ""
    ).upper()

    if symbol.isdigit():
        if currency == "HKD" or exchange in {"HKEX", "SEHK"}:
            return f"hk{symbol.zfill(5)}"
        if exchange in {"SSE", "SEHKNTL", "SHHKCONNECT"}:
            return f"sh{symbol.zfill(6)}"
        if exchange in {"SZSE", "SEHKSZSE", "SZHKCONNECT"}:
            return f"sz{symbol.zfill(6)}"
        if currency == "JPY" or exchange in {"TSE", "TSEJ", "JAPAN"}:
            return f"{symbol}.T"

    return symbol


def _dedupe(symbols: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def parse_positions(payload: bytes) -> list[str]:
    text = payload.decode("utf-8-sig", errors="replace").lstrip()
    if text.startswith("<"):
        root = _parse_xml(payload)
        if error := _xml_error(root):
            raise FlexError(f"IBKR Flex error {error[0]}: {error[1]}")
        rows = [element.attrib for element in root.iter("OpenPosition")]
    else:
        rows = list(csv.DictReader(io.StringIO(text)))

    symbols = _dedupe(_normalize_symbol(row) for row in rows)
    if not symbols:
        raise FlexError(
            "the Flex report contains no non-zero stock positions; include the Open Positions section"
        )
    return symbols


def main() -> int:
    token = os.getenv("IBKR_FLEX_TOKEN", "").strip()
    query_id = os.getenv("IBKR_FLEX_QUERY_ID", "").strip()
    if not token or not query_id:
        print("IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID are required", file=sys.stderr)
        return 2

    try:
        symbols = parse_positions(fetch_statement(token, query_id))
    except (FlexError, OSError) as exc:
        print(f"IBKR position sync failed: {exc}", file=sys.stderr)
        return 1

    print(",".join(symbols))
    print(f"IBKR position sync found {len(symbols)} analyzable holdings", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
