#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRTS 干员模型 PNG 批量导出工具
==============================

复刻 prts.wiki 干员页面「干员模型」小节里那个 PNG 导出按钮的行为，但去掉网页、
去掉手工点击，可以一次性把成百上千个干员的「默认状态」模型图导出来。

原理
----
1. 干员模型数据在 https://torappu.prts.wiki/assets/char_spine/<charId>/meta.json，
   里面记录了每套皮肤的 正面 / 背面 / 基建 三个模型（.skel + .atlas + .png）。
2. 网页上的查看器是 static.prts.wiki 上的一个小部件（SpineViewer）。
   本工具把它镜像到本地，用本地 HTTP 服务喂给一个无头 Chrome，
   每个模型开一个同源 iframe 加载该部件；部件会自动选中
   「默认皮肤 + 正面 + 待机」并完成取景（和网页上完全同一套代码，所以出图一致）。
3. 把时间轴钉在第 0 帧，替 URL.createObjectURL / 锚点 click 打补丁，
   截获部件导出的 PNG Blob，回传到本地服务落盘 —— 不产生任何浏览器下载目录垃圾。

依赖：Python 3.8+ 标准库 + 本机已安装的 Chrome 或 Edge。无需 pip install，无需 Node。

示例
----
    python prts_models.py --all                  # 全部干员
    python prts_models.py --ids char_151_myrtle,char_002_amiya
    python prts_models.py --all --bg light       # 复刻网页默认的浅色底
    python prts_models.py --all --size 2000      # 导出 2000x2000
    python prts_models.py --list-only            # 只列出干员与 charId，不下载
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

WIKI_API = "https://prts.wiki/api.php"
WIDGET_RAW = "https://prts.wiki/index.php?title=Widget:SpineViewer&action=raw"
ASSET_BASE = "https://torappu.prts.wiki/assets/char_spine"
WIDGET_BASE = "https://static.prts.wiki/widgets/production/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# prts.wiki 前面有 Cloudflare，缺这几个头容易被 403
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 查看器里的背景预设（源码 a=[...]）：深色 / 浅色 / 透明
BG_KEYS = {"transparent": "none", "none": "none", "透明": "none",
           "light": "light", "浅色": "light",
           "dark": "dark", "深色": "dark",
           "default": "default"}      # default = 跟随网页主题，即浅色 #f2f2f2

# 查看器里导出 PNG 默认走的是 1000x1000（源码中的 et = 1e3），这里只是给出可选值。
SIZE_PRESETS = [1000, 2000, 4000]


# --------------------------------------------------------------------------
# 网络小工具
# --------------------------------------------------------------------------
def http_get(url: str, timeout: int = 90, headers: dict | None = None) -> bytes:
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:
            last = e
        time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"请求失败 {url} -> {last}")


def wiki_api(params: dict, timeout: int = 90) -> dict:
    params = dict(params)
    params.setdefault("format", "json")
    url = WIKI_API + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(3):
        try:
            return json.loads(http_get(url, timeout=timeout).decode("utf-8", "replace"))
        except Exception as e:            # Cloudflare 偶发拦截 / 网络抖动，重试
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"wiki API 失败: {params} -> {last}")


def fetch_meta(char_id: str) -> dict | None:
    """取某个干员的模型清单；确实没有模型资源（404）时返回 None。"""
    url = f"{ASSET_BASE}/{char_id}/meta.json"
    try:
        return json.loads(http_get(url, timeout=60).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# --------------------------------------------------------------------------
# 干员清单：干员页 -> charId
# --------------------------------------------------------------------------
def _category_members(cat: str) -> list[str]:
    titles, cont = [], None
    while True:
        p = {"action": "query", "list": "categorymembers", "cmtitle": cat,
             "cmlimit": "500", "cmnamespace": "0"}
        if cont:
            p.update(cont)
        j = wiki_api(p)
        titles += [x["title"] for x in j.get("query", {}).get("categorymembers", [])]
        cont = j.get("continue")
        if not cont:
            return titles


def _char_id_of_page(page: str) -> str | None:
    """页面 HTML 里 #spine-root 的 data-id 就是 charId；只取「干员模型」小节，省流量。"""
    try:
        secs = wiki_api({"action": "parse", "page": page, "prop": "sections"}, timeout=60)
        idx = None
        for s in secs.get("parse", {}).get("sections", []):
            if s.get("line", "").strip() == "干员模型":
                idx = s.get("index")
        if idx is None:
            return None
        html = wiki_api({"action": "parse", "page": page, "prop": "text",
                         "section": idx}, timeout=60)
        m = re.search(r'data-id="(char_[A-Za-z0-9_]+)"',
                      html.get("parse", {}).get("text", {}).get("*", ""))
        return m.group(1) if m else None
    except Exception:
        return None


def load_operator_table(cache_file: str, workers: int, refresh: bool = False) -> list[dict]:
    """返回 [{'page','id','name'}]，带本地缓存。"""
    if not refresh and os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)
            if data:
                return data
        except Exception:
            pass

    pages = _category_members("Category:干员")
    print(f"[清单] 分类:干员 共 {len(pages)} 个页面，正在解析 charId ...", flush=True)
    rows, done = [], 0
    with futures.ThreadPoolExecutor(max_workers=max(4, workers)) as ex:
        for page, cid in zip(pages, ex.map(_char_id_of_page, pages)):
            done += 1
            if done % 50 == 0:
                print(f"[清单] {done}/{len(pages)}", flush=True)
            if cid:
                rows.append({"page": page, "id": cid, "name": page})
    rows.sort(key=lambda r: r["page"])
    os.makedirs(os.path.dirname(os.path.abspath(cache_file)), exist_ok=True)
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"[清单] 解析到 {len(rows)} 个带模型的干员，已缓存到 {cache_file}", flush=True)
    return rows


# --------------------------------------------------------------------------
# 镜像查看器小部件（版本号带 hash，所以每次运行都先问一次当前文件名）
# --------------------------------------------------------------------------
def mirror_widgets(cache_dir: str, force: bool = False) -> dict:
    os.makedirs(cache_dir, exist_ok=True)
    marker = os.path.join(cache_dir, "manifest.json")
    if not force and os.path.exists(marker):
        try:
            with open(marker, encoding="utf-8") as f:
                m = json.load(f)
            if all(os.path.exists(os.path.join(cache_dir, n)) for n in m.get("files", [])):
                return m
        except Exception:
            pass

    raw = http_get(WIDGET_RAW).decode("utf-8", "replace")
    queue = sorted(set(re.findall(r"https://static\.prts\.wiki/widgets/production/[A-Za-z0-9._\-]+", raw)))
    if not queue:
        raise RuntimeError("没能从 Widget:SpineViewer 解析出小部件文件列表")
    seen, files = set(), []
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        name = url.rsplit("/", 1)[-1]
        # 注意：static.prts.wiki 只对 prts.wiki 源发 CORS 头，但我们把文件放本地同源，
        # 所以这里用普通请求拿到字节即可。
        data = http_get(url, headers={"User-Agent": UA, "Referer": "https://prts.wiki/"})
        with open(os.path.join(cache_dir, name), "wb") as f:
            f.write(data)
        files.append(name)
        if name.endswith(".js"):
            src = data.decode("utf-8", "replace")
            for dep in re.findall(r'from"\./([A-Za-z0-9._\-]+\.js)"', src):
                queue.append(WIDGET_BASE + dep)
            for dep in re.findall(r'import\("\./([A-Za-z0-9._\-]+\.js)"\)', src):
                queue.append(WIDGET_BASE + dep)

    def pick(prefix):
        for n in files:
            if n.startswith(prefix + "."):
                return n
        return None

    manifest = {"files": files, "viewer": pick("SpineViewer"), "polyfills": pick("polyfills"),
                "css": pick("style")}
    if not manifest["viewer"] or not manifest["polyfills"]:
        raise RuntimeError(f"小部件文件缺失: {manifest}")
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"[部件] 已镜像 {len(files)} 个文件到 {cache_dir}", flush=True)
    return manifest


# --------------------------------------------------------------------------
# 无头浏览器里的驱动页（在 iframe 中加载小部件并截获导出的 PNG）
# --------------------------------------------------------------------------
DRIVER_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>PRTS 模型批量导出</title>
<style>
 html,body{margin:0;background:#101114;color:#e6e6e6;font:13px/1.6 system-ui,"Microsoft YaHei",sans-serif}
 #status{padding:8px 12px;position:sticky;top:0;background:#101114;z-index:9}
 #frames{display:flex;flex-wrap:wrap;gap:10px;padding:0 12px 40px}
 .slot{width:900px;height:900px;overflow:hidden;background:#fff;border:1px solid #333}
 iframe{width:900px;height:900px;border:0;display:block}
</style></head><body>
<div id="status">初始化…</div>
<div id="frames"></div>
<script>
const ITEMS = __ITEMS__;
const WORKERS = __WORKERS__;
const WIDGETS = __WIDGETS__;
const BG = __BG__;
const OUT_SIZE_SELECT = __SIZE__;
const ID_PREFIX = __ID_PREFIX__;

let next = 0, ok = 0, fail = 0, bytes = 0;
const failures = [];
const t0 = Date.now();
const statusEl = document.getElementById('status');
const framesEl = document.getElementById('frames');

function logLine(extra) {
  const secs = (Date.now() - t0) / 1000;
  const speed = ok ? (ok / secs).toFixed(2) : '0';
  statusEl.textContent = `已导出 ${ok}/${ITEMS.length}  失败 ${fail}  ${speed} 个/秒  ${extra || ''}`;
}

function timeout(ms, msg) {
  return new Promise((_, rej) => setTimeout(() => rej(new Error(msg)), ms));
}

function harnessHtml(item) {
  // 注入 #SPINEDATA（等价于网页上的 SPINEDATA 路径），省掉一次 meta.json 请求
  const meta = JSON.stringify(item.meta).replace(/</g, '\\u003c');
  return [
    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
    '<link rel="stylesheet" href="/widgets/' + WIDGETS.css + '">',
    '<script>window.__errs=[];',
    'window.onerror=function(m,s,l,c){window.__errs.push(m+" @"+l+":"+c)};',
    'window.addEventListener("unhandledrejection",function(e){window.__errs.push("reject: "+(e.reason&&e.reason.message||e.reason))});',
    'window.RLQ=[];setInterval(function(){if(window.RLQ&&window.RLQ.length){',
    'var q=window.RLQ;window.RLQ=[];q.forEach(function(e){try{e[1]()}catch(err){window.__errs.push("mount: "+(err&&err.message||err))}});}},20);<\/script>',
    '<script type="module" src="/widgets/' + WIDGETS.polyfills + '"><\/script>',
    '<script type="module" src="/widgets/' + WIDGETS.viewer + '"><\/script>',
    '</head><body style="margin:0;background:#fff">',
    '<div id="spine-root" style="width:900px;height:900px">',
    '<span style="display:none" id="SPINEDATA">' + meta + '</span>',
    '</div></body></html>'
  ].join('');
}

function waitFor(fn, timeoutMs, msg, step) {
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const timer = setInterval(() => {
      let v = null;
      try { v = fn(); } catch (e) { v = null; }
      if (v) { clearInterval(timer); resolve(v); }
      else if (Date.now() - t0 > timeoutMs) { clearInterval(timer); reject(new Error(msg)); }
    }, step || 50);
  });
}

async function runOne(item, slot) {
  const host = document.getElementById('slot' + slot);
  host.innerHTML = '';
  const ifr = document.createElement('iframe');
  host.appendChild(ifr);
  ifr.srcdoc = harnessHtml(item);   // srcdoc 继承父页 origin，模块脚本同源可加载

  const doc = () => ifr.contentDocument;
  const q = (sel) => doc().querySelectorAll(sel);
  const btns = () => [...doc().querySelectorAll('#spine-root button')];

  // 1. 等部件挂载后点「载入模型」（网页上也是这一步，部件不会自动装载）
  const loadBtn = await waitFor(
    () => btns().find(b => b.textContent.indexOf('载入模型') >= 0),
    20000, '查看器部件未挂载');
  loadBtn.click();

  // 2. 等模型载入完成：动作列表出现即代表已取景完成
  try {
    await waitFor(() => q('.sv__anim').length, 60000, '模型载入超时');
  } catch (e) {
    const errs = (ifr.contentWindow && ifr.contentWindow.__errs) || [];
    const text = (doc().querySelector('#spine-root') || {}).textContent || '';
    throw new Error(e.message + (errs.length ? ' | ' + errs.slice(0, 3).join(' ; ') : '') +
                    (text ? ' | 页面提示: ' + text.replace(/\s+/g, ' ').slice(0, 120) : ''));
  }

  const D = doc();
  const win = ifr.contentWindow;

  // 3. 背景：网页默认跟主题走（浅色 #f2f2f2），也可以点「透明底」
  if (BG && BG !== 'default') {
    const sw = D.querySelector('#spine-root [data-bg="' + BG + '"]');
    if (sw) sw.click();
  }

  // 4. 导出尺寸：网页里 PNG 固定按 1000x1000 渲染（源码 et=1e3，没有尺寸下拉）。
  //    这里劫持画布的 width/height setter，把渲染分辨率按倍数放大，取景完全不变。
  const SCALE = (OUT_SIZE_SELECT || 1000) / 1000;
  if (SCALE !== 1) {
    const cv = D.querySelector('#spine-root canvas');
    if (cv) {
      for (const prop of ['width', 'height']) {
        const desc = Object.getOwnPropertyDescriptor(win.HTMLCanvasElement.prototype, prop);
        Object.defineProperty(cv, prop, {
          configurable: true,
          get() { return desc.get.call(this); },
          set(v) { desc.set.call(this, Math.max(1, Math.round(v * SCALE))); }
        });
      }
    }
  }

  // 5. 打补丁：截获导出的 Blob 与文件名，并阻止真实下载
  let capturedBlob = null;
  let resolveDl;
  const dl = new Promise(r => { resolveDl = r; });
  const origCreate = win.URL.createObjectURL.bind(win.URL);
  win.URL.createObjectURL = function (b) { capturedBlob = b; return origCreate(b); };
  const anchorProto = win.HTMLAnchorElement.prototype;
  const origClick = anchorProto.click;
  anchorProto.click = function () {
    if (this.hasAttribute && this.hasAttribute('download')) {
      resolveDl({ blob: capturedBlob, name: this.getAttribute('download') });
      return;
    }
    return origClick.apply(this, arguments);
  };

  // 6. 钉到第 0 帧：点当前动作按钮（回到 t=0 并播放），同一任务内连点「上一帧」，
  //    第一次点击即暂停，后续点击把帧号一路夹到 0。
  const activeAnim = D.querySelector('#spine-root .sv__anim.is-active');
  if (activeAnim) activeAnim.click();
  const prevBtn = [...D.querySelectorAll('#spine-root button')]
    .find(b => (b.getAttribute('data-ak-tip') || '').startsWith('上一帧'));
  if (prevBtn) for (let i = 0; i < 40; i++) prevBtn.click();

  // 7. 点导出 PNG
  const pngBtn = [...D.querySelectorAll('#spine-root button')]
    .find(b => b.textContent.replace(/\s+/g, '') === 'PNG');
  if (!pngBtn) throw new Error('未找到 PNG 导出按钮');
  pngBtn.click();

  const got = await Promise.race([dl, timeout(30000, 'PNG 导出超时')]);
  if (!got || !got.blob) throw new Error('未捕获到导出数据');

  const buf = await got.blob.arrayBuffer();
  const fname = (ID_PREFIX ? item.id + '__' : '') + (got.name || item.id + '.png');
  await fetch('/save?name=' + encodeURIComponent(fname), { method: 'POST', body: buf });
  bytes += buf.byteLength;
  host.innerHTML = '';
  return fname;
}

async function worker(slot) {
  while (true) {
    const i = next++;
    if (i >= ITEMS.length) return;
    const item = ITEMS[i];
    try {
      await runOne(item, slot);
      ok++;
    } catch (e) {
      fail++;
      failures.push({ id: item.id, name: item.name, error: String(e && e.message || e) });
      try { await fetch('/log', { method: 'POST', body: item.id + ' 失败: ' + (e && e.message || e) }); } catch (_) {}
      document.getElementById('slot' + slot).innerHTML = '';
    }
    logLine(item.name);
  }
}

(async function () {
  const workers = [];
  for (let i = 0; i < WORKERS; i++) {
    const d = document.createElement('div');
    d.className = 'slot';
    d.id = 'slot' + i;
    framesEl.appendChild(d);
    workers.push(worker(i));
  }
  await Promise.all(workers);
  logLine('完成');
  try {
    await fetch('/done', { method: 'POST', body: JSON.stringify({ ok: ok, fail: fail, bytes: bytes, failures: failures }) });
  } catch (e) {}
})();
</script></body></html>
"""


# --------------------------------------------------------------------------
# 本地服务
# --------------------------------------------------------------------------
def png_size(path: str):
    """只读 PNG 头拿宽高（不依赖 Pillow）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(33)
        if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
            return None
        return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    except Exception:
        return None


class QuietServer(http.server.ThreadingHTTPServer):
    """浏览器关页面时会断开 keep-alive 连接，别让它刷一堆 traceback。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Exporter:
    def __init__(self, out_dir: str, widget_dir: str, queue: list[dict], size: int, bg: str,
                 workers: int, id_prefix: bool):
        self.out_dir = out_dir
        self.widget_dir = widget_dir
        self.queue = queue
        self.size = size
        self.bg = bg
        self.workers = workers
        self.id_prefix = id_prefix
        self.done = threading.Event()
        self.summary = {}
        self.saved = 0
        self.collisions = 0
        self.t0 = time.time()

    def make_handler(exporter):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):     # 静音
                pass

            def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                path = urllib.parse.urlparse(self.path).path
                if path == "/":
                    with open(exporter.driver_path, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                    return
                if path.startswith("/widgets/"):
                    name = os.path.basename(path)
                    fp = os.path.join(exporter.widget_dir, name)
                    if os.path.exists(fp):
                        ctype = "application/javascript" if name.endswith(".js") else \
                                "text/css" if name.endswith(".css") else "application/octet-stream"
                        with open(fp, "rb") as f:
                            self._send(200, f.read(), ctype)
                    else:
                        self._send(404)
                    return
                self._send(404)

            def do_POST(self):
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n) if n else b""
                if parsed.path == "/save":
                    raw_name = qs.get("name", ["model.png"])[0]
                    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", raw_name) or "model.png"
                    target = os.path.join(exporter.out_dir, safe)
                    if os.path.exists(target):
                        exporter.collisions += 1
                    with open(target, "wb") as f:
                        f.write(data)
                    exporter.saved += 1
                    if exporter.saved % 10 == 0:
                        el = time.time() - exporter.t0
                        print(f"  已保存 {exporter.saved}/{len(exporter.queue)}  "
                              f"({exporter.saved / max(el, 1e-6):.2f} 个/秒)", flush=True)
                    self._send(200, b"ok")
                    return
                if parsed.path == "/log":
                    print("  " + data.decode("utf-8", "replace"), flush=True)
                    self._send(200, b"ok")
                    return
                if parsed.path == "/done":
                    try:
                        exporter.summary = json.loads(data.decode("utf-8", "replace"))
                    except Exception:
                        exporter.summary = {}
                    exporter.done.set()
                    self._send(200, b"ok")
                    return
                self._send(404)

        return Handler


# --------------------------------------------------------------------------
# Chrome / Edge 定位
# --------------------------------------------------------------------------
def find_browser(explicit: str | None) -> str:
    if explicit:
        if os.path.exists(explicit):
            return explicit
        raise SystemExit(f"找不到浏览器: {explicit}")
    cands = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        root = os.environ.get(env)
        if not root:
            continue
        cands += [
            os.path.join(root, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(root, r"Microsoft\Edge\Application\msedge.exe"),
        ]
    cands += [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    raise SystemExit("没找到 Chrome/Edge，请用 --chrome 指定可执行文件路径")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def build_queue(rows: list[dict], workers: int, refresh: bool, meta_cache: str) -> list[dict]:
    """把 charId 列表补全成带 meta.json 的待导出清单（无模型的会被剔除）。"""
    cache = {}
    if not refresh and os.path.exists(meta_cache):
        try:
            with open(meta_cache, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    todo = [r for r in rows if r["id"] not in cache]
    if todo:
        print(f"[元数据] 拉取 {len(todo)} 个干员的 meta.json ...", flush=True)

        def _fetch(row):
            try:
                return fetch_meta(row["id"])
            except Exception as e:          # 网络抖动：不写缓存，下次重跑会重试
                return {"__error__": str(e)}

        done, bad = 0, []
        with futures.ThreadPoolExecutor(max_workers=max(4, workers)) as ex:
            for r, meta in zip(todo, ex.map(_fetch, todo)):
                done += 1
                if done % 50 == 0:
                    print(f"[元数据] {done}/{len(todo)}", flush=True)
                if isinstance(meta, dict) and "__error__" in meta:
                    bad.append((r["id"], meta["__error__"]))
                    continue
                cache[r["id"]] = meta      # None 表示确实没有模型资源
        if bad:
            print(f"[元数据] {len(bad)} 个拉取失败（未缓存，重跑会重试）："
                  + ", ".join(f"{i}({e[:40]})" for i, e in bad[:3]))
        os.makedirs(os.path.dirname(os.path.abspath(meta_cache)), exist_ok=True)
        with open(meta_cache, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)

    queue, missing = [], []
    for r in rows:
        meta = cache.get(r["id"])
        if meta:
            queue.append({"id": r["id"], "name": r["name"], "meta": meta})
        else:
            missing.append(r["id"])
    if missing:
        print(f"[元数据] {len(missing)} 个干员没有模型资源，已跳过（如 {', '.join(missing[:3])} ...）")
    return queue


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="批量导出 prts.wiki 干员模型的默认状态 PNG",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--all", action="store_true", help="导出全部干员（默认）")
    src.add_argument("--ids", help="指定 charId，逗号分隔，如 char_151_myrtle,char_002_amiya")
    src.add_argument("--ids-file", help="从文件读取 charId，一行一个")
    ap.add_argument("--out", default="prts_models", help="输出目录（默认 prts_models）")
    ap.add_argument("--cache", default=".prts_cache", help="缓存目录（默认 .prts_cache）")
    ap.add_argument("--workers", type=int, default=4, help="并发 iframe 数（默认 4，网络差可降到 2）")
    ap.add_argument("--bg", default="transparent",
                    help="背景：transparent(默认,透明底) / light(浅色,网页默认观感) / dark / default(跟随主题)")
    ap.add_argument("--size", type=int, default=1000, choices=SIZE_PRESETS,
                    help="导出边长像素，默认 1000（与网页一致）")
    ap.add_argument("--name", default="id", choices=["id", "wiki"],
                    help="输出文件名：id=charId前缀+网页原名（默认，避免同名干员互相覆盖）/ wiki=只用网页原名")
    ap.add_argument("--list-only", action="store_true", help="只列出干员与 charId")
    ap.add_argument("--dry-run", action="store_true", help="只统计将要导出的模型，不启动浏览器")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（试跑用）")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存，重新拉取干员清单与元数据")
    ap.add_argument("--chrome", help="Chrome/Edge 可执行文件路径")
    ap.add_argument("--keep-profile", action="store_true", help="保留临时浏览器 profile（排查问题用）")
    ap.add_argument("--timeout", type=int, default=0, help="总超时秒数（0=不限制）")
    args = ap.parse_args(argv)

    cache_dir = os.path.abspath(args.cache)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    # 1. 待办清单
    if args.ids or args.ids_file:
        if args.ids_file:
            with open(args.ids_file, encoding="utf-8") as f:
                ids = [x.strip() for x in f if x.strip() and not x.startswith("#")]
        else:
            ids = [x.strip() for x in args.ids.split(",") if x.strip()]
        rows = [{"id": i, "name": i, "page": i} for i in ids]
    else:
        rows = load_operator_table(os.path.join(cache_dir, "operators.json"),
                                   args.workers, refresh=args.refresh)

    if args.list_only:
        for r in rows[:args.limit] if args.limit else rows:
            print(f"{r['id']}\t{r['page']}")
        print(f"共 {len(rows)} 个")
        return 0

    if args.limit:
        rows = rows[:args.limit]

    if not rows:
        print("没有待导出的干员。")
        return 1

    queue = build_queue(rows, args.workers, args.refresh,
                        os.path.join(cache_dir, "meta.json"))
    if not queue:
        print("这些干员都没有模型资源。")
        return 1

    if args.dry_run:
        skins = {}
        for q in queue:
            for s, views in (q.get("meta", {}).get("skin") or {}).items():
                skins[s] = skins.get(s, 0) + len(views)
        print(f"[试跑] 待导出 {len(queue)} 个模型，输出目录 {out_dir}")
        print(f"[试跑] 其中包含 {len(skins)} 套皮肤/形态（如 {', '.join(list(skins)[:8])} ...）")
        return 0

    # 2. 已经导出过的直接跳过（文件名形如 [<charId>__]<干员名>-<皮肤>-<视角>-<动作>-f0.png）
    #    同时校验分辨率，换 --size 重跑时不会被误跳过。
    existing = os.listdir(out_dir)
    use_id = args.name == "id"
    pending, skipped = [], 0
    for item in queue:
        prefix = (item["id"] + "__") if use_id else (item["name"] + "-")
        hit = any(png_size(os.path.join(out_dir, f)) == (args.size, args.size)
                  for f in existing if f.startswith(prefix))
        if hit:
            skipped += 1
            continue
        pending.append(item)
    if skipped:
        print(f"[跳过] {skipped} 个已存在同名输出，剩余 {len(pending)} 个待导出")
    if not pending:
        print("全部已存在，无需下载。")
        return 0

    # 3. 镜像查看器部件
    manifest = mirror_widgets(os.path.join(cache_dir, "widgets"), force=args.refresh)

    # 4. 起本地服务
    bg_key = BG_KEYS.get(str(args.bg).lower(), str(args.bg))
    ex = Exporter(out_dir, os.path.join(cache_dir, "widgets"), pending,
                  args.size, bg_key, args.workers, use_id)
    driver = (DRIVER_HTML
              .replace("__ITEMS__", json.dumps(pending, ensure_ascii=False))
              .replace("__WORKERS__", str(max(1, args.workers)))
              .replace("__WIDGETS__", json.dumps(manifest, ensure_ascii=False))
              .replace("__BG__", json.dumps(bg_key, ensure_ascii=False))
              .replace("__SIZE__", str(args.size))
              .replace("__ID_PREFIX__", "true" if use_id else "false"))
    driver_path = os.path.join(cache_dir, "driver.html")
    with open(driver_path, "w", encoding="utf-8") as f:
        f.write(driver)
    ex.driver_path = driver_path

    port = free_port()
    httpd = QuietServer(("127.0.0.1", port), Exporter.make_handler(ex))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"[服务] {url}")

    # 5. 起无头浏览器
    browser = find_browser(args.chrome)
    profile = tempfile.mkdtemp(prefix="prts-chrome-")
    cmd = [
        browser,
        "--headless=new",
        "--enable-unsafe-swiftshader",     # 无 GPU 环境下用软件 WebGL
        "--user-data-dir=" + profile,
        "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-sync", "--disable-component-update",
        "--disable-background-networking", "--mute-audio",
        "--window-size=1900,1000",
        url,
    ]
    print(f"[浏览器] {browser}")
    print(f"[任务] 待导出 {len(pending)} 个模型，尺寸 {args.size}x{args.size}，背景 {args.bg}，并发 {args.workers}")
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while not ex.done.is_set():
            if proc.poll() is not None:
                print("[错误] 浏览器提前退出（可用 --keep-profile 并手动打开上面的地址排查）")
                break
            if args.timeout and time.time() - t0 > args.timeout:
                print("[错误] 达到 --timeout 上限，停止")
                break
            ex.done.wait(0.5)
    except KeyboardInterrupt:
        print("\n[中断] 用户取消")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        httpd.shutdown()
        if not args.keep_profile:
            shutil.rmtree(profile, ignore_errors=True)
        else:
            print(f"[提示] profile 保留在 {profile}")

    el = time.time() - t0
    s = ex.summary or {}
    print("")
    print(f"完成：成功 {ex.saved} / 待办 {len(pending)}，用时 {el:.1f}s"
          + (f"，失败 {s.get('fail')}" if s.get("fail") else ""))
    if ex.collisions:
        print(f"[注意] 有 {ex.collisions} 个文件被同名覆盖（不同干员同名，如阿米娅的各形态）；"
              f"建议用默认的 --name id 以 charId 区分")
    actual = len([n for n in os.listdir(out_dir) if n.lower().endswith(".png")])
    print(f"输出目录：{out_dir}（其中 PNG {actual} 个）")
    if s.get("failures"):
        print("失败明细：")
        for f in s["failures"][:20]:
            print(f"  - {f.get('id')}: {f.get('error')}")
    return 0 if ex.saved else 1


if __name__ == "__main__":
    sys.exit(main())
