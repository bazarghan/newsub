/**
 * SubLink — Frontend Logic
 * Fetches subscription data from backend API, renders configs
 */

(function () {
    "use strict";

    // ===== DOM Elements =====
    const landingPage = document.getElementById("landing-page");
    const loadingSection = document.getElementById("loading-section");
    const errorSection = document.getElementById("error-section");
    const resultPage = document.getElementById("result-page");
    const pathInput = document.getElementById("path-input");
    const fetchBtn = document.getElementById("fetch-btn");
    const retryBtn = document.getElementById("retry-btn");
    const errorMessage = document.getElementById("error-message");
    const trafficValue = document.getElementById("traffic-value");
    const timeValue = document.getElementById("time-value");
    const subLinkUrl = document.getElementById("sub-link-url");
    const copySubBtn = document.getElementById("copy-sub-btn");
    const configList = document.getElementById("config-list");
    const configCount = document.getElementById("config-count");
    const copyAllBtn = document.getElementById("copy-all-btn");
    const copyB64Btn = document.getElementById("copy-b64-btn");
    const resultSubName = document.getElementById("result-sub-name");
    const toast = document.getElementById("toast");

    let currentData = null;
    let currentPath = "";

    // ===== Check if URL path has a subscription path =====
    function init() {
        const urlPath = window.location.pathname.replace(/^\/+/, "");
        if (urlPath && urlPath !== "sub") {
            // Direct link access
            let subPath = urlPath;
            if (subPath.startsWith("sub/")) {
                subPath = subPath.substring(4);
            }
            pathInput.value = subPath;
            fetchSubscription(subPath);
        }
    }

    // ===== Show/Hide Sections =====
    function showSection(section) {
        [landingPage, loadingSection, errorSection, resultPage].forEach(
            (s) => (s.style.display = "none")
        );
        // Result page uses block layout; others use flex for centering
        section.style.display = section === resultPage ? "block" : "flex";
    }

    // ===== Toast =====
    function showToast(message, type = "success") {
        toast.textContent = (type === "success" ? "✅ " : "❌ ") + message;
        toast.className = "toast show " + type;
        setTimeout(() => {
            toast.className = "toast";
        }, 2500);
    }

    // ===== Copy to clipboard =====
    async function copyText(text) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch {
            // Fallback
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand("copy");
            document.body.removeChild(ta);
            return ok;
        }
    }

    // ===== Parse config link for display =====
    function parseConfigLink(link) {
        const result = { protocol: "other", address: "", port: "", transport: "", sni: "" };

        if (link.startsWith("vless://")) {
            result.protocol = "vless";
            try {
                const withoutScheme = link.substring(8);
                const hashIdx = withoutScheme.indexOf("#");
                const mainPart = hashIdx !== -1 ? withoutScheme.substring(0, hashIdx) : withoutScheme;
                const qIdx = mainPart.indexOf("?");
                const userHost = qIdx !== -1 ? mainPart.substring(0, qIdx) : mainPart;
                const queryStr = qIdx !== -1 ? mainPart.substring(qIdx + 1) : "";
                const atIdx = userHost.indexOf("@");
                const hostPort = userHost.substring(atIdx + 1);
                const colonIdx = hostPort.lastIndexOf(":");
                result.address = colonIdx !== -1 ? hostPort.substring(0, colonIdx) : hostPort;
                result.port = colonIdx !== -1 ? hostPort.substring(colonIdx + 1) : "443";

                const params = new URLSearchParams(queryStr);
                result.transport = params.get("type") || "tcp";
                result.sni = params.get("sni") || params.get("host") || "";
            } catch (e) { /* ignore */ }
        } else if (link.startsWith("vmess://")) {
            result.protocol = "vmess";
            try {
                const decoded = atob(link.substring(8));
                const data = JSON.parse(decoded);
                result.address = data.add || "";
                result.port = String(data.port || "443");
                result.transport = data.net || "tcp";
                result.sni = data.sni || data.host || "";
            } catch (e) { /* ignore */ }
        }

        return result;
    }

    // ===== Fetch Subscription =====
    async function fetchSubscription(path) {
        if (!path) {
            showToast("مسیر اشتراک را وارد کنید", "error");
            return;
        }

        currentPath = path;
        showSection(loadingSection);

        try {
            const resp = await fetch(`/sub/${path}`, {
                headers: { Accept: "application/json" },
            });

            const data = await resp.json();

            if (data.error || data.error_type) {
                showSection(errorSection);
                renderError(data);
                return;
            }

            currentData = data;
            renderResult(data);
        } catch (err) {
            showSection(errorSection);
            renderError({
                error_title: "خطا",
                error_subtitle: "مشکلی در اتصال رخ داد. لطفاً دوباره تلاش کنید.",
                error: err.message,
            });
        }
    }

    // ===== Render Error =====
    function renderError(data) {
        const errorTitle = document.getElementById("error-title");
        const errorCode = document.getElementById("error-code");
        const errorSub = document.getElementById("error-subtitle");

        if (errorCode) errorCode.textContent = data.error_title || "خطا";
        if (errorTitle) errorTitle.textContent = data.error || "اشتراک یافت نشد";
        if (errorSub) errorSub.textContent = data.error_subtitle || "";
    }

    // ===== Render Result =====
    function renderResult(data) {
        showSection(resultPage);

        // Info
        const info = data.info || {};
        trafficValue.textContent = info.traffic || "نامشخص";
        timeValue.textContent = info.time || "نامشخص";

        // Sub name
        const name = info.name ? decodeURIComponent(info.name).replace(/[📊⏳🇸🇪🇩🇪🇳🇱🇫🇮🇫🇷🇬🇧🇺🇸\d.GBMBTBKBD-]+/g, "").trim() : "";
        resultSubName.textContent = name || "";

        // Subscription link
        const subUrl = `${window.location.origin}/${currentPath}`;
        subLinkUrl.textContent = subUrl;

        // Configs
        const configs = data.configs || [];
        configCount.textContent = configs.length;
        configList.innerHTML = "";

        configs.forEach((link, idx) => {
            const parsed = parseConfigLink(link);
            const card = document.createElement("div");
            card.className = "config-card";
            card.style.animationDelay = `${idx * 0.05}s`;

            const protocolLabel = parsed.protocol.toUpperCase();
            const iconClass = parsed.protocol;

            card.innerHTML = `
                <div class="config-icon ${iconClass}">${protocolLabel.substring(0, 2)}</div>
                <div class="config-details">
                    <div class="config-address">${escapeHtml(parsed.address)}:${escapeHtml(parsed.port)}</div>
                    <div class="config-meta">
                        <span>🔹 ${escapeHtml(parsed.protocol)}</span>
                        <span>🌐 ${escapeHtml(parsed.transport)}</span>
                        ${parsed.sni ? `<span>🔒 ${escapeHtml(parsed.sni)}</span>` : ""}
                    </div>
                </div>
                <button class="config-copy-btn" data-idx="${idx}" title="کپی">📋</button>
            `;

            configList.appendChild(card);
        });

        // Add click listeners for individual copy buttons
        configList.querySelectorAll(".config-copy-btn").forEach((btn) => {
            btn.addEventListener("click", async (e) => {
                const idx = parseInt(e.currentTarget.dataset.idx);
                const link = configs[idx];
                const ok = await copyText(link);
                if (ok) {
                    e.currentTarget.classList.add("copied");
                    e.currentTarget.textContent = "✅";
                    showToast("کانفیگ کپی شد");
                    setTimeout(() => {
                        e.currentTarget.classList.remove("copied");
                        e.currentTarget.textContent = "📋";
                    }, 1500);
                }
            });
        });
    }

    // ===== Escape HTML =====
    function escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    // ===== Event Listeners =====
    fetchBtn.addEventListener("click", () => {
        fetchSubscription(pathInput.value.trim());
    });

    pathInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            fetchSubscription(pathInput.value.trim());
        }
    });

    retryBtn.addEventListener("click", () => {
        if (currentPath) {
            fetchSubscription(currentPath);
        } else {
            showSection(landingPage);
        }
    });

    copySubBtn.addEventListener("click", async () => {
        const url = `${window.location.origin}/${currentPath}`;
        const ok = await copyText(url);
        if (ok) {
            showToast("لینک اشتراک کپی شد");
            copySubBtn.textContent = "✅ کپی شد";
            setTimeout(() => {
                copySubBtn.textContent = "کپی لینک";
            }, 2000);
        }
    });

    copyAllBtn.addEventListener("click", async () => {
        if (!currentData) return;
        const all = (currentData.configs || []).join("\n");
        const ok = await copyText(all);
        if (ok) {
            showToast("همه کانفیگ‌ها کپی شد");
        }
    });

    copyB64Btn.addEventListener("click", async () => {
        if (!currentData) return;
        const ok = await copyText(currentData.sub_b64 || "");
        if (ok) {
            showToast("اشتراک Base64 کپی شد");
        }
    });

    // ===== Initialize =====
    init();
})();
