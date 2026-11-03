"""Read-only IF market-data verification, with no trading operations.

The weekday clock check is only a diagnostic hint.  It is deliberately not an
exchange holiday calendar.  SDK initialization and get_quote can block inside
the SDK, so callers should enforce a separate process-level timeout.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, time as clock_time, timedelta, timezone
from typing import Any, Mapping, Sequence


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAX_QUOTE_AGE_SECONDS = 10.0
MAX_FUTURE_SKEW_SECONDS = 2.0
MIN_FRESH_UPDATES = 2
IF_CONTRACT = re.compile(r"CFFEX\.IF(?P<year>\d{2})(?P<month>0[1-9]|1[0-2])\Z")

_NUMERIC_FIELDS = (
    "last_price", "bid_price1", "ask_price1", "bid_volume1", "ask_volume1",
    "volume", "open_interest", "price_tick", "volume_multiple", "expire_datetime",
)
_QUOTE_FIELDS = (
    "datetime", "instrument_id", "exchange_id", "ins_class", "expired",
    *_NUMERIC_FIELDS,
)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _china_datetime(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=CHINA_TZ)
    return value.astimezone(CHINA_TZ)


def _contract_name(value: Any) -> str | None:
    return value if isinstance(value, str) and IF_CONTRACT.fullmatch(value) else None


def _safe_quote(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Allowlist fields; discard arbitrary strings, credentials, NaN and infinity."""
    stamp = _china_datetime(raw.get("datetime"))
    result: dict[str, Any] = {
        "datetime": stamp.isoformat() if stamp else None,
        "instrument_id": _contract_name(raw.get("instrument_id")),
        "exchange_id": "CFFEX" if raw.get("exchange_id") == "CFFEX" else None,
        "ins_class": "FUTURE" if raw.get("ins_class") == "FUTURE" else None,
        "expired": raw.get("expired") if type(raw.get("expired")) is bool else None,
    }
    result.update({field: _number(raw.get(field)) for field in _NUMERIC_FIELDS})
    return result


def is_weekday_session_window(now: datetime) -> bool:
    """Whether the weekday and clock suggest IF continuous trading, not a holiday check."""
    local = _china_datetime(now)
    if local is None or local.weekday() >= 5:
        return False
    clock = local.time().replace(tzinfo=None)
    return (
        clock_time(9, 30) <= clock < clock_time(11, 30)
        or clock_time(13) <= clock < clock_time(15)
    )


def _snapshot_checks(
    real_contract: str | None, quote: Mapping[str, Any], now: datetime
) -> dict[str, bool]:
    match = IF_CONTRACT.fullmatch(real_contract or "")
    contract_month = (
        (2000 + int(match.group("year")), int(match.group("month")))
        if match else None
    )
    expiry = quote.get("expire_datetime")
    stamp = _china_datetime(quote.get("datetime"))
    price = quote.get("last_price")
    bid, ask = quote.get("bid_price1"), quote.get("ask_price1")
    bid_size, ask_size = quote.get("bid_volume1"), quote.get("ask_volume1")
    checks = {
        "symbol_is_if_future": match is not None,
        "quote_matches_requested_contract": bool(
            real_contract
            and quote.get("instrument_id") == real_contract
            and quote.get("exchange_id") == "CFFEX"
            and quote.get("ins_class") == "FUTURE"
        ),
        "contract_not_expired": bool(
            match and contract_month >= (now.year, now.month)
            and quote.get("expired") is False
            and expiry is not None and expiry > now.timestamp()
        ),
        "snapshot_has_timestamp": stamp is not None,
        "snapshot_has_price": price is not None and price > 0,
        "bid_ask_valid": bool(
            bid is not None and ask is not None and 0 < bid <= ask
            and bid_size is not None and bid_size > 0
            and ask_size is not None and ask_size > 0
        ),
    }
    checks["snapshot_valid"] = all(checks[key] for key in (
        "symbol_is_if_future", "quote_matches_requested_contract",
        "contract_not_expired", "snapshot_has_timestamp", "snapshot_has_price",
    ))
    return checks


def evaluate_market_observations(
    real_contract: str | None,
    observations: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate actual observed snapshots without connecting to any service.

    Each observation contains a ``quote`` mapping and ``received_at`` datetime
    (or ISO string). Two distinct fresh timestamp advances after the initial
    snapshot are needed to pass. Price changes alone do not prove a live feed.
    ``latency_age_seconds`` is quote age relative to the local clock, not a
    measurement of network transit latency.
    """
    local_now = _china_datetime(now if now is not None else datetime.now(CHINA_TZ))
    if local_now is None:
        raise ValueError("now must be a datetime")
    contract = _contract_name(real_contract)
    records: list[tuple[dict[str, Any], datetime | None]] = []
    for observation in observations:
        raw = observation.get("quote")
        if isinstance(raw, Mapping):
            records.append((_safe_quote(raw), _china_datetime(observation.get("received_at"))))

    latest = records[-1][0] if records else _safe_quote({})
    checks = _snapshot_checks(contract, latest, local_now)
    latest_time = _china_datetime(latest.get("datetime"))
    age = (local_now - latest_time).total_seconds() if latest_time else None
    checks["quote_is_fresh"] = (
        age is not None and -MAX_FUTURE_SKEW_SECONDS <= age <= MAX_QUOTE_AGE_SECONDS
    )

    latest_seen_time: datetime | None = None
    last_receipt: datetime | None = None
    updates, fresh_updates = 0, 0
    monotonic = True
    receipts_valid = True
    for quote, receipt in records:
        stamp = _china_datetime(quote.get("datetime"))
        if receipt is None or receipt > local_now:
            receipts_valid = False
        if receipt is not None:
            if last_receipt is not None and receipt < last_receipt:
                receipts_valid = False
            last_receipt = receipt
        if stamp is None:
            continue
        if latest_seen_time is not None and stamp < latest_seen_time:
            monotonic = False
        if latest_seen_time is not None and stamp > latest_seen_time:
            updates += 1
            receipt_age = (receipt - stamp).total_seconds() if receipt else None
            if (
                receipt_age is not None
                and -MAX_FUTURE_SKEW_SECONDS <= receipt_age <= MAX_QUOTE_AGE_SECONDS
                and _snapshot_checks(contract, quote, receipt)["snapshot_valid"]
            ):
                fresh_updates += 1
        if latest_seen_time is None or stamp > latest_seen_time:
            latest_seen_time = stamp

    checks.update({
        "timestamp_advanced": updates > 0,
        "fresh_updates_observed": fresh_updates >= MIN_FRESH_UPDATES,
        "timestamps_monotonic": monotonic,
        "receipt_times_valid": receipts_valid and bool(records),
        "weekday_session_window": is_weekday_session_window(local_now),
        "exchange_calendar_verified": False,
    })
    live = all(checks[key] for key in (
        "snapshot_valid", "bid_ask_valid", "quote_is_fresh",
        "fresh_updates_observed", "timestamps_monotonic", "receipt_times_valid",
    ))
    if live:
        status, error_code = "passed_live", None
    elif not checks["snapshot_valid"]:
        status = "failed"
        if not checks["symbol_is_if_future"]:
            error_code = "invalid_if_contract"
        elif not checks["quote_matches_requested_contract"]:
            error_code = "contract_metadata_mismatch"
        elif not checks["contract_not_expired"]:
            error_code = "expired_or_unknown_expiry"
        else:
            error_code = "invalid_snapshot"
    else:
        status = "partial_snapshot"
        if age is not None and age < -MAX_FUTURE_SKEW_SECONDS:
            error_code = "quote_time_in_future"
        elif not checks["quote_is_fresh"]:
            error_code = "stale_snapshot"
        elif not monotonic:
            error_code = "quote_time_regressed"
        elif not checks["receipt_times_valid"]:
            error_code = "invalid_receipt_times"
        elif not checks["bid_ask_valid"]:
            error_code = "invalid_order_book"
        else:
            error_code = "live_updates_not_observed"

    return {
        "status": status,
        "model_gate_passed": live,
        "real_contract": contract,
        "quote": latest,
        "updates": updates,
        "fresh_updates": fresh_updates,
        "latency_age_seconds": age,
        "checks": checks,
        "error_code": error_code,
    }


def _failure_report(
    error_code: str,
    real_contract: str | None = None,
    observations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    report = evaluate_market_observations(real_contract, observations)
    report["status"] = "partial_snapshot" if report["checks"]["snapshot_valid"] else "failed"
    report["model_gate_passed"] = False
    report["error_code"] = error_code
    return report


def _capture(quote: Any) -> dict[str, Any]:
    return {
        "received_at": datetime.now(CHINA_TZ),
        "quote": {field: getattr(quote, field, None) for field in _QUOTE_FIELDS},
    }


def run_market_probe(
    username: str, password: str, observation_seconds: int = 20
) -> dict[str, Any]:
    """Authenticate and observe IF quotes only; never submit or cancel orders.

    Credentials and raw exception messages are never included in the report.
    The parent process must bound total runtime, including SDK initialization.
    """
    if (
        not isinstance(username, str) or not username.strip()
        or not isinstance(password, str) or not password
    ):
        return _failure_report("missing_credentials")
    if type(observation_seconds) is not int or observation_seconds <= 0:
        return _failure_report("invalid_observation_seconds")

    try:
        from tqsdk import TqApi, TqAuth, TqSim
    except ImportError:
        return _failure_report("sdk_unavailable")
    except Exception:
        return _failure_report("sdk_import_failed")

    api = None
    real_contract = None
    observations: list[dict[str, Any]] = []
    phase = "initialization"
    report: dict[str, Any]
    try:
        api = TqApi(
            TqSim(init_balance=1_000_000),
            auth=TqAuth(username, password),
            disable_print=True,
            debug=False,
        )
        phase = "main_contract"
        main = api.get_quote("KQ.m@CFFEX.IF")
        mapping_deadline = time.time() + observation_seconds
        while not getattr(main, "underlying_symbol", ""):
            if not api.wait_update(deadline=mapping_deadline):
                break
        real_contract = _contract_name(getattr(main, "underlying_symbol", None))
        if real_contract is None:
            report = _failure_report("main_contract_unresolved")
        else:
            phase = "contract_quote"
            quote = api.get_quote(real_contract)
            observations.append(_capture(quote))
            phase = "observation"
            deadline = time.time() + observation_seconds
            while time.time() < deadline:
                if not api.wait_update(deadline=deadline):
                    break
                if api.is_changing(quote):
                    observations.append(_capture(quote))
            report = evaluate_market_observations(real_contract, observations)
    except Exception:
        report = _failure_report(f"{phase}_failed", real_contract, observations)
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                # A shutdown failure must not leak an SDK exception or turn an
                # interrupted/incomplete verification into a model gate pass.
                report = _failure_report("cleanup_failed", real_contract, observations)
    return report
