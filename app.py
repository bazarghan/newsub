#!/usr/bin/env python3
"""
SubLink — V2Ray subscription link converter for 3x-ui
Fetches base64-encoded links from 3x-ui panel, decodes them,
replaces IPs with clean IPs, and serves a beautiful Persian UI.
"""

import base64
import json
import re
import os
import ssl
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from config import BASE_URL, IPS_FILE, HOST, PORT


def load_clean_ips():
    """Load clean IPs from ips.txt, one per line."""
    ips_path = Path(__file__).parent / IPS_FILE
    if not ips_path.exists():
        return []
    ips = []
    for line in ips_path.read_text().strip().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ips.append(line)
    return ips


def fetch_subscription(path: str) -> str:
    """Fetch the raw subscription data from 3x-ui panel."""
    url = BASE_URL + path
    # Create SSL context that doesn't verify (self-signed panels)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url)
    req.add_header("User-Agent", "v2rayNG/1.8.5")
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        return resp.read().decode("utf-8")


def decode_subscription(raw: str) -> list[str]:
    """Decode base64 subscription into individual links."""
    try:
        decoded = base64.b64decode(raw.strip()).decode("utf-8")
    except Exception:
        decoded = raw.strip()
    links = [l.strip() for l in decoded.splitlines() if l.strip()]
    return links


def parse_vless_link(link: str) -> dict | None:
    """Parse a vless:// link into components."""
    if not link.startswith("vless://"):
        return None
    # vless://UUID@ADDRESS:PORT?params#fragment
    without_scheme = link[len("vless://"):]
    # Split fragment
    if "#" in without_scheme:
        main_part, fragment = without_scheme.split("#", 1)
    else:
        main_part, fragment = without_scheme, ""

    # Split params
    if "?" in main_part:
        user_host, query_str = main_part.split("?", 1)
    else:
        user_host, query_str = main_part, ""

    # Split user@host:port
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


def parse_vmess_link(link: str) -> dict | None:
    """Parse a vmess:// link (base64 JSON)."""
    if not link.startswith("vmess://"):
        return None
    try:
        raw_json = base64.b64decode(link[len("vmess://"):]).decode("utf-8")
        data = json.loads(raw_json)
        return {"protocol": "vmess", "data": data}
    except Exception:
        return None


def extract_traffic_and_time(fragment: str) -> dict:
    """Extract remaining traffic and time from fragment like:
    🇸🇪 sweden-l3xacrz3zn-9.89GB📊-31D⏳
    """
    decoded = urllib.parse.unquote(fragment)
    info = {"name": decoded, "traffic": "نامشخص", "time": "نامشخص"}

    # Traffic: number + GB/MB/KB/TB
    traffic_match = re.search(r'([\d.]+)\s*(GB|MB|KB|TB)', decoded, re.IGNORECASE)
    if traffic_match:
        val = float(traffic_match.group(1))
        unit = traffic_match.group(2).upper()
        info["traffic"] = f"{val:.2f} {unit}"

    # Time: number + D (days)
    time_match = re.search(r'(\d+)\s*D', decoded)
    if time_match:
        days = int(time_match.group(1))
        info["time"] = f"{days} روز"

    return info


def build_config_with_clean_ip(parsed: dict, clean_ip: str) -> str:
    """Build a new vless:// link with the clean IP, proper SNI, and extra params."""
    if parsed["protocol"] != "vless":
        return ""

    params = dict(parsed["params"])
    host_val = params.get("host", "")

    # Replace address with clean IP
    address = clean_ip
    # Set SNI = host header
    if host_val:
        params["sni"] = host_val

    # Add fingerprint and ALPN
    params["fp"] = "chrome"
    params["alpn"] = "h3,h2,http/1.1"

    # Build query string
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    fragment = parsed["fragment"]

    link = f"vless://{parsed['uuid']}@{address}:{parsed['port']}?{query}#{fragment}"
    return link


def build_vmess_with_clean_ip(parsed: dict, clean_ip: str) -> str:
    """Build a new vmess:// link with clean IP."""
    if parsed["protocol"] != "vmess":
        return ""
    data = dict(parsed["data"])
    host_val = data.get("host", "")
    data["add"] = clean_ip
    if host_val:
        data["sni"] = host_val
    raw = json.dumps(data, ensure_ascii=False)
    encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
    return f"vmess://{encoded}"


def process_subscription(raw_data: str) -> dict:
    """Process raw subscription data and return structured result."""
    links = decode_subscription(raw_data)
    clean_ips = load_clean_ips()

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

    # Generate configs with clean IPs
    configs = []
    for link in links:
        parsed = parse_vless_link(link)
        if parsed:
            if clean_ips:
                for ip in clean_ips:
                    new_link = build_config_with_clean_ip(parsed, ip)
                    configs.append(new_link)
            else:
                configs.append(link)
            continue

        parsed = parse_vmess_link(link)
        if parsed:
            if clean_ips:
                for ip in clean_ips:
                    new_link = build_vmess_with_clean_ip(parsed, ip)
                    configs.append(new_link)
            else:
                configs.append(link)
            continue

        # Unknown protocol, keep as-is
        configs.append(link)

    # Build subscription base64
    sub_content = "\n".join(configs)
    sub_b64 = base64.b64encode(sub_content.encode("utf-8")).decode("utf-8")

    return {
        "configs": configs,
        "info": info,
        "sub_b64": sub_b64,
        "original_count": len(links),
        "total_count": len(configs),
    }


class SubLinkHandler(BaseHTTPRequestHandler):
    """HTTP request handler for SubLink service."""

    def log_message(self, format, *args):
        print(f"[SubLink] {args[0]}")

    def do_GET(self):
        # Serve static files
        if self.path == "/" or self.path == "":
            self._serve_file("static/index.html", "text/html")
            return
        if self.path == "/style.css":
            self._serve_file("static/style.css", "text/css")
            return
        if self.path == "/app.js":
            self._serve_file("static/app.js", "application/javascript")
            return
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        # API: /sub/PATH -> fetch & process
        if self.path.startswith("/sub/"):
            sub_path = self.path[len("/sub/"):]
            self._handle_subscription(sub_path)
            return

        # Anything else -> try as subscription path
        sub_path = self.path.lstrip("/")
        if sub_path:
            self._handle_subscription(sub_path)
            return

        self.send_response(404)
        self.end_headers()

    def _serve_file(self, filepath: str, content_type: str):
        fpath = Path(__file__).parent / filepath
        if not fpath.exists():
            self.send_response(404)
            self.end_headers()
            return
        content = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _handle_subscription(self, sub_path: str):
        # Check Accept header to decide JSON vs HTML
        accept = self.headers.get("Accept", "")
        user_agent = self.headers.get("User-Agent", "").lower()
        is_v2ray_client = any(
            x in user_agent
            for x in ["v2rayng", "v2rayn", "clash", "shadowrocket", "quantumult", "surge", "hiddify", "streisand", "v2box", "nekoray"]
        )

        try:
            raw_data = fetch_subscription(sub_path)
        except urllib.error.HTTPError as e:
            return self._handle_fetch_error(e.code, accept, is_v2ray_client)
        except Exception as e:
            # SSL EOF, connection refused, timeout → all mean subscription not found
            return self._handle_fetch_error(404, accept, is_v2ray_client)

        # Check if response is empty or not valid base64/links
        if not raw_data or not raw_data.strip():
            return self._handle_fetch_error(404, accept, is_v2ray_client)

        result = process_subscription(raw_data)

        # If no configs were found after processing, treat as not found
        if not result.get("configs") and not result.get("error"):
            return self._handle_fetch_error(404, accept, is_v2ray_client)

        if result.get("error"):
            if "application/json" in accept:
                self._send_json({"error": result["error"], "error_type": "not_found"}, 404)
            elif is_v2ray_client:
                self.send_response(404)
                self.end_headers()
            else:
                self._serve_file("static/index.html", "text/html")
            return

        # If client is a v2ray app (not browser), return raw base64
        if is_v2ray_client or "text/plain" in accept:
            content = result.get("sub_b64", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", "inline")
            self.send_header("Profile-Update-Interval", "4")
            self.send_header("Subscription-Userinfo",
                             f"upload=0; download=0; total=0; expire=0")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
            return

        if "application/json" in accept:
            self._send_json(result)
            return

        # Browser: serve the HTML page (frontend will call /sub/ API)
        self._serve_file("static/index.html", "text/html")

    def _handle_fetch_error(self, status_code: int, accept: str, is_v2ray_client: bool):
        """Handle upstream fetch errors with a clean 404 response."""
        error_data = {
            "error": "اشتراک یافت نشد",
            "error_type": "not_found",
            "error_title": "۴۰۴",
            "error_subtitle": "اشتراک مورد نظر وجود ندارد یا منقضی شده است.",
        }
        if "application/json" in accept:
            self._send_json(error_data, 404)
        elif is_v2ray_client:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("subscription not found".encode("utf-8"))
        else:
            self._serve_file("static/index.html", "text/html")

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = HTTPServer((HOST, PORT), SubLinkHandler)
    print(f"""
╔══════════════════════════════════════════╗
║         🚀 SubLink Server               ║
║                                          ║
║   Running on http://{HOST}:{PORT}         ║
║   Base URL: {BASE_URL[:35]}...           ║
║   Clean IPs: {len(load_clean_ips())} loaded               ║
╚══════════════════════════════════════════╝
    """)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[SubLink] Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
