FROM python:3.11-slim

WORKDIR /app

# 安装依赖（使用清华镜像加速）
RUN pip install --no-cache-dir fastmcp httpx PyYAML -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制 Python 源文件和配置
COPY dolphin_mcp_server_secure.py .
COPY confirm_server.py .
COPY mcp_security.yaml .

# MCP SSE 服务端口 + 确认页面端口
EXPOSE 3000 8080

# 直接运行 Python 文件
CMD ["python3", "dolphin_mcp_server_secure.py"]
