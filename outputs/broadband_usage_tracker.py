#!/usr/bin/env python3
"""
Hathway broadband usage tracker.

Every run calls Hathway's usage details API, updates a JSON file, and rebuilds a
static HTML dashboard from that JSON. Run it continuously with --watch, or use
--once from launchd/cron every hour.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import http.client
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_JSON_PATH = SCRIPT_DIR / "broadband_usage.json"
DEFAULT_HTML_PATH = SCRIPT_DIR / "broadband_usage.html"
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "hathway_config.json"

API_URL = "https://ispselfcareadmin.hathway.net/api/isp/v1/customer/usagedetails"
API_HOST = "ispselfcareadmin.hathway.net"
DEFAULT_TIMEZONE = "Asia/Kolkata"
DEFAULT_INTERVAL_SECONDS = 3600
DEFAULT_DATE_MODE = "year-to-now"

SIZE_UNITS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024**2,
    "mib": 1024**2,
    "gb": 1024**3,
    "gib": 1024**3,
    "tb": 1024**4,
    "tib": 1024**4,
}


class ConfigError(RuntimeError):
    pass


def now_local_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def utc_iso_millis(value: dt.datetime) -> str:
    return (
        value.astimezone(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_iso_datetime(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_optional_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return read_json_file(path)


def pick_config_value(
    args: argparse.Namespace,
    config: Dict[str, Any],
    arg_name: str,
    env_name: str,
    default: Optional[str] = None,
) -> Optional[str]:
    value = getattr(args, arg_name)
    if value:
        return str(value)
    if os.environ.get(env_name):
        return str(os.environ[env_name])
    if config.get(arg_name):
        return str(config[arg_name])
    return default


def pick_config_value_any(
    args: argparse.Namespace,
    config: Dict[str, Any],
    arg_name: str,
    env_names: Sequence[str],
    default: Optional[str] = None,
) -> Optional[str]:
    value = getattr(args, arg_name)
    if value:
        return str(value)
    for env_name in env_names:
        env_value = os.environ.get(env_name)
        if env_value:
            return str(env_value)
    if config.get(arg_name):
        return str(config[arg_name])
    return default


def mask_secret(value: Optional[str], visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * (len(value) - visible) + value[-visible:]


def canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def labelize_path(path: str) -> str:
    clean = re.sub(r"\[\d+\]", "", path)
    clean = clean.replace(".", " ")
    clean = re.sub(r"[_\-]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return "Value"
    return clean[:1].upper() + clean[1:]


def format_bytes(num_bytes: Optional[int]) -> str:
    if num_bytes is None:
        return "Unavailable"
    value = float(max(0, int(num_bytes)))
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            if value < 10:
                return f"{value:.2f} {unit}"
            if value < 100:
                return f"{value:.1f} {unit}"
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.0f} PB"


def format_raw_value(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return "None"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def parse_size_to_bytes(value: Any, key_hint: str = "") -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None

    key = canonical(key_hint)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        match = re.search(
            r"(?P<number>-?\d+(?:\.\d+)?)\s*(?P<unit>tib|tb|gib|gb|mib|mb|kib|kb|bytes?|b)\b",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            number = float(match.group("number"))
            unit = match.group("unit").lower()
            return max(0, int(number * SIZE_UNITS[unit]))
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text) and any(
            term in key
            for term in (
                "data",
                "usage",
                "download",
                "downloaded",
                "upload",
                "uploaded",
                "quota",
                "balance",
                "remaining",
                "volume",
                "bytes",
            )
        ):
            return max(0, int(float(text)))
        return None

    if isinstance(value, (int, float)):
        number = float(value)
        if number < 0:
            return None
        if "bytes" in key or key.endswith("byte"):
            return int(number)
        if "kb" in key or "kbyte" in key:
            return int(number * 1024)
        if "mb" in key or "mbyte" in key:
            return int(number * 1024**2)
        if "gb" in key or "gbyte" in key:
            return int(number * 1024**3)
        if "tb" in key or "tbyte" in key:
            return int(number * 1024**4)
    return None


def flatten_json(value: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    rows: List[Tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(flatten_json(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{prefix}[{index}]"
            rows.extend(flatten_json(child, child_path))
    else:
        rows.append((prefix, value))
    return rows


def best_metric(
    flat_rows: Sequence[Tuple[str, Any]],
    include_terms: Sequence[str],
    exclude_terms: Sequence[str] = (),
) -> Optional[Dict[str, Any]]:
    candidates: List[Tuple[int, Dict[str, Any]]] = []
    include_canon = [canonical(term) for term in include_terms]
    exclude_canon = [canonical(term) for term in exclude_terms]

    for path, value in flat_rows:
        path_key = canonical(path)
        if not any(term in path_key for term in include_canon):
            continue
        if any(term in path_key for term in exclude_canon):
            continue

        bytes_value = parse_size_to_bytes(value, path)
        score = 10
        if bytes_value is not None:
            score += 25
        if "total" in path_key:
            score += 5
        if "current" in path_key or "used" in path_key:
            score += 4
        if "[" not in path:
            score += 3

        candidates.append(
            (
                score,
                {
                    "path": path,
                    "label": labelize_path(path),
                    "value": value,
                    "display": format_raw_value(value),
                    "bytes": bytes_value,
                },
            )
        )

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def interesting_fields(flat_rows: Sequence[Tuple[str, Any]], limit: int = 16) -> List[Dict[str, Any]]:
    terms = [
        "usage",
        "used",
        "download",
        "upload",
        "quota",
        "remaining",
        "balance",
        "fup",
        "plan",
        "speed",
        "expiry",
        "valid",
    ]
    fields: List[Tuple[int, Dict[str, Any]]] = []

    for path, value in flat_rows:
        key = canonical(path)
        if not any(term in key for term in terms):
            continue
        if isinstance(value, (dict, list)):
            continue

        bytes_value = parse_size_to_bytes(value, path)
        score = 5
        if bytes_value is not None:
            score += 15
        if "[" not in path:
            score += 4
        if any(term in key for term in ("total", "used", "usage")):
            score += 3

        fields.append(
            (
                score,
                {
                    "path": path,
                    "label": labelize_path(path),
                    "value": value,
                    "display": format_bytes(bytes_value) if bytes_value is not None else format_raw_value(value),
                    "bytes": bytes_value,
                },
            )
        )

    fields.sort(key=lambda item: item[0], reverse=True)
    deduped: List[Dict[str, Any]] = []
    seen_labels = set()
    for _, field in fields:
        label = str(field["label"])
        if label in seen_labels:
            continue
        deduped.append(field)
        seen_labels.add(label)
        if len(deduped) >= limit:
            break
    return deduped


def iter_lists(value: Any) -> Iterable[List[Any]]:
    if isinstance(value, list):
        yield value
        for item in value:
            yield from iter_lists(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_lists(item)


def parse_date_like(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        if 946_684_800 <= timestamp <= 4_102_444_800:
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date().isoformat()

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        timestamp = int(text)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        if 946_684_800 <= timestamp <= 4_102_444_800:
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).date().isoformat()

    patterns = [
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{2})/(\d{2})/(\d{4})",
        r"(\d{2})-(\d{2})-(\d{4})",
    ]
    match = re.search(patterns[0], text)
    if match:
        return match.group(1)

    for pattern in patterns[1:]:
        match = re.search(pattern, text)
        if match:
            day, month, year = match.groups()
            return f"{year}-{month}-{day}"
    return None


def extract_date_from_record(record: Dict[str, Any]) -> Optional[str]:
    for key, value in record.items():
        key_name = canonical(str(key))
        if any(term in key_name for term in ("date", "day", "time", "timestamp", "effective")):
            parsed = parse_date_like(value)
            if parsed:
                return parsed
    for value in record.values():
        parsed = parse_date_like(value)
        if parsed:
            return parsed
    return None


def extract_record_metric(record: Dict[str, Any], terms: Sequence[str]) -> Optional[int]:
    flat_rows = flatten_json(record)
    metric = best_metric(flat_rows, terms)
    if metric and metric.get("bytes") is not None:
        return int(metric["bytes"])
    return None


def extract_daily_usage(response_json: Any) -> List[Dict[str, Any]]:
    daily_rows: List[Dict[str, Any]] = []
    for candidate_list in iter_lists(response_json):
        dict_items = [item for item in candidate_list if isinstance(item, dict)]
        if len(dict_items) < 2:
            continue

        extracted: List[Dict[str, Any]] = []
        for item in dict_items:
            day = extract_date_from_record(item)
            if not day:
                continue
            download = extract_record_metric(item, ["download", "down", "rx"])
            upload = extract_record_metric(item, ["upload", "up", "tx"])
            total = extract_record_metric(item, ["totalusage", "usage", "used", "totaldata", "volume"])
            if total is None and (download is not None or upload is not None):
                total = int(download or 0) + int(upload or 0)
            if total is None:
                continue
            extracted.append(
                {
                    "day": day,
                    "download": int(download or total),
                    "upload": int(upload or 0),
                    "total": int(total),
                }
            )

        if len(extracted) > len(daily_rows):
            daily_rows = extracted

    combined: Dict[str, Dict[str, int]] = {}
    for row in daily_rows:
        bucket = combined.setdefault(row["day"], {"download": 0, "upload": 0, "total": 0})
        bucket["download"] += int(row["download"])
        bucket["upload"] += int(row["upload"])
        bucket["total"] += int(row["total"])

    return [
        {"day": day, **values}
        for day, values in sorted(combined.items(), key=lambda item: item[0])
    ]


def normalize_response(response_json: Any) -> Dict[str, Any]:
    flat_rows = flatten_json(response_json)
    total_used = best_metric(
        flat_rows,
        ["totalusage", "dataused", "useddata", "used", "usage"],
        ["remaining", "balance", "quota", "limit", "upload", "download"],
    )
    download = best_metric(flat_rows, ["download", "downlink", "rx"], ["speed"])
    upload = best_metric(flat_rows, ["upload", "uplink", "tx"], ["speed"])
    remaining = best_metric(flat_rows, ["remaining", "balance", "left"], ["validity"])
    quota = best_metric(flat_rows, ["quota", "limit", "fup", "allowance"])
    plan = best_metric(flat_rows, ["plan", "package"])
    speed = best_metric(flat_rows, ["speed", "bandwidth"])
    expiry = best_metric(flat_rows, ["expiry", "validity", "expire", "validtill"])

    fields = interesting_fields(flat_rows)
    daily_rows = extract_daily_usage(response_json)

    return {
        "total_used": total_used,
        "download": download,
        "upload": upload,
        "remaining": remaining,
        "quota": quota,
        "plan": plan,
        "speed": speed,
        "expiry": expiry,
        "fields": fields,
        "daily_usage": daily_rows,
    }


def empty_usage_data() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "provider": "hathway",
        "api_url": API_URL,
        "created_at": now_local_iso(),
        "updated_at": None,
        "account": {},
        "latest": None,
        "history": [],
    }


def load_usage_data(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return empty_usage_data()
    data = read_json_file(path)
    data.setdefault("schema_version", 1)
    data.setdefault("provider", "hathway")
    data.setdefault("api_url", API_URL)
    data.setdefault("created_at", now_local_iso())
    data.setdefault("account", {})
    data.setdefault("history", [])
    refresh_normalized_data(data)
    return data


def refresh_normalized_data(data: Dict[str, Any]) -> None:
    samples: List[Dict[str, Any]] = []
    latest = data.get("latest")
    if isinstance(latest, dict):
        samples.append(latest)
    samples.extend(sample for sample in data.get("history", []) if isinstance(sample, dict))

    for sample in samples:
        if sample.get("error") or "response_json" not in sample:
            continue
        sample["normalized"] = normalize_response(sample["response_json"])


def remove_raw_responses(data: Dict[str, Any]) -> None:
    samples: List[Dict[str, Any]] = []
    latest = data.get("latest")
    if isinstance(latest, dict):
        samples.append(latest)
    samples.extend(sample for sample in data.get("history", []) if isinstance(sample, dict))

    for sample in samples:
        sample.pop("response_json", None)


def write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)
        file.write("\n")
    temp_path.replace(path)


def resolve_date_window(args: argparse.Namespace, config: Dict[str, Any]) -> Tuple[str, str, str]:
    timezone_name = pick_config_value(args, config, "timezone", "HATHWAY_TIMEZONE", DEFAULT_TIMEZONE)
    assert timezone_name is not None
    zone = ZoneInfo(timezone_name)
    date_mode = pick_config_value_any(
        args,
        config,
        "date_mode",
        ["HATHWAY_DATE_MODE", "USAGE_RANGE_MODE"],
        DEFAULT_DATE_MODE,
    )
    assert date_mode is not None

    configured_start = pick_config_value(args, config, "start_date", "HATHWAY_START_DATE")
    configured_end = pick_config_value(args, config, "end_date", "HATHWAY_END_DATE")
    if configured_start and configured_end:
        return configured_start, configured_end, timezone_name

    now = dt.datetime.now(zone)
    if date_mode == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif date_mode == "last-30-days":
        start = now - dt.timedelta(days=30)
    elif date_mode == "month-to-now":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    return utc_iso_millis(start), utc_iso_millis(now), timezone_name


def build_auth_config(args: argparse.Namespace) -> Dict[str, str]:
    config = load_optional_config(args.config)

    authorization = pick_config_value_any(
        args,
        config,
        "authorization",
        ["HATHWAY_AUTHORIZATION", "HATHWAY_AUTH_TOKEN"],
    )
    account_no = pick_config_value(args, config, "account_no", "HATHWAY_ACCOUNT_NO")
    registered_mobile_no = pick_config_value_any(
        args,
        config,
        "registered_mobile_no",
        ["HATHWAY_REGISTERED_MOBILE_NO", "HATHWAY_MOBILE_NO"],
    )
    login_device = pick_config_value(args, config, "login_device", "HATHWAY_LOGIN_DEVICE", "web")
    start_date, end_date, timezone_name = resolve_date_window(args, config)

    missing = []
    if not authorization:
        missing.append("HATHWAY_AUTHORIZATION or HATHWAY_AUTH_TOKEN")
    if not account_no:
        missing.append("HATHWAY_ACCOUNT_NO")
    if not registered_mobile_no:
        missing.append("HATHWAY_REGISTERED_MOBILE_NO or HATHWAY_MOBILE_NO")
    if missing:
        raise ConfigError(
            "Missing required Hathway settings: "
            + ", ".join(missing)
            + f". Set them as environment variables or create {args.config} from hathway_config.example.json."
        )

    return {
        "authorization": str(authorization),
        "account_no": str(account_no),
        "registered_mobile_no": str(registered_mobile_no),
        "login_device": str(login_device or "web"),
        "start_date": start_date,
        "end_date": end_date,
        "timezone": timezone_name,
    }


def build_payload(auth_config: Dict[str, str]) -> Dict[str, str]:
    return {
        "account_no": auth_config["account_no"],
        "start_date": auth_config["start_date"],
        "end_date": auth_config["end_date"],
        "registered_mobile_no": auth_config["registered_mobile_no"],
        "login_device": auth_config["login_device"],
    }


def post_hathway_usage(auth_config: Dict[str, str], timeout: int) -> Tuple[int, Any, str]:
    payload_bytes = json.dumps(build_payload(auth_config), separators=(",", ":")).encode("utf-8")
    parsed_url = urllib.parse.urlparse(API_URL)
    host = parsed_url.hostname or API_HOST
    path = parsed_url.path or "/"
    if parsed_url.query:
        path = f"{path}?{parsed_url.query}"

    headers = {
        "Host": host,
        "Authorization": auth_config["authorization"],
        "Sec-CH-UA-Platform": '"macOS"',
        "Sec-CH-UA": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9,ne;q=0.8,kn;q=0.7",
        "Content-Type": "application/json",
        "Content-Length": str(len(payload_bytes)),
        "Origin": "https://ispselfcare.hathway.net",
        "DNT": "1",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://ispselfcare.hathway.net/",
        "Priority": "u=1, i",
        "Connection": "close",
    }

    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(
        host=host,
        port=parsed_url.port or 443,
        timeout=timeout,
        context=context,
    )

    try:
        connection.request("POST", path, body=payload_bytes, headers=headers)
        response = connection.getresponse()
        raw_body = response.read()
        content_encoding = (response.getheader("Content-Encoding") or "").lower()
        if "gzip" in content_encoding:
            raw_body = gzip.decompress(raw_body)
        elif "deflate" in content_encoding:
            raw_body = zlib.decompress(raw_body)
        raw_text = raw_body.decode("utf-8", errors="replace")
        status_code = int(response.status)
    finally:
        connection.close()

    try:
        response_json = json.loads(raw_text)
    except json.JSONDecodeError:
        response_json = {"raw_response": raw_text}

    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"Hathway API returned HTTP {status_code}: {raw_text[:300]}")

    return status_code, response_json, raw_text


def append_sample(
    data: Dict[str, Any],
    auth_config: Dict[str, str],
    status_code: int,
    response_json: Any,
    duration_ms: int,
    keep_history: int,
    include_raw_response: bool,
) -> Dict[str, Any]:
    timestamp = now_local_iso()
    normalized = normalize_response(response_json)
    request_window = {
        "start_date": auth_config["start_date"],
        "end_date": auth_config["end_date"],
        "timezone": auth_config["timezone"],
    }
    sample = {
        "timestamp": timestamp,
        "http_status": status_code,
        "duration_ms": duration_ms,
        "request_window": request_window,
        "normalized": normalized,
    }
    if include_raw_response:
        sample["response_json"] = response_json

    data["updated_at"] = timestamp
    data["account"] = {
        "account_no_masked": mask_secret(auth_config.get("account_no")),
        "registered_mobile_no_masked": mask_secret(auth_config.get("registered_mobile_no")),
        "login_device": auth_config.get("login_device", "web"),
    }
    data["latest"] = sample
    history = data.setdefault("history", [])
    history.append(sample)
    if keep_history > 0 and len(history) > keep_history:
        del history[: len(history) - keep_history]
    return data


def append_error_sample(
    data: Dict[str, Any],
    error_message: str,
    keep_history: int,
) -> Dict[str, Any]:
    timestamp = now_local_iso()
    sample = {
        "timestamp": timestamp,
        "http_status": None,
        "duration_ms": 0,
        "request_window": {},
        "normalized": {},
        "error": error_message,
    }
    data["updated_at"] = timestamp
    data["latest"] = sample
    history = data.setdefault("history", [])
    history.append(sample)
    if keep_history > 0 and len(history) > keep_history:
        del history[: len(history) - keep_history]
    return data


def metric_display(metric: Optional[Dict[str, Any]]) -> str:
    if not metric:
        return "Unavailable"
    bytes_value = metric.get("bytes")
    if bytes_value is not None:
        return format_bytes(int(bytes_value))
    return format_raw_value(metric.get("value"))


def metric_hint(metric: Optional[Dict[str, Any]], fallback: str) -> str:
    if not metric:
        return fallback
    return str(metric.get("label") or fallback)


def parse_day(value: Any) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def normalized_daily_usage_rows(daily_usage: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    combined: Dict[str, Dict[str, int]] = {}
    for row in daily_usage:
        day = parse_day(row.get("day"))
        if not day:
            continue
        key = day.isoformat()
        bucket = combined.setdefault(key, {"download": 0, "upload": 0, "total": 0})
        bucket["download"] += int(row.get("download", row.get("total", 0)) or 0)
        bucket["upload"] += int(row.get("upload", 0) or 0)
        bucket["total"] += int(row.get("total", 0) or 0)

    return [
        {"day": day, **values}
        for day, values in sorted(combined.items(), key=lambda item: item[0])
    ]


def local_today(timezone_name: str) -> dt.date:
    try:
        return dt.datetime.now(ZoneInfo(timezone_name)).date()
    except Exception:
        return dt.datetime.now().astimezone().date()


def usage_sum(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "download": sum(int(row.get("download", row.get("total", 0)) or 0) for row in rows),
        "upload": sum(int(row.get("upload", 0) or 0) for row in rows),
        "total": sum(int(row.get("total", 0) or 0) for row in rows),
    }


def rows_between(
    rows: Sequence[Dict[str, Any]],
    start: Optional[dt.date],
    end: Optional[dt.date],
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for row in rows:
        day = parse_day(row.get("day"))
        if not day:
            continue
        if start and day < start:
            continue
        if end and day > end:
            continue
        selected.append(row)
    return selected


def describe_period(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return "No daily rows"
    totals = usage_sum(rows)
    average = totals["total"] // max(1, len(rows))
    peak = max(rows, key=lambda row: int(row.get("total", 0) or 0))
    return f"{len(rows)} days | Avg {format_bytes(average)}/day | Peak {format_bytes(int(peak.get('total', 0) or 0))}"


def period_summaries(
    daily_usage: Sequence[Dict[str, Any]],
    timezone_name: str,
    overall_fallback_bytes: Optional[int] = None,
) -> List[Dict[str, Any]]:
    rows = normalized_daily_usage_rows(daily_usage)
    today = local_today(timezone_name)
    week_start = today - dt.timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    daily_rows = rows_between(rows, today, today)
    daily_label = "Today"
    daily_hint = None
    if not daily_rows and rows:
        daily_rows = [rows[-1]]
        daily_label = "Latest Day"
        daily_hint = (
            f"{rows[-1]['day']} | Download {format_bytes(int(rows[-1].get('download', rows[-1].get('total', 0)) or 0))} "
            f"| Upload {format_bytes(int(rows[-1].get('upload', 0) or 0))}"
        )

    periods = [
        (daily_label, daily_rows, "accent-blue", daily_hint),
        ("This Week", rows_between(rows, week_start, today), "accent-teal"),
        ("This Month", rows_between(rows, month_start, today), "accent-orange"),
        ("This Year", rows_between(rows, year_start, today), "accent-rose"),
        ("Overall", rows, "accent-green"),
    ]

    summaries = []
    for period in periods:
        label, period_rows, tone = period[:3]
        custom_hint = period[3] if len(period) > 3 else None
        totals = usage_sum(period_rows)
        if label == "Overall" and totals["total"] == 0 and overall_fallback_bytes is not None:
            totals["total"] = overall_fallback_bytes
            totals["download"] = overall_fallback_bytes
        summaries.append(
            {
                "label": label,
                "rows": period_rows,
                "download": totals["download"],
                "upload": totals["upload"],
                "total": totals["total"],
                "hint": custom_hint or describe_period(period_rows),
                "tone": tone,
            }
        )
    return summaries


def bucket_usage(rows: Sequence[Dict[str, Any]], bucket: str) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in normalized_daily_usage_rows(rows):
        day = parse_day(row.get("day"))
        if not day:
            continue

        if bucket == "weekly":
            iso = day.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
            label = f"W{iso.week:02d} {iso.year}"
            sort_key = (iso.year, iso.week)
        elif bucket == "monthly":
            key = f"{day.year}-{day.month:02d}"
            label = day.strftime("%b %Y")
            sort_key = (day.year, day.month)
        elif bucket == "yearly":
            key = str(day.year)
            label = key
            sort_key = (day.year,)
        else:
            key = day.isoformat()
            label = day.strftime("%d %b")
            sort_key = (day.year, day.month, day.day)

        item = buckets.setdefault(
            key,
            {
                "label": label,
                "sort_key": sort_key,
                "download": 0,
                "upload": 0,
                "total": 0,
            },
        )
        item["download"] += int(row.get("download", row.get("total", 0)) or 0)
        item["upload"] += int(row.get("upload", 0) or 0)
        item["total"] += int(row.get("total", 0) or 0)

    return [
        {key: value for key, value in item.items() if key != "sort_key"}
        for item in sorted(buckets.values(), key=lambda item: item["sort_key"])
    ]


def render_period_cards(summaries: Sequence[Dict[str, Any]]) -> str:
    if not summaries:
        return '<div class="empty">No period data yet.</div>'

    peak = max(max(int(summary.get("total", 0) or 0), 1) for summary in summaries)
    cards = []
    for summary in summaries:
        total = int(summary.get("total", 0) or 0)
        width = max(4, min(100, round(total * 100 / peak)))
        cards.append(
            f'<article class="period-card {html.escape(str(summary.get("tone", "")))}">'
            f'<span>{html.escape(str(summary.get("label", "")))}</span>'
            f'<strong>{html.escape(format_bytes(total))}</strong>'
            f'<small>{html.escape(str(summary.get("hint", "")))}</small>'
            f'<div class="meter"><i style="width: {width}%"></i></div>'
            "</article>"
        )
    return "\n".join(cards)


def latest_normalized(data: Dict[str, Any]) -> Dict[str, Any]:
    latest = data.get("latest") or {}
    normalized = latest.get("normalized") or {}
    return normalized if isinstance(normalized, dict) else {}


def history_metric_points(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    for sample in data.get("history", []):
        if not isinstance(sample, dict) or sample.get("error"):
            continue
        normalized = sample.get("normalized") or {}
        total = normalized.get("total_used") if isinstance(normalized, dict) else None
        if isinstance(total, dict) and total.get("bytes") is not None:
            points.append(
                {
                    "timestamp": str(sample.get("timestamp")),
                    "bytes": int(total["bytes"]),
                }
            )
    return points


def svg_history_line(points: Sequence[Dict[str, Any]], width: int = 920, height: int = 260) -> str:
    chart_points = list(points[-72:])
    if len(chart_points) < 2:
        return (
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Usage history chart">'
            f'<rect width="{width}" height="{height}" rx="8" fill="#ffffff"/>'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" fill="#667085" '
            f'font-size="15">Trend appears after two successful API samples</text></svg>'
        )

    padding_left = 44
    padding_right = 20
    padding_top = 18
    padding_bottom = 34
    inner_width = width - padding_left - padding_right
    inner_height = height - padding_top - padding_bottom
    values = [int(point["bytes"]) for point in chart_points]
    min_value = min(values)
    max_value = max(values)
    spread = max(max_value - min_value, 1)
    coords = []
    for index, value in enumerate(values):
        x = padding_left + inner_width * index / max(1, len(values) - 1)
        y = padding_top + inner_height - (inner_height * (value - min_value) / spread)
        coords.append((x, y))

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = (
        f"{padding_left},{padding_top + inner_height} "
        + line
        + f" {padding_left + inner_width},{padding_top + inner_height}"
    )

    grid = []
    for tick in range(5):
        y = padding_top + inner_height * tick / 4
        value = int(max_value - spread * tick / 4)
        grid.append(
            f'<line x1="{padding_left}" y1="{y:.1f}" x2="{padding_left + inner_width}" y2="{y:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1"/>'
            f'<text x="{padding_left - 10}" y="{y + 4:.1f}" text-anchor="end" fill="#667085" '
            f'font-size="11">{html.escape(format_bytes(value))}</text>'
        )

    first = html.escape(parse_iso_datetime(chart_points[0]["timestamp"]).strftime("%b %d %H:%M"))
    last = html.escape(parse_iso_datetime(chart_points[-1]["timestamp"]).strftime("%b %d %H:%M"))

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Usage history chart">'
        f'<rect width="{width}" height="{height}" rx="8" fill="#ffffff"/>'
        + "".join(grid)
        + f'<polygon points="{area}" fill="#dbeafe" opacity="0.85"/>'
        + f'<polyline points="{line}" fill="none" stroke="#2563eb" stroke-width="4" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        + "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#2563eb"/>' for x, y in coords[-14:])
        + f'<text x="{padding_left}" y="{height - 10}" fill="#667085" font-size="12">{first}</text>'
        + f'<text x="{padding_left + inner_width}" y="{height - 10}" text-anchor="end" fill="#667085" '
        f'font-size="12">{last}</text>'
        + "</svg>"
    )


def svg_daily_bars(days: Sequence[Dict[str, Any]], width: int = 920, height: int = 250) -> str:
    chart_days = list(days[-18:])
    if not chart_days:
        return (
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Daily usage chart">'
            f'<rect width="{width}" height="{height}" rx="8" fill="#ffffff"/>'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" fill="#667085" '
            f'font-size="15">No daily rows were found in the latest API response</text></svg>'
        )

    padding_left = 40
    padding_right = 20
    padding_top = 18
    padding_bottom = 44
    inner_width = width - padding_left - padding_right
    inner_height = height - padding_top - padding_bottom
    max_total = max(max(int(day["total"]) for day in chart_days), 1)
    gap = 8
    bar_width = max(10, (inner_width - gap * (len(chart_days) - 1)) / len(chart_days))

    bars = []
    for index, day in enumerate(chart_days):
        total = int(day["total"])
        download = int(day.get("download", total))
        upload = int(day.get("upload", 0))
        total_height = inner_height * total / max_total
        download_height = total_height * (download / total) if total else 0
        upload_height = total_height - download_height
        x = padding_left + index * (bar_width + gap)
        y_download = padding_top + inner_height - download_height
        y_upload = y_download - upload_height
        label = html.escape(str(day["day"])[5:])
        bars.append(
            f'<rect x="{x:.1f}" y="{y_upload:.1f}" width="{bar_width:.1f}" height="{upload_height:.1f}" '
            f'rx="4" fill="#f97316"/>'
            f'<rect x="{x:.1f}" y="{y_download:.1f}" width="{bar_width:.1f}" height="{download_height:.1f}" '
            f'rx="4" fill="#0f766e"/>'
            f'<text x="{x + bar_width / 2:.1f}" y="{height - 18}" text-anchor="middle" fill="#667085" '
            f'font-size="11">{label}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Daily usage chart">'
        f'<rect width="{width}" height="{height}" rx="8" fill="#ffffff"/>'
        f'<line x1="{padding_left}" y1="{padding_top + inner_height}" x2="{padding_left + inner_width}" '
        f'y2="{padding_top + inner_height}" stroke="#e5e7eb" stroke-width="1"/>'
        + "".join(bars)
        + f'<text x="{padding_left}" y="14" fill="#667085" font-size="12">Peak day: '
        f'{html.escape(format_bytes(max_total))}</text>'
        + "</svg>"
    )


def svg_usage_bars(
    rows: Sequence[Dict[str, Any]],
    empty_message: str,
    aria_label: str,
    limit: int,
    width: int = 920,
    height: int = 250,
) -> str:
    chart_rows = list(rows[-limit:])
    if not chart_rows:
        return (
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(aria_label)}">'
            f'<rect width="{width}" height="{height}" rx="8" fill="#ffffff"/>'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" fill="#667085" '
            f'font-size="15">{html.escape(empty_message)}</text></svg>'
        )

    padding_left = 42
    padding_right = 20
    padding_top = 18
    padding_bottom = 46
    inner_width = width - padding_left - padding_right
    inner_height = height - padding_top - padding_bottom
    max_total = max(max(int(row.get("total", 0) or 0) for row in chart_rows), 1)
    gap = 8
    bar_width = max(10, (inner_width - gap * (len(chart_rows) - 1)) / len(chart_rows))
    label_every = max(1, len(chart_rows) // 10)

    bars = []
    for index, row in enumerate(chart_rows):
        total = int(row.get("total", 0) or 0)
        download = int(row.get("download", total) or 0)
        upload = int(row.get("upload", 0) or 0)
        total_height = inner_height * total / max_total
        download_height = total_height * (download / total) if total else 0
        upload_height = total_height - download_height
        x = padding_left + index * (bar_width + gap)
        y_download = padding_top + inner_height - download_height
        y_upload = y_download - upload_height
        label = html.escape(str(row.get("label", row.get("day", ""))))
        label_svg = ""
        if index % label_every == 0 or index == len(chart_rows) - 1:
            label_svg = (
                f'<text x="{x + bar_width / 2:.1f}" y="{height - 18}" text-anchor="middle" '
                f'fill="#667085" font-size="11">{label}</text>'
            )

        bars.append(
            f'<rect x="{x:.1f}" y="{y_upload:.1f}" width="{bar_width:.1f}" height="{upload_height:.1f}" '
            f'rx="4" fill="#f97316"/>'
            f'<rect x="{x:.1f}" y="{y_download:.1f}" width="{bar_width:.1f}" height="{download_height:.1f}" '
            f'rx="4" fill="#0f766e"/>'
            f'{label_svg}'
        )

    grid = []
    for tick in range(4):
        y = padding_top + inner_height * tick / 3
        value = int(max_total * (1 - tick / 3))
        grid.append(
            f'<line x1="{padding_left}" y1="{y:.1f}" x2="{padding_left + inner_width}" y2="{y:.1f}" '
            f'stroke="#e5e7eb" stroke-width="1"/>'
            f'<text x="{padding_left - 10}" y="{y + 4:.1f}" text-anchor="end" fill="#667085" '
            f'font-size="11">{html.escape(format_bytes(value))}</text>'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(aria_label)}">'
        f'<rect width="{width}" height="{height}" rx="8" fill="#ffffff"/>'
        + "".join(grid)
        + f'<line x1="{padding_left}" y1="{padding_top + inner_height}" x2="{padding_left + inner_width}" '
        f'y2="{padding_top + inner_height}" stroke="#e5e7eb" stroke-width="1"/>'
        + "".join(bars)
        + "</svg>"
    )


def render_top_days(rows: Sequence[Dict[str, Any]], limit: int = 8) -> str:
    sorted_rows = sorted(
        normalized_daily_usage_rows(rows),
        key=lambda row: int(row.get("total", 0) or 0),
        reverse=True,
    )
    if not sorted_rows:
        return '<tr><td colspan="4">No daily usage rows yet.</td></tr>'

    rendered = []
    for row in sorted_rows[:limit]:
        rendered.append(
            "<tr>"
            f'<td>{html.escape(str(row.get("day", "")))}</td>'
            f'<td>{html.escape(format_bytes(int(row.get("download", row.get("total", 0)) or 0)))}</td>'
            f'<td>{html.escape(format_bytes(int(row.get("upload", 0) or 0)))}</td>'
            f'<td>{html.escape(format_bytes(int(row.get("total", 0) or 0)))}</td>'
            "</tr>"
        )
    return "\n".join(rendered)


def render_field_cards(fields: Sequence[Dict[str, Any]]) -> str:
    if not fields:
        return '<div class="empty">No recognizable usage fields were found yet.</div>'
    return "\n".join(
        '<article class="field-card">'
        f'<span>{html.escape(str(field.get("label", "Field")))}</span>'
        f'<strong>{html.escape(str(field.get("display", "")))}</strong>'
        f'<small>{html.escape(str(field.get("path", "")))}</small>'
        "</article>"
        for field in fields[:12]
    )


def render_history_rows(data: Dict[str, Any]) -> str:
    rows = []
    for sample in reversed(data.get("history", [])[-12:]):
        timestamp = html.escape(str(sample.get("timestamp", "")))
        status = sample.get("http_status")
        duration = sample.get("duration_ms")
        window = sample.get("request_window") or {}
        start = str(window.get("start_date", ""))[:10]
        end = str(window.get("end_date", ""))[:10]
        normalized = sample.get("normalized") or {}
        total = metric_display(normalized.get("total_used")) if isinstance(normalized, dict) else "Unavailable"
        result = "Error" if sample.get("error") else f"HTTP {status}"
        rows.append(
            "<tr>"
            f"<td>{timestamp}</td>"
            f"<td>{html.escape(start)} to {html.escape(end)}</td>"
            f"<td>{html.escape(total)}</td>"
            f"<td>{html.escape(result)}</td>"
            f"<td>{html.escape(str(duration or 0))} ms</td>"
            "</tr>"
        )
    if not rows:
        return '<tr><td colspan="5">No samples yet.</td></tr>'
    return "\n".join(rows)


def render_raw_response(data: Dict[str, Any]) -> str:
    latest = data.get("latest") or {}
    if latest.get("error"):
        return json.dumps({"error": latest["error"]}, indent=2, ensure_ascii=False)
    response_json = latest.get("response_json")
    if response_json is None:
        return json.dumps({"message": "Raw API response was omitted."}, indent=2)
    return json.dumps(response_json, indent=2, ensure_ascii=False)


def render_html(data: Dict[str, Any], json_path: Path) -> str:
    normalized = latest_normalized(data)
    latest = data.get("latest") or {}
    account = data.get("account") or {}
    daily_usage = normalized_daily_usage_rows(normalized.get("daily_usage") or [])
    history_points = history_metric_points(data)
    error = latest.get("error")

    total_used = normalized.get("total_used")
    remaining = normalized.get("remaining")
    quota = normalized.get("quota")
    plan = normalized.get("plan")
    speed = normalized.get("speed")
    expiry = normalized.get("expiry")
    fields = normalized.get("fields") or []

    updated_at = str(data.get("updated_at") or "Not sampled yet")
    request_window = latest.get("request_window") or {}
    timezone_name = str(request_window.get("timezone", DEFAULT_TIMEZONE))
    period = ""
    if request_window:
        period = f"{request_window.get('start_date', '')} to {request_window.get('end_date', '')}"

    overall_fallback = None
    if isinstance(total_used, dict) and total_used.get("bytes") is not None:
        overall_fallback = int(total_used["bytes"])
    summaries = period_summaries(daily_usage, timezone_name, overall_fallback)
    daily_rows = [{"label": str(row["day"])[5:], **row} for row in daily_usage]
    weekly_rows = bucket_usage(daily_usage, "weekly")
    monthly_rows = bucket_usage(daily_usage, "monthly")
    yearly_rows = bucket_usage(daily_usage, "yearly")

    status_class = "bad" if error else "good"
    status_label = "API error" if error else ("Ready" if latest else "Waiting for first sample")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hathway Broadband Usage</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --panel: #ffffff;
      --ink: #182230;
      --muted: #667085;
      --line: #d7dee9;
      --blue: #2563eb;
      --teal: #0f766e;
      --orange: #f97316;
      --rose: #e11d48;
      --green: #16a34a;
      --shadow: 0 18px 50px rgba(16, 24, 40, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: linear-gradient(180deg, #e9f4ff 0, #f7faf9 24rem, var(--bg) 24rem);
    }}
    main {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}
    header {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 22px;
      align-items: center;
      margin-bottom: 18px;
      padding: 24px;
      border-radius: 8px;
      background: linear-gradient(135deg, #101828 0%, #164e63 52%, #0f766e 100%);
      color: #ffffff;
      box-shadow: var(--shadow);
    }}
    .eyebrow {{
      margin: 0 0 10px;
      color: #a7f3d0;
      font-size: 0.78rem;
      font-weight: 850;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: clamp(2rem, 6vw, 4.1rem);
      line-height: 0.96;
      letter-spacing: 0;
    }}
    .subhead {{
      margin: 0;
      color: #dbeafe;
      font-size: 1rem;
      line-height: 1.5;
      max-width: 780px;
      overflow-wrap: anywhere;
    }}
    .status {{
      min-width: 260px;
      padding: 16px;
      border: 1px solid rgba(255, 255, 255, 0.22);
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.12);
    }}
    .status b {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 0.9rem;
    }}
    .dot {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
      background: #22c55e;
    }}
    .bad .dot {{ background: #fb7185; }}
    .status span {{
      display: block;
      margin-top: 8px;
      color: #dbeafe;
      font-size: 0.92rem;
      overflow-wrap: anywhere;
    }}
    .period-grid {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 14px;
      margin: 20px 0;
    }}
    .period-card {{
      min-height: 170px;
      padding: 18px;
      border: 1px solid var(--line);
      border-top: 5px solid var(--accent, var(--blue));
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .period-card span {{
      color: var(--muted);
      font-size: 0.76rem;
      font-weight: 850;
      text-transform: uppercase;
    }}
    .period-card strong {{
      display: block;
      margin-top: 12px;
      font-size: clamp(1.55rem, 4vw, 2.35rem);
      line-height: 1.02;
      overflow-wrap: anywhere;
    }}
    .period-card small {{
      display: block;
      min-height: 42px;
      margin-top: 10px;
      color: var(--muted);
      line-height: 1.35;
    }}
    .meter {{
      height: 8px;
      margin-top: 14px;
      border-radius: 999px;
      background: #e8eef5;
      overflow: hidden;
    }}
    .meter i {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: var(--accent, var(--blue));
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin: 18px 0 24px;
    }}
    .metric {{
      min-height: 138px;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .metric .label {{
      color: var(--muted);
      font-size: 0.76rem;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .metric .value {{
      margin-top: 12px;
      font-size: clamp(1.45rem, 4vw, 2.35rem);
      line-height: 1.04;
      font-weight: 850;
      overflow-wrap: anywhere;
    }}
    .metric .hint {{
      margin-top: 11px;
      color: var(--muted);
      font-size: 0.9rem;
      overflow-wrap: anywhere;
    }}
    .accent-blue {{ --accent: var(--blue); border-top-color: var(--blue); }}
    .accent-teal {{ --accent: var(--teal); border-top-color: var(--teal); }}
    .accent-orange {{ --accent: var(--orange); border-top-color: var(--orange); }}
    .accent-rose {{ --accent: var(--rose); border-top-color: var(--rose); }}
    .accent-green {{ --accent: var(--green); border-top-color: var(--green); }}
    .section {{
      margin-top: 18px;
    }}
    .chart-grid, .split-grid {{
      display: grid;
      gap: 14px;
      margin-top: 18px;
    }}
    .chart-grid {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .split-grid {{
      grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
    }}
    .chart-panel, .table-panel {{
      padding: 20px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .full-width {{
      grid-column: 1 / -1;
    }}
    .section-head {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 16px;
      margin-bottom: 16px;
    }}
    h2 {{
      margin: 0;
      font-size: 1.12rem;
    }}
    .note {{
      color: var(--muted);
      font-size: 0.92rem;
      text-align: right;
      overflow-wrap: anywhere;
    }}
    .chart {{
      width: 100%;
      overflow: hidden;
    }}
    .chart svg {{
      display: block;
      width: 100%;
      height: auto;
    }}
    .legend {{
      display: flex;
      gap: 16px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .legend span {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
    }}
    .swatch {{
      width: 12px;
      height: 12px;
      border-radius: 3px;
      display: inline-block;
    }}
    .swatch.download {{ background: var(--teal); }}
    .swatch.upload {{ background: var(--orange); }}
    .field-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .field-card {{
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfdff;
    }}
    .field-card span {{
      display: block;
      color: var(--muted);
      font-size: 0.78rem;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .field-card strong {{
      display: block;
      margin-top: 8px;
      font-size: 1.2rem;
      overflow-wrap: anywhere;
    }}
    .field-card small {{
      display: block;
      margin-top: 8px;
      color: var(--muted);
      overflow-wrap: anywhere;
    }}
    .empty {{
      color: var(--muted);
      padding: 16px 0;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.94rem;
    }}
    th, td {{
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
    }}
    th {{
      color: var(--muted);
      font-size: 0.74rem;
      text-transform: uppercase;
    }}
    tbody tr:last-child td {{ border-bottom: 0; }}
    details summary {{
      cursor: pointer;
      color: var(--blue);
      font-weight: 750;
    }}
    pre {{
      max-height: 520px;
      overflow: auto;
      margin: 16px 0 0;
      padding: 16px;
      border-radius: 8px;
      background: #101828;
      color: #e0f2fe;
      font-size: 0.85rem;
      line-height: 1.55;
    }}
    footer {{
      margin-top: 20px;
      color: var(--muted);
      font-size: 0.9rem;
      line-height: 1.5;
    }}
    code {{
      padding: 2px 6px;
      border-radius: 6px;
      background: #e8eef5;
      color: #344054;
    }}
    @media (max-width: 920px) {{
      header {{ grid-template-columns: 1fr; }}
      .status {{ min-width: 0; }}
      .period-grid, .metric-grid, .chart-grid, .split-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .field-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .section-head {{ display: block; }}
      .note {{ margin-top: 6px; text-align: left; }}
    }}
    @media (max-width: 560px) {{
      main {{ width: min(100% - 22px, 1180px); padding-top: 22px; }}
      header {{ padding: 18px; }}
      .period-grid, .metric-grid, .chart-grid, .split-grid, .field-grid {{ grid-template-columns: 1fr; }}
      .chart-panel, .table-panel {{ padding: 14px; }}
      table {{ display: block; overflow-x: auto; white-space: nowrap; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <p class="eyebrow">Hathway usage status</p>
        <h1>Hathway Broadband Usage</h1>
        <p class="subhead">{html.escape(period or "No API window recorded yet")}</p>
      </div>
      <div class="status {status_class}">
        <b><i class="dot"></i>{html.escape(status_label)}</b>
        <span>Last updated: {html.escape(updated_at)}</span>
      </div>
    </header>

    <section class="period-grid" aria-label="Daily weekly monthly yearly and overall usage">
      {render_period_cards(summaries)}
    </section>

    <section class="metric-grid" aria-label="Account and plan details">
      <article class="metric">
        <div class="label">Plan</div>
        <div class="value">{html.escape(metric_display(plan))}</div>
        <div class="hint">{html.escape(metric_hint(plan, "Plan field not found"))}</div>
      </article>
      <article class="metric">
        <div class="label">Quota</div>
        <div class="value">{html.escape(metric_display(quota))}</div>
        <div class="hint">{html.escape(metric_hint(quota, "Quota field not found"))}</div>
      </article>
      <article class="metric">
        <div class="label">Speed</div>
        <div class="value">{html.escape(metric_display(speed))}</div>
        <div class="hint">{html.escape(metric_hint(speed, "Speed field not found"))}</div>
      </article>
      <article class="metric">
        <div class="label">Expiry</div>
        <div class="value">{html.escape(metric_display(expiry))}</div>
        <div class="hint">Account {html.escape(str(account.get("account_no_masked", "")))}</div>
      </article>
    </section>

    <section class="chart-grid" aria-label="Usage charts">
      <article class="chart-panel full-width">
        <div class="section-head">
          <h2>Daily Usage</h2>
          <div class="note">Last 45 days from the latest API response</div>
        </div>
        <div class="chart">{svg_usage_bars(daily_rows, "No daily rows were found in the latest API response", "Daily usage chart", 45)}</div>
        <div class="legend">
          <span><i class="swatch download"></i>Download or total usage</span>
          <span><i class="swatch upload"></i>Upload</span>
        </div>
      </article>
      <article class="chart-panel">
        <div class="section-head">
          <h2>Weekly</h2>
          <div class="note">ISO week totals</div>
        </div>
        <div class="chart">{svg_usage_bars(weekly_rows, "No weekly totals yet", "Weekly usage chart", 16)}</div>
      </article>
      <article class="chart-panel">
        <div class="section-head">
          <h2>Monthly</h2>
          <div class="note">Month totals</div>
        </div>
        <div class="chart">{svg_usage_bars(monthly_rows, "No monthly totals yet", "Monthly usage chart", 12)}</div>
      </article>
      <article class="chart-panel">
        <div class="section-head">
          <h2>Yearly</h2>
          <div class="note">Overall by year</div>
        </div>
        <div class="chart">{svg_usage_bars(yearly_rows, "No yearly totals yet", "Yearly usage chart", 8)}</div>
      </article>
      <article class="chart-panel">
        <div class="section-head">
          <h2>API Trend</h2>
          <div class="note">Last 72 successful samples</div>
        </div>
        <div class="chart">{svg_history_line(history_points)}</div>
      </article>
    </section>

    <section class="split-grid">
      <article class="table-panel">
        <div class="section-head">
          <h2>Top Usage Days</h2>
          <div class="note">Highest total traffic</div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Day</th>
              <th>Download</th>
              <th>Upload</th>
              <th>Total</th>
            </tr>
          </thead>
          <tbody>{render_top_days(daily_usage)}</tbody>
        </table>
      </article>

      <article class="table-panel">
        <div class="section-head">
          <h2>Recent API Samples</h2>
          <div class="note">Newest first</div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Period</th>
              <th>Total used</th>
              <th>Status</th>
              <th>Duration</th>
            </tr>
          </thead>
          <tbody>{render_history_rows(data)}</tbody>
        </table>
      </article>
    </section>

    <section class="section">
      <div class="section-head">
        <h2>Recognized API Fields</h2>
        <div class="note">Useful values detected in the response</div>
      </div>
      <div class="field-grid">{render_field_cards(fields)}</div>
    </section>

    <section class="section">
      <details>
        <summary>Latest raw API response</summary>
        <pre>{html.escape(render_raw_response(data))}</pre>
      </details>
    </section>

    <footer>
      JSON file: <code>{html.escape(str(json_path))}</code>. Authorization and mobile number are not written into this dashboard by the script.
    </footer>
  </main>
</body>
</html>
"""


def write_html(data: Dict[str, Any], html_path: Path, json_path: Path) -> None:
    html_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = html_path.with_suffix(html_path.suffix + ".tmp")
    temp_path.write_text(render_html(data, json_path), encoding="utf-8")
    temp_path.replace(html_path)


def sample_once(args: argparse.Namespace) -> bool:
    data = load_usage_data(args.json_path)
    try:
        auth_config = build_auth_config(args)
        started = time.monotonic()
        status_code, response_json, _ = post_hathway_usage(auth_config, timeout=args.timeout)
        duration_ms = int((time.monotonic() - started) * 1000)
        append_sample(
            data=data,
            auth_config=auth_config,
            status_code=status_code,
            response_json=response_json,
            duration_ms=duration_ms,
            keep_history=args.keep_history,
            include_raw_response=not args.omit_raw_response,
        )
        success = True
        print(
            f"[{now_local_iso()}] Updated {args.json_path} and {args.html_path} "
            f"with Hathway usage details."
        )
    except Exception as exc:
        append_error_sample(data, str(exc), keep_history=args.keep_history)
        success = False
        print(f"[{now_local_iso()}] Error: {exc}", file=sys.stderr)

    if args.omit_raw_response:
        remove_raw_responses(data)
    write_json_atomic(args.json_path, data)
    write_html(data, args.html_path, args.json_path)
    return success


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Hathway broadband usage, save JSON history, and render an HTML dashboard."
    )
    parser.add_argument("--once", action="store_true", help="Fetch once, update JSON/HTML, and exit.")
    parser.add_argument("--watch", action="store_true", help="Fetch every --interval seconds. Default when --once is not used.")
    parser.add_argument("--render-only", action="store_true", help="Rebuild HTML from the current JSON without calling the API.")
    parser.add_argument("--interval", type=positive_int, default=DEFAULT_INTERVAL_SECONDS, help="Seconds between API calls in watch mode. Default: 3600.")
    parser.add_argument("--timeout", type=positive_int, default=30, help="API timeout in seconds. Default: 30.")
    parser.add_argument("--keep-history", type=int, default=2160, help="Number of hourly samples to keep. 2160 is about 90 days. Use 0 to keep all.")
    parser.add_argument("--omit-raw-response", action="store_true", help="Store only normalized usage data, not the full Hathway API response. Useful for GitHub Pages.")
    parser.add_argument("--json", dest="json_path", type=Path, default=DEFAULT_JSON_PATH, help=f"Usage JSON path. Default: {DEFAULT_JSON_PATH}")
    parser.add_argument("--html", dest="html_path", type=Path, default=DEFAULT_HTML_PATH, help=f"Dashboard HTML path. Default: {DEFAULT_HTML_PATH}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help=f"Optional local config JSON. Default: {DEFAULT_CONFIG_PATH}")

    parser.add_argument("--authorization", default=None, help="Hathway Authorization token. Prefer HATHWAY_AUTHORIZATION or config file.")
    parser.add_argument("--account-no", dest="account_no", default=None, help="Hathway account number. Prefer HATHWAY_ACCOUNT_NO or config file.")
    parser.add_argument("--registered-mobile-no", dest="registered_mobile_no", default=None, help="Registered mobile number. Prefer HATHWAY_REGISTERED_MOBILE_NO or config file.")
    parser.add_argument("--login-device", dest="login_device", default=None, help="Request login_device field. Default: web.")
    parser.add_argument("--timezone", default=None, help=f"Local timezone for automatic date windows. Default: {DEFAULT_TIMEZONE}.")
    parser.add_argument("--date-mode", choices=["year-to-now", "month-to-now", "today", "last-30-days"], default=None, help=f"Automatic date window when start/end are not supplied. Default: {DEFAULT_DATE_MODE}.")
    parser.add_argument("--start-date", dest="start_date", default=None, help="Override API start_date, for example 2026-05-31T18:30:00.000Z.")
    parser.add_argument("--end-date", dest="end_date", default=None, help="Override API end_date, for example 2026-06-17T18:30:00.000Z.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    if args.render_only:
        data = load_usage_data(args.json_path)
        if args.omit_raw_response:
            remove_raw_responses(data)
        write_json_atomic(args.json_path, data)
        write_html(data, args.html_path, args.json_path)
        print(f"Rendered {args.html_path} from {args.json_path}.")
        return 0

    if args.once:
        return 0 if sample_once(args) else 2

    print(
        f"Watching Hathway usage every {args.interval} seconds. "
        f"JSON: {args.json_path}. HTML: {args.html_path}. Press Ctrl+C to stop."
    )
    while True:
        started = time.monotonic()
        sample_once(args)
        elapsed = time.monotonic() - started
        try:
            time.sleep(max(1, args.interval - elapsed))
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
