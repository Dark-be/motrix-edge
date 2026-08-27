FROM ubuntu:22.04

# 设置环境变量，避免安装时的交互式提示
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y \
    tzdata \
    wget \
    curl \
    ca-certificates \
    can-utils \
    iproute2 \
    python3-pip \
    libusb-1.0-0 \
    ethtool \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh

WORKDIR /root/workspace/

CMD ["bash"]
