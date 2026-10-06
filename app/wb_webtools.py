# -*- coding: utf-8 -*-
"""反代代跑 web_search / web_fetch（开关在 wb_proxy.LOCAL_WEB_TOOLS）

WorkBuddy 上游没有任何搜索服务：客户端宣告的 web_search / web_search_preview /
web_fetch 直接丢给 chat endpoint 时模型不会产生 tool call，所以只能由反代自己
跑（issue #43）。三条现有约定：

  1. query_args() 同时接受 query / queries / q，多个查询合并成一次搜索；只认
     args["query"] 时模型改送 queries 会被回一句「你没问问题」，重试到回合耗光。
  2. install_tool_defs() 先把同名项目全部拿掉，再放进唯一一份我们的定义，否则
     上游会同时看到两个同名的 web_search。
  3. 回合用尽时不合成 resp_wrapup：调用端在最后一轮把工具收回，让模型自己用文字
     收尾，不把失败伪装成正常结束。

搜索后端是 DuckDuckGo 的 HTML 版（不需要 API key）。任何失败都回一句可读的
错误给模型，不假造结果。只用 Python 标准库。

web_fetch 是唯一「模型给什么地址就连什么地址」的入口，所以连接由这里自己管：
主机名的每一个解析结果都必须是对外可路由的地址，连接只用校验过的 IP（解析完
之后再改 DNS 也没用），重定向每一跳重新校验，响应体有字节上限，整次抓取有
总时限。
"""

import html as _html
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import time
import urllib.parse

WEB_SEARCH_NAME = "web_search"
WEB_FETCH_NAME = "web_fetch"

# 客户端会用这几种 type 宣告同一个工具
SEARCH_DECL_TYPES = ("web_search", "web_search_preview", "web_search_preview_2025_03_11")
FETCH_DECL_TYPES = ("web_fetch",)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_RESULTS = 10
MAX_FETCH_CHARS = 100000
HTTP_TIMEOUT = 20              # 单次连接/读的超时
FETCH_DEADLINE = 40            # 一次抓取（连接、重定向、读完）的总时限
MAX_RESPONSE_BYTES = 4 << 20   # 响应体字节上限：足够撑出十万字符正文，内存也可控
MAX_REDIRECTS = 5
READ_CHUNK = 65536
REDIRECT_STATUS = (301, 302, 303, 307, 308)
SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"


def max_rounds():
    """最多代跑几轮网络工具。环境变量可覆盖，方便临时关小。"""
    try:
        n = int(os.environ.get("WB_MAX_WEB_ROUNDS", "") or "")
    except (TypeError, ValueError):
        n = 0
    return min(8, n) if n > 0 else 3


MAX_WEB_ROUNDS = max_rounds()


def web_search_tool_def():
    return {
        "type": "function",
        "name": WEB_SEARCH_NAME,
        "description": (
            "Searches the web for real-time information and returns ranked results "
            "with titles, URLs and snippets. Use it for current events, documentation "
            "lookup, or anything beyond your knowledge cutoff. To read a page in full, "
            "call web_fetch on its URL afterwards."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query (at least 2 characters).",
                },
                "numResults": {
                    "type": "number",
                    "description": "How many results to return (1-10, default 5).",
                },
            },
            "required": ["query"],
        },
    }


def web_fetch_tool_def():
    return {
        "type": "function",
        "name": WEB_FETCH_NAME,
        "description": (
            "Fetches a URL and returns its readable text. Use startIndex to page "
            "through a long page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute http:// or https:// URL to fetch.",
                },
                "startIndex": {
                    "type": "number",
                    "description": "Character offset to continue reading a long page.",
                },
            },
            "required": ["url"],
        },
    }


def _declared(tools, types):
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("type") or "").strip().lower() in types:
            return True
        # 有些客户端会把它包成 function 形状
        if str(t.get("name") or "").strip().lower() in types:
            return True
    return False


def client_wants_web(tools):
    """客户端宣告了哪几个网络工具（服务器端或 function 形状都算）。"""
    return {
        "search": _declared(tools, SEARCH_DECL_TYPES + (WEB_SEARCH_NAME,)),
        "fetch": _declared(tools, FETCH_DECL_TYPES + (WEB_FETCH_NAME,)),
    }


def install_tool_defs(chat_tools, wants):
    """把客户端的网络工具宣告换成我们的 function。

    同名项目一律先移除（服务器端 type、客户端 function、上一轮学到的定义），
    只留唯一一份，否则上游会同时看到两个 web_search。
    """
    names = set()
    if wants.get("search"):
        names.add(WEB_SEARCH_NAME)
    if wants.get("fetch"):
        names.add(WEB_FETCH_NAME)

    kept = []
    for t in chat_tools or []:
        if not isinstance(t, dict):
            kept.append(t)
            continue
        type_name = str(t.get("type") or "").strip().lower()
        name = str(t.get("name") or "").strip().lower()
        if isinstance(t.get("function"), dict):
            name = name or str((t.get("function") or {}).get("name") or "").strip().lower()
        if name in names or type_name in names:
            continue
        kept.append(t)

    if wants.get("search"):
        kept.append(web_search_tool_def())
    if wants.get("fetch"):
        kept.append(web_fetch_tool_def())
    return kept


def is_internal_tool(name):
    return str(name or "").strip() in (WEB_SEARCH_NAME, WEB_FETCH_NAME)


def query_args(args):
    """从工具参数取出查询字符串。

    接受 query / queries / q，数组会用 " or " 接起来；只认 args["query"] 时
    模型改送 queries 会被回一句「你没问问题」，重试到回合用尽（issue #43）。
    """
    if not isinstance(args, dict):
        return ""
    raw = args.get("query")
    if raw is None:
        raw = args.get("queries")
    if raw is None:
        raw = args.get("q")
    if isinstance(raw, (list, tuple)):
        parts = [str(x).strip() for x in raw if str(x or "").strip()]
        return " or ".join(parts)
    return str(raw or "").strip()


def url_arg(args):
    if not isinstance(args, dict):
        return ""
    raw = args.get("url")
    if raw is None:
        raw = args.get("urls")
    if isinstance(raw, (list, tuple)):
        for x in raw:
            if str(x or "").strip():
                return str(x).strip()
        return ""
    return str(raw or "").strip()


class WebToolError(Exception):
    """抓取失败的原因（地址被拒、解析不了、超时、连不上），转成给模型的一句话。"""


class HttpStatusError(WebToolError):
    """上游回了非 2xx。"""

    def __init__(self, status):
        WebToolError.__init__(self, "HTTP %s" % status)
        self.status = status


_SSL_CONTEXT = None


def _ssl_context():
    """默认上下文：证书链与主机名都要校验（连接虽然钉在 IP 上，这里不受影响）。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def _remaining(deadline):
    """离整次抓取的 deadline 还有多久；顺带把单次连接/读的超时压在里面。"""
    left = deadline - time.monotonic()
    if left <= 0:
        raise WebToolError("Timed out after %ds." % FETCH_DEADLINE)
    return min(float(HTTP_TIMEOUT), left)


def _read_body(resp, sock, limit, deadline):
    """分块读响应体：读满 limit 再探一个字节就知道有没有更多，多了立刻停。

    read1 每次最多做一次底层读取，慢速服务器也会在每次读取之间回到这里检查
    deadline；read(n) 会一直凑够 n 字节才返回。
    """
    chunks = []
    total = 0
    while total < limit:
        sock.settimeout(_remaining(deadline))
        piece = resp.read1(min(READ_CHUNK, limit - total))
        if not piece:
            return b"".join(chunks), False
        chunks.append(piece)
        total += len(piece)
    sock.settimeout(_remaining(deadline))
    return b"".join(chunks), bool(resp.read(1))


def _decode(raw, charset):
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _connect(parsed, addresses, deadline):
    """连到已经校验过的 IP；https 用 URL 里的主机名握手，SNI 与证书校验都按它来。"""
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    failure = None
    for ip in addresses:
        timeout = _remaining(deadline)
        try:
            sock = socket.create_connection((str(ip), port), timeout)
        except OSError as exc:
            failure = exc
            continue
        if parsed.scheme != "https":
            return sock
        try:
            return _ssl_context().wrap_socket(sock, server_hostname=host)
        except OSError as exc:
            failure = exc
            sock.close()
    raise WebToolError("Could not connect to %s (%s)." % (host, failure))


def _fetch(parsed, addresses, deadline):
    """发一个 GET，返回 (状态码, 响应头, 响应体, 是否被字节上限截断)。

    重定向那一跳只读响应头：跳过去的地址多半用不上它带的页面。
    """
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = cls(host, port, timeout=_remaining(deadline))
    # 已经连好、https 也已经握手；塞进去免得它按主机名再解析一次（DNS 重绑定的口子）。
    # Host 与 Accept-Encoding（identity）由 putrequest 按连接的主机和端口补上。
    sock = _connect(parsed, addresses, deadline)
    conn.sock = sock
    try:
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        conn.putrequest("GET", target)
        conn.putheader("User-Agent", _USER_AGENT)
        conn.putheader("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        conn.putheader("Accept-Language", "en-US,en;q=0.9")
        conn.endheaders()
        resp = conn.getresponse()
        try:
            if resp.status in REDIRECT_STATUS and resp.headers.get("Location"):
                return resp.status, resp.headers, b"", False
            body, truncated = _read_body(resp, sock, MAX_RESPONSE_BYTES, deadline)
        finally:
            resp.close()
        return resp.status, resp.headers, body, truncated
    finally:
        conn.close()


def _http_get(url):
    """抓一个页面：逐跳校验重定向，返回 (解码后的文本, 响应体是否在字节上限处被截断)。"""
    deadline = time.monotonic() + FETCH_DEADLINE
    current = url
    for _hop in range(MAX_REDIRECTS + 1):
        target, addresses = _guard_url(current)
        parsed = urllib.parse.urlparse(target)
        try:
            status, headers, raw, truncated = _fetch(parsed, addresses, deadline)
        except (socket.timeout, TimeoutError):
            raise WebToolError("Timed out fetching %s (over %ds)." % (target, FETCH_DEADLINE))
        location = headers.get("Location")
        if status in REDIRECT_STATUS and location:
            current = urllib.parse.urljoin(target, location)
            continue
        if status >= 400:
            raise HttpStatusError(status)
        return _decode(raw, headers.get_content_charset() or "utf-8"), truncated
    raise WebToolError("Too many redirects (more than %d)." % MAX_REDIRECTS)


def _strip_tags(text):
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text or "")
    text = re.sub(r"(?is)<br\s*/?>", chr(10), text)
    text = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", chr(10), text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = text.replace(chr(160), " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", chr(10), text)
    text = re.sub(chr(10) + "{3,}", chr(10) + chr(10), text)
    return text.strip()


def _ddg_target(href):
    """解开 DuckDuckGo 的 /l/?uddg= 重定向。"""
    href = _html.unescape(str(href or "").strip())
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urllib.parse.urlparse(href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            target = (urllib.parse.parse_qs(parsed.query).get("uddg") or [""])[0]
            if target:
                return urllib.parse.unquote(target)
    except Exception:
        pass
    return href


def search(query, num_results=5):
    """DuckDuckGo HTML 版搜索，返回要喂给模型的可读字符串。"""
    query = str(query or "").strip()
    if len(query) < 2:
        return ('Error: web_search needs a query of at least 2 characters; '
                'got %r. Pass it as {"query": "..."}.' % query)
    try:
        n = int(num_results)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(MAX_RESULTS, n))

    url = SEARCH_ENDPOINT + "?" + urllib.parse.urlencode({"q": query})
    try:
        page, _truncated = _http_get(url)
    except HttpStatusError as exc:
        return "Error: the search backend answered HTTP %s for %r." % (exc.status, query)
    except WebToolError as exc:
        return "Error: could not reach the search backend for %r (%s)." % (query, exc)
    except Exception as exc:
        return "Error: could not reach the search backend for %r (%s)." % (
            query, type(exc).__name__)

    blocks = re.split(r'(?is)<div[^>]+class="[^"]*result__body[^"]*"', page)
    results = []
    for block in blocks[1:]:
        m_link = re.search(
            r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m_link:
            continue
        href = _ddg_target(m_link.group(1))
        title = _strip_tags(m_link.group(2))
        m_snip = re.search(r'(?is)class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block)
        snippet = _strip_tags(m_snip.group(1)) if m_snip else ""
        if not href or not title:
            continue
        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= n:
            break

    if not results:
        return ("No results found for: %s%sTry a broader or differently worded query."
                % (query, chr(10) + chr(10)))

    lines = ["%d. %s%s   %s%s   %s" % (i, r["title"], chr(10), r["url"], chr(10), r["snippet"])
             for i, r in enumerate(results, 1)]
    return ("Search results for: %s%s%s%s%sCite the sources you used at the end of "
            "your answer." % (query, chr(10) + chr(10), (chr(10) + chr(10)).join(lines),
                              chr(10) + chr(10), ""))


# is_private / is_global 各版本的覆盖范围不一样，这几段自己兜底
_EXTRA_BLOCKED_V4 = tuple(ipaddress.ip_network(n) for n in (
    "192.0.0.0/24",      # IETF 协议专用，未必被 is_private 覆盖
    "192.88.99.0/24",    # 6to4 中继 anycast，已废弃
))
_EXTRA_BLOCKED_V6 = tuple(ipaddress.ip_network(n) for n in (
    "2002::/16",         # 6to4，内嵌的 IPv4 可以是私网地址
))


def _as_ip(text):
    """解析成 ipaddress 对象；IPv4 映射的 IPv6 归一到 IPv4（::ffff:127.0.0.1）。"""
    ip = ipaddress.ip_address(str(text).split("%")[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _is_blocked(ip):
    """loopback / private / link-local / reserved / multicast / unspecified 一律拒绝。"""
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified or not ip.is_global):
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in _EXTRA_BLOCKED_V4
    return ip in _EXTRA_BLOCKED_V6


def _resolve(host):
    """解析主机的全部地址（IPv4 在前）；有一个不可对外就整个主机名拒绝。"""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses = []
    for info in infos:
        try:
            ip = _as_ip(info[4][0])
        except ValueError:
            continue
        if ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise WebToolError("Could not resolve %s." % host)
    addresses.sort(key=lambda ip: ip.version)   # 没有 IPv6 出口时不必陪它白等
    blocked = [str(ip) for ip in addresses if _is_blocked(ip)]
    if blocked:
        raise WebToolError("%s resolves to %s, which is not allowed." % (host, ", ".join(blocked)))
    return addresses


def _guard_url(url):
    """只允许对外的一般 http(s) 网址，返回 (规范化 URL, 可连接的 IP 列表)。

    主机名末尾的点先去掉（localhost. 与 localhost 是同一个名字），再看每一个
    解析结果；连接时只用这里返回的 IP，所以解析之后名字被改到内网也不受影响。
    """
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        port = parsed.port
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        raise WebToolError("Invalid URL.")
    if parsed.scheme.lower() not in ("http", "https"):
        raise WebToolError("Only http:// or https:// URLs are supported.")
    if parsed.username or parsed.password:
        raise WebToolError("Credentials in the URL are not allowed.")
    if not host or host.endswith(".localhost") \
            or ("." not in host and not _looks_like_ip(host)):
        raise WebToolError("Private, loopback or single-label hosts are not allowed.")
    try:
        addresses = _resolve(host)
    except (OSError, UnicodeError):
        raise WebToolError("Could not resolve %s." % host)
    netloc = "[%s]" % host if ":" in host else host
    if port:
        netloc += ":%d" % port
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc)), addresses


def _looks_like_ip(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def fetch(url, start_index=0):
    try:
        page, truncated = _http_get(url)
    except HttpStatusError as exc:
        return "Error: %s answered HTTP %s." % (url, exc.status)
    except WebToolError as exc:
        return "Error: could not fetch %s (%s)." % (url, exc)
    except Exception as exc:
        return "Error: could not fetch %s (%s)." % (url, type(exc).__name__)

    text = _strip_tags(page)
    try:
        start = max(0, int(start_index))
    except (TypeError, ValueError):
        start = 0
    if start >= len(text):
        return ("Error: startIndex %d is past the end of the page (%d characters "
                "total)." % (start, len(text)))

    end = min(start + MAX_FETCH_CHARS, len(text))
    head = "URL: %s%sCharacters: %d-%d of %d" % (url, chr(10), start, end, len(text))
    if truncated:
        head += (chr(10) + "Note: the response body was cut at %d bytes, so the page "
                 "may really be longer." % MAX_RESPONSE_BYTES)
    tail = ""
    if end < len(text):
        tail = (chr(10) + chr(10) + "[Truncated. Call web_fetch again with startIndex=%d "
                "to continue.]" % end)
    return head + chr(10) + chr(10) + text[start:end] + tail


def sources_from_result(result):
    """从搜索结果的可读文本里反解 (标题, 网址)，供客户端展示引用来源。"""
    out = []
    pattern = r"(?m)^\d+\.\s*(.+?)\s*\n\s*(https?://\S+)\s*$"
    for m in re.finditer(pattern, str(result or "")):
        url = m.group(2).strip().rstrip(".,;:!?")
        title = m.group(1).strip()
        if url and not any(s["url"] == url for s in out):
            out.append({"title": title or url, "url": url})
    return out


def execute(name, args_raw):
    """执行一次内部网络工具。永不抛异常，永远回一句能喂回模型的字符串。"""
    name = str(name or "").strip()
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw or "{}")
        except Exception:
            args = {}
    elif isinstance(args_raw, dict):
        args = args_raw
    else:
        args = {}
    if not isinstance(args, dict):
        args = {}

    try:
        if name == WEB_SEARCH_NAME:
            return search(query_args(args), args.get("numResults") or 5)
        if name == WEB_FETCH_NAME:
            return fetch(url_arg(args), args.get("startIndex") or 0)
        return "Error: %s is not a tool this gateway runs." % name
    except Exception as exc:
        return "Error running %s: %s: %s" % (name, type(exc).__name__, exc)