#!/usr/bin/env python3
"""
DolphinScheduler MCP Server — 安全增强版（浏览器确认后直接执行）

适配 FastMCP >= 3.4

完整流程:
  ① 大模型调用 deleteProject(code="123")
  ② SecurityMiddleware.on_call_tool 拦截 → action=confirm
  ③ 存储 executor（延迟执行的 async 函数），生成 confirm_id="a1b2c3"
  ④ 返回确认链接给大模型（ToolResult）
  ⑤ 用户在浏览器打开链接 → 点击确认
  ⑥ ConfirmServer 直接调用 executor 执行 API → 浏览器显示结果
  ⑦ 用户看到结果，告诉 LLM "已完成" 即可（LLM 无需二次请求）
"""

import asyncio
import json
import logging
import os
import sys
import time
import fnmatch
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import CallToolRequestParams, TextContent

from confirm_server import ConfirmQueue, start_confirm_server

# ==================== 配置区 ====================
BASE_URL = os.environ.get('DS_BASE_URL', 'http://localhost:12346')
SPEC_URL = f'{BASE_URL}/dolphinscheduler/v3/api-docs?group=v1(current)'
DS_TOKEN = os.environ.get('DS_TOKEN', '')
HOST = os.environ.get('MCP_HOST', '0.0.0.0')
PORT = int(os.environ.get('MCP_PORT', '3000'))
SECURITY_CONFIG_PATH = os.environ.get('MCP_SECURITY_CONFIG', str(Path(__file__).parent / 'mcp_security.yaml'))
# ===============================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    stream=sys.stderr,
)
logger = logging.getLogger('dolphin-mcp-secure')


# ──────────────────────────────────────────────
# OpenAPI Spec 修复（与原版完全相同）
# ──────────────────────────────────────────────

def _fix_malformed_refs(obj):
    if isinstance(obj, dict):
        if '$ref' in obj:
            ref_val = str(obj['$ref'])
            if ref_val.startswith('Error-ModelName') or '{' in ref_val or 'org.apache' in ref_val:
                obj.pop('$ref')
                obj['type'] = 'string'
                obj['description'] = f'(auto-fixed ref: {ref_val[:80]})'
        for v in obj.values():
            _fix_malformed_refs(v)
    elif isinstance(obj, list):
        for item in obj:
            _fix_malformed_refs(item)


def _infer_param_location(param_name: str, path: str) -> str:
    return 'path' if '{' + param_name + '}' in path else 'query'


def fix_openapi_spec(spec: dict) -> dict:
    paths = spec.get('paths', {})
    if not isinstance(paths, dict):
        return spec
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in list(path_item.items()):
            if not isinstance(operation, dict):
                continue
            if method.lower() not in ('get', 'post', 'put', 'delete', 'patch'):
                continue
            if 'parameters' in operation and isinstance(operation['parameters'], list):
                fixed = []
                for param in operation['parameters']:
                    if not isinstance(param, dict):
                        continue
                    if 'in' not in param:
                        param['in'] = _infer_param_location(param.get('name', ''), path)
                    if 'schema' not in param and 'type' in param:
                        param['schema'] = {'type': param.pop('type')}
                        if 'format' in param:
                            param['schema']['format'] = param.pop('format')
                    schema = param.get('schema')
                    if isinstance(schema, dict) and schema.get('format') == 'int64':
                        schema['type'] = 'string'
                        schema.pop('format', None)
                    _fix_malformed_refs(param)
                    fixed.append(param)
                operation['parameters'] = fixed
            for key in ('requestBody', 'responses'):
                if key in operation:
                    _fix_malformed_refs(operation[key])
    if 'components' in spec:
        _fix_malformed_refs(spec['components'])
    return spec


# ──────────────────────────────────────────────
# 安全配置
# ──────────────────────────────────────────────

class SecurityConfig:
    """加载并解析 mcp_security.yaml"""

    def __init__(self, config_path: str):
        with open(config_path) as f:
            self.cfg = yaml.safe_load(f)

        sec = self.cfg.get('security', {})
        self.enabled = sec.get('enabled', True)
        self.audit_log_path = sec.get('audit_log', 'mcp_audit.log')

        confirm_cfg = sec.get('confirm', {})
        self.confirm_base_url = os.environ.get(
            'MCP_CONFIRM_BASE_URL',
            confirm_cfg.get('base_url', 'http://localhost:8080'),
        )
        self.confirm_listen_port = int(os.environ.get(
            'MCP_CONFIRM_PORT',
            str(confirm_cfg.get('listen_port', 8080)),
        ))
        self.confirm_expire = int(os.environ.get(
            'MCP_CONFIRM_EXPIRE',
            str(confirm_cfg.get('expire_seconds', 300)),
        ))

        self._rules = []
        for level in self.cfg.get('risk_levels', []):
            action = level['action']
            for pattern in level.get('tools', []):
                self._rules.append((pattern, action))

        self._always_block = self.cfg.get('always_block', [])
        self._always_allow = self.cfg.get('always_allow', [])

    def get_action(self, tool_name: str) -> str:
        if not self.enabled:
            return 'allow'
        for p in self._always_block:
            if fnmatch.fnmatch(tool_name, p):
                return 'block'
        for p in self._always_allow:
            if fnmatch.fnmatch(tool_name, p):
                return 'allow'
        for pattern, action in self._rules:
            if fnmatch.fnmatch(tool_name, pattern):
                return action
        return 'confirm'


# ──────────────────────────────────────────────
# 审计日志
# ──────────────────────────────────────────────

class AuditLogger:
    def __init__(self, log_path: str):
        self._fh = open(log_path, 'a')

    def log(self, tool_name: str, args: dict, action: str, result: str):
        entry = {
            'timestamp': datetime.now().isoformat(),
            'tool': tool_name,
            'action': action,
            'args_summary': json.dumps(args, ensure_ascii=False, default=str)[:500],
            'result': result,
        }
        self._fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
        self._fh.flush()


# ──────────────────────────────────────────────
# 核心：安全中间件（浏览器确认后直接执行，LLM 无需重试）
# ──────────────────────────────────────────────

def _text_result(text: str, *, is_error: bool = False) -> ToolResult:
    """构造包含纯文本的 ToolResult，强制走 CallToolResult 序列化路径"""
    return ToolResult(
        content=[TextContent(type='text', text=text)],
        is_error=is_error,
        meta={'_security': True},
    )


class SecurityMiddleware(Middleware):
    """
    基于 FastMCP Middleware 的安全拦截层。

    confirm 级别的操作：
      - Middleware 拦截后不执行，而是存储一个 async executor
      - 用户浏览器确认后，ConfirmServer 直接调用 executor 执行 API
      - LLM 不需要二次请求
    """

    def __init__(self, sec_config: SecurityConfig, mcp_server: FastMCP):
        self.sec_config = sec_config
        self.mcp_server = mcp_server
        self.confirm_queue = ConfirmQueue(expire_seconds=sec_config.confirm_expire)
        self.audit = AuditLogger(sec_config.audit_log_path)

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next,
    ) -> ToolResult:
        """拦截所有工具调用，根据安全策略决定放行/记录/确认/阻止"""
        tool_name = context.message.name
        arguments = context.message.arguments or {}
        action = self.sec_config.get_action(tool_name)

        # ── block: 直接拒绝 ──
        if action == 'block':
            self.audit.log(tool_name, arguments, 'blocked', 'refused')
            return _text_result(f'🔒 工具 `{tool_name}` 已被安全策略屏蔽，无法执行。', is_error=True)

        # ── allow: 直接放行 ──
        if action == 'allow':
            return await call_next(context)

        # ── confirm: 暂停执行，等待浏览器确认后由 ConfirmServer 直接执行 ──
        if action == 'confirm':
            # 创建 executor：浏览器确认后调用 mcp_server.call_tool 执行
            async def deferred_executor():
                return await self.mcp_server.call_tool(
                    tool_name, arguments,
                    run_middleware=False,  # 跳过中间件，避免二次拦截
                )

            cid = self.confirm_queue.create(tool_name, arguments, executor=deferred_executor)
            confirm_url = f"{self.sec_config.confirm_base_url}/confirm/{cid}"

            self.audit.log(tool_name, arguments, 'confirm_deferred', f'pending:{cid}')

            return _text_result(
                f'🔒 **此操作需要用户在浏览器中确认后自动执行**\n\n'
                f'工具: `{tool_name}`\n'
                f'参数: `{json.dumps(arguments, ensure_ascii=False, default=str)}`\n\n'
                f'确认链接: {confirm_url}\n\n'
                f'请通知用户打开上方链接完成确认，操作将在确认后自动执行。\n'
                f'用户在浏览器中即可看到执行结果。'
            )

        # ── log: 记录日志后执行 ──
        if action == 'log':
            try:
                result = await call_next(context)
                self.audit.log(tool_name, arguments, 'log', 'success')
                return result
            except Exception as e:
                self.audit.log(tool_name, arguments, 'log', f'error: {e}')
                raise

        # fallback: 放行
        return await call_next(context)


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

async def remove_blocked_tools(mcp: FastMCP, sec_config: SecurityConfig):
    """
    从工具列表中移除被 block 的工具（让客户端完全看不到）。
    即使移除失败，SecurityMiddleware 仍会在调用时拦截 block 工具。
    """
    from fastmcp.exceptions import NotFoundError

    tools = await mcp.list_tools()
    removed = 0
    for tool in tools:
        action = sec_config.get_action(tool.name)
        if action == 'block':
            try:
                mcp._local_provider.remove_tool(tool.name)
                removed += 1
                logger.info(f'  🔒 BLOCK   {tool.name}')
            except (NotFoundError, KeyError):
                removed += 1
                logger.info(f'  🔒 BLOCK (middleware-only)   {tool.name}')
    return removed


async def setup_and_run():
    """异步启动流程"""

    # 1. 加载安全配置
    logger.info(f'加载安全配置: {SECURITY_CONFIG_PATH}')
    sec_config = SecurityConfig(SECURITY_CONFIG_PATH)

    # 2. 拉取并修复 OpenAPI Spec
    logger.info(f'获取 Spec: {SPEC_URL}')
    resp = httpx.get(SPEC_URL, headers={'token': DS_TOKEN}, timeout=15.0)
    resp.raise_for_status()
    spec = resp.json()
    logger.info(f'Spec 获取成功: {len(spec.get("paths", {}))} 个路径')

    spec = fix_openapi_spec(spec)
    spec['servers'] = [{'url': BASE_URL}]

    # 3. 创建 HTTP 客户端
    async_client = httpx.AsyncClient(
        base_url=BASE_URL,
        headers={'token': DS_TOKEN},
        timeout=30.0,
    )

    # 4. 创建 MCP Server
    logger.info('创建 MCP Server...')
    mcp = FastMCP.from_openapi(
        openapi_spec=spec,
        client=async_client,
        name='DolphinScheduler API Server (Secure)',
    )

    # 5. 注入安全中间件
    if sec_config.enabled:
        logger.info('注入安全中间件...')
        security_mw = SecurityMiddleware(sec_config, mcp_server=mcp)
        mcp.add_middleware(security_mw)

        # 设置事件循环引用（供 ConfirmServer 跨线程执行 async executor）
        event_loop = asyncio.get_event_loop()
        security_mw.confirm_queue.set_event_loop(event_loop)

        # 6. 移除被屏蔽的工具（客户端不可见）
        removed = await remove_blocked_tools(mcp, sec_config)

        # 7. 启动确认 Web 服务（后台线程）
        start_confirm_server(
            security_mw.confirm_queue,
            port=sec_config.confirm_listen_port,
        )
        logger.info(f'确认服务已启动: {sec_config.confirm_base_url}')

        tools = await mcp.list_tools()
        logger.info(f'剩余可用工具: {len(tools)} 个 (屏蔽: {removed})')
    else:
        logger.warning('⚠️ 安全模块未启用')

    # 8. 启动 MCP Server
    logger.info(f'MCP Server 启动: {HOST}:{PORT} (SSE)')
    await mcp.run_async(transport='sse', host=HOST, port=PORT)


def main():
    try:
        asyncio.run(setup_and_run())
    except Exception as e:
        logger.error(f'启动失败: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
