#!/usr/bin/env python3
"""
确认 Web 服务 — 浏览器确认后直接执行 API，无需 LLM 二次请求

完整流程:
  ① LLM 调用 deleteProject(code="123")
  ② Middleware 拦截 → 存储 executor → 返回确认链接给 LLM
  ③ 用户在浏览器打开链接 → 点击"确认执行"
  ④ ConfirmServer 直接调用 executor 执行 API → 浏览器显示结果
  ⑤ 用户看到结果，告诉 LLM "已完成" 即可

运行方式: 集成在 dolphin_mcp_server_secure.py 中自动启动（后台线程）
也可以单独运行测试: python confirm_server.py
"""

import asyncio
import json
import time
import uuid
import threading
from datetime import datetime

# Flask 可能有，也可能没有；没有时用简单的 HTTP 服务替代
try:
    from flask import Flask, request
    HAS_FLASK = True
except ImportError:
    Flask = None
    request = None
    HAS_FLASK = False


# ──────────────────────────────────────────────
# 共享确认队列（MCP Server 和 Web 服务共用同一个实例）
# ──────────────────────────────────────────────

class ConfirmQueue:
    """
    线程安全的确认队列，支持延迟执行。

    数据结构:
    {
        "a1b2c3d4": {
            "status": "pending",        # pending / approved / rejected / expired / error
            "tool": "deleteProject",
            "args": {"code": "123"},
            "created_at": 1718012345.0,
            "expire_at": 1718012645.0,
            "resolved_at": None,
            "executor": <async callable>, # 浏览器确认后直接执行
            "result": None,               # API 执行结果文本
        }
    }
    """

    def __init__(self, expire_seconds: int = 300):
        self.expire_seconds = expire_seconds
        self._queue: dict = {}
        self._lock = threading.Lock()
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """设置主 asyncio 事件循环（用于跨线程执行 async executor）"""
        self._event_loop = loop

    def create(self, tool_name: str, args: dict, executor=None) -> str:
        """创建一个待确认请求，返回 confirm_id"""
        confirm_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._queue[confirm_id] = {
                'status': 'pending',
                'tool': tool_name,
                'args': args,
                'created_at': now,
                'expire_at': now + self.expire_seconds,
                'resolved_at': None,
                'executor': executor,
                'result': None,
            }
        # 诊断日志：记录创建事件 + 当前队列快照（lock 已释放，可安全调用 diagnostic）
        print(f'  [ConfirmQueue] CREATE id={confirm_id} tool={tool_name} '
              f'expire_at={self._queue[confirm_id]["expire_at"]:.0f} | {self.diagnostic()}', flush=True)
        return confirm_id

    def diagnostic(self) -> str:
        """返回当前队列快照（用于日志诊断）。调用方不应已持有 _lock。"""
        with self._lock:
            pending = [cid for cid, r in self._queue.items() if r['status'] == 'pending']
            return (f'queue_size={len(self._queue)} pending_count={len(pending)} '
                    f'pending_ids={pending}')

    def get(self, confirm_id: str) -> dict | None:
        """获取确认请求详情"""
        with self._lock:
            record = self._queue.get(confirm_id)
            if not record:
                return None
            # 检查是否过期
            if record['status'] == 'pending' and time.time() > record['expire_at']:
                record['status'] = 'expired'
            return record

    def approve_and_execute(self, confirm_id: str) -> tuple[bool, str | None]:
        """
        确认并执行延迟操作。返回 (success, result_text)。
        在确认服务线程中调用，通过 run_coroutine_threadsafe 调度到主事件循环。
        """
        with self._lock:
            record = self._queue.get(confirm_id)
            if not record or record['status'] != 'pending':
                return False, '无效或已过期的确认请求'
            record['status'] = 'executing'

        executor = record.get('executor')
        print(f'  [ConfirmServer] executor={executor is not None} event_loop={self._event_loop is not None}', flush=True)

        if not executor or not self._event_loop:
            # 没有 executor，仅标记确认
            with self._lock:
                record['status'] = 'approved'
                record['resolved_at'] = time.time()
            return True, None

        # 跨线程调度 async executor 到主事件循环
        try:
            print(f'  [ConfirmServer] 调度 executor 到事件循环...', flush=True)
            future = asyncio.run_coroutine_threadsafe(executor(), self._event_loop)
            print(f'  [ConfirmServer] 等待结果 (timeout=15s)...', flush=True)
            result = future.result(timeout=15)
            result_text = self._extract_text(result)
            with self._lock:
                record['status'] = 'approved'
                record['resolved_at'] = time.time()
                record['result'] = result_text
            print(f'  [ConfirmServer] 执行成功: {result_text[:200]}', flush=True)
            return True, result_text
        except Exception as e:
            import traceback
            err = f'执行失败: {e}'
            print(f'  [ConfirmServer] 执行错误: {e}', flush=True)
            traceback.print_exc()
            with self._lock:
                record['status'] = 'error'
                record['resolved_at'] = time.time()
                record['result'] = err
            return False, err

    @staticmethod
    def _extract_text(result) -> str:
        """从 ToolResult 或其他类型中提取文本"""
        if result is None:
            return '(无返回)'
        # ToolResult 有 content 属性
        if hasattr(result, 'content'):
            texts = []
            for block in result.content:
                if hasattr(block, 'text'):
                    texts.append(block.text)
            return '\n'.join(texts) if texts else str(result)
        return str(result)

    def reject(self, confirm_id: str) -> bool:
        """拒绝执行"""
        with self._lock:
            record = self._queue.get(confirm_id)
            if not record or record['status'] != 'pending':
                return False
            record['status'] = 'rejected'
            record['resolved_at'] = time.time()
            return True

    def check(self, confirm_id: str) -> str:
        """
        检查确认状态，返回: pending / approved / rejected / expired / executing / error / not_found
        """
        record = self.get(confirm_id)
        if not record:
            return 'not_found'
        if record['status'] == 'pending' and time.time() > record['expire_at']:
            record['status'] = 'expired'
        return record['status']

    def cleanup(self):
        """清理过期的确认请求"""
        now = time.time()
        with self._lock:
            expired_ids = [
                cid for cid, rec in self._queue.items()
                if rec['status'] in ('expired', 'approved', 'rejected', 'error')
                and now - rec.get('resolved_at', rec['expire_at']) > 60
            ]
            for cid in expired_ids:
                del self._queue[cid]

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._queue.values() if r['status'] == 'pending')


# 全局单例（MCP Server 和 Web 服务共享）
confirm_queue = ConfirmQueue(expire_seconds=300)


# ──────────────────────────────────────────────
# 确认页面 HTML 模板
# ──────────────────────────────────────────────

def render_confirm_page(record: dict, confirm_id: str) -> str:
    """渲染确认页面"""
    args_json = json.dumps(record['args'], indent=2, ensure_ascii=False, default=str)
    created = datetime.fromtimestamp(record['created_at']).strftime('%Y-%m-%d %H:%M:%S')
    expire = datetime.fromtimestamp(record['expire_at']).strftime('%Y-%m-%d %H:%M:%S')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>操作确认 - DolphinScheduler MCP</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #f0f2f5;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .card {{
            background: white;
            border-radius: 12px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.1);
            max-width: 600px;
            width: 90%;
            padding: 32px;
        }}
        .header {{
            display: flex;
            align-items: center;
            gap: 12px;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid #eee;
        }}
        .icon {{ font-size: 32px; }}
        .title {{ font-size: 20px; font-weight: 600; color: #1a1a1a; }}
        .info-row {{
            display: flex; padding: 8px 0;
            border-bottom: 1px solid #f5f5f5;
        }}
        .info-label {{ width: 80px; color: #888; font-size: 14px; flex-shrink: 0; }}
        .info-value {{ color: #333; font-size: 14px; word-break: break-all; }}
        .args-box {{
            background: #f8f9fa; border: 1px solid #e8e8e8;
            border-radius: 8px; padding: 12px; margin: 12px 0;
            font-family: "SF Mono", Monaco, monospace;
            font-size: 13px; color: #333;
            white-space: pre-wrap; word-break: break-all;
        }}
        .actions {{ display: flex; gap: 12px; margin-top: 24px; }}
        .btn {{
            flex: 1; padding: 12px 24px; border: none;
            border-radius: 8px; font-size: 16px; font-weight: 500;
            cursor: pointer; transition: all 0.2s;
        }}
        .btn-approve {{ background: #52c41a; color: white; }}
        .btn-approve:hover {{ background: #389e0d; }}
        .btn-reject {{ background: #ff4d4f; color: white; }}
        .btn-reject:hover {{ background: #cf1322; }}
        .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        .confirm-id {{
            font-family: monospace; background: #f0f0f0;
            padding: 2px 8px; border-radius: 4px; font-size: 13px;
        }}
        .warning {{
            background: #fff7e6; border: 1px solid #ffe58f;
            border-radius: 8px; padding: 12px; margin: 12px 0;
            color: #ad6800; font-size: 14px;
        }}
        .refresh-hint {{ text-align: center; color: #999; font-size: 12px; margin-top: 16px; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <span class="icon">🔒</span>
            <span class="title">操作确认</span>
        </div>

        <div class="warning">
            ⚠️ 这是一个敏感操作，确认后将<strong>立即执行</strong>，请仔细核对。
        </div>

        <div class="info-row">
            <span class="info-label">确认ID</span>
            <span class="info-value confirm-id">{confirm_id}</span>
        </div>
        <div class="info-row">
            <span class="info-label">工具</span>
            <span class="info-value" style="font-weight:600;color:#c41d7f">{record['tool']}</span>
        </div>
        <div class="info-row">
            <span class="info-label">发起时间</span>
            <span class="info-value">{created}</span>
        </div>
        <div class="info-row">
            <span class="info-label">过期时间</span>
            <span class="info-value">{expire}</span>
        </div>

        <div style="margin-top:12px;font-size:14px;color:#888;">参数:</div>
        <div class="args-box">{args_json}</div>

        <form id="confirmForm">
            <div class="actions">
                <button type="button" class="btn btn-approve" onclick="doAction('approve')">
                    ✅ 确认执行
                </button>
                <button type="button" class="btn btn-reject" onclick="doAction('reject')">
                    ❌ 拒绝
                </button>
            </div>
        </form>

        <div class="refresh-hint">
            剩余 <span id="countdown"></span> 秒过期
        </div>
    </div>

    <script>
        function doAction(action) {{
            // 禁用按钮，防止重复点击
            document.querySelectorAll('.btn').forEach(b => {{
                b.disabled = true;
                b.textContent = '⏳ 执行中...';
            }});

            // 用 fetch 发 POST，不依赖表单提交
            fetch(window.location.href, {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                body: 'action=' + action
            }})
            .then(resp => resp.text())
            .then(html => {{ document.open(); document.write(html); document.close(); }})
            .catch(err => {{
                document.querySelector('.card').innerHTML =
                    '<div style="text-align:center"><div style="font-size:48px">❌</div>' +
                    '<h2>请求失败</h2><p>' + err + '</p></div>';
            }});
        }}

        const expireAt = {record['expire_at']} * 1000;
        function updateCountdown() {{
            const remain = Math.max(0, Math.floor((expireAt - Date.now()) / 1000));
            document.getElementById('countdown').textContent = remain;
            if (remain <= 0) location.reload();
        }}
        updateCountdown();
        setInterval(updateCountdown, 1000);
    </script>
</body>
</html>"""


def render_result_page(status: str, tool_name: str, confirm_id: str,
                        api_result: str | None = None) -> str:
    """渲染确认结果页面，包含 API 执行结果"""
    status_map = {
        'approved': ('✅ 操作已执行', 'status-approved',
                     f'操作 `{tool_name}` 已在服务器端直接执行。'),
        'rejected': ('❌ 已拒绝', 'status-rejected',
                     f'操作 `{tool_name}` 已拒绝，未执行。'),
        'expired':  ('⏰ 已过期', 'status-expired',
                     '确认请求已过期（超过有效期），请重新发起操作。'),
        'not_found': ('❓ 请求不存在', 'status-expired',
                      '该确认请求在服务端不存在。常见原因：服务已重启、'
                      '链接已失效、或点到了旧会话的链接。请重新发起操作。'),
        'error':    ('❌ 执行失败', 'status-rejected',
                     f'操作 `{tool_name}` 执行过程中发生错误。'),
    }
    title, css_class, message = status_map.get(status, ('❓', '', '未知状态'))

    # API 结果展示区
    result_html = ''
    if api_result:
        # 转义 HTML 特殊字符
        safe_result = (api_result
                       .replace('&', '&amp;')
                       .replace('<', '&lt;')
                       .replace('>', '&gt;'))
        result_html = f'''
        <div style="margin-top:20px;text-align:left;">
            <div style="font-size:14px;color:#888;margin-bottom:8px;">📋 API 执行结果:</div>
            <div class="args-box" style="
                background:#f8f9fa; border:1px solid #e8e8e8;
                border-radius:8px; padding:12px;
                font-family:'SF Mono',Monaco,monospace;
                font-size:13px; color:#333;
                white-space:pre-wrap; word-break:break-all;
                max-height:300px; overflow-y:auto;
            ">{safe_result}</div>
        </div>'''

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>确认结果 - DolphinScheduler MCP</title>
    <style>
        body {{
            font-family: -apple-system, sans-serif;
            background: #f0f2f5; min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
        }}
        .card {{
            background: white; border-radius: 12px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.1);
            max-width: 600px; width: 90%; padding: 32px; text-align: center;
        }}
        .icon {{ font-size: 48px; margin-bottom: 16px; }}
        .title {{ font-size: 20px; font-weight: 600; margin-bottom: 8px; }}
        .status-approved {{ color: #52c41a; }}
        .status-rejected {{ color: #ff4d4f; }}
        .status-expired {{ color: #faad14; }}
        .message {{ color: #666; font-size: 14px; }}
        .confirm-id {{
            font-family: monospace; background: #f0f0f0;
            padding: 2px 8px; border-radius: 4px; font-size: 12px;
            color: #999; margin-top: 16px; display: inline-block;
        }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">{title.split()[0]}</div>
        <div class="title {css_class}">{title}</div>
        <div class="message">{message}</div>
        {result_html}
        <div class="confirm-id">ID: {confirm_id}</div>
    </div>
</body>
</html>"""


# ──────────────────────────────────────────────
# Flask 版 Web 服务
# ──────────────────────────────────────────────

def create_flask_app(queue: ConfirmQueue) -> Flask:
    app = Flask(__name__)

    @app.route('/confirm/<confirm_id>')
    def show_confirm(confirm_id):
        record = queue.get(confirm_id)
        if not record:
            return render_result_page('expired', '未知', confirm_id), 404
        if record['status'] != 'pending':
            return render_result_page(
                record['status'], record['tool'], confirm_id,
                api_result=record.get('result'),
            )
        return render_confirm_page(record, confirm_id)

    @app.route('/confirm/<confirm_id>', methods=['POST'])
    def handle_confirm(confirm_id):
        action = request.form.get('action', '')
        if action == 'approve':
            success, api_result = queue.approve_and_execute(confirm_id)
            record = queue.get(confirm_id)
            status = 'approved' if success else 'error'
            return render_result_page(
                status, record['tool'] if record else '未知',
                confirm_id, api_result=api_result,
            )
        else:
            queue.reject(confirm_id)
            record = queue.get(confirm_id)
            return render_result_page(
                'rejected', record['tool'] if record else '未知', confirm_id,
            )

    @app.route('/api/status/<confirm_id>')
    def api_status(confirm_id):
        status = queue.check(confirm_id)
        record = queue.get(confirm_id)
        return json.dumps({
            'confirm_id': confirm_id,
            'status': status,
            'tool': record['tool'] if record else None,
        })

    return app


# ──────────────────────────────────────────────
# 简易 HTTP 版 Web 服务（无 Flask 依赖）
# ──────────────────────────────────────────────

from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


def create_simple_server(queue: ConfirmQueue, port: int):
    """不依赖 Flask 的简易确认服务"""

    class ConfirmHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path

            # API 状态查询
            if path.startswith('/api/status/'):
                confirm_id = path.split('/')[-1]
                status = queue.check(confirm_id)
                record = queue.get(confirm_id)
                self._json({
                    'confirm_id': confirm_id,
                    'status': status,
                    'tool': record['tool'] if record else None,
                })
                return

            # 确认页面
            if path.startswith('/confirm/'):
                confirm_id = path.split('/')[-1]
                record = queue.get(confirm_id)
                # 诊断日志：记录每次确认页查询的命中情况（区分 pending/过期/不存在）
                hit = record['status'] if record else 'NOT_FOUND'
                print(f'  [ConfirmServer] GET /confirm/{confirm_id} -> {hit} | {queue.diagnostic()}', flush=True)
                if record is None:
                    # 链接对应的请求不存在（服务重启 / 链接失效 / 旧会话）
                    self._html(render_result_page('not_found', '未知', confirm_id))
                elif record['status'] != 'pending':
                    self._html(render_result_page(
                        record['status'], record['tool'], confirm_id,
                        api_result=record.get('result'),
                    ))
                else:
                    self._html(render_confirm_page(record, confirm_id))
                return

            self._html('<h1>DolphinScheduler MCP 确认服务</h1><p>服务运行中</p>')

        def do_POST(self):
            path = urlparse(self.path).path
            if path.startswith('/confirm/'):
                confirm_id = path.split('/')[-1]
                try:
                    content_length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(content_length).decode()
                    params = parse_qs(body)
                    action = params.get('action', [''])[0]

                    print(f'  [ConfirmServer] POST {path} action={action}', flush=True)

                    if action == 'approve':
                        success, api_result = queue.approve_and_execute(confirm_id)
                        record = queue.get(confirm_id)
                        status = 'approved' if success else 'error'
                        self._html(render_result_page(
                            status,
                            record['tool'] if record else '未知',
                            confirm_id,
                            api_result=api_result,
                        ))
                    else:
                        queue.reject(confirm_id)
                        record = queue.get(confirm_id)
                        self._html(render_result_page(
                            'rejected',
                            record['tool'] if record else '未知',
                            confirm_id,
                        ))
                except Exception as e:
                    print(f'  [ConfirmServer] POST ERROR: {e}', flush=True)
                    import traceback
                    traceback.print_exc()
                    self._html(f'<h1>错误</h1><pre>{e}</pre>', code=500)
                return

            self._json({'error': 'not found'}, 404)

        def _html(self, content: str, code: int = 200):
            try:
                self.send_response(code)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(content.encode('utf-8'))
            except (BrokenPipeError, ConnectionResetError):
                # 客户端已断开（k8s 存活探针 / 浏览器刷新 / 取消请求），
                # 静默忽略，避免刷屏 traceback。
                pass

        def _json(self, data: dict, code: int = 200):
            try:
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format, *args):
            """输出访问日志"""
            if args:
                print(f'  [ConfirmServer] {args[0]}', flush=True)

    # 使用 ThreadingHTTPServer：每个请求独立线程，避免 approve_and_execute
    # 执行 API（最长 15s）期间阻塞存活探针 / 其它确认请求。
    server = ThreadingHTTPServer(('0.0.0.0', port), ConfirmHandler)
    server.daemon_threads = True
    return server


# ──────────────────────────────────────────────
# 启动确认服务（后台线程）
# ──────────────────────────────────────────────

def start_confirm_server(queue: ConfirmQueue, port: int = 8080) -> threading.Thread:
    """
    在后台线程中启动确认 Web 服务。
    返回线程对象。
    """
    if HAS_FLASK:
        app = create_flask_app(queue)
        thread = threading.Thread(
            target=lambda: app.run(host='0.0.0.0', port=port, threaded=True),
            daemon=True,
        )
    else:
        server = create_simple_server(queue, port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)

    thread.start()
    return thread


# ──────────────────────────────────────────────
# 单独运行测试
# ──────────────────────────────────────────────

if __name__ == '__main__':
    print("🚀 确认服务测试模式")
    print(f"   依赖 Flask: {'是' if HAS_FLASK else '否（使用内置 HTTP 服务）'}")

    # 模拟创建一个确认请求（无 executor，仅测试页面）
    cid = confirm_queue.create('deleteProject', {
        'code': '123456',
        'projectCode': '789',
    })
    print(f"   模拟确认请求: {cid}")
    print(f"   请打开浏览器访问: http://localhost:8080/confirm/{cid}")

    thread = start_confirm_server(confirm_queue, port=8080)
    print("   确认服务已启动，按 Ctrl+C 退出")

    try:
        while True:
            import time
            time.sleep(5)
            confirm_queue.cleanup()
            status = confirm_queue.check(cid)
            print(f"   状态: {status} | 待确认: {confirm_queue.pending_count()}")
            if status in ('approved', 'rejected', 'expired', 'error'):
                print(f"   确认结果: {status}")
                break
    except KeyboardInterrupt:
        print("\n   已停止")
