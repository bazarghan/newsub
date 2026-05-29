#!/usr/bin/env python3
"""
SubLink — V2Ray subscription link converter for 3x-ui
Fetches base64-encoded links from 3x-ui panel, decodes them,
replaces IPs with clean IPs per CDN, and serves a beautiful Persian UI.

Uses Flask for robust error handling — no request can crash the server.
"""

import base64
import json
import logging
import re
import ssl
import traceback
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, Response

from config import BASE_URL, CDN_DIR, HOST, PORT

# ===== App Setup =====
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

app = Flask(__name__, static_folder=None)  # Disable default static handler

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="[SubLink] %(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sublink")


# ===== CDN Config Loading =====
def load_cdn_configs() -> list[dict]:
    """Load all CDN configs from cdn/*.json files.

    Each CDN config has: name, abbreviation, sni, host, ips
    Returns list of CDN dicts, skipping invalid files.
    """
    cdn_path = BASE_DIR / CDN_DIR
    if not cdn_path.exists():
        return []

    configs = []
    for f in sorted(cdn_path.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data.get("ips"), list):
                continue
            if not data.get("abbreviation"):
                continue
            configs.append(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Skipping invalid CDN file {f.name}: {e}")
            continue
    return configs


# ===== Upstream Fetching =====
def fetch_subscription(path: str) -> str:
    """Fetch the raw subscription data from 3x-ui panel."""
    url = BASE_URL + path
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "v2rayNG/1.8.5")
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        return resp.read().decode("utf-8")


# ===== Link Parsing =====
def decode_subscription(raw: str) -> list[str]:
    """Decode base64 subscription into individual links."""
    try:
        decoded = base64.b64decode(raw.strip()).decode("utf-8")
    except Exception:
        decoded = raw.strip()
    return [line.strip() for line in decoded.splitlines() if line.strip()]


def parse_vless_link(link: str) -> dict | None:
    """Parse a vless:// link into components."""
    if not link.startswith("vless://"):
        return None
    try:
        without_scheme = link[len("vless://"):]
        if "#" in without_scheme:
            main_part, fragment = without_scheme.split("#", 1)
        else:
            main_part, fragment = without_scheme, ""

        if "?" in main_part:
            user_host, query_str = main_part.split("?", 1)
        else:
            user_host, query_str = main_part, ""

        uuid, host_port = user_host.split("@", 1)
        if ":" in host_port:
            address, port = host_port.rsplit(":", 1)
        else:
            address, port = host_port, "443"

        params = dict(urllib.parse.parse_qsl(query_str, keep_blank_values=True))

        return {
            "protocol": "vless",
            "uuid": uuid,
            "address": address,
            "port": port,
            "params": params,
            "fragment": fragment,
        }
    except Exception as e:
        logger.debug(f"Failed to parse VLESS link: {e}")
        return None


def parse_vmess_link(link: str) -> dict | None:
    """Parse a vmess:// link (base64 JSON)."""
    if not link.startswith("vmess://"):
        return None
    try:
        raw_json = base64.b64decode(link[len("vmess://"):]).decode("utf-8")
        data = json.loads(raw_json)
        return {"protocol": "vmess", "data": data}
    except Exception as e:
        logger.debug(f"Failed to parse VMESS link: {e}")
        return None


# ===== Fragment & Info Extraction =====
def extract_traffic_and_time(fragment: str) -> dict:
    """Extract remaining traffic and time from fragment."""
    decoded = urllib.parse.unquote(fragment)
    info = {"name": decoded, "traffic": "نامشخص", "time": "نامشخص"}

    traffic_match = re.search(r'([\d.]+)\s*(GB|MB|KB|TB)', decoded, re.IGNORECASE)
    if traffic_match:
        val = float(traffic_match.group(1))
        unit = traffic_match.group(2).upper()
        info["traffic"] = f"{val:.2f} {unit}"

    time_match = re.search(r'(\d+)\s*D', decoded)
    if time_match:
        days = int(time_match.group(1))
        info["time"] = f"{days} روز"

    return info


def _build_query_string(params: dict) -> str:
    """Build URL query string with V2Ray-compatible encoding.

    Unlike urllib.parse.urlencode, this preserves commas and slashes
    which V2Ray clients expect in alpn and path parameters.
    """
    parts = []
    for key, value in params.items():
        encoded_key = urllib.parse.quote(str(key), safe="")
        encoded_val = urllib.parse.quote(str(value), safe=",/:@")
        parts.append(f"{encoded_key}={encoded_val}")
    return "&".join(parts)


def clean_fragment(fragment: str, abbreviation: str, ip_index: int = 0) -> str:
    """Clean fragment and rebuild with CDN abbreviation.

    Input:  🇸🇪 sweden-l3xacrz3zn-9.89GB📊-31D⏳
    Output: 🇸🇪CF1-9.89GB📊-31D⏳
    """
    decoded = urllib.parse.unquote(fragment)

    # Extract flag emoji(s) at the start
    flags = ""
    i = 0
    while i < len(decoded):
        cp = ord(decoded[i])
        if 0x1F1E6 <= cp <= 0x1F1FF:
            flags += decoded[i]
            i += 1
        elif cp in (0xFE0F, 0x200D):
            flags += decoded[i]
            i += 1
        else:
            break

    # Extract traffic info
    traffic_str = ""
    traffic_match = re.search(r'([\d.]+)\s*(GB|MB|KB|TB)', decoded, re.IGNORECASE)
    if traffic_match:
        traffic_str = f"{traffic_match.group(1)}{traffic_match.group(2).upper()}"

    # Extract time info
    time_str = ""
    time_match = re.search(r'(\d+)\s*D', decoded)
    if time_match:
        time_str = f"{time_match.group(1)}D"

    # Build label: abbreviation + index (e.g. CF1, CF2)
    label = abbreviation
    if ip_index > 0:
        label += str(ip_index)

    # Build clean fragment: 🇸🇪CF1-9.89GB📊-31D⏳
    parts = [flags.rstrip(), label]
    suffix_parts = []
    if traffic_str:
        suffix_parts.append(f"{traffic_str}📊")
    if time_str:
        suffix_parts.append(f"{time_str}⏳")

    result = "".join(parts)
    if suffix_parts:
        result += "-" + "-".join(suffix_parts)

    return urllib.parse.quote(result, safe="")


# ===== Link Building =====
def build_config_with_cdn(parsed: dict, clean_ip: str, cdn: dict, ip_index: int = 0) -> str:
    """Build a new vless:// link with CDN-specific clean IP, SNI, host, and naming."""
    if parsed["protocol"] != "vless":
        return ""

    params = dict(parsed["params"])
    address = clean_ip

    if cdn.get("host"):
        params["host"] = cdn["host"]
    if cdn.get("sni"):
        params["sni"] = cdn["sni"]
    elif cdn.get("host"):
        params["sni"] = cdn["host"]

    params["fp"] = "chrome"
    params["alpn"] = "h3,h2,http/1.1"

    query = _build_query_string(params)
    fragment = clean_fragment(parsed["fragment"], cdn.get("abbreviation", ""), ip_index)

    return f"vless://{parsed['uuid']}@{address}:{parsed['port']}?{query}#{fragment}"


def build_vmess_with_cdn(parsed: dict, clean_ip: str, cdn: dict, ip_index: int = 0) -> str:
    """Build a new vmess:// link with CDN-specific settings."""
    if parsed["protocol"] != "vmess":
        return ""
    data = dict(parsed["data"])
    data["add"] = clean_ip

    if cdn.get("host"):
        data["host"] = cdn["host"]
    if cdn.get("sni"):
        data["sni"] = cdn["sni"]
    elif cdn.get("host"):
        data["sni"] = cdn["host"]

    if data.get("ps"):
        cleaned = clean_fragment(
            urllib.parse.quote(data["ps"]),
            cdn.get("abbreviation", ""),
            ip_index
        )
        data["ps"] = urllib.parse.unquote(cleaned)

    raw = json.dumps(data, ensure_ascii=False)
    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return f"vmess://{encoded}"


# ===== Subscription Processing =====
def process_subscription(raw_data: str) -> dict:
    """Process raw subscription data and return structured result."""
    links = decode_subscription(raw_data)
    cdn_configs = load_cdn_configs()

    if not links:
        return {"error": "هیچ لینکی یافت نشد", "configs": [], "info": {}}

    # Parse first link to extract info
    first_parsed = None
    for link in links:
        first_parsed = parse_vless_link(link) or parse_vmess_link(link)
        if first_parsed:
            break

    info = {}
    if first_parsed and first_parsed["protocol"] == "vless":
        info = extract_traffic_and_time(first_parsed["fragment"])
    elif first_parsed and first_parsed["protocol"] == "vmess":
        name = first_parsed["data"].get("ps", "")
        info = extract_traffic_and_time(urllib.parse.quote(name))

    # Generate configs: for each link × each CDN × each IP in that CDN
    configs = []
    for link in links:
        parsed = parse_vless_link(link)
        if parsed:
            if cdn_configs:
                for cdn in cdn_configs:
                    cdn_ips = cdn.get("ips", [])
                    for idx, ip in enumerate(cdn_ips, 1):
                        try:
                            new_link = build_config_with_cdn(parsed, ip, cdn, idx)
                            if new_link:
                                configs.append(new_link)
                        except Exception as e:
                            logger.warning(f"Error building VLESS config for {ip}: {e}")
            else:
                configs.append(link)
            continue

        parsed = parse_vmess_link(link)
        if parsed:
            if cdn_configs:
                for cdn in cdn_configs:
                    cdn_ips = cdn.get("ips", [])
                    for idx, ip in enumerate(cdn_ips, 1):
                        try:
                            new_link = build_vmess_with_cdn(parsed, ip, cdn, idx)
                            if new_link:
                                configs.append(new_link)
                        except Exception as e:
                            logger.warning(f"Error building VMESS config for {ip}: {e}")
            else:
                configs.append(link)
            continue

        # Unknown protocol, keep as-is
        configs.append(link)

    sub_content = "\n".join(configs)
    sub_b64 = base64.b64encode(sub_content.encode("utf-8")).decode("utf-8")

    return {
        "configs": configs,
        "info": info,
        "sub_b64": sub_b64,
        "original_count": len(links),
        "total_count": len(configs),
    }


# ===== Helper: Detect client type =====
def _detect_client():
    """Detect if the request is from a browser, JSON API, or V2Ray client."""
    accept = request.headers.get("Accept", "")
    user_agent = (request.headers.get("User-Agent", "") or "").lower()

    is_json = "application/json" in accept

    browser_agents = ["mozilla", "chrome", "safari", "firefox", "opera", "edge"]
    is_browser = "text/html" in accept and any(b in user_agent for b in browser_agents)

    return is_json, is_browser


def _error_response(error_msg="اشتراک یافت نشد", status=404):
    """Return error in the correct format based on client type."""
    is_json, is_browser = _detect_client()
    error_data = {
        "error": error_msg,
        "error_type": "not_found",
        "error_title": "۴۰۴",
        "error_subtitle": "اشتراک مورد نظر وجود ندارد یا منقضی شده است.",
    }
    if is_json:
        return jsonify(error_data), status
    elif is_browser:
        return send_from_directory(STATIC_DIR, "index.html")
    else:
        return Response("subscription not found\n", status=status,
                        content_type="text/plain; charset=utf-8")


# ===== Flask Error Handlers =====
@app.errorhandler(404)
def handle_404(e):
    """Catch all 404 errors — never crash."""
    return _error_response("صفحه یافت نشد", 404)


@app.errorhandler(405)
def handle_405(e):
    """Method not allowed."""
    return Response("method not allowed\n", status=405,
                    content_type="text/plain; charset=utf-8")


@app.errorhandler(500)
def handle_500(e):
    """Internal server error — log full trace, return clean response."""
    logger.error(f"Internal error: {e}\n{traceback.format_exc()}")
    is_json, _ = _detect_client()
    if is_json:
        return jsonify({"error": "خطای داخلی سرور", "error_type": "server_error"}), 500
    return Response("internal server error\n", status=500,
                    content_type="text/plain; charset=utf-8")


@app.errorhandler(Exception)
def handle_exception(e):
    """Catch-all: any unhandled exception returns 500, never crashes."""
    logger.error(f"Unhandled exception: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    is_json, _ = _detect_client()
    if is_json:
        return jsonify({"error": "خطای غیرمنتظره", "error_type": "server_error"}), 500
    return Response("internal server error\n", status=500,
                    content_type="text/plain; charset=utf-8")


# ===== Routes =====
@app.route("/")
def index():
    """Serve the main HTML page."""
    try:
        return send_from_directory(STATIC_DIR, "index.html")
    except Exception:
        return Response("page not available", status=503)


@app.route("/style.css")
def style():
    return send_from_directory(STATIC_DIR, "style.css", mimetype="text/css")


@app.route("/app.js")
def appjs():
    return send_from_directory(STATIC_DIR, "app.js", mimetype="application/javascript")


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/sub/<path:sub_path>")
def subscription_api(sub_path):
    """Handle subscription requests via /sub/PATH."""
    return _handle_subscription(sub_path)


@app.route("/<path:sub_path>")
def subscription_fallback(sub_path):
    """Catch-all: treat any other path as a subscription path."""
    # Skip paths that look like static files
    if sub_path in ("style.css", "app.js", "favicon.ico"):
        return Response(status=404)
    return _handle_subscription(sub_path)


def _handle_subscription(sub_path: str):
    """Core subscription handler — fully wrapped in error handling."""
    is_json, is_browser = _detect_client()

    # Sanitize path
    sub_path = sub_path.strip().strip("/")
    if not sub_path:
        return _error_response()

    # Fetch upstream
    try:
        raw_data = fetch_subscription(sub_path)
    except urllib.error.HTTPError as e:
        logger.info(f"Upstream HTTP {e.code} for path: {sub_path}")
        return _error_response()
    except urllib.error.URLError as e:
        logger.warning(f"Upstream connection error for {sub_path}: {e.reason}")
        return _error_response()
    except Exception as e:
        logger.warning(f"Upstream fetch error for {sub_path}: {type(e).__name__}: {e}")
        return _error_response()

    # Validate response
    if not raw_data or not raw_data.strip():
        return _error_response()

    # Process subscription
    try:
        result = process_subscription(raw_data)
    except Exception as e:
        logger.error(f"Processing error for {sub_path}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return _error_response("خطا در پردازش اشتراک", 500)

    # No configs found
    if not result.get("configs") and not result.get("error"):
        return _error_response()

    if result.get("error"):
        if is_json:
            return jsonify({"error": result["error"], "error_type": "not_found"}), 404
        elif is_browser:
            return send_from_directory(STATIC_DIR, "index.html")
        else:
            return Response(status=404)

    # JSON API response
    if is_json:
        return jsonify(result)

    # Browser: serve HTML (frontend will fetch JSON via JS)
    if is_browser:
        return send_from_directory(STATIC_DIR, "index.html")

    # V2Ray client: return raw base64 subscription
    content = result.get("sub_b64", "")
    resp = Response(content, status=200, content_type="text/plain; charset=utf-8")
    resp.headers["Content-Disposition"] = "inline"
    resp.headers["Profile-Update-Interval"] = "4"
    resp.headers["Subscription-Userinfo"] = "upload=0; download=0; total=0; expire=0"
    return resp


# ===== Main =====
def main():
    cdn_configs = load_cdn_configs()
    total_ips = sum(len(c.get("ips", [])) for c in cdn_configs)
    cdn_names = ", ".join(c.get("abbreviation", "?") for c in cdn_configs) or "none"

    print(f"""
╔══════════════════════════════════════════╗
║         🚀 SubLink Server (Flask)        ║
║                                          ║
║   Running on http://{HOST}:{PORT}         ║
║   Base URL: {BASE_URL[:35]}...           ║
║   CDNs: {cdn_names:<33}║
║   Total IPs: {total_ips:<28}║
╚══════════════════════════════════════════╝
    """)

    # Use Flask's built-in server with threading for concurrent requests
    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
