# en-blog —— 发布镜像：Hugo 构建 + nginx 托管
#
# ⚠️ 公开仓库文件：不得出现主机地址、内网路径、账号 / 密钥。
# 部署侧信息（目标主机、registry 地址、端口映射、SSH）全部在私有仓库 hancic128/app-deploy。
#
# 自包含构建（本地不需要装 hugo）：
#   docker build -t en-blog:local .
#   docker run --rm -p 8080:80 en-blog:local
#
# CI：.github/workflows/deploy.yml 在 v* tag 上构建同一份镜像、冒烟测试后推 ghcr，
# 再由 app-deploy 搬运到 ACR 并部署。

ARG HUGO_VERSION=0.166.0

FROM alpine:3.21 AS build
ARG HUGO_VERSION

# 直接用上游 release 的二进制，版本与本地开发环境（hugo extended）严格一致。
# 不走第三方 hugo 镜像：镜像里的 hugo 版本一旦和本地漂移，构建结果就会开始出现
# "本地对、线上错" 这种最难查的差异。
RUN apk add --no-cache ca-certificates curl tar \
 && curl -fsSL "https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/hugo_extended_${HUGO_VERSION}_linux-amd64.tar.gz" \
      -o /tmp/hugo.tar.gz \
 && tar -xzf /tmp/hugo.tar.gz -C /usr/local/bin hugo \
 && rm /tmp/hugo.tar.gz \
 && hugo version

WORKDIR /src
COPY . .

# 产物写到 /out，不去污染源码树里的 public/（.dockerignore 已排除它，这里是第二道保险）
RUN hugo --minify --gc --destination /out

FROM nginx:1.27-alpine

# 清掉基础镜像自带的示例页，再铺 Hugo 产物：镜像里只应该有本站的内容
RUN rm -rf /usr/share/nginx/html/*
COPY --from=build /out/ /usr/share/nginx/html/
COPY nginx/static.conf /etc/nginx/conf.d/default.conf
