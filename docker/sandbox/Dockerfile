# Jify code_sandbox 强制沙箱镜像
# 预装 code_sandbox 支持的全部语言解释器。需提前 build，运行期不拉取。
#
#   docker build -t jify-code-sandbox:latest ./docker/sandbox
#   docker build -t debian:bookworm-slim-sandbox ./docker/sandbox   # 或自定义 tag，再用 JIFY_SANDBOX_IMAGE 指向
#
# 容器运行约束（由 code_sandbox.py 的 CONTAINER_BASE 指定）：
#   --network=none --read-only --user=1000:1000 --memory=512m --cpus=1.0
# 因此镜像内必须存在 uid/gid 1000 的非 root 用户。

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# 替换为阿里云镜像源加速（官方源 deb.debian.org 在国内网络下不稳定/超时）
# 同时禁用代理环境变量，防止容器内无法连接外部源
RUN unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy && \
    sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list

# 安装系统包 & 各语言解释器
RUN unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy && \
    apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    php-cli \
    ruby \
    nodejs \
    npm \
    golang \
    default-jdk-headless \
    bash \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户（uid/gid 1000，与 code_sandbox 默认 --user 1000:1000 对应）
RUN groupadd -g 1000 sandbox && \
    useradd -u 1000 -g sandbox -m -s /bin/bash sandbox

# 工作区
RUN mkdir -p /workspace && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace