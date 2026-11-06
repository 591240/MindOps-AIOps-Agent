# MindOps 前端生产镜像：Vite 构建 → Nginx 静态托管 + API/SSE 反代。
FROM node:22-alpine AS build

WORKDIR /app

# 项目使用 pnpm（pnpm-lock.yaml lockfileVersion 9）
RUN npm install -g pnpm@9

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

# 同源反代部署：API 基础路径必须是相对路径，否则产物会写死 localhost
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN pnpm build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
