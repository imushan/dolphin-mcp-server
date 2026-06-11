#!/bin/bash
# =============================================================================
# DolphinScheduler MCP Adapter — 构建与部署脚本
# =============================================================================
set -e

REGISTRY="${REGISTRY:-your-registry/dolphin-mcp}"
IMAGE_NAME="dolphin-mcp"
IMAGE_TAG="latest"
FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "============================================="
echo "  DolphinScheduler MCP Adapter 部署脚本"
echo "============================================="

# ─── 步骤 1: 构建镜像 ────────────────────────────────
echo ""
echo "📦 [1/3] 构建 Docker 镜像: ${FULL_IMAGE}"
docker build -t "${FULL_IMAGE}" .

# ─── 步骤 2: 推送镜像 ────────────────────────────────
echo ""
echo "📤 [2/3] 推送镜像到仓库..."
docker push "${FULL_IMAGE}"

# ─── 步骤 3: 部署到 K8s ──────────────────────────────
echo ""
echo "🚀 [3/3] 部署到 Kubernetes..."
kubectl apply -f k8s-deployment.yaml

echo ""
echo "✅ 部署完成！"
echo ""
echo "📋 常用命令:"
echo "   查看状态:  kubectl get pods -l app=dolphin-mcp-adapter"
echo "   查看日志:  kubectl logs -f deployment/dolphin-mcp-adapter"
echo "   MCP 地址:  http://<NODE_IP>:32086/sse"
echo "   确认页面:  http://<NODE_IP>:32087/confirm/<id>"
echo ""
echo "⚠️  记得修改 k8s-deployment.yaml 中 ConfigMap 的 base_url 为实际节点 IP"
