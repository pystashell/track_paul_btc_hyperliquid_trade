#!/usr/bin/env python3
"""Maintain Paulwei's complete Hyperliquid BTC account history.

The public data directory deliberately contains one JSON file per archived API.
This module fetches into memory, validates a complete candidate archive, and only
then replaces the previous data directory.  It uses only Python's standard
library so it can run unchanged in GitHub Actions.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


API_URL = "https://api.hyperliquid.xyz/info"
USER_ADDRESS = "0xdae4df7207feb3b350e4284c8efe5f7dac37f637"
ACCOUNT_START_MS = 1_763_164_800_000  # 2025-11-15T00:00:00Z
TIME_RANGE_SATURATION = 500
MAX_GITHUB_DATA_FILE_BYTES = 45 * 1024 * 1024
BTC = "BTC"

EVENT_ENDPOINTS = (
    "historicalOrders",
    "userFillsByTime",
    "userFunding",
    "userNonFundingLedgerUpdates",
)
SNAPSHOT_ENDPOINTS = (
    "openOrders",
    "frontendOpenOrders",
    "clearinghouseState",
    "spotClearinghouseState",
)
ARCHIVE_ENDPOINTS = EVENT_ENDPOINTS + SNAPSHOT_ENDPOINTS
ARCHIVE_FILES = {endpoint: f"{endpoint}.json" for endpoint in ARCHIVE_ENDPOINTS}


class TrackerError(RuntimeError):
    """Expected, user-facing tracker failure."""


class ValidationError(TrackerError):
    """Archive or fetched data failed validation."""

    def __init__(self, message: str, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class ApiResult:
    response: Any
    captured_at: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def make_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TrackerError(f"Missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TrackerError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(encoded, encoding="utf-8", newline="\n")
    os.replace(temp_path, path)


def write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is not None:
        write_json(path, report)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def as_decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError(f"{label} is not a decimal: {value!r}") from exc


def event_time(record: dict[str, Any]) -> int:
    value = record.get("statusTimestamp")
    if value is None and isinstance(record.get("order"), dict):
        value = record["order"].get("timestamp")
    return int(value or 0)


def order_key(record: dict[str, Any]) -> tuple[Any, ...]:
    order = record.get("order")
    if not isinstance(order, dict) or order.get("oid") is None:
        raise ValidationError(f"historicalOrders record has no order.oid: {record!r}")
    return int(order["oid"]), str(record.get("status")), event_time(record)


def fill_key(record: dict[str, Any]) -> tuple[Any, ...]:
    if record.get("tid") is not None:
        return str(record.get("coin")), int(record.get("time", 0)), int(record["tid"])
    return (
        str(record.get("coin")),
        int(record.get("time", 0)),
        record.get("oid"),
        record.get("side"),
        record.get("px"),
        record.get("sz"),
        record.get("startPosition"),
        record.get("dir"),
    )


def funding_hour_key(record: dict[str, Any]) -> tuple[Any, ...]:
    delta = record.get("delta") or {}
    return int(record.get("time", 0)), str(delta.get("type")), str(delta.get("coin"))


def ledger_key(record: dict[str, Any]) -> tuple[Any, ...]:
    delta = record.get("delta") or {}
    return int(record.get("time", 0)), str(delta.get("type")), canonical(delta)


def utc_day_from_ms(value: Any) -> str:
    stamp = int(value)
    return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).date().isoformat()


def funding_group_key(record: dict[str, Any]) -> tuple[str, str, str]:
    delta = record.get("delta") or {}
    return (
        utc_day_from_ms(record.get("time", 0)),
        str(delta.get("type")),
        str(delta.get("coin")),
    )


def funding_samples(record: dict[str, Any]) -> int | None:
    value = (record.get("delta") or {}).get("nSamples")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid userFunding nSamples: {value!r}") from exc


def record_usdc(record: dict[str, Any]) -> Decimal:
    return as_decimal((record.get("delta") or {}).get("usdc"), "delta.usdc")


def merge_latest(
    batches: Iterable[Iterable[dict[str, Any]]],
    key_function: Callable[[dict[str, Any]], tuple[Any, ...]],
) -> tuple[list[dict[str, Any]], int]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    conflicts = 0
    for batch in batches:
        for record in batch:
            if not isinstance(record, dict):
                raise ValidationError(f"Expected an object record, got {type(record).__name__}")
            key = key_function(record)
            previous = merged.get(key)
            if previous is not None and canonical(previous) != canonical(record):
                conflicts += 1
            merged[key] = clone(record)
    return list(merged.values()), conflicts


def canonicalize_funding(
    batches: Iterable[Iterable[dict[str, Any]]],
    tolerance: Decimal = Decimal("0.000001"),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Choose exactly one non-overlapping representation for each UTC day.

    Hyperliquid eventually compresses hourly funding rows into one daily row with
    ``nSamples``.  If all original hourly rows were captured and their USDC sum
    still matches the newest aggregate, the hourly rows retain more information
    and are kept.  Otherwise the newest official aggregate is kept.  The two
    representations are never counted together.
    """

    observations: list[tuple[int, dict[str, Any]]] = []
    for sequence, batch in enumerate(batches):
        for record in batch:
            if not isinstance(record, dict):
                raise ValidationError("userFunding contains a non-object record")
            observations.append((sequence, clone(record)))

    hourly: dict[tuple[Any, ...], tuple[int, dict[str, Any]]] = {}
    aggregates: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
    for sequence, record in observations:
        if funding_samples(record) is None:
            hourly[funding_hour_key(record)] = (sequence, record)
        else:
            group = funding_group_key(record)
            prior = aggregates.get(group)
            candidate_rank = (sequence, funding_samples(record) or 0, int(record.get("time", 0)))
            prior_rank = (
                (prior[0], funding_samples(prior[1]) or 0, int(prior[1].get("time", 0)))
                if prior
                else None
            )
            if prior_rank is None or candidate_rank >= prior_rank:
                aggregates[group] = (sequence, record)

    hourly_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for _, record in hourly.values():
        hourly_groups.setdefault(funding_group_key(record), []).append(record)

    output: list[dict[str, Any]] = []
    all_groups = set(hourly_groups) | set(aggregates)
    hourly_days = 0
    aggregate_days = 0
    recovered_hourly_days = 0
    fallback_aggregate_days = 0
    for group in sorted(all_groups):
        hourly_rows = sorted(
            hourly_groups.get(group, []), key=lambda row: int(row.get("time", 0))
        )
        aggregate_entry = aggregates.get(group)
        if aggregate_entry is None:
            output.extend(hourly_rows)
            hourly_days += 1
            continue

        aggregate = aggregate_entry[1]
        expected_count = funding_samples(aggregate) or 0
        hourly_total = sum((record_usdc(row) for row in hourly_rows), Decimal("0"))
        aggregate_total = record_usdc(aggregate)
        if len(hourly_rows) == expected_count and abs(hourly_total - aggregate_total) <= tolerance:
            output.extend(hourly_rows)
            hourly_days += 1
            recovered_hourly_days += 1
        else:
            output.append(aggregate)
            aggregate_days += 1
            fallback_aggregate_days += 1

    output.sort(key=lambda row: (int(row.get("time", 0)), canonical(row)))
    report = {
        "records": len(output),
        "utcDays": len(all_groups),
        "hourlyDays": hourly_days,
        "aggregateDays": aggregate_days,
        "recoveredHourlyDays": recovered_hourly_days,
        "fallbackAggregateDays": fallback_aggregate_days,
        "usdcTotal": str(sum((record_usdc(row) for row in output), Decimal("0"))),
    }
    return output, report


class HyperliquidClient:
    def __init__(
        self,
        api_url: str = API_URL,
        min_interval_seconds: float = 0.35,
        timeout_seconds: float = 30.0,
        retries: int = 5,
    ) -> None:
        self.api_url = api_url
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self._last_request_started = 0.0

    def post(self, payload: dict[str, Any]) -> ApiResult:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.retries):
            elapsed = time.monotonic() - self._last_request_started
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)
            self._last_request_started = time.monotonic()
            request = urllib.request.Request(
                self.api_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "track-paul-btc-hyperliquid-trade/1.0",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    raw = response.read()
                decoded = json.loads(raw.decode("utf-8"))
                return ApiResult(decoded, utc_now_iso())
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {
                    408,
                    425,
                    429,
                    500,
                    502,
                    503,
                    504,
                }
                if not retryable or attempt + 1 >= self.retries:
                    break
                time.sleep(min(2**attempt, 16))
        raise TrackerError(f"Hyperliquid request failed for {payload.get('type')}: {last_error}")


def split_time_range(start_ms: int, end_ms: int, on_utc_day: bool) -> int:
    midpoint = (start_ms + end_ms) // 2
    if not on_utc_day:
        return midpoint
    day_ms = 86_400_000
    boundary = ((midpoint // day_ms) + 1) * day_ms - 1
    if boundary >= end_ms:
        boundary = (midpoint // day_ms) * day_ms - 1
    return boundary


def fetch_time_range(
    client: HyperliquidClient,
    endpoint: str,
    user: str,
    start_ms: int,
    end_ms: int,
    *,
    aggregate_by_time: bool | None = None,
    split_on_utc_day: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch a bounded time endpoint without silently accepting a capped page."""

    pages: list[dict[str, Any]] = []

    def fetch_part(part_start: int, part_end: int) -> list[dict[str, Any]]:
        request: dict[str, Any] = {
            "type": endpoint,
            "user": user,
            "startTime": part_start,
            "endTime": part_end,
        }
        if aggregate_by_time is not None:
            request["aggregateByTime"] = aggregate_by_time
        result = client.post(request)
        if not isinstance(result.response, list):
            raise ValidationError(
                f"{endpoint} returned {type(result.response).__name__}, expected array"
            )
        rows = result.response
        pages.append(
            {
                "startTime": part_start,
                "endTime": part_end,
                "count": len(rows),
                "capturedAt": result.captured_at,
            }
        )
        if len(rows) < TIME_RANGE_SATURATION:
            return rows
        if part_start >= part_end:
            raise ValidationError(
                f"{endpoint} returned at least {TIME_RANGE_SATURATION} rows at one millisecond"
            )
        split = split_time_range(part_start, part_end, split_on_utc_day)
        if split < part_start or split >= part_end:
            raise ValidationError(
                f"Cannot safely split saturated {endpoint} range {part_start}..{part_end}"
            )
        return fetch_part(part_start, split) + fetch_part(split + 1, part_end)

    rows = fetch_part(start_ms, end_ms)
    return rows, pages


def validate_event_batch(endpoint: str, payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValidationError(f"{endpoint} must return an array")
    rows = payload
    for index, record in enumerate(rows):
        if not isinstance(record, dict):
            raise ValidationError(f"{endpoint}[{index}] is not an object")

    if endpoint == "historicalOrders":
        keys = [order_key(row) for row in rows]
        for row in rows:
            order = row.get("order") or {}
            if order.get("coin") != BTC:
                raise ValidationError(
                    f"Scope violation: historicalOrders contains {order.get('coin')!r}, expected BTC"
                )
    elif endpoint == "userFillsByTime":
        keys = [fill_key(row) for row in rows]
        required = {"coin", "px", "sz", "side", "time", "oid", "fee", "closedPnl"}
        for row in rows:
            if not required.issubset(row):
                raise ValidationError(f"userFillsByTime record missing fields: {required - set(row)}")
            if row.get("coin") != BTC:
                raise ValidationError(
                    f"Scope violation: userFillsByTime contains {row.get('coin')!r}, expected BTC"
                )
    elif endpoint == "userFunding":
        keys = []
        for row in rows:
            delta = row.get("delta")
            if not isinstance(delta, dict) or delta.get("type") != "funding":
                raise ValidationError(f"Invalid userFunding delta: {delta!r}")
            if delta.get("coin") != BTC:
                raise ValidationError(
                    f"Scope violation: userFunding contains {delta.get('coin')!r}, expected BTC"
                )
            int(row.get("time"))
            record_usdc(row)
            keys.append((funding_hour_key(row), funding_samples(row)))
    elif endpoint == "userNonFundingLedgerUpdates":
        keys = [ledger_key(row) for row in rows]
        for row in rows:
            if not isinstance(row.get("delta"), dict):
                raise ValidationError("userNonFundingLedgerUpdates record has no delta object")
            int(row.get("time"))
    else:
        raise ValidationError(f"Unknown event endpoint: {endpoint}")

    if len(keys) != len(set(keys)):
        raise ValidationError(f"{endpoint} fetch contains duplicate stable keys")
    return rows


def validate_api_response(endpoint: str, response: Any) -> None:
    if endpoint in {"openOrders", "frontendOpenOrders"}:
        if not isinstance(response, list) or any(not isinstance(row, dict) for row in response):
            raise ValidationError(f"{endpoint} response must be an array of objects")
        keys: list[int] = []
        for row in response:
            if row.get("coin") != BTC:
                raise ValidationError(
                    f"Scope violation: {endpoint} contains {row.get('coin')!r}, expected BTC"
                )
            if row.get("oid") is None:
                raise ValidationError(f"{endpoint} order has no oid")
            keys.append(int(row["oid"]))
        if len(keys) != len(set(keys)):
            raise ValidationError(f"{endpoint} contains duplicate oid values")
    elif endpoint == "clearinghouseState":
        if not isinstance(response, dict) or not isinstance(response.get("assetPositions"), list):
            raise ValidationError("clearinghouseState response is missing assetPositions")
        other_coins = {
            str((item.get("position") or {}).get("coin"))
            for item in response["assetPositions"]
            if (item.get("position") or {}).get("coin") != BTC
        }
        if other_coins:
            raise ValidationError(
                f"Scope violation: clearinghouseState has non-BTC positions: {sorted(other_coins)}"
            )
    elif endpoint == "spotClearinghouseState":
        if not isinstance(response, dict) or not isinstance(response.get("balances"), list):
            raise ValidationError("spotClearinghouseState response is missing balances")
    else:
        raise ValidationError(f"Unknown non-event endpoint: {endpoint}")


def compare_open_order_views(
    open_orders: Sequence[dict[str, Any]],
    frontend_orders: Sequence[dict[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    open_by_oid = {int(row["oid"]): row for row in open_orders}
    frontend_by_oid = {int(row["oid"]): row for row in frontend_orders}
    if set(open_by_oid) != set(frontend_by_oid):
        warnings.append(
            "openOrders and frontendOpenOrders oid sets differ; requests are separate point-in-time reads"
        )
    for oid in set(open_by_oid) & set(frontend_by_oid):
        for field in ("coin", "side", "limitPx", "sz", "oid", "timestamp", "origSz"):
            if open_by_oid[oid].get(field) != frontend_by_oid[oid].get(field):
                warnings.append(f"open-order core field differs for oid={oid}: {field}")
    return warnings


def fetch_bundle(client: HyperliquidClient, user: str, start_ms: int) -> dict[str, Any]:
    run_id = make_run_id()
    diagnostics: dict[str, Any] = {"pages": {}}

    historical = client.post({"type": "historicalOrders", "user": user})

    fills, fill_pages = fetch_time_range(
        client,
        "userFillsByTime",
        user,
        start_ms,
        int(time.time() * 1000),
        aggregate_by_time=False,
    )
    diagnostics["pages"]["userFillsByTime"] = fill_pages

    funding, funding_pages = fetch_time_range(
        client,
        "userFunding",
        user,
        start_ms,
        int(time.time() * 1000),
        split_on_utc_day=True,
    )
    diagnostics["pages"]["userFunding"] = funding_pages

    ledger, ledger_pages = fetch_time_range(
        client,
        "userNonFundingLedgerUpdates",
        user,
        start_ms,
        int(time.time() * 1000),
    )
    diagnostics["pages"]["userNonFundingLedgerUpdates"] = ledger_pages

    open_orders = client.post({"type": "openOrders", "user": user})
    frontend_orders = client.post({"type": "frontendOpenOrders", "user": user})
    spot_state = client.post({"type": "spotClearinghouseState", "user": user})
    perp_state = client.post({"type": "clearinghouseState", "user": user})

    events = {
        "historicalOrders": historical.response,
        "userFillsByTime": fills,
        "userFunding": funding,
        "userNonFundingLedgerUpdates": ledger,
    }
    snapshots = {
        "openOrders": {"capturedAt": open_orders.captured_at, "response": open_orders.response},
        "frontendOpenOrders": {
            "capturedAt": frontend_orders.captured_at,
            "response": frontend_orders.response,
        },
        "spotClearinghouseState": {
            "capturedAt": spot_state.captured_at,
            "response": spot_state.response,
        },
        "clearinghouseState": {
            "capturedAt": perp_state.captured_at,
            "response": perp_state.response,
        },
    }

    for endpoint, response in events.items():
        validate_event_batch(endpoint, response)
    for endpoint, snapshot in snapshots.items():
        validate_api_response(endpoint, snapshot["response"])
    diagnostics["warnings"] = compare_open_order_views(
        snapshots["openOrders"]["response"],
        snapshots["frontendOpenOrders"]["response"],
    )
    return {
        "runId": run_id,
        "fetchedAt": utc_now_iso(),
        "events": events,
        "snapshots": snapshots,
        "diagnostics": diagnostics,
    }


def backfill_orders_for_fills(
    client: HyperliquidClient,
    user: str,
    orders: Sequence[dict[str, Any]],
    fills: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recover final HistoricalOrder objects for fill oids missing from the window."""

    known_oids = {int((row.get("order") or {})["oid"]) for row in orders}
    fill_oids = {int(row["oid"]) for row in fills if row.get("oid") is not None}
    missing_oids = sorted(fill_oids - known_oids)
    recovered: list[dict[str, Any]] = []
    unresolved: list[int] = []
    for oid in missing_oids:
        result = client.post({"type": "orderStatus", "user": user, "oid": oid})
        response = result.response
        if (
            isinstance(response, dict)
            and response.get("status") == "order"
            and isinstance(response.get("order"), dict)
        ):
            record = response["order"]
            validate_event_batch("historicalOrders", [record])
            if int((record.get("order") or {}).get("oid", -1)) != oid:
                raise ValidationError(
                    f"orderStatus returned oid={(record.get('order') or {}).get('oid')} for {oid}"
                )
            recovered.append(record)
        else:
            unresolved.append(oid)
    return recovered, {
        "requestedOids": missing_oids,
        "recoveredOids": [int((row.get("order") or {})["oid"]) for row in recovered],
        "unresolvedOids": unresolved,
    }


def new_snapshot_document(endpoint: str, user: str) -> dict[str, Any]:
    return {
        "schema": "hyperliquid.snapshot-history.v1",
        "request": {"type": endpoint, "user": user},
        "snapshots": [],
    }


def snapshot_utc_day(captured_at: str) -> str:
    value = captured_at.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"Invalid snapshot capturedAt: {captured_at!r}") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"Snapshot capturedAt has no timezone: {captured_at!r}")
    return parsed.astimezone(timezone.utc).date().isoformat()


def normalize_snapshot_document(document: dict[str, Any]) -> None:
    """Keep the newest successfully validated checkpoint for each UTC day."""

    newest_by_day: dict[str, dict[str, Any]] = {}
    for snapshot in document["snapshots"]:
        day = snapshot_utc_day(str(snapshot.get("capturedAt")))
        previous = newest_by_day.get(day)
        rank = (str(snapshot.get("capturedAt")), str(snapshot.get("runId")))
        previous_rank = (
            (str(previous.get("capturedAt")), str(previous.get("runId"))) if previous else None
        )
        if previous_rank is None or rank >= previous_rank:
            newest_by_day[day] = snapshot
    document["snapshots"] = sorted(
        newest_by_day.values(),
        key=lambda row: (str(row.get("capturedAt")), str(row.get("runId"))),
    )


def add_snapshot(
    document: dict[str, Any],
    endpoint: str,
    user: str,
    run_id: str,
    captured_at: str,
    response: Any,
) -> None:
    validate_snapshot_document(document, endpoint, user)
    validate_api_response(endpoint, response)
    normalize_snapshot_document(document)
    candidate = {
        "runId": run_id,
        "capturedAt": captured_at,
        "response": clone(response),
    }
    snapshots = document["snapshots"]
    identity = (run_id, captured_at)
    candidate_day = snapshot_utc_day(captured_at)
    for existing in snapshots:
        if (existing.get("runId"), existing.get("capturedAt")) == identity:
            if canonical(existing.get("response")) != canonical(response):
                raise ValidationError(f"Conflicting {endpoint} snapshot identity: {identity}")
            return
    for index, existing in enumerate(snapshots):
        if snapshot_utc_day(str(existing.get("capturedAt"))) != candidate_day:
            continue
        existing_rank = (str(existing.get("capturedAt")), str(existing.get("runId")))
        if (captured_at, run_id) >= existing_rank:
            snapshots[index] = candidate
        return
    snapshots.append(candidate)
    snapshots.sort(key=lambda row: (str(row.get("capturedAt")), str(row.get("runId"))))


def validate_snapshot_document(document: Any, endpoint: str, user: str = USER_ADDRESS) -> None:
    if not isinstance(document, dict):
        raise ValidationError(f"{endpoint}.json must be a snapshot-history object")
    if document.get("schema") != "hyperliquid.snapshot-history.v1":
        raise ValidationError(f"{endpoint}.json has an unsupported schema")
    request = document.get("request")
    if not isinstance(request, dict) or request.get("type") != endpoint:
        raise ValidationError(f"{endpoint}.json has the wrong request metadata")
    if str(request.get("user", "")).lower() != user.lower():
        raise ValidationError(f"{endpoint}.json has the wrong user address")
    if not isinstance(document.get("snapshots"), list):
        raise ValidationError(f"{endpoint}.json snapshots must be an array")


def load_archive(data_dir: Path) -> dict[str, Any]:
    return {
        endpoint: read_json(data_dir / filename)
        for endpoint, filename in ARCHIVE_FILES.items()
    }


def merge_bundle_into_archive(
    existing: dict[str, Any],
    bundle: dict[str, Any],
    user: str = USER_ADDRESS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    archive = clone(existing)
    events = bundle["events"]

    orders, order_conflicts = merge_latest(
        [archive["historicalOrders"], events["historicalOrders"]], order_key
    )
    fills, fill_conflicts = merge_latest(
        [archive["userFillsByTime"], events["userFillsByTime"]], fill_key
    )
    funding, funding_report = canonicalize_funding(
        [archive["userFunding"], events["userFunding"]]
    )
    ledger, ledger_conflicts = merge_latest(
        [
            archive["userNonFundingLedgerUpdates"],
            events["userNonFundingLedgerUpdates"],
        ],
        ledger_key,
    )
    orders.sort(key=lambda row: (-event_time(row), -int((row.get("order") or {}).get("oid", 0))))
    fills.sort(key=lambda row: (int(row.get("time", 0)), int(row.get("tid") or 0)))
    ledger.sort(key=lambda row: (int(row.get("time", 0)), canonical(row)))
    archive["historicalOrders"] = orders
    archive["userFillsByTime"] = fills
    archive["userFunding"] = funding
    archive["userNonFundingLedgerUpdates"] = ledger

    for endpoint in SNAPSHOT_ENDPOINTS:
        normalize_snapshot_document(archive[endpoint])
        snapshot = bundle["snapshots"][endpoint]
        add_snapshot(
            archive[endpoint],
            endpoint,
            user,
            bundle["runId"],
            snapshot["capturedAt"],
            snapshot["response"],
        )

    merge_report = {
        "mode": "update",
        "runId": bundle["runId"],
        "fetchedAt": bundle["fetchedAt"],
        "mergeConflicts": {
            "historicalOrders": order_conflicts,
            "userFillsByTime": fill_conflicts,
            "userNonFundingLedgerUpdates": ledger_conflicts,
        },
        "funding": funding_report,
        "fetch": bundle["diagnostics"],
    }
    return archive, merge_report


def write_archive_files(data_dir: Path, archive: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for endpoint in ARCHIVE_ENDPOINTS:
        write_json(data_dir / ARCHIVE_FILES[endpoint], archive[endpoint])


def latest_snapshot(document: dict[str, Any]) -> dict[str, Any]:
    snapshots = document["snapshots"]
    if not snapshots:
        raise ValidationError("Snapshot history is empty")
    return max(snapshots, key=lambda row: (str(row["capturedAt"]), str(row["runId"])))


def snapshot_by_run(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["runId"]): item for item in document["snapshots"]}


def btc_position(state: dict[str, Any]) -> dict[str, Any] | None:
    for item in state.get("assetPositions", []):
        position = item.get("position") or {}
        if position.get("coin") == BTC:
            return position
    return None


def signed_fill_size(fill: dict[str, Any]) -> Decimal:
    size = as_decimal(fill.get("sz"), "fill.sz")
    side = fill.get("side")
    if side == "B":
        return size
    if side == "A":
        return -size
    raise ValidationError(f"Unknown fill side: {side!r}")


def validate_archive(
    data_dir: Path,
    *,
    user: str = USER_ADDRESS,
    expected_funding_total: Decimal | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            failures.append(f"{name}: {detail}")

    try:
        actual_json = {path.name for path in data_dir.glob("*.json")}
        expected_json = set(ARCHIVE_FILES.values())
        check("exact_api_json_file_set", actual_json == expected_json, {
            "missing": sorted(expected_json - actual_json),
            "unexpected": sorted(actual_json - expected_json),
        })
        archive = load_archive(data_dir)

        for endpoint in EVENT_ENDPOINTS:
            validate_event_batch(endpoint, archive[endpoint])
        for endpoint in SNAPSHOT_ENDPOINTS:
            document = archive[endpoint]
            validate_snapshot_document(document, endpoint, user)
            identities: list[tuple[str, str]] = []
            snapshot_days: list[str] = []
            previous: tuple[str, str] | None = None
            for snapshot in document["snapshots"]:
                if not isinstance(snapshot, dict):
                    raise ValidationError(f"{endpoint} snapshot is not an object")
                identity = (str(snapshot.get("capturedAt")), str(snapshot.get("runId")))
                identities.append(identity)
                snapshot_days.append(snapshot_utc_day(identity[0]))
                validate_api_response(endpoint, snapshot.get("response"))
                if previous is not None and identity < previous:
                    failures.append(f"{endpoint} snapshots are not chronological")
                previous = identity
            check(
                f"{endpoint}_snapshot_identities_unique",
                len(identities) == len(set(identities)),
                len(identities),
            )
            check(
                f"{endpoint}_one_checkpoint_per_utc_day",
                len(snapshot_days) == len(set(snapshot_days)),
                len(snapshot_days),
            )

        orders = archive["historicalOrders"]
        fills = archive["userFillsByTime"]
        funding = archive["userFunding"]
        ledger = archive["userNonFundingLedgerUpdates"]

        order_keys = [order_key(row) for row in orders]
        fill_keys = [fill_key(row) for row in fills]
        ledger_keys = [ledger_key(row) for row in ledger]
        check("historicalOrders_keys_unique", len(order_keys) == len(set(order_keys)), len(orders))
        check("userFillsByTime_keys_unique", len(fill_keys) == len(set(fill_keys)), len(fills))
        check(
            "userNonFundingLedgerUpdates_keys_unique",
            len(ledger_keys) == len(set(ledger_keys)),
            len(ledger),
        )
        order_oids = {int((row.get("order") or {})["oid"]) for row in orders}
        missing_fill_oids = sorted(
            {
                int(row["oid"])
                for row in fills
                if row.get("oid") is not None and int(row["oid"]) not in order_oids
            }
        )
        check(
            "every_fill_oid_has_historical_order",
            not missing_fill_oids,
            missing_fill_oids,
        )
        check(
            "historicalOrders_newest_first",
            orders == sorted(
                orders,
                key=lambda row: (-event_time(row), -int((row.get("order") or {}).get("oid", 0))),
            ),
            len(orders),
        )
        check(
            "userFillsByTime_oldest_first",
            fills == sorted(
                fills, key=lambda row: (int(row.get("time", 0)), int(row.get("tid") or 0))
            ),
            len(fills),
        )
        check(
            "userFunding_oldest_first",
            funding == sorted(funding, key=lambda row: (int(row.get("time", 0)), canonical(row))),
            len(funding),
        )
        check(
            "userNonFundingLedgerUpdates_oldest_first",
            ledger == sorted(ledger, key=lambda row: (int(row.get("time", 0)), canonical(row))),
            len(ledger),
        )

        funding_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in funding:
            funding_groups.setdefault(funding_group_key(row), []).append(row)
        overlap_days: list[str] = []
        malformed_aggregate_days: list[str] = []
        for group, rows in funding_groups.items():
            aggregates = [row for row in rows if funding_samples(row) is not None]
            hourly = [row for row in rows if funding_samples(row) is None]
            if aggregates and hourly:
                overlap_days.append(group[0])
            if len(aggregates) > 1:
                malformed_aggregate_days.append(group[0])
        check("userFunding_no_hourly_daily_overlap", not overlap_days, sorted(set(overlap_days)))
        check(
            "userFunding_one_aggregate_per_day",
            not malformed_aggregate_days,
            sorted(set(malformed_aggregate_days)),
        )
        funding_total = sum((record_usdc(row) for row in funding), Decimal("0"))
        if expected_funding_total is not None:
            check(
                "userFunding_matches_current_api_total",
                abs(funding_total - expected_funding_total) <= Decimal("0.000001"),
                {"archive": str(funding_total), "api": str(expected_funding_total)},
            )

        run_maps = {endpoint: snapshot_by_run(archive[endpoint]) for endpoint in SNAPSHOT_ENDPOINTS}
        common_runs = set.intersection(*(set(mapping) for mapping in run_maps.values()))
        check("snapshot_endpoints_have_common_checkpoint", bool(common_runs), len(common_runs))
        run_sets_align = all(set(mapping) == common_runs for mapping in run_maps.values())
        check(
            "snapshot_endpoint_run_sets_align",
            run_sets_align,
            {endpoint: len(mapping) for endpoint, mapping in run_maps.items()},
        )
        if common_runs:
            position_checkpoint_failures: list[dict[str, str]] = []
            for run_id in sorted(common_runs):
                checkpoint_state = run_maps["clearinghouseState"][run_id]["response"]
                checkpoint_cutoff = int(checkpoint_state.get("time", 0))
                checkpoint_position = btc_position(checkpoint_state)
                checkpoint_size = as_decimal(
                    checkpoint_position.get("szi", "0") if checkpoint_position else "0",
                    "checkpoint position.szi",
                )
                checkpoint_replay = sum(
                    (
                        signed_fill_size(row)
                        for row in fills
                        if int(row.get("time", 0)) <= checkpoint_cutoff
                    ),
                    Decimal("0"),
                )
                if abs(checkpoint_replay - checkpoint_size) > Decimal("0.00000001"):
                    position_checkpoint_failures.append(
                        {
                            "runId": run_id,
                            "replayed": str(checkpoint_replay),
                            "state": str(checkpoint_size),
                        }
                    )
            check(
                "all_BTC_position_checkpoints_replay",
                not position_checkpoint_failures,
                {
                    "checked": len(common_runs),
                    "failures": position_checkpoint_failures,
                },
            )

            latest_run = max(
                common_runs,
                key=lambda run: run_maps["clearinghouseState"][run]["capturedAt"],
            )
            clearing_snapshot = run_maps["clearinghouseState"][latest_run]
            spot_snapshot = run_maps["spotClearinghouseState"][latest_run]
            state = clearing_snapshot["response"]
            cutoff = int(state.get("time", 0))
            position = btc_position(state)
            state_size = as_decimal(position.get("szi", "0") if position else "0", "position.szi")
            replay_size = sum(
                (signed_fill_size(row) for row in fills if int(row.get("time", 0)) <= cutoff),
                Decimal("0"),
            )
            check(
                "latest_BTC_position_replays_from_fills",
                abs(replay_size - state_size) <= Decimal("0.00000001"),
                {"runId": latest_run, "replayed": str(replay_size), "state": str(state_size)},
            )

            cutoff_funding = sum(
                (record_usdc(row) for row in funding if int(row.get("time", 0)) <= cutoff),
                Decimal("0"),
            )
            if position and isinstance(position.get("cumFunding"), dict):
                state_funding = as_decimal(position["cumFunding"].get("allTime"), "cumFunding.allTime")
                check(
                    "latest_cumulative_funding_reconciles",
                    abs(cutoff_funding + state_funding) <= Decimal("0.000001"),
                    {
                        "runId": latest_run,
                        "ledgerFunding": str(cutoff_funding),
                        "stateCumFunding": str(state_funding),
                    },
                )

            warnings.extend(
                compare_open_order_views(
                    run_maps["openOrders"][latest_run]["response"],
                    run_maps["frontendOpenOrders"][latest_run]["response"],
                )
            )

            ledger_types = {str((row.get("delta") or {}).get("type")) for row in ledger}
            if ledger_types <= {"deposit"}:
                deposits = sum(
                    (
                        as_decimal((row.get("delta") or {}).get("usdc"), "deposit.usdc")
                        for row in ledger
                        if int(row.get("time", 0)) <= cutoff
                    ),
                    Decimal("0"),
                )
                closed_pnl = sum(
                    (
                        as_decimal(row.get("closedPnl"), "fill.closedPnl")
                        for row in fills
                        if int(row.get("time", 0)) <= cutoff
                    ),
                    Decimal("0"),
                )
                fees = sum(
                    (
                        as_decimal(row.get("fee"), "fill.fee")
                        for row in fills
                        if int(row.get("time", 0)) <= cutoff
                    ),
                    Decimal("0"),
                )
                unrealized = as_decimal(
                    position.get("unrealizedPnl", "0") if position else "0",
                    "position.unrealizedPnl",
                )
                expected_equity = deposits + closed_pnl - fees + cutoff_funding + unrealized
                perp_value = as_decimal(
                    (state.get("marginSummary") or {}).get("accountValue"),
                    "marginSummary.accountValue",
                )
                spot_usdc = sum(
                    (
                        as_decimal(row.get("total"), "spot USDC total")
                        for row in spot_snapshot["response"].get("balances", [])
                        if row.get("coin") == "USDC"
                    ),
                    Decimal("0"),
                )
                # In Hyperliquid's unified-account state, spot USDC ``total`` is
                # the account-level equity and already includes the perp
                # allocation represented by marginSummary.accountValue.  Older
                # non-unified states can have no spot USDC balance, in which case
                # the perp account value is the usable checkpoint.
                observed_equity = spot_usdc if spot_usdc != 0 else perp_value
                equity_source = "spotClearinghouseState.USDC.total" if spot_usdc != 0 else "clearinghouseState.marginSummary.accountValue"
                residual = observed_equity - expected_equity
                check(
                    "latest_equity_reconciles_from_events",
                    abs(residual) <= Decimal("2"),
                    {
                        "runId": latest_run,
                        "eventEquation": str(expected_equity),
                        "state": str(observed_equity),
                        "stateSource": equity_source,
                        "residual": str(residual),
                    },
                )

            else:
                warnings.append(
                    "Equity equation skipped because non-deposit ledger types need explicit cash-flow signs: "
                    + ", ".join(sorted(ledger_types))
                )

        files: dict[str, Any] = {}
        for endpoint, filename in ARCHIVE_FILES.items():
            path = data_dir / filename
            files[filename] = {
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "records": (
                    len(archive[endpoint])
                    if endpoint in EVENT_ENDPOINTS
                    else len(archive[endpoint]["snapshots"])
                ),
            }
        oversized_files = {
            filename: metadata["bytes"]
            for filename, metadata in files.items()
            if metadata["bytes"] >= MAX_GITHUB_DATA_FILE_BYTES
        }
        check(
            "all_data_files_below_45_mib",
            not oversized_files,
            oversized_files,
        )
        report = {
            "status": "failed" if failures else "passed",
            "generatedAt": utc_now_iso(),
            "dataDirectory": str(data_dir.resolve()),
            "user": user,
            "checks": checks,
            "failures": failures,
            "warnings": sorted(set(warnings)),
            "files": files,
            "coverage": {
                "historicalOrderEvents": len(orders),
                "historicalOrderIds": len({int((row.get("order") or {})["oid"]) for row in orders}),
                "fills": len(fills),
                "fundingRecords": len(funding),
                "fundingUtcDays": len(funding_groups),
                "fundingUsdcTotal": str(funding_total),
                "nonFundingLedgerUpdates": len(ledger),
                "snapshotCheckpoints": {
                    endpoint: len(archive[endpoint]["snapshots"])
                    for endpoint in SNAPSHOT_ENDPOINTS
                },
            },
        }
    except (TrackerError, KeyError, TypeError, ValueError) as exc:
        failures.append(str(exc))
        report = {
            "status": "failed",
            "generatedAt": utc_now_iso(),
            "dataDirectory": str(data_dir.resolve()),
            "user": user,
            "checks": checks,
            "failures": failures,
            "warnings": sorted(set(warnings)),
        }

    if report["status"] != "passed":
        raise ValidationError("Archive validation failed", report)
    return report


def publish_candidate(
    target: Path,
    archive: dict[str, Any],
    *,
    user: str,
    expected_funding_total: Decimal | None = None,
    allow_replace: bool,
) -> dict[str, Any]:
    parent = target.resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / f".data-stage-{uuid.uuid4().hex}"
    stage.mkdir()
    backup = parent / f".data-backup-{uuid.uuid4().hex}"
    failed = parent / f".data-failed-{uuid.uuid4().hex}"
    moved_old = False
    created_new = False
    try:
        write_archive_files(stage, archive)
        candidate_report = validate_archive(
            stage, user=user, expected_funding_total=expected_funding_total
        )
        if target.exists():
            if not allow_replace:
                raise TrackerError(f"Refusing to replace existing directory without permission: {target}")
            os.replace(target, backup)
            moved_old = True
        target.mkdir()
        created_new = True
        for endpoint, filename in ARCHIVE_FILES.items():
            os.replace(stage / filename, target / filename)
        final_report = validate_archive(
            target, user=user, expected_funding_total=expected_funding_total
        )
        if candidate_report["files"] != final_report["files"]:
            raise ValidationError("Published files differ from the validated candidate")
        if moved_old:
            shutil.rmtree(backup)
        return final_report
    except Exception:
        if created_new and target.exists():
            os.replace(target, failed)
        if moved_old and backup.exists():
            os.replace(backup, target)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def validate_candidate_without_publish(
    target: Path,
    archive: dict[str, Any],
    *,
    user: str,
    expected_funding_total: Decimal | None = None,
) -> dict[str, Any]:
    """Fully serialize and re-read a candidate while leaving ``target`` untouched."""

    parent = target.resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".data-dry-run-", dir=parent) as temp_name:
        stage = Path(temp_name)
        write_archive_files(stage, archive)
        return validate_archive(
            stage,
            user=user,
            expected_funding_total=expected_funding_total,
        )


def combined_report(
    operation: dict[str, Any], validation: dict[str, Any]
) -> dict[str, Any]:
    return {
        "status": validation["status"],
        "generatedAt": utc_now_iso(),
        "operation": operation,
        "validation": validation,
    }


def command_validate(args: argparse.Namespace) -> int:
    report = validate_archive(args.data_dir, user=args.user)
    write_report(args.report, report)
    print(json.dumps(report["coverage"], ensure_ascii=False, indent=2))
    return 0


def command_update(args: argparse.Namespace) -> int:
    existing = load_archive(args.data_dir)
    client = HyperliquidClient(
        api_url=args.api_url,
        min_interval_seconds=args.min_interval,
        timeout_seconds=args.timeout,
        retries=args.http_retries,
    )
    last_error: ValidationError | None = None
    for attempt in range(1, args.consistency_attempts + 1):
        bundle = fetch_bundle(client, args.user, args.start_time_ms)
        recovered_orders, order_backfill = backfill_orders_for_fills(
            client,
            args.user,
            list(existing["historicalOrders"]) + list(bundle["events"]["historicalOrders"]),
            list(existing["userFillsByTime"]) + list(bundle["events"]["userFillsByTime"]),
        )
        if order_backfill["unresolvedOids"]:
            raise ValidationError(
                "orderStatus could not recover all fill order ids",
                {
                    "status": "failed",
                    "generatedAt": utc_now_iso(),
                    "failures": [
                        "Unresolved fill oids: "
                        + ", ".join(str(value) for value in order_backfill["unresolvedOids"])
                    ],
                    "orderStatusBackfill": order_backfill,
                },
            )
        bundle["events"]["historicalOrders"].extend(recovered_orders)
        bundle["diagnostics"]["orderStatusBackfill"] = order_backfill
        candidate, operation = merge_bundle_into_archive(existing, bundle, args.user)
        live_funding_total = sum(
            (record_usdc(row) for row in bundle["events"]["userFunding"]),
            Decimal("0"),
        )
        operation["consistencyAttempt"] = attempt
        operation["liveFundingUsdcTotal"] = str(live_funding_total)
        operation["dryRun"] = bool(args.dry_run)
        try:
            if args.dry_run:
                validation = validate_candidate_without_publish(
                    args.data_dir,
                    candidate,
                    user=args.user,
                    expected_funding_total=live_funding_total,
                )
            else:
                validation = publish_candidate(
                    args.data_dir,
                    candidate,
                    user=args.user,
                    expected_funding_total=live_funding_total,
                    allow_replace=True,
                )
        except ValidationError as exc:
            last_error = exc
            if attempt < args.consistency_attempts:
                continue
            failure_report = combined_report(
                operation,
                exc.report
                or {
                    "status": "failed",
                    "failures": [str(exc)],
                    "generatedAt": utc_now_iso(),
                },
            )
            write_report(args.report, failure_report)
            raise
        report = combined_report(operation, validation)
        write_report(args.report, report)
        print(json.dumps(report["validation"]["coverage"], ensure_ascii=False, indent=2))
        return 0
    raise last_error or ValidationError("No consistency attempt completed")


def path_argument(value: str) -> Path:
    return Path(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch, merge, and validate Paulwei's complete Hyperliquid BTC history."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    update = subparsers.add_parser(
        "update", help="Fetch today's APIs, validate a candidate merge, then publish locally."
    )
    update.add_argument("--data-dir", type=path_argument, default=Path("data"))
    update.add_argument("--report", type=path_argument, default=Path(".local/update.json"))
    update.add_argument("--user", default=USER_ADDRESS)
    update.add_argument("--start-time-ms", type=int, default=ACCOUNT_START_MS)
    update.add_argument("--api-url", default=API_URL)
    update.add_argument("--min-interval", type=float, default=0.35)
    update.add_argument("--timeout", type=float, default=30.0)
    update.add_argument("--http-retries", type=int, default=5)
    update.add_argument("--consistency-attempts", type=int, default=3)
    update.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate a serialized candidate without replacing the data directory.",
    )
    update.set_defaults(function=command_update)

    validate = subparsers.add_parser(
        "validate", help="Independently validate the current canonical data directory."
    )
    validate.add_argument("--data-dir", type=path_argument, default=Path("data"))
    validate.add_argument("--report", type=path_argument, default=Path(".local/validate.json"))
    validate.add_argument("--user", default=USER_ADDRESS)
    validate.set_defaults(function=command_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.function(args))
    except ValidationError as exc:
        if exc.report is not None and getattr(args, "report", None):
            write_report(args.report, exc.report)
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        if exc.report and exc.report.get("failures"):
            for failure in exc.report["failures"]:
                print(f"- {failure}", file=sys.stderr)
        return 2
    except TrackerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
