# DolphinScheduler MCP Server 部署指南

## 一、前置准备

### 1. 创建环境变量文件

```bash
cp .env.example .env
```

编辑 `.env`，填入实际配置：

```env
DS_BASE_URL=http://124.223.205.131:12346
DS_TOKEN=your-actual-token
MCP_CONFIRM_BASE_URL=http://124.223.205.131:32087
```

> **MCP_CONFIRM_BASE_URL** 是用户浏览器访问确认页面的地址，需要填部署机器的实际 IP。

---

## 二、Docker Compose 部署（单机推荐）

### 目录结构

```
dolphin-mcp-server/
├── .env                          # 环境变量（不提交 Git）
├── .env.example                  # 环境变量模板
├── docker-compose.yaml           # Compose 编排文件
├── Dockerfile                    # 镜像构建
├── dolphin_mcp_server_secure.py  # MCP 主服务
├── confirm_server.py             # 确认页面服务
└── mcp_security.yaml             # 安全策略配置
```

### 部署流程

```bash
# 1. 克隆项目
git clone https://github.com/imushan/dolphin-mcp-server.git
cd dolphin-mcp-server

# 2. 创建并编辑环境变量
cp .env.example .env
vi .env

# 3. 构建并启动
docker compose up -d --build

# 4. 查看日志
docker compose logs -f

# 5. 停止服务
docker compose down
```

### 验证服务

| 服务 | 地址 | 说明 |
|---|---|---|
| MCP SSE | `http://<IP>:32086/sse` | 大模型连接此地址 |
| 确认页面 | `http://<IP>:32087/confirm/<id>` | 浏览器确认操作 |

### 常用命令

```bash
# 重启
docker compose restart

# 重新构建（代码更新后）
docker compose up -d --build

# 查看容器状态
docker compose ps
```

---

## 三、K8s 部署（集群推荐）

### 前提条件

- K8s 集群（k3s / 标准 K8s 均可）
- 可访问的容器镜像仓库

### 部署流程

```bash
# 1. 克隆项目
git clone https://github.com/imushan/dolphin-mcp-server.git
cd dolphin-mcp-server

# 2. 创建 Secret（存放 Token，不要提交到 Git）
kubectl create secret generic dolphin-mcp-secrets \
  --from-literal=ds-token=your-actual-token

# 3. 修改 k8s-deployment.yaml 中的配置
#    - DS_BASE_URL: DolphinScheduler API 地址
#    - MCP_CONFIRM_BASE_URL: 节点 IP + 端口 32087
#    - image: 你的镜像仓库地址
vi k8s-deployment.yaml

# 4. 构建并推送镜像
docker build -t your-registry/dolphin-mcp:latest .
docker push your-registry/dolphin-mcp:latest

# 5. 部署
kubectl apply -f k8s-deployment.yaml

# 6. 查看状态
kubectl get pods -l app=dolphin-mcp-adapter
kubectl logs -f deployment/dolphin-mcp-adapter
```

### 验证服务

| 服务 | 地址 | 说明 |
|---|---|---|
| MCP SSE | `http://<NODE_IP>:32086/sse` | 大模型连接此地址 |
| 确认页面 | `http://<NODE_IP>:32087/confirm/<id>` | 浏览器确认操作 |

### 与已有 mcp-adapter 共存

本项目使用独立命名，不会与集群中已有的 `mcp-adapter` 冲突：

| 资源 | 已有服务 | 本项目 |
|---|---|---|
| Deployment | `mcp-adapter` | `dolphin-mcp-adapter` |
| MCP NodePort | 32084 | 32086 |
| 确认 NodePort | — | 32087 |

### 常用命令

```bash
# 查看部署状态
kubectl get all -l app=dolphin-mcp-adapter

# 查看日志
kubectl logs -f deployment/dolphin-mcp-adapter

# 更新部署（更新镜像后）
kubectl rollout restart deployment/dolphin-mcp-adapter

# 删除部署
kubectl delete -f k8s-deployment.yaml
kubectl delete secret dolphin-mcp-secrets
```

---

## 四、环境变量说明

| 变量名 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `DS_BASE_URL` | ✅ | `http://localhost:12346` | DolphinScheduler API 地址 |
| `DS_TOKEN` | ✅ | 空 | DolphinScheduler 认证 Token |
| `MCP_HOST` | ❌ | `0.0.0.0` | MCP 服务监听地址 |
| `MCP_PORT` | ❌ | `3000` | MCP 服务监听端口 |
| `MCP_CONFIRM_BASE_URL` | ✅ | `http://localhost:8080` | 确认页面访问地址（浏览器可访问） |
| `MCP_CONFIRM_PORT` | ❌ | `8080` | 确认服务监听端口 |
| `MCP_CONFIRM_EXPIRE` | ❌ | `300` | 确认请求过期时间（秒） |

---

## 五、安全策略说明

安全策略通过 `mcp_security.yaml` 配置：

- **allow** — 只读查询，直接放行
- **log** — 创建/更新类，记录日志后执行
- **confirm** — 删除/执行等敏感操作，需要浏览器确认
- **block** — 始终屏蔽（如 handleError*）

修改 `mcp_security.yaml` 后需要重新构建镜像（Docker Compose 用 `docker compose up -d --build`）。
