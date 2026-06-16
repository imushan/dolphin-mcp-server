#!/usr/bin/env python3
"""
MCP 协议层测试客户端 — 模拟不同客户端(hermes/copenclaw)连接远程 SSE,
抓取 tools/call 的原始返回,用于诊断为何删除操作在某些客户端失败。

不会真正删除数据:delete 工具被中间件拦截,只返回确认链接。
"""

import asyncio
import json
import sys

from fastmcp import Client

SSE_URL = "http://localhost:32086/sse"


async def main():
    print(f"=== 连接 SSE: {SSE_URL} ===", flush=True)
    client = Client(SSE_URL)

    try:
        async with client:
            # 1) 列出工具
            print("\n[1] list_tools ...", flush=True)
            tools = await client.list_tools()
            print(f"    工具总数: {len(tools)}", flush=True)

            # 找删除相关工具
            delete_tools = [t for t in tools if 'delete' in t.name.lower() or 'del' in t.name.lower()]
            print(f"    delete 相关工具: {len(delete_tools)}", flush=True)
            for t in delete_tools[:8]:
                print(f"      - {t.name}", flush=True)

            # 找查询项目的工具(先列出项目拿 code)
            query_tools = [t for t in tools if 'project' in t.name.lower() and ('query' in t.name.lower() or 'list' in t.name.lower() or 'get' in t.name.lower())]
            print(f"    项目查询相关工具:", flush=True)
            for t in query_tools[:8]:
                print(f"      - {t.name}", flush=True)

            # 2) 详细打印 deleteProject 工具定义(看 schema)
            for t in tools:
                if t.name == 'deleteProject':
                    print(f"\n[2] deleteProject 工具定义:", flush=True)
                    print(json.dumps(t.model_dump() if hasattr(t, 'model_dump') else dict(t), ensure_ascii=False, indent=2, default=str), flush=True)
                    break

            # 3) 尝试先查项目列表(只读,allow 放行)拿"测试项目"的 code
            project_code = None
            for t in query_tools:
                if t.name in ('queryProjectList', 'queryAllProjectList', 'queryProjectByCode', 'listProject'):
                    print(f"\n[3] 调用只读查询: {t.name}", flush=True)
                    try:
                        result = await client.call_tool(t.name, {})
                        print(f"    --- raw result type: {type(result).__name__} ---", flush=True)
                        print(f"    --- result repr (前800字符) ---", flush=True)
                        print(repr(result)[:800], flush=True)
                        # 尝试从文本里找"测试项目"和 code
                        try:
                            txt = result.content[0].text if result.content else ''
                        except Exception:
                            txt = str(result)
                        if '测试项目' in txt:
                            print("    >>> 文本中包含 '测试项目'", flush=True)
                            # 简单提取可能的 code
                            import re
                            m = re.search(r'"code"\s*[:=]\s*"?(\d+)"?', txt)
                            if m:
                                project_code = m.group(1)
                                print(f"    >>> 疑似 project code: {project_code}", flush=True)
                        break
                    except Exception as e:
                        print(f"    调用失败: {e}", flush=True)

            # 4) 调用 deleteProject —— 重点:抓取中间件返回的原始协议数据
            #    用一个占位 code(即使查不到测试项目,也能验证中间件的 confirm 拦截行为)
            target_code = project_code or "1"
            print(f"\n[4] 调用 deleteProject (code={target_code}) ...", flush=True)
            print("    (预期:被中间件拦截,返回 is_error=True + 确认链接,不真正删除)", flush=True)
            try:
                result = await client.call_tool('deleteProject', {'code': target_code})
                print(f"    --- raw result type: {type(result).__name__} ---", flush=True)

                # 关键诊断字段
                is_error = getattr(result, 'is_error', None)
                structured = getattr(result, 'structured_content', None) or getattr(result, 'structuredContent', None)
                meta = getattr(result, 'meta', None)
                print(f"\n    [is_error]        = {is_error!r}", flush=True)
                print(f"    [structured_content] = {json.dumps(structured, ensure_ascii=False) if structured else None}", flush=True)
                print(f"    [meta]            = {meta!r}", flush=True)

                print(f"\n    [content blocks]:", flush=True)
                for i, block in enumerate(result.content or []):
                    btype = type(block).__name__
                    text = getattr(block, 'text', None)
                    print(f"      block[{i}] type={btype}", flush=True)
                    if text:
                        print(f"        text:\n{text}", flush=True)

                # 完整 dump
                print(f"\n    [full result dump]:", flush=True)
                try:
                    print(json.dumps(result.model_dump(mode='python'), ensure_ascii=False, indent=2, default=str), flush=True)
                except Exception as e:
                    print(f"    (model_dump 失败: {e})", flush=True)
                    print(repr(result), flush=True)

            except Exception as e:
                print(f"    ❌ 调用 deleteProject 抛出异常: {type(e).__name__}: {e}", flush=True)
                import traceback
                traceback.print_exc()

    except Exception as e:
        print(f"\n❌ 连接/会话失败: {type(e).__name__}: {e}", flush=True)
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(main())
